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

# --- RDP: Solace → RisingWave webhook ---
# Guaranteed delivery path: fleet/> → durable queue → REST Delivery Point →
# HTTP POST to RisingWave webhook endpoint (risingwave:4560/webhook/dev/public/fleet_all_raw)

# ── 5. Durable ingest queue ───────────────────────────────────────────────────
# permission "consume" is required for the RDP's internal client to bind to this queue.
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

# ── 7. REST Delivery Point ────────────────────────────────────────────────────
log "Creating REST Delivery Point: risingwave-rdp"
# Must use streaming-profile (not default) — default has allowGuaranteedMsgReceiveEnabled: false
# which prevents the RDP from binding to the queue.
semp_post "/msgVpns/${VPN}/restDeliveryPoints" "$(cat <<JSON
{
  "restDeliveryPointName": "risingwave-rdp",
  "enabled": true,
  "clientProfileName": "streaming-profile"
}
JSON
)"

# ── 8. REST Consumers (multiple for throughput) ──────────────────────────────
# Each REST consumer = one persistent HTTP connection to RisingWave.
# Generator publishes ~56 msg/s; each consumer delivers ~14 msg/s (~70ms RTT).
# 8 consumers = ~112 msg/s — 2× headroom over generator rate, drains spikes fast.
# The single queue binding below feeds all consumers (Solace round-robins across them).
log "Creating REST Consumers (1-8): fleet-webhook-{1..8} → risingwave:4560"
for i in $(seq 1 8); do
  semp_post "/msgVpns/${VPN}/restDeliveryPoints/risingwave-rdp/restConsumers" "$(cat <<JSON
{
  "restConsumerName": "fleet-webhook-${i}",
  "remoteHost": "risingwave",
  "remotePort": 4560,
  "tlsEnabled": false,
  "enabled": true
}
JSON
)"
done

# ── 9. Content-Type header on each REST Consumer ──────────────────────────────
# requestHeaders endpoint requires SEMP API v2.23 (Solace Platform 9.13+).
# Falls back gracefully on older images — webhook still works without explicit header.
log "Setting Content-Type: application/json on fleet-webhook-{1..8}"
for i in $(seq 1 8); do
  semp_post_optional "Content-Type header on fleet-webhook-${i}" \
    "/msgVpns/${VPN}/restDeliveryPoints/risingwave-rdp/restConsumers/fleet-webhook-${i}/requestHeaders" \
    '{"headerName":"Content-Type","headerValue":"application/json"}'
done

# ── 10. Queue Bindings ────────────────────────────────────────────────────────
log "Creating queue binding: rw-ingest → /webhook/dev/public/fleet_all_raw"
semp_post "/msgVpns/${VPN}/restDeliveryPoints/risingwave-rdp/queueBindings" "$(cat <<JSON
{
  "queueBindingName": "rw-ingest",
  "postRequestTarget": "/webhook/dev/public/fleet_all_raw"
}
JSON
)"

log "Creating queue binding: events-ingest → /webhook/dev/public/fleet_all_raw"
semp_post "/msgVpns/${VPN}/restDeliveryPoints/risingwave-rdp/queueBindings" "$(cat <<JSON
{
  "queueBindingName": "events-ingest",
  "postRequestTarget": "/webhook/dev/public/fleet_all_raw"
}
JSON
)"

log "Solace setup complete."
log "  VPN:   ${VPN}"
log "  User:  streaming-user / default"
log "  Queue: rw-ingest     →  fleet/telemetry/> + fleet/commands/>"
log "  Queue: events-ingest →  fleet/events/>"
log "  RDP:   risingwave-rdp → risingwave:4560/webhook/dev/public/fleet_all_raw"
