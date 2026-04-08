#!/usr/bin/env bash
# Wait for the streaming-poc VPN to be configured in Solace (created by setup.sh),
# then start the fleet telemetry generator.
set -euo pipefail

SOLACE_SEMP="${SOLACE_SEMP:-http://solace:8080}"
SOLACE_HOST="${SOLACE_HOST:-tcp://solace:55555}"
SOLACE_VPN="${SOLACE_VPN:-streaming-poc}"
SOLACE_USER="${SOLACE_USER:-streaming-user}"
SOLACE_PASSWORD="${SOLACE_PASSWORD:-default}"
BURST="${BURST:-false}"

log() { echo "[generator] $(date '+%H:%M:%S')  $*"; }

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

BURST_FLAG=""
[[ "${BURST}" == "true" ]] && BURST_FLAG="--burst"

log "Starting fleet telemetry generator (burst=${BURST})..."
exec python generator.py \
  --host "${SOLACE_HOST}" \
  --vpn  "${SOLACE_VPN}" \
  --user "${SOLACE_USER}" \
  --password "${SOLACE_PASSWORD}" \
  ${BURST_FLAG}
