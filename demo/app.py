#!/usr/bin/env python3
"""
Fleet Operations AI — Agentic Demo
Demonstrates: grounding LLM reasoning in real-time streaming data

The AI assistant uses RisingWave materialized views as tools. Each view
is continuously updated via the Solace Platform → RisingWave pipeline.
The agent never processes raw events — it queries pre-aggregated state.

Usage:
    cp .env.template .env   # fill in ANTHROPIC_API_KEY (and optionally LITELLM_BASE_URL)
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8090
"""

import logging
import os
import json
import time
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s  %(message)s")
logger = logging.getLogger("fleet-agent")


def json_default(obj):
    """Handle types psycopg2 returns that stdlib json can't serialize."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

# ── Configuration ──────────────────────────────────────────────────────────────
RW_HOST = os.environ.get("RW_HOST", "localhost")
RW_PORT = int(os.environ.get("RW_PORT", "4566"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "").strip() or None
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


def make_anthropic_client() -> anthropic.Anthropic:
    """Return an Anthropic client, routing through LiteLLM proxy if configured."""
    kwargs: dict = {"api_key": ANTHROPIC_API_KEY}
    if LITELLM_BASE_URL:
        kwargs["base_url"] = LITELLM_BASE_URL
    return anthropic.Anthropic(**kwargs)

app = FastAPI(title="Fleet Operations AI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Mock data (used when RisingWave is not reachable) ─────────────────────────
def _recent(minutes_ago: int) -> str:
    """Return an ISO timestamp `minutes_ago` minutes in the past (for mock data freshness)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()

MOCK = {
    "fleet_alert_summary": [
        {"severity": "high",   "alert_count": 14, "affected_vehicles": 9},
        {"severity": "medium", "alert_count":  8, "affected_vehicles": 6},
        {"severity": "low",    "alert_count":  5, "affected_vehicles": 4},
    ],
    "high_severity_alerts": [
        {"vehicle_id": "vehicle_005", "event_type": "high_engine_temp",      "description": "Engine temperature exceeded 240°F",         "occurred_at": _recent(5)},
        {"vehicle_id": "vehicle_012", "event_type": "tire_pressure_warning", "description": "Front-left tire pressure dropped to 24 PSI", "occurred_at": _recent(6)},
        {"vehicle_id": "vehicle_018", "event_type": "high_engine_temp",      "description": "Engine temperature exceeded 235°F",         "occurred_at": _recent(7)},
        {"vehicle_id": "vehicle_005", "event_type": "speed_limit_exceeded",  "description": "Speed reached 92 mph (limit: 75 mph)",       "occurred_at": _recent(8)},
        {"vehicle_id": "vehicle_007", "event_type": "battery_voltage_low",   "description": "Battery voltage dropped to 11.2V",           "occurred_at": _recent(9)},
    ],
    "vehicle_speed_5min_avg": [
        {"vehicle_id": "vehicle_001", "avg_mph": 58.3, "max_mph": 71.0, "sample_count": 30, "window_end": _recent(2)},
        {"vehicle_id": "vehicle_002", "avg_mph": 62.1, "max_mph": 68.5, "sample_count": 29, "window_end": _recent(2)},
        {"vehicle_id": "vehicle_005", "avg_mph": 79.4, "max_mph": 92.0, "sample_count": 31, "window_end": _recent(2)},
        {"vehicle_id": "vehicle_012", "avg_mph": 45.2, "max_mph": 55.0, "sample_count": 28, "window_end": _recent(2)},
        {"vehicle_id": "vehicle_018", "avg_mph": 67.8, "max_mph": 74.0, "sample_count": 30, "window_end": _recent(2)},
    ],
    "alerts_with_context": [
        {"vehicle_id": "vehicle_005", "event_type": "high_engine_temp", "severity": "high", "description": "Engine temp 240°F",
         "alert_time": _recent(5), "speed": 89.2, "fuel_level": 62.1, "engine_temp": 238.5, "tire_pressure": 32.0, "battery_voltage": 13.1, "time_before_alert": "00:01:45"},
        {"vehicle_id": "vehicle_012", "event_type": "tire_pressure_warning", "severity": "high", "description": "Low tire pressure",
         "alert_time": _recent(6), "speed": 48.3, "fuel_level": 71.4, "engine_temp": 198.2, "tire_pressure": 26.1, "battery_voltage": 12.8, "time_before_alert": "00:02:00"},
    ],
    "vehicles_in_region_boston": [
        {"vehicle_id": "vehicle_003", "lat": 42.3601, "lon": -71.0589, "speed_mph": 28.4, "recorded_at": _recent(3)},
        {"vehicle_id": "vehicle_011", "lat": 42.3481, "lon": -71.0812, "speed_mph": 18.2, "recorded_at": _recent(4)},
        {"vehicle_id": "vehicle_017", "lat": 42.3315, "lon": -71.0204, "speed_mph": 33.7, "recorded_at": _recent(5)},
    ],
    "vehicle_event_counts_1h": [
        {"vehicle_id": "vehicle_005", "severity": "high",   "event_count": 7},
        {"vehicle_id": "vehicle_005", "severity": "medium", "event_count": 3},
        {"vehicle_id": "vehicle_012", "severity": "high",   "event_count": 4},
        {"vehicle_id": "vehicle_018", "severity": "high",   "event_count": 3},
        {"vehicle_id": "vehicle_007", "severity": "high",   "event_count": 2},
        {"vehicle_id": "vehicle_001", "severity": "low",    "event_count": 1},
    ],
}

