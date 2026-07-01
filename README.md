# TutorSpace Waitlist Bot

Telegram-бот для збору waitlist перед запуском TutorSpace, з механікою Founding Members (перші 30 місць).

## Структура проєкту

```
TutorSpaceBot/
├── bot.py            # Основний код бота
├── schema.sql         # SQL-схема таблиці Supabase (DROP + CREATE)
├── requirements.txt  # Python-залежності
├── .env.example      # Шаблон змінних середовища
├── .gitignore
├── Procfile           # Для Railway/Render
└── README.md
```

---

## 1. Створи бота в BotFather

1. Відкрий [@BotFather](https://t.me/BotFather) у Telegram.
2. Надішли `/newbot`, задай назву і username (наприклад `TutorSpaceBot`).
3. Скопіюй токен — це твій `BOT_TOKEN`.

---

## 2. Підніми базу даних у Supabase

1. Зайди на [supabase.com](https://supabase.com) → **New project**.
2. У **SQL Editor** виконай вміст файлу `schema.sql`. Він робить `DROP TABLE IF EXISTS` + створює таблицю заново — якщо в тебе вже були тестові записи зі старою схемою, вони видаляться.
3. У **Project Settings → API** скопіюй:
   - **Project URL** → `SUPABASE_URL`
   - Розділ **Legacy anon, service_role API keys** → рядок **`service_role`** (довгий JWT, `eyJhbGci...`) → `SUPABASE_SERVICE_KEY`

> Важливо: бери саме legacy `service_role` JWT, а не новий `sb_secret_...` ключ — supabase-py очікує класичний JWT-формат.

---

## 3. Заповни .env

```bash
cp .env.example .env
```

```env
BOT_TOKEN=1234567890:AAF...
SUPABASE_URL=https://xyzxyz.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGci...
ADMIN_ID=123456789                 # твій Telegram ID (дізнатись: @userinfobot)
INSTAGRAM_URL=                     # опційно; якщо порожньо — кнопка Instagram не показується
```

---

## 4. Запуск локально

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip3 install -r requirements.txt
python3 bot.py
```

Бот працює в режимі polling — публічний URL не потрібен.

---

## 5. Деплой на Railway

1. Запушти репозиторій на GitHub (`.env` НЕ комітиш — він у `.gitignore`).
2. На [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**.
3. У **Variables** додай ті ж 5 змінних, що в `.env`.
4. Railway побачить `Procfile` і запустить `python bot.py` як worker-процес.

### Деплой на Render

1. **New → Background Worker** → підключи репозиторій.
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `python bot.py`
4. Додай ті самі Environment Variables.

---

## Команди й кнопки

| Команда | Хто бачить | Опис |
|---------|-----------|------|
| `/start` | всі | Реєстрація; приймає реферальний параметр `ref_<id>` |
| `/status` | всі | Статус: founder-номер + місяці + друзі, або «у списку очікування» |
| `/stats` | тільки адмін | Усього / Founding Members / у списку очікування |

Inline-кнопки під привітанням і статусом:

- **📨 Поділитися з другом** — лише для Founding Members; відкриває нативний Telegram-шер посилання.
- **📊 Мій статус** — показує статус повторно (callback).
- **📸 Наш Instagram** — лише якщо задано `INSTAGRAM_URL`.

Додатково для адміна (`ADMIN_ID`) — постійна клавіатура внизу з кнопками:

- **👥 Waitlist** — те саме, що `/stats`.
- **🏆 Топ реферери** — топ-5 за кількістю запрошених друзів.
- **🆕 Останні реєстрації** — останні 5 хто приєднався (founder/waitlist + дата).

---

## Founding-cap механіка

- Перші **30** користувачів (`FOUNDING_LIMIT`) стають **Founding Members**: отримують `BASE_MONTHS = 2` місяці одразу.
- Після 30-го всі нові потрапляють у список очікування: `months_earned = 0`, без реферальних нарахувань.
- Кожен Founding Member має посилання `https://t.me/<bot>?start=ref_<telegram_id>`.
- Якщо новий друг заходить за посиланням і **встигає** стати Founding Member — обидва (і реферер, і новачок) отримують **+1 місяць** (`REFERRAL_MONTHS`), реферер отримує сповіщення.
- Якщо друг прийшов уже **після** заповнення 30 місць — бонус не нараховується нікому, а реферера сповіщають, що місця зайняті.
- Максимум **10 друзів** (`MAX_REFERRALS`) на одного реферера — далі без нарахувань.
- Самореферал та повторний `/start` бонусів не дають.
