#!/usr/bin/env bash
# =============================================================================
# provision_qdrant.sh — OceanTrust AI
# =============================================================================
# Purpose  : Wait for the Qdrant vector database to be ready, then create all
#            application collections following the architect's vector schema.
#
# Usage    : bash bin/scripts/provision_qdrant.sh
#            QDRANT_URL=http://localhost:6333 bash bin/scripts/provision_qdrant.sh
#
# Requires : curl, jq
# Ref      : docs/knowledge-ingestion.md, docs/vector-worker-spec.md
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------
readonly QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
readonly MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-120}"
readonly POLL_INTERVAL_SECONDS=5

# ---------------------------------------------------------------------------
# Collection definitions
#
# Format per entry: "name|vector_size|distance"
#
# Production collections (text-embedding-3-large → 3072-dim):
#   fishing_regulations  — Regulatory documents (laws, quota tables)
#   biological_manuals   — Species manuals, disease protocols
#   scientific_papers    — Open-access research, ICES/SINTEF reports
#   telemetry_vectors    — Real-time farm telemetry narratives (Vector Worker)
#
# Fallback collection (nomic-embed-text fallback → 768-dim):
#   telemetry_vectors_fallback — Written by Vector Worker when CB is OPEN
# ---------------------------------------------------------------------------
declare -a COLLECTIONS=(
  "fishing_regulations|3072|Cosine"
  "biological_manuals|3072|Cosine"
  "scientific_papers|3072|Cosine"
  "telemetry_vectors|3072|Cosine"
  "telemetry_vectors_fallback|768|Cosine"
)

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
log_info()    { echo "[INFO]  $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"; }
log_success() { echo "[OK]    $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"; }
log_warn()    { echo "[WARN]  $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >&2; }
log_error()   { echo "[ERROR] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >&2; }

# ---------------------------------------------------------------------------
# Wait for the Qdrant REST API to become available.
# ---------------------------------------------------------------------------
wait_for_qdrant() {
  log_info "Waiting for Qdrant at ${QDRANT_URL} (timeout: ${MAX_WAIT_SECONDS}s)..."
  local elapsed=0

  until curl -sf "${QDRANT_URL}/healthz" > /dev/null 2>&1; do
    if (( elapsed >= MAX_WAIT_SECONDS )); then
      log_error "Qdrant not ready after ${MAX_WAIT_SECONDS}s. Aborting."
      exit 1
    fi
    log_info "Qdrant not ready — retrying in ${POLL_INTERVAL_SECONDS}s... (${elapsed}s elapsed)"
    sleep "${POLL_INTERVAL_SECONDS}"
    (( elapsed += POLL_INTERVAL_SECONDS ))
  done

  log_success "Qdrant is ready at ${QDRANT_URL}."
}

# ---------------------------------------------------------------------------
# Check if a Qdrant collection already exists.
# Returns exit code 0 if it exists, 1 if not.
# ---------------------------------------------------------------------------
collection_exists() {
  local name="$1"
  local http_status

  http_status=$(curl -sf -o /dev/null -w "%{http_code}" \
    "${QDRANT_URL}/collections/${name}" 2>/dev/null || echo "000")

  [[ "${http_status}" == "200" ]]
}

# ---------------------------------------------------------------------------
# Create a single Qdrant collection with HNSW index configuration.
# Skips creation if the collection already exists (idempotent).
#
# Arguments:
#   $1  name         — Collection name
#   $2  vector_size  — Embedding dimensionality (3072 or 768)
#   $3  distance     — Distance metric ("Cosine", "Dot", "Euclid")
# ---------------------------------------------------------------------------
create_collection() {
  local name="$1"
  local vector_size="$2"
  local distance="$3"

  if collection_exists "${name}"; then
    log_warn "Collection '${name}' already exists — skipping creation."
    return 0
  fi

  log_info "Creating collection '${name}' (size=${vector_size}, distance=${distance})..."

  local http_status
  http_status=$(curl -sf -o /dev/null -w "%{http_code}" \
    -X PUT "${QDRANT_URL}/collections/${name}" \
    -H "Content-Type: application/json" \
    -d "{
      \"vectors\": {
        \"size\": ${vector_size},
        \"distance\": \"${distance}\",
        \"on_disk\": true
      },
      \"hnsw_config\": {
        \"m\": 16,
        \"ef_construct\": 200,
        \"on_disk\": true
      },
      \"optimizers_config\": {
        \"indexing_threshold\": 10000
      },
      \"on_disk_payload\": true
    }" 2>/dev/null || echo "000")

  if [[ "${http_status}" != "200" ]]; then
    log_error "Failed to create collection '${name}' (HTTP ${http_status})."
    return 1
  fi

  log_success "Collection '${name}' created (dim=${vector_size}, metric=${distance})."
}

