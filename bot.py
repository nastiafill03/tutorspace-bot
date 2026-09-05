from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone, timedelta
from typing import TypeVar
from urllib.parse import quote

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand,
    BotCommandScopeDefault, BotCommandScopeChat,
)
from dotenv import load_dotenv
from supabase import create_client, Client, ClientOptions

load_dotenv()

T = TypeVar("T")

# ── Constants ────────────────────────────────────────────────────────────────
BASE_MONTHS     = 2
REFERRAL_MONTHS = 1
MAX_REFERRALS   = 10
FOUNDING_LIMIT  = 30
BROADCAST_RATE  = 25  # messages / second (Telegram rate limit)

BTN_WAITLIST = "👥 Waitlist"
BTN_TOP      = "🏆 Топ реферери"
BTN_RECENT   = "🆕 Останні реєстрації"
BTN_LAUNCH   = "🚀 Запустити платформу"

STATUS_CALLBACK        = "show_status"
CONFIRM_LAUNCH_CALLBACK = "confirm_launch"
CANCEL_LAUNCH_CALLBACK  = "cancel_launch"
# Lesson action callbacks carry the request id: "lesson_confirm:<id>" / "lesson_reject:<id>"
LESSON_CONFIRM_PREFIX = "lesson_confirm:"
LESSON_REJECT_PREFIX  = "lesson_reject:"

# ── Init ─────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN            = required_env("BOT_TOKEN")
SUPABASE_URL         = required_env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = required_env("SUPABASE_SERVICE_KEY")
try:
    ADMIN_ID         = int(required_env("ADMIN_ID"))
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user id") from exc
PLATFORM_URL    = required_env("PLATFORM_URL")
INSTAGRAM_URL   = os.environ.get("INSTAGRAM_URL", "").strip()
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET", "").strip()
NOTIFY_PORT     = int(os.environ.get("NOTIFY_PORT", "8080"))

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
db: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
    options=ClientOptions(postgrest_client_timeout=12, storage_client_timeout=12),
)
bot_username_cache: str | None = None


async def db_call(func: Callable[..., T], *args, **kwargs) -> T:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=15.0)
        except (asyncio.TimeoutError, Exception) as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last_exc


# ── Keyboards ─────────────────────────────────────────────────────────────────

