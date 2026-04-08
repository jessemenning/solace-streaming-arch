#!/usr/bin/env bash
# =============================================================================
# run_demo.sh — Full end-to-end startup + demo for the Solace Streaming POC
#
# Orchestrates:
#   1. docker-compose up (all infrastructure)
#   2. Wait for all services to be healthy
#   3. Configure Solace Platform (SEMP v2)
#   4. Create Redpanda topics
#   5. Deploy Solace → Redpanda Kafka Connect connector
#   6. Initialize RisingWave schema (sources + materialized views)
#   7. Start fleet telemetry generator in the background
#   8. Wait for data to flow through the pipeline
#   9. Run demo query walkthrough
#
# Requirements: docker, docker-compose, psql, curl, python3, pip
# Usage: ./demo/run_demo.sh [--burst] [--skip-build]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

BURST_MODE=false
SKIP_BUILD=false

for arg in "$@"; do
  case "$arg" in
    --burst)      BURST_MODE=true ;;
    --skip-build) SKIP_BUILD=true ;;
    --help|-h)
      echo "Usage: $0 [--burst] [--skip-build]"
      echo "  --burst       Enable continuous high-severity alert spike for vehicle_005"
      echo "  --skip-build  Skip docker-compose build (use existing images)"
      exit 0 ;;
  esac
done

log()  { echo "[run_demo] $(date '+%H:%M:%S')  $*"; }
fail() { echo "[run_demo] FATAL: $*" >&2; exit 1; }

# Prefer python3.13 if available (system python3 may lack pip/packages)
if command -v python3.13 &>/dev/null; then
  PYTHON=python3.13
else
  PYTHON=python3
fi

# ── Preflight checks ──────────────────────────────────────────────────────────
for cmd in docker curl python3 psql; do
  if ! command -v "${cmd}" &>/dev/null; then
    fail "Required command not found: ${cmd}
    Install hint:
      psql  → sudo apt-get install -y postgresql-client
      curl  → sudo apt-get install -y curl
      docker → https://docs.docker.com/engine/install/"
  fi
done
log "Preflight checks passed (docker, curl, python3, psql)."

cleanup() { log "Cleaning up..."; }
trap cleanup EXIT

# ── Helper: wait for a URL to respond ────────────────────────────────────────
wait_for_url() {
  local label="$1"
  local url="$2"
  local user="${3:-}"
  local max_wait="${4:-180}"
  local elapsed=0
  local curl_auth=()
  [[ -n "${user}" ]] && curl_auth=(-u "${user}")

  log "Waiting for ${label} at ${url}..."
  while ! curl -sf "${curl_auth[@]}" "${url}" > /dev/null 2>&1; do
    sleep 5
    elapsed=$((elapsed + 5))
    if [[ ${elapsed} -ge ${max_wait} ]]; then
      fail "${label} did not become available within ${max_wait}s"
    fi
    log "  still waiting for ${label}... (${elapsed}s)"
  done
  log "${label} is ready."
}

# ── Helper: wait for psql ────────────────────────────────────────────────────
wait_for_psql() {
  local host="$1"
  local port="$2"
  local max_wait="${3:-120}"
  local elapsed=0
  log "Waiting for RisingWave at ${host}:${port}..."
  while ! psql -h "${host}" -p "${port}" -U root -d dev -c "SELECT 1" > /dev/null 2>&1; do
    sleep 5
    elapsed=$((elapsed + 5))
    [[ ${elapsed} -ge ${max_wait} ]] && fail "RisingWave not available within ${max_wait}s"
    log "  still waiting for RisingWave... (${elapsed}s)"
  done
  log "RisingWave is ready."
}

# ─── STEP 1: Build and start infrastructure ───────────────────────────────────
log "=== STEP 1: Starting infrastructure ==="
if [[ "${SKIP_BUILD}" == "true" ]]; then
  log "Skipping build (--skip-build set)"
  docker-compose up -d
else
  # Download the Solace connector JARs to the host so the Dockerfile can COPY them in.
  # (The cp-kafka-connect image has no package manager, so we can't download at build time.)
  log "Downloading Solace connector JARs to config/kafka-connect/plugins/ ..."
  bash config/kafka-connect/download-connector.sh

  log "Building images (kafka-connect, fleet-agent, fleet-generator)..."
  docker-compose build kafka-connect fleet-agent fleet-generator
  docker-compose up -d
fi
log "docker-compose started. Waiting for services to become healthy..."

# ─── STEP 2: Wait for all services ───────────────────────────────────────────
log "=== STEP 2: Health checks ==="

wait_for_url "Solace Platform SEMP" \
  "http://localhost:8180/SEMP/v2/config/about/api" \
  "admin:admin" 180

