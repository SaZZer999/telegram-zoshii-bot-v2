# DEPLOYMENT_RUNBOOK.md

Практична інструкція для деплою/діагностики цього бота на Render. Поточний стан фіч — `docs/PROJECT_STATE.md`.

## Render-сервіс (правильний)

* Репозиторій: `SaZZer999/telegram-zoshii-bot-v2`, гілка `main`.
* Render service name: **`telegram-zoshii-bot-v2`**.
* URL: `https://telegram-zoshii-bot-v2.onrender.com`.
* ⚠️ Старий сервіс **`telegram-zoshii-bot`** (без `-v2`) існує в Render, не підключений до цього репо — джерело плутанини з попереднього деплою, **не використовувати**.

## Build / Start команди

* **Build Command:** `pip install -r requirements.txt`
* **Start Command:** `gunicorn bot:app --bind 0.0.0.0:$PORT`
* У корені репозиторію є `Procfile` з тим самим рядком (`web: gunicorn bot:app --bind 0.0.0.0:$PORT`) як fallback/документація — але явний Start Command у Render dashboard має пріоритет над `Procfile`, тому саме дашборд-налаштування вирішальне.
* `requirements.txt` має містити `gunicorn` (доданий у `890893d fix: bind flask app to render port`).

## Health check

* `GET /health` → 200, тіло `{"ok": true}`.
* Не викликає Gemini, Groq, Postgres/Supabase чи Telegram — безпечний для пінгів кожні 5–10 хв.

## UptimeRobot

* Monitor URL: `https://telegram-zoshii-bot-v2.onrender.com/health`
* Інтервал: 5–10 хв.

## Webhook — формат без реального токена

Flask реєструє маршрут **`/webhook/<TELEGRAM_BOT_TOKEN>`** (не голий `/webhook`). Реальний токен ніколи не вставляти в чат/коміт/документацію — нижче скрізь плейсхолдер.

Правильний webhook URL:
```
https://telegram-zoshii-bot-v2.onrender.com/webhook/<TELEGRAM_BOT_TOKEN>
```

Встановити webhook (виконати самостійно з реальним токеном, не в чаті з асистентом):
```
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://telegram-zoshii-bot-v2.onrender.com/webhook/<TELEGRAM_BOT_TOKEN>
```

Перевірити поточний webhook (так само без токена в чаті):
```
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo
```

## Smoke test checklist після деплою

1. `GET /health` → 200 `{"ok": true}`.
2. Голосом: «Додай молоко і сир до покупок.» → з'являється прев'ю покупок.
3. Під час прев'ю текстом/голосом: «молока 1 л, а сиру 500 г» → «Оновив план:» з оновленими кількостями.
4. «1L, 500g» → те саме мапиться за порядком позицій.
5. ❌ Скасувати → нічого не додано.
6. Повторити, відредагувати, ✅ Так, застосувати → товари додані з відредагованими кількостями.
7. Надіслати фото чека → photo receipt прев'ю працює.
8. Голосом: «Що можна приготувати на вечерю з того, що є вдома?» → meal ideas.
9. Голосом: «Поясни, чому молоко згортається у каві?» → загальний AI-чат.

## Troubleshooting

### Wrong Render account / service
**Симптом:** деплой виглядає успішним, але зміни не з'являються, або env vars незнайомі.
**Перевірити:** URL у браузері = `https://telegram-zoshii-bot-v2.onrender.com`; у Render dashboard service name = `telegram-zoshii-bot-v2`, а не старий `telegram-zoshii-bot`.

### Wrong repo connected
**Симптом:** Render deploy log показує commit SHA, якого немає в `git log --all` цього репозиторію; встановлені пакети не збігаються з `requirements.txt` (наприклад, з'являється `python-telegram-bot`, якого тут ніколи не було).
**Перевірити:** `git ls-remote origin main` — SHA має збігатися з тим, що деплоїть Render. Render dashboard → Settings → Build & Deploy → GitHub repo = `SaZZer999/telegram-zoshii-bot-v2`, branch `main`. При розбіжності — Disconnect і Reconnect до правильного репозиторію.

### Wrong Build vs Start command
**Симптом:** build-логи не встановлюють `gunicorn`, або Start Command падає з `gunicorn: command not found`.
**Перевірити:** Build Command = `pip install -r requirements.txt` (і саме той коміт, що деплоїться, має `gunicorn` у `requirements.txt`); Start Command = `gunicorn bot:app --bind 0.0.0.0:$PORT`.

### `/webhook` 404 — route потребує токен
**Симптом:** Render логи показують `"POST /webhook HTTP/1.1" 404`.
**Причина:** Flask реєструє тільки `/webhook/<TELEGRAM_BOT_TOKEN>`, не голий `/webhook`. Telegram webhook був встановлений без токен-сегмента в URL.
**Виправлення:** викликати `setWebhook` з повним URL, що включає токен-сегмент (див. розділ "Webhook — формат без реального токена" вище).
