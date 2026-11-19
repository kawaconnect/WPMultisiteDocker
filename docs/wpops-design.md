# wpops: WordPress operations control plane

Design for the failover, backup-sync and restore features that are driven
from the SiteGuard Discord bot. Written 2026-10-13 against the current
estate (Azure App Service `primary-app` as production, `node2` as the
Cloudflare-tunnelled standby), but every part is generic: any number of
WordPress origins, any of them primary, single-site or multisite, controlled
over the LAN or over the internet.

## 1. Vocabulary

| Term | Meaning |
|---|---|
| **origin** | Something that serves WordPress. Kinds: `external` (Azure App Service, any host we do not manage; known by its DNS target) and `node` (a docker host running this repo, reachable through its own `wpops` agent). |
| **stack** | One compose project on a node: the main standby network (`wordpressmultisite`, multisite) or an extra single-site/multisite project under `stacks/<name>/`. A node may run several. |
| **site** | One public website: a blog in a multisite stack or the whole of a single-site stack. Identified by a slug (`example-shop`). A site has production domains, a per-origin internal hostname, and a Cloudflare account that owns its zones. |
| **primary** | The origin a site's production domains point at when nothing is overridden. Set per site in the inventory. Changing it is a config change, not a failover. |
| **override** | A site whose production DNS currently points somewhere other than its primary. Overrides are what the Discord banner reports. |
| **backup source** | A WordPress install (any origin) that runs the SiteGuard Backup plugin and exposes the sync API. Nodes pull from sources on a schedule. |

## 2. Components

```
┌────────────────────────────┐    HTTPS (tunnel) or SSH     ┌─────────────────────────────┐
│ Discord bot (k8s)          │ ───────────────────────────► │ node agent  `wpops`         │
│  /wp failover|backup|      │                              │  CLI + HTTPS API            │
│      restore|sites|perms   │ ◄─────────────────────────── │  inventory, state, jobs     │
│  banner, alerts, email     │   JSON results / job events  │  Cloudflare DNS + tunnels   │
└────────────┬───────────────┘                              │  SiteGuard pull + restore   │
             │ GET /wp-json/sgbackup/v1/health              └──────────┬──────────────────┘
             ▼                                                         │ GET /wp-json/sgbackup/v1/sync/*
┌────────────────────────────┐                              ┌──────────▼──────────────────┐
│ WordPress (any origin)     │                              │ WordPress (backup source)   │
│  SiteGuard health API      │                              │  SiteGuard sync API         │
└────────────────────────────┘                              └─────────────────────────────┘
```

### 2.1 Node agent (`scripts/wpops/`, Python 3.10+, stdlib only)

Installed on every docker node. One code base, two entry points:

- `wpops <command> [--json]`: CLI (what an operator or an SSH forced
  command runs).
- `wpops serve`: HTTPS API (systemd unit `wpops-agent`), bound to
  127.0.0.1 and published through the node's own Cloudflare tunnel as
  `ops.<node-standby-domain>` (for example `ops.standby.example.net`).
  Authentication is a bearer token (hashed at rest, rotated with
  `wpops token rotate`). Every request is logged to `state/audit.log`.

Both paths call the same Python functions, so the bot behaves identically
whether it reaches a node by LAN SSH or by internet HTTPS.

Config lives in `config/wpops.json` (node) and `config/inventory.json`
(sites and origins, shared between all nodes and the bot; the bot caches a
copy). The examples below are written as YAML to keep them short; the files
themselves are JSON (`config/*.example.json`). Secrets stay in `.env` (Cloudflare tokens, SMTP) and are never
returned by the API.

Sub-commands (all idempotent, all with `--dry-run` where they change
anything, all JSON-capable):

