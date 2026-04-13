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
import atexit
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta, timezone
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s  %(message)s")
logger = logging.getLogger("tryme")

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

SOLACE_HOST = os.environ.get("SOLACE_HOST", "tcp://localhost:55554")
SOLACE_VPN = os.environ.get("SOLACE_VPN", "streaming-poc")
SOLACE_USER = os.environ.get("SOLACE_USER", "streaming-user")
SOLACE_PASSWORD = os.environ.get("SOLACE_PASSWORD", "default")

REGISTRY_PATH = Path(
    os.environ.get("REGISTRY_PATH", str(Path(__file__).parent.parent / "config" / "topic-mv-registry.yaml"))
)

_executor = ThreadPoolExecutor(max_workers=20)
atexit.register(_executor.shutdown, wait=False)

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
    if isinstance(obj, timedelta):
        return str(obj)
    raise TypeError(f"Not serializable: {type(obj).__name__}")


def rw_query_history(mv: str, time_col: str, since_interval: str, limit: int = 500) -> list[dict]:
    from psycopg2 import sql as psql
    query = psql.SQL("SELECT * FROM {} WHERE {} > NOW() - INTERVAL %s ORDER BY {} ASC LIMIT %s").format(
        psql.Identifier(mv), psql.Identifier(time_col), psql.Identifier(time_col)
    )
    try:
        conn = psycopg2.connect(
            host=RW_HOST, port=RW_PORT,
            user="root", database="dev",
            connect_timeout=5
        )
        with conn.cursor() as cur:
            cur.execute(query, (since_interval, limit))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error("RisingWave query failed: %s", e)
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


# ── Ad-hoc query presets ──────────────────────────────────────────────────────
QUERY_PRESETS = [
    # ── Alerts ────────────────────────────────────────────────────────────────
    {
        "id": "fleet_alert_summary",
        "label": "Fleet Alert Summary",
        "category": "Alerts",
        "description": "Rolling 1-hour alert count and unique vehicles affected, grouped by severity.",
        "supports_since": False,
        "supports_vehicle": False,
        "supports_severity": False,
    },
    {
        "id": "high_alerts",
        "label": "High Severity Alerts",
        "category": "Alerts",
        "description": "Recent high-severity events: engine temperature, speed violations, tire pressure, battery.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": False,
    },
    {
        "id": "medium_alerts",
        "label": "Medium Severity Alerts",
        "category": "Alerts",
        "description": "Events requiring operator attention — medium severity tier.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": False,
    },
    {
        "id": "low_alerts",
        "label": "Low Severity Alerts",
        "category": "Alerts",
        "description": "Informational threshold crossings — low severity tier.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": False,
    },
    {
        "id": "all_alerts",
        "label": "All Alerts",
        "category": "Alerts",
        "description": "All alert events across severity levels. Filter by severity, vehicle, or time window.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": True,
    },
    {
        "id": "alerts_context",
        "label": "Alerts With Context",
        "category": "Alerts",
        "description": "Alerts joined with the telemetry from the 2 minutes before each alert fired.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": True,
    },
    {
        "id": "vehicle_activity",
        "label": "Vehicle Activity (1h)",
        "category": "Alerts",
        "description": "Per-vehicle alert counts by severity for the rolling last hour.",
        "supports_since": False,
        "supports_vehicle": True,
        "supports_severity": True,
    },
    # ── Telemetry ─────────────────────────────────────────────────────────────
    {
        "id": "speed_averages",
        "label": "Speed Averages (5-min)",
        "category": "Telemetry",
        "description": "5-minute tumbling-window average, max, and min speed per vehicle.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": False,
    },
    {
        "id": "fuel_levels",
        "label": "Fuel Levels",
        "category": "Telemetry",
        "description": "Raw fuel percentage readings from fleet telemetry.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": False,
    },
    {
        "id": "high_engine_temp",
        "label": "High Engine Temp",
        "category": "Telemetry",
        "description": "Vehicles with engine temperature above 220°F (pre-filtered by streaming SQL).",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": False,
    },
    {
        "id": "last_positions",
        "label": "Last Known Positions",
        "category": "Telemetry",
        "description": "Latest GPS position and speed for all vehicles.",
        "supports_since": True,
        "supports_vehicle": True,
        "supports_severity": False,
    },
    {
        "id": "boston_region",
        "label": "Boston Region",
        "category": "Telemetry",
        "description": "Vehicles currently inside the Boston metro bounding box (42.2–42.5°N, 71.2–70.9°W).",
        "supports_since": True,
        "supports_vehicle": False,
        "supports_severity": False,
    },
]

