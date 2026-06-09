#!/usr/bin/env bash
set -euo pipefail

WG_INTERFACE="${WG_INTERFACE:-wg0}"
WG_PORT="${WG_PORT:-51820}"
WG_ADDRESS="${WG_ADDRESS:-10.66.0.1/24}"
PUBLIC_INTERFACE="${PUBLIC_INTERFACE:-$(ip route show default | awk '{print $5; exit}')}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root" >&2
  exit 1
fi

apt-get update
apt-get install -y wireguard-tools iptables
install -d -m 700 /etc/wireguard

if [[ ! -f "/etc/wireguard/${WG_INTERFACE}.key" ]]; then
  umask 077
  wg genkey > "/etc/wireguard/${WG_INTERFACE}.key"
fi

PRIVATE_KEY="$(cat "/etc/wireguard/${WG_INTERFACE}.key")"
cat > "/etc/wireguard/${WG_INTERFACE}.conf" <<EOF
[Interface]
Address = ${WG_ADDRESS}
ListenPort = ${WG_PORT}
PrivateKey = ${PRIVATE_KEY}
PostUp = iptables -A FORWARD -i ${WG_INTERFACE} -j ACCEPT; iptables -A FORWARD -o ${WG_INTERFACE} -j ACCEPT; iptables -t nat -A POSTROUTING -o ${PUBLIC_INTERFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i ${WG_INTERFACE} -j ACCEPT; iptables -D FORWARD -o ${WG_INTERFACE} -j ACCEPT; iptables -t nat -D POSTROUTING -o ${PUBLIC_INTERFACE} -j MASQUERADE
EOF
chmod 600 "/etc/wireguard/${WG_INTERFACE}.conf"

cat > /etc/sysctl.d/90-vpn-forwarding.conf <<EOF
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sysctl --system
systemctl enable --now "wg-quick@${WG_INTERFACE}"

echo "WireGuard public key:"
wg show "${WG_INTERFACE}" public-key

