#!/usr/bin/env python3
"""
Event-driven, backfill-aware RisingWave query client.

Readiness notification arrives via Solace event -- instant, no polling.
Falls back to a status table for consumers that start after the event.

Pattern:
  1. Check rw_solace_connector_status table (handles late joiners)
  2. If not ready, subscribe to system/risingwave/connector/*/ready
  3. On event: system is ready -- query materialized views with confidence
  4. For new MVs created after system is live, use query_new_view() (client-side WAIT)

Usage:
    from backfill.query_client import RisingWaveClient

    client = RisingWaveClient()
    client.wait_for_ready()
    rows = client.query("SELECT * FROM high_severity_alerts LIMIT 10")
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.errors
import psycopg2.extras
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")


class RisingWaveClient:
    """
    Event-driven, backfill-aware RisingWave query client.

    Readiness is signaled via Solace event (primary) or status table (fallback).
    No polling loop. Zero RisingWave load while waiting.
    """

    def __init__(
        self,
        rw_host: Optional[str] = None,
        rw_port: Optional[int] = None,
        rw_user: str = "root",
        rw_database: str = "dev",
        solace_host: Optional[str] = None,
        solace_vpn: Optional[str] = None,
        solace_user: Optional[str] = None,
        solace_pass: Optional[str] = None,
        connector_name: str = "solace_events",
        auto_connect: bool = True,
    ):
        self._rw_host = rw_host or os.environ.get("RW_HOST", "localhost")
        self._rw_port = rw_port or int(os.environ.get("RW_PORT", "4566"))
        self._rw_user = rw_user
        self._rw_database = rw_database
        self._solace_host = solace_host or os.environ.get(
            "SOLACE_HOST", "tcp://localhost:55554"
        )
        self._solace_vpn = solace_vpn or os.environ.get("SOLACE_VPN", "streaming-poc")
        self._solace_user = solace_user or os.environ.get(
            "SOLACE_USER", "streaming-user"
        )
        self._solace_pass = solace_pass or os.environ.get("SOLACE_PASSWORD", "default")
        self._connector_name = connector_name

        self._ready = threading.Event()
        self._readiness_metadata: Optional[Dict[str, Any]] = None
        self._solace_service = None
        self._solace_receiver = None
        self.conn: Optional[psycopg2.extensions.connection] = None

        if auto_connect:
            self._connect_rw()
            if self._check_status_table():
                self._ready.set()
            else:
                self._setup_solace_subscription()

    # ── RisingWave connection ──────────────────────────────────────────────

    def _connect_rw(self) -> None:
        """Establish RisingWave connection."""
        self.conn = psycopg2.connect(
            host=self._rw_host,
            port=self._rw_port,
            user=self._rw_user,
            dbname=self._rw_database,
        )
        self.conn.autocommit = True

    # ── Readiness detection ────────────────────────────────────────────────

    def _check_status_table(self) -> bool:
        """
        Fallback for consumers that start after the readiness event was published.

        Single check on startup -- not a polling loop. If the status table
        shows is_ready = TRUE, the consumer proceeds immediately.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT is_ready, sentinel_detected_at,
                           events_before_sentinel, total_events_consumed
                    FROM rw_solace_connector_status
                    WHERE connector_name = %s AND is_ready = TRUE
                    """,
                    (self._connector_name,),
                )
                row = cur.fetchone()
                if row:
                    self._readiness_metadata = {
                        "sentinel_detected_at": row[1],
                        "historical_events": row[2],
                        "total_events": row[3],
                        "source": "status_table",
                    }
                    return True
        except psycopg2.errors.UndefinedTable:
            # Table not yet created by connector -- expected on first run
            # Reset the connection since the error leaves it in a bad state
            self.conn.rollback()
        except psycopg2.Error:
            self.conn.rollback()
        return False

    def _setup_solace_subscription(self) -> None:
        """Subscribe to the connector readiness event on Solace."""
        # Lazy import -- only needed when waiting for readiness
        from solace.messaging.messaging_service import MessagingService
        from solace.messaging.resources.topic_subscription import TopicSubscription

        config = {
            "solace.messaging.transport.host": self._solace_host,
            "solace.messaging.service.vpn-name": self._solace_vpn,
            "solace.messaging.authentication.scheme.basic.username": self._solace_user,
            "solace.messaging.authentication.scheme.basic.password": self._solace_pass,
        }

        self._solace_service = (
            MessagingService.builder().from_properties(config).build()
        )
        self._solace_service.connect()

        self._solace_receiver = (
            self._solace_service.create_direct_message_receiver_builder()
            .with_subscriptions(
                [TopicSubscription.of("system/risingwave/connector/*/ready")]
            )
            .build()
        )
        self._solace_receiver.start()

        # Start async receive in background thread
        def _listener():
            while not self._ready.is_set():
                msg = self._solace_receiver.receive_message(timeout=500)
                if msg is None:
                    continue
                try:
                    raw = msg.get_payload_as_string()
                    if raw is None:
                        raw_bytes = msg.get_payload_as_bytes()
                        raw = raw_bytes.decode("utf-8") if raw_bytes else "{}"
                    payload = json.loads(raw)
                    self._readiness_metadata = {
                        "sentinel_detected_at": payload.get(
                            "sentinel_detected_at"
                        ),
                        "historical_events": payload.get(
                            "historical_events_processed"
                        ),
                        "total_events": payload.get("total_events_consumed"),
                        "source": "solace_event",
                    }
                    self._ready.set()
                except Exception as exc:
                    print(f"[query_client] Error processing readiness event: {exc}")

        t = threading.Thread(target=_listener, daemon=True, name="readiness-listener")
        t.start()

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """Check if the system has signaled readiness (non-blocking)."""
        return self._ready.is_set()

    @property
    def readiness_metadata(self) -> Optional[Dict[str, Any]]:
        """Metadata from the readiness signal (None if not yet ready)."""
        return self._readiness_metadata

    def wait_for_ready(self, timeout: float = 600.0) -> Dict[str, Any]:
        """
        Block until the connector signals readiness.

        Notification arrives via:
          - Status table (single check on startup, fallback for late joiners)
          - Solace event (instant, primary path for live waiters)

        No polling. Uses threading.Event for zero-cost blocking wait.
        Zero load on RisingWave while waiting.

        Returns readiness metadata dict on success.
        Raises TimeoutError if not ready within *timeout* seconds.
        """
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError(
                f"Connector '{self._connector_name}' not ready within {timeout}s. "
                f"Check Solace queue depth and connector logs."
            )

        meta = self._readiness_metadata
        hist = meta.get("historical_events")
        src = meta.get("source", "unknown")
        count_str = f"{hist:,}" if isinstance(hist, int) else str(hist)
        print(
            f"System ready: {count_str} historical events processed "
            f"(notified via {src})"
        )
        return meta

    def query(
        self,
        sql: str,
        params: Optional[tuple] = None,
        as_dict: bool = False,
    ) -> List:
        """
        Query a materialized view.

        Call wait_for_ready() once before first query. After that, all queries
        reflect complete history + live data. No WAIT needed -- the connector
        flushed the barrier before signaling ready.

        Args:
            sql: SQL query string (parameterized with %s placeholders).
            params: Query parameters.
            as_dict: If True, return list of dicts instead of tuples.

        Returns:
            List of rows (tuples by default, dicts if as_dict=True).
        """
        factory = (
            psycopg2.extras.RealDictCursor if as_dict else None
        )
        with self.conn.cursor(cursor_factory=factory) as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def query_new_view(
        self,
        sql: str,
        params: Optional[tuple] = None,
        flush_timeout_ms: int = 30000,
        as_dict: bool = False,
    ) -> List:
        """
        Query a materialized view created AFTER the system went live.

        New MVs are backfilled from RisingWave's internal state. WAIT ensures
        the new MV is fully populated before querying.

        Only needed for post-startup MVs. Views that existed during backfill
        should use query() instead.

        Args:
            sql: SQL query string.
            params: Query parameters.
            flush_timeout_ms: Max time to wait for barrier flush (default 30s).
            as_dict: If True, return list of dicts.

        Returns:
            List of rows.
        """
        factory = (
            psycopg2.extras.RealDictCursor if as_dict else None
        )
        with self.conn.cursor(cursor_factory=factory) as cur:
            cur.execute(
                f"SET streaming_flush_wait_timeout_ms = {int(flush_timeout_ms)}"
            )
            cur.execute("WAIT")
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def close(self) -> None:
        """Disconnect from RisingWave and Solace."""
        if self._solace_receiver is not None:
            try:
                self._solace_receiver.terminate()
            except Exception:
                pass
        if self._solace_service is not None:
            try:
                self._solace_service.disconnect()
            except Exception:
                pass
        if self.conn is not None and not self.conn.closed:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ── Example usage ──────────────────────────────────────────────────────────


def _example_agent_main() -> None:
    """Reference pattern for a SAM agent."""
    import time

    with RisingWaveClient() as client:
        # Block until readiness event arrives (or status table shows ready).
        # Instant notification via Solace -- no polling.
        client.wait_for_ready()

        # Query with confidence -- full history reflected
        alerts = client.query(
            "SELECT * FROM high_severity_alerts "
            "ORDER BY event_timestamp DESC LIMIT 10"
        )
        print(f"Top alerts: {len(alerts)} rows")

        # Live query loop
        while True:
            speeds = client.query(
                "SELECT vehicle_id, avg_speed FROM vehicle_speed_5min_avg "
                "ORDER BY window_end DESC LIMIT 5"
            )
            for row in speeds:
                print(f"  {row[0]}: {row[1]:.1f} mph")

            summary = client.query("SELECT * FROM fleet_alert_summary")
            for row in summary:
                print(f"  {row[0]} severity: {row[1]} alerts across {row[2]} vehicles")

            time.sleep(5)


def _example_new_view() -> None:
    """Reference pattern for creating an MV after backfill is complete."""
    with RisingWaveClient() as client:
        client.wait_for_ready()

        # Create a new MV hours after the system went live
        with client.conn.cursor() as cur:
            cur.execute("""
                CREATE MATERIALIZED VIEW IF NOT EXISTS fuel_alerts AS
                SELECT vehicle_id, value AS fuel_level, event_timestamp
                FROM solace_events
                WHERE metric_type = 'fuel_level' AND value < 10.0
            """)

        # query_new_view issues WAIT to ensure the new MV is populated
        results = client.query_new_view(
            "SELECT * FROM fuel_alerts LIMIT 10"
        )
        print(f"Fuel alerts: {len(results)} rows")

        # After the first query_new_view, subsequent queries use query()
        results = client.query("SELECT * FROM fuel_alerts LIMIT 10")
        print(f"Fuel alerts (cached): {len(results)} rows")


if __name__ == "__main__":
    _example_agent_main()
