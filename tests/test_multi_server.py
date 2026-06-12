import asyncio
import base64
import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import Base, DeviceServerProfile, VpnServer
from app.admin import server_delete
from app.nodes import (
    NodeManagerRegistry,
    RemoteConfigNodeManager,
    check_server_health,
    ensure_default_server,
    render_server_uri,
)
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


def remote_server(transport: str = "xhttp") -> VpnServer:
    item = server("France", "FR", 10)
    item.transport = transport
    item.flow = "" if transport == "xhttp" else "xtls-rprx-vision"
    item.management_mode = "remote_config"
    item.ssh_host = "31.56.146.138"
    item.ssh_port = 22
    item.ssh_user = "root"
    item.ssh_key_path = "/run/secrets/france"
    item.remote_xray_config_path = "/opt/xray-fr/config.json"
    item.remote_compose_dir = "/opt/xray-fr"
    item.remote_container_name = "xray-fr"
    return item


def remote_base_config() -> dict:
    return {
        "inbounds": [
            {
                "tag": "vless-reality",
                "port": 443,
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "raw",
                    "security": "reality",
                    "realitySettings": {
                        "privateKey": "remote-private-key",
                        "target": "www.microsoft.com:443",
                        "serverNames": ["www.microsoft.com"],
                        "shortIds": ["0123456789abcdef"],
                    },
                },
            }
        ]
    }


async def test_remote_xhttp_sync_generates_full_config_without_flow():
    manager = RemoteConfigNodeManager(Settings())
    item = remote_server()
    calls = []

    async def ssh(server, command, stdin=None):
        calls.append((command, stdin))
        if command.startswith("cat /opt/xray-fr/config.json"):
            return json.dumps(remote_base_config()), ""
        return "", ""

    manager._ssh = ssh
    await manager.sync_clients(item, [{"id": "uuid", "email": "device@test"}])
    uploaded = json.loads(next(stdin for command, stdin in calls if command.startswith("cat >")))
    inbound = uploaded["inbounds"][0]
    assert inbound["settings"]["clients"] == [{"id": "uuid", "email": "device@test"}]
    assert inbound["streamSettings"]["network"] == "xhttp"
    assert "flow" not in inbound["settings"]["clients"][0]
    assert inbound["streamSettings"]["realitySettings"]["privateKey"] == "remote-private-key"
    assert any("docker run --rm" in command for command, _ in calls)
    assert any("docker compose restart" in command for command, _ in calls)


async def test_remote_vision_sync_adds_flow():
    manager = RemoteConfigNodeManager(Settings())
    item = remote_server("vision")
    uploaded = None

    async def ssh(server, command, stdin=None):
        nonlocal uploaded
        if command.startswith("cat /opt/xray-fr/config.json"):
            return json.dumps(remote_base_config()), ""
        if command.startswith("cat >"):
            uploaded = json.loads(stdin)
        return "", ""

    manager._ssh = ssh
    await manager.sync_clients(item, [{"id": "uuid", "email": "device@test"}])
    assert uploaded["inbounds"][0]["settings"]["clients"][0]["flow"] == "xtls-rprx-vision"
    assert uploaded["inbounds"][0]["streamSettings"]["network"] == "raw"


async def test_manual_sync_does_not_call_remote_backend(monkeypatch):
    nodes = NodeManagerRegistry(Settings())
    item = server("Manual", "FR", 10)

    async def fail(*args, **kwargs):
        raise AssertionError("remote backend must not be called")

    monkeypatch.setattr(nodes.managers["remote_config"], "sync_clients", fail)
    await nodes.managers["manual"].sync_clients(item, [{"id": "uuid", "email": "device@test"}])


async def test_remote_config_registry_syncs_active_profiles():
    engine, sessions = await database()
    nodes = NodeManagerRegistry(Settings())
    captured = []

    async def sync_clients(server, clients):
        captured.extend(clients)

    nodes.managers["remote_config"].sync_clients = sync_clients
    async with sessions() as session:
        item = remote_server()
        session.add(item)
        await session.flush()
        session.add(
            DeviceServerProfile(
                device_id="device-id",
                server_id=item.id,
                credential="active-uuid",
                client_email="active@test",
                uri="vless://active",
                is_active=True,
            )
        )
        await session.flush()
        await nodes.sync_server(session, item)
    assert captured == [{"id": "active-uuid", "email": "active@test"}]
    await engine.dispose()


