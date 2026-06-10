import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.models import Base
from app.nodes import NodeManagerRegistry
from app.webapp import validate_init_data


def signed_init_data(bot_token: str, telegram_id: int = 700) -> str:
    values = {
        "auth_date": str(int(datetime.now(UTC).timestamp())),
        "query_id": "test-query",
        "user": json.dumps(
            {
                "id": telegram_id,
                "first_name": "Web",
                "last_name": "User",
                "username": "web_user",
            },
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_validate_telegram_init_data():
    init_data = signed_init_data("secret-token")
    assert validate_init_data(init_data, "secret-token")["id"] == 700


def test_webapp_page_and_device_api(monkeypatch):
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        settings = Settings(
            bot_token="secret-token",
            bot_username="sumrak_test_bot",
            panel_public_url="https://panel.example.com",
            xray_public_host="vpn.example.com",
            xray_reality_public_key="public-key",
            xray_reality_short_id="0123456789abcdef",
        )

        async def override_session():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_settings] = lambda: settings
        app.state.nodes = NodeManagerRegistry(settings)
        headers = {"X-Telegram-Init-Data": signed_init_data(settings.bot_token)}
        with TestClient(app) as client:
            page = client.get("/webapp")
            assert page.status_code == 200
            assert "Sumrak VPN" in page.text

            me = client.get("/api/webapp/me", headers=headers)
            assert me.status_code == 200
            assert me.json()["status"] == "expired"

            created = client.post(
                "/api/webapp/devices", headers=headers, json={"platform": "ios"}
            )
            assert created.status_code == 201
            assert created.json()["name"] == "iPhone 1"
            assert created.json()["subscription_url"].startswith(
                "https://panel.example.com/sub/"
            )

            devices = client.get("/api/webapp/devices", headers=headers)
            assert len(devices.json()) == 1
            removed = client.delete(
                f"/api/webapp/devices/{created.json()['id']}", headers=headers
            )
            assert removed.status_code == 204
            assert client.get("/api/webapp/devices", headers=headers).json() == []

            referral = client.get("/api/webapp/referral", headers=headers)
            assert referral.status_code == 200
            assert referral.json()["link"].startswith("https://t.me/sumrak_test_bot")

            unauthorized = client.get("/api/webapp/me")
            assert unauthorized.status_code == 401

        app.dependency_overrides.clear()
        await engine.dispose()

    asyncio.run(scenario())
