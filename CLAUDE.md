# CLAUDE.md — Solace Streaming Architecture POC

Ground truth for this project. If code and this file disagree, trust the code and update this file.

---

## What This Is

A working POC combining two technologies to demonstrate a true event-streaming architecture:

| Layer | Technology | Role |
|---|---|---|
| Real-time pub/sub | Solace Platform | Low-latency fan-out, rich topic hierarchies, SEMP v2 management, REST Delivery Point |
| Streaming analytics | RisingWave | Incremental SQL — every materialized view is a "virtual topic" |

Domain: IoT fleet monitoring — configurable number of simulated vehicles (default 10) publishing combined telemetry and alerts.

No Kafka/Redpanda intermediary. A Python proxy subscribes to Solace queues, extracts message
envelope metadata (topic, sender timestamp), and HTTP POSTs the body to RisingWave's webhook
endpoint with the metadata injected as HTTP headers. RisingWave's `INCLUDE header` feature
(from a local fork) captures those headers as first-class table columns.

---

## Key Architecture Decisions

### Python proxy → RisingWave webhook (with INCLUDE header)

A Python proxy (`proxy/proxy.py`) replaces the Solace REST Delivery Point (RDP). It
subscribes to two durable queues via the Solace Python SDK and HTTP POSTs each message
body to the RisingWave webhook endpoint on port 4560. Unlike the RDP, the proxy injects
message envelope metadata as HTTP headers:
- `x-message-topic` — the Solace destination topic (e.g. `fleet/telemetry/vehicle_001/metrics`)
- `x-message-timestamp` — the sender timestamp as ISO 8601 UTC

Two queues prevent head-of-line blocking:
- `rw-ingest`: subscribed to `fleet/telemetry/>` and `fleet/commands/>` (high volume)
- `events-ingest`: subscribed to `fleet/events/>` (alerts must not be blocked by telemetry)

RisingWave's `INCLUDE header` feature captures these headers as first-class VARCHAR columns
(`_topic`, `_timestamp`). Routing MVs use `WHERE _topic LIKE 'fleet/telemetry/%'` — the
topic comes from the message envelope, not the payload body.

### `fleet_all_raw` webhook TABLE with INCLUDE header columns

RisingWave has one webhook TABLE (`fleet_all_raw`) with a `data JSONB` column plus two
`INCLUDE header` columns (`_topic VARCHAR`, `_timestamp VARCHAR`). Three routing MVs
filter by the `_topic` column (aliased as `solace_topic` for downstream compatibility):

```
fleet_all_raw (webhook TABLE — proxy HTTP POST → port 4560)
  columns: data JSONB, _topic VARCHAR, _timestamp VARCHAR
  ├── fleet_telemetry_raw  (MV — WHERE _topic LIKE 'fleet/telemetry/%')
  ├── fleet_events_raw     (MV — WHERE _topic LIKE 'fleet/events/%')
  └── fleet_commands_raw   (MV — WHERE _topic LIKE 'fleet/commands/%')
```

Analytics MVs build on the routing MVs. TUMBLE windowing uses `fleet_telemetry_raw` (which
has typed columns extracted from JSONB plus `_timestamp` cast as `recorded_at`).

### Webhook source

`fleet_all_raw` uses `connector = 'webhook'` (TABLE, not SOURCE). The webhook endpoint URL
is `http://risingwave:4560/webhook/dev/public/fleet_all_raw`. No auth is required.

### Clean payloads — no transport metadata in the message body

The generator publishes clean IoT payloads with no transport metadata (`solace_topic`,
`recorded_at`, `occurred_at` are **not** embedded in the JSON body). The Solace topic and
sender timestamp travel via the message envelope and are injected as HTTP headers by the
proxy. RisingWave captures them via `INCLUDE header` as `_topic` and `_timestamp` columns.

This design decouples the generator from RisingWave's routing layer. The generator only
needs to publish to the correct Solace topic — it has no knowledge of how downstream
systems route or timestamp messages.

### Custom RisingWave binary — `INCLUDE header` for webhook sources

**Status: implemented and running.** This project uses a local RisingWave fork on branch
`feat/webhook-include-header` (`~/risingwave/`) that adds `INCLUDE header` support to webhook
source tables. The upstream `risingwavelabs/risingwave:latest` image does **not** have this
feature — the custom binary must be present for the stack to work correctly.

**What it enables:**

```sql
CREATE TABLE fleet_all_raw (
    data JSONB
)
INCLUDE header 'x-message-topic'     AS _topic     VARCHAR
INCLUDE header 'x-message-timestamp' AS _timestamp VARCHAR
WITH (connector = 'webhook');
```

