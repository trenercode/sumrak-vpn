import base64

from app.config import Settings
from app.vpn import (
    ADD_USER_OPERATION,
    MockVpnBackend,
    REMOVE_USER_OPERATION,
    XrayBackend,
    add_user_operation_value,
    alter_inbound_payload,
    remove_user_operation_value,
)


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


def test_add_user_payload_uses_xray_typed_message_fields():
    value = add_user_operation_value("11111111-1111-1111-1111-111111111111", "device@test")
    payload = alter_inbound_payload("vless-reality", ADD_USER_OPERATION, value)

    assert payload == {
        "tag": "vless-reality",
        "operation": {
            "type": "xray.app.proxyman.command.AddUserOperation",
            "value": value,
        },
    }
    assert "@type" not in payload["operation"]
    assert "user" not in payload["operation"]
    decoded = base64.b64decode(value)
    assert b"11111111-1111-1111-1111-111111111111" in decoded
    assert b"device@test" in decoded
    assert b"xray.proxy.vless.Account" in decoded
    assert b"xtls-rprx-vision" in decoded


def test_remove_user_payload_uses_xray_typed_message_fields():
    value = remove_user_operation_value("device@test")
    payload = alter_inbound_payload("vless-reality", REMOVE_USER_OPERATION, value)

    assert payload["operation"]["type"] == "xray.app.proxyman.command.RemoveUserOperation"
    assert base64.b64decode(payload["operation"]["value"]) == b"\x0a\x0bdevice@test"


async def test_xray_backend_sends_typed_message_payloads():
    backend = XrayBackend(Settings(xray_inbound_tag="test-inbound"))
    calls = []

    async def capture(method, payload):
        calls.append((method, payload))
        return {}

    backend._grpc = capture
    await backend.activate_peer("11111111-1111-1111-1111-111111111111", "device@test")
    await backend.revoke_peer("device@test")

    assert calls[0][1]["operation"]["type"] == ADD_USER_OPERATION
    assert calls[1][1]["operation"]["type"] == REMOVE_USER_OPERATION
    assert all(set(payload["operation"]) == {"type", "value"} for _, payload in calls)
