# Secrets: 1Password references in .env

No secret is stored in plain text on the WordPress nodes. In the root `.env`
and in every `stacks/<name>/.env`, a secret value is a 1Password secret
reference instead:

```
CF_ACCOUNT_1_NAME=example
CF_ACCOUNT_1_ID=0123abcd...
CF_ACCOUNT_1_TOKEN=op://Vault/cloudflare-example/credential
MYSQL_PASSWORD="op://Vault/stack-node1-example/mysql_password"
```

Names, ids, ports, hostnames and flags stay as plain values.

## How the values are read

- A 1Password vault (named **Vault** in these examples). Items: `cloudflare-<account>` (field
  `credential`), `stack-<node>-<stack>` (fields named after the `.env` keys in
  lower case), `wpops-node-<node>` (node-level keys such as
  `WPOPS_SYNC_KEY_*`), `wpops-agent-<node>` (the agent bearer token the bot
  uses; the node keeps only its hash).
- Each node has the `op` CLI (`/usr/local/bin/op`) and a **read-only**
  service account token in `/etc/wpops/op-service-account-token` (root, 0600).
  `WPOPS_OP_TOKEN_FILE` points elsewhere; `OP_SERVICE_ACCOUNT_TOKEN` in the
  environment takes precedence.
- **1Password limits service accounts** (here: 1,000 reads per hour per token
  and 1,000 per day for the whole account, shared by every service account;
  `op service-account ratelimit` shows the current use). So wpops does not
  read 1Password per command. One batched `op inject` call resolves every
  reference of the node's `.env` files into a cache on tmpfs:
  `/run/wpops/cache/secrets.json` (directory 0700, file 0600, root only;
  memory, gone at reboot, never written to disk). Every wpops process, the
  agent and the cf scripts read that cache.
- The cache is read again from 1Password only when a `.env` file changes
  (its sha256 differs from the one recorded), a reference is missing, or
  `wpops secrets refresh` runs. There is no timer. After rotating a secret in
  1Password, run `wpops secrets refresh`.
- At boot, `wpops-secrets.service` (oneshot, before the agent) fills the cache
  if it is not fresh. It retries 3 times (30 s, 90 s) and never fails the boot.
  Running containers do not need it.
- When 1Password cannot be read (outage, rate limit), cached values keep
  working, including when they are stale, so a failover from the standby works
  as long as the cache was filled since boot. Further automatic attempts back
  off: 10 minutes, doubling to 1 hour (`/run/wpops/cache/backoff.json`). The
  agent retries in the background and sends one `secrets.unavailable` alert
  while values are missing, and one `secrets.restored` when they are back.
- A secret that is not available at all: `load_env` leaves the key out with a
  warning, so callers that do not need it keep working (failover needs only the
  Cloudflare tokens). Compose refuses to create containers when a variable its
  files use is unavailable, rather than passing the `op://` text.
- `scripts/cf-*.sh` resolve only `CF_*` references: from the environment
  (cf-backup), else the wpops cache, else one `op read`.

### Reads per day

A full refill reads every referenced item once: about 35 items per node (10
Cloudflare accounts, 23 stacks, the node item). In normal operation
(scheduled backups, bot status polls, registry timers, failover plans,
compose commands) the cache is used and **no reads** happen. Reads happen
only at boot, after a `.env` change, or on `wpops secrets refresh`.

## Docker Compose

Compose reads the stack's `.env` for interpolation, and would see the
`op://` strings. Every compose command wpops runs that creates containers
(`up`, `run`, `create`: stack up, restores, WP-CLI in a one-off container,
failover cron, tunnels) gets the resolved secrets from the cache as process
environment, which takes precedence over `.env` in interpolation. Commands
that only look at or act on existing containers (`ps`, `logs`, `exec`, `stop`,
`restart`, `config`, ...) get no secrets.

**Run compose through wpops:**

```bash
scripts/wpops/bin/wpops stack compose <stack> ps
scripts/wpops/bin/wpops stack compose <stack> up -d wordpress
scripts/wpops/bin/wpops stack compose <stack> exec wp-cron wp plugin list --allow-root
```

A plain `docker compose up` in `stacks/<stack>` would create containers with
the literal `op://...` strings as passwords (the site loses its database
connection). Read-only commands such as `docker compose ps` or `logs` are
harmless, but use wpops for anything that creates or recreates a container.

## Tunnel connectors

`docker-compose.wpops-tunnels.json` holds no tokens. Each connector has
`TUNNEL_TOKEN: ${TUNNEL_TOKEN_<ACCOUNT>}` and cf-backup has
`CF_ACCOUNT_n_TOKEN: ${CF_ACCOUNT_n_TOKEN}`. `wpops tunnels ensure` fetches
the tunnel tokens from the Cloudflare API (with the account tokens from
1Password) and passes them to `docker compose up`. Start or recreate the
connectors only with `wpops tunnels ensure`.

## Checking

```bash
scripts/wpops/bin/wpops secrets check            # cache state per .env file, plaintext keys, tunnel file (no 1Password reads)
scripts/wpops/bin/wpops secrets check --fresh    # refill the cache first (one 1Password call)
scripts/wpops/bin/wpops secrets refresh          # refill the cache (one call), e.g. after rotating a secret
```

`check` prints key names and counts, never values. It exits 1 when a secret is
missing from the cache, a secret key is in plain text, or a `.env` file
changed since the cache was filled (stale: run `secrets refresh`).

## New stacks

`wpops stack create` generates the stack's passwords into its `.env` in plain
text (the node's service account is read-only). Store them in 1Password as
`stack-<node>-<stack>` and replace the values with references; `wpops secrets
check` lists such keys until that is done.

## What still holds secrets on a node (by design)

- The tmpfs cache above (memory only, root only, cleared at reboot).

- Docker keeps every container's environment in its own configuration
  (`docker inspect`, `/var/lib/docker/containers/*/config.v2.json`, root
  only). Compose passes database and Redis passwords and tunnel tokens to the
  containers as environment variables, so they are there while a container
  exists. Removing that needs secrets mounted as files instead (a change to
  the images' configuration), not done here.
- `wp-config.php` in each stack's `data/` holds the database password, as
  WordPress requires.
- SiteGuard backup sets and database dumps under `backups/` hold the sites'
  databases.
