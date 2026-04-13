"""
Lightweight backfill status checker for web UIs.

Checks the rw_solace_connector_status table without requiring the Solace SDK.
Both the Try Me and Fleet Agent backends import this for readiness checks.
"""

import logging
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.errors

logger = logging.getLogger(__name__)


def check_backfill_status(
    rw_host: str = "localhost",
    rw_port: int = 4566,
    rw_user: str = "root",
    rw_database: str = "dev",
    connector_name: Optional[str] = None,
) -> dict:
    """
    Check the rw_solace_connector_status table for connector readiness.

    Returns a dict with:
        ready: bool — True if all connectors (or specified connector) are ready
        connectors: list — status of each connector
        table_exists: bool — whether the status table exists at all

    If the table doesn't exist, returns ready=True (no backfill configured,
    system is assumed to be running in live-only mode).
    """
    try:
        conn = psycopg2.connect(
            host=rw_host,
            port=rw_port,
            user=rw_user,
            dbname=rw_database,
            connect_timeout=3,
        )
        conn.autocommit = True
    except psycopg2.Error as exc:
        logger.debug("RisingWave unreachable for status check: %s", exc)
        return {
            "ready": False,
            "connectors": [],
            "table_exists": False,
            "error": f"RisingWave unreachable: {exc}",
        }

    try:
        with conn.cursor() as cur:
            if connector_name:
                cur.execute(
                    """
                    SELECT connector_name, is_ready, sentinel_detected_at,
                           events_before_sentinel, total_events_consumed, last_updated
                    FROM rw_solace_connector_status
                    WHERE connector_name = %s
                    """,
                    (connector_name,),
                )
            else:
                cur.execute(
                    """
                    SELECT connector_name, is_ready, sentinel_detected_at,
                           events_before_sentinel, total_events_consumed, last_updated
                    FROM rw_solace_connector_status
                    """
                )

            rows = cur.fetchall()
            if not rows:
                # Table exists but no rows — connector hasn't written status yet
                return {
                    "ready": False,
                    "connectors": [],
                    "table_exists": True,
                    "message": "Connector has not reported status yet",
                }

            connectors = []
            all_ready = True
            for row in rows:
                entry = {
                    "connector_name": row[0],
                    "is_ready": row[1],
                    "sentinel_detected_at": (
                        row[2].isoformat() if row[2] else None
                    ),
                    "events_before_sentinel": row[3],
                    "total_events_consumed": row[4],
                    "last_updated": (
                        row[5].isoformat() if row[5] else None
                    ),
                }
                connectors.append(entry)
                if not row[1]:
                    all_ready = False

            return {
                "ready": all_ready,
                "connectors": connectors,
                "table_exists": True,
            }

    except psycopg2.errors.UndefinedTable:
        # Table doesn't exist — no backfill configured, system is live-only
        conn.rollback()
        return {
            "ready": True,
            "connectors": [],
            "table_exists": False,
            "message": "No backfill status table — running in live-only mode",
        }
    except psycopg2.Error as exc:
        logger.warning("Status table query failed: %s", exc)
        return {
            "ready": True,
            "connectors": [],
            "table_exists": False,
            "error": str(exc),
        }
    finally:
        conn.close()
