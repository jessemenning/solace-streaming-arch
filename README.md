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
| Delivery | Solace REST Delivery Point (built-in) | Pushes messages from queue → RisingWave webhook |
| Streaming analytics | RisingWave | Incremental SQL — every view is a "virtual topic" |

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- `psql` — `sudo apt-get install -y postgresql-client`
- Copy `.env.template` to `.env` at project root and fill in credentials (Anthropic API key required for the Fleet Agent UI; Solace Cloud token required for Event Portal integration)

### Full demo (first run)

```bash
chmod +x demo/run_demo.sh demo/demo_queries.sh config/solace/setup.sh
./demo/run_demo.sh
```

The script builds images for the fleet generator and the Fleet Agent UI;
starts all services; configures Solace Platform (including the built-in REST Delivery Point
that bridges the queue to RisingWave's webhook); initializes RisingWave; and opens the
Fleet Agent UI automatically. The fleet generator and AI agent UI start automatically as
containers — no host Python required.

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
| Solace SMF (SDK) | tcp://localhost:55555 | streaming-user / default |
| RisingWave SQL | localhost:4566 | root / *(none)* |

---

## Data Streams

Messages flow through two Solace queues into the same REST Delivery Point. The RDP HTTP POSTs
each payload to RisingWave's webhook endpoint. Every message embeds its original Solace topic
address in the `solace_topic` field. RisingWave uses SQL `WHERE` clauses on that field to
replace Solace wildcard subscriptions.

Two queues prevent alert messages from being blocked behind high-volume telemetry:
- `rw-ingest` — `fleet/telemetry/>` + `fleet/commands/>`
- `events-ingest` — `fleet/events/>`

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
├── config/
│   ├── solace/
│   │   └── setup.sh                  SEMP v2 — VPN, queues rw-ingest + events-ingest, REST Delivery Point
│   └── risingwave/
│       ├── init.sql                  Auto-generated by generate_mvs.py — all tables + MVs
│       └── topic-mv-registry.yaml    Auto-generated — topic pattern → MV mapping for solace+ CLI
├── cli/
│   ├── solace_plus.py                solace+ CLI — unified real-time + historical queries
│   ├── requirements.txt
│   └── README.md                     CLI usage guide
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

**RisingWave views return no rows**  
1. Confirm queue has messages: check SEMP at http://localhost:8180 → `streaming-poc` VPN → Queues → `rw-ingest`  
2. Confirm RDP is active: check `risingwave-rdp` REST Delivery Point in SEMP  
3. Re-run `config/solace/setup.sh` if RDP is missing

**psql: command not found**  
`sudo apt-get install -y postgresql-client`

**Fleet generator not publishing data**  
Check `docker logs -f fleet-generator`. The container polls SEMP until the `streaming-poc` VPN exists — it will wait if `run_demo.sh` hasn't run Step 3 yet.

**Fleet Agent UI not starting**  
Ensure `.env` exists at project root with a valid `ANTHROPIC_API_KEY`. Copy from `.env.template` if missing.
