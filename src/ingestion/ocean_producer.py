"""
ocean_producer.py — OceanTrust AI
==================================
Async Redpanda producer for salmon farm IoT sensor telemetry.

Publishes SENSOR_TELEMETRY events to the `ocean.telemetry.v1` topic using
`farm_id` as the Kafka partition key. All events per farm land on the same
partition, guaranteeing strict per-farm ordering and preventing false alerts
from out-of-order readings.

Topic:         ocean.telemetry.v1
Schema:        schemas/iot_sensor_event.schema.json  v1.0.0
Partition key: farm_id  (e.g. "NO-FARM-0047")
Serialization: JSON — UTF-8
Compression:   Snappy
Timestamps:    ISO 8601 with UTC offset (+00:00)
Cadence:       30 s per sensor unit (Architect spec — kafka-topics.md §3)
Retry policy:  Exponential backoff — base 1 s, multiplier 2x, cap 60 s

Usage:
    python -m src.ingestion.ocean_producer

Environment variables (see .env.example):
    KAFKA_BOOTSTRAP_SERVERS   — Broker address (default: localhost:9092)
    KAFKA_CLIENT_ID           — Producer identifier
    TELEMETRY_TOPIC           — Target topic (default: ocean.telemetry.v1)
    EMIT_INTERVAL_SECONDS     — Cadence between readings (default: 30)
    FARMS_CONFIG_PATH         — Path to the JSON farm registry
    LOG_LEVEL                 — Logging verbosity (default: INFO)
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import structlog
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaConnectionError, KafkaError, KafkaTimeoutError
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Retry policy constants
# ---------------------------------------------------------------------------

_RETRY_BASE_DELAY_S: Final[float] = 1.0   # Initial backoff delay in seconds
_RETRY_MULTIPLIER: Final[float] = 2.0     # Exponential growth factor
_RETRY_MAX_DELAY_S: Final[float] = 60.0   # Upper bound per attempt
_RETRY_MAX_ATTEMPTS: Final[int] = 5       # Max attempts before raising

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class ProducerSettings(BaseSettings):
    """
    Runtime configuration loaded from environment variables or a .env file.
    All fields map 1-to-1 with the variables defined in .env.example.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kafka_bootstrap_servers: str = Field(
        default="localhost:9092",
        alias="KAFKA_BOOTSTRAP_SERVERS",
    )
    kafka_client_id: str = Field(
        default="ocean-telemetry-producer",
        alias="KAFKA_CLIENT_ID",
    )
    telemetry_topic: str = Field(
        default="ocean.telemetry.v1",
        alias="TELEMETRY_TOPIC",
    )
    emit_interval_seconds: float = Field(
        default=30.0,
        alias="EMIT_INTERVAL_SECONDS",
    )
    farms_config_path: str = Field(
        default="config/farms_config.json",
        alias="FARMS_CONFIG_PATH",
    )
    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging(log_level: str) -> structlog.stdlib.BoundLogger:
    """
    Configure structlog with JSON processors and return a service-bound logger.

    The log level is resolved against the stdlib logging module so that
    string values such as "DEBUG" or "INFO" map to their integer constants.
    """
    import logging as _logging

    numeric_level: int = getattr(_logging, log_level.upper(), _logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )
    return structlog.get_logger().bind(service="ocean-telemetry-producer")


# ---------------------------------------------------------------------------
# Domain Enums (mirrors iot_sensor_event.schema.json)
# ---------------------------------------------------------------------------


class DeviceType(StrEnum):
    MULTIPARAMETER_PROBE = "MULTIPARAMETER_PROBE"
    CAMERA_UNIT = "CAMERA_UNIT"
    FEEDING_SENSOR = "FEEDING_SENSOR"
    LICE_DETECTOR = "LICE_DETECTOR"
    CURRENT_METER = "CURRENT_METER"


