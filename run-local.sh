#!/usr/bin/env bash
set -euo pipefail

echo "=================================================================="
echo " Meridian Commerce Platform - Verification & Live Web Runner"
echo "=================================================================="

python3 scripts/simulate_and_verify.py

export LOCAL_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
cleanup() {
    if ! docker compose down; then
        echo "Warning: Compose did not stop cleanly; the local secrets volume was retained." >&2
    fi
    unset LOCAL_ADMIN_TOKEN
}
trap cleanup EXIT

echo "Starting local Docker Compose services."
echo "Local admin token: ${LOCAL_ADMIN_TOKEN}"
echo " -> Storefront UI: http://localhost:8080"
echo " -> Order console: http://localhost:8081"
echo "Press Ctrl+C to stop containers; PostgreSQL data remains in its named volume."
docker compose up --build --detach
docker compose logs --follow
