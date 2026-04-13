#!/bin/sh
# init-watchdog.sh — re-applies init.sql whenever RisingWave loses its views.
# Runs as a sidecar container; loops forever so it survives RisingWave restarts.

RW_DSN="postgresql://root@risingwave:4566/dev"

while true; do
    # 1. Wait until RisingWave accepts connections.
    if ! psql "$RW_DSN" -c "SELECT 1" >/dev/null 2>&1; then
        echo "[rw-init] RisingWave not ready — retrying in 10s"
        sleep 10
        continue
    fi

    # 2. Check whether the schema is applied (probe one known view).
    if psql "$RW_DSN" -c "SELECT 1 FROM high_severity_alerts LIMIT 0" >/dev/null 2>&1; then
        # Views present — nothing to do; check again in 30s.
        sleep 30
    else
        echo "[rw-init] Views missing — applying init.sql"
        if psql "$RW_DSN" -f /init.sql; then
            echo "[rw-init] Schema applied successfully."
        else
            echo "[rw-init] init.sql failed — will retry in 15s"
            sleep 15
        fi
    fi
done
