"""Cloudflare API client (urllib only) plus zone/tunnel backups.

Mirrors scripts/cf-common.sh: DNS records are replaced (delete + create) rather
than edited so the original record can be restored exactly; tunnel ingress is
merged, never replaced, so unrelated rules (the standby wildcard) survive.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import CF_API, REPO_DIR

CF_BACKUP_DIR = REPO_DIR / "data" / "cloudflare" / "backups"


class CfError(Exception):
    pass


class Cloudflare:
    def __init__(self, token: str, account_id: str = "", account_name: str = ""):
        self.token = token
        self.account_id = account_id
        self.account_name = account_name
        self._zone_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, body: Any | None = None, retries: int = 3) -> dict[str, Any]:
        url = CF_API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "wpops/1",
        })
        last: Exception | None = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    payload = json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    last = exc
                    continue
                try:
                    msgs = "; ".join(e.get("message", "") for e in json.loads(text).get("errors", []))
                except Exception:
                    msgs = text[:200]
                raise CfError(f"CF {method} {path} -> HTTP {exc.code}: {msgs}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CfError(f"CF {method} {path} -> {exc}") from None
            if payload.get("success") is False:
                msgs = "; ".join(e.get("message", "") for e in payload.get("errors", []))
                raise CfError(f"CF {method} {path} -> {msgs or 'unknown error'}")
            return payload
        raise CfError(f"CF {method} {path} -> {last}")

    def paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            q = dict(params, per_page=100, page=page)
            payload = self.request("GET", f"{path}?{urllib.parse.urlencode(q)}")
            out.extend(payload.get("result") or [])
            info = payload.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                return out
            page += 1

    # ----------------------------------------------------------------- zones
    def zones(self) -> list[dict[str, Any]]:
        params = {"status": "active"}
        if self.account_id:
            params["account.id"] = self.account_id
        return self.paged("/zones", params)

    def zone_id(self, domain: str) -> str:
        """Zone for a hostname: the longest registered zone that is a suffix of it."""
        d = domain.lower().rstrip(".")
        if d in self._zone_cache:
            return self._zone_cache[d]
        if not self._zone_cache:
            for z in self.zones():
                self._zone_cache[z["name"].lower()] = z["id"]
        parts = d.split(".")
        for i in range(len(parts) - 1):
            cand = ".".join(parts[i:])
            if cand in self._zone_cache:
                self._zone_cache[d] = self._zone_cache[cand]
                return self._zone_cache[cand]
        raise CfError(f"no zone in account '{self.account_name or self.account_id}' for {domain}")

    def dns_records(self, zone_id: str, name: str | None = None, rtype: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if name:
            params["name"] = name
        if rtype:
            params["type"] = rtype
        return self.paged(f"/zones/{zone_id}/dns_records", params)

    def create_record(self, zone_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        body = {
            "type": rec["type"], "name": rec["name"], "content": rec["content"],
            "proxied": bool(rec.get("proxied", True)), "ttl": int(rec.get("ttl") or 1),
        }
        if rec.get("priority") is not None:
            body["priority"] = rec["priority"]
        if rec.get("comment"):
            body["comment"] = rec["comment"]
        return self.request("POST", f"/zones/{zone_id}/dns_records", body)["result"]

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self.request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    # --------------------------------------------------------------- tunnels
    def tunnels(self) -> list[dict[str, Any]]:
        return self.paged(f"/accounts/{self.account_id}/cfd_tunnel", {"is_deleted": "false"})

    def tunnel(self, tunnel_id: str) -> dict[str, Any]:
        return self.request("GET", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}")["result"]

    def tunnel_by_name(self, name: str) -> dict[str, Any] | None:
        for t in self.tunnels():
            if t.get("name") == name:
                return t
        return None

    def tunnel_config(self, tunnel_id: str) -> dict[str, Any]:
        return self.request("GET", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations")["result"]

    def tunnel_ingress(self, tunnel_id: str) -> list[dict[str, Any]]:
        return (self.tunnel_config(tunnel_id).get("config") or {}).get("ingress") or []

    def set_tunnel_ingress(self, tunnel_id: str, ingress: list[dict[str, Any]]) -> None:
        if not ingress or "hostname" in ingress[-1]:
            ingress = ingress + [{"service": "http_status:404"}]
        self.request("PUT", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations",
                     {"config": {"ingress": ingress}})

    def merge_tunnel_ingress(self, tunnel_id: str, rules: list[dict[str, Any]]) -> None:
        """Add/update the given hostname rules, keep everything else, keep the catch-all last."""
        existing = self.tunnel_ingress(tunnel_id)
        new_hosts = {r["hostname"].lower() for r in rules}
        keep = [r for r in existing if r.get("hostname") and r["hostname"].lower() not in new_hosts]
        # cloudflared takes the first matching rule: exact hostnames must sit
        # before any wildcard (e.g. *.standby.example.net) or they are never reached.
        exact = [r for r in keep if not r["hostname"].startswith("*")]
        wild = [r for r in keep if r["hostname"].startswith("*")]
        catchall = [r for r in existing if not r.get("hostname")] or [{"service": "http_status:404"}]
        self.set_tunnel_ingress(tunnel_id, exact + rules + wild + catchall[-1:])

    def remove_tunnel_ingress_hosts(self, tunnel_id: str, hosts: list[str]) -> None:
        rm = {h.lower() for h in hosts}
        existing = self.tunnel_ingress(tunnel_id)
        remaining = [r for r in existing if not r.get("hostname") or r["hostname"].lower() not in rm]
        self.set_tunnel_ingress(tunnel_id, remaining)

    # --------------------------------------------------------------- backups
    def backup_zone(self, domain: str) -> Path:
        """Snapshot every record of the zone (same layout as scripts/cf-backup.sh)."""
        zone_id = self.zone_id(domain)
        zone_name = next((n for n, zid in self._zone_cache.items() if zid == zone_id and "." in n and not n.startswith(domain)), None)
        name = zone_name or domain
        d = CF_BACKUP_DIR / "dns" / name
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:
            pass
        path = d / (time.strftime("%Y-%m-%d_%H%M%S") + ".json")
        path.write_text(json.dumps(self.dns_records(zone_id), indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def backup_tunnel(self, tunnel_id: str) -> Path:
        d = CF_BACKUP_DIR / "tunnels" / (self.account_name or self.account_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / (time.strftime("%Y-%m-%d_%H%M%S") + ".json")
        path.write_text(json.dumps(self.tunnel_config(tunnel_id), indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path
