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


if __name__ == "__main__":
    unittest.main()
