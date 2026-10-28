#!/usr/bin/env bash
# =============================================================================
# cf-status.sh, Show current state of all tunnels + DNS
# =============================================================================
# Usage: ./scripts/cf-status.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/cf-common.sh"

cleanup_status() {
    local exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        echo "" >&2
        log_error "Status check interrupted (exit $exit_code)."
    fi
}
trap cleanup_status EXIT

echo "=== Cloudflare Status ==="
echo ""

# ── Validate ──────────────────────────────────────────────────────────────────

check_deps
load_config --read-only

CLOUD_IP="${CF_CLOUD_IP:-}"
STANDBY_DOMAIN="${CF_STANDBY_DOMAIN:-}"
ACCOUNT_COUNT=$(get_account_count)

# ── Tunnels ──────────────────────────────────────────────────────────────────

echo "── Tunnels ──────────────────────────────────────────────"
printf "  %-20s %-38s %-10s %s\n" "ACCOUNT" "TUNNEL ID" "STATUS" "HOSTNAMES"
printf "  %-20s %-38s %-10s %s\n" "-------" "---------" "------" "---------"

for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    name=$(get_account_var "$n" NAME)
    account_id=$(get_account_var "$n" ID)
    api_token=$(get_account_var "$n" TOKEN)
    is_standby=$(get_account_var "$n" STANDBY)
    tunnel_name="wp-docker-$name"

    tunnel_json=$(get_configured_tunnel "$account_id" "$api_token" "$name" 2>/dev/null) || tunnel_json=""
    if [ -z "$tunnel_json" ]; then
        printf "  %-20s %-38s %-10s %s\n" "$name" "(not created)" "-" "-"
        continue
    fi

    tunnel_id=$(echo "$tunnel_json" | jq -r '.id')
    tunnel_status=$(echo "$tunnel_json" | jq -r '.status // "unknown"')

    # Get ingress hostnames
    config=$(get_tunnel_config "$account_id" "$api_token" "$tunnel_id" 2>/dev/null) || config=""
    if [ -n "$config" ]; then
        hostnames=$(echo "$config" | jq -r '.config.ingress[]?.hostname // empty' 2>/dev/null | tr '\n' ', ' | sed 's/,$//')
        [ -z "$hostnames" ] && hostnames="(catch-all only)"
    else
        hostnames="(no config)"
    fi

    # Indicate standby/failover
    label="$name"
    [ "$is_standby" = "true" ] && label="$name [standby]"

    printf "  %-20s %-38s %-10s %s\n" "$label" "$tunnel_id" "$tunnel_status" "$hostnames"
done

# ── DNS Records ──────────────────────────────────────────────────────────────

echo ""
echo "── DNS Records ────────────────────────────────────────────"
printf "  %-35s %-8s %-45s %s\n" "NAME" "TYPE" "CONTENT" "PROXIED"
printf "  %-35s %-8s %-45s %s\n" "----" "----" "-------" "-------"

