# Backfill Architecture

How the system transitions cleanly from historical replay to live streaming — without polling, without gaps, and without the consumer needing to understand the mechanics.

---

## The Problem

When RisingWave restarts (or starts for the first time), events that arrived while it was offline are sitting in the Solace queue. The connector must drain these historical events before a consumer can trust that a query reflects complete history. But the consumer has no way of knowing when "historical" ends and "live" begins — unless the system tells it.

Naive approaches fall short:

| Approach | Problem |
|----------|---------|
| Poll RisingWave until data appears | Doesn't know when history is complete — just when *some* data exists |
| Wait a fixed time | Brittle; queue depth varies |
| Check queue depth via SEMP | Requires broker admin access from the consumer |
| Count expected events client-side | Consumer must know how many historical events exist |

The backfill pattern solves this by making the **connector** responsible for all correctness logic. The consumer just waits for a signal.

---

## Core Concept: The Sentinel

A **sentinel message** is a specially marked persistent message published at the tail of the ingest queue — after all historical events have been enqueued, before the connector starts consuming. It is identified by a Solace user property:

```
x-solace-sentinel: backfill-complete
```

Because Solace queues deliver messages in FIFO order, the sentinel arrives at the connector *after* every historical message. The connector intercepts it, triggers a barrier flush, and signals consumers that history is complete and all materialized views are current.

The sentinel never reaches RisingWave's source table — it is consumed and discarded by the connector. Business views contain zero sentinel-related logic.

---

## Sequence

```
Producer          Solace Queue        Connector             RisingWave         Consumer
   |                   |                  |                     |                  |
   |--- historical --->|                  |                     |                  |
   |--- events ------->|                  |                     |                  |
   |--- (N msgs) ----->|                  |                     |                  |
   |--- sentinel ----->|                  |                     |                  |
   |                   |                  |                     |                  |
   |                   |<-- consume msgs--|                     |                  |
   |                   |-- historical --> emit to source table->|                  |
   |                   |-- sentinel ----> intercept/ACK         |                  |
   |                   |                  |-- WAIT (barrier) -->|                  |
   |                   |                  |<-- flush complete --|                  |
   |                   |                  |-- UPSERT status --->|                  |
   |                   |<-----------------|-- readiness event --|----------------->|
   |                   |                  |                     |                  |
   |                   |                  |-- live events ------>|                  |
```

---

## Connector Behavior (target design — not yet implemented in Rust)

The connector's `SplitReader` checks every incoming message for the sentinel user property.

### Normal message
- Emit to RisingWave source table
- ACK on Solace (after checkpoint, for exactly-once)
- Increment event counter

### Sentinel message
1. **ACK on Solace immediately** — removes it from the queue
2. **Intercept** — do not emit to source table
3. **Record state** — `sentinel_detected = true`, `sentinel_detected_at = now()`
4. **Barrier flush** — issue SQL `WAIT` via a pg connection to RisingWave frontend; blocks until all materialized views have processed all events emitted before the sentinel
5. **Write status table** — UPSERT `rw_solace_connector_status` with `is_ready = TRUE` and event counts
6. **Publish readiness event** — send to `system/risingwave/connector/{name}/ready` on Solace
7. **Continue** — resume consuming live events normally

### Restart handling
- **Restart during backfill**: Resume from last RisingWave checkpoint. Sentinel is still ahead in the queue and will be detected again.
- **Restart after sentinel**: Checkpointed state records `sentinel_detected = true`. Connector re-publishes the readiness event and re-writes the status table so any new consumers get notified.

---

## Readiness Signal

The connector uses two channels to signal readiness, covering both live waiters and late joiners:

### Primary: Solace readiness event

Published to: `system/risingwave/connector/{connector_name}/ready`

```json
{
  "connector_name": "solace_events",
  "status": "ready",
  "sentinel_detected_at": "2026-04-13T12:00:00Z",
  "historical_events_processed": 150000,
  "total_events_consumed": 150000,
  "barrier_flush_complete": true,
  "message": "All historical events processed. All materialized views current. Safe to query."
}
```

Consumers subscribed to `system/risingwave/connector/*/ready` receive this in milliseconds. No polling.

### Fallback: status table

Written by the connector after barrier flush:

```sql
rw_solace_connector_status (
    connector_name          VARCHAR PRIMARY KEY,
    is_ready                BOOLEAN,
    sentinel_detected_at    TIMESTAMPTZ,
    events_before_sentinel  BIGINT,
    total_events_consumed   BIGINT,
    last_updated            TIMESTAMPTZ
)
```

Consumers that start *after* the readiness event was published (late joiners) check this table once on startup. If `is_ready = TRUE`, they proceed immediately without subscribing to Solace at all.

---

## Client Behavior (`RisingWaveClient`)

The `backfill/query_client.py` client encapsulates both readiness paths. Consumers call one method and proceed.

### Startup sequence

```
RisingWaveClient()
  ├── Connect to RisingWave
  ├── Check status table (one read, no loop)
  │     ├── is_ready = TRUE  →  set ready flag, skip Solace subscription
  │     └── not ready / table absent  →  subscribe to Solace readiness topic
  └── Background thread listens for readiness event
```

### `wait_for_ready()`

Blocks on a `threading.Event` — zero CPU cost, zero RisingWave load while waiting. Returns as soon as either path signals ready. Raises `TimeoutError` if not signaled within the timeout (default 10 min).

```python
with RisingWaveClient() as client:
    client.wait_for_ready()          # blocks; instant if already ready
    rows = client.query("SELECT * FROM high_severity_alerts LIMIT 10")
```

### `query()` vs `query_new_view()`

| Method | When to use |
|--------|-------------|
| `query(sql)` | Any MV that existed when backfill ran — no WAIT needed, barrier already flushed |
| `query_new_view(sql)` | An MV created *after* the system went live — issues `WAIT` to ensure the new MV is fully populated before reading |

---

## Operational Steps

```bash
# 1. Let historical events accumulate in the Solace queue

# 2. Publish the sentinel (marks the historical/live boundary)
python3 backfill/publish_sentinel.py

# 3. Start (or restart) the connector — it will drain history, detect sentinel, signal ready

# 4. Full orchestration (sentinel → schema → wait → verify)
bash backfill/backfill_and_go.sh

# 5. Monitor queue depth during drain
python3 backfill/monitor_queue.py
```

---

## Key Design Principles

**Smart connector, simple consumer.** Sentinel detection, barrier flush, and readiness signaling are entirely the connector's responsibility. The consumer calls `wait_for_ready()` and queries normally — it has no knowledge of queue depths, event counts, or timing.

**Event-driven readiness.** No polling loops. The Solace readiness event arrives in milliseconds. The `threading.Event` wait is zero-cost.

**Defense in depth.** The status table is a persistent fallback. If a consumer starts days after the original readiness event, it still gets an immediate answer from the table — no need to replay the Solace event.

**Clean business views.** The sentinel never reaches a source table, so materialized views need zero sentinel-related filters.

**Exactly-once semantics.** The connector uses `checkpoint` ACK mode. Sentinel interception is checkpointed. If the connector crashes mid-barrier, the sentinel is re-detected and the barrier re-issued from a clean state on restart.
