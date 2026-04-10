# solace+ — Unified Real-Time + Historical CLI

`solace+` extends Solace topic addressing with a time dimension. Use the same topic patterns you already know to query historical data in RisingWave or subscribe to live events from Solace Platform — or both at once.

---

## How It Works

Every Solace topic pattern maps to a RisingWave materialized view via a registry (`config/topic-mv-registry.yaml`). The CLI routes transparently:

- **`query`** — queries RisingWave only (snapshot or windowed analytics)
- **`subscribe`** — live SMF subscription; add `--since` to replay history first, then continue live
- **`topic`** — inspect registry mappings and measure lag
- **`alert`** — convenience wrappers for `fleet/events/*` patterns

---

## Install

```bash
cd cli
pip install -r requirements.txt
chmod +x solace_plus.py

# Generate the topic registry (no EP token required)
cd .. && python3 generate_mvs.py --skip-ep
```

No package install needed. Run directly as `./cli/solace_plus.py` from the project root, or add `cli/` to your `PATH`.

The registry (`config/topic-mv-registry.yaml`) must exist before any command runs. `--skip-ep` generates it from the static mappings without needing a Solace Cloud token. To include Event Portal–generated MVs, set `SOLACE_CLOUD_TOKEN` in `.env` and run `python3 generate_mvs.py` without the flag.

---

## Environment Variables

Set in `.env` at the project root (copied from `.env.template`), or export directly:

| Variable | Default | Description |
|----------|---------|-------------|
| `RW_HOST` | `localhost` | RisingWave host |
| `RW_PORT` | `4566` | RisingWave port (Postgres wire protocol) |
| `SOLACE_HOST` | `tcp://localhost:55554` | Solace Platform SMF endpoint |
| `SOLACE_VPN` | `streaming-poc` | Solace message VPN |
| `SOLACE_USER` | `streaming-user` | Solace client username |
| `SOLACE_PASSWORD` | `default` | Solace client password |

---

## Commands

### `query` — RisingWave snapshot or analytics

```
solace+ query <pattern> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--snapshot` | Current state (default) |
| `--window 5min` | Use windowed aggregation MV (e.g. `vehicle_speed_5min_avg`) |
| `--enrich context` | Use enriched join MV (e.g. `alerts_with_context`) |
| `--region boston` | Use spatial filter MV (e.g. `vehicles_in_region_boston`) |
| `--sql "..."` | Raw SQL — executes directly on RisingWave |
| `--limit N` | Row limit (default 20) |
| `--since <duration>` | Filter by time: `1h`, `30min`, `2d` |
| `--format table\|json\|csv` | Output format (default `table`) |

**Examples:**

```bash
# Latest speed readings for all vehicles
./cli/solace_plus.py query fleet/telemetry/*/metrics/speed --limit 10

# 5-minute rolling average
./cli/solace_plus.py query fleet/telemetry/*/metrics/speed --window 5min

# High alerts with correlated telemetry context
./cli/solace_plus.py query fleet/events/*/alerts/high --enrich context

# Vehicles currently in Boston metro bounding box
./cli/solace_plus.py query fleet/telemetry/*/metrics/location --region boston

# Last hour of fuel telemetry as JSON
./cli/solace_plus.py query fleet/telemetry/*/metrics/fuel_level --since 1h --format json

# Raw SQL
./cli/solace_plus.py query --sql "SELECT COUNT(*) FROM high_severity_alerts"
```

---

### `subscribe` — live + optional history replay

```
solace+ subscribe <pattern> [--since <duration>] [--format table|json|csv]
```

Without `--since`: pure live SMF subscription. Each message is emitted as a JSON event as it arrives.

With `--since`:
1. SMF subscription starts immediately (buffering live messages)
2. RisingWave history is queried and emitted oldest-first (`source: "history"`)
3. Buffered live messages are drained oldest-first, deduplicated against history (`source: "live"`)
4. Continues reading live messages indefinitely

Each emitted event is a JSON object:
```json
{
  "topic": "fleet/events/vehicle_005/alerts/high",
  "source": "history",
  "event_time": "2026-04-09T14:23:01.000000+00:00",
  "payload": {
    "vehicle_id": "vehicle_005",
    "event_type": "high_engine_temp",
    "severity": "high",
    "description": "Engine temp elevated: 231.4°F"
  }
}
```

**Examples:**

```bash
# Live subscription only
./cli/solace_plus.py subscribe fleet/events/*/alerts/high

# Replay last hour of alerts, then continue live
./cli/solace_plus.py subscribe fleet/events/*/alerts/high --since 1h

# Specific vehicle speed replay + live
./cli/solace_plus.py subscribe fleet/telemetry/vehicle_005/metrics/speed --since 30min
```

---

### `topic` — inspect registry and lag

```
solace+ topic info <pattern>     # Show registry entry: MV, time_col, enrichments, spatial
solace+ topic lag <pattern>      # Time since most recent record in the MV
solace+ topic list [--backed]    # List all patterns; --backed = only those with MV mapping
```

**Examples:**

```bash
./cli/solace_plus.py topic info "fleet/events/*/alerts/high"
./cli/solace_plus.py topic lag "fleet/telemetry/*/metrics/speed"
./cli/solace_plus.py topic list
./cli/solace_plus.py topic list --backed
```

---

### `alert` — convenience wrappers

```
solace+ alert list     [--severity high|medium|low] [--limit N] [--format ...]
solace+ alert history  [--since 4h] [--severity ...] [--enrich] [--format ...]
solace+ alert watch    [--severity high|medium|low]
```

`alert watch` subscribes live to `fleet/events/>` and filters severity client-side.

**Examples:**

```bash
./cli/solace_plus.py alert list --severity high --limit 5
./cli/solace_plus.py alert history --since 2h --enrich
./cli/solace_plus.py alert watch --severity high
```

---

## Registry

The topic-to-MV registry lives at `config/topic-mv-registry.yaml`. It is auto-generated by `generate_mvs.py` — run after adding or changing events in Solace Event Portal:

```bash
python3 generate_mvs.py
```

The registry maps each Solace topic pattern to a RisingWave MV, a time column, and optional enriched/windowed/spatial variants.

---

## Prerequisites

The full stack must be running (`./demo/run_demo.sh`) for queries and subscriptions to work. See [CLAUDE.md](../CLAUDE.md) for stack setup and environment variable documentation.
