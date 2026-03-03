#!/usr/bin/env bash
# =============================================================================
# provision_infrastructure.sh — OceanTrust.ai
# =============================================================================
# Purpose  : Wait for the Redpanda cluster to be ready, then create all
#            application topics following the Architect's domain schemas.
#
# Usage    : bash bin/scripts/provision_infrastructure.sh
#            RPK_BROKERS=localhost:9092 bash bin/scripts/provision_infrastructure.sh
#
# Requires : rpk CLI  — https://docs.redpanda.com/current/get-started/rpk/
#            OR Docker (will use rpk from the redpanda container as fallback)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------
readonly BROKER_ADDRESS="${RPK_BROKERS:-localhost:9092}"
readonly REDPANDA_CONTAINER="${REDPANDA_CONTAINER:-redpanda}"
readonly MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-120}"
readonly POLL_INTERVAL_SECONDS=5

# ---------------------------------------------------------------------------
# Topic definitions — OceanTrust.ai domain schemas (Architect v1.0.0)
#
# FORMAT per entry: "topic-name:partitions:replication-factor:retention-ms"
#
# Partition key strategy (enforced at producer level, not here):
#   salmon.farm.sensor.telemetry  → farm_id  (e.g. "NO-FARM-0047")
#   market.oslo.fish.prices       → species + ":" + product_form  (composite)
#
# Retention:
#   7 days  = 604800000 ms  (regulatory minimum for market data)
#   30 days = 2592000000 ms (IoT telemetry for trend analysis)
#   infinite = -1
# ---------------------------------------------------------------------------
declare -a TOPICS=(
  # ── IoT Sensor Telemetry — Salmon Farm ────────────────────────────────────
  # High-volume readings from physical sensor units (water quality, biological,
  # environment). Schema: iot_sensor_event.schema.json  event_type: SENSOR_TELEMETRY
  # 12 partitions: high write throughput from many concurrent sensor devices.
  "salmon.farm.sensor.telemetry:12:1:2592000000"

  # ── Market Data — Oslo Fish Prices ────────────────────────────────────────
  # Price ticks from Fish Pool Oslo, NASDAQ Commodities, and affiliated feeds.
  # Schema: market_data_event.schema.json  event_type: MARKET_PRICE_TICK
  # 8 partitions: moderate throughput, keyed by species+product_form composite.
  "market.oslo.fish.prices:8:1:604800000"

  # ── Dead-Letter Queue — All Domains ───────────────────────────────────────
  # Messages that failed schema validation or producer-level processing.
  # Consumed by the alert service and manual review workflows.
  "dlq.events.failed:4:1:604800000"

  # ── LLM Orchestration — Commands ──────────────────────────────────────────
  # Dispatch requests to LLM workers: anomaly analysis, report generation,
  # price forecasting, and regulatory compliance checks.
  "llm.commands.dispatch:6:1:604800000"

  # ── LLM Orchestration — Responses ─────────────────────────────────────────
  # Processed results returned by LLM workers back into the pipeline.
  "llm.responses.processed:6:1:604800000"
)

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

log_info() {
  echo "[INFO]  $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
}

log_success() {
  echo "[OK]    $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
}

log_warn() {
  echo "[WARN]  $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >&2
}

log_error() {
  echo "[ERROR] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >&2
}

# ---------------------------------------------------------------------------
# Resolve the rpk command: prefer local binary, fall back to Docker exec.
# ---------------------------------------------------------------------------
resolve_rpk_command() {
  if command -v rpk &>/dev/null; then
    log_info "Using local rpk binary: $(command -v rpk)"
    RPK_CMD="rpk --brokers ${BROKER_ADDRESS}"
  else
    log_warn "rpk binary not found locally. Falling back to docker exec on '${REDPANDA_CONTAINER}'."
    if ! docker inspect "${REDPANDA_CONTAINER}" &>/dev/null; then
      log_error "Container '${REDPANDA_CONTAINER}' not found. Start the stack first: docker compose up -d"
      exit 1
    fi
    RPK_CMD="docker exec ${REDPANDA_CONTAINER} rpk --brokers ${BROKER_ADDRESS}"
  fi
}