def founder_keyboard(ref_link: str) -> InlineKeyboardMarkup:
    invite_text = "Приєднуйся до TutorSpace — платформи для репетиторів 🎓"
    share_url = f"https://t.me/share/url?url={quote(ref_link, safe='')}&text={quote(invite_text)}"
    rows = [
        [InlineKeyboardButton(text="📨 Поділитися з другом", url=share_url)],
        [InlineKeyboardButton(text="📊 Мій статус", callback_data=STATUS_CALLBACK)],
    ]
    if INSTAGRAM_URL:
        rows.append([InlineKeyboardButton(text="📸 Наш Instagram", url=INSTAGRAM_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def waitlist_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📊 Мій статус", callback_data=STATUS_CALLBACK)]]
    if INSTAGRAM_URL:
        rows.append([InlineKeyboardButton(text="📸 Наш Instagram", url=INSTAGRAM_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=BTN_WAITLIST),
            KeyboardButton(text=BTN_TOP),
            KeyboardButton(text=BTN_RECENT),
            KeyboardButton(text=BTN_LAUNCH),
        ]],
        resize_keyboard=True,
        persistent=True,
    )


# ── DB helpers — waitlist ─────────────────────────────────────────────────────

def get_user(telegram_id: int) -> dict | None:
    res = db.table("waitlist_users").select("*").eq("telegram_id", telegram_id).execute()
    return res.data[0] if res.data else None


def founder_count() -> int:
    res = (
        db.table("waitlist_users")
        .select("telegram_id", count="exact")
        .eq("is_founder", True)
        .neq("telegram_id", ADMIN_ID)
        .execute()
    )
    return res.count or 0


def total_users() -> int:
    res = (
        db.table("waitlist_users")
        .select("telegram_id", count="exact")
        .neq("telegram_id", ADMIN_ID)
        .execute()
    )
    return res.count or 0


def top_referrers(limit: int = 5) -> list[dict]:
    res = (
        db.table("waitlist_users")
        .select("first_name, username, referral_count, months_earned")
        .eq("is_founder", True)
        .neq("telegram_id", ADMIN_ID)
        .order("referral_count", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def recent_users(limit: int = 5) -> list[dict]:
    res = (
        db.table("waitlist_users")
        .select("first_name, username, is_founder, founder_number, months_earned, joined_at")
        .neq("telegram_id", ADMIN_ID)
        .order("joined_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


# ── DB helpers — broadcast ────────────────────────────────────────────────────

def pending_broadcast_count() -> int:
    res = (
        db.table("waitlist_users")
        .select("telegram_id", count="exact")
        .is_("notified_at", "null")
        .neq("telegram_id", ADMIN_ID)
        .execute()
    )
    return res.count or 0


def pending_broadcast_users() -> list[dict]:
    res = (
        db.table("waitlist_users")
        .select("telegram_id, months_earned, signup_token")
        .is_("notified_at", "null")
        .neq("telegram_id", ADMIN_ID)
        .execute()
    )
    return res.data or []


def mark_notified(telegram_id: int, granted: int) -> None:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=30)
    db.table("waitlist_users").update({
        "notified_at": now.isoformat(),
        "token_expires_at": expires.isoformat(),
        "granted_months": granted,
    }).eq("telegram_id", telegram_id).execute()


# ── DB helpers — promo (kept for granted_months calculation in broadcast) ─────

def get_promo_code(code: str) -> dict | None:
    normalized = code.strip().upper()
    res = db.table("promo_codes").select("*").eq("code", normalized).execute()
    return res.data[0] if res.data else None


def get_promo_months(telegram_id: int) -> int:
    res = (
        db.table("promo_activations")
        .select("bonus_months")
        .eq("telegram_id", telegram_id)
        .execute()
    )
    return sum(row["bonus_months"] for row in (res.data or []))


# ── DB helpers — platform integration ────────────────────────────────────────

def _rpc_link_telegram(code: str, chat_id: int):
    # RPC name and params must match the function created in the Next.js / Supabase migration.
    return db.rpc("link_telegram", {
        "p_one_time_code": code,
        "p_chat_id": str(chat_id),
    }).execute()


def _get_profile_chat_id(profile_id: str) -> int | None:
    res = (
        db.table("profiles")
        .select("telegram_chat_id")
        .eq("id", profile_id)
        .execute()
    )
    if res.data and res.data[0].get("telegram_chat_id"):
        return int(res.data[0]["telegram_chat_id"])
    return None


def _rpc_confirm_lesson(reschedule_id: str):
    return db.rpc("confirm_lesson_reschedule", {"p_reschedule_id": reschedule_id}).execute()


def _rpc_reject_lesson(reschedule_id: str):
    return db.rpc("reject_lesson_reschedule", {"p_reschedule_id": reschedule_id}).execute()


# ── Misc helpers ──────────────────────────────────────────────────────────────

async def ref_link_for(user_id: int) -> str:
    global bot_username_cache
    if bot_username_cache is None:
        bot_info = await bot.get_me()
        bot_username_cache = bot_info.username
    return f"https://t.me/{bot_username_cache}?start=ref_{user_id}"


def status_view(user_data: dict, ref_link: str) -> tuple[str, InlineKeyboardMarkup]:
    if user_data["is_founder"]:
        text = (
            f"📊 Твій статус:\n\n"
            f"👑 Founding Member #{user_data['founder_number']} з {FOUNDING_LIMIT}\n"
            f"🗓 Безкоштовних місяців: {user_data['months_earned']}\n"
            f"👥 Запрошено друзів: {user_data['referral_count']}/{MAX_REFERRALS}"
        )
        return text, founder_keyboard(ref_link)

    text = "✋ Ти в списку очікування.\nПовідомимо при запуску."
    return text, waitlist_keyboard()


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def reject_non_admin(message: Message) -> None:
    await message.answer("Невідома команда.")


# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = message.from_user
    log.info("Incoming /start from user_id=%s username=%s", user.id, user.username)

    if user.id == ADMIN_ID:
        await message.answer(
            "👋 Привіт, адміне! Ти керуєш ботом і не рахуєшся учасником waitlist.\n"
            f"Твій ID: {user.id}\n\nКоманди: /admin, /stats, /top, /recent",
            reply_markup=admin_keyboard(),
        )
        return

    await message.answer(
        f"🎉 TutorSpace вже відкрита!\n\n"
        f"Заходь і реєструйся на платформі: {PLATFORM_URL}\n\n"
        f"Якщо ти отримав(-ла) персональне посилання — скористайся ним: там враховані "
        f"твої бонуси. Якщо ні — реєструйся за звичайним посиланням вище."
    )


# ── /status + кнопка "📊 Мій статус" ──────────────────────────────────────────

@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    if not is_admin(message):
        await reject_non_admin(message)
        return
    await message.answer(f"Твій Telegram ID: {message.from_user.id}")


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    log.info("Incoming /status from user_id=%s", message.from_user.id if message.from_user else None)
    if message.from_user.id == ADMIN_ID:
        await message.answer("Ти адмін — не рахуєшся учасником. Дивись /stats 📋")
        return

    user_data = await db_call(get_user, message.from_user.id)
    if not user_data:
        await message.answer(f"Тебе немає у списку. Зареєструйся на платформі: {PLATFORM_URL}")
        return

    ref_link = await ref_link_for(message.from_user.id)
    text, kb = status_view(user_data, ref_link)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == STATUS_CALLBACK)
async def cb_status(callback: CallbackQuery) -> None:
    if callback.from_user.id == ADMIN_ID:
        await callback.answer("Ти адмін — не рахуєшся учасником. Дивись /stats 📋", show_alert=True)
        return

    user_data = await db_call(get_user, callback.from_user.id)
    if not user_data:
        await callback.answer("Спочатку зареєструйся на платформі.", show_alert=True)
        return

    ref_link = await ref_link_for(callback.from_user.id)
    text, kb = status_view(user_data, ref_link)
    if callback.message:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ── /link — прив'язати Telegram до профілю TutorSpace ────────────────────────

@dp.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject) -> None:
    code = (command.args or "").strip()
    if not code:
        await message.answer(
            "Вкажи одноразовий код з профілю: /link <код>\n\n"
            "Знайдеш його в профілі на сайті TutorSpace у розділі «Налаштування»."
        )
        return

    chat_id = message.chat.id
    try:
        result = await db_call(_rpc_link_telegram, code, chat_id)
        if result.data:
            await message.answer(
                "✅ Telegram успішно прив'язано до профілю TutorSpace!\n"
                "Тепер будеш отримувати сповіщення про уроки та платежі тут."
            )
        else:
            await message.answer(
                "❌ Код невірний або вже використаний. Створи новий у профілі на сайті."
            )
    except Exception:
        log.exception("RPC link_telegram failed for chat_id=%s", chat_id)
        await message.answer("❌ Сталася помилка. Спробуй ще раз або зверніться до підтримки.")


# ── Broadcast: кнопка "🚀 Запустити платформу" ───────────────────────────────

@dp.message(F.text == BTN_LAUNCH)
async def btn_launch(message: Message) -> None:
    if not is_admin(message):
        return

    count = await db_call(pending_broadcast_count)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, надіслати", callback_data=CONFIRM_LAUNCH_CALLBACK)],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=CANCEL_LAUNCH_CALLBACK)],
    ])
    await message.answer(
        f"Буде надіслано персональні посилання {count} людям з вейтлісту.\n"
        f"Це незворотна дія. Підтвердити?",
        reply_markup=kb,
    )


@dp.callback_query(F.data == CONFIRM_LAUNCH_CALLBACK)
async def cb_confirm_launch(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Тільки для адміна.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("🚀 Розсилка запущена, зачекай...")
    await callback.answer()
    asyncio.create_task(run_broadcast(callback.from_user.id))


@dp.callback_query(F.data == CANCEL_LAUNCH_CALLBACK)
async def cb_cancel_launch(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Тільки для адміна.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("Скасовано.")
    await callback.answer()


async def run_broadcast(admin_chat_id: int) -> None:
    users = await db_call(pending_broadcast_users)
    sent = 0
    failed = 0

    for user in users:
        telegram_id = user["telegram_id"]
        promo_months = await db_call(get_promo_months, telegram_id)
        granted = (user.get("months_earned") or 0) + promo_months
        token = user.get("signup_token")
        link = f"{PLATFORM_URL}/welcome/{token}" if token else PLATFORM_URL

        if granted > 0:
            text = (
                f"Привіт! TutorSpace відкрита 🎉\n"
                f"Тобі належить {granted} міс. Pro безкоштовно — "
                f"заходь і реєструйся: {link}"
            )
        else:
            text = (
                f"Привіт! TutorSpace відкрита 🎉\n"
                f"Заходь і реєструйся: {link}"
            )

        try:
            await bot.send_message(telegram_id, text)
            await db_call(mark_notified, telegram_id, granted)
            sent += 1
        except Exception:
            log.warning("Broadcast send failed for telegram_id=%s", telegram_id)
            failed += 1

        await asyncio.sleep(1.0 / BROADCAST_RATE)

    try:
        await bot.send_message(
            admin_chat_id,
            f"✅ Готово. Надіслано: {sent}, не вдалось: {failed}.",
        )
    except Exception:
        log.exception("Could not send broadcast summary to admin")


# ── Lesson reschedule actions (from /notify webhook) ─────────────────────────

@dp.callback_query(F.data.startswith(LESSON_CONFIRM_PREFIX))
async def cb_lesson_confirm(callback: CallbackQuery) -> None:
    reschedule_id = callback.data[len(LESSON_CONFIRM_PREFIX):]
    try:
        await db_call(_rpc_confirm_lesson, reschedule_id)
        if callback.message:
            await callback.message.edit_text("✅ Перенесення підтверджено.")
        await callback.answer("Підтверджено!")
    except Exception:
        log.exception("confirm_lesson_reschedule failed for id=%s", reschedule_id)
        await callback.answer("Помилка. Спробуй ще раз.", show_alert=True)


@dp.callback_query(F.data.startswith(LESSON_REJECT_PREFIX))
async def cb_lesson_reject(callback: CallbackQuery) -> None:
    reschedule_id = callback.data[len(LESSON_REJECT_PREFIX):]
    try:
        await db_call(_rpc_reject_lesson, reschedule_id)
        if callback.message:
            await callback.message.edit_text("❌ Перенесення відхилено.")
        await callback.answer("Відхилено!")
    except Exception:
        log.exception("reject_lesson_reschedule failed for id=%s", reschedule_id)
        await callback.answer("Помилка. Спробуй ще раз.", show_alert=True)


# ── Адмін: /admin, /stats, /top, /recent + reply-кнопки ──────────────────────

@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    log.info("Incoming /admin from user_id=%s", message.from_user.id if message.from_user else None)
    if not is_admin(message):
        await reject_non_admin(message)
        return
    await message.answer("Адмін-меню відкрито.", reply_markup=admin_keyboard())


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    log.info("Incoming /stats from user_id=%s", message.from_user.id if message.from_user else None)
    if not is_admin(message):
        await reject_non_admin(message)
        return

    total    = await db_call(total_users)
    founders = await db_call(founder_count)
    waiting  = total - founders
    pending  = await db_call(pending_broadcast_count)
    await message.answer(
        f"📋 Статистика TutorSpace:\n\n"
        f"Усього: {total}\n"
        f"Founding Members: {founders}/{FOUNDING_LIMIT}\n"
        f"У списку очікування: {waiting}\n"
        f"Ще не отримали розсилку: {pending}"
    )


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    log.info("Incoming /top from user_id=%s", message.from_user.id if message.from_user else None)
    if not is_admin(message):
        await reject_non_admin(message)
        return

    users = await db_call(top_referrers)
    if not users:
        await message.answer("Ще нікого немає 🙁")
        return

    lines = []
    for i, u in enumerate(users, 1):
        name = u.get("first_name") or u.get("username") or "Без імені"
        username = f" (@{u['username']})" if u.get("username") else ""
        lines.append(
            f"{i}. {name}{username} — {u['referral_count']} друзів, {u['months_earned']} міс."
        )
    await message.answer("🏆 Топ реферерів:\n\n" + "\n".join(lines))


@dp.message(Command("recent"))
async def cmd_recent(message: Message) -> None:
    log.info("Incoming /recent from user_id=%s", message.from_user.id if message.from_user else None)
    if not is_admin(message):
        await reject_non_admin(message)
        return

    users = await db_call(recent_users)
    if not users:
        await message.answer("Ще нікого немає 🙁")
        return

    lines = []
    for u in users:
        name = u.get("first_name") or u.get("username") or "Без імені"
        username = f" (@{u['username']})" if u.get("username") else ""
        date = (u.get("joined_at") or "")[:10] or "без дати"
        label = (
            f"Founder #{u['founder_number']}, {u['months_earned']} міс."
            if u["is_founder"] else "у списку очікування"
        )
        lines.append(f"• {name}{username} — {label} [{date}]")
    await message.answer("🆕 Останні реєстрації:\n\n" + "\n".join(lines))


@dp.message(F.text == BTN_WAITLIST)
async def btn_waitlist(message: Message) -> None:
    if not is_admin(message):
        return
    await cmd_stats(message)


@dp.message(F.text == BTN_TOP)
async def btn_top(message: Message) -> None:
    if not is_admin(message):
        return
    await cmd_top(message)


@dp.message(F.text == BTN_RECENT)
async def btn_recent(message: Message) -> None:
    if not is_admin(message):
        return
    await cmd_recent(message)


# ── HTTP /notify endpoint (Supabase Database Webhooks) ───────────────────────

async def _dispatch_notification(chat_id: int, event_type: str, data: dict) -> None:
    if event_type == "lesson_reschedule_request":
        student_name  = data.get("student_name", "Учень")
        proposed_time = data.get("proposed_time", "")
        reschedule_id = data.get("reschedule_id", "")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Підтвердити",
                callback_data=f"{LESSON_CONFIRM_PREFIX}{reschedule_id}",
            ),
            InlineKeyboardButton(
                text="❌ Відхилити",
                callback_data=f"{LESSON_REJECT_PREFIX}{reschedule_id}",
            ),
        ]])
        await bot.send_message(
            chat_id,
            f"📅 {student_name} просить перенести урок.\n"
            f"Пропонований час: {proposed_time}",
            reply_markup=kb,
        )

    elif event_type == "lesson_cancelled":
        student_name = data.get("student_name", "Учень")
        date = data.get("date", "")
        await bot.send_message(chat_id, f"❌ {student_name} скасував(-ла) урок {date}.")

    elif event_type == "homework_assigned":
        title = data.get("title", "Нове завдання")
        await bot.send_message(chat_id, f"📝 Нове домашнє завдання: {title}")

    elif event_type == "grade_received":
        subject = data.get("subject", "")
        grade   = data.get("grade", "")
        await bot.send_message(chat_id, f"🎓 Нова оцінка з {subject}: {grade}")

    elif event_type == "payment_received":
        amount   = data.get("amount", "")
        currency = data.get("currency", "UAH")
        await bot.send_message(chat_id, f"💰 Отримано платіж: {amount} {currency}")

    else:
        log.warning("Unknown event_type in /notify: %s", event_type)


