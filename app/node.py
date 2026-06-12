import asyncio
import secrets
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Device, DeviceServerProfile, NodeEnrollment, User, VpnServer
from app.nodes import render_server_uri
from app.services import has_access
from app.vpn import new_client_email

router = APIRouter(tags=["node"])
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class NodeRegistration(BaseModel):
    node_token: str
    public_host: str
    public_port: int = 443
    reality_public_key: str
    reality_short_id: str
    reality_server_name: str = "www.microsoft.com"
    xhttp_path: str = "/"
    xhttp_mode: str = "auto"
    vless_encryption: str
    vless_decryption: str
    reality_mldsa65_seed: str
    reality_mldsa65_verify: str
    reality_spider_x: str = "/"
    agent_token: str

    @field_validator(
        "public_host",
        "reality_public_key",
        "reality_server_name",
        "vless_encryption",
        "vless_decryption",
        "reality_mldsa65_seed",
        "reality_mldsa65_verify",
        "agent_token",
    )
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value

    @field_validator("reality_short_id")
    @classmethod
    def validate_short_id(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) not in range(2, 17, 2) or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("shortId must contain 2-16 hexadecimal characters with even length")
        return value


class NodeReport(BaseModel):
    node_version: str
    last_error: str | None = None
    clients_count: int = 0
    reality_public_key: str | None = None


async def authenticated_agent(
    authorization: str = Header(""), session: AsyncSession = Depends(get_session)
) -> VpnServer:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing agent token")
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(select(VpnServer).where(VpnServer.agent_token == token))
    if server is None or not secrets.compare_digest(server.agent_token or "", token):
        raise HTTPException(401, "Invalid agent token")
    return server


async def reconcile_agent_server(session: AsyncSession, server: VpnServer) -> int:
    active_agents = list(
        await session.scalars(
            select(VpnServer).where(
                VpnServer.id != server.id,
                VpnServer.management_mode.in_(["agent", "agent_future"]),
                VpnServer.is_active.is_(True),
            )
        )
    )
    server_addresses = await resolve_host_addresses(server.public_host)
    for item in active_agents:
        if server_addresses.intersection(await resolve_host_addresses(item.public_host)):
            item.is_active = False

    existing_device_ids = set(
        await session.scalars(
            select(DeviceServerProfile.device_id).where(
                DeviceServerProfile.server_id == server.id
            )
        )
    )
    devices = (
        await session.execute(
            select(Device, User)
            .join(User, Device.user_id == User.id)
            .where(Device.is_revoked.is_(False))
        )
    ).all()
    created = 0
    for device, user in devices:
        if device.id in existing_device_ids or not has_access(user):
            continue
        credential = str(uuid.uuid4())
        session.add(
            DeviceServerProfile(
                device_id=device.id,
                server_id=server.id,
                credential=credential,
                client_email=new_client_email(),
                uri=render_server_uri(server, credential),
            )
        )
        created += 1
    await session.flush()
    return created


async def resolve_host_addresses(host: str) -> set[str]:
    addresses = {host.strip().lower()}
    try:
        results = await asyncio.get_running_loop().run_in_executor(
            None, socket.getaddrinfo, host, None
        )
    except OSError:
        return addresses
    addresses.update(result[4][0].lower() for result in results)
    return addresses


