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

from . import notify, state
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
            if stack_name != host.get("stack"):
                raise FailoverError("target stack differs from the site host mapping; update the inventory first")
            stack_cfg = origin.stacks.get(stack_name)
            if not stack_cfg:
                raise FailoverError(f"stack '{stack_name}' is not defined on node '{target}'")
            from .stacks import ingress_service
            service = ingress_service(origin, stack_name, cfg["failover"]["tunnel_service"])
            cf = clients.setdefault(site.cf_account, _cf_for(site.cf_account, accounts))
            for domain in site.domains:
                try:
                    zone_id = cf.zone_id(domain)
                    recs = [r for r in cf.dns_records(zone_id, name=domain) if r.get("type") in SWITCHABLE_TYPES]
                except CfError as exc:
                    p.refused.append({"site": site.slug, "domain": domain, "reason": str(exc)})
                    continue
                existing_override = state.overrides().get(domain, {})
                if (not recs and existing_override.get('to') == target
                        and existing_override.get('status') in ('failed', 'switching', 'preparing')):
                    recs = existing_override.get('original_records', [])
                current = _record_origin(inv, recs)
                sibling = [r for r in recs if r.get("type") == "CNAME" and r.get("content", "").lower().rstrip(".") in site.domains]
                if sibling and len(sibling) == len(recs):
                    # A sibling CNAME still needs ingress and an alias on the node.
                    seen = {domain}
                    follow = sibling[0]['content'].lower().rstrip('.')
                    while follow in site.domains and follow not in seen:
                        seen.add(follow)
                        followed = cf.dns_records(cf.zone_id(follow), name=follow)
                        current = _record_origin(inv, followed)
                        if current:
                            break
                        cnames = [r for r in followed if r.get('type') == 'CNAME']
                        if not cnames:
                            break
                        follow = cnames[0]['content'].lower().rstrip('.')
                existing_override = state.overrides().get(domain, {})
                if current == target and existing_override.get("status") not in ("failed", "switching", "preparing"):
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
                p.changes.append(Change(
                    site=site.slug, domain=domain, cf_account=site.cf_account, zone_id=zone_id,
                    from_origin=current, to_origin=target, stack=stack_name, service=service,
                    tunnel_id=tunnel_id, original_records=[_public_record(r) for r in recs],
                    create={"type": "CNAME", "name": domain, "content": f"{tunnel_id}.cfargotunnel.com",
                            # A tunnel CNAME only works proxied, even when the
                            # original records were DNS-only (restored on rollback).
                            "proxied": True, "ttl": 1,
                            "comment": f"wpops failover from {current}"},
                ))
        except (FailoverError, ConfigError) as exc:
            p.refused.append({"site": site.slug, "domain": "*", "reason": str(exc)})
    return p


def _recreate(cf: Cloudflare, zone_id: str, records: list[dict[str, Any]], domain: str, log) -> None:
    """Best effort: put records back after a failed replacement."""
    for r in records:
        try:
            cf.create_record(zone_id, r)
            log(f"put back {r.get('type')} {domain} -> {r.get('content')}")
        except Exception as exc:  # noqa: BLE001
            log(f"could not put back {r.get('type')} {domain} -> {r.get('content')}: {exc}")


# ---------------------------------------------------------------- aliases

def alias_file(origin: Origin, stack: str) -> Path:
    path = Path(origin.stacks[stack]["path"])
    return path / "data" / "wordpress" / "wp-content" / "wpops-host-aliases.json"


