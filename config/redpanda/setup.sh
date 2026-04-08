#!/usr/bin/env bash
# Create Redpanda topics for the fleet streaming POC.
# Run from the host (uses rpk via docker exec) or inside the container.
# Usage: ./config/redpanda/setup.sh
set -euo pipefail

log() { echo "[redpanda-setup] $*"; }

rpk_cmd() {
  # Try native rpk first; fall back to docker exec
  if command -v rpk &>/dev/null; then
    rpk "$@" --brokers localhost:19092
  else
    docker exec redpanda rpk "$@" --brokers localhost:9092
  fi
}

# ── Topics ───────────────────────────────────────────────────────────────────
# Coarse-grained topics — the Solace topic is preserved inside each message
# payload (solace_topic field) and optionally in Kafka message headers.
#
# Partition strategy:
#   - fleet-telemetry / fleet-events: 6 partitions (high-throughput streams)
#   - fleet-commands:                  3 partitions (lower volume, ordered)
#
# Retention: -1 = infinite (Redpanda is the system of record)

declare -A TOPICS=(
  ["fleet-telemetry"]="6"
  ["fleet-events"]="6"
  ["fleet-commands"]="3"
)

for topic in "${!TOPICS[@]}"; do
  partitions="${TOPICS[$topic]}"
  log "Creating topic: ${topic}  (partitions=${partitions}, retention=infinite)"
  rpk_cmd topic create "${topic}" \
    --partitions "${partitions}" \
    --replicas 1 \
    -c retention.ms=-1 \
    -c retention.bytes=-1 \
    || log "  (topic may already exist — continuing)"
done

# ── Internal Kafka Connect topics (auto-created but ensure they exist) ────────
for internal_topic in _connect-configs _connect-offsets _connect-status; do
  log "Ensuring internal topic: ${internal_topic}"
  rpk_cmd topic create "${internal_topic}" \
    --partitions 1 \
    --replicas 1 \
    -c cleanup.policy=compact \
    || true
done

log ""
log "Redpanda topic setup complete."
rpk_cmd topic list