class Region(StrEnum):
    NORWAY = "NORWAY"
    SCOTLAND = "SCOTLAND"
    CHILE = "CHILE"
    CANADA = "CANADA"
    FAROE_ISLANDS = "FAROE_ISLANDS"
    IRELAND = "IRELAND"
    ICELAND = "ICELAND"


class WaterBody(StrEnum):
    FJORD = "FJORD"
    OPEN_SEA = "OPEN_SEA"
    COASTAL = "COASTAL"
    INLAND_LAKE = "INLAND_LAKE"


class SensorStatus(StrEnum):
    NOMINAL = "NOMINAL"
    DEGRADED = "DEGRADED"
    FAULTY = "FAULTY"
    CALIBRATING = "CALIBRATING"
    OFFLINE = "OFFLINE"


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertCode(StrEnum):
    O2_LOW_WARNING = "O2_LOW_WARNING"
    O2_CRITICAL = "O2_CRITICAL"
    TEMP_HIGH_WARNING = "TEMP_HIGH_WARNING"
    TEMP_CRITICAL = "TEMP_CRITICAL"
    LICE_THRESHOLD_BREACH = "LICE_THRESHOLD_BREACH"
    ALGAE_BLOOM_DETECTED = "ALGAE_BLOOM_DETECTED"
    HIGH_MORTALITY = "HIGH_MORTALITY"
    SENSOR_FAULT = "SENSOR_FAULT"
    HIGH_TURBIDITY = "HIGH_TURBIDITY"
    PH_OUT_OF_RANGE = "PH_OUT_OF_RANGE"
    LOW_SALINITY = "LOW_SALINITY"
    HIGH_SALINITY = "HIGH_SALINITY"


# ---------------------------------------------------------------------------
# Pydantic Models (strict mirror of iot_sensor_event.schema.json v1.0.0)
# ---------------------------------------------------------------------------


class DeviceProducer(BaseModel):
    device_id: str
    device_type: DeviceType
    firmware_version: str
    manufacturer: str


class Coordinates(BaseModel):
    latitude: float
    longitude: float
    depth_meters: float


class Location(BaseModel):
    farm_id: str
    farm_name: str
    cage_id: str
    coordinates: Coordinates
    region: Region
    water_body: WaterBody


class WaterQuality(BaseModel):
    temperature_celsius: float | None
    dissolved_oxygen_mg_l: float | None
    oxygen_saturation_percent: float | None
    salinity_ppt: float | None
    ph: float | None
    turbidity_ntu: float | None
    chlorophyll_a_ug_l: float | None


class Biological(BaseModel):
    fish_count_estimate: int | None
    avg_biomass_kg: float | None
    feeding_rate_kg_per_hour: float | None
    lice_count_per_fish: float | None
    mortality_count_24h: int | None


class Environment(BaseModel):
    current_speed_m_s: float | None
    current_direction_degrees: float | None
    algae_bloom_detected: bool
    algae_bloom_type: str | None


class Alert(BaseModel):
    alert_code: AlertCode
    severity: AlertSeverity
    threshold_breached: float
    current_value: float
    message: str


class DataQuality(BaseModel):
    signal_strength_dbm: int | None
    battery_level_percent: float | None
    sensor_status: SensorStatus
    missing_fields: list[str]


class SensorTelemetryEvent(BaseModel):
    """
    Canonical event envelope for IoT sensor readings.

    All fields match iot_sensor_event.schema.json v1.0.0 exactly.
    The timestamp field is always set to the current UTC instant in
    ISO 8601 format with offset '+00:00', as required by the Architect's
    data contracts (data-schemas.md §1).
    """

    schema_version: str = "1.0.0"
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = "SENSOR_TELEMETRY"
    # ISO 8601 with explicit UTC offset — never naive datetime
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )
    producer: DeviceProducer
    location: Location
    water_quality: WaterQuality
    biological: Biological
    environment: Environment
    alerts: list[Alert]
    data_quality: DataQuality


