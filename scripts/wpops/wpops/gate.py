"""Admin gate: WordPress admin and login only after an Authentik outpost accepts the request.

A gated stack gets nginx/admin-gate.conf (+ admin-gate-php.inc), which the stack's Nginx already includes
(/etc/nginx/wpops/*.conf). Whether a stack is gated: its inventory flag `admin_gate` when present, else the
node's `admin_gate.default`; nothing is gated while `admin_gate.enabled` is false or no `auth_url` is set.

Forward auth needs one provider per gated host name in Authentik. wpops holds no Authentik credentials:
`wpops gate hosts` lists the names for the operator's tooling. A gated name without a provider answers 500 on
admin/login (fail closed); the public site is not affected.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import state
from .config import ConfigError, Inventory, Origin, _HOST_RE, load_inventory, node_config
from .stacks import compose, env_flag, ps, stack_path

CONF = "admin-gate.conf"
PHP_INC = "admin-gate-php.inc"
# Files of the external tool this feature replaces; removed when wpops writes or removes its own.
LEGACY = ("kc-authentik-gate.conf", "kc-gate-php.inc")
OUTPOST = "/outpost.goauthentik.io"


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    g = cfg.get("admin_gate") or {}
    url = str(g.get("auth_url") or "").strip().rstrip("/")
    host = url[len("https://"):] if url.startswith("https://") else ""
    if url and not _HOST_RE.fullmatch(host):
        raise ConfigError("admin_gate.auth_url must be https://<host name> (the Authentik server), without a path")
    return {"enabled": bool(g.get("enabled")) and bool(url), "auth_url": url, "auth_host": host,
            "default": bool(g.get("default", True))}


def wanted(origin: Origin, name: str, cfg: dict[str, Any]) -> bool:
    g = settings(cfg)
    st = origin.stacks[name]
    if not g["enabled"] or not st.get("managed"):
        return False
    flag = st.get("admin_gate")
    return g["default"] if flag is None else bool(flag)


def stack_hosts(inv: Inventory, node: str, name: str) -> dict[str, Any]:
    """Host names of one stack. `gated`: names that need their own provider (each site's own host name and the
    name this node publishes it under). `redirect`: other domains -> the site's own host name, where a gated
    path is sent instead. `canonical`: where a gated path on any unknown name goes."""
    origin = inv.origin(node)
    gated: list[str] = []
    redirect: dict[str, str] = {}
    canonical = ""

    def add(host: str) -> None:
        host = (host or "").lower().rstrip(".")
        if host and host not in gated:
            gated.append(host)

    for site in inv.sites:
        h = site.host_on(node)
        if not h or h.get("stack") != name:
            continue
        own = site.site_host_on(node)
        if not own:
            continue
        canonical = canonical or own
        add(own)
        add(str(h.get("host") or ""))
        for d in site.domains:
            redirect[d.lower().rstrip(".")] = own
    if not gated:
        # No site mapped yet (between `stack create` and `site add`): the stack's own domain.
        domain = env_flag(stack_path(origin, name), "WP_DOMAIN_CURRENT_SITE").lower()
        if domain:
            canonical = domain
            add(domain)
            add(origin.default_host(domain))
    return {"canonical": canonical, "gated": gated,
            "redirect": {d: t for d, t in sorted(redirect.items()) if d not in gated}}


def login_slugs(path: Path) -> list[str]:
    """Custom login paths of the stack (wpops stack login); they are gated like /wp-login.php."""
    try:
        policy = json.loads((path / "policy" / "login-paths.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not policy.get("enabled"):
        return []
    slugs = {s for s in [policy.get("default", ""), *(policy.get("blogs") or {}).values()] if s}
    return sorted(s for s in slugs if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", s))


PHP_INC_TEXT = """# Managed by wpops (admin gate). PHP handling for gated admin files; mirrors the stack's PHP location.
try_files $uri =404;
fastcgi_split_path_info ^(.+\\.php)(/.+)$;
fastcgi_pass php-fpm;
fastcgi_index index.php;
include fastcgi_params;
fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
fastcgi_param PATH_INFO $fastcgi_path_info;
fastcgi_param HTTP_HOST $host;
fastcgi_param SERVER_NAME $host;
fastcgi_param HTTPS $fastcgi_https if_not_empty;
fastcgi_buffers 16 16k;
fastcgi_buffer_size 32k;
fastcgi_read_timeout 300;
fastcgi_send_timeout 300;
fastcgi_connect_timeout 10;
fastcgi_intercept_errors on;
"""

_FASTCGI_FIXED = """    include fastcgi_params;
    fastcgi_pass php-fpm;
    fastcgi_param SCRIPT_FILENAME $document_root/%(script)s;
    fastcgi_param SCRIPT_NAME /%(script)s;
    fastcgi_param HTTP_HOST $host;
    fastcgi_param SERVER_NAME $host;
    fastcgi_param HTTPS $fastcgi_https if_not_empty;
    fastcgi_buffers 16 16k;
    fastcgi_buffer_size 32k;
    fastcgi_read_timeout 300;
    fastcgi_send_timeout 300;
    fastcgi_connect_timeout 10;
    fastcgi_intercept_errors on;
