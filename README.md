# OceanGuard: Multi-Agent AI for Autonomous Aquaculture Decision-Making

> **Autonomous orchestration system** combining *event-driven architecture*, *RAG*, and *multi-agent debate* to issue real-time regulatory verdicts for Norwegian salmon farms.

---

## 📐 Architecture Overview

The system is built on **strict layer separation** — the AI core never couples with the monitoring UI:

```
OceanGuard/
├── src/
│   ├── agents/         # AI Core: LangGraph debate graph, state machine, RAG tools
│   ├── ingestion/      # Knowledge loader (Qdrant) + telemetry producer (Redpanda)
│   ├── processing/     # Vector worker — async embedding pipeline
│   └── ui/             # Control Tower (Streamlit) — read-only, zero src/agents imports
├── bin/
│   ├── docker/         # docker-compose.yml: Redpanda, Qdrant, Ollama, Redpanda Console
│   └── scripts/        # simulate_alert.py (smoke test), run_ui.sh / run_ui.bat
├── docs/               # ADRs, architecture diagrams, agent specifications
├── data/knowledge/     # Regulatory documents indexed in Qdrant
└── logs/audit/debates/ # Immutable JSON debate traces (one file per event)
```

**Data flow:**

```
IoT Sensor → Redpanda (ocean.telemetry.v1)
                │
                ├─► Vector Worker  →  Qdrant (embeddings)
                └─► Agent Orchestrator (src/agents/main.py)
                        │
                        └─► LangGraph Debate Graph
                                ├── Biologist Node
                                ├── Commercial Node
                                └── Judge Node → Audit JSON
                                                     │
                                             Streamlit UI (read-only)
```

UI ↔ AI decoupling is achieved via **Redpanda as a message bus** and **direct JSON audit file reads** — the UI never imports any module from `src/agents/`.

---

## 🛡️ Biological Override Protocol (Life-First Protocol)

The system implements a **deterministic code-level guard** inside `judge_node` that executes **before any LLM call**:

```python
# src/agents/graph.py — judge_node()
_O2_LETHAL_THRESHOLD = 4.0  # mg/L

if current_o2 is not None and current_o2 < _O2_LETHAL_THRESHOLD:
    return {
        "recommended_action": "HARVEST_NOW",
        "confidence_score": 1.0,
        "reasoning": "EMERGENCY BIOLOGICAL OVERRIDE: ...",
        "cited_sources": ["HARD_OVERRIDE", "Akvakulturloven_§12"],
    }
```

**Why deterministic and not prompt-based:**
- LLMs cannot guarantee 100% instruction compliance under all context conditions
- O₂ < 4.0 mg/L implies mass mortality risk within minutes
- Norwegian Aquaculture Act (*Akvakulturloven* §12) establishes absolute legal thresholds

The override **returns immediately**, skipping the Gemini API call entirely — zero latency, zero cost in the worst biological scenario.

---

## 🥞 Tech Stack

