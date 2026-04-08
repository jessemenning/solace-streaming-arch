#!/usr/bin/env bash
set -euo pipefail

echo "=== Step 1: Create Event Portal objects ==="
./create_ep_objects.sh

echo ""
echo "=== Step 2: Generate RisingWave init.sql ==="
python3 generate_mvs.py

echo ""
echo "=== Step 3: Apply schema to RisingWave ==="
psql -h "${RW_HOST:-risingwave}" -p "${RW_PORT:-4566}" -U root -d dev \
     -f config/risingwave/init.sql

echo ""
echo "=== Setup complete ==="
