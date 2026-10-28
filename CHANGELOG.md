# Changelog

Versions are git tags (`vX.Y.Z`). This repo has no build; tags mark reviewed states of the stack and scripts.

## [0.0.3] - 2026-10-28

### Added
- Tests for token rotation, abandoned jobs, backup pruning, API input checks, and the Cloudflare shell scripts (backup, failover and rollback against a fake Cloudflare API). CI now syntax-checks every shell script.

### Changed
- `cf-failover.sh` configures tunnel ingress for every planned hostname before it changes any DNS record, and stops with DNS untouched if that fails. Before, ingress came after the switch, and a failure there left switched domains without a route that a re-run would never add.
- `cf-rollback.sh` restores DNS first and removes tunnel ingress afterwards, only for hostnames that were actually restored.
- Cloudflare API calls in the shell scripts time out (15 s connect, 120 s total) instead of hanging.

### Fixed
- `wpops token rotate` now takes effect immediately: the agent kept accepting the old token until it was restarted.
- A job left `running` by a restarted or crashed agent no longer blocks every later backup pull and restore (the scheduler silently skipped scheduled backups forever). Jobs record their process, and the agent marks abandoned ones as failed when it starts.
- `backup prune` (CLI or API) could delete a set whose download was still running. A set that failed checksum verification counted as complete and could push a good set out of retention.
- The agent API rejects malformed requests with 400 instead of a 500 error: `retain` of 0 or less on prune (which deleted every unkept set), non-string set references, and non-list `sites`/`entities`.
- `cf-failover.sh` aborted with "unbound variable" (`STANDBY_DOMAIN`) after the mandatory backup, so the legacy failover never got to switch DNS.
- `cf-rollback.sh` put back `unknown.cfargotunnel.com` instead of the original CNAME when the A record could not be created and no tunnel was found by name.
- `cf-backup.sh` stopped at the first domain whose zone could not be found (`set -e`), skipping every later domain and tunnel backup.
- The Cloudflare API token and new tunnel secrets were passed to curl as command-line arguments, visible to any local user in the process list. The per-run zone cache in `/tmp` is only used when it is a private directory owned by the caller.
- `cf-setup.sh` writes `docker-compose.tunnels.yml` (which holds the connector tokens) as 0600. Removing a domain from the failover list no longer also cuts it out of longer names (`a.com` from `shop.a.com`), and a non-numeric choice no longer ends the script.
- Nginx denies `/wp-content/wpops-host-aliases.json`, which listed production domains, standby hosts and blog IDs to anyone.
- The notification webhook no longer follows redirects, so its bearer token cannot be forwarded to another host.

## [0.0.2] - 2026-10-28

### Added
- Independent production stacks with `wpops stack create|init|up|stop|list|compose`: any number of single sites and multisite networks per node, alone or mixed. Each stack has its own MariaDB, Redis, content, cron, credentials and private networks; only its Nginx joins the shared `wpops-edge` network under a unique name.
- `wpops routes plan|apply`: publish each stack's standby (and, on a primary node, production) hostnames through the node's Cloudflare tunnels; DNS that points elsewhere is refused.
- Backup source profiles (`backup_sources` in the inventory) for several WordPress installations on one origin.
- Custom login paths per stack and per multisite blog (`--login-path` at creation, `wpops stack login`), using a checksum-pinned WPS Hide Login with the policy kept outside wp-content so it survives restores; custom login URLs get the same Nginx rate limit as `/wp-login.php`.
- `WP_MODE=single` for the root stack; the root stack's WP-CLI cron container now shares the WordPress config.
- Offline deployment tests (`tests/`) and GitLab CI; production guide in `docs/deployments.md`.

### Changed
- Failover prepares tunnel ingress and host aliases before switching DNS, includes sibling `www` CNAMEs, keeps the original records when retrying, refuses DNS changed since the plan, and must run on the destination node.
- `wpops tunnels ensure` runs a standalone connector project (`docker-compose.wpops-tunnels.json`) joined to every stack's ingress network and reuses the tunnel IDs in the inventory; new tunnels are named per node. Stop old `--profile tunnels` connectors before starting it.
- `cf-backup.sh` and `cf-status.sh` find tunnels by inventory ID and no longer require the failover-only settings.

### Fixed
- On a node with more than one stack, tunnel routes for a legacy stack name its Nginx container (`<project>-nginx-1`) instead of `nginx`, which every stack's Nginx answers to on the shared networks.
- `SUNRISE` is only defined when `WP_SUNRISE=true`; WordPress loads `sunrise.php` whenever the constant exists.

## [0.0.1] - 2026-10-14

### Fixed
- `scripts/cf-rollback.sh`: two top-level `local` assignments aborted the script under `set -e` in the error path that restores the original CNAME, which could leave a domain without a DNS record.