def write_aliases(inv: Inventory, this_node: str) -> dict[str, dict[str, Any]]:
    """Rebuild the host-alias maps for every stack on this node: published names that
    differ from the siteurl host, native domains of sites this node is primary for,
    and failover overrides."""
    origin = inv.origin(this_node)
    per_stack: dict[str, dict[str, Any]] = {name: {} for name in origin.stacks}
    # A node can also be primary. Native www/alternate domains need the same
    # alias handling without creating a temporary failover override.
    def add(st: str, hostname: str, entry: dict[str, Any]) -> None:
        # A hostname that is the stack's own siteurl host needs no alias.
        if hostname.lower() != entry['host']:
            per_stack[st][hostname.lower()] = entry

    for site in inv.sites:
        host = site.host_on(this_node)
        if not host or host.get('stack') not in per_stack:
            continue
        entry = {'site': site.slug, 'host': site.site_host_on(this_node)}
        if host.get('blog_id'):
            entry['blog_id'] = int(host['blog_id'])
        # Published name differs from the siteurl host (a staging name such as
        # shop-node.standby.example for a stack whose siteurl is shop.example):
        # serve it as an alias so the database keeps the production URLs.
        add(host['stack'], host['host'], entry)
        if site.enabled and site.primary == this_node:
            for domain in site.domains:
                add(host['stack'], domain, entry)
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
        entry: dict[str, Any] = {"site": site.slug, "host": site.site_host_on(this_node)}
        if host.get("blog_id"):
            entry["blog_id"] = int(host["blog_id"])
        # All sibling hostnames must work the instant apex DNS is switched.
        for hostname in site.domains:
            add(st, hostname, entry)
    for st, aliases in per_stack.items():
        f = alias_file(origin, st)
        if not f.parent.exists():
            if not aliases:
                continue
            raise FailoverError(f"stack {st} has no wp-content directory; initialise it first")
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"generated": int(time.time()), "aliases": aliases}, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(f)
        try:
            f.chmod(0o644)
        except OSError:
            pass
    # Host names changed: the admin gate lists them (never fails the caller).
    from . import gate
    gate.apply_quietly()
    return per_stack


def _set_cron(inv: Inventory, this_node: str, stacks: set[str], start: bool, log) -> list[str]:
    """Start or stop wp-cron on these stacks when the node is configured for it."""
    from .config import node_config as _nc
    if not stacks or not (_nc().get("failover") or {}).get("cron_follows_traffic"):
        return []
    from .stacks import compose
    origin = inv.origin(this_node)
    changed = []
    for st in sorted(stacks):
        if st not in origin.stacks or not origin.stacks[st].get("managed"):
            continue
        args = ("up", "-d", "--no-deps", "wp-cron") if start else ("stop", "wp-cron")
        try:
            r = compose(origin, st, *args, check=False, timeout=180)
        except ConfigError as exc:      # e.g. a secret 1Password could not supply
            log(f"wp-cron on {st}: {exc}")
            continue
        if r.returncode == 0:
            changed.append(st)
            log(f"wp-cron {'started' if start else 'stopped'} on {st}")
        else:
            log(f"wp-cron on {st}: {(r.stderr or r.stdout or '').strip()[-200:]}")
    return changed


# ------------------------------------------------------------------ apply

