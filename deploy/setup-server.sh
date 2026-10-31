#!/usr/bin/env bash
# Puts AdRx online on an Ubuntu or Debian server: installs Docker, builds the app and starts it behind Caddy,
# which gets the HTTPS certificate. Run it again after uploading a new version: it updates the site in place.
#
#   sudo bash deploy/setup-server.sh adrx-check.duckdns.org   # your free DuckDNS name or own domain (https)
#   sudo bash deploy/setup-server.sh                          # keep the current address (first time: http on the IP)
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DEPLOY_DIR"

step() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Please run it as root: sudo bash deploy/setup-server.sh ${1:-}"

ADDRESS="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
if [ -n "$ADDRESS" ]; then
  ADDRESS="${ADDRESS#http://}"
  ADDRESS="${ADDRESS#https://}"
  ADDRESS="${ADDRESS%%/*}"
  [[ "$ADDRESS" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
    || fail "\"$1\" doesn't look like a web address. Example: adrx-check.duckdns.org"
fi

step "1/5 Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 \
  || fail "Docker Compose is missing. Install the docker-compose-plugin package, then run this again."
docker --version

step "2/5 Memory"
mem_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 3500 ] && ! swapon --show | grep -q .; then
  echo "This server has ${mem_mb} MB of memory: adding 2 GB of swap for the speech model."
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  echo "${mem_mb} MB of memory: OK"
fi

step "3/5 Settings (deploy/.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created deploy/.env"
fi
if [ -n "$ADDRESS" ]; then
  if grep -q '^SITE_ADDRESS=' .env; then
    sed -i "s|^SITE_ADDRESS=.*|SITE_ADDRESS=$ADDRESS|" .env
  else
    echo "SITE_ADDRESS=$ADDRESS" >> .env
  fi
fi
SITE="$( (grep -E '^SITE_ADDRESS=' .env || true) | tail -n 1 | cut -d= -f2- | tr -d '[:space:]')"
SITE="${SITE:-:80}"
SERVER_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
if [ "$SITE" = ":80" ]; then
  URL="http://$SERVER_IP"
  warn "No web address yet, so the site uses plain http on the IP address. For https, run this again with"
  warn "a free DuckDNS name or your domain:  sudo bash deploy/setup-server.sh your-name.duckdns.org"
else
  URL="https://$SITE"
  NAME_IP="$(getent ahostsv4 "$SITE" | awk 'NR == 1 {print $1}' || true)"
  if [ "$NAME_IP" != "$SERVER_IP" ]; then
    warn "$SITE points to ${NAME_IP:-nothing}, not to this server ($SERVER_IP)."
    warn "Set the IP in DuckDNS (or the A record of your domain) to $SERVER_IP. The certificate is issued"
    warn "automatically a few minutes after that."
  else
    echo "$SITE points to this server ($SERVER_IP): OK"
  fi
fi

step "4/5 Firewall"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow OpenSSH >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443 >/dev/null
  echo "Opened ports 80 and 443 in ufw"
else
  echo "No active ufw firewall on the server. Ports 80 and 443 must be open in your cloud provider's firewall."
fi

step "5/5 Build and start (the first time takes about 10 minutes)"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true

echo "Waiting for $URL to answer..."
for _ in $(seq 1 60); do
  if curl -fsS --max-time 10 "$URL/api/health" >/dev/null 2>&1; then
    step "AdRx is online: $URL"
    exit 0
  fi
  sleep 5
done
warn "The app is running, but $URL did not answer after several minutes. Check:"
warn "  - ports 80 and 443 are open in your cloud provider's firewall (on AWS Lightsail: Networking tab)"
warn "  - the web address points to $SERVER_IP"
warn "  - the logs:  cd $DEPLOY_DIR && docker compose logs --tail 50"
exit 1
