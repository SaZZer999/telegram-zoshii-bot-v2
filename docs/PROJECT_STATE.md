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
* `mini_action_planner.py` — Unified Mini Action Planner V1: окремий, вузький last-resort Gemini-класифікатор (`add_to_shopping`/`add_to_inventory`/`ask_inventory`/`meal_ideas`/`unknown`), спрацьовує останнім у Phase D перед загальним AI-чатом. **Не змінювався** цією задачею.
* `action_planner.py` — Inventory Action Planner V1 (деталі нижче) — окремий від `mini_action_planner.py` модуль, інша дія, інше місце в routing.
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
* Photo Receipt Input V1/V2.2 — розпізнавання чека з фото, включно з коректними кількостями пакування (вага/об'єм замість «шт.») і категоріями позицій — див. розділ «Receipt V2.2» нижче.
* Voice Input V1 — голосові повідомлення через Groq Whisper, ті самі маршрути обробки тексту, що й друковані команди.
* Останній підтверджений env-чек при старті процесу: `GROQ LOADED: True`, `GEMINI LOADED: True`, `ACCESS RESTRICTED: True`, `DATABASE READY: True`.

## Receipt V2.2 — підтверджений стан (2026-07-13)

* Стабільний коміт: `68e7367 fix: calculate receipt package quantities and categories`.
* Стабільний тег: `stable-receipt-v2-package-categories-2026-07-13` (запушений в origin).
* Тегування нічого не змінило: робоче дерево було чистим до створення тега, схему БД, Render, webhook і env не чіпали.
* Підтверджено наживо в Telegram на реальному чеку Żabka:
  * прев'ю показало `Олія Bartek — 1 л`, `Сир Гауда — 270 г`, `Часник — 1 шт`, витрату `Żabka — 27,28 zł` (категорія «Продукти»);
  * розбір чека («покажи розбір чеку») показав виявлений розмір пакування («135 г») і коректні категорії для кожної позиції («SER GOUDA 135g» → Молочне та яйця, «CZOSNEK» → Овочі та зелень, «OLEJ BARTEK» → Інше їстівне), а «TORBA PAPIEROWA» коректно відкинуто як тару/оплату, не товар;
  * після підтвердження запаси показали `Сир Гауда — 270 г` під 🥛 Молочне та яйця та `Часник — 1 шт` під 🥦 Овочі та зелень — не під дефолтну «Інше їстівне».
* Висновок: розрахунок кількості пакування (вага/об'єм замість штук) і призначення категорій позицій чека підтверджено наживо; Receipt Debug/Explain лишається корисним інструментом і зберігається без змін.

## Відомі обмеження / майбутні задачі

* Повноцінний central Gemini Action Planner для ВСІХ побутових дій — свідомо не реалізований. Реалізовано лише перший обмежений етап — Inventory Action Planner V1 (нижче), що покриває тільки inventory-transform/merge-duplicates/rename/delete. Add/consume/expense так само йдуть через Global Household Router, read-запити — через `household_read_context.py`/`mini_action_planner.py`, як і раніше (див. `docs/ROADMAP.md`).
* Текстове редагування прев'ю обмежене `pending_inventory_transform` і add-частиною `pending_global_household`; expense-прев'ю, photo-receipt-прев'ю, `pending_cleanup_admin`, consume/expense-частини household-прев'ю — редагування текстом не підтримується (контрольоване повідомлення замість AI-вгадування).
* Усі pending-прев'ю живуть лише в пам'яті процесу — втрачаються при рестарті/redeploy.
* Word-number кількості ("один літр") у редагуванні прев'ю свідомо не підтримуються — тільки цифрові кількості.

## Inventory Delete Quantity-Match — підтверджено наживо в Telegram (2026-07-14)

* Баг видалення запасів за природною фразою кількості (`inventory.parse_inventory_delete_request`) виправлено: розпізнаються `одна штука`/`одну штуку`/`одне`/`одна` перед назвою, а числовий hint (`1 шт`, `14,5 л` тощо) звіряється з `quantity_text` конкретного рядка запасів, щоб вибрати саме потрібний запис серед кількох однойменних.
* Трейлінг-пояснення після кількості («воно вже не потрібно», «це вже не треба», «більше не треба», «закінчилось», а також довільне `, бо ...`) відсікається до розбору назви й кількості, тому не потрапляє в назву товару.
* Підтверджені приклади (regression-тести в `tests/test_inventory_delete_quantity_match.py`): «Видали молоко одна штука, воно вже не потрібно» і «Видали молоко одна штука, бо воно зіпсувалося» коректно вибирають `Молоко — 1 шт`, а не `Молоко — 14,5 л`; без кількості при двох записах спрацьовує існуюче уточнення (disambiguation), preview/confirm/cancel і stale-snapshot захист не змінені.
* **Підтверджено наживо в Telegram** (стабільний коміт `6b19cad fix: parse natural quantities in inventory deletion`): при одночасних записах `Молоко — 1 шт` і `Молоко — 14,5 л` команда «Видали молоко одна штука, воно вже не потрібно» показала preview саме для `Молоко — 1 шт`; скасування нічого не змінило; повторна команда з підтвердженням видалила лише `Молоко — 1 шт`, а `Молоко — 14,5 л` залишилося без змін.

## Inventory Action Planner V1 — підтверджено наживо в Telegram (2026-07-14)

* Новий окремий модуль **`action_planner.py`** — «Inventory Action Planner V1» / «Action Planner V1 for inventory administration». **Це НЕ `mini_action_planner.py`** (Unified Mini Action Planner V1 — `add_to_shopping`/`add_to_inventory`/`ask_inventory`/`meal_ideas`/`unknown`, last-resort у Phase D) — `mini_action_planner.py` цією задачею **не змінювався** і не замінений, лишається окремим модулем з окремою роллю, окремим dispatcher-слотом і власними тестами (`test_mini_action_planner_module.py`, `test_mini_action_planner_routing.py`).
* Призначення: `action_planner.py` розпізнає unresolved inventory-команди (transform кількох різних позицій у нову, об'єднання дублікатів однієї позиції, перейменування, видалення), які існуючі детерміновані regex-парсери (`inventory.parse_inventory_transform_request`/`parse_inventory_cleanup_request`/`parse_inventory_rename_request`/`parse_inventory_delete_request`) не змогли розпізнати. Це **не** повноцінний central Household Action Planner — add/consume/expense/read/meal-ideas і далі йдуть через Global Household Router/`household_read_context.py`/`mini_action_planner.py` без змін.
* Action allowlist (V1): `inventory_transform`, `inventory_merge_duplicates`, `inventory_rename`, `inventory_delete`, `clarify`, `unsupported`.
* Позиція в routing: **після** `inventory_transform_route` → `inventory_cleanup_route` → `inventory_admin_route`, **перед** `saved_list_router`, **не** перед `household_router.gate()` — pending-state (active preview) має пріоритет над усім routing planner-а.
* **Підтверджено наживо в Telegram** (коміт `6e259b3 feat: add inventory action planner`):
  * стрілкова форма «сосиски + мисливські ковбаски → м'ясні вироби» — правильний transform-preview (прибрати `Сосиски — 6 шт`, прибрати `Мисливські ковбаски — 2 шт`, додати `М'ясні вироби — 8 шт`, попередження про втрату інформації про окремі вихідні товари); `❌ Скасувати` — зміни не застосовані;
  * природна форма «В запасах об'єднай сосиски і мисливські ковбаски і запиши як м'ясні вироби» — той самий коректний transform-preview; `✅ Так, застосувати` — обидві вихідні позиції видалено, створено `М'ясні вироби — 8 шт`, інші записи запасів не змінилися;
  * pending-state priority підтверджено: поки transform-preview активний, нове голосове повідомлення НЕ маршрутизувалося як окрема household-операція — бот лишився в active preview flow і попросив підтвердити/скасувати/уточнити;
  * додатково підтверджено: `Назад` відкриває головне меню без звернення до Gemini; звичайне загальне питання й далі йде в general AI-chat.
* **Відоме обмеження (не підтверджено, окремий майбутній UX-кейс):** голосове редагування активного transform-preview може не спрацювати — під час перевірки Whisper розпізнав фразу неточно, і редагування плану голосом не відбулося (через неточну транскрипцію або недостатньо гнучкий Preview Edit parser). Duplicate-merge (`inventory_merge_duplicates`), rename і delete через новий planner окремо в Telegram ще не перевірені.

## Безпека — важливо

* **Ніколи** не вставляй справжній `TELEGRAM_BOT_TOKEN` у чат, коміт, лог чи документацію — навіть якщо він видно у Render deploy logs або webhook URL з Telegram API.
* Webhook URL має форму `/webhook/<TELEGRAM_BOT_TOKEN>` — при обговоренні завжди показуй лише плейсхолдер `<TELEGRAM_BOT_TOKEN>`, ніколи реальне значення.
* `.env` ніколи не читати, не показувати, не редагувати, не комітити (правило з `CLAUDE.md`).
