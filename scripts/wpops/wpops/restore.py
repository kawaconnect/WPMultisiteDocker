"""Apply a pulled backup set to a stack on this node (explicit, never automatic).

Flow: register the set in the stack's SiteGuard (`wp siteguardbackup import`,
which copies the files into wp-content/sgbackup), then `wp siteguardbackup
restore` with options derived from the stack mode and the site selection:

  multisite, all sites   -> --subsite-mode=mirror --db-restore-mode=migrate
                            (every blog remapped to this stack's domains)
  multisite, some sites  -> --subsite-mode=batch --batch-subsite-map=[...]
                            source blog ids come from the set's site list,
                            targets from the inventory (hosts.<node>.blog_id)
  single-site stack      -> --db-restore-mode=migrate (whole site)

A pre-restore backup is always taken by SiteGuard (--pre-restore-backup).
The stack must mount this repo's backups/ at /backups (read-only) for the
CLI container; `wpops setup` adds that.
"""

from __future__ import annotations

from pathlib import Path

import json
import re
import time
from typing import Any

from . import backup as bk
from . import notify, state
from . import disk
from .config import REPO_DIR, Inventory, node_config
from .jobs import Job
from .stacks import StackError, require_free, stack_path, wp

ENTITIES = ("db", "plugins", "themes", "uploads", "others", "wpcore")


class RestoreError(Exception):
    pass


def _parse_import_timestamp(output: str) -> int | None:
    m = re.search(r"Registered backup set: \S+ \((\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC\)", output)
    if not m:
        return None
    return int(time.mktime(time.strptime(m.group(1) + " UTC", "%Y-%m-%d %H:%M %Z")) - time.timezone)


def _timestamp_from_history(origin, stack: str, nonce: str) -> int | None:
    r = wp(origin, stack, "siteguardbackup", "list", "--format=json", timeout=300)
    try:
        for item in json.loads(r.stdout or "[]"):
            if item.get("nonce") == nonce:
                return int(time.mktime(time.strptime(item["date"] + " UTC", "%Y-%m-%d %H:%M %Z")) - time.timezone)
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    return None


def build_batch_map(inv: Inventory, man: dict[str, Any], node: str, sites: list[str]) -> list[dict[str, Any]]:
    src_sites = man.get("sites") or []
    by_domain = {str(s.get("domain", "")).lower(): s for s in src_sites}
    out = []
    for slug in sites:
        site = inv.site(slug)
        src_host = (site.host_on(man["source"]) or {}).get("host", "").lower()
        dst = site.host_on(node) or {}
        if not src_host or src_host not in by_domain:
            raise RestoreError(f"site '{slug}': source host '{src_host}' is not in the backup's site list")
        if not dst.get("blog_id"):
            raise RestoreError(f"site '{slug}': no blog_id on node '{node}'")
        out.append({
            "source_blog_id": int(by_domain[src_host]["blog_id"]),
            "source_domain": src_host,
            "action": "overwrite",
            "target_blog_id": int(dst["blog_id"]),
        })
    return out


