#!/usr/bin/env bash
# =============================================================================
# run_ui.sh — OceanTrust AI · Streamlit Control Tower launcher
# =============================================================================
# Usage:
#   chmod +x bin/scripts/run_ui.sh
#   ./bin/scripts/run_ui.sh
#
# Description:
#   Sets PYTHONPATH to the project root so that all `src.*` module imports
#   resolve correctly, then launches the Streamlit Control Tower dashboard.
#
# Environment variables (optional overrides):
#   STREAMLIT_SERVER_PORT  — HTTP port Streamlit listens on (default: 8501)
#   STREAMLIT_SERVER_HOST  — Bind host (default: localhost)
#   GOOGLE_API_KEY         — Required for the RAG search panel
#   QDRANT_HOST            — Qdrant hostname (default: localhost)
#   QDRANT_PORT            — Qdrant REST port (default: 6333)
#   KAFKA_BOOTSTRAP_SERVERS — Redpanda broker address (default: localhost:19092)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve the project root (two levels up from this script's directory)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ---------------------------------------------------------------------------
# Export PYTHONPATH so `src.*` imports work without an editable install
# ---------------------------------------------------------------------------
export PYTHONPATH="${PROJECT_ROOT}"

# ---------------------------------------------------------------------------
# Set default Kafka bootstrap to the external listener (host → container)
# ---------------------------------------------------------------------------
export KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-localhost:19092}"

echo "[run_ui] Project root  : ${PROJECT_ROOT}"
echo "[run_ui] PYTHONPATH    : ${PYTHONPATH}"
echo "[run_ui] Kafka brokers : ${KAFKA_BOOTSTRAP_SERVERS}"
echo "[run_ui] Launching Streamlit dashboard ..."

exec streamlit run \
    "${PROJECT_ROOT}/src/ui/dashboard.py" \
    --server.port "${STREAMLIT_SERVER_PORT:-8501}" \
    --server.address "${STREAMLIT_SERVER_HOST:-localhost}" \
    --server.headless true \
    --browser.gatherUsageStats false
