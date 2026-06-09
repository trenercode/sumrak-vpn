import asyncio
import base64
import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings


@dataclass(frozen=True)
class PeerProfile:
    public_key: str
    assigned_ip: str
    config: str


@dataclass(frozen=True)
class PeerStats:
    last_handshake_at: datetime | None
    transfer_rx: int
    transfer_tx: int


class VpnBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def create_peer(self, assigned_ip: str) -> PeerProfile:
        raise NotImplementedError

    async def revoke_peer(self, public_key: str) -> None:
        raise NotImplementedError

    async def activate_peer(self, public_key: str, assigned_ip: str) -> None:
        raise NotImplementedError

    async def peer_stats(self) -> dict[str, PeerStats]:
        raise NotImplementedError

    def render_config(self, private_key: str, assigned_ip: str) -> str:
        s = self.settings
        return (
            "[Interface]\n"
            f"PrivateKey = {private_key}\n"
            f"Address = {assigned_ip}/32\n"
            f"DNS = {s.wg_dns}\n\n"
            "[Peer]\n"
            f"PublicKey = {s.wg_server_public_key}\n"
            "AllowedIPs = 0.0.0.0/0\n"
            f"Endpoint = {s.wg_endpoint}\n"
            f"PersistentKeepalive = {s.wg_persistent_keepalive}\n"
        )


class MockVpnBackend(VpnBackend):
    async def create_peer(self, assigned_ip: str) -> PeerProfile:
        private_key = base64.b64encode(secrets.token_bytes(32)).decode()
        public_key = base64.b64encode(hashlib.sha256(private_key.encode()).digest()).decode()
        return PeerProfile(public_key, assigned_ip, self.render_config(private_key, assigned_ip))

    async def revoke_peer(self, public_key: str) -> None:
        return None

    async def activate_peer(self, public_key: str, assigned_ip: str) -> None:
        return None

    async def peer_stats(self) -> dict[str, PeerStats]:
        return {}


class WireGuardBackend(VpnBackend):
    async def _run(self, *args: str, stdin: str | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(stdin.encode() if stdin is not None else None)
        if process.returncode:
            raise RuntimeError(f"{' '.join(args)} failed: {stderr.decode().strip()}")
        return stdout.decode().strip()

    async def create_peer(self, assigned_ip: str) -> PeerProfile:
        private_key = await self._run("wg", "genkey")
        public_key = await self._run("wg", "pubkey", stdin=private_key)
        await self._run(
            "wg",
            "set",
            self.settings.wg_interface,
            "peer",
            public_key,
            "allowed-ips",
            f"{assigned_ip}/32",
        )
        return PeerProfile(public_key, assigned_ip, self.render_config(private_key, assigned_ip))

    async def revoke_peer(self, public_key: str) -> None:
        await self._run(
            "wg", "set", self.settings.wg_interface, "peer", public_key, "remove"
        )

    async def activate_peer(self, public_key: str, assigned_ip: str) -> None:
        await self._run(
            "wg",
            "set",
            self.settings.wg_interface,
            "peer",
            public_key,
            "allowed-ips",
            f"{assigned_ip}/32",
        )

    async def peer_stats(self) -> dict[str, PeerStats]:
        output = await self._run("wg", "show", self.settings.wg_interface, "dump")
        result: dict[str, PeerStats] = {}
        for line in output.splitlines()[1:]:
            fields = line.split("\t")
            if len(fields) < 8:
                continue
            timestamp = int(fields[4])
            result[fields[0]] = PeerStats(
                datetime.fromtimestamp(timestamp, UTC) if timestamp else None,
                int(fields[5]),
                int(fields[6]),
            )
        return result


def build_vpn_backend(settings: Settings) -> VpnBackend:
    if settings.vpn_backend == "wireguard":
        if not settings.wg_server_public_key:
            raise ValueError("WG_SERVER_PUBLIC_KEY is required for wireguard backend")
        return WireGuardBackend(settings)
    return MockVpnBackend(settings)


def next_available_ip(network: str, server_address: str, used_ips: set[str]) -> str:
    subnet = ipaddress.ip_network(network)
    for address in subnet.hosts():
        candidate = str(address)
        if candidate != server_address and candidate not in used_ips:
            return candidate
    raise RuntimeError("VPN address pool is exhausted")
