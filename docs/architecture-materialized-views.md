# RisingWave Materialized Views — Architecture Notes

## How MVs Map to Solace Topic Hierarchies

The core pattern: SQL `WHERE` clauses replace Solace wildcard subscriptions.

RisingWave connects directly to Solace queues via the native Solace source connector (SMF protocol). Two SOURCEs bind to separate queues: `fleet_ingest_telemetry` (telemetry + commands) and `fleet_ingest_events` (alerts). The connector captures the Solace destination topic and sender timestamp as metadata columns (`_rw_solace_destination`, `_rw_solace_timestamp`). Routing MVs alias `_rw_solace_destination` as `solace_topic` — the original Solace topic address is available for SQL filtering without being embedded in the message body. RisingWave MVs replicate Solace wildcard filtering:

| Solace subscription | RisingWave equivalent |
|---|---|
| `fleet/telemetry/>` | `WHERE solace_topic LIKE 'fleet/telemetry/%'` |
| `fleet/events/>` | `WHERE solace_topic LIKE 'fleet/events/%'` |
| `fleet/events/*/alerts/high` | `WHERE solace_topic LIKE 'fleet/events/%/alerts/high'` |
| `fleet/telemetry/*/metrics/speed` | `WHERE solace_topic LIKE 'fleet/telemetry/%/metrics/speed'` |

The `*` → `%` substitution is the direct translation between SMF wildcards and SQL LIKE patterns.

### Four-tier structure

```
Solace Event Portal  (design-time catalog — drives code generation)
  └── generate_mvs.py → config/risingwave/init.sql
        ↓
fleet_ingest_telemetry  (Solace SOURCE — rw-ingest queue; columns: data JSONB + metadata)
  ├── fleet_telemetry_raw  (static routing MV — LIKE 'fleet/telemetry/%')
  └── fleet_commands_raw   (static routing MV — LIKE 'fleet/commands/%')
fleet_ingest_events     (Solace SOURCE — events-ingest queue; columns: data JSONB + metadata)
  └── fleet_events_raw     (static routing MV — LIKE 'fleet/events/%')
        ↓
EP-generated MVs (one per event version — read from routing MVs):
  ├── vehicle_speed           (LIKE 'fleet/telemetry/%/metrics/speed')
  ├── vehicle_fuel_level      (LIKE 'fleet/telemetry/%/metrics/fuel_level')
  ├── vehicle_engine_temp     (LIKE 'fleet/telemetry/%/metrics/engine_temp')
  ├── vehicle_alert_high      (LIKE 'fleet/events/%/alerts/high')
  └── … (one MV per event version in "Fleet Operations" domain)
        ↓
Analytics MVs (static — build on routing MVs):
  ├── vehicle_speeds          (speed messages only)
  ├── high_severity_alerts    (alerts/high)
  ├── vehicle_speed_5min_avg  (TUMBLE window — no Solace equivalent)
  └── alerts_with_context     (stream-stream JOIN — no Solace equivalent)
```

MVs extend beyond topic filtering — windowed aggregations and stream-stream JOINs have no Solace equivalent.

Routing MVs read from their respective Solace SOURCE, extract typed columns from JSONB, and apply broad LIKE patterns. EP-generated MVs read from the routing MVs and use fine-grained LIKE patterns derived from event delivery descriptors. Analytics MVs above them see ordinary typed columns and are unaffected by the JSONB ingest format.

---

## Event Portal as Source of Truth

`generate_mvs.py` queries the Solace Event Portal REST API and regenerates `config/risingwave/init.sql` from the event catalog. This replaces hand-crafted SQL for the per-event-type views.

### How conversion works

Each EP event version carries a `deliveryDescriptor` with `addressLevels`. The script converts those levels directly to a SQL `LIKE` pattern:

| EP `addressLevelType` | SQL LIKE token |
|---|---|
| `literal` (e.g. `"speed"`) | exact string (e.g. `speed`) |
| `variable` (e.g. `"vehicle_id"`) | `%` wildcard |

Example: the `vehicle-speed` event version has address levels `fleet / telemetry / {vehicle_id} / metrics / speed`, which becomes `WHERE solace_topic LIKE 'fleet/telemetry/%/metrics/speed'`.

The script also reads the linked JSON Schema to select only the columns relevant to each event type, keeping each MV lean.

### Adding a new event type

