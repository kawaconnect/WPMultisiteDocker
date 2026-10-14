#!/usr/bin/env bash
# =============================================================================
# cf-rollback.sh, Restore DNS, switch real domains back to production
# =============================================================================
# Usage: ./scripts/cf-rollback.sh
#
# THIS IS A DANGEROUS OPERATION. It modifies live DNS for real domains.
#
# Safety:
#   1. Full backup of ALL accounts runs first (mandatory, cannot skip)
#   2. If backup fails → script aborts, nothing is touched
#   3. Shows exactly which records will change (dry-run preview)
#   4. Requires you to type ROLLBACK to confirm
#   5. Requires a second confirmation after seeing the change list
#   6. Only THEN does it modify DNS
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/cf-common.sh"

# Track partially-restored records for trap handler
declare -a PARTIALLY_RESTORED=()

cleanup_rollback() {
    local exit_code=$?
    if [ "$exit_code" -ne 0 ] && [ ${#PARTIALLY_RESTORED[@]} -gt 0 ]; then
        echo "" >&2
        log_error "Rollback interrupted (exit $exit_code). The following records were already restored:"
        for rec in "${PARTIALLY_RESTORED[@]}"; do
            log_error "  - $rec"
        done
        log_error "Re-run ./scripts/cf-rollback.sh to continue restoring remaining records."
    fi
}
trap cleanup_rollback EXIT

# ── Step 1: Validate ─────────────────────────────────────────────────────────

echo "[1/5] Checking dependencies and config..."
check_deps
load_config

CLOUD_IP="$CF_CLOUD_IP"
ACCOUNT_COUNT=$(get_account_count)

echo ""
echo "============================================================"
echo "  CLOUDFLARE ROLLBACK, RESTORE REAL DOMAINS TO PRODUCTION"
echo "============================================================"
echo ""
echo "  This will:"
echo "    - Change DNS CNAME records back to A records"
echo "    - Point real domains back at production IP ($CF_CLOUD_IP)"
echo "    - Clear tunnel ingress configurations"
echo ""
echo "  This will NOT touch:"
echo "    - The standby domain (*.$CF_STANDBY_DOMAIN)"
echo ""

# ── Step 2: First confirmation ───────────────────────────────────────────────

confirm_dangerous "ROLLBACK" \
    "This will modify live DNS for real customer domains."

# ── Step 3: Mandatory backup ─────────────────────────────────────────────────

echo ""
echo "[2/5] Running mandatory backup (ALL accounts)..."
require_backup "rollback"

# ── Step 4: Dry-run, show exactly what will change ──────────────────────────

echo "[3/5] Scanning DNS records (DRY RUN, nothing changed yet)..."
echo ""

# Collect planned changes
declare -a PLAN_ACCOUNTS=()
declare -a PLAN_ZONE_IDS=()
declare -a PLAN_RECORD_IDS=()
declare -a PLAN_NAMES=()
declare -a PLAN_PROXIED=()
declare -a PLAN_TUNNEL_IDS=()
PLAN_COUNT=0

for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    name=$(get_account_var "$n" NAME)
    account_id=$(get_account_var "$n" ID)
    api_token=$(get_account_var "$n" TOKEN)
    tunnel_name="wp-docker-$name"

    echo "  Account: $name"

    # Get tunnel ID for CNAME matching
    tunnel_json=$(get_tunnel_by_name "$account_id" "$api_token" "$tunnel_name" 2>/dev/null)
    tunnel_id=""
    cname_target=""
    if [ -n "$tunnel_json" ]; then
        tunnel_id=$(echo "$tunnel_json" | jq -r '.id')
        cname_target="${tunnel_id}.cfargotunnel.com"
    fi

    # Process each domain
    read -ra domains <<< "$(get_account_domains "$n")"
    for domain in "${domains[@]}"; do
        # The standby domain itself is infrastructure, never touch it here.
        [ "$domain" = "$CF_STANDBY_DOMAIN" ] && continue
        zone_id=$(get_zone_id "$domain" "$api_token" 2>/dev/null) || continue

        # Find CNAME records pointing at the tunnel
        cname_records=$(get_dns_records "$zone_id" "$api_token" "CNAME" 2>/dev/null) || continue

        matching=""
        if [ -n "$cname_target" ]; then
            matching=$(echo "$cname_records" | jq --arg target "$cname_target" \
                '[.[] | select(.content == $target)]')
        else
            # No tunnel found, look for any cfargotunnel.com CNAMEs
            matching=$(echo "$cname_records" | jq \
                '[.[] | select(.content | endswith(".cfargotunnel.com"))]')
        fi

        match_count=$(echo "$matching" | jq 'length')
        if [ "$match_count" -eq 0 ]; then
            log_info "$domain: no tunnel CNAMEs found"
            continue
        fi

        for ((r = 0; r < match_count; r++)); do
            record_id=$(echo "$matching" | jq -r ".[$r].id")
            record_name=$(echo "$matching" | jq -r ".[$r].name")
            record_proxied=$(echo "$matching" | jq -r ".[$r].proxied")
            record_content=$(echo "$matching" | jq -r ".[$r].content")

            PLAN_ACCOUNTS+=("$n")
            PLAN_ZONE_IDS+=("$zone_id")
            PLAN_RECORD_IDS+=("$record_id")
            PLAN_NAMES+=("$record_name")
            PLAN_PROXIED+=("$record_proxied")
            PLAN_TUNNEL_IDS+=("${tunnel_id:-unknown}")
            PLAN_COUNT=$((PLAN_COUNT + 1))
        done
    done
    echo ""
done

# ── Show the change plan ─────────────────────────────────────────────────────

if [ "$PLAN_COUNT" -eq 0 ]; then
    echo ""
    echo "No tunnel CNAMEs found. Domains may already point at production."
    exit 0
fi

echo ""
echo "  ============================================================"
echo "  PLANNED CHANGES ($PLAN_COUNT record(s) will be modified):"
echo "  ============================================================"
echo ""
printf "  %-40s %-30s → %s\n" "HOSTNAME" "CURRENT" "NEW"
printf "  %-40s %-30s   %s\n" "--------" "-------" "---"
for ((p = 0; p < PLAN_COUNT; p++)); do
    printf "  %-40s CNAME (tunnel) %16s → A %s\n" "${PLAN_NAMES[$p]}" "" "$CLOUD_IP"
done
echo ""

# ── Step 5: Second confirmation ──────────────────────────────────────────────

echo "  Backup location: $BACKUP_DIR"
echo ""

confirm_yes "You are about to modify $PLAN_COUNT live DNS record(s). Proceed?"

# ── Step 6: Reset tunnel ingress first ───────────────────────────────────────

echo "[4/5] Resetting tunnel ingress..."

# Clear ingress only for the hostnames actually being rolled back on each
# tunnel, NOT a blind wipe. A tunnel may carry unrelated standing rules
# (e.g. the standby account's own *.STANDBY_DOMAIN wildcard) that must
# survive a rollback of one of its other (production-domain) hosts.
declare -A HOSTS_BY_TUNNEL
declare -A ACCOUNT_BY_TUNNEL
for ((p = 0; p < PLAN_COUNT; p++)); do
    tid="${PLAN_TUNNEL_IDS[$p]}"
    [ "$tid" = "unknown" ] && continue
    HOSTS_BY_TUNNEL[$tid]+="${PLAN_NAMES[$p]} "
    ACCOUNT_BY_TUNNEL[$tid]="${PLAN_ACCOUNTS[$p]}"
done

for tunnel_id in "${!HOSTS_BY_TUNNEL[@]}"; do
    acct_n="${ACCOUNT_BY_TUNNEL[$tunnel_id]}"
    account_id=$(get_account_var "$acct_n" ID)
    api_token=$(get_account_var "$acct_n" TOKEN)
    hosts="${HOSTS_BY_TUNNEL[$tunnel_id]}"
    host_count=$(echo "$hosts" | wc -w)
    log_info "Clearing ingress for $host_count hostname(s) (unrelated rules preserved)"
    remove_tunnel_ingress_hosts "$account_id" "$api_token" "$tunnel_id" "$hosts" >/dev/null
    log_ok "Tunnel ingress updated"
done

# ── Step 7: Execute DNS changes ──────────────────────────────────────────────

echo ""
echo "[5/5] Restoring DNS records..."
echo ""

RESTORED=()

for ((p = 0; p < PLAN_COUNT; p++)); do
    acct_n="${PLAN_ACCOUNTS[$p]}"
    zone_id="${PLAN_ZONE_IDS[$p]}"
    record_id="${PLAN_RECORD_IDS[$p]}"
    record_name="${PLAN_NAMES[$p]}"
    record_proxied="${PLAN_PROXIED[$p]}"
    api_token=$(get_account_var "$acct_n" TOKEN)

    log_info "Restoring: $record_name (CNAME → A $CLOUD_IP)"

    # Delete the CNAME
    delete_dns_record "$zone_id" "$api_token" "$record_id"

    # Create A record; if this fails, attempt to restore original CNAME
    if ! create_dns_a "$zone_id" "$api_token" "$record_name" "$CLOUD_IP" "$record_proxied" >/dev/null; then
        log_error "Failed to create A record for $record_name, attempting to restore original CNAME"
        local tunnel_id_for_restore="${PLAN_TUNNEL_IDS[$p]}"
        local cname_target_restore="${tunnel_id_for_restore}.cfargotunnel.com"
        if create_dns_cname "$zone_id" "$api_token" "$record_name" "$cname_target_restore" "$record_proxied" >/dev/null; then
            log_ok "Restored original CNAME for $record_name"
        else
            log_error "CRITICAL: Could not restore CNAME for $record_name, manual intervention required!"
        fi
        continue
    fi

    RESTORED+=("$record_name")
    PARTIALLY_RESTORED+=("$record_name")
    log_ok "Restored: $record_name → $CLOUD_IP"
done

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "============================================================"
echo "  ROLLBACK COMPLETE"
echo "============================================================"
echo ""
echo "  Domains restored to production (${#RESTORED[@]} total):"
for h in "${RESTORED[@]}"; do
    echo "    - $h → $CLOUD_IP"
done
echo ""
echo "  Standby tunnel (*.$CF_STANDBY_DOMAIN) is unaffected."
echo "  Backups saved to: $BACKUP_DIR"
echo ""
