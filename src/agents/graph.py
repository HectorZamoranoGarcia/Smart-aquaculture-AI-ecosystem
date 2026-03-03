"""
graph.py — OceanTrust AI Agent Cluster
========================================
LangGraph StateGraph definition for the multi-agent debate workflow.

Graph topology (docs/agents-orchestration.md §1):
    START → assembler_node → biologist_node → commercial_node → judge_node
                                  ↑                                  |
                                  └──── (revision if needed, max 1) ─┘
                                                                      ↓
                                                                     END

Revision routing:
    judge_node returns to biologist_node if arguments are weak AND
    revision_count == 0. A second invocation of the Judge always routes to END.

LLM Provider: Google Gemini (gemini-2.5-flash-lite) via langchain-google-genai.
    Environment variable: GOOGLE_API_KEY

See: docs/agents-orchestration.md
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph

from src.agents.state import DebateState, VALID_ACTIONS
from src.agents.tools import (
    calculate_biomass_stress_index,
    calculate_harvest_opportunity_cost,
    emit_final_verdict,
    get_current_market_data,
    query_vector_knowledge_base,
    verify_regulatory_claim,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger("agent-graph")

_MAX_REVISIONS: int = 1  # Hard cap on revision rounds — prevents infinite loops

# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------


def _make_llm(temperature: float = 0.3) -> ChatGoogleGenerativeAI:
    """Instantiate a ChatGoogleGenerativeAI model bound to the GOOGLE_API_KEY env var.

    max_retries=5 enables LangChain's built-in exponential backoff on HTTP 429
    (rate-limit) responses — essential for Free Tier quota management.
    """
    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash-lite"),
        temperature=temperature,
        google_api_key=os.getenv("GOOGLE_API_KEY", ""),
        max_retries=5,
    )


# ---------------------------------------------------------------------------
# System Prompts (verbatim from agents-orchestration.md)
# ---------------------------------------------------------------------------

_BIOLOGIST_SYSTEM_PROMPT = """You are the **Lead Marine Biologist & Compliance Officer** for OceanTrust AI, overseeing salmon farm operations.
Your sole priorities are animal welfare, biological health, and strict adherence to environmental regulations. You DO NOT care about financial markets, spot prices, or trading margins.

You will be provided with:
1. Real-time sensor telemetry (Oxygen, Temperature, Sea Lice counts, Mortality).
2. Regulatory context retrieved from the legal database (e.g., Norwegian Akvakulturloven).
3. (If applicable) Previous arguments made in this debate.

Your task is to evaluate the telemetry against the legal and biological context.
- If regulatory thresholds (e.g., lice > 0.5 per fish) are breached or imminent, you MUST mandate immediate biological intervention (e.g., emergency harvest or chemical treatment).
- Identify compounding biological risks (e.g., high temperature + low oxygen).

**Output Requirements:**
1. State your biological risk assessment clearly.
2. Explicitly cite the provided regulatory documents (using their doc_id) to justify your stance.
3. Conclude with a strict biological recommendation: [HARVEST_NOW, HARVEST_PARTIAL, HOLD, TREAT]"""

_COMMERCIAL_SYSTEM_PROMPT = """You are the **Senior Commodities Trader & Harvesting Strategist** for OceanTrust AI.
Your sole priority is maximizing the financial yield of the farm's biomass based on the Oslo Fish Pool spot prices, futures contracts, and supply/demand sentiment.
While you acknowledge severe biological risks, your instinct is to delay harvesting if the current market price is depressed, or accelerate harvesting if prices are peaking.

You will be provided with:
1. The current market snapshot (Spot price, Bid/Ask spread, 30-day volatility).
2. The argument just submitted by the Biologist Agent.
3. Estimated current biomass available in the cage.

Your task is to evaluate the Biologist's recommendation through a financial lens.
- If the Biologist dictates HARVEST_NOW but the spot price is down 5% today, you must calculate the exact financial loss of that premature harvest and argue for a HOLD or DELAY if biological survival allows it.
- If the market is at a premium, you should aggressively support harvesting, even if biology is stable.

**Output Requirements:**
1. State your financial projection and opportunity cost analysis.
2. Critique the financial impact of the Biologist's recommendation.
3. Conclude with a strict commercial recommendation: [HARVEST_NOW, HARVEST_PARTIAL, HOLD, TREAT]"""

_JUDGE_SYSTEM_PROMPT = """You are the **Executive Arbitrator (The Judge)** for OceanTrust AI.
You must synthesize a final operational decision by evaluating the arguments submitted by the Biologist (focused on health/law) and the Commercial Trader (focused on profit).

