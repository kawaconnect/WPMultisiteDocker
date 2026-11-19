# Keep secrets out of container command lines, PHP source and helper containers

## Why

A security review found three places where stack secrets were more exposed than needed:

- Redis got its password as `--requirepass <password>` on the command line and the healthcheck as
  `redis-cli -a <password>`. Both show in `docker inspect` (Args, Healthcheck) and the password of every stack was
  readable in the host's process list during each healthcheck.
- The root `docker-compose.yml` pasted `.env` values into PHP (`WORDPRESS_CONFIG_EXTRA`). A quote or backslash in a
  value broke `wp-config.php` or injected PHP. The managed stack template already reads values with `getenv()`.
- `cf-backup` ran `apk add` as root on every start (network fetch, unpinned packages) and had the whole node `.env`
  mounted, although it only reads the Cloudflare settings.

## What changes

- Redis (stack template and root compose): the password is an environment variable of the redis container; the
  entrypoint hands `requirepass` to `redis-server` on stdin (`redis-server -`, which includes `redis.conf`), and the
  healthcheck uses `REDISCLI_AUTH` from the environment. Nothing secret is on a command line.
- Root compose and the instance template: PHP reads `WP_MODE`, `WP_SUBDOMAIN_INSTALL`, `WP_DOMAIN_CURRENT_SITE`,
  `WP_SUNRISE`, `REDIS_*` (`WP_MODE`, `WP_DOMAIN` for instances) with `getenv()`; compose passes them as environment.
- cf-backup: built once from `docker/cf-backup/Dockerfile` (pinned Alpine, bash, curl, jq). The image tag follows the
  Dockerfile's hash, so a changed Dockerfile builds a new image at the next `wpops tunnels ensure`. It gets only the
  `CF_*` settings as environment, no `.env` mount, no capabilities, a read-only root and `/tmp` on tmpfs.
  `scripts/cf-common.sh` accepts settings from the environment alone when there is no `.env`.

## Out of scope

- Moving the WordPress and database passwords out of the containers' environment (they stay visible to
  `docker inspect` for root, as before).
- Changing how wpops resolves secrets from 1Password.

## Rollout

Recreate one container at a time on the standby node first (`wpops stack compose <stack> up -d redis`, then
`wordpress`/`wp-cron` are unchanged for managed stacks), check every site, then production. `wpops tunnels ensure`
rebuilds cf-backup.
