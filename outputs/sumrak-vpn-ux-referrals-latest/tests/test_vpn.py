import asyncio
import json

from app.config import Settings
from app.vpn import MockVpnBackend, XrayBackend


def xray_config() -> dict:
    return {
        "inbounds": [
            {
                "tag": "vless-reality",
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
            }
        ]
    }


async def test_mock_profile_contains_vless_reality_parameters():
    settings = Settings(
        xray_public_host="vpn.example.com",
        xray_reality_public_key="public-key",
        xray_reality_short_id="0123456789abcdef",
    )
    profile = await MockVpnBackend(settings).create_peer("device@test", "Phone")
    assert profile.uri.startswith("vless://")
    assert "@vpn.example.com:8443?" in profile.uri
    assert "security=reality" in profile.uri
    assert "flow=xtls-rprx-vision" in profile.uri
    assert "pbk=public-key" in profile.uri
    assert "sni=www.microsoft.com" in profile.uri


async def test_activate_peer_adds_client_and_restarts_xray(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(xray_config()))
    backend = XrayBackend(Settings(xray_config_path=str(config_path)))
    restarts = []

    async def restart():
        restarts.append(True)

    backend._restart_xray = restart
    await backend.activate_peer("11111111-1111-1111-1111-111111111111", "device@test")

    clients = json.loads(config_path.read_text())["inbounds"][0]["settings"]["clients"]
    assert clients == [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "device@test",
            "flow": "xtls-rprx-vision",
        }
    ]
    assert restarts == [True]

    await backend.activate_peer("11111111-1111-1111-1111-111111111111", "device@test")
    assert restarts == [True]


async def test_revoke_peer_removes_client_and_restarts_xray(tmp_path):
    config = xray_config()
    config["inbounds"][0]["settings"]["clients"].append(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "device@test",
            "flow": "xtls-rprx-vision",
        }
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    backend = XrayBackend(Settings(xray_config_path=str(config_path)))
    restarts = []

    async def restart():
        restarts.append(True)

    backend._restart_xray = restart
    await backend.revoke_peer("device@test")

    clients = json.loads(config_path.read_text())["inbounds"][0]["settings"]["clients"]
    assert clients == []
    assert restarts == [True]

    await backend.revoke_peer("device@test")
    assert restarts == [True]


async def test_restart_xray_uses_docker_socket(monkeypatch):
    class Reader:
        async def readline(self):
            return b"HTTP/1.1 204 No Content\r\n"

        async def read(self):
            return b""

    class Writer:
        request = b""

        def write(self, value):
            self.request += value

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    writer = Writer()

    async def open_socket(path):
        assert path == "/var/run/docker.sock"
        return Reader(), writer

    monkeypatch.setattr(asyncio, "open_unix_connection", open_socket)
    await XrayBackend(Settings(xray_container_name="vpn-xray"))._restart_xray()
    assert writer.request.startswith(b"POST /containers/vpn-xray/restart?t=10 HTTP/1.1\r\n")
