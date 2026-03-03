"""
state.py — OceanTrust AI Agent Cluster
========================================
Shared state schema for the multi-agent debate workflow.

The DebateState TypedDict is the single source of truth passed through every
node in the LangGraph StateGraph.  Fields annotated with `operator.add` are
accumulated (append-only) across revisions, preserving the full debate history.

Fields intentionally NOT annotated:
    - All non-list fields are scalar; LangGraph replaces them on each write.

See: docs/agents-orchestration.md §2
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional

from typing_extensions import TypedDict


class DebateState(TypedDict):
    """
    Immutable working memory shared across all LangGraph agent nodes.

    Populated in two phases:
        1. Assembler phase — orchestrator populates raw context fields.
        2. Debate phase    — each agent appends its outputs.

    The `revision_count` field gates re-entry into the debate
    (max 1 revision per orchestration cycle; see graph.py).
    """

    # ── Orchestration metadata ─────────────────────────────────────────────
    debate_id: str            # UUIDv4 — audit trail identifier
    farm_id: str              # e.g. "NO-FARM-0047"
    trigger_alerts: list[dict[str, Any]]  # Raw alert dicts from ocean.alerts.v1
    revision_count: int       # Incremented on each Judge → Biologist revision loop

    # ── Raw context (assembled before LLM nodes run) ──────────────────────
    telemetry_snapshot: dict[str, Any]   # Latest sensor readings for this farm
    market_snapshot: dict[str, Any]      # Latest price tick from market.prices.v1
    rag_context: str          # Pre-fetched regulatory text from Qdrant
    historical_trends: str    # 30-day trend summary from TimescaleDB

    # ── Agent outputs (append-only — Annotated preserves history) ─────────
    # LangGraph merges these using operator.add (list concatenation)
    # so each revision round can be replayed for audit.
    biologist_arguments: Annotated[list[str], operator.add]
    commercial_arguments: Annotated[list[str], operator.add]

    # ── Final verdict (populated by the Judge before emitting) ────────────
    judge_verdict: Optional[str]             # Full reasoning narrative
    recommended_action: Optional[str]        # HARVEST_NOW | HARVEST_PARTIAL | HOLD | TREAT
    confidence_score: Optional[float]        # 0.0 – 1.0
    hallucination_detected: Optional[bool]   # True if Judge found fabricated claims
    cited_sources: list[str]                 # RAG chunk IDs and API references used


# ---------------------------------------------------------------------------
# Domain constants for recommended actions
# ---------------------------------------------------------------------------

VALID_ACTIONS: frozenset[str] = frozenset(
    {"HARVEST_NOW", "HARVEST_PARTIAL", "HOLD", "TREAT"}
)
