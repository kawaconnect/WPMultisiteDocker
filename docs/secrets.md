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
- wpops (`config.load_env`) resolves every `op://` value of a file with one
  `op inject` call. Values are kept in the process's memory only, for 15
  minutes. If 1Password cannot be reached after that, the values already read
  keep working; a value never read gives a clear `1Password: ...` error. The op
  CLI keeps its configuration in `/run/wpops/op` (tmpfs, 0700), which also
  works inside the agent's systemd sandbox.
- `scripts/cf-*.sh` resolve references the same way (`op read`). A value that
  is already in the environment wins; the `cf-backup` container gets its
  Cloudflare tokens that way.

## Docker Compose

Compose reads the stack's `.env` for interpolation, and would see the
`op://` strings. Every compose call wpops makes (`wpops stack up|stop|compose`,
WP-CLI, restores, failover cron, tunnels) passes the resolved secrets as
process environment, which takes precedence over `.env` in interpolation.
Nothing is written to disk.

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
scripts/wpops/bin/wpops secrets check            # every reference resolves; no plaintext secret keys
scripts/wpops/bin/wpops secrets check --no-resolve   # plaintext only, without asking 1Password
```

It prints key names and counts, never values, and exits 1 on any problem.

## New stacks

`wpops stack create` generates the stack's passwords into its `.env` in plain
text (the node's service account is read-only). Store them in 1Password as
`stack-<node>-<stack>` and replace the values with references; `wpops secrets
check` lists such keys until that is done.

## What still holds secrets on a node (by design)

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
