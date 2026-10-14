"""Node status (cheap by default; `live=True` also queries Cloudflare DNS)."""

from __future__ import annotations

import time
from typing import Any

from . import __version__, backup as bk, failover, jobs, state
from .config import BACKUPS_DIR, Inventory, REPO_DIR, load_env, cf_accounts
from . import disk
from .stacks import disk_free, ps


def tunnel_status(inv: Inventory, node: str) -> list[dict[str, Any]]:
    """Which Cloudflare accounts this node can serve: tunnel configured and a connector running."""
    origin = inv.origin(node)
    accounts = cf_accounts(load_env())
    out = []
    for acct in accounts.values():
        tid = origin.tunnels.get(acct.name)
        out.append({"account": acct.name, "tunnel": tid or None, "ready": bool(tid)})
    return out


def node_status(inv: Inventory, cfg: dict[str, Any], live: bool = False) -> dict[str, Any]:
    node = cfg["node"]
    origin = inv.origin(node)
    stacks = {}
    for name in origin.stacks:
        try:
            containers = ps(origin, name)
        except Exception as exc:  # noqa: BLE001
            containers = [{"error": str(exc)}]
        healthy = all((c.get("state") == "running") and (c.get("health") in ("", "healthy", None)) for c in containers if "error" not in c)
        stacks[name] = {"mode": origin.stacks[name].get("mode", "multisite"), "containers": containers, "healthy": healthy and bool(containers)}
    ovs = state.overrides()
    sets = bk.list_sets()
    latest = {}
    for s in sets:
        if s["complete"] and (s["source"] not in latest or s["set"] > latest[s["source"]]["set"]):
            latest[s["source"]] = s
    out: dict[str, Any] = {
        "node": node, "version": __version__, "time": int(time.time()), "repo": str(REPO_DIR),
        "stacks": stacks,
        "overrides": ovs,
        "override_count": len([o for o in ovs.values() if o.get("status") == "active"]),
        "tunnels": tunnel_status(inv, node),
        "backups": {"sets": len(sets), "latest": latest, "schedule": bk.schedule_info(),
                    "disk": disk_free(BACKUPS_DIR if BACKUPS_DIR.exists() else REPO_DIR)},
        "disk": disk.report(inv, node),
        "jobs": {"active": jobs.active(), "recent": jobs.list_jobs(5)},
        "sites": len(inv.sites),
    }
    if live:
        out["dns"] = failover.live_status(inv)
    return out
