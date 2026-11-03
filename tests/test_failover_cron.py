"""failover.cron_follows_traffic: wp-cron starts with traffic and stops after rollback."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "wpops"))
from wpops import failover  # noqa: E402


class FakeOrigin:
    stacks = {"shop": {"managed": True}, "legacy": {"managed": False}}


class FakeInv:
    def origin(self, name):
        return FakeOrigin()


class CronFollowsTraffic(unittest.TestCase):
    def run_cron(self, enabled, start, stacks):
        calls = []
        def fake_compose(origin, st, *args, **kw):
            calls.append((st, args))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("wpops.config.node_config", return_value={"failover": {"cron_follows_traffic": enabled}}), \
             patch("wpops.stacks.compose", side_effect=fake_compose):
            changed = failover._set_cron(FakeInv(), "standby", stacks, start, log=lambda s: None)
        return changed, calls

    def test_off_by_default(self):
        changed, calls = self.run_cron(False, True, {"shop"})
        self.assertEqual((changed, calls), ([], []))

    def test_start_on_failover_managed_stacks_only(self):
        changed, calls = self.run_cron(True, True, {"shop", "legacy", "unknown"})
        self.assertEqual(changed, ["shop"])
        self.assertEqual(calls, [("shop", ("up", "-d", "--no-deps", "wp-cron"))])

    def test_stop_on_rollback(self):
        changed, calls = self.run_cron(True, False, {"shop"})
        self.assertEqual(calls, [("shop", ("stop", "wp-cron"))])


class WpCliWithCronStopped(unittest.TestCase):
    def wp_args(self, running):
        from wpops import stacks
        seen = []
        def fake_compose(origin, name, *args, **kw):
            seen.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        services = [{"service": "nginx", "state": "running"}] + ([{"service": "wp-cron", "state": "running"}] if running else [])
        origin = SimpleNamespace(stacks={"shop": {}})
        with patch("wpops.stacks.ps", return_value=services), patch("wpops.stacks.compose", side_effect=fake_compose):
            stacks.wp(origin, "shop", "option", "get", "home")
        return seen[-1]

    def test_exec_when_running(self):
        self.assertEqual(self.wp_args(True)[:4], ("exec", "-T", "wp-cron", "wp"))

    def test_one_off_container_when_stopped(self):
        self.assertEqual(self.wp_args(False)[:7], ("run", "--rm", "--no-deps", "-T", "--entrypoint", "wp", "wp-cron"))


class AutoApplyTarget(unittest.TestCase):
    def inv(self):
        from wpops import config
        site = SimpleNamespace(slug="shop", host_on=lambda o: {"prod": {"stack": "shop"}, "standby": {"stack": "shop"}}.get(o))
        net = SimpleNamespace(slug="news", host_on=lambda o: {"prod": {"stack": "net"}, "standby": {"stack": "main", "blog_id": 2}}.get(o))
        origins = {"prod": SimpleNamespace(stacks={"shop": {"mode": "single"}, "net": {"mode": "multisite"}}),
                   "standby": SimpleNamespace(stacks={"shop": {"mode": "single"}, "main": {"mode": "multisite"}})}
        return SimpleNamespace(
            backup_sources={"prod-shop": config.BackupSource("prod-shop", "prod", "https://shop.example.com", "shop"),
                            "prod-net": config.BackupSource("prod-net", "prod", "https://net.example.com", "net"),
                            "prod-any": config.BackupSource("prod-any", "prod", "https://x.example.com", "")},
            sites=[site, net], origin=lambda n: origins[n])

    def test_single_stack_maps_to_the_site_stack_here(self):
        from wpops import backup
        self.assertEqual(backup.auto_apply_target(self.inv(), "prod-shop", "standby"), ("shop", "shop"))

    def test_no_target_for_blogs_profiles_without_stack_or_own_node(self):
        from wpops import backup
        inv = self.inv()
        self.assertIsNone(backup.auto_apply_target(inv, "prod-net", "standby"))
        self.assertIsNone(backup.auto_apply_target(inv, "prod-any", "standby"))
        self.assertIsNone(backup.auto_apply_target(inv, "prod-shop", "prod"))
        self.assertIsNone(backup.auto_apply_target(inv, "unknown", "standby"))


if __name__ == "__main__":
    unittest.main()
