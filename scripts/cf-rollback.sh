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

echo "============================================================"
echo "  CLOUDFLARE ROLLBACK, RESTORE REAL DOMAINS TO PRODUCTION"
echo "============================================================"
echo ""
echo "  This will:"
echo "    - Change DNS CNAME records back to A records"
echo "    - Point real domains back at production IP ($( cfg '.cloud_ip'))"
echo "    - Clear tunnel ingress configurations"
echo ""
echo "  This will NOT touch:"
echo "    - The standby domain (*.$( cfg '.standby_domain'))"
echo ""

# ── Step 1: First confirmation ───────────────────────────────────────────────

confirm_dangerous "ROLLBACK" \
    "This will modify live DNS for real customer domains."

# ── Step 2: Validate ─────────────────────────────────────────────────────────

echo "[1/5] Checking dependencies and config..."
check_deps
load_config

CLOUD_IP=$(cfg '.cloud_ip')
ACCOUNT_COUNT=$(cfg '.accounts | length')

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

for ((i = 0; i < ACCOUNT_COUNT; i++)); do
    is_standby=$(cfg ".accounts[$i].is_standby")
    [ "$is_standby" = "true" ] && continue

    name=$(cfg ".accounts[$i].name")
    account_id=$(cfg ".accounts[$i].account_id")
    api_token=$(cfg ".accounts[$i].api_token")
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
    domain_count=$(cfg ".accounts[$i].domains | length")
    for ((d = 0; d < domain_count; d++)); do
        domain=$(get_domain_name "$i" "$d")

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

            PLAN_ACCOUNTS+=("$i")
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

declare -A RESET_TUNNELS
for ((i = 0; i < ACCOUNT_COUNT; i++)); do
    is_standby=$(cfg ".accounts[$i].is_standby")
    [ "$is_standby" = "true" ] && continue

    account_id=$(cfg ".accounts[$i].account_id")
    api_token=$(cfg ".accounts[$i].api_token")
    tunnel_name="wp-docker-$(cfg ".accounts[$i].name")"

    tunnel_json=$(get_tunnel_by_name "$account_id" "$api_token" "$tunnel_name" 2>/dev/null)
    if [ -n "$tunnel_json" ]; then
        tunnel_id=$(echo "$tunnel_json" | jq -r '.id')
        if [ -z "${RESET_TUNNELS[$tunnel_id]:-}" ]; then
            ingress='[{"service": "http_status:404"}]'
            log_info "Resetting tunnel ingress to catch-all 404"
            configure_tunnel_ingress "$account_id" "$api_token" "$tunnel_id" "$ingress" >/dev/null
            log_ok "Tunnel ingress cleared"
            RESET_TUNNELS[$tunnel_id]=1
        fi
    fi
done

# ── Step 7: Execute DNS changes ──────────────────────────────────────────────

echo ""
echo "[5/5] Restoring DNS records..."
echo ""

RESTORED=()

for ((p = 0; p < PLAN_COUNT; p++)); do
    idx="${PLAN_ACCOUNTS[$p]}"
    zone_id="${PLAN_ZONE_IDS[$p]}"
    record_id="${PLAN_RECORD_IDS[$p]}"
    record_name="${PLAN_NAMES[$p]}"
    record_proxied="${PLAN_PROXIED[$p]}"
    api_token=$(cfg ".accounts[$idx].api_token")

    log_info "Restoring: $record_name (CNAME → A $CLOUD_IP)"

    # Delete the CNAME
    delete_dns_record "$zone_id" "$api_token" "$record_id"

    # Create A record pointing at cloud IP
    create_dns_a "$zone_id" "$api_token" "$record_name" "$CLOUD_IP" "$record_proxied" >/dev/null

    RESTORED+=("$record_name")
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
echo "  Standby tunnel (*.$( cfg '.standby_domain')) is unaffected."
echo "  Backups saved to: $BACKUP_DIR"
echo ""
