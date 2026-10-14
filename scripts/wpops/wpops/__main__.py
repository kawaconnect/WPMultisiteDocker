"""wpops command line. `wpops --help` lists everything; every command takes --json."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__, backup as bk, failover, jobs, notify, restore, state
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
        rows = [{"name": n, "sync_api": o.sync_api} for n, o in inv.origins.items() if o.sync_api]
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
                          lambda j: bk.pull(j, inv, args.source, args.label or "", fresh=not args.latest, nonce=args.nonce))
        _out(args, job.as_dict(), None if args.json else f"{job.status}: {json.dumps(job.result or job.error, default=str)}")
        return 0 if job.status == "succeeded" else 1
    if args.action == "prune":
        removed = bk.prune(args.source, args.retain, args.dry_run)
        _out(args, {"removed": removed}, "\n".join(removed) or "nothing to prune")
        return 0
    if args.action == "schedule":
        if args.set or args.retain is not None or args.sources is not None:
            info = bk.set_schedule(args.set, args.retain, args.sources.split(",") if args.sources else None)
        else:
            info = bk.schedule_info()
        _out(args, info, json.dumps(info, indent=2, default=str))
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
        example = CONFIG_DIR / "inventory.example.json"
        if example.exists():
            shutil.copyfile(example, INVENTORY_FILE)
            print(f"copied {example} -> {INVENTORY_FILE}; edit it")
        else:
            print(f"{INVENTORY_FILE} missing and no example to copy", file=sys.stderr)
            return 2
    inv, cfg = load_inventory(), node_config()
    origin = inv.origin(cfg["node"])
    for name in origin.stacks:
        try:
            r = install_host_alias(origin, name)
            if r["sunrise_required"]:
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

    s = sub.add_parser("status", help="node status"); s.add_argument("--live", action="store_true", help="also query Cloudflare DNS"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("sites", help="inventory"); s.add_argument("--live", action="store_true"); s.set_defaults(fn=cmd_sites)

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

    s = sub.add_parser("backup", help="pull/list/prune/schedule backup sets")
    s.add_argument("action", choices=["sources", "list", "pull", "prune", "schedule", "keep", "delete"])
    s.add_argument("--source"); s.add_argument("--label"); s.add_argument("--latest", action="store_true", help="pull the newest existing set instead of creating one")
    s.add_argument("--nonce", help="pull a specific existing set"); s.add_argument("--retain", type=int); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--set", help="schedule spec (weekly:sun:03:30|daily:03:30|off) or set ref for keep/delete")
    s.add_argument("--sources", help="comma-separated sources for the schedule"); s.add_argument("--unkeep", action="store_true"); s.add_argument("--yes", action="store_true")
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
    s = sub.add_parser("setup", help="first-time setup on this node"); s.add_argument("--node"); s.set_defaults(fn=cmd_setup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (ConfigError, failover.FailoverError, bk.SyncError, restore.RestoreError, StackError, TimeoutError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
