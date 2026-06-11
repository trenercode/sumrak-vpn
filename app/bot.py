import asyncio
import html
import logging
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, MenuButtonWebApp, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import (
    Base,
    BroadcastRecipient,
    Device,
    DeviceServerProfile,
    User,
    VpnClient,
    VpnServer,
)
from app.nodes import NodeManagerRegistry, check_server_health, server_label
from app.services import (
    access_until,
    active_vpn_clients,
    create_device,
    get_or_create_user,
    log_notification,
    notification_candidates,
    reconcile_vpn,
    referral_stats,
    revoke_device,
    subscription_status,
)

settings = get_settings()
nodes = NodeManagerRegistry(settings)
router = Router()

START_TEXT = """Добро пожаловать в Sumrak VPN 🌑

Быстрый и стабильный VPN для ваших устройств.

Что можно сделать:
🚀 получить VPN-профиль
📱 подключить до 10 устройств
💳 проверить подписку
🎁 пригласить друзей и получить бонус
📲 посмотреть инструкции по установке

Выберите действие ниже."""

PLATFORM_BUTTONS = {
    "ios": "iPhone / iPad",
    "android": "Android",
    "windows": "Windows",
    "macos": "macOS",
    "android_tv": "Android TV",
    "other": "Другое",
}


def add_support_button(keyboard: InlineKeyboardBuilder, text: str = "🆘 Техподдержка"):
    if settings.support_telegram_url:
        keyboard.button(text=text, url=settings.support_telegram_url)


def main_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🚀 Получить профиль", callback_data="device:create:start")
    keyboard.button(text="📱 Мои устройства", callback_data="device:list")
    keyboard.button(text="💳 Подписка", callback_data="subscription:status")
    keyboard.button(text="🎁 Реферальная программа", callback_data="referral:menu")
    keyboard.button(text="📲 Как подключить", callback_data="help:platforms")
    add_support_button(keyboard)
    keyboard.adjust(1)
    return keyboard.as_markup()


def support_keyboard():
    keyboard = InlineKeyboardBuilder()
    add_support_button(keyboard)
    keyboard.button(text="⬅️ В меню", callback_data="menu")
    keyboard.adjust(1)
    return keyboard.as_markup()


def platform_keyboard(prefix: str):
    keyboard = InlineKeyboardBuilder()
    for platform, label in PLATFORM_BUTTONS.items():
        keyboard.button(text=label, callback_data=f"{prefix}:{platform}")
    keyboard.button(text="⬅️ Назад", callback_data="menu")
    keyboard.adjust(2, 2, 2, 1)
    return keyboard.as_markup()


def profile_keyboard(device_id: str | None = None):
    keyboard = InlineKeyboardBuilder()
    if device_id:
        keyboard.button(
            text="📋 Получить ссылку подписки", callback_data=f"device:subscription:{device_id}"
        )
    keyboard.button(text="📱 Инструкция для iOS", callback_data="help:platform:ios")
    keyboard.button(text="🤖 Инструкция для Android", callback_data="help:platform:android")
    keyboard.button(
        text="💻 Инструкция для Windows/macOS", callback_data="help:platform:desktop"
    )
    add_support_button(keyboard)
    keyboard.button(text="⬅️ Назад", callback_data="device:list")
    keyboard.adjust(1)
    return keyboard.as_markup()


def referral_back_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="⬅️ Назад", callback_data="referral:menu")
    return keyboard.as_markup()


async def current_user(
    message_or_callback: Message | CallbackQuery, referral_code: str | None = None
) -> User:
    telegram_user = message_or_callback.from_user
    async with SessionLocal() as session:
        return await get_or_create_user(
            session,
            telegram_user.id,
            telegram_user.username,
            telegram_user.full_name,
            referral_code,
        )


@router.message(CommandStart())
async def start(message: Message, command: CommandObject):
    referral_code = None
    if command.args and command.args.startswith("ref_"):
        referral_code = command.args[4:]
    async with SessionLocal() as session:
        existed = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    user = await current_user(message, referral_code)
    if existed is None and user.referred_by_user_id:
        async with SessionLocal() as session:
            referrer = await session.get(User, user.referred_by_user_id)
            if referrer:
                try:
                    await message.bot.send_message(
                        referrer.telegram_id,
                        "По вашей ссылке зарегистрировался новый пользователь 🎉 "
                        "Бонус +15 дней будет начислен после его первой оплаты.",
                    )
                except Exception:
                    logging.exception("Could not notify referrer")
    await message.answer(START_TEXT, reply_markup=main_keyboard())


@router.message(Command("privacy"))
async def privacy(message: Message):
    await message.answer(
        "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-PO-RABOTE-S-PERSONALNYMI-DANNYMI-POLZOVATELEJ-06-10-3"
    )


