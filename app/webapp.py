import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Device, DeviceServerProfile, User, VpnClient
from app.nodes import server_label
from app.services import (
    access_until,
    create_device,
    get_or_create_user,
    referral_stats,
    revoke_device,
    subscription_status,
)

router = APIRouter()
api = APIRouter(prefix="/api/webapp", tags=["webapp"])

PLATFORMS = {
    "ios": "iPhone / iPad",
    "android": "Android",
    "windows": "Windows",
    "macos": "macOS",
    "android_tv": "Android TV",
    "other": "Другое",
}


class DeviceCreate(BaseModel):
    platform: str


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict:
    if not init_data or not bot_token:
        raise ValueError("missing_init_data")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("missing_hash")
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise ValueError("invalid_hash")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid_init_data") from error
    current_timestamp = int(datetime.now(UTC).timestamp())
    if abs(current_timestamp - auth_date) > max_age_seconds:
        raise ValueError("expired_init_data")
    return user


async def webapp_user(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    telegram_init_data: str = Header("", alias="X-Telegram-Init-Data"),
    dev_telegram_id: int | None = Header(None, alias="X-Dev-Telegram-Id"),
) -> User:
    if settings.webapp_dev_telegram_id and dev_telegram_id == settings.webapp_dev_telegram_id:
        return await get_or_create_user(session, dev_telegram_id, "local_dev", "Local Dev")
    try:
        telegram_user = validate_init_data(telegram_init_data, settings.bot_token)
    except ValueError as error:
        raise HTTPException(401, "Telegram authentication failed") from error
    return await get_or_create_user(
        session,
        int(telegram_user["id"]),
        telegram_user.get("username"),
        " ".join(
            part
            for part in [telegram_user.get("first_name"), telegram_user.get("last_name")]
            if part
        ),
    )


def subscription_url(settings: Settings, device: Device) -> str:
    return f"{settings.panel_public_url.rstrip('/')}/sub/{device.subscription_token}"


def serialize_device(settings: Settings, device: Device) -> dict:
    profiles = [
        {
            "server": server_label(profile.server),
            "active": profile.is_active and profile.server.is_active,
        }
        for profile in device.server_profiles
    ]
    return {
        "id": device.id,
        "name": device.name,
        "platform": device.platform,
        "platform_label": PLATFORMS.get(device.platform or "", "Другое"),
        "subscription_url": subscription_url(settings, device),
        "created_at": device.created_at,
        "servers": profiles,
    }


async def owned_device(session: AsyncSession, user: User, device_id: str) -> Device:
    device = await session.scalar(
        select(Device)
        .options(
            selectinload(Device.server_profiles).selectinload(DeviceServerProfile.server)
        )
        .where(Device.id == device_id, Device.user_id == user.id, Device.is_revoked.is_(False))
    )
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    return device


@router.get("/webapp", response_class=HTMLResponse)
async def webapp_page(request: Request):
    return request.app.state.templates.TemplateResponse(request, "webapp.html")


@api.get("/me")
async def me(
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    device_count = len(
        list(
            await session.scalars(
                select(Device.id).where(
                    Device.user_id == user.id, Device.is_revoked.is_(False)
                )
            )
        )
    )
    until = access_until(user)
    days_left = max(0, (until.date() - datetime.now(UTC).date()).days) if until else 0
    return {
        "name": user.full_name or user.username or "Пользователь",
        "username": user.username,
        "status": subscription_status(user),
        "access_until": until,
        "days_left": days_left,
        "devices_used": device_count,
        "device_limit": settings.max_devices,
        "has_devices": device_count > 0,
        "support_url": settings.support_telegram_url,
    }


@api.get("/devices")
async def devices(
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    items = list(
        await session.scalars(
            select(Device)
            .options(
                selectinload(Device.server_profiles).selectinload(DeviceServerProfile.server)
            )
            .where(Device.user_id == user.id, Device.is_revoked.is_(False))
            .order_by(Device.created_at)
        )
    )
    return [serialize_device(settings, item) for item in items]


@api.post("/devices", status_code=201)
async def add_device(
    payload: DeviceCreate,
    request: Request,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    if payload.platform not in PLATFORMS:
        raise HTTPException(422, "Неизвестная платформа")
    user = await session.get(User, user.id)
    try:
        device, _ = await create_device(
            session, user, payload.platform, settings, request.app.state.nodes
        )
    except ValueError as error:
        messages = {
            "inactive_subscription": "Подписка не активна. Продлите доступ.",
            "device_limit": "Достигнут лимит устройств.",
            "no_available_servers": "Сейчас нет доступных VPN-серверов.",
        }
        raise HTTPException(409, messages.get(str(error), "Не удалось создать устройство")) from error
    return serialize_device(settings, await owned_device(session, user, device.id))


@api.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: str,
    request: Request,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    device = await owned_device(session, user, device_id)
    await revoke_device(session, device, settings, request.app.state.nodes)


@api.get("/devices/{device_id}/subscription")
async def device_subscription(
    device_id: str,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    device = await owned_device(session, user, device_id)
    return {"subscription_url": subscription_url(settings, device)}


@api.get("/referral")
async def referral(
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    stats = await referral_stats(session, user)
    bot_username = settings.bot_username.strip("@")
    return {
        "link": f"https://t.me/{bot_username}?start=ref_{user.referral_code}",
        "invited": stats["invited"],
        "paid": stats["paid"],
        "days": stats["days"],
        "recent": [
            {
                "name": item.username or item.full_name or str(item.telegram_id),
                "created_at": item.created_at,
                "status": (
                    "Бонус начислен"
                    if item.referral_bonus_awarded_at
                    else ("Оплатил" if item.first_paid_at else "Зарегистрирован")
                ),
            }
            for item in stats["recent"]
        ],
    }


@api.get("/clients")
async def clients(
    platform: str | None = Query(None),
    _user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(VpnClient).where(VpnClient.is_active.is_(True))
    if platform:
        query = query.where(VpnClient.platform == platform)
    items = list(await session.scalars(query.order_by(VpnClient.sort_order, VpnClient.name)))
    return [
        {
            "id": item.id,
            "platform": item.platform,
            "platform_label": PLATFORMS.get(item.platform, item.platform),
            "name": item.name,
            "description": item.description,
            "download_url": item.download_url,
            "instruction": item.instruction_text,
        }
        for item in items
    ]


router.include_router(api)