| Layer | Technology | Role |
|---|---|---|
| **Agent Orchestration** | [LangGraph](https://langchain-ai.github.io/langgraph/) | Multi-agent state machine with revision rounds |
| **LLM** | Google Gemini 2.5 Flash Lite | Biologist, Commercial, Judge agents |
| **Embeddings** | Google `gemini-embedding-001` | Regulatory document vectorisation (768 dims) |
| **Vector DB** | [Qdrant](https://qdrant.tech/) v1.9+ | Semantic search over regulatory knowledge base |
| **Message Broker** | [Redpanda](https://redpanda.com/) | Kafka-compatible telemetry bus |
| **UI** | [Streamlit](https://streamlit.io/) | Control Tower — debate history and RAG search |
| **Containers** | Docker Compose | Full dev environment orchestration |
| **Language** | Python 3.11+ | Async/await via `asyncio` + `aiokafka` |

---

## 🚀 Setup & Execution

### Prerequisites
- Docker Desktop (WSL2 on Windows) or Docker Engine on Linux/macOS
- Python 3.11+
- An active `GOOGLE_API_KEY` ([Google AI Studio](https://aistudio.google.com/))

### 1. Clone and set up the environment

```bash
git clone https://github.com/HectorZamoranoGarcia/Smart-aquaculture-AI-ecosystem.git
cd Smart-aquaculture-AI-ecosystem

# Create and activate virtual environment
python -m venv .venv
source .venv/Scripts/activate    # Windows (Git Bash)
# source .venv/bin/activate      # Linux / macOS

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
export GOOGLE_API_KEY="your-key-here"
export KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:19092"   # IP avoids IPv6 issues on Windows
export PYTHONPATH=$(pwd)
```

### 3. Start the infrastructure (Redpanda + Qdrant)

```bash
docker compose -f bin/docker/docker-compose.yml up -d redpanda qdrant

# Verify Redpanda is healthy:
docker compose -f bin/docker/docker-compose.yml ps
```

### 4. Index the regulatory knowledge base

```bash
# Indexes docs in data/knowledge/ → fishing_regulations collection in Qdrant
python -m src.ingestion.knowledge_loader --data-dir data/knowledge
```

### 5. Start the agent orchestrator (Backend)

```bash
# Terminal 1 — Kafka consumer + LangGraph debate engine
python -m src.agents.main
```

### 6. Send a test alert

```bash
# Terminal 2 — Smoke test: critical O₂ + lice breach
python bin/scripts/simulate_alert.py \
    --farm-id "NORD-02" \
    --oxygen 4.1 \
    --lice 0.85

# Deterministic override (O₂ < 4.0 → HARVEST_NOW, no LLM call):
python bin/scripts/simulate_alert.py --farm-id "NORD-02" --oxygen 0.0 --lice 2.0
```

### 7. Launch the Control Tower UI

```bash
# Windows (CMD / PowerShell)
bin\scripts\run_ui.bat

# Git Bash / WSL / Linux
./bin/scripts/run_ui.sh

# Available at http://localhost:8501
```

---

## 🤖 Decision Flow: The Three Agents

```
┌─────────────────────────────────────────────────────────────────┐
│                  JUDGE NODE (Pre-LLM Check)                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ O₂ < 4.0 mg/L? → HARVEST_NOW (deterministic, no LLM)   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                        ↓ (if O₂ OK)                            │
├─────────────────┬──────────────────┬───────────────────────────┤
│  🔬 BIOLOGIST   │  📈 COMMERCIAL   │  ⚖️ JUDGE                 │
│                 │                  │                           │
│ Evaluates:      │ Evaluates:       │ Final arbitration:        │
│ • Water params  │ • Current market │ • Verifies legal          │
│ • Mortality     │   price          │   compliance (RAG)        │
│   risk          │ • ROI projection │ • Detects hallucinations  │
│ • Akvakulturloven│ • Treatment cost│ • Issues verdict:         │
│   §12 compliance│ • Price trend    │   HARVEST_NOW /           │
│                 │                  │   HARVEST_PARTIAL /       │
│                 │                  │   HOLD / TREAT            │
└─────────────────┴──────────────────┴───────────────────────────┘
```

**Judge's golden rule:** Biological safety and *Akvakulturloven* legal compliance always override financial uncertainty. Missing market data is treated as a **Neutral** position — never as justification for HOLD during a biological emergency.

---

## 📋 Audit & Compliance

Every debate generates an immutable JSON file at `logs/audit/debates/<YYYY-MM-DD>/<debate_id>.json`:

```json
{
  "audit_version": "1.0",
  "debate_id": "ed0ce3a5-...",
  "farm_id": "NORD-02",
  "recommended_action": "HARVEST_NOW",
  "confidence_score": 1.0,
  "hallucination_detected": false,
  "judge_verdict": "EMERGENCY BIOLOGICAL OVERRIDE: ...",
  "cited_sources": ["HARD_OVERRIDE", "Akvakulturloven_§12"],
  "biologist_arguments": ["..."],
  "commercial_arguments": ["..."],
  "revision_count": 0
}
```

These records enable:
- **Regulatory traceability**: Every autonomous decision is documented with its legal basis
- **Hallucination auditing**: `hallucination_detected: true` escalates to human operators
- **Replay & debugging**: The complete debate state is fully reproducible
- **Compliance retention**: Configurable via `AUDIT_LOG_DIR` environment variable

---

## ⚡ Key Architectural Decisions

| Decision | Alternative considered | Reason |
|---|---|---|
| Deterministic pre-LLM override | Prompt engineering alone | LLMs cannot guarantee 100% instructional compliance |
| Redpanda as message bus | Direct HTTP polling | Full UI ↔ Backend decoupling + message replay |
| Qdrant for RAG | ChromaDB / FAISS | Native async client, gRPC support, metadata filters |
| `gemini-2.5-flash-lite` | GPT-4o-mini | Free Tier compatible, `max_retries=5` for HTTP 429 |
| Manual Kafka offset commit | Auto-commit | Offset committed only AFTER debate completes — zero event loss |

---

*OceanGuard AI — Héctor Zamorano García — MIT License 2026*