wait_for_url "Redpanda Schema Registry" \
  "http://localhost:8081/subjects" \
  "" 120

wait_for_psql "localhost" 4566 120

wait_for_url "Kafka Connect" \
  "http://localhost:8083/" \
  "" 180

log "All services healthy."

# ─── STEP 3: Configure Solace Platform ───────────────────────────────────────
log "=== STEP 3: Configuring Solace Platform ==="
bash config/solace/setup.sh localhost

# ─── STEP 4: Create Redpanda topics ──────────────────────────────────────────
log "=== STEP 4: Creating Redpanda topics ==="
bash config/redpanda/setup.sh

# ─── STEP 5: Deploy Kafka Connect connector ───────────────────────────────────
log "=== STEP 5: Deploying Solace → Redpanda connector ==="

# Remove existing connector if present (idempotent restart)
if curl -sf http://localhost:8083/connectors/solace-redpanda-bridge > /dev/null 2>&1; then
  log "  Removing existing connector..."
  curl -sf -X DELETE http://localhost:8083/connectors/solace-redpanda-bridge > /dev/null
  sleep 2
fi

CONNECTOR_RESPONSE=$(
  curl -s -w "\n__HTTP_STATUS__%{http_code}" -X POST http://localhost:8083/connectors \
    -H "Content-Type: application/json" \
    -d @config/kafka-connect/solace-source.json
)
HTTP_STATUS=$(echo "${CONNECTOR_RESPONSE}" | grep "__HTTP_STATUS__" | sed 's/.*__HTTP_STATUS__//')
CONNECTOR_BODY=$(echo "${CONNECTOR_RESPONSE}" | sed '/__HTTP_STATUS__/d')
if [[ "${HTTP_STATUS}" != "201" && "${HTTP_STATUS}" != "200" ]]; then
  fail "Connector POST failed (HTTP ${HTTP_STATUS}): ${CONNECTOR_BODY}"
fi
log "  Connector deployed: $(echo "${CONNECTOR_BODY}" | "${PYTHON}" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("name","?"))' 2>/dev/null || echo 'ok')"

# Wait for connector to enter RUNNING state
log "  Waiting for connector to reach RUNNING state..."
for i in {1..30}; do
  state=$(
    curl -sf "http://localhost:8083/connectors/solace-redpanda-bridge/status" 2>/dev/null \
    | "${PYTHON}" -c 'import json,sys; d=json.load(sys.stdin); print(d["connector"]["state"])' 2>/dev/null \
    || echo "UNKNOWN"
  )
  if [[ "${state}" == "RUNNING" ]]; then
    log "  Connector is RUNNING."; break
  fi
  [[ $i -eq 30 ]] && log "  WARNING: connector did not reach RUNNING within 150s — check http://localhost:8083"
  sleep 5
done

# ─── STEP 6: Initialize RisingWave schema ────────────────────────────────────
log "=== STEP 6: Initializing RisingWave sources and materialized views ==="
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
log "  RisingWave schema initialized."

# ─── STEP 7: Generator ───────────────────────────────────────────────────────
log "=== STEP 7: Fleet telemetry generator ==="
log "  Running as container fleet-generator (started by docker-compose)."
log "  It will begin publishing once the streaming-poc VPN is ready."
if [[ "${BURST_MODE}" == "true" ]]; then
  log "  Burst mode requested — restarting fleet-generator with BURST=true..."
  BURST=true docker-compose up -d fleet-generator
fi

# ─── STEP 8: Wait for data to propagate ──────────────────────────────────────
log "=== STEP 8: Waiting 35 seconds for data to flow through pipeline ==="
log "  Solace → Redpanda → RisingWave"
for i in {1..7}; do
  sleep 5
  count=$(
    psql -h localhost -p 4566 -U root -d dev -t -c \
      "SELECT COUNT(*) FROM fleet_telemetry_raw" 2>/dev/null \
    | tr -d ' \n' || echo "0"
  )
  log "  RisingWave fleet_telemetry_raw row count: ${count}"
done

# ─── STEP 9: Demo queries ─────────────────────────────────────────────────────
log "=== STEP 9: Running demo queries ==="
bash demo/demo_queries.sh

log ""
log "=== Demo complete ==="
log ""
log "  Explore live:"
log "    http://localhost:8090    (Fleet Operations AI — agentic demo)"
log "    http://localhost:8888    (Redpanda Console)"
log "    http://localhost:5691    (RisingWave Dashboard)"
log "    http://localhost:8180    (Solace Platform admin)"
log "    psql -h localhost -p 4566 -U root -d dev"
log ""
log "  Generator logs:"
log "    docker logs -f fleet-generator"
log ""
log "  To stop everything:"
log "    docker-compose down"
