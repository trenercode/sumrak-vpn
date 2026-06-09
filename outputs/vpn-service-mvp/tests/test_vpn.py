from app.config import Settings
from app.vpn import MockVpnBackend, next_available_ip


def test_next_available_ip_skips_server_and_used_addresses():
    result = next_available_ip("10.66.0.0/29", "10.66.0.1", {"10.66.0.2"})
    assert result == "10.66.0.3"


async def test_mock_profile_contains_required_values():
    settings = Settings(
        wg_server_public_key="server-key",
        wg_endpoint="vpn.example.com:51820",
    )
    profile = await MockVpnBackend(settings).create_peer("10.66.0.2")
    assert "Address = 10.66.0.2/32" in profile.config
    assert "PublicKey = server-key" in profile.config
    assert "Endpoint = vpn.example.com:51820" in profile.config