"""


def render(hosts: dict[str, Any], g: dict[str, Any], slugs: list[str] | None = None) -> str:
    """The gate's Nginx configuration (server context) for one stack."""
    for h in [hosts["canonical"], *hosts["gated"], *hosts["redirect"], *hosts["redirect"].values(), g["auth_host"]]:
        if not _HOST_RE.fullmatch(h):
            raise ConfigError(f"admin gate: invalid host name {h!r}")
    out = [
        "# Managed by wpops (admin gate). Do not edit: `wpops gate apply` rewrites this file.",
        "# WordPress admin and login are served only after the Authentik outpost accepts the request.",
        "# Host names with their own provider: " + ", ".join(hosts["gated"]),
        "",
        "set $wpops_gate_known 0;",
        f"set $wpops_gate_canonical {hosts['canonical']};",
    ]
    out += [f"if ($host = {h}) {{ set $wpops_gate_known 1; }}" for h in hosts["gated"]]
    out += [f"if ($host = {d}) {{ set $wpops_gate_canonical {t}; }}" for d, t in hosts["redirect"].items()
            if t != hosts["canonical"]]
    out += [
        "",
        "# wp-login.php (also of a subdirectory blog): the stack owns `location = /wp-login.php`, so gated",
        "# requests are handed to a named location. action=postpass (password-protected posts) stays public.",
        "set $wpops_gate_login 0;",
        'if ($uri ~ "^(?:/[_0-9a-zA-Z-]+)?/wp-login\\.php$") { set $wpops_gate_login 1; }',
        "if ($arg_action = postpass) { set $wpops_gate_login 0; }",
    ]
    if slugs:
        pattern = "|".join(re.escape(s) for s in slugs)
        out += ["# Custom login paths (wpops stack login).",
                f'if ($uri ~ "^(?:/[_0-9a-zA-Z-]+)?/(?:{pattern})/?$") {{ set $wpops_gate_login 2; }}']
    out += [
        "if ($wpops_gate_login = 1) { return 418; }",
        "if ($wpops_gate_login = 2) { return 419; }",
        "error_page 418 = @wpops_gate_login;",
        "error_page 419 = @wpops_gate_login_path;",
        "# The hand-off above is itself an error_page jump; this lets the sign-in redirect chain from it.",
        "recursive_error_pages on;",
        "",
    ]
    for loc, script in (("@wpops_gate_login", "wp-login.php"), ("@wpops_gate_login_path", "index.php")):
        out += [
            f"location {loc} {{",
            "    if ($wpops_gate_known = 0) { return 302 https://$wpops_gate_canonical$request_uri; }",
            "    limit_req zone=wp_login burst=10 nodelay;",
            f"    auth_request {OUTPOST}/auth/nginx;",
            "    error_page 401 = @wpops_gate_signin;",
            (_FASTCGI_FIXED % {"script": script}).rstrip("\n"),
            "}",
            "",
        ]
    out += [
        "# Front-end AJAX and form posts stay public (WordPress serves visitors through them).",
        f"location = /wp-admin/admin-ajax.php {{ include /etc/nginx/wpops/{PHP_INC}; }}",
        f"location = /wp-admin/admin-post.php {{ include /etc/nginx/wpops/{PHP_INC}; }}",
        "",
        "location ^~ /wp-admin/ {",
        "    if ($wpops_gate_known = 0) { return 302 https://$wpops_gate_canonical$request_uri; }",
        f"    auth_request {OUTPOST}/auth/nginx;",
        "    error_page 401 = @wpops_gate_signin;",
        "    try_files $uri $uri/ /wp-admin/index.php?$args;",
        f"    location ~ \\.php$ {{ include /etc/nginx/wpops/{PHP_INC}; }}",
        "}",
        "",
        "# Outpost endpoints for this host (auth check, sign-in start, callback, sign-out).",
        f"location {OUTPOST} {{",
        "    resolver 127.0.0.11 valid=60s ipv6=off;",
        f"    set $wpops_gate_auth {g['auth_url']};",
        "    proxy_pass $wpops_gate_auth;",
        "    proxy_ssl_server_name on;",
        f"    proxy_ssl_name {g['auth_host']};",
        "    proxy_http_version 1.1;",
        f"    proxy_set_header Host {g['auth_host']};",
        "    proxy_set_header X-Forwarded-Host $host;",
        "    proxy_set_header X-Forwarded-Proto https;",
        "    proxy_set_header X-Original-URL https://$host$request_uri;",
        '    proxy_set_header Connection "";',
        "    proxy_pass_request_body off;",
        '    proxy_set_header Content-Length "";',
        "    proxy_buffer_size 32k;",
        "    proxy_buffers 8 32k;",
        "    proxy_connect_timeout 5s;",
        "    proxy_read_timeout 15s;",
        "}",
        "",
        "location @wpops_gate_signin {",
        "    internal;",
        f"    return 302 https://$host{OUTPOST}/start?rd=https://$host$request_uri;",
        "}",
        "",
    ]
    return "\n".join(out)


