"""Notifications: e-mail (SMTP from .env) and an optional webhook (the bot)."""

from __future__ import annotations

import json
import smtplib
import ssl
import time
import urllib.request
from email.message import EmailMessage
from typing import Any

from . import state
from .config import load_env, node_config

EVENTS_FILE = state.STATE_DIR / "events.jsonl"


def record_event(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Append to state/events.jsonl; the bot reads these through the API."""
    state.ensure_dirs()
    ev = {"ts": int(time.time()), "kind": kind, **data}
    with open(EVENTS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, sort_keys=True) + "\n")
    return ev


def recent_events(since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    if not EVENTS_FILE.exists():
        return []
    out = []
    for line in EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-2000:]:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("ts", 0) > since:
            out.append(ev)
    return out[-limit:]


def send_email(subject: str, body: str, to: list[str] | None = None) -> dict[str, Any]:
    cfg = node_config()["notify"]
    env = load_env()
    to = to or cfg.get("email_to") or []
    if not to:
        return {"sent": False, "reason": "no recipients configured (notify.email_to)"}
    host = env.get("SMTP_HOST")
    if not host:
        return {"sent": False, "reason": "SMTP_HOST not set in .env"}
    port = int(env.get("SMTP_PORT") or 587)
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASS", "")
    secure = env.get("SMTP_SECURE", "").lower() in ("1", "true", "ssl")
    sender = cfg.get("email_from") or env.get("SMTP_FROM") or user or f"wpops@{host}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if secure:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ctx)
                s.ehlo()
            elif user:
                raise RuntimeError(f"SMTP {host}:{port} offers no STARTTLS; refusing to send credentials in clear (use SMTP_SECURE or a TLS port)")
                s.ehlo()
            if user:
                s.login(user, password)
            s.send_message(msg)
    return {"sent": True, "to": to}


def post_webhook(event: dict[str, Any]) -> dict[str, Any]:
    cfg = node_config()["notify"]
    url = cfg.get("webhook_url")
    if not url:
        return {"posted": False, "reason": "no webhook_url"}
    env = load_env()
    headers = {"Content-Type": "application/json", "User-Agent": "wpops/1"}
    if env.get("WPOPS_WEBHOOK_TOKEN"):
        headers["Authorization"] = "Bearer " + env["WPOPS_WEBHOOK_TOKEN"]
    req = urllib.request.Request(url, data=json.dumps(event).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return {"posted": True, "status": resp.status}
    except Exception as exc:  # noqa: BLE001
        return {"posted": False, "reason": str(exc)}


def notify(kind: str, subject: str, body: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record the event, then e-mail and webhook on a best-effort basis."""
    ev = record_event(kind, {"subject": subject, **(data or {})})
    out: dict[str, Any] = {"event": ev}
    try:
        out["email"] = send_email(subject, body)
    except Exception as exc:  # noqa: BLE001
        out["email"] = {"sent": False, "reason": str(exc)}
    out["webhook"] = post_webhook({**ev, "body": body})
    return out
