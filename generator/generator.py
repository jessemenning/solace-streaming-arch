#!/usr/bin/env python3
"""
Fleet telemetry simulator — Solace Streaming Architecture POC

Simulates 20 vehicles publishing realistic IoT telemetry and alerts to the
Solace Platform event broker using the native Python SDK.

Topic hierarchy:
  fleet/telemetry/{vehicle_id}/metrics/{metric_type}  — sensor readings
  fleet/events/{vehicle_id}/alerts/{severity}          — alert events
  fleet/commands/{vehicle_id}/{command_type}           — vehicle commands (demo only)

Usage:
  python generator.py [--host tcp://localhost:55554] [--vpn streaming-poc]
                      [--user streaming-user] [--password default]
                      [--vehicles 20] [--burst] [--burst-vehicle vehicle_005]
"""

import argparse
import json
import math
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from solace.messaging.messaging_service import MessagingService
from solace.messaging.resources.topic import Topic
from solace.messaging.errors.pubsubplus_client_error import PubSubPlusClientError

# ─── Constants ────────────────────────────────────────────────────────────────

NUM_VEHICLES = 20
VEHICLE_IDS = [f"vehicle_{i:03d}" for i in range(1, NUM_VEHICLES + 1)]

# Boston metro bounding box (most vehicles roam here)
BOSTON_LAT_MIN, BOSTON_LAT_MAX = 42.22, 42.48
BOSTON_LON_MIN, BOSTON_LON_MAX = -71.18, -70.92

# Telemetry publish intervals (seconds)
TELEMETRY_INTERVAL = float(os.environ.get("TELEMETRY_INTERVAL", "2.0"))  # seconds between combined telemetry messages
ALERT_MIN_INTERVAL = 8.0   # minimum gap between alerts per vehicle
ALERT_PROBABILITY  = 0.06  # chance of alert each alert-tick

ALERT_TYPES = {
    "low_fuel":              {"severity_weights": [0.1, 0.5, 0.4], "metric": "fuel_level"},
    "high_engine_temp":      {"severity_weights": [0.2, 0.4, 0.4], "metric": "engine_temp"},
    "tire_pressure_warning": {"severity_weights": [0.4, 0.4, 0.2], "metric": "tire_pressure"},
    "speed_limit_exceeded":  {"severity_weights": [0.1, 0.3, 0.6], "metric": "speed"},
    "maintenance_due":       {"severity_weights": [0.7, 0.2, 0.1], "metric": None},
    "battery_voltage_low":   {"severity_weights": [0.3, 0.4, 0.3], "metric": "battery_voltage"},
}

SEVERITIES = ["low", "medium", "high"]


# ─── Vehicle state ────────────────────────────────────────────────────────────

