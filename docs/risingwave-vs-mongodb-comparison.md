# Solace + RisingWave vs. MongoDB: Architecture Comparison

| Capability | Solace + RisingWave | MongoDB |
|---|---|---|
| **Query model** | Results pre-computed incrementally as data arrives | Aggregation pipelines computed at query time — O(n) scan per request |
| **Stream-stream joins** | Native `JOIN ... WITHIN INTERVAL` — always live | Requires two queries, application-level correlation, and manual time-window logic |
| **Windowed aggregations** | TUMBLE/HOP windows built into SQL — e.g. 5-min rolling average | Must be implemented via scheduled jobs or Change Streams + application code |
| **Pub/sub fan-out** | Solace topics with rich wildcard hierarchies — microsecond latency | No native pub/sub; Change Streams approximate it via polling |
| **Virtual topic pattern** | MV = topic subscription (`vehicle_speeds` ≡ `fleet/telemetry/*/metrics`) | No equivalent — consumers must query; no predicate-based subscription |
| **Delivery guarantee** | Exactly-once via checkpoint-mode ACK | No delivery guarantee from producer; idempotency is application responsibility |
| **Backpressure** | Durable Solace queues buffer when consumer is slow; DMQ + TTL handle poison messages | No native backpressure; slow consumers fall behind silently |
| **Historical backfill** | Sentinel-based cutover — connector detects boundary, publishes readiness event atomically | Requires scheduled job, catch-up flag, polling, and manual race-condition handling |
| **Producer coupling** | Generator publishes to topic; knows nothing about downstream routing or schema | Producer must know consumer's data model to write documents correctly |
| **Operational visibility** | Queue depth, DMQ counts, connector status table, readiness events | Change Stream lag requires custom monitoring |
| **Where MongoDB wins** | — | Ad-hoc historical queries, flexible schema evolution, rich document nesting |
