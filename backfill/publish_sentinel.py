#!/usr/bin/env python3
"""
Publish a sentinel message to mark the boundary between historical and live events.

Run AFTER historical data has accumulated and BEFORE starting the connector.
The sentinel is identified by Solace user property 'x-solace-sentinel' — the
connector intercepts it and never emits it to the RisingWave source table.

Usage:
    python3 backfill/publish_sentinel.py
    python3 backfill/publish_sentinel.py --topic fleet/system/sentinel
    python3 backfill/publish_sentinel.py --host tcp://solace:55555 --vpn streaming-poc
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from solace.messaging.messaging_service import MessagingService
from solace.messaging.resources.topic import Topic

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")


def publish_sentinel(
    host: str,
    vpn: str,
    username: str,
    password: str,
    topic: str,
) -> None:
    """Publish a single sentinel message with the x-solace-sentinel user property."""
    config = {
        "solace.messaging.transport.host": host,
        "solace.messaging.service.vpn-name": vpn,
        "solace.messaging.authentication.scheme.basic.username": username,
        "solace.messaging.authentication.scheme.basic.password": password,
    }

    service = MessagingService.builder().from_properties(config).build()
    service.connect()

    publisher = service.create_persistent_message_publisher_builder().build()
    publisher.start()

    now = datetime.now(timezone.utc)
    sentinel_payload = json.dumps({
        "message": (
            "Backfill boundary marker. "
            "Intercepted by connector, never reaches RisingWave."
        ),
        "published_at": now.isoformat(),
    })

    # Sentinel identified by Solace user property, not payload content.
    message = (
        service.message_builder()
        .with_property("x-solace-sentinel", "backfill-complete")
        .with_application_message_type("sentinel")
        .build(sentinel_payload)
    )

    destination = Topic.of(topic)
    publisher.publish(message, destination)

    print(f"Sentinel published to '{topic}' at {now.isoformat()}")
    print("User property: x-solace-sentinel = backfill-complete")

    publisher.terminate()
    service.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish backfill sentinel to Solace",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SOLACE_HOST", "tcp://localhost:55554"),
    )
    parser.add_argument(
        "--vpn",
        default=os.environ.get("SOLACE_VPN", "streaming-poc"),
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("SOLACE_USER", "streaming-user"),
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("SOLACE_PASSWORD", "default"),
    )
    parser.add_argument(
        "--topic",
        default="fleet/system/sentinel",
        help="Must match queue subscription (fleet/>)",
    )
    args = parser.parse_args()

    try:
        publish_sentinel(
            args.host, args.vpn, args.username, args.password, args.topic,
        )
    except Exception as exc:
        print(f"ERROR: Failed to publish sentinel: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
