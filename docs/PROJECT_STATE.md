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
* `shopping_action_planner.py` — Shopping Action Planner V1 (деталі нижче) — окремий від `action_planner.py`/`mini_action_planner.py` модуль, лише `shopping_delete`/`shopping_mark_bought`, спрацьовує поза shopping mode/saved-list context.
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
* **Відоме обмеження, знайдене наживо:** голосове редагування активного transform-preview природною фразою («Запиши просто як м'ясо 2 штуки», спотворене Whisper на «Запаши...») не спрацьовувало — Preview Edit V1 приймав лише вузький список детермінованих формулювань і відповідав загальним «У тебе є незавершений план змін...». **Виправлено нижче — див. розділ «Preview Edit Planner V2 (pending_inventory_transform)».** Duplicate-merge (`inventory_merge_duplicates`), rename і delete через `action_planner.py` окремо в Telegram ще не перевірені.

## Preview Edit Planner V2 (pending_inventory_transform) — підтверджено наживо в Telegram (2026-07-14)

* Виправляє знайдений наживо баг: під час активного `pending_inventory_transform` preview команда «Запиши просто як м'ясо 2 штуки» (і спотворений Whisper-транскрипт «Запаши просто як м'ясо 2 штуки.») не розпізнавалися вузьким детермінованим Preview Edit V1 parser-ом і отримували загальну відповідь «У тебе є незавершений план змін...». Routing уже працював правильно (active `pending_inventory_transform` мав пріоритет, `action_planner.py` не перехоплював повідомлення, DB write не відбувався) — проблема була лише в самому парсері редагування.
* Реалізовано в `preview_editing.py`, новий розділ «PREVIEW EDIT PLANNER V2» (функції `classify_inventory_transform_preview_edit`/`preview_edit_plan_to_patch`/`looks_like_transform_preview_edit_attempt`), підключений через `bot._handle_inventory_transform_edit_text`. **Це не `preview_edit_planner.py`** (окремий, вже існуючий модуль — семантичний fallback для правок `pending_global_household`, rename/quantity/amount патчі) і **не** `action_planner.py`/`mini_action_planner.py` (вони створюють НОВІ дії/операції) — новий код може змінювати лише target-частину (назву/кількість) вже активного `pending_inventory_transform`, ніколи source-позиції, ніколи сам не підтверджує/не скасовує план, ніколи не пише в БД.
* Flow: deterministic `parse_inventory_transform_edit` (Preview Edit V1) завжди запускається першим і лишається безкоштовним fast path для всіх раніше підтримуваних формулювань (regression-тести підтверджують: жодного Gemini-виклику для вже працюючих форм). Лише коли він повертає no-match — і дешевий pre-gate `looks_like_transform_preview_edit_attempt` (не порожній текст, не довша за ~300 символів) погоджується, що це схоже на спробу редагування, — робиться ОДИН Gemini-виклик (`classify_inventory_transform_preview_edit`). Python-валідація (allowlist дій `set_target_name`/`set_target_quantity`/`set_target_name_and_quantity`/`clarify`/`unsupported`, суворий allowlist полів на дію, назва й одиниця/кількість через `quantities.parse_structured_quantity` з відхиленням нульових/від'ємних значень, максимальна довжина назви/уточнення) — будь-яка помилка (invalid JSON, timeout, unknown action, зайве поле на кшталт DB id/SQL/executor-назви) безпечно колапсує в `unsupported`. Валідований результат конвертується (`preview_edit_plan_to_patch`) у той самий patch-формат, який Preview Edit V1 вже застосовує (`apply_inventory_transform_patch`) — жодної дубльованої mutation-логіки; змінюється лише сам `pending_inventory_transform` (source item IDs і stale-snapshot інформація не чіпаються), DB write можливий лише після окремого confirm.
* Невдалий Gemini fallback показує конкретне повідомлення `PREVIEW_EDIT_PLANNER_UNSUPPORTED_MSG` («Не зрозумів, що змінити в плані. Напиши, наприклад: «Назви результат М'ясо» або «Зроби 2 шт».») замість загального «У тебе є незавершений план змін...» — preview і pending-стан лишаються без змін, DB не чіпається.
* Нова побутова дія («Додай молоко до покупок») під час активного transform-preview НЕ створює окрему операцію — `action_planner.py`/Global Household Router/general AI-chat/`saved_list_router` не отримують шансу, поки preview активний; Gemini fallback сам класифікує таке повідомлення як `unsupported`.
* Voice transcript іде тим самим `message_dispatcher.dispatch()` шляхом, що й друкований текст — окремої voice-специфічної логіки не додано, `voice_input.py`/Groq/Whisper не змінювались.
* **Підтверджено наживо в Telegram** (коміт `bb2b247 feat: add AI fallback for transform preview edits`): при активному transform-preview (прибрати `Сосиски — 6 шт`, прибрати `Мисливські ковбаски — 2 шт`, додати `М'ясні вироби — 8 шт`) команда «Запиши просто як м'ясо 2 штуки» коректно оновила лише pending-preview — вихідні позиції лишилися тими самими, нова цільова назва `М'ясо`, нова цільова кількість `2 шт`, до підтвердження БД не змінилася; `❌ Скасувати` лишило вихідні позиції в запасах без змін і не створило відредагований результат; після повторного створення preview й того самого редагування `✅ Так, застосувати` застосувало саме нову назву й кількість (старі target-значення не використані), а вихідні записи запасів змінилися лише після confirm.
* Голосовий сценарій, окремі форми («Назви результат м'ясні продукти», «Зроби 4 штуки», «М'ясо, 2 шт») і поведінка на незрозумілий текст **окремо в Telegram ще не перевірені** — не оголошувати підтвердженими до окремої ручної перевірки.
* **Текстове AI-редагування (Gemini fallback) підтримується лише для `pending_inventory_transform`.** Expense-прев'ю, photo-receipt-прев'ю, `pending_cleanup_admin` (rename/delete), consume-частина household-прев'ю та інші pending-стани й далі не підтримують текстове редагування (контрольоване повідомлення замість AI-вгадування) — без змін цією задачею.

## Expense-delete natural-language routing — не перевірено наживо (2026-07-14)

* `expenses._expense_delete_command_gate` розширено: тепер приймає природні посилання на попередню фінансову операцію («покупку», «платіж», «оплату», «транзакцію», «чек», «списання») разом із дієсловом видалення/скасування («видали»/«видалити»/«скасуй»/«скасувати»/«прибери»/«прибрати»), без обов'язкового слова `витрата` — напр. «Скасуй ту покупку на 50 zł», «Прибери останній платіж», «Видали останню оплату за інтернет».
* Дієслово ОБОВ'ЯЗКОВЕ в обох гілках gate — сам фінансовий стем без дієслова видалення нічого не запускає («Я оплатив інтернет 120 zł», «Запиши покупку на 50 zł» лишаються add-expense формулюваннями). Гола сума в zł (без фінансового слова) також свідомо НЕ є достатнім тригером — «Видали булочку 4 zł»/«Прибери булочку 4 zł» лишаються такими ж неоднозначними, як і раніше (існуючий захист від хибного трактування назви товару як витрати).
* Це **лише зміна gate** — сам existing expense-delete flow (Gemini-router `_ask_gemini_expense_router`, candidate resolution, `pending_expense_delete`/`expense_delete_selection`, preview/confirm/cancel, stale protection, `database.delete_expense`) повторно використаний без жодної зміни; `database.py` не чіпали.
* Позиція `expense_delete_command_route` у `message_dispatcher.py`/`bot.py` не змінена — жодного routing-рефакторингу.
* **Знахідка (не регресія цієї задачі):** `database.delete_expense` не пише в `household_action_journal`, тому видалення витрати через цей flow (`🗑️ Видалити витрату` / глобальна команда) сьогодні **не підтримує** «↩️ Скасувати останню дію» — на відміну від `execute_inventory_delete`/`execute_inventory_transform`. Це попередньо існуючий стан, не змінений і не введений цією задачею.
* **Не підтверджено наживо в Telegram** — перед оголошенням live-milestone потрібна ручна перевірка нових формулювань з реальними витратами.

## Shopping Action Planner V1 — не перевірено наживо (2026-07-14)

* Новий окремий модуль **`shopping_action_planner.py`** — global natural-language routing для видалення позиції зі списку покупок (`shopping_delete`) і позначення купленою (`shopping_mark_bought`), коли НЕ активні ні `shopping_mode`, ні збережений shopping-list context. **Це не `action_planner.py`** (Inventory Action Planner V1 — інша дія, інший домен) і **не** `mini_action_planner.py` — окремий модуль, окремий dispatcher-слот, власні тести.
* Action allowlist (V1): `shopping_delete`, `shopping_mark_bought`, `clarify`, `unsupported`. Свідомо НЕ включає shopping add/edit-quantity/clear-all, inventory- чи expense-дії, navigation, general chat — ці вже мають власні робочі маршрути.
* Pre-gate `looks_like_global_shopping_admin` — compositional, без hard-coded речень: (дієслово видалення `викресли`/`прибери`/`видали`/`забери` + посилання на список `спис...`) АБО (уже/вже + купи.../взял.../взяв) АБО (не треба/не потрібно + купува...). Кожна гілка вимагає ОБИДВІ частини — саме тому «Прибери молоко із запасів» (inventory), «Купив молоко за 10 zł» (нова покупка) і «Видали витрату за молоко» (expense) не проходять.
* Позиція в routing: **після** `global_expense_command`, **перед** трьома детермінованими inventory-гейтами й Inventory Action Planner V1. Явно **не спрацьовує**, коли `saved_list_context == "shopping_saved"` активний — у цьому випадку пріоритет лишається за вже існуючим `saved_list_router` (його власний, багатший Gemini-намір з підтримкою «купив»/«купили» в минулому часі, «все крім X» тощо) — новий planner існує лише для випадків ПОЗА mode/context, як і вимагала задача.
* Candidate resolution — Python, без другого Gemini-виклику: `resolve_shopping_candidates` повторно використовує `preview_editing._name_token_matches` (той самий declension-tolerant matcher, що вже застосовується для редагування `pending_global_household` add-прев'ю), звіряючи `item_name` з живим `get_active_shopping_items()`. 0 збігів → контрольоване повідомлення (не знайдено); 2+ збіги → нумерований список і прохання уточнити (без вгадування, без окремого нового pending-стану — наступне повідомлення просто повторно заходить у той самий planner).
* Existing executors повторно використані без змін: `legacy_shopping_flow._show_delete_preview`/`_show_mark_preview` → `pending_delete_batch`/`pending_mark_batch` → confirm-кнопки (`✅ Так, видалити` / `✅ Куплено + додати в запаси` / `✅ Куплено, без запасів`) → `database.delete_items_batch`/`mark_items_batch`, той самий stale-snapshot захист (`_verify_targets_in_tx`), той самий `✏️ Змінити вибір` fallback у mode.
* **Знахідка (не регресія цієї задачі):** `database.mark_items_batch`/`delete_items_batch` не пишуть у `household_action_journal` — так само, як і `database.delete_expense` (див. вище), цей legacy batch-шлях сьогодні не підтримує «↩️ Скасувати останню дію», для жодного з викликів (ні через mode-based flow, ні через новий global route). Попередньо існуючий стан, не введений цією задачею.
* Побічний фікс: під час підготовки тестів виявлено й виправлено попередній test-isolation gap у `tests/test_safe_undo_global_action.py` — `importlib.reload(expenses)` перепризначав `pending_expense`/`pending_expense_delete`/`expense_delete_selection` на нові об'єкти словників, а відновлювався лише `_bot`/`active_list_context`/`MAIN_KEYBOARD`; тепер відновлюються й ці три dict-посилання на `bot.*`, як і решта.
* **Не підтверджено наживо в Telegram** — перед оголошенням live-milestone потрібна ручна перевірка `shopping_delete`/`shopping_mark_bought` поза shopping mode/списком з реальними покупками.

## Fix: undo після додавання витрати (Journal standalone expense additions) — не перевірено наживо (2026-07-14)

* **Live-баг:** після підтвердження звичайної (не змішаної) витрати кнопкою «✅ Так, додати» кнопка «↩️ Скасувати останню дію» показувала прев'ю СТАРІШОЇ дії (напр. «🧊 Запаси\n• Повернути Молоко — 1 шт.») замість пропозиції видалити щойно додану витрату.
* **Точна причина (підтверджено читанням коду, не припущенням):** `database.add_expense` — єдина production-точка виклику для окремого (standalone) додавання витрати (`expenses.py:935`, всередині `handle_add_confirm`, спільного для меню витрат і глобальної команди «Запиши ... zł») — виконувала лише `INSERT INTO expenses` і **взагалі не писала запис у `household_action_journal`**. Тому `get_latest_undoable_action` (яка читає лише `household_action_journal`) продовжувала повертати попередню, вже існуючу дію. На відміну від цього, `apply_global_household_operations` (Global Household Router / photo-receipt confirm) вже й раніше коректно писала один journal-запис на кожну підтверджену дію, включно з action, що складається лише з витрати — це і був еталонний, вже робочий патерн.
* **Виправлення:** `database.add_expense` тепер додатково пише один активний `household_action_journal`-запис (`operation_type='global_household'`, порожні inventory/shopping buckets, один елемент у `expense_adds`) — **в тій самій транзакції**, до єдиного `conn.commit()`, тим самим форматом снепшотів, що й `apply_global_household_operations` уже використовує для власного `add_expense`. Завдяки цьому `get_latest_undoable_action`/`apply_undo_action`/`action_history.format_undo_preview` (усі повністю generic) обробляють цю дію без жодних власних змін. Схема БД не змінювалась — `household_action_journal` вже була достатньо generic (TEXT `operation_type`, JSONB payload/snapshot колонки).
* **Дублювання journal-записів виключено:** `database.add_expense` викликається рівно з одного місця в усьому коді (`expenses.py:935`) — Global Household Router і photo-receipt confirm flow ніколи не викликають `add_expense` напряму, а завжди йдуть через `apply_global_household_operations`, яка сама пише свій journal-запис. Тобто новий код не додає жодного ризику подвійного журналювання ні для змішаних дій, ні для чеків.
* Змінені файли: `database.py` (сама правка), `tests/test_action_journal.py` (нова секція `TestJournalWrittenForStandaloneExpenseAdd`, стара `test_add_expense_writes_no_journal` видалена — вона асертувала старий баг як «правильну» поведінку), `tests/test_expenses_v1.py` (одна асерція оновлена під новий порядок запитів), новий `tests/test_expense_add_undo_live_bug_fix.py` (наскрізне відтворення точного live-сценарію через реальний `bot.py`-диспетчер: підтвердити витрату → одразу натиснути «↩️ Скасувати останню дію» → прев'ю посилається саме на витрату, а не на стару дію запасів; плюс cancel/confirm/repeat/scope-ізоляція/«пізніша дія знову стає останньою»).
* **Свідомо поза межами цієї задачі** (лишаються відомими обмеженнями, не змінені): undo для видалення витрати (`database.delete_expense` і далі не пише journal — див. розділ «Expense-delete natural-language routing» вище), undo для видалення позиції з покупок і позначення купленою (`database.delete_items_batch`/`mark_items_batch` — див. розділ «Shopping Action Planner V1» вище).
* **Не підтверджено наживо в Telegram** — перед оголошенням live-milestone потрібна ручна перевірка: додати витрату → одразу «↩️ Скасувати останню дію» → перевірити, що прев'ю показує саме цю витрату → підтвердити скасування → перевірити, що витрата зникла зі списку останніх витрат, а інші записи не змінилися.

## Безпека — важливо

* **Ніколи** не вставляй справжній `TELEGRAM_BOT_TOKEN` у чат, коміт, лог чи документацію — навіть якщо він видно у Render deploy logs або webhook URL з Telegram API.
* Webhook URL має форму `/webhook/<TELEGRAM_BOT_TOKEN>` — при обговоренні завжди показуй лише плейсхолдер `<TELEGRAM_BOT_TOKEN>`, ніколи реальне значення.
* `.env` ніколи не читати, не показувати, не редагувати, не комітити (правило з `CLAUDE.md`).
