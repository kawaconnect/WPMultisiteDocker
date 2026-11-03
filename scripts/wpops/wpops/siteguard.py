"""SiteGuard health on this node's stacks: keys, local health requests, direct checks.

Every stack is its own WordPress install, so every stack's SiteGuard has its
own health key. The agent holds one key per stack (state/siteguard-keys.json,
0600) and reads each stack's health API over the stack's loopback port, so
the bot and other clients need only the node's agent token.

Keys are enrolled through WP-CLI inside the stack:
  SiteGuard 1.6.4+  `health_api_key --set-node`: a node key next to the site's
                    own key; the site's key (and whoever uses it) is untouched.
  older SiteGuard   `health_api_key --set`, only when the site has no key yet
                    (or with replace=True). Otherwise the agent falls back to
                    direct HTTP checks of the site.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import state
from .config import Origin, load_env

NODE_KEY_MIN_VERSION = (1, 6, 4)


def keys_file() -> Path:
    return state.STATE_DIR / "siteguard-keys.json"


def load_keys() -> dict[str, Any]:
    try:
        return state.read_json(keys_file(), {})
    except (OSError, ValueError):
        return {}


def save_key(stack: str, record: dict[str, Any] | None) -> None:
    with state.locked("siteguard-keys", timeout=30):
        keys = load_keys()
        if record is None:
            keys.pop(stack, None)
        else:
            keys[stack] = record
        state.write_json(keys_file(), keys)


def fingerprint(key: str) -> str:
    """Same fingerprint SiteGuard shows (sgk-<12 hex>)."""
    return "sgk-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def version_tuple(v: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


# ------------------------------------------------------------- loopback HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def loopback_port(origin: Origin, stack: str) -> int | None:
    """127.0.0.1 port the stack's Nginx publishes (managed: HTTP_PORT, legacy root: 8080)."""
    st = origin.stacks[stack]
    if st.get("http_port"):
        return int(st["http_port"])
    env = load_env(Path(st["path"]) / ".env", resolve=False)
    if env.get("HTTP_PORT", "").isdigit():
        return int(env["HTTP_PORT"])
    return None if st.get("managed") else 8080


def local_request(port: int, host: str, path: str, method: str = "GET", key: str | None = None,
                  timeout: float = 15) -> tuple[int, Any, float, str]:
    """(status, parsed JSON or None, seconds, error). Status 0 = no HTTP answer.

    The request goes to the stack's own Nginx with the site's Host header, as
    https (X-Forwarded-Proto), so WordPress answers as it does for visitors.
    """
    headers = {"Host": host, "X-Forwarded-Proto": "https", "User-Agent": "wpops-registry/1",
               "Accept": "application/json, text/html;q=0.5"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers,
                                 data=b"" if method == "POST" else None)
    started = time.monotonic()
    try:
        resp = _OPENER.open(req, timeout=timeout)
        status, raw = resp.status, resp.read(4 * 1024 * 1024)
        resp.close()
        err = ""
    except urllib.error.HTTPError as exc:
        status, raw, err = exc.code, exc.read(64 * 1024), f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return 0, None, time.monotonic() - started, f"no answer: {reason}"
    elapsed = time.monotonic() - started
    try:
        data = json.loads(raw.decode("utf-8", "replace")) if raw else None
    except json.JSONDecodeError:
        data = None
    return status, data, elapsed, err


def fetch_health(port: int, host: str, key: str, timeout: float = 15) -> dict[str, Any]:
    """{'ok': True, 'payload': {...}} or {'ok': False, 'kind': auth|disabled|missing|http|network, 'error'}."""
    status, data, _, err = local_request(port, host, "/wp-json/sgbackup/v1/health", key=key, timeout=timeout)
    if status == 200 and isinstance(data, dict) and isinstance(data.get("sites"), list):
        return {"ok": True, "payload": data}
    code = data.get("code") if isinstance(data, dict) else ""
    if status in (401, 403):
        kind = "auth"
    elif status == 404 and code == "disabled":
        kind = "disabled"
    elif status == 404:
        kind = "missing"
    elif status == 0:
        kind = "network"
    else:
        kind = "http"
    return {"ok": False, "kind": kind, "error": (err or f"HTTP {status}") + (f" {code}" if code else "")}


