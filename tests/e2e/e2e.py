#!/usr/bin/env python3
"""Opt-in end-to-end test of wpops stacks on a real Docker daemon.

Not part of the offline suite (CI has no daemon). It needs Docker with
Compose v2.20+, python3, curl, jq and openssl, internet access for images and
WPS Hide Login, and nothing else on the node: it creates the wpops-edge
network and 10.214.0.0/16 stack networks, and publishes 127.0.0.1 ports
18181-18190 (and 8080 for the mixed topology's legacy root stack).

    python3 tests/e2e/e2e.py TOPOLOGY WORKDIR [--host-ip IP] [--siteguard DIR]

TOPOLOGY is one of
    singles    two single-site stacks
    multisite  one subdomain multisite network
    networks   a subdomain network and a subdirectory network
    mixed      two singles, both networks and the legacy root docker-compose.yml
               stack (multisite, WP_SUNRISE=true) on one node
WORKDIR receives a fresh copy of this checkout (the node) and must not exist.
--host-ip is an address of this host that containers can reach (default: the
first address of `hostname -I`); the fake Cloudflare API listens there.
--siteguard DIR (a SiteGuard Backup plugin checkout) adds backup pull and
restore between stacks.

What it does, through the wpops CLI as an operator would (docs/deployments.md):
setup, stack create/init (with custom login paths), blogs with WP-CLI,
stack list/stop/up/compose, tunnels ensure and routes plan/apply against
tests/e2e/fake_cloudflare.py, then checks over HTTPS through
tests/e2e/cf_edge.py (Cloudflare edge + cloudflared stand-in on the
connector networks) that every routed hostname reaches the right site, that
failover serves production hostnames from the right stack/blog with
production URLs, that rollback restores DNS exactly, real logins through the
custom login paths, per-visitor login rate limits and cross-stack isolation.
A Docker daemon restart is not part of it (that needs control of the daemon).
Prints PASS/FAIL per check; exit status 1 if anything failed.
Containers are left running for inspection; stop them with
`wpops stack compose NAME down`, `docker compose -p wpops-e2e-tunnels -f
docker-compose.wpops-tunnels.json down` and `docker stop e2e-edge e2e-probe`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FAILS: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def check(ok: bool, what: str, detail: str = "") -> bool:
    log(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(what)
    return ok


def sh(cmd: list[str] | str, cwd: Path | None = None, check_rc: bool = True, input_text: str | None = None,
       env: dict | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), text=True, capture_output=True,
                       input=input_text, env=env, timeout=timeout)
    if check_rc and r.returncode:
        raise SystemExit(f"command failed ({r.returncode}): {cmd}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r


# --------------------------------------------------------------- topologies
# stack: (mode, domain, port, login slug, subdirectories, blogs [(slug, blog marker)])
STACKS = {
    "shop": ("single", "shop.example.net", 18181, "staff-door", False, []),
    "blog": ("single", "blog.example.net", 18182, "editors", False, []),
    "network": ("multisite", "network.example.net", 18183, "network-staff", False, [("news", 2), ("arts", 3)]),
    "paths": ("multisite", "paths.example.net", 18184, "paths-staff", True, [("alpha", 2), ("beta", 3)]),
}
TOPOLOGIES = {
    "singles": ["shop", "blog"],
    "multisite": ["network"],
    "networks": ["network", "paths"],
    "mixed": ["shop", "blog", "network", "paths"],
}
# Per-blog login overrides: stack -> (blog id, slug)
OVERRIDES = {"network": (2, "news-editors"), "paths": (2, "alpha-editors")}
# Sites (failover/routing): slug, stack, blog id, standby host, production domains, primary
SITES = [
    ("shop", "shop", None, "shop.example.net", ["shop.example.com", "www.shop.example.com"], "primary"),
    ("blog", "blog", None, "blog.example.net", ["blog.example.com"], "primary"),
    ("news", "network", 2, "news.network.example.net", ["news.example.com", "www.news.example.com"], "primary"),
    ("arts", "network", 3, "arts.network.example.net", ["arts.example.com"], "node"),
    ("org", "paths", 1, "paths.example.net", ["example.org", "www.example.org"], "primary"),
    ("legacy-blog", "legacy", 2, "lblog.legacy.example.net", ["legacy-blog.example.org"], "primary"),
]
PRIMARY_RECORDS = [
    ("example.com", "A", "shop.example.com", "192.0.2.10", True),
    ("example.com", "CNAME", "www.shop.example.com", "shop.example.com", True),
    ("example.com", "A", "blog.example.com", "192.0.2.10", False),
    ("example.com", "AAAA", "blog.example.com", "2001:db8::10", False),
    ("example.com", "A", "news.example.com", "192.0.2.10", True),
    ("example.com", "CNAME", "www.news.example.com", "news.example.com", True),
    ("example.org", "A", "example.org", "192.0.2.10", True),
    ("example.org", "CNAME", "www.example.org", "example.org", True),
    ("example.org", "A", "legacy-blog.example.org", "192.0.2.10", True),
    ("example.org", "MX", "example.org", "mail.example.org", False),
    ("example.com", "A", "untouched.example.com", "198.51.100.7", True),
]


class Node:
    def __init__(self, root: Path, host_ip: str, port: int = 8999):
        self.root = root
        self.fake = f"http://127.0.0.1:{port}"
        self.host_ip = host_ip
        self.port = port

    def wpops(self, *args: str, check_rc: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
        env["SSL_CERT_FILE"] = str(self.root.parent / "edge" / "ca.pem")
        return sh([str(self.root / "scripts/wpops/bin/wpops"), *args], cwd=self.root, check_rc=check_rc,
                  env=env, timeout=timeout)

    def wp(self, stack: str, *args: str, url: str | None = None) -> str:
        extra = [f"--url={url}"] if url else []
        return self.wpops("stack", "compose", stack, "exec", "-T", "wp-cron", "wp", *args, *extra).stdout.strip()

    def state(self) -> dict:
        with urllib.request.urlopen(self.fake + "/_fake/state", timeout=10) as r:
            return json.load(r)

    def fail_next(self, method: str, path: str, status: int = 400, count: int = 1) -> None:
        req = urllib.request.Request(self.fake + "/_fake/fail", method="POST",
                                     data=json.dumps({"method": method, "path": path, "status": status,
                                                      "count": count}).encode())
        urllib.request.urlopen(req, timeout=10).read()

    def records(self, exclude_standby: bool = True) -> list:
        out = []
        for r in self.state()["records"]:
            if exclude_standby and r["name"].endswith("example.net"):
                continue
            out.append({k: r.get(k) for k in ("type", "name", "content", "proxied", "ttl", "priority")})
        return sorted(out, key=lambda r: (r["name"], r["type"], r["content"]))

    def env_value(self, stack: str, key: str) -> str:
        for line in (self.root / "stacks" / stack / ".env").read_text().splitlines():
            if line.startswith(key + "="):
                return json.loads(line.split("=", 1)[1])
        return ""


class Edge:
    """cf_edge.py Nginx + a curl client, both on the connector project's networks."""

    def __init__(self, node: Node, networks: list[str], routes: list[str]):
        self.node, self.dir, self.routes = node, node.root.parent / "edge", routes
        self.dir.mkdir(exist_ok=True)
        self.render()
        sh(["docker", "run", "-d", "--rm", "--name", "e2e-edge", "--network", networks[0],
            "-v", f"{self.dir}:/e:ro", "-v", f"{self.dir}/nginx.conf:/etc/nginx/nginx.conf:ro",
            "nginx:1.31.6-alpine"])
        sh(["docker", "run", "-d", "--rm", "--name", "e2e-probe", "--network", networks[0], "alpine:3.23",
            "sleep", "86400"])
        for net in networks[1:]:
            sh(["docker", "network", "connect", net, "e2e-edge"])
            sh(["docker", "network", "connect", net, "e2e-probe"])
        sh(["docker", "exec", "e2e-probe", "apk", "add", "-q", "--no-cache", "curl", "bind-tools"])
        self.reload()

    def render(self) -> None:
        args = [sys.executable, str(HERE / "cf_edge.py"), "--fake", self.node.fake, "--out", str(self.dir)]
        for r in self.routes:
            args += ["--route", r]
        sh(args)

    def reload(self) -> None:
        self.render()
        sh(["docker", "cp", str(self.dir / "ca.pem"), "e2e-probe:/tmp/ca.pem"])
        sh(["docker", "exec", "e2e-edge", "nginx", "-s", "reload"])
        time.sleep(1)

    def curl(self, host: str, path: str, *args: str) -> str:
        return sh(["docker", "exec", "e2e-probe", "curl", "-s", "--cacert", "/tmp/ca.pem", "--connect-to",
                   f"{host}:443:e2e-edge:443", *args, f"https://{host}{path}"], check_rc=False).stdout

    def get(self, host: str, path: str = "/", *args: str) -> tuple[int, str, str]:
        out = self.curl(host, path, "-w", "\n%{http_code} %{redirect_url}", *args)
        body, _, tail = out.rpartition("\n")
        code, _, redirect = tail.partition(" ")
        return int(code or 0), redirect, body

    def login(self, host: str, slug_path: str, password: str, admin: str = "/wp-admin/") -> tuple[bool, str]:
        jar = f"/tmp/cj-{host}"
        sh(["docker", "exec", "e2e-probe", "sh", "-c", f": > {jar}"])
        self.curl(host, slug_path, "-c", jar, "-b", jar, "-o", "/dev/null")
        body = f"log=admin&pwd={password}&wp-submit=Log+In&testcookie=1"
        sh(["docker", "exec", "-i", "e2e-probe", "sh", "-c", "cat > /tmp/body"], input_text=body)
        post = self.curl(host, slug_path, "-c", jar, "-b", jar, "--data", "@/tmp/body", "-o", "/dev/null",
                         "-w", "%{http_code} %{redirect_url}")
        cookies = sh(["docker", "exec", "e2e-probe", "cat", jar]).stdout
        code, _, page = self.get(host, admin, "-b", jar)
        ok = post.startswith("302") and f".{host}" in cookies and code == 200 and "Dashboard" in page
        return ok, f"post={post} admin={code} cookie_domain_ok={f'.{host}' in cookies}"


