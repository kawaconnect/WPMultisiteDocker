"""Disk space: pre-flight checks before pulls/restores and a background watcher.

The watcher runs from the agent scheduler every tick. When any watched path
drops under the configured reserve it records a `disk.low` event (e-mail +
webhook through notify), repeats at most once a day while it stays low, and
records `disk.ok` once space is back. The bot relays both to Discord.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import notify, state
from .config import BACKUPS_DIR, REPO_DIR, Inventory, node_config
from .stacks import disk_free

STATE_FILE = state.STATE_DIR / "disk.json"
REPEAT_SECONDS = 24 * 3600


def reserve() -> tuple[int, float]:
    """(min free bytes, min free percent) from node config `disk`."""
    cfg = node_config().get("disk") or {}
    return int(float(cfg.get("min_free_gb", 10)) * 2**30), float(cfg.get("min_free_pct", 10))


def watched_paths(inv: Inventory, node: str) -> list[tuple[str, Path]]:
    """Backups dir plus every stack's data directory (deduplicated by filesystem)."""
    out: list[tuple[str, Path]] = [("backups", BACKUPS_DIR if BACKUPS_DIR.exists() else REPO_DIR)]
    try:
        origin = inv.origin(node)
    except Exception:  # noqa: BLE001
        origin = None
    for name, st in (origin.stacks.items() if origin else []):
        p = Path(st.get("path") or REPO_DIR) / "data"
        out.append((f"stack {name}", p if p.exists() else Path(st.get("path") or REPO_DIR)))
    extra = (node_config().get("disk") or {}).get("paths") or []
    for e in extra:
        out.append((str(e), Path(str(e))))
    seen: set[int] = set()
    uniq: list[tuple[str, Path]] = []
    for label, p in out:
        try:
            dev = p.stat().st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        uniq.append((label, p))
    return uniq


def report(inv: Inventory, node: str) -> list[dict[str, Any]]:
    min_free, min_pct = reserve()
    rows = []
    for label, p in watched_paths(inv, node):
        d = disk_free(p)
        pct = (d["free"] / d["total"] * 100) if d["total"] else 0.0
        rows.append({"label": label, "path": str(p), "total": d["total"], "free": d["free"],
                     "free_pct": round(pct, 1), "low": d["free"] < min_free or pct < min_pct,
                     "min_free_gb": round(min_free / 2**30, 1), "min_free_pct": min_pct})
    return rows


def require(path: Path, needed: int, label: str) -> None:
    """Raise unless `needed` bytes fit on `path` while keeping the configured reserve."""
    min_free, min_pct = reserve()
    d = disk_free(path)
    keep = max(min_free, int(d["total"] * min_pct / 100))
    if d["free"] - needed < keep:
        raise state.StateError(
            f"not enough disk for {label}: needs {needed // 2**20} MB plus a {keep // 2**30} GB reserve, "
            f"free {d['free'] // 2**20} MB on {path}")


def watch_tick(inv: Inventory, node: str) -> list[dict[str, Any]]:
    """Called by the scheduler: alert on low disk (daily repeat) and on recovery."""
    rows = report(inv, node)
    prev = state.read_json(STATE_FILE, {}) if STATE_FILE.exists() else {}
    now = int(time.time())
    changed = False
    for r in rows:
        key = r["path"]
        was = prev.get(key) or {}
        summary = f"{r['label']} ({r['path']}): {r['free'] // 2**30} GB free of {r['total'] // 2**30} GB ({r['free_pct']}%)"
        if r["low"]:
            if not was.get("low") or now - int(was.get("alerted", 0)) >= REPEAT_SECONDS:
                notify.notify("disk.low", f"[wpops] LOW DISK on {node}: {summary}",
                              f"Node {node} is below its reserve of {r['min_free_gb']} GB / {r['min_free_pct']}% free.\n"
                              f"{summary}\n\nBackup pulls and restores are refused until space is freed "
                              f"(prune old sets: wpops backup prune, or raise backup.retain_sets).",
                              {"node": node, "label": r["label"], "path": r["path"], "free": r["free"],
                               "total": r["total"], "free_pct": r["free_pct"]})
                prev[key] = {"low": True, "alerted": now}
                changed = True
        elif was.get("low"):
            notify.notify("disk.ok", f"[wpops] disk space recovered on {node}: {summary}", summary,
                          {"node": node, "label": r["label"], "path": r["path"], "free": r["free"],
                           "total": r["total"], "free_pct": r["free_pct"]})
            prev[key] = {"low": False, "alerted": 0}
            changed = True
    if changed:
        state.write_json(STATE_FILE, prev)
    return rows