# ---------------------------------------------------------------------------
# Wait for the Redpanda cluster to accept connections.
# Polls the Kafka API until successful or MAX_WAIT_SECONDS is reached.
# ---------------------------------------------------------------------------
wait_for_cluster() {
  log_info "Waiting for Redpanda cluster at ${BROKER_ADDRESS} (timeout: ${MAX_WAIT_SECONDS}s)..."

  local elapsed=0

  until ${RPK_CMD} cluster info &>/dev/null; do
    if (( elapsed >= MAX_WAIT_SECONDS )); then
      log_error "Cluster not ready after ${MAX_WAIT_SECONDS}s. Aborting."
      exit 1
    fi
    log_info "Cluster not ready yet — retrying in ${POLL_INTERVAL_SECONDS}s... (${elapsed}s elapsed)"
    sleep "${POLL_INTERVAL_SECONDS}"
    (( elapsed += POLL_INTERVAL_SECONDS ))
  done

  log_success "Redpanda cluster is ready at ${BROKER_ADDRESS}."
}

# ---------------------------------------------------------------------------
# Create a single topic.
# Skips creation if the topic already exists (idempotent operation).
#
# Arguments:
#   $1  topic_name       — Kafka topic name
#   $2  partitions       — Number of partitions
#   $3  replication      — Replication factor
#   $4  retention_ms     — Retention in milliseconds (-1 for infinite)
# ---------------------------------------------------------------------------
create_topic() {
  local topic_name="$1"
  local partitions="$2"
  local replication="$3"
  local retention_ms="$4"

  # Check if topic already exists to make the script idempotent.
  if ${RPK_CMD} topic describe "${topic_name}" &>/dev/null; then
    log_warn "Topic '${topic_name}' already exists — skipping creation."
    return 0
  fi

  log_info "Creating topic '${topic_name}' (partitions=${partitions}, replication=${replication}, retention=${retention_ms}ms)..."

  ${RPK_CMD} topic create "${topic_name}" \
    --partitions "${partitions}" \
    --replicas "${replication}" \
    --topic-config "retention.ms=${retention_ms}" \
    --topic-config "cleanup.policy=delete"

  log_success "Topic '${topic_name}' created successfully."
}

# ---------------------------------------------------------------------------
# Parse the TOPICS array and create each topic.
# Expected entry format: "name:partitions:replication:retention_ms"
# ---------------------------------------------------------------------------
provision_topics() {
  log_info "Provisioning ${#TOPICS[@]} topic(s)..."

  local failed=0

  for topic_spec in "${TOPICS[@]}"; do
    # Split the colon-delimited spec into individual fields.
    IFS=':' read -r topic_name partitions replication retention_ms <<< "${topic_spec}"

    if [[ -z "${topic_name}" || -z "${partitions}" || -z "${replication}" || -z "${retention_ms}" ]]; then
      log_error "Malformed topic spec: '${topic_spec}'. Expected format: 'name:partitions:replication:retention_ms'"
      (( failed++ ))
      continue
    fi

    if ! create_topic "${topic_name}" "${partitions}" "${replication}" "${retention_ms}"; then
      log_error "Failed to create topic '${topic_name}'."
      (( failed++ ))
    fi
  done

  if (( failed > 0 )); then
    log_error "${failed} topic(s) failed to provision. Review errors above."
    exit 1
  fi

  log_success "All ${#TOPICS[@]} topic(s) provisioned successfully."
}

# ---------------------------------------------------------------------------
# Print a summary of all topics in the cluster after provisioning.
# ---------------------------------------------------------------------------
print_topic_summary() {
  log_info "Current topic list in the cluster:"
  ${RPK_CMD} topic list
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
  log_info "=== Redpanda Infrastructure Provisioning ==="
  log_info "Broker : ${BROKER_ADDRESS}"
  log_info "Topics : ${#TOPICS[@]} defined"
  echo ""

  resolve_rpk_command
  wait_for_cluster
  provision_topics
  print_topic_summary

  echo ""
  log_success "=== Provisioning complete. Infrastructure is ready. ==="
  log_info "Redpanda Console: http://localhost:8080"
}

main "$@"