| Command | What it does |
|---|---|
| `status` | Node health: stacks, containers, tunnels, agent version, last sync, active overrides. |
| `sites list` | Inventory with current DNS target per domain and whether it is overridden. |
| `failover plan --to <origin> --sites a,b \| --all \| --down <list>` | Compute DNS changes. No side effects. |
| `failover apply ...` | Same selection; backs up the zone, writes `state/overrides.json` *before* touching DNS, switches records, updates tunnel ingress. Partial failure leaves a resumable state. |
| `failover rollback --sites ... \| --all` | Restore each domain's recorded original record exactly (A or CNAME, proxied flag, TTL). |
| `backup sources` / `backup pull --source <name> [--label]` | Pull a full backup set from a source's sync API into `backups/<source>/<timestamp>/`, verify sizes and hashes, write `manifest.json`. |
| `backup list` / `backup prune` | Retention: keep N weekly sets per source (configurable, default 4) plus any set marked `keep`. |
| `backup schedule` | Shows/sets the schedule (`weekly:sun:03:00`, `daily:03:00`, cron string). The agent runs the schedule itself (no system cron needed). |
| `restore apply --set <id> --stack <name> [--sites a,b \| --all] [--db-only\|--files-only]` | Imports the set into the stack's SiteGuard and restores it. Multisite: whole network or chosen subsites (`--subsite-mode` batch map). Single-site: whole site. Pre-restore safety backup is always taken. |
| `jobs list` / `jobs get <id>` / `jobs tail <id>` | Long operations (pull, restore, failover) run as jobs with progress and a log file; the bot polls these. |
| `notify test` | Sends the configured e-mail/webhook notification. |
| `token rotate` / `serve` | Agent management. |

### 2.2 Inventory (`config/inventory.json`)

```yaml
origins:
  azure:            # kind external: identified by what DNS points at
    kind: external
    targets:        # any of these counts as "pointing at azure"
      - {type: A, content: 203.0.113.10}
      - {type: CNAME, content: primary-app.azurewebsites.net}
      - {type: CNAME, content: primary-app-multisitewp.azurewebsites.net}
    sync_api: https://example-primary.com      # SiteGuard sync API base, optional
  node2:
    kind: node
    agent: https://ops.standby.example.net    # bot → agent (internet)
    ssh: root@192.0.2.10                  # bot → agent (LAN), optional
    standby_domain: standby.example.net
    staging_suffix: "n2"                  # staging names <domain-without-dots>-n2.<standby_domain>
    stacks:
      main:  {path: /opt/docker/wordpressmultisite, mode: multisite, service: "http://nginx:80"}
    tunnels:                                # per Cloudflare account, created by cf-setup
      primary-account: 00000000-0000-0000-0000-000000000001
      example-three:    00000000-0000-0000-0000-000000000002

sites:
  - slug: example-shop
    name: Example Shop
    primary: azure
    cf_account: example-shop
    domains: [example-shop.com, www.example-shop.com, example-shop.net, www.example-shop.net]
    hosts:                                  # how each origin serves it
      azure:  {host: example-shop.com}
      node2: {stack: main, blog_id: 11}    # host defaults to example-shopcom-n2.standby.example.net
```

Rules the agent enforces:

- A domain can only be switched to a node that has a tunnel for the
  domain's Cloudflare account **and** a stack entry for the site. Otherwise
  `plan` refuses with the reason (this is what would have black-holed six
  accounts today).
- "Down" is decided by the bot (SiteGuard health + its own probes) and
  passed explicitly as a list; the agent never guesses.
- The original DNS record is stored verbatim before any change, so rollback
  restores exactly what was there, whether that was an A record to Azure or
  a CNAME to `primary-app.azurewebsites.net`.

### 2.3 SiteGuard Backup plugin: sync API

New REST routes under `sgbackup/v1/sync/`, protected by a dedicated sync
key (separate from the health key; `wp siteguardbackup sync_key create`,
stored hashed like the health key). All responses `Cache-Control: no-store`,
rate limited like the health API.

