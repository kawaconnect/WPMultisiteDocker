# Independent WordPress deployments

A node can run any number of independent single-site WordPress installations,
multisite networks, or both, up to the host's available resources. A deployment
is called a **stack**; a multisite stack can contain many blogs. Every stack
owns its MariaDB, Redis, files, cron, credentials and private networks. Only its
Nginx joins `wpops-edge`, using a unique name such as `wpops-shop`.
Managed private networks allocate two /25s per stack from `10.214.0.0/16`
(up to 254 managed stacks per checkout), avoiding Docker default-pool exhaustion.
Adjust this range in the template if it overlaps your LAN or VPN.
Cloudflare connectors and DNS backups are shared node services. WordPress
containers do not receive Cloudflare credentials.

The original root `docker-compose.yml` remains supported. It defaults to
`WP_MODE=multisite`; set `WP_MODE=single` for a single-site root deployment.
Changing that variable does **not** convert an existing database. The isolated
`instance.sh` environments remain development tools; use the commands below
for production stacks.

## 1. Configure this node

Requirements: Python 3.10+, Docker with Compose v2.20+ (or newer), and a user
that can access Docker and write the checkout. Restoring root-owned content
may require running the wpops agent as root, as with the existing agent.

From the checkout root:

```bash
cp .env.example .env
# Edit .env: CF_ACCOUNT_n_NAME, _ID, _TOKEN and _STANDBY.
# Root-project DB credentials are unused if you deploy only managed stacks.
# See cloudflare-token-setup.md for token permissions.
wpops() { ./scripts/wpops/bin/wpops "$@"; }
wpops setup --node node-a
```

First setup writes an empty `config/inventory.json` for your chosen node.
It does not populate example sites or change DNS. The node name must be a slug.
The inventory is local and gitignored. Keep a protected copy on each node and
keep site/origin definitions consistent between nodes.

## 2. Create and initialise stacks

`create` writes configuration and registers the stack; it starts no containers
and changes no DNS. Supply a different loopback HTTP port for each stack; `create`
refuses a port that another stack has or that is in use on 127.0.0.1 (the legacy
root stack publishes 127.0.0.1:8080).
A custom login path downloads WPS Hide Login 1.9.19 from WordPress.org and
checks its pinned SHA-256. Its verified copy lives outside WordPress content.

Two independent single sites:

```bash
wpops stack create shop --mode single --domain shop.example.com --port 18081 --login-path staff-door
wpops stack create blog --mode single --domain blog.example.com --port 18082 --login-path editors
```

One multisite network (use just this command for a multisite-only node):

```bash
wpops stack create network --mode multisite --domain network.example.com --port 18083 --login-path network-staff
```

Running all three gives a mixed node. Add further single or multisite stacks in
the same way. Multisite defaults to domain/subdomain mode; use
`--subdirectories` for a path-based network. `init` creates only the main blog;
add blogs explicitly with WP-CLI or Network Admin. New blogs get the main site's
`https://` scheme (WordPress itself would give subdomain blogs `http://`). For standby/staging stacks,
use the standby hostname as `--domain` and add `--noindex` at creation.

```bash
wpops stack init shop --admin-email admin@example.com
wpops stack init blog --admin-email admin@example.com
wpops stack init network --admin-email admin@example.com
wpops stack compose network exec -T wp-cron wp site create --slug=news --title=News --email=admin@example.com
wpops stack list
```

Initial credentials are in `stacks/<name>/.env` (0600); passwords are passed to
the installer via stdin. `init` refuses to install over a populated database.
If you need HTTP-only local testing, pass `--scheme http` to `init` and visit the
loopback port with the configured Host header. Production URLs default to HTTPS.
Image tags match the existing production stack. `WORDPRESS_IMAGE` in a stack's
`.env` can select a compatible FPM image; review upgrades against your plugins.

