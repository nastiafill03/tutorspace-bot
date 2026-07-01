from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand,
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

BTN_WAITLIST = "👥 Waitlist"
BTN_TOP      = "🏆 Топ реферери"
BTN_RECENT   = "🆕 Останні реєстрації"

STATUS_CALLBACK = "show_status"

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
INSTAGRAM_URL        = os.environ.get("INSTAGRAM_URL", "").strip()

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
db: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
    options=ClientOptions(postgrest_client_timeout=12, storage_client_timeout=12),
)
registration_lock = asyncio.Lock()
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
    invite_text = "Приєднуйся до раннього доступу TutorSpace — платформи для репетиторів 🎓"
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
        ]],
        resize_keyboard=True,
        persistent=True,
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_user(telegram_id: int) -> dict | None:
    res = db.table("waitlist_users").select("*").eq("telegram_id", telegram_id).execute()
    return res.data[0] if res.data else None


def create_user(telegram_id: int, username: str | None, first_name: str | None,
                referred_by: int | None, is_founder: bool, founder_number: int | None,
                months_earned: int) -> dict:
    row = {
        "telegram_id": telegram_id,
        "username": username,
        "first_name": first_name,
        "referred_by": referred_by,
        "referral_count": 0,
        "months_earned": months_earned,
        "is_founder": is_founder,
        "founder_number": founder_number,
    }
    res = db.table("waitlist_users").insert(row).execute()
    return res.data[0]


def increment_referrer(referrer_id: int) -> dict:
    ref = get_user(referrer_id)
    if not ref:
        return {}
    new_count  = ref["referral_count"] + 1
    new_months = ref["months_earned"] + REFERRAL_MONTHS
    res = (
        db.table("waitlist_users")
        .update({"referral_count": new_count, "months_earned": new_months})
        .eq("telegram_id", referrer_id)
        .execute()
    )
    return res.data[0] if res.data else {}


def founder_count() -> int:
    # ADMIN_ID is excluded everywhere here — the admin manages the waitlist, doesn't join it.
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


def parse_ref_id(arg: str, user_id: int) -> int | None:
    """Parses 'ref_<id>' deep-link param. Returns None on absence, bad format, or self-referral."""
    if not arg.startswith("ref_"):
        return None
    try:
        ref_id = int(arg[4:])
    except ValueError:
        return None
    return ref_id if ref_id != user_id else None


async def ref_link_for(user_id: int) -> str:
    global bot_username_cache
    if bot_username_cache is None:
        bot_info = await bot.get_me()
        bot_username_cache = bot_info.username
    return f"https://t.me/{bot_username_cache}?start=ref_{user_id}"


