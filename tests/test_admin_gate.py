"""Admin gate: which stacks and host names are gated, the rendered Nginx config, apply with rollback, and a
live Nginx check of gated and public paths (set WPOPS_TEST_NGINX, as for the rate-limit test)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "wpops"))
from wpops import gate  # noqa: E402
from wpops.config import ConfigError, Inventory, Origin, Site  # noqa: E402

CFG = {"node": "n1", "admin_gate": {"enabled": True, "auth_url": "https://auth.example.net", "default": True}}


def inventory(root: Path, stacks: dict, sites: list) -> Inventory:
    origin = Origin(name="n1", kind="node", standby_domain="standby.example.net", staging_suffix="n1",
                    stacks={n: {"path": str(root / n), "mode": "single", "managed": True, **extra}
                            for n, extra in stacks.items()})
    inv = Inventory.__new__(Inventory)
    inv.origins = {"n1": origin}
    inv.sites = sites
    inv.backup_sources = {}
    inv.raw = {}
    return inv


def site(slug, domains, stack, host, site_host=""):
    h = {"stack": stack, "host": host}
    if site_host:
        h["site_host"] = site_host
    return Site(slug=slug, name=slug, primary="n1", cf_account="a", domains=domains, hosts={"n1": h})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name in ("shop", "exempt", "fresh"):
            (self.root / name / "nginx").mkdir(parents=True)
        (self.root / "fresh" / ".env").write_text('WP_DOMAIN_CURRENT_SITE="fresh.example"\n')
        self.inv = inventory(self.root, {"shop": {}, "exempt": {"admin_gate": False}, "fresh": {}}, [
            site("shop", ["shop.example", "www.shop.example"], "shop", "shopexample-n1.standby.example.net", "shop.example"),
            site("exempt", ["exempt.example"], "exempt", "exemptexample-n1.standby.example.net", "exempt.example"),
        ])
        p = patch.object(gate.state, "locked"); p.start(); self.addCleanup(p.stop)
        p = patch.object(gate.state, "audit"); p.start(); self.addCleanup(p.stop)


class Decisions(Base):
    def test_hosts_of_a_mapped_stack(self):
        h = gate.stack_hosts(self.inv, "n1", "shop")
        self.assertEqual(h["canonical"], "shop.example")
        self.assertEqual(h["gated"], ["shop.example", "shopexample-n1.standby.example.net"])
        self.assertEqual(h["redirect"], {"www.shop.example": "shop.example"})

    def test_stack_without_a_site_uses_its_own_domain(self):
        h = gate.stack_hosts(self.inv, "n1", "fresh")
        self.assertEqual(h["gated"], ["fresh.example", "freshexample-n1.standby.example.net"])
        (self.root / "fresh" / ".env").write_text('WP_DOMAIN_CURRENT_SITE="draft.standby.example.net"\n')
        self.assertEqual(gate.stack_hosts(self.inv, "n1", "fresh")["gated"], ["draft.standby.example.net"])

    def test_flag_overrides_node_default_and_disabled_gates_nothing(self):
        o = self.inv.origin("n1")
        self.assertTrue(gate.wanted(o, "shop", CFG))
        self.assertFalse(gate.wanted(o, "exempt", CFG))
        off = {"node": "n1", "admin_gate": {**CFG["admin_gate"], "default": False}}
        self.assertFalse(gate.wanted(o, "shop", off))
        o.stacks["shop"]["admin_gate"] = True
        self.assertTrue(gate.wanted(o, "shop", off))
        self.assertFalse(gate.wanted(o, "shop", {"node": "n1", "admin_gate": {"enabled": False, "auth_url": "https://a.example"}}))
        self.assertFalse(gate.wanted(o, "shop", {"node": "n1", "admin_gate": {"enabled": True, "auth_url": ""}}))
        o.stacks["shop"]["managed"] = False
        self.assertFalse(gate.wanted(o, "shop", CFG))

    def test_auth_url_must_be_a_plain_https_host(self):
        for bad in ("http://auth.example.net", "https://auth.example.net/x", "https://auth.example.net;evil"):
            with self.assertRaises(ConfigError):
                gate.settings({"admin_gate": {"enabled": True, "auth_url": bad}})

    def test_hosts_lists_only_gated_stacks(self):
        rows = gate.hosts(self.inv, CFG)
        self.assertEqual([r["stack"] for r in rows], ["fresh", "shop"])


class Rendering(Base):
    def test_config_gates_login_admin_and_custom_paths(self):
        conf = gate.render(gate.stack_hosts(self.inv, "n1", "shop"), gate.settings(CFG), ["staff-door"])
        self.assertIn("if ($host = shopexample-n1.standby.example.net) { set $wpops_gate_known 1; }", conf)
        self.assertIn("set $wpops_gate_canonical shop.example;", conf)
        self.assertIn("location ^~ /wp-admin/", conf)
        self.assertIn("(?:staff\\-door)", conf)
        self.assertIn("set $wpops_gate_auth https://auth.example.net;", conf)
        self.assertIn('if ($wpops_gate_postpass = "1:POST:action=postpass") { set $wpops_gate_login 3; }', conf)
        self.assertNotIn("$arg_action", conf)   # first-argument semantics: ?action=postpass&action=login got through
        self.assertIn("fastcgi_param SCRIPT_FILENAME $document_root/wp-content/wpops-postpass.php;", conf)
        self.assertIn("location = /wp-content/wpops-postpass.php { return 404; }", conf)

    def test_invalid_host_name_is_refused(self):
        with self.assertRaises(ConfigError):
            gate.render({"canonical": "shop.example", "gated": ["shop.example;}"], "redirect": {}}, gate.settings(CFG))


class Apply(Base):
    def run_apply(self, running=True, test_rc=0):
        calls = []

        def fake_compose(origin, name, *args, **kw):
            calls.append((name, args))
            return subprocess.CompletedProcess(args, test_rc if args[-1] == "-t" else 0, "", "boom")
        with patch.object(gate, "compose", fake_compose), patch.object(gate, "_nginx_running", return_value=running):
            return gate.apply(self.inv, CFG), calls

    def test_writes_gated_stacks_removes_legacy_and_exempt_files(self):
        (self.root / "shop/nginx/kc-authentik-gate.conf").write_text("old")
        (self.root / "exempt/nginx" / gate.CONF).write_text("stale")
        r, calls = self.run_apply()
        self.assertEqual(sorted(r["changed"]), ["exempt", "fresh", "shop"])
        self.assertEqual(r["failed"], {})
        self.assertTrue((self.root / "shop/nginx" / gate.CONF).exists())
        self.assertTrue((self.root / "shop/nginx" / gate.PHP_INC).exists())
        self.assertFalse((self.root / "shop/nginx/kc-authentik-gate.conf").exists())
        self.assertFalse((self.root / "exempt/nginx" / gate.CONF).exists())
        self.assertIn(("shop", ("exec", "-T", "nginx", "nginx", "-s", "reload")), calls)
        again, calls = self.run_apply()
        self.assertEqual((again["changed"], calls), ([], []))
        self.assertEqual({x["stack"]: x["state"] for x in gate.status(self.inv, CFG)},
                         {"shop": "ok", "fresh": "ok", "exempt": "off"})

    def test_failed_nginx_test_restores_previous_files(self):
        legacy = self.root / "shop/nginx/kc-authentik-gate.conf"
        legacy.write_text("old gate")
        r, _ = self.run_apply(test_rc=1)
        self.assertIn("shop", r["failed"])
        self.assertEqual(legacy.read_text(), "old gate")
        self.assertFalse((self.root / "shop/nginx" / gate.CONF).exists())

    def test_node_without_the_gate_leaves_a_hand_installed_gate_alone(self):
        legacy = self.root / "shop/nginx/kc-authentik-gate.conf"
        legacy.write_text("hand installed")
        (self.root / "fresh/nginx" / gate.CONF).write_text("ours, from when the gate was on")
        off = {"node": "n1", "admin_gate": {"enabled": False, "auth_url": "https://auth.example.net"}}
        with patch.object(gate, "compose") as c, patch.object(gate, "_nginx_running", return_value=False):
            r = gate.apply(self.inv, off)
        self.assertEqual(r["changed"], ["fresh"])
        self.assertEqual(legacy.read_text(), "hand installed")
        self.assertFalse((self.root / "fresh/nginx" / gate.CONF).exists())
        c.assert_not_called()

    def test_postpass_entry_point_follows_the_gate(self):
        for name in ("shop", "exempt"):
            (self.root / name / "data/wordpress/wp-content").mkdir(parents=True)
        (self.root / "exempt/data/wordpress/wp-content" / gate.POSTPASS).write_text("left over")
        self.run_apply()
        helper = self.root / "shop/data/wordpress/wp-content" / gate.POSTPASS
        self.assertEqual(helper.read_text(), gate.POSTPASS_TEXT)
        self.assertFalse((self.root / "exempt/data/wordpress/wp-content" / gate.POSTPASS).exists())
        # Lost in a restore of wp-content: reported, and put back without touching Nginx.
        helper.unlink()
        self.assertEqual({x["stack"]: x["state"] for x in gate.status(self.inv, CFG)}["shop"], "outdated")
        r, calls = self.run_apply()
        self.assertEqual((r["changed"], calls), (["shop"], []))
        self.assertTrue(helper.exists())

    def test_postpass_entry_point_keeps_only_the_post_password(self):
        if not shutil.which("php"):
            self.skipTest("php not installed")
        web = self.root / "web"
        (web / "wp-content").mkdir(parents=True)
        (web / "wp-content" / gate.POSTPASS).write_text(gate.POSTPASS_TEXT)
        (web / "wp-login.php").write_text("<?php echo json_encode(array($_GET, $_POST, $_REQUEST, $_SERVER['QUERY_STRING']));")
        driver = ("$_SERVER['REQUEST_METHOD']=$argv[1]; $_SERVER['QUERY_STRING']='action=postpass&key=k';"
                  "$_GET=array('action'=>'postpass','key'=>'k','checkemail'=>'1');"
                  "$_POST=array('action'=>'login','log'=>'admin','pwd'=>'x','post_password'=>'secret','redirect_to'=>'https://evil.example');"
                  "$_REQUEST=array_merge($_GET,$_POST); require $argv[2];")
        out = subprocess.run(["php", "-r", driver, "POST", str(web / "wp-content" / gate.POSTPASS)], capture_output=True, text=True)
        get, post, request, query = json.loads(out.stdout)
        self.assertEqual(get, {"action": "postpass"})
        self.assertEqual(post, {"post_password": "secret"})
        self.assertEqual(request, {"action": "postpass", "post_password": "secret"})
        self.assertEqual(query, "action=postpass")
        out = subprocess.run(["php", "-r", driver, "GET", str(web / "wp-content" / gate.POSTPASS)], capture_output=True, text=True)
        self.assertEqual(out.stdout, "")

    def test_stopped_stack_gets_files_without_nginx_calls(self):
        r, calls = self.run_apply(running=False)
        self.assertEqual(calls, [])
        self.assertIn("shop", r["changed"])

    def test_status_reports_missing_and_outdated(self):
        self.run_apply()
        (self.root / "shop/nginx" / gate.CONF).write_text("edited by hand")
        (self.root / "fresh/nginx" / gate.CONF).unlink()
        st = {x["stack"]: x["state"] for x in gate.status(self.inv, CFG)}
        self.assertEqual((st["shop"], st["fresh"]), ("outdated", "missing"))


AUTH_SERVER = r"""
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = "session=ok" in (self.headers.get("Cookie") or "")
        self.send_response(200 if ok else 401); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 18098), H).serve_forever()
