#!/usr/bin/env bash
# =============================================================================
# failover-activate.sh, Wrapper for cf-failover.sh
# =============================================================================
# Usage: ./scripts/failover-activate.sh
#
# This script now delegates to cf-failover.sh which handles everything:
#   - Starts Docker services + tunnels
#   - Backs up current DNS state
#   - Switches real domains from production to Docker tunnels
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SCRIPT_DIR/cf-failover.sh" ]; then
    echo "ERROR: cf-failover.sh not found. Run cf-setup.sh first."
    exit 1
fi

exec "$SCRIPT_DIR/cf-failover.sh" "$@"
