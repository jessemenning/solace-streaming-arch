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
streaming SQL for what it's best at, and connect them directly.**

---

## The Architecture in Plain Language

```
IoT devices / applications
        │  publish events
        ▼
  Solace Platform          ← real-time routing, fan-out, guaranteed delivery
        │  native SMF protocol (direct queue binding)
        ▼
    RisingWave             ← "virtual topics" — any slice of data, expressed as SQL
        │
        ▼
  Dashboards, alerts, downstream applications
```

**Solace Platform** is the traffic cop. It routes events with sub-millisecond latency
to the right consumers, handles guaranteed delivery, and enforces access control.
Nothing about this architecture changes how Solace works for the systems already connected to it.

**RisingWave connects directly to Solace** via a native source connector that speaks
the Solace SMF protocol. There is no proxy, no webhook, no HTTP intermediary. RisingWave
binds to Solace's durable queues and receives messages — including the original topic
address and sender timestamp — natively. Delivery semantics are exactly-once: RisingWave
acknowledges messages only after successfully checkpointing them, so nothing is lost and
nothing is duplicated. Backpressure flows naturally from RisingWave back to the broker —
if the streaming engine slows down, the queue holds messages until it catches up. No
dead-letter queue configuration or retry logic is needed at the application level.

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

## Agentic Use Cases

AI agents need the same thing analytical consumers need: **a pre-computed, continuously
updated view of the world** — without building their own state management. This architecture
is a natural substrate for agentic systems.

### The core problem for agents

An AI agent that wants to act on real-time events has two options today:

1. **Subscribe to raw topics directly** — the agent receives every individual message and
   must maintain its own state (counts, averages, join windows, geofence membership) in memory.
   It cannot survive a restart without replaying history. Its context window fills with raw
   telemetry instead of distilled signals.

2. **Poll a database or data warehouse** — the agent gets clean aggregated data but with
   batch latency. It can't react to what's happening *right now*.

This architecture provides a third option: **materialized views as agent-ready context sources.**
Every view is always current, always pre-aggregated, and queryable via standard SQL.
The agent asks a question; the answer is already computed.

### Concrete agentic patterns

**Trigger agent on anomaly detection**

Instead of an agent processing every raw telemetry message, it subscribes to a change feed
from `high_engine_temp_vehicles` or `high_severity_alerts`. The agent only activates when
an anomaly has already been identified by the streaming layer — its first message is
*"vehicle_012 has had engine temp above 220°F for 4 consecutive readings,"* not 400 raw
sensor values to reason across.

**Agent with pre-built situational context**

An agent dispatched to handle an alert can query `alerts_with_context` to immediately
retrieve the correlated telemetry from the 2 minutes before the event. It doesn't need to
join streams itself — that join is already materialized and waiting. The agent's first tool
call returns a complete picture: what happened, what the vehicle was doing, how it compared
to fleet norms.

**Multi-agent coordination via shared state**

In a multi-agent fleet operations system, different agents handle different responsibilities
(routing, maintenance scheduling, driver communication). Each queries the same materialized
views as its shared world model. There is no per-agent state to synchronize — the streaming
layer is the single source of truth, and every agent sees the same current reality.

**Agent-driven geofence and SLA monitoring**

An operations agent queries `vehicles_in_region_boston` and `vehicle_event_counts_1h` on
a schedule to detect SLA breaches (e.g., too many high-severity events, vehicle outside
expected region). Because the views are always current, the agent's polling interval
determines its response latency — not the speed of a data pipeline.

**Grounding LLM reasoning in real-time data**

When a large language model needs to reason about fleet state, giving it access to
`vehicle_last_known_position`, `fleet_alert_summary`, and `vehicle_speed_5min_avg` as
tool outputs grounds its responses in the actual current state of the world. The LLM
doesn't need to process raw event streams — it queries views that have already distilled
thousands of messages into the facts that matter.

> **This pattern is demonstrated live.** The Fleet Operations AI UI (http://localhost:8090
> when the stack is running) is a working agentic demo: a Claude-backed assistant with
> 7 SQL-backed tools querying RisingWave materialized views in real time. Ask it questions
> like "Which vehicles have the most alerts today?" or "What's happening with vehicle_005?"
> and it selects and calls the appropriate tools, then synthesizes the results into a response.

### Why this architecture is well-suited for agentic workloads

| Agent requirement | How this architecture meets it |
|---|---|
| Low-latency context | Views update in seconds, not minutes — agents get current state on demand |
| Clean signal, not raw noise | Aggregations and joins are pre-computed — agent context is distilled, not voluminous |
| Stateless agent design | State lives in the streaming layer — agents can restart without replaying history |
| Consistent world model | Multiple agents query the same views — no per-agent state divergence |
| Easy to add new views | A new agent capability = a new SQL query, not a new topic hierarchy |
| Standard interfaces | RisingWave speaks PostgreSQL — any agent framework with a SQL tool can connect |

