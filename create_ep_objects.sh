#!/usr/bin/env bash
# create_ep_objects.sh  — clean-start: deletes any existing "Fleet Operations" domain
# and recreates all objects from scratch.  Exits with code 2 if Event Portal is unreachable
# so callers can fall back gracefully to statically defined events.
#
# Usage: ./create_ep_objects.sh
# Reads SOLACE_CLOUD_TOKEN from .env at project root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
EP_BASE="https://api.solace.cloud/api/v2/architecture"

# ── Preflight checks ──────────────────────────────────────────────────────────

# Accept token from environment (Docker / CI) or fall back to reading .env file (local dev).
if [[ -z "${SOLACE_CLOUD_TOKEN:-}" ]]; then
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: SOLACE_CLOUD_TOKEN not set in environment and .env not found at $ENV_FILE"
    exit 1
  fi
  # Extract token — handles quoted and unquoted values
  SOLACE_CLOUD_TOKEN=$(grep '^SOLACE_CLOUD_TOKEN=' "$ENV_FILE" \
    | sed 's/^SOLACE_CLOUD_TOKEN=//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//')
  if [[ -z "$SOLACE_CLOUD_TOKEN" ]]; then
    echo "ERROR: SOLACE_CLOUD_TOKEN is not set in $ENV_FILE"
    exit 1
  fi
fi

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required.  sudo apt-get install -y jq"
  exit 1
fi

# ── EP connectivity check ────────────────────────────────────────────────────
echo "Checking Event Portal connectivity..."
_EP_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $SOLACE_CLOUD_TOKEN" \
  "${EP_BASE}/applicationDomains?pageSize=1")
if [[ "$_EP_HTTP" != "200" ]]; then
  echo "WARNING: Event Portal is unreachable or rejected the token (HTTP ${_EP_HTTP})." >&2
  echo "WARNING: Skipping EP object creation — the stack will fall back to statically defined events." >&2
  exit 2
fi
echo "  connected (HTTP 200)"

# ── Helpers ───────────────────────────────────────────────────────────────────

# GET with query params.  Use --data-urlencode "k=v" for strings, -d "k=v" for IDs.
ep_get() {
  local path="$1"; shift
  curl -s -G "$EP_BASE$path" \
    -H "Authorization: Bearer $SOLACE_CLOUD_TOKEN" \
    "$@"
}

ep_post() {
  local path="$1"
  local body="$2"
  local resp
  resp=$(curl -s -X POST "$EP_BASE$path" \
    -H "Authorization: Bearer $SOLACE_CLOUD_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$body")
  if echo "$resp" | jq -e '.errorId' &>/dev/null; then
    echo "ERROR from $path: $(echo "$resp" | jq -r '.message // .')" >&2
    exit 1
  fi
  echo "$resp"
}

ep_delete() {
  local path="$1"
  local body http_code
  body=$(curl -s -w "\n__HTTP_STATUS__%{http_code}" -X DELETE "$EP_BASE$path" \
    -H "Authorization: Bearer $SOLACE_CLOUD_TOKEN")
  http_code=$(echo "$body" | grep '^__HTTP_STATUS__' | sed 's/__HTTP_STATUS__//')
  body=$(echo "$body" | grep -v '^__HTTP_STATUS__')
  if [[ "$http_code" != 2* ]]; then
    echo "ERROR: DELETE $path returned HTTP $http_code" >&2
    echo "  Response: $body" >&2
    exit 1
  fi
}

# Return ID of first object with exact name match, or empty string.
find_by_name() {
  local path="$1"
  local name="$2"
  ep_get "$path" --data-urlencode "name=$name" \
    | jq -r --arg n "$name" '.data[] | select(.name==$n) | .id' | head -1
}

# Return ID of first object with exact name match scoped to a domain, or empty string.
find_in_domain() {
  local path="$1"
  local name="$2"
  local domain_id="$3"
  ep_get "$path" --data-urlencode "name=$name" -d "applicationDomainId=$domain_id" \
    | jq -r --arg n "$name" '.data[] | select(.name==$n) | .id' | head -1
}

