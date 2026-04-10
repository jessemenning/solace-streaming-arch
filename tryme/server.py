#!/usr/bin/env python3
"""
Solace+ Try Me — FastAPI backend
Enhanced Try Me tab with history replay from RisingWave + live from Solace Platform SMF.

Endpoints:
  GET  /                  → serve index.html
  GET  /config            → connection info and topic list
  POST /publish           → publish to Solace SMF
  GET  /subscribe         → SSE: optional history from RisingWave, then live from SMF
  DELETE /subscribe/{sid} → stop a live subscription
"""

import asyncio
import json
import os
import queue
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from typing import Optional

import psycopg2
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")

# ── SDK imports (graceful if not installed) ───────────────────────────────────
try:
    from solace.messaging.messaging_service import MessagingService
    from solace.messaging.resources.topic import Topic
    from solace.messaging.resources.topic_subscription import TopicSubscription

    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
RW_HOST = os.environ.get("RW_HOST", "localhost")
RW_PORT = int(os.environ.get("RW_PORT", "4566"))

SOLACE_HOST = os.environ.get("SOLACE_HOST", "tcp://localhost:55555")
SOLACE_VPN = os.environ.get("SOLACE_VPN", "streaming-poc")
SOLACE_USER = os.environ.get("SOLACE_USER", "streaming-user")
SOLACE_PASSWORD = os.environ.get("SOLACE_PASSWORD", "default")

REGISTRY_PATH = Path(
    os.environ.get("REGISTRY_PATH", str(Path(__file__).parent.parent / "config" / "topic-mv-registry.yaml"))
)

_executor = ThreadPoolExecutor(max_workers=20)

# ── Registry ──────────────────────────────────────────────────────────────────
def _load_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    with open(REGISTRY_PATH) as f:
        doc = yaml.safe_load(f)
    return doc.get("mappings", [])


def _pat_to_regex(pattern: str) -> re.Pattern:
    """Convert Solace topic pattern (wildcards: *, >) to regex."""
    parts = pattern.split("/")
    regex_parts = []
    for p in parts:
        if p == ">":
            regex_parts.append(".+")
            break
        elif p == "*":
            regex_parts.append("[^/]+")
        else:
            regex_parts.append(re.escape(p))
    return re.compile("^" + "/".join(regex_parts) + "$")


def find_entry(topic_pattern: str) -> Optional[dict]:
    """Find best registry entry for a given topic pattern."""
    mappings = _load_registry()
    # Exact match first
    for m in mappings:
        if m["pattern"] == topic_pattern:
            return m
    # Pattern covers topic
    for m in mappings:
        if _pat_to_regex(m["pattern"]).match(topic_pattern):
            return m
    # Topic pattern contains registry pattern
    for m in mappings:
        if _pat_to_regex(topic_pattern).match(m["pattern"].replace("*", "x").replace(">", "x/y")):
            return m
    return None


# ── RisingWave ────────────────────────────────────────────────────────────────
def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Not serializable: {type(obj).__name__}")


def rw_query_history(mv: str, time_col: str, since_interval: str, limit: int = 500) -> list[dict]:
    sql = (
        f"SELECT * FROM {mv} "
        f"WHERE {time_col} > NOW() - INTERVAL '{since_interval}' "
        f"ORDER BY {time_col} ASC "
        f"LIMIT {limit}"
    )
    try:
        conn = psycopg2.connect(
            host=RW_HOST, port=RW_PORT,
            user="root", database="dev",
            connect_timeout=5
        )
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        return [{"_error": str(e)}]


# ── Duration helpers ──────────────────────────────────────────────────────────
_DURATION_MAP = {
    "15m": "15 minutes",
    "30m": "30 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "8h": "8 hours",
    "24h": "24 hours",
    "48h": "48 hours",
}


def to_pg_interval(since: str) -> Optional[str]:
    return _DURATION_MAP.get(since.strip().lower())


# ── Solace SMF receiver (polling thread) ─────────────────────────────────────
def _make_broker_props() -> dict:
    return {
        "solace.messaging.transport.host": SOLACE_HOST,
        "solace.messaging.service.vpn-name": SOLACE_VPN,
        "solace.messaging.authentication.scheme.basic.username": SOLACE_USER,
        "solace.messaging.authentication.scheme.basic.password": SOLACE_PASSWORD,
    }