def apply(job: Job, inv: Inventory, node: str, set_ref: str, stack: str, sites: list[str] | None = None,
          all_sites: bool = False, entities: list[str] | None = None, cleanup: bool | None = None) -> dict[str, Any]:
    log = job.log
    origin = inv.origin(node)
    if stack not in origin.stacks:
        raise RestoreError(f"stack '{stack}' is not defined on node '{node}'")
    mode = origin.stacks[stack].get("mode", "multisite")
    set_dir, man = bk.manifest(set_ref)
    if not man.get("pulled_at"):
        raise RestoreError(f"{set_ref} is not completely pulled")
    missing = [f["name"] for f in man["files"] if not (set_dir / f["name"]).exists()]
    if missing:
        raise RestoreError(f"{set_ref} is missing files: {', '.join(missing[:5])}")
    set_bytes = sum(int(f.get("size", 0)) for f in man["files"])
    stack_path = Path(origin.stacks[stack].get("path") or REPO_DIR)
    try:
        disk.require(stack_path if stack_path.exists() else REPO_DIR, set_bytes * 2, f"restore of {set_ref} ({set_bytes // 2**20} MB set, extracted twice)")
    except disk.DiskError as exc:
        raise RestoreError(str(exc)) from None
    if entities:
        bad = [e for e in entities if e not in ENTITIES]
        if bad:
            raise RestoreError(f"unknown entities: {', '.join(bad)}")
    if mode == "multisite" and not all_sites and not sites:
        raise RestoreError("multisite stack: pass --all or --sites a,b")
    if mode != "multisite" and sites:
        raise RestoreError("single-site stack: use --all (there are no subsites)")
    cfg = node_config()
    cleanup = cfg.get("restore", {}).get("cleanup_imported", True) if cleanup is None else cleanup
    total = sum(int(f["size"]) for f in man["files"])
    require_free(stack_path(origin, stack), int(total * 2.2), "restore (import copy + extraction)")

    with state.locked(f"restore-{stack}"):
        job.set_progress(phase="import", set=set_ref, stack=stack)
        container_dir = f"/backups/{man['source']}/{set_dir.name}/"
        log(f"importing {set_ref} into stack '{stack}' ({mode})")
        r = wp(origin, stack, "siteguardbackup", "import", container_dir, timeout=3600)
        log(r.stdout.strip())
        if r.returncode != 0:
            raise RestoreError("import failed: " + (r.stderr.strip() or r.stdout.strip())[-400:])
        ts = _parse_import_timestamp(r.stdout) or _timestamp_from_history(origin, stack, man["nonce"])
        if not ts:
            raise RestoreError("could not determine the imported set's timestamp")
        args = ["siteguardbackup", "restore", str(ts), "--yes", "--pre-restore-backup", "--db-restore-mode=migrate"]
        if entities:
            args.append("--entities=" + ",".join(entities))
        if mode == "multisite":
            if all_sites:
                args.append("--subsite-mode=mirror")
            else:
                bmap = build_batch_map(inv, man, node, sites or [])
                args += ["--subsite-mode=batch", "--batch-subsite-map=" + json.dumps(bmap)]
                log("batch map: " + json.dumps(bmap))
        job.set_progress(phase="restore", timestamp=ts)
        log("restore: wp " + " ".join(a if not a.startswith("--batch-subsite-map") else "--batch-subsite-map=…" for a in args))
        r = wp(origin, stack, *args, timeout=4 * 3600)
        tail = (r.stdout or "").strip().splitlines()[-40:]
        for line in tail:
            log(line)
        ok = r.returncode == 0 and "Restore complete" in (r.stdout or "")
        if not ok:
            raise RestoreError("restore failed: " + ((r.stderr or r.stdout or "").strip())[-400:])
        # reload PHP-FPM so opcache/mu-plugin changes from the restore take effect
        try:
            from .stacks import compose
            compose(origin, stack, "kill", "-s", "USR2", "wordpress", check=False, timeout=60)
        except Exception:  # noqa: BLE001
            pass
        if cleanup:
            r2 = wp(origin, stack, "siteguardbackup", "delete", str(ts), "--yes", timeout=600)
            log("cleanup imported copy: " + ("ok" if r2.returncode == 0 else (r2.stderr or r2.stdout).strip()[-200:]))
        man.setdefault("applied", []).append({
            "at": int(time.time()), "node": node, "stack": stack, "sites": sites or ("all" if all_sites else []),
            "entities": entities or "all", "actor": job.actor, "job": job.id,
        })
        bk.save_manifest(set_dir, man)
    result = {"set": set_ref, "stack": stack, "sites": sites or "all", "timestamp": ts}
    state.audit(job.actor, "restore.apply", result)
    notify.notify("restore.applied", f"[wpops] backup applied on {node}/{stack}: {set_ref}",
                  f"Set {set_ref} was restored onto stack '{stack}' on node {node} "
                  f"({'all sites' if all_sites or not sites else ', '.join(sites)}). Job {job.id}.\n", result)
    return result
