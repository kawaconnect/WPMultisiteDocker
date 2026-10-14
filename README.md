# WordPress Multisite: Docker Infrastructure

Docker-based WordPress Multisite with Nginx reverse proxy, MariaDB, Redis object cache, WP-CLI cron, and Cloudflare tunnel support. Designed as standby/failover infrastructure with automatic multisite activation on DB sync.

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              frontend network               │
                    │                 (bridge)                    │
                    │                                             │
   :8080 ──────────┤► nginx ◄──── cloudflared-standby            │
                    │    │         cloudflared-{account} ...     │
                    │    │         (profiles: tunnels)            │
                    │    │                                        │
                    │  wordpress (PHP-FPM)                        │
                    │    │                                        │
                    ├────┼────────────────────────────────────────┤
                    │    │        backend network                 │
                    │    │        (internal, no internet)        │
                    │    │                                        │
                    │    ├──── mariadb                            │
                    │    ├──── redis                              │
                    │    └──── wp-cron                            │
                    └─────────────────────────────────────────────┘
```

| Service | Image | Purpose |
|---------|-------|---------|
| **mariadb** | mariadb:11.8 | Database (LTS) |
| **redis** | redis:8-alpine | Object cache (384MB, LRU) |
| **wordpress** | wordpress:7-php8.3-fpm | PHP-FPM application |
| **nginx** | nginx:1.31-alpine | Reverse proxy, static files, security |
| **wp-cron** | wordpress:cli | Runs WP cron every minute via WP-CLI |
| **cloudflared-\*** | cloudflare/cloudflared | Tunnel connectors (only start with `--profile tunnels`) |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/Deenyoro/WPMultisiteDocker.git /opt/docker/wordpressmultisite
cd /opt/docker/wordpressmultisite
```

### 2. Create environment file

```bash
cp .env.example .env
vi .env
```

At minimum, change these:
- `MYSQL_ROOT_PASSWORD`: strong random password
- `MYSQL_PASSWORD`: strong random password
- `WORDPRESS_DB_PASSWORD`: must match `MYSQL_PASSWORD`
- `WP_DOMAIN_CURRENT_SITE`: your primary domain (default: `example.com`)
- `CF_STANDBY_DOMAIN`: your standby domain
- `CF_CLOUD_IP`: your production IP
- `CF_ACCOUNT_*`: Cloudflare account IDs, API tokens, and domains

### 3. Create data directories

```bash
mkdir -p data/{mysql,redis,wordpress} logs/nginx
```

### 4. Start core services

```bash
docker compose up -d
```

This starts mariadb, redis, wordpress, nginx, and wp-cron. Containers boot in dependency order, MariaDB and Redis must be healthy before WordPress starts, and WordPress must be healthy before Nginx starts.

### 5. Verify

```bash
docker compose ps
curl http://localhost:8080/wp-admin/install.php
docker compose exec redis redis-cli ping
```

### 6. Complete WordPress install

Visit `http://your-server:8080` and complete the WordPress installation wizard.

## Cloudflare Tunnels

This server has **no public IP**, all traffic comes through Cloudflare Tunnels. Tunnels, DNS, and failover are fully automated via scripts. You fill in the CF variables in `.env`, then scripts handle everything via the CF API.

### How It Works

```
User's browser
    → mysite.standby-domain.com (DNS points to Cloudflare)
    → Cloudflare edge (matches hostname to tunnel)
    → cloudflared container (tunnel connector running in Docker)
    → nginx:80 (reverse proxy)
    → wordpress:9000 (PHP-FPM)
    → WordPress matches hostname to correct site via wp_blogs table
```

### Two Modes

**Standby (normal operation):**
- One tunnel on the standby Cloudflare account
- Wildcard `*.your-standby-domain.com` points to `http://nginx:80`
- Sites accessible as standby subdomains (e.g. `mysite.standby-domain.com`)
- Real domains stay on production, untouched
- Per-domain control: add domains to `CF_ACCOUNT_{N}_FAILOVER` in `.env` to mark them as failover-only (inactive until needed)

**Failover (production is down):**
- Scripts automatically switch real domains from production A records to tunnel CNAMEs
- Each real domain lives on a specific Cloudflare account
- One tunnel per account handles all domains on that account
- DNS backup taken before any changes, rollback available
- Requires typing `FAILOVER` + second confirmation before anything is modified

### API Token Permissions

Each Cloudflare account in `.env` needs an API token with these minimum permissions:

| Permission | Level | Why |
|------------|-------|-----|
| **Zone : Zone : Read** | Zone | Look up zone IDs by domain name |
| **Zone : DNS : Edit** | Zone | Read, create, and delete DNS records |
| **Account : Cloudflare Tunnel : Edit** | Account | Create/list tunnels, get tokens, update ingress config |