def start_receiver(
    topic_pattern: str,
    async_q: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
) -> threading.Event:
    """Start a Solace SMF polling receiver in a background thread.

    Messages are posted to async_q via loop.call_soon_threadsafe so they
    land safely in the asyncio event loop.
    """
    stop_event = threading.Event()

    def _run():
        if not SDK_AVAILABLE:
            # Emit a warning item so the SSE stream can report it
            loop.call_soon_threadsafe(
                async_q.put_nowait,
                {"type": "error", "message": "Solace SDK not available — install solace-pubsubplus"},
            )
            return

        try:
            svc = MessagingService.builder().from_properties(_make_broker_props()).build()
            svc.connect()
        except Exception as e:
            loop.call_soon_threadsafe(
                async_q.put_nowait,
                {"type": "error", "message": f"Solace connect failed: {e}"},
            )
            return

        try:
            receiver = (
                svc.create_direct_message_receiver_builder()
                .with_subscriptions([TopicSubscription.of(topic_pattern)])
                .build()
            )
            receiver.start()

            while not stop_event.is_set():
                msg = receiver.receive_message(timeout=200)
                if msg is None:
                    continue
                topic = msg.get_destination_name()
                try:
                    payload = json.loads(msg.get_payload_as_string() or "{}")
                except Exception:
                    raw = msg.get_payload_as_bytes()
                    try:
                        payload = json.loads(raw.decode("utf-8")) if raw else {}
                    except Exception:
                        payload = {"_raw": raw.decode("utf-8", errors="replace") if raw else ""}

                item = {"type": "live", "topic": topic, "payload": payload}
                try:
                    loop.call_soon_threadsafe(async_q.put_nowait, item)
                except Exception:
                    pass  # queue full or loop closed

            receiver.terminate()
        finally:
            svc.disconnect()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return stop_event


# ── Publisher ─────────────────────────────────────────────────────────────────
def _publish_sync(topic: str, payload: dict) -> dict:
    if not SDK_AVAILABLE:
        return {"ok": False, "error": "Solace SDK not available"}
    try:
        svc = MessagingService.builder().from_properties(_make_broker_props()).build()
        svc.connect()
        pub = svc.create_direct_message_publisher_builder().build()
        pub.start()
        msg = svc.message_builder().build(json.dumps(payload))
        pub.publish(message=msg, destination=Topic.of(topic))
        pub.terminate()
        svc.disconnect()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Solace+ Try Me")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def root():
    html = Path(__file__).parent / "index.html"
    from fastapi.responses import HTMLResponse as HR
    return HR(content=html.read_text(), headers={"Cache-Control": "no-store"})


@app.get("/config")
async def get_config():
    """Return connection info and available topics with history support."""
    mappings = _load_registry()
    topics = []
    seen = set()
    for m in mappings:
        p = m["pattern"]
        if p not in seen:
            seen.add(p)
            topics.append(
                {
                    "pattern": p,
                    "mv": m.get("mv"),
                    "has_history": bool(m.get("time_column")),
                    "time_column": m.get("time_column"),
                    "enriched": m.get("enriched"),
                    "aggregates": m.get("aggregates"),
                }
            )
    return {
        "solace": {
            "host": SOLACE_HOST,
            "vpn": SOLACE_VPN,
            "user": SOLACE_USER,
            "sdk_available": SDK_AVAILABLE,
        },
        "risingwave": {
            "host": RW_HOST,
            "port": RW_PORT,
        },
        "topics": topics,
        "duration_presets": list(_DURATION_MAP.keys()),
    }


class PublishRequest(BaseModel):
    topic: str
    payload: dict


@app.post("/publish")
async def publish(req: PublishRequest):
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_executor, _publish_sync, req.topic, req.payload)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Publish failed"))
    return {"ok": True, "topic": req.topic}


