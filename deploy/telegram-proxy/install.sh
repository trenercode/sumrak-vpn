#!/usr/bin/env bash
set -euo pipefail

INSTALL_TOKEN="${1:-}"
PANEL_URL="__PANEL_URL__"
INSTALL_DIR="/opt/sumrak-telegram-proxy"
PROXY_IMAGE="nineseconds/mtg:1"

[[ "$(id -u)" == "0" ]] || { echo "Run as root" >&2; exit 1; }
[[ -n "$INSTALL_TOKEN" ]] || { echo "INSTALL_TOKEN is required" >&2; exit 1; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]] || {
  echo "Only Ubuntu/Debian is supported" >&2; exit 1;
}

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl openssl iproute2
if ss -lnt | awk '{print $4}' | grep -Eq '(^|:|\])443$'; then
  echo "TCP port 443 is already occupied" >&2
  exit 1
fi
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-plugin

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
docker pull "$PROXY_IMAGE"
SECRET="$(docker run --rm "$PROXY_IMAGE" generate-secret tls -c telegram.org | tail -1)"
[[ "$SECRET" == ee* ]] || { echo "Could not generate FakeTLS secret" >&2; exit 1; }
AGENT_TOKEN="$(openssl rand -hex 32)"
PUBLIC_HOST="${PUBLIC_HOST:-$(curl -fsSL https://api.ipify.org)}"

curl -fsSL "$PANEL_URL/telegram-proxy/agent.py" -o agent.py
curl -fsSL "$PANEL_URL/telegram-proxy/Dockerfile.agent" -o Dockerfile.agent
cat > compose.yaml <<EOF
name: sumrak-telegram-proxy

services:
  proxy:
    image: $PROXY_IMAGE
    container_name: sumrak-telegram-proxy
    restart: unless-stopped
    command: ["run", "$SECRET"]
    environment:
      MTG_BIND: 0.0.0.0:3128
      MTG_STATS_BIND: 0.0.0.0:3129
    ports:
      - "443:3128"
  agent:
    build:
      context: .
      dockerfile: Dockerfile.agent
    container_name: sumrak-telegram-proxy-agent
    restart: unless-stopped
    environment:
      PANEL_URL: "$PANEL_URL"
      AGENT_TOKEN: "$AGENT_TOKEN"
    volumes:
      - ./:/data
      - /var/run/docker.sock:/var/run/docker.sock
EOF
docker compose build --no-cache agent
docker compose run --rm --no-deps -T agent docker compose version </dev/null
docker compose up -d proxy
curl -fsSL -X POST "$PANEL_URL/api/telegram-proxy/register" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"install_token":"%s","public_host":"%s","public_port":443,"secret":"%s","agent_token":"%s","version":"1.0.0"}' "$INSTALL_TOKEN" "$PUBLIC_HOST" "$SECRET" "$AGENT_TOKEN")"
docker compose up -d agent
echo "Sumrak Telegram Proxy installed: $PUBLIC_HOST:443"