Use `wpops stack up NAME`, `wpops stack stop NAME`, or
`wpops stack compose NAME <compose arguments>`. The latter runs any Compose
command in the stack's project, such as `logs -f`, `config`, `exec`, `pull` and
`down`, with Compose's own output and exit status (`--json` captures it instead). There is no
automatic destructive database migration when changing the deployment mode.

## 3. Map sites to stacks and origins

`stack create` writes the local `origins.node-a.stacks` entries automatically.
Add logical sites and remote origins to `config/inventory.json`. A single-site
stack takes one logical site, with any number of domain aliases. Each blog in a
domain-based multisite can be a logical site with its own domain list and blog ID.
For path-based blogs sharing a domain, DNS failover applies to the whole domain;
represent that network as one logical site at its main blog. Every blog below it
then follows: with `example.org` mapped to the main blog of a network installed
as `paths.example.net`, `https://example.org/alpha/` serves the `/alpha/` blog.
DNS cannot switch one URL path independently of another path on the same hostname.

A site can use a different stack on each node. Example site fragment:

```json
{
  "slug": "shop",
  "primary": "primary",
  "failover_to": "node-a",
  "cf_account": "customer",
  "domains": ["shop.example.com", "www.shop.example.com"],
  "hosts": {
    "primary": {"host": "shop.example.com"},
    "node-a": {"stack": "shop", "host": "shop.standby.example.net"},
    "node-b": {"stack": "shared-network", "blog_id": 7, "host": "shop.other-standby.example.net"}
  }
}
```

Define `primary` as an external origin with the A/CNAME targets that identify
its production DNS. Define `node-b` as a node with its stacks and tunnels if
using it. Omit unused origins/host entries. A node can itself be a site's
`primary`, so this supports active primary stacks as well as standby copies.
Set `host` to the site's actual installed hostname on that origin. Multisite
hosts require a positive `blog_id`.

To publish a stack under a staging name while WordPress keeps its production
siteurl (for example a node holding a copy of `shop.example.com` that should be
reachable as `shopexamplecom-node-a.standby.example.net`), set `host` to the
staging name and `site_host` to the siteurl host:

```json
"node-a": {"stack": "shop", "host": "shopexamplecom-node-a.standby.example.net", "site_host": "shop.example.com"}
```

`wpops routes apply` publishes `host` (DNS in the standby zone, tunnel ingress
to the stack) and writes it into the stack's host-alias map, so requests for the
staging name are answered with staging URLs and nothing in the database
changes. Failover and restore treat `site_host` as the stack's own hostname.
Without `site_host`, `host` is the siteurl host, as before.
A staging name is published even while the site has `enabled: false`, so a
prepared copy can be checked before cutover; its production domains stay out of
routes and failover until the site is enabled. All selected sites must have mappings for
the destination node. `--stack` on failover cannot bypass that mapping.

For an existing root stack, configure its absolute `path`, `mode`, and
`service: "http://nginx:80"`. On a node with more than one stack, wpops routes
that stack to its container (`<project>-nginx-1`, where the project is
`project` from the inventory or the directory name), because every stack's
Nginx answers to `nginx` on the shared networks. Set `ingress_network` explicitly if its Compose
network name differs from `<directory>_frontend`. Enable `WP_SUNRISE=true` for
multisite host aliases and recreate the WordPress/cron containers. Managed
stacks enable the host-alias runtime automatically. After updating the checkout,
run `docker compose up -d` in the root directory as well, so the legacy
containers pick up changed Compose settings.

### Visitor IPs behind the tunnel

Through a tunnel, every request reaches Nginx from a cloudflared connector,
so Nginx must take the visitor IP from `CF-Connecting-IP`. If it does not,
the `/wp-login.php` and custom-login rate limit (20 requests/minute) is one
bucket shared by all visitors, and one client can lock everyone out. Nginx
believes that header only from Cloudflare's edge ranges
(`cloudflare-real-ip.conf`) and from the connector side of `wpops-edge`
(`connector-real-ip.conf`).