Create the token in Cloudflare dashboard: **My Profile > API Tokens > Create Token > Custom Token**.

Set **Zone Resources** to the specific zones (domains) the token should access, and **Account Resources** to the specific account. Never grant broader access than needed.

### Setup

1. Edit `.env` with your Cloudflare config:
   ```bash
   vi .env
   ```
   Fill in `CF_STANDBY_DOMAIN`, `CF_CLOUD_IP`, and `CF_ACCOUNT_*` variables. Mark your standby account with `CF_ACCOUNT_{N}_STANDBY=true`. Add domains to `CF_ACCOUNT_{N}_FAILOVER` for those that should only be touched during failover.

2. Run setup (creates tunnels, configures DNS, generates compose file):
   ```bash
   ./scripts/cf-setup.sh
   ```

3. Start tunnels:
   ```bash
   docker compose --profile tunnels up -d
   ```

4. Verify:
   ```bash
   ./scripts/cf-status.sh
   ```

### Automation Scripts

| Script | Purpose |
|--------|---------|
| `scripts/cf-setup.sh` | Create tunnels, configure standby, generate `docker-compose.tunnels.yml` |
| `scripts/cf-failover.sh` | Back up DNS, switch real domains from production to Docker tunnels |
| `scripts/cf-rollback.sh` | Restore DNS, switch real domains back to production |
| `scripts/cf-status.sh` | Show current state of all tunnels + DNS |
| `scripts/cf-backup.sh` | Scheduled backup of DNS records + tunnel configs with rotation |

### Safety Guarantees

Every script enforces these rules:

1. **Backup before mutation**: `cf-setup.sh`, `cf-failover.sh`, and `cf-rollback.sh` all run a mandatory full backup of every account's DNS records and tunnel configs before touching anything. If the backup fails, the script aborts.

2. **Standby-only in normal mode**: `cf-setup.sh` only creates DNS records and configures ingress for the standby domain (`*.your-standby-domain.com`). Failover account tunnels are created but left idle with catch-all 404. Real domains are never touched.

3. **Double confirmation for dangerous ops**: `cf-failover.sh` and `cf-rollback.sh` both require:
   - Typing the exact word `FAILOVER` or `ROLLBACK` to proceed
   - Reviewing a dry-run preview showing every DNS record that will change
   - A second `y/N` confirmation after seeing the change list

4. **Precise targeting**: Failover only switches A records matching your `CF_CLOUD_IP` exactly. Rollback only switches CNAMEs ending in `.cfargotunnel.com`. No other records are touched.

5. **Read-only scripts**: `cf-status.sh` and `cf-backup.sh` only use GET requests and never modify anything.

### Failover

When production goes down:
```bash
./scripts/cf-failover.sh
```
This backs up DNS, switches A records to tunnel CNAMEs, and configures tunnel ingress, all automatically.

### Rollback

To restore domains to production:
```bash
./scripts/cf-rollback.sh
```
The standby tunnel is unaffected by failover/rollback.

### Backups

Run manually or add to cron for daily backups:
```bash
./scripts/cf-backup.sh

# Cron: daily at 2am
0 2 * * * /opt/docker/wordpressmultisite/scripts/cf-backup.sh
```

Retention: 7 daily, 4 weekly (Sunday), 6 monthly (1st), 5 yearly (Jan 1st). Backups stored in `data/cloudflare/backups/`.


## wpops: failover, backup pulls and restores (node agent)

`scripts/wpops/` is the control plane the Discord bot drives (design and
scenarios: [docs/wpops-design.md](docs/wpops-design.md)). It is stdlib
Python, runs on every docker node, and does the same work from the CLI and
from its HTTP API (`wpops serve`, published through the node's own tunnel as
`ops.<standby-domain>`).

```bash
scripts/wpops/bin/wpops setup --node node2        # config skeletons, host-alias plugin into every stack
scripts/wpops/bin/wpops token rotate                # agent bearer token (give it to the bot: /wp node add)
scripts/wpops/install-agent.sh --bind 172.19.0.1   # systemd unit; bind the docker gateway so cloudflared reaches it
wpops tunnels ensure                                # idle tunnel per Cloudflare account + docker-compose.tunnels.yml
wpops status [--live]  /  wpops sites --live       # node health, overrides, tunnels, backups / where DNS points
wpops failover plan --sites a,b | --all [--to NODE] # dry run, shows refusals (no tunnel, foreign record, ...)
wpops failover apply ... --yes                      # DNS -> tunnel CNAME, originals saved in state/overrides.json
wpops failover rollback --sites a | --all --yes     # exact original records restored, ingress cleaned
wpops backup pull --source azure                    # SiteGuard sync API: fresh full backup, resumable download
wpops backup list | prune | schedule --set weekly:sun:03:30 --retain 4
wpops restore apply --set azure/<set> --stack main --all | --sites a,b --yes   # never automatic
```