"""


class LiveNginx(Base):
    @unittest.skipUnless(os.environ.get("WPOPS_TEST_NGINX") and shutil.which("unshare") and shutil.which("ip") and shutil.which("curl"),
                         "set WPOPS_TEST_NGINX to an nginx binary (needs unshare/ip/curl for a private network namespace)")
    def test_gated_and_public_paths(self):
        if subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode:
            self.skipTest("no permission for a private network namespace")
        nginx = os.environ["WPOPS_TEST_NGINX"]
        conf = self.root / "nginx"
        for d in ("conf.d", "wpops", "wpops-http", "logs", "tmp", "www/wp-admin"):
            (conf / d).mkdir(parents=True)
        (conf / "www/wp-content").mkdir()
        for f in ("wp-login.php", "index.php", "wp-admin/index.php", "wp-admin/admin-ajax.php", "wp-content/" + gate.POSTPASS):
            (conf / "www" / f).write_text("<?php")
        mime = Path(nginx).resolve().parents[2] / "etc/nginx/mime.types"
        (conf / "mime.types").write_text(mime.read_text() if mime.exists() else "types { text/html html; }\n")
        (conf / "fastcgi_params").write_text("")
        for f in (ROOT / "config/nginx/conf.d").glob("*.conf"):
            text = f.read_text().replace("wordpress:9000", "127.0.0.1:9").replace("listen 80;", "listen 127.0.0.1:18099;")
            (conf / "conf.d" / f.name).write_text(text.replace("/var/www/html", str(conf / "www")).replace("/etc/nginx", str(conf)))
        main = (ROOT / "config/nginx/nginx.production.conf").read_text().replace("/etc/nginx", str(conf))
        main = main.replace("user  nginx;", "user root root;").replace("/var/log/nginx", str(conf / "logs")).replace("/var/run/nginx.pid", str(conf / "nginx.pid"))
        temp = " ".join(f"{k}_temp_path {conf}/tmp/{k};" for k in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        (conf / "nginx.conf").write_text(main.replace("http {", "http {\n    " + temp, 1))
        gated = gate.render(gate.stack_hosts(self.inv, "n1", "shop"), gate.settings(CFG), ["staff-door"])
        # The test outpost is plain HTTP on loopback; everything else is the file wpops would write.
        gated = gated.replace("set $wpops_gate_auth https://auth.example.net;", "set $wpops_gate_auth http://127.0.0.1:18098;")
        (conf / "wpops" / gate.CONF).write_text(gated.replace("/etc/nginx", str(conf)))
        (conf / "wpops" / gate.PHP_INC).write_text(gate.PHP_INC_TEXT)
        (conf / "auth.py").write_text(AUTH_SERVER)
        script = r"""set -e
