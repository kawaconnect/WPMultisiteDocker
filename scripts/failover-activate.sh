#!/usr/bin/env bash
# =============================================================================
# failover-activate.sh, Activate standby WordPress Multisite
# =============================================================================
# Usage: ./scripts/failover-activate.sh
#
# Steps:
#   1. Ensures core services are healthy
#   2. Starts Cloudflare tunnel containers (--profile tunnels)
#   3. Verifies tunnel connectivity
#
# After running this script, update DNS in each Cloudflare account to point
# domains to the tunnel CNAME.
# =============================================================================
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$COMPOSE_DIR"

echo "=== WordPress Multisite Failover Activation ==="
echo ""

# Step 1: Ensure core services are running and healthy
echo "[1/3] Checking core services..."
docker compose up -d
echo "    Waiting for services to become healthy..."
docker compose exec -T mariadb healthcheck.sh --connect --innodb_initialized
docker compose exec -T redis redis-cli ping | grep -q PONG
echo "    Core services: OK"
echo ""

# Step 2: Start tunnel containers
echo "[2/3] Starting Cloudflare tunnel containers..."
docker compose --profile tunnels up -d
echo "    Tunnel containers started."
echo ""

# Step 3: Verify tunnels are running
echo "[3/3] Verifying tunnel connectivity..."
sleep 5
TUNNEL_CONTAINERS=$(docker compose --profile tunnels ps --format '{{.Name}}' | grep cloudflared || true)
if [ -z "$TUNNEL_CONTAINERS" ]; then
    echo "    WARNING: No cloudflared containers found. Check your .env tokens."
    exit 1
fi

ALL_HEALTHY=true
for container in $TUNNEL_CONTAINERS; do
    STATUS=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "not found")
    if [ "$STATUS" = "running" ]; then
        echo "    $container: running"
    else
        echo "    $container: $STATUS (ERROR)"
        ALL_HEALTHY=false
    fi
done

echo ""
if [ "$ALL_HEALTHY" = true ]; then
    echo "=== Failover activation COMPLETE ==="
    echo ""
    echo "Next steps:"
    echo "  1. In each Cloudflare account's Zero Trust dashboard, update DNS"
    echo "     to point domains to the tunnel CNAME."
    echo "  2. Verify sites load: curl -H 'Host: example.com' http://localhost:8080"
else
    echo "=== Failover activation completed with WARNINGS ==="
    echo "Some tunnel containers are not running. Check logs:"
    echo "  docker compose --profile tunnels logs cloudflared-account1"
    exit 1
fi
