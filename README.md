# Solace Streaming Architecture — Fleet Monitoring POC

A working proof-of-concept combining the **Solace Platform** event broker and
**RisingWave** (streaming SQL engine) to demonstrate the "virtual topic" pattern:
replace Solace wildcard subscriptions with SQL materialized views.

> **Developer docs:** [CLAUDE.md](CLAUDE.md) — architecture decisions, file map, port map, troubleshooting  
> **Business rationale:** [BUSINESS.md](docs/BUSINESS.md) — why this architecture, comparisons, applicability

---

## Core Concept

**Solace handles routing and fan-out. RisingWave turns Solace's dynamic topic space
into SQL materialized views — replacing "topic design" with "query design."**

| Layer | Technology | Role |
|---|---|---|
| Real-time pub/sub | Solace Platform | Low-latency fan-out, rich topic hierarchies |
| Streaming analytics | RisingWave | Incremental SQL — every view is a "virtual topic"; native Solace source connector reads directly from Solace queues via SMF |

---

## Quick Start

### Prerequisites

- Docker (>= 20.10) and Docker Compose v2
- `psql` — `sudo apt-get install -y postgresql-client` (Debian/Ubuntu) or `brew install libpq` (macOS)
- `curl`
- `python3` (3.10+ for host-side scripts; containers use 3.11)
- Copy `.env.template` to `.env` at project root and fill in credentials:
  - `ANTHROPIC_API_KEY` — **required** for the Fleet Agent UI
  - `SOLACE_CLOUD_TOKEN` — optional, only needed for Event Portal integration

### RisingWave with Solace connector

This project uses a **custom RisingWave image** that includes the Solace source connector
(not yet merged upstream). The image is pre-built and published — no local compilation
needed:

```
ghcr.io/jessemenning/risingwave:solace-connector
```