# ── RisingWave connection pool ────────────────────────────────────────────────
_rw_pool = None

def _get_pool():
    global _rw_pool
    if _rw_pool is None:
        _rw_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=10,
            host=RW_HOST, port=RW_PORT,
            user="root", database="dev",
            connect_timeout=2,
        )
    return _rw_pool


def rw_query(sql: str, params=None, limit: int = 20) -> tuple[list[dict], bool]:
    """Returns (rows, is_live). Falls back to mock data on connection failure."""
    conn = None
    try:
        conn = _get_pool().getconn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchmany(limit)
        return ([dict(r) for r in rows], True)
    except Exception as exc:
        logger.warning("RisingWave query failed: %s", exc)
        return ([], False)
    finally:
        if conn is not None:
            try:
                _get_pool().putconn(conn)
            except Exception:
                pass


def run_tool(name: str, args: dict) -> tuple[list[dict], bool]:
    """Execute a tool and return (result, is_live)."""
    queries = {
        "get_fleet_alert_summary": lambda a: rw_query(
            "SELECT severity, alert_count, affected_vehicles FROM fleet_alert_summary "
            "ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
        ),
        "get_high_severity_alerts": lambda a: rw_query(
            "SELECT vehicle_id, event_type, description, occurred_at "
            "FROM high_severity_alerts ORDER BY occurred_at DESC LIMIT %s",
            params=(min(a.get("limit", 10), 20),)
        ),
        "get_vehicle_speed_averages": lambda a: rw_query(
            "SELECT vehicle_id, ROUND(avg_speed_mph::numeric,1) AS avg_mph, "
            "ROUND(max_speed_mph::numeric,1) AS max_mph, sample_count, window_end "
            "FROM vehicle_speed_5min_avg ORDER BY window_end DESC, vehicle_id LIMIT 20"
        ),
        "get_alerts_with_context": lambda a: rw_query(
            "SELECT vehicle_id, event_type, severity, description, alert_time, "
            "ROUND(speed::numeric,1) AS speed, ROUND(fuel_level::numeric,1) AS fuel_level, "
            "ROUND(engine_temp::numeric,1) AS engine_temp, ROUND(tire_pressure::numeric,1) AS tire_pressure, "
            "ROUND(battery_voltage::numeric,2) AS battery_voltage, time_before_alert "
            "FROM alerts_with_context ORDER BY alert_time DESC LIMIT %s",
            params=(min(a.get("limit", 10), 15),)
        ),
        "get_vehicles_in_boston": lambda a: rw_query(
            "SELECT vehicle_id, ROUND(latitude::numeric,4) AS lat, "
            "ROUND(longitude::numeric,4) AS lon, ROUND(speed_mph::numeric,1) AS speed_mph, "
            "recorded_at FROM vehicles_in_region_boston ORDER BY recorded_at DESC"
        ),
        "get_vehicle_activity": lambda a: rw_query(
            "SELECT vehicle_id, severity, event_count "
            "FROM vehicle_event_counts_1h ORDER BY event_count DESC, vehicle_id LIMIT 30"
        ),
        "get_vehicle_details": lambda a: rw_query(
            "SELECT vehicle_id, event_type, severity, description, occurred_at "
            "FROM high_severity_alerts WHERE vehicle_id = %s ORDER BY occurred_at DESC LIMIT 10",
            params=(a.get("vehicle_id"),)
        ),
    }

    fn = queries.get(name)
    if not fn:
        return [{"error": f"Unknown tool: {name}"}], False

    rows, is_live = fn(args)

    # Fall back to mock data if RisingWave is unreachable
    if not is_live:
        mock_key = {
            "get_fleet_alert_summary": "fleet_alert_summary",
            "get_high_severity_alerts": "high_severity_alerts",
            "get_vehicle_speed_averages": "vehicle_speed_5min_avg",
            "get_alerts_with_context": "alerts_with_context",
            "get_vehicles_in_boston": "vehicles_in_region_boston",
            "get_vehicle_activity": "vehicle_event_counts_1h",
            "get_vehicle_details": "high_severity_alerts",
        }.get(name)
        data = MOCK.get(mock_key, [])
        if name == "get_vehicle_details" and args.get("vehicle_id"):
            data = [r for r in data if r.get("vehicle_id") == args["vehicle_id"]]
        elif name == "get_high_severity_alerts":
            data = data[:args.get("limit", 10)]
        return data, False

    return rows, True


