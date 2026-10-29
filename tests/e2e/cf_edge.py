#!/usr/bin/env python3
"""Emulate the Cloudflare edge + cloudflared for the opt-in end-to-end tests.

Reads every tunnel's ingress rules from the fake Cloudflare API
(tests/e2e/fake_cloudflare.py) and writes an Nginx config that terminates TLS
for each rule's hostname and proxies to the rule's service, the way
cloudflared would, adding CF-Connecting-IP / CF-Visitor / X-Forwarded-Proto.
Run that Nginx in a container joined to the same networks as the generated
connector project (docker-compose.wpops-tunnels.json), so service names
resolve exactly as they do for the real connectors.

    python3 tests/e2e/cf_edge.py --fake http://127.0.0.1:8999 --out DIR

DIR receives nginx.conf, a test CA (ca.pem, created once) and a certificate
for exactly the routed hostnames.
Requests whose hostname matches no rule get 404, like the http_status:404
catch-all.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from pathlib import Path

TEMPLATE = """
events {}
http {
  resolver 127.0.0.11 valid=5s ipv6=off;
  access_log /dev/stdout;
  server { listen 443 ssl default_server; ssl_certificate /e/cert.pem; ssl_certificate_key /e/key.pem; return 404; }
%s
}
"""

SERVER = """  server {
    listen 443 ssl; server_name %(host)s;
    ssl_certificate /e/cert.pem; ssl_certificate_key /e/key.pem;
    client_max_body_size 0;
    location / {
      set $svc %(service)s;
      proxy_pass $svc;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header CF-Visitor '{"scheme":"https"}';
      proxy_set_header CF-Connecting-IP $http_x_test_client_ip;
      proxy_redirect off;
      proxy_buffering off;
    }
  }
"""


def certs(out: Path, names: list[str]) -> None:
    """A test CA once, then a leaf certificate for exactly these hostnames."""
    def run(*args: str) -> None:
        subprocess.run(list(args), check=True, capture_output=True)
    if not (out / "ca.pem").exists():
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30", "-subj", "/CN=e2e test CA",
            "-keyout", str(out / "ca.key"), "-out", str(out / "ca.pem"), "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=e2e edge",
        "-keyout", str(out / "key.pem"), "-out", str(out / "edge.csr"))
    # Python's ssl (VERIFY_X509_STRICT) wants these extensions on a leaf.
    (out / "ext.cnf").write_text("subjectAltName=" + ",".join(f"DNS:{n}" for n in names or ["edge.invalid"]) + "\n"
                                 "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                 "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
    run("openssl", "x509", "-req", "-in", str(out / "edge.csr"), "-CA", str(out / "ca.pem"), "-CAkey", str(out / "ca.key"),
        "-CAcreateserial", "-days", "30", "-extfile", str(out / "ext.cnf"), "-out", str(out / "cert.pem"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", default="http://127.0.0.1:8999")
    ap.add_argument("--out", required=True)
    ap.add_argument("--route", action="append", default=[], metavar="HOST=SERVICE",
                    help="extra route, e.g. a backup source's main host (repeatable)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(args.fake + "/_fake/state", timeout=10) as r:
        st = json.load(r)
    seen, servers = set(), []
    extra = [{"hostname": h, "service": svc} for h, _, svc in (r.partition("=") for r in args.route)]
    for t in [*st["tunnels"].values(), {"config": {"ingress": extra}}]:
        for rule in (t.get("config") or {}).get("ingress") or []:
            host = rule.get("hostname")
            if not host or host in seen or not rule.get("service", "").startswith("http"):
                continue
            seen.add(host)
            servers.append(SERVER % {"host": host, "service": rule["service"]})
    certs(out, sorted(seen))
    (out / "nginx.conf").write_text(TEMPLATE % "".join(servers))
    print(f"{len(servers)} hostnames")


if __name__ == "__main__":
    main()
