#!/usr/bin/env bash
# Configure Solace Platform via SEMP v2 REST API.
# Run from the host after the broker is healthy.
# Usage: ./config/solace/setup.sh [SOLACE_HOST]
set -euo pipefail

SOLACE_HOST="${1:-localhost}"
SEMP_BASE="http://${SOLACE_HOST}:8180/SEMP/v2/config"
CREDS="admin:admin"
VPN="streaming-poc"

log() { echo "[solace-setup] $*"; }

semp_post() {
  local path="$1"
  local body="$2"
  local response
  response=$(curl -s -u "${CREDS}" -X POST "${SEMP_BASE}${path}" \
    -H "Content-Type: application/json" -d "${body}" 2>&1)
  # Any non-2xx response is a failure, EXCEPT "already exists" which is idempotent.
  if echo "${response}" | grep -q '"responseCode":[^2]' && \
     ! echo "${response}" | grep -qi 'already.exists\|ALREADY_EXISTS'; then
    echo "[solace-setup] ERROR on POST ${path}:" >&2
    echo "${response}" >&2
    exit 1
  fi
}

# Like semp_post but logs a warning and continues on INVALID_PATH (feature not supported
# in this Solace version) instead of aborting.
semp_post_optional() {
  local label="$1"
  local path="$2"
  local body="$3"
  local response
  response=$(curl -s -u "${CREDS}" -X POST "${SEMP_BASE}${path}" \
    -H "Content-Type: application/json" -d "${body}" 2>&1)
  if echo "${response}" | grep -qi 'INVALID_PATH'; then
    log "WARNING: ${label} skipped — endpoint not available in this Solace version (upgrade to 9.13+ for full support)"
  elif echo "${response}" | grep -q '"responseCode":[^2]' && \
       ! echo "${response}" | grep -qi 'already.exists\|ALREADY_EXISTS'; then
    echo "[solace-setup] ERROR on POST ${path}:" >&2
    echo "${response}" >&2
    exit 1
  fi
}

# ── 1. Message VPN ──────────────────────────────────────────────────────────
log "Creating VPN: ${VPN}"
semp_post "/msgVpns" "$(cat <<JSON
{
  "msgVpnName": "${VPN}",
  "enabled": true,
  "maxMsgSpoolUsage": 512,
  "maxConnectionCount": 100,
  "authenticationBasicEnabled": true,
  "authenticationBasicType": "internal"
}
JSON
)"

# ── 2. Client profile ────────────────────────────────────────────────────────
log "Creating client profile: streaming-profile"
semp_post "/msgVpns/${VPN}/clientProfiles" "$(cat <<JSON
{
  "clientProfileName": "streaming-profile",
  "allowGuaranteedMsgSendEnabled": true,
  "allowGuaranteedMsgReceiveEnabled": true,
  "allowTransactedSessionsEnabled": true,
  "allowBridgeConnectionsEnabled": false,
  "allowGuaranteedEndpointCreateEnabled": true
}
JSON
)"

# ── 3. ACL profile ───────────────────────────────────────────────────────────
log "Creating ACL profile: streaming-acl"
semp_post "/msgVpns/${VPN}/aclProfiles" "$(cat <<JSON
{
  "aclProfileName": "streaming-acl",
  "clientConnectDefaultAction": "allow",
  "publishTopicDefaultAction": "allow",
  "subscribeTopicDefaultAction": "allow"
}
JSON
)"

# ── 4. Client username ───────────────────────────────────────────────────────
log "Creating client username: streaming-user"
semp_post "/msgVpns/${VPN}/clientUsernames" "$(cat <<JSON
{
  "clientUsername": "streaming-user",
  "password": "default",
  "enabled": true,
  "clientProfileName": "streaming-profile",
  "aclProfileName": "streaming-acl"
}
JSON
)"

# --- Queues: Solace → RisingWave native Solace connector ---
# Guaranteed delivery path: fleet/> → durable queue → RisingWave Solace SOURCE.
# The RisingWave Solace source connector binds directly to these queues via SMF.

# ── 5. Durable ingest queue ───────────────────────────────────────────────────
# permission "consume" is required for the RisingWave connector to bind to this queue.
log "Creating queue: rw-ingest"
semp_post "/msgVpns/${VPN}/queues" "$(cat <<JSON
{
  "queueName": "rw-ingest",
  "ingressEnabled": true,
  "egressEnabled": true,
  "accessType": "exclusive",
  "permission": "consume",
  "maxMsgSpoolUsage": 100,
  "maxRedeliveryCount": 3,
  "deadMsgQueue": "#DEAD_MSG_QUEUE",
  "maxTtl": 300,
  "respectTtlEnabled": true
}
JSON
)"

# ── 6. Topic subscriptions on rw-ingest ──────────────────────────────────────
# Telemetry and commands only — events go to a separate queue to avoid
# alert messages being blocked behind high-volume telemetry.
log "Adding subscriptions fleet/telemetry/> and fleet/commands/> to rw-ingest"
semp_post "/msgVpns/${VPN}/queues/rw-ingest/subscriptions" "$(cat <<JSON
{
  "subscriptionTopic": "fleet/telemetry/>"
}
JSON
)"
semp_post "/msgVpns/${VPN}/queues/rw-ingest/subscriptions" "$(cat <<JSON
{
  "subscriptionTopic": "fleet/commands/>"
}
JSON
)"

# ── 6b. Events queue (separate from telemetry to avoid head-of-line blocking) ─
log "Creating queue: events-ingest"
semp_post "/msgVpns/${VPN}/queues" "$(cat <<JSON
{
  "queueName": "events-ingest",
  "ingressEnabled": true,
  "egressEnabled": true,
  "accessType": "exclusive",
  "permission": "consume",
  "maxMsgSpoolUsage": 100,
  "maxRedeliveryCount": 3,
  "deadMsgQueue": "#DEAD_MSG_QUEUE",
  "maxTtl": 300,
  "respectTtlEnabled": true
}
JSON
)"

log "Adding subscription fleet/events/> to events-ingest"
semp_post "/msgVpns/${VPN}/queues/events-ingest/subscriptions" "$(cat <<JSON
{
  "subscriptionTopic": "fleet/events/>"
}
JSON
)"

log "Solace setup complete."
log "  VPN:   ${VPN}"
log "  User:  streaming-user / default"
log "  Queue: rw-ingest     →  fleet/telemetry/> + fleet/commands/>"
log "  Queue: events-ingest →  fleet/events/>"
log "  Connector: RisingWave Solace SOURCE binds to both queues via SMF"