# ── SQL safety ────────────────────────────────────────────────────────────────
_SQL_DENY = re.compile(
    r"\b(drop|create|alter|insert|update|delete|truncate|grant|revoke|execute|exec|copy|vacuum|analyze)\b",
    re.IGNORECASE,
)


def is_safe_sql(sql: str) -> bool:
    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        return False
    # Block multiple statements (semicolons outside trailing whitespace)
    if ";" in stripped.rstrip(";"):
        return False
    if _SQL_DENY.search(stripped):
        return False
    return True


def _build_preset_sql(
    preset_id: str,
    since_interval: Optional[str],
    vehicle_id: Optional[str],
    severity: Optional[str],
    limit: int,
) -> tuple[Optional[str], list]:
    """Return (sql_template, params_list) for a given preset, or (None, []) on unknown id."""

    def build_where(conditions: list) -> str:
        return ("WHERE " + " AND ".join(conditions)) if conditions else ""

    params: list = []

    # ── fleet_alert_summary ───────────────────────────────────────────────────
    if preset_id == "fleet_alert_summary":
        sql = (
            "SELECT severity, alert_count, affected_vehicles "
            "FROM fleet_alert_summary "
            "ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
        )
        return sql, params

    # ── high_alerts ───────────────────────────────────────────────────────────
    if preset_id == "high_alerts":
        conds = []
        if since_interval:
            conds.append("occurred_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        sql = (
            "SELECT vehicle_id, event_type, description, occurred_at, solace_topic "
            f"FROM high_severity_alerts {build_where(conds)} "
            "ORDER BY occurred_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── medium_alerts ─────────────────────────────────────────────────────────
    if preset_id == "medium_alerts":
        conds = []
        if since_interval:
            conds.append("occurred_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        sql = (
            "SELECT vehicle_id, event_type, description, occurred_at "
            f"FROM medium_severity_alerts {build_where(conds)} "
            "ORDER BY occurred_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── low_alerts ────────────────────────────────────────────────────────────
    if preset_id == "low_alerts":
        conds = []
        if since_interval:
            conds.append("occurred_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        sql = (
            "SELECT vehicle_id, event_type, description, occurred_at "
            f"FROM low_severity_alerts {build_where(conds)} "
            "ORDER BY occurred_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── all_alerts ────────────────────────────────────────────────────────────
    if preset_id == "all_alerts":
        conds = []
        if since_interval:
            conds.append("occurred_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        if severity:
            conds.append("severity = %s")
            params.append(severity)
        sql = (
            "SELECT vehicle_id, severity, event_type, description, occurred_at, solace_topic "
            f"FROM fleet_events_raw {build_where(conds)} "
            "ORDER BY occurred_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── alerts_context ────────────────────────────────────────────────────────
    if preset_id == "alerts_context":
        conds = []
        if since_interval:
            conds.append("alert_time > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        if severity:
            conds.append("severity = %s")
            params.append(severity)
        sql = (
            "SELECT vehicle_id, event_type, severity, description, alert_time, "
            "ROUND(speed::numeric,1) AS speed_mph, ROUND(fuel_level::numeric,1) AS fuel_pct, "
            "ROUND(engine_temp::numeric,1) AS engine_temp_f, ROUND(tire_pressure::numeric,1) AS psi, "
            "ROUND(battery_voltage::numeric,2) AS volts, time_before_alert "
            f"FROM alerts_with_context {build_where(conds)} "
            "ORDER BY alert_time DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── vehicle_activity ──────────────────────────────────────────────────────
    if preset_id == "vehicle_activity":
        conds = []
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        if severity:
            conds.append("severity = %s")
            params.append(severity)
        sql = (
            "SELECT vehicle_id, severity, event_count "
            f"FROM vehicle_event_counts_1h {build_where(conds)} "
            "ORDER BY event_count DESC, vehicle_id LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── speed_averages ────────────────────────────────────────────────────────
    if preset_id == "speed_averages":
        conds = []
        if since_interval:
            conds.append("window_end > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        sql = (
            "SELECT vehicle_id, window_start, window_end, "
            "ROUND(avg_speed_mph::numeric,1) AS avg_mph, "
            "ROUND(max_speed_mph::numeric,1) AS max_mph, "
            "ROUND(min_speed_mph::numeric,1) AS min_mph, sample_count "
            f"FROM vehicle_speed_5min_avg {build_where(conds)} "
            "ORDER BY window_end DESC, vehicle_id LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── fuel_levels ───────────────────────────────────────────────────────────
    if preset_id == "fuel_levels":
        conds = []
        if since_interval:
            conds.append("recorded_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        sql = (
            "SELECT vehicle_id, ROUND(fuel_pct::numeric,1) AS fuel_pct, recorded_at "
            f"FROM vehicle_fuel_levels {build_where(conds)} "
            "ORDER BY recorded_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── high_engine_temp ──────────────────────────────────────────────────────
    if preset_id == "high_engine_temp":
        conds = []
        if since_interval:
            conds.append("recorded_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        sql = (
            "SELECT vehicle_id, ROUND(engine_temp_f::numeric,1) AS engine_temp_f, recorded_at "
            f"FROM high_engine_temp_vehicles {build_where(conds)} "
            "ORDER BY recorded_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── last_positions ────────────────────────────────────────────────────────
    if preset_id == "last_positions":
        conds = []
        if since_interval:
            conds.append("recorded_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        if vehicle_id:
            conds.append("vehicle_id = %s")
            params.append(vehicle_id)
        sql = (
            "SELECT vehicle_id, ROUND(latitude::numeric,4) AS lat, "
            "ROUND(longitude::numeric,4) AS lon, "
            "ROUND(speed_mph::numeric,1) AS speed_mph, recorded_at "
            f"FROM vehicle_last_known_position {build_where(conds)} "
            "ORDER BY recorded_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    # ── boston_region ─────────────────────────────────────────────────────────
    if preset_id == "boston_region":
        conds = []
        if since_interval:
            conds.append("recorded_at > NOW() - INTERVAL %s")
            params.append(since_interval)
        sql = (
            "SELECT vehicle_id, ROUND(latitude::numeric,4) AS lat, "
            "ROUND(longitude::numeric,4) AS lon, "
            "ROUND(speed_mph::numeric,1) AS speed_mph, recorded_at "
            f"FROM vehicles_in_region_boston {build_where(conds)} "
            "ORDER BY recorded_at DESC LIMIT %s"
        )
        params.append(limit)
        return sql, params

    return None, []


def _run_query_sync(sql: str, params: list, limit: int) -> dict:
    """Execute a SELECT query against RisingWave and return rows + metadata."""
    t0 = time.time()
    try:
        conn = psycopg2.connect(
            host=RW_HOST, port=RW_PORT,
            user="root", database="dev",
            connect_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute(sql, params if params else None)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchmany(limit)]
        conn.close()
        elapsed = int((time.time() - t0) * 1000)
        return {"rows": rows, "count": len(rows), "elapsed_ms": elapsed, "sql": sql, "error": None}
    except Exception as e:
        elapsed = int((time.time() - t0) * 1000)
        return {"rows": [], "count": 0, "elapsed_ms": elapsed, "sql": sql, "error": str(e)}


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

                raw_ts = msg.get_sender_timestamp()
                if raw_ts is not None:
                    ts_str = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc).isoformat()
                else:
                    ts_str = datetime.now(timezone.utc).isoformat()
                item = {"type": "live", "topic": topic, "payload": payload, "ts": ts_str}
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
        "query_presets": QUERY_PRESETS,
    }


class PublishRequest(BaseModel):
    topic: str
    payload: dict


class QueryRequest(BaseModel):
    preset: Optional[str] = None
    sql: Optional[str] = None
    since: Optional[str] = None
    vehicle_id: Optional[str] = None
    severity: Optional[str] = None
    limit: int = 100


@app.post("/publish")
async def publish(req: PublishRequest):
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_executor, _publish_sync, req.topic, req.payload)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Publish failed"))
    return {"ok": True, "topic": req.topic}


@app.post("/query")
async def run_query(req: QueryRequest):
    """Execute an ad-hoc or preset query against RisingWave."""
    limit = max(1, min(req.limit, 1000))

    if req.sql:
        # Ad-hoc SQL — safety check
        if not is_safe_sql(req.sql):
            raise HTTPException(
                status_code=400,
                detail="Only SELECT statements are allowed. DROP/CREATE/ALTER/INSERT/UPDATE/DELETE are not permitted.",
            )
        sql = req.sql.strip().rstrip(";")
        params: list = []
    elif req.preset:
        since_interval = to_pg_interval(req.since) if req.since else None
        sql, params = _build_preset_sql(
            req.preset,
            since_interval,
            req.vehicle_id or None,
            req.severity or None,
            limit,
        )
        if sql is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {req.preset!r}")
    else:
        raise HTTPException(status_code=400, detail="Provide either 'preset' or 'sql'.")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_executor, _run_query_sync, sql, params, limit)

    if result["error"]:
        raise HTTPException(status_code=500, detail=result["error"])

    # Serialize datetime/Decimal before returning
    serialized = json.loads(json.dumps(result["rows"], default=json_default))
    return {
        "rows": serialized,
        "count": result["count"],
        "elapsed_ms": result["elapsed_ms"],
        "sql": result["sql"],
    }


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
                ts = item.get("ts") or payload.get("recorded_at") or payload.get("occurred_at") or payload.get("issued_at")
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
