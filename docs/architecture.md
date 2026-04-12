# Architecture — Solace Streaming POC

## The Problem This Solves

Traditional event-driven architectures using Solace alone require careful topic
hierarchy design upfront. Every new consumer use case — "give me only high-severity
alerts", "give me speeds averaged over 5 minutes", "give me vehicles inside this
geographic region" — requires a new topic, a new bridge, or a new filter
application running continuously.

This POC demonstrates an alternative: use Solace for what it is best at
(real-time routing, fan-out, guaranteed delivery), and move the "subscription logic" into the
database layer (RisingWave materialized views). A Python proxy subscribes to Solace's durable
queues and delivers messages to RisingWave's webhook endpoint, injecting message envelope
metadata (topic, timestamp) as HTTP headers.

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
     │  Python proxy (solace-proxy container)
     │  — binds to queues via PersistentMessageReceiver
     │  — HTTP POST to risingwave:4560/webhook/dev/public/fleet_all_raw
     │    headers: x-message-topic, x-message-timestamp
     ▼
RisingWave Streaming SQL Engine   port 4566 (Postgres wire protocol)
  Webhook TABLE: fleet_all_raw
    columns: data JSONB
    INCLUDE header 'x-message-topic'     VARCHAR AS _topic
    INCLUDE header 'x-message-timestamp' VARCHAR AS _timestamp
  Routing MVs (extract typed columns from JSONB, filter by _topic header):
    fleet_telemetry_raw           → WHERE _topic LIKE 'fleet/telemetry/%'
    fleet_events_raw              → WHERE _topic LIKE 'fleet/events/%'
    fleet_commands_raw            → WHERE _topic LIKE 'fleet/commands/%'
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

### 1. Python proxy instead of Solace REST Delivery Point (RDP)

Solace's built-in REST Delivery Point (RDP) can HTTP-POST message bodies to a webhook
endpoint, but it forwards **only the raw body** — topic address and sender timestamp are
stripped at the HTTP boundary. There is no way to configure the RDP to inject dynamic
per-message headers (the topic changes on every message; the RDP only supports static
header values).

The Python proxy solves this by subscribing to Solace queues via the SDK and constructing
the HTTP request itself. It injects `x-message-topic` (the Solace destination topic from
`msg.get_destination_name()`) and `x-message-timestamp` (the sender timestamp from
`msg.get_sender_timestamp()`, converted to ISO 8601 UTC) as headers on every POST.

RisingWave captures them via `INCLUDE header` as first-class VARCHAR columns (`_topic`,
`_timestamp`) on the `fleet_all_raw` table. This keeps generator payloads clean —
producers publish pure IoT data with no transport metadata embedded. Routing and timestamp
logic lives in the delivery layer (proxy + RisingWave), not the producer. Routing MVs
alias `_topic AS solace_topic` and `_timestamp::TIMESTAMPTZ AS recorded_at` so all
downstream analytics MVs require zero changes.

### 2. Single webhook TABLE with JSONB routing MVs

RisingWave's webhook connector accepts a `data JSONB` column plus `INCLUDE header` columns.
The routing layer (three MVs) extracts typed columns from the JSONB body and filters by
`_topic`. All analytics MVs above them see ordinary typed columns and are unaffected by
the ingest format.

This isolates the JSONB extraction concern to one layer, keeping analytics MVs clean and readable.

### 3. RisingWave in playground mode

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
| Proxy receive → HTTP POST → RisingWave webhook | 5–50 ms (network + HTTP round-trip) |
| RisingWave incremental view update | < 100 ms for simple filters |
| Total typical | 50–200 ms |

For pure real-time consumers that need sub-10 ms delivery, they should
subscribe directly to Solace topics (not via RisingWave).
RisingWave is for analytical/aggregated views where near-real-time (< 1 s)
is sufficient.
