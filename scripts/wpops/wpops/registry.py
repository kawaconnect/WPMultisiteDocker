"""Registry of every WordPress deployment on this node, one entry per site.

A site is a single-site stack, or one blog of a multisite stack. Every stack
of this node (inventory `origins.<node>.stacks`) is included, and every live
blog of a multisite stack, whether or not the inventory maps it to a logical
site. Entries are identified by `uid` = `<node>:<stack>:<blog id>` (1 for a
single site) and carry a numeric `site_id` that is unique on this node and
never reused.

State (under state/):
  registry.json            membership, revision, numeric ids
  registry-changes.jsonl   change feed: {rev, ts, op: added|removed|changed, uid, site}
  registry-facts.json      per stack, from WP-CLI and disk: versions, plugins, blogs, sizes
  registry-health.json     per site: last health result (SiteGuard or direct check)
  siteguard-keys.json      per stack: the key the agent reads SiteGuard's health API with

The agent's scheduler refreshes health every minute and facts every
`registry.facts_interval_min`; stack and site commands refresh the membership
at once. Membership changes bump `revision`, append to the change feed and
record `registry.site_added|removed|changed` events (and post them to the
webhook), so clients can add and remove sites without polling everything.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import __version__, notify, siteguard as sg, state
from .config import ConfigError, Inventory, Origin, load_env, load_inventory, node_config

MEMBER_FIELDS = ("node", "stack", "mode", "blog_id", "slug", "name", "url", "host", "domains", "primary",
                 "failover_to", "cf_account", "mapped", "managed")
CHANGES_KEEP = 1000
_facts_lock = threading.Lock()


def _file(name: str) -> Path:
    return state.STATE_DIR / name


def _read(name: str, default: Any) -> Any:
    try:
        return state.read_json(_file(name), default)
    except (OSError, ValueError):
        return default


def _cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    return (cfg or node_config()).get("registry") or {}


# ------------------------------------------------------------------ facts

FACTS_PHP = r"""
$o = array('wp' => get_bloginfo('version'), 'php' => PHP_VERSION, 'multisite' => is_multisite(),
  'home' => home_url('/'), 'name' => get_bloginfo('name'),
  'siteguard' => defined('SGBACKUP_VERSION') ? SGBACKUP_VERSION : null, 'plugins' => array(), 'blogs' => array());
