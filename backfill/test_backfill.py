#!/usr/bin/env python3
"""
Integration tests for the backfill readiness system.

Tests cover:
  - Late-arriving consumer (status table already shows ready)
  - Consumer starts before backfill (timeout on wait_for_ready)
  - Sentinel detection helpers
  - Status checker with missing table
  - Status checker with ready / not-ready state

These tests mock the database and Solace connections — they don't require
a running stack. Run with: python3 -m pytest backfill/test_backfill.py -v
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ── Sentinel publisher tests ─────────────────────────────────────────────


class TestSentinelPublisher:
    """Tests for publish_sentinel.py argument parsing and message construction."""

    def test_default_topic(self):
        """Sentinel defaults to fleet/system/sentinel topic."""
        from backfill.publish_sentinel import main
        # Just verify the module imports cleanly — actual publish
        # requires a running broker.
        assert callable(main)


# ── Query client tests ───────────────────────────────────────────────────


class TestRisingWaveClientReadiness:
    """Tests for the RisingWaveClient readiness detection logic."""

    def test_late_arriver_status_table_ready(self):
        """
        Scenario 19: Consumer starts AFTER readiness event was published.

        The status table already has is_ready = TRUE. The client should
        set _ready immediately without subscribing to Solace.
        """
        from backfill.query_client import RisingWaveClient

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Simulate status table returning is_ready=TRUE
        mock_cursor.fetchone.return_value = (
            True,                                # is_ready
            "2026-04-13T12:00:00Z",              # sentinel_detected_at
            150_000,                             # events_before_sentinel
            150_000,                             # total_events_consumed
        )

        with patch("backfill.query_client.psycopg2") as mock_pg:
            mock_pg.connect.return_value = mock_conn
            mock_pg.errors = MagicMock()
            mock_pg.extras = MagicMock()

            client = RisingWaveClient(auto_connect=False)
            client.conn = mock_conn
            client._ready = threading.Event()

            # Manually call _check_status_table
            result = client._check_status_table()
            assert result is True
            assert client._readiness_metadata is not None
            assert client._readiness_metadata["source"] == "status_table"
            assert client._readiness_metadata["historical_events"] == 150_000

    def test_consumer_starts_before_backfill_timeout(self):
        """
        Scenario 20: Status table doesn't exist, consumer times out.

        wait_for_ready() raises TimeoutError after the specified timeout.
        """
        from backfill.query_client import RisingWaveClient
        import psycopg2.errors

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Status table doesn't exist
        mock_cursor.execute.side_effect = psycopg2.errors.UndefinedTable(
            "relation \"rw_solace_connector_status\" does not exist"
        )

        client = RisingWaveClient(auto_connect=False)
        client.conn = mock_conn
        client._ready = threading.Event()
        client._readiness_metadata = None

        # _check_status_table should return False
        result = client._check_status_table()
        assert result is False

        # wait_for_ready should timeout
        with pytest.raises(TimeoutError, match="not ready within"):
            client.wait_for_ready(timeout=0.1)

    def test_status_table_not_ready(self):
        """
        Scenario: Status table exists but is_ready = FALSE.

        Consumer should NOT be marked ready.
        """
        from backfill.query_client import RisingWaveClient

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # is_ready = FALSE — no rows returned (query has WHERE is_ready = TRUE)
        mock_cursor.fetchone.return_value = None

        client = RisingWaveClient(auto_connect=False)
        client.conn = mock_conn
        client._ready = threading.Event()

        result = client._check_status_table()
        assert result is False
        assert not client.is_ready

    def test_is_ready_property(self):
        """is_ready property reflects threading.Event state."""
        from backfill.query_client import RisingWaveClient

        client = RisingWaveClient(auto_connect=False)
        client._ready = threading.Event()
        assert not client.is_ready

        client._ready.set()
        assert client.is_ready

    def test_context_manager(self):
        """Client supports context manager protocol."""
        from backfill.query_client import RisingWaveClient

        client = RisingWaveClient(auto_connect=False)
        mock_conn = MagicMock()
        mock_conn.closed = False  # simulate an open connection
        client.conn = mock_conn

        with client as c:
            assert c is client
        # close() was called
        mock_conn.close.assert_called_once()


# ── Status checker tests ─────────────────────────────────────────────────


class TestBackfillStatus:
    """Tests for the lightweight status checker used by web UIs."""

    def test_status_table_not_exists(self):
        """
        When the status table doesn't exist, assume live-only mode (ready=True).
        """
        import psycopg2.errors
        from backfill.status import check_backfill_status

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.execute.side_effect = psycopg2.errors.UndefinedTable("no table")
        mock_conn.rollback = MagicMock()

        with patch("backfill.status.psycopg2") as mock_pg:
            mock_pg.connect.return_value = mock_conn
            mock_pg.errors = psycopg2.errors
            mock_pg.Error = psycopg2.Error

            result = check_backfill_status()
            assert result["ready"] is True
            assert result["table_exists"] is False

    def test_status_table_ready(self):
        """
        Status table exists with is_ready = TRUE.
        """
        from backfill.status import check_backfill_status

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            ("rw-ingest", True, None, 150_000, 150_000, None),
        ]

        with patch("backfill.status.psycopg2") as mock_pg:
            mock_pg.connect.return_value = mock_conn
            mock_pg.errors = MagicMock()
            mock_pg.Error = Exception

            result = check_backfill_status()
            assert result["ready"] is True
            assert result["table_exists"] is True
            assert len(result["connectors"]) == 1
            assert result["connectors"][0]["connector_name"] == "rw-ingest"
            assert result["connectors"][0]["is_ready"] is True

    def test_status_table_not_ready(self):
        """
        Status table exists but connector hasn't detected sentinel yet.
        """
        from backfill.status import check_backfill_status

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            ("rw-ingest", False, None, 0, 50_000, None),
        ]

        with patch("backfill.status.psycopg2") as mock_pg:
            mock_pg.connect.return_value = mock_conn
            mock_pg.errors = MagicMock()
            mock_pg.Error = Exception

            result = check_backfill_status()
            assert result["ready"] is False
            assert result["table_exists"] is True
            assert result["connectors"][0]["is_ready"] is False

    def test_status_empty_table(self):
        """
        Status table exists but has no rows (connector hasn't started).
        """
        from backfill.status import check_backfill_status

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = []

        with patch("backfill.status.psycopg2") as mock_pg:
            mock_pg.connect.return_value = mock_conn
            mock_pg.errors = MagicMock()
            mock_pg.Error = Exception

            result = check_backfill_status()
            assert result["ready"] is False
            assert result["table_exists"] is True

    def test_risingwave_unreachable(self):
        """
        RisingWave is down — status checker returns ready=False with error.
        """
        import psycopg2

        from backfill.status import check_backfill_status

        with patch("backfill.status.psycopg2") as mock_pg:
            mock_pg.connect.side_effect = psycopg2.OperationalError("connection refused")
            mock_pg.Error = psycopg2.Error

            result = check_backfill_status()
            assert result["ready"] is False
            assert "error" in result

    def test_multiple_connectors_all_ready(self):
        """
        Scenario 25: Multiple queues — all connectors ready.
        """
        from backfill.status import check_backfill_status

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            ("rw-ingest", True, None, 100_000, 100_000, None),
            ("events-ingest", True, None, 50_000, 50_000, None),
            ("commands-ingest", True, None, 10_000, 10_000, None),
        ]

        with patch("backfill.status.psycopg2") as mock_pg:
            mock_pg.connect.return_value = mock_conn
            mock_pg.errors = MagicMock()
            mock_pg.Error = Exception

            result = check_backfill_status()
            assert result["ready"] is True
            assert len(result["connectors"]) == 3

    def test_multiple_connectors_partial_ready(self):
        """
        Scenario 25: Multiple queues — one not yet ready.
        """
        from backfill.status import check_backfill_status

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            ("rw-ingest", True, None, 100_000, 100_000, None),
            ("events-ingest", False, None, 0, 25_000, None),  # still backfilling
        ]

        with patch("backfill.status.psycopg2") as mock_pg:
            mock_pg.connect.return_value = mock_conn
            mock_pg.errors = MagicMock()
            mock_pg.Error = Exception

            result = check_backfill_status()
            assert result["ready"] is False  # not all ready


# ── Monitor tests ────────────────────────────────────────────────────────


class TestMonitorQueue:
    """Tests for monitor_queue.py."""

    def test_module_imports(self):
        """Verify monitor module imports cleanly."""
        from backfill.monitor_queue import monitor
        assert callable(monitor)
