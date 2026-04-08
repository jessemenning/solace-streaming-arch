# CLAUDE.md — Solace Streaming Architecture POC

Ground truth for this project. If code and this file disagree, trust the code and update this file.

---

## What This Is

A working POC combining three technologies to demonstrate a true event-streaming architecture:

| Layer | Technology | Role |
|---|---|---|
| Real-time pub/sub | Solace Platform | Low-latency fan-out, rich topic hierarchies, SEMP v2 management |
| Durable log | Redpanda | Replayable append-only record, Kafka-compatible |
| Streaming analytics | RisingWave | Incremental SQL — every materialized view is a "virtual topic" |
| Bridge | Kafka Connect + Solace Source Connector v3.3.0 | Moves messages from Solace → Redpanda |

Domain: IoT fleet monitoring — 20 simulated vehicles publishing telemetry and alerts.

---

## Key Architecture Decisions

### Single Redpanda topic

The Solace Source Connector routes **all** queue messages to one Redpanda topic (`fleet-events`).
Every message embeds its original Solace topic in the `solace_topic` field.
RisingWave uses `WHERE solace_topic LIKE 'fleet/telemetry/%'` to replace Solace wildcard subscriptions.

This is intentional. One topic simplifies the connector config and lets SQL handle fan-out.

### `fleet_all_raw` unified source

RisingWave has one SOURCE (`fleet_all_raw`) that reads `fleet-events` with all possible fields
from all message types declared (nullable). Three routing MVs filter by `solace_topic`:

```
fleet_all_raw (SOURCE)
  ├── fleet_telemetry_raw  (MV — WHERE solace_topic LIKE 'fleet/telemetry/%')
  ├── fleet_events_raw     (MV — WHERE solace_topic LIKE 'fleet/events/%')
  └── fleet_commands_raw   (MV — WHERE solace_topic LIKE 'fleet/commands/%')
```

Analytics MVs build on the routing MVs. TUMBLE windowing queries `fleet_all_raw` directly
(not a derived MV) to avoid watermark propagation issues.

### FORMAT PLAIN ENCODE JSON

RisingWave's JSON ingestion is lenient — fields absent in a given message arrive as NULL.
This is what allows a single source schema to handle telemetry, alert, and command payloads.

---

## File Map

| Path | Purpose |
|---|---|
| `docker-compose.yml` | All five services: solace, redpanda, redpanda-console, risingwave, kafka-connect |
| `config/solace/setup.sh` | SEMP v2 REST: creates VPN `streaming-poc`, client profile, ACL, user, queue `q/redpanda-bridge`, topic subscription `fleet/>` |
| `config/redpanda/setup.sh` | Creates `fleet-events` topic via `rpk` |
| `config/kafka-connect/Dockerfile` | Extends `cp-kafka-connect`; downloads Solace connector JARs at build time |
| `config/kafka-connect/download-connector.sh` | Downloads connector ZIPs from GitHub Releases to `plugins/` |
| `config/kafka-connect/solace-source.json` | Connector config: reads `q/redpanda-bridge`, writes to `fleet-events` |
| `config/risingwave/init.sql` | DROP + CREATE for all sources and MVs; idempotent — safe to re-run |
| `generator/generator.py` | 20-vehicle fleet simulator; publishes via Solace Platform Python SDK |
| `generator/requirements.txt` | `solace-pubsubplus` (SDK package name) |
| `demo/run_demo.sh` | End-to-end orchestrator: build → health checks → configure → init schema → run generator → demo queries |
| `demo/demo_queries.sh` | 7-query walkthrough illustrating virtual topic pattern |

---

## Topic Hierarchy

