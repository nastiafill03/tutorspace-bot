from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone, timedelta
from typing import TypeVar
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import CommandStart, Command, CommandObject, BaseFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
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

BTN_TEACHER_SCHEDULE = "📅 Розклад"
BTN_TEACHER_HW       = "📝 Домашні завдання"
BTN_STUDENT_LESSONS  = "📅 Мої уроки"
BTN_STUDENT_HW       = "📝 Моє ДЗ"
BTN_PLATFORM         = "🌐 Відкрити платформу"
BTN_SUPPORT          = "🆘 Підтримка"

MONTH_UK = ["січня","лютого","березня","квітня","травня","червня",
            "липня","серпня","вересня","жовтня","листопада","грудня"]

STATUS_CALLBACK        = "show_status"
CONFIRM_LAUNCH_CALLBACK = "confirm_launch"
CANCEL_LAUNCH_CALLBACK  = "cancel_launch"
# Lesson action callbacks carry the request id: "lesson_confirm:<id>" / "lesson_reject:<id>"
LESSON_CONFIRM_PREFIX = "lesson_confirm:"
LESSON_REJECT_PREFIX  = "lesson_reject:"
RESCHEDULE_PREFIX     = "reschedule_req:"
LESSON_OK_PREFIX      = "lesson_ok:"

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# In-memory state: {telegram_user_id: {teacher_id, starts_at}}
_reschedule_pending: dict[int, dict] = {}

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
INSTAGRAM_URL        = os.environ.get("INSTAGRAM_URL", "").strip()
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "").strip()
NOTIFY_PORT          = int(os.environ.get("NOTIFY_PORT", "8080"))
SUPPORT_BOT_TOKEN    = os.environ.get("SUPPORT_BOT_TOKEN", "").strip()
SUPPORT_BOT_USERNAME = os.environ.get("SUPPORT_BOT_USERNAME", "").strip()

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
db: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
    options=ClientOptions(postgrest_client_timeout=12, storage_client_timeout=12),
)
bot_username_cache: str | None = None

# Support bot (optional — only active when SUPPORT_BOT_TOKEN is set)
support_bot: Bot | None = Bot(token=SUPPORT_BOT_TOKEN) if SUPPORT_BOT_TOKEN else None
support_dp  = Dispatcher()


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


def teacher_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TEACHER_SCHEDULE), KeyboardButton(text=BTN_TEACHER_HW)],
            [KeyboardButton(text=BTN_PLATFORM),         KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def student_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STUDENT_LESSONS), KeyboardButton(text=BTN_STUDENT_HW)],
            [KeyboardButton(text=BTN_PLATFORM),        KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def role_keyboard(role: str) -> ReplyKeyboardMarkup | None:
    if role == "teacher":
        return teacher_menu_keyboard()
    if role == "student":
        return student_menu_keyboard()
    return None


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
        .select("telegram_user_id")
        .eq("id", profile_id)
        .execute()
    )
    if res.data and res.data[0].get("telegram_user_id"):
        return int(res.data[0]["telegram_user_id"])
    return None


