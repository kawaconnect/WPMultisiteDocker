# Admin gate: single sign-on in front of WordPress admin and login

With the admin gate on, a stack's Nginx serves `/wp-admin/` and `/wp-login.php` (and a custom login path) only
after an [Authentik](https://goauthentik.io) outpost accepts the request. A visitor without a session is sent to
the Authentik sign-in for that host name and comes back to the page they asked for. The normal WordPress login
still follows: the gate decides who may reach the login page, WordPress decides who may log in.

The gate is off unless a node configures it.

## Configure a node

`config/wpops.json`:

```json
"admin_gate": {
  "enabled": true,
  "auth_url": "https://auth.example.net",
  "default": true
}
```

| Key | Meaning |
|---|---|
| `enabled` | `false`: no stack on this node is gated, and existing gate files are removed at the next apply |
| `auth_url` | `https://<host>` of the Authentik server whose embedded outpost answers forward auth. No path |
| `default` | Managed stacks without their own flag are gated (`true`) or not (`false`) |
| `check_interval_min` | How often the agent puts back a missing or outdated gate file (default 10) |

One stack can differ from the node default. The flag lives in that node's inventory entry for the stack
(`origins.<node>.stacks.<stack>.admin_gate`):

```bash
wpops stack gate shop off       # this stack is never gated on this node
wpops stack gate shop on        # gated even when the node default is false
wpops stack gate shop default   # follow the node default again
```

Only managed stacks (created with `wpops stack create`) are gated.

## What wpops does by itself

wpops writes `stacks/<name>/nginx/admin-gate.conf` and `admin-gate-php.inc`; the stack's Nginx already includes
that folder. It rewrites them whenever they are missing or no longer match the configuration:

- when a stack is created (before its first start) and initialised
- when host aliases are rewritten: a site is added or changed, routes are published, a failover or rollback runs
- after a restore, and when a custom login path is set
- from the agent, every `check_interval_min` minutes (event `gate.repaired`, or `gate.failed`)

A changed file is tested with `nginx -t` inside the stack's container before Nginx is reloaded. When the test
fails the previous files are put back and Nginx keeps running with them. No container is restarted.

```bash
wpops gate status     # per stack: ok, missing, outdated, to-remove, off, error (exit 1 when apply has work)
wpops gate apply      # do it now; --stack <name> for one
wpops gate hosts      # host names that need a provider in Authentik (see below)
```

## Authentik side

Forward auth for a single application needs one **proxy provider in "forward auth (single application)" mode per
host name**, each with an application, all assigned to the embedded outpost. The external host of the provider is
`https://<host name>`. Who may pass is decided there (bind the application to the groups that may reach admin).

wpops holds no Authentik credentials and creates nothing there. `wpops --json gate hosts` lists what is needed:

```json
[{"stack": "shop", "canonical": "shop.example.com",
  "hosts": ["shop.example.com", "shopexamplecom-n1.standby.example.net"]}]
```

Per stack these are the site's own host name (its siteurl) and the name this node publishes it under (the
staging name). Other domains of the site, such as `www.`, need no provider: a gated path on them is redirected
to the site's own host name.

**A gated host name without a provider answers 500 on admin and login** (the outpost answers 404 for a name it
does not know). That is deliberate: the gate fails closed. The public site is not affected. Create the provider
and the next request works; no reload is needed.

## What stays public

| Path | Why |
|---|---|
| The site itself, `/wp-json/`, `/wp-cron.php` | Visitors, the REST API (plugins, backup sync), scheduled tasks |
| `/wp-admin/admin-ajax.php`, `/wp-admin/admin-post.php` | WordPress serves front-end AJAX and form posts through them |
| `POST /wp-login.php?action=postpass` | Visitors unlocking a password-protected page. Only a POST whose whole query string is `action=postpass`; it can run nothing but that handler |

The REST API with WordPress application passwords is therefore not behind the gate. `xmlrpc.php` is denied by the
stack's Nginx whether or not the gate is on.

## Faults

| Symptom | Cause |
|---|---|
| 500 on wp-admin and wp-login, site fine | No provider for that host name in Authentik, or Authentik is not reachable from the node |
| Redirect to the site's own host name instead of the sign-in | The request came in on a domain without its own provider (`www.` or a secondary domain). Expected |
| Signed in at Authentik, then "access denied" there | The person is not in a group bound to the application |
| A front-end form or AJAX call broke | It calls a `/wp-admin/` URL other than `admin-ajax.php` / `admin-post.php` |
| Authentik is down | Admin and login of every gated stack are unreachable; the sites stay up. Emergency: `"enabled": false` in `config/wpops.json`, then `wpops gate apply` |
| `wpops gate status` says `error` | The stack has no host name yet: map a site to it (`wpops site add`) |

## Moving from a hand-installed gate

Stacks that carry `kc-authentik-gate.conf` / `kc-gate-php.inc` from the earlier external tool are switched by
`wpops gate apply` once the node has `"enabled": true`: it writes its own two files and removes those in the same
step, tests, and reloads. On such a node a stack that is not gated under the configuration has them removed as
well, so set the flags of exempt stacks first (`wpops stack gate <name> off`). While `enabled` is false wpops
leaves those hand-installed files alone; upgrading wpops never removes a gate by itself.
