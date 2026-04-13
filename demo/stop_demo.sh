#!/usr/bin/env bash
# stop_demo.sh — tear down all demo services and verify nothing is left running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.yml"

KEEP_VOLUMES=false
for arg in "$@"; do
    case "$arg" in
        --keep-volumes) KEEP_VOLUMES=true ;;
    esac
done

echo "==> Stopping all demo services..."
# Solace broker takes 60–90 s to gracefully shut down; --timeout 20 forces SIGKILL after 20 s
if [ "$KEEP_VOLUMES" = true ]; then
    docker compose -f "$COMPOSE_FILE" down --timeout 20 --remove-orphans
else
    docker compose -f "$COMPOSE_FILE" down -v --timeout 20 --remove-orphans
fi

echo ""
echo "==> Verifying no project containers remain..."
RUNNING=$(docker compose -f "$COMPOSE_FILE" ps -q 2>/dev/null || true)
if [ -n "$RUNNING" ]; then
    echo "WARNING: Some containers are still present:"
    docker compose -f "$COMPOSE_FILE" ps
    exit 1
fi

echo "==> All services stopped."
echo ""
if [ "$KEEP_VOLUMES" = true ]; then
    echo "NOTE: Volume (solace-data) preserved."
    echo "      Next run will resume from existing data."
else
    echo "NOTE: Volume (solace-data) deleted. Next run starts fresh."
    echo "      To preserve volumes instead, run: $0 --keep-volumes"
fi
