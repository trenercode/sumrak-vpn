import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import DeviceServerProfile
from app.nodes import NodeManagerRegistry, ensure_default_server, render_server_uri


async def sync_staging_xray() -> None:
    settings = get_settings()
    if settings.xray_container_name != "sumrak-vpn-test-xray":
        raise RuntimeError("Refusing to sync a non-staging Xray container")
    if settings.xray_public_port == 8443:
        raise RuntimeError("Refusing to sync the production Xray port")

    nodes = NodeManagerRegistry(settings)
    async with SessionLocal() as session:
        server = await ensure_default_server(session, settings)
        was_local_config = server.management_mode == "local_config"
        if not was_local_config:
            server.name = settings.default_server_name
            server.host = settings.xray_public_host
            server.public_host = settings.xray_public_host
            server.public_port = settings.xray_public_port
            server.transport = "vision"
            server.reality_target = f"{settings.xray_reality_server_name}:443"
            server.reality_server_name = settings.xray_reality_server_name
            server.reality_public_key = settings.xray_reality_public_key
            server.reality_short_id = settings.xray_reality_short_id
            server.fingerprint = settings.xray_fingerprint
            server.flow = settings.xray_flow
        server.xray_config_path = settings.xray_config_path
        server.management_mode = "local_config"
        server.is_active = True
        await session.commit()

        profiles = list(
            await session.scalars(
                select(DeviceServerProfile).where(
                    DeviceServerProfile.server_id == server.id,
                    DeviceServerProfile.is_active.is_(True),
                )
            )
        )
        for profile in profiles:
            await nodes.activate(server, profile.credential, profile.client_email)
            profile.uri = render_server_uri(server, profile.credential)
        await session.commit()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync_staging_xray())
