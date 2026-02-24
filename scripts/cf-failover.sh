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

# ── Step 1: Validate ─────────────────────────────────────────────────────────

echo "[1/6] Checking dependencies and config..."
check_deps
load_config

CLOUD_IP="$CF_CLOUD_IP"
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
    is_standby=$(get_account_var "$n" STANDBY)
    [ "$is_standby" = "true" ] && continue

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

# ── Step 7: Execute changes ──────────────────────────────────────────────────

echo "[5/6] Switching DNS records..."
echo ""

SWITCHED=()
declare -A INGRESS_BY_TUNNEL  # tunnel_id → space-separated hostnames

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

    # Create CNAME pointing to tunnel
    create_dns_cname "$zone_id" "$api_token" "$record_name" "$cname_target" "$record_proxied" >/dev/null

    SWITCHED+=("$record_name")
    INGRESS_BY_TUNNEL[$tunnel_id]+="$record_name "
    log_ok "Switched: $record_name"
done

# ── Step 8: Configure tunnel ingress ─────────────────────────────────────────

echo ""
echo "[6/6] Configuring tunnel ingress..."

for tunnel_id in "${!INGRESS_BY_TUNNEL[@]}"; do
    hosts="${INGRESS_BY_TUNNEL[$tunnel_id]}"

    # Find the account for this tunnel
    for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
        is_standby=$(get_account_var "$n" STANDBY)
        [ "$is_standby" = "true" ] && continue

        account_id=$(get_account_var "$n" ID)
        api_token=$(get_account_var "$n" TOKEN)
        tunnel_name="wp-docker-$(get_account_var "$n" NAME)"
        tj=$(get_tunnel_by_name "$account_id" "$api_token" "$tunnel_name" 2>/dev/null)
        [ -z "$tj" ] && continue
        tid=$(echo "$tj" | jq -r '.id')
        if [ "$tid" = "$tunnel_id" ]; then
            # Build ingress JSON
            ingress="["
            for host in $hosts; do
                ingress+="{\"hostname\": \"$host\", \"service\": \"http://nginx:80\"},"
            done
            ingress+="{\"service\": \"http_status:404\"}]"

            host_count=$(echo "$hosts" | wc -w)
            log_info "Configuring tunnel ingress ($host_count hostname(s))"
            configure_tunnel_ingress "$account_id" "$api_token" "$tunnel_id" "$ingress" >/dev/null
            log_ok "Tunnel ingress updated"
            break
        fi
    done
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
