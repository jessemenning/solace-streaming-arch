-- =============================================================================
-- RisingWave Streaming SQL — Fleet Monitoring POC
-- =============================================================================
-- Architecture note:
--   The Solace Platform REST Delivery Point (RDP) receives messages from the
--   rw-ingest queue and HTTP POSTs each payload to RisingWave's webhook
--   endpoint.  No connector-node, no Kafka, no Redpanda.
--
--   Every message embeds its original Solace topic in the `solace_topic`
--   field.  RisingWave uses that field to filter streams — replacing Solace
--   wildcard subscriptions with SQL WHERE clauses.
--
--   Solace Platform (fleet/>) → rw-ingest queue → risingwave-rdp RDP →
--     HTTP POST → fleet_all_raw (webhook TABLE — data JSONB)
--          ├─ fleet_telemetry_raw   (MV — solace_topic LIKE 'fleet/telemetry/%')
--          ├─ fleet_events_raw      (MV — solace_topic LIKE 'fleet/events/%')
--          └─ fleet_commands_raw    (MV — solace_topic LIKE 'fleet/commands/%')
-- =============================================================================

SET timezone = 'UTC';
SET client_min_messages = WARNING;

-- ── Tear down previous objects (safe to re-run) ──────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS vehicle_event_counts_1h      CASCADE;
DROP MATERIALIZED VIEW IF EXISTS vehicle_last_known_position  CASCADE;
DROP MATERIALIZED VIEW IF EXISTS vehicles_in_region_boston    CASCADE;
DROP MATERIALIZED VIEW IF EXISTS alerts_with_context          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS high_engine_temp_vehicles    CASCADE;
DROP MATERIALIZED VIEW IF EXISTS vehicle_fuel_levels          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS fleet_alert_summary          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS vehicle_speed_5min_avg       CASCADE;
DROP MATERIALIZED VIEW IF EXISTS vehicle_speeds               CASCADE;
DROP MATERIALIZED VIEW IF EXISTS low_severity_alerts          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS medium_severity_alerts       CASCADE;
DROP MATERIALIZED VIEW IF EXISTS high_severity_alerts         CASCADE;
DROP MATERIALIZED VIEW IF EXISTS fleet_telemetry_raw          CASCADE;
DROP MATERIALIZED VIEW IF EXISTS fleet_events_raw             CASCADE;
DROP MATERIALIZED VIEW IF EXISTS fleet_commands_raw           CASCADE;
-- Drop as TABLE (webhook path) or SOURCE (legacy Kafka/connector-node path)
DROP TABLE  IF EXISTS fleet_all_raw         CASCADE;
DROP SOURCE IF EXISTS fleet_all_raw         CASCADE;
DROP SOURCE IF EXISTS fleet_telemetry_raw   CASCADE;
DROP SOURCE IF EXISTS fleet_events_raw      CASCADE;
DROP SOURCE IF EXISTS fleet_commands_raw    CASCADE;


-- ── Webhook ingest table — one row per Solace message, payload as JSONB ──────
-- RisingWave webhook connector requires a single JSONB column.
-- The Solace RDP POSTs the generator's JSON payload as the HTTP body.
-- Every message includes solace_topic, enabling topic-based routing in MVs below.
--
-- Webhook endpoint: http://risingwave:4560/webhook/dev/public/fleet_all_raw
CREATE TABLE fleet_all_raw (
    data JSONB
) WITH (
    connector = 'webhook'
);


