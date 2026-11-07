"""Tunnel connector compose services (offline)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/wpops'))
from wpops import config, tunnels  # noqa: E402


class ConnectorServiceTests(unittest.TestCase):
    def test_default_has_no_protocol(self):
        svc = tunnels.connector_service("acme-co", ["edge"])
        self.assertEqual(svc["environment"], {"TUNNEL_TOKEN": "${TUNNEL_TOKEN_ACME_CO}"})
        self.assertEqual(svc["command"], ["tunnel", "--no-autoupdate", "run"])

    def test_http2(self):
        svc = tunnels.connector_service("acme", ["edge"], "http2")
        self.assertEqual(svc["environment"]["TUNNEL_TRANSPORT_PROTOCOL"], "http2")

    def test_protocol_from_node_config(self):
        self.assertEqual(tunnels.tunnel_protocol(config.NODE_DEFAULTS), "")
        self.assertEqual(tunnels.tunnel_protocol({"tunnels": {"protocol": " HTTP2 "}}), "http2")
        self.assertEqual(tunnels.tunnel_protocol({}), "")
        with self.assertRaises(config.ConfigError):
            tunnels.tunnel_protocol({"tunnels": {"protocol": "udp"}})


if __name__ == "__main__":
    unittest.main()