def title(body: str) -> str:
    m = re.search(r"<title>([^<]*)", body)
    return m.group(1) if m else ""


def hosts_in(body: str) -> set[str]:
    return {m.replace("\\/", "/").split("//", 1)[1] for m in re.findall(r"https?:(?:\\?/){2}[a-z0-9.-]+", body)
            if "w.org" not in m and "w3.org" not in m and "wordpress.org" not in m}


# ----------------------------------------------------------------- the run

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("topology", choices=sorted(TOPOLOGIES))
    ap.add_argument("workdir")
    ap.add_argument("--host-ip", default="")
    ap.add_argument("--siteguard", default="")
    ap.add_argument("--fake-port", type=int, default=8999)
    args = ap.parse_args()
    work = Path(args.workdir).resolve()
    if work.exists():
        raise SystemExit(f"{work} exists; use a new directory")
    host_ip = args.host_ip or sh("hostname -I").stdout.split()[0]
    stacks = TOPOLOGIES[args.topology]
    legacy = args.topology == "mixed"
    sites = [s for s in SITES if s[1] in stacks or (legacy and s[1] == "legacy")]

    # A fresh node: the tracked files of this checkout.
    files = sh(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO).stdout.split("\n")
    root = work / "node"
    for f in filter(None, files):
        if (REPO / f).is_file():
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / f, root / f)
    node = Node(root, host_ip, args.fake_port)

    # Fake Cloudflare with the production records of the selected sites.
    zones = {"acc-standby": ["example.net"], "acc-customer": ["example.com", "example.org"]}
    names = {d for s in sites for d in s[4]} | {"untouched.example.com"}
    seed = {"accounts": {a: {"token": f"tok-{a}", "zones": z} for a, z in zones.items()},
            "records": [{"zone": z, "type": t, "name": n, "content": c, "proxied": p, "ttl": 1 if p else 300,
                         **({"priority": 10} if t == "MX" else {})}
                        for z, t, n, c, p in PRIMARY_RECORDS if n in names]}
    (work / "seed.json").write_text(json.dumps(seed))
    fake = subprocess.Popen([sys.executable, str(HERE / "fake_cloudflare.py"), "--port", str(args.fake_port),
                             "--seed", str(work / "seed.json")], stdout=open(work / "fakecf.log", "w"),
                            stderr=subprocess.STDOUT)
    time.sleep(1)
    try:
        return run(node, work, args, stacks, sites, legacy)
    finally:
        fake.terminate()


