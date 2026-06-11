import asyncio
import base64

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import Base, DeviceServerProfile, VpnServer
from app.nodes import NodeManagerRegistry, check_server_health, ensure_default_server, render_server_uri
from app.services import create_device, get_or_create_user, revoke_device, subscription_uris


async def database():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def server(name: str, country: str, priority: int, default: bool = False) -> VpnServer:
    return VpnServer(
        name=name,
        country_code=country,
        country_name=name,
        public_host=f"{name.lower()}.example.com",
        public_port=8443,
        reality_server_name="www.microsoft.com",
        reality_public_key=f"{name}-key",
        reality_short_id="0123456789abcdef",
        management_mode="manual",
        is_default=default,
        priority=priority,
    )


async def test_create_device_profiles_for_multiple_servers_and_revoke_all():
    engine, sessions = await database()
    settings = Settings(xray_public_host="default.example.com")
    nodes = NodeManagerRegistry(settings)
    async with sessions() as session:
        session.add_all([server("Germany", "DE", 10, True), server("Netherlands", "NL", 20)])
        await session.commit()
        user = await get_or_create_user(session, 100, "multi", "Multi")
        device, profiles = await create_device(session, user, "android", settings, nodes)
        assert len(profiles) == 2
        uris = await subscription_uris(session, device, settings, nodes)
        assert len(uris) == 2
        assert "Germany" in uris[0]
        assert "Netherlands" in uris[1]
        netherlands = await session.scalar(
            select(VpnServer).where(VpnServer.name == "Netherlands")
        )
        netherlands.is_active = False
        await session.commit()
        assert len(await subscription_uris(session, device, settings, nodes)) == 1
        await revoke_device(session, device, settings, nodes)
        stored = list(
            await session.scalars(
                select(DeviceServerProfile).where(DeviceServerProfile.device_id == device.id)
            )
        )
        assert all(not profile.is_active for profile in stored)
    await engine.dispose()


def test_render_uri_uses_server_specific_reality_settings():
    item = server("Germany", "DE", 10)
    uri = render_server_uri(item, "11111111-1111-1111-1111-111111111111")
    assert "@germany.example.com:8443?" in uri
    assert "pbk=Germany-key" in uri
    assert "Germany" in uri


def test_render_uri_uses_xhttp_without_vision_flow():
    item = server("XHTTP", "DE", 10)
    item.transport = "xhttp"
    item.flow = ""
    item.xhttp_path = "/sumrak"
    item.xhttp_mode = "auto"
    uri = render_server_uri(item, "11111111-1111-1111-1111-111111111111")
    assert "type=xhttp" in uri
    assert "path=%2Fsumrak" in uri
    assert "mode=auto" in uri
    assert "flow=" not in uri
    assert "headerType=" not in uri


async def test_new_default_server_uses_xhttp():
    engine, sessions = await database()
    async with sessions() as session:
        item = await ensure_default_server(
            session,
            Settings(
                xray_public_host="staging.example.com",
                xray_reality_public_key="public-key",
                xray_reality_short_id="0123456789abcdef",
            ),
        )
        assert item.transport == "xhttp"
        assert item.flow == ""
        assert item.xhttp_path == "/"
        assert item.xhttp_mode == "auto"
    await engine.dispose()


async def test_health_check_online_and_offline(monkeypatch):
    engine, sessions = await database()
    nodes = NodeManagerRegistry(Settings())

    class Writer:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def open_connection(host, port):
        if port == 1:
            raise OSError("offline")
        return object(), Writer()

    monkeypatch.setattr("app.nodes.asyncio.open_connection", open_connection)
    async with sessions() as session:
        online = server("Online", "DE", 1)
        online.public_host = "127.0.0.1"
        online.public_port = 8443
        offline = server("Offline", "NL", 2)
        offline.public_host = "127.0.0.1"
        offline.public_port = 1
        session.add_all([online, offline])
        await session.commit()
        assert await check_server_health(session, online, nodes) == "online"
        assert await check_server_health(session, offline, nodes) == "offline"
    await engine.dispose()


def test_subscription_endpoint_and_servers_admin():
    async def scenario():
        engine, sessions = await database()
        settings = Settings(xray_public_host="default.example.com")
        nodes = NodeManagerRegistry(settings)
        async with sessions() as session:
            session.add(server("Germany", "DE", 10, True))
            await session.commit()
            user = await get_or_create_user(session, 200, "endpoint", "Endpoint")
            device, _ = await create_device(session, user, "ios", settings, nodes)
            token = device.subscription_token

        async def override_session():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        app.state.nodes = nodes
        with TestClient(app) as client:
            response = client.get(f"/sub/{token}")
            assert response.status_code == 200
            assert response.text.startswith("vless://")
            encoded = client.get(f"/sub/{token}?base64=true")
            assert base64.b64decode(encoded.text).decode() == response.text
            response = client.get("/admin/servers", auth=("admin", "change-me-now"))
            assert response.status_code == 200
            assert "Germany" in response.text
            assert response.text.index("Стабильный режим (рекомендуется)") < response.text.index(
                "Быстрый режим (резервный)"
            )
            response = client.post(
                "/admin/servers",
                auth=("admin", "change-me-now"),
                data={
                    "name": "Netherlands",
                    "country_code": "NL",
                    "country_name": "Netherlands",
                    "public_host": "nl.example.com",
                    "public_port": "8443",
                    "reality_public_key": "nl-key",
                    "reality_short_id": "0123456789abcdef",
                    "management_mode": "manual",
                    "max_devices": "",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            async with sessions() as session:
                created_server = await session.scalar(
                    select(VpnServer).where(VpnServer.name == "Netherlands")
                )
                assert created_server.transport == "xhttp"
                assert created_server.flow == ""
                assert created_server.xhttp_path == "/"
                assert created_server.xhttp_mode == "auto"
        app.dependency_overrides.clear()
        await engine.dispose()

    asyncio.run(scenario())
