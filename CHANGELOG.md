# Changelog

Versions are git tags (`vX.Y.Z`). This repo has no build; tags mark reviewed states of the stack and scripts.

## [0.0.15] - 2026-11-03

### Fixed
- WP-CLI on a stack whose wp-cron is stopped (a standby) failed with `service "wp-cron" is not running`, so `wpops restore apply` could not run there. wpops 0.2.7 runs WP-CLI in a one-off container of the same service (`compose run --rm --no-deps --entrypoint wp wp-cron`) when wp-cron is not running. The cron loop is never started.

## [0.0.14] - 2026-11-03

### Added
- `failover.cron_follows_traffic` in config/wpops.json (wpops 0.2.6, default false). On a standby node whose stacks keep wp-cron stopped, `wpops failover apply` starts wp-cron on every stack it switched domains to, and `wpops failover rollback` stops it again once none of that stack's domains are served by the node. Only managed stacks are touched. The audit log records which stacks changed.

## [0.0.13] - 2026-11-03

### Fixed
- Tests: the deployment tests stub the event log as well as notifications, so a pull in a test no longer writes to the repository's state/ directory (the 0.0.12 tag pipeline failed on this). wpops 0.2.5; no behaviour change from 0.0.12.

## [0.0.12] - 2026-11-03

### Changed
- Backup notifications are aggregated (wpops 0.2.4). A pull no longer e-mails or posts to the webhook; it is only recorded in the event log (`backup.pulled`). A scheduled run sends one summary, `backup.run_failed`, and only when at least one source failed. It lists every failed source and its job.
- The scheduler marks its slot when a run starts, so restarting the agent during a run does not start the run again. Turning the schedule off during a run stops it before the next source.

## [0.0.11] - 2026-11-03

### Added
- Default staging hostnames (wpops 0.2.3). A site's name on a node is `<production domain without www., dots removed>-<staging_suffix>.<standby_domain>` unless its host entry sets `host`, which stays an override. `staging_suffix` is a new node origin setting (default: the node name). Example: example-store.com on a node with suffix `node1` and standby domain standby.example.net is `examplestorecom-node1.standby.example.net`.
- The inventory is refused when a staging label is longer than 63 characters or two sites on one node share a hostname; the message says which host entry to override.
- `wpops site add` stores no `host` when the default applies and records `site_host` from the stack; `wpops stack init` returns the stack's staging name; registry members carry `staging_host`.
- `wpops tunnels ensure` publishes every staging name after starting the connectors (staging-only routes: standby-zone CNAME, tunnel ingress, host alias). It never touches production domains.
- Docs: docs/deployments.md, "Staging hostnames". Tests for the rule, the override, label length, collisions and staging-only routes.

### Changed
- A host that is not one of the site's production domains counts as a staging name for routes, whether or not `site_host` is set.

## [0.0.10] - 2026-11-03

### Changed
- Staging names (`host` different from `site_host`) are published by `wpops routes apply` and kept in the host-alias map even while the site has `enabled: false`. A prepared site can be checked at its staging name before cutover; `enabled: false` still keeps its production domains out of routes and failover (wpops 0.2.2).

## [0.0.9] - 2026-11-03

### Added
- Staging hostnames for stacks that keep their production siteurl (wpops 0.2.1): an inventory host entry may set `site_host` (the hostname WordPress uses as its siteurl) next to `host` (the name the node publishes). `wpops routes apply` publishes `host` and the agent writes it into the stack's host-alias map, so a single-site or multisite copy of a production site can be checked at a staging name without a search-replace. See docs/deployments.md, "Add logical sites".

### Changed
- The host-alias map no longer contains entries for a stack's own siteurl host.
- Failover aliases and restore source matching use `site_host` when it is set.

## [0.0.8] - 2026-11-02

### Fixed
- `tests/test_registry.py`: the agent process test waited for the first stack's facts only and failed in CI when the second stack's blogs were not listed yet. No change to wpops itself; 0.0.7 had the same code.

## [0.0.7] - 2026-11-02

Every WordPress deployment on a node, single site or multisite, is now tracked one site at a time, so monitoring no longer depends on one key for one multisite.

### Added
- Site registry in the wpops agent (wpops 0.2.0): every single-site stack and every blog of every multisite stack, with slug, domains, stack, mode, health, WordPress/PHP/plugin versions, last backup, disk usage and tunnel/override state. Sites are identified by `<node>:<stack>:<blog id>` and a numeric id that is never reused.
- API with the existing agent token: `GET /v1/registry`, `GET /v1/registry/sites/<id>`, `GET /v1/registry/changes?since=<rev>` (change feed), `GET /v1/siteguard/health` (the whole node in SiteGuard's health format), `POST /v1/registry/refresh`, `POST /v1/registry/sites/add|remove`, `POST /v1/siteguard/enroll`. `GET /v1/status` reports registry counts.
- Events `registry.site_added`, `registry.site_removed`, `registry.site_changed` and `siteguard.enrolled` (event log and webhook). Every event now has a sequence number; `GET /v1/events?after=<seq>` pages by it.
- Health per site from the stack's own SiteGuard health API, read with a key the agent enrols itself (a node key on SiteGuard 1.6.4+, so the site's own key is never replaced), or a direct HTTP check through the stack's loopback port. Stale SiteGuard data triggers one fresh check.
- CLI: `wpops registry [list|show|changes|refresh]`, `wpops site add|remove`, `wpops siteguard status|enroll`, `wpops stack remove` (stops the stack, unregisters it and moves its files to `stacks/_removed/`; nothing is deleted, DNS is not touched).
- `config/wpops.json` `registry` settings; docs: design section 2.6, deployment guide section 7.
- Tests: `tests/test_registry.py` (including `wpops serve` run as a process against stand-in stacks) and a registry stage in `tests/e2e/e2e.py`.