@router.get("/node/install.sh", response_class=PlainTextResponse)
async def install_script(settings: Settings = Depends(get_settings)):
    panel_url = settings.panel_public_url.rstrip("/")
    script = f"""#!/usr/bin/env bash
set -euo pipefail
NODE_TOKEN="${{1:-}}"
PANEL_URL="{panel_url}"
[[ "$(id -u)" == "0" ]] || {{ echo "Run as root" >&2; exit 1; }}
[[ -n "$NODE_TOKEN" ]] || {{ echo "NODE_TOKEN is required" >&2; exit 1; }}
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get update && apt-get install -y docker-compose-plugin
fi
if ! command -v openssl >/dev/null; then
  apt-get update && apt-get install -y openssl
fi
mkdir -p /opt/sumrak-node
cd /opt/sumrak-node
XRAY_IMAGE="ghcr.io/xtls/xray-core:26.6.1"
docker pull "$XRAY_IMAGE"
KEYS="$(docker run --rm "$XRAY_IMAGE" x25519)"
PRIVATE_KEY="$(printf '%s\\n' "$KEYS" | awk -F': ' 'tolower($1) ~ /private/ {{print $2; exit}}')"
PUBLIC_KEY="$(printf '%s\\n' "$KEYS" | awk -F': ' 'tolower($1) ~ /^(public key|password \\(publickey\\))$/ {{print $2; exit}}')"
[[ -n "$PRIVATE_KEY" && -n "$PUBLIC_KEY" ]] || {{ echo "Could not parse REALITY private/public key" >&2; exit 1; }}
VLESS_KEYS="$(docker run --rm "$XRAY_IMAGE" vlessenc)"
VLESS_DECRYPTION="$(printf '%s\\n' "$VLESS_KEYS" | awk -F'"' '/"decryption"/ {{print $4; exit}}')"
VLESS_ENCRYPTION="$(printf '%s\\n' "$VLESS_KEYS" | awk -F'"' '/"encryption"/ {{print $4; exit}}')"
[[ -n "$VLESS_DECRYPTION" && -n "$VLESS_ENCRYPTION" ]] || {{ echo "Could not parse VLESS encryption keys" >&2; exit 1; }}
MLDSA_KEYS="$(docker run --rm "$XRAY_IMAGE" mldsa65)"
MLDSA_SEED="$(printf '%s\\n' "$MLDSA_KEYS" | awk -F': ' 'tolower($1) == "seed" {{print $2; exit}}')"
MLDSA_VERIFY="$(printf '%s\\n' "$MLDSA_KEYS" | awk -F': ' 'tolower($1) == "verify" {{print $2; exit}}')"
[[ -n "$MLDSA_SEED" && -n "$MLDSA_VERIFY" ]] || {{ echo "Could not parse ML-DSA65 keys" >&2; exit 1; }}
SHORT_ID="$(openssl rand -hex 8)"
AGENT_TOKEN="$(openssl rand -hex 32)"
PUBLIC_HOST="${{PUBLIC_HOST:-$(curl -fsSL https://api.ipify.org)}}"
[[ "$SHORT_ID" =~ ^[0-9a-f]{{16}}$ ]] || {{ echo "Invalid REALITY shortId" >&2; exit 1; }}
[[ -n "$PUBLIC_HOST" ]] || {{ echo "Could not determine public host" >&2; exit 1; }}
cat > config.json <<EOF
{{"log":{{"loglevel":"warning"}},"inbounds":[{{"tag":"vless-reality","listen":"0.0.0.0","port":443,"protocol":"vless","settings":{{"clients":[],"decryption":"$VLESS_DECRYPTION"}},"streamSettings":{{"network":"xhttp","security":"reality","xhttpSettings":{{"host":"","path":"/","mode":"auto","xPaddingBytes":"100-1000","scMaxEachPostBytes":"1000000","scMaxBufferedPosts":30,"scStreamUpServerSecs":"20-80"}},"realitySettings":{{"show":false,"target":"web.max.ru:443","serverNames":["web.max.ru"],"privateKey":"$PRIVATE_KEY","shortIds":["$SHORT_ID"],"mldsa65Seed":"$MLDSA_SEED"}}}},"sniffing":{{"enabled":false,"destOverride":["http","tls","quic"]}}}}],"outbounds":[{{"tag":"direct","protocol":"freedom"}},{{"tag":"blocked","protocol":"blackhole"}}]}}
EOF
curl -fsSL "$PANEL_URL/node/agent.py" -o agent.py
curl -fsSL "$PANEL_URL/node/Dockerfile.agent" -o Dockerfile.agent
cat > compose.yaml <<EOF
services:
  xray:
    image: ghcr.io/xtls/xray-core:26.6.1
    container_name: sumrak-node-xray
    restart: unless-stopped
    command: run -config /etc/xray/config.json
    ports: ["443:443"]
    volumes: ["./config.json:/etc/xray/config.json:ro"]
  agent:
    build:
      context: .
      dockerfile: Dockerfile.agent
    container_name: sumrak-node-agent
    restart: unless-stopped
    environment:
      PANEL_URL: "$PANEL_URL"
      AGENT_TOKEN: "$AGENT_TOKEN"
      HOST_NODE_DIR: "/opt/sumrak-node"
      XRAY_CONTAINER_NAME: "sumrak-node-xray"
      XRAY_IMAGE: "ghcr.io/xtls/xray-core:26.6.1"
    volumes:
      - ./:/data
      - /var/run/docker.sock:/var/run/docker.sock
EOF
docker compose build --no-cache agent
docker compose up -d xray
curl -fsSL -X POST "$PANEL_URL/api/node/register" -H 'Content-Type: application/json' -d "$(printf '{{"node_token":"%s","public_host":"%s","public_port":443,"reality_public_key":"%s","reality_short_id":"%s","reality_server_name":"web.max.ru","xhttp_path":"/","xhttp_mode":"auto","vless_encryption":"%s","vless_decryption":"%s","reality_mldsa65_seed":"%s","reality_mldsa65_verify":"%s","reality_spider_x":"/","agent_token":"%s"}}' "$NODE_TOKEN" "$PUBLIC_HOST" "$PUBLIC_KEY" "$SHORT_ID" "$VLESS_ENCRYPTION" "$VLESS_DECRYPTION" "$MLDSA_SEED" "$MLDSA_VERIFY" "$AGENT_TOKEN")"
docker compose up -d agent
docker exec sumrak-node-agent docker version
docker exec sumrak-node-agent docker restart sumrak-node-xray
echo "Sumrak node installed: $PUBLIC_HOST:443"
"""
    return PlainTextResponse(script, media_type="text/x-shellscript")


