"""wpops command line. `wpops --help` lists everything; every command takes --json."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from typing import Any

from . import __version__, backup as bk, failover, jobs, notify, restore, state
from .cf import CfError
from .config import (CONFIG_DIR, INVENTORY_FILE, NODE_CONFIG_FILE, REPO_DIR, ConfigError, load_inventory,
                     node_config, public_inventory, save_node_config)
from .ops import node_status
from .stacks import StackError, install_host_alias, stack_path


def _actor() -> str:
    return os.environ.get("WPOPS_ACTOR") or f"cli:{getpass.getuser()}"


def _out(args: argparse.Namespace, data: Any, text: str | None = None) -> None:
    if getattr(args, "json", False) or text is None:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(text)


def _sel(args: argparse.Namespace) -> tuple[list[str], bool]:
    sites = [s.strip() for s in (args.sites or "").split(",") if s.strip()]
    return sites, bool(getattr(args, "all", False))


def _run_inline(kind: str, params: dict[str, Any], fn) -> jobs.Job:
    job = jobs.Job(kind, {**params, "_echo": True}, _actor())
    job.run(fn)
    return job


# ------------------------------------------------------------------ commands

def cmd_status(args: argparse.Namespace) -> int:
    inv, cfg = load_inventory(), node_config()
    st = node_status(inv, cfg, live=args.live)
    if args.json:
        _out(args, st)
        return 0
    print(f"node {st['node']} (wpops {st['version']})")
    for name, s in st["stacks"].items():
        print(f"  stack {name}: {'healthy' if s['healthy'] else 'PROBLEM'} ({s['mode']})")
        for c in s["containers"]:
            if "error" in c:
                print(f"    error: {c['error']}")
            else:
                print(f"    {c['service']:<28} {c['state']:<8} {c.get('health') or ''}")
    print(f"  overrides active: {st['override_count']}")
    for d, o in st["overrides"].items():
        print(f"    {d}: {o['from']} -> {o['to']} [{o['status']}] since {o['switched_at']}")
    print("  tunnels: " + ", ".join(f"{t['account']}={'ok' if t['ready'] else 'MISSING'}" for t in st["tunnels"]))
    b = st["backups"]
    print(f"  backups: {b['sets']} sets, schedule {b['schedule']['schedule']} (next {b['schedule']['next_run']}), "
          f"free {b['disk']['free'] // 2**30} GiB")
    for src, s in b["latest"].items():
        print(f"    latest {src}: {s['set']} ({s['size'] // 2**20} MB, {s['sites']} sites)")
    if st["jobs"]["active"]:
        print("  active jobs: " + ", ".join(f"{j['id']} {j['kind']}" for j in st["jobs"]["active"]))
    if args.live:
        for d in st["dns"]:
            flag = "ok" if d.get("ok") else ("OVERRIDE" if d.get("override") else "CHECK")
            print(f"  dns {d['domain']:<40} {d.get('current', d.get('error')):<12} {flag}")
    return 0


def cmd_sites(args: argparse.Namespace) -> int:
    inv = load_inventory()
    if args.live:
        rows = failover.live_status(inv)
        _out(args, rows, "\n".join(f"{r['site']:<28} {r['domain']:<40} {r.get('current', r.get('error'))}" for r in rows))
        return 0
    data = public_inventory(inv)["sites"]
    ovs = state.overrides()
    lines = []
    for s in data:
        ov = [d for d in s["domains"] if d in ovs]
        lines.append(f"{s['slug']:<28} primary={s['primary']:<8} {', '.join(s['domains'])}" + (f"  OVERRIDE({len(ov)})" if ov else ""))
    _out(args, data, "\n".join(lines))
    return 0


def cmd_failover(args: argparse.Namespace) -> int:
    inv, cfg = load_inventory(), node_config()
    node = cfg["node"]
    if args.action == "rollback":
        sites, all_sites = _sel(args)
        domains = [d.strip() for d in (args.domains or "").split(",") if d.strip()]
        job = _run_inline("failover.rollback", {"sites": sites, "all": all_sites, "domains": domains},
                          lambda j: failover.rollback(inv, _actor(), node, sites=sites, all_sites=all_sites,
                                                      domains=domains or None, log=j.log))
        _out(args, job.as_dict(), None if args.json else f"{job.status}: {json.dumps(job.result or job.error, default=str)}")
        return 0 if job.status == "succeeded" else 1
    sites, all_sites = _sel(args)
    if args.down:
        sites = [s.strip() for s in args.down.split(",") if s.strip()]
    selected = failover.resolve_selection(inv, sites, all_sites)
    plan = failover.plan(inv, selected, to=args.to, stack=args.stack, cfg=cfg)
    if args.action == "plan" or not plan.changes:
        d = plan.as_dict()
        text = "\n".join([f"to: {d['to']}"] +
                         [f"  switch {c['domain']:<40} {c['from_origin']} -> {c['to_origin']} ({c['stack']})" for c in d["changes"]] +
                         [f"  noop   {n['domain']:<40} {n['reason']}" for n in d["noop"]] +
                         [f"  REFUSE {r['site']}/{r['domain']}: {r['reason']}" for r in d["refused"]])
        _out(args, d, text)
        return 0 if (plan.changes or not plan.refused) else 2
    if not args.yes:
        print("refusing to apply without --yes (run `failover plan` first)", file=sys.stderr)
        return 2
    job = _run_inline("failover.apply", {"to": plan.to_origin, "domains": [c.domain for c in plan.changes]},
                      lambda j: failover.apply(inv, plan, _actor(), node, log=j.log))
    _out(args, job.as_dict(), None if args.json else f"{job.status}: {json.dumps(job.result or job.error, default=str)}")
    return 0 if job.status == "succeeded" and not (job.result or {}).get("failed") else 1


def cmd_backup(args: argparse.Namespace) -> int:
    inv = load_inventory()
    if args.action == "sources":
        rows = [{"name": n, "sync_api": o.sync_api} for n, o in inv.sources().items()]
        _out(args, rows, "\n".join(f"{r['name']:<12} {r['sync_api']}" for r in rows))
        return 0
    if args.action == "list":
        rows = bk.list_sets(args.source)
        _out(args, rows, "\n".join(
            f"{r['id']:<40} {r['size'] // 2**20:>7} MB  {r['sites']:>3} sites  {'complete' if r['complete'] else 'PARTIAL'}"
            f"{'  keep' if r['keep'] else ''}{'  applied' if r['applied'] else ''}  {r['label']}" for r in rows))
        return 0
    if args.action == "pull":
        if not args.source:
            print("--source is required", file=sys.stderr)
            return 2
        job = _run_inline("backup.pull", {"source": args.source, "label": args.label or ""},
                          lambda j: bk.pull(j, inv, args.source, args.label or "", fresh=not args.latest, nonce=args.nonce,
                                            entities=[e for e in (args.entities or "").split(",") if e] or None,
                                            db=not args.no_db))
        _out(args, job.as_dict(), None if args.json else f"{job.status}: {json.dumps(job.result or job.error, default=str)}")
        return 0 if job.status == "succeeded" else 1
    if args.action == "prune":
        removed = bk.prune(args.source, args.retain, args.dry_run)
        _out(args, {"removed": removed}, "\n".join(removed) or "nothing to prune")
        return 0
    if args.action == "schedule":
        if args.set or args.retain is not None or args.sources is not None:
            sources = [x.strip() for x in args.sources.split(",") if x.strip()] if args.sources else None
            unknown = [x for x in sources or [] if x not in inv.sources()]
            if unknown:
                raise ConfigError(f"unknown backup sources: {', '.join(unknown)} (see wpops backup sources)")
            info = bk.set_schedule(args.set, args.retain, sources)
        else:
            info = bk.schedule_info()
        _out(args, info, json.dumps(info, indent=2, default=str))
        return 0
    if args.action in ("pause", "resume"):
        names = [x.strip() for x in (args.sources or args.source or "").split(",") if x.strip()]
        if not names:
            print("pause/resume needs --source <name> (or --sources a,b)", file=sys.stderr)
            return 2
        unknown = [x for x in names if x not in inv.sources()]
        if unknown:
            raise ConfigError(f"unknown backup sources: {', '.join(unknown)} (see wpops backup sources)")
        info = bk.pause_sources(names, paused=(args.action == "pause"))
        _out(args, info, f"paused sources: {', '.join(info.get('paused') or []) or '(none)'}")
        return 0
    if args.action == "keep":
        man = bk.mark_keep(args.set, not args.unkeep)
        _out(args, man, f"{args.set}: keep={man['keep']}")
        return 0
    if args.action == "delete":
        if not args.yes:
            print("refusing without --yes", file=sys.stderr)
            return 2
        bk.delete_set(args.set)
        state.audit(_actor(), "backup.delete", {"set": args.set})
        print(f"deleted {args.set}")
        return 0
    return 2


def cmd_restore(args: argparse.Namespace) -> int:
    inv, cfg = load_inventory(), node_config()
    sites, all_sites = _sel(args)
    if not args.yes:
        print("refusing without --yes", file=sys.stderr)
        return 2
    entities = [e.strip() for e in (args.entities or "").split(",") if e.strip()] or None
    job = _run_inline("restore.apply", {"set": args.set, "stack": args.stack, "sites": sites, "all": all_sites},
                      lambda j: restore.apply(j, inv, cfg["node"], args.set, args.stack, sites=sites or None,
                                              all_sites=all_sites, entities=entities))
    _out(args, job.as_dict(), None if args.json else f"{job.status}: {json.dumps(job.result or job.error, default=str)}")
    return 0 if job.status == "succeeded" else 1


def cmd_jobs(args: argparse.Namespace) -> int:
    if args.action == "list":
        rows = jobs.list_jobs(args.limit)
        _out(args, rows, "\n".join(f"{j['id']}  {j['kind']:<18} {j['status']:<10} {j['actor']}" for j in rows))
        return 0
    if args.action == "get":
        j = jobs.get(args.id)
        if not j:
            print("no such job", file=sys.stderr)
            return 1
        _out(args, j, json.dumps(j, indent=2, default=str))
        return 0
    if args.action == "tail":
        print(jobs.tail(args.id, args.lines))
        return 0
    return 2


def cmd_token(args: argparse.Namespace) -> int:
    from .agent import rotate_token
    tok = rotate_token()
    print(tok if args.json is False else json.dumps({"token": tok}))
    print("# store this token in the bot (WPOPS_TOKEN_<node>), it is not kept in clear anywhere here", file=sys.stderr)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .agent import serve
    serve(extra_binds=args.bind or [])
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    r = notify.notify("test", "[wpops] test notification", "This is a test from the wpops agent.", {})
    _out(args, r, json.dumps(r, indent=2, default=str))
    return 0


def cmd_tunnels(args: argparse.Namespace) -> int:
    from . import tunnels
    if args.action == "ensure":
        r = tunnels.ensure(start=not args.no_start)
        _out(args, r, None if args.json else json.dumps(r, indent=2))
        return 0 if all(v.get("ok") for k, v in r.items() if not k.startswith("_")) and not r.get("_compose") else 1
    rows = tunnels.status()
    _out(args, rows, "\n".join(f"{r['account']:<32} {r.get('tunnel') or 'MISSING':<40} {r.get('status', r.get('error', ''))} conns={r.get('connections', '-')}" for r in rows))
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    from . import config as cfgmod, vault
    if args.action == "refresh":
        if args.boot:
            r = vault.refresh_boot(log=lambda m: print(m, file=sys.stderr))
            _out(args, r, "ok" if r["ok"] else "1Password not reachable; the agent retries")
            return 0                    # boot must not fail: running containers do not need it
        r = cfgmod.refresh_cache()
        _out(args, r, f"cache filled: {r['refs']} references from {r['files']} .env files (one 1Password call)")
        return 0
    r = vault.check(fresh=args.fresh)
    lines = []
    if "refresh" in r:
        lines.append(f"refresh: {r['refresh'].get('error') or str(r['refresh'].get('refs')) + ' references read'}")
    c = r["cache"]
    filled = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["filled_at"])) if c.get("filled_at") else "never"
    lines.append(f"cache    {c['file']}: {c['values']} values, filled {filled} ({c['origin']})"
                 + (f"; last 1Password error: {c['last_error']}" if c.get("last_error") else ""))
    for f in r["files"]:
        state_ = "ok" if f["ok"] and not f["warn"] else "WARN" if f["ok"] else "PROBLEM"
        extra = [f"cache {f['cache']}"] if f.get("cache") else []
        if f["plaintext_keys"]:
            extra.append("plaintext: " + ",".join(f["plaintext_keys"]))
        if f.get("not_cached"):
            extra.append("not cached: " + ",".join(f["not_cached"]))
        if f.get("empty"):
            extra.append("empty: " + ",".join(f["empty"]))
        lines.append(f"{state_:<8} {f['refs']:>3} refs  {f['file']}  {'; '.join(extra)}".rstrip())
    t = r["tunnels"]
    lines.append(f"{'ok' if t['ok'] else 'PROBLEM':<8} tunnels  {t['file']}  "
                 + ("literal secrets: " + ",".join(t["literal_tokens"]) if t["literal_tokens"] else t.get("error", "no secrets in the file")))
    _out(args, r, "\n".join(lines))
    return 0 if r["ok"] and not r["warnings"] else 1


def cmd_stack(args: argparse.Namespace) -> int:
    from . import provision, registry
    from .stacks import compose
    if args.action == 'create':
        result = provision.create(args.name, args.mode, args.domain, args.port,
                                  args.login_path or '', not args.subdirectories, args.noindex)
        result['registry_revision'] = registry.refresh()['revision']
    elif args.action == 'init':
        result = provision.initialise(args.name, args.admin_email, args.admin_user, args.scheme)
        # Versions, blogs and the SiteGuard key for the registry (best effort).
        try:
            result['registry'] = registry.refresh_facts([args.name]).get(args.name)
        except Exception as exc:  # noqa: BLE001
            result['registry'] = {'error': str(exc)}
    elif args.action == 'remove':
        if not args.yes:
            raise ConfigError('stack remove stops the stack and unregisters it; add --yes')
        result = provision.remove(args.name, detach_sites=args.detach_sites, keep_files=args.keep_files)
    elif args.action == 'login':
        result = provision.set_login(args.name, args.path, args.blog_id)
    else:
        inv, cfg = load_inventory(), node_config()
        origin = inv.origin(cfg['node'])
        if args.action == 'list':
            result = origin.stacks
        else:
            if args.action == 'up':
                provision.ensure_edge()
            compose_args = {'up': ['up', '-d', '--wait'], 'stop': ['stop'],
                            'compose': getattr(args, 'args', [])}[args.action]
            if not compose_args:
                raise ConfigError('compose arguments are required')
            if args.action == 'compose' and not args.json:
                # Plain Compose: its own output, prompts and exit status (logs -f, exec, nginx -t).
                return compose(origin, args.name, *compose_args, check=False, capture=False, timeout=None).returncode
            r = compose(origin, args.name, *compose_args)
            result = {'stack': args.name, 'output': r.stdout.strip()}
    _out(args, result)
    return 0


def _health_flag(h: dict[str, Any]) -> str:
    return {"operational": "up", "degraded": "DEGRADED", "down": "DOWN"}.get(h.get("status") or "", "?")


def cmd_registry(args: argparse.Namespace) -> int:
    from . import registry
    if args.action == "changes":
        data = registry.changes(args.since, args.limit)
        _out(args, data, "\n".join(f"rev {c['rev']:<5} {c['op']:<8} {c['uid']:<40} {c['site'].get('url')}"
                                   for c in data["changes"]) or f"no changes after revision {args.since} (now {data['revision']})")
        return 0
    if args.action == "refresh":
        stacks = [x.strip() for x in (args.stacks or "").split(",") if x.strip()] or None
        result: dict[str, Any] = {}
        if args.facts:
            result["facts"] = registry.refresh_facts(stacks, log=lambda m: print(m, file=sys.stderr))
        result["health"] = registry.refresh_health()
        _out(args, result, json.dumps(result, indent=2, default=str))
        return 0
    if args.action == "show":
        if not args.id:
            raise ConfigError("registry show needs a site uid, <stack>:<blog id> or slug")
        site = registry.site(args.id)
        if not site:
            print("no such site", file=sys.stderr)
            return 1
        _out(args, site, json.dumps(site, indent=2, default=str))
        return 0
    snap = registry.snapshot(live=args.live)
    lines = [f"node {snap['node']}  revision {snap['revision']}  {len(snap['sites'])} sites"]
    for st in snap["stacks"]:
        v = st["versions"]
        lines.append(f"  stack {st['name']} ({st['mode']}{', managed' if st['managed'] else ''}): WordPress {v.get('wordpress') or '?'}, "
                     f"PHP {v.get('php') or '?'}, SiteGuard {v.get('siteguard') or '-'} [{st['siteguard']['status'] or 'no data'}]"
                     + (f"  facts error: {st['facts_error']}" if st["facts_error"] else ""))
    for s in snap["sites"]:
        h = s["health"]
        lines.append(f"  {s['uid']:<36} {_health_flag(h):<8} {s['slug'] or '(unmapped)':<20} {s['url']}  [{h.get('source')}]"
                     + (f" {h.get('error')}" if h.get("error") else ""))
    _out(args, snap, "\n".join(lines))
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    from . import registry
    if args.action == "add":
        domains = [d.strip().lower() for d in (args.domains or "").split(",") if d.strip()] or None
        result = registry.site_add(args.slug, args.stack, host=args.host or "", domains=domains, blog_id=args.blog_id,
                                   cf_account=args.cf_account or "", primary=args.primary or "", name=args.name or "",
                                   failover_to=args.failover_to or "")
    else:
        if not args.yes:
            raise ConfigError("site remove changes the inventory; add --yes")
        result = registry.site_remove(args.slug, everywhere=args.everywhere)
    _out(args, result)
    return 0


def cmd_siteguard(args: argparse.Namespace) -> int:
    from . import registry, siteguard
    inv, cfg = load_inventory(), node_config()
    origin = inv.origin(cfg["node"])
    if args.action == "status":
        keys = siteguard.load_keys()
        snap = {s["name"]: s["siteguard"] for s in registry.snapshot()["stacks"]}
        rows = [{"stack": n, "key": (keys.get(n) or {}).get("fp"), "kind": (keys.get(n) or {}).get("kind"),
                 **{k: v for k, v in (snap.get(n) or {}).items() if k not in ("key", "key_kind")}} for n in origin.stacks]
        _out(args, rows, "\n".join(f"{r['stack']:<24} key={r['key'] or '-':<18} {r['kind'] or '':<5} {r['status'] or 'no data'} {r['error']}"
                                   for r in rows))
        return 0
    names = [args.stack] if args.stack else list(origin.stacks)
    results = {}
    for name in names:
        if name not in origin.stacks:
            raise ConfigError(f"unknown stack '{name}'")
        facts = registry.collect_facts(origin, name)
        if "error" in facts:
            results[name] = {"enrolled": False, "reason": facts["error"]}
            continue
        results[name] = siteguard.enroll(origin, name, facts.get("siteguard"), registry._stack_host(origin, name, facts),
                                         replace=args.replace, log=lambda m: print(m, file=sys.stderr))
    _out(args, results, "\n".join(f"{n}: {'enrolled (' + r['kind'] + ' key ' + r['fp'] + ')' if r.get('enrolled') else 'not enrolled: ' + r.get('reason', '')}"
                                  for n, r in results.items()))
    return 0 if all(r.get("enrolled") for r in results.values()) else 1


def cmd_routes(args: argparse.Namespace) -> int:
    from . import routes
    inv, cfg = load_inventory(), node_config()
    if args.action == 'apply' and not args.yes:
        raise ConfigError('run routes plan first, then routes apply --yes')
    result = (routes.apply if args.action == 'apply' else routes.plan)(inv, cfg['node'])
    _out(args, result)
    return 2 if result['refused'] else 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Write config skeletons, install the host-alias plugin into every stack, add the backups mount."""
    CONFIG_DIR.mkdir(exist_ok=True)
    state.ensure_dirs()
    if not NODE_CONFIG_FILE.exists():
        if not args.node:
            print("--node <origin-name> is required on first setup", file=sys.stderr)
            return 2
        save_node_config({"node": args.node})
        print(f"wrote {NODE_CONFIG_FILE}")
    if not INVENTORY_FILE.exists():
        name = node_config()['node']
        skeleton = {'origins': {name: {'kind': 'node', 'stacks': {}, 'tunnels': {}}},
                    'sites': [], 'backup_sources': {}}
        INVENTORY_FILE.write_text(json.dumps(skeleton, indent=2)+'\n', encoding='utf-8')
        print(f'wrote empty inventory for {name}: {INVENTORY_FILE}; see inventory.example.json for site mappings')
    inv, cfg = load_inventory(), node_config()
    origin = inv.origin(cfg["node"])
    for name in origin.stacks:
        try:
            r = install_host_alias(origin, name)
            # Managed stacks always run with WP_SUNRISE=true (docker-compose.stack.yml).
            if r["sunrise_required"] and not origin.stacks[name].get("managed"):
                env_path = stack_path(origin, name) / ".env"
                txt = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
                if "WP_SUNRISE=true" not in txt:
                    print(f"  NOTE: set WP_SUNRISE=true in {env_path} and `docker compose up -d wordpress` for alias hostnames to work on multisite")
        except StackError as exc:
            print(f"  stack {name}: {exc}", file=sys.stderr)
    from .agent import token_hash
    if not token_hash():
        print("no agent token yet: run `wpops token rotate`")
    print("setup complete")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wpops", description=f"wpops {__version__}, WordPress node operations")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    stack = sub.add_parser('stack', help='independent production single-site or multisite deployments')
    actions = stack.add_subparsers(dest='action', required=True)
    s = actions.add_parser('create', help='write configuration; starts no containers and changes no DNS')
    s.add_argument('name'); s.add_argument('--mode', required=True, choices=['single', 'multisite'])
    s.add_argument('--domain', required=True); s.add_argument('--port', required=True, type=int)
    s.add_argument('--login-path'); s.add_argument('--subdirectories', action='store_true')
    s.add_argument('--noindex', action='store_true', help='standby/staging: send a noindex header')
    s.set_defaults(fn=cmd_stack)
    s = actions.add_parser('init', help='start and install an empty stack')
    s.add_argument('name'); s.add_argument('--admin-email', required=True)
    s.add_argument('--admin-user', default='admin'); s.add_argument('--scheme', choices=['http', 'https'], default='https')
    s.set_defaults(fn=cmd_stack)
    s = actions.add_parser('login', help='configure the default or a multisite blog login slug')
    s.add_argument('name'); s.add_argument('--path', required=True); s.add_argument('--blog-id', type=int)
    s.set_defaults(fn=cmd_stack)
    s = actions.add_parser('remove', help='stop and unregister a stack (data is kept; DNS is not touched)')
    s.add_argument('name'); s.add_argument('--yes', action='store_true')
    s.add_argument('--detach-sites', action='store_true', help="also unmap sites that use it on this node")
    s.add_argument('--keep-files', action='store_true', help='leave stacks/<name> in place (default: move to stacks/_removed/)')
    s.set_defaults(fn=cmd_stack)
    for action in ('up', 'stop', 'list', 'compose'):
        s = actions.add_parser(action)
        if action != 'list':
            s.add_argument('name')
        if action == 'compose':
            s.add_argument('args', nargs=argparse.REMAINDER)
        s.set_defaults(fn=cmd_stack)

    s = sub.add_parser("status", help="node status"); s.add_argument("--live", action="store_true", help="also query Cloudflare DNS"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser('routes', help='publish native primary and standby hosts without taking over foreign DNS')
    s.add_argument('action', choices=['plan', 'apply']); s.add_argument('--yes', action='store_true')
    s.set_defaults(fn=cmd_routes)
    s = sub.add_parser("sites", help="inventory"); s.add_argument("--live", action="store_true"); s.set_defaults(fn=cmd_sites)

    s = sub.add_parser("registry", help="every WordPress site on this node: health, versions, backups, disk, tunnel")
    s.add_argument("action", nargs="?", default="list", choices=["list", "show", "changes", "refresh"])
    s.add_argument("id", nargs="?", help="show: site uid, <stack>:<blog id> or slug")
    s.add_argument("--live", action="store_true", help="list: also query Cloudflare DNS")
    s.add_argument("--since", type=int, default=0, help="changes: after this revision")
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--facts", action="store_true", help="refresh: also versions, blogs, disk and SiteGuard keys (WP-CLI)")
    s.add_argument("--stacks", help="refresh: comma-separated stacks (default all)")
    s.set_defaults(fn=cmd_registry)

    s = sub.add_parser("site", help="map or unmap a site of this node in the inventory")
    acts = s.add_subparsers(dest="action", required=True)
    a = acts.add_parser("add", help="map a site (new, or this node's host of an existing site)")
    a.add_argument("slug"); a.add_argument("--stack", required=True); a.add_argument("--blog-id", type=int)
    a.add_argument("--host", help="installed hostname (default: from the stack)")
    a.add_argument("--domains", help="comma-separated production domains (default: the host)")
    a.add_argument("--cf-account", help="Cloudflare account of the site's zones (required for a new site)")
    a.add_argument("--primary", help="primary origin (default: this node)"); a.add_argument("--name")
    a.add_argument("--failover-to"); a.set_defaults(fn=cmd_site)
    a = acts.add_parser("remove", help="unmap a site from this node")
    a.add_argument("slug"); a.add_argument("--everywhere", action="store_true", help="drop the site from the inventory")
    a.add_argument("--yes", action="store_true"); a.set_defaults(fn=cmd_site)

    s = sub.add_parser("siteguard", help="SiteGuard health keys held by the agent, one per stack")
    s.add_argument("action", choices=["status", "enroll"]); s.add_argument("stack", nargs="?")
    s.add_argument("--replace", action="store_true", help="SiteGuard before 1.6.4: replace the site's own key")
    s.set_defaults(fn=cmd_siteguard)

    s = sub.add_parser("failover", help="plan/apply/rollback DNS failover")
    s.add_argument("action", choices=["plan", "apply", "rollback"])
    s.add_argument("--sites", help="comma-separated site slugs")
    s.add_argument("--all", action="store_true", help="every enabled site")
    s.add_argument("--down", help="comma-separated slugs the caller considers down (same as --sites)")
    s.add_argument("--domains", help="rollback: specific domains")
    s.add_argument("--to", help="target origin (node)")
    s.add_argument("--stack", help="target stack on that node")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_failover)

    s = sub.add_parser("backup", help="pull/list/prune/schedule/pause/resume backup sets")
    s.add_argument("action", choices=["sources", "list", "pull", "prune", "schedule", "pause", "resume", "keep", "delete"])
    s.add_argument("--source"); s.add_argument("--label"); s.add_argument("--latest", action="store_true", help="pull the newest existing set instead of creating one")
    s.add_argument("--nonce", help="pull a specific existing set"); s.add_argument("--retain", type=int); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--set", help="schedule spec (weekly:sun:03:30|daily:03:30|off) or set ref for keep/delete")
    s.add_argument("--sources", help="comma-separated sources for the schedule"); s.add_argument("--unkeep", action="store_true"); s.add_argument("--yes", action="store_true")
    s.add_argument("--entities", help="pull: limit the source backup to these file entities (plugins,themes,uploads,others,wpcore)")
    s.add_argument("--no-db", action="store_true", help="pull: skip the database (test runs)")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help="apply a pulled set to a stack")
    s.add_argument("action", choices=["apply"]); s.add_argument("--set", required=True); s.add_argument("--stack", required=True)
    s.add_argument("--sites"); s.add_argument("--all", action="store_true"); s.add_argument("--entities"); s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("jobs", help="job records"); s.add_argument("action", choices=["list", "get", "tail"]); s.add_argument("id", nargs="?")
    s.add_argument("--limit", type=int, default=30); s.add_argument("--lines", type=int, default=60); s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("token", help="rotate the agent token"); s.add_argument("action", choices=["rotate"]); s.set_defaults(fn=cmd_token)
    s = sub.add_parser("serve", help="run the HTTP agent"); s.add_argument("--bind", action="append", help="extra bind address"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("notify", help="send a test notification"); s.add_argument("action", choices=["test"]); s.set_defaults(fn=cmd_notify)
    s = sub.add_parser("tunnels", help="per-account Cloudflare tunnels on this node"); s.add_argument("action", choices=["status", "ensure"]); s.add_argument("--no-start", action="store_true"); s.set_defaults(fn=cmd_tunnels)
    s = sub.add_parser("secrets", help="1Password references in .env and their tmpfs cache (docs/secrets.md)")
    s.add_argument("action", choices=["check", "refresh"])
    s.add_argument("--fresh", action="store_true", help="check: refill the cache from 1Password first (one call)")
    s.add_argument("--boot", action="store_true", help="refresh: only if the cache is not fresh; retry, never fail")
    s.set_defaults(fn=cmd_secrets)
    s = sub.add_parser("setup", help="first-time setup on this node"); s.add_argument("--node"); s.set_defaults(fn=cmd_setup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (ConfigError, failover.FailoverError, bk.SyncError, restore.RestoreError, StackError, TimeoutError,
            OSError, subprocess.CalledProcessError, CfError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = str(exc.stderr).strip()[-2000:]
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), **({"stderr": detail} if detail else {})}))
        else:
            print(f"error: {exc}" + (f"\n{detail}" if detail else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
