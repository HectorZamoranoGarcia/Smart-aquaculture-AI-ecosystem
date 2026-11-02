#!/usr/bin/env bash
# Provision local Redpanda topics
# Run: bash bin/scripts/provision_infrastructure.sh
# Requires: rpk or Docker

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

readonly BROKER_ADDRESS="${RPK_BROKERS:-localhost:9092}"
readonly REDPANDA_CONTAINER="${REDPANDA_CONTAINER:-redpanda}"
readonly MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-120}"
readonly POLL_INTERVAL_SECONDS=5

# ---------------------------------------------------------------------------
# Topic definitions
#
# Format: name:partitions:replicas:retention-ms
#
# Producers set the partition keys
#
# Retention is measured in milliseconds
# ---------------------------------------------------------------------------

declare -a TOPICS=(
  # Sample sensor events

  "salmon.farm.sensor.telemetry:12:1:2592000000"

  # Market price records

  "market.oslo.fish.prices:8:1:604800000"

  # Failed messages

  "dlq.events.failed:4:1:604800000"

  # Agent requests

  "llm.commands.dispatch:6:1:604800000"

  # Agent responses

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
# Find rpk locally or in Docker
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
# Wait for Redpanda
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
# Create one topic if needed
# ---------------------------------------------------------------------------

create_topic() {
  local topic_name="$1"
  local partitions="$2"
  local replication="$3"
  local retention_ms="$4"

  # Skip topics that already exist

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
# Create configured topics
# ---------------------------------------------------------------------------

provision_topics() {
  log_info "Provisioning ${#TOPICS[@]} topic(s)..."

  local failed=0

  for topic_spec in "${TOPICS[@]}"; do
    # Split the topic settings

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
# List available topics
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
