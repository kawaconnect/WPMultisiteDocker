# Changelog

Versions are git tags (`vX.Y.Z`). This repo has no build; tags mark reviewed states of the stack and scripts.

## [0.0.1] - 2026-10-14

### Fixed
- `scripts/cf-rollback.sh`: two top-level `local` assignments aborted the script under `set -e` in the error path that restores the original CNAME, which could leave a domain without a DNS record.
