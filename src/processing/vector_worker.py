"""
vector_worker.py — OceanTrust AI
==================================
Vector Ingestion Worker: consumes salmon farm IoT telemetry events, translates
them into semantically rich narratives, generates 768-dim embeddings, and
upserts into Qdrant.  Kafka offset is committed ONLY after Qdrant ACKs.

Pipeline:  ocean.telemetry.v1 → narrative → embed → Qdrant upsert → commit
Topic:          ocean.telemetry.v1
Consumer group: cg-vector-ingestion
Qdrant coll.:   telemetry_vectors
Vector dim:     768  (OpenAI text-embedding-3-small @768 / Ollama nomic-embed-text)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid as _uuid
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Final

import httpx
import structlog
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from openai import AsyncOpenAI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VECTOR_DIMENSION: Final[int] = 768
_QDRANT_COLLECTION: Final[str] = "telemetry_vectors"
_CONSUMER_GROUP: Final[str] = "cg-vector-ingestion"
_OPENAI_MODEL: Final[str] = "text-embedding-3-small"
_OLLAMA_MODEL: Final[str] = "nomic-embed-text"
_CB_FAILURE_THRESHOLD: Final[int] = 5
_CB_RECOVERY_TIMEOUT_S: Final[float] = 60.0


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class WorkerSettings(BaseSettings):
    """Runtime configuration loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    kafka_bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    telemetry_topic: str = Field(default="ocean.telemetry.v1", alias="TELEMETRY_TOPIC")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
    qdrant_host: str = Field(default="localhost", alias="QDRANT_HOST")
    qdrant_port: int = Field(default=6333, alias="QDRANT_PORT")
    qdrant_collection: str = Field(default=_QDRANT_COLLECTION, alias="QDRANT_COLLECTION")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging(log_level: str) -> structlog.stdlib.BoundLogger:
    """Configure structlog JSON processors and return a bound logger."""
    import logging as _logging
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(_logging, log_level.upper(), _logging.INFO)
        ),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )
    return structlog.get_logger().bind(service="vector-worker")


# ---------------------------------------------------------------------------
# Domain enums for narrative classification
# ---------------------------------------------------------------------------


class OxygenState(StrEnum):
    CRITICAL = "critically low"
    WARNING  = "below safe threshold"
    NOMINAL  = "nominal"


class TemperatureState(StrEnum):
    CRITICAL = "critically elevated"
    WARNING  = "elevated"
    NOMINAL  = "nominal"


class LiceState(StrEnum):
    BREACH  = "above regulatory limit"
    NOMINAL = "within safe range"


# ---------------------------------------------------------------------------
# Semantic Translation
# ---------------------------------------------------------------------------