async def test_remote_sync_keeps_candidate_when_validation_fails():
    manager = RemoteConfigNodeManager(Settings())
    item = remote_server()
    calls = []

    async def ssh(server, command, stdin=None):
        calls.append(command)
        if command.startswith("cat /opt/xray-fr/config.json"):
            return json.dumps(remote_base_config()), ""
        if command.startswith("docker run --rm"):
            raise RuntimeError("candidate invalid")
        return "", ""

    manager._ssh = ssh
    try:
        await manager.sync_clients(item, [{"id": "uuid", "email": "device@test"}])
    except RuntimeError as error:
        assert "/opt/xray-fr/config.candidate.json" in str(error)
    else:
        raise AssertionError("sync must fail")
    assert not any("cp /opt/xray-fr/config.candidate.json" in command for command in calls)


async def test_remote_sync_rolls_back_when_restart_fails():
    manager = RemoteConfigNodeManager(Settings())
    item = remote_server()
    calls = []

    async def ssh(server, command, stdin=None):
        calls.append(command)
        if command.startswith("cat /opt/xray-fr/config.json"):
            return json.dumps(remote_base_config()), ""
        if "cp /opt/xray-fr/config.candidate.json /opt/xray-fr/config.json" in command:
            raise RuntimeError("restart failed")
        return "", ""

    manager._ssh = ssh
    try:
        await manager.sync_clients(item, [{"id": "uuid", "email": "device@test"}])
    except RuntimeError as error:
        assert "rollback completed" in str(error).lower()
    else:
        raise AssertionError("sync must fail")
    assert any(
        command.startswith("cp /opt/xray-fr/config.json.backup /opt/xray-fr/config.json")
        for command in calls
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


def test_render_uri_uses_post_quantum_xhttp_parameters():
    item = server("PQ XHTTP", "FR", 10)
    item.transport = "xhttp"
    item.flow = ""
    item.pq_enabled = True
    item.vless_encryption = "mlkem-client-encryption"
    item.reality_mldsa65_verify = "mldsa-verify"
    item.reality_spider_x = "/"
    uri = render_server_uri(item, "11111111-1111-1111-1111-111111111111")
    assert "encryption=mlkem-client-encryption" in uri
    assert "pqv=mldsa-verify" in uri
    assert "spx=%2F" in uri
    assert "x_padding_bytes=100-1000" in uri
    assert "scMaxEachPostBytes" in uri


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


async def test_delete_server_removes_only_selected_server_profiles():
    engine, sessions = await database()
    async with sessions() as session:
        first = server("France old", "FR", 100)
        second = server("France online", "FR", 100)
        session.add_all([first, second])
        await session.flush()
        first_profile = DeviceServerProfile(
            device_id="device-old",
            server_id=first.id,
            credential="credential-old",
            client_email="old@vpn.local",
            uri="vless://old",
            is_active=True,
        )
        second_profile = DeviceServerProfile(
            device_id="device-online",
            server_id=second.id,
            credential="credential-online",
            client_email="online@vpn.local",
            uri="vless://online",
            is_active=True,
        )
        session.add_all([first_profile, second_profile])
        await session.commit()

        response = await server_delete(first.id, session)
        assert response.status_code == 303
        assert await session.get(VpnServer, first.id) is None
        assert await session.get(DeviceServerProfile, first_profile.id) is None
        assert await session.get(VpnServer, second.id) is not None
        assert await session.get(DeviceServerProfile, second_profile.id) is not None
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

        remote = remote_server()
        session.add(remote)
        await session.commit()

        async def remote_health(server):
            return "online"

        nodes.managers["remote_config"].health_check = remote_health
        assert await check_server_health(session, remote, nodes) == "online"
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
