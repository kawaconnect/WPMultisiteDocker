#!/usr/bin/env bash
# =============================================================================
# cf-setup.sh, Create tunnels, configure standby, generate docker-compose
# =============================================================================
# Usage: ./scripts/cf-setup.sh
#
# Idempotent, safe to run multiple times. Reuses existing tunnels.
#
# SAFETY:
#   - Only configures the STANDBY tunnel (*.standby_domain) ingress + DNS
#   - Failover tunnels are created but LEFT IDLE (catch-all 404, no DNS changes)
#   - Backs up ALL accounts before touching anything
#   - Never modifies DNS on non-standby domains
#
# DOMAIN MODES:
#   - Each domain can be in CF_ACCOUNT_{N}_FAILOVER or not
#   - Not in failover list (default) → ACTIVE, routed through standby tunnel now
#   - In failover list → INACTIVE, configs ready but not touched until failover
#   - Interactive mode lists all domains and lets you toggle the flag
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/cf-common.sh"

echo "=== Cloudflare Tunnel Setup ==="
echo ""

# ── Step 1: Validate ──────────────────────────────────────────────────────────

echo "[1/7] Checking dependencies and config..."
check_deps
load_config

STANDBY_DOMAIN="$CF_STANDBY_DOMAIN"
ACCOUNT_COUNT=$(get_account_count)

echo ""
echo "[2/7] Validating API tokens..."
for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    name=$(get_account_var "$n" NAME)
    token=$(get_account_var "$n" TOKEN)
    validate_token "$token" "$name"
done

# ── Step 2: Interactive domain review ────────────────────────────────────────

echo ""
echo "[3/7] Domain configuration..."
echo ""
echo "  ACTIVE domains are routed through the standby tunnel now."
echo "  FAILOVER domains have configs ready but are not touched until failover."
echo ""

# Collect all non-standby domains for review
declare -a ALL_DOMAIN_ACCT_N=()
declare -a ALL_DOMAIN_NAMES=()
declare -a ALL_DOMAIN_FAILOVER=()
declare -a ALL_DOMAIN_ACCT_NAMES=()

for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    is_standby=$(get_account_var "$n" STANDBY)
    [ "$is_standby" = "true" ] && continue

    acct_name=$(get_account_var "$n" NAME)
    read -ra domains <<< "$(get_account_domains "$n")"
    for domain in "${domains[@]}"; do
        ALL_DOMAIN_ACCT_N+=("$n")
        ALL_DOMAIN_ACCT_NAMES+=("$acct_name")
        ALL_DOMAIN_NAMES+=("$domain")
        if is_domain_failover "$n" "$domain"; then
            ALL_DOMAIN_FAILOVER+=("true")
        else
            ALL_DOMAIN_FAILOVER+=("false")
        fi
    done
done