# ---------------------------------------------------------------------------
# Farm Config Loader
# ---------------------------------------------------------------------------


def load_farm_registry(config_path: str) -> list[dict[str, Any]]:
    """
    Load and return the list of farm definitions from an external JSON file.

    Args:
        config_path: Relative or absolute path to the farms_config.json file.

    Returns:
        A list of raw farm configuration dictionaries.

    Raises:
        FileNotFoundError: If the config file does not exist at the given path.
        ValueError: If the JSON is malformed or missing the 'farms' key.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Farm registry not found at '{path.resolve()}'. "
            "Set FARMS_CONFIG_PATH or ensure config/farms_config.json exists."
        )

    raw = json.loads(path.read_text(encoding="utf-8"))

    if "farms" not in raw:
        raise ValueError(
            f"Invalid farm registry: missing top-level 'farms' key in '{path}'."
        )

    return list(raw["farms"])


# ---------------------------------------------------------------------------
# Farm Simulator
# ---------------------------------------------------------------------------


class FarmSimulator:
    """
    Generates realistic, randomised sensor readings for a single farm node.

    Readings are anchored to per-farm baseline values defined in
    `config/farms_config.json` and fluctuate with Gaussian noise.

    Alert detection mirrors all thresholds from kafka-topics.md §3.5.
    """

    def __init__(self, farm_config: dict[str, Any]) -> None:
        self._config = farm_config
        self._baseline: dict[str, float] = farm_config.get("baseline", {})

    @property
    def farm_id(self) -> str:
        return str(self._config["farm_id"])

    def generate_event(self) -> SensorTelemetryEvent:
        """Build and return a fully populated, randomised telemetry event."""
        water_quality = self._generate_water_quality()
        biological = self._generate_biological()
        environment = self._generate_environment()
        alerts = self._detect_alerts(water_quality, biological, environment)

        return SensorTelemetryEvent(
            producer=self._build_device_producer(),
            location=self._build_location(),
            water_quality=water_quality,
            biological=biological,
            environment=environment,
            alerts=alerts,
            data_quality=self._generate_data_quality(),
        )

    # ------------------------------------------------------------------
    # Private construction helpers
    # ------------------------------------------------------------------

    def _build_device_producer(self) -> DeviceProducer:
        cfg = self._config
        country = cfg["country_iso"]
        farm_num = cfg["farm_num"]
        cage = cfg["cage_id"].replace("-", "")
        return DeviceProducer(
            device_id=f"SENSOR-{country}-FARM{farm_num}-{cage}-UNIT01",
            device_type=DeviceType.MULTIPARAMETER_PROBE,
            firmware_version="3.2.1",
            manufacturer="AquaSense",
        )

    def _build_location(self) -> Location:
        cfg = self._config
        coords = cfg["coordinates"]
        return Location(
            farm_id=cfg["farm_id"],
            farm_name=cfg["farm_name"],
            cage_id=cfg["cage_id"],
            coordinates=Coordinates(
                latitude=float(coords["latitude"]),
                longitude=float(coords["longitude"]),
                depth_meters=float(coords["depth_meters"]),
            ),
            region=Region(cfg["region"]),
            water_body=WaterBody(cfg["water_body"]),
        )

    def _generate_water_quality(self) -> WaterQuality:
        """Apply Gaussian noise around each farm's baseline readings."""
        b = self._baseline
        return WaterQuality(
            temperature_celsius=round(random.gauss(b.get("temperature_celsius", 12.0), 1.2), 3),
            dissolved_oxygen_mg_l=round(random.gauss(b.get("dissolved_oxygen_mg_l", 10.5), 0.7), 3),
            oxygen_saturation_percent=round(random.gauss(91.0, 3.0), 3),
            salinity_ppt=round(random.gauss(b.get("salinity_ppt", 33.0), 0.4), 3),
            ph=round(random.gauss(b.get("ph", 7.80), 0.08), 3),
            turbidity_ntu=round(max(0.0, random.gauss(b.get("turbidity_ntu", 1.5), 0.4)), 3),
            chlorophyll_a_ug_l=round(
                max(0.0, random.gauss(b.get("chlorophyll_a_ug_l", 2.0), 0.9)), 3
            ),
        )

    @staticmethod
    def _generate_biological() -> Biological:
        return Biological(
            fish_count_estimate=random.randint(4_000, 6_200),
            avg_biomass_kg=round(random.gauss(3.8, 0.35), 3),
            feeding_rate_kg_per_hour=round(max(0.0, random.gauss(42.0, 4.5)), 3),
            lice_count_per_fish=round(max(0.0, random.gauss(0.15, 0.07)), 3),
            mortality_count_24h=random.randint(0, 10),
        )

    @staticmethod
    def _generate_environment() -> Environment:
        algae_detected = random.random() < 0.03  # 3 % baseline bloom probability
        return Environment(
            current_speed_m_s=round(max(0.0, random.gauss(0.25, 0.07)), 3),
            current_direction_degrees=round(random.uniform(0.0, 360.0), 1),
            algae_bloom_detected=algae_detected,
            algae_bloom_type=None,
        )

    @staticmethod
    def _detect_alerts(
        water: WaterQuality,
        bio: Biological,
        env: Environment,
    ) -> list[Alert]:
        """
        Evaluate readings against the threshold table in kafka-topics.md §3.5.
        Returns only the alerts that are actually triggered in this cycle.
        """
        triggered: list[Alert] = []

        # ── Dissolved oxygen ─────────────────────────────────────────────
        do = water.dissolved_oxygen_mg_l
        if do is not None:
            if do < 7.0:
                triggered.append(Alert(
                    alert_code=AlertCode.O2_CRITICAL,
                    severity=AlertSeverity.CRITICAL,
                    threshold_breached=7.0,
                    current_value=do,
                    message=f"Dissolved oxygen critically low: {do:.3f} mg/L (limit < 7.0)",
                ))
            elif do < 9.0:
                triggered.append(Alert(
                    alert_code=AlertCode.O2_LOW_WARNING,
                    severity=AlertSeverity.WARNING,
                    threshold_breached=9.0,
                    current_value=do,
                    message=f"Dissolved oxygen approaching lower threshold: {do:.3f} mg/L",
                ))

        # ── Temperature ──────────────────────────────────────────────────
        temp = water.temperature_celsius
        if temp is not None:
            if temp > 18.0:
                triggered.append(Alert(
                    alert_code=AlertCode.TEMP_CRITICAL,
                    severity=AlertSeverity.CRITICAL,
                    threshold_breached=18.0,
                    current_value=temp,
                    message=f"Water temperature critically high: {temp:.3f} °C (limit > 18.0)",
                ))
            elif temp > 16.0:
                triggered.append(Alert(
                    alert_code=AlertCode.TEMP_HIGH_WARNING,
                    severity=AlertSeverity.WARNING,
                    threshold_breached=16.0,
                    current_value=temp,
                    message=f"Water temperature elevated: {temp:.3f} °C (limit > 16.0)",
                ))

        # ── Sea lice — Norwegian regulatory limit: 0.5 per fish ──────────
        lice = bio.lice_count_per_fish
        if lice is not None and lice > 0.5:
            triggered.append(Alert(
                alert_code=AlertCode.LICE_THRESHOLD_BREACH,
                severity=AlertSeverity.CRITICAL,
                threshold_breached=0.5,
                current_value=lice,
                message=f"Sea lice exceeds regulatory limit: {lice:.3f} per fish",
            ))

        # ── Mortality ────────────────────────────────────────────────────
        mort = bio.mortality_count_24h
        if mort is not None and mort > 50:
            triggered.append(Alert(
                alert_code=AlertCode.HIGH_MORTALITY,
                severity=AlertSeverity.CRITICAL,
                threshold_breached=50.0,
                current_value=float(mort),
                message=f"High mortality event: {mort} fish removed in last 24 h",
            ))

        # ── Algae bloom ──────────────────────────────────────────────────
        chl = water.chlorophyll_a_ug_l
        if env.algae_bloom_detected or (chl is not None and chl > 10.0):
            triggered.append(Alert(
                alert_code=AlertCode.ALGAE_BLOOM_DETECTED,
                severity=AlertSeverity.WARNING,
                threshold_breached=10.0,
                current_value=chl if chl is not None else 0.0,
                message="Harmful algae bloom signal detected by edge classifier",
            ))

        return triggered

    @staticmethod
    def _generate_data_quality() -> DataQuality:
        return DataQuality(
            signal_strength_dbm=random.randint(-85, -50),
            battery_level_percent=round(random.uniform(60.0, 100.0), 1),
            sensor_status=SensorStatus.NOMINAL,
            missing_fields=[],
        )


