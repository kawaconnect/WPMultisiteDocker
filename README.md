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
| **mariadb** | mariadb:11.4 | Database (LTS) |
| **redis** | redis:7-alpine | Object cache (384MB, LRU) |
| **wordpress** | wordpress:6-fpm | PHP-FPM application |
| **nginx** | nginx:1.27-alpine | Reverse proxy, static files, security |
| **wp-cron** | wordpress:cli | Runs WP cron every 5 minutes via WP-CLI |
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

This server has **no public IP**, all traffic comes through Cloudflare Tunnels. Tunnels, DNS, and failover are fully automated via scripts. You fill in one config file (`accounts.json`), then scripts handle everything via the CF API.

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
- Per-domain control: set `"failover": true` in `accounts.json` to mark domains as failover-only (inactive until needed)

**Failover (production is down):**
- Scripts automatically switch real domains from production A records to tunnel CNAMEs
- Each real domain lives on a specific Cloudflare account
- One tunnel per account handles all domains on that account
- DNS backup taken before any changes, rollback available
- Requires typing `FAILOVER` + second confirmation before anything is modified

### API Token Permissions

Each Cloudflare account in `accounts.json` needs an API token with these minimum permissions:

| Permission | Level | Why |
|------------|-------|-----|
| **Zone : Zone : Read** | Zone | Look up zone IDs by domain name |
| **Zone : DNS : Edit** | Zone | Read, create, and delete DNS records |
| **Account : Cloudflare Tunnel : Edit** | Account | Create/list tunnels, get tokens, update ingress config |

Create the token in Cloudflare dashboard: **My Profile > API Tokens > Create Token > Custom Token**.

Set **Zone Resources** to the specific zones (domains) the token should access, and **Account Resources** to the specific account. Never grant broader access than needed.

### Setup

1. Create the config file:
   ```bash
   cp accounts.json.example accounts.json
   vi accounts.json
   ```
   Fill in your `standby_domain`, `cloud_ip`, CF account IDs, and API tokens. Mark your standby account as `is_standby: true`. Set `"failover": true` on domains that should only be touched during failover.

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

4. **Precise targeting**: Failover only switches A records matching your `cloud_ip` exactly. Rollback only switches CNAMEs ending in `.cfargotunnel.com`. No other records are touched.

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
├── .env                        # Secrets (gitignored)
├── .env.example                # Template
├── accounts.json               # CF account config (gitignored)
├── accounts.json.example       # Template
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
├── logs/                       # Log files (gitignored)
│   └── nginx/
└── scripts/
    ├── cf-common.sh            # Shared functions (sourced by all cf-* scripts)
    ├── cf-setup.sh             # Create tunnels + generate compose
    ├── cf-failover.sh          # Switch domains to Docker
    ├── cf-rollback.sh          # Restore domains to production
    ├── cf-status.sh            # Show current state
    ├── cf-backup.sh            # Backup DNS + tunnel configs
    └── failover-activate.sh    # Wrapper for cf-failover.sh
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
