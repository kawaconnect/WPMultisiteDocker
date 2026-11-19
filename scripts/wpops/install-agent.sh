#!/usr/bin/env bash
# Install/refresh the wpops agent systemd unit on this node.
#   scripts/wpops/install-agent.sh [--bind 172.19.0.1]
# --bind adds an address (e.g. the docker frontend gateway so cloudflared can reach it).
# If ufw is active, allow the docker networks to reach that port:
#   ufw allow from 172.16.0.0/12 to any port 8787 proto tcp
# and set agent.trusted_proxies (config/wpops.json) to the docker subnet(s).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
usage() {
  echo "usage: $0 [--bind ADDRESS]..."
  echo "  Installs and starts wpops-agent.service and wpops-secrets.service (run as root)."
  echo "  --bind ADDRESS  extra IP address the agent listens on, besides agent.bind in"
  echo "                  config/wpops.json (e.g. the docker gateway 172.19.0.1)"
}
BIND_ARGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --bind)
      # Written to /etc/default/wpops-agent and expanded on the unit's command line.
      [[ "${2:-}" =~ ^[0-9A-Fa-f:.]+$ ]] || { echo "--bind needs an IP address" >&2; usage >&2; exit 2; }
      BIND_ARGS="$BIND_ARGS --bind $2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root: this writes systemd units and /usr/local/bin/wpops" >&2; exit 1; }
[ -f "$REPO/state/agent-token.sha256" ] \
  || echo "note: no agent token yet; the agent will not start until you run: wpops token rotate" >&2
sed "s|/opt/docker/wordpressmultisite|$REPO|g" "$HERE/wpops-agent.service" > /etc/systemd/system/wpops-agent.service
sed "s|/opt/docker/wordpressmultisite|$REPO|g" "$HERE/wpops-secrets.service" > /etc/systemd/system/wpops-secrets.service
printf 'WPOPS_BIND_ARGS=%s\n' "$BIND_ARGS" > /etc/default/wpops-agent
chmod 600 /etc/default/wpops-agent
ln -sf "$HERE/bin/wpops" /usr/local/bin/wpops
systemctl daemon-reload
systemctl enable wpops-secrets   # fills the 1Password cache at boot (docs/secrets.md)
systemctl enable --now wpops-agent
systemctl --no-pager --lines=3 status wpops-agent
