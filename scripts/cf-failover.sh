#!/usr/bin/env bash
# =============================================================================
# cf-failover.sh, Switch real domains from production to Docker tunnels
# =============================================================================
# Usage: ./scripts/cf-failover.sh
#
# THIS IS A DANGEROUS OPERATION. It modifies live DNS for real domains.
# Multiple confirmations are required before any changes are made.
#
# Safety:
#   1. Full backup of ALL accounts runs first (mandatory, cannot skip)
#   2. If backup fails → script aborts, nothing is touched
#   3. Shows exactly which records will change (dry-run preview)
#   4. Requires you to type FAILOVER to confirm
#   5. Requires a second confirmation after seeing the change list
#   6. Only THEN does it modify DNS
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/cf-common.sh"

# Track partially-switched records for trap handler
declare -a PARTIALLY_SWITCHED=()

cleanup_failover() {
    local exit_code=$?
    if [ "$exit_code" -ne 0 ] && [ ${#PARTIALLY_SWITCHED[@]} -gt 0 ]; then
        echo "" >&2
        log_error "Failover interrupted (exit $exit_code). The following records were already switched:"
        for rec in "${PARTIALLY_SWITCHED[@]}"; do
            log_error "  - $rec"
        done
        log_error "Run ./scripts/cf-rollback.sh to restore, or re-run cf-failover.sh to continue."
    fi
}
trap cleanup_failover EXIT

# ── Step 1: Validate ─────────────────────────────────────────────────────────

echo "[1/6] Checking dependencies and config..."
check_deps
load_config

CLOUD_IP="$CF_CLOUD_IP"
STANDBY_DOMAIN="$CF_STANDBY_DOMAIN"
ACCOUNT_COUNT=$(get_account_count)

echo ""
echo "============================================================"
echo "  CLOUDFLARE FAILOVER, SWITCH REAL DOMAINS TO DOCKER"
echo "============================================================"
echo ""
echo "  This will:"
echo "    - Change DNS A records on REAL customer domains"
echo "    - Point them at Cloudflare tunnels instead of production"
echo "    - Modify tunnel ingress configurations"
echo ""
echo "  This will NOT touch:"
echo "    - The standby domain (*.$CF_STANDBY_DOMAIN)"
echo "    - Any records that don't point at $CF_CLOUD_IP"
echo ""

# ── Step 2: First confirmation ───────────────────────────────────────────────

confirm_dangerous "FAILOVER" \
    "This will modify live DNS for real customer domains."

# ── Step 3: Mandatory backup ─────────────────────────────────────────────────

echo ""
echo "[2/6] Running mandatory backup (ALL accounts)..."
require_backup "failover"

# ── Step 4: Check Docker health ──────────────────────────────────────────────

echo "[3/6] Verifying Docker services..."

cd "$PROJECT_DIR"
docker compose up -d --quiet-pull 2>/dev/null || true

# Wait for core services
for svc in mariadb redis wordpress nginx; do
    status=$(docker compose ps --format '{{.Health}}' "$svc" 2>/dev/null || echo "missing")
    if [ "$status" = "healthy" ] || [ "$status" = "" ]; then
        log_ok "$svc: running"
    else
        log_warn "$svc: $status, waiting..."
        docker compose up -d "$svc" 2>/dev/null
        sleep 10
    fi
done

# Start tunnel containers
docker compose --profile tunnels up -d --quiet-pull 2>/dev/null || true
sleep 3
log_ok "Tunnel containers started"

# ── Step 5: Dry-run, show exactly what will change ──────────────────────────

echo ""
echo "[4/6] Scanning DNS records (DRY RUN, nothing changed yet)..."
echo ""

# Collect planned changes for display and later execution
declare -a PLAN_ACCOUNTS=()    # account number (1-based)
declare -a PLAN_ZONE_IDS=()    # zone id
declare -a PLAN_RECORD_IDS=()  # record id to delete
declare -a PLAN_NAMES=()       # hostname
declare -a PLAN_PROXIED=()     # proxied flag
declare -a PLAN_TUNNEL_IDS=()  # tunnel id for CNAME target
PLAN_COUNT=0

for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    name=$(get_account_var "$n" NAME)
    account_id=$(get_account_var "$n" ID)
    api_token=$(get_account_var "$n" TOKEN)
    tunnel_name="wp-docker-$name"

    echo "  Account: $name"

    # Get tunnel ID
    tunnel_json=$(get_tunnel_by_name "$account_id" "$api_token" "$tunnel_name")
    if [ -z "$tunnel_json" ]; then
        log_error "Tunnel '$tunnel_name' not found, run cf-setup.sh first"
        continue
    fi
    tunnel_id=$(echo "$tunnel_json" | jq -r '.id')

    # Process each domain on this account
    read -ra domains <<< "$(get_account_domains "$n")"
    for domain in "${domains[@]}"; do
        # The standby domain itself is infrastructure, not a production
        # site to fail over, skip just this one entry (the account may
        # still own other real production domains worth processing).
        [ "$domain" = "$STANDBY_DOMAIN" ] && continue
        zone_id=$(get_zone_id "$domain" "$api_token") || continue

        # Find A records pointing at cloud IP
        a_records=$(get_dns_records "$zone_id" "$api_token" "A") || continue
        matching=$(echo "$a_records" | jq --arg ip "$CLOUD_IP" '[.[] | select(.content == $ip)]')
        match_count=$(echo "$matching" | jq 'length')

        if [ "$match_count" -eq 0 ]; then
            log_info "$domain: no A records pointing at $CLOUD_IP"
            continue
        fi

        for ((r = 0; r < match_count; r++)); do
            record_id=$(echo "$matching" | jq -r ".[$r].id")
            record_name=$(echo "$matching" | jq -r ".[$r].name")
            record_proxied=$(echo "$matching" | jq -r ".[$r].proxied")

            PLAN_ACCOUNTS+=("$n")
            PLAN_ZONE_IDS+=("$zone_id")
            PLAN_RECORD_IDS+=("$record_id")
            PLAN_NAMES+=("$record_name")
            PLAN_PROXIED+=("$record_proxied")
            PLAN_TUNNEL_IDS+=("$tunnel_id")
            PLAN_COUNT=$((PLAN_COUNT + 1))
        done
    done
    echo ""
done

# ── Show the change plan ─────────────────────────────────────────────────────

if [ "$PLAN_COUNT" -eq 0 ]; then
    echo ""
    echo "No records to switch. Nothing points at $CLOUD_IP."
    echo "Check that .env has failover accounts with real domains."
    exit 0
fi

echo ""
echo "  ============================================================"
echo "  PLANNED CHANGES ($PLAN_COUNT record(s) will be modified):"
echo "  ============================================================"
echo ""
printf "  %-40s %-20s → %s\n" "HOSTNAME" "CURRENT" "NEW"
printf "  %-40s %-20s   %s\n" "--------" "-------" "---"
for ((p = 0; p < PLAN_COUNT; p++)); do
    cname_target="${PLAN_TUNNEL_IDS[$p]}.cfargotunnel.com"
    printf "  %-40s A %-18s → CNAME %s\n" "${PLAN_NAMES[$p]}" "$CLOUD_IP" "$cname_target"
done
echo ""

# ── Step 6: Second confirmation ──────────────────────────────────────────────

echo "  Backup location: $BACKUP_DIR"
echo "  Rollback command: ./scripts/cf-rollback.sh"
echo ""

confirm_yes "You are about to modify $PLAN_COUNT live DNS record(s). Proceed?"

# ── Step 7: Configure tunnel ingress (before any DNS change) ─────────────────

# Every hostname must already be routed by its tunnel when its DNS switches,
# and a failure here must stop the run while DNS is still untouched: a
# re-run only finds records that still point at $CLOUD_IP, so ingress missed
# after a switch would never be added.
echo "[5/6] Configuring tunnel ingress..."

declare -A INGRESS_BY_TUNNEL   # tunnel_id → space-separated hostnames
declare -A ACCOUNT_BY_TUNNEL   # tunnel_id → account number
for ((p = 0; p < PLAN_COUNT; p++)); do
    INGRESS_BY_TUNNEL[${PLAN_TUNNEL_IDS[$p]}]+="${PLAN_NAMES[$p]} "
    ACCOUNT_BY_TUNNEL[${PLAN_TUNNEL_IDS[$p]}]="${PLAN_ACCOUNTS[$p]}"
done

for tunnel_id in "${!INGRESS_BY_TUNNEL[@]}"; do
    acct_n="${ACCOUNT_BY_TUNNEL[$tunnel_id]}"
    account_id=$(get_account_var "$acct_n" ID)
    api_token=$(get_account_var "$acct_n" TOKEN)
    hosts="${INGRESS_BY_TUNNEL[$tunnel_id]}"
    # Named-host rules only; the merge keeps unrelated rules already on the
    # tunnel (e.g. the standby wildcard on the standby account's own tunnel).
    ingress=$(printf '%s\n' $hosts | jq -R 'select(length > 0) | {hostname: ., service: "http://nginx:80"}' | jq -s -c .)
    host_count=$(echo "$hosts" | wc -w)
    log_info "Configuring tunnel ingress ($host_count hostname(s))"
    if ! merge_tunnel_ingress "$account_id" "$api_token" "$tunnel_id" "$ingress" >/dev/null; then
        log_error "Tunnel ingress update failed, no DNS record has been changed."
        exit 1
    fi
    log_ok "Tunnel ingress updated"
done

# ── Step 8: Execute DNS changes ──────────────────────────────────────────────

echo ""
echo "[6/6] Switching DNS records..."
echo ""

SWITCHED=()

for ((p = 0; p < PLAN_COUNT; p++)); do
    acct_n="${PLAN_ACCOUNTS[$p]}"
    zone_id="${PLAN_ZONE_IDS[$p]}"
    record_id="${PLAN_RECORD_IDS[$p]}"
    record_name="${PLAN_NAMES[$p]}"
    record_proxied="${PLAN_PROXIED[$p]}"
    tunnel_id="${PLAN_TUNNEL_IDS[$p]}"
    api_token=$(get_account_var "$acct_n" TOKEN)
    cname_target="${tunnel_id}.cfargotunnel.com"

    log_info "Switching: $record_name (A $CLOUD_IP → CNAME $cname_target)"

    # Delete the A record
    delete_dns_record "$zone_id" "$api_token" "$record_id"

    # Create CNAME pointing to tunnel; if this fails, attempt to restore original A record
    if ! create_dns_cname "$zone_id" "$api_token" "$record_name" "$cname_target" "$record_proxied" >/dev/null; then
        log_error "Failed to create CNAME for $record_name, attempting to restore original A record"
        if create_dns_a "$zone_id" "$api_token" "$record_name" "$CLOUD_IP" "$record_proxied" >/dev/null; then
            log_ok "Restored original A record for $record_name"
        else
            log_error "CRITICAL: Could not restore A record for $record_name, manual intervention required!"
        fi
        continue
    fi

    SWITCHED+=("$record_name")
    PARTIALLY_SWITCHED+=("$record_name")
    log_ok "Switched: $record_name"
done

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "============================================================"
echo "  FAILOVER COMPLETE"
echo "============================================================"
echo ""
echo "  Domains switched to Docker (${#SWITCHED[@]} total):"
for h in "${SWITCHED[@]}"; do
    echo "    - $h"
done
echo ""
echo "  Backups saved to: $BACKUP_DIR"
echo "  To rollback:      ./scripts/cf-rollback.sh"
echo ""
