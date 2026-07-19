"""
Цей файл виконується автоматично перед усіма тестами (стандарт pytest).

bot.py під час імпорту одразу читає BOT_TOKEN / SUPABASE_URL / SUPABASE_SERVICE_KEY /
ADMIN_ID з оточення і створює реальний aiogram.Bot та supabase Client.
Якщо не підставити сюди фейкові значення ДО імпорту bot.py, тести або впадуть
з помилкою "Missing required environment variable", або (гірше) підхоплять
справжні токени з .env і теоретично зможуть постукати в реальний бот/базу.

os.environ.setdefault нічого не робить, якщо змінна вже задана (наприклад,
в CI), тому явно продакшн-значення ми ніколи не перезатираємо чужим тестовим —
але тут завжди підставляємо свої фейкові, бо .env не встиг завантажитись.
"""
import os

os.environ["BOT_TOKEN"] = "123456:TEST-fake-token-for-unit-tests-only"
os.environ["SUPABASE_URL"] = "https://example.supabase.co"
os.environ["SUPABASE_SERVICE_KEY"] = "fake.jwt.token"
os.environ["ADMIN_ID"] = "999999999"
