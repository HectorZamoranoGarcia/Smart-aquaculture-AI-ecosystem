"""
tools.py — OceanTrust AI Agent Cluster
========================================
All callable tools available to the LangGraph agent nodes.

Each tool is a plain async function decorated with @tool from langchain_core.
The agents call these tools via their bound LLM (tool-calling mode).

Tool registry per agent (see docs/agents-orchestration.md §6):
    Biologist  : calculate_biomass_stress_index, query_vector_knowledge_base
    Commercial : get_current_market_data, calculate_harvest_opportunity_cost
    Judge      : verify_regulatory_claim, query_vector_knowledge_base,
                 request_debate_revision, emit_final_verdict

Embedding Provider: Google Generative AI (models/gemini-embedding-001, 768 dims).
    Environment variable: GOOGLE_API_KEY
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langchain_core.tools import tool
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, ScoredPoint

# ---------------------------------------------------------------------------
# Embedding constants — gemini-embedding-001 outputs 768-dim vectors.
# This aligns with the Ollama nomic-embed-text fallback (ADR-004) and
# the knowledge_loader.py ingestion pipeline.
# ---------------------------------------------------------------------------

_GEMINI_EMBEDDING_MODEL: str = "models/gemini-embedding-001"
_EMBEDDING_DIM: int = 768


# ---------------------------------------------------------------------------
# Shared Qdrant client factory (lazily initialised per call)
# ---------------------------------------------------------------------------

def _qdrant_client() -> AsyncQdrantClient:
    """Return an async Qdrant client configured from environment variables."""
    return AsyncQdrantClient(
        host=os.getenv("QDRANT_HOST", "localhost"),
        port=int(os.getenv("QDRANT_PORT", "6333")),
    )


def _embed_query(text: str) -> list[float]:
    """
    Synchronously embed a single text string using Google Generative AI.
    Uses GoogleGenerativeAIEmbeddings (models/gemini-embedding-001, 768-dim).
    Called via asyncio.to_thread from async tools to avoid blocking the event loop.
    """
    embedder = GoogleGenerativeAIEmbeddings(
        model=_GEMINI_EMBEDDING_MODEL,
        google_api_key=os.getenv("GOOGLE_API_KEY", ""),
    )
    return embedder.embed_query(text)


# ---------------------------------------------------------------------------
# Biologist Tools
# ---------------------------------------------------------------------------


@tool
def calculate_biomass_stress_index(
    temp_c: float,
    oxygen_mg_l: float,
    fish_avg_weight_kg: float,
) -> float:
    """
    Deterministic formula estimating the biological stress index of a pen.
    Returns a normalised float 0.0 (no stress) to 1.0 (critical / mass mortality risk).

    Formula:
        base_stress    = max(0, (temp_c - 12.0) / 8.0)
        oxygen_stress  = max(0, (9.0 - oxygen_mg_l) / 9.0)
        weight_penalty = 1.0 + (fish_avg_weight_kg / 10.0) * 0.1
        raw_index      = (0.5 * oxygen_stress + 0.5 * base_stress) * weight_penalty

    Values > 0.7 indicate urgent biological intervention.
    """
    base_stress   = max(0.0, (temp_c - 12.0) / 8.0)
    oxygen_stress = max(0.0, (9.0 - oxygen_mg_l) / 9.0)
    weight_factor = 1.0 + (fish_avg_weight_kg / 10.0) * 0.1
    raw           = (0.5 * oxygen_stress + 0.5 * base_stress) * weight_factor
    return round(min(raw, 1.0), 4)


@tool
async def query_vector_knowledge_base(
    query: str,
    collection: str = "biological_manuals",
    jurisdiction: str = "NORWAY",
    top_k: int = 5,
) -> str:
    """
    Semantic similarity search against the Qdrant regulatory and manual collections.
    Applies a mandatory jurisdiction filter to prevent cross-border regulatory confusion.

    Args:
        query:        Natural language query.
        collection:   Target Qdrant collection name.
        jurisdiction: ISO country name used as payload filter (default: NORWAY).
        top_k:        Number of results to retrieve.

    Returns:
        Newline-separated string of the top matching text chunks.

    Embedding provider: Google Generative AI (models/gemini-embedding-001, 768-dim).
    """
    # Run synchronous Google embedding in a thread to avoid blocking the event loop
    query_vector: list[float] = await asyncio.to_thread(_embed_query, query)

    client = _qdrant_client()
    # query_points() replaces the deprecated .search() in qdrant-client >= 1.7.0
    result = await client.query_points(
        collection_name=collection,
        query=query_vector,
        query_filter=Filter(
            must=[FieldCondition(key="jurisdiction", match=MatchValue(value=jurisdiction))]
        ),
        limit=top_k,
        with_payload=True,
    )
    await client.close()

    chunks = [
        f"[{r.payload.get('doc_id', 'N/A')}] {r.payload.get('text', '')}"
        for r in result.points
    ]
    return "\n---\n".join(chunks) if chunks else "No relevant documents found."


# ---------------------------------------------------------------------------
# Commercial Trader Tools
# ---------------------------------------------------------------------------


@tool
async def get_current_market_data(
    species: str = "ATLANTIC_SALMON",
    product_form: str = "FRESH_HOG",
) -> dict[str, Any]:
    """
    Retrieve the latest price tick for a given species/product_form from the
    market_vectors Qdrant collection (compacted cache from market.prices.v1).
    """
    client = _qdrant_client()
    composite_key = f"{species}:{product_form}"
    scroll_result = await client.scroll(
        collection_name="market_vectors",
        scroll_filter=Filter(
            must=[FieldCondition(key="partition_key", match=MatchValue(value=composite_key))]
        ),
        limit=1,
        with_payload=True,
    )
    await client.close()

    records, _ = scroll_result
    if not records:
        return {"error": f"No market data found for {composite_key}"}

    payload = records[0].payload
    return {
        "species": species,
        "product_form": product_form,
        "spot_price_per_kg": payload.get("spot_price_per_kg"),
        "bid_price_per_kg": payload.get("bid_price_per_kg"),
        "ask_price_per_kg": payload.get("ask_price_per_kg"),
        "price_change_pct": payload.get("price_change_pct"),
        "volatility_index_30d": payload.get("volatility_index_30d"),
        "analyst_consensus": payload.get("analyst_consensus"),
        "currency": payload.get("currency", "NOK"),
    }


@tool
def calculate_harvest_opportunity_cost(
    current_biomass_kg: float,
    current_spot_price_nok: float,
    futures_30d_price_nok: float,
) -> dict[str, Any]:
    """
    Project the financial gain or loss of delaying the harvest by 30 days.

    Returns current_revenue, projected_revenue, opportunity_cost_nok,
    and a human-readable recommendation string.
    """
    current_rev   = round(current_biomass_kg * current_spot_price_nok, 2)
    projected_rev = round(current_biomass_kg * futures_30d_price_nok, 2)
    cost          = round(projected_rev - current_rev, 2)

    if cost < 0:
        rec = "HARVEST_NOW — immediate harvest is financially superior."
    elif cost > 0:
        rec = f"HOLD — delaying 30 days could yield +{cost:,.0f} NOK extra revenue."
    else:
        rec = "NEUTRAL — spot and futures prices are equivalent."

    return {
        "current_biomass_kg": current_biomass_kg,
        "current_revenue_nok": current_rev,
        "projected_revenue_nok": projected_rev,
        "opportunity_cost_nok": cost,
        "recommendation": rec,
    }


# ---------------------------------------------------------------------------
# Judge Tools
# ---------------------------------------------------------------------------


@tool
async def verify_regulatory_claim(legal_text_snippet: str) -> bool:
    """
    Anti-hallucination check for the Judge agent.

    Performs a high-threshold vector similarity search (score >= 0.92) to
    confirm the given legal snippet actually exists in the knowledge base.
    Returns True if verified, False if the claim cannot be corroborated.

    Embedding provider: Google Generative AI (models/gemini-embedding-001, 768-dim).
    """
    # Run synchronous Google embedding in a thread to avoid blocking the event loop
    query_vector: list[float] = await asyncio.to_thread(_embed_query, legal_text_snippet)

    client = _qdrant_client()
    # query_points() replaces the deprecated .search() in qdrant-client >= 1.7.0
    # score_threshold filters out low-confidence matches server-side.
    result = await client.query_points(
        collection_name="fishing_regulations",
        query=query_vector,
        limit=1,
        with_payload=True,
        score_threshold=0.92,
    )
    await client.close()
    return len(result.points) > 0


@tool
def emit_final_verdict(verdict_json: dict[str, Any]) -> str:
    """
    Terminal tool that validates and records the Judge's structured verdict.
    Calling this signals LangGraph that the debate cycle is complete.
    """
    import json
    required = {"reasoning", "hallucination_detected", "recommended_action",
                "confidence_score", "cited_sources"}
    missing = required - set(verdict_json.keys())
    if missing:
        return f"ERROR: verdict_json is missing required fields: {sorted(missing)}"
    return json.dumps({"status": "verdict_recorded", "verdict": verdict_json})