class TelemetryNarrativeBuilder:
    """
    Converts structured IoT sensor readings into a dense natural language
    narrative for semantic embedding.  Numeric values are annotated with
    their operational state descriptors so the embedding captures meaning.
    """

    @staticmethod
    def build(payload: dict[str, Any]) -> str:
        """Build and return a narrative string from a raw telemetry payload."""
        loc = payload.get("location", {})
        wq  = payload.get("water_quality", {})
        bio = payload.get("biological", {})
        env = payload.get("environment", {})
        alerts = payload.get("alerts", [])
        dq  = payload.get("data_quality", {})

        farm_id    = loc.get("farm_id", "UNKNOWN")
        farm_name  = loc.get("farm_name", "")
        region     = loc.get("region", "")
        water_body = loc.get("water_body", "")
        cage       = loc.get("cage_id", "")
        depth      = loc.get("coordinates", {}).get("depth_meters", "?")

        temp  = wq.get("temperature_celsius")
        do    = wq.get("dissolved_oxygen_mg_l")
        sal   = wq.get("salinity_ppt")
        ph    = wq.get("ph")
        turb  = wq.get("turbidity_ntu")
        chl   = wq.get("chlorophyll_a_ug_l")
        lice  = bio.get("lice_count_per_fish")
        mort  = bio.get("mortality_count_24h", 0)
        speed = env.get("current_speed_m_s")
        algae = env.get("algae_bloom_detected", False)

        def _fmt(v: float | None, unit: str, decimals: int = 3) -> str:
            return f"{v:.{decimals}f} {unit}" if v is not None else "sensor fault"

        alert_count = len(alerts)
        alert_str = (
            ", ".join(f"{a.get('alert_code')} ({a.get('severity')})" for a in alerts)
            if alert_count > 0 else "none"
        )

        return (
            f"Salmon farm {farm_id} ({farm_name}) {water_body}/{region}, "
            f"cage {cage} at {depth} m. "
            f"Water: temp {_fmt(temp, '°C')} [{TelemetryNarrativeBuilder._classify_temp(temp)}], "
            f"DO {_fmt(do, 'mg/L')} [{TelemetryNarrativeBuilder._classify_o2(do)}], "
            f"salinity {_fmt(sal, 'ppt')}, pH {_fmt(ph, '', 2)}. "
            f"Biological: lice {_fmt(lice, '/fish')} [{TelemetryNarrativeBuilder._classify_lice(lice)}], "
            f"mortality 24h: {mort} fish. "
            f"Environment: current {_fmt(speed, 'm/s')}, "
            f"{'algae bloom DETECTED' if algae else 'no algae bloom'}. "
            f"Active alerts ({alert_count}): {alert_str}. "
            f"Sensor: {dq.get('sensor_status', 'UNKNOWN')}."
        )

    @staticmethod
    def _classify_o2(v: float | None) -> str:
        if v is None: return "sensor fault"
        if v < 7.0:   return OxygenState.CRITICAL
        if v < 9.0:   return OxygenState.WARNING
        return OxygenState.NOMINAL

    @staticmethod
    def _classify_temp(v: float | None) -> str:
        if v is None: return "sensor fault"
        if v > 18.0:  return TemperatureState.CRITICAL
        if v > 16.0:  return TemperatureState.WARNING
        return TemperatureState.NOMINAL

    @staticmethod
    def _classify_lice(v: float | None) -> str:
        if v is None:  return "unavailable"
        if v > 0.5:    return LiceState.BREACH
        return LiceState.NOMINAL


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class EmbeddingCircuitBreaker:
    """
    Three-state Circuit Breaker protecting the OpenAI embedding endpoint.
    CLOSED → OPEN after _CB_FAILURE_THRESHOLD consecutive failures.
    OPEN → HALF_OPEN after _CB_RECOVERY_TIMEOUT_S seconds.
    HALF_OPEN → CLOSED on success, OPEN on failure.
    """

    def __init__(self, logger: structlog.stdlib.BoundLogger | None = None) -> None:
        self._state           = CircuitState.CLOSED
        self._failure_count   = 0
        self._last_failure_at = 0.0
        self._log = (logger or structlog.get_logger()).bind(component="circuit-breaker")

    @property
    def state(self) -> CircuitState:
        return self._state

    def should_use_fallback(self) -> bool:
        """Returns True when the circuit is OPEN and recovery period has not elapsed."""
        if self._state == CircuitState.CLOSED:
            return False
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_at >= _CB_RECOVERY_TIMEOUT_S:
                self._state = CircuitState.HALF_OPEN
                self._log.info("circuit_half_open")
                return False
            return True
        return False  # HALF_OPEN — let through

    def record_primary_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._log.info("circuit_recovered", new_state="CLOSED")
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_primary_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_at = time.monotonic()
        if self._failure_count >= _CB_FAILURE_THRESHOLD:
            if self._state != CircuitState.OPEN:
                self._log.warning("circuit_opened", failures=self._failure_count)
            self._state = CircuitState.OPEN


# ---------------------------------------------------------------------------
# Embedding Service
# ---------------------------------------------------------------------------


class EmbeddingService:
    """
    Generates 768-dim embeddings.
    Primary: OpenAI text-embedding-3-small (dimensions=768).
    Fallback: Ollama nomic-embed-text (natively 768-dim).
    """

    def __init__(
        self,
        settings: WorkerSettings,
        circuit_breaker: EmbeddingCircuitBreaker,
        logger: structlog.stdlib.BoundLogger,
    ) -> None:
        self._openai  = AsyncOpenAI(api_key=settings.openai_api_key)
        self._ollama_url = settings.ollama_base_url
        self._cb   = circuit_breaker
        self._log  = logger.bind(component="embedding-service")

    async def embed(self, text: str) -> list[float]:
        """Return a 768-dim vector for `text`, with circuit-breaker failover."""
        if self._cb.should_use_fallback():
            self._log.info("routed_to_ollama", reason="circuit_open")
            return await self._embed_ollama(text)
        try:
            vector = await self._embed_openai(text)
            self._cb.record_primary_success()
            return vector
        except Exception as exc:
            self._log.warning("openai_failed", error=str(exc), action="fallback_to_ollama")
            self._cb.record_primary_failure()
            return await self._embed_ollama(text)

    async def _embed_openai(self, text: str) -> list[float]:
        resp = await self._openai.embeddings.create(
            model=_OPENAI_MODEL, input=text, dimensions=_VECTOR_DIMENSION
        )
        return resp.data[0].embedding

    async def _embed_ollama(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._ollama_url}/api/embeddings",
                json={"model": _OLLAMA_MODEL, "prompt": text},
            )
            resp.raise_for_status()
        embedding: list[float] = resp.json()["embedding"]
        if len(embedding) != _VECTOR_DIMENSION:
            raise RuntimeError(
                f"Ollama returned {len(embedding)}-dim vector, expected {_VECTOR_DIMENSION}."
            )
        return embedding


# ---------------------------------------------------------------------------
# Qdrant Client
# ---------------------------------------------------------------------------


