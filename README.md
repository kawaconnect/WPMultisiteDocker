# WordPress Multisite: Docker Infrastructure

Docker-based WordPress Multisite (subdirectory mode) with Nginx reverse proxy, MariaDB, Redis object cache, WP-CLI cron, and Cloudflare tunnel support. Designed as standby/failover infrastructure with automatic multisite activation on DB sync.

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              frontend network               │
                    │                 (bridge)                    │
                    │                                             │
   :8080 ──────────┤► nginx ◄──── cloudflared-account1           │
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

**6 services** (5 core + 1 tunnel per Cloudflare account):

| Service | Image | Purpose |
|---------|-------|---------|
| **mariadb** | mariadb:11.4 | Database (LTS) |
| **redis** | redis:7-alpine | Object cache (384MB, LRU) |
| **wordpress** | wordpress:6-fpm | PHP-FPM application |
| **nginx** | nginx:1.27-alpine | Reverse proxy, static files, security |
| **wp-cron** | wordpress:cli | Runs WP cron every 5 minutes via WP-CLI |
| **cloudflared-account1** | cloudflare/cloudflared | Tunnel connector (only starts with `--profile tunnels`) |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/Deenyoro/WPMultisiteDocker.git /opt/docker/wordpressmultisite
cd /opt/docker/wordpressmultisite
```

### 2. Create environment file

```bash
cp .env.example .env
```

Edit `.env` and set real passwords:

```bash
vi .env
```

At minimum, change these:
- `MYSQL_ROOT_PASSWORD`: strong random password
- `MYSQL_PASSWORD`: strong random password
- `WORDPRESS_DB_PASSWORD`: must match `MYSQL_PASSWORD`
- `CLOUDFLARE_TUNNEL_TOKEN_ACCOUNT1`: your tunnel token (when ready)

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
# All containers healthy
docker compose ps

# WordPress responds
curl http://localhost:8080/wp-admin/install.php

# Redis working
docker compose exec redis redis-cli ping

# MariaDB healthy
docker compose logs mariadb | tail -5
```

### 6. Complete WordPress install

Visit `http://your-server:8080` and complete the WordPress installation wizard.

## Multisite Setup

### Fresh install (no existing DB)

After completing the single-site WordPress install:

1. Go to **Tools → Network Setup** in wp-admin (available because `WP_ALLOW_MULTISITE` is already enabled)
2. Choose **Sub-directories** and click Install
3. Restart the WordPress container to pick up the new multisite tables:

```bash
docker compose restart wordpress
```

The multisite constants (`MULTISITE`, `SUBDOMAIN_INSTALL`, etc.) activate automatically when the `wp_blogs` table is detected in the database.

### Synced DB from production

If you restore/sync a database from your production WordPress Multisite:

1. Import the database into MariaDB
2. Restart WordPress:

```bash
docker compose restart wordpress
```

Multisite activates automatically, all sites, domain mappings, and network-activated plugins come from the database.

## Domain Mapping

This setup uses WordPress `SUNRISE` for domain mapping. The `wp-content/sunrise.php` file is a placeholder, install your domain mapping plugin (e.g., Mercator, WordPress MU Domain Mapping) and it will handle the rest.

Nginx is configured with `server_name _` (catch-all), so it accepts any hostname. WordPress domain mapping routes requests to the correct site based on the database.

## Cloudflare Tunnels

Tunnel containers are behind a Docker Compose profile and **do not start by default**.

### Activate tunnels (failover)

```bash
# Option 1: Use the failover script
./scripts/failover-activate.sh

# Option 2: Manual
docker compose --profile tunnels up -d
```

### Adding tunnel tokens

1. Create a tunnel in Cloudflare Zero Trust dashboard
2. Copy the connector token
3. Add it to `.env`:

```
CLOUDFLARE_TUNNEL_TOKEN_ACCOUNT1=eyJhIjoiYWJj...
```

### Multiple Cloudflare accounts

To add another account, uncomment the `cloudflared-account2` block in `docker-compose.yml` and add the token to `.env`:

```
CLOUDFLARE_TUNNEL_TOKEN_ACCOUNT2=eyJhIjoiZGVm...
```

Hostname-to-service mapping is configured in the Cloudflare Zero Trust dashboard, not in Docker. Point your tunnel hostnames to `http://nginx:80`.

## Directory Structure

```
wordpressmultisite/
├── docker-compose.yml          # All services, networks, healthchecks
├── .env                        # Secrets (gitignored)
├── .env.example                # Template for .env
├── .gitignore
├── config/
│   ├── nginx/
│   │   ├── nginx.conf                 # Worker config, gzip, security headers
│   │   └── conf.d/
│   │       ├── default.conf           # WP multisite server block, FastCGI
│   │       └── cloudflare-real-ip.conf # Restore real visitor IPs
│   ├── php/
│   │   └── uploads.ini                # Upload limits, memory, OPcache
│   └── redis/
│       └── redis.conf                 # 384MB limit, LRU eviction
├── data/                       # Persistent volumes (gitignored)
│   ├── mysql/
│   ├── redis/
│   └── wordpress/
├── logs/                       # Log files (gitignored)
│   └── nginx/
└── scripts/
    └── failover-activate.sh    # Activates tunnels for failover
```

## Network Isolation

- **frontend** (bridge), nginx, wordpress, cloudflared. Has internet access.
- **backend** (internal), mariadb, redis, wordpress, nginx, wp-cron. No internet access. Database and cache are completely isolated.

MariaDB and Redis ports are **not published** to the host. Only port 8080 (nginx) is exposed for local testing.

## Security Hardening

Nginx blocks direct access to:
- `wp-config.php` (403)
- `xmlrpc.php` and `wp-trackback.php` (403)
- Hidden files like `.env`, `.htaccess` (403, except `.well-known`)
- PHP execution in `wp-includes/` (403)
- PHP execution in `wp-content/uploads/` (403)

Security headers applied to all responses:
- `X-Frame-Options: SAMEORIGIN`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`

Server version is hidden (`server_tokens off`).

## Resource Limits

| Service | Memory Limit |
|---------|-------------|
| MariaDB | 1 GB |
| Redis | 512 MB (384 MB maxmemory) |
| WordPress | 1 GB |
| Nginx | 256 MB |
| WP-Cron | 256 MB |
| Cloudflared | 128 MB |

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
docker compose logs -f mariadb

# Restart a service
docker compose restart wordpress

# Run WP-CLI command
docker compose exec wordpress wp plugin list --allow-root

# Access MariaDB
docker compose exec mariadb mariadb -u wordpress -p wordpress

# Check Redis
docker compose exec redis redis-cli info memory

# Stop everything
docker compose down

# Stop everything and remove volumes
docker compose down -v

# Full rebuild
docker compose down && docker compose up -d --force-recreate
```

## Failover Flow

1. Sync database from production to this instance's MariaDB
2. Run `./scripts/failover-activate.sh`, starts tunnel containers
3. In each Cloudflare account's Zero Trust dashboard, update DNS to point domains to the tunnel CNAME
4. All sites come up on standby instantly, domain mapping and network-activated plugins are already in the DB

## Updating Cloudflare IP Ranges

The file `config/nginx/conf.d/cloudflare-real-ip.conf` contains Cloudflare's IP ranges for restoring real visitor IPs. These should be periodically verified against https://www.cloudflare.com/ips/.