- `config/inventory.json` (per node, **not committed**; copy from `inventory.example.json`): origins (external like Azure, or
  nodes), sites (slug, primary, Cloudflare account, production domains, how
  each origin serves it). `config/wpops.json` (per node): agent bind/port,
  schedule, retention, notification e-mail/webhook.
- Production hostnames are served by the **host-alias layer**
  (`plugin/kc-host-alias`: `sunrise.php` + mu-plugin, map in
  `wp-content/wpops-host-aliases.json`), so a restore keeps the stack's own
  URLs and a failover still answers `https://<production-domain>` with the
  production canonical, media and REST URLs. Responses carry
  `X-KC-Host-Alias: <site>` while an alias is active.
- Network: the agent listens on loopback plus the docker frontend gateway
  (`install-agent.sh --bind 172.19.0.1`) so cloudflared can publish it. With a
  host firewall, allow that gateway port from the docker networks only
  (`ufw allow from 172.16.0.0/12 to any port 8787 proto tcp`) and keep nginx on
  `127.0.0.1:8080`. Set `agent.trusted_proxies` in `config/wpops.json` to the
  docker subnet(s) so `CF-Connecting-IP` is believed only from cloudflared.
- Disk: `disk.min_free_gb` / `disk.min_free_pct` (defaults 10 / 10) are enforced
  before every pull and restore; the agent e-mails/webhooks `disk.low` (daily
  while it lasts) and `disk.ok`, and the Discord bot relays both.
- Secrets stay in `.env`: `CF_ACCOUNT_n_*`, `WPOPS_SYNC_KEY_<SOURCE>`
  (SiteGuard sync key of each source), optional `SMTP_*`.
- Pulled sets live under `backups/<source>/<set>/` (retention per source);
  the `wp-cron` service mounts `./backups` read-only at `/backups` for
  `wp siteguardbackup import`.

## Isolated Instances (single site or multisite)

Besides the main standby stack, `scripts/instance.sh` runs any number of
throwaway WordPress instances, each either a **single site** or a
**multisite** network. Use them for development, plugin testing and
site-transfer tests. They never touch the main stack:

- own compose project (`wpi-<name>`), networks, database and files under
  `instances/<name>/` (gitignored)
- no tunnels, no Cloudflare credentials, no `cf-backup`, no Redis, so nothing
  here can trigger failover
- nginx listens on `127.0.0.1:<port>` only; send `Host: <name>.test`
- WP-CLI runs as `www-data` (uid 33), never root
- instances reach each other as `http://<name>.test/` over the internal
  `wpi-mesh` network, which has no internet route

```bash
scripts/instance.sh create site-a --mode single        # single site
scripts/instance.sh create net-b  --mode multisite     # network + sub1, sub2 (subdirectories)
scripts/instance.sh list
scripts/instance.sh wp site-a option get siteurl        # WP-CLI as www-data
scripts/instance.sh wp net-b --url=http://net-b.test/sub1/ post list
scripts/instance.sh install-plugin site-a ./path/to/plugin --activate
curl -H "Host: site-a.test" http://127.0.0.1:18080/
scripts/instance.sh stop site-a
scripts/instance.sh destroy site-a --yes               # deletes its DB and files
```

`destroy` only ever removes `instances/<name>` after resolving the path and
checking it is exactly that directory.

Testing plugins that delete things (backup/restore/retention) belongs here or
on another disposable host: never run them as root on a machine holding
unique state.

## Multisite Modes

The multisite mode is controlled by three variables in `.env`:

```bash
WP_SUBDOMAIN_INSTALL=true      # true = subdomain, false = subdirectory
WP_DOMAIN_CURRENT_SITE=example.com
WP_SUNRISE=false               # true only if using a SUNRISE domain mapping plugin
```

### Subdomain mode (default: matches production)

Sites live on separate domains. WordPress core handles domain routing natively via the `wp_blogs` table, no plugins needed. Each site can have its own domain (e.g., `example-primary.com`, `example-shop.com`) by editing the site URL in **Network Admin → Sites → Edit**.

### Subdirectory mode

Sites live under paths (e.g., `example.com/site1`). Simpler setup, no wildcard DNS needed. If you want separate domains in this mode, set `WP_SUNRISE=true` and install a domain mapping plugin (Mercator, etc.) with its `sunrise.php` in `wp-content/`.

### Fresh install (no existing DB)

After completing the single-site WordPress install:

1. Go to **Tools → Network Setup** in wp-admin (`WP_ALLOW_MULTISITE` is already enabled)
2. Choose **Sub-domains** or **Sub-directories** (must match your `WP_SUBDOMAIN_INSTALL` setting)
3. Restart: `docker compose restart wordpress`

