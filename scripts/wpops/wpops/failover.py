"""Failover: move a site's production domains between origins, and back.

Model
-----
A site's production domains normally point at its *primary* origin. A
failover points some or all of them at another origin (a node: CNAME to that
node's Cloudflare tunnel for the site's account). Every domain that is moved
gets an *override* record in state/overrides.json holding the exact original
DNS records, written before DNS is touched, so rollback restores them
verbatim and a crash mid-way leaves a resumable, inspectable state.

Nodes serve production hostnames through the host-alias plugin (see
plugin/kc-host-alias): the agent writes <stack>/data/wordpress/wp-content/
wpops-host-aliases.json mapping each production hostname to the stack's
blog/host, so no database change is needed in either direction.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import state
from .cf import CfError, Cloudflare
from .config import ConfigError, Inventory, Origin, Site, cf_accounts, load_env, node_config

SWITCHABLE_TYPES = {"A", "AAAA", "CNAME"}


class FailoverError(Exception):
    pass


@dataclass
class Change:
    site: str
    domain: str
    cf_account: str
    zone_id: str
    from_origin: str            # what the records point at now ("" = unknown/other)
    to_origin: str
    stack: str
    service: str                # tunnel ingress service for this hostname
    tunnel_id: str
    original_records: list[dict[str, Any]]
    create: dict[str, Any]
    action: str = "switch"      # switch | noop | refuse
    reason: str = ""


@dataclass
class Plan:
    to_origin: str
    changes: list[Change] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    noop: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "to": self.to_origin,
            "changes": [asdict(c) for c in self.changes],
            "refused": self.refused,
            "noop": self.noop,
            "summary": {"switch": len(self.changes), "refused": len(self.refused), "noop": len(self.noop)},
        }


def _cf_for(account_name: str, accounts: dict) -> Cloudflare:
    acct = accounts.get(account_name)
    if not acct or not acct.token:
        raise FailoverError(f"Cloudflare account '{account_name}' is not configured in .env")
    return Cloudflare(acct.token, acct.account_id, acct.name)


def _record_origin(inv: Inventory, recs: list[dict[str, Any]]) -> str:
    for o in inv.origins.values():
        if any(o.matches_record(r) for r in recs):
            return o.name
    return ""


def _public_record(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "type", "name", "content", "proxied", "ttl", "priority", "comment")
    return {k: r[k] for k in keep if k in r}


def resolve_selection(inv: Inventory, sites: list[str] | None, all_sites: bool) -> list[Site]:
    if all_sites:
        return [s for s in inv.sites if s.enabled]
    if not sites:
        raise FailoverError("no sites selected")
    out = []
    for slug in sites:
        s = inv.site(slug)
        out.append(s)
    return out


def target_for(inv: Inventory, site: Site, to: str | None, cfg: dict[str, Any]) -> str:
    to = to or site.failover_to or cfg["failover"].get("default_to") or ""
    if not to:
        nodes = [o for o in inv.nodes() if o.name != site.primary]
        if len(nodes) == 1:
            to = nodes[0].name
    if not to:
        raise FailoverError(f"site '{site.slug}': no target origin given and none configured")
    return to


def plan(inv: Inventory, sites: list[Site], to: str | None = None, stack: str | None = None,
         cfg: dict[str, Any] | None = None, accounts: dict | None = None) -> Plan:
    """Compute DNS changes to point the selected sites at `to`. No side effects."""
    cfg = cfg or node_config()
    accounts = accounts if accounts is not None else cf_accounts(load_env())
    clients: dict[str, Cloudflare] = {}
    first_target = None
    p = Plan(to_origin="")
    for site in sites:
        try:
            target = target_for(inv, site, to, cfg)
            first_target = first_target or target
            p.to_origin = p.to_origin or target
            origin = inv.origin(target)
            if not site.enabled:
                raise FailoverError("site is disabled in the inventory")
            host = site.host_on(target)
            if not host:
                raise FailoverError(f"site has no host entry for origin '{target}'")
            if origin.kind != "node":
                raise FailoverError(f"origin '{target}' is external; the agent can only switch to a node")
            tunnel_id = origin.tunnels.get(site.cf_account)
            if not tunnel_id:
                raise FailoverError(f"node '{target}' has no tunnel for Cloudflare account '{site.cf_account}' (run cf-setup.sh there)")
            stack_name = stack or host.get("stack") or ""
            stack_cfg = origin.stacks.get(stack_name)
            if not stack_cfg:
                raise FailoverError(f"stack '{stack_name}' is not defined on node '{target}'")
            service = stack_cfg.get("service") or cfg["failover"]["tunnel_service"]
            cf = clients.setdefault(site.cf_account, _cf_for(site.cf_account, accounts))
            for domain in site.domains:
                try:
                    zone_id = cf.zone_id(domain)
                    recs = [r for r in cf.dns_records(zone_id, name=domain) if r.get("type") in SWITCHABLE_TYPES]
                except CfError as exc:
                    p.refused.append({"site": site.slug, "domain": domain, "reason": str(exc)})
                    continue
                current = _record_origin(inv, recs)
                if current == target:
                    p.noop.append({"site": site.slug, "domain": domain, "reason": f"already on {target}"})
                    continue
                if not recs:
                    p.refused.append({"site": site.slug, "domain": domain, "reason": "no A/AAAA/CNAME record exists; nothing to switch"})
                    continue
                if not current:
                    p.refused.append({"site": site.slug, "domain": domain,
                                      "reason": "record points somewhere not in the inventory: " +
                                                ", ".join(f"{r['type']} {r['content']}" for r in recs)})
                    continue
                proxied = any(r.get("proxied") for r in recs)
                p.changes.append(Change(
                    site=site.slug, domain=domain, cf_account=site.cf_account, zone_id=zone_id,
                    from_origin=current, to_origin=target, stack=stack_name, service=service,
                    tunnel_id=tunnel_id, original_records=[_public_record(r) for r in recs],
                    create={"type": "CNAME", "name": domain, "content": f"{tunnel_id}.cfargotunnel.com",
                            "proxied": True if not recs else proxied, "ttl": 1,
                            "comment": f"wpops failover from {current}"},
                ))
        except (FailoverError, ConfigError) as exc:
            p.refused.append({"site": site.slug, "domain": "*", "reason": str(exc)})
    return p


# ---------------------------------------------------------------- aliases

def alias_file(origin: Origin, stack: str) -> Path:
    path = Path(origin.stacks[stack]["path"])
    return path / "data" / "wordpress" / "wp-content" / "wpops-host-aliases.json"


def write_aliases(inv: Inventory, this_node: str) -> dict[str, dict[str, Any]]:
    """Rebuild the host-alias maps for every stack on this node from the overrides."""
    origin = inv.origin(this_node)
    per_stack: dict[str, dict[str, Any]] = {name: {} for name in origin.stacks}
    for domain, ov in state.overrides().items():
        if ov.get("to") != this_node:
            continue
        try:
            site = inv.site(ov["site"])
        except ConfigError:
            continue
        host = site.host_on(this_node) or {}
        st = ov.get("stack") or host.get("stack") or ""
        if st not in per_stack:
            continue
        entry: dict[str, Any] = {"site": site.slug, "host": host.get("host", "")}
        if host.get("blog_id"):
            entry["blog_id"] = int(host["blog_id"])
        per_stack[st][domain] = entry
    for st, aliases in per_stack.items():
        f = alias_file(origin, st)
        if not f.parent.exists():
            continue
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"generated": int(time.time()), "aliases": aliases}, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(f)
        try:
            f.chmod(0o644)
        except OSError:
            pass
    return per_stack


# ------------------------------------------------------------------ apply

def apply(inv: Inventory, p: Plan, actor: str, this_node: str, accounts: dict | None = None,
          log=print) -> dict[str, Any]:
    """Execute a plan. Idempotent: domains already overridden are skipped."""
    accounts = accounts if accounts is not None else cf_accounts(load_env())
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    with state.locked("failover"):
        ovs = state.overrides()
        clients: dict[str, Cloudflare] = {}
        backed_zones: set[str] = set()
        backed_tunnels: set[str] = set()
        ingress: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for c in p.changes:
            cf = clients.setdefault(c.cf_account, _cf_for(c.cf_account, accounts))
            if c.domain in ovs and ovs[c.domain].get("status") == "active":
                done.append({"domain": c.domain, "status": "already-active"})
                continue
            try:
                if c.zone_id not in backed_zones:
                    cf.backup_zone(c.domain)
                    backed_zones.add(c.zone_id)
                if c.tunnel_id not in backed_tunnels:
                    cf.backup_tunnel(c.tunnel_id)
                    backed_tunnels.add(c.tunnel_id)
                # 1. record the intent + originals BEFORE touching DNS
                ovs[c.domain] = {
                    "site": c.site, "from": c.from_origin, "to": c.to_origin, "stack": c.stack,
                    "cf_account": c.cf_account, "zone_id": c.zone_id, "tunnel_id": c.tunnel_id,
                    "original_records": c.original_records, "switched_at": int(time.time()),
                    "actor": actor, "status": "switching",
                }
                state.save_overrides(ovs)
                # 2. replace records
                for r in c.original_records:
                    cf.delete_record(c.zone_id, r["id"])
                created = cf.create_record(c.zone_id, c.create)
                ovs[c.domain]["record_id"] = created.get("id")
                ovs[c.domain]["status"] = "active"
                state.save_overrides(ovs)
                ingress.setdefault((c.cf_account, c.tunnel_id), []).append({"hostname": c.domain, "service": c.service})
                done.append({"domain": c.domain, "status": "switched", "from": c.from_origin, "to": c.to_origin})
                log(f"switched {c.domain}: {c.from_origin} -> {c.to_origin}")
            except (CfError, Exception) as exc:  # noqa: BLE001 - keep going, report
                ovs[c.domain]["status"] = "failed"
                ovs[c.domain]["error"] = str(exc)
                state.save_overrides(ovs)
                failed.append({"domain": c.domain, "error": str(exc)})
                log(f"FAILED {c.domain}: {exc}")
        # 3. tunnel ingress (merge, per tunnel)
        for (acct, tid), rules in ingress.items():
            try:
                clients[acct].merge_tunnel_ingress(tid, rules)
                log(f"tunnel {tid}: ingress +{len(rules)} host(s)")
            except CfError as exc:
                failed.append({"tunnel": tid, "error": f"ingress update failed: {exc}"})
        # 4. host aliases on this node
        try:
            write_aliases(inv, this_node)
        except Exception as exc:  # noqa: BLE001
            failed.append({"aliases": this_node, "error": str(exc)})
    state.audit(actor, "failover.apply", {"to": p.to_origin, "done": done, "failed": failed})
    return {"done": done, "failed": failed, "overrides": state.overrides()}


def rollback(inv: Inventory, actor: str, this_node: str, sites: list[str] | None = None,
             all_sites: bool = False, domains: list[str] | None = None, accounts: dict | None = None,
             log=print) -> dict[str, Any]:
    """Restore the original records for the selected overrides."""
    accounts = accounts if accounts is not None else cf_accounts(load_env())
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    with state.locked("failover"):
        ovs = state.overrides()
        wanted = {d for d in domains or []}
        if sites:
            wanted |= {d for d, ov in ovs.items() if ov.get("site") in sites}
        if all_sites:
            wanted = set(ovs)
        if not wanted:
            return {"done": [], "failed": [], "overrides": ovs, "message": "nothing to roll back"}
        clients: dict[str, Cloudflare] = {}
        removed: dict[tuple[str, str], list[str]] = {}
        for domain in sorted(wanted):
            ov = ovs.get(domain)
            if not ov:
                failed.append({"domain": domain, "error": "no override recorded"})
                continue
            try:
                cf = clients.setdefault(ov["cf_account"], _cf_for(ov["cf_account"], accounts))
                ov["status"] = "rolling-back"
                state.save_overrides(ovs)
                # delete whatever currently sits on the name (our CNAME, or anything left half-way)
                for r in cf.dns_records(ov["zone_id"], name=domain):
                    if r.get("type") in SWITCHABLE_TYPES:
                        cf.delete_record(ov["zone_id"], r["id"])
                for r in ov["original_records"]:
                    cf.create_record(ov["zone_id"], r)
                removed.setdefault((ov["cf_account"], ov["tunnel_id"]), []).append(domain)
                del ovs[domain]
                state.save_overrides(ovs)
                done.append({"domain": domain, "status": "restored", "to": ov["from"]})
                log(f"restored {domain} -> {ov['from']}")
            except (CfError, Exception) as exc:  # noqa: BLE001
                ov["status"] = "rollback-failed"
                ov["error"] = str(exc)
                state.save_overrides(ovs)
                failed.append({"domain": domain, "error": str(exc)})
                log(f"FAILED {domain}: {exc}")
        for (acct, tid), hosts in removed.items():
            try:
                clients[acct].remove_tunnel_ingress_hosts(tid, hosts)
                log(f"tunnel {tid}: ingress -{len(hosts)} host(s)")
            except CfError as exc:
                failed.append({"tunnel": tid, "error": f"ingress cleanup failed: {exc}"})
        try:
            write_aliases(inv, this_node)
        except Exception as exc:  # noqa: BLE001
            failed.append({"aliases": this_node, "error": str(exc)})
    state.audit(actor, "failover.rollback", {"done": done, "failed": failed})
    return {"done": done, "failed": failed, "overrides": state.overrides()}


def live_status(inv: Inventory, accounts: dict | None = None) -> list[dict[str, Any]]:
    """Per domain: where DNS points right now (one Cloudflare query per domain)."""
    accounts = accounts if accounts is not None else cf_accounts(load_env())
    clients: dict[str, Cloudflare] = {}
    ovs = state.overrides()
    out = []
    for site in inv.sites:
        try:
            cf = clients.setdefault(site.cf_account, _cf_for(site.cf_account, accounts))
        except FailoverError as exc:
            for d in site.domains:
                out.append({"site": site.slug, "domain": d, "error": str(exc)})
            continue
        for d in site.domains:
            try:
                zone_id = cf.zone_id(d)
                recs = [r for r in cf.dns_records(zone_id, name=d) if r.get("type") in SWITCHABLE_TYPES]
                cur = _record_origin(inv, recs)
                out.append({
                    "site": site.slug, "domain": d, "primary": site.primary, "current": cur or "other",
                    "records": [f"{r['type']} {r['content']}" for r in recs],
                    "override": ovs.get(d, {}).get("status"),
                    "ok": (cur == site.primary) if cur else False,
                })
            except CfError as exc:
                out.append({"site": site.slug, "domain": d, "error": str(exc)})
    return out
