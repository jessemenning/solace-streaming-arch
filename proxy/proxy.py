#!/usr/bin/env python3
"""
Solace-to-RisingWave webhook proxy

Replaces the Solace REST Delivery Point (RDP) with a Python process that:
  1. Binds to durable queues on the Solace broker
  2. Extracts message envelope metadata (topic, sender timestamp)
  3. HTTP POSTs the raw body to the RisingWave webhook endpoint with
     metadata injected as HTTP headers

Headers injected:
  x-message-topic      — Solace destination topic (e.g. fleet/telemetry/vehicle_001/metrics)
  x-message-timestamp  — Sender timestamp as ISO 8601 UTC string

RisingWave captures these via INCLUDE header on the webhook TABLE:
  CREATE TABLE fleet_all_raw (data JSONB)
  INCLUDE header 'x-message-topic'     AS _topic     VARCHAR
  INCLUDE header 'x-message-timestamp' AS _timestamp VARCHAR
  WITH (connector = 'webhook');

Scaling:
  PROXY_WORKERS_PER_QUEUE controls the number of worker threads per queue.
  Each worker runs its own polling loop with a dedicated requests.Session.
  For horizontal scaling, set queues to non-exclusive and run multiple containers.

DLQ:
  On HTTP failure, the proxy NAKs the message with FAILED disposition.
  After maxRedeliveryCount (3) failures, Solace moves it to #DEAD_MSG_QUEUE.
"""

import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import requests
from solace.messaging.messaging_service import MessagingService
from solace.messaging.resources.queue import Queue
from solace.messaging.config.message_acknowledgement_configuration import Outcome
from solace.messaging.errors.pubsubplus_client_error import PubSubPlusClientError

# ─── Configuration ───────────────────────────────────────────────────────────

SOLACE_HOST     = os.environ.get("SOLACE_HOST", "tcp://localhost:55555")
SOLACE_VPN      = os.environ.get("SOLACE_VPN", "streaming-poc")
SOLACE_USER     = os.environ.get("SOLACE_USER", "streaming-user")
SOLACE_PASSWORD = os.environ.get("SOLACE_PASSWORD", "default")

RW_HOST         = os.environ.get("RW_HOST", "risingwave")
RW_WEBHOOK_PORT = os.environ.get("RW_WEBHOOK_PORT", "4560")
RW_WEBHOOK_URL  = f"http://{RW_HOST}:{RW_WEBHOOK_PORT}/webhook/dev/public/fleet_all_raw"

WORKERS_PER_QUEUE = int(os.environ.get("PROXY_WORKERS_PER_QUEUE", "4"))

QUEUES = ["rw-ingest", "events-ingest"]

# ─── Globals ─────────────────────────────────────────────────────────────────

shutdown_event = threading.Event()
msg_count = 0
err_count = 0
count_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[proxy] {msg}", flush=True)


# ─── Worker ──────────────────────────────────────────────────────────────────

