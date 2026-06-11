import asyncio
import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.models import Base, DeviceServerProfile, NodeEnrollment, VpnServer
from app.nodes import AgentNodeManager


async def database():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_node_install_register_sync_and_report():
    async def scenario():
        engine, sessions = await database()
        settings = Settings(panel_public_url="https://panel.example.com")
        async with sessions() as session:
            enrollment = NodeEnrollment(
                node_token="one-time-token",
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                server_name="France",
                expected_country_code="FR",
                status="pending",
            )
            session.add(enrollment)
            await session.commit()

        async def override_session():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as client:
            install = client.get("/node/install.sh")
            assert install.status_code == 200
            assert "sumrak-node-agent" in install.text
            assert "docker compose build --no-cache agent" in install.text
            assert "docker exec sumrak-node-agent docker version" in install.text
            assert "docker exec sumrak-node-agent docker restart sumrak-node-xray" in install.text
            assert 'PANEL_URL="https://panel.example.com"' in install.text
            assert '$PANEL_URL/api/node/register' in install.text
            dockerfile = client.get("/node/Dockerfile.agent")
            assert dockerfile.status_code == 200
            assert "docker.io ca-certificates" in dockerfile.text
            assert "&& docker --version" not in dockerfile.text
            assert "CMD [\"python\", \"/data/agent.py\"]" in dockerfile.text
            agent = client.get("/node/agent.py")
            assert "config.candidate.json" in agent.text
            assert '"-test"' in agent.text

            payload = {
                "node_token": "one-time-token",
                "public_host": "31.56.146.138",
                "public_port": 443,
                "reality_public_key": "public-key",
                "reality_short_id": "1ef9e2d66ba1729a",
                "reality_server_name": "www.microsoft.com",
                "xhttp_path": "/",
                "xhttp_mode": "auto",
                "agent_token": "agent-secret",
            }
            response = client.post("/api/node/register", json=payload)
            assert response.status_code == 200
            assert client.post("/api/node/register", json=payload).status_code == 409

            async with sessions() as session:
                server = await session.scalar(select(VpnServer).where(VpnServer.name == "France"))
                assert server.management_mode == "agent"
                assert server.transport == "xhttp"
                session.add(
                    DeviceServerProfile(
                        device_id="device-id",
                        server_id=server.id,
                        credential="uuid",
                        client_email="device@test",
                        uri="vless://uuid",
                        is_active=True,
                    )
                )
                await session.commit()

            headers = {"Authorization": "Bearer agent-secret"}
            sync = client.get("/api/node/sync", headers=headers)
            assert sync.status_code == 200
            assert sync.json()["clients"] == [
                {"id": "uuid", "email": "device@test", "flow": ""}
            ]
            assert client.get("/api/node/sync").status_code == 401
            report = client.post(
                "/api/node/report",
                headers=headers,
                json={"node_version": "0.1.0", "clients_count": 1, "last_error": None},
            )
            assert report.status_code == 200

            async with sessions() as session:
                server = await session.scalar(
                    select(VpnServer).where(VpnServer.agent_token == "agent-secret")
                )
                enrollment = await session.scalar(
                    select(NodeEnrollment).where(NodeEnrollment.node_token == "one-time-token")
                )
                assert server.agent_version == "0.1.0"
                assert server.agent_clients_count == 1
                assert enrollment.status == "used"
                assert enrollment.used_at is not None

        app.dependency_overrides.clear()
        await engine.dispose()

    asyncio.run(scenario())


async def test_agent_health_uses_last_seen_and_error():
    manager = AgentNodeManager(Settings())
    server = VpnServer(
        name="Agent",
        public_host="agent.example.com",
        reality_server_name="www.microsoft.com",
        reality_public_key="key",
        reality_short_id="short",
    )
    assert await manager.health_check(server) == "offline"
    server.agent_last_seen_at = datetime.now(UTC)
    assert await manager.health_check(server) == "online"
    server.agent_last_error = "sync failed"
    assert await manager.health_check(server) == "error"
    server.agent_last_error = None
    server.agent_last_seen_at = datetime.now(UTC) - timedelta(minutes=3)
    assert await manager.health_check(server) == "offline"


def test_agent_renders_xhttp_and_vision_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    monkeypatch.setenv("AGENT_TOKEN", "secret")
    runtime = importlib.import_module("app.node_agent_runtime")
    runtime.CONFIG_PATH = tmp_path / "config.json"
    runtime.HOST_NODE_DIR = tmp_path
    runtime.CONFIG_PATH.write_text(
        json.dumps(
            {
                "inbounds": [
                    {
                        "tag": "vless-reality",
                        "settings": {"clients": []},
                        "streamSettings": {
                            "realitySettings": {"privateKey": "private-key"}
                        },
                    }
                ]
            }
        )
    )
    desired = {
        "transport": "xhttp",
        "public_port": 443,
        "reality_target": "www.microsoft.com:443",
        "reality_server_name": "www.microsoft.com",
        "reality_short_id": "short-id",
        "xhttp_path": "/vpn",
        "xhttp_mode": "auto",
        "clients": [{"id": "uuid", "email": "device@test", "flow": ""}],
    }

    xhttp = json.loads(runtime.render(desired).read_text())
    inbound = xhttp["inbounds"][0]
    assert inbound["settings"]["clients"] == [{"id": "uuid", "email": "device@test"}]
    assert inbound["streamSettings"]["network"] == "xhttp"
    assert inbound["streamSettings"]["xhttpSettings"] == {"path": "/vpn", "mode": "auto"}
    assert inbound["streamSettings"]["realitySettings"]["privateKey"] == "private-key"

    desired["transport"] = "vision"
    vision = json.loads(runtime.render(desired).read_text())
    inbound = vision["inbounds"][0]
    assert inbound["settings"]["clients"][0]["flow"] == "xtls-rprx-vision"
    assert inbound["streamSettings"]["network"] == "raw"
    assert "xhttpSettings" not in inbound["streamSettings"]


def test_agent_rolls_back_config_when_restart_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    monkeypatch.setenv("AGENT_TOKEN", "secret")
    runtime = importlib.import_module("app.node_agent_runtime")
    runtime.CONFIG_PATH = tmp_path / "config.json"
    runtime.HOST_NODE_DIR = tmp_path
    runtime.CONFIG_PATH.write_text('{"version":"working"}')
    candidate = tmp_path / "config.candidate.json"
    candidate.write_text('{"version":"candidate"}')
    calls = 0

    def run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.CalledProcessError(1, args[0], stderr="restart failed")
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    try:
        runtime.apply(candidate)
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("restart failure must be propagated")

    assert runtime.CONFIG_PATH.read_text() == '{"version":"working"}'
    assert candidate.exists()