HTTP request headers become first-class VARCHAR table columns. The proxy injects
`x-message-topic` (Solace destination topic) and `x-message-timestamp` (sender ISO 8601 UTC)
on every POST. RisingWave materialises them as `_topic` and `_timestamp` — no embedding in
the JSON body required.

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

**How it works (key code locations in `~/risingwave/`):**

- `HeaderColumnInfo` struct in `src/frontend/src/webhook/mod.rs` carries the header name
  for each INCLUDE column. `acquire_table_info()` scans the table catalog for
  `AdditionalColumnType::HeaderInner` columns and builds `column_indices` dynamically.
- `generate_data_chunk()` produces a multi-column DataChunk: JSONB body first, then one
  `Utf8Array` column per INCLUDE header. Absent or non-UTF8 headers → NULL.
- In batched (JSONL) mode, header values are replicated across all rows (headers are
  per-request, not per-line).
- `WEBHOOK_CONNECTOR` was added to `COMPATIBLE_ADDITIONAL_COLUMNS` in
  `src/connector/src/parser/additional_columns.rs`.
- `check_create_table_with_source` INCLUDE guard relaxed via `is_webhook_connector()` on
  `WithOptions`; webhook INCLUDE columns processed through the existing
  `handle_addition_columns()` pipeline.

**Edge cases:** absent headers → NULL, non-UTF8 header values → NULL (silent), batched mode
replicates per-request headers across all newline-delimited rows, only VARCHAR/BYTEA column
types accepted (others fail at DDL time).

