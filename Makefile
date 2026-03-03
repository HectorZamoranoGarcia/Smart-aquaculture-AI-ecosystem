# =============================================================================
# Makefile — OceanTrust AI Developer Shortcuts
# =============================================================================
# Infrastructure targets:
#   make up              Start the full local stack (Redpanda + Qdrant + Ollama)
#   make down            Stop containers (volumes preserved)
#   make provision       Create Kafka topics AND Qdrant collections
#   make provision-kafka Create Redpanda topics only
#   make provision-qdrant Create Qdrant collections only
#   make status          Show service health and endpoint summary
#   make logs            Tail Redpanda broker logs
#   make logs-all        Tail all service logs
#   make clean           Full teardown — remove containers, volumes, caches
#   make help            Show this message
# =============================================================================

DOCKER_COMPOSE_FILE  := bin/docker/docker-compose.yml
PROVISION_KAFKA      := bin/scripts/provision_infrastructure.sh
PROVISION_QDRANT     := bin/scripts/provision_qdrant.sh
SHELL                := /usr/bin/env bash

.PHONY: up down provision provision-kafka provision-qdrant status logs logs-all clean dashboard help

# -----------------------------------------------------------------------------
# help — Print all available targets
# -----------------------------------------------------------------------------
help:
	@echo ""
	@echo "  OceanTrust AI — Developer Makefile"
	@echo "  ────────────────────────────────────────────────────"
	@echo "  Infrastructure:"
	@echo "    up               Start full stack (Redpanda + Qdrant + Ollama)"
	@echo "    down             Stop containers (volumes preserved)"
	@echo "    clean            Full teardown — containers, volumes, Python caches"
	@echo ""
	@echo "  Provisioning:"
	@echo "    provision        Create Redpanda topics + Qdrant collections"
	@echo "    provision-kafka  Create Redpanda topics only"
	@echo "    provision-qdrant Create Qdrant collections + payload indices"
	@echo ""
	@echo "  Knowledge Ingestion:
    load-knowledge   Embed all Markdown docs from data/knowledge/ into Qdrant
    dry-run-loader   Validate chunking without calling OpenAI or Qdrant

  Observability:"
	@echo "    status           Print service health and endpoint URLs"
	@echo "    logs             Tail Redpanda broker logs"
	@echo "    logs-all         Tail all service logs"
	@echo "    dashboard        Launch the Streamlit Control Tower UI"
	@echo "  ────────────────────────────────────────────────────"
	@echo ""

# -----------------------------------------------------------------------------
# up — Start the full OceanTrust dev stack in detached mode.
# Service startup order is enforced by healthchecks in docker-compose.yml:
#   redpanda (healthy) → console
#   qdrant   (healthy) → producers / workers
#   ollama   (healthy) → vector worker embedding fallback
# -----------------------------------------------------------------------------
up:
	@echo "[make] Starting OceanTrust AI local stack..."
	docker compose -f $(DOCKER_COMPOSE_FILE) up -d
	@echo ""
	@echo "[make] Stack started. Services:"
	@echo "  Redpanda Console  → http://localhost:8080"
	@echo "  Qdrant Dashboard  → http://localhost:6333/dashboard"
	@echo "  Ollama REST API   → http://localhost:11434"
	@echo ""
	@echo "[make] Run 'make provision' once services are healthy."

# -----------------------------------------------------------------------------
# down — Stop all containers without removing persistent volumes.
# (Data is preserved for the next 'make up')
# -----------------------------------------------------------------------------
down:
	@echo "[make] Stopping OceanTrust AI stack..."
	docker compose -f $(DOCKER_COMPOSE_FILE) down

# -----------------------------------------------------------------------------
# provision — Idempotent full provisioning of all data infrastructure.
# Runs Kafka topic creation first, then Qdrant collection creation.
# Safe to re-run: skips already-existing topics and collections.
# -----------------------------------------------------------------------------
provision: provision-kafka provision-qdrant
	@echo ""
	@echo "[make] All provisioning complete. Infrastructure is ready."

