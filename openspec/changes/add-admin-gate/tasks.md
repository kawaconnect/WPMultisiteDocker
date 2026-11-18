# Tasks

- [x] 1. Node config defaults (`admin_gate`) and validation of the per-stack `admin_gate` flag
- [x] 2. `wpops/gate.py`: wanted hosts per stack, rendering, apply with nginx test + rollback, legacy file removal
- [x] 3. Hooks: stack create/init, alias rewrite, runtime reinstall after restore, agent loop
- [x] 4. CLI: `gate status|apply|hosts`, `stack gate <name> on|off|default`
- [x] 5. Tests: host selection, rendering, apply/rollback, live Nginx check of the gated and public paths
- [x] 6. Docs (`docs/admin-gate.md`, README pointer, example config), CHANGELOG, version
- [x] 7. Roll out: standby node first, then production; switch each stack from the external file to the built-in one
