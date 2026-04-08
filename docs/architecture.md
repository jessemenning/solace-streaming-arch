# Architecture — Solace Streaming POC

## The Problem This Solves

Traditional event-driven architectures using Solace alone require careful topic
hierarchy design upfront. Every new consumer use case — "give me only high-severity
alerts", "give me speeds averaged over 5 minutes", "give me vehicles inside this
geographic region" — requires a new topic, a new bridge, or a new filter
application running continuously.

This POC demonstrates an alternative: use Solace for what it is best at
(real-time routing, fan-out, guaranteed delivery), preserve the complete event
stream in a durable log (Redpanda), and move the "subscription logic" into the
database layer (RisingWave materialized views).

---

## Data Flow

```
Fleet vehicles
     │  native Python SDK  (solace-pubsubplus)
     ▼
Solace Platform Event Broker   port 55555 (SMF)
  VPN: streaming-poc
  Queue: q/redpanda-bridge  ← subscription: fleet/>
     │
     │  Kafka Connect  (Solace Source Connector)
     ▼
Redpanda (Kafka-compatible log)
  Topics:
    fleet-telemetry   (6 partitions, infinite retention)
    fleet-events      (6 partitions, infinite retention)
    fleet-commands    (3 partitions, infinite retention)
  Every message payload includes:  solace_topic  (exact original topic)
     │
     │  Kafka protocol  (bootstrap: redpanda:9092)
     ▼
RisingWave Streaming SQL Engine   port 4566 (Postgres wire protocol)
  Sources: fleet_telemetry_raw, fleet_events_raw, fleet_commands_raw
  Materialized views (incremental, always-up-to-date):
    high_severity_alerts          → replaces fleet/events/*/alerts/high
    vehicle_speed_5min_avg        → 5-min tumbling window (no Solace equivalent)
    alerts_with_context           → stream-stream join (no Solace equivalent)
    vehicles_in_region_boston     → spatial filter (no Solace equivalent)
    …
```

---

## Design Decisions

### 1. Coarse-grained Redpanda topics

Solace supports thousands of fine-grained topic variants.
Redpanda topics map to partitions and replication units — it is not practical
(or necessary) to create one Redpanda topic per Solace topic.

Decision: bridge all `fleet/>` traffic into three coarse-grained Redpanda topics
(`fleet-telemetry`, `fleet-events`, `fleet-commands`).
The original Solace topic is preserved in the JSON payload as `solace_topic`.
RisingWave SQL filters on `solace_topic` or payload fields to reconstruct any
logical sub-stream that was previously a Solace topic.

### 2. Solace topic preserved in payload, not only headers

The Solace Kafka connector can propagate the source destination as a Kafka header.
However, header support in RisingWave's Kafka connector is evolving.
Embedding `solace_topic` directly in the JSON payload guarantees the information
survives regardless of connector or schema changes.

### 3. Partition key = vehicle_id (via DESTINATION mapping)

The connector config sets `sol.kafka_message_key=DESTINATION`, which uses the
Solace destination (topic) string as the Kafka partition key.
Since the topic always contains the vehicle ID, messages for the same vehicle
hash to the same partition, maintaining per-vehicle ordering in Redpanda.

Alternative: a custom message processor that extracts the vehicle ID from the
payload. This adds complexity for marginal gain in a POC.

### 4. RisingWave in playground mode

For the POC, RisingWave runs in single-node `playground` mode.
This removes the need for a separate storage backend (etcd, MinIO) and makes
the stack simpler to start locally.
For production, RisingWave would run in distributed mode with:
  - Meta node (coordination)
  - Compute nodes (query execution)
  - Compactor (storage management)
  - Object store (S3 or compatible)

### 5. Infinite retention in Redpanda

Redpanda is the system of record.
Setting `retention.ms=-1` means no messages are deleted by time.
In production, set a retention policy aligned with your replay and compliance needs.

---

## Virtual Topics vs. Solace Subscriptions

| Consumer need | Solace approach | RisingWave approach |
|---|---|---|
| All high-severity alerts | Subscribe to `fleet/events/*/alerts/high` | `SELECT * FROM high_severity_alerts` |
| Speed for one vehicle | Subscribe to `fleet/telemetry/vehicle_005/metrics/speed` | `SELECT * FROM vehicle_speeds WHERE vehicle_id='vehicle_005'` |
| 5-minute speed average | External stateful consumer + aggregation app | `SELECT * FROM vehicle_speed_5min_avg` |
| Correlated alerts + telemetry | Two subscriptions + external join app | `SELECT * FROM alerts_with_context` |
| Vehicles in a region | External geospatial service | `SELECT * FROM vehicles_in_region_boston` |
| New dimension added | New topic hierarchy + migration | New `WHERE` clause or new materialized view |

---

## Latency Characteristics

End-to-end latency (Solace publish → RisingWave materialized view visible):

| Segment | Expected latency |
|---|---|
| Solace publish → Redpanda via Kafka Connect | 50–500 ms (connector poll interval) |
| Redpanda → RisingWave source ingestion | 10–100 ms |
| RisingWave incremental view update | < 100 ms for simple filters |
| Total typical | 200 ms – 1 s |

For pure real-time consumers that need sub-100 ms delivery, they should
subscribe directly to Solace topics (not via RisingWave).
RisingWave is for analytical/aggregated views where near-real-time (< 5 s)
is sufficient.
