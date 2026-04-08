#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Demo Query Walkthrough — Solace Streaming Architecture POC
#
# Connects to RisingWave (Postgres wire protocol, port 4566) and runs a
# curated set of queries demonstrating the "virtual topic" pattern:
#   each materialized view = a Solace wildcard subscription expressed as SQL.
#
# Prerequisites: psql must be available on the host.
# Usage: ./demo/demo_queries.sh [--host localhost] [--port 4566]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RW_HOST="${1:-localhost}"
RW_PORT="${2:-4566}"
PSQL="psql -h ${RW_HOST} -p ${RW_PORT} -U root -d dev"

# ── Helpers ───────────────────────────────────────────────────────────────────
hr() { printf '\n%s\n' "$(printf '─%.0s' {1..72})"; }
banner() {
  hr
  echo "  $1"
  hr
}
run_query() {
  local label="$1"
  local sql="$2"
  echo ""
  echo "▶  ${label}"
  echo "   SQL: ${sql}"
  echo ""
  ${PSQL} -c "${sql}"
}

# ─────────────────────────────────────────────────────────────────────────────
banner "Solace Streaming Architecture — Virtual Topic Demo"
echo "  RisingWave:  ${RW_HOST}:${RW_PORT}"
echo "  Each query = a Solace subscription expressed in SQL"
# ─────────────────────────────────────────────────────────────────────────────

# ── 1. Current high-severity alerts ──────────────────────────────────────────
banner "1 of 7  |  HIGH-SEVERITY ALERTS
  Solace equivalent: subscribe to  fleet/events/*/alerts/high"

run_query "All high-severity alerts (most recent first)" \
  "SELECT vehicle_id, event_type, description, occurred_at
   FROM high_severity_alerts
   ORDER BY occurred_at DESC
   LIMIT 20;"

# ── 2. Real-time speed readings ───────────────────────────────────────────────
banner "2 of 7  |  REAL-TIME SPEED READINGS
  Solace equivalent: subscribe to  fleet/telemetry/*/metrics/speed"

run_query "Current speed for all vehicles" \
  "SELECT vehicle_id, speed_mph, latitude, longitude, recorded_at
   FROM vehicle_speeds
   ORDER BY recorded_at DESC
   LIMIT 20;"

# ── 3. 5-minute tumbling window averages ─────────────────────────────────────
banner "3 of 7  |  5-MINUTE SPEED AVERAGES (TUMBLING WINDOW)
  No Solace equivalent — requires external aggregation state"

run_query "Speed averages per vehicle, most recent windows" \
  "SELECT vehicle_id,
          window_start, window_end,
          ROUND(avg_speed_mph::numeric, 2) AS avg_mph,
          ROUND(max_speed_mph::numeric, 2) AS max_mph,
          sample_count
   FROM vehicle_speed_5min_avg
   ORDER BY window_end DESC, vehicle_id
   LIMIT 20;"

# ── 4. Fleet alert summary ────────────────────────────────────────────────────
banner "4 of 7  |  FLEET ALERT SUMMARY (ROLLING 1 HOUR)
  No Solace equivalent — requires stateful counting across all topics"

run_query "Alert counts by severity (last hour)" \
  "SELECT severity,
          alert_count,
          affected_vehicles
   FROM fleet_alert_summary
   ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END;"

# ── 5. Correlated alerts + telemetry ─────────────────────────────────────────
banner "5 of 7  |  ALERTS WITH CORRELATED TELEMETRY CONTEXT
  No Solace equivalent — requires stream-stream join across topics"

run_query "Alerts with telemetry recorded within 2 min before the event" \
  "SELECT vehicle_id, event_type, severity, description,
          alert_time, metric_type,
          ROUND(metric_value::numeric, 2) AS metric_value, unit,
          time_before_alert
   FROM alerts_with_context
   ORDER BY alert_time DESC
   LIMIT 10;"

# ── 6. Geofence: vehicles in Boston metro ─────────────────────────────────────
banner "6 of 7  |  GEOFENCE — VEHICLES IN BOSTON METRO (42.2–42.5°N, 71.2–70.9°W)
  No Solace equivalent — spatial filtering requires a separate service"

run_query "Vehicles currently inside the Boston metro bounding box" \
  "SELECT vehicle_id,
          ROUND(latitude::numeric,  5) AS lat,
          ROUND(longitude::numeric, 5) AS lon,
          ROUND(speed_mph::numeric, 1) AS speed_mph,
          recorded_at
   FROM vehicles_in_region_boston
   ORDER BY recorded_at DESC
   LIMIT 10;"

# ── 7. Per-vehicle event count heatmap ───────────────────────────────────────
banner "7 of 7  |  VEHICLE ACTIVITY HEATMAP (LAST HOUR)"

run_query "Alert count per vehicle, segmented by severity" \
  "SELECT vehicle_id, severity, event_count
   FROM vehicle_event_counts_1h
   ORDER BY event_count DESC, vehicle_id
   LIMIT 20;"

hr
echo ""
echo "  Demo complete."
echo ""
echo "  Connect directly for live queries:"
echo "    psql -h ${RW_HOST} -p ${RW_PORT} -U root -d dev"
echo ""
echo "  Redpanda Console (topic browser):"
echo "    http://localhost:8888"
echo ""
echo "  RisingWave Dashboard:"
echo "    http://localhost:5691"
hr
echo ""
