from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import (
    Broadcast,
    BroadcastRecipient,
    Device,
    DeviceServerProfile,
    NotificationLog,
    ReferralReward,
    User,
    VpnClient,
    VpnServer,
)
from app.nodes import (
    NodeManagerRegistry,
    active_servers,
    create_server_profile,
    ensure_default_server,
    render_server_uri,
)
from app.vpn import new_client_email

PLATFORM_LABELS = {
    "ios": "iPhone",
    "android": "Android",
    "windows": "Windows",
    "macos": "Mac",
    "android_tv": "Android TV",
    "other": "Устройство",
}


@dataclass(frozen=True)
class Notification:
    user_id: str
    telegram_id: int
    key: str
    text: str
    metadata: dict


def now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def has_access(user: User) -> bool:
    current = now()
    trial_ends_at = aware(user.trial_ends_at)
    subscription_ends_at = aware(user.subscription_ends_at)
    return not user.is_blocked and (
        (trial_ends_at is not None and trial_ends_at > current)
        or (subscription_ends_at is not None and subscription_ends_at > current)
    )


def access_until(user: User) -> datetime | None:
    values = [
        aware(value) for value in (user.trial_ends_at, user.subscription_ends_at) if value
    ]
    return max(values) if values else None


def subscription_status(user: User) -> str:
    current = now()
    subscription_ends_at = aware(user.subscription_ends_at)
    trial_ends_at = aware(user.trial_ends_at)
    if subscription_ends_at and subscription_ends_at > current:
        return "active"
    if trial_ends_at and trial_ends_at > current:
        return "trial"
    return "expired"


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    full_name: str,
    referral_code: str | None = None,
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, username=username, full_name=full_name)
        if referral_code:
            referrer = await session.scalar(
                select(User).where(User.referral_code == referral_code)
            )
            if referrer and referrer.telegram_id != telegram_id:
                user.referred_by_user_id = referrer.id
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
    base = max(now(), aware(user.subscription_ends_at) or now())
    user.subscription_ends_at = base + timedelta(days=days)
    await session.commit()


async def record_successful_payment(
    session: AsyncSession, user: User, subscription_days: int
) -> User | None:
    first_payment = user.first_paid_at is None
    await grant_subscription(session, user, subscription_days)
    if not first_payment:
        return None
    user.first_paid_at = now()
    if user.referred_by_user_id and user.first_payment_discount_used_at is None:
        user.first_payment_discount_used_at = now()
    if not user.referred_by_user_id or user.referral_bonus_awarded_at:
        await session.commit()
        return None

    referrer = await session.get(User, user.referred_by_user_id)
    if referrer is None:
        await session.commit()
        return None
    await grant_subscription(session, referrer, 15)
    user.referral_bonus_awarded_at = now()
    session.add(
        ReferralReward(
            referrer_user_id=referrer.id,
            referred_user_id=user.id,
            days_awarded=15,
        )
    )
    await session.commit()
    return referrer


async def create_device(
    session: AsyncSession,
    user: User,
    platform: str,
    settings: Settings,
    nodes: NodeManagerRegistry,
) -> tuple[Device, list[DeviceServerProfile]]:
    await start_trial(session, user, settings)
    await session.refresh(user)
    if not has_access(user):
        raise ValueError("inactive_subscription")

    active_devices = list(
        await session.scalars(
            select(Device).where(Device.user_id == user.id, Device.is_revoked.is_(False))
        )
    )
    if len(active_devices) >= settings.max_devices:
        raise ValueError("device_limit")

    platform_count = sum(1 for device in active_devices if device.platform == platform)
    name = f"{PLATFORM_LABELS.get(platform, 'Устройство')} {platform_count + 1}"
    client_email = new_client_email()
    credential = str(uuid.uuid4())
    device = Device(
        user_id=user.id,
        name=name,
        platform=platform,
        credential=credential,
        client_email=client_email,
    )
    session.add(device)
    await session.flush()
    profiles: list[DeviceServerProfile] = []
    try:
        servers = await active_servers(session, settings)
        if not servers:
            raise ValueError("no_available_servers")
        for server in servers:
            profile = await create_server_profile(
                session,
                device.id,
                server,
                nodes,
                credential=credential if server.is_default else None,
                client_email=client_email if server.is_default else None,
            )
            profiles.append(profile)
        await session.commit()
    except Exception:
        for profile in profiles:
            server = await session.get(VpnServer, profile.server_id)
            if server:
                await nodes.revoke(server, profile.client_email)
        await session.rollback()
        raise
    await session.refresh(device)
    return device, profiles


