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
| Bridge | Solace Kafka Sender (built into broker) | Moves messages from Solace → Redpanda |

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- `psql` — `sudo apt-get install -y postgresql-client`
- Copy `.env.template` to `.env` at project root and fill in credentials (Anthropic API key required for the Fleet Agent UI; Solace Cloud token required for Event Portal integration)

### Full demo (first run)

```bash
chmod +x demo/run_demo.sh demo/demo_queries.sh config/solace/setup.sh config/redpanda/setup.sh
./demo/run_demo.sh
```

The script builds images for the fleet generator and the Fleet Agent UI;
starts all services; configures Solace Platform (including the built-in Kafka Sender bridge);
creates the Redpanda topic; initializes RisingWave; and opens the Fleet Agent UI automatically.
The fleet generator and AI agent UI start automatically as containers — no host Python required.

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

Stops all services, removes orphaned containers, and **deletes volumes** (`solace-data`,
`redpanda-data`) so the next run starts completely fresh. To preserve data instead:

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
| **Fleet Operations AI (agentic demo)** | **http://localhost:8090** | — |
| Solace Platform admin | http://localhost:8180 | admin / admin |
| Redpanda Console | http://localhost:8888 | — |
| RisingWave Dashboard | http://localhost:5691 | — |
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

### Event Portal integration

The Fleet Operations application domain in Solace Event Portal is the catalog of record for all events and schemas. `generate_mvs.py` queries it to regenerate `config/risingwave/init.sql` — each event version in EP becomes one materialized view.

```bash
# One-time: create EP domain, schemas, events, applications
./create_ep_objects.sh

# After adding or changing events in EP
python3 generate_mvs.py
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
```

Requires `SOLACE_CLOUD_TOKEN` in `.env`. See `CLAUDE.md` for full EP workflow.

---

## Project Structure

```
solace-streaming-arch/
├── docker-compose.yml
├── .env.template                     Copy to .env and fill in credentials
├── README.md
├── CLAUDE.md                         Developer ground truth
├── create_ep_objects.sh              One-time EP setup — domain, schemas, events, apps
├── generate_mvs.py                   Regenerates init.sql from EP event catalog
├── config/
│   ├── solace/
│   │   └── setup.sh                  SEMP v2 — VPN, queue, user, Kafka Sender to Redpanda
│   ├── redpanda/
│   │   └── setup.sh                  rpk topic creation
│   └── risingwave/
│       └── init.sql                  Auto-generated by generate_mvs.py — all sources + MVs
├── docs/
│   └── BUSINESS.md                   Business rationale and stakeholder overview
├── generator/
│   ├── generator.py                  20-vehicle fleet telemetry simulator
│   ├── entrypoint.sh                 Waits for streaming-poc VPN before starting
│   ├── Dockerfile
│   └── requirements.txt
└── demo/
    ├── run_demo.sh                   Full end-to-end orchestrator
    ├── stop_demo.sh                  Tear down all services (deletes volumes by default)
    ├── demo_queries.sh               7-query demo walkthrough
    ├── app.py                        FastAPI backend — Claude + RisingWave tools
    ├── index.html                    Fleet Operations AI single-page UI
    ├── Dockerfile
    └── requirements.txt
```

---

## Troubleshooting

**Solace takes too long to start**  
The community broker image takes 60–90 seconds on first boot. `run_demo.sh` waits up to 3 minutes.

**RisingWave sources return no rows**  
1. Confirm Kafka Sender is running: `curl -u admin:admin http://localhost:8180/SEMP/v2/config/msgVpns/streaming-poc/kafkaSenders/redpanda-sender`  
2. Confirm messages in Redpanda: `docker exec redpanda rpk topic consume fleet-events --num 3`  
3. Confirm queue has a subscription: `curl -u admin:admin http://localhost:8180/SEMP/v2/monitor/msgVpns/streaming-poc/queues/q%2Fredpanda-bridge/subscriptions`

**psql: command not found**  
`sudo apt-get install -y postgresql-client`

**Fleet generator not publishing data**  
Check `docker logs -f fleet-generator`. The container polls SEMP until the `streaming-poc` VPN exists — it will wait if `run_demo.sh` hasn't run Step 3 yet.

**Fleet Agent UI not starting**  
Ensure `.env` exists at project root with a valid `ANTHROPIC_API_KEY`. Copy from `.env.template` if missing.