# Helper to print the domain table
print_domain_table() {
    printf "  %-4s %-15s %-30s %-12s %s\n" "#" "ACCOUNT" "DOMAIN" "MODE" "STANDBY ACCESS"
    printf "  %-4s %-15s %-30s %-12s %s\n" "-" "-------" "------" "----" "--------------"
    for ((x = 0; x < ${#ALL_DOMAIN_NAMES[@]}; x++)); do
        local num=$((x + 1))
        if [ "${ALL_DOMAIN_FAILOVER[$x]}" = "true" ]; then
            printf "  %-4s %-15s %-30s %-12s %s\n" "$num" "${ALL_DOMAIN_ACCT_NAMES[$x]}" "${ALL_DOMAIN_NAMES[$x]}" "FAILOVER" "(inactive)"
        else
            local standby_host="${ALL_DOMAIN_NAMES[$x]%%.*}.$STANDBY_DOMAIN"
            printf "  %-4s %-15s %-30s %-12s %s\n" "$num" "${ALL_DOMAIN_ACCT_NAMES[$x]}" "${ALL_DOMAIN_NAMES[$x]}" "ACTIVE" "$standby_host"
        fi
    done
}

if [ ${#ALL_DOMAIN_NAMES[@]} -gt 0 ]; then
    echo "  Current domain configuration:"
    echo ""
    print_domain_table
    echo ""
    echo -n "  Toggle a domain? Enter number, or press Enter to continue: "
    read -r toggle_input

    while [ -n "$toggle_input" ]; do
        idx=$((toggle_input - 1))
        if [ "$idx" -ge 0 ] && [ "$idx" -lt ${#ALL_DOMAIN_NAMES[@]} ]; then
            local_domain="${ALL_DOMAIN_NAMES[$idx]}"
            acct_n="${ALL_DOMAIN_ACCT_N[$idx]}"

            if [ "${ALL_DOMAIN_FAILOVER[$idx]}" = "true" ]; then
                # Remove domain from failover list
                ALL_DOMAIN_FAILOVER[$idx]="false"
                current=$(get_account_var "$acct_n" FAILOVER)
                new=$(echo "$current" | sed "s/$local_domain//" | sed 's/,,/,/g' | sed 's/^,//;s/,$//')
                if grep -q "^CF_ACCOUNT_${acct_n}_FAILOVER=" "$ENV_FILE"; then
                    sed -i "s/^CF_ACCOUNT_${acct_n}_FAILOVER=.*/CF_ACCOUNT_${acct_n}_FAILOVER=$new/" "$ENV_FILE"
                else
                    echo "CF_ACCOUNT_${acct_n}_FAILOVER=$new" >> "$ENV_FILE"
                fi
                eval "CF_ACCOUNT_${acct_n}_FAILOVER=\"$new\""
                log_info "Toggled $local_domain → ACTIVE"
            else
                # Add domain to failover list
                ALL_DOMAIN_FAILOVER[$idx]="true"
                current=$(get_account_var "$acct_n" FAILOVER)
                if [ -z "$current" ]; then
                    new="$local_domain"
                else
                    new="$current,$local_domain"
                fi
                if grep -q "^CF_ACCOUNT_${acct_n}_FAILOVER=" "$ENV_FILE"; then
                    sed -i "s/^CF_ACCOUNT_${acct_n}_FAILOVER=.*/CF_ACCOUNT_${acct_n}_FAILOVER=$new/" "$ENV_FILE"
                else
                    echo "CF_ACCOUNT_${acct_n}_FAILOVER=$new" >> "$ENV_FILE"
                fi
                eval "CF_ACCOUNT_${acct_n}_FAILOVER=\"$new\""
                log_info "Toggled $local_domain → FAILOVER"
            fi
        else
            log_warn "Invalid number: $toggle_input"
        fi

        echo ""
        print_domain_table
        echo ""
        echo -n "  Toggle a domain? Enter number, or press Enter to continue: "
        read -r toggle_input
    done
fi

# ── Step 3: Mandatory backup ─────────────────────────────────────────────────

echo ""
echo "[4/7] Running mandatory backup..."
require_backup "setup"

# ── Step 4: Create/reuse tunnels ──────────────────────────────────────────────

echo "[5/7] Creating/reusing tunnels..."

# Arrays to collect data for docker-compose generation
declare -a TUNNEL_NAMES=()
declare -a TUNNEL_TOKENS=()

for ((n = 1; n <= ACCOUNT_COUNT; n++)); do
    name=$(get_account_var "$n" NAME)
    account_id=$(get_account_var "$n" ID)
    api_token=$(get_account_var "$n" TOKEN)
    is_standby=$(get_account_var "$n" STANDBY)
    tunnel_name="wp-docker-$name"

    echo ""
    echo "  Account: $name ($account_id)"

    # Check if tunnel already exists
    existing=$(get_tunnel_by_name "$account_id" "$api_token" "$tunnel_name")
    if [ -n "$existing" ]; then
        tunnel_id=$(echo "$existing" | jq -r '.id')
        log_ok "Tunnel exists: $tunnel_name ($tunnel_id)"
    else
        log_info "Creating tunnel: $tunnel_name"
        tunnel_json=$(create_tunnel "$account_id" "$api_token" "$tunnel_name")
        tunnel_id=$(echo "$tunnel_json" | jq -r '.id')
        log_ok "Tunnel created: $tunnel_name ($tunnel_id)"
    fi

    # Get connector token
    connector_token=$(get_tunnel_token "$account_id" "$api_token" "$tunnel_id")
    log_ok "Got connector token"

    TUNNEL_NAMES+=("$name")
    TUNNEL_TOKENS+=("$connector_token")

    # ── Configure ingress: ONLY for standby ──────────────────────────────
    if [ "$is_standby" = "true" ]; then
        # Standby: *.standby_domain → nginx + catch-all 404
        ingress=$(jq -n \
            --arg host "*.$STANDBY_DOMAIN" \
            '[
                {hostname: $host, service: "http://nginx:80"},
                {service: "http_status:404"}
            ]')
        log_info "Configuring standby ingress: *.$STANDBY_DOMAIN → http://nginx:80"
        configure_tunnel_ingress "$account_id" "$api_token" "$tunnel_id" "$ingress" >/dev/null
        log_ok "Standby ingress configured"

        # Create wildcard DNS CNAME on standby domain only
        read -ra standby_domains <<< "$(get_account_domains "$n")"
        domain="${standby_domains[0]}"
        zone_id=$(get_zone_id "$domain" "$api_token")
        cname_target="${tunnel_id}.cfargotunnel.com"

        # Check if wildcard CNAME already exists
        existing_cname=$(get_dns_records "$zone_id" "$api_token" "CNAME" "*.$domain")
        existing_count=$(echo "$existing_cname" | jq 'length')

        if [ "$existing_count" -gt 0 ]; then
            log_ok "Wildcard DNS exists: *.$domain → $(echo "$existing_cname" | jq -r '.[0].content')"
        else
            log_info "Creating wildcard DNS: *.$domain → $cname_target"
            create_dns_cname "$zone_id" "$api_token" "*.$domain" "$cname_target" >/dev/null
            log_ok "Wildcard DNS created"
        fi
    else
        # Failover: DO NOT configure ingress or touch DNS
        log_ok "Failover tunnel ready (idle, no DNS or ingress changes)"
    fi
done

# ── Step 5: Generate docker-compose.tunnels.yml ──────────────────────────────

echo ""
echo "[6/7] Generating docker-compose.tunnels.yml..."

COMPOSE_TUNNELS="$PROJECT_DIR/docker-compose.tunnels.yml"

# Derive network name from project directory (same convention as docker compose)
PROJECT_NAME=$(basename "$PROJECT_DIR" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]//g')
NETWORK_NAME="${PROJECT_NAME}_frontend"

{
    cat <<HEADER
# Auto-generated by cf-setup.sh, do not edit manually
# Re-run ./scripts/cf-setup.sh to regenerate

services:
HEADER

    for ((j = 0; j < ${#TUNNEL_NAMES[@]}; j++)); do
        svc_name="${TUNNEL_NAMES[$j]}"
        svc_token="${TUNNEL_TOKENS[$j]}"
        cat <<EOF
  cloudflared-${svc_name}:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    profiles: [tunnels]
    networks: [frontend]
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: "${svc_token}"
    deploy:
      resources:
        limits:
          memory: 128M

EOF
    done

    cat <<EOF
networks:
  frontend:
    external: true
    name: ${NETWORK_NAME}
EOF
} > "$COMPOSE_TUNNELS"

log_ok "Generated: docker-compose.tunnels.yml (${#TUNNEL_NAMES[@]} tunnel service(s))"

# ── Step 6: Set COMPOSE_FILE in .env ─────────────────────────────────────────

echo ""
echo "[7/7] Updating .env..."

COMPOSE_LINE="COMPOSE_FILE=docker-compose.yml:docker-compose.tunnels.yml"

if [ -f "$ENV_FILE" ]; then
    if grep -q "^COMPOSE_FILE=" "$ENV_FILE"; then
        # Update existing line
        sed -i "s|^COMPOSE_FILE=.*|$COMPOSE_LINE|" "$ENV_FILE"
        log_ok "Updated COMPOSE_FILE in .env"
    else
        # Append
        echo "" >> "$ENV_FILE"
        echo "# --- Docker Compose files ---" >> "$ENV_FILE"
        echo "$COMPOSE_LINE" >> "$ENV_FILE"
        log_ok "Added COMPOSE_FILE to .env"
    fi
else
    log_warn ".env not found, create it from .env.example and re-run"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Tunnels created:"
for ((j = 0; j < ${#TUNNEL_NAMES[@]}; j++)); do
    echo "  - cloudflared-${TUNNEL_NAMES[$j]}"
done
echo ""
echo "ONLY the standby tunnel (*.$STANDBY_DOMAIN) has been configured."
echo "Failover tunnels are idle, they will NOT touch real domains"
echo "until you explicitly run: ./scripts/cf-failover.sh"
echo ""

# Show domain summary
if [ ${#ALL_DOMAIN_NAMES[@]} -gt 0 ]; then
    echo "Active domains (accessible now via *.$STANDBY_DOMAIN):"
    for ((x = 0; x < ${#ALL_DOMAIN_NAMES[@]}; x++)); do
        if [ "${ALL_DOMAIN_FAILOVER[$x]}" != "true" ]; then
            standby_host="${ALL_DOMAIN_NAMES[$x]%%.*}.$STANDBY_DOMAIN"
            echo "  - ${ALL_DOMAIN_NAMES[$x]} → $standby_host"
        fi
    done
    echo ""
    has_failover=false
    for ((x = 0; x < ${#ALL_DOMAIN_NAMES[@]}; x++)); do
        if [ "${ALL_DOMAIN_FAILOVER[$x]}" = "true" ]; then
            if [ "$has_failover" = false ]; then
                echo "Failover domains (configs ready, not touched until failover):"
                has_failover=true
            fi
            echo "  - ${ALL_DOMAIN_NAMES[$x]}"
        fi
    done
    [ "$has_failover" = true ] && echo ""
fi

echo "Next steps:"
echo "  1. Start tunnels:    docker compose --profile tunnels up -d"
echo "  2. Verify:           ./scripts/cf-status.sh"
echo "  3. Test standby:     curl -H 'Host: test.$STANDBY_DOMAIN' http://localhost:8080"