async def ensure_device_profiles(
    session: AsyncSession, device: Device, settings: Settings, nodes: NodeManagerRegistry
) -> list[DeviceServerProfile]:
    profiles = list(
        await session.scalars(
            select(DeviceServerProfile).where(DeviceServerProfile.device_id == device.id)
        )
    )
    existing_server_ids = {profile.server_id for profile in profiles}
    default_server = await ensure_default_server(session, settings)
    for server in await active_servers(session, settings):
        if server.id in existing_server_ids:
            continue
        profile = await create_server_profile(
            session,
            device.id,
            server,
            nodes,
            credential=device.credential if server.id == default_server.id else None,
            client_email=device.client_email if server.id == default_server.id else None,
        )
        profiles.append(profile)
    await session.commit()
    return profiles


async def revoke_device(
    session: AsyncSession, device: Device, settings: Settings, nodes: NodeManagerRegistry
) -> None:
    if device.is_revoked:
        return
    profiles = await ensure_device_profiles(session, device, settings, nodes)
    for profile in profiles:
        if not profile.is_active:
            continue
        server = await session.get(VpnServer, profile.server_id)
        if server:
            await nodes.revoke(server, profile.client_email)
        profile.is_active = False
        profile.revoked_at = now()
    device.is_revoked = True
    device.revoked_at = now()
    await session.commit()


async def reconcile_vpn(
    session: AsyncSession, settings: Settings, nodes: NodeManagerRegistry
) -> None:
    users = list(await session.scalars(select(User).options(selectinload(User.devices))))
    for user in users:
        for device in user.devices:
            if device.is_revoked:
                continue
            profiles = await ensure_device_profiles(session, device, settings, nodes)
            if has_access(user):
                for profile in profiles:
                    server = await session.get(VpnServer, profile.server_id)
                    if server and server.is_active and profile.is_active:
                        await nodes.activate(server, profile.credential, profile.client_email)
            else:
                for profile in profiles:
                    server = await session.get(VpnServer, profile.server_id)
                    if server and profile.is_active:
                        await nodes.revoke(server, profile.client_email)
                    profile.is_active = False
                    profile.revoked_at = now()
                device.is_revoked = True
                device.revoked_at = now()
    servers = list(await session.scalars(select(VpnServer).where(VpnServer.is_active.is_(True))))
    for server in servers:
        try:
            stats = await nodes.stats(server)
        except Exception:
            continue
        profiles = list(
            await session.scalars(
                select(DeviceServerProfile).where(
                    DeviceServerProfile.server_id == server.id,
                    DeviceServerProfile.is_active.is_(True),
                )
            )
        )
        for profile in profiles:
            peer = stats.get(profile.client_email)
            if peer:
                profile.last_activity_at = peer.last_activity_at or profile.last_activity_at
                profile.transfer_rx = peer.transfer_rx
                profile.transfer_tx = peer.transfer_tx
    active_devices = list(
        await session.scalars(select(Device).where(Device.is_revoked.is_(False)))
    )
    for device in active_devices:
        profiles = list(
            await session.scalars(
                select(DeviceServerProfile).where(DeviceServerProfile.device_id == device.id)
            )
        )
        device.transfer_rx = sum(profile.transfer_rx for profile in profiles)
        device.transfer_tx = sum(profile.transfer_tx for profile in profiles)
        activities = [profile.last_activity_at for profile in profiles if profile.last_activity_at]
        device.last_activity_at = max(activities) if activities else device.last_activity_at
    await session.commit()


async def subscription_uris(
    session: AsyncSession, device: Device, settings: Settings, nodes: NodeManagerRegistry
) -> list[str]:
    if device.is_revoked:
        return []
    user = await session.get(User, device.user_id)
    if user is None or not has_access(user):
        return []
    await ensure_device_profiles(session, device, settings, nodes)
    profiles = list(
        (
            await session.execute(
                select(DeviceServerProfile, VpnServer)
                .join(VpnServer)
                .where(
                    DeviceServerProfile.device_id == device.id,
                    DeviceServerProfile.is_active.is_(True),
                    VpnServer.is_active.is_(True),
                )
                .order_by(VpnServer.priority, VpnServer.name)
            )
        ).all()
    )
    return [render_server_uri(server, profile.credential) for profile, server in profiles]


async def referral_stats(session: AsyncSession, user: User) -> dict:
    referrals = list(
        await session.scalars(
            select(User)
            .where(User.referred_by_user_id == user.id)
            .order_by(User.created_at.desc())
        )
    )
    paid = sum(item.first_paid_at is not None for item in referrals)
    rewarded = sum(item.referral_bonus_awarded_at is not None for item in referrals)
    return {
        "invited": len(referrals),
        "paid": paid,
        "days": rewarded * 15,
        "recent": referrals[:20],
    }


