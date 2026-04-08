# Solace Streaming Architecture — Fleet Monitoring POC

A working proof-of-concept combining the **Solace Platform** event broker,
**Redpanda** (durable log), and **RisingWave** (streaming SQL engine) to
demonstrate the "virtual topic" pattern: replace Solace wildcard subscriptions
with SQL materialized views.

> **Developer docs:** [CLAUDE.md](CLAUDE.md) — architecture decisions, file map, port map, troubleshooting  
> **Business rationale:** [BUSINESS.md](BUSINESS.md) — why this architecture, comparisons, applicability

---

## Core Concept

**Solace handles routing and fan-out. Redpanda is the durable log. RisingWave
turns Solace's dynamic topic space into SQL materialized views — replacing
"topic design" with "query design."**

| Layer | Technology | Role |
|---|---|---|
| Real-time pub/sub | Solace Platform | Low-latency fan-out, rich topic hierarchies |
| Durable log | Redpanda | Replayable append-only record |
| Streaming analytics | RisingWave | Incremental SQL — every view is a "virtual topic" |
| Bridge | Kafka Connect + Solace Source Connector | Moves messages from Solace → Redpanda |

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- `psql` — `sudo apt-get install -y postgresql-client`
- Python 3.11+ with pip

### Full demo (first run)

```bash
chmod +x demo/run_demo.sh demo/demo_queries.sh config/solace/setup.sh config/redpanda/setup.sh
./demo/run_demo.sh
```

The script builds the Kafka Connect image, starts all services, configures Solace Platform,
creates the Redpanda topic, deploys the connector, initializes RisingWave, starts the
20-vehicle fleet generator, and runs a curated 7-query demo walkthrough.

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
SELECT vehicle_id, event_type, severity, metric_type, metric_value, time_before_alert
FROM alerts_with_context ORDER BY alert_time DESC LIMIT 10;

-- Vehicles currently inside the Boston metro bounding box
SELECT vehicle_id, latitude, longitude, speed_mph
FROM vehicles_in_region_boston ORDER BY recorded_at DESC LIMIT 10;
```

---

## Service URLs

| Service | URL | Credentials |
|---|---|---|
| Solace Platform admin | http://localhost:8180 | admin / admin |
| Redpanda Console | http://localhost:8888 | — |
| RisingWave Dashboard | http://localhost:5691 | — |
| Kafka Connect REST | http://localhost:8083 | — |
| Solace SMF (SDK) | tcp://localhost:55555 | streaming-user / default |
| RisingWave SQL | localhost:4566 | root / *(none)* |

---

## Data Streams

All messages flow through a single Redpanda topic (`fleet-events`). Every message
embeds its original Solace topic address in the `solace_topic` field. RisingWave
uses SQL `WHERE` clauses on that field to replace Solace wildcard subscriptions.

### Topic hierarchy (simulator output)

```
fleet/telemetry/{vehicle_id}/metrics/{metric_type}
  metric_type: speed | fuel_level | engine_temp | tire_pressure | battery_voltage | location

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
| `vehicle_speeds` | `fleet/telemetry/*/metrics/speed` |
| `vehicle_fuel_levels` | `fleet/telemetry/*/metrics/fuel_level` |
| `vehicle_last_known_position` | `fleet/telemetry/*/metrics/speed` |
| `vehicles_in_region_boston` | *(no equivalent — spatial predicate)* |
| `vehicle_speed_5min_avg` | *(no equivalent — tumbling window aggregation)* |
| `fleet_alert_summary` | *(no equivalent — stateful count across all topics)* |
| `alerts_with_context` | *(no equivalent — cross-stream join)* |
| `vehicle_event_counts_1h` | *(no equivalent — per-vehicle rolling count)* |

---

## Project Structure

```
solace-streaming-arch/
├── docker-compose.yml
├── README.md
├── CLAUDE.md                         Developer ground truth
├── BUSINESS.md                       Business rationale
├── config/
│   ├── solace/
│   │   └── setup.sh                  SEMP v2 — VPN, queue, user, subscriptions
│   ├── redpanda/
│   │   └── setup.sh                  rpk topic creation
│   ├── kafka-connect/
│   │   ├── Dockerfile                Extends cp-kafka-connect, installs Solace connector
│   │   ├── download-connector.sh     Downloads connector JARs from GitHub Releases
│   │   └── solace-source.json        Connector config
│   └── risingwave/
│       └── init.sql                  All sources + materialized views (idempotent)
├── generator/
│   ├── generator.py                  20-vehicle fleet telemetry simulator
│   └── requirements.txt
└── demo/
    ├── run_demo.sh                   Full end-to-end orchestrator
    └── demo_queries.sh               7-query demo walkthrough
```

---

## Troubleshooting

**Solace takes too long to start**  
The community broker image takes 60–90 seconds on first boot. `run_demo.sh` waits up to 3 minutes.

**Connector stays in FAILED state**  
Check logs: `docker-compose logs kafka-connect`. Common cause: wrong `CONNECT_PLUGIN_PATH`.
It must be `/usr/share/java` (the parent directory), not the plugin subdirectory.

**RisingWave sources return no rows**  
1. Confirm connector is RUNNING: `curl http://localhost:8083/connectors/solace-redpanda-bridge/status`  
2. Confirm messages in Redpanda: `docker exec redpanda rpk topic consume fleet-events --num 3`  
3. Confirm queue has a subscription: `curl -u admin:admin http://localhost:8180/SEMP/v2/monitor/msgVpns/streaming-poc/queues/q%2Fredpanda-bridge/subscriptions`

**psql: command not found**  
`sudo apt-get install -y postgresql-client`

**Generator fails with ModuleNotFoundError**  
Use `python3.13` (or whichever Python has pip). The system `/usr/bin/python3` may have no packages.
