#!/usr/bin/env bash
# =============================================================================
# cf-common.sh, Shared functions for Cloudflare automation scripts
# =============================================================================
# Sourced by: cf-setup.sh, cf-failover.sh, cf-rollback.sh, cf-status.sh, cf-backup.sh
#
# SAFETY RULES:
#   - NOTHING is ever changed without a backup in place first
#   - In normal mode, only the standby domain is touched
#   - Failover/rollback require multiple explicit confirmations
# =============================================================================

# Resolve project root (parent of scripts/)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
BACKUP_DIR="$PROJECT_DIR/data/cloudflare/backups"
_CF_RUNTIME_CACHE="/tmp/.cf-runtime-cache-$$"

# CF API base URL
CF_API="https://api.cloudflare.com/client/v4"

# Zone ID cache (associative array)
declare -A _ZONE_CACHE

# =============================================================================
# Logging
# =============================================================================

log_info()  { echo "  [INFO]  $*"; }
log_ok()    { echo "  [OK]    $*"; }
log_warn()  { echo "  [WARN]  $*"; }
log_error() { echo "  [ERROR] $*" >&2; }

# =============================================================================
# Dependency checks
# =============================================================================

check_deps() {
    local missing=()
    for cmd in jq curl openssl docker; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log_error "Missing required tools: ${missing[*]}"
        log_error "Install with: apt-get install -y ${missing[*]}"
        exit 1
    fi
}

# =============================================================================
# Config loading (.env based)
# =============================================================================

load_config() {
    if [ ! -f "$ENV_FILE" ]; then
        log_error ".env not found. Copy .env.example to .env and fill in values."
        exit 1
    fi

    set -a
    source "$ENV_FILE"
    set +a

    # Validate required CF vars
    if [ "${1:-}" != "--read-only" ] && [ -z "${CF_STANDBY_DOMAIN:-}" ]; then
        log_error "CF_STANDBY_DOMAIN not set in .env"
        exit 1
    fi
    if [ "${1:-}" != "--read-only" ] && [ -z "${CF_CLOUD_IP:-}" ]; then
        log_error "CF_CLOUD_IP not set in .env"
        exit 1
    fi

    local account_count
    account_count=$(get_account_count)
    if [ "$account_count" -eq 0 ]; then
        log_error "No CF accounts configured (CF_ACCOUNT_1_NAME not set in .env)"
        exit 1
    fi

    # Validate each account
    local n
    for ((n = 1; n <= account_count; n++)); do
        local name account_id api_token
        name=$(get_account_var "$n" NAME)
        account_id=$(get_account_var "$n" ID)
        api_token=$(get_account_var "$n" TOKEN)

        if [ -z "$name" ] || [ -z "$account_id" ] || [ -z "$api_token" ]; then
            log_error "Account $n is missing NAME, ID, or TOKEN"
            exit 1
        fi

        local domains_var="CF_ACCOUNT_${n}_DOMAINS"
        if [ -n "${!domains_var:-}" ]; then
            log_info "Account $n ($name): using explicit domains from .env"
        else
            log_info "Account $n ($name): will auto-discover zones from API"
        fi
    done

    log_ok "Config loaded: $account_count account(s), standby=${CF_STANDBY_DOMAIN:-unset}"
}

# get_account_count, returns number of configured CF accounts
get_account_count() {
    local n=1
    while true; do
        local varname="CF_ACCOUNT_${n}_NAME"
        if [ -z "${!varname:-}" ]; then
            echo $((n - 1))
            return
        fi
        n=$((n + 1))
    done
}

# get_account_var N KEY, returns value of CF_ACCOUNT_{N}_{KEY}
get_account_var() {
    local n="$1" key="$2"
    local varname="CF_ACCOUNT_${n}_${key}"
    echo "${!varname:-}"
}

# get_account_domains N, prints space-separated list of domains
# Uses CF_ACCOUNT_N_DOMAINS from .env if set, otherwise auto-discovers from API
get_account_domains() {
    local n="$1"
    local varname="CF_ACCOUNT_${n}_DOMAINS"
    local domains_csv="${!varname:-}"

    if [ -n "$domains_csv" ]; then
        # Explicit domains in .env, use them (comma → space)
        IFS=',' read -ra domains <<< "$domains_csv"
        echo "${domains[@]}"
    else
        # Auto-discover from CF API
        local account_id api_token
        account_id=$(get_account_var "$n" ID)
        api_token=$(get_account_var "$n" TOKEN)
        discover_account_zones "$account_id" "$api_token"
    fi
}

