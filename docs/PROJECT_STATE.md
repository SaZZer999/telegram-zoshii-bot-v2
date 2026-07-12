# PROJECT_STATE.md

Короткий знімок поточного стану проєкту — для швидкого старту нової сесії Claude Code без повторного розкопування історії чату. Детальний опис фіч і плани — `docs/PROJECT.md` / `docs/ROADMAP.md`.

## Поточна архітектура

* **Flask webhook** (`bot.py`) — один процес, синхронна обробка кожного Telegram-запиту в межах одного HTTP-виклику.
* **PostgreSQL / Supabase** через `psycopg` (`database.py`) — постійне сховище, ідемпотентні міграції при старті (`init_db()`).
* **Gemini API** — весь AI-розбір тексту й chat, прямі HTTP-запити через `requests` (без SDK).
* **Groq Whisper** — транскрипція голосових повідомлень (`voice_input.py`).
* **Render Web Service** — хостинг, деплой автоматично при push у `main`; процес запускається через `gunicorn`, не через `python bot.py` (dev-режим лишився тільки для локального запуску).

## Важливі файли

* `bot.py` — Flask-маршрути (`/`, `/health`, `/webhook/<TELEGRAM_BOT_TOKEN>`), клавіатури, промпти, pending-стан у пам'яті процесу.
* `message_dispatcher.py` — впорядкований роутинг вхідного тексту (навігація → спецкнопки → меню → pending-стани → команди → AI-фолбек).
* `database.py` — весь доступ до PostgreSQL, транзакції, stale-snapshot захист.
* `expenses.py` — домен витрат.
* `household_router.py` — Global Household Router v1 (змішані побутові команди одним Gemini-викликом).
* `preview_editing.py` — деталі нижче ("Preview Edit").
* `quantities.py` — парсинг/форматування структурованих кількостей (одна спільна реалізація для `bot.py`/`database.py`).
* `voice_input.py` — голосовий ввід (Groq Whisper).
* `photo_receipts.py` — розпізнавання чеків з фото.
* `Procfile`, `requirements.txt` — деплой на Render (gunicorn).
* `docs/PROJECT.md` — повний опис фіч. `docs/ROADMAP.md` — план розвитку. `docs/DEPLOYMENT_RUNBOOK.md` — інструкція деплою/troubleshooting.

## Поточний задеплоєний сервіс

* Репозиторій: `SaZZer999/telegram-zoshii-bot-v2`, гілка `main`.
* Render-сервіс: **`telegram-zoshii-bot-v2`** → `https://telegram-zoshii-bot-v2.onrender.com`.
* Останній підтверджено робочий коміт: `890893d fix: bind flask app to render port`.
* ⚠️ Існує старий Render-сервіс **`telegram-zoshii-bot`** (без `-v2`) — це джерело плутанини з попереднього деплою, ним **не користуватись**, він не підключений до цього репозиторію.

## Що зараз працює (підтверджено)

* `GET /health` → 200 `{"ok": true}`, без звернень до Gemini/Groq/Postgres/Telegram.
* Telegram webhook на `/webhook/<TELEGRAM_BOT_TOKEN>`, процес запущено через `gunicorn bot:app`.
* Shopping/inventory Global Household Router v1 (додати до покупок/запасів, часткове списання, витрати — одним повідомленням).
* **Preview Edit V1** — текстове редагування активного `pending_inventory_transform` прев'ю (об'єднання позицій запасів).
* **Preview Edit V2** — текстове редагування активного `pending_global_household` add-прев'ю (покупки/запаси): правка кількості за назвою, позиційне скорочення ("1 л, 500 г"), перейменування; ніколи не пише в БД до підтвердження.
* Photo Receipt Input V1 — розпізнавання чека з фото.
* Voice Input V1 — голосові повідомлення через Groq Whisper, ті самі маршрути обробки тексту, що й друковані команди.
* Останній підтверджений env-чек при старті процесу: `GROQ LOADED: True`, `GEMINI LOADED: True`, `ACCESS RESTRICTED: True`, `DATABASE READY: True`.

## Відомі обмеження / майбутні задачі

* Повноцінний Gemini Action Planner — свідомо не реалізований (див. `docs/ROADMAP.md`).
* Текстове редагування прев'ю обмежене `pending_inventory_transform` і add-частиною `pending_global_household`; expense-прев'ю, photo-receipt-прев'ю, `pending_cleanup_admin`, consume/expense-частини household-прев'ю — редагування текстом не підтримується (контрольоване повідомлення замість AI-вгадування).
* Усі pending-прев'ю живуть лише в пам'яті процесу — втрачаються при рестарті/redeploy.
* Word-number кількості ("один літр") у редагуванні прев'ю свідомо не підтримуються — тільки цифрові кількості.

## Безпека — важливо

* **Ніколи** не вставляй справжній `TELEGRAM_BOT_TOKEN` у чат, коміт, лог чи документацію — навіть якщо він видно у Render deploy logs або webhook URL з Telegram API.
* Webhook URL має форму `/webhook/<TELEGRAM_BOT_TOKEN>` — при обговоренні завжди показуй лише плейсхолдер `<TELEGRAM_BOT_TOKEN>`, ніколи реальне значення.
* `.env` ніколи не читати, не показувати, не редагувати, не комітити (правило з `CLAUDE.md`).
