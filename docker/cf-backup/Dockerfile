# Cloudflare DNS + tunnel config backup (scripts/cf-backup.sh), service
# cf-backup of the wpops tunnels project and of the legacy docker-compose.yml.
# Built once on the node; nothing is installed when the container starts.
# The scripts, config and backup folder are mounted at /app.
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
RUN apk add --no-cache bash curl jq
WORKDIR /app
CMD ["/bin/sh", "-c", "while true; do bash /app/scripts/cf-backup.sh || true; sleep 21600; done"]