### Changed
- A restore schedules a registry refresh of the stack and a check of the agent's SiteGuard key.

## [0.0.6] - 2026-11-02

### Added

## [0.0.5] - 2026-10-29

Every topology (two single sites, one multisite network, a subdomain plus a subdirectory network, and a node mixing all of them with the legacy root stack) was run for real on Docker, with a stand-in for the Cloudflare API and edge. These are the problems that run found.

### Added
- `tests/e2e/`: an opt-in end-to-end run on a real Docker daemon (`e2e.py`), an in-memory Cloudflare API (`fake_cloudflare.py`) and an HTTPS stand-in for the Cloudflare edge and connectors (`cf_edge.py`). Not run in CI; see `docs/deployments.md`, "Validation".
- `CF_API_URL` (environment or `.env`) points wpops and the `cf-*.sh` scripts at such a test API. Leave it unset in production.
- Regression tests for every fix below.

### Fixed
- `wpops stack init` refused every new stack ("database is not empty or cannot be inspected"): its empty-database check used `wp db tables`, which fails until WordPress is installed. It now asks MariaDB directly.
- `wpops stack init` printed the generated admin password (WP-CLI echoes the prompted value back). It is now left out of the output.
- `wpops backup pull` always failed at the end of the download with `name 'info' is not defined`, so no backup set could be pulled (CLI, API or schedule).
- After a database restore wpops reloaded PHP-FPM with `USR2`. Docker does not restart a container whose PHP-FPM re-executed itself that way, so after the next Docker or host restart that stack's WordPress stayed down. wpops now restarts the WordPress container instead.
- PHP-FPM could not signal its own workers (no `KILL` capability in the WordPress containers), so a reload never finished and every stop waited for a forced kill. Added to the managed, legacy and instance Compose files.
- Failing over a DNS-only (grey cloud) record created an unproxied CNAME to the tunnel, which does not work, so the site went offline. The tunnel CNAME is now always proxied; rollback still restores the original setting.
- When Cloudflare rejected the new record during failover or rollback, the old one had already been deleted and the name was left without any record (and a `www` CNAME pointing at it failed too). The deleted records are now put back; re-running apply or rollback finishes the job.
- After failing over the domain of a subdirectory network, only the main blog worked: `example.org/alpha/` showed the main blog's 404 page. The alias now covers every blog below the mapped one, with links that keep the blog's path.
- Blogs created with WP-CLI on a subdomain network got `http://` URLs. New blogs now follow the main site's `https://`.
- A Cloudflare error for one account during `wpops tunnels ensure` removed that account's connector from the generated connector project. The existing connector is now kept.
- Legacy root stack on a new node: MariaDB could not take ownership of its new data directory and never started, and Redis could not save, so it refused every write. Added the missing capabilities.
- Legacy root stack: its WP-CLI/cron container was only on the internal network, where name lookups fail on hosts with a DNS search domain, so `wp` commands (and wpops restores) could not reach the database. It now also joins the frontend network, like the managed stacks.
- `wpops stack create` accepted a port already in use on 127.0.0.1 (such as the legacy stack's 8080); the stack then could not start.
- `wpops stack compose` swallowed Compose's output: `nginx -t` (from the docs) printed nothing, `logs -f` could not stream and failures did not say why. It now runs Compose directly and returns its exit status. Failed `stack up`/`stop` show Compose's error.
- Cloudflare API errors in `wpops routes`, `failover` and other commands ended in a Python traceback instead of an error message.
- The agent accepted restore requests for unknown stacks, sets or sites and failed them later as jobs (holding the restore slot meanwhile); they are now rejected with 400. `wpops backup schedule --sources` rejects unknown sources, as the API already did.
- `wpops setup` told operators to set `WP_SUNRISE=true` in managed stacks' `.env`, which they already have.

### Not changed
- The legacy `cf-failover.sh` still copies the original record's proxied flag to the tunnel CNAME (the problem fixed above for wpops): its rollback has no record of the original setting, so making the CNAME proxied would silently change grey-cloud records on rollback. Use `wpops failover` for DNS-only records.

## [0.0.4] - 2026-10-28

### Added
- `config/nginx/conf.d/connector-real-ip.conf`: Nginx takes the visitor IP from `CF-Connecting-IP` when the request comes from a tunnel connector on `wpops-edge`. The network gateway, which carries the loopback-published port and host traffic, is excluded. Cloudflare's own ranges stay trusted as before.
- Node-local `config/nginx/conf.d/*.local.conf` (gitignored) for the legacy root stack's Docker-assigned frontend subnet; steps in `docs/deployments.md` ("Visitor IPs behind the tunnel").
- Tests: the trusted ranges match the edge subnet, and a live Nginx check in a private network namespace (CI installs Nginx for it).

### Changed
- wpops creates `wpops-edge` with the fixed subnet `10.214.255.0/24`. `wpops stack up` and `wpops tunnels ensure` warn when an older network with another subnet exists; the docs explain how to recreate it.
- The tunnel project's `cf-backup` container no longer joins `wpops-edge`, so only connectors and the stacks' Nginx are on it.

### Fixed
- Behind the tunnel every visitor reached Nginx from the connector's address, so the `/wp-login.php` and custom-login rate limit (20 requests/minute) was one bucket shared by everyone, and one client could lock all users out of logging in. Logs and WordPress also saw the connector's address instead of the visitor's.

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