@router.message(Command("terms"))
async def terms(message: Message):
    await message.answer(
        "https://telegra.ph/Polzovatelskoe-soglashenie-Publichnaya-oferta-06-10-5"
    )


@router.message(Command("menu"))
@router.callback_query(F.data == "menu")
async def menu(event: Message | CallbackQuery):
    if isinstance(event, CallbackQuery):
        await event.answer()
        message = event.message
    else:
        message = event
    await message.answer(START_TEXT, reply_markup=main_keyboard())


@router.callback_query(F.data == "subscription:status")
async def show_subscription(callback: CallbackQuery):
    user = await current_user(callback)
    async with SessionLocal() as session:
        used = len(
            list(
                await session.scalars(
                    select(Device).where(
                        Device.user_id == user.id, Device.is_revoked.is_(False)
                    )
                )
            )
        )
    status = subscription_status(user)
    until = access_until(user)
    days = max(0, (until.date() - datetime.now(UTC).date()).days) if until else 0
    until_text = f"{until:%d.%m.%Y %H:%M UTC}" if until else "—"
    text = f"💳 Подписка\n\nСтатус: {status}\nДействует до: {until_text}\n"
    text += f"Осталось дней: {days}\nУстройства: {used}/{settings.max_devices}"
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="💳 Продлить подписку", callback_data="subscription:renew")
    keyboard.button(text="🎁 Ввести промокод", callback_data="subscription:promo")
    add_support_button(keyboard, "🆘 Помощь")
    keyboard.button(text="⬅️ Назад", callback_data="menu")
    keyboard.adjust(1)
    await callback.answer()
    await callback.message.answer(text, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.in_({"subscription:renew", "subscription:promo"}))
async def subscription_planned(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Оплата и промокоды появятся в следующей версии. Сейчас доступ можно продлить "
        "через поддержку.",
        reply_markup=support_keyboard(),
    )


@router.callback_query(F.data == "device:create:start")
async def choose_device_platform(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "На какое устройство устанавливаем VPN?",
        reply_markup=platform_keyboard("device:create"),
    )


@router.callback_query(F.data.startswith("device:create:"))
async def device_create(callback: CallbackQuery):
    platform = callback.data.rsplit(":", 1)[-1]
    if platform not in PLATFORM_BUTTONS:
        return
    user = await current_user(callback)
    await callback.answer()
    async with SessionLocal() as session:
        user = await session.get(User, user.id)
        try:
            device, _profiles = await create_device(session, user, platform, settings, nodes)
        except ValueError as error:
            if str(error) == "inactive_subscription":
                text = "Подписка не активна. Чтобы получить VPN-профиль, продлите доступ."
            elif str(error) == "device_limit":
                text = (
                    "Вы уже подключили максимум устройств. Удалите старое устройство "
                    "в разделе «Мои устройства»."
                )
            elif str(error) == "no_available_servers":
                text = "Сейчас нет доступных VPN-серверов. Напишите в поддержку."
            else:
                text = "Что-то пошло не так. Если нужно срочно — напишите в поддержку."
            await callback.message.answer(text, reply_markup=support_keyboard())
            return
        except Exception:
            logging.exception("Device creation failed")
            await callback.message.answer(
                "Что-то пошло не так. Мы уже знаем о проблеме. "
                "Если нужно срочно — напишите в поддержку.",
                reply_markup=support_keyboard(),
            )
            return

    subscription_url = f"{settings.panel_public_url.rstrip('/')}/sub/{device.subscription_token}"
    text = (
        "Ваш VPN-профиль готов ✅\n\n"
        "Добавьте ссылку в приложение Happ/Streisand.\n"
        "Внутри приложения вы увидите доступные страны и сможете переключаться между ними.\n\n"
        f"<code>{html.escape(subscription_url)}</code>"
    )
    await callback.message.answer(
        text, parse_mode="HTML", reply_markup=profile_keyboard(device.id)
    )


@router.callback_query(F.data == "device:list")
async def device_list(callback: CallbackQuery):
    user = await current_user(callback)
    await callback.answer()
    async with SessionLocal() as session:
        devices = list(
            await session.scalars(
                select(Device)
                .options(
                    selectinload(Device.server_profiles).selectinload(
                        DeviceServerProfile.server
                    )
                )
                .where(Device.user_id == user.id, Device.is_revoked.is_(False))
                .order_by(Device.created_at)
            )
        )
    keyboard = InlineKeyboardBuilder()
    for device in devices:
        keyboard.button(
            text=f"🔄 Обновить подписку: {device.name}",
            callback_data=f"device:subscription:{device.id}",
        )
        keyboard.button(text=f"🗑 Удалить {device.name}", callback_data=f"device:revoke:{device.id}")
    keyboard.button(text="➕ Добавить устройство", callback_data="device:create:start")
    keyboard.button(text="📲 Как подключить VPN", callback_data="help:platforms")
    keyboard.button(text="⬅️ Назад", callback_data="menu")
    keyboard.adjust(1)
    text = "📱 Мои устройства\n\n" + (
        "\n\n".join(
            f"• {device.name}\n"
            + "\n".join(
                f"  {server_label(profile.server)} — "
                f"{'активен' if profile.is_active and profile.server.is_active else 'недоступен'}"
                for profile in device.server_profiles
            )
            for device in devices
        )
        if devices
        else "Активных устройств пока нет."
    )
    await callback.message.answer(text, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("device:copy:"))
