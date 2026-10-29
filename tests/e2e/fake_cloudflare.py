#!/usr/bin/env python3
"""In-memory stand-in for the parts of the Cloudflare v4 API that wpops and
scripts/cf-*.sh call. For the opt-in end-to-end tests only (tests/e2e/).

    python3 tests/e2e/fake_cloudflare.py --port 8999 --seed seed.json

Point the tools at it with CF_API_URL=http://<host>:8999/client/v4 (process
environment or .env).

Seed format:
    {"accounts": {"<account id>": {"token": "...", "zones": ["example.com"]}},
     "records": [{"zone": "example.com", "type": "A", "name": "shop.example.com",
                  "content": "192.0.2.10", "proxied": true}]}

Test control endpoints (no auth, never part of the real API):
    GET  /_fake/state                 accounts, zones, records, tunnels, request log
    POST /_fake/records               {"zone", "type", "name", "content", ...} add a record
    POST /_fake/fail                  {"method": "POST", "path": "regex", "status": 500, "count": 1}
                                      make the next matching calls fail
    POST /_fake/reset-log             clear the request log

Behaviour copied from the real API where the tools depend on it: tokens only
see their own account's zones and tunnels; a CNAME cannot share a name with
any other A/AAAA/CNAME record (error 81053); results are paginated with
result_info.total_pages; tunnel configurations carry a version.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/client/v4"


class Store:
    def __init__(self, seed: dict):
        self.lock = threading.Lock()
        self.accounts = {}      # account id -> {"token", "zones": [zone ids]}
        self.zones = {}         # zone id -> {"id", "name", "account"}
        self.records = {}       # record id -> record (with "zone_id")
        self.tunnels = {}       # tunnel id -> {"id", "name", "account", "secret", "config", "version"}
        self.failures = []      # [{"method", "path", "status", "count"}]
        self.log = []
        for acct, info in (seed.get("accounts") or {}).items():
            self.accounts[acct] = {"token": info["token"], "zones": []}
            for name in info.get("zones", []):
                zid = uuid.uuid4().hex
                self.zones[zid] = {"id": zid, "name": name, "account": acct, "status": "active"}
                self.accounts[acct]["zones"].append(zid)
        for rec in seed.get("records") or []:
            self.add_record(self.zone_by_name(rec["zone"])["id"], rec, check=False)

    def zone_by_name(self, name):
        for z in self.zones.values():
            if z["name"] == name:
                return z
        raise KeyError(name)

    def account_for_token(self, token):
        for acct, info in self.accounts.items():
            if info["token"] == token:
                return acct
        return None

    def add_record(self, zone_id, body, check=True):
        rtype = str(body.get("type", "")).upper()
        name = str(body.get("name", "")).lower().rstrip(".")
        zone = self.zones[zone_id]
        if name == "@":
            name = zone["name"]
        elif not (name == zone["name"] or name.endswith("." + zone["name"])):
            name = f"{name}.{zone['name']}"
        if check and rtype in ("A", "AAAA", "CNAME"):
            clash = [r for r in self.records.values() if r["zone_id"] == zone_id and r["name"] == name
                     and r["type"] in ("A", "AAAA", "CNAME")]
            if clash and (rtype == "CNAME" or any(r["type"] == "CNAME" for r in clash)):
                return None, (400, 81053, "An A, AAAA, or CNAME record with that host already exists.")
        rid = uuid.uuid4().hex
        rec = {"id": rid, "zone_id": zone_id, "zone_name": zone["name"], "type": rtype, "name": name,
               "content": str(body.get("content", "")).rstrip(".") if rtype == "CNAME" else str(body.get("content", "")),
               "proxied": bool(body.get("proxied", False)), "ttl": int(body.get("ttl") or 1)}
        for k in ("priority", "comment"):
            if body.get(k) is not None:
                rec[k] = body[k]
        self.records[rid] = rec
        return rec, None


def paginate(items, query):
    per = int(query.get("per_page", 20))
    page = int(query.get("page", 1))
    total_pages = max(1, -(-len(items) // per))
    chunk = items[(page - 1) * per: page * per]
    return {"success": True, "errors": [], "messages": [], "result": chunk,
            "result_info": {"page": page, "per_page": per, "count": len(chunk),
                            "total_count": len(items), "total_pages": total_pages}}


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status, code, message):
            self._send(status, {"success": False, "errors": [{"code": code, "message": message}],
                                "messages": [], "result": None})

        def _ok(self, result):
            self._send(200, {"success": True, "errors": [], "messages": [], "result": result})

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode() or "{}") if raw else {}

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method):
            url = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                body = self._body() if method in ("POST", "PUT") else {}
            except json.JSONDecodeError:
                return self._error(400, 6007, "Malformed JSON in request body")
            with store.lock:
                if url.path.startswith("/_fake/"):
                    return self._control(method, url.path, body)
                store.log.append({"method": method, "path": url.path, "query": url.query})
                for f in store.failures:
                    if f["count"] > 0 and f["method"] == method and re.search(f["path"], url.path):
                        f["count"] -= 1
                        return self._error(int(f["status"]), 10000 + int(f["status"]), "injected failure")
                auth = self.headers.get("Authorization", "")
                acct = store.account_for_token(auth[7:]) if auth.startswith("Bearer ") else None
                if not acct:
                    return self._error(400, 6003, "Invalid request headers")
                if not url.path.startswith(PREFIX):
                    return self._error(404, 7000, "No route for that URI")
                return self._api(method, url.path[len(PREFIX):], query, body, acct)

        def _control(self, method, path, body):
            if path == "/_fake/state" and method == "GET":
                return self._send(200, {"zones": list(store.zones.values()),
                                        "records": sorted(store.records.values(), key=lambda r: (r["name"], r["type"])),
                                        "tunnels": {t: {k: v for k, v in d.items() if k != "secret"}
                                                    for t, d in store.tunnels.items()},
                                        "log": store.log})
            if path == "/_fake/records" and method == "POST":
                rec, err = store.add_record(store.zone_by_name(body.pop("zone"))["id"], body, check=False)
                return self._send(200, rec)
            if path == "/_fake/fail" and method == "POST":
                store.failures.append({"method": body["method"], "path": body["path"],
                                       "status": int(body.get("status", 500)), "count": int(body.get("count", 1))})
                return self._send(200, {"ok": True})
            if path == "/_fake/reset-log" and method == "POST":
                store.log.clear()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "no such control endpoint"})

        def _api(self, method, path, query, body, acct):
            parts = [p for p in path.split("/") if p]
            if parts == ["user", "tokens", "verify"]:
                return self._ok({"id": "tok", "status": "active"})
            if parts == ["zones"] and method == "GET":
                zones = [z for z in store.zones.values() if z["account"] == acct]
                if query.get("account.id") and query["account.id"] != acct:
                    zones = []
                if query.get("name"):
                    zones = [z for z in zones if z["name"] == query["name"]]
                out = [{"id": z["id"], "name": z["name"], "status": "active", "account": {"id": z["account"]}}
                       for z in sorted(zones, key=lambda z: z["name"])]
                return self._send(200, paginate(out, query))
            if len(parts) >= 3 and parts[0] == "zones" and parts[2] == "dns_records":
                zone = store.zones.get(parts[1])
                if not zone or zone["account"] != acct:
                    return self._error(403, 9109, "Unauthorized to access requested resource")
                if len(parts) == 3 and method == "GET":
                    recs = [r for r in store.records.values() if r["zone_id"] == zone["id"]]
                    if query.get("name"):
                        recs = [r for r in recs if r["name"] == query["name"].lower().rstrip(".")]
                    if query.get("type"):
                        recs = [r for r in recs if r["type"] == query["type"].upper()]
                    return self._send(200, paginate(sorted(recs, key=lambda r: (r["name"], r["type"], r["id"])), query))
                if len(parts) == 3 and method == "POST":
                    rec, err = store.add_record(zone["id"], body)
                    if err:
                        return self._error(*err)
                    return self._ok(rec)
                if len(parts) == 4 and method == "DELETE":
                    if parts[3] not in store.records or store.records[parts[3]]["zone_id"] != zone["id"]:
                        return self._error(404, 81044, "Record does not exist.")
                    del store.records[parts[3]]
                    return self._ok({"id": parts[3]})
            if len(parts) >= 3 and parts[0] == "accounts" and parts[2] == "cfd_tunnel":
                if parts[1] != acct:
                    return self._error(403, 10000, "Authentication error")
                if len(parts) == 3 and method == "GET":
                    ts = [t for t in store.tunnels.values() if t["account"] == acct]
                    if query.get("name"):
                        ts = [t for t in ts if t["name"] == query["name"]]
                    return self._send(200, paginate([self._tunnel(t) for t in ts], query))
                if len(parts) == 3 and method == "POST":
                    if any(t["name"] == body.get("name") and t["account"] == acct for t in store.tunnels.values()):
                        return self._error(409, 1013, "You already have a tunnel with this name")
                    tid = str(uuid.uuid4())
                    store.tunnels[tid] = {"id": tid, "name": body.get("name"), "account": acct,
                                          "secret": body.get("tunnel_secret") or base64.b64encode(secrets.token_bytes(32)).decode(),
                                          "config": {"ingress": [{"service": "http_status:404"}]}, "version": 0}
                    return self._ok(self._tunnel(store.tunnels[tid]))
                tunnel = store.tunnels.get(parts[3]) if len(parts) > 3 else None
                if not tunnel or tunnel["account"] != acct:
                    return self._error(404, 1003, "Tunnel not found")
                if len(parts) == 4 and method == "GET":
                    return self._ok(self._tunnel(tunnel))
                if len(parts) == 5 and parts[4] == "token" and method == "GET":
                    tok = {"a": acct, "t": tunnel["id"], "s": tunnel["secret"]}
                    return self._ok(base64.b64encode(json.dumps(tok).encode()).decode())
                if len(parts) == 5 and parts[4] == "configurations":
                    if method == "GET":
                        return self._ok({"tunnel_id": tunnel["id"], "version": tunnel["version"],
                                         "config": tunnel["config"], "source": "cloudflare"})
                    if method == "PUT":
                        ingress = (body.get("config") or {}).get("ingress") or []
                        if not ingress or "hostname" in ingress[-1]:
                            return self._error(400, 1056, "The last ingress rule must match all URLs")
                        tunnel["config"] = body["config"]
                        tunnel["version"] += 1
                        return self._ok({"tunnel_id": tunnel["id"], "version": tunnel["version"],
                                         "config": tunnel["config"]})
            return self._error(404, 7000, f"No route for {method} {path}")

        @staticmethod
        def _tunnel(t):
            return {"id": t["id"], "name": t["name"], "status": "inactive", "connections": [],
                    "config_src": "cloudflare", "account_tag": t["account"]}

    return Handler


def serve(port: int, seed: dict, bind: str = "0.0.0.0") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), make_handler(Store(seed)))
    srv.daemon_threads = True
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8999)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--seed", required=True)
    args = ap.parse_args()
    with open(args.seed, encoding="utf-8") as fh:
        seed = json.load(fh)
    srv = serve(args.port, seed, args.bind)
    print(f"fake Cloudflare API on {args.bind}:{args.port}{PREFIX}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
