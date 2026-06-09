import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import Base, Device, User
from app.services import (
    access_until,
    create_device,
    get_or_create_user,
    has_access,
    reconcile_vpn,
    revoke_device,
)
from app.vpn import build_vpn_backend

settings = get_settings()
vpn = build_vpn_backend(settings)
router = Router()


def main_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Получить профиль", callback_data="device:create")
    keyboard.button(text="Мои устройства", callback_data="device:list")
    keyboard.button(text="Статус подписки", callback_data="subscription:status")
    keyboard.adjust(1)
    return keyboard.as_markup()


async def current_user(message_or_callback: Message | CallbackQuery) -> User:
    telegram_user = message_or_callback.from_user
    async with SessionLocal() as session:
        return await get_or_create_user(
            session,
            telegram_user.id,
            telegram_user.username,
            telegram_user.full_name,
        )


@router.message(CommandStart())
async def start(message: Message):
    await current_user(message)
    await message.answer(
        "Добро пожаловать. Здесь можно получить VPN-профиль и управлять устройствами.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("menu"))
async def menu(message: Message):
    await message.answer("Управление VPN:", reply_markup=main_keyboard())


@router.callback_query(F.data == "subscription:status")
async def subscription_status(callback: CallbackQuery):
    user = await current_user(callback)
    until = access_until(user)
    if has_access(user) and until:
        text = f"Доступ активен до {until:%d.%m.%Y %H:%M} UTC."
    elif user.trial_ends_at is None:
        text = f"Пробный период на {settings.trial_days} дн. начнется при выдаче первого профиля."
    else:
        text = "Доступ не активен. Обратитесь к администратору."
    await callback.answer()
    await callback.message.answer(text, reply_markup=main_keyboard())


@router.callback_query(F.data == "device:create")
async def device_create(callback: CallbackQuery):
    user = await current_user(callback)
    await callback.answer()
    async with SessionLocal() as session:
        user = await session.get(User, user.id)
        count = len(
            list(
                await session.scalars(
                    select(Device).where(Device.user_id == user.id, Device.is_revoked.is_(False))
                )
            )
        )
        try:
            device, profile = await create_device(
                session, user, f"Устройство {count + 1}", settings, vpn
            )
        except ValueError as error:
            await callback.message.answer(str(error), reply_markup=main_keyboard())
            return

    document = BufferedInputFile(profile.uri.encode(), filename=f"vpn-{device.id[:8]}.txt")
    await callback.message.answer_document(
        document,
        caption=(
            f"Профиль VLESS Reality для «{device.name}».\n\n"
            "Скопируйте ссылку из файла и импортируйте ее в Hiddify, v2rayN, "
            "Nekoray или другой Reality-совместимый клиент."
        ),
        reply_markup=main_keyboard(),
    )
    await callback.message.answer(f"<code>{html.escape(profile.uri)}</code>", parse_mode="HTML")


@router.callback_query(F.data == "device:list")
async def device_list(callback: CallbackQuery):
    user = await current_user(callback)
    await callback.answer()
    async with SessionLocal() as session:
        devices = list(
            await session.scalars(
                select(Device)
                .where(Device.user_id == user.id, Device.is_revoked.is_(False))
                .order_by(Device.created_at)
            )
        )
    if not devices:
        await callback.message.answer("Активных устройств пока нет.", reply_markup=main_keyboard())
        return
    keyboard = InlineKeyboardBuilder()
    for device in devices:
        keyboard.button(text=f"Отозвать: {device.name}", callback_data=f"device:revoke:{device.id}")
    keyboard.adjust(1)
    text = "\n".join(f"• {device.name}" for device in devices)
    await callback.message.answer(text, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("device:revoke:"))
async def device_revoke(callback: CallbackQuery):
    device_id = callback.data.rsplit(":", 1)[-1]
    user = await current_user(callback)
    async with SessionLocal() as session:
        device = await session.get(Device, device_id)
        if device is None or device.user_id != user.id:
            await callback.answer("Устройство не найдено", show_alert=True)
            return
        await revoke_device(session, device, vpn)
    await callback.answer("Профиль отозван")
    await callback.message.answer("Устройство отключено.", reply_markup=main_keyboard())


async def main():
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    logging.basicConfig(level=logging.INFO)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    async def reconcile_loop():
        while True:
            try:
                async with SessionLocal() as session:
                    await reconcile_vpn(session, vpn)
            except Exception:
                logging.exception("VPN reconciliation failed")
            await asyncio.sleep(60)

    task = asyncio.create_task(reconcile_loop())
    try:
        await dispatcher.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
