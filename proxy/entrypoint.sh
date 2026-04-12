#!/usr/bin/env bash
# Wait for the streaming-poc VPN and queues to be configured in Solace
# (created by setup.sh), then start the webhook proxy.
set -euo pipefail

SOLACE_SEMP="${SOLACE_SEMP:-http://solace:8080}"
SOLACE_VPN="${SOLACE_VPN:-streaming-poc}"

log() { echo "[proxy] $(date '+%H:%M:%S')  $*"; }

log "Waiting for Solace Platform to be reachable..."
until curl -sf -u admin:admin "${SOLACE_SEMP}/SEMP/v2/config/about/api" > /dev/null 2>&1; do
  sleep 3
done
log "Solace Platform is up."

log "Waiting for VPN '${SOLACE_VPN}' to be configured..."
until curl -sf -u admin:admin \
  "${SOLACE_SEMP}/SEMP/v2/config/msgVpns/${SOLACE_VPN}" > /dev/null 2>&1; do
  sleep 3
done
log "VPN '${SOLACE_VPN}' is ready."

log "Waiting for queue 'rw-ingest' to exist..."
until curl -sf -u admin:admin \
  "${SOLACE_SEMP}/SEMP/v2/config/msgVpns/${SOLACE_VPN}/queues/rw-ingest" > /dev/null 2>&1; do
  sleep 3
done
log "Queue 'rw-ingest' is ready."

log "Starting Solace-to-RisingWave webhook proxy..."
exec python proxy.py