**GitHub:** [jessemenning/risingwave-webhook-headers](https://github.com/jessemenning/risingwave-webhook-headers) — private fork, branch `feat/webhook-include-header`.

---

### Future connector path — native Solace connector for RisingWave

A native Rust connector subscribing directly to Solace via the Solace C SDK (solclient)
through FFI bindings would eliminate the proxy and enable proper Solace ACK on commit:

```sql
CREATE TABLE fleet_all (
    data        JSONB,
    _topic      VARCHAR,
    _timestamp  TIMESTAMPTZ
) WITH (connector = 'solace', solace.host = '...', solace.queue = 'rw-ingest');
```

Effort: 5–9 weeks. No such connector exists in the RisingWave ecosystem — would be a
genuine open-source contribution.

---

## File Map

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Seven services: solace, risingwave, fleet-generator, solace-proxy, fleet-agent, tryme, ep-setup |
| `config/solace/setup.sh` | SEMP v2 REST: creates VPN, client profile, ACL, user, queues `rw-ingest` + `events-ingest` (no RDP — proxy binds to queues directly) |
| `create_ep_objects.sh` | **Clean-start** EP setup: deletes any existing "Fleet Operations" domain then recreates all schemas, events, and applications; exits 2 (not 1) if EP is unreachable so callers can fall back gracefully |
| `config/ep-setup/entrypoint.sh` | Container entrypoint for `ep-setup` service; runs `create_ep_objects.sh` and captures exit 2 → graceful fallback to `--skip-ep`; emits end-of-run WARNING if EP was unavailable |
| `generate_mvs.py` | Queries EP catalog → regenerates `config/risingwave/init.sql` and `config/topic-mv-registry.yaml`; `--skip-ep` writes static-only `init.sql` (no EP token required) |
| `config/risingwave/init.sql` | Auto-generated by `generate_mvs.py` — DROP + CREATE for all tables and MVs; idempotent — safe to re-run |
| `config/topic-mv-registry.yaml` | Auto-generated by `generate_mvs.py` — maps Solace topic patterns to RisingWave MVs; read by `solace+` CLI |
| `cli/solace_plus.py` | `solace+` CLI — unified real-time + historical query tool; routes topic patterns to RisingWave (history) or Solace Platform SMF (live) |
| `cli/requirements.txt` | CLI dependencies: `psycopg2-binary`, `solace-pubsubplus`, `click`, `pyyaml`, `tabulate`, `python-dotenv` |
| `cli/README.md` | Usage guide for `solace+`: commands, flags, install steps, examples |
| `proxy/proxy.py` | Solace-to-RisingWave webhook proxy; binds to queues, extracts topic + timestamp from message envelope, POSTs body with metadata headers; configurable workers via `PROXY_WORKERS_PER_QUEUE` |
| `proxy/requirements.txt` | `solace-pubsubplus`, `requests` |
| `proxy/Dockerfile` | Python 3.11-slim image; copies proxy.py + entrypoint.sh |
| `proxy/entrypoint.sh` | Polls SEMP until VPN + queues exist, then starts proxy |
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
| `fleet_telemetry_raw` | `fleet/telemetry/>` | Routing MV — `WHERE _topic LIKE`; aliases `_topic` as `solace_topic`, `_timestamp` as `recorded_at` |
| `fleet_events_raw` | `fleet/events/>` | Routing MV — aliases `_timestamp` as `occurred_at` |
| `fleet_commands_raw` | `fleet/commands/>` | Routing MV — aliases `_timestamp` as `issued_at` |
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
| RisingWave webhook | 4560 | HTTP POST from Solace RDP → `fleet_all_raw` table |
| RisingWave Dashboard | 5691 | — |
| Fleet Agent UI | 8090 | Agentic demo — FastAPI + Claude + RisingWave tools |
| Solace+ Try Me | 8091 | Interactive pub/sub UI with history replay from RisingWave |
| solace-proxy | — | No host port; internal container bridges Solace queues → RisingWave webhook |
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

## Proxy Notes

- The Python proxy (`proxy/proxy.py`) replaces the Solace REST Delivery Point (RDP)
- Two queues (both use `permission: "consume"`):
  - `rw-ingest`: `fleet/telemetry/>` + `fleet/commands/>` — high-volume telemetry
  - `events-ingest`: `fleet/events/>` — alerts; separate queue prevents head-of-line blocking by telemetry
- Proxy binds to both queues via `PersistentMessageReceiver` (Solace Python SDK)
- Scaling: `PROXY_WORKERS_PER_QUEUE` (default 4) worker threads per queue; for horizontal scaling, set queues to non-exclusive and run multiple proxy containers
- DLQ handling: on HTTP failure the proxy NAKs with `FAILED` disposition; after `maxRedeliveryCount` (3) failures Solace moves the message to `#DEAD_MSG_QUEUE`
- Webhook endpoint: `http://risingwave:4560/webhook/dev/public/fleet_all_raw`
- No auth on RisingWave webhook endpoint (optional — not configured)
- Queue hardening: `maxRedeliveryCount: 3` moves stuck messages to `#DEAD_MSG_QUEUE` after 3 failed deliveries; `maxTtl: 300` (5 min) + `respectTtlEnabled: true` auto-expires old messages — prevents backlog accumulation if RisingWave is temporarily unavailable
- Headers injected per request: `x-message-topic` (Solace destination topic), `x-message-timestamp` (sender timestamp as ISO 8601 UTC)

---

## Common Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `fleet_telemetry_raw` returns 0 rows | Proxy not delivering to RisingWave | Check `docker logs solace-proxy`; verify queues exist in SEMP |
| Proxy not connecting | VPN or queues not yet configured | `proxy/entrypoint.sh` polls SEMP — check `docker logs -f solace-proxy` for wait status |
| Proxy delivering but `_topic` is NULL | Proxy not sending `x-message-topic` header | Check proxy version; verify `Content-Type` and custom headers in proxy logs |
| Generator not publishing | VPN not yet configured | `generator/entrypoint.sh` polls SEMP — run `docker logs -f fleet-generator` to see wait status |
| SEMP calls fail silently | `curl -sf` swallows errors | Use `curl -s` to see error body |
| `psql: command not found` | Client not installed | `sudo apt-get install -y postgresql-client` |
| Fleet Agent UI shows "DEMO" tag | RisingWave unreachable or no data | Confirm RisingWave is healthy; UI falls back to mock data automatically |
| Fleet Agent UI `401 Unauthorized` | Missing or wrong `ANTHROPIC_API_KEY` | Check `.env` at project root; ensure it exists (copy from `.env.template`) |
| Queue backlog builds up | Proxy workers not keeping up with generator rate | Increase `PROXY_WORKERS_PER_QUEUE` or run additional proxy containers |
| Proxy errors; queue messages accumulating | RisingWave returning HTTP errors; after `maxRedeliveryCount` (3) failures messages go to `#DEAD_MSG_QUEUE` | Check `docker logs solace-proxy` for HTTP error details; drain queue: `curl -u admin:admin -X PUT "http://localhost:8180/SEMP/v2/action/msgVpns/streaming-poc/queues/rw-ingest/deleteMsgs" -H 'Content-Type: application/json' -d '{}'` |
| RisingWave loses all tables after restart | `playground` mode has no persistent state | Re-run `psql ... -f config/risingwave/init.sql` after any RisingWave restart |
| Try Me live events show `_raw` with framing bytes | Solace SMF SDK `get_payload_as_bytes()` includes 5-byte protocol header | Fixed: `tryme/server.py` uses `get_payload_as_string()` first |
| `ep-setup` container shows "statically defined events" warning | Event Portal unreachable or `SOLACE_CLOUD_TOKEN` not set | Expected — stack runs normally on static schema; set token and restart `ep-setup` to add EP catalog objects |
| `ep-setup` exits with code 2 | EP connectivity check failed (bad token or network) | Check token validity and network; exit 2 is intentional (graceful fallback, not a fatal error) |
