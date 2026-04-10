#!/usr/bin/env bash
set -euo pipefail

EP_AVAILABLE=true

echo "=== Step 1: Create Event Portal objects ==="
if [[ -n "${SOLACE_CLOUD_TOKEN:-}" ]]; then
  ep_exit=0
  ./create_ep_objects.sh || ep_exit=$?
  if [[ "$ep_exit" -eq 2 ]]; then
    echo ""
    echo "WARNING: Event Portal unavailable — falling back to statically defined events." >&2
    EP_AVAILABLE=false
  elif [[ "$ep_exit" -ne 0 ]]; then
    exit "$ep_exit"
  fi
else
  echo "  SOLACE_CLOUD_TOKEN not set — skipping EP object creation"
  EP_AVAILABLE=false
fi

echo ""
if [[ "$EP_AVAILABLE" == "true" ]]; then
  echo "=== Step 2: Generate RisingWave init.sql (from Event Portal catalog) ==="
  python3 generate_mvs.py
else
  echo "=== Step 2: Generate RisingWave init.sql (static schema only — EP unavailable) ==="
  python3 generate_mvs.py --skip-ep
fi

echo ""
echo "=== Step 3: Apply schema to RisingWave ==="
psql -h "${RW_HOST:-risingwave}" -p "${RW_PORT:-4566}" -U root -d dev \
     -f config/risingwave/init.sql

echo ""
if [[ "$EP_AVAILABLE" != "true" ]]; then
  echo "========================================================================" >&2
  echo "WARNING: Event Portal was unavailable during setup." >&2
  echo "         The stack is running with statically defined events only." >&2
  echo "         To include EP catalog objects, set SOLACE_CLOUD_TOKEN and restart." >&2
  echo "========================================================================" >&2
fi

echo "=== Setup complete ==="
