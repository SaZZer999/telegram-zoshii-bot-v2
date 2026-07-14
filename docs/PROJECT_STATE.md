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

## Inventory Action Planner V1 — не перевірено наживо (2026-07-14)

* Новий окремий модуль **`action_planner.py`** — «Inventory Action Planner V1» / «Action Planner V1 for inventory administration». **Це НЕ `mini_action_planner.py`** (Unified Mini Action Planner V1 — `add_to_shopping`/`add_to_inventory`/`ask_inventory`/`meal_ideas`/`unknown`, last-resort у Phase D) — `mini_action_planner.py` цією задачею **не змінювався**, лишається окремим модулем з окремим dispatcher-слотом і власними тестами (`test_mini_action_planner_module.py`, `test_mini_action_planner_routing.py`).
* Призначення: розпізнає природні inventory-команди (об'єднання кількох різних позицій у нову, об'єднання дублікатів однієї позиції, перейменування, видалення), які існуючі детерміновані regex-парсери (`inventory.parse_inventory_transform_request`/`parse_inventory_cleanup_request`/`parse_inventory_rename_request`/`parse_inventory_delete_request`) не змогли розпізнати.
* Action allowlist (V1): `inventory_transform`, `inventory_merge_duplicates`, `inventory_rename`, `inventory_delete`, `clarify`, `unsupported`. Свідомо НЕ включає `delegate_global_household`/`navigation_back`/`general_chat`/add-to-shopping/add-to-inventory/consume/expense/meal-ideas/ask-inventory — ці вже мають власні робочі маршрути раніше в ланцюжку.
* Позиція в routing (`message_dispatcher.CommandRouteDeps.action_planner_route`, wired через `bot._try_action_planner`): **після** `inventory_transform_route` → `inventory_cleanup_route` → `inventory_admin_route`, **перед** `saved_list_router`. Тобто: старі детерміновані формулювання лишаються безкоштовними (жодного Gemini-виклику), planner отримує лише те, що ці три гейти не змогли обробити, і встигає спрацювати раніше за broad AI `saved_list_router`. Planner НЕ стоїть перед `household_router.gate()` — жодного подвійного Gemini-виклику для звичайних add/consume/expense команд.
* Cheap pre-gate `action_planner.looks_like_inventory_admin_or_transform` — не викликає Gemini для повідомлень без ознак transform/merge/rename/delete (стрілка `→`/`+`, «запиши як»/«назви як/це», дієслівні корені «об'єдна»/«перейменуй»/«видали»/«прибери»/«забери»); не перехоплює звичайне редагування кількості/категорії збереженого списку (`saved_list_router` лишається незмінним).
* Точковий guard у `inventory.py` (`_looks_like_transform_shape`, викликається з `parse_inventory_cleanup_request`) — cleanup-merge більше не перехоплює повідомлення, яке явно має форму transform кількох позицій у нову (стрілка/плюс/«запиши як»/«назви як/це»/«перетвори X на Y»/«в одну позицію»); звичайні duplicate-merge команди («Об'єднай молоко», «Об'єднай усі записи молока», «Об'єднай дублікати молока в запасах») не зачеплені.
* Деталі виявлених причин обох живих багів, JSON-схема, Python-валідація (жодних DB id/SQL/довільних полів, `source_names` мінімум 2/максимум 10) — див. звіт цієї задачі в історії розмови; коротко: `сосиски + мисливські ковбаски → м'ясні вироби` і `В запасах об'єднай сосиски і мисливські ковбаски і запиши як м'ясні вироби` тепер коректно розпізнаються як `inventory_transform`, а не потрапляють у cleanup-merge/`saved_list_router`.
* DB write можливий лише через існуючі `_start_inventory_transform`/`_start_inventory_cleanup`/`_start_inventory_rename`/`_start_inventory_delete` → `database.execute_inventory_transform`/`execute_inventory_cleanup_merge`/`execute_inventory_rename`/`execute_inventory_delete` (той самий stale-snapshot захист, ті самі pending-словники, підтверджений undo) — planner ніколи не пише в БД напряму.
* Додано deterministic alias **`Назад`/`назад`** (`message_dispatcher._dispatch_navigation`) — виконує ту саму дію, що й кнопка «⬅️ Головне меню», без жодного звернення до Gemini.
* **Не підтверджено наживо в Telegram** — перед оголошенням live-milestone потрібна ручна перевірка обох сценаріїв transform, duplicate-merge, rename, delete та навігації `Назад` з реальними запасами.

## Безпека — важливо

* **Ніколи** не вставляй справжній `TELEGRAM_BOT_TOKEN` у чат, коміт, лог чи документацію — навіть якщо він видно у Render deploy logs або webhook URL з Telegram API.
* Webhook URL має форму `/webhook/<TELEGRAM_BOT_TOKEN>` — при обговоренні завжди показуй лише плейсхолдер `<TELEGRAM_BOT_TOKEN>`, ніколи реальне значення.
* `.env` ніколи не читати, не показувати, не редагувати, не комітити (правило з `CLAUDE.md`).
