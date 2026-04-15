# Solace + RisingWave vs. VMware Tanzu: Architecture Comparison

The Tanzu streaming stack has no single product equivalent. A comparable capability set requires assembling: **Tanzu RabbitMQ or Tanzu for Apache Kafka (transport) + Apache Flink (stream processing) + Spring Cloud Data Flow (pipeline orchestration)**, all deployed on Tanzu Application Platform or Tanzu Kubernetes Grid. The table below treats this assembled stack as "Tanzu."

| Capability | Solace + RisingWave | Tanzu Stack |
|---|---|---|
| **Query model** | MVs pre-computed incrementally as data arrives — zero-latency reads | Flink SQL pre-computes some state; ad-hoc queries require a separate analytical store (e.g. Postgres, Iceberg) |
| **SQL stream-stream joins** | Native `JOIN ... WITHIN INTERVAL` on RisingWave — always live | Flink SQL supports this; requires a separate Flink cluster (JobManager + TaskManagers) |
| **Windowed aggregations** | `TUMBLE` / `HOP` built into standard SQL; no extra cluster | Flink SQL supports TUMBLE/HOP; Kafka Streams windowing is programmatic Java, not SQL |
| **Pub/sub fan-out** | Solace Platform topics with multi-level wildcards (`*` / `>`); millions of msg/sec, microsecond latency | RabbitMQ topic exchange supports `*` / `#` wildcards but degrades at high binding counts; Kafka has flat partitions — no server-side wildcard routing |
| **Virtual topic pattern** | MV = topic subscription (`vehicle_speeds` ≡ `fleet/telemetry/*/metrics`) — declarative in SQL | No equivalent; consumers must query Flink state or poll a sink store; subscription semantics must be hand-coded |
| **IoT-scale fan-out** | Native — Solace designed for per-device topic hierarchies at scale; dynamic subscriptions | RabbitMQ: tens of thousands msg/sec ceiling before routing overhead climbs; Kafka: partitions must be pre-planned, no per-device wildcard routing |
| **No-Kafka intermediary** | RisingWave connects directly to Solace queues via SMF — no Kafka layer | Flink typically requires Kafka as its source; a Solace mirror over Kafka adds latency and another service to operate |
| **Delivery guarantee** | Exactly-once via checkpoint-mode ACK in the connector | Exactly-once via Flink + Kafka transactional producers — works but requires coordinated configuration across both systems |
| **Backpressure** | Durable Solace queues buffer when consumer is slow; DMQ + TTL handle poison messages | Kafka consumer lag provides buffering; RabbitMQ Classic Queue has memory/disk limits and can drop messages under pressure; DMQ equivalent requires manual policy configuration |
| **Historical backfill** | Sentinel-based cutover: connector detects boundary, triggers barrier flush, emits readiness event atomically — consumers get one event, no polling | Must be hand-built: catch-up job, flag in shared store, polling loop, manual race-condition handling; no platform-level primitive for this pattern |
| **Producer coupling** | Generator publishes to topic; knows nothing about downstream routing or schema | Same for Kafka; RabbitMQ producers must declare exchanges/bindings or rely on a broker-side policy |
| **SQL interface** | Postgres wire protocol — any `psql` client, ORM, or BI tool connects directly | Flink SQL available via REST or SQL Client; no Postgres wire protocol; BI tools need a separate sink (Postgres, Iceberg) |
| **Operational footprint** | Two services: Solace Platform + RisingWave | Four or more: Kafka/RabbitMQ + Flink cluster + SCDF server + k8s infrastructure; each has independent upgrades, sizing, and failure modes |
| **Connector to broker** | Custom SMF connector (this POC) — compiled into RisingWave binary | Flink Kafka connector is mature and upstream; RabbitMQ connector for Flink exists but is less widely used |
| **Where Tanzu wins** | — | Broader Spring/Kubernetes ecosystem; Flink connector maturity; vendor-supported enterprise support contracts; richer SCDF pipeline UI; easier path for teams already on Tanzu Application Platform |