@router.get("/node/agent.py", response_class=PlainTextResponse)
async def agent_runtime():
    return PlainTextResponse(
        (BASE_DIR / "node_agent_runtime.py").read_text(), media_type="text/x-python"
    )


@router.get("/node/Dockerfile.agent", response_class=PlainTextResponse)
async def agent_dockerfile():
    return PlainTextResponse(
        (PROJECT_DIR / "deploy" / "node" / "Dockerfile.agent").read_text(),
        media_type="text/plain",
    )


@router.post("/api/node/register")
async def register_node(
    payload: NodeRegistration, session: AsyncSession = Depends(get_session)
):
    current = datetime.now(UTC)
    enrollment = await session.scalar(
        select(NodeEnrollment).where(NodeEnrollment.node_token == payload.node_token)
    )
    if (
        enrollment is None
        or enrollment.status != "pending"
        or enrollment.used_at is not None
        or as_utc(enrollment.expires_at) <= current
    ):
        raise HTTPException(409, "Enrollment token is invalid, expired, or already used")
    if await session.scalar(select(VpnServer.id).where(VpnServer.agent_token == payload.agent_token)):
        raise HTTPException(409, "Agent token already exists")
    server = VpnServer(
        name=enrollment.server_name,
        country_code=enrollment.expected_country_code,
        country_name=enrollment.expected_country_code,
        public_host=payload.public_host,
        public_port=payload.public_port,
        protocol="vless-reality",
        transport="xhttp",
        reality_target=f"{payload.reality_server_name}:443",
        reality_server_name=payload.reality_server_name,
        reality_public_key=payload.reality_public_key,
        reality_short_id=payload.reality_short_id,
        fingerprint="firefox",
        flow="",
        xhttp_path=payload.xhttp_path,
        xhttp_mode=payload.xhttp_mode,
        pq_enabled=True,
        vless_encryption=payload.vless_encryption,
        vless_decryption=payload.vless_decryption,
        reality_mldsa65_seed=payload.reality_mldsa65_seed,
        reality_mldsa65_verify=payload.reality_mldsa65_verify,
        reality_spider_x=payload.reality_spider_x,
        management_mode="agent",
        agent_token=payload.agent_token,
        agent_last_seen_at=current,
        agent_version="installing",
        agent_clients_count=0,
        is_active=True,
    )
    session.add(server)
    await session.flush()
    await reconcile_agent_server(session, server)
    enrollment.used_at = current
    enrollment.status = "used"
    await session.commit()
    return {"server_id": server.id, "status": "registered"}


@router.get("/api/node/sync")
async def sync_node(
    server: VpnServer = Depends(authenticated_agent),
    session: AsyncSession = Depends(get_session),
):
    current = datetime.now(UTC)
    await reconcile_agent_server(session, server)
    clients = [
        {"id": profile.credential, "email": profile.client_email, "flow": server.flow or ""}
        for profile in await session.scalars(
            select(DeviceServerProfile).where(
                DeviceServerProfile.server_id == server.id,
                DeviceServerProfile.is_active.is_(True),
            )
        )
    ]
    server.agent_last_seen_at = current
    server.agent_last_sync_at = current
    server.agent_clients_count = len(clients)
    server.health_status = "online"
    await session.commit()
    return {
        "transport": server.transport,
        "public_port": server.public_port,
        "reality_target": server.reality_target,
        "reality_server_name": server.reality_server_name,
        "reality_short_id": server.reality_short_id,
        "xhttp_path": server.xhttp_path,
        "xhttp_mode": server.xhttp_mode,
        "pq_enabled": server.pq_enabled,
        "vless_decryption": server.vless_decryption,
        "reality_mldsa65_seed": server.reality_mldsa65_seed,
        "clients": clients,
    }


@router.post("/api/node/report")
async def report_node(
    payload: NodeReport,
    server: VpnServer = Depends(authenticated_agent),
    session: AsyncSession = Depends(get_session),
):
    server.agent_last_seen_at = datetime.now(UTC)
    server.agent_version = payload.node_version
    server.agent_last_error = payload.last_error
    server.agent_clients_count = payload.clients_count
    if payload.reality_public_key:
        server.reality_public_key = payload.reality_public_key
    server.health_status = "error" if payload.last_error else "online"
    await session.commit()
    return {"status": "ok"}