| Route | Purpose |
|---|---|
| `POST /sync/backup` `{label, entities?, subsites?}` | Start a full (network) backup. Returns the job nonce. The plugin's own resumable job machinery does the work under WP-Cron, exactly as a scheduled backup would. |
| `GET /sync/jobs/<nonce>` | Progress / finished / failed. |
| `GET /sync/sets` | Backup sets with every file name, size and SHA-256. |
| `GET /sync/sets/<nonce>/files/<name>` | Streams one file; honours `Range` so the agent can resume. |
| `DELETE /sync/sets/<nonce>` | Optional: remove a set the puller has copied (retention stays on the source's own schedule otherwise). |

The receiver side on the node is the existing `wp siteguardbackup import
<dir>` command, which registers the set in the stack's SiteGuard history
without restoring. Apply later uses `restore`.

### 2.4 Discord bot

New module `src/services/wpops.ts` (transport: HTTPS to the agent, SSH via
the `ssh2` library as fallback; chosen per origin from the inventory) and
these commands. Every command checks the permission table first.

| Command | Permission | Effect |
|---|---|---|
| `/wp status` | view | Inventory summary, overrides, last backups, agent health. |
| `/wp failover down [to]` | failover | Switch every site the bot currently sees as down to the standby (default: the site's configured `failover_to`, else the only node). Confirms with a button; posts the plan first. |
| `/wp failover all [to]` | failover | Same, every site. |
| `/wp failover site <site> [to]` | failover | One site. |
| `/wp failover rollback [site\|all]` | failover | Restore primary. |
| `/wp backup now <source>` / `list` / `schedule` / `retention` | backup | Pull control. |
| `/wp restore apply <set> [stack] [sites]` | restore | Apply a pulled set (never automatic). |
| `/wp perms grant|revoke|list <@user\|@role> <capability>` | admin | Delegation. Capabilities: `view`, `failover`, `backup`, `restore`, `admin`. ADMIN_USER_IDS always have `admin`. Stored in the bot's SQLite (`permissions` table) per guild. |

Status board: when any override is active, the embed gets a red banner at
the top ("FAILOVER ACTIVE, 3 sites on node2 since 14:02, /wp failover
rollback to restore") that stays until the override is gone. The per-site
WordPress list collapses to one line per origin ("Azure WordPress 14/14 up",
"node2 standby: agent OK, tunnels 2/2, last backup 2d ago") with the down
sites named inline; `/sg-monitors` still shows every site.

Backup events: the bot polls each node's `jobs` and posts "backup pulled"
with size and site count, and e-mails the same through the SMTP already
configured in the bot (`/setup smtp`). The agent can also e-mail directly
(`notify.smtp` in `wpops.json`) for nodes that run without the bot.

### 2.5 Uptime Kuma

- One HTTP monitor per node agent (`https://ops.<standby>/healthz`) and one
  per tunnel origin (`https://<standby-domain>/`). Grouped under
  "WordPress nodes".
- Certificate expiry notification turned on for every HTTPS monitor and the
  Kuma expiry thresholds set to 21/14/7 days. SiteGuard's own SSL days are
  shown on `/sg-monitors`; the bot alerts at the same thresholds.

### 2.6 Site registry (one node, many deployments)

With every site in its own stack, a single SiteGuard key no longer covers the
estate: each stack is its own WordPress install with its own SiteGuard and
its own health key. The agent therefore keeps a registry of every deployment
on its node and is the one thing a client (the bot, SiteGuard tooling) talks
to. A client registers a node once, with the node's agent token, and learns
its sites from it.

**What is a site.** Every single-site stack, and every live blog (not
archived, deleted or spam) of every multisite stack, mapped in the inventory
or not. Identity: `uid` = `<node>:<stack>:<blog id>` (blog id 1 for a single
site), plus a numeric `site_id` that is unique on the node and never reused.
Mapped sites carry their inventory slug, production domains, primary and
Cloudflare account; unmapped blogs carry their own hostname.

**Per site:** slug, name, URL, host, domains, stack, mode, blog id; health
(status, HTTP code, response time, SSL days/issuer, consecutive failures,
`source` siteguard|direct); WordPress, PHP and SiteGuard versions and the
plugin list (per stack); last SiteGuard backup and backup count, latest set
pulled from it and latest set restored onto it; disk usage (stack data,
uploads of that blog, the filesystem's free space); tunnel for its
Cloudflare account and any failover override of its domains (`?live=1`
adds where DNS points now).

**How it is collected.**

| Data | Source | When |
|---|---|---|
| Membership | inventory + facts | every minute, and at once after `stack create/init/remove`, `site add/remove` |
| Health | the stack's SiteGuard `/health` over its loopback port with the agent's key; otherwise `GET /` (or the blog's path) with the site's Host | every minute |
| Versions, plugins, blogs, last SiteGuard backup | one `wp eval` per stack | `registry.facts_interval_min` (360), after a restore, on `registry refresh --facts` |
| Disk usage | walk of `stacks/<name>/data` | with the facts |

Requests to a stack go to `127.0.0.1:<HTTP_PORT>` (legacy root stack: 8080,
or `http_port` in its inventory entry) with `X-Forwarded-Proto: https`.
SiteGuard data older than `registry.stale_after_min` (30) triggers one
`POST /health/check` (at most every 10 minutes); data older than four times
that is not trusted and the site is checked directly.

**SiteGuard keys.** With `registry.siteguard_enroll: "auto"` (default) the
agent enrols a key in every stack where SiteGuard is active: SiteGuard 1.6.4+
gets a *node key* (`wp siteguardbackup health_api_key --set-node`, read from
stdin), accepted next to the site's own key, which stays as it is. Older
SiteGuard gets `--set` only when the site has no key at all; otherwise the
site is checked directly until SiteGuard is updated (or `wpops siteguard
enroll <stack> --replace`). The monitor is switched on if it was off. Keys
live in `state/siteguard-keys.json` (0600) and never leave the node. A key
the site stops accepting (a restore replaced its options) is enrolled again,
at most every 30 minutes; a restore also schedules a facts refresh.

**API** (bearer agent token, as every other route):

| Route | |
|---|---|
| `GET /v1/registry[?live=1]` | `{node, revision, stacks[], sites[]}` with everything above |
| `GET /v1/registry/sites/<uid \| stack:blog \| slug>` | one site |
| `GET /v1/registry/changes?since=<rev>&limit=` | `{revision, reset, changes: [{rev, ts, op: added\|removed\|changed, uid, site}]}`; `reset: true` means the feed no longer reaches back that far: fetch `/v1/registry` |
| `GET /v1/siteguard/health` | every site in SiteGuard's health format (`api_version` 2: `summary`, `alerts`, `sites[]`), each row with `uid`, `node`, `stack`, `blog_id`, `slug`, `check_source`, versions and `last_backup` |
| `GET /v1/events?after=<seq>` | events now carry a monotonic `seq`; `after` pages forward from it (`since=<unix time>` still works) |
| `POST /v1/registry/refresh {facts?, stacks?}` | health pass now; with `facts` a `registry.refresh` job |
| `POST /v1/registry/sites/add {slug, stack, blog_id?, host?, domains?, cf_account?, primary?, name?, failover_to?}` | map a site (new, or this node's host of an existing one) |
| `POST /v1/registry/sites/remove {slug, everywhere?}` | unmap it from this node (refused while a failover override is active) |
| `POST /v1/siteguard/enroll {stack, replace?}` | enrol a key now |

`GET /v1/status` gains `registry: {revision, sites, status counts}`.

**Events** (`state/events.jsonl`, webhook; no e-mail): `registry.site_added`,
`registry.site_removed`, `registry.site_changed` (`uid`, `site_id`, `slug`,
`stack`, `url`, `rev`), `siteguard.enrolled`.

**Adding and removing.** `wpops stack create/init` add a stack (and its
blogs once facts are in), `wp site create` in a network adds a blog on the
next facts refresh, `wpops site add/remove` maps sites, and `wpops stack
remove NAME --yes [--detach-sites] [--keep-files]` stops a stack (`compose
down`, data kept), drops it from the inventory and moves `stacks/<name>` to
`stacks/_removed/<name>-<time>`. Nothing is deleted, and DNS records and
tunnel ingress for its hostnames are left alone. Every one of these shows up
in the change feed, so clients follow without any configuration of their own.

## 3. Scenarios covered

| Scenario | How |
|---|---|
| Azure primary, one docker standby (today) | Inventory as above. |
| Two docker nodes, either primary | Both are `node` origins with agents; `primary:` per site picks one; failover `--to` the other; rollback returns to primary. The sync schedule can point at either node as source. |
| Many single-site stacks on one node, monitored per site | Site registry (2.6): one agent token per node; each stack's SiteGuard read with a node key; sites appear and disappear through the change feed. |
| Single-site stacks | `stacks/<name>/` compose project generated by `wpops stack create <name> --mode single --domain x`; the tunnel ingress maps the hostname to that stack's nginx; inventory `hosts.<node>.stack = <name>` with no `blog_id`. Restore on a single-site stack restores the whole site. See [deployment guide](deployments.md). |
| Site on both a multisite and a single-site stack | Allowed; `hosts` lists both; failover `--to` uses that origin's configured stack mapping. `--stack` must match it. |
| Bot cannot reach the LAN | Agent transport `https` through the node's tunnel; SSH is only an optional fallback. |
| Node cannot reach the bot / no bot at all | Agent runs its own schedule, prune and e-mail; CLI works locally; API works for any other client. |
| Partial failover failure | Overrides file written first; `failover apply` is re-runnable and skips already-switched domains; `rollback` uses recorded originals. |
| Backup source unreachable | Pull job fails, is retried on the next tick (max 3 per window), bot alerts after the second failure, e-mail on final failure. |

## 4. Out of scope / decisions

- The agent does not auto-apply backups. `restore apply` is always explicit.
- The agent does not decide that a site is down. The bot does, with the
  same two sources it already uses.
- Cloudflare tunnels are created by the existing `cf-setup.sh` (now
  non-interactive with `--yes`), once per account per node.
- No new database: node state is JSON files under `state/`, bot state is
  its existing SQLite.