# discover_account_zones ACCOUNT_ID TOKEN, returns space-separated zone names
# Results are cached per PID in temp files to avoid repeated API calls
discover_account_zones() {
    local account_id="$1" token="$2"
    local cache_file="$_CF_RUNTIME_CACHE/zones_${account_id}"

    # Return cached result if available
    if _cf_cache_ready && [ -f "$cache_file" ]; then
        cat "$cache_file"
        return
    fi

    local all_zones="" page=1
    while true; do
        local response
        response=$(cf_api GET "/zones?account.id=$account_id&status=active&per_page=50&page=$page" "$token") || {
            log_error "Failed to discover zones for account $account_id"
            return 1
        }
        local page_zones
        page_zones=$(echo "$response" | jq -r '.result[].name' 2>/dev/null | tr '\n' ' ' | sed 's/ $//')
        if [ -n "$page_zones" ]; then
            all_zones="${all_zones:+$all_zones }$page_zones"
        fi

        local total_pages
        total_pages=$(echo "$response" | jq '.result_info.total_pages // 1')
        if [ "$page" -ge "$total_pages" ]; then
            break
        fi
        ((page++))
    done

    # Cache result (only in a directory this user created; never through a
    # directory or symlink someone else planted under the predictable name)
    if _cf_cache_ready; then
        echo "$all_zones" > "$cache_file"
    fi
    echo "$all_zones"
}

# _cf_cache_ready, create the per-run cache dir privately; succeed only if it
# is a real directory owned by this user.
_cf_cache_ready() {
    [ -e "$_CF_RUNTIME_CACHE" ] || mkdir -m 700 "$_CF_RUNTIME_CACHE" 2>/dev/null || true
    [ -d "$_CF_RUNTIME_CACHE" ] && [ ! -L "$_CF_RUNTIME_CACHE" ] && [ -O "$_CF_RUNTIME_CACHE" ]
}

# is_domain_failover N DOMAIN, returns 0 (true) if domain is in failover list
is_domain_failover() {
    local n="$1" domain="$2"
    local varname="CF_ACCOUNT_${n}_FAILOVER"
    local failover_list="${!varname:-}"
    [[ ",$failover_list," == *",$domain,"* ]]
}

# =============================================================================
# Safety: confirmation prompts
# =============================================================================

# confirm_dangerous PROMPT, requires typing a specific word to proceed
# Usage: confirm_dangerous "FAILOVER" "This will switch real domains to Docker tunnels."
confirm_dangerous() {
    local keyword="$1"
    shift
    local message="$*"

    echo ""
    echo "  ============================================================"
    echo "  WARNING: DANGEROUS OPERATION"
    echo "  ============================================================"
    echo "  $message"
    echo "  ============================================================"
    echo ""
    echo -n "  Type '$keyword' to confirm (anything else aborts): "
    read -r answer
    if [ "$answer" != "$keyword" ]; then
        echo ""
        log_error "Aborted. You typed '$answer', expected '$keyword'."
        exit 1
    fi
    echo ""
}

# confirm_yes PROMPT, simple yes/no confirmation
# Usage: confirm_yes "Proceed with backup?"
confirm_yes() {
    local message="$1"
    echo ""
    echo -n "  $message [y/N]: "
    read -r answer
    if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
        log_error "Aborted by user."
        exit 1
    fi
    echo ""
}

# =============================================================================
# Safety: mandatory backup gate
# =============================================================================