- **Managed stacks**: wpops creates `wpops-edge` as `10.214.255.0/24`. Only
  the connectors and each stack's Nginx are on it. WordPress, WP-CLI,
  MariaDB and Redis are not, so PHP code cannot claim another IP. The
  gateway `10.214.255.1` is not trusted: requests to the loopback-published
  port (`127.0.0.1:<HTTP_PORT>`) and from the host come from it and keep
  their own address. Keep that port on loopback.
- **Nodes created before v0.0.4** have a `wpops-edge` with a Docker-assigned
  subnet. `wpops stack up` and `wpops tunnels ensure` warn about this, and
  until it is fixed, behind-the-tunnel logins still share one bucket.
  Recreating the network briefly takes the sites down:
  ```bash
  docker compose -p wpops-<node>-tunnels -f docker-compose.wpops-tunnels.json down
  wpops stack compose <name> down          # every managed stack; data stays on disk
  docker network rm wpops-edge
  wpops tunnels ensure                     # recreates 10.214.255.0/24, starts connectors
  wpops stack up <name>                    # every managed stack
  ```
- **Legacy root stack** (Nginx on `<project>_frontend`): Docker picked that
  subnet, so it cannot ship in the repo. Look it up and trust it in a
  node-local file (`*.local.conf` is gitignored and read by every Nginx on
  the node):
  ```bash
  docker network inspect <project>_frontend --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
  echo 'set_real_ip_from <that subnet>;' > config/nginx/conf.d/real-ip.local.conf
  docker compose exec nginx nginx -s reload
  ```
  The legacy WordPress and `cf-backup` containers and that network's gateway
  (the loopback port) share this subnet and could also set the header. This
  only lets them pick the address they are rate limited as, but if that is
  not acceptable, move the legacy site to a managed stack instead.

## 4. Cloudflare routes and failover

```bash
wpops tunnels ensure
wpops routes plan
wpops routes apply --yes
```

`ensure` creates/reuses per-account tunnels and runs a standalone connector
project plus the six-hourly Cloudflare DNS/config backup worker. It works
without a root/main WordPress stack. Existing inventory tunnel IDs are reused;
new tunnel names include the node name so different nodes do not accidentally
share one tunnel. Start configured legacy stacks before starting connectors
that need their external frontend networks.

`routes` publishes this node's installed standby hostnames, plus production
hostnames for sites whose `primary` is this node. The standby Cloudflare account
must have access to the standby hostname's zone. Every hostname points to the
correct stack's unique service address, including multisite blogs and aliases.
The plan refuses existing DNS pointing elsewhere or domains with active
failover overrides. It does not take production traffic away from another
origin. Review the plan; use failover for temporary traffic moves, or migrate
existing DNS explicitly for a permanent primary deployment.

Do not run the legacy `cf-setup.sh`/`cf-failover.sh` scripts to manage this mixed
fleet: those scripts assume the root Nginx service. If migrating an existing
connector installation, stop the old connectors before starting the new
standalone connector project, to avoid competing configurations on a shared
tunnel. `cf-backup.sh`, `cf-status.sh` and the stored DNS backups remain usable.

Before a failover, pull/apply a recent backup to the destination. Failover
switches routing; it does not synchronise databases or merge writes.

```bash
# Run on node-a (the destination):
wpops failover plan --sites shop --to node-a
wpops failover apply --sites shop --to node-a --yes
# Multiple sites keep their individual stack mappings:
wpops failover plan --all --to node-a
# Return traffic to the saved original DNS records:
wpops failover rollback --sites shop --yes
```