@router.callback_query(F.data.startswith("device:subscription:"))
async def device_subscription(callback: CallbackQuery):
    device_id = callback.data.rsplit(":", 1)[-1]
    user = await current_user(callback)
    async with SessionLocal() as session:
        device = await session.get(Device, device_id)
        if not device or device.user_id != user.id or device.is_revoked:
            await callback.answer("Устройство не найдено", show_alert=True)
            return
    subscription_url = f"{settings.panel_public_url.rstrip('/')}/sub/{device.subscription_token}"
    await callback.answer()
    await callback.message.answer(
        f"Ссылка подписки для «{html.escape(device.name)}»:\n\n"
        f"<code>{html.escape(subscription_url)}</code>\n\n"
        "Добавьте её как подписку в VPN-клиент и обновляйте список серверов.",
        parse_mode="HTML",
        reply_markup=profile_keyboard(device.id),
    )


@router.callback_query(F.data.startswith("device:revoke:"))
async def device_revoke(callback: CallbackQuery):
    device_id = callback.data.rsplit(":", 1)[-1]
    user = await current_user(callback)
    async with SessionLocal() as session:
        device = await session.get(Device, device_id)
        if device is None or device.user_id != user.id:
            await callback.answer("Устройство не найдено", show_alert=True)
            return
        await revoke_device(session, device, settings, nodes)
    await callback.answer("Профиль отозван")
    await callback.message.answer("Устройство отключено.", reply_markup=main_keyboard())


@router.callback_query(F.data == "help:platforms")
async def help_platforms(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Выберите платформу:", reply_markup=platform_keyboard("help:platform")
    )


@router.callback_query(F.data.startswith("help:platform:"))
async def help_platform(callback: CallbackQuery):
    platform = callback.data.rsplit(":", 1)[-1]
    platforms = ["windows", "macos"] if platform == "desktop" else [platform]
    async with SessionLocal() as session:
        clients = []
        for item in platforms:
            clients.extend(await active_vpn_clients(session, item))
    await callback.answer()
    keyboard = InlineKeyboardBuilder()
    for client in clients:
        keyboard.button(text=f"⬇️ {client.name}", url=client.download_url)
        keyboard.button(text=f"📖 Инструкция: {client.name}", callback_data=f"client:help:{client.id}")
    add_support_button(keyboard)
    keyboard.button(text="⬅️ Назад", callback_data="help:platforms")
    keyboard.adjust(2)
    text = "Рекомендуемые VPN-клиенты:" if clients else "Для этой платформы инструкции пока не добавлены."
    await callback.message.answer(text, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("client:help:"))
async def client_help(callback: CallbackQuery):
    client_id = callback.data.rsplit(":", 1)[-1]
    async with SessionLocal() as session:
        client = await session.get(VpnClient, client_id)
    await callback.answer()
    if not client or not client.is_active:
        await callback.message.answer("Инструкция недоступна.", reply_markup=support_keyboard())
        return
    await callback.message.answer(
        f"📖 {client.name}\n\n{client.instruction_text}", reply_markup=support_keyboard()
    )


@router.callback_query(F.data == "referral:menu")
async def referral_menu(callback: CallbackQuery):
    await current_user(callback)
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔗 Моя ссылка", callback_data="referral:link")
    keyboard.button(text="📊 Статистика", callback_data="referral:stats")
    keyboard.button(text="ℹ️ Как это работает", callback_data="referral:how")
    keyboard.button(text="⬅️ Назад", callback_data="menu")
    keyboard.adjust(1)
    await callback.answer()
    await callback.message.answer(
        "🎁 Реферальная программа\n"
        "Приглашайте друзей и получайте +15 дней VPN за каждого, кто оплатит подписку.\n"
        "Ваш друг получит скидку 50% на первую оплату.",
        reply_markup=keyboard.as_markup(),
    )


