# OceanGuard AI

OceanGuard AI is an educational prototype for exploring event-driven aquaculture monitoring. It connects simulated sensor events to a multi-agent workflow, stores vector data in Qdrant, and shows saved decisions in a Streamlit dashboard.

![Python 3.12](https://img.shields.io/badge/Python-3.12-3670A0?style=flat-square&logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/Agents-LangGraph-111111?style=flat-square)
![Redpanda](https://img.shields.io/badge/Events-Redpanda-EF3340?style=flat-square)
![Qdrant](https://img.shields.io/badge/Vector%20store-Qdrant-D13A4F?style=flat-square)

## Project flow

1. The producer sends generated telemetry events to Redpanda
2. The orchestrator sends each event through a LangGraph discussion between biology, commercial, and judge agents
3. The workflow reads reference material and vector records from Qdrant
4. Debate records are saved as JSON files for the Streamlit dashboard

The repository also includes a vector worker for telemetry embeddings and scripts for loading knowledge documents and running sample alerts. See [the architecture guide](docs/architecture.md) and [the orchestration notes](docs/agents-orchestration.md) for more detail.

## Requirements

- Python 3.12 or later
- Docker with Docker Compose
- Google and OpenAI API keys for the configured model and embedding calls
- Ollama is included in the local Compose setup as an embedding fallback

Copy `.env.example` to `.env` and add the keys needed for the parts you plan to run. Keep `.env` private.

## Run locally

```bash
python -m pip install -r requirements.txt
docker compose -f bin/docker/docker-compose.yml up -d redpanda qdrant
```

Start the producer and orchestrator in separate terminals:

```bash
python -m src.ingestion.ocean_producer
python -m src.agents.main
```

The sample producer generates telemetry from the local farm configuration. The repository does not include physical sensor integration or a live market data feed. Market records must already be available to the configured Qdrant collection.

## Important limits

This is a learning prototype, not an operational aquaculture or compliance system. The oxygen override and other thresholds are code examples and have not been validated for real farms. Agent output may be wrong; do not use it to make animal-health, harvesting, or legal decisions.

AI tools were used during development. The prototype and its sample thresholds need independent review before they are used beyond a local demonstration.

## Project structure

```text
src/
├── agents/       LangGraph workflow and agent tools
├── ingestion/    Knowledge loader and telemetry producer
├── processing/   Telemetry embedding worker
└── ui/           Streamlit dashboard
bin/
├── docker/       Local service configuration
└── scripts/      Provisioning and demo utilities
docs/             Architecture, schemas, and design notes
schemas/          Event schemas
data/knowledge/   Sample reference documents
```

## Author

Héctor Zamorano García

## License

See [LICENSE](LICENSE).
