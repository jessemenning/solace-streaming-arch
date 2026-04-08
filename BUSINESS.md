# Business Value — Solace Streaming Architecture

## The Problem This Solves

Most enterprises that adopt an event-driven architecture hit the same wall:
**event routing and event analytics are solved by different teams, using different tools, with no shared model.**

The pub/sub broker handles routing. A separate streaming platform handles analytics.
Data is duplicated, schemas drift, and the "topic design" that made sense for routing
becomes a constraint that makes analytics harder. Adding a new analytical view requires
changing the topic hierarchy — which means changing producers, consumers, and ops config
in concert.

This POC demonstrates a different approach: **use the broker for what it's best at, use
streaming SQL for what it's best at, and connect them with a durable log.**

---

## The Architecture in Plain Language

```
IoT devices / applications
        │  publish events
        ▼
  Solace Platform          ← real-time routing, fan-out, guaranteed delivery
        │  bridge connector
        ▼
     Redpanda              ← durable, replayable log of every event
        │  streaming SQL
        ▼
    RisingWave             ← "virtual topics" — any slice of data, expressed as SQL
        │
        ▼
  Dashboards, alerts, downstream applications
```

**Solace Platform** is the traffic cop. It routes events with sub-millisecond latency
to the right consumers, handles guaranteed delivery, and enforces access control.
Nothing about this architecture changes how Solace works for the systems already connected to it.

**Redpanda** is the flight recorder. Every event is written to an immutable, replayable log.
This gives the analytics layer time to catch up after restarts, replay history for new views,
and audit every event that ever occurred.

**RisingWave** replaces topic subscriptions with SQL. Instead of designing a topic hierarchy
that anticipates every future consumer, developers write SQL queries. A new analytical view
takes minutes, not a sprint.

---

## Why This Matters: "Query Design" vs. "Topic Design"

Traditional pub/sub forces teams to anticipate consumers at design time. If a logistics team
wants "all high-severity engine alerts for vehicles in the Northeast region," someone has to:

1. Decide whether that filtering belongs in the topic hierarchy or the consumer code
2. Convince the platform team to add a new topic pattern
3. Update the Solace configuration and redeploy

With this architecture, that view is a SQL query:

```sql
SELECT vehicle_id, event_type, description, occurred_at
FROM fleet_events_raw
WHERE severity = 'high'
  AND solace_topic LIKE 'fleet/events/%'
  AND vehicle_id IN (SELECT vehicle_id FROM vehicles_in_region_northeast);
```

The materialized view updates in real time. No topic redesign. No broker config change.
No producer changes. The broker continues to handle routing exactly as before.

---

## Capabilities That Have No Pub/Sub Equivalent

Some analytical questions cannot be answered with topic subscriptions alone,
no matter how well the topic hierarchy is designed:

| Business question | Why pub/sub can't answer it alone | How this architecture answers it |
|---|---|---|
| "What was the average speed of each vehicle over the last 5 minutes?" | Requires stateful aggregation across multiple messages | `vehicle_speed_5min_avg` — TUMBLE window, updated continuously |
| "How many high-severity alerts has each vehicle triggered in the last hour?" | Requires a rolling count with time-based expiry | `vehicle_event_counts_1h` — rolling window, always current |
| "Which alerts were preceded by abnormal telemetry in the 2 minutes before?" | Requires joining two streams with a time window | `alerts_with_context` — stream-stream join, no external state store needed |
| "Which vehicles are currently inside the Boston metro area?" | Requires spatial filtering on coordinates | `vehicles_in_region_boston` — spatial predicate, live |
| "Give me a fleet-wide alert summary broken down by severity" | Requires aggregation across all vehicles and all topics | `fleet_alert_summary` — single SQL GROUP BY, updated in real time |

These are not edge cases. They are the questions that operations centers, logistics managers,
and field service teams ask every day. Without a streaming SQL layer, answering them requires
either building custom stateful consumers or running batch queries against a data warehouse —
both of which add latency, cost, and operational complexity.

---

## Applicability Beyond Fleet Monitoring