@dataclass
class Vehicle:
    vehicle_id:   str
    lat:          float = field(default_factory=lambda: random.uniform(BOSTON_LAT_MIN, BOSTON_LAT_MAX))
    lon:          float = field(default_factory=lambda: random.uniform(BOSTON_LON_MIN, BOSTON_LON_MAX))
    heading:      float = field(default_factory=lambda: random.uniform(0, 360))
    speed:        float = field(default_factory=lambda: random.uniform(20, 70))
    fuel_pct:     float = field(default_factory=lambda: random.uniform(20, 95))
    engine_temp:  float = field(default_factory=lambda: random.uniform(175, 210))
    tire_psi:     float = field(default_factory=lambda: random.uniform(30, 36))
    battery_v:    float = field(default_factory=lambda: random.uniform(12.4, 14.8))
    last_alert:   float = 0.0

    def step(self, dt: float) -> None:
        """Advance vehicle simulation by dt seconds."""
        # Drift heading slightly
        self.heading += random.gauss(0, 5)
        self.heading %= 360

        # Speed: random walk, clamped to realistic range
        self.speed = max(0, min(90, self.speed + random.gauss(0, 3)))

        # Move position (approx degrees per second at this speed)
        speed_deg_per_sec = (self.speed * 1.60934) / 111_320  # mph → m/s → deg/s
        rad = math.radians(self.heading)
        self.lat += speed_deg_per_sec * dt * math.cos(rad)
        self.lon += speed_deg_per_sec * dt * math.sin(rad) / math.cos(math.radians(self.lat))

        # Clamp to Boston area (vehicles bounce off the boundary)
        if self.lat < BOSTON_LAT_MIN or self.lat > BOSTON_LAT_MAX:
            self.heading = 180 - self.heading
            self.lat = max(BOSTON_LAT_MIN, min(BOSTON_LAT_MAX, self.lat))
        if self.lon < BOSTON_LON_MIN or self.lon > BOSTON_LON_MAX:
            self.heading = -self.heading
            self.lon = max(BOSTON_LON_MIN, min(BOSTON_LON_MAX, self.lon))

        # Slowly deplete fuel
        self.fuel_pct = max(5, self.fuel_pct - random.uniform(0, 0.005))

        # Engine temp: rises with speed, fluctuates
        target_temp = 175 + self.speed * 0.4
        self.engine_temp += (target_temp - self.engine_temp) * 0.05 + random.gauss(0, 1)
        self.engine_temp = max(150, min(260, self.engine_temp))

        # Tire pressure: slow drift
        self.tire_psi = max(22, min(38, self.tire_psi + random.gauss(0, 0.01)))

        # Battery voltage: random walk around 13.5V
        self.battery_v = max(11.0, min(15.0, self.battery_v + random.gauss(0, 0.05)))

    def telemetry_payload(self, metric_type: str) -> dict:
        """Build a telemetry message payload for a specific metric."""
        metric_map = {
            "speed":           (self.speed,       "mph"),
            "fuel_level":      (self.fuel_pct,    "percent"),
            "engine_temp":     (self.engine_temp, "fahrenheit"),
            "tire_pressure":   (self.tire_psi,    "psi"),
            "battery_voltage": (self.battery_v,   "volts"),
        }
        value, unit = metric_map[metric_type]
        topic = f"fleet/telemetry/{self.vehicle_id}/metrics/{metric_type}"
        return {
            "vehicle_id":   self.vehicle_id,
            "metric_type":  metric_type,
            "value":        round(value, 3),
            "unit":         unit,
            "latitude":     round(self.lat, 6),
            "longitude":    round(self.lon, 6),
            "recorded_at":  datetime.now(timezone.utc).isoformat(),
            "solace_topic": topic,
        }

    def telemetry_all_payload(self) -> dict:
        """Build a single combined telemetry message with all metrics."""
        topic = f"fleet/telemetry/{self.vehicle_id}/metrics"
        return {
            "vehicle_id":      self.vehicle_id,
            "speed":           round(self.speed, 3),
            "fuel_level":      round(self.fuel_pct, 3),
            "engine_temp":     round(self.engine_temp, 3),
            "tire_pressure":   round(self.tire_psi, 3),
            "battery_voltage": round(self.battery_v, 3),
            "latitude":        round(self.lat, 6),
            "longitude":       round(self.lon, 6),
            "recorded_at":     datetime.now(timezone.utc).isoformat(),
            "solace_topic":    topic,
        }

    def alert_payload(self, alert_type: str, severity: str) -> dict:
        """Build an alert event payload."""
        alert_meta = ALERT_TYPES[alert_type]
        topic = f"fleet/events/{self.vehicle_id}/alerts/{severity}"

        descriptions = {
            "low_fuel":              f"Fuel level critical: {self.fuel_pct:.1f}%",
            "high_engine_temp":      f"Engine temp elevated: {self.engine_temp:.1f}°F",
            "tire_pressure_warning": f"Tire pressure anomaly: {self.tire_psi:.1f} psi",
            "speed_limit_exceeded":  f"Speed {self.speed:.1f} mph exceeds posted limit",
            "maintenance_due":       "Scheduled maintenance interval reached",
            "battery_voltage_low":   f"Battery voltage low: {self.battery_v:.2f}V",
        }

        extra = {}
        if alert_meta["metric"]:
            metric = alert_meta["metric"]
            val_map = {
                "fuel_level":      self.fuel_pct,
                "engine_temp":     self.engine_temp,
                "tire_pressure":   self.tire_psi,
                "speed":           self.speed,
                "battery_voltage": self.battery_v,
            }
            extra["current_value"] = round(val_map.get(metric, 0), 3)
            extra["metric"] = metric

        return {
            "vehicle_id":   self.vehicle_id,
            "event_type":   alert_type,
            "severity":     severity,
            "description":  descriptions[alert_type],
            "payload":      extra,
            "occurred_at":  datetime.now(timezone.utc).isoformat(),
            "solace_topic": topic,
        }


# ─── Publisher ────────────────────────────────────────────────────────────────