def status_view(user_data: dict, ref_link: str) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the (text, keyboard) pair shared by /status and the inline status button."""
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


# ── /start ────────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = message.from_user
    log.info("Incoming /start from user_id=%s username=%s args=%r", user.id, user.username, command.args)

    if user.id == ADMIN_ID:
        # Admin manages the waitlist — never registered as a participant or founder slot.
        await message.answer(
            "👋 Привіт, адміне! Ти керуєш ботом і не рахуєшся учасником waitlist.\n"
            f"Твій ID: {user.id}\n\n"
            "Команди: /admin, /stats, /top, /recent",
            reply_markup=admin_keyboard(),
        )
        return

    async with registration_lock:
        existing = await db_call(get_user, user.id)

        if not existing:
            ref_id = parse_ref_id((command.args or "").strip(), user.id)
            referrer = await db_call(get_user, ref_id) if ref_id else None
            referrer_eligible = (
                referrer is not None
                and referrer["is_founder"]
                and referrer["referral_count"] < MAX_REFERRALS
            )

            current_founders = await db_call(founder_count)
            becomes_founder = current_founders < FOUNDING_LIMIT

            if becomes_founder:
                founder_number = current_founders + 1
                referred_by = ref_id if referrer_eligible else None
                months_earned = BASE_MONTHS

                await db_call(
                    create_user,
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    referred_by=referred_by,
                    is_founder=True,
                    founder_number=founder_number,
                    months_earned=months_earned,
                )
                updated_referrer = (
                    await db_call(increment_referrer, referred_by) if referred_by else None
                )
            else:
                founder_number = None
                referred_by = None
                months_earned = 0
                updated_referrer = None
                await db_call(
                    create_user,
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    referred_by=None,
                    is_founder=False,
                    founder_number=None,
                    months_earned=0,
                )
        else:
            ref_id = None
            referrer = None
            becomes_founder = False
            founder_number = None
            referred_by = None
            months_earned = 0
            updated_referrer = None

    if existing:
        # Repeat /start — only show status; bonuses are credited on first entry only.
        ref_link = await ref_link_for(user.id)
        text, kb = status_view(existing, ref_link)
        await message.answer(text, reply_markup=kb)
        return

    if becomes_founder:
        if referred_by:
            updated = updated_referrer or {}
            try:
                await bot.send_message(
                    referred_by,
                    f"🎉 Друг приєднався як Founding Member! +{REFERRAL_MONTHS} місяць.\n"
                    f"Тепер у тебе {updated.get('months_earned', '?')} · "
                    f"запрошено {updated.get('referral_count', '?')}/{MAX_REFERRALS}.",
                )
            except Exception:
                # Referrer may have blocked the bot — never let this break onboarding.
                log.warning("Could not notify referrer %s", referred_by)

        slots_left = FOUNDING_LIMIT - founder_number
        text = (
            f"🎉 Вітаю в ранньому доступі TutorSpace! Ти — Founding Member #{founder_number} "
            f"з {FOUNDING_LIMIT}. Лишилось {slots_left} місць. "
            f"За передзапис тобі нараховано {BASE_MONTHS} місяці безкоштовно. "
            f"Приведи колегу-репетитора, поки є місця — і ти отримаєш ще "
            f"+{REFERRAL_MONTHS} місяць (до {MAX_REFERRALS} друзів)."
        )
        if referred_by:
            text += (
                f"\n\nТи прийшов за запрошенням друга, але стартовий бонус для нового "
                f"Founding Member лишається {months_earned} місяці."
            )

        ref_link = await ref_link_for(user.id)
        await message.answer(text, reply_markup=founder_keyboard(ref_link))

    else:
        if referrer is not None:
            try:
                await bot.send_message(
                    ref_id,
                    "👀 Друг хотів приєднатися, але Founding-місця вже зайнято, "
                    "бонус не нараховано. Дякуємо, що ділишся 🙌",
                )
            except Exception:
                log.warning("Could not notify referrer %s about cap", ref_id)

        text = (
            f"🙌 Founding-місця вже зайняті — усі {FOUNDING_LIMIT} розібрали! "
            f"Але ти в списку очікування. Щойно відкриємо доступ — напишемо тобі одним із перших."
        )
        await message.answer(text, reply_markup=waitlist_keyboard())


# ── /status + кнопка "📊 Мій статус" ──────────────────────────────────────────

@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    await message.answer(
        f"Твій Telegram ID: {user.id}\n"
        f"ADMIN_ID у боті: {ADMIN_ID}\n"
        f"Адмін-доступ: {'так' if user.id == ADMIN_ID else 'ні'}"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    log.info(
        "Incoming /status from user_id=%s username=%s",
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
    )
    if message.from_user.id == ADMIN_ID:
        await message.answer("Ти адмін — не рахуєшся учасником. Дивись /stats 📋")
        return

    user_data = await db_call(get_user, message.from_user.id)
    if not user_data:
        await message.answer("Ти ще не в списку очікування. Напиши /start, щоб приєднатись!")
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
        await callback.answer("Спочатку напиши /start", show_alert=True)
        return

    ref_link = await ref_link_for(callback.from_user.id)
    text, kb = status_view(user_data, ref_link)
    if callback.message:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ── Адмін: /admin, /stats, /top, /recent + reply-кнопки ──────────────────────

def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def reject_non_admin(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "unknown"
    await message.answer(
        "Ця команда тільки для адміна.\n"
        f"Твій Telegram ID: {user_id}\n"
        f"ADMIN_ID у цьому запущеному боті: {ADMIN_ID}\n"
        "Якщо це ти, постав свій ID в ADMIN_ID і перезапусти саме той інстанс, який зараз відповідає."
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    log.info(
        "Incoming /admin from user_id=%s username=%s",
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
    )
    if not is_admin(message):
        await reject_non_admin(message)
        return

    await message.answer("Адмін-меню відкрито.", reply_markup=admin_keyboard())

@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    log.info(
        "Incoming /stats from user_id=%s username=%s",
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
    )
    if not is_admin(message):
        await reject_non_admin(message)
        return

    total    = await db_call(total_users)
    founders = await db_call(founder_count)
    waiting  = total - founders
    await message.answer(
        f"📋 Статистика TutorSpace:\n\n"
        f"Усього: {total}\n"
        f"Founding Members: {founders}/{FOUNDING_LIMIT}\n"
        f"У списку очікування: {waiting}"
    )


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    log.info(
        "Incoming /top from user_id=%s username=%s",
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
    )
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
    log.info(
        "Incoming /recent from user_id=%s username=%s",
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
    )
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
    try:
        await bot.set_my_commands([
            BotCommand(command="start",  description="Почати / головне меню"),
            BotCommand(command="id",     description="Показати мій Telegram ID"),
            BotCommand(command="status", description="Мій статус"),
            BotCommand(command="admin",  description="Адмін-меню"),
            BotCommand(command="stats",  description="Статистика для адміна"),
            BotCommand(command="top",    description="Топ реферерів"),
            BotCommand(command="recent", description="Останні реєстрації"),
        ])
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Starting TutorSpaceBot (polling)...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramConflictError:
        log.error("Polling conflict: stop other running bot instances or disable webhook.")
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