ip link set lo up
python3 "$2/auth.py" & AUTH=$!
"$1" -t -c "$2/nginx.conf" -e "$2/logs/error.log"
"$1" -c "$2/nginx.conf" -e "$2/logs/error.log"
sleep 0.7
get() { name=$1; shift; printf '%s ' "$name"; curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$@"; }
B=http://127.0.0.1:18099
get login        -H 'Host: shop.example' $B/wp-login.php
get admin        -H 'Host: shop.example' "$B/wp-admin/?x=1"
get staging      -H 'Host: shopexample-n1.standby.example.net' $B/wp-admin/
get blog-login   -H 'Host: shop.example' $B/blog/wp-login.php
get custom       -H 'Host: shop.example' $B/staff-door/
get www          -H 'Host: www.shop.example' $B/wp-login.php
get postpass     -H 'Host: shop.example' -X POST "$B/wp-login.php?action=postpass"
get postpass-get -H 'Host: shop.example' "$B/wp-login.php?action=postpass"
get postpass-dup -H 'Host: shop.example' -X POST "$B/wp-login.php?action=postpass&action=login"
get postpass-pre -H 'Host: shop.example' -X POST "$B/wp-login.php?action=login&action=postpass"
get postpass-enc -H 'Host: shop.example' -X POST "$B/wp-login.php?action=postpass%26action=login"
get admin-pp     -H 'Host: shop.example' -X POST "$B/wp-admin/index.php?action=postpass"
get helper       -H 'Host: shop.example' -X POST $B/wp-content/wpops-postpass.php
get ajax         -H 'Host: shop.example' $B/wp-admin/admin-ajax.php
get home         -H 'Host: shop.example' $B/
get authed       -H 'Host: shop.example' -H 'Cookie: session=ok' $B/wp-admin/index.php
get authed-login -H 'Host: shop.example' -H 'Cookie: session=ok' -X POST $B/wp-login.php
"$1" -c "$2/nginx.conf" -e "$2/logs/error.log" -s stop
kill $AUTH
"""
        r = subprocess.run(["unshare", "-rn", "bash", "-c", script, "t", nginx, str(conf)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = {l.split(" ", 1)[0]: l.split(" ", 1)[1].strip() for l in r.stdout.splitlines() if " " in l}
        start = "/outpost.goauthentik.io/start?rd=https://"
        self.assertEqual(got["login"], "302 https://shop.example" + start + "shop.example/wp-login.php", r.stdout)
        self.assertEqual(got["admin"], "302 https://shop.example" + start + "shop.example/wp-admin/?x=1", r.stdout)
        self.assertTrue(got["staging"].startswith("302 https://shopexample-n1.standby.example.net" + start), r.stdout)
        self.assertTrue(got["blog-login"].startswith("302 https://shop.example" + start), r.stdout)
        self.assertTrue(got["custom"].startswith("302 https://shop.example" + start), r.stdout)
        # A name without its own provider is sent to the site's own host name.
        self.assertEqual(got["www"], "302 https://shop.example/wp-login.php", r.stdout)
        # Public paths reach PHP (502: no PHP in this test), not the outpost.
        for name in ("postpass", "ajax", "home"):
            self.assertEqual(got[name].split()[0], "502", name + ": " + r.stdout)
        # Anything but the exact postpass POST is gated.
        for name in ("postpass-get", "postpass-dup", "postpass-pre", "postpass-enc", "admin-pp"):
            self.assertTrue(got[name].startswith("302 https://shop.example" + start), name + ": " + r.stdout)
        self.assertEqual(got["helper"].split()[0], "404", r.stdout)   # the entry point is not reachable by its URL
        # A signed-in request passes the gate and reaches PHP, also as a POST to the login.
        self.assertEqual(got["authed"].split()[0], "502", r.stdout)
        self.assertEqual(got["authed-login"].split()[0], "502", r.stdout)


if __name__ == "__main__":
    unittest.main()