def _get_profile_by_tg(telegram_user_id: int) -> dict | None:
    res = (
        db.table("profiles")
        .select("id, role, full_name")
        .eq("telegram_user_id", telegram_user_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _insert_support_message(
    sender_tg_id: int,
    profile_id: str | None,
    role: str,
    name: str,
    text: str,
) -> str | None:
    res = db.table("support_messages").insert({
        "sender_telegram_id": sender_tg_id,
        "sender_profile_id":  profile_id,
        "sender_role":        role,
        "sender_name":        name,
        "message":            text,
    }).execute()
    return res.data[0]["id"] if res.data else None


def _update_support_admin_msg_id(row_id: str, admin_message_id: int) -> None:
    db.table("support_messages").update({"admin_message_id": admin_message_id}).eq("id", row_id).execute()


def _find_support_by_admin_msg(admin_message_id: int) -> dict | None:
    res = (
        db.table("support_messages")
        .select("id, sender_telegram_id, sender_name")
        .eq("admin_message_id", admin_message_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _save_support_reply(row_id: str, reply: str) -> None:
    db.table("support_messages").update({
        "admin_reply": reply,
        "replied_at":  datetime.now(timezone.utc).isoformat(),
    }).eq("id", row_id).execute()


def _find_support_by_id(support_msg_id: str) -> dict | None:
    res = (
        db.table("support_messages")
        .select("id, sender_telegram_id, sender_name, sender_profile_id, sender_role")
        .eq("id", support_msg_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _debug_snapshot(profile_id: str, role: str) -> str:
    res = db.table("profiles").select("full_name, telegram_user_id, created_at").eq("id", profile_id).execute()
    if not res.data:
        return "Профіль не знайдено."
    p = res.data[0]
    name = p.get("full_name") or "—"
    tg_id = p.get("telegram_user_id") or "—"
    try:
        reg = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).astimezone(KYIV_TZ)
        reg_str = reg.strftime("%-d %b %Y")
    except Exception:
        reg_str = "—"

    if role == "teacher":
        students_res = (
            db.table("teacher_students").select("id", count="exact")
            .eq("teacher_id", profile_id).eq("is_active", True).execute()
        )
        students_count = students_res.count or 0

        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=7)
        lessons_res = (
            db.table("lessons").select("id", count="exact")
            .eq("teacher_id", profile_id).eq("status", "planned")
            .gte("starts_at", week_start.isoformat()).lt("starts_at", week_end.isoformat())
            .execute()
        )
        lessons_week = lessons_res.count or 0

        hw_submitted = len(_teacher_hw_submitted(profile_id))

        pay_res = (
            db.table("payment_notifications").select("id", count="exact")
            .eq("teacher_id", profile_id).eq("is_read", False).execute()
        )
        unpaid = pay_res.count or 0

        return (
            f"👤 {name}\n"
            f"🆔 Telegram ID: {tg_id}\n"
            f"📅 Реєстрація: {reg_str}\n\n"
            f"👥 Учнів: {students_count}\n"
            f"📆 Уроків цього тижня: {lessons_week}\n"
            f"📝 ДЗ на перевірці: {hw_submitted}\n"
            f"💳 Непідтверджених оплат: {unpaid}"
        )

    # student
    ts_res = (
        db.table("teacher_students").select("id, teacher_id, lesson_balance")
        .eq("student_id", profile_id).eq("is_active", True).execute()
    )
    ts_rows = ts_res.data or []
    ts_ids = [r["id"] for r in ts_rows]

    teacher_lines = []
    for ts in ts_rows:
        t_res = db.table("profiles").select("full_name").eq("id", ts["teacher_id"]).execute()
        t_name = t_res.data[0]["full_name"] if t_res.data else "—"
        teacher_lines.append(f"  • {t_name} — баланс: {ts.get('lesson_balance', 0)} ур.")
    teachers_text = "\n".join(teacher_lines) if teacher_lines else "  —"

    upcoming = len(_student_lessons_upcoming(ts_ids)) if ts_ids else 0

    hw_all = _student_homework_list(ts_ids) if ts_ids else []
    status_labels = {
        "assigned": "призначено",
        "submitted": "здано",
        "revision_requested": "на доопрацюванні",
        "completed": "завершено",
    }
    counts = Counter(hw["status"] for hw in hw_all)
    hw_lines = [f"  {status_labels.get(s, s)}: {n}" for s, n in counts.items()] if counts else ["  —"]

    return (
        f"👤 {name}\n"
        f"🆔 Telegram ID: {tg_id}\n"
        f"📅 Реєстрація: {reg_str}\n\n"
        f"🎓 Викладачі:\n{teachers_text}\n\n"
        f"📆 Найближчих уроків: {upcoming}\n\n"
        f"📚 Домашні завдання:\n" + "\n".join(hw_lines)
    )


def _teacher_lessons_7days(teacher_id: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    res = (
        db.table("lessons")
        .select("id, student_id, starts_at")
        .eq("teacher_id", teacher_id)
        .eq("status", "planned")
        .gte("starts_at", now.isoformat())
        .lte("starts_at", end.isoformat())
        .order("starts_at")
        .execute()
    )
    return res.data or []


def _teacher_hw_submitted(teacher_id: str) -> list[dict]:
    res = (
        db.table("homework")
        .select("id, student_id, title, submitted_at")
        .eq("teacher_id", teacher_id)
        .eq("status", "submitted")
        .order("submitted_at", desc=True)
        .execute()
    )
    return res.data or []


def _teacher_hw_assigned_soon(teacher_id: str) -> list[dict]:
    end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    res = (
        db.table("homework")
        .select("id, student_id, title, deadline")
        .eq("teacher_id", teacher_id)
        .eq("status", "assigned")
        .lte("deadline", end)  # NULL deadlines excluded by PostgREST automatically
        .order("deadline")
        .execute()
    )
    return res.data or []


def _student_ts_ids(profile_id: str) -> list[str]:
    res = (
        db.table("teacher_students")
        .select("id")
        .eq("student_id", profile_id)
        .eq("is_active", True)
        .execute()
    )
    return [r["id"] for r in (res.data or [])]


def _student_lessons_upcoming(ts_ids: list[str]) -> list[dict]:
    if not ts_ids:
        return []
    now = datetime.now(timezone.utc)
    res = (
        db.table("lessons")
        .select("id, teacher_id, starts_at")
        .in_("student_id", ts_ids)
        .eq("status", "planned")
        .gte("starts_at", now.isoformat())
        .order("starts_at")
        .limit(5)
        .execute()
    )
    return res.data or []


def _student_homework_list(ts_ids: list[str]) -> list[dict]:
    if not ts_ids:
        return []
    res = (
        db.table("homework")
        .select("id, title, deadline, status")
        .in_("student_id", ts_ids)
        .order("deadline")
        .limit(15)
        .execute()
    )
    return res.data or []


def _lookup_link_token(token: str) -> dict | None:
    res = (
        db.table("telegram_link_tokens")
        .select("user_id, created_at")
        .eq("token", token)
        .execute()
    )
    return res.data[0] if res.data else None


def _link_telegram_user(profile_id: str, telegram_user_id: int) -> None:
    db.table("profiles").update({"telegram_user_id": telegram_user_id}).eq("id", profile_id).execute()


def _delete_link_token(token: str) -> None:
    db.table("telegram_link_tokens").delete().eq("token", token).execute()


def _rpc_confirm_lesson(reschedule_id: str):
    return db.rpc("confirm_lesson_reschedule", {"p_reschedule_id": reschedule_id}).execute()


def _rpc_reject_lesson(reschedule_id: str):
    return db.rpc("reject_lesson_reschedule", {"p_reschedule_id": reschedule_id}).execute()


def _get_lesson(lesson_id: str) -> dict | None:
    res = (
        db.table("lessons")
        .select("id, teacher_id, student_id, starts_at")
        .eq("id", lesson_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _get_ts_row(ts_id: str) -> dict | None:
    """Get teacher_students row; lessons.student_id is teacher_students.id."""
    res = (
        db.table("teacher_students")
        .select("student_id, student_name")
        .eq("id", ts_id)
        .execute()
    )
    return res.data[0] if res.data else None


def _get_profile_name(profile_id: str) -> str | None:
    res = db.table("profiles").select("full_name").eq("id", profile_id).execute()
    return res.data[0]["full_name"] if res.data else None


def _get_profile_tg(profile_id: str) -> int | None:
    res = db.table("profiles").select("telegram_user_id").eq("id", profile_id).execute()
    if res.data and res.data[0].get("telegram_user_id"):
        return int(res.data[0]["telegram_user_id"])
    return None


def _mark_sent(lesson_id: str, column: str) -> None:
    db.table("lessons").update({column: True}).eq("id", lesson_id).execute()


def _mark_hw_reminder_sent(hw_id: str) -> None:
    db.table("homework").update({"reminder_sent": True}).eq("id", hw_id).execute()


def _homework_in_window(lesson_ids: list[str]) -> list[dict]:
    """Return homework rows for the given lessons that still need a reminder."""
    if not lesson_ids:
        return []
    res = (
        db.table("homework")
        .select("id, lesson_id")
        .in_("lesson_id", lesson_ids)
        .eq("reminder_sent", False)
        .neq("status", "completed")
        .execute()
    )
    return res.data or []


def _lessons_in_window(minutes_from: int, minutes_to: int, sent_col: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    window_start = (now + timedelta(minutes=minutes_from)).isoformat()
    window_end   = (now + timedelta(minutes=minutes_to)).isoformat()
    res = (
        db.table("lessons")
        .select("id, teacher_id, student_id, starts_at")
        .eq("status", "planned")
        .eq(sent_col, False)
        .gte("starts_at", window_start)
        .lte("starts_at", window_end)
        .execute()
    )
    return res.data or []


# ── Reminder loop ─────────────────────────────────────────────────────────────

async def send_reminders() -> None:
    # 24h window: 23h57m – 24h03m
    for lesson in await db_call(_lessons_in_window, 23 * 60 + 57, 24 * 60 + 3, "reminder_24h_sent"):
        try:
            ts_row = await db_call(_get_ts_row, lesson["student_id"])
            if not ts_row or not ts_row.get("student_id"):
                continue
            student_tg = await db_call(_get_profile_tg, ts_row["student_id"])
            if not student_tg:
                continue
            teacher_name = await db_call(_get_profile_name, lesson["teacher_id"]) or "Вчитель"
            starts = datetime.fromisoformat(lesson["starts_at"].replace("Z", "+00:00"))
            time_str = starts.astimezone(KYIV_TZ).strftime("%H:%M")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Перенести", callback_data=f"{RESCHEDULE_PREFIX}{lesson['id']}"),
                InlineKeyboardButton(text="Все ок",    callback_data=f"{LESSON_OK_PREFIX}{lesson['id']}"),
            ]])
            await bot.send_message(
                student_tg,
                f"Завтра о {time_str} у тебе урок з {teacher_name}.",
                reply_markup=kb,
            )
            await db_call(_mark_sent, lesson["id"], "reminder_24h_sent")
        except Exception:
            log.exception("24h reminder failed for lesson_id=%s", lesson.get("id"))

    # 1h window: 57m – 63m
    for lesson in await db_call(_lessons_in_window, 57, 63, "reminder_1h_sent"):
        try:
            ts_row = await db_call(_get_ts_row, lesson["student_id"])
            if not ts_row or not ts_row.get("student_id"):
                continue
            student_tg = await db_call(_get_profile_tg, ts_row["student_id"])
            if not student_tg:
                continue
            teacher_name = await db_call(_get_profile_name, lesson["teacher_id"]) or "Вчитель"
            starts = datetime.fromisoformat(lesson["starts_at"].replace("Z", "+00:00"))
            time_str = starts.astimezone(KYIV_TZ).strftime("%H:%M")
            await bot.send_message(student_tg, f"За годину у тебе урок з {teacher_name}.")
            await db_call(_mark_sent, lesson["id"], "reminder_1h_sent")
        except Exception:
            log.exception("1h reminder failed for lesson_id=%s", lesson.get("id"))

    # Homework reminders (24h before lesson)
    lessons_24h_all = await db_call(_lessons_in_window, 23 * 60 + 57, 24 * 60 + 3, "reminder_24h_sent")
    # Include lessons whose 24h reminder was already sent in a previous cycle
    # so homework reminders still fire even if reminder_24h_sent is already true.
    now = datetime.now(timezone.utc)
    w_start = (now + timedelta(minutes=23 * 60 + 57)).isoformat()
    w_end   = (now + timedelta(minutes=24 * 60 + 3)).isoformat()
    all_24h_lessons = await db_call(
        lambda: db.table("lessons")
        .select("id, teacher_id, student_id, starts_at")
        .eq("status", "planned")
        .gte("starts_at", w_start)
        .lte("starts_at", w_end)
        .execute()
    )
    lesson_map = {r["id"]: r for r in (all_24h_lessons.data or [])}
    hw_rows = await db_call(_homework_in_window, list(lesson_map.keys()))
    for hw in hw_rows:
        try:
            lesson = lesson_map.get(hw["lesson_id"])
            if not lesson:
                continue
            ts_row = await db_call(_get_ts_row, lesson["student_id"])
            if not ts_row or not ts_row.get("student_id"):
                continue
            student_tg = await db_call(_get_profile_tg, ts_row["student_id"])
            if not student_tg:
                continue
            teacher_name = await db_call(_get_profile_name, lesson["teacher_id"]) or "Вчитель"
            starts = datetime.fromisoformat(lesson["starts_at"].replace("Z", "+00:00"))
            time_str = starts.astimezone(KYIV_TZ).strftime("%d.%m о %H:%M")
            await bot.send_message(
                student_tg,
                f"Не забудь зробити домашнє завдання до уроку з {teacher_name} {time_str}.",
            )
            await db_call(_mark_hw_reminder_sent, hw["id"])
        except Exception:
            log.exception("homework reminder failed for hw_id=%s", hw.get("id"))


async def reminder_loop() -> None:
    while True:
        try:
            await send_reminders()
        except Exception:
            log.exception("reminder_loop crashed")
        await asyncio.sleep(5 * 60)


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

async def handle_link_token(message: Message, token: str) -> None:
    """Handle deep-link Telegram account linking via /start <token>."""
    try:
        row = await db_call(_lookup_link_token, token)
    except Exception:
        log.exception("DB error looking up link token")
        await message.answer("❌ Сталася помилка. Спробуй ще раз або зверніться до підтримки.")
        return

    if not row:
        await message.answer(
            "❌ Посилання недійсне або вже використане.\n"
            "Створи нове в профілі на сайті TutorSpace."
        )
        return

    created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - created_at > timedelta(minutes=15):
        try:
            await db_call(_delete_link_token, token)
        except Exception:
            pass
        await message.answer(
            "⏰ Посилання прострочене (дійсне 15 хвилин).\n"
            "Створи нове в профілі на сайті TutorSpace."
        )
        return

    profile_id = row["user_id"]
    telegram_user_id = message.from_user.id

    try:
        await db_call(_link_telegram_user, profile_id, telegram_user_id)
        await db_call(_delete_link_token, token)
    except Exception:
        log.exception("Failed to link telegram_user_id=%s to profile_id=%s", telegram_user_id, profile_id)
        await message.answer("❌ Сталася помилка. Спробуй ще раз або зверніться до підтримки.")
        return

    try:
        role_res = await db_call(
            lambda pid: db.table("profiles").select("role").eq("id", pid).execute(),
            profile_id,
        )
        role = (role_res.data[0].get("role") if role_res.data else None) or ""
    except Exception:
        role = ""

    role_label = "викладача" if role == "teacher" else "учня" if role == "student" else "користувача"
    await message.answer(
        f"✅ Telegram успішно прив'язано до акаунту {role_label} на TutorSpace!\n"
        "Тепер будеш отримувати сповіщення про уроки та платежі тут.",
        reply_markup=role_keyboard(role),
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = message.from_user
    log.info("Incoming /start from user_id=%s username=%s", user.id, user.username)
    arg = (command.args or "").strip()

    # Deep-link account linking token (40-char hex, not a referral link) —
    # check this BEFORE the admin branch, so linking works even when the
    # person linking is using the same Telegram account as ADMIN_ID.
    if arg and not arg.startswith("ref_"):
        await handle_link_token(message, arg)
        return

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


# ── /menu + меню вчителя / учня ───────────────────────────────────────────────

def _day_label(dt: datetime) -> str:
    local = dt.astimezone(KYIV_TZ)
    today = datetime.now(KYIV_TZ).date()
    if local.date() == today:
        return "Сьогодні"
    if local.date() == today + timedelta(days=1):
        return "Завтра"
    return f"{local.day} {MONTH_UK[local.month - 1]}"


async def _require_profile(message: Message) -> dict | None:
    """Return linked profile or send 'not linked' message and return None."""
    profile = await db_call(_get_profile_by_tg, message.from_user.id)
    if not profile:
        await message.answer(
            "Ти ще не підключив(-ла) Telegram до платформи.\n"
            f"Зайди в профіль на {PLATFORM_URL} і натисни «Підключити Telegram»."
        )
    return profile


@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    profile = await _require_profile(message)
    if not profile:
        return
    kb = role_keyboard(profile["role"])
    if kb:
        await message.answer("Головне меню:", reply_markup=kb)
    else:
        await message.answer(f"Платформа: {PLATFORM_URL}")


@dp.message(F.text == BTN_TEACHER_SCHEDULE)
async def btn_teacher_schedule(message: Message) -> None:
    profile = await _require_profile(message)
    if not profile or profile["role"] != "teacher":
        await message.answer("Ця кнопка доступна тільки для викладачів.")
        return

    lessons = await db_call(_teacher_lessons_7days, profile["id"])
    if not lessons:
        await message.answer("Найближчим часом уроків не заплановано.")
        return

    # Group by day
    groups: dict[str, list[str]] = {}
    for lesson in lessons:
        starts = datetime.fromisoformat(lesson["starts_at"].replace("Z", "+00:00"))
        day = _day_label(starts)
        time_str = starts.astimezone(KYIV_TZ).strftime("%H:%M")
        ts_row = await db_call(_get_ts_row, lesson["student_id"])
        student_name = ts_row["student_name"] if ts_row else "—"
        groups.setdefault(day, []).append(f"  {time_str} — {student_name}")

    lines = []
    for day, items in groups.items():
        lines.append(f"📅 {day}:")
        lines.extend(items)
    await message.answer("\n".join(lines))


@dp.message(F.text == BTN_TEACHER_HW)
async def btn_teacher_hw(message: Message) -> None:
    profile = await _require_profile(message)
    if not profile or profile["role"] != "teacher":
        await message.answer("Ця кнопка доступна тільки для викладачів.")
        return

    submitted = await db_call(_teacher_hw_submitted, profile["id"])
    assigned_soon = await db_call(_teacher_hw_assigned_soon, profile["id"])

    lines: list[str] = []

    if submitted:
        lines.append("🔎 Очікують перевірки:")
        for hw in submitted:
            ts_row = await db_call(_get_ts_row, hw["student_id"])
            name = ts_row["student_name"] if ts_row else "—"
            date_str = ""
            if hw.get("submitted_at"):
                d = datetime.fromisoformat(hw["submitted_at"].replace("Z", "+00:00"))
                date_str = f" (здано {d.astimezone(KYIV_TZ).strftime('%-d.%-m')})"
            lines.append(f"  {name} — «{hw['title']}»{date_str}")

    if assigned_soon:
        if lines:
            lines.append("")
        lines.append("⏳ Ще не здали (дедлайн найближчим часом):")
        for hw in assigned_soon:
            ts_row = await db_call(_get_ts_row, hw["student_id"])
            name = ts_row["student_name"] if ts_row else "—"
            date_str = ""
            if hw.get("deadline"):
                d = datetime.fromisoformat(hw["deadline"].replace("Z", "+00:00"))
                date_str = f" (до {d.astimezone(KYIV_TZ).strftime('%-d.%-m')})"
            lines.append(f"  {name} — «{hw['title']}»{date_str}")

    if not lines:
        await message.answer("Все виконано, перевіряти нема чого 🎉")
        return

    await message.answer("\n".join(lines))


@dp.message(F.text == BTN_STUDENT_LESSONS)
async def btn_student_lessons(message: Message) -> None:
    profile = await _require_profile(message)
    if not profile or profile["role"] != "student":
        await message.answer("Ця кнопка доступна тільки для учнів.")
        return

    ts_ids = await db_call(_student_ts_ids, profile["id"])
    lessons = await db_call(_student_lessons_upcoming, ts_ids)
    if not lessons:
        await message.answer("Найближчих уроків не заплановано.")
        return

    lines = []
    for lesson in lessons:
        starts = datetime.fromisoformat(lesson["starts_at"].replace("Z", "+00:00"))
        dt_str = starts.astimezone(KYIV_TZ).strftime("%-d.%-m %H:%M")
        teacher_name = await db_call(_get_profile_name, lesson["teacher_id"]) or "Вчитель"
        lines.append(f"{dt_str} — урок з {teacher_name}")
    await message.answer("\n".join(lines))


@dp.message(F.text == BTN_STUDENT_HW)
async def btn_student_hw(message: Message) -> None:
    profile = await _require_profile(message)
    if not profile or profile["role"] != "student":
        await message.answer("Ця кнопка доступна тільки для учнів.")
        return

    ts_ids = await db_call(_student_ts_ids, profile["id"])
    hw_list = await db_call(_student_homework_list, ts_ids)
    if not hw_list:
        await message.answer("Домашніх завдань немає.")
        return

    STATUS_LABELS = {
        "assigned":           "🔴 Не здано",
        "submitted":          "🟡 На перевірці",
        "revision_requested": "🟠 Потрібно доопрацювати",
        "completed":          "✅ Виконано",
    }
    lines = []
    for hw in hw_list:
        label = STATUS_LABELS.get(hw.get("status", ""), "—")
        date_str = ""
        if hw.get("deadline"):
            d = datetime.fromisoformat(hw["deadline"].replace("Z", "+00:00"))
            date_str = f", до {d.astimezone(KYIV_TZ).strftime('%-d.%-m')}"
        lines.append(f"{label} «{hw['title']}»{date_str}")
    await message.answer("\n".join(lines))


@dp.message(F.text == BTN_PLATFORM)
async def btn_platform(message: Message) -> None:
    profile = await _require_profile(message)
    if not profile:
        return
    if profile["role"] == "teacher":
        await message.answer(f"Платформа (вчитель): {PLATFORM_URL}/teacher/dashboard")
    else:
        await message.answer(f"Платформа (учень): {PLATFORM_URL}/student/home")


@dp.message(F.text == BTN_SUPPORT)
async def btn_support(message: Message) -> None:
    if SUPPORT_BOT_USERNAME:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Написати в підтримку",
                url=f"https://t.me/{SUPPORT_BOT_USERNAME}",
            )
        ]])
        await message.answer("Напиши нам напряму — швидше відповімо 🙂", reply_markup=kb)
    else:
        await message.answer(f"Зв'яжись з нами через платформу: {PLATFORM_URL}")


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


# ── Lesson reminders: "Перенести" / "Все ок" callbacks ───────────────────────

@dp.callback_query(F.data.startswith(RESCHEDULE_PREFIX))
async def cb_reschedule_request(callback: CallbackQuery) -> None:
    lesson_id = callback.data[len(RESCHEDULE_PREFIX):]
    lesson = await db_call(_get_lesson, lesson_id)
    if not lesson:
        await callback.answer("Урок не знайдено.", show_alert=True)
        return

    starts = datetime.fromisoformat(lesson["starts_at"].replace("Z", "+00:00"))
    time_str = starts.astimezone(KYIV_TZ).strftime("%d.%m.%Y о %H:%M")
    _reschedule_pending[callback.from_user.id] = {
        "teacher_id": lesson["teacher_id"],
        "starts_at":  lesson["starts_at"],
    }
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Без причини")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    if callback.message:
        await callback.message.answer(
            f"Напиши причину переносу уроку {time_str} або натисни «Без причини»:",
            reply_markup=kb,
        )
    await callback.answer()


@dp.callback_query(F.data.startswith(LESSON_OK_PREFIX))
async def cb_lesson_ok(callback: CallbackQuery) -> None:
    await callback.answer("Чудово! Побачимось на уроці 👍")


class _AwaitingRescheduleReason(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in _reschedule_pending)


@dp.message(_AwaitingRescheduleReason(), F.text)
async def handle_reschedule_reason(message: Message) -> None:
    state = _reschedule_pending.pop(message.from_user.id)
    reason_text = (message.text or "").strip()
    reason_display = "не вказана" if reason_text in ("Без причини", "") else reason_text

    starts = datetime.fromisoformat(state["starts_at"].replace("Z", "+00:00"))
    time_str = starts.astimezone(KYIV_TZ).strftime("%d.%m.%Y о %H:%M")
    student_name = message.from_user.full_name or "Учень"

    teacher_tg = await db_call(_get_profile_tg, state["teacher_id"])
    if teacher_tg:
        try:
            await bot.send_message(
                teacher_tg,
                f"{student_name} хоче перенести урок {time_str}. Причина: {reason_display}.",
            )
        except Exception:
            log.exception("Failed to notify teacher about reschedule, teacher_id=%s", state["teacher_id"])

    await message.answer(
        "Повідомлення надіслано. Домовся з вчителем про новий час окремо — бот тут не узгоджує заміну.",
        reply_markup=ReplyKeyboardRemove(),
    )


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


# ── Support bot handlers ───────────────────────────────────────────────────────

@support_dp.message(Command("start"))
async def support_start(message: Message) -> None:
    if not support_bot:
        return
    if message.from_user and message.from_user.id == ADMIN_ID:
        await message.answer(
            "Привіт, адміне! Тут збираються повідомлення від користувачів. "
            "Відповідай реплаєм на будь-яке з них.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            "Привіт! Напиши своє питання чи проблему — ми відповімо якнайшвидше.",
            reply_markup=ReplyKeyboardRemove(),
        )


@support_dp.message(F.text)
async def support_handle_message(message: Message) -> None:
    if not support_bot:
        return
    uid = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None

    # Admin replying to a forwarded user message
    if uid == ADMIN_ID and message.reply_to_message:
        original = _find_support_by_admin_msg(message.reply_to_message.message_id)
        if original:
            _save_support_reply(original["id"], message.text or "")
            try:
                await support_bot.send_message(
                    original["sender_telegram_id"],
                    f"Відповідь підтримки:\n{message.text}",
                )
                await message.reply("Відповідь надіслано ✅")
            except Exception:
                await message.reply("Не вдалося надіслати відповідь користувачу.")
        else:
            await message.reply("Не вдалося знайти відповідне повідомлення.")
        return

    # Regular user sending a support message — look up their profile for name/role
    profile = _get_profile_by_tg(uid)
    sender_name = profile["full_name"] if profile else (username or str(uid))
    sender_role = profile["role"] if profile else "unknown"
    row_id = _insert_support_message(uid, profile["id"] if profile else None, sender_role, sender_name, message.text or "")
    try:
        kb = None
        if row_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🔍 Дані користувача",
                    callback_data=f"support_debug:{row_id}",
                )
            ]])
        forwarded = await support_bot.send_message(
            ADMIN_ID,
            f"📩 Підтримка від {sender_name} (@{username or uid}):\n\n{message.text}",
            reply_markup=kb,
        )
        if row_id:
            _update_support_admin_msg_id(row_id, forwarded.message_id)
    except Exception:
        log.warning("Could not forward support message to admin")
    await message.reply("Дякуємо! Ми отримали твоє повідомлення та відповімо найближчим часом.")


