"""Node state: small JSON documents under state/ with file locking, plus the audit log."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import STATE_DIR

OVERRIDES_FILE = STATE_DIR / "overrides.json"
AUDIT_LOG = STATE_DIR / "audit.log"
LOCK_DIR = STATE_DIR / "locks"


def ensure_dirs() -> None:
    for d in (STATE_DIR, LOCK_DIR, STATE_DIR / "jobs"):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


@contextmanager
def locked(name: str, timeout: float = 120.0) -> Iterator[None]:
    """Exclusive advisory lock. Serialises failover/rollback and backup runs."""
    ensure_dirs()
    path = LOCK_DIR / f"{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"lock '{name}' is held by another operation") from None
                time.sleep(0.25)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            data = fh.read()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return json.loads(data) if data.strip() else default


def write_json(path: Path, data: Any) -> None:
    """Atomic replace; the previous version is kept as <name>.prev for forensics."""
    ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    if path.exists():
        os.replace(path, path.with_name(path.name + ".prev"))
    os.replace(tmp, path)


def overrides() -> dict[str, Any]:
    """domain -> override record (see failover.py)."""
    return read_json(OVERRIDES_FILE, {})


def save_overrides(data: dict[str, Any]) -> None:
    write_json(OVERRIDES_FILE, data)


def audit(actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
    ensure_dirs()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "actor": actor, "action": action}
    if detail:
        rec["detail"] = detail
    with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    try:
        os.chmod(AUDIT_LOG, 0o600)
    except OSError:
        pass
