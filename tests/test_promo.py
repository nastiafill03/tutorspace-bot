"""
Тести для функціоналу активації промокодів у bot.py.

Основна логіка перевірок (чи існує код, чи вже активований, ліміти, запис
активації) винесена в bot.validate_and_activate_promo(telegram_id, raw_code) —
чисту async-функцію без жодних Telegram-об'єктів. Це навмисний рефакторинг
(див. коментар над функцією в bot.py): process_promo_code як aiogram-хендлер
залежить від Message/FSMContext, які незручно й неінформативно підробляти
заради перевірки самої бізнес-логіки. Тому більшість тестів тут викликають
validate_and_activate_promo напряму, а окремі тести — перевіряють, що хендлери
(cb_activate_promo, cmd_promo, process_promo_code) правильно керують FSM-станом
і викликають цю функцію.

Як запустити:
    pip install -r requirements-dev.txt
    pytest tests/test_promo.py -v
(або просто `pytest -v` з кореня репозиторію — pytest.ini уже налаштований
на asyncio_mode = auto, тому `async def test_...` запускаються без додаткових
декораторів).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import bot


USER_ID = 111
CODE = "SPACE2M"


def make_active_promo(**overrides) -> dict:
    """Валідний спільний (не одноразовий) промокод без ліміту — базовий фікстур-словник."""
    promo = {
        "code": CODE,
        "bonus_months": 2,
        "is_active": True,
        "is_single_use": False,
        "used_by": None,
        "used_at": None,
        "max_activations": None,
        "activations_count": 0,
    }
    promo.update(overrides)
    return promo


def make_message(user_id: int, text: str | None) -> SimpleNamespace:
    """Мінімальна підробка aiogram.Message: тільки поля, які реально читає process_promo_code()."""
    from_user = SimpleNamespace(id=user_id)
    return SimpleNamespace(from_user=from_user, text=text, answer=AsyncMock())


def make_callback(user_id: int) -> SimpleNamespace:
    """Мінімальна підробка aiogram.CallbackQuery для кнопки 'activate_promo'."""
    from_user = SimpleNamespace(id=user_id)
    message = SimpleNamespace(answer=AsyncMock())
    return SimpleNamespace(from_user=from_user, message=message, data=bot.ACTIVATE_PROMO_CALLBACK, answer=AsyncMock())


def make_state() -> SimpleNamespace:
    """Мінімальна підробка aiogram.fsm.context.FSMContext — тільки set_state/clear."""
    return SimpleNamespace(set_state=AsyncMock(), clear=AsyncMock())


def make_db_mock() -> Mock:
    """Підробка supabase-клієнта bot.db для тестів, що перевіряють сам record_activation.

    Ланцюжок .table(...).insert(...).execute() / .table(...).update(...).eq(...).execute()
    повертає ту саму дочірню Mock-структуру незалежно від імені таблиці — для наших
    перевірок цього достатньо, бо ми дивимось на call_args (з яким payload/таблицею
    її викликали), а не на реальну персистентність.
    """
    db = Mock()
    db.table.return_value.insert.return_value.execute.return_value = SimpleNamespace(data=[{"id": 1}])
    db.table.return_value.update.return_value.eq.return_value.execute.return_value = SimpleNamespace(data=[{}])
    return db


# ── Валідний код — успіх ───────────────────────────────────────────────────────

async def test_valid_code_activates(monkeypatch):
    """Активний, не одноразовий код, ще не активований цим юзером → успіх і виклик record_activation."""
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=make_active_promo()))
    monkeypatch.setattr(bot, "get_activation", Mock(return_value=None))
    mock_record = Mock(return_value={})
    monkeypatch.setattr(bot, "record_activation", mock_record)

    success, text = await bot.validate_and_activate_promo(USER_ID, CODE)

    assert success is True
    assert "місяці" in text or "місяц" in text  # текст згадує нараховані місяці
    mock_record.assert_called_once_with(USER_ID, CODE, 2)


# ── Захист від повторної активації ─────────────────────────────────────────────

async def test_already_activated_by_same_user(monkeypatch):
    """Якщо ця людина вже активувала цей код раніше — повторна активація забороняється."""
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=make_active_promo()))
    monkeypatch.setattr(bot, "get_activation", Mock(return_value={"id": 1, "telegram_id": USER_ID, "code": CODE}))
    mock_record = Mock()
    monkeypatch.setattr(bot, "record_activation", mock_record)

    success, text = await bot.validate_and_activate_promo(USER_ID, CODE)

    assert success is False
    assert "вже активував" in text
    mock_record.assert_not_called()


# ── Неіснуючий / неактивний код ────────────────────────────────────────────────

async def test_nonexistent_code(monkeypatch):
    """Код, якого немає в базі — коректна відмова, без звернень до record_activation."""
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=None))
    mock_record = Mock()
    monkeypatch.setattr(bot, "record_activation", mock_record)

    success, text = await bot.validate_and_activate_promo(USER_ID, "NOSUCHCODE")

    assert success is False
    assert "не існує" in text
    mock_record.assert_not_called()


async def test_inactive_code(monkeypatch):
    """Код існує в базі, але is_active=False — має оброблятись так само, як неіснуючий."""
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=make_active_promo(is_active=False)))
    mock_record = Mock()
    monkeypatch.setattr(bot, "record_activation", mock_record)

    success, text = await bot.validate_and_activate_promo(USER_ID, CODE)

    assert success is False
    assert "не існує" in text or "не діє" in text
    mock_record.assert_not_called()


# ── Одноразовий код ─────────────────────────────────────────────────────────────

async def test_single_use_already_used(monkeypatch):
    """is_single_use=True і used_by уже заповнено — код більше не можна активувати нікому."""
    promo = make_active_promo(is_single_use=True, used_by=222)
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=promo))
    monkeypatch.setattr(bot, "get_activation", Mock(return_value=None))
    mock_record = Mock()
    monkeypatch.setattr(bot, "record_activation", mock_record)

    success, text = await bot.validate_and_activate_promo(USER_ID, CODE)

    assert success is False
    assert "вже використано" in text
    mock_record.assert_not_called()


def test_single_use_marks_used(monkeypatch):
    """record_activation() для одноразового коду має оновити promo_codes: used_by, is_active=False.

    Тут навмисно викликаємо саму record_activation() (не через validate_and_activate_promo),
    бо саме вона відповідає за апдейт в promo_codes — це і є та частина, яку треба перевірити.
    """
    promo = make_active_promo(is_single_use=True, used_by=None, activations_count=0)
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=promo))
    db_mock = make_db_mock()
    monkeypatch.setattr(bot, "db", db_mock)

    bot.record_activation(USER_ID, CODE, 2)

    # insert у promo_activations стався з правильними даними
    insert_payload = db_mock.table.return_value.insert.call_args.args[0]
    assert insert_payload["telegram_id"] == USER_ID
    assert insert_payload["code"] == CODE
    assert insert_payload["bonus_months"] == 2

    # update у promo_codes позначив код використаним саме цим юзером
    update_payload = db_mock.table.return_value.update.call_args.args[0]
    assert update_payload["used_by"] == USER_ID
    assert update_payload["is_active"] is False
    assert "used_at" in update_payload
    db_mock.table.return_value.update.return_value.eq.assert_called_with("code", CODE)


# ── Ліміт активацій спільного коду ─────────────────────────────────────────────

async def test_shared_code_limit_reached(monkeypatch):
    """activations_count >= max_activations для спільного коду → відмова, без нового запису."""
    promo = make_active_promo(is_single_use=False, activations_count=5, max_activations=5)
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=promo))
    monkeypatch.setattr(bot, "get_activation", Mock(return_value=None))
    mock_record = Mock()
    monkeypatch.setattr(bot, "record_activation", mock_record)

    success, text = await bot.validate_and_activate_promo(USER_ID, CODE)

    assert success is False
    assert "ліміт" in text.lower() or "вичерпано" in text.lower()
    mock_record.assert_not_called()


async def test_shared_code_under_limit(monkeypatch):
    """activations_count < max_activations → успіх, і validate_and_activate_promo викликає record_activation."""
    promo = make_active_promo(is_single_use=False, activations_count=3, max_activations=5)
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=promo))
    monkeypatch.setattr(bot, "get_activation", Mock(return_value=None))
    mock_record = Mock(return_value={})
    monkeypatch.setattr(bot, "record_activation", mock_record)

    success, _text = await bot.validate_and_activate_promo(USER_ID, CODE)

    assert success is True
    mock_record.assert_called_once_with(USER_ID, CODE, promo["bonus_months"])


def test_shared_code_increments_counter(monkeypatch):
    """record_activation() для спільного коду має інкрементувати activations_count в promo_codes.

    Так само, як test_single_use_marks_used, перевіряємо саму record_activation(),
    бо це та функція, яка формує запит на оновлення лічильника.
    """
    promo = make_active_promo(is_single_use=False, activations_count=3, max_activations=5)
    monkeypatch.setattr(bot, "get_promo_code", Mock(return_value=promo))
    db_mock = make_db_mock()
    monkeypatch.setattr(bot, "db", db_mock)

    bot.record_activation(USER_ID, CODE, 2)

    update_payload = db_mock.table.return_value.update.call_args.args[0]
    assert update_payload == {"activations_count": 4}
    db_mock.table.return_value.update.return_value.eq.assert_called_with("code", CODE)


# ── Нормалізація вводу ──────────────────────────────────────────────────────────

async def test_code_normalization(monkeypatch):
    """' space2m ' (пробіли, нижній регістр) має розпізнаватись як код SPACE2M."""
    mock_get_promo = Mock(return_value=make_active_promo())
    monkeypatch.setattr(bot, "get_promo_code", mock_get_promo)
    monkeypatch.setattr(bot, "get_activation", Mock(return_value=None))
    monkeypatch.setattr(bot, "record_activation", Mock(return_value={}))

    success, _text = await bot.validate_and_activate_promo(USER_ID, "  space2m  ")

    assert success is True
    mock_get_promo.assert_called_once_with(CODE)


# ── FSM-потік ────────────────────────────────────────────────────────────────────

async def test_activate_promo_sets_state():
    """Натискання кнопки 'activate_promo' встановлює стан очікування коду і просить його ввести."""
    callback = make_callback(USER_ID)
    state = make_state()

    await bot.cb_activate_promo(callback, state)

    state.set_state.assert_awaited_once_with(bot.PromoStates.waiting_for_code)
    callback.message.answer.assert_awaited_once()
    prompt_text = callback.message.answer.call_args.args[0]
    assert "код" in prompt_text.lower()
    callback.answer.assert_awaited_once()


async def test_promo_command_sets_state():
    """Команда /promo так само встановлює стан очікування коду і надсилає підказку."""
    message = make_message(USER_ID, text="/promo")
    state = make_state()

    await bot.cmd_promo(message, state)

    state.set_state.assert_awaited_once_with(bot.PromoStates.waiting_for_code)
    message.answer.assert_awaited_once()
    prompt_text = message.answer.call_args.args[0]
    assert "код" in prompt_text.lower()


# ── Стан очищається завжди ───────────────────────────────────────────────────────

async def test_state_cleared_after_each_outcome(monkeypatch):
    """process_promo_code() має очищати FSM-стан і при успіху, і при кожній відмові —
    інакше бот "зависає" в очікуванні коду після невдалої спроби.
    """
    outcomes = [
        (True, "🎉 Промокод активовано! Тобі нараховано 2 місяці Pro."),
        (False, "❌ Такого промокоду не існує або він більше не діє."),
        (False, "ℹ️ Ти вже активував цей промокод раніше."),
        (False, "❌ Цей код вже використано."),
        (False, "❌ Ліміт активацій цього промокоду вичерпано."),
    ]

    for success, text in outcomes:
        mock_validate = AsyncMock(return_value=(success, text))
        monkeypatch.setattr(bot, "validate_and_activate_promo", mock_validate)

        message = make_message(USER_ID, text="SOMECODE")
        state = make_state()

        await bot.process_promo_code(message, state)

        state.clear.assert_awaited_once()
        message.answer.assert_awaited_once_with(text)
