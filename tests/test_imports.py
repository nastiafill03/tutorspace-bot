"""
Smoke-тест: bot.py має імпортуватись без винятків, якщо підставити фейкові
обов'язкові env-змінні. Ловить банальні, але дорогі помилки (відсутній
імпорт, синтаксична помилка, помилка ініціалізації на рівні модуля) ще на
етапі тестів — а не тоді, коли бот одразу падає після деплою на Railway.

Модуль bot.py вже імпортований раніше (test_promo.py / test_signup.py
роблять `import bot` під час збору тестів pytest, коли conftest.py вже
підставив свої фейкові env-змінні), тому тут ми примусово прибираємо його
з sys.modules і імпортуємо наново зі своїми власними (monkeypatch) значеннями —
це і є справжня перевірка "чи модуль взагалі завантажується з нуля".
Той факт, що інші тестові файли вже тримають власне посилання на старий
об'єкт модуля (зафіксоване на момент їхнього імпорту), означає, що це
перезавантаження їх не зачіпає.
"""
from __future__ import annotations

import importlib
import sys


def test_bot_module_imports_with_fake_env(monkeypatch):
    """bot.py успішно завантажується (import + ініціалізація Bot/Supabase-клієнта) з фейковими env."""
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST-fake-token-for-smoke-test")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake.jwt.token")
    monkeypatch.setenv("ADMIN_ID", "999999999")

    sys.modules.pop("bot", None)
    fresh_bot = importlib.import_module("bot")

    # module-level стан ініціалізувався коректно з підставлених env-змінних
    assert fresh_bot.ADMIN_ID == 999999999
    assert fresh_bot.dp is not None
    assert fresh_bot.db is not None

    # ключові об'єкти для промокодів на місці (не забули імпорт FSM тощо)
    assert fresh_bot.PromoStates.waiting_for_code is not None
    assert callable(fresh_bot.validate_and_activate_promo)
