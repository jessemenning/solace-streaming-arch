# CLAUDE.md — Solace Streaming Architecture POC

Ground truth for this project. If code and this file disagree, trust the code and update this file.

---

## What This Is

A working POC combining two technologies to demonstrate a true event-streaming architecture:

| Layer | Technology | Role |
|---|---|---|
| Real-time pub/sub | Solace Platform | Low-latency fan-out, rich topic hierarchies, SEMP v2 management |
| Streaming analytics | RisingWave | Incremental SQL — every materialized view is a "virtual topic" |

Domain: IoT fleet monitoring — configurable number of simulated vehicles (default 10) publishing combined telemetry and alerts.

No Kafka/Redpanda intermediary. RisingWave connects directly to Solace queues via the
native Solace source connector (SMF protocol). Message envelope metadata (destination topic,
sender timestamp) is captured as first-class metadata columns — no proxy or webhook required.

---

## Key Architecture Decisions

### Native Solace source connector — direct queue binding via SMF

RisingWave connects directly to Solace queues using the native Solace source connector.
No proxy, webhook, or HTTP intermediary is required. The connector binds to Solace queues
via the SMF protocol and delivers messages with proper ACK-on-checkpoint semantics.

Two Solace SOURCEs bind to separate queues to prevent head-of-line blocking:
- `fleet_ingest_telemetry` → `rw-ingest` queue (`fleet/telemetry/>` + `fleet/commands/>`)
- `fleet_ingest_events` → `events-ingest` queue (`fleet/events/>`)

Metadata columns capture the Solace destination topic and sender timestamp directly from
the message envelope:
- `_rw_solace_destination` (VARCHAR) — the Solace topic (e.g. `fleet/telemetry/vehicle_001/metrics`)
- `_rw_solace_timestamp` (TIMESTAMPTZ) — the sender timestamp

Routing MVs read from their respective source, filter by `_rw_solace_destination LIKE`,
and alias the metadata columns for downstream compatibility (`solace_topic`, `recorded_at`,
`occurred_at`, `issued_at`).

### Solace SOURCEs and routing MV tree

```
fleet_ingest_telemetry (Solace SOURCE — rw-ingest queue)
  columns: data JSONB, _rw_solace_destination VARCHAR, _rw_solace_timestamp TIMESTAMPTZ
  ├── fleet_telemetry_raw  (MV — WHERE destination LIKE 'fleet/telemetry/%')
  └── fleet_commands_raw   (MV — WHERE destination LIKE 'fleet/commands/%')

fleet_ingest_events (Solace SOURCE — events-ingest queue)
  columns: data JSONB, _rw_solace_destination VARCHAR, _rw_solace_timestamp TIMESTAMPTZ
  └── fleet_events_raw     (MV — WHERE destination LIKE 'fleet/events/%')
```

Analytics MVs build on the routing MVs. TUMBLE windowing uses `fleet_telemetry_raw` (which
has typed columns extracted from JSONB plus `_rw_solace_timestamp` aliased as `recorded_at`).

### Connector acknowledgment modes

The connector supports two ACK modes:
- `checkpoint` (default): messages are ACKed only after RisingWave checkpoints — exactly-once semantics
- `immediate`: messages are ACKed upon read — at-least-once, lower latency

This project uses `checkpoint` mode for exactly-once delivery guarantees.

### Clean payloads — no transport metadata in the message body

The generator publishes clean IoT payloads with no transport metadata (`solace_topic`,
`recorded_at`, `occurred_at` are **not** embedded in the JSON body). The Solace topic and
sender timestamp travel via the message envelope and are captured by the connector as
metadata columns (`_rw_solace_destination`, `_rw_solace_timestamp`).

This design decouples the generator from RisingWave's routing layer. The generator only
needs to publish to the correct Solace topic — it has no knowledge of how downstream
systems route or timestamp messages.

### Custom RisingWave binary — Solace connector not yet upstream

**Status: implemented but not merged upstream.** The Solace source connector has not been
merged to the main RisingWave repository yet. This project uses a local fork on branch
`feat/solace-source-connector` (`~/risingwave/`). The custom binary must be present for the
stack to work — the stock `risingwavelabs/risingwave:latest` image does **not** include the
Solace connector.