# Return ID of a version matching $ver under a parent, or empty string.
# $id_param: e.g. "schemaIds=abc123"
find_version() {
  local path="$1"
  local id_param="$2"
  local ver="$3"
  ep_get "$path" -d "$id_param" \
    | jq -r --arg v "$ver" '.data[] | select(.version==$v) | .id' | head -1
}

# ── 0. Clean-start: remove existing domain and all its objects ────────────────

echo "Checking for existing 'Fleet Operations' domain..."
STALE_ID=$(find_by_name "/applicationDomains" "Fleet Operations")
if [[ -n "$STALE_ID" ]]; then
  echo "  Found existing domain ($STALE_ID) — removing for clean start..."
  # Disable deletion protection first (no-op if already false)
  _PATCH_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X PATCH "$EP_BASE/applicationDomains/$STALE_ID" \
    -H "Authorization: Bearer $SOLACE_CLOUD_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name": "Fleet Operations", "deletionProtected": false}')
  if [[ "$_PATCH_HTTP" != 2* ]]; then
    echo "WARNING: PATCH to disable deletion protection returned HTTP $_PATCH_HTTP — proceeding anyway" >&2
  fi
  ep_delete "/applicationDomains/$STALE_ID"
  # Verify the domain is actually gone
  _VERIFY_ID=$(find_by_name "/applicationDomains" "Fleet Operations")
  if [[ -n "$_VERIFY_ID" ]]; then
    echo "ERROR: Domain still exists after delete (ID: $_VERIFY_ID). Cannot proceed with clean start." >&2
    exit 1
  fi
  echo "  Removed and verified. All schemas, events, and applications deleted."
fi

# ── 1. Application Domain ─────────────────────────────────────────────────────

echo "Ensuring application domain..."
DOMAIN_ID=$(find_by_name "/applicationDomains" "Fleet Operations")
if [[ -z "$DOMAIN_ID" ]]; then
  DOMAIN_ID=$(ep_post "/applicationDomains" '{
    "name": "Fleet Operations",
    "description": "IoT fleet monitoring domain — IoT fleet simulator publishing combined telemetry and alerts. Demonstrates Solace Platform + RisingWave streaming architecture."
  }' | jq -r '.data.id')
  echo "  created: $DOMAIN_ID"
else
  echo "  exists:  $DOMAIN_ID"
fi

# ── 2. Schemas ────────────────────────────────────────────────────────────────

echo "Ensuring schemas..."

ensure_schema() {
  local name="$1"
  local id
  id=$(find_in_domain "/schemas" "$name" "$DOMAIN_ID")
  if [[ -z "$id" ]]; then
    id=$(ep_post "/schemas" \
      "$(jq -n --arg d "$DOMAIN_ID" --arg n "$name" \
        '{applicationDomainId:$d,name:$n,schemaType:"jsonSchema"}')" \
      | jq -r '.data.id')
    echo "  created: $name ($id)" >&2
  else
    echo "  exists:  $name ($id)" >&2
  fi
  echo "$id"
}

SCHEMA_TELEMETRY_ID=$(ensure_schema "FleetTelemetry")
SCHEMA_ALERT_ID=$(ensure_schema "FleetAlert")
SCHEMA_COMMAND_ID=$(ensure_schema "FleetCommand")

# ── 3. Schema Versions ────────────────────────────────────────────────────────

echo "Ensuring schema versions..."

