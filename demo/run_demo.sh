#!/usr/bin/env bash
# =============================================================================
# run_demo.sh — Full end-to-end startup + demo for the Solace Streaming POC
#
# Orchestrates:
#   1. docker-compose up (all infrastructure)
#   2. Wait for all services to be healthy
#   3. Configure Solace Platform (SEMP v2) — VPN, user, ACL
#   4. Initialize RisingWave schema (CDC table + materialized views)
#   5. Wait for data to flow through the pipeline
#   6. Auto-launch Fleet Operations AI UI
#
# Requirements: docker, docker-compose, psql, curl, python3
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

# ── Helper: wait for Solace message spool ────────────────────────────────────
# SEMP becomes responsive before the message spool finishes initializing.
# Poll the default VPN's monitor endpoint until operationalStatus is "Up".
wait_for_solace_spool() {
  local max_wait="${1:-180}"
  local elapsed=0
  log "Waiting for Solace message spool to become active..."
  while true; do
    local status
    status=$(curl -sf -u "admin:admin" \
      "http://localhost:8180/SEMP/v2/monitor/msgVpns/default" 2>/dev/null | \
      ${PYTHON} -c "
import sys, json
try:
    d = json.load(sys.stdin)
    data = d.get('data', {})
    # Check several possible readiness indicators
    op = data.get('operationalStatus', data.get('state', ''))
    spool = data.get('msgSpoolOperational', None)
    if spool is True or op.lower() == 'up':
        print('Up')
    else:
        print(op or 'NotReady')
except Exception:
    print('Unknown')
" 2>/dev/null || echo "Unknown")
    if [[ "${status}" == "Up" ]]; then
      log "Solace message spool is active."
      break
    fi
    sleep 5
    elapsed=$((elapsed + 5))
    [[ ${elapsed} -ge ${max_wait} ]] && fail "Solace message spool did not become active within ${max_wait}s"
    log "  still waiting for message spool... (${elapsed}s, status=${status})"
  done
}

# ── Helper: wait for TCP port ────────────────────────────────────────────────
wait_for_port() {
  local label="$1"
  local host="$2"
  local port="$3"
  local max_wait="${4:-180}"
  local elapsed=0
  log "Waiting for ${label} at ${host}:${port}..."
  while ! bash -c "echo > /dev/tcp/${host}/${port}" 2>/dev/null; do
    sleep 5
    elapsed=$((elapsed + 5))
    [[ ${elapsed} -ge ${max_wait} ]] && fail "${label} did not become available within ${max_wait}s"
    log "  still waiting for ${label}... (${elapsed}s)"
  done
  log "${label} is ready."
}

# ─── STEP 0: Clear previous container logs ───────────────────────────────────
log "=== STEP 0: Removing existing containers (clears logs) ==="
docker-compose down --remove-orphans 2>/dev/null || true

# ─── STEP 1: Build and start infrastructure ───────────────────────────────────
log "=== STEP 1: Starting infrastructure ==="
if [[ "${SKIP_BUILD}" == "true" ]]; then
  log "Skipping build (--skip-build set)"
  docker-compose up -d
else
  log "Building images (fleet-agent, fleet-generator, tryme)..."
  docker-compose build fleet-agent fleet-generator tryme
  docker-compose up -d
fi
log "docker-compose started. Waiting for services to become healthy..."

# ─── STEP 2: Wait for all services ───────────────────────────────────────────
log "=== STEP 2: Health checks ==="

wait_for_url "Solace Platform SEMP" \
  "http://localhost:8180/SEMP/v2/config/about/api" \
  "admin:admin" 180

wait_for_solace_spool 180

wait_for_psql "localhost" 4566 120

log "All services healthy."

# ─── STEP 3: Configure Solace Platform ──────────────────────────────────────
log "=== STEP 3: Configuring Solace Platform (VPN, user) ==="
bash config/solace/setup.sh localhost

# ─── STEP 4: Initialize RisingWave schema ────────────────────────────────────
log "=== STEP 4: Initializing RisingWave CDC table and materialized views ==="

# Generate topic-mv-registry.yaml (read by solace+ CLI).
# Use EP catalog if SOLACE_CLOUD_TOKEN is set; otherwise generate static mappings only.
SOLACE_CLOUD_TOKEN="${SOLACE_CLOUD_TOKEN:-$(grep -E '^SOLACE_CLOUD_TOKEN=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'"'"' ')}"
if [[ -n "${SOLACE_CLOUD_TOKEN}" ]]; then
  log "  Generating topic-mv-registry.yaml from Event Portal catalog..."
  SOLACE_CLOUD_TOKEN="${SOLACE_CLOUD_TOKEN}" ${PYTHON} generate_mvs.py 2>&1 | sed 's/^/  /'
else
  log "  SOLACE_CLOUD_TOKEN not set — generating static registry only..."
  ${PYTHON} generate_mvs.py --skip-ep 2>&1 | sed 's/^/  /'
fi

psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
log "  RisingWave schema initialized."

# ─── STEP 5: Generator ───────────────────────────────────────────────────────
log "=== STEP 5: Fleet telemetry generator ==="
log "  Running as container fleet-generator (started by docker-compose)."
log "  It will begin publishing once the streaming-poc VPN is ready."
if [[ "${BURST_MODE}" == "true" ]]; then
  log "  Burst mode requested — restarting fleet-generator with BURST=true..."
  BURST=true docker-compose up -d fleet-generator
fi

# ─── STEP 6: Wait for data to propagate ──────────────────────────────────────
log "=== STEP 6: Waiting 35 seconds for data to flow through pipeline ==="
log "  Solace Platform → rw-ingest queue → risingwave-rdp (RDP) → RisingWave webhook"
for i in {1..7}; do
  sleep 5
  count=$(
    psql -h localhost -p 4566 -U root -d dev -t -c \
      "SELECT COUNT(*) FROM fleet_telemetry_raw" 2>/dev/null \
    | tr -d ' \n' || echo "0"
  )
  log "  RisingWave fleet_telemetry_raw row count: ${count}"
done

log ""
log "=== Demo complete ==="

# ─── Auto-launch Fleet Operations AI UI ──────────────────────────────────────
wait_for_url "Fleet Operations AI UI" "http://localhost:8090" "" 60
wait_for_url "Solace+ Try Me" "http://localhost:8091" "" 60
if command -v xdg-open &>/dev/null; then
  xdg-open "http://localhost:8090" &>/dev/null &
elif command -v open &>/dev/null; then
  open "http://localhost:8090"
fi
log ""
log "  Explore live:"
log "    http://localhost:8090    (Fleet Operations AI — agentic demo)"
log "    http://localhost:8091    (Solace+ Try Me — pub/sub with history replay)"
log "    http://localhost:5691    (RisingWave Dashboard)"
log "    http://localhost:8180    (Solace Platform admin)"
log "    psql -h localhost -p 4566 -U root -d dev"
log ""
log "  Generator logs:"
log "    docker logs -f fleet-generator"
log ""
log "  To stop everything:"
log "    docker-compose down"
