# OceanGuard AI

Multi-Agent Intelligence System for Autonomous Aquaculture Decision-Making

Version: 3.0.0 | Status: Production-Ready | License: MIT

![Python 3.11](https://img.shields.io/badge/python-v3.11-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
![LangGraph 0.1](https://img.shields.io/badge/LangGraph-v0.1-000000?style=for-the-badge&logo=langchain&logoColor=white)
![Google Gemini 2.0](https://img.shields.io/badge/Google%20Gemini-v2.0-8E75C2?style=for-the-badge&logo=googlegemini&logoColor=white)
![Redpanda](https://img.shields.io/badge/Redpanda-latest-FF0000?style=for-the-badge&logo=redpanda&logoColor=white)
![Qdrant](https://img.shields.io/badge/Qdrant-latest-D13A4F?style=for-the-badge&logo=qdrant&logoColor=white)

## Overview

OceanGuard AI is an event-driven, asynchronous platform that orchestrates LLM-based agents
to issue real-time operational verdicts for Norwegian salmon farms. When IoT sensors detect
threshold breaches, the system triggers a structured multi-agent debate governed by both
regulatory law and financial constraints, producing an auditable, traceable decision record.

The architecture is designed for scalability and regulatory compliance. The AI core is fully
decoupled from the monitoring UI via Redpanda, and all decisions are backed by a deterministic
safety layer that bypasses LLM inference for life-critical scenarios.

---

## Directory Structure

```
OceanGuard/
|-- src/
|   |-- agents/         LangGraph debate graph, state machine, RAG tools
|   |-- ingestion/      knowledge_loader.py (Qdrant) + ocean_producer.py (Redpanda)
|   |-- processing/     vector_worker.py — async embedding pipeline
|   `-- ui/             dashboard.py — read-only Streamlit Control Tower
|
|-- bin/
|   |-- docker/         docker-compose.yml: Redpanda, Qdrant, Redpanda Console
|   `-- scripts/        simulate_alert.py, run_ui.sh, run_ui.bat, provision scripts
|
|-- docs/               Architecture specs, ADRs, agent and schema documentation
|-- schemas/            JSON Schema definitions for IoT events and market data
|-- data/knowledge/     Regulatory documents for Qdrant ingestion
|-- logs/audit/debates/ Immutable JSON debate records (one per Kafka event processed)
`-- config/             Farm configuration register
```

---

## System Architecture

The system is divided into four loosely coupled planes:

```
IoT Sensors / Smoke Test Script
        |
        v
ocean_producer.py  --------->  Redpanda  (ocean.telemetry.v1)
                                    |
              +---------------------+---------------------+
              |                                           |
              v                                           v
    vector_worker.py                          src/agents/main.py
    (Qdrant embeddings)                       (LangGraph Orchestrator)
                                                          |
                                    +---------------------+---------------------+
                                    |                     |                     |
                                    v                     v                     v
                               Biologist Node     Commercial Node          Judge Node
                               (Risk/Law)         (ROI/Market)             (Arbitration)
                                    |                     |                     |
                                    +---------------------+                     |
                                                                                v
                                                                   logs/audit/debates/
                                                                   (JSON audit trace)
                                                                                |
                                                                                v
                                                               src/ui/dashboard.py
                                                               (read-only Streamlit UI)
```

Redpanda operates a dual-listener topology. Internal Docker traffic uses
`internal://redpanda:9092`. Host-side scripts (producers, consumers running outside Docker)
use `external://127.0.0.1:19092`. This separation prevents IPv6 resolution failures on Windows.

---

## Deterministic Safety Layer — Hard Override Protocol

The Judge node implements a code-level safety check that executes before any call to the
Gemini API. This guard is not prompt-based — it is pure Python evaluated deterministically
on every invocation of the judge node.

Trigger condition: `dissolved_oxygen_mg_l < 4.0 mg/L`

When triggered, the function returns immediately with:

```python
# src/agents/graph.py — judge_node()

_O2_LETHAL_THRESHOLD = 4.0  # mg/L — immediate mass mortality risk

if current_o2 is not None and current_o2 < _O2_LETHAL_THRESHOLD:
    log.warning("judge_hard_override_triggered", dissolved_oxygen_mg_l=current_o2)
    return {
        "recommended_action":  "HARVEST_NOW",
        "confidence_score":    1.0,
        "judge_verdict":       "EMERGENCY BIOLOGICAL OVERRIDE: ...",
        "cited_sources":       ["HARD_OVERRIDE", "Akvakulturloven_§12"],
        "hallucination_detected": False,
    }
```

Rationale for a deterministic guard over prompt engineering:

- LLMs cannot guarantee 100% instruction compliance under all context conditions.
- Oxygen below 4.0 mg/L causes mass mortality within minutes, not hours.
- Akvakulturloven §12 establishes legally binding treatment thresholds with no exceptions.
- The override emits zero API calls, achieving immediate response with no latency or cost.

The Biologist and Commercial nodes still run prior to the Judge. Their arguments are captured
in the audit trace even when the override fires, preserving a complete evidentiary record.

---

## Tech Stack

| Layer              | Technology                       | Notes                                   |
|--------------------|----------------------------------|-----------------------------------------|
| Agent Orchestration| LangGraph 0.1+                   | Finite state machine, max 1 revision    |
| LLM                | Google Gemini 2.5 Flash Lite     | max_retries=5 for Free Tier 429 errors  |
| Embeddings         | models/gemini-embedding-001      | 768-dimension vectors, Cosine distance  |
| Vector DB          | Qdrant latest                    | query_points() API, metadata filtering  |
| Message Broker     | Redpanda                         | Kafka-compatible, dual-listener setup   |
| UI                 | Streamlit                        | No src/agents imports — fully decoupled |
| Containers         | Docker Compose                   | Single-node dev configuration           |
| Language           | Python 3.11+                     | asyncio + aiokafka throughout           |

---

## Setup and Deployment

### Prerequisites

- Docker Desktop with WSL2 backend (Windows) or Docker Engine (Linux/macOS)
- Python 3.11 or later
- A valid GOOGLE_API_KEY from Google AI Studio

### Step 1 — Clone and install

```bash
git clone https://github.com/HectorZamoranoGarcia/Smart-aquaculture-AI-ecosystem.git
cd Smart-aquaculture-AI-ecosystem

python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
# source .venv/bin/activate     # Linux / macOS

pip install -r requirements.txt
```

### Step 2 — Configure environment variables

```bash
export GOOGLE_API_KEY="your-key-here"
export KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:19092"
export PYTHONPATH=$(pwd)
```

The orchestrator validates both variables at startup and raises RuntimeError if either
is missing, providing a clear message before any Kafka or LLM connection is attempted.

### Step 3 — Start infrastructure

```bash
docker compose -f bin/docker/docker-compose.yml up -d redpanda qdrant

# Verify broker readiness
docker exec redpanda rpk cluster info
```

### Step 4 — Index the regulatory knowledge base

```bash
python -m src.ingestion.knowledge_loader --data-dir data/knowledge
```

This command reads all Markdown documents from `data/knowledge/`, chunks them, generates
768-dimension embeddings via `models/gemini-embedding-001`, and upserts them to the
`fishing_regulations` collection in Qdrant.

### Step 5 — Start the agent orchestrator

```bash
# Terminal 1
python -m src.agents.main
```

The orchestrator connects to Redpanda on `KAFKA_BOOTSTRAP_SERVERS`, joins consumer group
`cg-agent-orchestrator`, and processes each alert event through the LangGraph debate graph.
Kafka offsets are committed manually after the full debate completes — no event is lost
if the process crashes mid-debate.

### Step 6 — Send a test alert

```bash
# Terminal 2
python bin/scripts/simulate_alert.py --farm-id "NORD-02" --oxygen 4.1 --lice 0.85

# Trigger the deterministic Hard Override
python bin/scripts/simulate_alert.py --farm-id "NORD-02" --oxygen 0.0 --lice 2.0
```

### Step 7 — Launch the Control Tower UI

```bash
# Windows CMD / PowerShell
bin\scripts\run_ui.bat

# Git Bash / WSL / Linux
./bin/scripts/run_ui.sh
```

The UI is available at `http://localhost:8501`. It reads debate records from
`logs/audit/debates/` and exposes a RAG search panel backed by Qdrant. It imports
zero modules from `src/agents/` — all coupling is through the filesystem and Qdrant.

---

## Agent Roles

### Biologist Node — Risk Assessment

Queries the `fishing_regulations` Qdrant collection using the farm's jurisdiction as a
required metadata filter. Produces a structured risk assessment citing specific legal
articles. Evaluates dissolved oxygen, sea lice count, temperature, and mortality indicators
against Norwegian Akvakulturloven thresholds.

### Commercial Node — Financial ROI

Evaluates current market conditions, historical price trends, and harvest cost projections.
Produces a structured financial argument for or against immediate harvest. If no market data
is available, the node returns a neutral position — the Judge treats this as neither
supporting nor opposing the Biologist's recommendation.

### Judge Node — Final Arbitration

Receives arguments from both prior nodes, checks Biologist legal citations against the RAG
context to detect hallucinations, then emits a final structured verdict. Before invoking the
LLM, the Hard Override check runs — if oxygen is below the lethal threshold, the LLM is
never called and HARVEST_NOW is returned immediately with confidence 1.0.

Valid verdict actions: `HARVEST_NOW`, `HARVEST_PARTIAL`, `HOLD`, `TREAT`.

---

## Audit and Compliance

Every debate produces an immutable JSON record at
`logs/audit/debates/<YYYY-MM-DD>/<debate_id>.json`:

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

Records are sorted by file modification time. The Streamlit UI renders the newest debate
first. The `hallucination_detected` flag triggers an escalation log entry at ERROR level,
tagged for human operator review.

---

## Key Design Decisions

| Decision                         | Alternative             | Reason                                               |
|----------------------------------|-------------------------|------------------------------------------------------|
| Deterministic pre-LLM override   | Prompt engineering only | LLMs cannot guarantee compliance in all contexts     |
| Redpanda dual-listener topology  | Single port binding     | Isolates Docker-internal and host-external traffic   |
| Manual Kafka offset commit       | Auto-commit             | Offset committed only after debate — zero event loss |
| Qdrant query_points() API        | .search() deprecated    | API changed in qdrant-client >= 1.7.0                |
| max_retries=5 on ChatGoogleAI    | No retry                | Free Tier quota: HTTP 429 backoff handled by LangChain|
| gzip compression on producer     | snappy                  | No native snappy library required; stdlib gzip works  |

---

## Author

Héctor Zamorano García

## Notes

* This project was developed for educational purposes to explore the synergy between multi-agent systems and real-time event streaming in industrial environments.
* The development process embraced the vibe coding philosophy, focusing on high-level orchestration and rapid iteration through natural language interaction with advanced AI models.
* This implementation serves as a technical sandbox to test next-generation AI tools and explore the shifting paradigm of software engineering, emphasizing that modern engineers must adapt to AI-native workflows to remain competitive.
* The project demonstrates how to bridge the gap between probabilistic AI outputs and deterministic safety requirements in critical sectors like aquaculture.
* Occasional AI assistance was utilized for complex logic verification, architectural brainstorming, and debugging distributed system latencies during the orchestration phase.

## License

Standard MIT License.
