# Add the admin gate (single sign-on in front of WordPress admin and login)

## Why

Operators put WordPress admin and login behind an identity provider so that only staff signed in there reach
`/wp-admin/` and `/wp-login.php`. Until now that was done from outside the product, by copying an Nginx file into
each stack's `nginx/` folder. Anything that created or rebuilt a stack produced it without the gate, and a
separate job had to notice and repair it. The product owns the stack's Nginx configuration, so it should own
this file too.

## What changes

- New node setting `admin_gate` in `config/wpops.json`: the Authentik server whose outpost answers forward
  auth, and whether stacks are gated by default.
- New per-stack inventory flag `admin_gate` (true/false) that overrides the node default, so an exemption lives
  in the node's own inventory.
- wpops writes `stacks/<name>/nginx/admin-gate.conf` (+ `admin-gate-php.inc`) for every managed stack that is
  gated and removes them for a stack that is not. It does so when a stack is created or initialised, when the
  host aliases are rewritten (site added, routes published, failover, restore) and from the agent's loop, so a
  gate that was lost comes back by itself.
- New commands: `wpops gate status`, `wpops gate apply`, `wpops gate hosts`, `wpops stack gate <name> on|off|default`.
- A changed file is tested with `nginx -t` in the stack's container before the reload; a failed test puts the
  previous files back.
- Files of the earlier external tool (`kc-authentik-gate.conf`, `kc-gate-php.inc`) are removed when wpops
  writes its own, so the two never run together.

## Out of scope

- Creating the applications/providers in the identity provider. Forward auth needs one per gated host name;
  `wpops gate hosts` lists them so the operator's own tooling can create them. wpops holds no identity-provider
  credentials.
- WordPress accounts. After the gate the normal WordPress login still follows.

## Impact

- Default is off (`admin_gate.enabled: false`): nodes that do not configure it behave as before.
- Fail closed: a gated host name without a provider in the identity provider answers 500 on admin/login until
  the provider exists; the public site is not affected.
