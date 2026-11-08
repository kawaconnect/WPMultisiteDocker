"""backup.pause_sources: a paused source is skipped by scheduled pulls, resume restores it."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "wpops"))
from wpops import backup  # noqa: E402


class FakeInv:
    def sources(self):
        # insertion order is the pull order
        return {"node1-a": object(), "node1-b": object(), "node1-c": object()}


class BackupPause(unittest.TestCase):
    def _cfg(self, sources=None, paused=None):
        return {"backup": {"sources": sources or [], "paused": paused or [],
                           "schedule": "off", "retain_sets": 7, "auto_apply": True}}

    def test_sources_to_pull_skips_paused(self):
        with patch("wpops.backup.node_config", return_value=self._cfg(paused=["node1-b"])):
            self.assertEqual(backup.sources_to_pull(FakeInv()), ["node1-a", "node1-c"])

    def test_sources_to_pull_all_when_none_paused(self):
        with patch("wpops.backup.node_config", return_value=self._cfg()):
            self.assertEqual(backup.sources_to_pull(FakeInv()), ["node1-a", "node1-b", "node1-c"])

    def test_explicit_sources_list_respects_pause(self):
        cfg = self._cfg(sources=["node1-a", "node1-b"], paused=["node1-b"])
        with patch("wpops.backup.node_config", return_value=cfg):
            self.assertEqual(backup.sources_to_pull(FakeInv()), ["node1-a"])

    def test_pause_then_resume_toggles_and_is_idempotent(self):
        cfg = self._cfg()
        with patch("wpops.backup.node_config", return_value=cfg), \
             patch("wpops.backup.save_node_config"), \
             patch("wpops.backup.state.read_json", return_value={}):
            backup.pause_sources(["node1-b"])
            backup.pause_sources(["node1-b"])  # idempotent: no duplicate
            self.assertEqual(cfg["backup"]["paused"], ["node1-b"])
            self.assertNotIn("node1-b", backup.sources_to_pull(FakeInv()))
            info = backup.pause_sources(["node1-b"], paused=False)
            self.assertEqual(cfg["backup"]["paused"], [])
            self.assertEqual(info["paused"], [])
            self.assertIn("node1-b", backup.sources_to_pull(FakeInv()))


if __name__ == "__main__":
    unittest.main()
