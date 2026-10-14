#!/usr/bin/env bash
# Install/refresh the wpops agent systemd unit on this node.
#   scripts/wpops/install-agent.sh [--bind 172.19.0.1]
# --bind adds an address (e.g. the docker frontend gateway so cloudflared can reach it).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BIND_ARGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bind) BIND_ARGS="$BIND_ARGS --bind $2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
sed "s|/opt/docker/wordpressmultisite|$REPO|g" "$HERE/wpops-agent.service" > /etc/systemd/system/wpops-agent.service
printf 'WPOPS_BIND_ARGS=%s\n' "$BIND_ARGS" > /etc/default/wpops-agent
chmod 600 /etc/default/wpops-agent
ln -sf "$HERE/bin/wpops" /usr/local/bin/wpops
systemctl daemon-reload
systemctl enable --now wpops-agent
systemctl --no-pager --lines=3 status wpops-agent