def apply(inv: Inventory, p: Plan, actor: str, this_node: str, accounts: dict | None = None,
          log=print) -> dict[str, Any]:
    """Execute a plan. Idempotent: domains already overridden are skipped."""
    accounts = accounts if accounts is not None else cf_accounts(load_env())
    if any(c.to_origin != this_node for c in p.changes):
        raise FailoverError('run apply on the target node so its aliases can be prepared before DNS')
    from .stacks import ps, install_host_alias, env_flag
    origin = inv.origin(this_node)
    for stack in {c.stack for c in p.changes}:
        containers = ps(origin, stack)
        for service in ('wordpress', 'nginx'):
            if not any(c.get('service') == service and c.get('state') == 'running'
                       and c.get('health') in ('healthy', '', None) for c in containers):
                raise FailoverError(f'stack {stack}: {service} is not ready')
        if not origin.stacks[stack].get('managed'):
            install_host_alias(origin, stack, log=log)
            if (origin.stacks[stack].get('mode', 'multisite') == 'multisite'
                    and env_flag(Path(origin.stacks[stack]['path']), 'WP_SUNRISE') != 'true'):
                raise FailoverError(f'stack {stack}: enable WP_SUNRISE=true and recreate WordPress first')
    done, failed = [], []
    with state.locked('failover'):
        ovs = state.overrides()
        clients = {}
        backed_zones, backed_tunnels = set(), set()
        for c in p.changes:
            if c.domain in ovs and ovs[c.domain].get('status') == 'active':
                done.append({'domain': c.domain, 'status': 'already-active'})
                continue
            try:
                cf = clients.setdefault(c.cf_account, _cf_for(c.cf_account, accounts))
                if c.zone_id not in backed_zones:
                    cf.backup_zone(c.domain)
                    backed_zones.add(c.zone_id)
                if c.tunnel_id not in backed_tunnels:
                    cf.backup_tunnel(c.tunnel_id)
                    backed_tunnels.add(c.tunnel_id)
                previous = ovs.get(c.domain)
                if previous and (previous.get('to') != this_node or previous.get('stack') != c.stack):
                    raise FailoverError('an existing override targets another stack/node; roll it back first')
                # Preserve the original records when retrying a partially applied operation.
                ovs[c.domain] = previous or {
                    'site': c.site, 'from': c.from_origin, 'to': c.to_origin, 'stack': c.stack,
                    'cf_account': c.cf_account, 'zone_id': c.zone_id, 'tunnel_id': c.tunnel_id,
                    'original_records': c.original_records, 'switched_at': int(time.time()), 'actor': actor,
                }
                ovs[c.domain]['status'] = 'preparing'
                state.save_overrides(ovs)
                rules = [{'hostname': d, 'service': c.service} for d in inv.site(c.site).domains]
                cf.merge_tunnel_ingress(c.tunnel_id, rules)
                write_aliases(inv, this_node)
                ovs[c.domain]['status'] = 'switching'
                state.save_overrides(ovs)
                # Refuse records changed by another operator since the plan.
                current_records = [r for r in cf.dns_records(c.zone_id, name=c.domain)
                                   if r.get('type') in SWITCHABLE_TYPES]
                allowed = {(r['type'], r['content'].lower().rstrip('.'))
                           for r in ovs[c.domain]['original_records']}
                allowed.add(('CNAME', c.create['content'].lower().rstrip('.')))
                if any((r['type'], r['content'].lower().rstrip('.')) not in allowed for r in current_records):
                    raise FailoverError('DNS changed since the plan; refusing to replace unexpected records')
                # Re-read current DNS for retries rather than deleting stale record IDs.
                for r in current_records:
                    if r.get('type') in SWITCHABLE_TYPES:
                        cf.delete_record(c.zone_id, r['id'])
                try:
                    created = cf.create_record(c.zone_id, c.create)
                except Exception:
                    # Put the deleted records back so the name keeps resolving;
                    # the override keeps the originals and a retry switches again.
                    _recreate(cf, c.zone_id, current_records, c.domain, log)
                    raise
                ovs[c.domain].update(record_id=created.get('id'), status='active')
                ovs[c.domain].pop('error', None)
                state.save_overrides(ovs)
                done.append({'domain': c.domain, 'status': 'switched', 'from': c.from_origin, 'to': c.to_origin})
                log(f'switched {c.domain}: {c.from_origin} -> {c.to_origin}')
            except Exception as exc:
                if c.domain in ovs:
                    ovs[c.domain].update(status='failed', error=str(exc))
                    state.save_overrides(ovs)
                failed.append({'domain': c.domain, 'error': str(exc)})
                log(f'FAILED {c.domain}: {exc}')
    switched_stacks = {c.stack for c in p.changes if any(d['domain'] == c.domain and d['status'] in ('switched', 'already-active') for d in done)}
    cron = _set_cron(inv, this_node, switched_stacks, True, log)
    state.audit(actor, "failover.apply", {"to": p.to_origin, "done": done, "failed": failed, "cron_started": cron})
    switched = [d["domain"] for d in done if d.get("status") == "switched"]
    if switched or failed:
        notify.notify(
            "failover.applied",
            f"[wpops] FAILOVER to {p.to_origin}: {len(switched)} domain(s) switched" + (f", {len(failed)} failed" if failed else ""),
            "Domains now served by the backup node:\n  " + "\n  ".join(switched or ["none"]) +
            ("\nFailures:\n  " + "\n  ".join(json.dumps(f) for f in failed) if failed else "") +
            f"\nActor: {actor}\nRoll back with: wpops failover rollback --sites ...\n",
            {"to": p.to_origin, "domains": switched, "failed": failed, "actor": actor},
        )
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
                if ov.get("to") != this_node:
                    raise FailoverError("run rollback on the node that owns this override")
                cf = clients.setdefault(ov["cf_account"], _cf_for(ov["cf_account"], accounts))
                ov["status"] = "rolling-back"
                state.save_overrides(ovs)
                # delete whatever currently sits on the name (our CNAME, or anything left half-way)
                current = [r for r in cf.dns_records(ov["zone_id"], name=domain) if r.get("type") in SWITCHABLE_TYPES]
                for r in current:
                    cf.delete_record(ov["zone_id"], r["id"])
                restored_any = False
                try:
                    for r in ov["original_records"]:
                        cf.create_record(ov["zone_id"], r)
                        restored_any = True
                except Exception:
                    if not restored_any:
                        # Nothing of the original came back: keep serving from this node.
                        _recreate(cf, ov["zone_id"], current, domain, log)
                    raise
                removed.setdefault((ov["cf_account"], ov["tunnel_id"]), []).extend(inv.site(ov["site"]).domains)
                del ovs[domain]
                state.save_overrides(ovs)
                done.append({"domain": domain, "status": "restored", "to": ov["from"], "stack": ov.get("stack", "")})
                log(f"restored {domain} -> {ov['from']}")
            except (CfError, Exception) as exc:  # noqa: BLE001
                ov["status"] = "rollback-failed"
                ov["error"] = str(exc)
                state.save_overrides(ovs)
                failed.append({"domain": domain, "error": str(exc)})
                log(f"FAILED {domain}: {exc}")
        still_needed = {domain for ov in ovs.values() if ov.get('to') == this_node
                        for domain in inv.site(ov['site']).domains}
        for (acct, tid), hosts in removed.items():
            hosts = sorted(set(hosts) - still_needed)
            if not hosts:
                continue
            try:
                clients[acct].remove_tunnel_ingress_hosts(tid, hosts)
                log(f"tunnel {tid}: ingress -{len(hosts)} host(s)")
            except CfError as exc:
                failed.append({"tunnel": tid, "error": f"ingress cleanup failed: {exc}"})
        try:
            write_aliases(inv, this_node)
        except Exception as exc:  # noqa: BLE001
            failed.append({"aliases": this_node, "error": str(exc)})
    serving = {ov.get("stack") for ov in state.overrides().values() if ov.get("to") == this_node}
    idle = {d.get("stack") for d in done if d.get("status") == "restored" and d.get("stack")} - serving
    cron = _set_cron(inv, this_node, idle, False, log)
    state.audit(actor, "failover.rollback", {"done": done, "failed": failed, "cron_stopped": cron})
    restored = [d["domain"] for d in done if d.get("status") == "restored"]
    if restored or failed:
        notify.notify(
            "failover.rolledback",
            f"[wpops] ROLLBACK: {len(restored)} domain(s) back on primary" + (f", {len(failed)} failed" if failed else ""),
            "Domains restored to their primary:\n  " + "\n  ".join(restored or ["none"]) +
            ("\nFailures:\n  " + "\n  ".join(json.dumps(f) for f in failed) if failed else "") + f"\nActor: {actor}\n",
            {"domains": restored, "failed": failed, "actor": actor},
        )
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
                if not cur and recs and all(r.get("type") == "CNAME" and r.get("content", "").lower().rstrip(".") in site.domains for r in recs):
                    cur = "follows:" + recs[0]["content"]
                out.append({
                    "site": site.slug, "domain": d, "primary": site.primary, "current": cur or "other",
                    "records": [f"{r['type']} {r['content']}" for r in recs],
                    "override": ovs.get(d, {}).get("status"),
                    "ok": (cur == site.primary or cur.startswith("follows:")) if cur else False,
                })
            except CfError as exc:
                out.append({"site": site.slug, "domain": d, "error": str(exc)})
    return out
