from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base, Broadcast, BroadcastRecipient, ReferralReward
from app.services import (
    create_device,
    get_or_create_user,
    log_notification,
    notification_candidates,
    prepare_broadcast,
    record_successful_payment,
    revoke_device,
)
from app.vpn import MockVpnBackend


async def database():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_new_user_referral_and_first_payment_reward():
    engine, sessions = await database()
    async with sessions() as session:
        referrer = await get_or_create_user(session, 1, "referrer", "Referrer")
        invited = await get_or_create_user(
            session, 2, "invited", "Invited", referral_code=referrer.referral_code
        )
        assert invited.referred_by_user_id == referrer.id

        rewarded_user = await record_successful_payment(session, invited, 30)
        await session.refresh(referrer)
        assert rewarded_user.id == referrer.id
        assert invited.first_paid_at is not None
        assert invited.referral_bonus_awarded_at is not None
        assert referrer.subscription_ends_at is not None
        assert await session.scalar(select(ReferralReward)) is not None

        assert await record_successful_payment(session, invited, 30) is None
    await engine.dispose()


async def test_device_creation_and_removal_preserves_vpn_contract():
    engine, sessions = await database()
    settings = Settings(
        trial_days=3,
        xray_public_host="vpn.example.com",
        xray_reality_public_key="key",
        xray_reality_short_id="0123456789abcdef",
    )
    vpn = MockVpnBackend(settings)
    async with sessions() as session:
        user = await get_or_create_user(session, 3, "device_user", "Device User")
        device, profile = await create_device(session, user, "ios", settings, vpn)
        assert device.platform == "ios"
        assert device.name == "iPhone 1"
        assert profile.uri.startswith("vless://")
        await revoke_device(session, device, vpn)
        assert device.is_revoked
    await engine.dispose()


async def test_notification_is_not_returned_after_logging():
    engine, sessions = await database()
    async with sessions() as session:
        user = await get_or_create_user(session, 4, "notify", "Notify")
        from app.services import now

        user.trial_ends_at = now() + timedelta(hours=12)
        await session.commit()
        candidates = await notification_candidates(session)
        assert len(candidates) == 1
        assert candidates[0].key.startswith("trial_1d:")
        await log_notification(session, candidates[0])
        assert await notification_candidates(session) == []
    await engine.dispose()


async def test_broadcast_prepares_active_recipients():
    engine, sessions = await database()
    async with sessions() as session:
        active = await get_or_create_user(session, 5, "active", "Active")
        inactive = await get_or_create_user(session, 6, "inactive", "Inactive")
        from app.services import now

        active.subscription_ends_at = now() + timedelta(days=5)
        item = Broadcast(title="Test", text="Message", target_type="active", status="draft")
        session.add(item)
        await session.commit()
        assert await prepare_broadcast(session, item) == 1
        recipient = await session.scalar(select(BroadcastRecipient))
        assert recipient.user_id == active.id
        assert recipient.user_id != inactive.id
    await engine.dispose()
