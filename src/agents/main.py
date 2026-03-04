"""
main.py — OceanTrust AI Agent Orchestrator
============================================
Entry point for the multi-agent debate cluster.

Listens to the `ocean.telemetry.v1` Redpanda topic and, for each telemetry
event with active alerts, initiates a full algorithmic debate via the
LangGraph `debate_graph`.  The final judge verdict is:
    1. Logged to stdout via structlog (JSON format).
    2. Persisted as a JSON audit trace in `logs/audit/debates/<date>/`.
    3. Escalated to human operators if hallucination_detected is True.

Kafka offset commit strategy:
    Offsets are committed after the debate graph completes, regardless of
    whether the verdict flagged a hallucination.  Graph failures are isolated
    per-message — a single bad event will never stop the consumer.

Usage:
    python -m src.agents.main

Environment variables (see .env.example):
    KAFKA_BOOTSTRAP_SERVERS  — default: localhost:9092
    ORCHESTRATOR_TOPIC       — default: ocean.telemetry.v1
    GOOGLE_API_KEY           — required for LLM debate nodes
    GEMINI_CHAT_MODEL        — default: gemini-2.5-flash-lite
    QDRANT_HOST              — default: localhost
    QDRANT_PORT              — default: 6333
    AUDIT_LOG_DIR            — default: logs/audit/debates
    LOG_LEVEL                — default: INFO
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.agents.graph import debate_graph
from src.agents.state import DebateState

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class OrchestratorSettings(BaseSettings):
    """Runtime configuration for the debate orchestrator."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    kafka_bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    orchestrator_topic: str = Field(default="ocean.telemetry.v1", alias="ORCHESTRATOR_TOPIC")
    audit_log_dir: str = Field(default="logs/audit/debates", alias="AUDIT_LOG_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging(log_level: str) -> structlog.stdlib.BoundLogger:
    """Configure structlog JSON renderer and return a bound service logger."""
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
    return structlog.get_logger().bind(service="agent-orchestrator")


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------


class DebateAuditLogger:
    """
    Persists full debate records as JSON files for compliance and debugging.

    Directory layout:
        <audit_log_dir>/
            <YYYY-MM-DD>/
                <debate_id>.json

    Each file captures the complete final DebateState so every decision
    made by the agent cluster is fully reproducible and auditable.
    """

    def __init__(self, base_dir: str, logger: structlog.stdlib.BoundLogger) -> None:
        self._base = Path(base_dir)
        self._log  = logger.bind(component="audit-logger")

    def persist(self, state: DebateState) -> Path:
        """
        Serialise and write the final debate state to disk.

        The target directory is created at runtime if it does not exist.
        Uses exist_ok=True so concurrent processes do not race on mkdir.
        """
        today      = datetime.now(UTC).strftime("%Y-%m-%d")
        debate_id  = state.get("debate_id", str(uuid.uuid4()))
        target_dir = self._base / today
        target_dir.mkdir(parents=True, exist_ok=True)

        audit_file = target_dir / f"{debate_id}.json"
        audit_record = {
            "audit_version": "1.0",
            "persisted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "debate_id": debate_id,
            "farm_id": state.get("farm_id"),
            "trigger_alerts": state.get("trigger_alerts", []),
            "recommended_action": state.get("recommended_action"),
            "confidence_score": state.get("confidence_score"),
            "hallucination_detected": state.get("hallucination_detected"),
            "judge_verdict": state.get("judge_verdict"),
            "cited_sources": state.get("cited_sources", []),
            "biologist_arguments": state.get("biologist_arguments", []),
            "commercial_arguments": state.get("commercial_arguments", []),
            "revision_count": state.get("revision_count", 0),
        }
        audit_file.write_text(
            json.dumps(audit_record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._log.info("audit_trace_persisted", path=str(audit_file), debate_id=debate_id)
        return audit_file


# ---------------------------------------------------------------------------
# DebateState factory
# ---------------------------------------------------------------------------


def build_initial_debate_state(telemetry_payload: dict[str, Any]) -> DebateState:
    """
    Build the initial DebateState from a raw telemetry event payload.

    Context fields (rag_context, market_snapshot, historical_trends) are
    left empty — the `assembler_node` inside the graph hydrates them from
    Qdrant before the LLM nodes run.

    The debate is only triggered when alerts are present.  The caller is
    responsible for filtering events without alerts before calling this.
    """
    farm_id = telemetry_payload.get("location", {}).get("farm_id", "UNKNOWN")
    alerts  = telemetry_payload.get("alerts", [])

    return {
        "debate_id":        str(uuid.uuid4()),
        "farm_id":          farm_id,
        "trigger_alerts":   alerts,
        "revision_count":   0,
        "telemetry_snapshot":  telemetry_payload,
        "market_snapshot":     {},
        "rag_context":         "",
        "historical_trends":   "",
        "biologist_arguments":  [],
        "commercial_arguments": [],
        "judge_verdict":        None,
        "recommended_action":   None,
        "confidence_score":     None,
        "hallucination_detected": None,
        "cited_sources":        [],
    }


# ---------------------------------------------------------------------------
# Verdict logger
# ---------------------------------------------------------------------------


def _log_verdict(
    final_state: DebateState,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """
    Emit a structured log entry for the Judge's final verdict.

    Hallucination events are elevated to ERROR and tagged for human escalation.
    The Kafka consumer continues regardless.
    """
    context = {
        "debate_id":          final_state.get("debate_id"),
        "farm_id":            final_state.get("farm_id"),
        "recommended_action": final_state.get("recommended_action"),
        "confidence_score":   final_state.get("confidence_score"),
        "revision_count":     final_state.get("revision_count"),
        "hallucination":      final_state.get("hallucination_detected"),
        "cited_sources":      final_state.get("cited_sources"),
    }

    if final_state.get("hallucination_detected"):
        logger.error(
            "debate_verdict_hallucination_flagged",
            **context,
            action_required="ESCALATE_TO_HUMAN_OPS",
            reasoning_excerpt=(final_state.get("judge_verdict") or "")[:300],
        )
    else:
        logger.info(
            "debate_verdict_accepted",
            **context,
            reasoning_excerpt=(final_state.get("judge_verdict") or "")[:300],
        )


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------


class DebateOrchestrator:
    """
    Kafka consumer that triggers a multi-agent debate for each alert event.

    Processing contract:
        1. Consume a message from ocean.telemetry.v1.
        2. Skip events with no active alerts (zero LLM cost for nominal readings).
        3. Build an initial DebateState and invoke debate_graph.
        4. Log the verdict and persist the audit trace.
        5. Commit the Kafka offset in `finally` — always, even on hallucination.
        6. On graph exception: log, skip, continue — consumer never stops.

    Consumer group `cg-agent-orchestrator` is isolated from the vector
    ingestion worker so both pipelines receive all messages independently.
    """

    _CONSUMER_GROUP: str = "cg-agent-orchestrator"

    def __init__(
        self,
        settings: OrchestratorSettings,
        logger: structlog.stdlib.BoundLogger,
    ) -> None:
        self._settings     = settings
        self._log          = logger
        self._audit_logger = DebateAuditLogger(settings.audit_log_dir, logger)

    async def run(self) -> None:
        """Start the consumer and enter the main event loop."""
        consumer = AIOKafkaConsumer(
            self._settings.orchestrator_topic,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._CONSUMER_GROUP,
            auto_offset_reset="latest",
            enable_auto_commit=False,
            value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
        )

        self._log.info(
            "orchestrator_started",
            topic=self._settings.orchestrator_topic,
            group=self._CONSUMER_GROUP,
        )
        await consumer.start()

        try:
            async for msg in consumer:
                await self._dispatch(consumer, msg)
        except asyncio.CancelledError:
            self._log.info("orchestrator_cancelled")
        except KafkaError as exc:
            self._log.error("kafka_fatal_error", error=str(exc))
        finally:
            await consumer.stop()
            self._log.info("orchestrator_stopped")

    async def _dispatch(self, consumer: AIOKafkaConsumer, msg: Any) -> None:
        """
        Process a single telemetry message through the debate pipeline.

        Events with no alerts are acknowledged and skipped immediately.
        The Kafka offset is committed in `finally` regardless of outcome.
        """
        payload: dict[str, Any] = msg.value
        farm_id  = payload.get("location", {}).get("farm_id", "UNKNOWN")
        event_id = payload.get("event_id", "UNKNOWN")
        alerts   = payload.get("alerts", [])

        log = self._log.bind(farm_id=farm_id, event_id=event_id, offset=msg.offset)

        if not alerts:
            log.debug("event_skipped_no_alerts")
            await consumer.commit()
            return

        log.info("debate_triggered", alert_count=len(alerts))
        initial_state = build_initial_debate_state(payload)

        try:
            final_state: DebateState = await debate_graph.ainvoke(initial_state)
            _log_verdict(final_state, log)
            self._audit_logger.persist(final_state)
        except Exception as exc:
            log.error(
                "debate_graph_exception",
                error=str(exc),
                exc_type=type(exc).__name__,
                action="message_skipped_offset_committed",
            )
        finally:
            await consumer.commit()


# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------


def _validate_environment() -> None:
    """Validate that all required environment variables are present.

    Raises:
        RuntimeError: If any required variable is missing, with a clear
            message identifying the missing key and its purpose.
    """
    required: dict[str, str] = {
        "GOOGLE_API_KEY": "Google Generative AI key required for LLM debate nodes",
        "KAFKA_BOOTSTRAP_SERVERS": "Redpanda/Kafka broker address (e.g. 127.0.0.1:19092)",
    }
    missing = [
        f"  {key} — {desc}"
        for key, desc in required.items()
        if not os.getenv(key)
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variables:\n"
            + "\n".join(missing)
            + "\n\nSet them in your shell or in a .env file at the project root."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Configure environment and launch the orchestrator event loop."""
    _validate_environment()
    settings = OrchestratorSettings()
    logger   = configure_logging(settings.log_level)
    runner   = DebateOrchestrator(settings=settings, logger=logger)

    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        logger.info("orchestrator_stopped", reason="KeyboardInterrupt")


if __name__ == "__main__":
    main()