# require_backup, runs a FULL backup of all accounts before any mutation.
# If the backup fails, the calling script MUST abort. No exceptions.
require_backup() {
    local caller="${1:-unknown}"

    echo ""
    echo "  ── Mandatory backup before $caller ──"
    echo ""

    local backup_errors=0

    local account_count
    account_count=$(get_account_count)

    for ((n = 1; n <= account_count; n++)); do
        local name account_id api_token tunnel_name
        name=$(get_account_var "$n" NAME)
        account_id=$(get_account_var "$n" ID)
        api_token=$(get_account_var "$n" TOKEN)
        tunnel_name="wp-docker-$name"

        log_info "Backing up: $name"

        # Backup DNS for each domain
        local domains
        read -ra domains <<< "$(get_account_domains "$n")"
        for domain in "${domains[@]}"; do
            local zone_id
            # "|| ..." keeps set -e from ending the run before the error is counted
            zone_id=$(get_zone_id "$domain" "$api_token" 2>/dev/null) || zone_id=""
            if [ -z "$zone_id" ]; then
                log_error "Could not get zone ID for $domain"
                backup_errors=$((backup_errors + 1))
                continue
            fi
            backup_dns_zone "$domain" "$zone_id" "$api_token" || backup_errors=$((backup_errors + 1))
        done

        # Backup tunnel config if tunnel exists
        local tunnel_json
        if ! tunnel_json=$(get_configured_tunnel "$account_id" "$api_token" "$name" 2>/dev/null); then
            log_error "Could not look up the tunnel for $name"
            backup_errors=$((backup_errors + 1))
            tunnel_json=""
        fi
        if [ -n "$tunnel_json" ]; then
            local tunnel_id
            tunnel_id=$(echo "$tunnel_json" | jq -r '.id')
            backup_tunnel_config "$name" "$account_id" "$api_token" "$tunnel_id" || backup_errors=$((backup_errors + 1))
        fi
    done

    if [ "$backup_errors" -gt 0 ]; then
        echo ""
        log_error "Backup failed with $backup_errors error(s)."
        log_error "ABORTING $caller, cannot proceed without a clean backup."
        log_error "Fix the errors above and try again."
        exit 1
    fi

    log_ok "Backup complete, safe to proceed with $caller"
    echo ""
}

# =============================================================================
# Cloudflare API wrapper
# =============================================================================