Failover checks local stack readiness, backs up DNS/tunnel configuration, and
prepares ingress and host aliases before changing DNS. `www` CNAME aliases are
included and their original relationships are restored on rollback. The tunnel
CNAME is always proxied (a tunnel only works through Cloudflare's proxy), also
for DNS-only records; rollback restores the original proxy setting and TTL. If
Cloudflare rejects the new record, the deleted one is put back, so the name keeps
resolving; re-run apply (or rollback) once the cause is fixed. Run apply
and rollback on the node holding the local overrides. DNS and application
readiness across real nodes still require a deployment smoke test.

## 5. Backups for multiple installations on one origin

SiteGuard Backup with its sync API and WP-CLI import/restore commands must be
installed on the source and destination installations. It is an external
plugin, not bundled in this repository. Existing `origins.<name>.sync_api`
entries still work. For multiple endpoints on one origin, add named profiles:

```json
"backup_sources": {
  "primary-shop": {
    "origin": "primary",
    "sync_api": "https://shop.example.com"
  },
  "primary-blog": {
    "origin": "primary",
    "sync_api": "https://blog.example.com"
  },
  "node-b-network": {
    "origin": "node-b",
    "stack": "shared-network",
    "sync_api": "https://network.other-standby.example.net"
  }
}
```

Configure `.env` with `WPOPS_SYNC_KEY_PRIMARY_SHOP`,
`WPOPS_SYNC_KEY_PRIMARY_BLOG`, etc. (uppercase profile name, hyphens become
underscores). The legacy shared `WPOPS_SYNC_KEY` fallback also works. HTTPS is
required except for loopback sources. Profile names must differ from origin
names. Omit a legacy origin-level `sync_api` when profiles replace it, to avoid
pulling the same source twice in an unrestricted schedule.

```bash
wpops backup sources
wpops backup pull --source primary-shop
wpops backup list --source primary-shop
wpops restore apply --set primary-shop/SET_ID --stack shop --all --yes
wpops backup schedule --set daily:03:30 --sources primary-shop,primary-blog --retain 4
# For a selected domain-based blog in a multisite backup:
wpops restore apply --set node-b-network/SET_ID --stack network --sites news --yes
```

Add the `news` site's source and destination host/blog mappings before a
selected-blog restore. SiteGuard's migrate mode keeps the destination's own
WordPress users, so log in with the destination stack's credentials afterwards.
After a restore wpops restarts the stack's WordPress container (PHP-FPM). Backups are mounted read-only into every stack's cron/CLI
container. Retention, checksums, disk reserves, notifications and the existing
agent schedule apply to each source profile. Backup manifests record both the
profile and its source origin/stack so restore mapping remains unambiguous.

Topology support does not itself convert single-site databases into multisite
or split networks into singles; cross-mode content migrations depend on the
SiteGuard version and its supported migration options. Test those migrations
separately. Use matching source/destination modes for ordinary full restores.

## 6. Custom login paths

The requested behaviour uses [WPS Hide Login](https://wordpress.org/plugins/wps-hide-login/):
the login form moves to your slug, `/wp-login.php` and logged-out `/wp-admin`
access are hidden, and the authenticated dashboard still uses `/wp-admin`.
WordPress public AJAX/REST endpoints remain available to themes and plugins.
A slug is a single segment containing lowercase letters, numbers and hyphens;
reserved WordPress paths are rejected.

```bash
# Set before init, or change an existing deployment:
wpops stack login shop --path staff-door
# Per-blog overrides in a multisite network (including a subdirectory blog):
wpops stack login network --path network-staff
wpops stack login network --blog-id 2 --path news-editors
# After changing paths on a RUNNING stack, reload custom-login rate-limit rules:
wpops stack compose network exec -T nginx nginx -s reload
```

The default applies to every blog without an override, including newly created
blogs. A path-based blog uses its own site prefix (e.g.
`https://network.example.com/news/news-editors/`). Domain-mapped blogs use their
own hostname. Login, reset-password and logout links are generated by the
plugin. Custom login requests receive the same Nginx rate limit as the default
login endpoint. Exclude every custom login path from Cloudflare/page caching.
Do not create a WordPress page or another route using that slug.

Deployment policy is in `stacks/<name>/policy/login-paths.json`, mounted outside
wp-content. The deployment MU plugins also live outside restored content and
continue to load site-provided MU plugins. After a wpops restore, destination
host-alias files and the verified login plugin are reapplied. Do not activate a
competing login-hiding plugin on the same installation.

For emergency recovery, set `"enabled": false` in the policy file and deactivate
WPS Hide Login through WP-CLI if it was separately activated in the database.
This restores the normal WordPress login. Re-enable the policy after correcting
the configured slug. This feature is not an authentication replacement; normal
WordPress user permissions continue to apply.

## 7. Site registry and monitoring

Every stack and every blog of a multisite stack is tracked by the agent
(design: [wpops-design.md](wpops-design.md), section 2.6). Nothing needs to be
registered by hand: `stack create/init` and new blogs are picked up, and the
bot follows through the agent API with the node's one token.

```bash
wpops registry                         # every site: health, source, URL
wpops registry show shop               # one site: versions, plugins, backups, disk, tunnel
wpops registry refresh --facts         # collect versions/blogs/disk and SiteGuard keys now
wpops registry changes --since 0       # change feed
wpops siteguard status                 # which stacks the agent reads SiteGuard on
wpops siteguard enroll shop            # enrol now (SiteGuard 1.6.4+: node key; site key untouched)
wpops site add shop --stack shop --cf-account customer --domains shop.example.com,www.shop.example.com
wpops site add news --stack network --blog-id 2 --cf-account customer --domains news.example.com
wpops site remove news --yes           # unmap from this node (refused during a failover)
wpops stack remove tmp --yes           # compose down, unregister, move to stacks/_removed/
```

Health comes from the stack's own SiteGuard when the agent has a key there,
otherwise from a direct request through the stack's loopback port. Install
SiteGuard Backup 1.6.4 or later in each stack for SSL data and SiteGuard's own
checks. `config/wpops.json` `registry`: `enabled`, `facts_interval_min` (360),
`health_timeout` (15), `siteguard_enroll` (`auto` or `off`),
`stale_after_min` (30).

## Validation

```bash
# Optional: WPOPS_TEST_PHP=/path/to/php enables the PHP policy hook check.
python3 -m unittest discover -s tests -v
wpops stack compose shop config --quiet
wpops stack compose shop exec -T nginx nginx -t
```

The offline suite covers all deployment combinations, Compose isolation,
source profiles, destination mapping, login policy, connector generation,
mocked Cloudflare failover/rollback, and the site registry
(`tests/test_registry.py`: membership and change feed, SiteGuard and direct
health against local stand-ins for each stack, key enrolment, site and stack
add/remove, the API, and `wpops serve` run as a process with a fake `docker`).

`tests/e2e/e2e.py` is an opt-in end-to-end run on a real Docker daemon (not run
in CI). It builds a fresh node in a new directory and drives it through the
commands above against an in-memory Cloudflare API (`tests/e2e/fake_cloudflare.py`;
wpops and the `cf-*.sh` scripts use it through `CF_API_URL`), with an HTTPS
stand-in for the Cloudflare edge and connectors (`tests/e2e/cf_edge.py`). Use
a disposable Docker host, one run per host: it takes 127.0.0.1:18181-18190 (and
8080), the `wpops-edge` network and 10.214.0.0/16, and leaves the containers
running for inspection.

```bash
python3 tests/e2e/e2e.py singles   /tmp/e2e-singles     # two single sites
python3 tests/e2e/e2e.py multisite /tmp/e2e-multisite   # one subdomain network
python3 tests/e2e/e2e.py networks  /tmp/e2e-networks    # subdomain + subdirectory networks
python3 tests/e2e/e2e.py mixed     /tmp/e2e-mixed --siteguard /path/to/siteguardbackup
``` Before production use, run a live rehearsal
with disposable domains: install single/multisite stacks, test login/reset/logout,
pull and restore SiteGuard backups, switch apex and www, check the correct
site content, and roll back. Cloudflare API calls and live containers require
your credentials and Docker access; the offline suite does not simulate those
external services completely.