def desired_files(inv: Inventory, node: str, name: str, cfg: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    """({file name: text} the stack's nginx/ folder should hold for the gate, its hosts). Empty = not gated."""
    origin = inv.origin(node)
    if not wanted(origin, name, cfg):
        return {}, {"canonical": "", "gated": [], "redirect": {}}
    hosts = stack_hosts(inv, node, name)
    if not hosts["gated"]:
        raise ConfigError(f"stack {name}: admin gate is on but no host name is known (map a site or set its domain)")
    path = stack_path(origin, name)
    return {CONF: render(hosts, settings(cfg), login_slugs(path)), PHP_INC: PHP_INC_TEXT}, hosts


def _owned(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Files wpops manages in a stack's nginx/ folder. The earlier hand-installed gate is taken over only on a
    node that has switched the admin gate on: an upgrade alone must never remove a gate somebody installed."""
    return (CONF, PHP_INC, *LEGACY) if settings(cfg)["enabled"] else (CONF, PHP_INC)


def _current(nginx: Path, owned: tuple[str, ...]) -> dict[str, str]:
    out = {}
    for f in owned:
        try:
            out[f] = (nginx / f).read_text(encoding="utf-8")
        except OSError:
            pass
    return out


def _write(nginx: Path, files: dict[str, str], owned: tuple[str, ...]) -> None:
    nginx.mkdir(exist_ok=True)
    for f in owned:
        if f not in files:
            (nginx / f).unlink(missing_ok=True)
    # The PHP include before the file that includes it, so a reload in between never sees a dangling include.
    for f in sorted(files, key=lambda x: x.endswith(".conf")):
        tmp = nginx / (f + ".tmp")
        tmp.write_text(files[f], encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(nginx / f)


def _nginx_running(origin: Origin, name: str) -> bool:
    return any(c.get("service") == "nginx" and c.get("state") == "running" for c in ps(origin, name))


def status(inv: Inventory | None = None, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Per stack, without changing anything: gated or not, and whether its files are current."""
    inv, cfg = inv or load_inventory(), cfg or node_config()
    node = cfg["node"]
    origin = inv.origin(node)
    rows = []
    for name in sorted(origin.stacks):
        row: dict[str, Any] = {"stack": name, "gated": False, "state": "off", "canonical": "", "hosts": []}
        try:
            files, hosts = desired_files(inv, node, name, cfg)
            current = _current(stack_path(origin, name) / "nginx", _owned(cfg))
            row.update(gated=bool(files), canonical=hosts["canonical"], hosts=hosts["gated"])
            if files:
                row["state"] = ("ok" if current == files else "missing" if CONF not in current else "outdated")
            elif current:
                row["state"] = "to-remove"
        except ConfigError as exc:
            row.update(state="error", error=str(exc))
        rows.append(row)
    return rows


def apply(inv: Inventory | None = None, cfg: dict[str, Any] | None = None, only: list[str] | None = None,
          log=lambda _m: None) -> dict[str, Any]:
    """Bring the gate files of this node's stacks in line with the configuration. A stack whose files change
    gets `nginx -t` and a reload when its Nginx runs; a failed test puts the previous files back."""
    inv, cfg = inv or load_inventory(), cfg or node_config()
    node = cfg["node"]
    origin = inv.origin(node)
    names = sorted(origin.stacks) if only is None else list(only)
    result: dict[str, Any] = {"changed": [], "failed": {}, "gated": 0}
    for name in names:
        if name not in origin.stacks:
            raise ConfigError(f"unknown stack {name}")
        if not origin.stacks[name].get("managed"):
            continue
        try:
            files, _hosts = desired_files(inv, node, name, cfg)
            nginx = stack_path(origin, name) / "nginx"
            if not nginx.parent.is_dir():
                continue
            result["gated"] += 1 if files else 0
            with state.locked(f"gate-{name}"):
                owned = _owned(cfg)
                before = _current(nginx, owned)
                if before == files:
                    continue
                _write(nginx, files, owned)
                if _nginx_running(origin, name):
                    t = compose(origin, name, "exec", "-T", "nginx", "nginx", "-t", check=False, timeout=60)
                    if t.returncode:
                        _write(nginx, before, owned)
                        raise ConfigError("nginx -t failed, previous files restored: "
                                          + (t.stderr or t.stdout or "").strip()[-300:])
                    r = compose(origin, name, "exec", "-T", "nginx", "nginx", "-s", "reload", check=False, timeout=60)
                    if r.returncode:
                        raise ConfigError("nginx reload failed: " + (r.stderr or r.stdout or "").strip()[-300:])
            result["changed"].append(name)
            log(f"admin gate {'written' if files else 'removed'}: {name}")
            state.audit("local", "gate.apply", {"stack": name, "gated": bool(files)})
        except Exception as exc:  # noqa: BLE001 - one stack must not stop the others
            result["failed"][name] = str(exc)
            log(f"admin gate failed for {name}: {exc}")
    return result


def apply_quietly(only: list[str] | None = None, log=lambda _m: None) -> dict[str, Any] | None:
    """For hooks inside other operations (stack create, alias rewrite, restore): never raises."""
    try:
        return apply(only=only, log=log)
    except Exception as exc:  # noqa: BLE001
        log(f"admin gate not applied: {exc}")
        return {"changed": [], "failed": {"*": str(exc)}, "gated": 0}


def hosts(inv: Inventory | None = None, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Gated stacks with the host names that each need a forward-auth provider in Authentik."""
    return [{"stack": r["stack"], "canonical": r["canonical"], "hosts": r["hosts"]}
            for r in status(inv, cfg) if r["gated"]]


def set_stack(name: str, value: bool | None) -> dict[str, Any]:
    """Set (true/false) or clear (None = follow the node default) a stack's `admin_gate` flag, then apply."""
    from .config import INVENTORY_FILE, save_inventory
    with state.locked("provision"):
        inv, cfg = load_inventory(), node_config()
        node = cfg["node"]
        if name not in inv.origin(node).stacks:
            raise ConfigError(f"unknown stack {name}")
        raw = json.loads(json.dumps(inv.raw))
        st = raw["origins"][node]["stacks"][name]
        if value is None:
            st.pop("admin_gate", None)
        else:
            st["admin_gate"] = bool(value)
        if raw != inv.raw:
            save_inventory(raw, INVENTORY_FILE)
            state.audit("local", "gate.set", {"stack": name, "admin_gate": value})
    r = apply(only=[name])
    if name in r["failed"]:
        raise ConfigError(f"stack {name}: {r['failed'][name]}")
    row = next(x for x in status() if x["stack"] == name)
    return {"stack": name, "admin_gate": value, "gated": row["gated"], "state": row["state"], "hosts": row["hosts"]}
