import asyncio
import base64
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from app.config import Settings

ADD_USER_OPERATION = "xray.app.proxyman.command.AddUserOperation"
REMOVE_USER_OPERATION = "xray.app.proxyman.command.RemoveUserOperation"
VLESS_ACCOUNT = "xray.proxy.vless.Account"


def _protobuf_varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _protobuf_bytes(field_number: int, value: bytes) -> bytes:
    return _protobuf_varint((field_number << 3) | 2) + _protobuf_varint(len(value)) + value


def _protobuf_string(field_number: int, value: str) -> bytes:
    return _protobuf_bytes(field_number, value.encode())


def _typed_message(message_type: str, value: bytes) -> bytes:
    return _protobuf_string(1, message_type) + _protobuf_bytes(2, value)


def add_user_operation_value(credential: str, client_email: str) -> str:
    account = _protobuf_string(1, credential) + _protobuf_string(2, "xtls-rprx-vision")
    user = _protobuf_string(2, client_email) + _protobuf_bytes(
        3, _typed_message(VLESS_ACCOUNT, account)
    )
    operation = _protobuf_bytes(1, user)
    return base64.b64encode(operation).decode()


def remove_user_operation_value(client_email: str) -> str:
    return base64.b64encode(_protobuf_string(1, client_email)).decode()


def alter_inbound_payload(tag: str, operation_type: str, operation_value: str) -> dict:
    return {
        "tag": tag,
        "operation": {
            "type": operation_type,
            "value": operation_value,
        },
    }


@dataclass(frozen=True)
class PeerProfile:
    credential: str
    client_email: str
    uri: str


@dataclass(frozen=True)
class PeerStats:
    last_activity_at: datetime | None
    transfer_rx: int
    transfer_tx: int


class VpnBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def create_peer(self, client_email: str, label: str) -> PeerProfile:
        raise NotImplementedError

    async def revoke_peer(self, client_email: str) -> None:
        raise NotImplementedError

    async def activate_peer(self, credential: str, client_email: str) -> None:
        raise NotImplementedError

    async def peer_stats(self) -> dict[str, PeerStats]:
        raise NotImplementedError

    def render_uri(self, credential: str, label: str) -> str:
        s = self.settings
        query = urlencode(
            {
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "sni": s.xray_reality_server_name,
                "fp": s.xray_fingerprint,
                "pbk": s.xray_reality_public_key,
                "sid": s.xray_reality_short_id,
                "type": "tcp",
                "headerType": "none",
            }
        )
        return (
            f"vless://{credential}@{s.xray_public_host}:{s.xray_public_port}"
            f"?{query}#{quote(label)}"
        )


class MockVpnBackend(VpnBackend):
    async def create_peer(self, client_email: str, label: str) -> PeerProfile:
        credential = str(uuid.uuid4())
        return PeerProfile(credential, client_email, self.render_uri(credential, label))

    async def revoke_peer(self, client_email: str) -> None:
        return None

    async def activate_peer(self, credential: str, client_email: str) -> None:
        return None

    async def peer_stats(self) -> dict[str, PeerStats]:
        return {}


class XrayBackend(VpnBackend):
    async def _grpc(self, method: str, payload: dict) -> dict:
        process = await asyncio.create_subprocess_exec(
            "grpcurl",
            "-plaintext",
            "-d",
            json.dumps(payload),
            self.settings.xray_api_address,
            method,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            error = stderr.decode().strip()
            if "already exists" in error.lower() or "not found" in error.lower():
                return {}
            raise RuntimeError(f"Xray API failed: {error}")
        output = stdout.decode().strip()
        return json.loads(output) if output else {}

    async def create_peer(self, client_email: str, label: str) -> PeerProfile:
        credential = str(uuid.uuid4())
        await self.activate_peer(credential, client_email)
        return PeerProfile(credential, client_email, self.render_uri(credential, label))

    async def activate_peer(self, credential: str, client_email: str) -> None:
        await self._grpc(
            "xray.app.proxyman.command.HandlerService/AlterInbound",
            alter_inbound_payload(
                self.settings.xray_inbound_tag,
                ADD_USER_OPERATION,
                add_user_operation_value(credential, client_email),
            ),
        )

    async def revoke_peer(self, client_email: str) -> None:
        await self._grpc(
            "xray.app.proxyman.command.HandlerService/AlterInbound",
            alter_inbound_payload(
                self.settings.xray_inbound_tag,
                REMOVE_USER_OPERATION,
                remove_user_operation_value(client_email),
            ),
        )

    async def peer_stats(self) -> dict[str, PeerStats]:
        response = await self._grpc(
            "xray.app.stats.command.StatsService/QueryStats",
            {"pattern": "user>>>", "reset": False},
        )
        result: dict[str, PeerStats] = {}
        for item in response.get("stat", []):
            parts = item.get("name", "").split(">>>")
            if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
                continue
            email, direction = parts[1], parts[3]
            previous = result.get(email, PeerStats(None, 0, 0))
            value = int(item.get("value", 0))
            active_at = datetime.now(UTC) if value else previous.last_activity_at
            result[email] = PeerStats(
                active_at,
                value if direction == "downlink" else previous.transfer_rx,
                value if direction == "uplink" else previous.transfer_tx,
            )
        return result


def build_vpn_backend(settings: Settings) -> VpnBackend:
    if settings.vpn_backend == "xray":
        required = {
            "XRAY_PUBLIC_HOST": settings.xray_public_host,
            "XRAY_REALITY_PUBLIC_KEY": settings.xray_reality_public_key,
            "XRAY_REALITY_SHORT_ID": settings.xray_reality_short_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing Xray settings: {', '.join(missing)}")
        return XrayBackend(settings)
    return MockVpnBackend(settings)


def new_client_email() -> str:
    return f"device-{secrets.token_hex(12)}@vpn.local"