The domain (IoT fleet) is illustrative. The pattern applies wherever:

- **A Solace Platform deployment already routes events** from producers to consumers
- **Analytical consumers need views that cross topics** or require time-windowed aggregation
- **New consumer requirements emerge faster than topic hierarchies can be redesigned**

Concrete examples from Solace customer domains:

| Domain | Existing Solace use case | New capability this architecture adds |
|---|---|---|
| Financial markets | Trade and quote distribution to downstream systems | Real-time P&L by portfolio, rolling VWAP, anomaly detection across symbol streams |
| Utilities / smart grid | Meter event routing to billing and grid operations | Demand aggregation by region, outage correlation across meter clusters |
| Healthcare / hospital ops | Patient monitor alerts routed to nursing stations | Cross-patient anomaly patterns, staffing-demand rolling averages |
| Retail / supply chain | Inventory and order events routed to fulfillment | Out-of-stock prediction, cross-location inventory rebalancing signals |
| Manufacturing / Industry 4.0 | Sensor data routed to SCADA and historian | Multi-sensor correlation for predictive maintenance, OEE dashboards |

In each case, the Solace Platform deployment continues to operate unchanged.
The streaming SQL layer is additive — it consumes from the durable log without
touching the broker's routing configuration or producer applications.

---

## Comparison with Alternatives

### Why not just use Solace topic hierarchies?

Topic hierarchies are powerful for routing but are a design-time artifact.
They cannot express aggregations, joins, or time windows. A consumer that needs
"average speed over 5 minutes" must implement that logic itself, maintain its own state,
and handle recovery on restart. Multiply that by dozens of consumers and you have
dozens of independent state management implementations.

### Why not just use a data warehouse?

A data warehouse processes historical data in batch. It cannot answer "what is happening
right now." The latency of a batch pipeline — even a fast one — is measured in minutes to hours.
This architecture delivers SQL query results with latency measured in seconds.

### Why not use a traditional stream processor (Flink, Spark Streaming)?

Traditional stream processors are powerful but require writing code in Java or Scala,
managing cluster configuration, and deploying JVM applications. Operational overhead
is high and developer onboarding is slow. RisingWave exposes the same capability through
standard PostgreSQL-compatible SQL — the same language most analysts and data engineers already know.

### Why Redpanda instead of Kafka?

Redpanda is Kafka-compatible but eliminates the ZooKeeper dependency and has lower
operational overhead. For a single-tenant POC or small-scale deployment, it reduces
the infrastructure footprint significantly while remaining a drop-in replacement for
Kafka-native connectors and consumers.

---

## What This POC Proves

1. **The integration is real and working.** Solace Platform publishes, Kafka Connect bridges,
   Redpanda stores, RisingWave queries — all running together in Docker Compose with a live
   20-vehicle simulator generating continuous data.

2. **The virtual topic pattern works at the SQL layer.** Eleven materialized views — including
   windowed aggregations, stream-stream joins, and spatial filters — all update continuously
   from a single source of truth.

3. **The architecture is non-invasive.** Nothing in the Solace Platform configuration changes
   for existing producers or consumers. The bridge connector reads from a queue that already
   exists (or is added without disruption).

4. **The developer experience is dramatically simpler.** Adding a new analytical view is
   a SQL query, not a sprint. No topic hierarchy negotiation, no producer changes, no
   custom stateful consumer code.

---

## Recommended Next Steps

- **Production sizing:** RisingWave in playground mode (single process) handles this POC.
  A production deployment uses a distributed cluster with separate meta, compute, and storage nodes.
  Redpanda similarly scales horizontally.

- **Schema registry:** This POC uses plain JSON without schema enforcement. Production deployments
  benefit from a schema registry (Redpanda includes one) to catch payload drift early.

- **Sink connectors:** RisingWave can push results to downstream systems (databases, dashboards,
  notification services) via Kafka Connect sink connectors — closing the loop from event to action.

- **Solace Event Portal integration:** The topic hierarchy in this POC maps directly to
  Event Portal events and schemas. Cataloging these events in Event Portal makes the
  architecture discoverable and governable across teams.