```
fleet/telemetry/{vehicle_id}/metrics/{metric_type}
  metric_type: speed | fuel_level | engine_temp | tire_pressure | battery_voltage | location

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
| `fleet_telemetry_raw` | `fleet/telemetry/>` | Routing MV — base for telemetry analytics |
| `fleet_events_raw` | `fleet/events/>` | Routing MV — base for alert analytics |
| `fleet_commands_raw` | `fleet/commands/>` | Routing MV |
| `high_severity_alerts` | `fleet/events/*/alerts/high` | — |
| `medium_severity_alerts` | `fleet/events/*/alerts/medium` | — |
| `low_severity_alerts` | `fleet/events/*/alerts/low` | — |
| `vehicle_speeds` | `fleet/telemetry/*/metrics/speed` | — |
| `vehicle_speed_5min_avg` | *(no equivalent)* | TUMBLE window on `fleet_all_raw` |
| `fleet_alert_summary` | *(no equivalent)* | Rolling 1h count, grouped by severity |
| `vehicle_fuel_levels` | `fleet/telemetry/*/metrics/fuel_level` | — |
| `high_engine_temp_vehicles` | `fleet/telemetry/*/metrics/engine_temp` + filter | value > 220°F |
| `alerts_with_context` | *(no equivalent)* | Stream-stream join: alerts ⋈ telemetry within 2min |
| `vehicles_in_region_boston` | *(no equivalent)* | Spatial predicate on lat/lon |
| `vehicle_last_known_position` | `fleet/telemetry/*/metrics/speed` + latest | All speed rows; consumer applies latest logic |
| `vehicle_event_counts_1h` | *(no equivalent)* | Per-vehicle, per-severity count rolling 1h |

---

## Port Map

| Service | Host port | Notes |
|---|---|---|
| Solace SEMP admin | 8180 | Host 8080 was in use; mapped to 8180 |
| Solace SMF (SDK) | 55555 | Native Solace protocol |
| Solace MQTT | 1883 | — |
| Redpanda Kafka API (internal) | 9092 | Used by Kafka Connect and RisingWave |
| Redpanda Kafka API (external) | 19092 | Host access for `rpk` |
| Redpanda Schema Registry | 8081 | — |
| Redpanda Console UI | 8888 | → maps to container 8080 |
| RisingWave SQL (psql) | 4566 | Postgres wire protocol |
| RisingWave Dashboard | 5691 | — |
| Kafka Connect REST | 8083 | — |

---

## Running the Stack

### Full demo (first run — builds images)
```bash
chmod +x demo/run_demo.sh demo/demo_queries.sh config/solace/setup.sh config/redpanda/setup.sh
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
bash config/solace/setup.sh localhost
bash config/redpanda/setup.sh
# Deploy connector via curl (see run_demo.sh step 5)
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
python3.13 generator/generator.py --host tcp://localhost:55555 --vpn streaming-poc --user streaming-user --password default
```

### Re-initialize RisingWave schema only (idempotent)
```bash
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
```

---

## Credentials

| Service | Username | Password |
|---|---|---|
| Solace Platform admin | admin | admin |
| Solace Platform client | streaming-user | default |
| RisingWave | root | *(none)* |

---

## Python Notes

- Use `python3.13` on this host — `/usr/bin/python3` has no pip
- `run_demo.sh` auto-detects `python3.13` via `$PYTHON` variable
- SDK package name on PyPI: `solace-pubsubplus` (note: this is the package name, not the brand name)
- Import path: `from solace.messaging.errors.pubsubplus_client_error import PubSubPlusClientError`

---

## Kafka Connect Notes

- Plugin JAR directory: `/usr/share/java/solace-connector/` inside the container
- `CONNECT_PLUGIN_PATH` must be set to the **parent** directory (`/usr/share/java`), not the plugin dir itself — Kafka Connect classloader isolation requires this
- Connector name: `solace-redpanda-bridge`
- Connector class: `com.solace.connector.kafka.connect.source.SolaceSourceConnector`
- Source queue: `q/redpanda-bridge` (has wildcard subscription `fleet/>`)
- Destination topic: `fleet-events`

---

## Common Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `fleet_telemetry_raw` returns 0 rows | Wrong topic name in SOURCE | Re-run `init.sql`; source must read `fleet-events` |
| Connector in FAILED state | ClassNotFoundException | `CONNECT_PLUGIN_PATH` must be `/usr/share/java`, not the plugin subdir |
| Generator `ModuleNotFoundError` | Wrong Python binary | Use `python3.13`, not `/usr/bin/python3` |
| SEMP calls fail silently | `curl -sf` swallows errors | Use `curl -s` to see error body |
| Queue has no subscriptions | `setup.sh` error not surfaced | Check SEMP: `GET /msgVpns/streaming-poc/queues/q%2Fredpanda-bridge/subscriptions` |
| `psql: command not found` | Client not installed | `sudo apt-get install -y postgresql-client` |
