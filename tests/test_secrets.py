"""Secrets in .env files are 1Password references (op://...) resolved at runtime."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "wpops"))
from wpops import config, stacks, tunnels, vault  # noqa: E402

SECRETS = {
    "op://V/stack-a/mysql_password": "db-pass-123",
    "op://V/stack-a/redis_password": "redis \"quoted\" pass",
    "op://V/cloudflare-one/credential": "cf-token-one",
}

# A stand-in for the op CLI: `inject` replaces {{ op://... }}, `read` prints one
# value. Every call is appended to $FAKE_OP_LOG; $FAKE_OP_FAIL makes it fail.
FAKE_OP = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, re, sys
    secrets = json.loads(os.environ["FAKE_OP_SECRETS"])
    with open(os.environ["FAKE_OP_LOG"], "a") as fh:
        fh.write(" ".join(sys.argv[1:2]) + " token=" + str(bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"))) + "\\n")
    if os.environ.get("FAKE_OP_FAIL"):
        sys.stderr.write("[ERROR] could not connect to 1Password\\n"); sys.exit(1)
    if sys.argv[1] == "inject":
        def sub(m):
            ref = m.group(1)
            if ref not in secrets:
                sys.stderr.write(f"[ERROR] could not resolve {ref}\\n"); sys.exit(1)
            return secrets[ref]
        # Like the real CLI, inject drops control characters from the template.
        text = re.sub(r"[\\x00-\\x08\\x0b-\\x1f]", "", sys.stdin.read())
        sys.stdout.write(re.sub(r"\\{\\{ (op://[^ ]+) \\}\\}", sub, text))
    elif sys.argv[1] == "read":
        ref = sys.argv[-1]
        if ref not in secrets:
            sys.stderr.write(f"[ERROR] could not resolve {ref}\\n"); sys.exit(1)
        sys.stdout.write(secrets[ref])
""")


class OpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / "stacks").mkdir(parents=True)
        self.op = self.tmp / "op"
        self.op.write_text(FAKE_OP)
        self.op.chmod(0o755)
        self.log = self.tmp / "op.log"
        self.token = self.tmp / "token"
        self.token.write_text("svc-token\n")
        env = {"FAKE_OP_SECRETS": json.dumps(SECRETS), "FAKE_OP_LOG": str(self.log)}
        self.patches = [
            patch.dict(os.environ, env),
            patch.object(config, "OP_BIN", str(self.op)),
            patch.object(config, "OP_TOKEN_FILE", self.token),
            patch.object(config, "RUNTIME_DIR", self.tmp / "run"),
            patch.object(config, "REPO_DIR", self.repo),
            patch.object(config, "ENV_FILE", self.repo / ".env"),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
        config._warned.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def env_file(self, text, name=".env"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
        return path

    def node_files(self):
        root = self.env_file("""\
            CF_ACCOUNT_1_NAME=one
            CF_ACCOUNT_1_ID=abc
            CF_ACCOUNT_1_TOKEN=op://V/cloudflare-one/credential
            """)
        stack = self.env_file("""\
            HTTP_PORT=8081
            MYSQL_PASSWORD="op://V/stack-a/mysql_password"
            REDIS_PASSWORD=op://V/stack-a/redis_password
            """, "stacks/a/.env")
        return root, stack


class Cache(OpTestCase):
    def test_one_batched_call_for_the_whole_node_then_cache_only(self):
        root, stack = self.node_files()
        env = config.load_env(stack)
        self.assertEqual(env["HTTP_PORT"], "8081")
        self.assertEqual(env["MYSQL_PASSWORD"], "db-pass-123")
        self.assertEqual(env["REDIS_PASSWORD"], 'redis "quoted" pass')
        # The root .env was resolved by the same call; later processes read the cache.
        self.assertEqual(config.cf_accounts(config.load_env(root))["one"].token, "cf-token-one")
        for _ in range(5):
            config.load_env(stack)
        self.assertEqual(self.calls(), ["inject token=True"])

    def test_cache_is_private_tmpfs_style(self):
        _, stack = self.node_files()
        config.load_env(stack)
        d = config.cache_dir()
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((d / "secrets.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(config.RUNTIME_DIR.stat().st_mode), 0o700)

    def test_changed_env_file_is_read_again(self):
        _, stack = self.node_files()
        config.load_env(stack)
        stack.write_text(stack.read_text() + "WP_MODE=single\n")
        config.load_env(stack)
        self.assertEqual(len(self.calls()), 2)

    def test_refresh_command_reads_once(self):
        self.node_files()
        r = config.refresh_cache()
        self.assertEqual((r["files"], r["refs"]), (2, 3))
        self.assertEqual(len(self.calls()), 1)

    def test_resolve_false_keeps_references(self):
        _, stack = self.node_files()
        self.assertEqual(config.load_env(stack, resolve=False)["MYSQL_PASSWORD"], "op://V/stack-a/mysql_password")
        self.assertEqual(self.calls(), [])

    def test_plain_files_never_call_op(self):
        path = self.env_file("HTTP_PORT=8081\n")
        self.assertEqual(config.load_env(path), {"HTTP_PORT": "8081"})
        self.assertEqual(self.calls(), [])

    def test_down_with_a_cache_keeps_working_and_backs_off(self):
        root, stack = self.node_files()
        config.load_env(stack)
        stack.write_text(stack.read_text() + "WP_MODE=single\n")     # cache now stale
        with patch.dict(os.environ, {"FAKE_OP_FAIL": "1"}):
            self.assertEqual(config.load_env(stack)["MYSQL_PASSWORD"], "db-pass-123")
            for _ in range(5):                                       # within the backoff: no more calls
                self.assertEqual(config.cf_accounts(config.load_env(root))["one"].token, "cf-token-one")
        self.assertEqual(len(self.calls()), 2)
        b = config.backoff_state()
        self.assertEqual(b["failures"], 1)
        self.assertIn("1Password", b["error"])
        self.assertGreaterEqual(b["next_try"] - b["failed_at"], config.BACKOFF_MIN - 1)

    def test_down_without_a_cache(self):
        root, stack = self.node_files()
        with patch.dict(os.environ, {"FAKE_OP_FAIL": "1"}):
            env = config.load_env(stack)                      # missing secrets are left out, with a warning
            self.assertNotIn("MYSQL_PASSWORD", env)
            self.assertEqual(env["HTTP_PORT"], "8081")
            with self.assertRaises(config.SecretError) as cm:  # compose needs them: refuse
                config.secret_env(stack, {"MYSQL_PASSWORD"})
        self.assertIn("MYSQL_PASSWORD", str(cm.exception))
        self.assertIsInstance(cm.exception, config.ConfigError)
        self.assertEqual(len(self.calls()), 1)               # backoff: one attempt only

    def test_unknown_reference_is_a_clear_error(self):
        self.env_file("A=op://V/missing/field\n")
        with self.assertRaises(config.SecretError) as cm:
            config.refresh_cache()
        self.assertIn("op://V/missing/field", str(cm.exception))

    def test_missing_token_file(self):
        self.node_files()
        with patch.object(config, "OP_TOKEN_FILE", self.tmp / "absent"):
            with self.assertRaises(config.SecretError) as cm:
                config.refresh_cache()
        self.assertIn("service account token", str(cm.exception))

    def test_secret_env_only_needs_what_compose_uses(self):
        _, stack = self.node_files()
        stack.write_text(stack.read_text() + "WP_ADMIN_PASSWORD=op://V/stack-a/not-there\n")
        with self.assertRaises(config.SecretError):
            config.refresh_cache()
        # Seeded values for what compose interpolates are enough.
        config._write_cache_json("secrets.json", {"values": {"op://V/stack-a/mysql_password": "db-pass-123",
                                                             "op://V/stack-a/redis_password": "r"}, "sources": {}})
        env = config.secret_env(stack, {"MYSQL_PASSWORD", "REDIS_PASSWORD"})
        self.assertEqual(env, {"MYSQL_PASSWORD": "db-pass-123", "REDIS_PASSWORD": "r"})

    def test_boot_refresh_skips_a_fresh_cache(self):
        self.node_files()
        self.assertTrue(vault.refresh_boot(log=lambda m: None)["read"])
        self.assertFalse(vault.refresh_boot(log=lambda m: None)["read"])
        self.assertEqual(len(self.calls()), 1)

    def test_boot_refresh_gives_up_quietly(self):
        self.node_files()
        with patch.dict(os.environ, {"FAKE_OP_FAIL": "1"}), patch.object(vault.time, "sleep"):
            self.assertFalse(vault.refresh_boot(attempts=3, log=lambda m: None)["ok"])
        self.assertEqual(len(self.calls()), 3)


class AgentAlerts(OpTestCase):
    def run_loop_once(self):
        from wpops import agent
        a = object.__new__(agent.Agent)
        a._stop = SimpleNamespace(wait=lambda s: True)
        a.node = lambda: "node1"
        sent = []
        with patch.object(agent.notify, "notify", side_effect=lambda kind, *a, **k: sent.append(kind)):
            a.secrets_loop()
        return sent

    def test_alert_once_then_recovery_once(self):
        self.node_files()
        with patch.dict(os.environ, {"FAKE_OP_FAIL": "1"}):
            self.assertEqual(self.run_loop_once(), ["secrets.unavailable"])
            b = config.backoff_state()
            config._write_cache_json("backoff.json", {**b, "next_try": 0})   # backoff elapsed
            self.assertEqual(self.run_loop_once(), [])                       # still down: no second alert
        config._write_cache_json("backoff.json", {**config.backoff_state(), "next_try": 0})
        self.assertEqual(self.run_loop_once(), ["secrets.restored"])
        self.assertEqual(self.run_loop_once(), [])
        self.assertEqual(len(self.calls()), 3)                                # never on a timer once filled


class ComposeEnvironment(OpTestCase):
    def run_compose(self, *args):
        _, stack = self.node_files()
        compose_file = self.repo / "docker-compose.stack.yml"
        compose_file.write_text("services:\n  db:\n    environment:\n      P: ${MYSQL_PASSWORD}\n      R: $REDIS_PASSWORD\n")
        stack.write_text(stack.read_text() + f"COMPOSE_FILE={compose_file}\nCOMPOSE_PROJECT_NAME=wps-a\n")
        origin = SimpleNamespace(name="n", stacks={"a": {"path": str(stack.parent)}})
        seen = {}
        real_run = subprocess.run
        def fake_run(cmd, **kw):
            if cmd[0] != "docker":          # the op CLI stand-in runs for real
                return real_run(cmd, **kw)
            seen.update(kw["env"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(stacks.subprocess, "run", side_effect=fake_run), \
             patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "other"}):
            stacks.compose(origin, "a", *args)
        return seen

    def test_up_gets_resolved_secrets_as_process_env(self):
        seen = self.run_compose("up", "-d")
        self.assertEqual(seen["MYSQL_PASSWORD"], "db-pass-123")
        self.assertNotIn("HTTP_PORT", seen)           # compose reads non-secrets from .env itself
        self.assertNotIn("COMPOSE_PROJECT_NAME", seen)

    def test_read_only_commands_never_touch_1password(self):
        for args in (("ps",), ("exec", "-T", "wp-cron", "wp"), ("logs",), ("stop", "wp-cron"), ("config",)):
            seen = self.run_compose(*args)
            self.assertNotIn("MYSQL_PASSWORD", seen)
        self.assertEqual(self.calls(), [])

    def test_compose_variables(self):
        self.run_compose("ps")
        self.assertEqual(stacks.compose_variables(self.repo / "stacks" / "a"), {"MYSQL_PASSWORD", "REDIS_PASSWORD"})


class TunnelFile(unittest.TestCase):
    def test_token_variable_names(self):
        self.assertEqual(tunnels.token_var("the-promise-center-of-homewood"),
                         "TUNNEL_TOKEN_THE_PROMISE_CENTER_OF_HOMEWOOD")

    def test_ensure_writes_no_tokens_and_passes_them_at_up(self):
        env = {"CF_ACCOUNT_1_NAME": "one", "CF_ACCOUNT_1_ID": "id1", "CF_ACCOUNT_1_TOKEN": "cf-secret-1",
               "CF_ACCOUNT_2_NAME": "two", "CF_ACCOUNT_2_ID": "id2", "CF_ACCOUNT_2_TOKEN": "cf-secret-2"}
        origin = config.Origin(name="node1", kind="node", stacks={}, tunnels={"one": "t1", "two": "t2"})
        inv = SimpleNamespace(origin=lambda n: origin, raw={})

        class FakeCf:
            def __init__(self, token, account_id, name):
                self.name = name
            def verify_token(self):
                return {"status": "active"}
            def tunnel(self, tid):
                if self.name == "two":
                    raise tunnels.CfError("HTTP 500")
                return {"id": tid}
            def tunnel_token(self, tid):
                return "tunnel-secret-" + tid

        tmp = Path(tempfile.mkdtemp())
        compose = tmp / "docker-compose.wpops-tunnels.json"
        # An old file with literal tokens, as written by earlier versions.
        compose.write_text(json.dumps({"services": {
            "cloudflared-one": {"environment": {"TUNNEL_TOKEN": "tunnel-secret-t1"}},
            "cloudflared-two": {"environment": {"TUNNEL_TOKEN": "tunnel-secret-t2"}}}}))
        runs = []
        def fake_run(cmd, **kw):
            runs.append((cmd, kw["env"]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(tunnels, "REPO_DIR", tmp), \
             patch.object(tunnels, "load_env", return_value=env), \
             patch.object(tunnels, "node_config", return_value={"node": "node1"}), \
             patch.object(tunnels, "load_inventory", return_value=inv), \
             patch.object(tunnels, "Cloudflare", FakeCf), \
             patch.object(tunnels.subprocess, "run", side_effect=fake_run), \
             patch.object(tunnels, "publish_staging", return_value={"ok": True}), \
             patch.object(tunnels.state, "audit"), \
             patch("wpops.provision.ensure_edge"):
            tunnels.ensure(log=lambda m: None)
        text = compose.read_text()
        self.assertNotIn("secret", text)
        services = json.loads(text)["services"]
        self.assertEqual(services["cloudflared-one"]["environment"], {"TUNNEL_TOKEN": "${TUNNEL_TOKEN_ONE}"})
        # "two" failed at the API: its service stays in the file but is not recreated.
        self.assertIn("cloudflared-two", services)
        self.assertEqual(services["cf-backup"]["environment"],
                         {"CF_ACCOUNT_1_TOKEN": "${CF_ACCOUNT_1_TOKEN}", "CF_ACCOUNT_2_TOKEN": "${CF_ACCOUNT_2_TOKEN}"})
        self.assertEqual(stat.S_IMODE(compose.stat().st_mode), 0o600)
        (cmd, child_env), = runs
        self.assertEqual(cmd[-2:], ["cloudflared-one", "cf-backup"])
        self.assertEqual(child_env["TUNNEL_TOKEN_ONE"], "tunnel-secret-t1")
        self.assertNotIn("TUNNEL_TOKEN_TWO", child_env)
        self.assertEqual(child_env["CF_ACCOUNT_2_TOKEN"], "cf-secret-2")
        self.assertEqual(vault.check_tunnels_file(compose)["literal_tokens"], [])


class SecretsCheck(OpTestCase):
    def test_plaintext_keys_are_reported_by_name(self):
        path = self.env_file("""\
            HTTP_PORT=8081
            CF_ACCOUNT_1_ID=abc
            CF_ACCOUNT_1_TOKEN=plain-token
            MYSQL_PASSWORD=op://V/stack-a/mysql_password
            WPOPS_SYNC_KEY_X=plain-key
            """)
        r = vault.check_file(path, config.load_cache())
        self.assertEqual(r["plaintext_keys"], ["CF_ACCOUNT_1_TOKEN", "WPOPS_SYNC_KEY_X"])
        self.assertFalse(r["ok"])
        self.assertNotIn("plain-token", json.dumps(r))

    def test_cache_states(self):
        root, stack = self.node_files()
        r = vault.check()
        self.assertFalse(r["ok"])
        self.assertEqual({f["cache"] for f in r["files"]}, {"missing"})
        self.assertEqual(self.calls(), [])                    # check reads the cache only
        r = vault.check(fresh=True)
        self.assertTrue(r["ok"])
        self.assertEqual({f["cache"] for f in r["files"]}, {"fresh"})
        stack.write_text(stack.read_text() + "WP_MODE=single\n")
        r = vault.check()
        self.assertTrue(r["ok"])
        self.assertEqual(r["warnings"], 1)
        self.assertNotIn("db-pass-123", json.dumps(r))

    def test_literal_tunnel_token_is_flagged(self):
        path = self.env_file(json.dumps({"services": {"cloudflared-a": {"environment": {"TUNNEL_TOKEN": "abc"}}}}),
                             "docker-compose.wpops-tunnels.json")
        self.assertEqual(vault.check_tunnels_file(path)["literal_tokens"], ["cloudflared-a.TUNNEL_TOKEN"])


class CfCommonScript(OpTestCase):
    """scripts/cf-common.sh load_config: references resolve, preset environment wins."""

    def run_load_config(self, extra_env):
        env_file = self.env_file("""\
            CF_ACCOUNT_1_NAME=one
            CF_ACCOUNT_1_ID=abc
            CF_ACCOUNT_1_TOKEN=op://V/cloudflare-one/credential
            MYSQL_PASSWORD=op://V/not-for-these-scripts/password
            """, "cf/.env")
        script = (f'source "{ROOT}/scripts/cf-common.sh"; ENV_FILE="{env_file}"; '
                  'load_config --read-only >/dev/null; '
                  'printf "%s" "$CF_ACCOUNT_1_TOKEN" | sha256sum | cut -c1-16')
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", WPOPS_OP_TOKEN_FILE=str(self.token),
                   **extra_env)
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    @staticmethod
    def digest(value):
        import hashlib
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def test_reference_is_read_with_op(self):
        self.assertEqual(self.run_load_config({"WPOPS_RUNTIME_DIR": str(self.tmp / "empty")}), self.digest("cf-token-one"))
        self.assertEqual(self.calls(), ["read token=True"])

    def test_without_op_cli_preset_tokens_are_enough(self):
        # The cf-backup container has no op CLI: only CF_* values are needed,
        # and they come from the environment.
        env_file = self.env_file("""\
            CF_ACCOUNT_1_NAME=one
            CF_ACCOUNT_1_ID=abc
            CF_ACCOUNT_1_TOKEN=op://V/cloudflare-one/credential
            MYSQL_PASSWORD=op://V/stack-a/mysql_password
            """, "cf2/.env")
        script = (f'source "{ROOT}/scripts/cf-common.sh"; ENV_FILE="{env_file}"; op_read() {{ return 1; }}; '
                  'load_config --read-only >/dev/null && printf "%s" "$CF_ACCOUNT_1_TOKEN"')
        r = subprocess.run(["bash", "-c", script], env=dict(os.environ, CF_ACCOUNT_1_TOKEN="from-wpops"),
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, "from-wpops"), r.stderr)

    def test_reference_is_read_from_the_wpops_cache(self):
        config._write_cache_json("secrets.json", {"values": {"op://V/cloudflare-one/credential": "cached"},
                                                  "sources": {}})
        self.assertEqual(self.run_load_config({"WPOPS_RUNTIME_DIR": str(config.RUNTIME_DIR)}), self.digest("cached"))
        self.assertEqual(self.calls(), [])

    def test_value_from_environment_wins(self):
        self.assertEqual(self.run_load_config({"CF_ACCOUNT_1_TOKEN": "from-wpops"}), self.digest("from-wpops"))
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