TELEMETRY_CONTENT='{"$schema":"http://json-schema.org/draft-07/schema#","type":"object","title":"FleetTelemetry","description":"Combined telemetry snapshot from a fleet vehicle — all sensors in one message","required":["vehicle_id","speed","fuel_level","engine_temp","tire_pressure","battery_voltage","latitude","longitude","recorded_at","solace_topic"],"properties":{"vehicle_id":{"type":"string","description":"Unique vehicle identifier, e.g. vehicle_001"},"speed":{"type":"number","description":"Current speed in mph"},"fuel_level":{"type":"number","description":"Fuel level as percentage (0-100)"},"engine_temp":{"type":"number","description":"Engine temperature in Fahrenheit"},"tire_pressure":{"type":"number","description":"Tire pressure in PSI"},"battery_voltage":{"type":"number","description":"Battery voltage in volts"},"latitude":{"type":"number","minimum":-90,"maximum":90},"longitude":{"type":"number","minimum":-180,"maximum":180},"recorded_at":{"type":"string","format":"date-time"},"solace_topic":{"type":"string","description":"The Solace topic this message was published to"}}}'

ALERT_CONTENT='{"$schema":"http://json-schema.org/draft-07/schema#","type":"object","title":"FleetAlert","description":"An alert event emitted by a fleet vehicle","required":["vehicle_id","event_type","severity","description","occurred_at","solace_topic"],"properties":{"vehicle_id":{"type":"string"},"event_type":{"type":"string","enum":["low_fuel","high_engine_temp","tire_pressure_warning","speed_limit_exceeded","maintenance_due","battery_voltage_low"]},"severity":{"type":"string","enum":["low","medium","high"]},"description":{"type":"string"},"payload":{"type":"object","properties":{"current_value":{"type":"number"},"metric":{"type":"string"}}},"occurred_at":{"type":"string","format":"date-time"},"solace_topic":{"type":"string"}}}'

COMMAND_CONTENT='{"$schema":"http://json-schema.org/draft-07/schema#","type":"object","title":"FleetCommand","description":"A command sent to a fleet vehicle","required":["vehicle_id","command_type","issued_at"],"properties":{"vehicle_id":{"type":"string"},"command_type":{"type":"string","description":"e.g. return_to_base, reduce_speed, diagnostic_check"},"parameters":{"type":"object"},"issued_at":{"type":"string","format":"date-time"}}}'

ensure_schema_version() {
  local schema_id="$1"
  local content="$2"
  local id
  id=$(find_version "/schemaVersions" "schemaIds=$schema_id" "1.0.0")
  if [[ -z "$id" ]]; then
    id=$(ep_post "/schemaVersions" \
      "$(jq -n --arg sid "$schema_id" --arg c "$content" \
        '{schemaId:$sid,version:"1.0.0",content:$c}')" \
      | jq -r '.data.id')
    echo "  created: $schema_id → $id" >&2
  else
    echo "  exists:  $schema_id → $id" >&2
  fi
  echo "$id"
}

SV_TELEMETRY_ID=$(ensure_schema_version "$SCHEMA_TELEMETRY_ID" "$TELEMETRY_CONTENT")
SV_ALERT_ID=$(ensure_schema_version "$SCHEMA_ALERT_ID" "$ALERT_CONTENT")
SV_COMMAND_ID=$(ensure_schema_version "$SCHEMA_COMMAND_ID" "$COMMAND_CONTENT")

echo "  sv_telemetry=$SV_TELEMETRY_ID  sv_alert=$SV_ALERT_ID  sv_command=$SV_COMMAND_ID"

# ── 4. Events ─────────────────────────────────────────────────────────────────

echo "Ensuring events..."

ensure_event() {
  local name="$1"
  local id
  id=$(find_in_domain "/events" "$name" "$DOMAIN_ID")
  if [[ -z "$id" ]]; then
    id=$(ep_post "/events" \
      "$(jq -n --arg d "$DOMAIN_ID" --arg n "$name" \
        '{applicationDomainId:$d,name:$n}')" \
      | jq -r '.data.id')
    echo "  created: $name ($id)" >&2
  else
    echo "  exists:  $name ($id)" >&2
  fi
  echo "$id"
}

