-- ============================================================
-- RISINGWAVE SCHEMA -- EVENT-DRIVEN BACKFILL DESIGN
--
-- No sentinel filters. No backfill logic. Just business SQL.
-- The sentinel is intercepted by the connector and never
-- reaches this source table.
--
-- This schema uses the unified solace_events source table for
-- the backfill pattern. The production schema (config/risingwave/init.sql)
-- uses three separate sources for schema separation.
-- ============================================================

-- ----------------------------------------------------------
-- STATUS TABLE (fallback for late-joining consumers)
-- Created by the connector automatically, but we define it
-- here so it exists before the connector starts.
-- ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS rw_solace_connector_status (
    connector_name          VARCHAR PRIMARY KEY,
    is_ready                BOOLEAN DEFAULT FALSE,
    sentinel_detected_at    TIMESTAMPTZ,
    events_before_sentinel  BIGINT,
    total_events_consumed   BIGINT,
    last_updated            TIMESTAMPTZ DEFAULT NOW()
);

-- ----------------------------------------------------------
-- SOURCE TABLE
-- Receives ONLY business events from the connector.
-- Sentinel is intercepted and never emitted.
-- ----------------------------------------------------------

-- DROP SOURCE IF EXISTS solace_events CASCADE;

-- NOTE: This CREATE SOURCE statement is a reference for when
-- the backfill-aware connector is merged. The WITH clause
-- parameters match the native Solace connector syntax.
-- Uncomment and adjust when the connector supports sentinel
-- detection and readiness publishing.

-- CREATE SOURCE IF NOT EXISTS solace_events (
--     vehicle_id      VARCHAR,
--     event_type      VARCHAR,
--     metric_type     VARCHAR,
--     severity        VARCHAR,
--     value           DOUBLE PRECISION,
--     unit            VARCHAR,
--     latitude        DOUBLE PRECISION,
--     longitude       DOUBLE PRECISION,
--     description     VARCHAR,
--     payload         JSONB,
--     _rw_solace_destination VARCHAR,
--     _rw_solace_timestamp   TIMESTAMPTZ
-- ) WITH (
--     connector = 'solace',
--     host = 'tcp://solace:55555',
--     vpn = 'streaming-poc',
--     username = 'streaming-user',
--     password = 'default',
--     queue = 'q/risingwave-ingest',
--     guaranteed = 'true',
--     ack_mode = 'checkpoint'
-- ) FORMAT PLAIN ENCODE JSON;

-- ----------------------------------------------------------
-- BUSINESS MATERIALIZED VIEWS
-- No sentinel awareness needed. Pure domain logic.
-- These build on the solace_events source once it exists.
-- ----------------------------------------------------------

-- High severity alerts
-- CREATE MATERIALIZED VIEW IF NOT EXISTS high_severity_alerts AS
-- SELECT vehicle_id, event_type, severity, description, payload,
--        _rw_solace_timestamp AS event_timestamp
-- FROM solace_events
-- WHERE severity = 'high';

-- Vehicle speeds (from telemetry)
-- CREATE MATERIALIZED VIEW IF NOT EXISTS vehicle_speeds AS
-- SELECT vehicle_id, value AS speed_mph, _rw_solace_timestamp AS event_timestamp
-- FROM solace_events
-- WHERE metric_type = 'speed';

-- 5-minute speed averages (TUMBLE on source, not MV)
-- CREATE MATERIALIZED VIEW IF NOT EXISTS vehicle_speed_5min_avg AS
-- SELECT
--     vehicle_id,
--     window_start,
--     window_end,
--     AVG(value) AS avg_speed,
--     MAX(value) AS max_speed,
--     COUNT(*)   AS sample_count
-- FROM TUMBLE(solace_events, _rw_solace_timestamp, INTERVAL '5 MINUTES')
-- WHERE metric_type = 'speed'
-- GROUP BY vehicle_id, window_start, window_end;

-- Fleet alert summary (rolling 1h window)
-- CREATE MATERIALIZED VIEW IF NOT EXISTS fleet_alert_summary AS
-- SELECT
--     severity,
--     COUNT(*)                    AS alert_count,
--     COUNT(DISTINCT vehicle_id)  AS affected_vehicles
-- FROM solace_events
-- WHERE severity IS NOT NULL
--   AND _rw_solace_timestamp > NOW() - INTERVAL '1 HOUR'
-- GROUP BY severity;

-- Alerts with correlated telemetry context (stream-stream join)
-- CREATE MATERIALIZED VIEW IF NOT EXISTS alerts_with_context AS
-- SELECT
--     e.vehicle_id,
--     e.event_type,
--     e.severity,
--     e.description,
--     e._rw_solace_timestamp AS alert_time,
--     t.metric_type,
--     t.value                AS metric_value,
--     t._rw_solace_timestamp AS metric_time
-- FROM solace_events e
-- JOIN solace_events t
--     ON e.vehicle_id = t.vehicle_id
--     AND t.metric_type IS NOT NULL
--     AND t._rw_solace_timestamp BETWEEN e._rw_solace_timestamp - INTERVAL '2 MINUTES'
--                                     AND e._rw_solace_timestamp
-- WHERE e.severity IS NOT NULL;

-- Spatial filter: vehicles in Boston region
-- CREATE MATERIALIZED VIEW IF NOT EXISTS vehicles_in_boston AS
-- SELECT vehicle_id, latitude, longitude, value AS speed,
--        _rw_solace_timestamp AS event_timestamp
-- FROM solace_events
-- WHERE metric_type = 'speed'
--   AND latitude BETWEEN 42.2 AND 42.5
--   AND longitude BETWEEN -71.2 AND -70.9;
