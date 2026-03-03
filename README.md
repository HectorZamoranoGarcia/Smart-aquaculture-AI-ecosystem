# OceanTrust AI

Project focused on real-time aquaculture risk management using a distributed **multi-agent LLM debate** orchestrated with **LangGraph**, powered by event-streaming with **Redpanda** and retrieval-augmented generation with **Qdrant** — entirely migrated to **Google Gemini** for deterministic reasoning and embedding extraction.

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![Redpanda](https://img.shields.io/badge/Redpanda-v24.3-E63946?logo=apachekafka&logoColor=white)
![Qdrant](https://img.shields.io/badge/Qdrant-latest-orange)
![LangGraph](https://img.shields.io/badge/LangGraph-0.2-4CAF50)
![Gemini](https://img.shields.io/badge/Gemini-2.5_Flash_Lite-1A73E8?logo=google&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

## Project Overview

OceanTrust AI is a production-grade distributed platform that automates critical operational decisions in industrial salmon farming. The system resolves the core commercial dilemma operators face daily: when an IoT sensor detects a biological emergency (e.g., low dissolved oxygen, sea lice outbreak), does regulatory compliance or market timing take priority?

The architecture implements a **deterministic, auditable multi-agent workflow** from first principles. It consumes telemetry via Kafka topics, maps payload data against legal texts using a vector database, and delegates conflict resolution to a LangGraph network where three specialized agents (Biologist, Commercial Trader, Judge) debate over objective data before issuing a structured verdict. All dependencies on OpenAI have been replaced by Google's **Gemini 2.5 Flash Lite** and `models/gemini-embedding-001`, resulting in faster inference, reduced latency, and native JSON output compliance.

## Architecture & Tech Stack

### 1. Event-Driven Infrastructure (Redpanda)
The messaging backbone leverages Redpanda deployed in a single-node configuration with a **Dual Listener topology**:
- Internal traffic (Docker subnet containers, e.g., vector worker, RAG orchestrator) routes via `internal://redpanda:9092`.
- External traffic (host scripts, e.g., `simulate_alert.py`) routes via `external://localhost:19092`.
- The telemetry producer and market feed generator utilize native `gzip` compression via `AIOKafkaProducer`, abandoning heavy C++ dependencies like `snappy`.

### 2. Vector Database (Qdrant)
The persistence layer for legal documentation is powered by Qdrant (`latest` version), fully updated to support the **Universal Query API** (`query_points`).
- Collections (`fishing_regulations`, `market_vectors`) are provisioned with **768 dimensions** and `Cosine` distance metrics to perfectly align with Gemini's embedding output size.

### 3. AI Cognitive Engine (Google Gemini)
The cognitive layer is powered exclusively by the Google AI stack:
- **LLM Engine:** `gemini-2.5-flash-lite` serves as the engine for all LangGraph nodes. Network resilience is guaranteed through an Exponential Backoff strategy (`max_retries=5`) utilizing the `tenacity` library to mitigate `429 RESOURCE_EXHAUSTED` errors during parallel batching.
- **Embeddings:** `models/gemini-embedding-001` generates the 768-dim vectors via `GoogleGenerativeAIEmbeddings` for the semantic ingestion pipeline.

### 4. Multi-Agent Workflow (LangGraph)
The pipeline subscribes to `ocean.telemetry.v1`, triggering the RAG flow. Three distinct roles evaluate the payload:
- **Biologist Agent:** Queries the Qdrant knowledge base, prioritizing biological welfare and statutory limits.
- **Commercial Agent:** Prioritizes harvest opportunity cost and market timing.
- **Judge Agent:** Arbitrates the debate, resolves hallucinations, and outputs a final decision.

### Repository Structure

```text
oceantrust-ai/
├── bin/
│   ├── docker/
│   │   └── docker-compose.yml         # Redpanda Dual Listener + Qdrant latest
│   └── scripts/
│       ├── provision_infrastructure.sh
│       └── provision_qdrant.sh        # Provisions 768-dim collections
├── config/
│   └── farms_config.json              # 5 simulated farms (Norway, Scotland, Chile)
├── data/
│   └── knowledge/
│       └── norway_aquaculture_law.md  # Akvakulturloven
├── docs/
│   ├── architecture.md                # System design and Gemini integration specs
│   ├── agents-orchestration.md        # LangGraph StateGraph design
│   ├── vector-worker-spec.md          # Consumer loop logic
│   └── ...
├── logs/
│   └── audit/
│       └── debates/                   # JSON traceability sink (Mandatory regulatory compliance)
├── schemas/
│   └── iot_sensor_event.schema.json
├── src/
│   ├── agents/
│   │   ├── main.py                    # LangGraph orchestrator consumer
│   │   ├── state.py                   # TypedDict state registry
│   │   └── tools.py
│   ├── ingestion/
│   │   ├── ocean_producer.py          # AIOKafkaProducer with gzip
│   │   └── knowledge_loader.py        # Gemini 768-dim embedding script
│   └── workers/
│       └── vector_worker.py
├── .env.example
├── Makefile
└── README.md
```

## Prerequisites & Environment Variables

You must install Docker Desktop and Python 3.12+.
Variables required in your `.env` file at the repository root:

```dotenv
# Required for Gemini 2.5 Flash Lite and Embeddings
GOOGLE_API_KEY=AIzaSyYourKeyHere...

# Dual Listener Endpoints (Internal for Docker, External for Host scripts)
KAFKA_BOOTSTRAP_SERVERS=localhost:19092
KAFKA_INTERNAL_SERVERS=redpanda:9092

# Qdrant Database
QDRANT_URL=http://localhost:6333
```

## Local Deployment

Launch the entire stack using the `docker-compose.yml` configuration. This brings up Redpanda (with the dual listener profile) and Qdrant.

```bash
cd bin/docker
docker compose up -d
```

Verify the health of the containers, then provision the schemas and collections:

```bash
# From the project root
make provision
```

*Note: The script `provision_qdrant.sh` creates the `fishing_regulations` and `market_vectors` collections natively set to 768 dimensions.*

## Running the Pipeline

**1. Load the Knowledge Base**
Embed the Norwegian aquaculture regulations using `models/gemini-embedding-001` and upsert into Qdrant:
```bash
python -m src.ingestion.knowledge_loader
```

**2. Start the Telemetry Simulator**
Generate simulated IoT payloads compressed via `gzip` to the external Redpanda listener (`localhost:19092`):
```bash
python -m src.ingestion.ocean_producer --mode telemetry
```

**3. Launch the LangGraph Orchestrator**
Start the main consumer that orchestrates the `gemini-2.5-flash-lite` multi-agent debate:
```bash
python -m src.agents.main
```

## Audit & Observability

Mandatory traceability is a core requirement for aquaculture regulatory compliance.
The system enforces strict audit trails: every decision resolved by the LangGraph workflow is persisted locally as a structured document before being acknowledged to the Kafka broker.

Navigate to the audit directory to inspect the generated trails:
```bash
cat logs/audit/debates/<debate_id>.json
```
These JSON files contain the full array of trigger alerts, the query history mapped to the Qdrant knowledge base, the respective arguments formulated by the Biologist and Commercial agents, and the final deterministic verdict issued by the Judge — alongside its calculated confidence score.