# ── Tool definitions for Claude ───────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_fleet_alert_summary",
        "description": (
            "Get a real-time summary of fleet-wide alerts grouped by severity "
            "(high/medium/low) for the rolling last hour. Returns alert counts "
            "and unique vehicles affected. Use this first for any fleet status question."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_high_severity_alerts",
        "description": (
            "Get the most recent high-severity vehicle alerts. Returns vehicle_id, "
            "event_type (e.g. high_engine_temp, tire_pressure_warning, speed_limit_exceeded), "
            "human-readable description, and timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max alerts to return (default 10)"},
            },
        },
    },
    {
        "name": "get_vehicle_speed_averages",
        "description": (
            "Get 5-minute tumbling-window average speeds for all vehicles. "
            "Returns avg_mph, max_mph, sample_count, and window timestamp. "
            "This aggregation has no Solace topic equivalent — it requires streaming SQL."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_alerts_with_context",
        "description": (
            "Get recent alerts with correlated sensor telemetry from the 2 minutes "
            "before each alert. This is a live stream-stream join — shows what the "
            "vehicle was doing just before the alert fired."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
        },
    },
    {
        "name": "get_vehicles_in_boston",
        "description": (
            "Get vehicles currently inside the Boston metro area "
            "(42.2–42.5°N, 71.2–70.9°W). Live spatial filter on GPS coordinates. "
            "No equivalent Solace subscription — requires SQL spatial predicate."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_vehicle_activity",
        "description": (
            "Get per-vehicle alert counts by severity for the rolling last hour. "
            "Useful for identifying which vehicles have the most issues."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_vehicle_details",
        "description": "Get recent alerts for a specific vehicle by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle_id": {"type": "string", "description": "e.g. vehicle_005"},
            },
            "required": ["vehicle_id"],
        },
    },
]