# -----------------------------------------------------------------------------
# provision-kafka — Create all Redpanda topics defined in the architect spec.
# Ref: docs/kafka-topics.md
# -----------------------------------------------------------------------------
provision-kafka:
	@echo "[make] Provisioning Redpanda topics..."
	bash $(PROVISION_KAFKA)

# -----------------------------------------------------------------------------
# provision-qdrant — Create Qdrant collections and payload indices.
# Collections: fishing_regulations, biological_manuals, scientific_papers,
#              telemetry_vectors (dim=3072), telemetry_vectors_fallback (dim=768)
# Ref: docs/knowledge-ingestion.md, docs/vector-worker-spec.md
# -----------------------------------------------------------------------------
provision-qdrant:
	@echo "[make] Provisioning Qdrant collections..."
	bash $(PROVISION_QDRANT)

# -----------------------------------------------------------------------------
# status — Display health status of all containers and endpoint URLs.
# -----------------------------------------------------------------------------
status:
	@echo ""
	@echo "  OceanTrust AI — Service Status"
	@echo "  ─────────────────────────────────────────────────────────────"
	docker compose -f $(DOCKER_COMPOSE_FILE) ps
	@echo ""
	@echo "  Endpoints:"
	@echo "    Redpanda Kafka API   → localhost:9092"
	@echo "    Redpanda Schema Reg  → http://localhost:8081"
	@echo "    Redpanda Console UI  → http://localhost:8080"
	@echo "    Qdrant REST API      → http://localhost:6333"
	@echo "    Qdrant Dashboard     → http://localhost:6333/dashboard"
	@echo "    Ollama Inference     → http://localhost:11434"
	@echo "  ─────────────────────────────────────────────────────────────"
	@echo ""

# -----------------------------------------------------------------------------
# load-knowledge — Run the knowledge loader against all Markdown files in
# data/knowledge/. Requires OPENAI_API_KEY to be set in the environment.
# Ref: src/ingestion/knowledge_loader.py, docs/knowledge-ingestion.md
# -----------------------------------------------------------------------------
load-knowledge:
	@echo "[make] Loading knowledge base into Qdrant..."
	python -m src.ingestion.knowledge_loader --data-dir data/knowledge
	@echo "[make] Knowledge loading complete."

# Run the loader in dry-run mode — no API calls, no Qdrant writes
dry-run-loader:
	@echo "[make] Running knowledge loader in dry-run mode (no API calls)..."
	python -m src.ingestion.knowledge_loader --data-dir data/knowledge --dry-run

# -----------------------------------------------------------------------------
# logs — Stream Redpanda broker logs (most useful for debugging producers)
# -----------------------------------------------------------------------------
logs:
	docker compose -f $(DOCKER_COMPOSE_FILE) logs -f redpanda

# -----------------------------------------------------------------------------
# logs-all — Stream logs from all services simultaneously
# -----------------------------------------------------------------------------
logs-all:
	docker compose -f $(DOCKER_COMPOSE_FILE) logs -f

# -----------------------------------------------------------------------------
# clean — Full teardown: remove all containers, named volumes, and Python caches.
# WARNING: This deletes ALL persisted Redpanda and Qdrant data.
# -----------------------------------------------------------------------------
clean:
	@echo "[make] Tearing down full OceanTrust AI stack and removing volumes..."
	docker compose -f $(DOCKER_COMPOSE_FILE) down -v --remove-orphans
	@echo "[make] Removing Python caches..."
	find . -type d -name "__pycache__"  -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache"   -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache"   -exec rm -rf {} + 2>/dev/null || true
	@echo "[make] Clean complete."

# -----------------------------------------------------------------------------
# dashboard — Launch the Streamlit Control Tower UI.
# Requires: pip install -e '.[viz]' and the full stack running (make up).
# Opens at http://localhost:8501 by default.
# -----------------------------------------------------------------------------
dashboard:
	@echo "[make] Starting OceanTrust AI Control Tower..."
	@echo "[make] Dashboard available at: http://localhost:8501"
	streamlit run src/visualization/dashboard.py \
		--server.port 8501 \
		--server.headless true \
		--browser.gatherUsageStats false