### Connection to Solace Agent Mesh

In deployments using Solace Agent Mesh (SAM), agents communicate over Solace Platform topics.
This architecture extends that pattern: the same broker that routes agent-to-agent messages
also delivers IoT and operational events directly into RisingWave, which computes the
situational awareness layer that agents query as tools. The result is a unified event fabric
where real-world events and agent reasoning exist in the same architectural plane.

---

## Unified CLI: The Same Address Space Across Time

The `solace+` CLI extends Solace topic addressing with a time dimension. A developer who already knows the topic pattern `fleet/events/*/alerts/high` can use that same pattern to query the last hour of historical alerts from RisingWave, subscribe to live alerts from the broker, or do both in sequence — with a single command and no knowledge of which system stores what. History is replayed as individual Solace-style events before live messages begin, so the handoff is seamless. This collapses two previously separate interfaces (psql for history, SDK for live) into one, removes the need to know which materialized view backs which topic, and lets developers build queries, playbooks, and AI tools that treat the event stream as a single continuous source regardless of whether the data is seconds old or hours old.

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
The streaming SQL layer is additive — it consumes from the broker without
touching the routing configuration or producer applications.

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

### Why a native connector instead of a proxy or REST Delivery Point?

Earlier versions of this architecture used a Python proxy to bridge Solace queues to
RisingWave's webhook endpoint. That worked, but it added a moving part: an intermediary
service that had to be deployed, monitored, scaled, and restarted independently. HTTP
delivery meant retry logic, dead-letter queue configuration, and no native backpressure
from the streaming engine to the broker.

The native Solace source connector eliminates all of that. RisingWave connects directly
to Solace queues over the SMF protocol — the same protocol Solace uses internally. Message
metadata (topic, timestamp) arrives natively without HTTP header injection. Delivery is
exactly-once via checkpoint-based acknowledgment. Backpressure is handled by the protocol
itself: if RisingWave falls behind, the broker holds messages in the queue. Named
dead-message queues per ingest channel isolate failed messages automatically — no
application-level retry logic or custom dead-letter routing required. No webhook
endpoint, no proxy service to operate. (The Solace connector currently requires a custom
RisingWave build until it is merged upstream — once accepted, the stock image works.)

The result is fewer services to deploy, stronger delivery guarantees, and an architecture
that is straightforward to explain: generators publish to Solace, RisingWave reads from
Solace. That directness matters for production adoption.

### Why not a message queue intermediary (Kafka/Redpanda)?

Adding a broker intermediary between Solace and RisingWave adds operational complexity —
another service to size, monitor, and fail. The native connector achieves durable,
exactly-once delivery by binding directly to Solace's native queues, with no additional
infrastructure, connector plugins, or schema registry.

---

## What This POC Proves

1. **The integration is real and working.** Solace Platform publishes, RisingWave reads
   directly via the native Solace source connector — all running together in Docker
   Compose with a live 20-vehicle simulator generating continuous data.

2. **The virtual topic pattern works at the SQL layer.** Eleven materialized views — including
   windowed aggregations, stream-stream joins, and spatial filters — all update continuously
   from a single source of truth.

3. **The architecture is non-invasive.** Nothing in the Solace Platform configuration changes
   for existing producers or consumers. The durable queue reads from a subscription that already
   exists (or is added without disruption).

3a. **The native connector is a genuine open-source contribution.** No Solace source connector
   existed in the RisingWave ecosystem before this project. The connector we built — speaking
   the Solace SMF protocol with checkpoint-based exactly-once delivery — is a first, and it
   benefits anyone who wants to connect Solace Platform to RisingWave without intermediaries.

4. **The developer experience is dramatically simpler.** Adding a new analytical view is
   a SQL query, not a sprint. No topic hierarchy negotiation, no producer changes, no
   custom stateful consumer code.

5. **The agentic pattern is running.** The Fleet Operations AI demo (included in the stack)
   shows an LLM using materialized views as tool outputs. It demonstrates concretely what
   "grounding AI reasoning in real-time data" means in practice — not a slide, a working
   system asking and answering questions about live fleet state.

---

## Recommended Next Steps

- **Production sizing:** RisingWave in playground mode (single process) handles this POC.
  A production deployment uses a distributed cluster with separate meta, compute, and storage nodes.

- **Schema registry:** This POC uses plain JSON without schema enforcement. Production deployments
  benefit from a schema registry to catch payload drift early.

- **Sink connectors:** RisingWave can push results to downstream systems (databases, dashboards,
  notification services) via sink connectors — closing the loop from event to action.

- **Solace Event Portal integration:** Complete. The Fleet Operations application domain
  in Event Portal is the catalog of record for all events and schemas. `generate_mvs.py`
  queries the EP catalog to regenerate RisingWave materialized views automatically —
  adding a new event in EP and re-running the script produces a new live SQL view with
  no manual SQL authoring required.
