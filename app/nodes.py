import asyncio
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import DeviceServerProfile, VpnServer
from app.vpn import PeerStats, XrayBackend, new_client_email


def country_flag(code: str) -> str:
    code = code.upper()
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(127397 + ord(char)) for char in code)


def server_label(server: VpnServer) -> str:
    location = server.country_name or server.name
    if server.city:
        location = f"{location}, {server.city}"
    return f"{country_flag(server.country_code)} {location}"


def render_server_uri(server: VpnServer, credential: str) -> str:
    is_xhttp = server.transport == "xhttp"
    parameters = {
        "encryption": "none",
        "security": "reality",
        "sni": server.reality_server_name,
        "fp": server.fingerprint,
        "pbk": server.reality_public_key,
        "sid": server.reality_short_id,
        "type": "xhttp" if is_xhttp else "tcp",
    }
    if is_xhttp:
        parameters.update({"path": server.xhttp_path or "/", "mode": server.xhttp_mode or "auto"})
    else:
        parameters.update({"flow": server.flow or "xtls-rprx-vision", "headerType": "none"})
    query = urlencode(parameters)
    return (
        f"vless://{credential}@{server.public_host}:{server.public_port}"
        f"?{query}#{quote(server_label(server))}"
    )


class NodeManager(ABC):
    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    async def activate(self, server: VpnServer, credential: str, client_email: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def revoke(self, server: VpnServer, client_email: str) -> None:
        raise NotImplementedError

    async def stats(self, server: VpnServer) -> dict[str, PeerStats]:
        return {}

    async def apply_config(self, server: VpnServer) -> None:
        return None


class LocalConfigNodeManager(NodeManager):
    def backend(self, server: VpnServer) -> XrayBackend:
        node_settings = self.settings.model_copy(
            update={
                "xray_config_path": server.xray_config_path or self.settings.xray_config_path,
                "xray_flow": "" if server.transport == "xhttp" else server.flow,
            }
        )
        return XrayBackend(node_settings)

    async def activate(self, server: VpnServer, credential: str, client_email: str) -> None:
        await self.backend(server).activate_peer(credential, client_email)

    async def revoke(self, server: VpnServer, client_email: str) -> None:
        await self.backend(server).revoke_peer(client_email)

    async def stats(self, server: VpnServer) -> dict[str, PeerStats]:
        return await self.backend(server).peer_stats()

    async def apply_config(self, server: VpnServer) -> None:
        await self.backend(server).apply_server_config(server)


class ManualNodeManager(NodeManager):
    async def activate(self, server: VpnServer, credential: str, client_email: str) -> None:
        return None

    async def revoke(self, server: VpnServer, client_email: str) -> None:
        return None


class AgentNodeManager(ManualNodeManager):
    """Placeholder for the future sumrak-node-agent integration."""


class NodeManagerRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.managers = {
            "local_config": LocalConfigNodeManager(settings),
            "manual": ManualNodeManager(settings),
            "ssh_future": ManualNodeManager(settings),
            "agent_future": AgentNodeManager(settings),
        }

    def manager(self, server: VpnServer) -> NodeManager:
        return self.managers.get(server.management_mode, self.managers["manual"])

    async def activate(self, server: VpnServer, credential: str, client_email: str) -> None:
        await self.manager(server).activate(server, credential, client_email)

    async def revoke(self, server: VpnServer, client_email: str) -> None:
        await self.manager(server).revoke(server, client_email)

    async def stats(self, server: VpnServer) -> dict[str, PeerStats]:
        return await self.manager(server).stats(server)

    async def apply_config(self, server: VpnServer) -> None:
        await self.manager(server).apply_config(server)

    async def health_check(self, server: VpnServer, timeout: float = 5.0) -> str:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(server.public_host, server.public_port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return "online"
        except (OSError, TimeoutError):
            return "offline"
        except Exception:
            return "error"


async def ensure_default_server(session: AsyncSession, settings: Settings) -> VpnServer:
    server = await session.scalar(select(VpnServer).where(VpnServer.is_default.is_(True)))
    if server:
        return server
    server = VpnServer(
        name=settings.default_server_name,
        country_code=settings.default_server_country_code,
        country_name=settings.default_server_country_name,
        city=settings.default_server_city,
        host=settings.xray_public_host,
        public_host=settings.xray_public_host or "127.0.0.1",
        public_port=settings.xray_public_port,
        protocol="vless-reality",
        transport="xhttp",
        reality_target=f"{settings.xray_reality_server_name}:443",
        reality_server_name=settings.xray_reality_server_name,
        reality_public_key=settings.xray_reality_public_key,
        reality_short_id=settings.xray_reality_short_id,
        fingerprint=settings.xray_fingerprint,
        flow="",
        xhttp_path="/",
        xhttp_mode="auto",
        xray_config_path=settings.xray_config_path,
        management_mode="local_config" if settings.vpn_backend == "xray" else "manual",
        is_active=True,
        is_default=True,
        priority=0,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)
    return server


async def active_servers(session: AsyncSession, settings: Settings) -> list[VpnServer]:
    await ensure_default_server(session, settings)
    servers = list(
        await session.scalars(
            select(VpnServer)
            .where(VpnServer.is_active.is_(True))
            .order_by(VpnServer.priority, VpnServer.name)
        )
    )
    available = []
    for server in servers:
        count = await session.scalar(
            select(func.count())
            .select_from(DeviceServerProfile)
            .where(
                DeviceServerProfile.server_id == server.id,
                DeviceServerProfile.is_active.is_(True),
            )
        )
        server.current_devices = count
        if server.max_devices is None or count < server.max_devices:
            available.append(server)
    return available


async def create_server_profile(
    session: AsyncSession,
    device_id: str,
    server: VpnServer,
    nodes: NodeManagerRegistry,
    credential: str | None = None,
    client_email: str | None = None,
) -> DeviceServerProfile:
    credential = credential or str(uuid.uuid4())
    client_email = client_email or new_client_email()
    await nodes.activate(server, credential, client_email)
    profile = DeviceServerProfile(
        device_id=device_id,
        server_id=server.id,
        credential=credential,
        client_email=client_email,
        uri=render_server_uri(server, credential),
    )
    session.add(profile)
    return profile


async def update_server_device_count(session: AsyncSession, server: VpnServer) -> None:
    server.current_devices = await session.scalar(
        select(func.count())
        .select_from(DeviceServerProfile)
        .where(
            DeviceServerProfile.server_id == server.id,
            DeviceServerProfile.is_active.is_(True),
        )
    )
    await session.commit()


async def check_server_health(
    session: AsyncSession, server: VpnServer, nodes: NodeManagerRegistry
) -> str:
    server.health_status = await nodes.health_check(server)
    server.last_health_check_at = datetime.now(UTC)
    await session.commit()
    return server.health_status
