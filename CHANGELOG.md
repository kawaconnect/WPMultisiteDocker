# Changelog

Versions are git tags (`vX.Y.Z`). This repo has no build; tags mark reviewed states of the stack and scripts.

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