@router.callback_query(F.data == "referral:link")
async def referral_link(callback: CallbackQuery):
    user = await current_user(callback)
    bot_username = settings.bot_username or (await callback.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{user.referral_code}"
    await callback.answer()
    await callback.message.answer(
        f"Ваша реферальная ссылка:\n\n<code>{link}</code>",
        parse_mode="HTML",
        reply_markup=referral_back_keyboard(),
    )


@router.callback_query(F.data == "referral:stats")
async def referral_statistics(callback: CallbackQuery):
    user = await current_user(callback)
    async with SessionLocal() as session:
        user = await session.get(User, user.id)
        stats = await referral_stats(session, user)
    lines = [
        "📊 Статистика рефералов",
        f"Приглашено: {stats['invited']}",
        f"Оплатили: {stats['paid']}",
        f"Начислено дней: {stats['days']}",
        "",
        "Последние приглашённые:",
    ]
    for item in stats["recent"][:10]:
        status = "бонус начислен" if item.referral_bonus_awarded_at else ("оплатил" if item.first_paid_at else "зарегистрирован")
        lines.append(f"{item.created_at:%d.%m.%Y} · @{item.username or item.telegram_id} · {status}")
    await callback.answer()
    await callback.message.answer("\n".join(lines), reply_markup=referral_back_keyboard())


@router.callback_query(F.data == "referral:how")
async def referral_how(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Друг регистрируется по вашей ссылке и получает скидку 50% на первую оплату. "
        "После его первой успешной оплаты вам автоматически начисляется +15 дней.",
        reply_markup=referral_back_keyboard(),
    )


async def notification_loop(bot: Bot):
    while True:
        try:
            async with SessionLocal() as session:
                notifications = await notification_candidates(session)
                for notification in notifications:
                    try:
                        await bot.send_message(
                            notification.telegram_id,
                            notification.text,
                            reply_markup=support_keyboard(),
                        )
                        await log_notification(session, notification)
                        await asyncio.sleep(settings.broadcast_delay_seconds)
                    except Exception:
                        logging.exception("Notification send failed")
        except Exception:
            logging.exception("Notification loop failed")
        await asyncio.sleep(300)


async def broadcast_loop(bot: Bot):
    while True:
        delay = 2.0
        try:
            async with SessionLocal() as session:
                recipient = await session.scalar(
                    select(BroadcastRecipient)
                    .options(selectinload(BroadcastRecipient.broadcast))
                    .where(BroadcastRecipient.status == "pending")
                    .order_by(BroadcastRecipient.id)
                    .limit(1)
                )
                if recipient:
                    delay = settings.broadcast_delay_seconds
                    user = await session.get(User, recipient.user_id)
                    broadcast = recipient.broadcast
                    try:
                        if broadcast.image_file_id_or_url:
                            await bot.send_photo(
                                user.telegram_id,
                                broadcast.image_file_id_or_url,
                                caption=broadcast.text,
                            )
                        else:
                            await bot.send_message(user.telegram_id, broadcast.text)
                        recipient.status = "sent"
                        recipient.sent_at = datetime.now(UTC)
                    except Exception as error:
                        recipient.status = "failed"
                        recipient.error = str(error)[:1000]
                    await session.commit()
                    remaining = await session.scalar(
                        select(BroadcastRecipient.id).where(
                            BroadcastRecipient.broadcast_id == broadcast.id,
                            BroadcastRecipient.status == "pending",
                        )
                    )
                    if remaining is None:
                        broadcast.status = "done"
                        broadcast.sent_at = datetime.now(UTC)
                        await session.commit()
        except Exception:
            logging.exception("Broadcast loop failed")
        await asyncio.sleep(delay)


async def reconcile_loop():
    while True:
        try:
            async with SessionLocal() as session:
                await reconcile_vpn(session, settings, nodes)
        except Exception:
            logging.exception("VPN reconciliation failed")
        await asyncio.sleep(60)


async def health_check_loop():
    while True:
        try:
            async with SessionLocal() as session:
                servers = list(
                    await session.scalars(select(VpnServer).where(VpnServer.is_active.is_(True)))
                )
                for server in servers:
                    await check_server_health(session, server, nodes)
        except Exception:
            logging.exception("VPN server health check failed")
        await asyncio.sleep(300)


async def main():
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    logging.basicConfig(level=logging.INFO)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    bot = Bot(settings.bot_token)
    await bot.delete_my_commands()
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Sumrak VPN",
            web_app=WebAppInfo(
                url=settings.webapp_url or f"{settings.panel_public_url.rstrip('/')}/webapp"
            ),
        )
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    tasks = [
        asyncio.create_task(reconcile_loop()),
        asyncio.create_task(health_check_loop()),
        asyncio.create_task(notification_loop(bot)),
        asyncio.create_task(broadcast_loop(bot)),
    ]
    try:
        await dispatcher.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
