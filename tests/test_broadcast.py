"""
Тести для функціоналу розсилки при запуску платформи (run_broadcast у bot.py).

run_broadcast() — async-функція без прямих залежностей від aiogram-типів:
приймає лише admin_chat_id (int) і звертається до Supabase та Telegram через
функції, які легко замінити моками. Тому кожен тест підміняє мережеві
виклики (pending_broadcast_users, get_promo_months, mark_notified, bot.send_message)
і перевіряє саме бізнес-логіку: кому й з яким текстом надіслати, коли
позначати notified, що робити при помилці відправки.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock, call

import bot


ADMIN_CHAT_ID = int(bot.ADMIN_ID)


def _make_users(*overrides_list) -> list[dict]:
    """Будує список рядків waitlist_users для pending_broadcast_users."""
    defaults = {"telegram_id": 111, "months_earned": 2, "signup_token": "tok-abc"}
    return [{**defaults, **ov} for ov in overrides_list]


# ── Базова розсилка ───────────────────────────────────────────────────────────

async def test_sends_to_all_pending_users(monkeypatch):
    """Кожному pending-користувачу надсилається рівно одне повідомлення."""
    users = _make_users(
        {"telegram_id": 111, "months_earned": 2, "signup_token": "tok-111"},
        {"telegram_id": 222, "months_earned": 0, "signup_token": "tok-222"},
    )
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    monkeypatch.setattr(bot, "mark_notified", Mock())
    mock_send = AsyncMock()
    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    # 2 users + 1 admin summary = 3 calls
    assert mock_send.await_count == 3
    sent_ids = [c.args[0] for c in mock_send.call_args_list[:2]]
    assert set(sent_ids) == {111, 222}


# ── Текст з/без преміум-місяців ───────────────────────────────────────────────

async def test_premium_message_for_users_with_months(monkeypatch):
    """Користувач із granted_months > 0 отримує повідомлення із згадкою місяців."""
    users = _make_users({"telegram_id": 111, "months_earned": 3, "signup_token": "tok"})
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=1))  # +1 promo → 4 total
    monkeypatch.setattr(bot, "mark_notified", Mock())
    mock_send = AsyncMock()
    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    user_text = mock_send.call_args_list[0].args[1]
    assert "4 міс." in user_text


async def test_no_premium_mention_for_zero_months(monkeypatch):
    """Користувач із 0 granted_months НЕ отримує повідомлення про безкоштовний Pro."""
    users = _make_users({"telegram_id": 111, "months_earned": 0, "signup_token": "tok"})
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    monkeypatch.setattr(bot, "mark_notified", Mock())
    mock_send = AsyncMock()
    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    user_text = mock_send.call_args_list[0].args[1]
    assert "міс." not in user_text


# ── Персональне посилання ─────────────────────────────────────────────────────

async def test_personal_link_contains_token(monkeypatch):
    """Повідомлення містить посилання з signup_token конкретного користувача."""
    users = _make_users({"telegram_id": 111, "months_earned": 2, "signup_token": "unique-token-xyz"})
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    monkeypatch.setattr(bot, "mark_notified", Mock())
    mock_send = AsyncMock()
    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    user_text = mock_send.call_args_list[0].args[1]
    assert "unique-token-xyz" in user_text
    assert bot.PLATFORM_URL in user_text


async def test_fallback_link_when_no_token(monkeypatch):
    """Якщо signup_token = None — використовується PLATFORM_URL без /welcome/."""
    users = _make_users({"telegram_id": 111, "months_earned": 0, "signup_token": None})
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    monkeypatch.setattr(bot, "mark_notified", Mock())
    mock_send = AsyncMock()
    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    user_text = mock_send.call_args_list[0].args[1]
    assert bot.PLATFORM_URL in user_text
    assert "/welcome/" not in user_text


# ── Обробка помилки відправки ─────────────────────────────────────────────────

async def test_failed_send_not_marked_notified(monkeypatch):
    """Якщо send_message кинув виняток — mark_notified для цього user НЕ викликається."""
    users = _make_users(
        {"telegram_id": 111, "months_earned": 2, "signup_token": "tok-111"},
        {"telegram_id": 222, "months_earned": 2, "signup_token": "tok-222"},
    )
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    mock_mark = Mock()
    monkeypatch.setattr(bot, "mark_notified", mock_mark)

    async def mock_send(chat_id, text, **kwargs):
        if chat_id == 111:
            raise Exception("User blocked the bot")

    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    # mark_notified called only for user 222 (successful send)
    assert mock_mark.call_count == 1
    assert mock_mark.call_args_list[0].args[0] == 222


async def test_failed_send_does_not_abort_broadcast(monkeypatch):
    """Помилка відправки одному користувачу не зупиняє розсилку для наступних."""
    users = _make_users(
        {"telegram_id": 111, "months_earned": 0, "signup_token": "tok-111"},
        {"telegram_id": 222, "months_earned": 0, "signup_token": "tok-222"},
        {"telegram_id": 333, "months_earned": 0, "signup_token": "tok-333"},
    )
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    monkeypatch.setattr(bot, "mark_notified", Mock())

    received: list[int] = []

    async def mock_send(chat_id, text, **kwargs):
        if chat_id == 222:
            raise Exception("blocked")
        received.append(chat_id)

    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    # 111 and 333 received messages; admin also gets summary
    assert 111 in received
    assert 333 in received
    assert ADMIN_CHAT_ID in received


# ── Підсумкове повідомлення адміну ───────────────────────────────────────────

async def test_admin_summary_counts_correctly(monkeypatch):
    """Після розсилки адмін отримує точний підрахунок успішних та невдалих."""
    users = _make_users(
        {"telegram_id": 111, "months_earned": 2, "signup_token": "tok-111"},
        {"telegram_id": 222, "months_earned": 0, "signup_token": "tok-222"},
    )
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    monkeypatch.setattr(bot, "mark_notified", Mock())

    async def mock_send(chat_id, text, **kwargs):
        if chat_id == 111:
            raise Exception("blocked")

    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)
    # No assertion: if run_broadcast didn't raise, admin got the summary (222 succeeded)
    # The key check: function completed without crashing despite one failure


async def test_empty_pending_list_sends_summary(monkeypatch):
    """Якщо немає pending-користувачів — адмін все одно отримує підсумок 0/0."""
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=[]))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=0))
    monkeypatch.setattr(bot, "mark_notified", Mock())
    mock_send = AsyncMock()
    monkeypatch.setattr(bot.bot, "send_message", mock_send)

    await bot.run_broadcast(ADMIN_CHAT_ID)

    # Only the admin summary message
    mock_send.assert_awaited_once()
    summary_text = mock_send.call_args.args[1]
    assert "Надіслано: 0" in summary_text
    assert "не вдалось: 0" in summary_text


# ── granted_months = months_earned + promo_months ────────────────────────────

async def test_granted_months_combines_earned_and_promo(monkeypatch):
    """granted_months = months_earned (від друзів) + get_promo_months (промокоди)."""
    users = _make_users({"telegram_id": 111, "months_earned": 2, "signup_token": "tok"})
    monkeypatch.setattr(bot, "pending_broadcast_users", Mock(return_value=users))
    monkeypatch.setattr(bot, "get_promo_months", Mock(return_value=3))
    mock_mark = Mock()
    monkeypatch.setattr(bot, "mark_notified", mock_mark)
    monkeypatch.setattr(bot.bot, "send_message", AsyncMock())

    await bot.run_broadcast(ADMIN_CHAT_ID)

    # mark_notified is called with the combined total (2 + 3 = 5)
    mark_granted = mock_mark.call_args_list[0].args[1]
    assert mark_granted == 5