class FleetPublisher:
    def __init__(self, messaging_service: MessagingService):
        self._svc = messaging_service
        self._publisher = (
            messaging_service.create_direct_message_publisher_builder().build()
        )
        self._publisher.start()
        self._msg_count = 0

    def publish(self, topic_str: str, payload: dict) -> None:
        try:
            msg = (
                self._svc.message_builder()
                .with_application_message_id(f"msg-{self._msg_count}")
                .build(json.dumps(payload))
            )
            self._publisher.publish(message=msg, destination=Topic.of(topic_str))
            self._msg_count += 1
        except PubSubPlusClientError as e:
            print(f"[publisher] ERROR: {e}", file=sys.stderr)

    def stop(self) -> None:
        self._publisher.terminate()


# ─── Main simulation loop ─────────────────────────────────────────────────────

def run_simulation(
    args: argparse.Namespace,
    publisher: FleetPublisher,
    vehicles: list[Vehicle],
) -> None:
    tick = 0
    print(f"[generator] Simulating {len(vehicles)} vehicles. Ctrl-C to stop.")

    while True:
        now = time.time()

        for v in vehicles:
            v.step(TELEMETRY_INTERVAL)

            # Publish one combined message per vehicle with all metrics
            payload = v.telemetry_all_payload()
            publisher.publish(payload["solace_topic"], payload)

            # Random alert generation
            if now - v.last_alert > ALERT_MIN_INTERVAL and random.random() < ALERT_PROBABILITY:
                alert_type = random.choice(list(ALERT_TYPES.keys()))
                weights = ALERT_TYPES[alert_type]["severity_weights"]
                severity = random.choices(SEVERITIES, weights=weights, k=1)[0]

                # In burst mode, always emit high-severity for the target vehicle
                if args.burst and v.vehicle_id == args.burst_vehicle:
                    severity = "high"

                payload = v.alert_payload(alert_type, severity)
                publisher.publish(payload["solace_topic"], payload)
                v.last_alert = now
                print(
                    f"[alert] {v.vehicle_id} | {severity:6s} | {alert_type}",
                    flush=True,
                )

        # Burst mode: extra high-severity alerts for demo vehicle
        if args.burst and tick % 5 == 0:
            bv = next((v for v in vehicles if v.vehicle_id == args.burst_vehicle), vehicles[0])
            for _ in range(3):
                alert_type = random.choice(list(ALERT_TYPES.keys()))
                payload = bv.alert_payload(alert_type, "high")
                publisher.publish(payload["solace_topic"], payload)
            print(f"[burst] Sent 3 extra HIGH alerts for {args.burst_vehicle}", flush=True)

        if tick % 10 == 0:
            print(
                f"[generator] tick={tick}  msgs_sent={publisher._msg_count}  "
                f"vehicles={len(vehicles)}",
                flush=True,
            )

        tick += 1
        # Sleep the remainder of the tick interval
        elapsed = time.time() - now
        time.sleep(max(0, TELEMETRY_INTERVAL - elapsed))


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Fleet telemetry simulator")
    parser.add_argument("--host",           default="tcp://localhost:55554")
    parser.add_argument("--vpn",            default="streaming-poc")
    parser.add_argument("--user",           default="streaming-user")
    parser.add_argument("--password",       default="default")
    parser.add_argument("--vehicles",       type=int, default=NUM_VEHICLES)
    parser.add_argument("--burst",          action="store_true",
                        help="Emit a continuous spike of high-severity alerts for the burst vehicle")
    parser.add_argument("--burst-vehicle",  default="vehicle_005",
                        help="Vehicle ID to target with burst alerts (default: vehicle_005)")
    args = parser.parse_args()

    broker_props = {
        "solace.messaging.transport.host":                        args.host,
        "solace.messaging.service.vpn-name":                      args.vpn,
        "solace.messaging.authentication.scheme.basic.username":  args.user,
        "solace.messaging.authentication.scheme.basic.password":  args.password,
    }

    print(f"[generator] Connecting to {args.host}  VPN={args.vpn}  user={args.user}")
    messaging_service = MessagingService.builder().from_properties(broker_props).build()

    try:
        messaging_service.connect()
    except PubSubPlusClientError as e:
        print(f"[generator] Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[generator] Connected. Burst mode: {'ON — ' + args.burst_vehicle if args.burst else 'OFF'}")

    vehicles = [Vehicle(vid) for vid in VEHICLE_IDS[: args.vehicles]]
    publisher = FleetPublisher(messaging_service)

    def _shutdown(sig, frame):
        print("\n[generator] Shutting down...")
        publisher.stop()
        messaging_service.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run_simulation(args, publisher, vehicles)


if __name__ == "__main__":
    main()
