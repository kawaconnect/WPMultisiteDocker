"""Jobs: long operations (pull, restore, failover) with a JSON record and a log file.

A job record lives at state/jobs/<id>.json; its log at state/jobs/<id>.log.
The CLI runs jobs inline (so `wpops backup pull` blocks and prints the log);
the agent runs them on a thread and returns the id straight away, and the bot
polls `jobs get`.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import state
from .config import STATE_DIR

JOBS_DIR = STATE_DIR / "jobs"
TERMINAL = {"succeeded", "failed", "cancelled"}
# A queued/running record from before jobs carried an owner is treated as
# abandoned once it is this old (longer than any pull or restore can take).
LEGACY_STALE_SECONDS = 24 * 3600


def _proc_start(pid: int) -> str | None:
    """Kernel start time of a process (tells a live process from a reused pid), or None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def _owner() -> dict[str, Any]:
    return {"pid": os.getpid(), "start": _proc_start(os.getpid())}


def is_stale(j: dict[str, Any], now: float | None = None) -> bool:
    """True for a queued/running record whose process has gone (agent restart, crash, killed CLI)."""
    if j.get("status") not in ("queued", "running"):
        return False
    owner = j.get("owner")
    if isinstance(owner, dict) and owner.get("pid"):
        if owner.get("start") is None:
            return False  # no /proc when the job was created: cannot tell
        return _proc_start(int(owner["pid"])) != owner["start"]
    return (now or time.time()) - int(j.get("created") or 0) > LEGACY_STALE_SECONDS


class Job:
    def __init__(self, kind: str, params: dict[str, Any], actor: str, job_id: str | None = None):
        self.id = job_id or time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        self.kind = kind
        self.params = params
        self.actor = actor
        self.status = "queued"
        self.created = int(time.time())
        self.started: int | None = None
        self.finished: int | None = None
        self.progress: dict[str, Any] = {}
        self.result: Any = None
        self.error: str = ""
        self.owner = _owner()
        self._log_fh = None
        self._lock = threading.Lock()
        self.save()

    # ----------------------------------------------------------- persistence
    @property
    def path(self) -> Path:
        return JOBS_DIR / f"{self.id}.json"

    @property
    def log_path(self) -> Path:
        return JOBS_DIR / f"{self.id}.log"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "params": self.params, "actor": self.actor,
            "status": self.status, "created": self.created, "started": self.started,
            "finished": self.finished, "progress": self.progress, "result": self.result,
            "error": self.error, "terminal": self.status in TERMINAL, "owner": self.owner,
        }

    def save(self) -> None:
        with self._lock:
            state.write_json(self.path, self.as_dict())

    def log(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        msg = f"[{ts}] {line}"
        with self._lock:
            if self._log_fh is None:
                state.ensure_dirs()
                self._log_fh = open(self.log_path, "a", encoding="utf-8")
            self._log_fh.write(msg + "\n")
            self._log_fh.flush()
        if self.params.get("_echo"):
            print(msg, flush=True)

    def set_progress(self, **kw: Any) -> None:
        self.progress.update(kw)
        self.save()

    # -------------------------------------------------------------- running
    def run(self, fn: Callable[["Job"], Any]) -> "Job":
        self.status = "running"
        self.started = int(time.time())
        self.save()
        try:
            self.result = fn(self)
            self.status = "succeeded"
        except Exception as exc:  # noqa: BLE001
            self.status = "failed"
            self.error = str(exc) or exc.__class__.__name__
            self.log("ERROR: " + self.error)
            self.log(traceback.format_exc().rstrip())
        finally:
            self.finished = int(time.time())
            self.save()
            with self._lock:
                if self._log_fh:
                    self._log_fh.close()
                    self._log_fh = None
        return self


_running: dict[str, threading.Thread] = {}


def start_background(job: Job, fn: Callable[[Job], Any], on_done: Callable[[Job], None] | None = None) -> Job:
    def _target() -> None:
        try:
            job.run(fn)
        finally:
            _running.pop(job.id, None)
            if on_done:
                try:
                    on_done(job)
                except Exception:  # noqa: BLE001
                    pass
    t = threading.Thread(target=_target, name=f"job-{job.id}", daemon=True)
    _running[job.id] = t
    t.start()
    return job


def active(kind: str | None = None) -> list[dict[str, Any]]:
    """Queued/running jobs whose process is still alive. A record left behind by
    a restarted agent must not block every later pull or restore."""
    out = []
    for j in list_jobs(limit=200):
        if j["status"] in ("queued", "running") and (kind is None or j["kind"] == kind) and not is_stale(j):
            out.append(j)
    return out


def reap_stale() -> list[str]:
    """Mark abandoned queued/running records as failed (called at agent start)."""
    reaped = []
    for f in sorted(JOBS_DIR.glob("*.json")) if JOBS_DIR.exists() else []:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(j, dict) and is_stale(j):
            j.update(status="failed", terminal=True, finished=j.get("finished") or int(time.time()),
                     error=j.get("error") or "interrupted: the process running this job exited")
            state.write_json(f, j)
            reaped.append(str(j.get("id", f.stem)))
    return reaped


def get(job_id: str) -> dict[str, Any] | None:
    if not job_id.replace("-", "").isalnum():
        return None
    p = JOBS_DIR / f"{job_id}.json"
    return state.read_json(p, None) if p.exists() else None


def tail(job_id: str, lines: int = 60) -> str:
    if not job_id or not job_id.replace("-", "").isalnum():
        return ""
    p = JOBS_DIR / f"{job_id}.log"
    if not p.exists():
        return ""
    data = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def list_jobs(limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
    state.ensure_dirs()
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.name, reverse=True)
    out = []
    for f in files:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if kind and j.get("kind") != kind:
            continue
        out.append(j)
        if len(out) >= limit:
            break
    return out


def prune_jobs(keep: int = 200, max_age_days: int = 90) -> int:
    state.ensure_dirs()
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.name, reverse=True)
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for i, f in enumerate(files):
        if i < keep and f.stat().st_mtime > cutoff:
            continue
        for p in (f, f.with_suffix(".log"), f.with_name(f.name + ".prev")):
            if p.exists():
                p.unlink()
        removed += 1
    return removed