# cf_api METHOD PATH TOKEN [BODY]
# Returns JSON response body. Exits on HTTP error.
cf_api() {
    local method="$1" path="$2" token="$3" body="${4:-}"
    local url="$CF_API$path"
    local response http_code

    # The token and the body (a tunnel secret, for create_tunnel) are passed
    # through a file descriptor and stdin, never as arguments that any local
    # user can read in the process list.
    local curl_args=(
        -s
        --connect-timeout 15
        --max-time 120
        -w "\n%{http_code}"
        -X "$method"
        -H "Content-Type: application/json"
    )
    if [ -n "$body" ]; then
        curl_args+=(--data-binary @-)
    fi

    # On a transport error curl still writes http_code 000, handled below.
    response=$(printf '%s' "$body" | curl "${curl_args[@]}" \
        -H @<(printf 'Authorization: Bearer %s\n' "$token") "$url") || true
    http_code=$(echo "$response" | tail -1)
    response=$(echo "$response" | sed '$d')

    if ! [[ "$http_code" =~ ^[0-9]+$ ]] || [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
        log_error "CF API $method $path → HTTP ${http_code:-none}"
        local err_detail
        err_detail=$(echo "$response" | jq -r '.errors[]?.message // empty' 2>/dev/null)
        if [ -n "$err_detail" ]; then
            log_error "$err_detail"
        fi
        return 1
    fi

    # Check CF-level success
    local success
    success=$(echo "$response" | jq -r '.success // true')
    if [ "$success" = "false" ]; then
        local errors
        errors=$(echo "$response" | jq -r '.errors[]?.message // "unknown error"')
        log_error "CF API $method $path → $errors"
        return 1
    fi

    echo "$response"
}

# =============================================================================
# Zone helpers
# =============================================================================

# get_zone_id DOMAIN TOKEN, returns zone ID (cached)
get_zone_id() {
    local domain="$1" token="$2"

    # Check cache
    if [ -n "${_ZONE_CACHE[$domain]:-}" ]; then
        echo "${_ZONE_CACHE[$domain]}"
        return
    fi

    local response zone_id
    response=$(cf_api GET "/zones?name=$domain&status=active" "$token") || return 1
    zone_id=$(echo "$response" | jq -r '.result[0].id // empty')

    if [ -z "$zone_id" ]; then
        log_error "Zone not found for domain: $domain"
        return 1
    fi

    _ZONE_CACHE[$domain]="$zone_id"
    echo "$zone_id"
}

# =============================================================================
# DNS helpers
# =============================================================================

# get_dns_records ZONE_ID TOKEN [TYPE] [NAME]
# Returns JSON array of matching records
get_dns_records() {
    local zone_id="$1" token="$2" type="${3:-}" name="${4:-}"
    local query="per_page=100"
    [ -n "$type" ] && query+="&type=$type"
    [ -n "$name" ] && query+="&name=$name"

    local response all_records page total_pages
    page=1
    all_records="[]"

    while true; do
        response=$(cf_api GET "/zones/$zone_id/dns_records?$query&page=$page" "$token") || return 1
        local page_records
        page_records=$(echo "$response" | jq '.result')
        all_records=$(echo "$all_records" "$page_records" | jq -s '.[0] + .[1]')

        total_pages=$(echo "$response" | jq '.result_info.total_pages // 1')
        if [ "$page" -ge "$total_pages" ]; then
            break
        fi
        ((page++))
    done

    echo "$all_records"
}

# create_dns_cname ZONE_ID TOKEN NAME TARGET [PROXIED]
create_dns_cname() {
    local zone_id="$1" token="$2" name="$3" target="$4" proxied="${5:-true}"
    local body
    body=$(jq -n \
        --arg name "$name" \
        --arg content "$target" \
        --argjson proxied "$proxied" \
        '{type: "CNAME", name: $name, content: $content, proxied: $proxied, ttl: 1}')

    cf_api POST "/zones/$zone_id/dns_records" "$token" "$body"
}

# create_dns_a ZONE_ID TOKEN NAME IP [PROXIED]
create_dns_a() {
    local zone_id="$1" token="$2" name="$3" ip="$4" proxied="${5:-true}"
    local body
    body=$(jq -n \
        --arg name "$name" \
        --arg content "$ip" \
        --argjson proxied "$proxied" \
        '{type: "A", name: $name, content: $content, proxied: $proxied, ttl: 1}')

    cf_api POST "/zones/$zone_id/dns_records" "$token" "$body"
}

# delete_dns_record ZONE_ID TOKEN RECORD_ID
delete_dns_record() {
    local zone_id="$1" token="$2" record_id="$3"
    cf_api DELETE "/zones/$zone_id/dns_records/$record_id" "$token" >/dev/null
}

# =============================================================================
# Tunnel helpers
# =============================================================================

# list_tunnels ACCOUNT_ID TOKEN, returns JSON array of tunnels
list_tunnels() {
    local account_id="$1" token="$2"
    local response
    response=$(cf_api GET "/accounts/$account_id/cfd_tunnel?is_deleted=false" "$token") || return 1
    echo "$response" | jq '.result'
}

# create_tunnel ACCOUNT_ID TOKEN NAME, returns tunnel JSON object
create_tunnel() {
    local account_id="$1" token="$2" name="$3"
    local secret
    secret=$(openssl rand -base64 32)
    local body
    body=$(jq -n \
        --arg name "$name" \
        --arg secret "$secret" \
        '{name: $name, tunnel_secret: $secret, config_src: "cloudflare"}')

    local response
    response=$(cf_api POST "/accounts/$account_id/cfd_tunnel" "$token" "$body") || return 1
    echo "$response" | jq '.result'
}

# get_tunnel_by_name ACCOUNT_ID TOKEN NAME, returns tunnel JSON or empty
get_tunnel_by_name() {
    local account_id="$1" token="$2" name="$3"
    local response
    response=$(cf_api GET "/accounts/$account_id/cfd_tunnel?name=$name&is_deleted=false" "$token") || return 1
    local tunnel
    tunnel=$(echo "$response" | jq -r '.result[0] // empty')
    if [ -n "$tunnel" ] && [ "$tunnel" != "null" ]; then
        echo "$tunnel"
    fi
}

# get_configured_tunnel ACCOUNT_ID TOKEN ACCOUNT_NAME
# Prefer the wpops inventory; retain name lookup for legacy-only deployments.
get_configured_tunnel() {
    local account_id="$1" token="$2" account_name="$3" node="" tunnel_id=""
    if [ -f "$PROJECT_DIR/config/wpops.json" ] && [ -f "$PROJECT_DIR/config/inventory.json" ]; then
        node=$(jq -r '.node // empty' "$PROJECT_DIR/config/wpops.json") || return 1
        tunnel_id=$(jq -r --arg node "$node" --arg account "$account_name" \
            '.origins[$node].tunnels[$account] // empty' "$PROJECT_DIR/config/inventory.json") || return 1
    fi
    if [ -n "$tunnel_id" ]; then
        local response
        response=$(cf_api GET "/accounts/$account_id/cfd_tunnel/$tunnel_id" "$token") || return 1
        echo "$response" | jq -c '.result // empty'
    else
        get_tunnel_by_name "$account_id" "$token" "wp-docker-$account_name"
    fi
}

# get_tunnel_token ACCOUNT_ID TOKEN TUNNEL_ID, returns connector token
get_tunnel_token() {
    local account_id="$1" token="$2" tunnel_id="$3"
    local response
    response=$(cf_api GET "/accounts/$account_id/cfd_tunnel/$tunnel_id/token" "$token") || return 1
    echo "$response" | jq -r '.result'
}

# configure_tunnel_ingress ACCOUNT_ID TOKEN TUNNEL_ID INGRESS_JSON
# INGRESS_JSON is a JSON array of ingress rules
configure_tunnel_ingress() {
    local account_id="$1" token="$2" tunnel_id="$3" ingress_json="$4"
    local body
    body=$(jq -n --argjson ingress "$ingress_json" '{config: {ingress: $ingress}}')

    cf_api PUT "/accounts/$account_id/cfd_tunnel/$tunnel_id/configurations" "$token" "$body"
}

# merge_tunnel_ingress ACCOUNT_ID TOKEN TUNNEL_ID NEW_RULES_JSON
# NEW_RULES_JSON is a JSON array of {hostname, service} rules (no catch-all).
# Adds/updates only these hostnames in the tunnel's existing ingress config
# unrelated rules (e.g. the standby wildcard) are preserved. Use this instead
# of configure_tunnel_ingress whenever a tunnel may already carry other rules.
merge_tunnel_ingress() {
    local account_id="$1" token="$2" tunnel_id="$3" new_rules_json="$4"
    local current existing merged
    current=$(get_tunnel_config "$account_id" "$token" "$tunnel_id") || return 1
    existing=$(echo "$current" | jq -c '.config.ingress // []')
    merged=$(jq -n -c \
        --argjson existing "$existing" \
        --argjson new "$new_rules_json" '
        ($new | map(.hostname)) as $newhosts
        | ($existing | map(select(.hostname != null and ((.hostname as $h | $newhosts | index($h)) | not)))) as $keep_named
        | ($existing | map(select(.hostname == null))) as $catchall
        | (if ($catchall | length) == 0 then [{"service":"http_status:404"}] else $catchall end) as $tail
        | ($keep_named + $new + $tail)
    ')
    configure_tunnel_ingress "$account_id" "$token" "$tunnel_id" "$merged"
}

# remove_tunnel_ingress_hosts ACCOUNT_ID TOKEN TUNNEL_ID HOSTS
# HOSTS is a space-separated list of hostnames to remove from the tunnel's
# ingress config. Everything else (e.g. the standby wildcard) is preserved.
remove_tunnel_ingress_hosts() {
    local account_id="$1" token="$2" tunnel_id="$3" hosts="$4"
    local current existing hosts_json remaining has_catchall
    current=$(get_tunnel_config "$account_id" "$token" "$tunnel_id") || return 1
    existing=$(echo "$current" | jq -c '.config.ingress // []')
    hosts_json=$(printf '%s\n' $hosts | jq -R . | jq -s -c .)
    remaining=$(jq -n -c \
        --argjson existing "$existing" \
        --argjson rm "$hosts_json" '
        $existing | map(select(.hostname == null or ((.hostname as $h | $rm | index($h)) | not)))
    ')
    has_catchall=$(echo "$remaining" | jq '[.[] | select(.hostname == null)] | length')
    if [ "$has_catchall" -eq 0 ]; then
        remaining=$(echo "$remaining" | jq -c '. + [{"service":"http_status:404"}]')
    fi
    configure_tunnel_ingress "$account_id" "$token" "$tunnel_id" "$remaining"
}

# get_tunnel_config ACCOUNT_ID TOKEN TUNNEL_ID, returns tunnel config JSON
get_tunnel_config() {
    local account_id="$1" token="$2" tunnel_id="$3"
    local response
    response=$(cf_api GET "/accounts/$account_id/cfd_tunnel/$tunnel_id/configurations" "$token") || return 1
    echo "$response" | jq '.result'
}

# =============================================================================
# Token validation
# =============================================================================

validate_token() {
    local token="$1" label="$2"
    local response

    # Try /user/tokens/verify first (requires User:API Tokens:Read permission)
    if response=$(cf_api GET "/user/tokens/verify" "$token" 2>/dev/null); then
        local status
        status=$(echo "$response" | jq -r '.result.status // "unknown"')
        if [ "$status" = "active" ]; then
            log_ok "Token valid: $label"
            return 0
        elif [ "$status" != "unknown" ]; then
            log_error "API token for '$label' status: $status (expected: active)"
            return 1
        fi
    fi

    # Fallback: try listing zones (works with Zone:Read permission)
    if cf_api GET "/zones?per_page=1" "$token" &>/dev/null; then
        log_ok "Token valid: $label"
        return 0
    fi

    log_error "API token for '$label' is invalid or expired"
    return 1
}

# =============================================================================
# Backup helpers
# =============================================================================

# normalize_dns_records JSON, extract stable fields and sort deterministically
normalize_dns_records() {
    echo "$1" | jq -S '[.[] | {type, name, content, proxied}] | sort_by([.name, .type])'
}

# normalize_tunnel_config JSON, extract ingress rules only
normalize_tunnel_config() {
    echo "$1" | jq -S '.config.ingress // empty'
}

# backup_dns_zone DOMAIN ZONE_ID TOKEN
# Saves all DNS records to data/cloudflare/backups/dns/{domain}/{timestamp}.json
# Skips write if DNS is unchanged since last backup
backup_dns_zone() {
    local domain="$1" zone_id="$2" token="$3"
    local dir="$BACKUP_DIR/dns/$domain"
    local timestamp
    timestamp=$(date +%Y-%m-%d_%H%M%S)
    mkdir -p "$dir"
    chmod 700 "$dir"

    local records
    records=$(get_dns_records "$zone_id" "$token") || return 1

    # Change detection: compare against last backup
    local prev
    prev=$(get_latest_backup "$dir")
    if [ -n "$prev" ]; then
        local current_norm prev_norm
        current_norm=$(normalize_dns_records "$records")
        prev_norm=$(normalize_dns_records "$(cat "$prev")")
        if [ "$current_norm" = "$prev_norm" ]; then
            log_info "DNS unchanged: $domain (skipped)"
            return 0
        fi
    fi

    if ! echo "$records" | jq '.' > "$dir/$timestamp.json"; then
        log_error "Failed to write DNS backup for $domain"
        return 1
    fi
    chmod 600 "$dir/$timestamp.json"
    log_ok "DNS backup: $domain → $dir/$timestamp.json ($(echo "$records" | jq 'length') records)"
}

# backup_tunnel_config ACCOUNT_NAME ACCOUNT_ID TOKEN TUNNEL_ID
# Saves tunnel config to data/cloudflare/backups/tunnels/{name}/{timestamp}.json
# Skips write if config is unchanged since last backup
backup_tunnel_config() {
    local name="$1" account_id="$2" token="$3" tunnel_id="$4"
    local dir="$BACKUP_DIR/tunnels/$name"
    local timestamp
    timestamp=$(date +%Y-%m-%d_%H%M%S)
    mkdir -p "$dir"
    chmod 700 "$dir"

    local config
    config=$(get_tunnel_config "$account_id" "$token" "$tunnel_id") || return 1

    # Change detection: compare against last backup
    local prev
    prev=$(get_latest_backup "$dir")
    if [ -n "$prev" ]; then
        local current_norm prev_norm
        current_norm=$(normalize_tunnel_config "$config")
        prev_norm=$(normalize_tunnel_config "$(cat "$prev")")
        if [ -n "$current_norm" ] && [ "$current_norm" = "$prev_norm" ]; then
            log_info "Tunnel config unchanged: $name (skipped)"
            return 0
        fi
    fi

    if ! echo "$config" | jq '.' > "$dir/$timestamp.json"; then
        log_error "Failed to write tunnel backup for $name"
        return 1
    fi
    chmod 600 "$dir/$timestamp.json"
    log_ok "Tunnel backup: $name → $dir/$timestamp.json"
}

# get_latest_backup DIR, returns path to most recent .json file
get_latest_backup() {
    local dir="$1"
    ls -t "$dir"/*.json 2>/dev/null | head -1
}

# =============================================================================
# Backup retention, grandfather-father-son
# =============================================================================

cleanup_backups() {
    local dir="$1"
    [ -d "$dir" ] || return 0

    local now_epoch
    now_epoch=$(date +%s)

    # Collect all backup files sorted newest first
    local files=()
    while IFS= read -r f; do
        files+=("$f")
    done < <(ls -t "$dir"/*.json 2>/dev/null)

    [ ${#files[@]} -eq 0 ] && return 0

    # Build sets of keepers
    declare -A keepers

    local daily_count=0 weekly_count=0 monthly_count=0 yearly_count=0
    local prev_day="" prev_week="" prev_month="" prev_year=""

    for f in "${files[@]}"; do
        local basename
        basename=$(basename "$f" .json)
        # Extract date part: 2026-02-23_140000 → 2026-02-23
        local datepart="${basename%%_*}"

        # Parse date components
        local year="${datepart%%-*}"
        local rest="${datepart#*-}"
        local month="${rest%%-*}"
        local day="${rest#*-}"

        # Day of week (0=Sunday)
        local dow
        dow=$(date -d "$datepart" +%w 2>/dev/null) || continue
        local file_epoch
        file_epoch=$(date -d "$datepart" +%s 2>/dev/null) || continue
        local age_days=$(( (now_epoch - file_epoch) / 86400 ))

        local keep=false

        # Daily: last 7 days
        if [ "$age_days" -lt 7 ] && [ "$daily_count" -lt 7 ] && [ "$datepart" != "$prev_day" ]; then
            keep=true
            daily_count=$((daily_count + 1))
            prev_day="$datepart"
        fi

        # Weekly: keep Sundays from last 4 weeks
        if [ "$dow" = "0" ] && [ "$age_days" -lt 28 ] && [ "$weekly_count" -lt 4 ]; then
            local weeknum
            weeknum=$(date -d "$datepart" +%Y-%W 2>/dev/null)
            if [ "$weeknum" != "$prev_week" ]; then
                keep=true
                weekly_count=$((weekly_count + 1))
                prev_week="$weeknum"
            fi
        fi

        # Monthly: keep 1st of month from last 6 months
        if [ "$day" = "01" ] && [ "$age_days" -lt 180 ] && [ "$monthly_count" -lt 6 ]; then
            local ym="${year}-${month}"
            if [ "$ym" != "$prev_month" ]; then
                keep=true
                monthly_count=$((monthly_count + 1))
                prev_month="$ym"
            fi
        fi

        # Yearly: keep Jan 1st from last 5 years
        if [ "$month" = "01" ] && [ "$day" = "01" ] && [ "$age_days" -lt 1825 ] && [ "$yearly_count" -lt 5 ]; then
            if [ "$year" != "$prev_year" ]; then
                keep=true
                yearly_count=$((yearly_count + 1))
                prev_year="$year"
            fi
        fi

        if [ "$keep" = true ]; then
            keepers["$f"]=1
        fi
    done

    # Always keep the most recent file
    keepers["${files[0]}"]=1

    # Delete non-keepers
    local deleted=0
    for f in "${files[@]}"; do
        if [ -z "${keepers[$f]:-}" ]; then
            rm -f "$f"
            deleted=$((deleted + 1))
        fi
    done

    if [ "$deleted" -gt 0 ]; then
        log_info "Retention cleanup: removed $deleted old backup(s) from $dir"
    fi
}
