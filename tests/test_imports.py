"""
Smoke-тест: bot.py має імпортуватись без винятків, якщо підставити фейкові
обов'язкові env-змінні. Ловить банальні, але дорогі помилки (відсутній
імпорт, синтаксична помилка, помилка ініціалізації на рівні модуля) ще на
етапі тестів — а не тоді, коли бот одразу падає після деплою на Railway.
"""
from __future__ import annotations

import importlib
import sys


def test_bot_module_imports_with_fake_env(monkeypatch):
    """bot.py успішно завантажується з фейковими env-змінними."""
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST-fake-token-for-smoke-test")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake.jwt.token")
    monkeypatch.setenv("ADMIN_ID", "999999999")
    monkeypatch.setenv("PLATFORM_URL", "https://test.example.com")

    sys.modules.pop("bot", None)
    fresh_bot = importlib.import_module("bot")

    assert fresh_bot.ADMIN_ID == 999999999
    assert fresh_bot.PLATFORM_URL == "https://test.example.com"
    assert fresh_bot.dp is not None
    assert fresh_bot.db is not None
    assert callable(fresh_bot.run_broadcast)
    assert callable(fresh_bot.get_promo_months)
