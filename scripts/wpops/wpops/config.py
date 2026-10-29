"""Configuration: repo paths, .env secrets, node config and the site inventory.

Files (all relative to the repo root, which is the directory holding
docker-compose.yml):

  .env                     secrets: CF_ACCOUNT_n_*, SMTP_*, WPOPS_* (never returned by the API)
  config/wpops.json        this node: name, agent bind/port, backup schedule, retention, notify
  config/inventory.json    origins and sites (per node, gitignored; same file on every node)
  state/                   overrides.json, jobs/, audit.log, token hash, schedule state
  backups/<source>/<set>/  pulled backup sets
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_DIR = Path(os.environ.get("WPOPS_REPO_DIR") or Path(__file__).resolve().parents[3])
CONFIG_DIR = REPO_DIR / "config"
STATE_DIR = REPO_DIR / "state"
BACKUPS_DIR = REPO_DIR / "backups"
ENV_FILE = REPO_DIR / ".env"
NODE_CONFIG_FILE = CONFIG_DIR / "wpops.json"
INVENTORY_FILE = CONFIG_DIR / "inventory.json"



class ConfigError(Exception):
    pass


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse a docker-compose style .env (KEY=VALUE, optional quotes, # comments)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


# Cloudflare API base URL. CF_API_URL (environment, else .env) points wpops at
# a test double such as tests/e2e/fake_cloudflare.py; leave it unset in production.
CF_API = (os.environ.get("CF_API_URL") or load_env().get("CF_API_URL")
          or "https://api.cloudflare.com/client/v4").rstrip("/")


@dataclass
class CfAccount:
    index: int
    name: str
    account_id: str
    token: str
    standby: bool = False
    domains: list[str] = field(default_factory=list)


def cf_accounts(env: dict[str, str] | None = None) -> dict[str, CfAccount]:
    """CF_ACCOUNT_n_* from .env, keyed by account name."""
    env = env if env is not None else load_env()
    out: dict[str, CfAccount] = {}
    n = 1
    while env.get(f"CF_ACCOUNT_{n}_NAME"):
        name = env[f"CF_ACCOUNT_{n}_NAME"]
        out[name] = CfAccount(
            index=n,
            name=name,
            account_id=env.get(f"CF_ACCOUNT_{n}_ID", ""),
            token=env.get(f"CF_ACCOUNT_{n}_TOKEN", ""),
            standby=env.get(f"CF_ACCOUNT_{n}_STANDBY", "false").lower() == "true",
            domains=[d.strip() for d in env.get(f"CF_ACCOUNT_{n}_DOMAINS", "").split(",") if d.strip()],
        )
        n += 1
    return out


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc


NODE_DEFAULTS: dict[str, Any] = {
    "node": "",                      # this node's origin name in the inventory (required)
    "agent": {
        "bind": ["127.0.0.1"],       # extra addresses are added at start (docker gateway)
        "port": 8787,
        "public_url": "",            # e.g. https://ops.standby.example.net (informational)
        "rate_limit_per_min": 120,
        # CF-Connecting-IP is believed only from these peers (CIDRs), e.g. the
        # docker network cloudflared sits on. Empty = always use the TCP peer.
        "trusted_proxies": [],
    },
    "backup": {
        "schedule": "weekly:sun:03:30",   # weekly:<day>:<HH:MM> | daily:<HH:MM> | off
        "retain_sets": 4,                 # per source, plus any set marked keep
        "sources": [],                    # names of origins with sync_api to pull from; [] = all
        "pull_timeout_min": 180,
        "max_set_gb": 50,
        "delete_on_source": True,         # remove a set we created on the source once verified here (or if the pull fails)                 # refuse to pull a set larger than this, whatever the source claims
        # File entities every pull asks the source for (plus the database) so the
        # set is complete regardless of the source's own SiteGuard settings.
        # Subset of plugins, themes, uploads, others, wpcore; [] = source defaults.
        "entities": ["plugins", "themes", "uploads", "others"],
        "auto_apply": False,              # never applied automatically unless true (per design)
    },
    "disk": {
        "min_free_gb": 10,                # alert + refuse pulls/restores below this much free space
        "min_free_pct": 10,               # ... or below this percent free
        "paths": [],                      # extra paths to watch besides backups/ and each stack's data/
    },
    "notify": {
        "email_to": [],                   # [] = e-mail disabled on the node (the bot still e-mails)
        "email_from": "",
        "webhook_url": "",                # optional: POST JSON events here (the bot's /wpops/events)
    },
    "failover": {
        "default_to": "",                 # origin to use when a caller does not say --to
        "tunnel_service": "http://nginx:80",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def node_config() -> dict[str, Any]:
    cfg = _merge(NODE_DEFAULTS, _read_json(NODE_CONFIG_FILE, {}))
    if not cfg.get("node"):
        raise ConfigError(f"{NODE_CONFIG_FILE}: 'node' (this node's origin name) is required")
    return cfg


def save_node_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NODE_CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, NODE_CONFIG_FILE)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


@dataclass
class Origin:
    name: str
    kind: str                                   # external | node
    targets: list[dict[str, str]] = field(default_factory=list)   # external: DNS records that mean "here"
    sync_api: str = ""                          # SiteGuard sync API base URL (https://site)
    agent: str = ""                             # node: agent URL for the bot
    standby_domain: str = ""                    # node
    stacks: dict[str, dict[str, Any]] = field(default_factory=dict)   # node: name -> {path, mode, service}
    tunnels: dict[str, str] = field(default_factory=dict)             # node: cf account name -> tunnel id

    def tunnel_target(self, account: str) -> str | None:
        tid = self.tunnels.get(account)
        return f"{tid}.cfargotunnel.com" if tid else None

    def matches_record(self, rec: dict[str, Any]) -> bool:
        """Does this DNS record point at this origin?"""
        rtype = rec.get("type", "").upper()
        content = rec.get("content", "").rstrip(".").lower()
        if self.kind == "node":
            return rtype == "CNAME" and content.endswith(".cfargotunnel.com") and \
                content.split(".")[0] in {t.lower() for t in self.tunnels.values()}
        for t in self.targets:
            if t.get("type", "").upper() == rtype and t.get("content", "").rstrip(".").lower() == content:
                return True
        return False


@dataclass
class Site:
    slug: str
    name: str
    primary: str
    cf_account: str
    domains: list[str]
    hosts: dict[str, dict[str, Any]]            # origin -> {host, stack?, blog_id?}
    failover_to: str = ""                       # preferred node for this site (optional)
    enabled: bool = True

    def host_on(self, origin: str) -> dict[str, Any] | None:
        return self.hosts.get(origin)


@dataclass
class BackupSource:
    name: str
    origin: str
    sync_api: str
    stack: str = ""


@dataclass
class Inventory:
    origins: dict[str, Origin]
    sites: list[Site]
    raw: dict[str, Any]
    backup_sources: dict[str, BackupSource] = field(default_factory=dict)

    def sources(self) -> dict[str, BackupSource]:
        sources = {n: BackupSource(n, n, o.sync_api) for n, o in self.origins.items() if o.sync_api}
        sources.update(self.backup_sources)
        return sources

    def site(self, slug: str) -> Site:
        for s in self.sites:
            if s.slug == slug:
                return s
        raise ConfigError(f"unknown site '{slug}'")

    def origin(self, name: str) -> Origin:
        try:
            return self.origins[name]
        except KeyError:
            raise ConfigError(f"unknown origin '{name}'") from None

    def site_for_domain(self, domain: str) -> Site | None:
        d = domain.lower().rstrip(".")
        for s in self.sites:
            if d in [x.lower() for x in s.domains]:
                return s
        return None

    def nodes(self) -> list[Origin]:
        return [o for o in self.origins.values() if o.kind == "node"]


def _checked_sync_api(origin: str, url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(f"origin {origin}: sync_api must be https:// (got {url}); the sync key travels in a header")
    return url


def _validate_inventory(inv: Inventory) -> None:
    domains: set[str] = set()
    single_sites: set[tuple[str, str]] = set()
    for o in inv.origins.values():
        if o.kind not in ("node", "external"):
            raise ConfigError(f"origin '{o.name}': kind must be node or external")
        for name, st in o.stacks.items():
            if not _SLUG_RE.fullmatch(name) or st.get("mode", "multisite") not in ("single", "multisite"):
                raise ConfigError(f"origin '{o.name}': invalid stack name or mode: {name}")
            if not st.get("path") or not Path(st["path"]).is_absolute():
                raise ConfigError(f"stack '{name}': path must be absolute")
    for name, src in inv.backup_sources.items():
        if not _SLUG_RE.fullmatch(name) or name in inv.origins:
            raise ConfigError(f"backup source '{name}': use a unique slug distinct from origins")
        origin = inv.origin(src.origin)
        if src.stack and src.stack not in origin.stacks:
            raise ConfigError(f"backup source '{name}': unknown stack '{src.stack}'")
        if not src.sync_api:
            raise ConfigError(f"backup source '{name}': sync_api is required")
    seen: set[str] = set()
    for s in inv.sites:
        if not _SLUG_RE.match(s.slug):
            raise ConfigError(f"site slug '{s.slug}' is invalid")
        if s.slug in seen:
            raise ConfigError(f"duplicate site slug '{s.slug}'")
        seen.add(s.slug)
        if s.primary not in inv.origins:
            raise ConfigError(f"site '{s.slug}': primary origin '{s.primary}' is not defined")
        if s.failover_to and s.failover_to not in inv.origins:
            raise ConfigError(f"site '{s.slug}': failover_to origin '{s.failover_to}' is not defined")
        for d in s.domains:
            if not _HOST_RE.match(d.lower()):
                raise ConfigError(f"site '{s.slug}': domain '{d}' is invalid")
            if d in domains:
                raise ConfigError(f"domain '{d}' belongs to multiple sites")
            domains.add(d)
        for oname, h in s.hosts.items():
            if oname not in inv.origins:
                raise ConfigError(f"site '{s.slug}': hosts.{oname} is not a defined origin")
            o = inv.origins[oname]
            if o.kind == "node":
                stack = h.get("stack")
                if not stack or stack not in o.stacks:
                    raise ConfigError(f"site '{s.slug}': hosts.{oname}.stack '{stack}' is not a stack of node '{oname}'")
                if o.stacks[stack].get("mode", "multisite") == "multisite" and not h.get("blog_id"):
                    raise ConfigError(f"site '{s.slug}': hosts.{oname} needs blog_id (multisite stack)")
                if o.stacks[stack].get("mode", "multisite") == "single":
                    key = (oname, stack)
                    if key in single_sites:
                        raise ConfigError(f"single-site stack '{oname}/{stack}' has multiple sites")
                    single_sites.add(key)
                    if h.get("blog_id") not in (None, 1):
                        raise ConfigError(f"single-site stack '{stack}': blog_id must be omitted or 1")
                elif isinstance(h.get("blog_id"), bool) or not isinstance(h.get("blog_id"), int) or h['blog_id'] < 1:
                    raise ConfigError(f"site '{s.slug}': blog_id must be a positive integer")
            if not h.get("host"):
                raise ConfigError(f"site '{s.slug}': hosts.{oname}.host is required")
            if not _HOST_RE.fullmatch(h['host'].lower()):
                raise ConfigError(f"site '{s.slug}': invalid host '{h['host']}'")


def load_inventory(path: Path = INVENTORY_FILE) -> Inventory:
    raw = _read_json(path, None)
    if raw is None:
        raise ConfigError(f"{path} not found; see config/inventory.example.json")
    origins: dict[str, Origin] = {}
    for name, o in (raw.get("origins") or {}).items():
        origins[name] = Origin(
            name=name,
            kind=o.get("kind", "external"),
            targets=o.get("targets", []),
            sync_api=_checked_sync_api(name, o.get("sync_api", "")),
            agent=o.get("agent", "").rstrip("/"),
            standby_domain=o.get("standby_domain", ""),
            stacks=o.get("stacks", {}),
            tunnels=o.get("tunnels", {}),
        )
    sites = [
        Site(
            slug=s["slug"],
            name=s.get("name", s["slug"]),
            primary=s["primary"],
            cf_account=s["cf_account"],
            domains=[d.lower().rstrip(".") for d in s.get("domains", [])],
            hosts=s.get("hosts", {}),
            failover_to=s.get("failover_to", ""),
            enabled=s.get("enabled", True),
        )
        for s in raw.get("sites", [])
    ]
    sources = {name: BackupSource(name, src['origin'], _checked_sync_api(name, src.get('sync_api', '')),
                                  src.get('stack', '')) for name, src in raw.get('backup_sources', {}).items()}
    inv = Inventory(origins=origins, sites=sites, raw=raw, backup_sources=sources)
    _validate_inventory(inv)
    return inv


def public_inventory(inv: Inventory) -> dict[str, Any]:
    """Inventory as returned by the API (no secrets are in it, but keep it explicit)."""
    return {
        "origins": {
            n: {
                "kind": o.kind, "targets": o.targets, "sync_api": bool(o.sync_api), "agent": o.agent,
                "standby_domain": o.standby_domain, "stacks": o.stacks, "tunnels": o.tunnels,
            } for n, o in inv.origins.items()
        },
        "backup_sources": {n: {"origin": src.origin, "stack": src.stack, "sync_api": bool(src.sync_api)}
                           for n, src in inv.sources().items()},
        "sites": [
            {
                "slug": s.slug, "name": s.name, "primary": s.primary, "cf_account": s.cf_account,
                "domains": s.domains, "hosts": s.hosts, "failover_to": s.failover_to, "enabled": s.enabled,
            } for s in inv.sites
        ],
    }
