#!/usr/bin/env python
"""
simulate_alert.py — OceanTrust AI Smoke Test Utility
======================================================
Publishes a synthetic IoT telemetry alert event to ocean.telemetry.v1
to manually trigger the multi-agent debate pipeline.

The event is crafted to breach two regulatory thresholds simultaneously:
    - dissolved_oxygen_mg_l = 5.8 mg/L  (CRITICAL: below legal limit 6.0)
    - lice_count_per_fish   = 0.62      (BREACH: above Norwegian limit 0.5)

This triggers both O2_CRITICAL and LICE_THRESHOLD_BREACH alerts, giving the
Biologist Agent clear regulatory grounds to mandate action, and forcing the
Judge to arbitrate between that mandate and the Commercial Trader's reply.

Usage:
    # Requires: make up && make provision && python -m src.agents.main (in another shell)
    python bin/scripts/simulate_alert.py

    # Custom parameters
    python bin/scripts/simulate_alert.py \\
        --farm-id SC-FARM-0012 \\
        --oxygen 6.3 \\
        --lice 0.55

Environment variables:
    KAFKA_BOOTSTRAP_SERVERS  — default: 127.0.0.1:19092  (IP avoids IPv6 issues on Windows)
    TELEMETRY_TOPIC          — default: ocean.telemetry.v1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime

import structlog
from aiokafka import AIOKafkaProducer

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
log: structlog.stdlib.BoundLogger = structlog.get_logger("simulate-alert")


# ---------------------------------------------------------------------------
# Alert detection helpers (mirrors ocean_producer.py thresholds)
# ---------------------------------------------------------------------------

_O2_CRITICAL_THRESHOLD: float  = 7.0   # mg/L — CRITICAL below this
_O2_WARNING_THRESHOLD: float   = 9.0   # mg/L — WARNING below this
_TEMP_CRITICAL_THRESHOLD: float = 18.0  # °C
_LICE_BREACH_THRESHOLD: float  = 0.5   # per fish — Norwegian regulatory limit


def _build_alerts(oxygen: float, temp: float, lice: float) -> list[dict]:
    """Build alert structures matching the iot_sensor_event.schema.json alerts array."""
    alerts = []

    if oxygen < _O2_CRITICAL_THRESHOLD:
        alerts.append({
            "alert_code": "O2_CRITICAL",
            "severity": "CRITICAL",
            "threshold_breached": _O2_CRITICAL_THRESHOLD,
            "current_value": oxygen,
            "message": (
                f"Dissolved oxygen critically low: {oxygen:.2f} mg/L "
                f"(legal minimum 6.0 mg/L per Akvakulturloven §12)"
            ),
        })
    elif oxygen < _O2_WARNING_THRESHOLD:
        alerts.append({
            "alert_code": "O2_LOW_WARNING",
            "severity": "WARNING",
            "threshold_breached": _O2_WARNING_THRESHOLD,
            "current_value": oxygen,
            "message": f"Dissolved oxygen below safe threshold: {oxygen:.2f} mg/L",
        })

    if temp > _TEMP_CRITICAL_THRESHOLD:
        alerts.append({
            "alert_code": "TEMP_CRITICAL",
            "severity": "CRITICAL",
            "threshold_breached": _TEMP_CRITICAL_THRESHOLD,
            "current_value": temp,
            "message": f"Water temp critically elevated: {temp:.2f} °C (limit > 18.0 °C)",
        })

    if lice > _LICE_BREACH_THRESHOLD:
        alerts.append({
            "alert_code": "LICE_THRESHOLD_BREACH",
            "severity": "CRITICAL",
            "threshold_breached": _LICE_BREACH_THRESHOLD,
            "current_value": lice,
            "message": (
                f"Sea lice count {lice:.2f}/fish exceeds Norwegian "
                "regulatory treatment threshold of 0.5 per fish"
            ),
        })

    return alerts


# ---------------------------------------------------------------------------
# Event factory
# ---------------------------------------------------------------------------


def build_alert_event(
    farm_id: str,
    oxygen_mg_l: float,
    temperature_c: float,
    lice_per_fish: float,
) -> dict:
    """
    Construct a fully compliant iot_sensor_event v1.0.0 payload with alert conditions.

    All field names and enum values match iot_sensor_event.schema.json exactly,
    so the event can be validated by verify_stream.py after it is produced.
    """
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    alerts  = _build_alerts(oxygen_mg_l, temperature_c, lice_per_fish)

    return {
        "schema_version": "1.0.0",
        "event_id":       str(uuid.uuid4()),
        "event_type":     "SENSOR_TELEMETRY",
        "timestamp":      now_utc,
        "producer": {
            "device_id":        f"SENSOR-SMOKE-TEST-{farm_id}",
            "device_type":      "MULTIPARAMETER_PROBE",
            "firmware_version": "3.2.1",
            "manufacturer":     "AquaSense",
        },
        "location": {
            "farm_id":   farm_id,
            "farm_name": "Smoke Test Farm",
            "cage_id":   "CAGE-01",
            "coordinates": {
                "latitude":     60.1533,
                "longitude":    6.0142,
                "depth_meters": 5.0,
            },
            "region":     "NORWAY",
            "water_body": "FJORD",
        },
        "water_quality": {
            "temperature_celsius":       temperature_c,
            "dissolved_oxygen_mg_l":     oxygen_mg_l,
            "oxygen_saturation_percent": round(oxygen_mg_l / 14.6 * 100, 2),
            "salinity_ppt":              33.2,
            "ph":                        7.82,
            "turbidity_ntu":             1.4,
            "chlorophyll_a_ug_l":        2.1,
        },
        "biological": {
            "fish_count_estimate":      5000,
            "avg_biomass_kg":           4.2,
            "feeding_rate_kg_per_hour": 45.0,
            "lice_count_per_fish":      lice_per_fish,
            "mortality_count_24h":      3,
        },
        "environment": {
            "current_speed_m_s":         0.22,
            "current_direction_degrees": 145.0,
            "algae_bloom_detected":      False,
            "algae_bloom_type":          None,
        },
        "alerts": alerts,
        "data_quality": {
            "signal_strength_dbm":   -62,
            "battery_level_percent": 78.5,
            "sensor_status":         "NOMINAL",
            "missing_fields":        [],
        },
    }


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


async def publish_alert(
    farm_id: str,
    oxygen_mg_l: float,
    temperature_c: float,
    lice_per_fish: float,
    bootstrap_servers: str,
    topic: str,
) -> None:
    """Publish a single synthetic alert event to Redpanda."""
    event   = build_alert_event(farm_id, oxygen_mg_l, temperature_c, lice_per_fish)
    payload = json.dumps(event).encode("utf-8")
    key     = farm_id.encode("utf-8")

    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        acks="all",
        compression_type="gzip",
        # Fail fast — better to surface the error immediately than hang
        # the subprocess for 30+ seconds while the UI waits.
        request_timeout_ms=5000,
        retry_backoff_ms=500,
        metadata_max_age_ms=3000,
    )

    try:
        await producer.start()
    except Exception as exc:
        print(f"[simulate_alert] ERROR: cannot connect to broker at {bootstrap_servers}: {exc}",
              file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        await producer.send_and_wait(topic=topic, key=key, value=payload)
        log.info(
            "alert_event_published",
            topic=topic,
            farm_id=farm_id,
            event_id=event["event_id"],
            alert_count=len(event["alerts"]),
            alert_codes=[a["alert_code"] for a in event["alerts"]],
            oxygen_mg_l=oxygen_mg_l,
            lice_per_fish=lice_per_fish,
        )
    except Exception as exc:
        print(f"[simulate_alert] ERROR: failed to publish event: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        await producer.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OceanTrust AI — Smoke Test: publish a synthetic CRITICAL alert event "
            "to ocean.telemetry.v1 to trigger the multi-agent debate pipeline."
        )
    )
    parser.add_argument(
        "--farm-id", default="NO-FARM-0047",
        help="Farm identifier (partition key). Default: NO-FARM-0047",
    )
    parser.add_argument(
        "--oxygen", type=float, default=5.8,
        help="Dissolved oxygen in mg/L. Default: 5.8 (CRITICAL — below 6.0 legal limit)",
    )
    parser.add_argument(
        "--temp", type=float, default=12.4,
        help="Water temperature in °C. Default: 12.4 (nominal)",
    )
    parser.add_argument(
        "--lice", type=float, default=0.62,
        help="Lice count per fish. Default: 0.62 (BREACH — above 0.5 Norwegian limit)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    # 127.0.0.1 avoids IPv6 / getaddrinfo resolution failures on Windows.
    # KAFKA_BOOTSTRAP_SERVERS env var always takes priority.
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:19092")
    topic     = os.getenv("TELEMETRY_TOPIC", "ocean.telemetry.v1")

    log.info(
        "smoke_test_starting",
        farm_id=args.farm_id,
        oxygen_mg_l=args.oxygen,
        temp_c=args.temp,
        lice_per_fish=args.lice,
        target=f"{bootstrap}/{topic}",
    )

    asyncio.run(
        publish_alert(
            farm_id=args.farm_id,
            oxygen_mg_l=args.oxygen,
            temperature_c=args.temp,
            lice_per_fish=args.lice,
            bootstrap_servers=bootstrap,
            topic=topic,
        )
    )


if __name__ == "__main__":
    main()
