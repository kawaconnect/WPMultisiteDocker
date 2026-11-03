"""`wpops secrets check`: are this node's secrets 1Password references, and do they resolve?

Looks at the root .env, every stacks/<name>/.env and the tunnel connector
project. Reports key names and counts only, never values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import ENV_FILE, REPO_DIR, SecretError, _parse_env, clear_secret_cache, is_op_ref, resolve_refs

# Keys whose values are secrets. Everything else in .env (names, ids, ports,
# hostnames, flags) is configuration and stays as plain text.
SECRET_KEY_RE = re.compile(r"(PASSWORD|TOKEN|SECRET|_KEY(_|$)|PASS$|PASSPHRASE|CREDENTIAL)", re.I)


def env_files() -> list[Path]:
    files = [ENV_FILE] if ENV_FILE.exists() else []
    stacks = REPO_DIR / "stacks"
    if stacks.is_dir():
        files += sorted(p for p in stacks.glob("*/.env") if not p.parent.name.startswith("_"))
    return files


def check_file(path: Path, resolve: bool = True) -> dict[str, Any]:
    env = _parse_env(path)
    refs = {k: v for k, v in env.items() if is_op_ref(v)}
    plain = sorted(k for k, v in env.items() if SECRET_KEY_RE.search(k) and v and not is_op_ref(v))
    out: dict[str, Any] = {"file": str(path), "refs": len(refs), "plaintext_keys": plain, "ok": not plain}
    if resolve and refs:
        try:
            values = resolve_refs(set(refs.values()))
            empty = sorted(k for k, v in refs.items() if not values.get(v))
            if empty:
                out["empty"] = empty
                out["ok"] = False
        except SecretError as exc:
            out["error"] = str(exc)
            out["ok"] = False
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


def check(resolve: bool = True) -> dict[str, Any]:
    if resolve:
        clear_secret_cache()        # always ask 1Password, not this process's cache
    files = [check_file(p, resolve) for p in env_files()]
    tunnels = check_tunnels_file()
    return {"ok": all(f["ok"] for f in files) and tunnels["ok"], "files": files, "tunnels": tunnels}