if (!function_exists('get_plugins')) { require_once ABSPATH . 'wp-admin/includes/plugin.php'; }
$active = (array) get_option('active_plugins', array());
$network = is_multisite() ? array_keys((array) get_site_option('active_sitewide_plugins', array())) : array();
foreach (get_plugins() as $file => $p) {
  $o['plugins'][] = array('file' => $file, 'name' => $p['Name'], 'version' => $p['Version'],
    'active' => in_array($file, $active, true) || in_array($file, $network, true),
    'network' => in_array($file, $network, true));
}
$o['last_backup'] = 0; $o['backup_count'] = 0;
if (function_exists('sgbackup_load_classes')) {
  sgbackup_load_classes();
  if (class_exists('SiteGuard_Backup_History')) {
    $h = SiteGuard_Backup_History::get_history();
    $o['backup_count'] = is_array($h) ? count($h) : 0;
    $l = SiteGuard_Backup_History::get_latest();
    if ($l && isset($l['timestamp'])) { $o['last_backup'] = (int) $l['timestamp']; }
  }
}
if (is_multisite()) {
  foreach (get_sites(array('number' => 0)) as $s) {
    $o['blogs'][] = array('blog_id' => (int) $s->blog_id, 'domain' => $s->domain, 'path' => $s->path,
      'url' => get_home_url((int) $s->blog_id, '/'), 'name' => get_blog_option((int) $s->blog_id, 'blogname'),
      'archived' => (int) $s->archived, 'deleted' => (int) $s->deleted, 'spam' => (int) $s->spam);
  }
}
echo "\nWPOPS_FACTS:" . json_encode($o) . "\n";
"""


def collect_facts(origin: Origin, stack: str) -> dict[str, Any]:
    """One WP-CLI call inside the stack. {'error': ...} when WordPress does not answer."""
    from .stacks import wp
    try:
        r = wp(origin, stack, "eval", FACTS_PHP, timeout=300)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[-300:]}
    for line in (r.stdout or "").splitlines():
        if line.startswith("WPOPS_FACTS:"):
            try:
                data = json.loads(line[len("WPOPS_FACTS:"):])
            except json.JSONDecodeError:
                break
            return data if isinstance(data, dict) else {"error": "unexpected WP-CLI output"}
    return {"error": ((r.stderr or r.stdout or "").strip() or f"WP-CLI exit {r.returncode}")[-300:]}


def tree_size(path: Path, skip: Path | None = None) -> int:
    """Allocated bytes below path (hard links once), without following symlinks."""
    total, seen = 0, set()
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        if skip is not None and Path(dirpath) == skip.parent and skip.name in dirnames:
            dirnames.remove(skip.name)
        for name in filenames + dirnames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return total


def disk_usage(origin: Origin, stack: str, blogs: list[int]) -> dict[str, Any]:
    """Stack data size (database, files, cache) and uploads per blog."""
    data = Path(origin.stacks[stack]["path"]) / "data"
    out: dict[str, Any] = {"stack_bytes": tree_size(data) if data.exists() else 0, "uploads": {},
                           "measured_at": int(time.time())}
    uploads = data / "wordpress" / "wp-content" / "uploads"
    if uploads.exists():
        sites_dir = uploads / "sites"
        out["uploads"]["1"] = tree_size(uploads, skip=sites_dir)
        for bid in blogs:
            if bid > 1 and (sites_dir / str(bid)).exists():
                out["uploads"][str(bid)] = tree_size(sites_dir / str(bid))
    return out


def facts_due(cfg: dict[str, Any] | None = None, now: float | None = None) -> list[str]:
    """Stacks whose facts are missing, stale or invalidated."""
    inv, ncfg = load_inventory(), (cfg or node_config())
    interval = max(5, int(_cfg(ncfg).get("facts_interval_min", 360))) * 60
    now = now or time.time()
    facts = _read("registry-facts.json", {})
    stacks = inv.origin(ncfg["node"]).stacks
    return [n for n in stacks if now - int((facts.get(n) or {}).get("checked_at") or 0) >= interval]


def refresh_facts(stacks: list[str] | None = None, log=lambda _m: None) -> dict[str, Any]:
    """Collect facts (and enrol SiteGuard keys) for the given stacks, or all."""
    if not _facts_lock.acquire(blocking=False):
        return {"skipped": "a facts refresh is already running"}
    try:
        inv, cfg = load_inventory(), node_config()
        node = cfg["node"]
        origin = inv.origin(node)
        names = [s for s in (stacks or list(origin.stacks)) if s in origin.stacks]
        results: dict[str, Any] = {}
        for name in names:
            log(f"{name}: collecting facts")
            f = collect_facts(origin, name)
            with state.locked("registry-facts", timeout=60):
                facts = _read("registry-facts.json", {})
                prev = facts.get(name) or {}
                now = int(time.time())
                if "error" in f:
                    entry = {**prev, "error": f["error"], "error_at": now, "checked_at": now}
                else:
                    blogs = [b["blog_id"] for b in f.get("blogs") or []]
                    entry = {**f, "checked_at": now, "ok_at": now,
                             "disk": disk_usage(origin, name, blogs)}
                facts[name] = entry
                for gone in [k for k in facts if k not in origin.stacks]:
                    facts.pop(gone)
                state.write_json(_file("registry-facts.json"), facts)
            results[name] = {"ok": "error" not in f, **({"error": f["error"]} if "error" in f else {})}
            if "error" not in f:
                results[name]["enrol"] = _maybe_enroll(origin, name, entry, cfg, log)
        # New blogs and fresh keys: check health now, not on the next minute.
        try:
            refresh_health(log=log)
        except Exception:  # noqa: BLE001
            refresh(log=log)
        return results
    finally:
        _facts_lock.release()


def _enrol_wanted(name: str, facts: dict[str, Any], cfg: dict[str, Any], now: float | None = None) -> bool:
    """SiteGuard is active here and the agent has no working key: enrol, but at
    most every 30 minutes after a rejected key (SiteGuard's failed-auth limiter)
    and once per facts interval when enrolment is not possible (old SiteGuard
    with its own key)."""
    if _cfg(cfg).get("siteguard_enroll", "auto") != "auto" or not facts.get("siteguard"):
        return False
    rec = sg.load_keys().get(name)
    kind = ((_read("registry-health.json", {}).get("_stacks") or {}).get(name) or {}).get("sg_kind")
    if rec and kind != "auth":
        return False
    last = int((_read("registry-enrol.json", {}).get(name) or {}).get("at") or 0)
    wait = 1800 if rec else max(5, int(_cfg(cfg).get("facts_interval_min", 360))) * 60
    return (now or time.time()) - last >= wait


def enrolment_due(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or node_config()
    facts = _read("registry-facts.json", {})
    return [n for n, f in facts.items() if _enrol_wanted(n, f or {}, cfg)]


def _maybe_enroll(origin: Origin, name: str, facts: dict[str, Any], cfg: dict[str, Any], log) -> Any:
    if not _enrol_wanted(name, facts, cfg):
        return None
    host = _stack_host(origin, name, facts)
    r = sg.enroll(origin, name, facts.get("siteguard"), host, log=log)
    with state.locked("registry-enrol", timeout=30):
        attempts = _read("registry-enrol.json", {})
        attempts[name] = {"at": int(time.time()), "enrolled": bool(r.get("enrolled")), "reason": r.get("reason", "")}
        state.write_json(_file("registry-enrol.json"), attempts)
    if r.get("enrolled"):
        notify.event("siteguard.enrolled", {"stack": name, "key_kind": r["kind"], "fp": r["fp"]})
    else:
        log(f"{name}: SiteGuard key not enrolled: {r.get('reason')}")
    return r


def invalidate(stack: str) -> None:
    """Facts are stale (e.g. after a restore): collect them on the next tick."""
    with state.locked("registry-facts", timeout=60):
        facts = _read("registry-facts.json", {})
        if stack in facts:
            facts[stack]["checked_at"] = 0
            state.write_json(_file("registry-facts.json"), facts)


# -------------------------------------------------------------- membership

def _stack_host(origin: Origin, name: str, facts: dict[str, Any] | None = None) -> str:
    home = (facts or {}).get("home") or ""
    host = urllib.parse.urlsplit(home).hostname if home else ""
    if host:
        return host
    env = load_env(Path(origin.stacks[name]["path"]) / ".env")
    return env.get("WP_DOMAIN_CURRENT_SITE") or "localhost"


def _active_blog(b: dict[str, Any]) -> bool:
    return not (b.get("deleted") or b.get("spam") or b.get("archived"))


def build_members(inv: Inventory, node: str, facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    origin = inv.origin(node)
    mapped: dict[tuple[str, int], tuple[Any, dict[str, Any]]] = {}
    for s in inv.sites:
        h = s.host_on(node)
        if h and h.get("stack"):
            mapped[(h["stack"], int(h.get("blog_id") or 1))] = (s, h)
    out: dict[str, dict[str, Any]] = {}
    for name, st in origin.stacks.items():
        mode = st.get("mode", "multisite")
        f = facts.get(name) or {}
        main_host = _stack_host(origin, name, f)
        blogs: dict[int, dict[str, Any]] = {}
        if mode == "single":
            blogs[1] = {"url": f.get("home") or f"https://{main_host}/", "name": f.get("name") or ""}
        else:
            for b in f.get("blogs") or []:
                if _active_blog(b):
                    blogs[int(b["blog_id"])] = {"url": b.get("url") or f"https://{b['domain']}{b.get('path') or '/'}",
                                                "name": b.get("name") or ""}
            if not blogs:
                blogs[1] = {"url": f.get("home") or f"https://{main_host}/", "name": f.get("name") or ""}
        for (mstack, bid) in mapped:
            if mstack == name and bid not in blogs:
                blogs[bid] = {"url": "", "name": "", "missing": bool(f.get("blogs"))}
        for bid, b in sorted(blogs.items()):
            site, h = mapped.get((name, bid), (None, None))
            url = b["url"] or (f"https://{h['host']}/" if h else "")
            host = (h or {}).get("host") or urllib.parse.urlsplit(url).hostname or main_host
            uid = f"{node}:{name}:{bid}"
            out[uid] = {
                "uid": uid, "node": node, "stack": name, "mode": mode, "blog_id": bid,
                "slug": site.slug if site else None,
                "name": (site.name if site and site.name != site.slug else "") or b["name"] or (site.slug if site else host),
                "url": url, "host": host,
                "domains": list(site.domains) if site else [host],
                "primary": site.primary if site else node,
                "failover_to": site.failover_to if site else "",
                "cf_account": site.cf_account if site else "",
                "mapped": site is not None, "managed": bool(st.get("managed")),
                **({"missing": True} if b.get("missing") else {}),
            }
    return out


def _member_view(e: dict[str, Any]) -> dict[str, Any]:
    return {k: e.get(k) for k in MEMBER_FIELDS}


def refresh(inv: Inventory | None = None, node: str | None = None, log=lambda _m: None) -> dict[str, Any]:
    """Rebuild membership from the inventory and facts; record changes. Cheap (no network)."""
    inv = inv or load_inventory()
    node = node or node_config()["node"]
    facts = _read("registry-facts.json", {})
    members = build_members(inv, node, facts)
    events: list[tuple[str, dict[str, Any]]] = []
    with state.locked("registry", timeout=60):
        reg = _read("registry.json", {}) or {}
        prev: dict[str, Any] = reg.get("sites") or {}
        ids: dict[str, int] = reg.get("ids") or {}
        rev = int(reg.get("revision") or 0)
        next_id = max(ids.values(), default=0) + 1
        changes = []
        for uid, m in members.items():
            if uid not in ids:
                ids[uid] = next_id
                next_id += 1
            m["site_id"] = ids[uid]
            if uid not in prev:
                changes.append(("added", uid, m))
            elif _member_view(prev[uid]) != _member_view(m):
                changes.append(("changed", uid, m))
        for uid, m in prev.items():
            if uid not in members:
                changes.append(("removed", uid, m))
        if changes or not reg:
            now = int(time.time())
            for op, uid, m in changes:
                rev += 1
                rec = {"rev": rev, "ts": now, "op": op, "uid": uid, "site": m}
                _append_change(rec)
                events.append((f"registry.site_{op}", {"uid": uid, "rev": rev, "site_id": m.get("site_id"),
                                                       "slug": m.get("slug"), "stack": m.get("stack"),
                                                       "url": m.get("url"), "node": node}))
                log(f"registry: {op} {uid} ({m.get('url')})")
            reg = {"node": node, "revision": rev, "updated": now, "sites": members, "ids": ids}
            state.write_json(_file("registry.json"), reg)
    for kind, data in events:
        notify.event(kind, data)
    return {"revision": rev, "changes": len(events)}


def _append_change(rec: dict[str, Any]) -> None:
    path = _file("registry-changes.jsonl")
    state.ensure_dirs()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) > 2 * CHANGES_KEEP:
        state.write_private_text(path, "\n".join(lines[-CHANGES_KEEP:]) + "\n")


def changes(since: int = 0, limit: int = 500) -> dict[str, Any]:
    """Change feed after revision `since`. reset=True: the feed no longer reaches
    back that far; fetch the whole registry instead."""
    reg = _read("registry.json", {}) or {}
    rev = int(reg.get("revision") or 0)
    path = _file("registry-changes.jsonl")
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    oldest = rows[0]["rev"] if rows else rev + 1
    out = [r for r in rows if r["rev"] > since][:max(1, limit)]
    return {"node": reg.get("node"), "revision": rev, "since": since,
            "reset": since < oldest - 1 and since < rev, "changes": out}


# ------------------------------------------------------------------ health

def _sg_site(payload: dict[str, Any], blog_id: int, single: bool) -> dict[str, Any] | None:
    sites = [s for s in payload.get("sites") or [] if isinstance(s, dict)]
    if single:
        return sites[0] if sites else None
    for s in sites:
        if int(s.get("site_id") or 0) == blog_id:
            return s
    return None


def refresh_health(log=lambda _m: None) -> dict[str, Any]:
    """One health pass over every site: SiteGuard when the stack has a key, else a direct check."""
    inv, cfg = load_inventory(), node_config()
    node = cfg["node"]
    origin = inv.origin(node)
    rcfg = _cfg(cfg)
    timeout = float(rcfg.get("health_timeout", 15))
    stale_after = int(rcfg.get("stale_after_min", 30)) * 60
    refresh(inv, node, log)
    reg = _read("registry.json", {}) or {}
    members: dict[str, Any] = reg.get("sites") or {}
    keys = sg.load_keys()
    prev = _read("registry-health.json", {}) or {}
    prev_stacks: dict[str, Any] = prev.get("_stacks") or {}
    now = int(time.time())
    stacks: dict[str, Any] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for name in origin.stacks:
        port = sg.loopback_port(origin, name)
        info: dict[str, Any] = {"port": port, "sg": None, "sg_kind": "", "sg_error": "",
                                "forced_at": int((prev_stacks.get(name) or {}).get("forced_at") or 0)}
        key = (keys.get(name) or {}).get("key")
        main = next((m for m in members.values() if m["stack"] == name and m["blog_id"] == 1), None)
        host = urllib.parse.urlsplit((main or {}).get("url") or "").hostname or _stack_host(origin, name)
        if port and key:
            r = sg.fetch_health(port, host, key, timeout)
            if r["ok"]:
                payload = r["payload"]
                age = now - int(payload.get("checked_at") or 0)
                if age > stale_after and now - info["forced_at"] > 600:
                    info["forced_at"] = now
                    fresh = sg.force_check(port, host, key, timeout=max(60.0, timeout))
                    if fresh:
                        payload, age = fresh, now - int(fresh.get("checked_at") or 0)
                if age <= 4 * stale_after:
                    payloads[name] = payload
                    info["sg"] = payload.get("version")
                    info["checked_at"] = int(payload.get("checked_at") or 0)
                    info["environment"] = payload.get("environment")
                else:
                    info["sg_kind"], info["sg_error"] = "stale", f"SiteGuard last checked {age // 60} min ago"
            else:
                info["sg_kind"], info["sg_error"] = r["kind"], r["error"]
        elif not key:
            info["sg_kind"] = "no-key"
        stacks[name] = info
    # Direct checks run in parallel: one hung stack must not hold up the others.
    direct: dict[str, Any] = {}
    todo = {uid: m for uid, m in members.items()
            if not m.get("missing") and stacks[m["stack"]]["port"]
            and (payloads.get(m["stack"]) is None
                 or _sg_site(payloads[m["stack"]], m["blog_id"], m["mode"] == "single") is None)}
    if todo:
        from concurrent.futures import ThreadPoolExecutor

        def _check(m: dict[str, Any]) -> dict[str, Any]:
            u = urllib.parse.urlsplit(m["url"] or f"https://{m['host']}/")
            return sg.direct_check(stacks[m["stack"]]["port"], u.hostname or m["host"], u.path or "/", timeout)
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            for uid, res in zip(todo, pool.map(_check, todo.values())):
                direct[uid] = res
    sites: dict[str, Any] = {}
    for uid, m in members.items():
        p = prev.get(uid) or {}
        payload = payloads.get(m["stack"])
        row: dict[str, Any] | None = None
        if payload is not None:
            s = _sg_site(payload, m["blog_id"], m["mode"] == "single")
            if s is not None:
                row = {k: s.get(k) for k in ("status", "http_code", "response_time", "last_check", "error",
                                             "ssl_ok", "ssl_error", "ssl_stale", "ssl_expiry_days", "ssl_expires_at",
                                             "ssl_issuer", "ssl_last_success", "ssl_checked_at", "check_method",
                                             "consecutive_failures")}
                row["source"] = "siteguard"
        if row is None:
            d = direct.get(uid)
            if d is None:
                row = {"status": "unknown", "error": "blog not found in the stack" if m.get("missing")
                       else "no loopback port for this stack", "http_code": 0, "response_time": 0}
            else:
                fails = 0 if d["up"] else int(p.get("consecutive_failures") or 0) + 1
                row = {"status": "operational" if d["up"] else "down", "http_code": d["http_code"],
                       "response_time": d["response_time"], "error": d["error"], "consecutive_failures": fails,
                       "check_method": "wpops-direct"}
            row.update({"source": "direct", "last_check": now})
            # Certificate data from SiteGuard's last answer, if there was one (marked stale).
            for k in ("ssl_expiry_days", "ssl_expires_at", "ssl_issuer", "ssl_last_success"):
                if k in p:
                    row[k] = p[k]
            if "ssl_expiry_days" in row:
                row["ssl_stale"] = True
        row["since"] = p.get("since", now) if p.get("status") == row["status"] else now
        sites[uid] = row
    out = {"updated": now, "_stacks": stacks, **sites}
    with state.locked("registry-health", timeout=60):
        state.write_json(_file("registry-health.json"), out)
    return {"sites": len(sites), "siteguard_stacks": len(payloads)}


# ------------------------------------------------------------------- views

def _latest_sets(node: str, inv: Inventory) -> dict[str, dict[str, Any]]:
    """Per stack: latest pulled set from a source on this stack, latest set restored onto it."""
    from . import backup as bk
    src_stack = {n: s.stack for n, s in inv.sources().items() if s.origin == node and s.stack}
    out: dict[str, dict[str, Any]] = {}
    try:
        sets = bk.list_sets()
    except Exception:  # noqa: BLE001
        return out
    for s in sets:
        if not s.get("complete"):
            continue
        st = src_stack.get(s["source"])
        if st:
            cur = out.setdefault(st, {}).get("pulled")
            if not cur or (s.get("pulled_at") or 0) > (cur.get("pulled_at") or 0):
                out[st]["pulled"] = {"set": s["id"], "pulled_at": s.get("pulled_at"), "size": s.get("size")}
        for a in s.get("applied") or []:
            if a.get("node") == node and a.get("stack"):
                cur = out.setdefault(a["stack"], {}).get("restored")
                if not cur or (a.get("at") or 0) > (cur.get("at") or 0):
                    out[a["stack"]]["restored"] = {"set": s["id"], "at": a.get("at"), "sites": a.get("sites")}
    return out


def snapshot(live: bool = False) -> dict[str, Any]:
    """The full registry: membership plus health, versions, backups, disk, tunnel/DNS."""
    from . import disk
    from .ops import tunnel_status
    inv, cfg = load_inventory(), node_config()
    node = cfg["node"]
    origin = inv.origin(node)
    reg = _read("registry.json", {}) or {}
    if not reg.get("sites") and origin.stacks:
        refresh(inv, node)
        reg = _read("registry.json", {}) or {}
    facts = _read("registry-facts.json", {})
    health = _read("registry-health.json", {})
    stacks_h = health.get("_stacks") or {}
    keys = sg.load_keys()
    ovs = state.overrides()
    tunnels = {t["account"]: t for t in tunnel_status(inv, node)}
    sets = _latest_sets(node, inv)
    try:
        fs = {r["label"]: r for r in disk.report(inv, node)}
    except Exception:  # noqa: BLE001
        fs = {}
    dns: dict[str, list[dict[str, Any]]] = {}
    if live:
        from . import failover
        for row in failover.live_status(inv):
            dns.setdefault(row["site"], []).append(row)
    stack_rows = []
    for name, st in origin.stacks.items():
        f = facts.get(name) or {}
        h = stacks_h.get(name) or {}
        env_ = h.get("environment") or {}
        k = keys.get(name) or {}
        stack_rows.append({
            "name": name, "mode": st.get("mode", "multisite"), "managed": bool(st.get("managed")),
            "path": st.get("path"), "port": h.get("port"),
            "versions": {"wordpress": env_.get("wp_version") or f.get("wp"), "php": env_.get("php_version") or f.get("php"),
                         "siteguard": h.get("sg") or f.get("siteguard"), "checked_at": f.get("ok_at")},
            "plugins": f.get("plugins") or [],
            "siteguard": {"key": k.get("fp"), "key_kind": k.get("kind"), "status": h.get("sg_kind") or ("ok" if h.get("sg") else ""),
                          "error": h.get("sg_error", ""), "checked_at": h.get("checked_at")},
            "backup": {"siteguard_last": (env_.get("last_backup") or f.get("last_backup") or 0),
                       "siteguard_sets": env_.get("backup_count", f.get("backup_count", 0)), **sets.get(name, {})},
            "disk": {**(f.get("disk") or {}), "filesystem": fs.get(f"stack {name}")},
            "facts_error": f.get("error", "") if f.get("error_at", 0) >= f.get("ok_at", 0) else "",
            "sites": sorted(uid for uid, m in (reg.get("sites") or {}).items() if m["stack"] == name),
        })
    by_stack = {s["name"]: s for s in stack_rows}
    sites = []
    for uid, m in sorted((reg.get("sites") or {}).items(), key=lambda kv: kv[1].get("site_id", 0)):
        st = by_stack.get(m["stack"]) or {}
        h = health.get(uid) or {"status": "unknown", "source": "none"}
        fdisk = (facts.get(m["stack"]) or {}).get("disk") or {}
        acct = m.get("cf_account") or ""
        sites.append({
            **m, "id": f"{m['stack']}:{m['blog_id']}",
            "health": h,
            "versions": {k: v for k, v in (st.get("versions") or {}).items()},
            "backup": st.get("backup") or {},
            "disk": {"stack_bytes": fdisk.get("stack_bytes"), "uploads_bytes": (fdisk.get("uploads") or {}).get(str(m["blog_id"])),
                     "measured_at": fdisk.get("measured_at"), "filesystem": (st.get("disk") or {}).get("filesystem")},
            "tunnel": {"account": acct or None, **({"tunnel": tunnels[acct]["tunnel"], "ready": tunnels[acct]["ready"]}
                                                    if acct in tunnels else {"tunnel": None, "ready": False})},
            "overrides": {d: ovs[d].get("status") for d in m.get("domains") or [] if d in ovs},
            **({"dns": dns.get(m["slug"], [])} if live else {}),
        })
    return {"node": node, "agent_version": __version__, "revision": int(reg.get("revision") or 0),
            "updated": reg.get("updated"), "health_updated": health.get("updated"),
            "stacks": stack_rows, "sites": sites}


def site(uid_or_slug: str) -> dict[str, Any] | None:
    for s in snapshot()["sites"]:
        if uid_or_slug in (s["uid"], s["id"], s.get("slug")):
            return s
    return None


def siteguard_payload(public_url: str = "") -> dict[str, Any]:
    """Every site of this node in SiteGuard's health format (api_version 2), so a
    client that reads one SiteGuard multisite can read a whole node the same way.
    Each site also carries uid, node, stack, blog_id, slug and check_source."""
    snap = snapshot()
    warn = 14
    summary = {"total": 0, "operational": 0, "degraded": 0, "down": 0, "unknown": 0, "ssl_ok": 0,
               "ssl_failing": 0, "ssl_stale": 0, "ssl_expiring_soon": 0, "ssl_expired": 0}
    alerts, rows, last = [], [], 0
    for s in snap["sites"]:
        h = s["health"]
        status = h.get("status") or "unknown"
        url = s["url"] or f"https://{s['host']}/"
        row = {
            "site_id": s["site_id"], "url": url, "domain": urllib.parse.urlsplit(url).hostname or s["host"],
            "status": status, "http_code": int(h.get("http_code") or 0), "response_time": float(h.get("response_time") or 0),
            "ssl_expiry_days": int(h["ssl_expiry_days"]) if h.get("ssl_expiry_days") is not None else -1,
            "ssl_expires_at": h.get("ssl_expires_at") or 0, "ssl_issuer": h.get("ssl_issuer") or "",
            "ssl_ok": bool(h.get("ssl_ok")), "ssl_error": h.get("ssl_error") or "", "ssl_stale": bool(h.get("ssl_stale")),
            "ssl_last_success": int(h.get("ssl_last_success") or 0), "ssl_checked_at": int(h.get("ssl_checked_at") or 0),
            "last_check": int(h.get("last_check") or 0), "error": h.get("error") or "",
            "check_method": h.get("check_method") or "", "consecutive_failures": int(h.get("consecutive_failures") or 0),
            "uid": s["uid"], "node": s["node"], "stack": s["stack"], "blog_id": s["blog_id"], "slug": s["slug"],
            "name": s["name"], "mode": s["mode"], "check_source": h.get("source") or "none",
            "versions": s["versions"], "last_backup": (s["backup"] or {}).get("siteguard_last") or 0,
        }
        rows.append(row)
        last = max(last, row["last_check"])
        summary["total"] += 1
        summary[status if status in summary else "unknown"] += 1
        if status == "down":
            alerts.append({"severity": "critical", "type": "site_down", "domain": row["domain"], "site_id": row["site_id"],
                           "message": f"{row['domain']} is DOWN: {row['error'] or 'site unreachable'}"})
        if url.startswith("https://") and row["ssl_checked_at"]:
            if row["ssl_ok"]:
                summary["ssl_ok"] += 1
            if row["ssl_stale"]:
                summary["ssl_stale"] += 1
            if row["ssl_error"] and not row["ssl_ok"]:
                summary["ssl_failing"] += 1
                alerts.append({"severity": "warning", "type": "ssl_probe_failed", "domain": row["domain"],
                               "site_id": row["site_id"], "message": row["ssl_error"],
                               "ssl_last_success": row["ssl_last_success"]})
            elif row["ssl_ok"] and not row["ssl_stale"] and 0 <= row["ssl_expiry_days"] <= warn:
                summary["ssl_expiring_soon"] += 1
                alerts.append({"severity": "critical" if row["ssl_expiry_days"] <= 3 else "warning",
                               "type": "ssl_expiring", "domain": row["domain"], "site_id": row["site_id"],
                               "ssl_expiry_days": row["ssl_expiry_days"],
                               "message": f"{row['domain']} SSL certificate expires in {row['ssl_expiry_days']} days"})
    overall = "operational"
    if summary["down"]:
        overall = "down" if summary["down"] == summary["total"] else "degraded"
    elif summary["degraded"]:
        overall = "degraded"
    elif not summary["total"]:
        overall = "unknown"
    return {
        "status": overall, "site_url": public_url or f"wpops://{snap['node']}", "node": snap["node"],
        "checked_at": int(snap.get("health_updated") or last or 0), "ssl_checked_at": 0,
        "check_interval": 60, "check_interval_label": "wpops", "ssl_warn_days": warn,
        "summary": summary, "alerts": alerts, "sites": rows,
        "version": f"wpops {__version__}", "api_version": 2, "source": "wpops",
        "registry_revision": snap["revision"],
    }


# ------------------------------------------------------------ inventory edits

def _require_no_override(domains: list[str]) -> None:
    ovs = state.overrides()
    busy = [d for d in domains if (ovs.get(d) or {}).get("status") == "active"]
    if busy:
        raise ConfigError(f"active failover override on {', '.join(busy)}; roll it back first")


def site_add(slug: str, stack: str, host: str = "", domains: list[str] | None = None, blog_id: int | None = None,
             cf_account: str = "", primary: str = "", name: str = "", failover_to: str = "") -> dict[str, Any]:
    """Map a site of this node in the inventory (new site, or this node's host of an existing one)."""
    from .config import INVENTORY_FILE, save_inventory
    with state.locked("provision"):
        inv, cfg = load_inventory(), node_config()
        node = cfg["node"]
        origin = inv.origin(node)
        if stack not in origin.stacks:
            raise ConfigError(f"unknown stack '{stack}'")
        mode = origin.stacks[stack].get("mode", "multisite")
        if mode == "multisite" and not blog_id:
            raise ConfigError("a multisite stack needs --blog-id")
        if mode == "single" and blog_id not in (None, 1):
            raise ConfigError("a single-site stack has no blog id")
        facts = _read("registry-facts.json", {}).get(stack) or {}
        if not host:
            if mode == "single":
                host = _stack_host(origin, stack, facts)
            else:
                b = next((b for b in facts.get("blogs") or [] if int(b["blog_id"]) == blog_id), None)
                if not b:
                    raise ConfigError(f"blog {blog_id} not known in stack {stack}; pass --host")
                host = b["domain"]
        host = host.lower()
        raw = json.loads(json.dumps(inv.raw))
        entry = {"stack": stack, "host": host, **({"blog_id": blog_id} if mode == "multisite" else {})}
        existing = next((s for s in raw.setdefault("sites", []) if s.get("slug") == slug), None)
        if existing:
            existing.setdefault("hosts", {})[node] = entry
            if domains:
                existing["domains"] = domains
            for k, v in (("name", name), ("cf_account", cf_account), ("primary", primary), ("failover_to", failover_to)):
                if v:
                    existing[k] = v
            created = False
        else:
            if not cf_account:
                raise ConfigError("a new site needs --cf-account (the Cloudflare account of its zones)")
            raw["sites"].append({"slug": slug, "name": name or slug, "primary": primary or node,
                                 "cf_account": cf_account, "domains": domains or [host],
                                 "hosts": {node: entry}, **({"failover_to": failover_to} if failover_to else {})})
            created = True
        save_inventory(raw, INVENTORY_FILE)
        state.audit("local", "registry.site_add", {"slug": slug, "stack": stack, "host": host, "blog_id": blog_id})
    r = refresh()
    return {"slug": slug, "created": created, "host": entry, "revision": r["revision"]}


def site_remove(slug: str, everywhere: bool = False) -> dict[str, Any]:
    """Unmap a site from this node (or drop it from the inventory entirely)."""
    from .config import INVENTORY_FILE, save_inventory
    with state.locked("provision"):
        inv, cfg = load_inventory(), node_config()
        node = cfg["node"]
        site_ = inv.site(slug)
        _require_no_override(site_.domains)
        raw = json.loads(json.dumps(inv.raw))
        entry = next(s for s in raw["sites"] if s.get("slug") == slug)
        if node not in (entry.get("hosts") or {}) and not everywhere:
            raise ConfigError(f"site '{slug}' is not hosted on {node}")
        (entry.get("hosts") or {}).pop(node, None)
        dropped = everywhere or not entry.get("hosts")
        if dropped:
            raw["sites"] = [s for s in raw["sites"] if s.get("slug") != slug]
        elif entry.get("primary") == node or entry.get("failover_to") == node:
            raise ConfigError(f"site '{slug}' names {node} as primary/failover_to; change that first or remove it everywhere")
        save_inventory(raw, INVENTORY_FILE)
        state.audit("local", "registry.site_remove", {"slug": slug, "dropped": dropped})
    r = refresh()
    return {"slug": slug, "dropped_from_inventory": dropped, "revision": r["revision"]}
