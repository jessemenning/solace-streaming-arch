# Architecture — Solace Streaming POC

## The Problem This Solves

Traditional event-driven architectures using Solace alone require careful topic
hierarchy design upfront. Every new consumer use case — "give me only high-severity
alerts", "give me speeds averaged over 5 minutes", "give me vehicles inside this
geographic region" — requires a new topic, a new bridge, or a new filter
application running continuously.

This POC demonstrates an alternative: use Solace for what it is best at
(real-time routing, fan-out, guaranteed delivery), and move the "subscription logic" into the
database layer (RisingWave materialized views). RisingWave connects directly to Solace queues
via the native Solace source connector (SMF protocol), consuming messages with full envelope
metadata (topic, sender timestamp) preserved as first-class columns.

---

## Data Flow

```
Solace Event Portal  (cloud catalog — design-time, not in the live data path)
  Domain: Fleet Operations
  Schemas: FleetTelemetryMetric, FleetAlert, FleetCommand (JSON Schema draft-07)
  Events: vehicle-speed, vehicle-fuel-level, vehicle-engine-temp, ...
  Applications: fleet-generator (produces), risingwave-analytics (consumes), fleet-agent
     │
     │  create_ep_objects.sh   → populates EP domain, schemas, events, apps
     │  generate_mvs.py        → reads EP catalog → writes config/risingwave/init.sql
     ▼
config/risingwave/init.sql   (auto-generated — one MV per EP event version)


Fleet vehicles
     │  native Python SDK  (solace-pubsubplus)
     ▼
Solace Platform Event Broker   port 55554 (SMF)
  VPN: streaming-poc
  Queue: rw-ingest      ← subscriptions: fleet/telemetry/>, fleet/commands/>
  Queue: events-ingest  ← subscription: fleet/events/>
     │
     │  native Solace source connector (SMF protocol, checkpoint ACK)
     │  — no proxy, no webhook — direct queue-to-SOURCE binding
     ▼
RisingWave Streaming SQL Engine   port 4566 (Postgres wire protocol)
  SOURCE: fleet_ingest_telemetry  ← rw-ingest queue
    columns: data JSONB
    metadata: _rw_solace_destination VARCHAR, _rw_solace_timestamp VARCHAR
  SOURCE: fleet_ingest_events     ← events-ingest queue
    columns: data JSONB
    metadata: _rw_solace_destination VARCHAR, _rw_solace_timestamp VARCHAR
  Routing MVs (extract typed columns from JSONB, filter by metadata columns):
    fleet_telemetry_raw           → FROM fleet_ingest_telemetry WHERE _rw_solace_destination LIKE 'fleet/telemetry/%'
    fleet_commands_raw            → FROM fleet_ingest_telemetry WHERE _rw_solace_destination LIKE 'fleet/commands/%'
    fleet_events_raw              → FROM fleet_ingest_events   WHERE _rw_solace_destination LIKE 'fleet/events/%'
  EP-generated MVs (one per event version in Fleet Operations domain):
    vehicle_speed                 → WHERE solace_topic LIKE 'fleet/telemetry/%/metrics/speed'
    vehicle_fuel_level            → WHERE solace_topic LIKE 'fleet/telemetry/%/metrics/fuel_level'
    vehicle_alert_high            → WHERE solace_topic LIKE 'fleet/events/%/alerts/high'
    … (one MV per event version; re-run generate_mvs.py to extend)
  Analytics MVs (static — aggregation, join, window):
    high_severity_alerts          → replaces fleet/events/*/alerts/high
    vehicle_speed_5min_avg        → 5-min tumbling window (no Solace equivalent)
    alerts_with_context           → stream-stream join (no Solace equivalent)
    vehicles_in_region_boston     → spatial filter (no Solace equivalent)
    …
```

---

## Event Portal Integration

Solace Event Portal serves as the **event catalog and schema registry** for this architecture. It is a design-time component — not in the live data path — but it drives code generation for the RisingWave layer.

### Workflow

1. **`create_ep_objects.sh`** — run once (idempotent) to populate the "Fleet Operations" application domain in EP:
   - 3 JSON Schema draft-07 schemas: `FleetTelemetryMetric`, `FleetAlert`, `FleetCommand`
   - 10 events with versioned delivery descriptors (topic address levels)
   - 3 applications (`fleet-generator`, `risingwave-analytics`, `fleet-agent`) with produce/consume relationships wired
   - Reads `SOLACE_CLOUD_TOKEN` from `.env`

2. **`generate_mvs.py`** — queries the EP catalog and regenerates two files:
   - `config/risingwave/init.sql` — one `CREATE MATERIALIZED VIEW` per event version; static analytics MVs appended
   - `config/topic-mv-registry.yaml` — maps Solace topic patterns to RisingWave MVs; read by `solace+` CLI
   - Converts EP delivery descriptor `addressLevels` → SQL `LIKE` patterns (`variable` levels → `%`)
   - Uses the linked schema's `properties` to select only the relevant columns for each MV