async def notify_handler(request: web.Request) -> web.Response:
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Webhook-Secret", "")
        if secret != WEBHOOK_SECRET:
            return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    event_type = data.get("event_type", "")
    profile_id = data.get("profile_id", "")
    event_data = data.get("data", {})

    if not profile_id:
        return web.Response(status=400, text="Missing profile_id")

    chat_id = await db_call(_get_profile_chat_id, profile_id)
    if not chat_id:
        return web.Response(status=404, text="No telegram_chat_id for this profile")

    try:
        await _dispatch_notification(chat_id, event_type, event_data)
    except Exception:
        log.exception("Failed to dispatch notification for profile_id=%s", profile_id)
        return web.Response(status=500, text="Dispatch error")

    return web.Response(text="OK")


def create_notify_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/notify", notify_handler)
    return app


# ── Entry point ───────────────────────────────────────────────────────────────

@dp.error()
async def error_handler(event) -> bool:
    log.exception("Unhandled update error", exc_info=event.exception)
    message = getattr(event.update, "message", None)
    callback_query = getattr(event.update, "callback_query", None)
    if message:
        await message.answer("Сталася технічна помилка. Спробуй ще раз за хвилинку.")
    elif callback_query:
        await callback_query.answer(
            "Сталася технічна помилка. Спробуй ще раз за хвилинку.",
            show_alert=True,
        )
    return True


