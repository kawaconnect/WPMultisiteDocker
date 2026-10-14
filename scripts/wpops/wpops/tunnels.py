"""Per-account Cloudflare tunnels for this node (idle until a failover uses them).

`wpops tunnels ensure` does, for every CF_ACCOUNT_n in .env:
  - verify the token, get-or-create tunnel "wp-docker-<account>" (no DNS, no ingress
    changes for non-standby accounts: an idle tunnel cannot affect traffic)
  - record the tunnel id in config/inventory.json under this node
  - regenerate docker-compose.tunnels.yml (same layout as cf-setup.sh) and start
    the connectors with `docker compose --profile tunnels up -d`
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
    stack_path = Path(origin.stacks.get("main", {}).get("path") or REPO_DIR)
    results: dict[str, Any] = {}
    tokens: list[tuple[str, str]] = []
    changed = False
    for acct in cf_accounts(env).values():
        cf = Cloudflare(acct.token, acct.account_id, acct.name)
        name = f"wp-docker-{acct.name}"
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
            t = cf.tunnel_by_name(name)
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
    project = stack_path.name.lower()
    project = "".join(ch for ch in project if ch.isalnum() or ch in "_-")
    lines = ["services:"]
    for name, token in tokens:
        lines += [
            f"  cloudflared-{name}:",
            "    image: cloudflare/cloudflared:latest",
            "    restart: unless-stopped",
            "    profiles: [tunnels]",
            "    networks: [frontend]",
            "    command: tunnel --no-autoupdate run",
            "    environment:",
            f'      TUNNEL_TOKEN: "{token}"',
            "    deploy:",
            "      resources:",
            "        limits:",
            "          memory: 128M",
        ]
    lines += ["networks:", "  frontend:", "    external: true", f"    name: {project}_frontend", ""]
    compose = stack_path / "docker-compose.tunnels.yml"
    compose.write_text("\n".join(lines), encoding="utf-8")
    compose.chmod(0o600)
    log(f"wrote {compose} ({len(tokens)} connector(s))")
    if start and tokens:
        r = subprocess.run(["docker", "compose", "--profile", "tunnels", "up", "-d", "--quiet-pull"], cwd=stack_path,
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