The multisite constants activate automatically when the `wp_blogs` table is detected in the database.

### Synced DB from production

1. Import the database into MariaDB
2. Restart: `docker compose restart wordpress`

Multisite activates automatically, all sites, domain mappings, and network-activated plugins come from the database.

## Domain Mapping

Nginx is configured with `server_name _` (catch-all), so it accepts any hostname. WordPress routes requests to the correct site by matching the HTTP Host header against the `domain` column in the `wp_blogs` table.

**Subdomain mode (default):** Domain mapping is built into WordPress core. No plugin required.

**Subdirectory mode with custom domains:** Requires `WP_SUNRISE=true` and a domain mapping plugin.

## Directory Structure

```
wordpressmultisite/
├── docker-compose.yml
├── docker-compose.tunnels.yml  # Auto-generated by cf-setup.sh (gitignored)
├── docker-compose.instance.yml # Template for isolated single/multisite instances
├── .env                        # Secrets + CF config (gitignored)
├── .env.example                # Template
├── .gitignore
├── config/
│   ├── nginx/
│   │   ├── nginx.conf
│   │   └── conf.d/
│   │       ├── default.conf
│   │       └── cloudflare-real-ip.conf
│   ├── php/
│   │   └── uploads.ini
│   └── redis/
│       └── redis.conf
├── data/                       # Persistent volumes (gitignored)
│   ├── mysql/
│   ├── redis/
│   ├── wordpress/
│   └── cloudflare/
│       └── backups/
│           ├── dns/{domain}/{timestamp}.json
│           └── tunnels/{name}/{timestamp}.json
├── instances/<name>/           # Isolated instances: .env + data (gitignored)
├── logs/                       # Log files (gitignored)
│   └── nginx/
└── scripts/
    ├── cf-common.sh            # Shared functions (sourced by all cf-* scripts)
    ├── cf-setup.sh             # Create tunnels + generate compose
    ├── cf-failover.sh          # Switch domains to Docker
    ├── cf-rollback.sh          # Restore domains to production
    ├── cf-status.sh            # Show current state
    ├── cf-backup.sh            # Backup DNS + tunnel configs
    ├── failover-activate.sh    # Wrapper for cf-failover.sh
    └── instance.sh             # Create/run/destroy isolated single or multisite instances
```

## Network Isolation

- **frontend** (bridge), nginx, wordpress, cloudflared containers. Has internet access.
- **backend** (internal), mariadb, redis, wordpress, nginx, wp-cron. No internet access.

MariaDB and Redis ports are **not published** to the host. Only port 8080 (nginx) is exposed for local testing.

## Security Hardening

Nginx blocks direct access to:
- `wp-config.php` (403)
- `xmlrpc.php` and `wp-trackback.php` (403)
- Hidden files like `.env`, `.htaccess` (403, except `.well-known`)
- PHP execution in `wp-includes/` (403)
- PHP execution in `wp-content/uploads/` (403)

Security headers: `X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`. Server version hidden (`server_tokens off`).

## Resource Limits

| Service | Memory Limit |
|---------|-------------|
| MariaDB | 1 GB |
| Redis | 512 MB (384 MB maxmemory) |
| WordPress | 1 GB |
| Nginx | 256 MB |
| WP-Cron | 256 MB |
| Cloudflared (each) | 128 MB |

## PHP Configuration

| Setting | Value |
|---------|-------|
| `upload_max_filesize` | 256M |
| `post_max_size` | 300M |
| `memory_limit` | 512M |
| `max_execution_time` | 300s |
| `max_input_vars` | 5000 |
| OPcache | Enabled, 256MB, 2s revalidation |

## Common Operations

```bash
# View logs
docker compose logs -f wordpress
docker compose logs -f nginx

# Start/stop tunnels
docker compose --profile tunnels up -d
docker compose --profile tunnels stop

# Cloudflare status
./scripts/cf-status.sh

# Backup DNS + tunnel configs
./scripts/cf-backup.sh

# Run WP-CLI command
docker compose exec wordpress wp plugin list --allow-root

# Access MariaDB
docker compose exec mariadb mariadb -u wordpress -p wordpress

# Check Redis
docker compose exec redis redis-cli info memory

# Stop everything
docker compose down

# Full rebuild
docker compose down && docker compose up -d --force-recreate
```

## Failover Flow

1. Sync database from production to this instance's MariaDB
2. Run `./scripts/cf-failover.sh`, backs up DNS, switches domains, configures tunnels
3. All sites come up instantly on Docker
4. To rollback: `./scripts/cf-rollback.sh`

## Updating Cloudflare IP Ranges

The file `config/nginx/conf.d/cloudflare-real-ip.conf` contains Cloudflare's IP ranges for restoring real visitor IPs. These should be periodically verified against https://www.cloudflare.com/ips/.
