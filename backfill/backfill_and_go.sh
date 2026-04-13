#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# BACKFILL AND GO -- Event-Driven Design
#
# The connector handles sentinel detection, barrier flush,
# and readiness signaling via Solace event internally.
# This script orchestrates the startup sequence.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── Configuration (from .env or environment) ─────────────────

source_env() {
    if [[ -f "$PROJECT_ROOT/.env" ]]; then
        set -a
        # shellcheck source=/dev/null
        source "$PROJECT_ROOT/.env"
        set +a
    fi
}
source_env

SOLACE_HOST="${SOLACE_HOST:-tcp://localhost:55554}"
SOLACE_SEMP="${SOLACE_SEMP:-http://localhost:8180}"
SOLACE_VPN="${SOLACE_VPN:-streaming-poc}"
SOLACE_USER="${SOLACE_USER:-streaming-user}"
SOLACE_PASSWORD="${SOLACE_PASSWORD:-default}"
SOLACE_ADMIN_USER="${SOLACE_ADMIN_USER:-admin}"
SOLACE_ADMIN_PASS="${SOLACE_ADMIN_PASS:-admin}"
SENTINEL_TOPIC="${SENTINEL_TOPIC:-fleet/system/sentinel}"
RW_HOST="${RW_HOST:-localhost}"
RW_PORT="${RW_PORT:-4566}"

# Detect Python
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" &>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    done
fi

echo "============================================"
echo "  SOLACE + RISINGWAVE BACKFILL ORCHESTRATOR"
echo "  (Event-driven readiness)"
echo "============================================"
echo ""

# ── Step 1: Check Solace queue depth ─────────────────────────

echo "Step 1: Checking Solace queue depth..."
QUEUE_ENCODED=$("$PYTHON" -c "import urllib.parse; print(urllib.parse.quote('q/risingwave-ingest', safe=''))")
QUEUE_DEPTH=$(curl -s -u "${SOLACE_ADMIN_USER}:${SOLACE_ADMIN_PASS}" \
  "${SOLACE_SEMP}/SEMP/v2/monitor/msgVpns/${SOLACE_VPN}/queues/${QUEUE_ENCODED}" \
  | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['data']['spooledMsgCount'])" 2>/dev/null || echo "0")
echo "  Queue depth: ${QUEUE_DEPTH} messages"
echo ""

# ── Step 2: Publish sentinel ─────────────────────────────────

echo "Step 2: Publishing sentinel..."
"$PYTHON" "$SCRIPT_DIR/publish_sentinel.py" \
  --host "${SOLACE_HOST}" --vpn "${SOLACE_VPN}" \
  --username "${SOLACE_USER}" --password "${SOLACE_PASSWORD}" \
  --topic "${SENTINEL_TOPIC}"
echo ""

# ── Step 3: Create RisingWave schema (starts connector) ─────

echo "Step 3: Creating RisingWave schema (starts connector)..."
psql -h "${RW_HOST}" -p "${RW_PORT}" -U root -d dev \
  -f "$SCRIPT_DIR/risingwave/init.sql"
echo "  Schema created. Connector is consuming."
echo ""

# ── Step 4: Wait for readiness event ─────────────────────────

echo "Step 4: Waiting for readiness event from connector..."
"$PYTHON" -c "
import sys
sys.path.insert(0, '${PROJECT_ROOT}')
from backfill.query_client import RisingWaveClient
client = RisingWaveClient(
    rw_host='${RW_HOST}', rw_port=${RW_PORT},
    solace_host='${SOLACE_HOST}', solace_vpn='${SOLACE_VPN}',
    solace_user='${SOLACE_USER}', solace_pass='${SOLACE_PASSWORD}',
)
status = client.wait_for_ready(timeout=600)
hist = status.get('historical_events', '?')
src = status.get('source', 'unknown')
count = f'{hist:,}' if isinstance(hist, int) else str(hist)
print(f'Ready: {count} events via {src}')
client.close()
"
echo ""

# ── Step 5: Verification queries ─────────────────────────────

echo "Step 5: Verification queries..."
echo ""
echo "--- Connector Status ---"
psql -h "${RW_HOST}" -p "${RW_PORT}" -U root -d dev \
  -c "SELECT * FROM rw_solace_connector_status;" 2>/dev/null || echo "  (status table not yet created)"

echo ""
echo "--- High Severity Alerts (top 5) ---"
psql -h "${RW_HOST}" -p "${RW_PORT}" -U root -d dev \
  -c "SELECT vehicle_id, severity, event_timestamp FROM high_severity_alerts ORDER BY event_timestamp DESC LIMIT 5;" 2>/dev/null || echo "  (no data yet)"

echo ""
echo "--- Fleet Alert Summary ---"
psql -h "${RW_HOST}" -p "${RW_PORT}" -U root -d dev \
  -c "SELECT * FROM fleet_alert_summary;" 2>/dev/null || echo "  (no data yet)"

echo ""
echo "============================================"
echo "  SYSTEM IS LIVE"
echo "  Readiness event received from connector."
echo "  All MVs reflect complete history."
echo "  Consumers can query with confidence."
echo "============================================"