**Binary:**
```
/home/jmenning/risingwave/target/debug/risingwave-stripped
```
Mounted read-only into the container via `docker-compose.yml`:
```yaml
volumes:
  - /home/jmenning/risingwave/target/debug/risingwave-stripped:/risingwave/bin/risingwave
```
Use an absolute path — snap Docker does not expand `~` correctly.

**To rebuild after code changes to `~/risingwave/`:**
```bash
cd ~/risingwave
export PATH="$HOME/.cargo/bin:/tmp/protoc-install/bin:$HOME/.local/bin:$PATH"
export OPENSSL_LIB_DIR=/usr/lib/x86_64-linux-gnu OPENSSL_INCLUDE_DIR=/usr/include
export PROTOC=/tmp/protoc-install/bin/protoc
cargo build -p risingwave_cmd_all --profile dev
strip -o target/debug/risingwave-stripped target/debug/risingwave
docker compose restart risingwave
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
```

**Key code locations in `~/risingwave/`:**

- Connector source: `src/connector/src/source/solace/`
- Metadata column registration: `src/connector/src/parser/additional_columns.rs`
- Metadata extraction: `src/connector/src/source/solace/source/message.rs`
- Proto definitions: `proto/plan_common.proto` (Solace-specific message types)

**Available metadata columns:**

| METADATA FROM key | Default column name | Type | Description |
|---|---|---|---|
| `destination` | `_rw_solace_destination` | VARCHAR | Solace destination topic |
| `timestamp` | `_rw_solace_timestamp` | TIMESTAMPTZ | Sender timestamp |
| `replication_group_message_id` | `_rw_solace_replication_group_message_id` | VARCHAR | Broker-assigned unique ID |
| `correlation_id` | `_rw_solace_correlation_id` | VARCHAR | Correlation identifier |
| `sequence_number` | `_rw_solace_sequence_number` | BIGINT | Publisher sequence number |
| `priority` | `_rw_solace_priority` | INT | Message priority |
| `redelivered` | `_rw_solace_redelivered` | BOOLEAN | Redelivery flag |
| `application_message_id` | `_rw_solace_application_message_id` | VARCHAR | App-set message ID |
| `expiration` | `_rw_solace_expiration` | TIMESTAMPTZ | Message expiration |
| `reply_to` | `_rw_solace_reply_to` | VARCHAR | Reply-to topic |