NUM_VEHICLES = int(os.environ.get("NUM_VEHICLES", "10"))

SYSTEM_PROMPT = f"""You are the Fleet Operations AI for a real-time vehicle monitoring system.

You have access to LIVE data through streaming materialized views. Each tool query returns data that is continuously updated from {NUM_VEHICLES} IoT vehicles publishing telemetry and alerts via a Solace Platform → RisingWave pipeline (no intermediary broker or connector code required).

The key insight behind this architecture: instead of subscribing to raw event topics, you query pre-computed views. Aggregations like 5-minute speed averages, stream-stream joins for correlated context, and spatial geofence queries are impossible with pub/sub alone — they're expressed here as SQL and updated in real time.

When answering:
- Lead with what matters operationally (safety issues, anomalies, vehicles needing attention)
- Be concise — operators need fast answers, not essays
- Reference specific vehicle IDs, numbers, and timestamps from tool results
- When you see a concerning pattern (e.g. a vehicle appearing in multiple alert types), surface it
- Note when data is from the live stack vs simulated demo data (the tool results will indicate this)"""


# ── API ───────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    messages: list[dict]


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text()


@app.get("/fleet-stats")
async def fleet_stats():
    """Live fleet summary for the header stats bar."""
    rows, is_live = run_tool("get_fleet_alert_summary", {})
    stats = {r["severity"]: r for r in rows if "error" not in r}
    return {
        "high":      stats.get("high",   {}).get("alert_count", 0),
        "medium":    stats.get("medium", {}).get("alert_count", 0),
        "low":       stats.get("low",    {}).get("alert_count", 0),
        "vehicles":  NUM_VEHICLES,
        "live":      is_live,
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, detail="ANTHROPIC_API_KEY environment variable not set")

    client = make_anthropic_client()
    messages = list(req.messages)

    async def generate():
        nonlocal messages

        while True:
            collected_content: list[dict] = []
            tool_calls: list[dict] = []
            current_tool: dict | None = None
            current_input_buf = ""

            with client.messages.stream(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            ) as stream:
                for event in stream:
                    etype = event.type

                    if etype == "content_block_start":
                        block = event.content_block
                        if block.type == "text":
                            collected_content.append({"type": "text", "text": ""})
                        elif block.type == "tool_use":
                            current_tool = {"id": block.id, "name": block.name, "input": {}}
                            current_input_buf = ""
                            yield f"data: {json.dumps({'type': 'tool_start', 'name': block.name})}\n\n"

                    elif etype == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            text = delta.text
                            if collected_content and collected_content[-1]["type"] == "text":
                                collected_content[-1]["text"] += text
                            yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
                        elif delta.type == "input_json_delta":
                            current_input_buf += delta.partial_json

                    elif etype == "content_block_stop":
                        if current_tool is not None:
                            try:
                                current_tool["input"] = json.loads(current_input_buf) if current_input_buf else {}
                            except Exception:
                                current_tool["input"] = {}
                            tool_calls.append(dict(current_tool))
                            collected_content.append({
                                "type": "tool_use",
                                "id": current_tool["id"],
                                "name": current_tool["name"],
                                "input": current_tool["input"],
                            })
                            current_tool = None
                            current_input_buf = ""

            # Append assistant turn
            messages = messages + [{"role": "assistant", "content": collected_content}]

            if not tool_calls:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break

            # Execute tools and stream results to frontend
            tool_results = []
            for tc in tool_calls:
                data, is_live = run_tool(tc["name"], tc["input"])
                result_str = json.dumps(data, default=json_default)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": result_str,
                })
                yield f"data: {json.dumps({'type': 'tool_result', 'name': tc['name'], 'input': tc['input'], 'rows': data, 'live': is_live}, default=json_default)}\n\n"

            messages = messages + [{"role": "user", "content": tool_results}]

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