def worker(receiver, queue_name: str, worker_id: int) -> None:
    """Poll receiver for messages and POST to RisingWave."""
    global msg_count, err_count
    session = requests.Session()
    session.headers["Content-Type"] = "application/json"

    tag = f"{queue_name}/w{worker_id}"
    log(f"[{tag}] Worker started")

    while not shutdown_event.is_set():
        try:
            msg = receiver.receive_message(timeout=1000)
        except PubSubPlusClientError:
            if shutdown_event.is_set():
                break
            continue

        if msg is None:
            continue

        try:
            # Extract envelope metadata
            topic = msg.get_destination_name() or ""
            sender_ts = msg.get_sender_timestamp()
            if sender_ts is not None:
                ts_iso = datetime.fromtimestamp(sender_ts / 1000, tz=timezone.utc).isoformat()
            else:
                ts_iso = datetime.now(timezone.utc).isoformat()

            payload = msg.get_payload_as_string()
            if payload is None:
                raw = msg.get_payload_as_bytes()
                payload = raw.decode("utf-8") if raw else "{}"

            # POST to RisingWave with metadata headers
            resp = session.post(
                RW_WEBHOOK_URL,
                data=payload,
                headers={
                    "x-message-topic": topic,
                    "x-message-timestamp": ts_iso,
                },
                timeout=10,
            )

            if resp.status_code == 200:
                receiver.settle(msg, Outcome.ACCEPTED)
                with count_lock:
                    msg_count += 1
            else:
                log(f"[{tag}] HTTP {resp.status_code} from RisingWave: {resp.text[:200]}")
                receiver.settle(msg, Outcome.FAILED)
                with count_lock:
                    err_count += 1

        except requests.RequestException as e:
            log(f"[{tag}] HTTP error: {e}")
            try:
                receiver.settle(msg, Outcome.FAILED)
            except PubSubPlusClientError:
                pass
            with count_lock:
                err_count += 1
        except Exception as e:
            log(f"[{tag}] Unexpected error: {e}")
            try:
                receiver.settle(msg, Outcome.FAILED)
            except PubSubPlusClientError:
                pass
            with count_lock:
                err_count += 1

    session.close()
    log(f"[{tag}] Worker stopped")


# ─── Stats reporter ─────────────────────────────────────────────────────────

def stats_reporter() -> None:
    """Periodically log throughput stats."""
    while not shutdown_event.is_set():
        shutdown_event.wait(10)
        if not shutdown_event.is_set():
            log(f"stats: delivered={msg_count}  errors={err_count}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    log(f"Connecting to {SOLACE_HOST}  VPN={SOLACE_VPN}  user={SOLACE_USER}")
    log(f"RisingWave webhook: {RW_WEBHOOK_URL}")
    log(f"Workers per queue: {WORKERS_PER_QUEUE}")

    broker_props = {
        "solace.messaging.transport.host":                       SOLACE_HOST,
        "solace.messaging.service.vpn-name":                     SOLACE_VPN,
        "solace.messaging.authentication.scheme.basic.username": SOLACE_USER,
        "solace.messaging.authentication.scheme.basic.password": SOLACE_PASSWORD,
    }

    messaging_service = MessagingService.builder().from_properties(broker_props).build()

    try:
        messaging_service.connect()
    except PubSubPlusClientError as e:
        log(f"Connection failed: {e}")
        sys.exit(1)

    log("Connected to Solace broker")

    receivers = []
    threads = []

    for queue_name in QUEUES:
        queue = Queue.durable_exclusive_queue(queue_name)
        receiver = (
            messaging_service.create_persistent_message_receiver_builder()
            .with_required_message_outcome_support(Outcome.FAILED, Outcome.REJECTED)
            .build(queue)
        )
        receiver.start()
        receivers.append(receiver)
        log(f"Bound to queue: {queue_name}")

        for wid in range(WORKERS_PER_QUEUE):
            t = threading.Thread(
                target=worker,
                args=(receiver, queue_name, wid),
                daemon=True,
            )
            t.start()
            threads.append(t)

    # Stats thread
    stats_t = threading.Thread(target=stats_reporter, daemon=True)
    stats_t.start()

    log(f"Proxy running — {len(QUEUES)} queues x {WORKERS_PER_QUEUE} workers = "
        f"{len(QUEUES) * WORKERS_PER_QUEUE} total workers")

    # Wait for shutdown signal
    def _shutdown(sig, frame):
        log("Shutdown signal received — draining workers...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_event.set()

    # Wait for workers to finish
    for t in threads:
        t.join(timeout=5)

    # Terminate receivers
    for r in receivers:
        try:
            r.terminate()
        except Exception:
            pass

    messaging_service.disconnect()
    log(f"Proxy stopped. Final stats: delivered={msg_count}  errors={err_count}")


if __name__ == "__main__":
    main()