@support_dp.callback_query(F.data.startswith("support_debug:"))
async def support_debug_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    support_msg_id = callback.data.split(":", 1)[1]
    row = _find_support_by_id(support_msg_id)
    if not row:
        await callback.answer("Повідомлення не знайдено.", show_alert=True)
        return

    profile_id = row.get("sender_profile_id")
    role = row.get("sender_role") or "unknown"

    if not profile_id:
        await callback.answer()
        await callback.message.answer("Профіль не знайдено — акаунт не прив'язаний до платформи.")
        return

    snapshot = _debug_snapshot(profile_id, role)
    await callback.answer()
    await callback.message.answer(snapshot)


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


async def _run_support_bot() -> None:
    if not support_bot:
        return
    try:
        await support_bot.set_my_commands(
            [BotCommand(command="start", description="Написати в підтримку")],
            scope=BotCommandScopeDefault(),
        )
        await support_bot.delete_webhook(drop_pending_updates=True)
        log.info("Starting SupportBot (polling)...")
        await support_dp.start_polling(support_bot, allowed_updates=support_dp.resolve_used_update_types())
    except TelegramConflictError:
        log.error("SupportBot polling conflict — stop duplicate instances.")
        raise
    except Exception:
        log.exception("SupportBot crashed — main bot keeps running.")
    finally:
        await support_bot.session.close()


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
        asyncio.create_task(reminder_loop())
        if support_bot:
            asyncio.create_task(_run_support_bot())

        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except TelegramConflictError:
            log.error("Polling conflict: stop other running bot instances or disable webhook.")
            raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
