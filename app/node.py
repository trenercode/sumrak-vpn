import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import DeviceServerProfile, NodeEnrollment, VpnServer

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
    agent_token: str


class NodeReport(BaseModel):
    node_version: str
    last_error: str | None = None
    clients_count: int = 0


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
KEYS="$(docker run --rm ghcr.io/xtls/xray-core:latest x25519)"
PRIVATE_KEY="$(printf '%s\\n' "$KEYS" | awk -F': ' 'tolower($1) ~ /private/ {{print $2; exit}}')"
PUBLIC_KEY="$(printf '%s\\n' "$KEYS" | awk -F': ' 'tolower($1) ~ /public|password/ {{print $2; exit}}')"
SHORT_ID="$(openssl rand -hex 8)"
AGENT_TOKEN="$(openssl rand -hex 32)"
PUBLIC_HOST="${{PUBLIC_HOST:-$(curl -fsSL https://api.ipify.org)}}"
cat > config.json <<EOF
{{"log":{{"loglevel":"warning"}},"inbounds":[{{"tag":"vless-reality","listen":"0.0.0.0","port":443,"protocol":"vless","settings":{{"clients":[],"decryption":"none"}},"streamSettings":{{"network":"xhttp","security":"reality","xhttpSettings":{{"path":"/","mode":"auto"}},"realitySettings":{{"show":false,"target":"www.microsoft.com:443","serverNames":["www.microsoft.com"],"privateKey":"$PRIVATE_KEY","shortIds":["$SHORT_ID"]}}}}}}],"outbounds":[{{"tag":"direct","protocol":"freedom"}}]}}
EOF
curl -fsSL "$PANEL_URL/node/agent.py" -o agent.py
curl -fsSL "$PANEL_URL/node/Dockerfile.agent" -o Dockerfile.agent
cat > compose.yaml <<EOF
services:
  xray:
    image: ghcr.io/xtls/xray-core:latest
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
    volumes:
      - ./:/data
      - /var/run/docker.sock:/var/run/docker.sock
EOF
docker compose build --no-cache agent
docker compose up -d
docker exec sumrak-node-agent docker version
docker exec sumrak-node-agent docker restart sumrak-node-xray
curl -fsSL -X POST "$PANEL_URL/api/node/register" -H 'Content-Type: application/json' -d "$(printf '{{"node_token":"%s","public_host":"%s","public_port":443,"reality_public_key":"%s","reality_short_id":"%s","reality_server_name":"www.microsoft.com","xhttp_path":"/","xhttp_mode":"auto","agent_token":"%s"}}' "$NODE_TOKEN" "$PUBLIC_HOST" "$PUBLIC_KEY" "$SHORT_ID" "$AGENT_TOKEN")"
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
        fingerprint="chrome",
        flow="",
        xhttp_path=payload.xhttp_path,
        xhttp_mode=payload.xhttp_mode,
        management_mode="agent",
        agent_token=payload.agent_token,
        agent_last_seen_at=current,
        agent_version="installing",
        agent_clients_count=0,
        is_active=True,
    )
    session.add(server)
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
    server.health_status = "error" if payload.last_error else "online"
    await session.commit()
    return {"status": "ok"}
