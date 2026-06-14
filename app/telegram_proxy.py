import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin import require_admin
from app.config import Settings, get_settings
from app.db import get_session
from app.models import TelegramProxyEvent, TelegramProxyNode

router = APIRouter(tags=["telegram-proxy"])
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mask_secret(secret: str | None) -> str:
    if not secret:
        return "—"
    return f"{secret[:4]}…{secret[-4:]}" if len(secret) > 8 else "••••••"


def proxy_link(node: TelegramProxyNode) -> str:
    query = urlencode(
        {"server": node.public_host, "port": node.public_port, "secret": node.secret or ""}
    )
    return f"https://t.me/proxy?{query}"


def is_online(node: TelegramProxyNode, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    seen = node.last_seen_at
    if seen and seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return bool(node.enabled and node.status == "online" and seen and seen > now - timedelta(minutes=2))


async def best_proxy(session: AsyncSession) -> TelegramProxyNode | None:
    nodes = list(
        await session.scalars(
            select(TelegramProxyNode)
            .where(TelegramProxyNode.enabled.is_(True))
            .order_by(TelegramProxyNode.priority, TelegramProxyNode.name)
        )
    )
    return next((node for node in nodes if node.secret and is_online(node)), None)


def add_event(session: AsyncSession, node: TelegramProxyNode, event_type: str, message: str = ""):
    session.add(TelegramProxyEvent(node_id=node.id, event_type=event_type, message=message[:2000]))


def issue_install_token(node: TelegramProxyNode) -> str:
    raw = secrets.token_urlsafe(32)
    node.install_token_hash = token_hash(raw)
    node.install_token_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    node.status = "pending"
    return raw


class ProxyRegistration(BaseModel):
    install_token: str
    public_host: str
    public_port: int = 443
    secret: str
    agent_token: str
    version: str = "1"


class ProxyHeartbeat(BaseModel):
    status: str = "online"
    version: str = "1"
    active_connections: int | None = None
    traffic_bytes: int | None = None
    config_hash: str | None = None
    error: str | None = None


async def authenticated_proxy_agent(
    authorization: str = Header(""), session: AsyncSession = Depends(get_session)
) -> TelegramProxyNode:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing agent token")
    raw = authorization.removeprefix("Bearer ").strip()
    digest = token_hash(raw)
    node = await session.scalar(
        select(TelegramProxyNode).where(TelegramProxyNode.agent_token_hash == digest)
    )
    if node is None or not secrets.compare_digest(node.agent_token_hash or "", digest):
        raise HTTPException(401, "Invalid agent token")
    return node


@router.get("/telegram-proxy/install.sh", response_class=PlainTextResponse)
async def install_script(settings: Settings = Depends(get_settings)):
    script = (PROJECT_DIR / "deploy" / "telegram-proxy" / "install.sh").read_text()
    return PlainTextResponse(
        script.replace("__PANEL_URL__", settings.panel_public_url.rstrip("/")),
        media_type="text/x-shellscript",
    )


@router.get("/telegram-proxy/agent.py", response_class=PlainTextResponse)
async def agent_script():
    return PlainTextResponse(
        (BASE_DIR / "telegram_proxy_agent_runtime.py").read_text(),
        media_type="text/x-python",
    )


@router.get("/telegram-proxy/Dockerfile.agent", response_class=PlainTextResponse)
async def agent_dockerfile():
    return PlainTextResponse(
        (PROJECT_DIR / "deploy" / "telegram-proxy" / "Dockerfile.agent").read_text()
    )


@router.post("/api/telegram-proxy/register")
async def register_proxy(payload: ProxyRegistration, session: AsyncSession = Depends(get_session)):
    digest = token_hash(payload.install_token)
    node = await session.scalar(
        select(TelegramProxyNode).where(TelegramProxyNode.install_token_hash == digest)
    )
    now = datetime.now(UTC)
    expires = node.install_token_expires_at if node else None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if node is None or not expires or expires <= now:
        raise HTTPException(409, "Install token is invalid or expired")
    node.public_host = payload.public_host.strip()
    node.public_port = payload.public_port
    registered_secret = payload.secret.strip()
    if not (node.secret or "").startswith("ee"):
        node.secret = registered_secret
    node.agent_token_hash = token_hash(payload.agent_token)
    node.install_token_hash = None
    node.install_token_expires_at = None
    node.enabled = True
    node.status = "online"
    node.version = payload.version
    node.last_seen_at = now
    node.health_error = None
    add_event(session, node, "registered", f"{node.public_host}:{node.public_port}")
    await session.commit()
    return {"node_id": node.id, "status": "registered", "secret": node.secret}


@router.post("/api/telegram-proxy/heartbeat")
async def proxy_heartbeat(
    payload: ProxyHeartbeat,
    node: TelegramProxyNode = Depends(authenticated_proxy_agent),
    session: AsyncSession = Depends(get_session),
):
    node.status = payload.status if node.enabled else "disabled"
    node.version = payload.version
    node.active_connections = payload.active_connections
    node.traffic_bytes = payload.traffic_bytes
    node.current_config_hash = payload.config_hash
    node.health_error = payload.error
    node.last_seen_at = datetime.now(UTC)
    if payload.error:
        add_event(session, node, "error", payload.error)
    await session.commit()
    return {"ok": True, "enabled": node.enabled}


@router.api_route("/api/telegram-proxy/sync", methods=["GET", "POST"])
async def proxy_sync(
    node: TelegramProxyNode = Depends(authenticated_proxy_agent),
    session: AsyncSession = Depends(get_session),
):
    node.last_sync_at = datetime.now(UTC)
    await session.commit()
    return {
        "enabled": node.enabled,
        "secret": node.secret,
        "sponsor_tag": node.sponsor_tag or "",
        "public_host": node.public_host,
        "public_port": node.public_port,
    }


def templates(request: Request):
    return request.app.state.templates


@router.get(
    "/admin/telegram-proxies",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def proxies_admin(request: Request, session: AsyncSession = Depends(get_session)):
    nodes = list(
        await session.scalars(
            select(TelegramProxyNode).order_by(TelegramProxyNode.priority, TelegramProxyNode.name)
        )
    )
    return templates(request).TemplateResponse(
        request,
        "telegram_proxies.html",
        {"nodes": nodes, "is_online": is_online, "mask_secret": mask_secret},
    )


@router.post("/admin/telegram-proxies", dependencies=[Depends(require_admin)])
async def create_proxy(
    request: Request,
    name: str = Form(...),
    country_code: str = Form(""),
    priority: int = Form(100),
    public_port: int = Form(443),
    session: AsyncSession = Depends(get_session),
):
    node = TelegramProxyNode(
        name=name.strip(),
        country_code=country_code.strip().upper(),
        priority=priority,
        public_port=public_port,
    )
    session.add(node)
    await session.flush()
    raw = issue_install_token(node)
    add_event(session, node, "install_token_issued")
    await session.commit()
    command = (
        f"curl -sSL {get_settings().panel_public_url.rstrip('/')}/telegram-proxy/install.sh "
        f"| bash -s -- {raw}"
    )
    return templates(request).TemplateResponse(
        request,
        "telegram_proxy_install.html",
        {"node": node, "command": command},
    )


@router.get(
    "/admin/telegram-proxies/{node_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def proxy_detail(
    request: Request, node_id: str, session: AsyncSession = Depends(get_session)
):
    node = await session.scalar(
        select(TelegramProxyNode)
        .where(TelegramProxyNode.id == node_id)
        .options(selectinload(TelegramProxyNode.events))
    )
    if node is None:
        raise HTTPException(404)
    return templates(request).TemplateResponse(
        request,
        "telegram_proxy.html",
        {
            "node": node,
            "online": is_online(node),
            "secret_masked": mask_secret(node.secret),
        },
    )


@router.post("/admin/telegram-proxies/{node_id}", dependencies=[Depends(require_admin)])
async def update_proxy(
    node_id: str,
    name: str = Form(...),
    country_code: str = Form(""),
    priority: int = Form(100),
    sponsor_tag: str = Form(""),
    sponsor_channel: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    node = await session.get(TelegramProxyNode, node_id)
    if node is None:
        raise HTTPException(404)
    node.name = name.strip()
    node.country_code = country_code.strip().upper()
    node.priority = priority
    node.sponsor_tag = sponsor_tag.strip() or None
    node.sponsor_channel = sponsor_channel.strip() or None
    add_event(session, node, "updated")
    await session.commit()
    return RedirectResponse(f"/admin/telegram-proxies/{node.id}", status_code=303)


@router.post("/admin/telegram-proxies/{node_id}/toggle", dependencies=[Depends(require_admin)])
async def toggle_proxy(node_id: str, session: AsyncSession = Depends(get_session)):
    node = await session.get(TelegramProxyNode, node_id)
    if node is None:
        raise HTTPException(404)
    node.enabled = not node.enabled
    node.status = "unknown" if node.enabled else "disabled"
    add_event(session, node, "enabled" if node.enabled else "disabled")
    await session.commit()
    return RedirectResponse(f"/admin/telegram-proxies/{node.id}", status_code=303)


@router.post("/admin/telegram-proxies/{node_id}/install", dependencies=[Depends(require_admin)])
async def regenerate_install(
    request: Request, node_id: str, session: AsyncSession = Depends(get_session)
):
    node = await session.get(TelegramProxyNode, node_id)
    if node is None:
        raise HTTPException(404)
    raw = issue_install_token(node)
    add_event(session, node, "install_token_issued")
    await session.commit()
    command = (
        f"curl -sSL {get_settings().panel_public_url.rstrip('/')}/telegram-proxy/install.sh "
        f"| bash -s -- {raw}"
    )
    return templates(request).TemplateResponse(
        request, "telegram_proxy_install.html", {"node": node, "command": command}
    )


@router.post("/admin/telegram-proxies/{node_id}/delete", dependencies=[Depends(require_admin)])
async def delete_proxy(node_id: str, session: AsyncSession = Depends(get_session)):
    node = await session.get(TelegramProxyNode, node_id)
    if node is None:
        raise HTTPException(404)
    if node.enabled or is_online(node):
        raise HTTPException(409, "Disable the proxy before deleting it")
    await session.delete(node)
    await session.commit()
    return RedirectResponse("/admin/telegram-proxies", status_code=303)
