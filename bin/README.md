# bin/ — Infrastructure Automation

This directory contains all infrastructure-as-code and automation for OceanTrust AI.

## Subdirectories

| Directory | Purpose | Status |
|-----------|---------|--------|
| `docker/` | Docker Compose for local development (Redpanda, Kafka UI, Schema Registry) | 🔜 Phase 2 |
| `kubernetes/` | Kubernetes manifests for production workloads | 🔜 Phase 2 |
| `helm/` | Helm charts for parameterized deployments | 🔜 Phase 2 |

## Local Dev Stack (Planned — `docker/`)

The Docker Compose environment will provide:
- **Redpanda** — Kafka-compatible broker (single node for dev)
- **Redpanda Console** — Kafka UI for topic browsing and schema inspection
- **Schema Registry** — For enforcing `iot_sensor_event` and `market_data_event` schemas
- **Qdrant** — Vector database for LLM agent RAG pipeline

## Kubernetes Manifests (Planned — `kubernetes/`)

Namespaces:
- `oceantrust-streaming` — Redpanda cluster + producers/consumers
- `oceantrust-agents` — LangGraph orchestrator and LLM agent pods
- `oceantrust-storage` — Qdrant vector DB and persistent volumes
