import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import main_keyboard
from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.models import Base, TelegramProxyNode
from app.telegram_proxy import (
    best_proxy,
    is_online,
    issue_install_token,
    mask_secret,
    proxy_link,
    token_hash,
)
from app.telegram_proxy_agent_runtime import render_compose, run_command


async def database():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_proxy_token_link_mask_and_health():
    node = TelegramProxyNode(
        name="France",
        public_host="proxy.example.com",
        public_port=443,
        secret="ee1234567890abcdef",
        enabled=True,
        status="online",
        last_seen_at=datetime.now(UTC),
    )
    raw = issue_install_token(node)
    assert node.install_token_hash == token_hash(raw)
    assert node.install_token_expires_at > datetime.now(UTC)
    assert raw not in node.install_token_hash
    node.status = "online"
    assert is_online(node)
    node.last_seen_at = datetime.now(UTC) - timedelta(minutes=3)
    assert not is_online(node)
    assert proxy_link(node) == (
        "https://t.me/proxy?server=proxy.example.com&port=443&secret=ee1234567890abcdef"
    )
    assert mask_secret(node.secret) == "ee12…cdef"


def test_best_proxy_uses_online_priority():
    async def scenario():
        engine, sessions = await database()
        async with sessions() as session:
            session.add_all(
                [
                    TelegramProxyNode(
                        name="Offline",
                        public_host="offline.example.com",
                        secret="eeoffline",
                        priority=1,
                        enabled=True,
                        status="online",
                        last_seen_at=datetime.now(UTC) - timedelta(minutes=3),
                    ),
                    TelegramProxyNode(
                        name="Online",
                        public_host="online.example.com",
                        secret="eeonline",
                        priority=10,
                        enabled=True,
                        status="online",
                        last_seen_at=datetime.now(UTC),
                    ),
                ]
            )
            await session.commit()
            assert (await best_proxy(session)).name == "Online"
        await engine.dispose()

    asyncio.run(scenario())


def test_proxy_register_heartbeat_and_one_time_token():
    async def scenario():
        engine, sessions = await database()
        node = TelegramProxyNode(name="France")
        raw = issue_install_token(node)
        async with sessions() as session:
            session.add(node)
            await session.commit()

        async def override_session():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_settings] = lambda: Settings(
            panel_public_url="https://panel.example.com"
        )
        with TestClient(app) as client:
            install = client.get("/telegram-proxy/install.sh")
            assert install.status_code == 200
            assert 'PANEL_URL="https://panel.example.com"' in install.text
            assert 'INSTALL_DIR="/opt/sumrak-telegram-proxy"' in install.text
            assert "TCP port 443 is already occupied" in install.text
            assert "nineseconds/mtg:2" in install.text
            assert 'FAKETLS_DOMAIN="ya.ru"' in install.text
            assert 'SECRET="ee$(openssl rand -hex 16)$FAKETLS_DOMAIN_HEX"' in install.text
            assert '"secret":"%s"' in install.text
            assert "net.netfilter.nf_conntrack_max=262144" in install.text
            assert "network_mode: host" in install.text
            assert "agent docker compose version </dev/null" in install.text
            dockerfile = client.get("/telegram-proxy/Dockerfile.agent")
            assert dockerfile.status_code == 200
            assert "/usr/local/libexec/docker/cli-plugins/docker-compose" in dockerfile.text
            payload = {
                "install_token": raw,
                "public_host": "proxy.example.com",
                "public_port": 443,
                "secret": "ee1234567890abcdef",
                "agent_token": "agent-secret",
                "version": "1.0.0",
            }
            assert client.post("/api/telegram-proxy/register", json=payload).status_code == 200
            assert client.post("/api/telegram-proxy/register", json=payload).status_code == 409
            headers = {"Authorization": "Bearer agent-secret"}
            assert client.post("/api/telegram-proxy/sync", headers=headers).status_code == 200
            assert (
                client.post(
                    "/api/telegram-proxy/heartbeat",
                    headers=headers,
                    json={"status": "online", "version": "1.0.1", "active_connections": 5},
                ).status_code
                == 200
            )
            auth = ("admin", "change-me-now")
            admin_list = client.get("/admin/telegram-proxies", auth=auth)
            assert admin_list.status_code == 200
            assert "proxy.example.com:443" in admin_list.text
            admin_detail = client.get(f"/admin/telegram-proxies/{node.id}", auth=auth)
            assert admin_detail.status_code == 200
            assert "ee12…cdef" in admin_detail.text
            assert "ee1234567890abcdef" not in admin_detail.text
        async with sessions() as session:
            stored = await session.scalar(
                select(TelegramProxyNode).where(TelegramProxyNode.name == "France")
            )
            assert stored.install_token_hash is None
            assert stored.status == "online"
            assert stored.active_connections == 5
            assert stored.version == "1.0.1"
        app.dependency_overrides.clear()
        await engine.dispose()

    asyncio.run(scenario())


def test_mtg_faketls_config_and_bot_has_button():
    compose = render_compose(
        {
            "secret": "ee0123456789abcdef0123456789abcdef79612e7275",
            "sponsor_tag": "0123456789abcdef",
            "public_host": "151.243.3.15",
            "public_port": 443,
            "enabled": True,
        }
    )
    assert compose.startswith("name: sumrak-telegram-proxy")
    assert "image: nineseconds/mtg:2" in compose
    assert (
        'command: ["simple-run", "0.0.0.0:443", '
        '"ee0123456789abcdef0123456789abcdef79612e7275"]'
    ) in compose
    assert "TAG:" not in compose
    assert "network_mode: host" in compose
    assert "ports:" not in compose
    assert "\nservices:\n  proxy:\n" in compose
    labels = [button.text for row in main_keyboard().inline_keyboard for button in row]
    assert "🔗 Прокси Telegram" in labels


def test_mtg_rejects_legacy_secret():
    try:
        render_compose({"secret": "dd0123456789abcdef0123456789abcdef"})
        raise AssertionError("render_compose should reject legacy secrets")
    except ValueError as exc:
        assert "ee FakeTLS secret" in str(exc)


def test_agent_command_error_includes_stderr(monkeypatch):
    class Result:
        returncode = 125
        stdout = ""
        stderr = "docker: 'compose' is not a docker command"

    monkeypatch.setattr("app.telegram_proxy_agent_runtime.subprocess.run", lambda *args, **kwargs: Result())
    try:
        run_command(["docker", "compose", "config"])
        raise AssertionError("run_command should fail")
    except RuntimeError as exc:
        assert "docker: 'compose' is not a docker command" in str(exc)
