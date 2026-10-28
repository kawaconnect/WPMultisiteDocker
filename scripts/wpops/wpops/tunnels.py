"""Per-account Cloudflare tunnels for this node (idle until a failover uses them).

`wpops tunnels ensure` does, for every CF_ACCOUNT_n in .env:
  - verify the token, get-or-create tunnel "wp-docker-<account>" (no DNS, no ingress
    changes for non-standby accounts: an idle tunnel cannot affect traffic)
  - record the tunnel id in config/inventory.json under this node
  - generate a standalone docker-compose.wpops-tunnels.json connector project
    joined to every configured ingress network
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .cf import CfError, Cloudflare
from .config import INVENTORY_FILE, REPO_DIR, cf_accounts, load_env, load_inventory, node_config
from . import state


def ensure(start: bool = True, log=print) -> dict[str, Any]:
    env = load_env()
    cfg = node_config()
    inv = load_inventory()
    node = cfg["node"]
    origin = inv.origin(node)
    results: dict[str, Any] = {}
    tokens: list[tuple[str, str]] = []
    changed = False
    for acct in cf_accounts(env).values():
        cf = Cloudflare(acct.token, acct.account_id, acct.name)
        from .config import _SLUG_RE, ConfigError
        if not _SLUG_RE.fullmatch(acct.name) or not _SLUG_RE.fullmatch(node):
            raise ConfigError("node and Cloudflare account names must be URL slugs")
        name = f"wp-docker-{node}-{acct.name}"
        try:
            # /user/tokens/verify needs the "User API Tokens: Read" permission, which
            # most zone/tunnel tokens do not carry; a failure here is not a verdict.
            try:
                v = cf.verify_token()
                if v.get("status") not in ("active", None):
                    raise CfError(f"token status {v.get('status')}")
            except CfError as exc:
                if "401" not in str(exc) and "403" not in str(exc):
                    raise
            tid = origin.tunnels.get(acct.name)
            t = cf.tunnel(tid) if tid else cf.tunnel_by_name(name)
            if not t:
                t = cf.create_tunnel(name)
                log(f"{acct.name}: created tunnel {name} ({t['id']})")
            else:
                log(f"{acct.name}: tunnel exists ({t['id']})")
            tid = t["id"]
            tokens.append((acct.name, cf.tunnel_token(tid)))
            if origin.tunnels.get(acct.name) != tid:
                origin.tunnels[acct.name] = tid
                changed = True
            results[acct.name] = {"tunnel": tid, "ok": True}
        except CfError as exc:
            log(f"{acct.name}: FAILED {exc}")
            results[acct.name] = {"ok": False, "error": str(exc)}
    if changed:
        raw = inv.raw
        raw["origins"][node]["tunnels"] = origin.tunnels
        tmp = INVENTORY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, INVENTORY_FILE)
        log(f"inventory updated: {INVENTORY_FILE} (commit it)")
    # A standalone connector project can serve any mixture of stacks, including
    # a legacy main stack. No base WordPress project needs to be running.
    networks = {"edge": {"external": True, "name": "wpops-edge"}}
    for st in origin.stacks.values():
        if st.get("ingress_network") == "wpops-edge":
            continue
        from .stacks import compose_project
        net = st.get("ingress_network") or f"{compose_project(st)}_frontend"
        if net not in [n["name"] for n in networks.values()]:
            networks[f"stack{len(networks)}"] = {"external": True, "name": net}
    services = {}
    for name, token in tokens:
        services[f"cloudflared-{name}"] = {
            "image": "cloudflare/cloudflared:2026.10.0", "restart": "unless-stopped",
            "networks": list(networks), "command": ["tunnel", "--no-autoupdate", "run"],
            "environment": {"TUNNEL_TOKEN": token},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            "deploy": {"resources": {"limits": {"memory": "128M"}}},
        }
    services["cf-backup"] = {
        "image": "alpine:3.23", "restart": "unless-stopped", "networks": ["edge"],
        "volumes": [f"{REPO_DIR}/scripts:/app/scripts:ro", f"{REPO_DIR}/config:/app/config:ro",
                    f"{REPO_DIR}/data/cloudflare/backups:/app/data/cloudflare/backups",
                    f"{REPO_DIR}/.env:/app/.env:ro"],
        "entrypoint": ["/bin/sh", "-c"],
        "command": ["apk add --no-cache bash curl jq openssl && "
                    "ln -sf /bin/true /usr/local/bin/docker && "
                    "while true; do bash /app/scripts/cf-backup.sh; sleep 21600; done"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    compose = REPO_DIR / "docker-compose.wpops-tunnels.json"
    state.write_private_text(compose, json.dumps({"services": services, "networks": networks}, indent=2)+"\n")
    log(f"wrote {compose} ({len(tokens)} connector(s))")
    if start and tokens:
        from .provision import ensure_edge
        ensure_edge()
        child_env = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')}
        r = subprocess.run(["docker", "compose", "-p", f"wpops-{node}-tunnels", "-f", str(compose),
                            "up", "-d", "--quiet-pull"], cwd=REPO_DIR, env=child_env,
                           capture_output=True, text=True, timeout=600)
        log((r.stdout + r.stderr).strip()[-800:])
        results["_compose"] = r.returncode
    state.audit("local", "tunnels.ensure", {k: v for k, v in results.items()})
    return results


def status() -> list[dict[str, Any]]:
    env = load_env()
    inv = load_inventory()
    node = node_config()["node"]
    origin = inv.origin(node)
    out = []
    for acct in cf_accounts(env).values():
        tid = origin.tunnels.get(acct.name)
        entry: dict[str, Any] = {"account": acct.name, "tunnel": tid}
        if tid:
            try:
                t = Cloudflare(acct.token, acct.account_id, acct.name).tunnel(tid)
                entry["status"] = t.get("status")
                entry["connections"] = len(t.get("connections") or [])
            except CfError as exc:
                entry["error"] = str(exc)
        out.append(entry)
    return out