Source: [jessemenning/risingwave](https://github.com/jessemenning/risingwave) branch `feat/solace-source-connector`.

### Full demo (first run)

```bash
chmod +x demo/run_demo.sh demo/demo_queries.sh config/solace/setup.sh
./demo/run_demo.sh
```

> **`run_demo.sh` is the only supported entry point.** It builds images, starts the
> stack, configures Solace Platform, initializes RisingWave schema, and opens the UI.
> Running `docker-compose up -d` alone will start services but skip VPN and schema setup.

The fleet generator and AI agent UI start automatically as containers — no host Python
required.

### Restart after reboot (skip rebuild)

```bash
./demo/run_demo.sh --skip-build
```

### Burst mode (demo spike)

```bash
./demo/run_demo.sh --burst
```

Sends continuous high-severity alerts from `vehicle_005` — useful for live demos of
`high_severity_alerts` and `vehicle_event_counts_1h`.

### Stop the demo

```bash
./demo/stop_demo.sh
```

Stops all services, removes orphaned containers, and **deletes volumes** (`solace-data`)
so the next run starts completely fresh. To preserve data instead:

```bash
./demo/stop_demo.sh --keep-volumes
```

### Query RisingWave directly

```bash
psql -h localhost -p 4566 -U root -d dev
```

```sql
-- All high-severity alerts (Solace equivalent: fleet/events/*/alerts/high)
SELECT vehicle_id, event_type, description, occurred_at
FROM high_severity_alerts ORDER BY occurred_at DESC LIMIT 20;

-- 5-minute speed averages (no Solace equivalent — windowed aggregation)
SELECT vehicle_id, window_start, window_end,
       ROUND(avg_speed_mph::numeric, 2) AS avg_mph, sample_count
FROM vehicle_speed_5min_avg ORDER BY window_end DESC LIMIT 20;

-- Alerts correlated with telemetry from the 2 minutes before the event
SELECT vehicle_id, event_type, severity, speed, engine_temp, time_before_alert
FROM alerts_with_context ORDER BY alert_time DESC LIMIT 10;

-- Vehicles currently inside the Boston metro bounding box
SELECT vehicle_id, latitude, longitude, speed_mph
FROM vehicles_in_region_boston ORDER BY recorded_at DESC LIMIT 10;
```

---

## Service URLs

| Service | URL | Credentials |
|---|---|---|
| **Fleet Operations AI (agentic demo)** | **http://localhost:8090** | — |
| **Solace+ Try Me (pub/sub + history replay)** | **http://localhost:8091** | — |
| Solace Platform admin | http://localhost:8180 | admin / admin |
| RisingWave Dashboard | http://localhost:5691 | — |
| Solace SMF (SDK) | tcp://localhost:55554 | streaming-user / default |
| RisingWave SQL (psql) | localhost:4566 | root / *(none)* |

---

## Data Streams

RisingWave connects directly to Solace queues via the native Solace source connector
(SMF protocol). Three SOURCEs — one per message schema — bind to separate queues and
receive connector metadata columns from the Solace message envelope:
`_rw_solace_destination` (topic) and `_rw_solace_timestamp` (connector processing time).
Each SOURCE uses typed columns matching its queue's flat JSON schema; no JSONB extraction
is needed. Routing MVs alias the metadata columns for downstream use.

Three queues (and three SOURCEs) — one per schema — prevent head-of-line blocking and
avoid mixed-schema union columns:
- `fleet_ingest_telemetry` SOURCE → `rw-ingest` queue — `fleet/telemetry/>`
- `fleet_ingest_events` SOURCE → `events-ingest` queue — `fleet/events/>`
- `fleet_ingest_commands` SOURCE → `commands-ingest` queue — `fleet/commands/>`

Each ingest queue has a dedicated dead-message queue (`dlq-telemetry`, `dlq-events`,
`dlq-commands`) for messages that fail after 3 redeliveries.

### Topic hierarchy (simulator output)

```
fleet/telemetry/{vehicle_id}/metrics
  payload: combined snapshot — speed, fuel_level, engine_temp,
           tire_pressure, battery_voltage, latitude, longitude (1 msg/vehicle/tick)

fleet/events/{vehicle_id}/alerts/{severity}
  severity: low | medium | high

fleet/commands/{vehicle_id}/{command_type}
```

### Materialized views (virtual topics)

| View | Equivalent Solace subscription |
|---|---|
| `high_severity_alerts` | `fleet/events/*/alerts/high` |
| `medium_severity_alerts` | `fleet/events/*/alerts/medium` |
| `low_severity_alerts` | `fleet/events/*/alerts/low` |
| `vehicle_speeds` | `fleet/telemetry/*/metrics` |
| `vehicle_fuel_levels` | `fleet/telemetry/*/metrics` |
| `vehicle_last_known_position` | `fleet/telemetry/*/metrics` |
| `vehicles_in_region_boston` | *(no equivalent — spatial predicate)* |
| `vehicle_speed_5min_avg` | *(no equivalent — tumbling window aggregation)* |
| `fleet_alert_summary` | *(no equivalent — stateful count across all topics)* |
| `alerts_with_context` | *(no equivalent — cross-stream join)* |
| `vehicle_event_counts_1h` | *(no equivalent — per-vehicle rolling count)* |

### Event Portal integration

The Fleet Operations application domain in Solace Event Portal is the catalog of record for all events and schemas. `generate_mvs.py` queries it to regenerate both `config/risingwave/init.sql` and `config/topic-mv-registry.yaml` — each event version in EP becomes one materialized view, and the registry maps Solace topic patterns to those views for the `solace+` CLI.

```bash
# Clean-start: delete existing domain and recreate everything from scratch
./create_ep_objects.sh

# After adding or changing events in EP
python3 generate_mvs.py
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
```

Requires `SOLACE_CLOUD_TOKEN` in `.env`. See `CLAUDE.md` for full EP workflow.

### solace+ CLI — unified real-time + historical queries

`solace+` uses the same Solace topic patterns you already know to query historical data in RisingWave or subscribe to live events from Solace Platform — or both at once.

```bash
cd cli && pip install -r requirements.txt && chmod +x solace_plus.py

# Historical query
./cli/solace_plus.py query fleet/events/*/alerts/high --since 1h --limit 10

# Live subscription with history replay
./cli/solace_plus.py subscribe fleet/events/*/alerts/high --since 1h

# Inspect registry
./cli/solace_plus.py topic info "fleet/telemetry/*/metrics/speed"
```

See [cli/README.md](cli/README.md) for the full command reference.

---

## Project Structure

```
solace-streaming-arch/
├── docker-compose.yml
├── .env.template                     Copy to .env and fill in credentials
├── README.md
├── CLAUDE.md                         Developer ground truth
├── create_ep_objects.sh              Clean-start EP setup — deletes existing domain, recreates all objects
├── generate_mvs.py                   Regenerates init.sql + topic-mv-registry.yaml from EP catalog
├── risingwave-solace/
│   └── Dockerfile                    Custom RisingWave image with Solace connector (binary not committed)
├── config/
│   ├── solace/
│   │   └── setup.sh                  SEMP v2 — VPN, queues rw-ingest + events-ingest + commands-ingest, DMQs
│   ├── risingwave/
│   │   ├── init.sql.default          Committed fallback — copied to init.sql on first run
│   │   └── (init.sql)               Auto-generated by generate_mvs.py (gitignored)
│   └── ep-setup/
│       ├── Dockerfile
│       └── entrypoint.sh             Runs create_ep_objects.sh + generate_mvs.py + schema init at container start
├── cli/
│   ├── solace_plus.py                solace+ CLI — unified real-time + historical queries
│   ├── requirements.txt
│   └── README.md                     CLI usage guide
├── docs/
│   └── BUSINESS.md                   Business rationale and stakeholder overview
├── generator/
│   ├── generator.py                  Fleet telemetry simulator (default 10 vehicles, configurable via NUM_VEHICLES)
│   ├── entrypoint.sh                 Waits for streaming-poc VPN before starting
│   ├── Dockerfile
│   └── requirements.txt
├── demo/
│   ├── run_demo.sh                   Full end-to-end orchestrator
│   ├── stop_demo.sh                  Tear down all services (deletes volumes by default)
│   ├── demo_queries.sh               7-query demo walkthrough
│   ├── app.py                        FastAPI backend — Claude + RisingWave tools
│   ├── index.html                    Fleet Operations AI single-page UI
│   ├── Dockerfile
│   └── requirements.txt
├── tryme/
│   ├── server.py                     FastAPI backend for Solace+ Try Me: SSE subscribe, publish, topic registry
│   ├── index.html                    Solace+ Try Me UI — pub/sub with history replay
│   ├── Dockerfile
│   └── requirements.txt
└── backfill/
    ├── query_client.py               Event-driven RisingWave client with backfill-aware readiness
    ├── publish_sentinel.py           Publishes sentinel boundary marker to Solace
    ├── monitor_queue.py              SEMP queue depth monitor for backfill progress
    ├── status.py                     Lightweight readiness checker (used by Try Me + Fleet Agent)
    ├── backfill_and_go.sh            End-to-end backfill orchestration script
    ├── requirements.txt
    ├── CONNECTOR_DESIGN.md           Original design spec for connector-side implementation
    └── risingwave/
        └── init.sql                  Reference schema for backfill pattern
```

---

## Historical Backfill

The `backfill/` module implements event-driven historical data replay. When RisingWave needs
to catch up on events that accumulated in a Solace queue while it was offline, this system
coordinates the transition from historical to live data:

1. **Sentinel publisher** marks the boundary between historical and live events using a Solace
   user property (`x-solace-sentinel`), not payload content
2. **Connector** (when backfill-aware) intercepts the sentinel, triggers a barrier flush, and
   publishes a readiness event to Solace
3. **Consumers** subscribe to the readiness event for instant notification; late joiners check a
   status table once on startup

```bash
# Full orchestration
bash backfill/backfill_and_go.sh

# Or step by step:
python3 backfill/publish_sentinel.py          # Mark boundary
psql ... -f backfill/risingwave/init.sql      # Create schema (starts connector)
python3 backfill/monitor_queue.py             # Watch progress (optional)
```

Consumer code:

```python
from backfill.query_client import RisingWaveClient

with RisingWaveClient() as client:
    client.wait_for_ready()  # Blocks until connector signals readiness
    rows = client.query("SELECT * FROM high_severity_alerts LIMIT 10")
```

Both the Fleet Agent UI and Try Me UI check backfill readiness automatically via
`/backfill-status` endpoints and show status indicators during backfill.

For full developer documentation, see `CLAUDE.md`. For the original connector design spec,
see `backfill/CONNECTOR_DESIGN.md`.

---

## For Developers

### Re-initializing RisingWave schema

RisingWave runs in `playground` mode (stateless). **All sources and MVs are lost on
container restart.** After any restart, re-apply the schema:

```bash
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
```

Or simply re-run `./demo/run_demo.sh --skip-build`, which handles this automatically.

### Regenerating init.sql from Event Portal

```bash
# With EP token — generates MVs from EP catalog + static views
SOLACE_CLOUD_TOKEN=your-token python3 generate_mvs.py

# Without EP token — static views only
python3 generate_mvs.py --skip-ep
```

After regenerating, re-apply: `psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql`

### Rebuilding the custom RisingWave image

Only needed if you modify the Solace source connector in `~/risingwave/`:

```bash
# Build the Rust binary (takes 10–30 min)
cd ~/risingwave && cargo build -p risingwave_cmd_all --profile dev
strip -o target/debug/risingwave-stripped target/debug/risingwave

# Package into Docker image
cp target/debug/risingwave-stripped ~/solace-streaming-arch/risingwave-solace/
docker build -t ghcr.io/jessemenning/risingwave:solace-connector risingwave-solace/
docker push ghcr.io/jessemenning/risingwave:solace-connector
```

See `CLAUDE.md` for the full build environment variables.

### Running services individually

```bash
# Start only infrastructure (Solace + RisingWave)
docker-compose up -d solace risingwave

# Configure Solace VPN manually
bash config/solace/setup.sh localhost

# Start fleet generator
docker-compose up -d fleet-generator

# Start the demo UI (needs .env with ANTHROPIC_API_KEY)
docker-compose up -d fleet-agent
```

---

## Troubleshooting

**Solace takes too long to start**  
The community broker image takes 60–90 seconds on first boot. `run_demo.sh` waits up to 3 minutes.

**RisingWave views return no rows**  
1. Check RisingWave dashboard at http://localhost:5691 for source connector status  
2. Confirm queues exist: check SEMP at http://localhost:8180 → `streaming-poc` VPN → Queues → `rw-ingest`  
3. Re-run `config/solace/setup.sh` if queues are missing, then restart RisingWave: `docker compose restart risingwave`

**RisingWave loses all sources after restart**  
`playground` mode is stateless — re-run `psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql`

**Docker Compose version error**  
Requires Docker Compose v2 (`docker compose`, not `docker-compose`). Upgrade Docker or install the compose plugin.

**psql: command not found**  
`sudo apt-get install -y postgresql-client` (Debian/Ubuntu) or `brew install libpq` (macOS)

**Fleet generator not publishing data**  
Check `docker logs -f fleet-generator`. The container polls SEMP until the `streaming-poc` VPN exists — it will wait if `run_demo.sh` hasn't run Step 3 yet. Exits after 3 minutes if Solace is unreachable.

**Fleet Agent UI not starting**  
Ensure `.env` exists at project root with a valid `ANTHROPIC_API_KEY`. Copy from `.env.template` if missing.

**Fleet Agent chat shows "Connection error: network error"**  
The Anthropic API returned an error before the first SSE byte — typically budget exceeded or an invalid key. The chat UI will show a specific error message. Add Anthropic credits at https://console.anthropic.com or re-check `ANTHROPIC_API_KEY` in `.env`.

**Port conflicts**  
Default Solace SEMP port 8080 is often in use; this project maps it to 8180. If other ports conflict, edit `docker-compose.yml` port mappings.