-- ── Routing views — "virtual topic subscriptions" ────────────────────────────
-- These replace Solace wildcard subscriptions.  Subscribing to
--   fleet/telemetry/*/metrics/*
-- is now:
--   SELECT * FROM fleet_telemetry_raw;
--
-- Fields are extracted from the JSONB payload and cast to their correct types.

CREATE MATERIALIZED VIEW fleet_telemetry_raw AS
SELECT
    data->>'vehicle_id'                        AS vehicle_id,
    data->>'metric_type'                       AS metric_type,
    (data->>'value')::DOUBLE PRECISION         AS value,
    data->>'unit'                              AS unit,
    (data->>'latitude')::DOUBLE PRECISION      AS latitude,
    (data->>'longitude')::DOUBLE PRECISION     AS longitude,
    (data->>'recorded_at')::TIMESTAMPTZ        AS recorded_at,
    data->>'solace_topic'                      AS solace_topic
FROM   fleet_all_raw
WHERE  data->>'solace_topic' LIKE 'fleet/telemetry/%';

CREATE MATERIALIZED VIEW fleet_events_raw AS
SELECT
    data->>'vehicle_id'                        AS vehicle_id,
    data->>'event_type'                        AS event_type,
    data->>'severity'                          AS severity,
    data->>'description'                       AS description,
    (data->'payload')                          AS payload,
    (data->>'occurred_at')::TIMESTAMPTZ        AS occurred_at,
    data->>'solace_topic'                      AS solace_topic
FROM   fleet_all_raw
WHERE  data->>'solace_topic' LIKE 'fleet/events/%';

CREATE MATERIALIZED VIEW fleet_commands_raw AS
SELECT
    data->>'vehicle_id'                        AS vehicle_id,
    data->>'command_type'                      AS command_type,
    (data->'parameters')                       AS parameters,
    (data->>'issued_at')::TIMESTAMPTZ          AS issued_at,
    data->>'issued_by'                         AS issued_by,
    data->>'solace_topic'                      AS solace_topic
FROM   fleet_all_raw
WHERE  data->>'solace_topic' LIKE 'fleet/commands/%';


-- =============================================================================
-- ANALYTICS MATERIALIZED VIEWS — "Virtual Topics"
-- =============================================================================

-- ─── 1. High-severity alerts ─────────────────────────────────────────────────
CREATE MATERIALIZED VIEW high_severity_alerts AS
SELECT vehicle_id, event_type, description, payload, occurred_at, solace_topic
FROM   fleet_events_raw
WHERE  severity = 'high';

-- ─── 2. Alert severity tiers ─────────────────────────────────────────────────
CREATE MATERIALIZED VIEW medium_severity_alerts AS
SELECT vehicle_id, event_type, description, payload, occurred_at
FROM   fleet_events_raw
WHERE  severity = 'medium';

CREATE MATERIALIZED VIEW low_severity_alerts AS
SELECT vehicle_id, event_type, description, payload, occurred_at
FROM   fleet_events_raw
WHERE  severity = 'low';

-- ─── 3. Real-time speed readings ─────────────────────────────────────────────
CREATE MATERIALIZED VIEW vehicle_speeds AS
SELECT vehicle_id, value AS speed_mph, latitude, longitude, recorded_at, solace_topic
FROM   fleet_telemetry_raw
WHERE  metric_type = 'speed';

-- ─── 4. 5-minute tumbling window — speed averages per vehicle ────────────────
-- Uses fleet_telemetry_raw (typed columns extracted from JSONB) as the TUMBLE source.
CREATE MATERIALIZED VIEW vehicle_speed_5min_avg AS
SELECT
    vehicle_id,
    window_start,
    window_end,
    AVG(value)  AS avg_speed_mph,
    MAX(value)  AS max_speed_mph,
    MIN(value)  AS min_speed_mph,
    COUNT(*)    AS sample_count
FROM TUMBLE(fleet_telemetry_raw, recorded_at, INTERVAL '5 MINUTES')
WHERE metric_type = 'speed'
  AND recorded_at IS NOT NULL
GROUP BY vehicle_id, window_start, window_end;

-- ─── 5. Fleet-wide alert summary — rolling 1-hour window ─────────────────────
CREATE MATERIALIZED VIEW fleet_alert_summary AS
SELECT
    severity,
    COUNT(*)                   AS alert_count,
    COUNT(DISTINCT vehicle_id) AS affected_vehicles
FROM fleet_events_raw
WHERE occurred_at > now() - INTERVAL '1 HOUR'
GROUP BY severity;

-- ─── 6. Per-vehicle fuel levels ──────────────────────────────────────────────
CREATE MATERIALIZED VIEW vehicle_fuel_levels AS
SELECT vehicle_id, value AS fuel_pct, recorded_at
FROM   fleet_telemetry_raw
WHERE  metric_type = 'fuel_level';

-- ─── 7. High engine temperature alerts ───────────────────────────────────────
CREATE MATERIALIZED VIEW high_engine_temp_vehicles AS
SELECT vehicle_id, value AS engine_temp_f, recorded_at
FROM   fleet_telemetry_raw
WHERE  metric_type = 'engine_temp'
  AND  value > 220;

-- ─── 8. Alerts with correlated telemetry (stream-stream join) ────────────────
CREATE MATERIALIZED VIEW alerts_with_context AS
SELECT
    e.vehicle_id,
    e.event_type,
    e.severity,
    e.description,
    e.occurred_at                    AS alert_time,
    t.metric_type,
    t.value                          AS metric_value,
    t.unit,
    t.recorded_at                    AS metric_time,
    e.occurred_at - t.recorded_at    AS time_before_alert
FROM fleet_events_raw    e
JOIN fleet_telemetry_raw t
    ON  e.vehicle_id = t.vehicle_id
    AND t.recorded_at BETWEEN e.occurred_at - INTERVAL '2 MINUTES'
                          AND e.occurred_at;

-- ─── 9. Geofence — vehicles in Boston metro bounding box ─────────────────────
CREATE MATERIALIZED VIEW vehicles_in_region_boston AS
SELECT vehicle_id, latitude, longitude, value AS speed_mph, recorded_at
FROM   fleet_telemetry_raw
WHERE  metric_type = 'speed'
  AND  latitude  BETWEEN 42.2 AND 42.5
  AND  longitude BETWEEN -71.2 AND -70.9;

-- ─── 10. Per-vehicle latest position ─────────────────────────────────────────
CREATE MATERIALIZED VIEW vehicle_last_known_position AS
SELECT vehicle_id, latitude, longitude, value AS speed_mph, recorded_at
FROM   fleet_telemetry_raw
WHERE  metric_type = 'speed';

-- ─── 11. 1-hour event count per vehicle ──────────────────────────────────────
CREATE MATERIALIZED VIEW vehicle_event_counts_1h AS
SELECT
    vehicle_id,
    severity,
    COUNT(*) AS event_count
FROM fleet_events_raw
WHERE occurred_at > now() - INTERVAL '1 HOUR'
GROUP BY vehicle_id, severity;
