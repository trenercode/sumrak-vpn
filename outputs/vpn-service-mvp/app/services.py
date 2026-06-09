from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import Device, User
from app.vpn import PeerProfile, VpnBackend, new_client_email


def now() -> datetime:
    return datetime.now(UTC)


def has_access(user: User) -> bool:
    current = now()
    return not user.is_blocked and (
        (user.trial_ends_at is not None and user.trial_ends_at > current)
        or (user.subscription_ends_at is not None and user.subscription_ends_at > current)
    )


def access_until(user: User) -> datetime | None:
    values = [value for value in (user.trial_ends_at, user.subscription_ends_at) if value]
    return max(values) if values else None


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, username: str | None, full_name: str
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, username=username, full_name=full_name)
        session.add(user)
    else:
        user.username = username
        user.full_name = full_name
    await session.commit()
    await session.refresh(user)
    return user


async def start_trial(session: AsyncSession, user: User, settings: Settings) -> None:
    if user.trial_ends_at is None:
        user.trial_ends_at = now() + timedelta(days=settings.trial_days)
        await session.commit()


async def grant_subscription(session: AsyncSession, user: User, days: int) -> None:
    base = max(now(), user.subscription_ends_at or now())
    user.subscription_ends_at = base + timedelta(days=days)
    await session.commit()


async def create_device(
    session: AsyncSession,
    user: User,
    name: str,
    settings: Settings,
    vpn: VpnBackend,
) -> tuple[Device, PeerProfile]:
    await start_trial(session, user, settings)
    await session.refresh(user)
    if not has_access(user):
        raise ValueError("Подписка не активна")

    active_devices = list(
        await session.scalars(
            select(Device).where(Device.user_id == user.id, Device.is_revoked.is_(False))
        )
    )
    if len(active_devices) >= settings.max_devices:
        raise ValueError(f"Достигнут лимит: {settings.max_devices} устройств")

    client_email = new_client_email()
    profile = await vpn.create_peer(client_email, name)
    device = Device(
        user_id=user.id,
        name=name[:80],
        credential=profile.credential,
        client_email=profile.client_email,
    )
    session.add(device)
    try:
        await session.commit()
    except Exception:
        await vpn.revoke_peer(profile.client_email)
        raise
    await session.refresh(device)
    return device, profile


async def revoke_device(session: AsyncSession, device: Device, vpn: VpnBackend) -> None:
    if device.is_revoked:
        return
    await vpn.revoke_peer(device.client_email)
    device.is_revoked = True
    device.revoked_at = now()
    await session.commit()


async def reconcile_vpn(session: AsyncSession, vpn: VpnBackend) -> None:
    users = list(
        await session.scalars(select(User).options(selectinload(User.devices)))
    )
    for user in users:
        for device in user.devices:
            if device.is_revoked:
                continue
            if has_access(user):
                await vpn.activate_peer(device.credential, device.client_email)
            else:
                await vpn.revoke_peer(device.client_email)
                device.is_revoked = True
                device.revoked_at = now()
    stats = await vpn.peer_stats()
    for device in await session.scalars(select(Device).where(Device.is_revoked.is_(False))):
        peer = stats.get(device.client_email)
        if peer:
            if peer.transfer_rx != device.transfer_rx or peer.transfer_tx != device.transfer_tx:
                device.last_activity_at = now()
            device.transfer_rx = peer.transfer_rx
            device.transfer_tx = peer.transfer_tx
    await session.commit()


async def load_user_with_devices(session: AsyncSession, user_id: str) -> User | None:
    return await session.scalar(
        select(User).options(selectinload(User.devices)).where(User.id == user_id)
    )