@app.get("/subscribe")
async def subscribe_sse(
    topic: str = Query(..., description="Solace topic pattern to subscribe"),
    since: Optional[str] = Query(None, description="History window: 15m, 30m, 1h, 4h, 8h, 24h"),
    limit: int = Query(500, description="Max historical rows to return"),
):
    """
    Server-Sent Events stream.

    Event types:
      {"type":"status", "message":"..."}
      {"type":"history_start", "count": N, "mv":"...", "since":"..."}
      {"type":"history", "topic":"...", "payload":{...}, "ts":"..."}
      {"type":"history_done", "count": N}
      {"type":"live", "topic":"...", "payload":{...}}
      {"type":"error", "message":"..."}
      {"type":"ping"}
    """
    loop = asyncio.get_running_loop()
    async_q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    stop_ev: Optional[threading.Event] = None

    async def stream():
        nonlocal stop_ev
        seen: set = set()
        hist_count = 0


        # ── 1. Start live receiver immediately (buffer during history replay) ──
        yield f"data: {json.dumps({'type':'status','message':'Connecting to Solace Platform...'})}\n\n"
        stop_ev = start_receiver(topic, async_q, loop)
        yield f"data: {json.dumps({'type':'status','message':'Subscribed to live stream'})}\n\n"

        # ── 2. History replay ─────────────────────────────────────────────────
        if since:
            pg_iv = to_pg_interval(since)
            entry = find_entry(topic)

            if pg_iv and entry and entry.get("time_column"):
                mv = entry["mv"]
                time_col = entry["time_column"]

                yield f"data: {json.dumps({'type':'status','message':f'Fetching history from {mv}...'})}\n\n"

                # Run blocking RisingWave query in thread pool
                rows = await loop.run_in_executor(
                    _executor, rw_query_history, mv, time_col, pg_iv, limit
                )

                if rows and "_error" in rows[0]:
                    yield f"data: {json.dumps({'type':'error','message':rows[0]['_error']})}\n\n"
                else:
                    hist_count = len(rows)
                    yield f"data: {json.dumps({'type':'history_start','count':hist_count,'mv':mv,'since':since})}\n\n"

                    for row in rows:
                        ts_val = row.get(time_col)
                        ts_str = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val) if ts_val else None
                        dedup_key = (row.get("vehicle_id"), ts_str)
                        seen.add(dedup_key)

                        row_serial = json.loads(json.dumps(row, default=json_default))
                        evt = {
                            "type": "history",
                            "topic": row_serial.get("solace_topic") or entry["pattern"],
                            "payload": row_serial,
                            "ts": ts_str,
                        }
                        yield f"data: {json.dumps(evt)}\n\n"
                        await asyncio.sleep(0)  # yield control

                    yield f"data: {json.dumps({'type':'history_done','count':hist_count})}\n\n"
            elif not pg_iv:
                msg = f'Unknown duration "{since}" — use 15m, 30m, 1h, 4h, 8h, or 24h'
                yield f"data: {json.dumps({'type':'error','message':msg})}\n\n"
            elif not entry:
                msg = f'Topic "{topic}" not found in registry — history unavailable (live only)'
                yield f"data: {json.dumps({'type':'error','message':msg})}\n\n"
            else:
                msg = f'Topic "{topic}" matched MV "{entry["mv"]}" but has no time column — history unavailable (live only)'
                yield f"data: {json.dumps({'type':'error','message':msg})}\n\n"

        # ── 3. Live stream (dedup against history) ────────────────────────────
        yield f"data: {json.dumps({'type':'status','message':'Streaming live messages...'})}\n\n"

        try:
            while True:
                try:
                    item = await asyncio.wait_for(async_q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    yield 'data: {"type":"ping"}\n\n'
                    continue

                if item.get("type") == "error":
                    yield f"data: {json.dumps(item)}\n\n"
                    continue

                payload = item.get("payload", {})
                ts = payload.get("recorded_at") or payload.get("occurred_at") or payload.get("issued_at")
                dedup_key = (payload.get("vehicle_id"), ts)

                if dedup_key in seen:
                    seen.discard(dedup_key)  # allow future duplicates
                    continue

                evt = {
                    "type": "live",
                    "topic": item.get("topic", topic),
                    "payload": payload,
                    "ts": ts,
                }
                yield f"data: {json.dumps(evt, default=json_default)}\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            if stop_ev:
                stop_ev.set()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091, log_level="info")