EV_TELEMETRY_ID=$(ensure_event "vehicle-telemetry")
EV_ALERT_ID=$(ensure_event "vehicle-alert")
EV_COMMAND_ID=$(ensure_event "vehicle-command")

# ── 5. Event Versions ─────────────────────────────────────────────────────────

echo "Ensuring event versions..."

# ── vehicle-telemetry: combined payload, topic fleet/telemetry/{vehicle_id}/metrics ──
EVV_TELEMETRY_ID=$(find_version "/eventVersions" "eventIds=$EV_TELEMETRY_ID" "1.0.0")
if [[ -z "$EVV_TELEMETRY_ID" ]]; then
  EVV_TELEMETRY_ID=$(ep_post "/eventVersions" "$(jq -n \
    --arg eid "$EV_TELEMETRY_ID" \
    --arg svid "$SV_TELEMETRY_ID" \
    '{
      eventId: $eid,
      version: "1.0.0",
      description: "Combined telemetry snapshot — speed, fuel, engine temp, tire pressure, battery voltage, and GPS in one message per vehicle per tick",
      schemaVersionId: $svid,
      deliveryDescriptor: {
        brokerType: "solace",
        address: {
          addressType: "topic",
          addressLevels: [
            {name:"fleet",      addressLevelType:"literal"},
            {name:"telemetry",  addressLevelType:"literal"},
            {name:"vehicle_id", addressLevelType:"variable"},
            {name:"metrics",    addressLevelType:"literal"}
          ]
        }
      }
    }')" | jq -r '.data.id')
fi

EVV_ALERT_ID=$(find_version "/eventVersions" "eventIds=$EV_ALERT_ID" "1.0.0")
if [[ -z "$EVV_ALERT_ID" ]]; then
  EVV_ALERT_ID=$(ep_post "/eventVersions" "$(jq -n \
    --arg eid "$EV_ALERT_ID" \
    --arg svid "$SV_ALERT_ID" \
    '{
      eventId: $eid,
      version: "1.0.0",
      description: "Alert event — severity (low/medium/high) encoded as the final topic level",
      schemaVersionId: $svid,
      deliveryDescriptor: {
        brokerType: "solace",
        address: {
          addressType: "topic",
          addressLevels: [
            {name:"fleet",      addressLevelType:"literal"},
            {name:"events",     addressLevelType:"literal"},
            {name:"vehicle_id", addressLevelType:"variable"},
            {name:"alerts",     addressLevelType:"literal"},
            {name:"severity",   addressLevelType:"variable"}
          ]
        }
      }
    }')" | jq -r '.data.id')
fi

EVV_COMMAND_ID=$(find_version "/eventVersions" "eventIds=$EV_COMMAND_ID" "1.0.0")
if [[ -z "$EVV_COMMAND_ID" ]]; then
  EVV_COMMAND_ID=$(ep_post "/eventVersions" "$(jq -n \
    --arg eid "$EV_COMMAND_ID" \
    --arg svid "$SV_COMMAND_ID" \
    '{
      eventId: $eid,
      version: "1.0.0",
      description: "Command dispatched to a fleet vehicle (return_to_base, reduce_speed, diagnostic_check, etc.)",
      schemaVersionId: $svid,
      deliveryDescriptor: {
        brokerType: "solace",
        address: {
          addressType: "topic",
          addressLevels: [
            {name:"fleet",        addressLevelType:"literal"},
            {name:"commands",     addressLevelType:"literal"},
            {name:"vehicle_id",   addressLevelType:"variable"},
            {name:"command_type", addressLevelType:"variable"}
          ]
        }
      }
    }')" | jq -r '.data.id')
fi

# ── 6. Applications ───────────────────────────────────────────────────────────

echo "Ensuring applications..."