def force_check(port: int, host: str, key: str, timeout: float = 60) -> dict[str, Any] | None:
    """POST /health/check (SiteGuard runs its checks now). None on cooldown/error."""
    status, data, _, _ = local_request(port, host, "/wp-json/sgbackup/v1/health/check", "POST", key, timeout)
    return data if status == 200 and isinstance(data, dict) and isinstance(data.get("sites"), list) else None


def direct_check(port: int, host: str, path: str = "/", timeout: float = 15) -> dict[str, Any]:
    """Fetch the page itself. Any answer below 500 counts as up (the site is serving)."""
    status, _, elapsed, err = local_request(port, host, path or "/", timeout=timeout)
    up = 0 < status < 500
    return {"up": up, "http_code": status, "response_time": round(elapsed, 3),
            "error": "" if up else (err or f"HTTP {status}")}


# ---------------------------------------------------------------- enrolment

def _wp_stdin(origin: Origin, stack: str, args: list[str], text: str, timeout: int = 120):
    from .stacks import compose
    cli = origin.stacks[stack].get("cli_service") or "wp-cron"
    return compose(origin, stack, "exec", "-T", cli, "wp", *args, "--allow-root", check=False,
                   timeout=timeout, input_text=text)


def key_status(origin: Origin, stack: str) -> dict[str, Any] | None:
    """SiteGuard's own view of its health key, or None when SiteGuard is not active."""
    from .stacks import wp
    r = wp(origin, stack, "siteguardbackup", "health_api_key", "--format=json", timeout=120)
    if r.returncode:
        return None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return None


def enroll(origin: Origin, stack: str, sg_version: str | None, host: str, replace: bool = False,
           log=lambda _m: None) -> dict[str, Any]:
    """Give the stack's SiteGuard a key the agent holds, enable its monitor, verify over HTTP."""
    from .stacks import wp
    status = key_status(origin, stack)
    if status is None:
        return {"enrolled": False, "reason": "SiteGuard Backup is not active in this stack"}
    key = secrets.token_hex(24)          # 48 letters and digits, as SiteGuard requires (>= 32)
    if version_tuple(sg_version) >= NODE_KEY_MIN_VERSION:
        kind, args = "node", ["siteguardbackup", "health_api_key", "--set-node"]
    elif replace or not (status.get("key") or {}).get("has_key"):
        kind, args = "site", ["siteguardbackup", "health_api_key", "--set"]
    else:
        return {"enrolled": False,
                "reason": f"SiteGuard {sg_version or '?'} has its own key and no node key support (1.6.4+); "
                          "update SiteGuard, or enroll with replace (wpops siteguard enroll --replace)"}
    r = _wp_stdin(origin, stack, args, key)
    if r.returncode:
        return {"enrolled": False, "reason": ((r.stderr or r.stdout or "").strip() or "WP-CLI failed")[-300:]}
    log(f"{stack}: SiteGuard {kind} key set ({fingerprint(key)})")
    if not status.get("enabled"):
        e = wp(origin, stack, "siteguardbackup", "health_config", "enable", timeout=120)
        if e.returncode:
            return {"enrolled": False, "reason": "could not enable the health monitor: " +
                    ((e.stderr or e.stdout or "").strip())[-200:]}
        log(f"{stack}: SiteGuard health monitor enabled")
    port = loopback_port(origin, stack)
    record = {"key": key, "fp": fingerprint(key), "kind": kind, "enrolled_at": int(time.time()),
              "siteguard": sg_version or ""}
    save_key(stack, record)
    verified = False
    if port:
        check = fetch_health(port, host, key)
        verified = check["ok"]
        if not verified:
            log(f"{stack}: health API check after enrolment: {check.get('error')}")
    state.audit("local", "siteguard.enroll", {"stack": stack, "kind": kind, "fp": record["fp"]})
    return {"enrolled": True, "kind": kind, "fp": record["fp"], "verified": verified}
