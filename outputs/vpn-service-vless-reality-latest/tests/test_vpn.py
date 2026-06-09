from app.config import Settings
from app.vpn import MockVpnBackend


async def test_mock_profile_contains_vless_reality_parameters():
    settings = Settings(
        xray_public_host="vpn.example.com",
        xray_reality_public_key="public-key",
        xray_reality_short_id="0123456789abcdef",
    )
    profile = await MockVpnBackend(settings).create_peer("device@test", "Phone")
    assert profile.uri.startswith("vless://")
    assert "@vpn.example.com:443?" in profile.uri
    assert "security=reality" in profile.uri
    assert "flow=xtls-rprx-vision" in profile.uri
    assert "pbk=public-key" in profile.uri
