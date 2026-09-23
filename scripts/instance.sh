#!/usr/bin/env bash
# =============================================================================
# instance.sh, isolated WordPress instances, single site or multisite
# =============================================================================
# Usage:
#   scripts/instance.sh create  <name> --mode single|multisite [--port N]
#   scripts/instance.sh up      <name>
#   scripts/instance.sh stop    <name>
#   scripts/instance.sh destroy <name> --yes
#   scripts/instance.sh wp      <name> <wp-cli args...>     (runs as www-data)
#   scripts/instance.sh install-plugin <name> <local-plugin-dir> [--activate]
#   scripts/instance.sh compose <name> <compose args...>
#   scripts/instance.sh list
#
# Each instance lives in instances/<name>/ (gitignored) with its own .env,
# database and wp-content, runs as compose project "wpi-<name>", and is
# reachable only on http://127.0.0.1:<port>/ (send "Host: <name>.test").
#
# Instances can reach each other as http://<name>.test/ over the internal
# "wpi-mesh" network (no internet), e.g. for site-to-site transfers.
#
# Instances never touch the main standby stack: they share no networks,
# volumes or containers with it, get no Cloudflare credentials, and start no
# tunnels. Nothing here can trigger failover.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
INSTANCES_DIR="$REPO_DIR/instances"
COMPOSE_FILE="$REPO_DIR/docker-compose.instance.yml"

die() { echo "instance.sh: $*" >&2; exit 2; }

valid_name() {
	[[ "${1:-}" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "invalid instance name '${1:-}' (use a-z, 0-9, -)"
}

instance_dir() {
	valid_name "$1"
	printf '%s/%s' "$INSTANCES_DIR" "$1"
}

ensure_mesh() {
	docker network inspect wpi-mesh >/dev/null 2>&1 \
		|| docker network create --internal --subnet 10.212.0.0/16 wpi-mesh >/dev/null
}

# Per-instance /24 inside 10.213.0.0/16 (see docker-compose.instance.yml).
free_net() {
	local n
	for n in $(seq 1 254); do
		grep -rqs "^INSTANCE_NET=$n\$" "$INSTANCES_DIR"/*/.env || { echo "$n"; return; }
	done
	die "no free instance subnet"
}

compose() {
	local name="$1"; shift
	ensure_mesh
	local dir; dir="$(instance_dir "$name")"
	[ -f "$dir/.env" ] || die "no such instance: $name"
	# Instances created before per-instance subnets existed: assign one.
	grep -q '^INSTANCE_NET=' "$dir/.env" || echo "INSTANCE_NET=$(free_net)" >> "$dir/.env"
	docker compose -p "wpi-$name" -f "$COMPOSE_FILE" --project-directory "$REPO_DIR" \
		--env-file "$dir/.env" "$@"
}

rand() { openssl rand -hex 16; }

free_port() {
	local p=18080
	while ss -ltn "( sport = :$p )" | grep -q ":$p" || grep -rqs "^HTTP_PORT=$p\$" "$INSTANCES_DIR"/*/.env; do
		p=$((p + 1))
	done
	echo "$p"
}

wait_for() {
	local what="$1" tries="$2"; shift 2
	for _ in $(seq 1 "$tries"); do
		if "$@" >/dev/null 2>&1; then return 0; fi
		sleep 3
	done
	die "timed out waiting for $what"
}

cmd_create() {
	local name="${1:-}"; shift || true
	local mode="" port=""
	while [ $# -gt 0 ]; do
		case "$1" in
			--mode) mode="${2:-}"; shift 2 ;;
			--port) port="${2:-}"; shift 2 ;;
			*) die "unknown option $1" ;;
		esac
	done
	local dir; dir="$(instance_dir "$name")"
	case "$mode" in single|multisite) ;; *) die "--mode single|multisite is required" ;; esac
	[ -e "$dir" ] && die "instance $name already exists"
	[ -n "$port" ] || port="$(free_port)"
	[[ "$port" =~ ^[0-9]+$ ]] || die "invalid port $port"

	mkdir -p "$dir/data/mysql" "$dir/data/wordpress"
	chmod 700 "$dir"
	# Containers run with all capabilities dropped, so they cannot chown their
	# own data dirs: hand them over up front (mysql=999, www-data=33).
	chown 999:999 "$dir/data/mysql"
	chown 33:33 "$dir/data/wordpress"
	local admin_pass; admin_pass="$(rand)"
	cat > "$dir/.env" <<EOF