1. Create or update the event in EP (`create_ep_objects.sh` or EP UI)
2. Run `python3 generate_mvs.py`
3. Re-apply: `psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql`

The new MV appears automatically — no SQL editing required.

### Dry run

```bash
python3 generate_mvs.py --dry-run
```

Prints the generated SQL to stdout without writing the file.

---

## Dynamic MV Creation

MVs are created via standard SQL DDL. Since RisingWave speaks the Postgres wire protocol, any client (`psql`, `psycopg2`, JDBC) can issue `CREATE MATERIALIZED VIEW` at runtime:

```python
cur.execute(f"""
    CREATE MATERIALIZED VIEW alerts_{vehicle_id} AS
    SELECT * FROM fleet_events_raw
    WHERE vehicle_id = '{vehicle_id}' AND severity = 'high'
""")
```

For large backfills, use non-blocking creation:
```sql
SET BACKGROUND_DDL = true;
CREATE MATERIALIZED VIEW my_mv AS SELECT ...;
```

### DDL limitations

| Capability | Status |
|---|---|
| Create MV at runtime via SQL | Yes |
| Background (non-blocking) creation | Yes |
| `CREATE OR REPLACE` | No — drop and recreate |
| `ALTER` to change the query | No — only `RENAME` supported |
| Modify MV without data loss window | No — DROP clears the view's data |
| Dependents (MVs built on MVs) | Must recreate the whole chain bottom-up |

---

## Performance Implications

### Actor model is the binding constraint

Every MV is decomposed into fragments → actors (parallel workers). Actors run continuously — they consume CPU, memory, and storage at all times, not just at query time.

```sql
SELECT * FROM rw_worker_actor_count;  -- check current actor distribution
```

Default limits per worker parallelism:
| Threshold | Default |
|---|---|
| Soft limit | 6 actors |
| Hard limit (DDL fails) | 7 actors |

On a single-node dev setup with 4 parallelism, the soft limit is hit at ~24 total actors.

### Cost by MV type

| MV type | Typical actor cost | Example in this project |
|---|---|---|
| Simple filter/projection | ~4 actors | `fleet_telemetry_raw` |
| Aggregation + GROUP BY | 8–12 actors | `fleet_alert_summary` |
| TUMBLE window | 8–12 actors | `vehicle_speed_5min_avg` |
| Stream-stream JOIN | 12–16+ actors | `alerts_with_context` |

### Raising cluster limits

```toml
# meta node config
[meta.developer]
meta_actor_cnt_per_worker_parallelism_soft_limit = 100
meta_actor_cnt_per_worker_parallelism_hard_limit = 400
```

### Backpressure diagnosis

```sql
EXPLAIN CREATE MATERIALIZED VIEW my_mv AS SELECT ...;  -- map operators to fragments
```

Then find the bottleneck in Grafana: **Streaming Actors > Actor Output Blocking Time Ratio**.

Fix options in order of preference:
1. Simplify the query
2. `ALTER MATERIALIZED VIEW m SET PARALLELISM = N`
3. Add compute nodes to the cluster

---

## Dynamic Create/Drop Strategy

### The core trade-off

```
Persistent MV:  continuous actor cost, instant query, full history
Dynamic MV:     zero cost at rest, backfill latency on create, data loss on drop
Direct SELECT:  zero actor cost, full scan cost per query, no incremental state
```

### When dynamic create/drop helps

- **Ad-hoc analysis** — user requests a targeted view for a bounded time window; drop on disconnect
- **Burst/incident response** — spin up a JOIN MV for affected vehicles during an incident, drop when resolved
- **Scheduled batch windows** — create at shift start, drop at shift end; avoids 24/7 actor consumption

### When it doesn't help

- **Cheap routing MVs** — filter-only MVs (~4 actors) don't justify the DDL complexity
- **High-churn use cases** — creating/dropping every few seconds: DDL overhead and backfill cost exceed savings; use direct `SELECT` instead
- **Historical data requirements** — MVs backfill only from creation time; dropped MVs lose all accumulated state

### Recommended split for this project

| Category | Approach |
|---|---|
| Routing MVs (`fleet_telemetry_raw`, etc.) | Persistent — cheap, always needed |
| Always-on analytics (`high_severity_alerts`, `vehicle_speeds`) | Persistent |
| Per-vehicle deep-dive, incident-scoped JOINs | Dynamic — create on demand, drop when done |
| One-off lookups | Direct `SELECT` — no MV needed |