async def active_vpn_clients(session: AsyncSession, platform: str) -> list[VpnClient]:
    return list(
        await session.scalars(
            select(VpnClient)
            .where(VpnClient.platform == platform, VpnClient.is_active.is_(True))
            .order_by(VpnClient.sort_order, VpnClient.name)
        )
    )


async def notification_candidates(session: AsyncSession) -> list[Notification]:
    current = now()
    users = list(await session.scalars(select(User).options(selectinload(User.devices))))
    sent_keys = set((await session.execute(select(NotificationLog.user_id, NotificationLog.type))).all())
    rewards = list(await session.scalars(select(ReferralReward)))
    rewards_by_referrer: dict[str, list[ReferralReward]] = {}
    for reward in rewards:
        rewards_by_referrer.setdefault(reward.referrer_user_id, []).append(reward)
    result: list[Notification] = []

    def add(user: User, key: str, text: str, metadata: dict):
        if (user.id, key) not in sent_keys:
            result.append(Notification(user.id, user.telegram_id, key, text, metadata))

    for user in users:
        if user.is_blocked:
            continue
        if user.subscription_ends_at:
            subscription_ends_at = aware(user.subscription_ends_at)
            remaining = subscription_ends_at - current
            stamp = subscription_ends_at.isoformat()
            if timedelta(days=1) < remaining <= timedelta(days=3):
                add(user, f"subscription_3d:{stamp}", "Ваша подписка закончится через 3 дня.", {})
            if timedelta(0) < remaining <= timedelta(days=1):
                add(user, f"subscription_1d:{stamp}", "Ваша подписка закончится завтра.", {})
            if remaining <= timedelta(0):
                add(
                    user,
                    f"subscription_expired:{stamp}",
                    "Подписка завершена. Для продолжения работы VPN продлите доступ.",
                    {},
                )
        elif user.trial_ends_at:
            trial_ends_at = aware(user.trial_ends_at)
            remaining = trial_ends_at - current
            stamp = trial_ends_at.isoformat()
            if timedelta(0) < remaining <= timedelta(days=1):
                add(
                    user,
                    f"trial_1d:{stamp}",
                    "Ваш пробный период закончится завтра. Чтобы VPN не отключился, "
                    "продлите подписку заранее.",
                    {},
                )
            if remaining <= timedelta(0):
                add(
                    user,
                    f"trial_expired:{stamp}",
                    "Пробный период завершён. Для продолжения работы VPN оплатите подписку.",
                    {},
                )
        if not has_access(user) and any(device.revoked_at for device in user.devices):
            until = access_until(user)
            stamp = until.isoformat() if until else "none"
            add(
                user,
                f"devices_disabled:{stamp}",
                "Подписка закончилась, VPN-профили временно отключены.",
                {},
            )
        for reward in rewards_by_referrer.get(user.id, []):
            add(
                user,
                f"referral_reward:{reward.id}",
                "Ваш приглашённый оплатил подписку. "
                "Мы начислили вам +15 дней VPN 🎁",
                {"reward_id": reward.id},
            )
    return result


async def log_notification(session: AsyncSession, notification: Notification) -> None:
    session.add(
        NotificationLog(
            user_id=notification.user_id,
            type=notification.key,
            metadata_json=notification.metadata,
        )
    )
    await session.commit()


async def prepare_broadcast(session: AsyncSession, broadcast: Broadcast) -> int:
    current = now()
    query = select(User)
    if broadcast.target_type == "active":
        query = query.where(
            User.is_blocked.is_(False),
            or_(User.trial_ends_at > current, User.subscription_ends_at > current),
        )
    elif broadcast.target_type == "expiring":
        query = query.where(
            User.subscription_ends_at > current,
            User.subscription_ends_at <= current + timedelta(days=3),
        )
    elif broadcast.target_type == "single":
        query = query.where(User.id == broadcast.target_user_id)
    users = list(await session.scalars(query))
    session.add_all(
        [
            BroadcastRecipient(
                broadcast_id=broadcast.id, user_id=user.id, status="pending"
            )
            for user in users
        ]
    )
    broadcast.status = "sending"
    await session.commit()
    return len(users)


async def load_user_with_devices(session: AsyncSession, user_id: str) -> User | None:
    return await session.scalar(
        select(User)
        .options(
            selectinload(User.devices)
            .selectinload(Device.server_profiles)
            .selectinload(DeviceServerProfile.server)
        )
        .where(User.id == user_id)
    )
