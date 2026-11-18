# admin-gate

## ADDED Requirements

### Requirement: Gate WordPress admin and login behind forward auth
When the admin gate is enabled for a stack, the stack's Nginx SHALL serve `/wp-admin/` and `/wp-login.php` only
after the configured Authentik outpost accepts the request, and SHALL send an unauthenticated browser to the
outpost's sign-in start for the same host.

#### Scenario: Unauthenticated admin request
- **WHEN** a browser without a session requests `/wp-admin/` or `/wp-login.php` on a gated host name
- **THEN** the answer is a 302 to `/outpost.goauthentik.io/start` on that host with the original URL as `rd`

#### Scenario: Paths that stay public
- **WHEN** any client requests the site itself, `/wp-json/`, `/wp-admin/admin-ajax.php`, `/wp-admin/admin-post.php`,
  `/wp-cron.php`, or sends a POST to `/wp-login.php` whose whole query string is `action=postpass`
- **THEN** the request is served without forward auth

#### Scenario: The password-protected-post exception cannot reach the login
- **WHEN** a request to `/wp-login.php` carries `action=postpass` together with another `action` argument, is not a
  POST, or carries an `action` field in its body
- **THEN** it is either sent to the sign-in like any other login request or handled as a postpass request only

#### Scenario: Host name without its own provider
- **WHEN** a gated path is requested on a host name of the stack that is not one of the stack's gated host names
- **THEN** the answer is a 302 to the same path on the site's own host name

### Requirement: Gate state follows configuration
Whether a stack is gated SHALL be decided by the stack's `admin_gate` flag in the inventory when present, else by
the node's `admin_gate.default`; with `admin_gate.enabled` false no stack is gated. Only managed stacks are gated.

#### Scenario: New stack on a node that gates by default
- **WHEN** `wpops stack create` creates a stack on a node with `admin_gate.enabled` and `admin_gate.default` true
- **THEN** the stack's `nginx/admin-gate.conf` exists before its containers first start

#### Scenario: Exempt stack
- **WHEN** a stack's inventory entry has `admin_gate: false`
- **THEN** wpops writes no gate file for it and removes an existing one

### Requirement: The gate repairs itself and never breaks Nginx
wpops SHALL rewrite a missing or outdated gate file when host aliases are rewritten, after a restore and
periodically from the agent, SHALL test the configuration before reloading Nginx, and SHALL restore the previous
files when the test fails.

#### Scenario: Gate file deleted
- **WHEN** a gated stack's `admin-gate.conf` is missing
- **THEN** the next `wpops gate apply` or agent pass writes it again and reloads Nginx

#### Scenario: Configuration test fails
- **WHEN** `nginx -t` fails after new gate files were written
- **THEN** the previous files are put back, Nginx is not reloaded, and the stack is reported as failed

### Requirement: List the host names that need a provider
`wpops gate hosts` SHALL print every gated host name of the node, so that the operator's tooling can create one
forward-auth provider per name.

#### Scenario: JSON output
- **WHEN** `wpops --json gate hosts` runs
- **THEN** the output lists each gated stack with its own host name and its gated host names
