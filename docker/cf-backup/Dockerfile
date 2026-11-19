# Cloudflare DNS + tunnel config backup (scripts/cf-backup.sh), service
# cf-backup of the wpops tunnels project and of the legacy docker-compose.yml.
# Built once on the node; nothing is installed when the container starts.
# The scripts, config and backup folder are mounted at /app.
FROM alpine:3.23@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0
RUN apk add --no-cache bash curl jq
WORKDIR /app
CMD ["/bin/sh", "-c", "while true; do bash /app/scripts/cf-backup.sh || true; sleep 21600; done"]