**Rules of Arbitration:**
1. **Absolute Compliance:** If the Biologist cites a hard legal threshold (e.g., Norwegian law mandates treatment at 0.5 lice/fish), you CANNOT override this for financial gain. The law is absolute.
2. **Hallucination Check:** Verify that the Biologist's legal claims actually exist in the provided `rag_context`. If they hallucinated a law, discard their argument.
3. **Compromise:** If biology allows a 48-hour delay without mass mortality and the Trader proves prices will rebound, you may rule for HOLD. If the conflict is irreconcilable, rule HARVEST_PARTIAL to hedge risk.

**Output Requirements:**
You must return a structured JSON synthesis containing exactly these fields:
- `reasoning`: A step-by-step breakdown weighing both sides (max 150 words).
- `hallucination_detected`: boolean (true if either agent invented facts not in the context).
- `recommended_action`: Exact string -> "HARVEST_NOW", "HARVEST_PARTIAL", "HOLD", or "TREAT".
- `confidence_score`: Float between 0.0 and 1.0.
- `cited_sources`: Array of any document IDs or data APIs definitively relied upon for this verdict.

After calling verify_regulatory_claim and emit_final_verdict, you are done."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _format_context_message(state: DebateState) -> str:
    """Serialise the debate context fields into a structured human-turn prompt."""
    return (
        f"=== DEBATE CONTEXT ===\n"
        f"Farm ID: {state['farm_id']}\n"
        f"Trigger Alerts: {json.dumps(state['trigger_alerts'], indent=2)}\n\n"
        f"--- Telemetry Snapshot ---\n{json.dumps(state['telemetry_snapshot'], indent=2)}\n\n"
        f"--- RAG / Regulatory Context ---\n{state['rag_context']}\n\n"
        f"--- Market Snapshot ---\n{json.dumps(state['market_snapshot'], indent=2)}\n\n"
        f"--- Historical Trends ---\n{state['historical_trends']}\n"
    )


# ---------------------------------------------------------------------------
# Graph Nodes
# ---------------------------------------------------------------------------


async def assembler_node(state: DebateState) -> dict[str, Any]:
    """
    Data Assembly Phase: pre-fetch all context before any LLM node runs.
    This node makes NO LLM calls — purely deterministic context hydration.
    """
    log.info("assembler_started", farm_id=state["farm_id"], debate_id=state["debate_id"])

    alert_text = " ".join(
        a.get("message", a.get("alert_code", "")) for a in state["trigger_alerts"]
    )
    rag_result  = await query_vector_knowledge_base.ainvoke(
        {"query": alert_text, "collection": "biological_manuals", "jurisdiction": "NORWAY"}
    )
    market_data = await get_current_market_data.ainvoke(
        {"species": "ATLANTIC_SALMON", "product_form": "FRESH_HOG"}
    )

    log.info("assembler_complete", rag_chars=len(rag_result))
    return {
        "rag_context": rag_result,
        "market_snapshot": market_data,
        "historical_trends": (
            "30-day trend: water temp stable at 12±1°C, DO averaging 10.2 mg/L. "
            "No prior lice breaches in last 90 days."
        ),
        "biologist_arguments": [],
        "commercial_arguments": [],
        "revision_count": 0,
        "judge_verdict": None,
        "recommended_action": None,
        "confidence_score": None,
        "hallucination_detected": None,
        "cited_sources": [],
    }


async def biologist_node(state: DebateState) -> dict[str, Any]:
    """
    Biologist Agent: evaluates telemetry against regulatory context.
    Tools: calculate_biomass_stress_index, query_vector_knowledge_base.
    """
    revision = state.get("revision_count", 0)
    log.info("biologist_node_invoked", farm_id=state["farm_id"], revision=revision)

    llm = _make_llm(temperature=0.2).bind_tools(
        [calculate_biomass_stress_index, query_vector_knowledge_base]
    )
    context_msg = _format_context_message(state)
    if revision > 0:
        prior = state.get("biologist_arguments", [])
        context_msg += (
            f"\n=== REVISION REQUEST ===\n"
            f"The Judge requested you strengthen your argument.\n"
            f"Your prior argument:\n{prior[-1] if prior else 'None'}\n"
            f"The Commercial Trader argued:\n{state.get('commercial_arguments', [''])[-1]}\n"
        )

    response = await llm.ainvoke([
        SystemMessage(content=_BIOLOGIST_SYSTEM_PROMPT),
        HumanMessage(content=context_msg),
    ])
    log.info("biologist_argument_generated", chars=len(response.content))
    return {"biologist_arguments": [response.content]}


