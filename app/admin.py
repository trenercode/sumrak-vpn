import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db import get_session
from app.models import (
    Broadcast,
    BroadcastRecipient,
    Device,
    DeviceServerProfile,
    NodeEnrollment,
    User,
    VpnClient,
    VpnServer,
)
from app.nodes import NodeManagerRegistry, check_server_health, update_server_device_count
from app.services import (
    grant_subscription,
    load_user_with_devices,
    prepare_broadcast,
    record_successful_payment,
    revoke_device,
)

router = APIRouter(prefix="/admin")
security = HTTPBasic()


def require_admin(
    credentials: HTTPBasicCredentials = Depends(security),
    settings: Settings = Depends(get_settings),
) -> None:
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_password = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )


def templates(request: Request):
    return request.app.state.templates


def node_registry(request: Request) -> NodeManagerRegistry:
    return request.app.state.nodes


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    current = datetime.now(UTC)
    total_users = await session.scalar(select(func.count()).select_from(User))
    active_users = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.is_blocked.is_(False),
            or_(User.trial_ends_at > current, User.subscription_ends_at > current),
        )
    )
    active_devices = await session.scalar(
        select(func.count()).select_from(Device).where(Device.is_revoked.is_(False))
    )
    online_devices = await session.scalar(
        select(func.count())
        .select_from(Device)
        .where(
            Device.is_revoked.is_(False),
            Device.last_activity_at > current - timedelta(minutes=3),
        )
    )
    recent_users = list(await session.scalars(select(User).order_by(User.created_at.desc()).limit(10)))
    return templates(request).TemplateResponse(
        request,
        "dashboard.html",
        {
            "total_users": total_users,
            "active_users": active_users,
            "active_devices": active_devices,
            "online_devices": online_devices,
            "recent_users": recent_users,
        },
    )