ensure_application() {
  local name="$1"
  local id
  id=$(find_in_domain "/applications" "$name" "$DOMAIN_ID")
  if [[ -z "$id" ]]; then
    id=$(ep_post "/applications" \
      "$(jq -n --arg d "$DOMAIN_ID" --arg n "$name" \
        '{applicationDomainId:$d,name:$n,applicationType:"standard",brokerType:"solace"}')" \
      | jq -r '.data.id')
    echo "  created: $name ($id)" >&2
  else
    echo "  exists:  $name ($id)" >&2
  fi
  echo "$id"
}

APP_GENERATOR_ID=$(ensure_application "fleet-generator")
APP_RISINGWAVE_ID=$(ensure_application "risingwave-analytics")
APP_AGENT_ID=$(ensure_application "fleet-agent")
APP_TRYME_ID=$(ensure_application "solace-plus-tryme")

# ── 7. Application Versions ───────────────────────────────────────────────────

echo "Ensuring application versions..."

ALL_CONSUMER_EVV_IDS=$(jq -n \
  --arg a "$EVV_TELEMETRY_ID" \
  --arg b "$EVV_ALERT_ID" \
  '[$a,$b]')

ensure_app_version() {
  local app_id="$1"
  local body="$2"
  local existing_id
  existing_id=$(find_version "/applicationVersions" "applicationIds=$app_id" "1.0.0")
  if [[ -z "$existing_id" ]]; then
    ep_post "/applicationVersions" "$body" > /dev/null
    echo "  created app version for $app_id"
  else
    echo "  exists app version: $app_id → $existing_id"
  fi
}

ensure_app_version "$APP_GENERATOR_ID" "$(jq -n \
  --arg appId "$APP_GENERATOR_ID" \
  --argjson produced "$ALL_CONSUMER_EVV_IDS" \
  '{
    applicationId: $appId,
    version: "1.0.0",
    description: "IoT fleet simulator — publishes combined telemetry and alerts to Solace Platform",
    declaredProducedEventVersionIds: $produced
  }')"

ensure_app_version "$APP_RISINGWAVE_ID" "$(jq -n \
  --arg appId "$APP_RISINGWAVE_ID" \
  --argjson consumed "$ALL_CONSUMER_EVV_IDS" \
  '{
    applicationId: $appId,
    version: "1.0.0",
    description: "Streaming analytics layer — consumes fleet events via Solace RDP webhook, processes with incremental SQL materialized views",
    declaredConsumedEventVersionIds: $consumed
  }')"

ensure_app_version "$APP_AGENT_ID" "$(jq -n \
  --arg appId "$APP_AGENT_ID" \
  --arg cmdEvv "$EVV_COMMAND_ID" \
  --argjson consumed "$ALL_CONSUMER_EVV_IDS" \
  '{
    applicationId: $appId,
    version: "1.0.0",
    description: "Fleet Operations AI Agent — Claude-powered assistant that queries RisingWave materialized views and can dispatch commands to vehicles",
    declaredConsumedEventVersionIds: $consumed,
    declaredProducedEventVersionIds: [$cmdEvv]
  }')"

ensure_app_version "$APP_TRYME_ID" "$(jq -n \
  --arg appId "$APP_TRYME_ID" \
  --arg cmdEvv "$EVV_COMMAND_ID" \
  --argjson consumed "$ALL_CONSUMER_EVV_IDS" \
  '{
    applicationId: $appId,
    version: "1.0.0",
    description: "Solace+ Try Me — interactive pub/sub explorer; replays RisingWave history then streams live events from Solace Platform; can publish commands to vehicles",
    declaredConsumedEventVersionIds: $consumed,
    declaredProducedEventVersionIds: [$cmdEvv]
  }')"

echo ""
echo "Fleet Operations domain ready."
echo "  Domain ID : $DOMAIN_ID"
echo ""
echo "Event version IDs (used by generate_mvs.py):"
echo "  telemetry : $EVV_TELEMETRY_ID"
echo "  alert     : $EVV_ALERT_ID"
echo "  command   : $EVV_COMMAND_ID"
