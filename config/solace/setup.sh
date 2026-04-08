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
  # 400 with "already exists" is acceptable — treat as success
  if echo "${response}" | grep -q '"status":"ERROR"' && \
     ! echo "${response}" | grep -qi 'already exists\|ALREADY_EXISTS\|6\b'; then
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

# ── 5. Queues ────────────────────────────────────────────────────────────────
create_queue() {
  local qname="$1"
  local encoded_qname
  encoded_qname=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$qname', safe=''))" 2>/dev/null \
    || echo "${qname//\//%2F}")

  log "Creating queue: ${qname}" >&2
  semp_post "/msgVpns/${VPN}/queues" "$(cat <<JSON
{
  "queueName": "${qname}",
  "accessType": "exclusive",
  "egressEnabled": true,
  "ingressEnabled": true,
  "permission": "consume",
  "maxMsgSpoolUsage": 200
}
JSON
)"
  echo "${encoded_qname}"
}

# Main bridge queue — captures ALL fleet events from Solace for the connector
ENC_MAIN=$(create_queue "q/redpanda-bridge")

# ── 6. Topic subscriptions on queues ─────────────────────────────────────────
log "Adding subscriptions to q/redpanda-bridge"
semp_post "/msgVpns/${VPN}/queues/${ENC_MAIN}/subscriptions" \
  '{"subscriptionTopic": "fleet/>"}'

# ── 7. Kafka Sender (built-in Solace → Redpanda bridge) ─────────────────────
log "Creating Kafka Sender: redpanda-sender"
semp_post "/msgVpns/${VPN}/kafkaSenders" "$(cat <<JSON
{
  "kafkaSenderName": "redpanda-sender",
  "bootstrapAddressList": "redpanda:9092",
  "authenticationScheme": "none",
  "enabled": true
}
JSON
)"

log "Creating queue binding: q/redpanda-bridge → fleet-events"
# URL-encode the sender name for the path (no special chars needed here)
semp_post "/msgVpns/${VPN}/kafkaSenders/redpanda-sender/queueBindings" "$(cat <<JSON
{
  "queueBindingName": "q/redpanda-bridge",
  "remoteTopicName": "fleet-events",
  "enabled": true
}
JSON
)"

log "Solace setup complete."
log "  VPN:           ${VPN}"
log "  User:          streaming-user / default"
log "  Queue:         q/redpanda-bridge  →  fleet/>"
log "  Kafka Sender:  redpanda-sender  →  redpanda:9092  →  fleet-events"
