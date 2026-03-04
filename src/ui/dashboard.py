"""
dashboard.py — OceanTrust AI · Control Tower
=============================================
Streamlit UI for real-time farm telemetry monitoring and agent debate inspection.

Architecture constraints:
    - ZERO imports from src.agents (fully decoupled read-only UI)
    - Telemetry injection via subprocess calling bin/scripts/simulate_alert.py
    - Debate history read from logs/audit/debates/**/*.json (newest-first)
    - RAG search via direct Qdrant client (no LangChain agent layer)

Sections:
    ┌─ Sidebar ──────────────────────────────────────┐
    │  Alert Injection form (simulate_alert.py)       │
    └─────────────────────────────────────────────────┘
    ┌─ Main panel ───────────────────────────────────┐
    │  Tab 1 · Debate History  (audit JSON files)    │
    │  Tab 2 · Regulatory RAG Search  (Qdrant)       │
    └─────────────────────────────────────────────────┘

Run:
    bin/scripts/run_ui.bat          # Windows
    ./bin/scripts/run_ui.sh         # Unix / WSL
    # or directly:
    PYTHONPATH=. streamlit run src/ui/dashboard.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Page configuration — must be the very first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="OceanTrust AI · Control Tower",
    page_icon="🐟",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PROJECT_ROOT: Path  = Path(__file__).resolve().parents[2]
_AUDIT_DIR: Path     = _PROJECT_ROOT / "logs" / "audit" / "debates"
_SIM_SCRIPT: Path    = _PROJECT_ROOT / "bin" / "scripts" / "simulate_alert.py"

_AGENT_COLORS: dict[str, str] = {
    "biologist":  "#2ecc71",   # green
    "commercial": "#3498db",   # blue
    "judge":      "#f39c12",   # amber
}

_VERDICT_BADGE: dict[str, str] = {
    "HARVEST_NOW":     "🔴",
    "HARVEST_PARTIAL": "🟠",
    "HOLD":            "🟡",
    "TREAT":           "🟣",
}


# ---------------------------------------------------------------------------
# Helper: load and sort debate JSON files (newest modification time first)
# ---------------------------------------------------------------------------

def _load_debates(limit: int = 50) -> list[dict]:
    """
    Scan logs/audit/debates/**/*.json recursively.
    Returns a list of parsed debate dicts sorted by file modification time,
    newest first. Falls back to an empty list if the directory does not exist.
    """
    if not _AUDIT_DIR.exists():
        return []

    json_files: list[Path] = sorted(
        _AUDIT_DIR.rglob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,   # newest first
    )

    debates: list[dict] = []
    for path in json_files[:limit]:
        try:
            debates.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass   # skip corrupted / empty files silently
    return debates


# ---------------------------------------------------------------------------
# Helper: inject alert event via subprocess → simulate_alert.py
# ---------------------------------------------------------------------------

def _inject_alert(
    farm_id: str,
    oxygen: float,
    temp: float,
    lice: float,
) -> tuple[bool, str]:
    """
    Publish a synthetic IoT telemetry alert to the Kafka topic by calling
    bin/scripts/simulate_alert.py as a subprocess.

    Uses sys.executable so the call runs inside the same virtual environment.
    Returns (success: bool, output: str).
    """
    cmd: list[str] = [
        sys.executable,
        str(_SIM_SCRIPT),
        "--farm-id", farm_id,
        "--oxygen",  str(oxygen),
        "--temp",    str(temp),
        "--lice",    str(lice),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_PROJECT_ROOT),
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


# ---------------------------------------------------------------------------
# Helper: RAG search (direct Qdrant query — zero src.agents imports)
# ---------------------------------------------------------------------------

def _rag_search(query: str, collection: str, top_k: int = 3) -> list[dict]:
    """
    Embed the query with Google Generative AI and search the given Qdrant
    collection.  Returns a list of scored result dicts.

    Falls back to an empty list with a logged warning on any error.
    Importing here (lazy) keeps startup fast when Qdrant/Google are unavailable.
    """
    try:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        embedder = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-001",
            google_api_key=os.getenv("GOOGLE_API_KEY", ""),
        )
        query_vector: list[float] = embedder.embed_query(query)

        client = QdrantClient(
            host=os.getenv("QDRANT_HOST", "localhost"),
            port=int(os.getenv("QDRANT_PORT", "6333")),
        )
        result = client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                "score":      round(p.score, 4),
                "doc_id":     p.payload.get("doc_id", "N/A"),
                "text":       p.payload.get("text", ""),
                "authority":  p.payload.get("authority", "N/A"),
                "effective":  p.payload.get("effective_date", "N/A"),
            }
            for p in result.points
        ]
    except Exception as exc:  # noqa: BLE001
        st.warning(f"RAG search error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Sidebar — Alert Injection Form
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    """Render the sidebar alert-injection form."""
    st.sidebar.title("🚨 Alert Injection")
    st.sidebar.caption(
        "Push a synthetic telemetry event to the Redpanda broker "
        "(`ocean.telemetry.v1`). The agent orchestrator will pick it up "
        "and start a multi-agent debate."
    )

    with st.sidebar.form("alert_injection_form"):
        farm_id = st.text_input(
            "Farm ID",
            value="NORD-02",
            help="Kafka partition key — must match a registered farm.",
        )
        oxygen = st.slider(
            "Dissolved Oxygen (mg/L)",
            min_value=0.0,
            max_value=15.0,
            value=4.1,
            step=0.1,
            help="Values below 7.0 trigger O2_CRITICAL alert.",
        )
        temp = st.slider(
            "Water Temperature (°C)",
            min_value=0.0,
            max_value=30.0,
            value=12.5,
            step=0.5,
        )
        lice = st.slider(
            "Sea Lice (per fish)",
            min_value=0.0,
            max_value=3.0,
            value=0.85,
            step=0.05,
            help="Norwegian regulatory limit: 0.5 per fish.",
        )
        submitted = st.form_submit_button("🚀 Publish Alert Event")

    if submitted:
        with st.sidebar:
            with st.spinner("Publishing to Kafka…"):
                ok, output = _inject_alert(farm_id, oxygen, temp, lice)
            if ok:
                st.success("✅ Event published successfully!")
            else:
                st.error("❌ Publish failed — check broker connectivity.")
            if output:
                st.code(output, language="text")


# ---------------------------------------------------------------------------
# Tab 1 — Debate History
# ---------------------------------------------------------------------------

def _render_debate_history() -> None:
    """Render sorted multi-agent debate records from the audit JSON files."""
    st.subheader("🤖 Multi-Agent Debate History")
    st.caption(
        f"Reading from `{_AUDIT_DIR.relative_to(_PROJECT_ROOT)}` — "
        "sorted by modification time, newest first."
    )

    if st.button("🔄 Refresh", key="refresh_debates"):
        st.rerun()

    debates = _load_debates()

    if not debates:
        st.info(
            "No debate records found yet. "
            "Publish an alert event from the sidebar and wait for the "
            "orchestrator to process it."
        )
        return

    for debate in debates:
        debate_id       = debate.get("debate_id", "unknown")
        farm_id         = debate.get("farm_id", "N/A")
        action          = debate.get("recommended_action", "N/A")
        confidence      = debate.get("confidence_score")
        hallucination   = debate.get("hallucination_detected", False)
        verdict_text    = debate.get("judge_verdict", "")
        bio_args        = debate.get("biologist_arguments", [])
        comm_args       = debate.get("commercial_arguments", [])
        cited           = debate.get("cited_sources", [])

        badge       = _VERDICT_BADGE.get(action, "⚪")
        conf_label  = f"{confidence:.0%}" if isinstance(confidence, float) else "N/A"
        hall_label  = "⚠️ YES" if hallucination else "✅ NO"

        with st.expander(
            f"{badge} **{action}** — Farm `{farm_id}` · Debate `{debate_id[:8]}…`",
            expanded=False,
        ):
            col_meta1, col_meta2, col_meta3 = st.columns(3)
            col_meta1.metric("Recommended Action", action)
            col_meta2.metric("Confidence",          conf_label)
            col_meta3.metric("Hallucination",        hall_label)

            if verdict_text:
                st.markdown("##### ⚖️ Judge Reasoning")
                st.markdown(verdict_text)

            if bio_args:
                # Extract color lookups into variables — avoids backslashes
                # inside f-string expressions (invalid in Python < 3.12).
                color_bio = _AGENT_COLORS["biologist"]
                label_bio = "🔬 Biologist"
                st.markdown(
                    f"<div style='border-left:4px solid {color_bio};"
                    f"padding:8px 12px;margin:6px 0;border-radius:4px;'>"
                    f"<strong style='color:{color_bio}'>{label_bio}</strong><br>"
                    f"{bio_args[-1]}</div>",
                    unsafe_allow_html=True,
                )

            if comm_args:
                color_comm = _AGENT_COLORS["commercial"]
                label_comm = "📈 Commercial"
                st.markdown(
                    f"<div style='border-left:4px solid {color_comm};"
                    f"padding:8px 12px;margin:6px 0;border-radius:4px;'>"
                    f"<strong style='color:{color_comm}'>{label_comm}</strong><br>"
                    f"{comm_args[-1]}</div>",
                    unsafe_allow_html=True,
                )

            if cited:
                st.markdown(f"**📚 Cited Sources:** `{'`, `'.join(cited)}`")


# ---------------------------------------------------------------------------
# Tab 2 — Regulatory RAG Search
# ---------------------------------------------------------------------------

def _render_rag_search() -> None:
    """Render the Qdrant regulatory knowledge-base search panel."""
    st.subheader("🔍 Regulatory Knowledge Search")
    st.caption(
        "Embed your question with Google Generative AI and retrieve the top "
        "matching chunks from the `fishing_regulations` Qdrant collection."
    )

    collection = st.selectbox(
        "Target Collection",
        options=["fishing_regulations", "biological_manuals", "scientific_papers"],
        index=0,
    )
    query = st.text_input(
        "Query",
        placeholder="e.g. What is the Norwegian lice treatment threshold?",
    )
    top_k = st.slider("Max Results", min_value=1, max_value=10, value=3)

    if st.button("🔎 Search", disabled=not query):
        with st.spinner("Embedding and searching Qdrant…"):
            results = _rag_search(query, collection, top_k)

        if not results:
            st.warning("No results found. Check Qdrant connectivity or the collection contents.")
            return

        for i, r in enumerate(results, start=1):
            with st.expander(
                f"#{i} · Score `{r['score']}` · `{r['doc_id']}` — {r['authority']}",
                expanded=(i == 1),
            ):
                st.markdown(f"**Effective date:** `{r['effective']}`")
                st.markdown(r["text"])


# ---------------------------------------------------------------------------
# Main — page assembly
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point — assemble and render the Control Tower dashboard."""
    # ── Page header ─────────────────────────────────────────────────────────
    st.title("🐟 OceanTrust AI · Control Tower")
    st.markdown(
        "Real-time aquaculture monitoring dashboard. "
        "Inject alerts from the sidebar and inspect multi-agent debate verdicts below."
    )
    st.divider()

    # ── Sidebar ──────────────────────────────────────────────────────────────
    _render_sidebar()

    # ── Main tabs ────────────────────────────────────────────────────────────
    tab_debates, tab_rag = st.tabs(["🤖 Debate History", "🔍 Regulatory Search"])

    with tab_debates:
        _render_debate_history()

    with tab_rag:
        _render_rag_search()

    # ── Footer ───────────────────────────────────────────────────────────────
    st.divider()
    st.caption(
        f"OceanTrust AI · {datetime.now().strftime('%Y-%m-%d %H:%M')} local · "
        "Audit logs: `logs/audit/debates/`"
    )


if __name__ == "__main__":
    main()
