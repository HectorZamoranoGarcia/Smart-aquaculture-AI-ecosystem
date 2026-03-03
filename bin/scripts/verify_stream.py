#!/usr/bin/env python
"""
verify_stream.py — OceanTrust AI
==================================
Independent QA consumer for the ocean.telemetry.v1 stream.

Connects to the real Redpanda broker, consumes messages from the telemetry
topic, and validates every payload against the canonical
`schemas/iot_sensor_event.schema.json` using the jsonschema library.

Usage (requires: make up && make provision):
    python bin/scripts/verify_stream.py

Environment Variables:
    KAFKA_BOOTSTRAP_SERVERS   — default: localhost:9092
    TELEMETRY_TOPIC           — default: ocean.telemetry.v1
    LOG_LEVEL                 — default: INFO
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import structlog
from aiokafka import AIOKafkaConsumer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ],
)
log: structlog.stdlib.BoundLogger = structlog.get_logger("verify-stream")


# ---------------------------------------------------------------------------
# Schema loader
# ---------------------------------------------------------------------------


def load_canonical_schema() -> dict[str, Any]:
    """
    Resolve the canonical JSON schema from the project /schemas directory.
    The path is calculated relative to this script's location so it works
    regardless of the working directory.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    schema_path = project_root / "schemas" / "iot_sensor_event.schema.json"

    if not schema_path.exists():
        log.error("schema_not_found", path=str(schema_path))
        sys.exit(1)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    log.info("schema_loaded", schema_id=schema.get("$id"), version="1.0.0")
    return schema


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _highest_alert_severity(payload: dict[str, Any]) -> str:
    """Extract the highest severity level from the alerts array."""
    priority = {"CRITICAL": 3, "WARNING": 2, "INFO": 1}
    current_max = 0
    highest = "NONE"
    for alert in payload.get("alerts", []):
        sev = alert.get("severity", "INFO")
        if priority.get(sev, 0) > current_max:
            current_max = priority[sev]
            highest = sev
    return highest


def validate_payload(
    payload: dict[str, Any],
    schema: dict[str, Any],
    partition: int,
    offset: int,
) -> bool:
    """
    Validate a single message payload against the canonical schema.

    Returns True if valid, False if not. Logs the result either way.
    """
    event_id = payload.get("event_id", "UNKNOWN")
    farm_id = payload.get("location", {}).get("farm_id", "N/A")
    timestamp = payload.get("timestamp", "N/A")
    temp = payload.get("water_quality", {}).get("temperature_celsius")
    do_level = payload.get("water_quality", {}).get("dissolved_oxygen_mg_l")
    severity = _highest_alert_severity(payload)

    try:
        jsonschema.validate(instance=payload, schema=schema)

        log.info(
            "message_valid",
            event_id=event_id,
            farm_id=farm_id,
            timestamp=timestamp,
            temp_celsius=temp,
            do_mg_l=do_level,
            highest_severity=severity,
            partition=partition,
            offset=offset,
        )
        return True

    except jsonschema.ValidationError as exc:
        log.error(
            "schema_validation_failed",
            event_id=event_id,
            farm_id=farm_id,
            error=exc.message,
            failed_path=list(exc.path),
            partition=partition,
            offset=offset,
        )
        return False


# ---------------------------------------------------------------------------
# Main consumer loop
# ---------------------------------------------------------------------------


async def consume_and_verify(
    bootstrap_servers: str,
    topic: str,
    schema: dict[str, Any],
) -> None:
    """
    Continuously consume messages from the telemetry topic and validate each one.

    Uses a dedicated consumer group `verify-stream-qa` so validation traffic
    is fully isolated from production consumers.
    """
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        # Isolated QA group — does not interfere with production consumer groups
        group_id="verify-stream-qa",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
    )

    log.info(
        "verifier_starting",
        brokers=bootstrap_servers,
        topic=topic,
        msg="Consuming from offset=earliest. Press Ctrl+C to stop.",
    )

    await consumer.start()

    valid_count = 0
    invalid_count = 0

    try:
        async for msg in consumer:
            is_valid = validate_payload(
                payload=msg.value,
                schema=schema,
                partition=msg.partition,
                offset=msg.offset,
            )
            if is_valid:
                valid_count += 1
            else:
                invalid_count += 1

    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()
        log.info(
            "verification_complete",
            valid=valid_count,
            invalid=invalid_count,
            pass_rate_pct=round(valid_count / max(valid_count + invalid_count, 1) * 100, 2),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("TELEMETRY_TOPIC", "ocean.telemetry.v1")
    schema = load_canonical_schema()

    try:
        asyncio.run(consume_and_verify(bootstrap_servers, topic, schema))
    except KeyboardInterrupt:
        log.info("verifier_stopped", reason="KeyboardInterrupt")


if __name__ == "__main__":
    main()