for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    api_token=$(get_account_var "$n" TOKEN)

    read -ra domains <<< "$(get_account_domains "$n")"
    for domain in "${domains[@]}"; do
        zone_id=$(get_zone_id "$domain" "$api_token" 2>/dev/null) || continue

        # Get A, AAAA, and CNAME records
        records=$(get_dns_records "$zone_id" "$api_token" 2>/dev/null) || continue

        # Filter to relevant types
        filtered=$(echo "$records" | jq '[.[] | select(.type == "A" or .type == "AAAA" or .type == "CNAME")]')
        count=$(echo "$filtered" | jq 'length')

        for ((r = 0; r < count; r++)); do
            rec_name=$(echo "$filtered" | jq -r ".[$r].name")
            rec_type=$(echo "$filtered" | jq -r ".[$r].type")
            rec_content=$(echo "$filtered" | jq -r ".[$r].content")
            rec_proxied=$(echo "$filtered" | jq -r ".[$r].proxied")

            # Truncate long content
            if [ ${#rec_content} -gt 44 ]; then
                rec_content="${rec_content:0:41}..."
            fi

            printf "  %-35s %-8s %-45s %s\n" "$rec_name" "$rec_type" "$rec_content" "$rec_proxied"
        done
    done
done

# ── Docker Containers ────────────────────────────────────────────────────────

echo ""
echo "── Docker Tunnel Containers ────────────────────────────────"

cd "$PROJECT_DIR"
printf "  %-35s %-12s %s\n" "CONTAINER" "STATUS" "UPTIME"
printf "  %-35s %-12s %s\n" "---------" "------" "------"

tunnel_compose=(docker compose --profile tunnels)
start_hint="docker compose --profile tunnels up -d"
if [ -f "$PROJECT_DIR/docker-compose.wpops-tunnels.json" ] && [ -f "$PROJECT_DIR/config/wpops.json" ]; then
    node=$(jq -r '.node' "$PROJECT_DIR/config/wpops.json")
    tunnel_compose=(docker compose -p "wpops-$node-tunnels" -f "$PROJECT_DIR/docker-compose.wpops-tunnels.json")
    start_hint="wpops tunnels ensure"
fi
tunnel_containers=$("${tunnel_compose[@]}" ps --format '{{.Name}}\t{{.State}}\t{{.Status}}' 2>/dev/null | grep cloudflared || true)

if [ -z "$tunnel_containers" ]; then
    echo "  (no tunnel containers running)"
    echo "  Start with: $start_hint"
else
    while IFS=$'\t' read -r cname cstate cstatus; do
        printf "  %-35s %-12s %s\n" "$cname" "$cstate" "$cstatus"
    done <<< "$tunnel_containers"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "── Summary ──────────────────────────────────────────────────"
echo "  Standby domain:  *.$STANDBY_DOMAIN"
echo "  Cloud IP:        $CLOUD_IP"
echo "  Accounts:        $ACCOUNT_COUNT"

# Check if any domain points at tunnel (failover active)
failover_active=false
for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    is_standby=$(get_account_var "$n" STANDBY)
    [ "$is_standby" = "true" ] && continue

    api_token=$(get_account_var "$n" TOKEN)
    read -ra domains <<< "$(get_account_domains "$n")"
    for domain in "${domains[@]}"; do
        zone_id=$(get_zone_id "$domain" "$api_token" 2>/dev/null) || continue
        cnames=$(get_dns_records "$zone_id" "$api_token" "CNAME" 2>/dev/null) || continue
        tunnel_cnames=$(echo "$cnames" | jq '[.[] | select(.content | endswith(".cfargotunnel.com"))] | length')
        if [ "$tunnel_cnames" -gt 0 ]; then
            failover_active=true
            break 2
        fi
    done
done

if [ "$failover_active" = true ]; then
    echo "  Mode:            FAILOVER ACTIVE (domains pointing at Docker)"
    echo ""
    echo "  To rollback:     ./scripts/cf-rollback.sh"
else
    echo "  Mode:            Normal (domains on production, standby ready)"
fi

# Show domain modes
echo ""
echo "── Domain Modes ──────────────────────────────────────────────"
printf "  %-30s %-12s %s\n" "DOMAIN" "MODE" "STANDBY ACCESS"
printf "  %-30s %-12s %s\n" "------" "----" "--------------"
for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    is_standby=$(get_account_var "$n" STANDBY)
    [ "$is_standby" = "true" ] && continue

    read -ra domains <<< "$(get_account_domains "$n")"
    for domain in "${domains[@]}"; do
        if is_domain_failover "$n" "$domain"; then
            printf "  %-30s %-12s %s\n" "$domain" "FAILOVER" "(inactive)"
        else
            standby_host="${domain%%.*}.$STANDBY_DOMAIN"
            printf "  %-30s %-12s %s\n" "$domain" "ACTIVE" "$standby_host"
        fi
    done
done

echo ""
