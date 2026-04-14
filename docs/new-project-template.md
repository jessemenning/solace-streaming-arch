# New Streaming Project Template

Use this as the starting prompt when creating a new Solace + RisingWave streaming
architecture from this project as a template.

For full lessons learned on porting, see `~/faa-streaming-arch/docs/lessons-learned.md`.

---

## Prompt

```
Use ~/solace-streaming-arch as the template. Build a new streaming
architecture project for [use case] where:

Upstream broker: [host/VPN/credentials, or "same local broker — no bridge needed"]
Topic patterns to bridge: [e.g. VEHICLE/telemetry/>, VEHICLE/status/>]
Topic structure: [list every level — e.g. VEHICLE/{id}/{status}/{lat}/{lon}/...]
Payload: [topic-encoded | JSON body — describe nested structure if body]
Key filter conditions: [e.g. status field values — specify exact case]

Deliverables:
1. ingestor.py with _topic_to_json() (topic-encoded data) or body extraction
   (JSON body data), producing flat JSON matching the RisingWave SOURCE column schema
2. init.sql.default using INCLUDE destination/timestamp syntax (not METADATA FROM)
   and quoted WITH-clause properties ("solace.url", "solace.queue", etc.)
3. docker-compose.yml with port remapping if 8180/4566/8091 conflict with existing stacks
4. config/solace/setup.sh with the new VPN name and queue names
5. generate_mvs.py STATIC_ANALYTICS_MVS with correct filter values for this domain
6. CLAUDE.md, README.md, BUSINESS.md updated for the new use case

Before writing any SQL filter conditions, print DISTINCT values of every
filter column from 10 sample messages. Never assume case or vocabulary.
Within 60 seconds of first data flowing, run:
  SELECT * FROM <routing_mv> LIMIT 5
to verify no columns are NULL. NULL columns at this stage mean payload
format mismatch — fix before proceeding.
```

---

## Critical syntax reference

### CREATE SOURCE (custom Solace connector)

Metadata columns use `INCLUDE` after the column list — **not** `METADATA FROM` inside it:

```sql
CREATE SOURCE my_source (
    field_one  VARCHAR,
    field_two  DOUBLE PRECISION
)
INCLUDE destination AS _rw_solace_destination
INCLUDE timestamp   AS _rw_solace_timestamp
WITH (
    connector         = 'solace',
    "solace.url"      = 'tcp://solace:55555',
    "solace.queue"    = 'my-queue',
    "solace.vpn_name" = 'my-vpn',
    "solace.username" = 'my-user',
    "solace.password" = 'my-password',
    "solace.ack_mode" = 'checkpoint'
)
FORMAT PLAIN ENCODE JSON;
```

`METADATA FROM 'destination'` inside the column list causes:
`sql parser error: expected ',' or ')' after column definition, found: METADATA`

### Safe queue purge sequence

Never purge a Solace queue while RisingWave has an active flow on it.
Always drop sources first:

```bash
# 1. Drop sources (closes flows cleanly)
psql -h localhost -p 4566 -U root -d dev \
  -c "DROP SOURCE my_source CASCADE;"

# 2. Safe to purge now
curl -sf -u admin:admin -X PUT \
  'http://localhost:8180/SEMP/v2/action/msgVpns/<vpn>/queues/<queue>/deleteMsgs' \
  -H 'Content-Type: application/json' -d '{}'

# 3. Recreate
psql -h localhost -p 4566 -U root -d dev -f config/risingwave/init.sql
```

Purging while connected causes `Bad Flow pointer '(nil)' in solClient_flow_settleMsg`
and requires source recreation to recover.

---

## Checklist before first commit

- [ ] Sampled actual upstream messages — ran `SELECT DISTINCT` on every filter column
- [ ] Payload format confirmed (topic-encoded vs. JSON body; nested vs. flat)
- [ ] `init.sql.default` generated from `generate_mvs.py --skip-ep`, not hand-edited
- [ ] `INCLUDE destination AS` / `INCLUDE timestamp AS` syntax (not `METADATA FROM`)
- [ ] Quoted property names in WITH clause (`"solace.url"`, `"solace.queue"`, etc.)
- [ ] First 5 rows of routing MV checked — no NULL payload columns
- [ ] Port map verified — no conflicts with other running stacks
- [ ] `.env.template` has all required variables; `.env` is in `.gitignore`
