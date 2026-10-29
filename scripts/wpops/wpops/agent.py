"""HTTP API for the node (stdlib ThreadingHTTPServer), published through the node's tunnel.

Auth: `Authorization: Bearer <token>`; the SHA-256 of the token is kept in
state/agent-token.sha256 (`wpops token rotate` prints a new token once).
TLS is terminated by Cloudflare (tunnel) or by the LAN caller's own trust in
the plain port; the agent itself speaks HTTP on the configured bind addresses.
"""

from __future__ import annotations

import hashlib
import ipaddress
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import __version__, backup as bk, failover, jobs, notify, restore, state, disk
from .config import _HOST_RE, ConfigError, load_inventory, node_config, public_inventory
from .ops import node_status

TOKEN_FILE = state.STATE_DIR / "agent-token.sha256"
_ACTOR_RE = re.compile(r"^[A-Za-z0-9 _.@#:/-]{1,80}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _int_arg(q: dict[str, str], key: str, default: int) -> int:
    try:
        return int(q.get(key, default))
    except ValueError:
        raise ApiError(400, f"{key} must be an integer") from None


def _str_list(value: Any, what: str) -> list[str] | None:
    """None, or a list of strings (anything else is a 400)."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiError(400, f"{what} must be a list of strings")
    return value


def _set_ref(body: dict[str, Any]) -> str:
    ref = body.get("set")
    if not isinstance(ref, str) or not ref:
        raise ApiError(400, "set must be a <source>/<set-id> string")
    return ref


def token_hash() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""


def rotate_token() -> str:
    state.ensure_dirs()
    tok = secrets.token_urlsafe(36)
    # Atomic replace: a running agent reads the hash on every request, so the
    # old token stops working at once and no request sees a half-written file.
    state.write_private_text(TOKEN_FILE, hashlib.sha256(tok.encode()).hexdigest() + "\n")
    state.audit("local", "token.rotate")
    return tok


class RateLimiter:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self.hits: dict[str, deque] = defaultdict(deque)
        self.failed: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            self._sweep(self.hits, now - 60)
            q = self.hits[ip]
            if len(q) >= self.per_min:
                return False
            q.append(now)
            return True

    @staticmethod
    def _sweep(table: dict[str, deque], cutoff: float) -> None:
        """Drop expired timestamps and forget keys with none left (bounded memory)."""
        for key in list(table):
            q = table[key]
            while q and q[0] < cutoff:
                q.popleft()
            if not q:
                del table[key]

    def auth_failed(self, ip: str) -> None:
        with self.lock:
            self.failed[ip].append(time.time())

    def auth_blocked(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            self._sweep(self.failed, now - 600)
            return len(self.failed.get(ip, ())) >= 10


class Agent:
    def __init__(self) -> None:
        self.cfg = node_config()
        self.inv = load_inventory()
        self.limiter = RateLimiter(int(self.cfg["agent"].get("rate_limit_per_min", 120)))
        self.started = int(time.time())
        self.trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._load_trusted()
        self._stop = threading.Event()

    def _load_trusted(self) -> None:
        nets = []
        for cidr in self.cfg["agent"].get("trusted_proxies") or []:
            try:
                nets.append(ipaddress.ip_network(str(cidr), strict=False))
            except ValueError as exc:
                raise ConfigError(f"agent.trusted_proxies: bad CIDR {cidr!r}") from exc
        self.trusted = nets

    def trusts_proxy(self, peer: str) -> bool:
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(ip in net for net in self.trusted)

    # ---------------------------------------------------------------- helpers
    def reload(self) -> None:
        self.cfg = node_config()
        self.inv = load_inventory()
        self._load_trusted()

    def node(self) -> str:
        return self.cfg["node"]

    # ---------------------------------------------------------------- routes
    def route(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any], actor: str) -> Any:
        self.reload()
        parts = [p for p in path.split("/") if p]
        q1 = {k: v[0] for k, v in query.items()}
        if parts[:1] != ["v1"]:
            raise ApiError(404, "not found")
        p = parts[1:]
        if method == "GET":
            if p == ["status"]:
                return node_status(self.inv, self.cfg, live=q1.get("live") == "1")
            if p == ["inventory"]:
                return public_inventory(self.inv)
            if p == ["sites"]:
                if q1.get("live") == "1":
                    return {"sites": failover.live_status(self.inv)}
                return {"sites": public_inventory(self.inv)["sites"], "overrides": state.overrides()}
            if p == ["overrides"]:
                return {"overrides": state.overrides()}
            if p == ["backups"]:
                return {"sets": bk.list_sets(q1.get("source"))}
            if p == ["backups", "schedule"]:
                return bk.schedule_info()
            if p == ["jobs"]:
                return {"jobs": jobs.list_jobs(_int_arg(q1, "limit", 30), q1.get("kind"))}
            if len(p) == 2 and p[0] == "jobs":
                j = jobs.get(p[1])
                if not j:
                    raise ApiError(404, "no such job")
                return j
            if len(p) == 3 and p[0] == "jobs" and p[2] == "log":
                if not p[1].replace("-", "").isalnum():
                    raise ApiError(400, "bad job id")
                return {"log": jobs.tail(p[1], max(1, min(_int_arg(q1, "lines", 80), 2000)))}
            if p == ["events"]:
                return {"events": notify.recent_events(_int_arg(q1, "since", 0), _int_arg(q1, "limit", 100))}
            raise ApiError(404, "not found")
        if method == "POST":
            if p == ["failover", "plan"]:
                return self._plan(body).as_dict()
            if p == ["failover", "apply"]:
                plan = self._plan(body)
                if not plan.changes:
                    return {"job": None, "plan": plan.as_dict(), "message": "nothing to switch"}
                job = jobs.Job("failover.apply", {"to": plan.to_origin, "domains": [c.domain for c in plan.changes]}, actor)
                jobs.start_background(job, lambda j: failover.apply(self.inv, plan, actor, self.node(), log=j.log))
                return {"job": job.id, "plan": plan.as_dict()}
            if p == ["failover", "rollback"]:
                sel = self._selection(body)
                job = jobs.Job("failover.rollback", {k: v for k, v in sel.items() if v}, actor)
                domains = body.get("domains")
                if domains is not None:
                    if not isinstance(domains, list) or not all(isinstance(d, str) and _HOST_RE.match(d.lower()) for d in domains):
                        raise ApiError(400, "domains must be a list of hostnames")
                    domains = [d.lower() for d in domains]
                jobs.start_background(job, lambda j: failover.rollback(
                    self.inv, actor, self.node(), sites=sel["sites"], all_sites=sel["all"],
                    domains=domains, log=j.log))
                return {"job": job.id}
            if p == ["backups", "pull"]:
                source = body.get("source") or ""
                if not isinstance(source, str) or source not in self.inv.sources():
                    raise ApiError(400, "unknown source")
                label, nonce = body.get("label", ""), body.get("nonce")
                if not isinstance(label, str) or (nonce is not None and not isinstance(nonce, str)):
                    raise ApiError(400, "label and nonce must be strings")
                entities = _str_list(body.get("entities"), "entities")
                if jobs.active("backup.pull"):
                    raise ApiError(409, "a pull is already running")
                job = jobs.Job("backup.pull", {"source": source, "label": label}, actor)
                jobs.start_background(job, lambda j: bk.pull(j, self.inv, source, label,
                                                             fresh=bool(body.get("fresh", True)), nonce=nonce,
                                                             entities=entities, db=bool(body.get("db", True))))
                return {"job": job.id}
            if p == ["backups", "prune"]:
                source = body.get("source")
                if source is not None and not isinstance(source, str):
                    raise ApiError(400, "source must be a string")
                retain = body.get("retain")
                # retain=0 (or a negative number) would delete every unkept set.
                if retain is not None and (not isinstance(retain, int) or isinstance(retain, bool) or retain < 1 or retain > 365):
                    raise ApiError(400, "retain must be an integer 1-365")
                return {"removed": bk.prune(source or None, retain, bool(body.get("dry_run", False)), log=lambda s: None)}
            if p == ["backups", "schedule"]:
                schedule = body.get("schedule")
                if schedule is not None and not isinstance(schedule, str):
                    raise ApiError(400, "schedule must be a string")
                retain = body.get("retain")
                if retain is not None and (not isinstance(retain, int) or isinstance(retain, bool) or retain < 1 or retain > 365):
                    raise ApiError(400, "retain must be an integer 1-365")
                sources = body.get("sources")
                if sources is not None:
                    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
                        raise ApiError(400, "sources must be a list of backup source names")
                    unknown = [s for s in sources if s not in self.inv.sources()]
                    if unknown:
                        raise ApiError(400, f"unknown sources: {', '.join(unknown)}")
                return bk.set_schedule(schedule, retain, sources)
            if p == ["backups", "keep"]:
                return {"manifest": bk.mark_keep(_set_ref(body), bool(body.get("keep", True)))}
            if p == ["backups", "delete"]:
                ref = _set_ref(body)
                bk.delete_set(ref)
                state.audit(actor, "backup.delete", {"set": ref})
                return {"deleted": ref}
            if p == ["restore", "apply"]:
                set_ref = body.get("set") or ""
                stack = body.get("stack") or ""
                if not set_ref or not stack or not isinstance(set_ref, str) or not isinstance(stack, str):
                    raise ApiError(400, "set and stack are required")
                sel = self._selection(body)
                entities = _str_list(body.get("entities"), "entities")
                # Reject bad input here, not as a failed job that holds the restore slot.
                if stack not in self.inv.origin(self.node()).stacks:
                    raise ApiError(400, f"unknown stack '{stack}'")
                try:
                    bk.manifest(set_ref)
                except bk.SyncError as exc:
                    raise ApiError(400, str(exc)) from None
                for slug in sel["sites"]:
                    self.inv.site(slug)
                if jobs.active("restore.apply"):
                    raise ApiError(409, "a restore is already running")
                job = jobs.Job("restore.apply", {"set": set_ref, "stack": stack, **{k: v for k, v in sel.items() if v}}, actor)
                jobs.start_background(job, lambda j: restore.apply(
                    j, self.inv, self.node(), set_ref, stack, sites=sel["sites"], all_sites=sel["all"],
                    entities=entities))
                return {"job": job.id}
            if p == ["notify", "test"]:
                return notify.notify("test", "[wpops] test notification", "This is a test from the wpops agent.", {})
            raise ApiError(404, "not found")
        raise ApiError(405, "method not allowed")

    def _selection(self, body: dict[str, Any]) -> dict[str, Any]:
        sites = body.get("sites") or []
        if isinstance(sites, str):
            sites = [s.strip() for s in sites.split(",") if s.strip()]
        _str_list(sites, "sites")
        return {"sites": sites, "all": bool(body.get("all"))}

    def _plan(self, body: dict[str, Any]) -> failover.Plan:
        sel = self._selection(body)
        try:
            sites = failover.resolve_selection(self.inv, sel["sites"], sel["all"])
            return failover.plan(self.inv, sites, to=body.get("to") or None, stack=body.get("stack") or None, cfg=self.cfg)
        except (failover.FailoverError, ConfigError) as exc:
            raise ApiError(400, str(exc)) from None

    # ------------------------------------------------------------- scheduler
    def scheduler(self) -> None:
        last_housekeeping = 0.0
        while not self._stop.wait(60):
            try:
                self.reload()
                disk.watch_tick(self.inv, self.node())
                if bk.due() and not jobs.active("backup.pull"):
                    results = {}
                    for src in bk.sources_to_pull(self.inv):
                        job = jobs.Job("backup.pull", {"source": src, "label": "scheduled", "scheduled": True}, "scheduler")
                        job.run(lambda j, s=src: bk.pull(j, self.inv, s, "scheduled " + time.strftime("%Y-%m-%d")))
                        results[src] = {"status": job.status, "error": job.error, "result": job.result}
                        if job.status != "succeeded":
                            notify.notify("backup.failed", f"[wpops] scheduled backup from {src} FAILED",
                                          f"Job {job.id}: {job.error}\n\n{jobs.tail(job.id, 40)}", {"source": src, "job": job.id})
                    bk.mark_run(results)
                if time.time() - last_housekeeping > 86400:
                    jobs.prune_jobs()
                    last_housekeeping = time.time()
            except Exception as exc:  # noqa: BLE001
                notify.record_event("scheduler.error", {"error": str(exc)})

    def stop(self) -> None:
        self._stop.set()


def make_handler(agent: Agent) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"wpops/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet; audit log covers mutations
            pass

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        timeout = 30  # per-connection socket timeout (slowloris)

        def _client_ip(self) -> str:
            peer = self.client_address[0]
            cf = (self.headers.get("CF-Connecting-IP") or "").strip()
            if cf and agent.trusts_proxy(peer):
                try:
                    return str(ipaddress.ip_address(cf))
                except ValueError:
                    return peer
            return peer

        def _auth(self) -> bool:
            auth = self.headers.get("Authorization", "")
            # Read on every request so `wpops token rotate` revokes the old
            # token without restarting the agent.
            expected = token_hash()
            if not auth.startswith("Bearer ") or not expected:
                return False
            given = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
            return hmac.compare_digest(given, expected)

        def _handle(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/healthz":
                self._send(200, {"ok": True, "version": __version__, "node": agent.node(), "uptime": int(time.time()) - agent.started})
                return
            ip = self._client_ip()
            if not agent.limiter.check(ip):
                self._send(429, {"error": "rate limited"})
                return
            if agent.limiter.auth_blocked(ip):
                self._send(429, {"error": "too many failed authentications"})
                return
            if not self._auth():
                agent.limiter.auth_failed(ip)
                self._send(401, {"error": "unauthorized"})
                return
            actor = self.headers.get("X-Actor", "").strip() or "api"
            if not _ACTOR_RE.match(actor):
                actor = "api"
            body: dict[str, Any] = {}
            if method == "POST":
                if self.headers.get("Transfer-Encoding"):
                    self._send(411, {"error": "Content-Length required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    self._send(400, {"error": "bad Content-Length"})
                    return
                if length > 1_000_000:
                    self._send(413, {"error": "body too large"})
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode() or "{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "invalid JSON"})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": "body must be an object"})
                    return
            try:
                result = agent.route(method, url.path, urllib.parse.parse_qs(url.query), body, f"{actor}@{ip}")
                if method == "POST":
                    state.audit(f"{actor}@{ip}", f"api {url.path}", {k: v for k, v in body.items() if k != "token"})
                self._send(200, result)
            except ApiError as exc:
                self._send(exc.status, {"error": str(exc)})
            except (ConfigError, bk.SyncError, restore.RestoreError, failover.FailoverError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                notify.record_event("api.error", {"path": url.path, "error": str(exc)})
                self._send(500, {"error": f"internal error: {exc}"})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

    return Handler


def serve(extra_binds: list[str] | None = None) -> None:
    agent = Agent()
    if not token_hash():
        raise ConfigError("no agent token; run `wpops token rotate` first")
    port = int(agent.cfg["agent"].get("port", 8787))
    binds = list(dict.fromkeys((agent.cfg["agent"].get("bind") or ["127.0.0.1"]) + (extra_binds or [])))
    os.umask(0o077)  # every file the agent creates starts private
    reaped = jobs.reap_stale()
    if reaped:
        state.audit("local", "jobs.reap", {"jobs": reaped})
    handler = make_handler(agent)
    servers = []
    for b in binds:
        srv = ThreadingHTTPServer((b, port), handler)
        srv.daemon_threads = True
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, name=f"http-{b}", daemon=True).start()
        print(f"wpops agent {__version__} listening on {b}:{port}", flush=True)
    threading.Thread(target=agent.scheduler, name="scheduler", daemon=True).start()
    state.audit("local", "agent.start", {"binds": binds, "port": port})
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
        for srv in servers:
            srv.shutdown()
