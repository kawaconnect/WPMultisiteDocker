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
    if [ -z "${CF_STANDBY_DOMAIN:-}" ]; then
        log_error "CF_STANDBY_DOMAIN not set in .env"
        exit 1
    fi
    if [ -z "${CF_CLOUD_IP:-}" ]; then
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
    done

    log_ok "Config loaded: $account_count account(s), standby=$CF_STANDBY_DOMAIN"
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
get_account_domains() {
    local n="$1"
    local varname="CF_ACCOUNT_${n}_DOMAINS"
    IFS=',' read -ra domains <<< "${!varname:-}"
    echo "${domains[@]}"
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
            zone_id=$(get_zone_id "$domain" "$api_token" 2>/dev/null)
            if [ -z "$zone_id" ]; then
                log_error "Could not get zone ID for $domain"
                backup_errors=$((backup_errors + 1))
                continue
            fi
            backup_dns_zone "$domain" "$zone_id" "$api_token" || backup_errors=$((backup_errors + 1))
        done

        # Backup tunnel config if tunnel exists
        local tunnel_json
        tunnel_json=$(get_tunnel_by_name "$account_id" "$api_token" "$tunnel_name" 2>/dev/null)
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

    local curl_args=(
        -s
        -w "\n%{http_code}"
        -X "$method"
        -H "Authorization: Bearer $token"
        -H "Content-Type: application/json"
    )
    if [ -n "$body" ]; then
        curl_args+=(-d "$body")
    fi

    response=$(curl "${curl_args[@]}" "$url")
    http_code=$(echo "$response" | tail -1)
    response=$(echo "$response" | sed '$d')

    if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
        log_error "CF API $method $path → HTTP $http_code"
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
    if ! response=$(cf_api GET "/user/tokens/verify" "$token" 2>/dev/null); then
        log_error "API token for '$label' is invalid or expired"
        return 1
    fi
    local status
    status=$(echo "$response" | jq -r '.result.status // "unknown"')
    if [ "$status" != "active" ]; then
        log_error "API token for '$label' status: $status (expected: active)"
        return 1
    fi
    log_ok "Token valid: $label"
}

# =============================================================================
# Backup helpers
# =============================================================================

# backup_dns_zone DOMAIN ZONE_ID TOKEN
# Saves all DNS records to data/cloudflare/backups/dns/{domain}/{timestamp}.json
backup_dns_zone() {
    local domain="$1" zone_id="$2" token="$3"
    local dir="$BACKUP_DIR/dns/$domain"
    local timestamp
    timestamp=$(date +%Y-%m-%d_%H%M%S)
    mkdir -p "$dir"

    local records
    records=$(get_dns_records "$zone_id" "$token") || return 1
    echo "$records" | jq '.' > "$dir/$timestamp.json"
    log_ok "DNS backup: $domain → $dir/$timestamp.json ($(echo "$records" | jq 'length') records)"
}

# backup_tunnel_config ACCOUNT_NAME ACCOUNT_ID TOKEN TUNNEL_ID
# Saves tunnel config to data/cloudflare/backups/tunnels/{name}/{timestamp}.json
backup_tunnel_config() {
    local name="$1" account_id="$2" token="$3" tunnel_id="$4"
    local dir="$BACKUP_DIR/tunnels/$name"
    local timestamp
    timestamp=$(date +%Y-%m-%d_%H%M%S)
    mkdir -p "$dir"

    local config
    config=$(get_tunnel_config "$account_id" "$token" "$tunnel_id") || return 1
    echo "$config" | jq '.' > "$dir/$timestamp.json"
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