@router.get("/users", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def users(request: Request, q: str = "", session: AsyncSession = Depends(get_session)):
    query = select(User).options(selectinload(User.devices)).order_by(User.created_at.desc())
    if q:
        query = query.where(
            or_(User.username.ilike(f"%{q}%"), User.full_name.ilike(f"%{q}%"))
        )
    result = list(await session.scalars(query.limit(200)))
    return templates(request).TemplateResponse(request, "users.html", {"users": result, "q": q})


@router.get("/users/{user_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def user_detail(request: Request, user_id: str, session: AsyncSession = Depends(get_session)):
    user = await load_user_with_devices(session, user_id)
    if user is None:
        raise HTTPException(404)
    return templates(request).TemplateResponse(request, "user.html", {"user": user})


@router.post("/users/{user_id}/grant", dependencies=[Depends(require_admin)])
async def grant(
    user_id: str,
    days: int = Form(..., ge=1, le=3650),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    await grant_subscription(session, user, days)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/payment", dependencies=[Depends(require_admin)])
async def payment(
    user_id: str,
    days: int = Form(..., ge=1, le=3650),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    await record_successful_payment(session, user, days)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/toggle-block", dependencies=[Depends(require_admin)])
async def toggle_block(
    request: Request, user_id: str, session: AsyncSession = Depends(get_session)
):
    user = await load_user_with_devices(session, user_id)
    if user is None:
        raise HTTPException(404)
    user.is_blocked = not user.is_blocked
    if user.is_blocked:
        for device in user.devices:
            if not device.is_revoked:
                await revoke_device(session, device, get_settings(), node_registry(request))
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/devices/{device_id}/revoke", dependencies=[Depends(require_admin)])
async def revoke(request: Request, device_id: str, session: AsyncSession = Depends(get_session)):
    device = await session.get(Device, device_id)
    if device is None:
        raise HTTPException(404)
    await revoke_device(session, device, get_settings(), node_registry(request))
    return RedirectResponse(f"/admin/users/{device.user_id}", status_code=303)


@router.get("/servers", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def servers(request: Request, session: AsyncSession = Depends(get_session)):
    items = list(await session.scalars(select(VpnServer).order_by(VpnServer.priority, VpnServer.name)))
    for item in items:
        await update_server_device_count(session, item)
    enrollments = list(
        await session.scalars(
            select(NodeEnrollment).order_by(NodeEnrollment.created_at.desc()).limit(20)
        )
    )
    return templates(request).TemplateResponse(
        request, "servers.html", {"servers": items, "enrollments": enrollments}
    )


@router.post("/servers/enroll", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def server_enroll(
    request: Request,
    server_name: str = Form(...),
    expected_country_code: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    enrollment = NodeEnrollment(
        node_token=secrets.token_urlsafe(32),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        server_name=server_name,
        expected_country_code=expected_country_code.upper(),
        status="pending",
    )
    session.add(enrollment)
    await session.commit()
    items = list(await session.scalars(select(VpnServer).order_by(VpnServer.priority, VpnServer.name)))
    enrollments = list(
        await session.scalars(
            select(NodeEnrollment).order_by(NodeEnrollment.created_at.desc()).limit(20)
        )
    )
    command = (
        f"curl -sSL {get_settings().panel_public_url.rstrip('/')}/node/install.sh "
        f"| bash -s -- {enrollment.node_token}"
    )
    return templates(request).TemplateResponse(
        request,
        "servers.html",
        {"servers": items, "enrollments": enrollments, "enrollment_command": command},
    )


@router.post("/servers", dependencies=[Depends(require_admin)])
async def server_create(
    request: Request,
    name: str = Form(...),
    country_code: str = Form(""),
    country_name: str = Form(""),
    city: str = Form(""),
    host: str = Form(""),
    public_host: str = Form(...),
    public_port: int = Form(8443),
    reality_public_key: str = Form(...),
    reality_short_id: str = Form(...),
    reality_target: str = Form("www.microsoft.com:443"),
    reality_server_name: str = Form("www.microsoft.com"),
    fingerprint: str = Form("chrome"),
    flow: str = Form("xtls-rprx-vision"),
    transport: str = Form("xhttp"),
    xhttp_path: str = Form("/"),
    xhttp_mode: str = Form("auto"),
    management_mode: str = Form("manual"),
    xray_config_path: str = Form(""),
    ssh_host: str = Form(""),
    ssh_port: int = Form(22),
    ssh_user: str = Form(""),
    ssh_key_path: str = Form(""),
    remote_xray_config_path: str = Form(""),
    remote_compose_dir: str = Form(""),
    remote_container_name: str = Form(""),
    priority: int = Form(100),
    max_devices: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    item = VpnServer(
            name=name,
            country_code=country_code.upper(),
            country_name=country_name,
            city=city,
            host=host,
            public_host=public_host,
            public_port=public_port,
            reality_public_key=reality_public_key,
            reality_short_id=reality_short_id,
            reality_target=reality_target,
            reality_server_name=reality_server_name,
            fingerprint=fingerprint,
            flow="" if transport == "xhttp" else flow,
            transport=transport,
            xhttp_path=xhttp_path or "/",
            xhttp_mode=xhttp_mode or "auto",
            management_mode=management_mode,
            xray_config_path=xray_config_path or None,
            ssh_host=ssh_host or None,
            ssh_port=ssh_port,
            ssh_user=ssh_user or None,
            ssh_key_path=ssh_key_path or None,
            remote_xray_config_path=remote_xray_config_path or None,
            remote_compose_dir=remote_compose_dir or None,
            remote_container_name=remote_container_name or None,
            priority=priority,
            max_devices=int(max_devices) if max_devices else None,
        )
    session.add(item)
    await session.flush()
    try:
        await node_registry(request).apply_server(session, item)
    except Exception as error:
        await session.rollback()
        items = list(
            await session.scalars(select(VpnServer).order_by(VpnServer.priority, VpnServer.name))
        )
        return templates(request).TemplateResponse(
            request,
            "servers.html",
            {"servers": items, "config_error": str(error)},
            status_code=400,
        )
    await session.commit()
    return RedirectResponse("/admin/servers", status_code=303)


@router.get(
    "/servers/{server_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def server_detail(
    request: Request, server_id: str, session: AsyncSession = Depends(get_session)
):
    server = await session.get(VpnServer, server_id)
    if server is None:
        raise HTTPException(404)
    await update_server_device_count(session, server)
    profiles = list(
        await session.scalars(
            select(DeviceServerProfile)
            .where(
                DeviceServerProfile.server_id == server_id,
                DeviceServerProfile.is_active.is_(True),
            )
            .order_by(DeviceServerProfile.created_at.desc())
            .limit(200)
        )
    )
    return templates(request).TemplateResponse(
        request, "server.html", {"server": server, "profiles": profiles}
    )


@router.post("/servers/{server_id}", dependencies=[Depends(require_admin)])
async def server_update(
    request: Request,
    server_id: str,
    name: str = Form(...),
    country_code: str = Form(""),
    country_name: str = Form(""),
    city: str = Form(""),
    host: str = Form(""),
    public_host: str = Form(...),
    public_port: int = Form(8443),
    reality_public_key: str = Form(...),
    reality_short_id: str = Form(...),
    reality_target: str = Form("www.microsoft.com:443"),
    reality_server_name: str = Form("www.microsoft.com"),
    fingerprint: str = Form("chrome"),
    flow: str = Form("xtls-rprx-vision"),
    transport: str = Form("xhttp"),
    xhttp_path: str = Form("/"),
    xhttp_mode: str = Form("auto"),
    management_mode: str = Form("manual"),
    xray_config_path: str = Form(""),
    ssh_host: str = Form(""),
    ssh_port: int = Form(22),
    ssh_user: str = Form(""),
    ssh_key_path: str = Form(""),
    remote_xray_config_path: str = Form(""),
    remote_compose_dir: str = Form(""),
    remote_container_name: str = Form(""),
    priority: int = Form(100),
    max_devices: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    server = await session.get(VpnServer, server_id)
    if server is None:
        raise HTTPException(404)
    for field, value in {
        "name": name,
        "country_code": country_code.upper(),
        "country_name": country_name,
        "city": city,
        "host": host,
        "public_host": public_host,
        "public_port": public_port,
        "reality_public_key": reality_public_key,
        "reality_short_id": reality_short_id,
        "reality_target": reality_target,
        "reality_server_name": reality_server_name,
        "fingerprint": fingerprint,
        "flow": "" if transport == "xhttp" else flow,
        "transport": transport,
        "xhttp_path": xhttp_path or "/",
        "xhttp_mode": xhttp_mode or "auto",
        "management_mode": management_mode,
        "xray_config_path": xray_config_path or None,
        "ssh_host": ssh_host or None,
        "ssh_port": ssh_port,
        "ssh_user": ssh_user or None,
        "ssh_key_path": ssh_key_path or None,
        "remote_xray_config_path": remote_xray_config_path or None,
        "remote_compose_dir": remote_compose_dir or None,
        "remote_container_name": remote_container_name or None,
        "priority": priority,
        "max_devices": int(max_devices) if max_devices else None,
    }.items():
        setattr(server, field, value)
    try:
        await node_registry(request).apply_server(session, server)
    except Exception as error:
        await session.rollback()
        server = await session.get(VpnServer, server_id)
        profiles = list(
            await session.scalars(
                select(DeviceServerProfile)
                .where(
                    DeviceServerProfile.server_id == server_id,
                    DeviceServerProfile.is_active.is_(True),
                )
                .order_by(DeviceServerProfile.created_at.desc())
                .limit(200)
            )
        )
        return templates(request).TemplateResponse(
            request,
            "server.html",
            {"server": server, "profiles": profiles, "config_error": str(error)},
            status_code=400,
        )
    await session.commit()
    return RedirectResponse(f"/admin/servers/{server_id}", status_code=303)


@router.post("/servers/{server_id}/toggle", dependencies=[Depends(require_admin)])
async def server_toggle(server_id: str, session: AsyncSession = Depends(get_session)):
    server = await session.get(VpnServer, server_id)
    if server is None:
        raise HTTPException(404)
    server.is_active = not server.is_active
    await session.commit()
    return RedirectResponse(f"/admin/servers/{server_id}", status_code=303)


@router.post("/servers/{server_id}/default", dependencies=[Depends(require_admin)])
async def server_default(server_id: str, session: AsyncSession = Depends(get_session)):
    servers = list(await session.scalars(select(VpnServer)))
    found = False
    for server in servers:
        server.is_default = server.id == server_id
        found = found or server.is_default
    if not found:
        raise HTTPException(404)
    await session.commit()
    return RedirectResponse(f"/admin/servers/{server_id}", status_code=303)


@router.post("/servers/{server_id}/health", dependencies=[Depends(require_admin)])
async def server_health(
    request: Request, server_id: str, session: AsyncSession = Depends(get_session)
):
    server = await session.get(VpnServer, server_id)
    if server is None:
        raise HTTPException(404)
    await check_server_health(session, server, node_registry(request))
    return RedirectResponse(f"/admin/servers/{server_id}", status_code=303)


@router.post("/servers/{server_id}/delete", dependencies=[Depends(require_admin)])
async def server_delete(server_id: str, session: AsyncSession = Depends(get_session)):
    server = await session.get(VpnServer, server_id)
    if server is None:
        raise HTTPException(404)
    active_profile = await session.scalar(
        select(DeviceServerProfile.id).where(
            DeviceServerProfile.server_id == server_id,
            DeviceServerProfile.is_active.is_(True),
        )
    )
    if active_profile:
        raise HTTPException(409, "Server has active devices")
    profiles = list(
        await session.scalars(
            select(DeviceServerProfile).where(DeviceServerProfile.server_id == server_id)
        )
    )
    for profile in profiles:
        await session.delete(profile)
    await session.delete(server)
    await session.commit()
    return RedirectResponse("/admin/servers", status_code=303)


@router.get("/clients", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def clients(request: Request, session: AsyncSession = Depends(get_session)):
    items = list(
        await session.scalars(select(VpnClient).order_by(VpnClient.platform, VpnClient.sort_order))
    )
    return templates(request).TemplateResponse(request, "clients.html", {"clients": items})


@router.post("/clients", dependencies=[Depends(require_admin)])
async def client_create(
    platform: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    download_url: str = Form(...),
    instruction_text: str = Form(...),
    sort_order: int = Form(100),
    session: AsyncSession = Depends(get_session),
):
    session.add(
        VpnClient(
            platform=platform,
            name=name,
            description=description,
            download_url=download_url,
            instruction_text=instruction_text,
            sort_order=sort_order,
        )
    )
    await session.commit()
    return RedirectResponse("/admin/clients", status_code=303)


@router.post("/clients/{client_id}", dependencies=[Depends(require_admin)])
async def client_update(
    client_id: str,
    platform: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    download_url: str = Form(...),
    instruction_text: str = Form(...),
    sort_order: int = Form(100),
    session: AsyncSession = Depends(get_session),
):
    client = await session.get(VpnClient, client_id)
    if client is None:
        raise HTTPException(404)
    client.platform = platform
    client.name = name
    client.description = description
    client.download_url = download_url
    client.instruction_text = instruction_text
    client.sort_order = sort_order
    await session.commit()
    return RedirectResponse("/admin/clients", status_code=303)


@router.post("/clients/{client_id}/toggle", dependencies=[Depends(require_admin)])
async def client_toggle(client_id: str, session: AsyncSession = Depends(get_session)):
    client = await session.get(VpnClient, client_id)
    if client is None:
        raise HTTPException(404)
    client.is_active = not client.is_active
    await session.commit()
    return RedirectResponse("/admin/clients", status_code=303)


@router.get("/broadcasts", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def broadcasts(request: Request, session: AsyncSession = Depends(get_session)):
    items = list(await session.scalars(select(Broadcast).order_by(Broadcast.created_at.desc())))
    return templates(request).TemplateResponse(request, "broadcasts.html", {"broadcasts": items})


@router.post("/broadcasts", dependencies=[Depends(require_admin)])
async def broadcast_create(
    title: str = Form(...),
    text: str = Form(...),
    image_file_id_or_url: str = Form(""),
    target_type: str = Form(...),
    target_user_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    item = Broadcast(
        title=title,
        text=text,
        image_file_id_or_url=image_file_id_or_url or None,
        target_type=target_type,
        target_user_id=target_user_id or None,
        status="draft",
    )
    session.add(item)
    await session.commit()
    return RedirectResponse(f"/admin/broadcasts/{item.id}", status_code=303)


@router.get(
    "/broadcasts/{broadcast_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def broadcast_detail(
    request: Request, broadcast_id: str, session: AsyncSession = Depends(get_session)
):
    item = await session.get(Broadcast, broadcast_id)
    if item is None:
        raise HTTPException(404)
    counts = dict(
        (
            await session.execute(
                select(BroadcastRecipient.status, func.count())
                .where(BroadcastRecipient.broadcast_id == broadcast_id)
                .group_by(BroadcastRecipient.status)
            )
        ).all()
    )
    return templates(request).TemplateResponse(
        request, "broadcast.html", {"broadcast": item, "counts": counts}
    )


@router.post("/broadcasts/{broadcast_id}/send", dependencies=[Depends(require_admin)])
async def broadcast_send(broadcast_id: str, session: AsyncSession = Depends(get_session)):
    item = await session.get(Broadcast, broadcast_id)
    if item is None:
        raise HTTPException(404)
    if item.status == "draft":
        await prepare_broadcast(session, item)
    return RedirectResponse(f"/admin/broadcasts/{broadcast_id}", status_code=303)