**GitHub:** [jessemenning/risingwave](https://github.com/jessemenning/risingwave) — branch `feat/solace-source-connector`.

---

## File Map

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Six services: solace, risingwave, fleet-generator, fleet-agent, tryme, ep-setup |
| `config/solace/setup.sh` | SEMP v2 REST: creates VPN, client profile, ACL, user, queues `rw-ingest` + `events-ingest` (RisingWave Solace connector binds to queues directly) |
| `create_ep_objects.sh` | **Clean-start** EP setup: deletes any existing "Fleet Operations" domain then recreates all schemas, events, and applications; exits 2 (not 1) if EP is unreachable so callers can fall back gracefully |
| `config/ep-setup/entrypoint.sh` | Container entrypoint for `ep-setup` service; runs `create_ep_objects.sh` and captures exit 2 → graceful fallback to `--skip-ep`; emits end-of-run WARNING if EP was unavailable |
| `generate_mvs.py` | Queries EP catalog → regenerates `config/risingwave/init.sql` and `config/topic-mv-registry.yaml`; `--skip-ep` writes static-only `init.sql` (no EP token required) |
| `config/risingwave/init.sql` | Auto-generated by `generate_mvs.py` — DROP + CREATE for all sources and MVs; idempotent — safe to re-run |
| `config/topic-mv-registry.yaml` | Auto-generated by `generate_mvs.py` — maps Solace topic patterns to RisingWave MVs; read by `solace+` CLI |
| `cli/solace_plus.py` | `solace+` CLI — unified real-time + historical query tool; routes topic patterns to RisingWave (history) or Solace Platform SMF (live) |
| `cli/requirements.txt` | CLI dependencies: `psycopg2-binary`, `solace-pubsubplus`, `click`, `pyyaml`, `tabulate`, `python-dotenv` |
| `cli/README.md` | Usage guide for `solace+`: commands, flags, install steps, examples |
| `generator/generator.py` | Fleet simulator; publishes clean IoT payloads (no transport metadata) via Solace Platform Python SDK; vehicle count and interval configurable via env |
| `generator/requirements.txt` | `solace-pubsubplus` (SDK package name) |
| `generator/Dockerfile` | Python 3.11-slim image; copies generator.py + entrypoint.sh |
| `generator/entrypoint.sh` | Polls SEMP until `streaming-poc` VPN exists, then starts generator; supports `BURST` env var |
| `demo/run_demo.sh` | End-to-end orchestrator: build → health checks → configure Solace → init schema → wait for data |
| `demo/stop_demo.sh` | Tears down all services and **deletes volumes** (`solace-data`) by default; pass `--keep-volumes` to preserve data |
| `demo/demo_queries.sh` | 7-query walkthrough illustrating virtual topic pattern |
| `demo/app.py` | FastAPI backend: 7 Claude tools backed by RisingWave SQL; SSE streaming `/chat` endpoint |
| `demo/index.html` | Solace-branded single-page UI; chat + Agent Activity panels; live fleet stats bar |
| `demo/requirements.txt` | `fastapi`, `uvicorn`, `anthropic`, `psycopg2-binary`, `python-dotenv` |
| `demo/Dockerfile` | Python 3.11-slim; exposes port 8090 |
| `tryme/server.py` | FastAPI backend for Solace+ Try Me: SSE `/subscribe` streams RisingWave history then live SMF; `/publish` publishes to Solace; `/config` returns topic registry |
| `tryme/index.html` | Solace-branded Try Me UI; subscribes with optional history window; deduplicates history vs live; supports publish |
| `tryme/requirements.txt` | `fastapi`, `uvicorn`, `psycopg2-binary`, `solace-pubsubplus`, `pyyaml`, `python-dotenv` |
| `tryme/Dockerfile` | Python 3.11-slim; exposes port 8091 |
| `.env.template` | Committed placeholder — copy to `.env` at project root and fill in credentials |

---

## Topic Hierarchy

```
fleet/telemetry/{vehicle_id}/metrics
  payload: combined snapshot — speed, fuel_level, engine_temp,
           tire_pressure, battery_voltage, latitude, longitude (one msg/vehicle/tick)

fleet/events/{vehicle_id}/alerts/{severity}
  severity: low | medium | high
  event_type: low_fuel | high_engine_temp | tire_pressure_warning |
              speed_limit_exceeded | maintenance_due | battery_voltage_low

fleet/commands/{vehicle_id}/{command_type}
```

---

## RisingWave Materialized Views

| View | Equivalent Solace subscription | Notes |
|---|---|---|
| `fleet_telemetry_raw` | `fleet/telemetry/>` | Routing MV on `fleet_ingest_telemetry`; aliases `_rw_solace_destination` → `solace_topic`, `_rw_solace_timestamp` → `recorded_at` |
| `fleet_events_raw` | `fleet/events/>` | Routing MV on `fleet_ingest_events`; aliases `_rw_solace_timestamp` → `occurred_at` |
| `fleet_commands_raw` | `fleet/commands/>` | Routing MV on `fleet_ingest_telemetry`; aliases `_rw_solace_timestamp` → `issued_at` |
| `high_severity_alerts` | `fleet/events/*/alerts/high` | — |
| `medium_severity_alerts` | `fleet/events/*/alerts/medium` | — |
| `low_severity_alerts` | `fleet/events/*/alerts/low` | — |
| `vehicle_speeds` | `fleet/telemetry/*/metrics` | `speed AS speed_mph`; no per-metric WHERE needed |
| `vehicle_speed_5min_avg` | *(no equivalent)* | TUMBLE window on `fleet_telemetry_raw` |
| `fleet_alert_summary` | *(no equivalent)* | Rolling 1h count, grouped by severity |
| `vehicle_fuel_levels` | `fleet/telemetry/*/metrics` | `fuel_level AS fuel_pct` |
| `high_engine_temp_vehicles` | `fleet/telemetry/*/metrics` + filter | `WHERE engine_temp > 220` |
| `alerts_with_context` | *(no equivalent)* | Stream-stream join: alerts ⋈ telemetry within 2min; wide metric columns |
| `vehicles_in_region_boston` | *(no equivalent)* | Spatial predicate on lat/lon |
| `vehicle_last_known_position` | `fleet/telemetry/*/metrics` | All combined telemetry rows |
| `vehicle_event_counts_1h` | *(no equivalent)* | Per-vehicle, per-severity count rolling 1h |

---

## Port Map

| Service | Host port | Notes |
|---|---|---|
| Solace SEMP admin | 8180 | Host 8080 was in use; mapped to 8180 |
| Solace SMF (SDK) | 55554 | Host port → container 55555 (Native Solace protocol) |
| Solace MQTT | 1883 | — |
| RisingWave SQL (psql) | 4566 | Postgres wire protocol |
| ~~RisingWave webhook~~ | ~~4560~~ | Removed — native Solace connector replaces webhook ingestion |
| RisingWave Dashboard | 5691 | — |
| Fleet Agent UI | 8090 | Agentic demo — FastAPI + Claude + RisingWave tools |
| Solace+ Try Me | 8091 | Interactive pub/sub UI with history replay from RisingWave |
| ~~solace-proxy~~ | — | Removed — native Solace connector replaces proxy |
| fleet-generator | — | No host port; internal container publishes to Solace on streaming-net |

---

## Running the Stack

### Full demo (first run — builds images)
```bash
chmod +x demo/run_demo.sh demo/demo_queries.sh config/solace/setup.sh
./demo/run_demo.sh
```

### Restart after reboot (skip rebuild)
```bash
./demo/run_demo.sh --skip-build
```

### Burst mode (demo spike: vehicle_005 emits continuous high-severity alerts)
```bash
./demo/run_demo.sh --burst
```

### Manual steps (if running piece by piece)
```bash
docker-compose up -d
bash config/solace/setup.sh localhost   # creates VPN, client profile, ACL, user, queue, RDP
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
# fleet-generator and fleet-agent start automatically as containers
```

### Fleet Agent UI prerequisites
Copy `.env.template` to `.env` at project root and fill in:
```
ANTHROPIC_API_KEY=sk-...
LITELLM_BASE_URL=          # optional — leave blank to call Anthropic directly
SOLACE_CLOUD_TOKEN=        # Solace Cloud API token for Event Portal catalog
```

### Re-initialize RisingWave schema only (idempotent)
```bash
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
```

### Stop the stack (wipes volumes — clean slate)
```bash
./demo/stop_demo.sh
```

### Stop the stack (preserve volumes — resume later)
```bash
./demo/stop_demo.sh --keep-volumes
```

### Event Portal — create / regenerate objects
```bash
# Clean-start: delete existing "Fleet Operations" domain and recreate everything
# Exits 2 with a WARNING if Event Portal is unreachable
./create_ep_objects.sh

# After adding/changing events in EP, regenerate init.sql then re-apply
python3 generate_mvs.py
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql

# Preview generated SQL without writing (dry run)
python3 generate_mvs.py --dry-run
```

### Running solace+ CLI

```bash
# Install
cd cli && pip install -r requirements.txt
chmod +x solace_plus.py && cd ..

# Generate init.sql + registry from static schema only (no EP token required)
python3 generate_mvs.py --skip-ep

# Query RisingWave snapshot (stack must be running)
./cli/solace_plus.py query fleet/telemetry/*/metrics/speed --limit 5
./cli/solace_plus.py query fleet/telemetry/*/metrics/speed --window 5min
./cli/solace_plus.py query fleet/events/*/alerts/high --enrich context
./cli/solace_plus.py query fleet/telemetry/*/metrics/location --region boston
./cli/solace_plus.py query --sql "SELECT COUNT(*) FROM high_severity_alerts"

# Inspect registry
./cli/solace_plus.py topic info "fleet/events/*/alerts/high"
./cli/solace_plus.py topic lag "fleet/telemetry/*/metrics/speed"
./cli/solace_plus.py topic list --backed

# Alert shortcuts
./cli/solace_plus.py alert list --severity high
./cli/solace_plus.py alert history --since 1h --enrich

# Subscribe with history replay then live (requires running stack)
./cli/solace_plus.py subscribe fleet/events/*/alerts/high --since 1h
./cli/solace_plus.py subscribe fleet/telemetry/vehicle_005/metrics/speed --since 30min
```

solace+ env vars (override defaults via `.env` or export):

| Variable | Default | Description |
|---|---|---|
| `RW_HOST` | `localhost` | RisingWave host |
| `RW_PORT` | `4566` | RisingWave port |
| `SOLACE_HOST` | `tcp://localhost:55554` | Solace Platform SMF endpoint |
| `SOLACE_VPN` | `streaming-poc` | Solace message VPN |
| `SOLACE_USER` | `streaming-user` | Solace client username |
| `SOLACE_PASSWORD` | `default` | Solace client password |

---

## Credentials

| Service | Username | Password |
|---|---|---|
| Solace Platform admin | admin | admin |
| Solace Platform client | streaming-user | default |
| RisingWave | root | *(none)* |

---

## Python Notes

- Use `python3.13` on this host for any direct invocations — `/usr/bin/python3` has no pip
- `run_demo.sh` auto-detects `python3.13` via `$PYTHON` variable (used for JSON parsing in shell)
- SDK package name on PyPI: `solace-pubsubplus` (note: this is the package name, not the brand name)
- Import path: `from solace.messaging.errors.pubsubplus_client_error import PubSubPlusClientError`
- The fleet generator and fleet-agent both run in Docker (python:3.11-slim) — no host Python needed

---

## Solace Connector Notes

- Two Solace SOURCEs bind directly to durable queues via SMF protocol (no proxy)
- Two queues (both use `permission: "consume"`):
  - `rw-ingest`: `fleet/telemetry/>` + `fleet/commands/>` — high-volume telemetry
  - `events-ingest`: `fleet/events/>` — alerts; separate queue prevents head-of-line blocking by telemetry
- ACK mode: `checkpoint` — messages ACKed only after RisingWave checkpoint (exactly-once)
- Queue hardening: `maxRedeliveryCount: 3` moves stuck messages to `#DEAD_MSG_QUEUE` after 3 failed deliveries; `maxTtl: 300` (5 min) + `respectTtlEnabled: true` auto-expires old messages
- Metadata from message envelope: `_rw_solace_destination` (topic), `_rw_solace_timestamp` (sender time)

---

## Common Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `fleet_telemetry_raw` returns 0 rows | Solace connector not receiving from queue | Check RisingWave dashboard at :5691 for source status; verify queues exist in SEMP |
| Generator not publishing | VPN not yet configured | `generator/entrypoint.sh` polls SEMP — run `docker logs -f fleet-generator` to see wait status |
| SEMP calls fail silently | `curl -sf` swallows errors | Use `curl -s` to see error body |
| `psql: command not found` | Client not installed | `sudo apt-get install -y postgresql-client` |
| Fleet Agent UI shows "DEMO" tag | RisingWave unreachable or no data | Confirm RisingWave is healthy; UI falls back to mock data automatically |
| Fleet Agent UI `401 Unauthorized` | Missing or wrong `ANTHROPIC_API_KEY` | Check `.env` at project root; ensure it exists (copy from `.env.template`) |
| Queue backlog builds up | RisingWave checkpoint interval too slow or connector not keeping up | Check queue depth via SEMP; consider `immediate` ack mode for lower latency |
| RisingWave loses all sources after restart | `playground` mode has no persistent state | Re-run `psql ... -f config/risingwave/init.sql` after any RisingWave restart |
| Try Me live events show `_raw` with framing bytes | Solace SMF SDK `get_payload_as_bytes()` includes 5-byte protocol header | Fixed: `tryme/server.py` uses `get_payload_as_string()` first |
| `ep-setup` container shows "statically defined events" warning | Event Portal unreachable or `SOLACE_CLOUD_TOKEN` not set | Expected — stack runs normally on static schema; set token and restart `ep-setup` to add EP catalog objects |
| `ep-setup` exits with code 2 | EP connectivity check failed (bad token or network) | Check token validity and network; exit 2 is intentional (graceful fallback, not a fatal error) |
