#!/usr/bin/env bash
# =============================================================================
# cf-backup.sh, Scheduled backup of all DNS records + tunnel configs
# =============================================================================
# Usage: ./scripts/cf-backup.sh
#
# Can be run manually or via cron:
#   0 2 * * * /opt/docker/wordpressmultisite/scripts/cf-backup.sh
#
# Retention (grandfather-father-son):
#   - Daily:   last 7 days
#   - Weekly:  last 4 Sundays
#   - Monthly: last 6 first-of-month
#   - Yearly:  last 5 January 1st
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/cf-common.sh"

cleanup_backup() {
    local exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        echo "" >&2
        log_error "Backup interrupted (exit $exit_code). Some backups may be incomplete."
    fi
}
trap cleanup_backup EXIT

echo "=== Cloudflare Backup ==="
echo ""

# ── Validate ──────────────────────────────────────────────────────────────────

check_deps
load_config

ACCOUNT_COUNT=$(get_account_count)
ERRORS=0

# ── Backup all accounts ─────────────────────────────────────────────────────

for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    name=$(get_account_var "$n" NAME)
    account_id=$(get_account_var "$n" ID)
    api_token=$(get_account_var "$n" TOKEN)
    tunnel_name="wp-docker-$name"

    echo "  Account: $name"

    # Backup DNS for each domain
    read -ra domains <<< "$(get_account_domains "$n")"
    for domain in "${domains[@]}"; do
        zone_id=$(get_zone_id "$domain" "$api_token" 2>/dev/null)
        if [ -z "$zone_id" ]; then
            log_warn "Could not get zone ID for $domain, skipping"
            ERRORS=$((ERRORS + 1))
            continue
        fi
        backup_dns_zone "$domain" "$zone_id" "$api_token" || ERRORS=$((ERRORS + 1))
    done

    # Backup tunnel config
    tunnel_json=$(get_tunnel_by_name "$account_id" "$api_token" "$tunnel_name" 2>/dev/null)
    if [ -n "$tunnel_json" ]; then
        tunnel_id=$(echo "$tunnel_json" | jq -r '.id')
        backup_tunnel_config "$name" "$account_id" "$api_token" "$tunnel_id" || ERRORS=$((ERRORS + 1))
    else
        log_warn "Tunnel '$tunnel_name' not found, skipping tunnel backup"
    fi

    echo ""
done

# ── Retention cleanup ────────────────────────────────────────────────────────

echo "  Running retention cleanup..."

# Clean DNS backups
for dir in "$BACKUP_DIR"/dns/*/; do
    [ -d "$dir" ] && cleanup_backups "$dir"
done

# Clean tunnel backups
for dir in "$BACKUP_DIR"/tunnels/*/; do
    [ -d "$dir" ] && cleanup_backups "$dir"
done

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
if [ "$ERRORS" -eq 0 ]; then
    echo "=== Backup Complete ==="
else
    echo "=== Backup Complete (with $ERRORS warning(s)) ==="
fi
echo "  Location: $BACKUP_DIR"

# Show backup sizes
if [ -d "$BACKUP_DIR" ]; then
    total_size=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
    total_files=$(find "$BACKUP_DIR" -name "*.json" 2>/dev/null | wc -l)
    echo "  Total: $total_files file(s), $total_size"
fi
echo ""
