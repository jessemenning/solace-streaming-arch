#!/usr/bin/env python3
"""
Monitor Solace queue depth during backfill for observability.

Polls SEMP v2 monitor API and prints consumption progress. Independent of
the connector's readiness mechanism -- purely an operator visibility tool.

Usage:
    python3 backfill/monitor_queue.py
    python3 backfill/monitor_queue.py --queue q/risingwave-ingest --interval 2
"""

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")


def monitor(
    host: str,
    vpn: str,
    queue_name: str,
    user: str,
    password: str,
    interval: int = 5,
) -> None:
    """Poll SEMP v2 and print queue consumption progress until the queue empties."""
    encoded_queue = quote(queue_name, safe="")
    url = f"{host}/SEMP/v2/monitor/msgVpns/{vpn}/queues/{encoded_queue}"
    initial_depth = None
    start_time = time.monotonic()

    print(f"Monitoring queue '{queue_name}' on VPN '{vpn}'")
    print(f"SEMP endpoint: {url}")
    print()

    while True:
        try:
            resp = requests.get(url, auth=(user, password), timeout=10)
            resp.raise_for_status()
            data = resp.json()["data"]
            current_depth = data.get("spooledMsgCount", 0)

            if initial_depth is None:
                initial_depth = current_depth
                print(f"Initial queue depth: {initial_depth:,} messages")
                if initial_depth == 0:
                    print("Queue already empty -- nothing to backfill.")
                    return
                print()

            consumed = initial_depth - current_depth
            pct = (consumed / initial_depth * 100) if initial_depth > 0 else 100
            elapsed = time.monotonic() - start_time
            rate = consumed / elapsed if elapsed > 0 else 0

            print(
                f"Depth: {current_depth:>10,}  |  "
                f"Consumed: {consumed:>10,}  |  "
                f"{pct:5.1f}%  |  "
                f"{rate:,.0f} msg/s"
            )

            if current_depth == 0:
                print()
                print(
                    f"Queue empty -- backfill complete. "
                    f"{consumed:,} messages in {elapsed:.0f}s "
                    f"({rate:,.0f} msg/s avg)"
                )
                return

        except requests.RequestException as exc:
            print(f"SEMP error: {exc}", file=sys.stderr)
        except KeyError as exc:
            print(f"Unexpected SEMP response structure: {exc}", file=sys.stderr)

        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor Solace queue depth during backfill",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SOLACE_SEMP", "http://localhost:8180"),
    )
    parser.add_argument(
        "--vpn",
        default=os.environ.get("SOLACE_VPN", "streaming-poc"),
    )
    parser.add_argument(
        "--queue",
        default="q/risingwave-ingest",
        help="Queue name to monitor (default: q/risingwave-ingest)",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("SOLACE_ADMIN_USER", "admin"),
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("SOLACE_ADMIN_PASS", "admin"),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Poll interval in seconds (default: 5)",
    )
    args = parser.parse_args()

    try:
        monitor(
            args.host, args.vpn, args.queue,
            args.user, args.password, args.interval,
        )
    except KeyboardInterrupt:
        print("\n[stopped]", file=sys.stderr)


if __name__ == "__main__":
    main()
