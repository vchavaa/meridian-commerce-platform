#!/usr/bin/env bash
set -euo pipefail

echo "=================================================================="
echo " Meridian Commerce Platform - Verification & Live Web Runner"
echo "=================================================================="

python3 scripts/simulate_and_verify.py

echo ""
echo "Starting Web App B (order-service) on http://localhost:8081 ..."
python3 apps/order-service/app.py &
ORDER_PID=$!
trap "kill $ORDER_PID 2>/dev/null || true" EXIT

echo "Starting Web App A (storefront-ui) on http://localhost:8080 ..."
echo " -> Open http://localhost:8080 in your browser (Press Ctrl+C to stop)"
python3 apps/storefront-ui/app.py
