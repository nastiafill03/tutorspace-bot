"""
Тести для сценарію реєстрації у waitlist (обробник /start у bot.py).

Чому саме cmd_start(), а не щось інше:
cmd_start() — це єдине місце в коді, яке приймає вхідне повідомлення від
людини і вирішує, записувати її в базу (create_user) чи ні (якщо вона вже
там є). Це "мозок" запису у waitlist, тому саме його баг найдорожчий:
якщо тут щось піде не так — можна або задублювати користувача в базі,
або (як з referral-сповіщенням) випадково написати не тій людині.

Всюди, де реальний код звертається до Supabase (get_user, create_user,
founder_count, increment_referrer) або до Telegram (bot.send_message),
ми підміняємо ці функції на моки — тест жодного разу не йде в мережу.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bot


NEW_USER_ID = 111
REFERRER_ID = 222


def make_message(user_id: int, username: str = "new_user", first_name: str = "Ірина") -> SimpleNamespace:
    """Мінімальна підробка aiogram.Message: тільки ті поля, які реально читає cmd_start()."""
    from_user = SimpleNamespace(id=user_id, username=username, first_name=first_name)
    return SimpleNamespace(from_user=from_user, answer=AsyncMock())


def make_command(args: str | None = None) -> SimpleNamespace:
    """Мінімальна підробка aiogram.CommandObject (deep-link параметр /start ref_<id>)."""
    return SimpleNamespace(args=args)


async def test_new_user_creates_waitlist_record(monkeypatch):
    # --- setup: підміняємо всі звернення до Supabase моками ---
    mock_create_user = Mock(return_value={"telegram_id": NEW_USER_ID})
    monkeypatch.setattr(bot, "get_user", Mock(return_value=None))       # користувача ще немає в базі
    monkeypatch.setattr(bot, "create_user", mock_create_user)
    monkeypatch.setattr(bot, "founder_count", Mock(return_value=0))     # місця ще є
    monkeypatch.setattr(bot, "bot_username_cache", "test_bot")          # щоб не ходити в Telegram за get_me()

    message = make_message(NEW_USER_ID, username="ira", first_name="Ірина")
    command = make_command(args=None)  # прийшла без реферального посилання

    # --- дія: людина вперше пише /start ---
    await bot.cmd_start(message, command)

    # --- перевірка: у базу записався саме цей user_id з правильними даними ---
    mock_create_user.assert_called_once_with(
        telegram_id=NEW_USER_ID,
        username="ira",
        first_name="Ірина",
        referred_by=None,
        is_founder=True,
        founder_number=1,
        months_earned=bot.BASE_MONTHS,
    )
    # і відповідь пішла тільки в чат цієї людини (один виклик message.answer)
    message.answer.assert_awaited_once()


async def test_duplicate_start_does_not_create_second_record(monkeypatch):
    # --- setup: get_user каже, що такий telegram_id вже є в базі ---
    existing_user = {
        "telegram_id": NEW_USER_ID,
        "is_founder": True,
        "founder_number": 1,
        "months_earned": 2,
        "referral_count": 0,
    }
    mock_create_user = Mock()
    monkeypatch.setattr(bot, "get_user", Mock(return_value=existing_user))
    monkeypatch.setattr(bot, "create_user", mock_create_user)
    monkeypatch.setattr(bot, "bot_username_cache", "test_bot")

    message = make_message(NEW_USER_ID)
    command = make_command(args=None)

    # --- дія: та сама людина повторно пише /start ---
    await bot.cmd_start(message, command)

    # --- перевірка: другого запису в базі НЕ створено ---
    mock_create_user.assert_not_called()
    # людині просто показали статус, знову ж — один виклик, тільки їй
    message.answer.assert_awaited_once()


async def test_referral_confirmation_goes_only_to_referrer(monkeypatch):
    # --- setup: новий користувач прийшов за посиланням реферера REFERRER_ID ---
    referrer_data = {
        "telegram_id": REFERRER_ID,
        "is_founder": True,
        "referral_count": 2,
        "months_earned": 4,
    }

    def fake_get_user(telegram_id):
        # get_user викликається двічі: за id того, хто пише, і за id реферера
        if telegram_id == REFERRER_ID:
            return referrer_data
        return None  # NEW_USER_ID ще немає в базі

    monkeypatch.setattr(bot, "get_user", fake_get_user)
    monkeypatch.setattr(bot, "create_user", Mock(return_value={}))
    monkeypatch.setattr(bot, "founder_count", Mock(return_value=5))
    monkeypatch.setattr(
        bot, "increment_referrer", Mock(return_value={"months_earned": 5, "referral_count": 3})
    )
    monkeypatch.setattr(bot, "bot_username_cache", "test_bot")
    mock_send_message = AsyncMock()
    monkeypatch.setattr(bot.bot, "send_message", mock_send_message)

    message = make_message(NEW_USER_ID)
    command = make_command(args=f"ref_{REFERRER_ID}")

    # --- дія: новий користувач реєструється за реферальним посиланням ---
    await bot.cmd_start(message, command)

    # --- перевірка: сповіщення пішло ОДИН раз і саме реферу, а не циклом по всій базі ---
    mock_send_message.assert_awaited_once()
    sent_to_chat_id = mock_send_message.call_args.args[0]
    assert sent_to_chat_id == REFERRER_ID

    # а підтвердження новому користувачу пішло через message.answer — тобто
    # прямо в той чат, звідки прийшло повідомлення, а не окремою розсилкою
    message.answer.assert_awaited_once()