def run(node: Node, work: Path, args, stacks: list[str], sites: list, legacy: bool) -> int:
    root = node.root
    # docs/deployments.md section 1
    env = (root / ".env.example").read_text()
    for k, v in {"CF_ACCOUNT_1_NAME": "standby", "CF_ACCOUNT_1_ID": "acc-standby", "CF_ACCOUNT_1_TOKEN": "tok-acc-standby",
                 "CF_ACCOUNT_1_DOMAINS": "example.net", "CF_ACCOUNT_2_NAME": "customer",
                 "CF_ACCOUNT_2_ID": "acc-customer", "CF_ACCOUNT_2_TOKEN": "tok-acc-customer",
                 "CF_ACCOUNT_2_DOMAINS": "example.com,example.org", "CF_ACCOUNT_2_FAILOVER": "",
                 "CF_STANDBY_DOMAIN": "example.net", "CF_CLOUD_IP": "192.0.2.10",
                 "MYSQL_PASSWORD": "e2e-legacy-db", "WORDPRESS_DB_PASSWORD": "e2e-legacy-db",
                 "MYSQL_ROOT_PASSWORD": "e2e-legacy-root", "REDIS_PASSWORD": "e2e-legacy-redis",
                 "WP_DOMAIN_CURRENT_SITE": "legacy.example.net", "WP_SUNRISE": "true"}.items():
        env = re.sub(rf"^{k}=.*$", f"{k}={v}", env, flags=re.M)
    (root / ".env").write_text(env + f"\nCF_API_URL=http://{node.host_ip}:{node.port}/client/v4\n")
    check(node.wpops("setup", "--node", "e2e").returncode == 0, "wpops setup --node e2e")

    # section 2: create, init, blogs
    for name in stacks:
        mode, domain, port, slug, subdirs, blogs = STACKS[name]
        a = ["stack", "create", name, "--mode", mode, "--domain", domain, "--port", str(port), "--login-path", slug]
        check(node.wpops(*a, *(["--subdirectories"] if subdirs else [])).returncode == 0, f"stack create {name}")
    r = node.wpops("stack", "create", stacks[0], "--mode", "single", "--domain", "x.example.net", "--port", "18189",
                   check_rc=False)
    check(r.returncode != 0 and "already exists" in r.stderr, "stack create refuses an existing name")
    r = node.wpops("stack", "create", "other", "--mode", "single", "--domain", "x.example.net",
                   "--port", str(STACKS[stacks[0]][2]), check_rc=False)
    check(r.returncode != 0 and "port" in r.stderr, "stack create refuses a used port")
    for name in stacks:
        r = node.wpops("stack", "init", name, "--admin-email", "admin@example.com", check_rc=False, timeout=900)
        check(r.returncode == 0 and '"installed"' in r.stdout, f"stack init {name}", r.stderr[-300:])
        check(node.env_value(name, "WP_ADMIN_PASSWORD") not in r.stdout, f"init output hides the admin password ({name})")
        r = node.wpops("stack", "init", name, "--admin-email", "admin@example.com", check_rc=False, timeout=900)
        check(r.returncode == 0 and "already-installed" in r.stdout, f"stack init {name} again is a no-op")
    for name in stacks:
        mode, domain, port, slug, subdirs, blogs = STACKS[name]
        node.wp(name, "option", "update", "blogname", f"MARK-{name}-1" if mode == "multisite" else f"MARK-{name}")
        for blog, bid in blogs:
            node.wp(name, "site", "create", f"--slug={blog}", f"--title=MARK-{name}-{blog}", "--email=admin@example.com")
        if mode == "multisite":
            urls = node.wp(name, "site", "list", "--field=url").split()
            check(all(u.startswith("https://") for u in urls), f"{name}: new blogs get https URLs", " ".join(urls))
        if name in OVERRIDES:
            bid, oslug = OVERRIDES[name]
            node.wpops("stack", "login", name, "--blog-id", str(bid), "--path", oslug)
            node.wpops("stack", "compose", name, "exec", "-T", "nginx", "nginx", "-s", "reload")
    listed = json.loads(node.wpops("stack", "list").stdout)
    check(sorted(listed) == sorted(stacks), "stack list shows every stack")
    r = node.wpops("stack", "compose", stacks[0], "exec", "-T", "nginx", "nginx", "-t")
    check("syntax is ok" in r.stderr + r.stdout, "stack compose passes Compose output through (nginx -t)")

    if legacy:
        sh(["docker", "compose", "up", "-d", "--wait"], cwd=root, timeout=900)
        lw = ["docker", "compose", "exec", "-T", "wp-cron", "wp"]
        sh(lw + ["core", "multisite-install", "--url=https://legacy.example.net", "--title=MARK-legacy-1",
                 "--admin_user=admin", "--admin_email=admin@example.com", "--admin_password=e2e-legacy-admin",
                 "--skip-email", "--skip-config", "--subdomains"], cwd=root)
        sh(lw + ["site", "create", "--slug=lblog", "--title=MARK-legacy-lblog", "--email=admin@example.com"], cwd=root)
        sh(lw + ["rewrite", "structure", "/%postname%/"], cwd=root)
        check(True, "legacy root multisite stack installed next to managed stacks")

    # section 3: inventory
    inv = json.loads((root / "config/inventory.json").read_text())
    inv["origins"]["primary"] = {"kind": "external", "targets": [{"type": "A", "content": "192.0.2.10"},
                                                                  {"type": "AAAA", "content": "2001:db8::10"}]}
    if legacy:
        inv["origins"]["e2e"]["stacks"]["legacy"] = {"path": str(root), "mode": "multisite", "service": "http://nginx:80"}
    for slug, stack, bid, host, domains, primary in sites:
        h = {"stack": stack, "host": host, **({"blog_id": bid} if bid else {})}
        hosts = {"e2e": h, **({"primary": {"host": domains[0]}} if primary == "primary" else {})}
        inv["sites"].append({"slug": slug, "primary": "primary" if primary == "primary" else "e2e",
                             "failover_to": "e2e", "cf_account": "customer", "domains": domains, "hosts": hosts})
    (root / "config/inventory.json").write_text(json.dumps(inv, indent=2))
    check(node.wpops("setup").returncode == 0, "wpops setup with sites (installs host-alias files)")

    # section 4: tunnels and routes
    r = node.wpops("tunnels", "ensure", check_rc=False, timeout=900)
    check(r.returncode == 0, "tunnels ensure", r.stderr[-300:] + r.stdout[-300:])
    tunnels_file = json.loads((root / "docker-compose.wpops-tunnels.json").read_text())
    networks = [n["name"] for n in tunnels_file["networks"].values()]
    check(networks[0] == "wpops-edge" and (not legacy or f"{root.name}_frontend" in networks),
          "connector project joins wpops-edge (and the legacy frontend)", str(networks))
    first = json.dumps(inv["origins"]["e2e"].get("tunnels"))
    node.wpops("tunnels", "ensure", timeout=900)
    again = json.loads((root / "config/inventory.json").read_text())["origins"]["e2e"]["tunnels"]
    check(len(again) == 2 and len(node.state()["tunnels"]) == 2, "tunnels ensure again reuses both tunnels", first)
    node.fail_next("GET", "acc-customer/cfd_tunnel/.*/token")
    node.wpops("tunnels", "ensure", check_rc=False, timeout=900)
    services = json.loads((root / "docker-compose.wpops-tunnels.json").read_text())["services"]
    check("cloudflared-customer" in services, "a Cloudflare error keeps the existing connector in the project")

    plan = json.loads(node.wpops("routes", "plan").stdout)
    check(not plan["refused"], "routes plan has no refusals", json.dumps(plan["refused"]))
    services_by_stack = {r["stack"]: r["service"] for r in plan["routes"]}
    for st in stacks:
        check(services_by_stack.get(st, f"http://wpops-{st}:80") == f"http://wpops-{st}:80", f"{st} routed to wpops-{st}")
    if legacy:
        check(services_by_stack.get("legacy") == f"http://{root.name}-nginx-1:80",
              "legacy stack routed to its container name", services_by_stack.get("legacy", ""))
    node.wpops("routes", "apply", "--yes")
    plan2 = json.loads(node.wpops("routes", "plan").stdout)
    check(all(not r["create_dns"] for r in plan2["routes"]), "routes apply again creates no DNS")
    node.wpops("routes", "apply", "--yes")

    routes = [f"{STACKS[s][1]}=http://wpops-{s}:80" for s in stacks]
    edge = Edge(node, networks, routes)
    markers = {"shop": "MARK-shop", "blog": "MARK-blog", "news": "MARK-network-news", "arts": "MARK-network-arts",
               "org": "MARK-paths-1", "legacy-blog": "MARK-legacy-lblog"}
    for slug, stack, bid, host, domains, primary in sites:
        code, _, body = edge.get(host)
        check(code == 200 and title(body) == markers[slug], f"routed standby host {host} serves {markers[slug]}",
              f"{code} {title(body)}")
        if primary == "node":
            for d in domains:
                code, _, body = edge.get(d)
                check(title(body) == markers[slug] and hosts_in(body) <= {d, "network.example.net"},
                      f"native production host {d} serves {slug} with its own URLs", str(hosts_in(body)))
    if legacy:
        ips = sh(["docker", "exec", "e2e-probe", "dig", "+short", "nginx", "@127.0.0.11"]).stdout.split()
        edge_only = sh(["docker", "run", "--rm", "--network", "wpops-edge", "alpine:3.23", "nslookup", "nginx", "127.0.0.11"],
                       check_rc=False).stdout
        check(len(re.findall(r"Address: 10\.214\.255\.", edge_only)) == len(stacks),
              "plain 'nginx' is ambiguous on wpops-edge (every managed stack answers)", edge_only[-300:])
        check(len(ips) >= 1, "'nginx' resolves from the connector networks", str(ips))

    # isolation
    for a in stacks:
        for b in stacks:
            if a == b:
                continue
            target = sh(["docker", "inspect", f"wps-{b}-mariadb-1", "--format",
                         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"]).stdout.strip()
            r = sh(["docker", "exec", f"wps-{a}-wordpress-1", "php", "-r",
                    f"echo @fsockopen('{target}', 3306, $e, $m, 2) ? 'open' : 'closed';"], check_rc=False)
            check(r.stdout.strip() == "closed", f"{a} cannot reach {b}'s MariaDB", r.stdout)
    r = sh([str(root / "scripts/wpops/bin/wpops"), "stack", "compose", stacks[0], "ps", "--format", "{{.Name}}"],
           cwd=root, env={**os.environ, "COMPOSE_PROJECT_NAME": f"wps-{stacks[-1]}",
                          "COMPOSE_FILE": str(root / "docker-compose.yml")})
    check(all(n.startswith(f"wps-{stacks[0]}-") for n in r.stdout.split()), "COMPOSE_* in the caller's shell is ignored")
    if len(stacks) > 1:
        node.wpops("stack", "stop", stacks[0])
        others = [edge.get(STACKS[s][1])[0] for s in stacks[1:]]
        check(all(c in (200, 302) for c in others), f"stopping {stacks[0]} leaves the other stacks serving", str(others))
        node.wpops("stack", "up", stacks[0], timeout=900)
        check(edge.get(STACKS[stacks[0]][1])[0] in (200, 302), f"stack up {stacks[0]} serves again")

    # failover / rollback
    before = node.records()
    fo = [s for s in sites if s[5] == "primary"]
    sel = ",".join(s[0] for s in fo)
    p = json.loads(node.wpops("--json", "failover", "plan", "--sites", sel, "--to", "e2e").stdout)
    check(p["summary"]["switch"] == sum(len(s[4]) for s in fo) and not p["refused"], "failover plan switches every domain",
          json.dumps(p["summary"]))
    r = node.wpops("failover", "apply", "--sites", sel, "--to", "e2e", "--yes", check_rc=False)
    check(r.returncode == 0, "failover apply", r.stdout[-400:])
    recs = {(x["name"], x["type"]): x for x in node.records()}
    for s in fo:
        for d in s[4]:
            x = recs.get((d, "CNAME"), {})
            check(x.get("content", "").endswith(".cfargotunnel.com") and x.get("proxied") is True,
                  f"{d} is a proxied tunnel CNAME", json.dumps(x))
    edge.reload()
    for slug, stack, bid, host, domains, primary in fo:
        for d in domains:
            code, _, body = edge.get(d)
            check(code == 200 and title(body) == markers[slug], f"{d} serves {markers[slug]} from {stack}", f"{code} {title(body)}")
            check(hosts_in(body) <= {d, "network.example.net", "legacy.example.net"},
                  f"{d} links point at {d}", str(hosts_in(body)))
    if "paths" in stacks:
        for path, mark in (("/alpha/", "MARK-paths-alpha"), ("/beta/", "MARK-paths-beta")):
            code, _, body = edge.get("example.org", path)
            check(code == 200 and title(body) == mark, f"example.org{path} serves the subdirectory blog", f"{code} {title(body)}")

    # custom login on production hostnames
    logins = {"shop": ("shop.example.com", "/staff-door/", "/wp-admin/"),
              "blog": ("blog.example.com", "/editors/", "/wp-admin/"),
              "network": ("news.example.com", "/news-editors/", "/wp-admin/"),
              "paths": ("example.org", "/alpha/alpha-editors/", "/alpha/wp-admin/")}
    for st in stacks:
        host, slug_path, admin = logins[st]
        check(edge.get(host, "/wp-login.php")[0] == 404, f"{host}/wp-login.php is hidden")
        code, redirect, _ = edge.get(host, "/wp-admin/")
        check(code in (302, 404) and "wp-login" not in redirect, f"{host}/wp-admin/ does not reveal the login", redirect)
        ok, detail = edge.login(host, slug_path, node.env_value(st, "WP_ADMIN_PASSWORD"), admin)
        check(ok, f"real login through {host}{slug_path}", detail)
    if "network" in stacks:
        check(edge.get("news.example.com", "/network-staff/")[0] == 404, "the network default slug is replaced on the overridden blog")
    if "paths" in stacks:
        check(edge.get("example.org", "/beta/paths-staff/")[0] == 200, "subdirectory blog without override uses the default slug")
    host, slug_path, _ = logins[stacks[0]]
    codes = [edge.get(host, slug_path, "-H", "X-Test-Client-IP: 203.0.113.5", "-o", "/dev/null")[0] for _ in range(25)]
    other = edge.get(host, slug_path, "-H", "X-Test-Client-IP: 198.51.100.9")[0]
    check(429 in codes and other == 200, "login rate limit is per visitor behind the connector", f"{codes} other={other}")

    # failure injection: a failed CNAME create must not leave the name empty
    node.wpops("failover", "rollback", "--all", "--yes")
    first = fo[0]
    node.fail_next("POST", "dns_records$")
    node.wpops("failover", "apply", "--sites", first[0], "--to", "e2e", "--yes", check_rc=False)
    names = {x["name"] for x in node.records()}
    check(first[4][0] in names, "after a failed CNAME create the original record is back")
    node.wpops("failover", "apply", "--sites", first[0], "--to", "e2e", "--yes")
    node.wpops("failover", "rollback", "--all", "--yes")
    after = node.records()
    check(after == before, "rollback restores the original DNS records exactly",
          json.dumps([x for x in after if x not in before]) + " / " + json.dumps([x for x in before if x not in after]))
    st = node.state()
    customer = [t for t in st["tunnels"].values() if t["name"].endswith("-customer")][0]
    left = {r.get("hostname") for r in customer["config"]["ingress"]}
    native = {d for s in sites if s[5] == "node" for d in s[4]}
    check(left - {None} == native, "rollback removes only the failover ingress rules", str(left))

    if args.siteguard and "shop" in stacks:
        backups(node, edge, Path(args.siteguard))

    log(f"\n{len(FAILS)} failure(s)" + (": " + "; ".join(FAILS) if FAILS else ""))
    return 1 if FAILS else 0


def backups(node: Node, edge: Edge, siteguard: Path) -> None:
    """SiteGuard: pull from shop (source profile) and restore onto a new single stack."""
    root = node.root
    node.wpops("stack", "create", "shop2", "--mode", "single", "--domain", "shop2.example.net", "--port", "18190",
               "--login-path", "shop2-door", "--noindex")
    node.wpops("stack", "init", "shop2", "--admin-email", "admin@example.com", timeout=900)
    for st in ("shop", "shop2"):
        dst = root / "stacks" / st / "data/wordpress/wp-content/plugins/siteguardbackup"
        sh(["cp", "-a", str(siteguard), str(dst)])
        sh(["chown", "-R", "33:33", str(dst)])
        node.wp(st, "plugin", "activate", "siteguardbackup")
    key = re.search(r"sgs_[0-9a-f]+", node.wp("shop", "siteguardbackup", "sync_api_key", "--regenerate")).group(0)
    with open(root / ".env", "a") as fh:
        fh.write(f"WPOPS_SYNC_KEY_PRIMARY_SHOP={key}\n")
    inv = json.loads((root / "config/inventory.json").read_text())
    inv["backup_sources"] = {"primary-shop": {"origin": "primary", "sync_api": "https://shop.example.net"}}
    (root / "config/inventory.json").write_text(json.dumps(inv, indent=2))
    edge_ip = sh(["docker", "inspect", "e2e-edge", "--format",
                  '{{(index .NetworkSettings.Networks "wpops-edge").IPAddress}}']).stdout.strip()
    hosts = Path("/etc/hosts").read_text()
    if "shop.example.net" not in hosts:
        with open("/etc/hosts", "a") as fh:
            fh.write(f"{edge_ip} shop.example.net\n")
    node.wp("shop", "post", "create", "--post_title=RESTORE-MARK", "--post_status=publish")
    r = node.wpops("backup", "pull", "--source", "primary-shop", check_rc=False, timeout=1800)
    check(r.returncode == 0, "backup pull from a source profile", r.stdout[-500:])
    sets = node.wpops("--json", "backup", "list", "--source", "primary-shop")
    ref = json.loads(sets.stdout)[0]["id"]
    r = node.wpops("restore", "apply", "--set", ref, "--stack", "shop2", "--all", "--yes", check_rc=False, timeout=3600)
    check(r.returncode == 0, "restore apply onto another stack", r.stdout[-500:])
    edge.routes.append("shop2.example.net=http://wpops-shop2:80")
    edge.reload()
    code, _, body = edge.get("shop2.example.net")
    check("RESTORE-MARK" in body and hosts_in(body) <= {"shop2.example.net"}, "restored content serves under the destination host")
    check(edge.get("shop2.example.net", "/shop2-door/")[0] == 200 and edge.get("shop2.example.net", "/staff-door/")[0] == 404,
          "the destination's login policy survives the restore")


if __name__ == "__main__":
    sys.exit(main())