INSTANCE_NAME=$name
INSTANCE_DIR=$dir
INSTANCE_NET=$(free_net)
WP_MODE=$mode
WP_DOMAIN=$name.test
HTTP_PORT=$port
MYSQL_ROOT_PASSWORD=$(rand)
MYSQL_PASSWORD=$(rand)
WP_ADMIN_USER=admin
WP_ADMIN_PASSWORD=$admin_pass
EOF
	chmod 600 "$dir/.env"

	compose "$name" up -d --quiet-pull
	wait_for "wp-config.php" 60 test -f "$dir/data/wordpress/wp-config.php"
	wait_for "database" 40 compose "$name" exec -T cli wp db check --quiet

	local url="http://$name.test"
	if [ "$mode" = multisite ]; then
		compose "$name" exec -T cli wp core multisite-install --url="$url" --title="$name" \
			--admin_user=admin --admin_password="$admin_pass" --admin_email="admin@$name.test" \
			--skip-email --skip-config
		compose "$name" exec -T cli wp site create --slug=sub1 --title="Subsite 1" --email="admin@$name.test" --url="$url"
		compose "$name" exec -T cli wp site create --slug=sub2 --title="Subsite 2" --email="admin@$name.test" --url="$url"
	else
		compose "$name" exec -T cli wp core install --url="$url" --title="$name" \
			--admin_user=admin --admin_password="$admin_pass" --admin_email="admin@$name.test" --skip-email
	fi
	compose "$name" exec -T cli wp rewrite structure '/%postname%/' --quiet || true

	echo "created $mode instance $name"
	echo "  http://127.0.0.1:$port/  (Host: $name.test)   admin / see $dir/.env"
}

cmd_install_plugin() {
	local name="${1:-}" src="${2:-}" activate="${3:-}"
	instance_dir "$name" >/dev/null
	[ -d "$src" ] || die "not a directory: $src"
	local slug; slug="$(basename "$(realpath -e -- "$src")")"
	[[ "$slug" =~ ^[A-Za-z0-9._-]+$ ]] || die "bad plugin directory name $slug"
	local c; c="$(compose "$name" ps -q wordpress)"
	[ -n "$c" ] || die "instance $name is not running"
	docker exec -u 0 "$c" rm -rf -- "/var/www/html/wp-content/plugins/$slug"
	docker cp -q "$src" "$c:/var/www/html/wp-content/plugins/$slug"
	docker exec -u 0 "$c" chown -R 33:33 "/var/www/html/wp-content/plugins/$slug"
	if [ "$activate" = "--activate" ]; then
		local mode; mode="$(. "$(instance_dir "$name")/.env"; echo "$WP_MODE")"
		if [ "$mode" = multisite ]; then
			compose "$name" exec -T cli wp plugin activate "$slug" --network
		else
			compose "$name" exec -T cli wp plugin activate "$slug"
		fi
	fi
	echo "installed $slug into $name"
}

cmd_destroy() {
	local name="${1:-}"; local confirm="${2:-}"
	local dir; dir="$(instance_dir "$name")"
	[ "$confirm" = "--yes" ] || die "destroy deletes the instance's database and files; pass --yes"
	[ -d "$dir" ] || die "no such instance: $name"

	# Refuse unless the resolved path is exactly instances/<name>.
	local real_dir real_parent
	real_dir="$(realpath -e -- "$dir")"
	real_parent="$(realpath -e -- "$INSTANCES_DIR")"
	case "$real_dir" in
		"$real_parent"/?*) ;;
		*) die "refusing: $real_dir is not inside $real_parent" ;;
	esac
	[ "$real_dir" = "$real_parent/$name" ] || die "refusing: $real_dir is not $real_parent/$name"

	if [ -f "$dir/.env" ]; then
		compose "$name" down --volumes --remove-orphans || true
	fi
	rm -rf --one-file-system -- "$real_dir"
	echo "destroyed $name"
}

cmd_list() {
	[ -d "$INSTANCES_DIR" ] || { echo "(no instances)"; return; }
	for env in "$INSTANCES_DIR"/*/.env; do
		[ -f "$env" ] || continue
		# shellcheck disable=SC1090
		( . "$env"; printf '%-20s %-10s 127.0.0.1:%-6s %s\n' "$INSTANCE_NAME" "$WP_MODE" "$HTTP_PORT" \
			"$(docker compose -p "wpi-$INSTANCE_NAME" ps --format '{{.Service}}:{{.State}}' 2>/dev/null | tr '\n' ' ')" )
	done
}

sub="${1:-}"; shift || true
case "$sub" in
	create)  cmd_create "$@" ;;
	up)      compose "${1:-}" up -d ;;
	stop)    compose "${1:-}" stop ;;
	destroy) cmd_destroy "$@" ;;
	install-plugin) cmd_install_plugin "$@" ;;
	wp)      name="${1:-}"; shift || true; compose "$name" exec -T cli wp "$@" ;;
	compose) name="${1:-}"; shift || true; compose "$name" "$@" ;;
	list)    cmd_list ;;
	*) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
