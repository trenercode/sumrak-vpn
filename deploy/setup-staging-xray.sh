#!/usr/bin/env bash
set -euo pipefail

XRAY_IMAGE="${XRAY_IMAGE:-ghcr.io/xtls/xray-core:latest}"
STAGING_PUBLIC_HOST="${STAGING_PUBLIC_HOST:-test.sumrak.digital}"
REALITY_TARGET="${REALITY_TARGET:-www.microsoft.com:443}"
REALITY_SERVER_NAME="${REALITY_SERVER_NAME:-www.microsoft.com}"
XRAY_PORT="${XRAY_PORT:-18443}"
CONFIG_DIR="${CONFIG_DIR:-deploy/xray}"

if [[ "${XRAY_PORT}" == "8443" ]]; then
  echo "Refusing to use the production Xray port 8443 for staging" >&2
  exit 1
fi

if [[ -e "${CONFIG_DIR}/config.json" || -e "${CONFIG_DIR}/reality.env" ]]; then
  echo "${CONFIG_DIR}/config.json or reality.env already exists; refusing to overwrite keys" >&2
  exit 1
fi

if ! command -v docker >/dev/null || ! command -v openssl >/dev/null; then
  echo "Docker and OpenSSL are required" >&2
  exit 1
fi

mkdir -p "${CONFIG_DIR}"
KEYS="$(docker run --rm "${XRAY_IMAGE}" x25519)"
PRIVATE_KEY="$(printf '%s\n' "${KEYS}" | awk -F': ' 'tolower($1) ~ /private/ {print $2; exit}')"
PUBLIC_KEY="$(printf '%s\n' "${KEYS}" | awk -F': ' 'tolower($1) ~ /^(public key|password \(publickey\))$/ {print $2; exit}')"
SHORT_ID="$(openssl rand -hex 8)"

if [[ -z "${PRIVATE_KEY}" || -z "${PUBLIC_KEY}" ]]; then
  echo "Could not parse staging x25519 keys" >&2
  exit 1
fi

cat > "${CONFIG_DIR}/config.json" <<EOF
{
  "log": {"loglevel": "warning"},
  "api": {
    "tag": "api",
    "listen": "127.0.0.1:10085",
    "services": ["StatsService", "ReflectionService"]
  },
  "policy": {
    "levels": {
      "0": {"statsUserUplink": true, "statsUserDownlink": true}
    }
  },
  "stats": {},
  "inbounds": [
    {
      "tag": "vless-reality",
      "listen": "0.0.0.0",
      "port": ${XRAY_PORT},
      "protocol": "vless",
      "settings": {"clients": [], "decryption": "none"},
      "streamSettings": {
        "network": "raw",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "target": "${REALITY_TARGET}",
          "serverNames": ["${REALITY_SERVER_NAME}"],
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": ["${SHORT_ID}"]
        }
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls", "quic"]
      }
    }
  ],
  "outbounds": [
    {"tag": "direct", "protocol": "freedom"},
    {"tag": "blocked", "protocol": "blackhole"}
  ]
}
EOF

cat > "${CONFIG_DIR}/reality.env" <<EOF
VPN_BACKEND=xray
XRAY_CONFIG_PATH=/data/xray/config.json
XRAY_CONTAINER_CONFIG_PATH=/etc/xray/config.json
XRAY_CONTAINER_NAME=sumrak-vpn-test-xray
XRAY_PUBLIC_HOST=${STAGING_PUBLIC_HOST}
XRAY_PUBLIC_PORT=${XRAY_PORT}
XRAY_REALITY_SERVER_NAME=${REALITY_SERVER_NAME}
XRAY_REALITY_PUBLIC_KEY=${PUBLIC_KEY}
XRAY_REALITY_SHORT_ID=${SHORT_ID}
XRAY_FINGERPRINT=chrome
XRAY_FLOW=
DEFAULT_SERVER_NAME=Sumrak VPN Staging
DEFAULT_SERVER_COUNTRY_CODE=DE
DEFAULT_SERVER_COUNTRY_NAME=Test
DEFAULT_SERVER_CITY=Staging
EOF

chmod 600 "${CONFIG_DIR}/config.json" "${CONFIG_DIR}/reality.env"
echo "Created isolated staging Xray config and REALITY keys in ${CONFIG_DIR}"
echo "Public staging endpoint: ${STAGING_PUBLIC_HOST}:${XRAY_PORT}"
