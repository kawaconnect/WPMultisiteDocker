# Tasks

- [x] 1. Redis password via environment and stdin config; healthcheck with REDISCLI_AUTH (stack + root compose)
- [x] 2. getenv() for every `.env` value used in WORDPRESS_CONFIG_EXTRA (root compose, instance template)
- [x] 3. cf-backup image from docker/cf-backup, CF_* environment only, no capabilities, read-only root
- [x] 4. cf-common.sh: settings from the environment when no .env exists; cf-backup needs only jq and curl
- [x] 5. Tests: compose renders no secret on a command line or in PHP source; cf-backup gets no .env; env-only cf scripts
- [x] 6. CHANGELOG, version
- [ ] 7. Roll out: standby node first, then production, one container at a time
