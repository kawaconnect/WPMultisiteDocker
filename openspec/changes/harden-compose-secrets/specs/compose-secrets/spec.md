# compose-secrets

## ADDED Requirements

### Requirement: No secret on a container command line
Compose files SHALL NOT put a password or token into a container's command, entrypoint arguments or healthcheck
definition.

#### Scenario: Redis password
- **WHEN** a stack is rendered with `docker compose config`
- **THEN** the redis service's command, entrypoint and healthcheck contain no password, and the password is only in
  its environment

### Requirement: .env values are not PHP source
Values from `.env` SHALL reach WordPress through the container environment and be read with `getenv()`.

#### Scenario: A value with a quote
- **WHEN** `WP_DOMAIN_CURRENT_SITE` contains `'` or `\`
- **THEN** `WORDPRESS_CONFIG_EXTRA` is unchanged and the value only appears in the environment

### Requirement: cf-backup sees only Cloudflare settings
The cf-backup container SHALL get only `CF_*` settings, SHALL NOT have the node `.env` mounted and SHALL NOT install
packages when it starts.

#### Scenario: Tunnel project
- **WHEN** `wpops tunnels ensure` writes `docker-compose.wpops-tunnels.json`
- **THEN** cf-backup has an image built from `docker/cf-backup`, `CF_*` variables only, no `.env` volume, no
  capabilities and a read-only root filesystem