async def main() -> None:
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET is not set — /notify endpoint accepts requests without auth")

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start",  description="Почати / головне меню"),
                BotCommand(command="status", description="Мій статус у waitlist"),
                BotCommand(command="link",   description="Прив'язати Telegram до профілю"),
            ],
            scope=BotCommandScopeDefault(),
        )
        await bot.set_my_commands(
            [
                BotCommand(command="start",  description="Почати / головне меню"),
                BotCommand(command="id",     description="Показати мій Telegram ID"),
                BotCommand(command="status", description="Мій статус"),
                BotCommand(command="link",   description="Прив'язати Telegram до профілю"),
                BotCommand(command="admin",  description="Адмін-меню"),
                BotCommand(command="stats",  description="Статистика для адміна"),
                BotCommand(command="top",    description="Топ реферерів"),
                BotCommand(command="recent", description="Останні реєстрації"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
        await bot.delete_webhook(drop_pending_updates=True)

        notify_app = create_notify_app()
        runner = web.AppRunner(notify_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", NOTIFY_PORT)
        await site.start()
        log.info("Notify webhook server listening on port %s", NOTIFY_PORT)

        log.info("Starting TutorSpaceBot (polling)...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramConflictError:
        log.error("Polling conflict: stop other running bot instances or disable webhook.")
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
