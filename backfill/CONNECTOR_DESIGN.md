# Connector-Side Backfill Design

Design reference for the native Solace connector in `~/risingwave/` (branch `feat/solace-source-connector`).
All changes described here are **fully implemented** in `src/connector/src/source/solace/source/reader.rs`.

## Key code locations

| File | What to change |
|------|---------------|
| `src/connector/src/source/solace/source/reader.rs` | Sentinel detection in `next()`, barrier flush, readiness event |
| `src/connector/src/source/solace/source/message.rs` | User property extraction (`x-solace-sentinel`) |
| `src/connector/src/source/solace/mod.rs` | State struct additions (`sentinel_detected`, `sentinel_detected_at`) |

## Sentinel Detection in SplitReader

On each message received from the Solace queue:

1. Check for user property `x-solace-sentinel`
2. If present and value is `backfill-complete`:
   - ACK the sentinel on Solace
   - Do **NOT** emit to the source table (intercept)
   - If already detected (duplicate), log warning and skip
   - Record in checkpointed state: `sentinel_detected = true`, `sentinel_detected_at = Utc::now()`
   - Trigger barrier flush (SQL `WAIT` via pg connection to local frontend)
   - Write `rw_solace_connector_status` table (fallback for late-joining consumers)
   - Publish readiness event to Solace topic `system/risingwave/connector/{name}/ready`
   - Continue consuming live events
3. If not present: normal message processing (emit to source table, ACK, increment counter)

## Barrier Flush

Use SQL WAIT via a pg connection to the local RisingWave frontend:

```rust
let conn = PgConnection::connect("postgresql://root@localhost:4566/dev").await?;
conn.execute("SET streaming_flush_wait_timeout_ms = 60000").await?;
conn.execute("WAIT").await?;
conn.close().await?;
```

This ensures all materialized views have processed all events up to the sentinel before
readiness is signaled.

## Readiness Event

Published to: `system/risingwave/connector/{connector_name}/ready`

Payload:
```json
{
    "connector_name": "solace_events",
    "status": "ready",
    "sentinel_detected_at": "2026-04-13T...",
    "historical_events_processed": 150000,
    "total_events_consumed": 150000,
    "barrier_flush_complete": true,
    "message": "All historical events processed. All materialized views current. Safe to query."
}
```

User property on the message: `x-risingwave-event = connector-ready`

## Status Table (Fallback)

```sql
CREATE TABLE IF NOT EXISTS rw_solace_connector_status (
    connector_name          VARCHAR PRIMARY KEY,
    is_ready                BOOLEAN DEFAULT FALSE,
    sentinel_detected_at    TIMESTAMPTZ,
    events_before_sentinel  BIGINT,
    total_events_consumed   BIGINT,
    last_updated            TIMESTAMPTZ DEFAULT NOW()
);
```

Written by the connector via UPSERT after barrier flush completes.

## Restart Handling

- **Restart during backfill**: Resume from last checkpoint. Sentinel still ahead on queue.
- **Restart after sentinel**: Checkpointed state shows `sentinel_detected = true`.
  Re-publish readiness event and re-write status table for new subscribers.

## User Property Extraction

The Solace C SDK exposes user properties via `solClient_msg_getProperty()`. The Rust
connector's message wrapper needs to expose this (currently only destination, timestamp,
and a few metadata fields are extracted). Add:

```rust
pub fn get_user_property(&self, key: &str) -> Option<String> {
    // Use solClient_msg_getUserPropertyMap then lookup key
}
```

This is the only new API surface needed in the message module.
