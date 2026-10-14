"""Backup sync: pull SiteGuard backup sets from a source WordPress, keep N, schedule.

Source protocol (SiteGuard Backup plugin, sgbackup/v1/sync, bearer sync key):
  POST /sync/backup        {label}              -> {nonce}
  GET  /sync/jobs/<nonce>                       -> {status, terminal, succeeded, ...}
  GET  /sync/sets                               -> {sets: [{timestamp, nonce, label, files:[{name,size,sha256}], sites, ...}]}
  GET  /sync/sets/<nonce>/file?name=<name>      -> bytes (Range supported)

Pulled sets live in backups/<source>/<set-id>/ with a manifest.json. They are
never applied automatically (restore.py does that on request).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import notify, state
from .config import BACKUPS_DIR, ConfigError, Inventory, load_env, node_config, save_node_config
from .jobs import Job
from . import disk
from .stacks import disk_free

SCHEDULE_STATE = state.STATE_DIR / "schedule.json"
_SET_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{4}-[A-Za-z0-9]{4,32}$")
_FILE_RE = re.compile(r"^backup_[A-Za-z0-9._-]+\.(zip|gz|sql|crypt|json|txt)$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _declared_total(resp, offset: int) -> int | None:
    """Total size the source declares for this file (Content-Range or Content-Length + offset)."""
    cr = resp.headers.get("Content-Range", "")
    m = re.match(r"bytes \d+-\d+/(\d+)$", cr)
    if m:
        return int(m.group(1))
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        return int(cl) + (offset if resp.status == 206 else 0)
    return None


class SyncError(Exception):
    pass


# ------------------------------------------------------------------ client

class SyncClient:
    def __init__(self, base: str, key: str, timeout: int = 60):
        self.base = base.rstrip("/") + "/wp-json/sgbackup/v1/sync"
        self.key = key
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any | None = None, headers: dict[str, str] | None = None,
             stream: bool = False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        h = {"Authorization": f"Bearer {self.key}", "Accept": "application/json", "User-Agent": "wpops/1"}
        if data is not None:
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            # Never follow redirects: the Authorization header must not be
            # re-sent to another host or scheme the source points us at.
            resp = _OPENER.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace")[:300]
            raise SyncError(f"{method} {path} -> HTTP {exc.code}: {text}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise SyncError(f"{method} {path} -> {exc}") from None
        if stream:
            return resp
        with resp:
            raw = resp.read().decode()
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise SyncError(f"{method} {path} -> non-JSON response: {raw[:120]}") from None

    def ping(self) -> dict[str, Any]:
        return self._req("GET", "/ping")

    def start_backup(self, label: str, entities: list[str] | None = None, db: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {"label": label, "db": db}
        if entities:
            body["entities"] = entities
        return self._req("POST", "/backup", body)

    def job(self, nonce: str) -> dict[str, Any]:
        return self._req("GET", f"/jobs/{urllib.parse.quote(nonce)}")

    def sets(self) -> dict[str, Any]:
        """{'sets': [...], 'sites': [...], 'mode': ..., 'site_url': ...} (sizes only, fast)."""
        return self._req("GET", "/sets")

    def delete_remote(self, nonce: str) -> dict[str, Any]:
        """Remove a backup set on the source (frees the primary's storage)."""
        return self._req("DELETE", f"/sets/{urllib.parse.quote(nonce)}")

    def hashes(self, nonce: str, log, attempts: int = 8) -> dict[str, str]:
        """sha256 per file of one set. The source hashes on demand and caches per
        file, so a big set may need several long calls before all are present."""
        saved = self.timeout
        self.timeout = 600
        try:
            for i in range(1, attempts + 1):
                try:
                    listing = self._req("GET", "/sets?hash=1")
                except SyncError as exc:
                    log(f"  hash listing attempt {i}: {exc}")
                else:
                    for s in listing.get("sets", []):
                        if str(s.get("nonce")) == nonce:
                            got = {f["name"]: f["sha256"] for f in s.get("files", []) if f.get("sha256")}
                            if got and len(got) == len(s.get("files", [])):
                                return got
                            log(f"  hash listing attempt {i}: {len(got)}/{len(s.get('files', []))} hashed so far")
                            break
                time.sleep(min(120, 15 * i))
            return {}
        finally:
            self.timeout = saved

    def download(self, nonce: str, name: str, dest: Path, expected_size: int, log) -> None:
        """Resumable download with Range; verifies the final size."""
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if dest.exists() and dest.stat().st_size == expected_size:
            return
        attempts = 0
        while True:
            attempts += 1
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                resp = self._req("GET", f"/sets/{urllib.parse.quote(nonce)}/file?name={urllib.parse.quote(name)}",
                                 headers=headers, stream=True)
            except SyncError as exc:
                if attempts >= 5:
                    raise
                log(f"  retry {attempts}: {exc}")
                time.sleep(5 * attempts)
                continue
            with resp:
                if have and resp.status != 206:
                    have = 0
                    mode = "wb"
                else:
                    mode = "ab" if have else "wb"
                # The source's headers must agree with the size it advertised in
                # the listing; a stream is never read past that size.
                declared = _declared_total(resp, have)
                if declared is not None and declared != expected_size:
                    raise SyncError(f"{name}: source now reports {declared} bytes, listing said {expected_size}")
                with open(part, mode) as fh:
                    while have < expected_size:
                        try:
                            chunk = resp.read(min(1024 * 1024, expected_size - have))
                        except (TimeoutError, ConnectionError, OSError) as exc:
                            log(f"  stream interrupted at {have} bytes: {exc}")
                            chunk = b""
                            break
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
            if have >= expected_size:
                break
            if attempts >= 20:
                raise SyncError(f"{name}: gave up after {attempts} attempts at {have}/{expected_size} bytes")
            time.sleep(3)
        if have != expected_size:
            raise SyncError(f"{name}: size mismatch {have} != {expected_size}")
        os.replace(part, dest)


# -------------------------------------------------------------------- sets

def set_dir(source: str, set_id: str) -> Path:
    if not _SET_ID_RE.match(set_id) or "/" in source or source in (".", ".."):
        raise SyncError("invalid set id or source")
    return BACKUPS_DIR / source / set_id


def list_sets(source: str | None = None) -> list[dict[str, Any]]:
    out = []
    if not BACKUPS_DIR.exists():
        return out
    for src in sorted(BACKUPS_DIR.iterdir()):
        if not src.is_dir() or (source and src.name != source):
            continue
        for d in sorted(src.iterdir(), reverse=True):
            m = d / "manifest.json"
            if not d.is_dir() or not m.exists():
                continue
            try:
                man = json.loads(m.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            size = sum(int(f.get("size", 0)) for f in man.get("files", []))
            complete = all((d / f["name"]).exists() for f in man.get("files", []))
            out.append({
                "id": f"{src.name}/{d.name}", "source": src.name, "set": d.name, "nonce": man.get("nonce"),
                "timestamp": man.get("timestamp"), "label": man.get("label", ""), "size": size,
                "files": len(man.get("files", [])), "complete": complete, "keep": bool(man.get("keep")),
                "pulled_at": man.get("pulled_at"), "sites": len(man.get("sites", [])),
                "mode": man.get("mode", ""), "applied": man.get("applied", []),
            })
    return out


def manifest(set_ref: str) -> tuple[Path, dict[str, Any]]:
    try:
        source, set_id = set_ref.split("/", 1)
    except ValueError:
        raise SyncError("set reference must be <source>/<set-id>") from None
    d = set_dir(source, set_id)
    m = d / "manifest.json"
    if not m.exists():
        raise SyncError(f"no such backup set: {set_ref}")
    return d, json.loads(m.read_text(encoding="utf-8"))


def save_manifest(d: Path, man: dict[str, Any]) -> None:
    tmp = d / "manifest.json.tmp"
    tmp.write_text(json.dumps(man, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, d / "manifest.json")
    (d / "manifest.json").chmod(0o644)


def mark_keep(set_ref: str, keep: bool) -> dict[str, Any]:
    d, man = manifest(set_ref)
    man["keep"] = keep
    save_manifest(d, man)
    return man


def delete_set(set_ref: str) -> None:
    d, _ = manifest(set_ref)
    shutil.rmtree(d)


def prune(source: str | None = None, retain: int | None = None, dry_run: bool = False, log=print) -> list[str]:
    cfg = node_config()["backup"]
    retain = retain if retain is not None else int(cfg.get("retain_sets", 4))
    removed: list[str] = []
    by_source: dict[str, list[dict[str, Any]]] = {}
    for s in list_sets(source):
        by_source.setdefault(s["source"], []).append(s)
    for src, sets in by_source.items():
        sets.sort(key=lambda s: s["set"], reverse=True)
        kept = 0
        for s in sets:
            if s["keep"]:
                continue
            if not s["complete"]:
                # incomplete and older than a day: junk
                if time.time() - (s.get("pulled_at") or 0) > 86400:
                    log(f"prune {s['id']} (incomplete)")
                    if not dry_run:
                        delete_set(s["id"])
                    removed.append(s["id"])
                continue
            kept += 1
            if kept > retain:
                log(f"prune {s['id']} (retain {retain})")
                if not dry_run:
                    delete_set(s["id"])
                removed.append(s["id"])
    return removed


# -------------------------------------------------------------------- pull

def source_key(source: str, env: dict[str, str] | None = None) -> str:
    env = env if env is not None else load_env()
    key = env.get(f"WPOPS_SYNC_KEY_{source.upper().replace('-', '_')}") or env.get("WPOPS_SYNC_KEY")
    if not key:
        raise SyncError(f"no sync key for source '{source}' (.env WPOPS_SYNC_KEY_{source.upper().replace('-', '_')})")
    return key


def client_for(inv: Inventory, source: str) -> SyncClient:
    o = inv.origin(source)
    if not o.sync_api:
        raise SyncError(f"origin '{source}' has no sync_api configured")
    return SyncClient(o.sync_api, source_key(source))


def pull(job: Job, inv: Inventory, source: str, label: str = "", fresh: bool = True,
         nonce: str | None = None, entities: list[str] | None = None, db: bool = True) -> dict[str, Any]:
    """Create (or reuse) a backup on the source and copy it here."""
    cfg = node_config()["backup"]
    log = job.log
    cli = client_for(inv, source)
    info = cli.ping()
    log(f"source {source}: SiteGuard {info.get('version')} at {info.get('site_url')} ({info.get('mode')})")
    try:
        return _pull_locked(job, inv, source, label, fresh, nonce, entities, db, cfg, cli, log,
                            lambda n: _note_created(job, n))
    except Exception:
        # A fresh backup we created on the source but could not pull would sit
        # there forever, eating the primary's storage. Remove it (best effort).
        created_nonce = job.progress.get("created_nonce")
        if created_nonce and cfg.get("delete_on_source", True):
            try:
                cli.delete_remote(created_nonce)
                log(f"removed unfinished set {created_nonce} from the source")
            except Exception as exc:  # noqa: BLE001
                log(f"could not remove set {created_nonce} from the source: {exc}")
        raise


def _note_created(job: Job, nonce: str) -> None:
    job.set_progress(created_nonce=nonce)


def _pull_locked(job: Job, inv: Inventory, source: str, label: str, fresh: bool, nonce: str | None,
                 entities: list[str] | None, db: bool, cfg: dict[str, Any], cli: "SyncClient", log,
                 note_created) -> dict[str, Any]:
    with state.locked(f"pull-{source}", timeout=5):
        if fresh and not nonce:
            label = label or f"wpops {time.strftime('%Y-%m-%d')}"
            if entities is None:
                entities = [str(e) for e in (cfg.get("entities") or [])] or None
            log(f"requesting db={db} entities={','.join(entities) if entities else 'source defaults'}")
            started = cli.start_backup(label, entities=entities, db=db)
            nonce = started.get("nonce")
            if not nonce:
                raise SyncError(f"source did not return a job nonce: {started}")
            note_created(str(nonce))
            log(f"backup started on source, nonce {nonce}")
            job.set_progress(phase="source-backup", nonce=nonce)
            deadline = time.time() + int(cfg.get("pull_timeout_min", 180)) * 60
            last = ""
            while True:
                st = cli.job(nonce)
                desc = f"{st.get('status')} {st.get('progress', '')}".strip()
                if desc != last:
                    log(f"  source job: {desc}")
                    last = desc
                if st.get("terminal"):
                    if not st.get("succeeded"):
                        raise SyncError(f"source backup ended with status '{st.get('status')}'")
                    break
                if time.time() > deadline:
                    raise SyncError("timed out waiting for the source backup")
                time.sleep(15)
        listing = cli.sets()
        sets = listing.get("sets", [])
        if nonce:
            chosen = next((s for s in sets if s.get("nonce") == nonce), None)
            if not chosen:
                raise SyncError(f"set {nonce} not found on source after backup")
        else:
            full = [s for s in sets if s.get("full")]
            if not full:
                raise SyncError("source has no complete backup set")
            chosen = max(full, key=lambda s: int(s.get("timestamp", 0)))
        ts = int(chosen["timestamp"])
        set_id = time.strftime("%Y%m%d-%H%M", time.gmtime(ts)) + "-" + str(chosen["nonce"])
        files = [f for f in chosen.get("files", []) if _FILE_RE.match(f.get("name", ""))]
        if not files:
            raise SyncError("set has no downloadable files")
        total = sum(int(f["size"]) for f in files)
        ceiling = int(float(cfg.get("max_set_gb", 50)) * 2**30)
        if total > ceiling:
            raise SyncError(f"set is {total // 2**30} GB, above backup.max_set_gb={cfg.get('max_set_gb', 50)}")
        dest = set_dir(source, set_id)
        dest.mkdir(parents=True, exist_ok=True)
        # Pulled sets are consumed by the stack's WP-CLI container (www-data), so
        # they must stay world-readable despite the agent's private umask.
        for d in (BACKUPS_DIR, dest.parent, dest):
            if d.exists():
                d.chmod(0o755)
        where = BACKUPS_DIR if BACKUPS_DIR.exists() else dest.parent
        try:
            disk.require(where, int(total * 1.1), f"pull of {source} ({total // 2**20} MB)")
        except disk.DiskError as first:
            # Make room first: the new set counts toward retain_sets, so drop the
            # oldest unkept sets of this source down to retain_sets - 1, then re-check.
            keep_n = max(1, int(cfg.get("retain_sets", 4)) - 1)
            dropped = prune(source, keep_n, log=log)
            if dropped:
                log(f"pruned to make room: {', '.join(dropped)}")
            try:
                disk.require(where, int(total * 1.1), f"pull of {source} ({total // 2**20} MB)")
            except disk.DiskError as exc:
                dest.rmdir() if not any(dest.iterdir()) else None
                raise SyncError(str(exc)) from None
        man = {
            "source": source, "nonce": chosen["nonce"], "timestamp": ts, "label": chosen.get("label", ""),
            "mode": listing.get("mode", ""), "site_url": listing.get("site_url", ""), "sites": listing.get("sites", []),
            "entities": chosen.get("entities", []), "files": files, "pulled_at": None, "keep": False, "applied": [],
            "plugin_version": info.get("version"),
        }
        save_manifest(dest, man)
        job.set_progress(phase="download", set=f"{source}/{set_id}", total_bytes=total, done_bytes=0)
        done = 0
        for f in files:
            name, size = f["name"], int(f["size"])
            log(f"  {name} ({size // 2**20} MB)")
            cli.download(chosen["nonce"], name, dest / name, size, log)
            (dest / name).chmod(0o644)
            done += size
            job.set_progress(done_bytes=done)
        # Integrity: sizes were enforced during download; now compare sha256 with
        # what the source computes for its own copies (needs SiteGuard >= 1.6.0).
        job.set_progress(phase="verify")
        expected = cli.hashes(str(chosen["nonce"]), log)
        if len(expected) != len(files):
            raise SyncError(f"source could not provide sha256 for every file ({len(expected)}/{len(files)}); set kept but not marked complete")
        for f in files:
            h = hashlib.sha256()
            with open(dest / f["name"], "rb") as fh:
                for chunk in iter(lambda: fh.read(4 * 2**20), b""):
                    h.update(chunk)
            if h.hexdigest() != expected[f["name"]]:
                raise SyncError(f"{f['name']}: checksum mismatch")
            f["sha256"] = expected[f["name"]]
        log(f"verified sha256 of {len(files)} files")
        man["pulled_at"] = int(time.time())
        save_manifest(dest, man)
        log(f"pulled {source}/{set_id}: {len(files)} files, {total // 2**20} MB")
        if cfg.get("delete_on_source", True) and (fresh or cfg.get("delete_on_source") == "always"):
            # The verified copy lives here now; free the primary's storage.
            try:
                cli.delete_remote(str(chosen["nonce"]))
                log(f"removed set {chosen['nonce']} from the source (copy verified here)")
            except SyncError as exc:
                log(f"warning: could not remove set from the source: {exc}")
        removed = prune(source, log=log)
        result = {"set": f"{source}/{set_id}", "files": len(files), "bytes": total, "sites": len(man["sites"]),
                  "pruned": removed, "label": man["label"]}
        notify.notify(
            "backup.pulled",
            f"[wpops] backup pulled from {source}: {set_id}",
            f"Backup set {source}/{set_id} ({total // 2**20} MB, {len(files)} files, {len(man['sites'])} sites)\n"
            f"was downloaded to this node and is ready to apply with: wpops restore apply --set {source}/{set_id}\n"
            f"Pruned: {', '.join(removed) or 'nothing'}\n",
            result,
        )
        return result


# ---------------------------------------------------------------- schedule

def parse_schedule(spec: str) -> dict[str, Any]:
    """weekly:<day>:<HH:MM> | daily:<HH:MM> | off."""
    spec = (spec or "off").strip().lower()
    if spec == "off":
        return {"kind": "off"}
    parts = spec.split(":")
    try:
        if parts[0] == "weekly" and len(parts) == 4:
            day = DAYS.index(parts[1][:3])
            return {"kind": "weekly", "day": day, "hour": int(parts[2]), "minute": int(parts[3])}
        if parts[0] == "daily" and len(parts) == 3:
            return {"kind": "daily", "hour": int(parts[1]), "minute": int(parts[2])}
    except (ValueError, IndexError):
        pass
    raise ConfigError(f"invalid schedule '{spec}' (weekly:sun:03:30 | daily:03:30 | off)")


def next_run(spec: str, after: float | None = None) -> int | None:
    s = parse_schedule(spec)
    if s["kind"] == "off":
        return None
    after = after or time.time()
    t = time.localtime(after)
    base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, s["hour"], s["minute"], 0, 0, 0, -1))
    if s["kind"] == "daily":
        return int(base if base > after else base + 86400)
    delta = (s["day"] - t.tm_wday) % 7
    cand = base + delta * 86400
    if cand <= after:
        cand += 7 * 86400
    return int(cand)


def schedule_info() -> dict[str, Any]:
    cfg = node_config()["backup"]
    st = state.read_json(SCHEDULE_STATE, {})
    return {
        "schedule": cfg.get("schedule"), "retain_sets": cfg.get("retain_sets"), "sources": cfg.get("sources"),
        "auto_apply": cfg.get("auto_apply", False), "last_run": st.get("last_run"), "last_result": st.get("last_result"),
        "next_run": next_run(cfg.get("schedule", "off")),
    }


def set_schedule(schedule: str | None = None, retain: int | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    cfg = node_config()
    if schedule is not None:
        parse_schedule(schedule)
        cfg["backup"]["schedule"] = schedule
    if retain is not None:
        if retain < 1 or retain > 100:
            raise ConfigError("retain must be 1..100")
        cfg["backup"]["retain_sets"] = retain
    if sources is not None:
        cfg["backup"]["sources"] = sources
    save_node_config(cfg)
    return schedule_info()


def due(now: float | None = None) -> bool:
    """True when the schedule's next slot has passed since the last run."""
    cfg = node_config()["backup"]
    spec = cfg.get("schedule", "off")
    if parse_schedule(spec)["kind"] == "off":
        return False
    now = now or time.time()
    st = state.read_json(SCHEDULE_STATE, {})
    last = st.get("last_run") or 0
    if not last:
        # First tick after install: anchor the schedule now instead of firing
        # immediately for a slot that passed before the agent existed.
        state.write_json(SCHEDULE_STATE, {"last_run": int(now), "last_result": {"note": "schedule anchored at install"}})
        return False
    nxt = next_run(spec, after=last)
    return nxt is not None and nxt <= now


def mark_run(result: dict[str, Any]) -> None:
    state.write_json(SCHEDULE_STATE, {"last_run": int(time.time()), "last_result": result})


def sources_to_pull(inv: Inventory) -> list[str]:
    cfg = node_config()["backup"]
    wanted = cfg.get("sources") or [n for n, o in inv.origins.items() if o.sync_api]
    return [s for s in wanted if s in inv.origins and inv.origins[s].sync_api]
