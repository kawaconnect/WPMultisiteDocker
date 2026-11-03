"""`wpops secrets check|refresh`: 1Password references, the tmpfs cache, plaintext.

check looks at the root .env, every stacks/<name>/.env and the tunnel
connector project, using the cache only (no 1Password reads unless --fresh).
It reports key names and counts, never values.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import config
from .config import REPO_DIR, SecretError, _parse_env, is_op_ref

# Keys whose values are secrets. Everything else in .env (names, ids, ports,
# hostnames, flags) is configuration and stays as plain text.
SECRET_KEY_RE = re.compile(r"(PASSWORD|TOKEN|SECRET|_KEY(_|$)|PASS$|PASSPHRASE|CREDENTIAL)", re.I)


def check_file(path: Path, cache: dict[str, Any]) -> dict[str, Any]:
    env = _parse_env(path)
    refs = {k: v for k, v in env.items() if is_op_ref(v)}
    plain = sorted(k for k, v in env.items() if SECRET_KEY_RE.search(k) and v and not is_op_ref(v))
    out: dict[str, Any] = {"file": str(path), "refs": len(refs), "plaintext_keys": plain}
    missing = sorted(k for k, r in refs.items() if r not in cache["values"])
    empty = sorted(k for k, r in refs.items() if r in cache["values"] and not cache["values"][r])
    if refs:
        out["cache"] = ("missing" if len(missing) == len(refs) else "incomplete" if missing
                        else "fresh" if config.cache_fresh(path, cache) else "stale")
    if missing:
        out["not_cached"] = missing
    if empty:
        out["empty"] = empty
    out["ok"] = not plain and not missing and not empty
    out["warn"] = out.get("cache") == "stale"
    return out


def check_tunnels_file(path: Path | None = None) -> dict[str, Any]:
    path = path or REPO_DIR / "docker-compose.wpops-tunnels.json"
    out: dict[str, Any] = {"file": str(path), "ok": True, "literal_tokens": []}
    if not path.exists():
        return out
    try:
        services = json.loads(path.read_text(encoding="utf-8")).get("services") or {}
    except (OSError, ValueError) as exc:
        return {**out, "ok": False, "error": f"unreadable: {exc}"}
    for name, svc in services.items():
        for key, value in ((svc or {}).get("environment") or {}).items():
            if SECRET_KEY_RE.search(key) and isinstance(value, str) and value and not value.startswith("${"):
                out["literal_tokens"].append(f"{name}.{key}")
    out["ok"] = not out["literal_tokens"]
    return out


def check(fresh: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if fresh:
        try:
            out["refresh"] = config.refresh_cache()
        except SecretError as exc:
            out["refresh"] = {"error": str(exc)}
    cache = config.load_cache()
    files = [check_file(p, cache) for p in config.node_env_files()]
    tunnels = check_tunnels_file()
    b = config.backoff_state()
    out.update({
        "ok": all(f["ok"] for f in files) and tunnels["ok"] and "error" not in out.get("refresh", {}),
        "warnings": sum(1 for f in files if f["warn"]),
        "cache": {"file": str(config.cache_dir() / "secrets.json"), "origin": cache.get("origin", "none"),
                  "filled_at": cache.get("filled_at"), "values": len(cache["values"]),
                  "last_error": b.get("error", ""), "next_try": b.get("next_try")},
        "files": files, "tunnels": tunnels,
    })
    return out


def refresh_boot(attempts: int = 3, first_delay: float = 30, log=print) -> dict[str, Any]:
    """At boot: fill the cache if any file is not fresh, retrying with backoff.
    Gives up quietly after `attempts`; the agent keeps retrying and alerts once."""
    files = config.node_env_files()
    if all(config.cache_fresh(p) for p in files if config.file_refs(p)):
        log("secrets cache is fresh; 1Password not contacted")
        return {"ok": True, "read": False}
    delay = first_delay
    for n in range(1, attempts + 1):
        try:
            r = config.refresh_cache(files)
            log(f"secrets cache filled: {r['refs']} references from {r['files']} files (one 1Password call)")
            return {"ok": True, "read": True, **r}
        except SecretError as exc:
            log(f"attempt {n}/{attempts}: {exc}")
            if n < attempts:
                time.sleep(delay)
                delay *= 3
    return {"ok": False}
