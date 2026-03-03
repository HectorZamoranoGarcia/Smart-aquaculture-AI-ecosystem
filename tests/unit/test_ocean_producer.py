"""
test_ocean_producer.py — Unit Tests
===================================
Tests core logic, configuration defaults, payload serialisation, and
exponential backoff retry behaviour without requiring a live Redpanda broker.
Uses `pytest-mock` to patch `aiokafka.AIOKafkaProducer`.
"""

from __future__ import annotations

import json
import re
import uuid
from unittest.mock import AsyncMock

import pytest
from aiokafka.errors import KafkaTimeoutError
from structlog.testing import capture_logs

from src.ingestion.ocean_producer import (
    FarmSimulator,
    OceanTelemetryProducer,
    ProducerSettings,
    _publish_with_backoff,
    configure_logging,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_logger():
    """Returns a structlog bound logger silenced at CRITICAL level."""
    return configure_logging("CRITICAL")


@pytest.fixture
def dummy_farm_config():
    """A minimal valid configuration for the FarmSimulator."""
    return {
        "farm_id": "NO-TEST-0001",
        "farm_name": "Test Farm Alpha",
        "cage_id": "CAGE-01",
        "country_iso": "NO",
        "farm_num": "0001",
        "coordinates": {"latitude": 60.0, "longitude": 6.0, "depth_meters": 5.0},
        "region": "NORWAY",
        "water_body": "FJORD",
        "baseline": {
            "temperature_celsius": 12.0,
            "dissolved_oxygen_mg_l": 10.5,
            "salinity_ppt": 33.0,
            "ph": 7.80,
            "turbidity_ntu": 1.5,
            "chlorophyll_a_ug_l": 2.0,
        },
    }


@pytest.fixture
def dummy_event(dummy_farm_config):
    """Generates a valid SensorTelemetryEvent from the test farm config."""
    sim = FarmSimulator(dummy_farm_config)
    return sim.generate_event()


@pytest.fixture
def standard_settings(tmp_path):
    """Provides standard ProducerSettings without requiring a real .env file."""
    config_file = tmp_path / "farms.json"
    config_file.write_text('{"farms": []}')
    return ProducerSettings(
        kafka_bootstrap_servers="mock:9092",
        kafka_client_id="test-client",
        telemetry_topic="test.topic",
        emit_interval_seconds=1.0,
        farms_config_path=str(config_file),
    )


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_event_iso8601_utc_timestamp(dummy_event):
    """
    Timestamps must follow ISO 8601 with an explicit +00:00 UTC offset.
    Naive datetimes or Z-suffix are not compliant with the Architect spec.
    """
    iso_utc_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
    assert iso_utc_pattern.match(dummy_event.timestamp), (
        f"Timestamp '{dummy_event.timestamp}' does not match expected format "
        "YYYY-MM-DDThh:mm:ss+00:00"
    )


@pytest.mark.unit
def test_event_id_is_valid_uuidv4(dummy_event):
    """Every event must carry a UUIDv4 as its idempotency / deduplication key."""
    parsed = uuid.UUID(dummy_event.event_id, version=4)
    assert str(parsed) == dummy_event.event_id


@pytest.mark.unit
def test_event_type_constant(dummy_event):
    """The event_type discriminator must always be SENSOR_TELEMETRY."""
    assert dummy_event.event_type == "SENSOR_TELEMETRY"


@pytest.mark.unit
def test_schema_version_semver(dummy_event):
    """schema_version must be a valid semantic version string."""
    semver_pattern = re.compile(r"^\d+\.\d+\.\d+$")
    assert semver_pattern.match(dummy_event.schema_version)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_producer_uses_snappy_compression_and_full_acks(
    mocker, standard_settings, mock_logger
):
    """
    AIOKafkaProducer MUST be instantiated with compression_type='snappy'
    and acks='all'. These settings are safety-critical for data durability.
    """
    mock_aiokafka = mocker.patch(
        "src.ingestion.ocean_producer.AIOKafkaProducer", autospec=True
    )

    producer = OceanTelemetryProducer(standard_settings, mock_logger)
    await producer.start()
    await producer.stop()

    mock_aiokafka.assert_called_once()
    _, kwargs = mock_aiokafka.call_args
    assert kwargs["compression_type"] == "snappy", "Compression must be snappy"
    assert kwargs["acks"] == "all", "Acknowledgement must require all in-sync replicas"
    assert kwargs["bootstrap_servers"] == "mock:9092"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_publish_uses_farm_id_as_partition_key(
    mocker, standard_settings, mock_logger, dummy_event
):
    """
    The partition key passed to send_and_wait must be farm_id.
    This drives the Murmur2 hash assignment for per-farm ordering.
    """
    mock_kafka_instance = AsyncMock()
    mocker.patch(
        "src.ingestion.ocean_producer.AIOKafkaProducer",
        return_value=mock_kafka_instance,
    )

    async with OceanTelemetryProducer(standard_settings, mock_logger) as producer:
        await producer.publish(dummy_event)

    mock_kafka_instance.send_and_wait.assert_awaited_once()
    _, kwargs = mock_kafka_instance.send_and_wait.call_args
    assert kwargs["key"] == dummy_event.location.farm_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_publish_value_is_json_serializable_dict(
    mocker, standard_settings, mock_logger, dummy_event
):
    """
    The message value must be a dict that is fully JSON-serializable.
    Pydantic Enum types must be unwrapped to their string values.
    """
    mock_kafka_instance = AsyncMock()
    mocker.patch(
        "src.ingestion.ocean_producer.AIOKafkaProducer",
        return_value=mock_kafka_instance,
    )

    async with OceanTelemetryProducer(standard_settings, mock_logger) as producer:
        await producer.publish(dummy_event)

    _, kwargs = mock_kafka_instance.send_and_wait.call_args
    payload = kwargs["value"]
    assert isinstance(payload, dict)

    # Must not raise — all types must be natively JSON-serializable
    serialized = json.dumps(payload)
    assert dummy_event.event_id in serialized
    assert dummy_event.location.farm_id in serialized


@pytest.mark.asyncio
@pytest.mark.unit
async def test_backoff_retries_five_times_then_raises(mocker, mock_logger):
    """
    _publish_with_backoff must retry exactly 5 times on persistent failure
    and then re-raise the KafkaTimeoutError to the caller.
    Sleep is mocked to keep the test suite instant.
    """
    mock_producer = AsyncMock()
    mock_producer.send_and_wait.side_effect = KafkaTimeoutError("Broker offline")
    mock_sleep = mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    with pytest.raises(KafkaTimeoutError):
        await _publish_with_backoff(
            producer=mock_producer,
            topic="test.topic",
            key="NO-TEST-0001",
            value={"test": 1},
            logger=mock_logger,
        )

    assert mock_producer.send_and_wait.call_count == 5
    assert mock_sleep.call_count == 5


@pytest.mark.asyncio
@pytest.mark.unit
async def test_backoff_delay_progression_is_exponential(mocker, mock_logger):
    """
    Sleep delays between retries must double each attempt:
    1.0s → 2.0s → 4.0s → 8.0s → 16.0s (capped at 60s max).
    """
    mock_producer = AsyncMock()
    mock_producer.send_and_wait.side_effect = KafkaTimeoutError("Timeout")
    mock_sleep = mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    with pytest.raises(KafkaTimeoutError):
        await _publish_with_backoff(
            producer=mock_producer,
            topic="test.topic",
            key="key",
            value={},
            logger=mock_logger,
        )

    actual_delays = [call[0][0] for call in mock_sleep.call_args_list]
    assert actual_delays == [1.0, 2.0, 4.0, 8.0, 16.0]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_backoff_recovers_on_intermediate_success(mocker, mock_logger):
    """
    If the broker recovers on the 3rd attempt, the backoff must return
    without raising, having slept exactly 2 times.
    """
    mock_producer = AsyncMock()
    mock_producer.send_and_wait.side_effect = [
        KafkaTimeoutError("Timeout 1"),
        KafkaTimeoutError("Timeout 2"),
        None,  # Success on attempt 3
    ]
    mock_sleep = mocker.patch("asyncio.sleep", new_callable=AsyncMock)

    # Must not raise
    await _publish_with_backoff(
        producer=mock_producer,
        topic="test.topic",
        key="key",
        value={},
        logger=mock_logger,
    )

    assert mock_producer.send_and_wait.call_count == 3
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_publish_raises_if_producer_not_started(mock_logger, standard_settings):
    """
    Calling publish() before start() must raise RuntimeError immediately,
    not a cryptic AttributeError from a None internal producer.
    """
    producer = OceanTelemetryProducer(standard_settings, mock_logger)
    # Intentionally not calling start()

    farm_cfg = {
        "farm_id": "NO-TEST-0001",
        "farm_name": "Test",
        "cage_id": "CAGE-01",
        "country_iso": "NO",
        "farm_num": "0001",
        "coordinates": {"latitude": 60.0, "longitude": 6.0, "depth_meters": 5.0},
        "region": "NORWAY",
        "water_body": "FJORD",
    }
    from src.ingestion.ocean_producer import FarmSimulator
    event = FarmSimulator(farm_cfg).generate_event()

    with pytest.raises(RuntimeError, match="Producer is not started"):
        await producer.publish(event)
