# WordPress Multisite: Docker Infrastructure

Docker-based WordPress Multisite with Nginx reverse proxy, MariaDB, Redis object cache, WP-CLI cron, and Cloudflare tunnel support. Designed as standby/failover infrastructure with automatic multisite activation on DB sync.

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              frontend network               │
                    │                 (bridge)                    │
                    │                                             │
   :8080 ──────────┤► nginx ◄──── cloudflared-standby.example.net       │
                    │    │         cloudflared-account2 ...       │
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

This server has **no public IP**, all traffic comes through Cloudflare Tunnels. Each Cloudflare account needs its own tunnel and its own `cloudflared` container. One tunnel handles **all** domains on that account.

### How It Works

```
User's browser
    → main.standby.example.net (DNS points to Cloudflare)
    → Cloudflare edge (matches hostname to tunnel)
    → cloudflared container (tunnel connector running in Docker)
    → nginx:80 (reverse proxy)
    → wordpress:9000 (PHP-FPM)
    → WordPress matches hostname to correct site via wp_blogs table
```

### Two Modes

**Standby (normal operation):**
- One tunnel on the `standby.example.net` Cloudflare account
- Wildcard `*.standby.example.net` points to `http://nginx:80`
- Sites accessible as: `main.standby.example.net`, `example-shop.standby.example.net`, etc.
- Real domains stay on Azure, untouched

**Failover (Azure is down):**
- Real domains like `example-primary.com` need to reach this server instead of Azure
- Each real domain lives on a specific Cloudflare account
- You need a tunnel on **each** of those accounts
- Add the real domains as public hostnames on the correct account's tunnel
- Cloudflare automatically updates DNS, traffic flows to Docker

### Setup: Standby Tunnel

1. Log into the Cloudflare account that owns `standby.example.net`
2. Go to **Zero Trust → Networks → Tunnels → Create a tunnel**
3. Name it (e.g., `wp-standby`), copy the connector token
4. Paste in `.env`:
   ```
   CF_TUNNEL_STANDBY=eyJhIjoiYWJj...
   ```
5. In the tunnel's **Public Hostname** tab, add:
   - Subdomain: `*` | Domain: `standby.example.net` | Service: `http://nginx:80`
6. Start the tunnel:
   ```bash
   docker compose --profile tunnels up -d
   ```

All 16 sites are now accessible at `*.standby.example.net` subdomains.

### Setup: Failover Tunnels

For each Cloudflare account that has real domains you want to fail over:

1. Log into that Cloudflare account
2. Create a tunnel, copy the token
3. Add token to `.env`:
   ```
   CF_TUNNEL_ACCOUNT2=eyJhIjoiZGVm...
   ```
4. Uncomment the matching service in `docker-compose.yml`:
   ```yaml
   cloudflared-account2:
     image: cloudflare/cloudflared:latest
     restart: unless-stopped
     profiles:
       - tunnels
     networks:
       - frontend
     command: tunnel --no-autoupdate run
     environment:
       TUNNEL_TOKEN: ${CF_TUNNEL_ACCOUNT2}
     deploy:
       resources:
         limits:
           memory: 128M
   ```
5. Restart tunnels:
   ```bash
   docker compose --profile tunnels up -d
   ```
6. When failover is needed, go to that account's tunnel **Public Hostname** tab and add the real domains → `http://nginx:80`

Repeat for each Cloudflare account. The containers are lightweight (128MB each) and just sit idle until you add hostnames.

### Reverting Failover

1. Remove the real domains from the tunnel's Public Hostname tab
2. Re-create the original A records pointing at Azure's IP
3. Traffic goes back to Azure

The standby tunnel (`*.standby.example.net`) keeps running, it's unaffected.

## Multisite Modes

The multisite mode is controlled by three variables in `.env`:

```bash
WP_SUBDOMAIN_INSTALL=true      # true = subdomain, false = subdirectory
WP_DOMAIN_CURRENT_SITE=example.com
WP_SUNRISE=false               # true only if using a SUNRISE domain mapping plugin
```

### Subdomain mode (default: matches Azure production)

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
├── .env                        # Secrets (gitignored)
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
│   └── wordpress/
├── logs/                       # Log files (gitignored)
│   └── nginx/
└── scripts/
    └── failover-activate.sh
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
2. Run `./scripts/failover-activate.sh`
3. In each CF account's tunnel, add real domains as public hostnames → `http://nginx:80`
4. Cloudflare updates DNS automatically, all sites come up instantly

## Updating Cloudflare IP Ranges

The file `config/nginx/conf.d/cloudflare-real-ip.conf` contains Cloudflare's IP ranges for restoring real visitor IPs. These should be periodically verified against https://www.cloudflare.com/ips/.