class QdrantIngestionClient:
    """
    Async Qdrant client. Ensures the collection and payload indexes exist.
    Indexed fields: farm_id (KEYWORD), timestamp (DATETIME), alert_severity (KEYWORD).
    """

    _INDEXED_FIELDS: Final[dict[str, PayloadSchemaType]] = {
        "farm_id":       PayloadSchemaType.KEYWORD,
        "alert_severity": PayloadSchemaType.KEYWORD,
        "timestamp":     PayloadSchemaType.DATETIME,
    }

    def __init__(self, settings: WorkerSettings, logger: structlog.stdlib.BoundLogger) -> None:
        self._client     = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        self._collection = settings.qdrant_collection
        self._log        = logger.bind(component="qdrant", collection=self._collection)

    async def ensure_collection_ready(self) -> None:
        """Create collection and indexes if they do not exist (idempotent)."""
        existing = {c.name for c in await self._client.get_collections()}
        if self._collection not in existing:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=_VECTOR_DIMENSION, distance=Distance.COSINE),
            )
            for field, schema in self._INDEXED_FIELDS.items():
                await self._client.create_payload_index(
                    collection_name=self._collection, field_name=field, field_schema=schema
                )
            self._log.info("collection_created", dims=_VECTOR_DIMENSION)
        else:
            self._log.info("collection_exists")

    async def upsert(self, event_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        """Upsert a vector point. Uses UUID int hash as deterministic point ID."""
        point_id = _uuid.UUID(event_id).int >> 64
        await self._client.upsert(
            collection_name=self._collection,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        self._log.info("vector_upserted", event_id=event_id, farm_id=payload.get("farm_id"))

    async def close(self) -> None:
        await self._client.close()


def _build_qdrant_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract and flatten indexed metadata from a raw telemetry event."""
    loc    = payload.get("location", {})
    alerts = payload.get("alerts", [])
    priority = {"CRITICAL": 3, "WARNING": 2, "INFO": 1}
    highest, high_val = "NONE", 0
    for a in alerts:
        sev = a.get("severity", "INFO")
        if priority.get(sev, 0) > high_val:
            high_val, highest = priority[sev], sev
    return {
        "farm_id":         loc.get("farm_id"),
        "timestamp":       payload.get("timestamp"),
        "alert_severity":  highest,
        "event_id":        payload.get("event_id"),
        "farm_name":       loc.get("farm_name"),
        "region":          loc.get("region"),
        "water_body":      loc.get("water_body"),
        "cage_id":         loc.get("cage_id"),
        "temperature_celsius":   payload.get("water_quality", {}).get("temperature_celsius"),
        "dissolved_oxygen_mg_l": payload.get("water_quality", {}).get("dissolved_oxygen_mg_l"),
        "lice_count_per_fish":   payload.get("biological", {}).get("lice_count_per_fish"),
        "alert_codes":     [a.get("alert_code") for a in alerts],
        "sensor_status":   payload.get("data_quality", {}).get("sensor_status"),
        "ingested_at":     datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }


# ---------------------------------------------------------------------------
# Vector Worker
# ---------------------------------------------------------------------------


class VectorWorker:
    """
    Coordinates the consume → translate → embed → upsert → commit pipeline.
    Offset is committed AFTER Qdrant ACKs (At-Least-Once guarantee).
    """

    def __init__(self, settings: WorkerSettings, logger: structlog.stdlib.BoundLogger) -> None:
        cb = EmbeddingCircuitBreaker(logger=logger)
        self._embedding = EmbeddingService(settings, cb, logger)
        self._qdrant    = QdrantIngestionClient(settings, logger)
        self._settings  = settings
        self._log       = logger

    async def run(self) -> None:
        """Start the consumer and enter the main processing loop."""
        await self._qdrant.ensure_collection_ready()
        consumer = AIOKafkaConsumer(
            self._settings.telemetry_topic,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=_CONSUMER_GROUP,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
        )
        self._log.info("worker_starting", topic=self._settings.telemetry_topic)
        await consumer.start()
        try:
            async for msg in consumer:
                await self._process(consumer, msg)
        except asyncio.CancelledError:
            pass
        except KafkaError as exc:
            self._log.error("kafka_error", error=str(exc))
        finally:
            await consumer.stop()
            await self._qdrant.close()
            self._log.info("worker_stopped")

    async def _process(self, consumer: AIOKafkaConsumer, msg: Any) -> None:
        """Process one message end-to-end; commit offset only after Qdrant ACK."""
        payload  = msg.value
        event_id = payload.get("event_id", "UNKNOWN")
        farm_id  = payload.get("location", {}).get("farm_id", "UNKNOWN")
        log = self._log.bind(event_id=event_id, farm_id=farm_id, offset=msg.offset)
        try:
            narrative = TelemetryNarrativeBuilder.build(payload)
            vector    = await self._embedding.embed(narrative)
            qpayload  = _build_qdrant_payload(payload)
            await self._qdrant.upsert(event_id=event_id, vector=vector, payload=qpayload)
            await consumer.commit()
            log.info("message_processed_and_committed")
        except Exception as exc:
            log.error("processing_failed", error=str(exc), action="offset_not_committed")


def main() -> None:
    settings = WorkerSettings()
    logger   = configure_logging(settings.log_level)
    worker   = VectorWorker(settings=settings, logger=logger)
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        logger.info("worker_stopped", reason="KeyboardInterrupt")


if __name__ == "__main__":
    main()