3. **Re-apply to RisingWave** after regenerating:
   ```bash
   python3 generate_mvs.py
   psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
   ```

### Why this matters

Adding a new event type in EP automatically produces a corresponding RisingWave MV on the next `generate_mvs.py` run. The virtual-topic pattern scales without hand-crafting SQL — the EP catalog *is* the subscription definition.

---

## Design Decisions

### 1. Native Solace source connector — direct queue binding

RisingWave connects directly to Solace queues over SMF using the native Solace source
connector. No proxy, no webhook, no HTTP intermediary. Each SOURCE binds to a Solace
queue and consumes messages with full envelope metadata preserved:

- `_rw_solace_destination` — the Solace destination topic (e.g. `fleet/telemetry/vehicle_001/metrics`)
- `_rw_solace_timestamp` — the sender timestamp

These appear as first-class columns on every SOURCE. This keeps generator payloads clean —
producers publish pure IoT data with no transport metadata embedded. Routing and timestamp
logic lives entirely in the RisingWave SOURCE + MV layer, not in any external delivery
component. Routing MVs alias `_rw_solace_destination AS solace_topic` and cast
`_rw_solace_timestamp` as `recorded_at` / `occurred_at` / `issued_at` so all downstream
analytics MVs require zero changes.

ACK mode is **checkpoint** — RisingWave acknowledges messages to Solace on barrier
commit, providing exactly-once semantics between the broker and the streaming engine.

Earlier iterations used a Python webhook proxy to bridge Solace queues to RisingWave's
webhook endpoint. The native connector eliminates that component entirely, removing an
HTTP hop, simplifying deployment, and enabling back-pressure-aware consumption with
proper Solace ACK semantics.

### 2. Two SOURCEs with JSONB routing MVs

Two RisingWave SOURCEs bind to separate Solace queues:
- `fleet_ingest_telemetry` — `rw-ingest` queue (telemetry + commands, high volume)
- `fleet_ingest_events` — `events-ingest` queue (alerts, lower volume)

Separate queues prevent head-of-line blocking: alert messages are never delayed by a
telemetry backlog. Each SOURCE has a `data JSONB` column plus Solace metadata columns.

The routing layer (three MVs) extracts typed columns from the JSONB body and filters by
`_rw_solace_destination`. Each routing MV reads from its appropriate SOURCE:
- `fleet_telemetry_raw` and `fleet_commands_raw` read from `fleet_ingest_telemetry`
- `fleet_events_raw` reads from `fleet_ingest_events`

All analytics MVs above them see ordinary typed columns and are unaffected by the ingest
format. This isolates the JSONB extraction concern to one layer, keeping analytics MVs
clean and readable.

### 3. Custom RisingWave binary — Solace connector not yet upstream

The Solace source connector has not been merged to the upstream RisingWave repository yet.
This project uses a local RisingWave fork on branch `feat/solace-source-connector`
(`~/risingwave/`). The custom binary is mounted into the container via `docker-compose.yml`.
Once the connector is merged upstream, the volume mount can be removed and the stock
`risingwavelabs/risingwave` image used directly.

### 4. RisingWave in playground mode

For the POC, RisingWave runs in single-node `playground` mode.
This removes the need for a separate storage backend (etcd, MinIO) and makes
the stack simpler to start locally.
For production, RisingWave would run in distributed mode with:
  - Meta node (coordination)
  - Compute nodes (query execution)
  - Compactor (storage management)
  - Object store (S3 or compatible)

---

## Virtual Topics vs. Solace Subscriptions

| Consumer need | Solace approach | RisingWave approach |
|---|---|---|
| All high-severity alerts | Subscribe to `fleet/events/*/alerts/high` | `SELECT * FROM high_severity_alerts` |
| Speed for one vehicle | Subscribe to `fleet/telemetry/vehicle_005/metrics/speed` | `SELECT * FROM vehicle_speeds WHERE vehicle_id='vehicle_005'` |
| 5-minute speed average | External stateful consumer + aggregation app | `SELECT * FROM vehicle_speed_5min_avg` |
| Correlated alerts + telemetry | Two subscriptions + external join app | `SELECT * FROM alerts_with_context` |
| Vehicles in a region | External geospatial service | `SELECT * FROM vehicles_in_region_boston` |
| New dimension added | New topic hierarchy + migration | New `WHERE` clause or new materialized view |

---

## Latency Characteristics

End-to-end latency (Solace publish → RisingWave materialized view visible):

| Segment | Expected latency |
|---|---|
| Solace publish → queue `rw-ingest` | < 5 ms |
| Solace queue → RisingWave SOURCE (native connector, SMF) | < 10 ms |
| RisingWave incremental view update | < 100 ms for simple filters |
| Total typical | 20–150 ms |

For pure real-time consumers that need sub-10 ms delivery, they should
subscribe directly to Solace topics (not via RisingWave).
RisingWave is for analytical/aggregated views where near-real-time (< 1 s)
is sufficient.