# ---------------------------------------------------------------------------
# Exponential Backoff Retry
# ---------------------------------------------------------------------------


async def _publish_with_backoff(
    producer: AIOKafkaProducer,
    topic: str,
    key: str,
    value: dict[str, Any],
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """
    Attempt to publish a single message with exponential backoff on failure.

    Retries on transient broker errors (KafkaTimeoutError, KafkaConnectionError).
    The delay after attempt `n` (0-indexed) is:

        delay = min(base * multiplier^n, max_delay)

    Raises the underlying KafkaError after exhausting all retry attempts.

    Args:
        producer: A started AIOKafkaProducer instance.
        topic:    Target Kafka topic name.
        key:      Partition key (farm_id string).
        value:    Message payload as a plain Python dictionary.
        logger:   Bound structlog logger for emit-level tracing.
    """
    log = logger.bind(topic=topic, farm_id=key)
    last_error: KafkaError | None = None

    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            await producer.send_and_wait(topic=topic, key=key, value=value)
            return  # Success — exit immediately

        except (KafkaTimeoutError, KafkaConnectionError) as exc:
            last_error = exc
            delay = min(
                _RETRY_BASE_DELAY_S * math.pow(_RETRY_MULTIPLIER, attempt),
                _RETRY_MAX_DELAY_S,
            )
            log.warning(
                "publish_retry",
                attempt=attempt + 1,
                max_attempts=_RETRY_MAX_ATTEMPTS,
                retry_after_s=round(delay, 2),
                error=str(exc),
            )
            await asyncio.sleep(delay)

    log.error(
        "publish_failed_permanently",
        attempts=_RETRY_MAX_ATTEMPTS,
        error=str(last_error),
    )
    raise last_error  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Async Producer
# ---------------------------------------------------------------------------


class OceanTelemetryProducer:
    """
    Async Redpanda producer for salmon farm telemetry events.

    Wraps aiokafka.AIOKafkaProducer with:
    - GZIP compression (no native library required — available in stdlib)
    - farm_id-keyed messages for strict per-farm ordering
    - Exponential backoff retry on transient broker failures
    - Structured JSON logging via structlog

    Use as an async context manager to ensure clean lifecycle management:

        async with OceanTelemetryProducer(settings, logger) as producer:
            await producer.publish(event)
    """

    def __init__(
        self,
        settings: ProducerSettings,
        logger: structlog.stdlib.BoundLogger,
    ) -> None:
        self._settings = settings
        self._log = logger.bind(topic=settings.telemetry_topic)
        self._producer: AIOKafkaProducer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise and start the underlying AIOKafkaProducer."""
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            client_id=self._settings.kafka_client_id,
            # acks="all" requires acknowledgement from the leader and all ISRs
            acks="all",
            # GZIP compression: available in Python stdlib — no extra native lib needed.
            # Matches the compression.type accepted by Redpanda in dev mode.
            compression_type="gzip",
            # Serialise values to UTF-8 JSON bytes at the producer level
            value_serializer=lambda payload: json.dumps(payload).encode("utf-8"),
            # Encode the string partition key (farm_id) to bytes
            key_serializer=lambda k: k.encode("utf-8") if k else None,
            # Batch up to 5 ms before flushing — reduces broker round-trips
            linger_ms=5,
            request_timeout_ms=30_000,
        )
        await self._producer.start()
        self._log.info(
            "producer_started",
            bootstrap=self._settings.kafka_bootstrap_servers,
            compression="gzip",
        )

    async def stop(self) -> None:
        """Flush in-flight messages and close the broker connection."""
        if self._producer is not None:
            await self._producer.stop()
            self._log.info("producer_stopped")

    async def __aenter__(self) -> OceanTelemetryProducer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: SensorTelemetryEvent) -> None:
        """
        Publish one telemetry event to the configured topic.

        The Kafka partition key is `event.location.farm_id` — an opaque
        string that the broker hashes with Murmur2 to deterministically
        assign the message to a partition. All events from the same farm
        are always routed to the same partition, preserving insertion order.

        Args:
            event: A populated SensorTelemetryEvent instance.

        Raises:
            RuntimeError: If called outside the context manager / before start().
            KafkaError:   If all retry attempts are exhausted.
        """
        if self._producer is None:
            raise RuntimeError(
                "Producer is not started. "
                "Use 'async with OceanTelemetryProducer(...)' or call start() explicitly."
            )

        partition_key: str = event.location.farm_id
        payload: dict[str, Any] = event.model_dump(mode="json")

        await _publish_with_backoff(
            producer=self._producer,
            topic=self._settings.telemetry_topic,
            key=partition_key,
            value=payload,
            logger=self._log,
        )

        self._log.info(
            "event_published",
            farm_id=partition_key,
            event_id=event.event_id,
            timestamp=event.timestamp,
            alert_count=len(event.alerts),
        )


# ---------------------------------------------------------------------------
# Simulation Loop
# ---------------------------------------------------------------------------


async def run_simulation(
    settings: ProducerSettings,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """
    Continuously emit telemetry events for all registered farm nodes.

    Farm definitions are loaded once from the external JSON registry
    (FARMS_CONFIG_PATH). Each iteration publishes one event per farm and
    then sleeps for `emit_interval_seconds`. Runs until cancelled.

    Args:
        settings: Typed runtime configuration.
        logger:   Pre-configured structlog bound logger.
    """
    farm_registry = load_farm_registry(settings.farms_config_path)
    simulators = [FarmSimulator(cfg) for cfg in farm_registry]

    logger.info(
        "simulation_started",
        farm_count=len(simulators),
        farm_ids=[s.farm_id for s in simulators],
        topic=settings.telemetry_topic,
        interval_seconds=settings.emit_interval_seconds,
    )

    async with OceanTelemetryProducer(settings=settings, logger=logger) as producer:
        while True:
            for simulator in simulators:
                try:
                    event = simulator.generate_event()
                    await producer.publish(event)
                except KafkaError as exc:
                    # All retries exhausted — log and continue to next farm
                    # to avoid a single bad partition blocking the whole loop.
                    logger.error(
                        "farm_skipped_after_retries",
                        farm_id=simulator.farm_id,
                        error=str(exc),
                    )

            logger.debug(
                "emit_cycle_complete",
                farms_emitted=len(simulators),
            )
            await asyncio.sleep(settings.emit_interval_seconds)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """Configure environment and launch the async simulation loop."""
    settings = ProducerSettings()
    logger = configure_logging(settings.log_level)

    try:
        asyncio.run(run_simulation(settings, logger))
    except KeyboardInterrupt:
        logger.info("simulation_stopped", reason="KeyboardInterrupt")


if __name__ == "__main__":
    main()