async def commercial_node(state: DebateState) -> dict[str, Any]:
    """
    Commercial Agent: evaluates market impact of the Biologist's recommendation.
    Tools: get_current_market_data, calculate_harvest_opportunity_cost.
    """
    log.info("commercial_node_invoked", farm_id=state["farm_id"])

    llm = _make_llm(temperature=0.3).bind_tools(
        [get_current_market_data, calculate_harvest_opportunity_cost]
    )
    bio_argument = (state.get("biologist_arguments") or ["No argument provided."])[-1]
    context_msg  = (
        _format_context_message(state)
        + f"\n=== BIOLOGIST ARGUMENT ===\n{bio_argument}\n"
    )

    response = await llm.ainvoke([
        SystemMessage(content=_COMMERCIAL_SYSTEM_PROMPT),
        HumanMessage(content=context_msg),
    ])
    log.info("commercial_argument_generated", chars=len(response.content))
    return {"commercial_arguments": [response.content]}


async def judge_node(state: DebateState) -> dict[str, Any]:
    """
    Judge Agent: synthesises the debate and emits the binding verdict.
    Must invoke verify_regulatory_claim before emitting emit_final_verdict.
    """
    revision  = state.get("revision_count", 0)
    bio_args  = state.get("biologist_arguments",  ["No argument."])
    comm_args = state.get("commercial_arguments", ["No argument."])
    log.info("judge_node_invoked", farm_id=state["farm_id"], revision=revision)

    llm = _make_llm(temperature=0.1).bind_tools(
        [verify_regulatory_claim, emit_final_verdict, query_vector_knowledge_base]
    )
    context_msg = (
        _format_context_message(state)
        + f"\n=== BIOLOGIST ARGUMENT ===\n{bio_args[-1]}\n"
        + f"\n=== COMMERCIAL ARGUMENT ===\n{comm_args[-1]}\n"
        + f"\nRevision round: {revision}/{_MAX_REVISIONS}\n"
        + (
            "NOTE: This is your FINAL ruling. You MUST call emit_final_verdict now."
            if revision >= _MAX_REVISIONS else
            "If arguments are too weak, signal a revision by returning "
            "'REQUEST_REVISION' as recommended_action (only if revision_count == 0)."
        )
    )

    response = await llm.ainvoke([
        SystemMessage(content=_JUDGE_SYSTEM_PROMPT),
        HumanMessage(content=context_msg),
    ])

    try:
        verdict_data = json.loads(response.content)
    except (json.JSONDecodeError, TypeError):
        verdict_data = {
            "reasoning": response.content[:500],
            "hallucination_detected": False,
            "recommended_action": "HOLD",
            "confidence_score": 0.5,
            "cited_sources": [],
        }

    action = verdict_data.get("recommended_action", "HOLD")
    log.info("judge_verdict_issued", action=action, revision=revision)

    return {
        "judge_verdict": verdict_data.get("reasoning"),
        "recommended_action": action if action in VALID_ACTIONS else "HOLD",
        "confidence_score": verdict_data.get("confidence_score", 0.5),
        "hallucination_detected": verdict_data.get("hallucination_detected", False),
        "cited_sources": verdict_data.get("cited_sources", []),
        "revision_count": revision + (
            1 if action == "REQUEST_REVISION" and revision < _MAX_REVISIONS else 0
        ),
    }


# ---------------------------------------------------------------------------
# Conditional edge: route after Judge
# ---------------------------------------------------------------------------


def route_after_judge(state: DebateState) -> Literal["biologist_node", "__end__"]:
    """
    Returns "biologist_node" if the Judge requests a revision on round 0.
    Returns END in all other cases.
    """
    action   = state.get("recommended_action")
    revision = state.get("revision_count", 0)

    if action == "REQUEST_REVISION" and revision <= _MAX_REVISIONS:
        log.info("judge_routing_revision", revision=revision)
        return "biologist_node"

    log.info("judge_routing_end", action=action)
    return END


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_debate_graph() -> StateGraph:
    """Compile and return the LangGraph StateGraph for the multi-agent debate."""
    graph = StateGraph(DebateState)

    graph.add_node("assembler_node",  assembler_node)
    graph.add_node("biologist_node",  biologist_node)
    graph.add_node("commercial_node", commercial_node)
    graph.add_node("judge_node",      judge_node)

    graph.add_edge(START,             "assembler_node")
    graph.add_edge("assembler_node",  "biologist_node")
    graph.add_edge("biologist_node",  "commercial_node")
    graph.add_edge("commercial_node", "judge_node")

    graph.add_conditional_edges(
        "judge_node",
        route_after_judge,
        {"biologist_node": "biologist_node", END: END},
    )
    return graph.compile()


# Compiled singleton — import and invoke from main.py
debate_graph = build_debate_graph()