# ---------------------------------------------------------------------------
# Create payload field indices on telemetry_vectors for fast agent querying.
# Ref: docs/vector-worker-spec.md §3.1
# ---------------------------------------------------------------------------
create_telemetry_indices() {
  log_info "Creating payload indices on 'telemetry_vectors'..."

  local fields=(
    '{"field_name": "farm_id",        "field_schema": "keyword"}'
    '{"field_name": "alert_severity", "field_schema": "keyword"}'
    '{"field_name": "region",         "field_schema": "keyword"}'
    '{"field_name": "timestamp_unix", "field_schema": "integer"}'
  )

  for field_json in "${fields[@]}"; do
    local field_name
    field_name=$(echo "${field_json}" | grep -o '"farm_id\|alert_severity\|region\|timestamp_unix"' | head -1 | tr -d '"')

    curl -sf -o /dev/null \
      -X PUT "${QDRANT_URL}/collections/telemetry_vectors/index" \
      -H "Content-Type: application/json" \
      -d "${field_json}" 2>/dev/null && \
      log_success "  Index created: ${field_name}" || \
      log_warn    "  Index may already exist: ${field_name} — skipping."
  done
}

# ---------------------------------------------------------------------------
# Create payload field indices on regulatory collections for agent filtering.
# Ref: docs/knowledge-ingestion.md §5.3
# ---------------------------------------------------------------------------
create_regulatory_indices() {
  local reg_collections=("fishing_regulations" "biological_manuals" "scientific_papers")

  for col in "${reg_collections[@]}"; do
    log_info "Creating payload indices on '${col}'..."

    local fields=(
      '{"field_name": "jurisdiction",    "field_schema": "keyword"}'
      '{"field_name": "document_type",   "field_schema": "keyword"}'
      '{"field_name": "species",         "field_schema": "keyword"}'
      '{"field_name": "language",        "field_schema": "keyword"}'
      '{"field_name": "is_active",       "field_schema": "bool"}'
      '{"field_name": "effective_date",  "field_schema": "keyword"}'
    )

    for field_json in "${fields[@]}"; do
      curl -sf -o /dev/null \
        -X PUT "${QDRANT_URL}/collections/${col}/index" \
        -H "Content-Type: application/json" \
        -d "${field_json}" 2>/dev/null || true
    done
    log_success "  Indices set on '${col}'."
  done
}

# ---------------------------------------------------------------------------
# Parse the COLLECTIONS array and create each collection.
# ---------------------------------------------------------------------------
provision_collections() {
  log_info "Provisioning ${#COLLECTIONS[@]} Qdrant collection(s)..."

  local failed=0

  for spec in "${COLLECTIONS[@]}"; do
    IFS='|' read -r name vector_size distance <<< "${spec}"

    if ! create_collection "${name}" "${vector_size}" "${distance}"; then
      (( failed++ ))
    fi
  done

  if (( failed > 0 )); then
    log_error "${failed} collection(s) failed to provision."
    exit 1
  fi

  log_success "All ${#COLLECTIONS[@]} collection(s) provisioned."
}

# ---------------------------------------------------------------------------
# Print a summary of all collections in the Qdrant instance.
# ---------------------------------------------------------------------------
print_collection_summary() {
  log_info "Current Qdrant collections:"
  curl -sf "${QDRANT_URL}/collections" | \
    jq -r '.result.collections[] | "  \(.name)"' 2>/dev/null || \
    log_warn "jq not available — skipping summary. Install jq for formatted output."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
  log_info "=== Qdrant Collection Provisioning ==="
  log_info "URL         : ${QDRANT_URL}"
  log_info "Collections : ${#COLLECTIONS[@]} defined"
  echo ""

  wait_for_qdrant
  provision_collections
  create_telemetry_indices
  create_regulatory_indices
  print_collection_summary

  echo ""
  log_success "=== Qdrant provisioning complete. ==="
  log_info "Qdrant Dashboard: ${QDRANT_URL}/dashboard"
}

main "$@"
