import json
import os
import re
from collections import deque
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from flask import Flask, request
from dotenv import load_dotenv
from groq import Groq
import requests
from database import (
    init_db,
    get_or_create_household,
    get_or_create_user,
    get_active_shopping_items,
    add_shopping_items_batch,
    get_inventory_items,
    add_inventory_items_batch,
    add_or_merge_inventory_item,
    mark_items_batch,
    delete_items_batch,
    delete_inventory_items_batch,
    apply_inventory_consumption,
    apply_compound_inventory_operations,
    apply_inventory_reconciliation,
    execute_merge_shopping,
    execute_merge_inventory,
    update_shopping_items_batch,
    update_inventory_items_batch,
    save_list_context,
    get_list_context,
    clear_list_context,
    StaleSnapshotError,
)

STALE_PREVIEW_MSG = "Список змінився з іншого пристрою. Онови список і повтори дію."

# =========================
# ENV
# =========================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = set(int(i.strip()) for i in _raw_allowed.split(",") if i.strip().isdigit())

print("GROQ LOADED:", GROQ_API_KEY is not None)
print("GEMINI LOADED:", GEMINI_API_KEY is not None)
print("ACCESS RESTRICTED:", bool(ALLOWED_USER_IDS))

try:
    init_db()
    print("DATABASE READY: True")
except Exception:
    print("DATABASE READY: False")

# =========================
# AI CLIENTS
# =========================
client = Groq(api_key=GROQ_API_KEY)

GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
GEMINI_COOKING_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# =========================
# MEMORY
# =========================
user_history = {}
waiting_for_ingredients = {}
active_list_context = {}      # chat_id -> "shopping" | "inventory"
shopping_mode = {}            # chat_id -> "adding" | "marking" | "deleting" | "editing_number" | "editing_text"
pending_batch = {}            # chat_id -> {items, ignored_items, household_id, user_db_id}
pending_mark_batch = {}       # chat_id -> {items, household_id, user_db_id}
pending_delete_batch = {}     # chat_id -> {items, household_id, user_db_id}
pending_merge = {}            # chat_id -> {groups, household_id, user_db_id, list_type}
inventory_mode = {}           # chat_id -> "adding" | "removing"
pending_inventory_batch = {}  # chat_id -> {items, ignored_items, household_id, user_db_id}
pending_remove_batch = {}     # chat_id -> {items, household_id, user_db_id}
saved_list_context = {}       # chat_id -> "shopping_saved" | "inventory_saved"
pending_saved_edit = {}       # chat_id -> {items_snapshot, validated_updates, household_id, user_db_id, context_type}
pending_quick_purchase = {}   # chat_id -> {items, ignored_items, household_id, user_db_id}
pending_inventory_consumption = {}  # chat_id -> {resolved, household_id, user_db_id}
pending_compound_inventory = {}  # chat_id -> {inventory_changes, add_to_shopping, household_id, user_db_id}
pending_inventory_reconciliation = {}  # chat_id -> {updates, additions, deletes, household_id, user_db_id}
pending_inventory_reconciliation_clarify = {}  # chat_id -> {ambiguous_group, rest, household_id, user_db_id}

_SEEN_UPDATE_IDS_MAXLEN = 1000
_seen_update_ids = deque(maxlen=_SEEN_UPDATE_IDS_MAXLEN)   # oldest-first, bounded
_seen_update_ids_set = set()                               # O(1) membership


def _is_duplicate_update(update_id):
    """Test-and-set idempotency guard for Telegram update_id.

    Returns True if this update_id was already seen (caller should short-circuit
    without re-processing or re-sending anything). Returns False and records the
    id for a new update_id. Process-local, in-memory, bounded to the most recent
    _SEEN_UPDATE_IDS_MAXLEN ids (oldest evicted first).
    """
    if update_id is None:
        return False
    if update_id in _seen_update_ids_set:
        return True
    if len(_seen_update_ids) >= _seen_update_ids.maxlen:
        oldest = _seen_update_ids.popleft()
        _seen_update_ids_set.discard(oldest)
    _seen_update_ids.append(update_id)
    _seen_update_ids_set.add(update_id)
    return False


SYSTEM_PROMPT = (
    "Ти корисний AI-помічник. Відповідай українською.\n"
    "У тебе немає доступу до інтернету в реальному часі: ніколи не стверджуй, що маєш доступ до інтернету, "
    "і не вигадуй поточну погоду, новини, курси валют, розклади рейсів чи інші дані, що потребують "
    "актуального інтернет-джерела.\n"
    "Якщо запитують поточну дату або час — використовуй надану нижче актуальну дату й час Europe/Warsaw "
    "як єдине надійне джерело.\n"
    "Якщо запитують погоду чи інші актуальні зовнішні дані — чесно відповідай: "
    "«У цій версії бота я не маю доступу до актуального прогнозу чи інтернет-пошуку, тому не хочу вигадувати дані.»\n"
    "Ніколи не пиши «Я зафіксував», «Я зберіг» або «Я оновив запаси», якщо в цьому чаті реально не відбулася "
    "підтверджена операція над базою даних. Не вигадуй зміни в PostgreSQL, обсяги упаковок, перерахунки "
    "одиниць виміру чи суми між несумісними одиницями — якщо не впевнений, чесно скажи, що не можеш це визначити."
)

_UA_WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
_UA_MONTHS_GENITIVE = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


def get_warsaw_datetime_context(now=None):
    """Authoritative Europe/Warsaw date/time string for the general AI chat prompt.

    Pure: if now is given (a tz-aware datetime), it's used as-is instead of the
    real clock — this is what makes it unit-testable without mocking time.
    """
    if now is None:
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
    weekday = _UA_WEEKDAYS[now.weekday()]
    month = _UA_MONTHS_GENITIVE[now.month - 1]
    return (
        f"Актуальна локальна дата й час: {now.day} {month} {now.year}, {weekday}, "
        f"{now.strftime('%H:%M')}, Europe/Warsaw.\n"
        "Це єдине надійне джерело поточного часу для відповіді."
    )

COOKING_SYSTEM_PROMPT = (
    "Ти кулінарний помічник. Користувач надсилає перелік продуктів, які є вдома. "
    "Запропонуй максимум 3 реалістичні страви з цих продуктів. "
    "Якщо є м'ясо, риба, яйця, сир, вершки або овочі — не пропонуй десерт, якщо користувач прямо не просить солодке. "
    "«Сливки» поруч із куркою, сиром або м'ясом трактуй як вершки. "
    "Не вигадуй продукти, яких немає в списку, крім солі, перцю, олії та води. "
    "Для кожної страви вкажи: назву, короткі кроки приготування, приблизний час. "
    "Не радь мити сиру курку або іншу сиру птицю під краном. "
    "Відповідай природною українською мовою."
)

DB_ERROR_MSG = "Не вдалося виконати дію зі списком покупок. Спробуйте ще раз трохи пізніше."
INVENTORY_ERROR_MSG = "Не вдалося виконати дію із запасами. Спробуйте ще раз трохи пізніше."
SELECTION_ERROR_MSG = "Не зміг точно зрозуміти, які товари ти маєш на увазі. Спробуй написати інакше."

DEFAULT_CATEGORY = "Інше їстівне"

VALID_CATEGORIES = {
    "М'ясо та риба", "Молочне та яйця", "Овочі та зелень",
    "Фрукти та ягоди", "Хліб і випічка", "Крупи, макарони та борошно",
    "Соуси, спеції та бакалія", "Солодке та снеки",
    "Напої", "Заморожене", "Інше їстівне",
}

CATEGORY_ORDER = [
    "М'ясо та риба", "Молочне та яйця", "Овочі та зелень",
    "Фрукти та ягоди", "Хліб і випічка", "Крупи, макарони та борошно",
    "Соуси, спеції та бакалія", "Солодке та снеки",
    "Напої", "Заморожене", "Інше їстівне",
]

CATEGORY_EMOJIS = {
    "М'ясо та риба":              "🥩",
    "Молочне та яйця":            "🥛",
    "Овочі та зелень":            "🥦",
    "Фрукти та ягоди":            "🍎",
    "Хліб і випічка":             "🍞",
    "Крупи, макарони та борошно": "🌾",
    "Соуси, спеції та бакалія":   "🧂",
    "Солодке та снеки":           "🍫",
    "Напої":                      "🥤",
    "Заморожене":                 "🧊",
    "Інше їстівне":               "🛒",
}

SHOPPING_PARSE_PROMPT = (
    "Розбий текст на список продуктів для покупки. Правила:\n"
    "- розділяй позиції за новими рядками, комами, крапками з комою або природними розділеннями;\n"
    "- «Мисливські ковбаски 4» — це ОДИН товар із кількістю «4 шт.», не два;\n"
    "- is_consumable: true лише для їжі, напоїв, спецій та соусів; "
    "навушники, батарейки, побутова хімія, засоби гігієни, посуд, інструменти, електроніка → false;\n"
    "- виправляй лише очевидні орфографічні помилки; was_corrected: true якщо виправив, інакше false;\n"
    "- не вигадуй товари, яких немає в тексті;\n"
    "- нормалізуй одиниці: «500 грам» → «500 г», «2 штуки» → «2 шт.», «1.5 л» → «1,5 л», «півтора літри» → «1,5 л»;\n"
    "- якщо вказано лише число, додавай одиницю тільки коли це очевидно: "
    "штучні товари (сосиски, яйця, ковбаски) → «шт.», рідини (молоко, вершки, кефір) → «л»; "
    "якщо неясно — лишай число без одиниці;\n"
    "- category — одна з: М'ясо та риба, Молочне та яйця, Овочі та зелень, Фрукти та ягоди, "
    "Хліб і випічка, Крупи макарони та борошно, Соуси спеції та бакалія, Солодке та снеки, "
    "Напої, Заморожене, Інше їстівне;\n"
    "- ignored_items — оригінальні назви позицій з тексту, де is_consumable=false.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без жодного додаткового тексту:\n"
    '{"items":['
    '{"name":"Молоко","quantity_text":"1,5 л","category":"Молочне та яйця","was_corrected":false,"is_consumable":true},'
    '{"name":"Вершки","quantity_text":"","category":"Молочне та яйця","was_corrected":true,"is_consumable":true}'
    '],"ignored_items":["Навушники"]}'
)

SELECTION_PROMPT = (
    "Визнач, які позиції зі списку користувач хоче вибрати.\n"
    "Правила інтерпретації:\n"
    "- «всі», «все», «усе», «прибери все», «видали все», «все купив» тощо → всі номери списку\n"
    "- «все крім X» або «залиш X, решту прибери» → всі номери, крім позицій, що відповідають X\n"
    "- числа і діапазони: «1 2 3», «1-4», «2, 5-7» → відповідні номери\n"
    "- назви або фрази → знайди відповідні позиції за назвою або змістом\n"
    "Правила відповіді:\n"
    "- Відповідай ТІЛЬКИ валідним JSON без жодного тексту: {\"selected_numbers\": [1, 3, 5]}\n"
    "- Вказуй тільки номери, які є в списку; без дублікатів; за зростанням\n"
    "- Якщо нічого не підходить — відповідай {\"selected_numbers\": []}\n"
)

INTENT_ROUTER_PROMPT = (
    "Ти аналізуєш список товарів і визначаєш:\n"
    "1. Чи хоче користувач об'єднати однакові або дублюючі позиції?\n"
    "2. Якщо так — які позиції можна безпечно об'єднати?\n\n"
    "Фрази об'єднання: «об'єднай», «злий дублікати», «прибери дублікати», «згрупуй повтори», "
    "«зроби однакові однією позицією» та подібні за змістом.\n\n"
    "Правила об'єднання:\n"
    "- Об'єднуй лише якщо назви означають той самий продукт\n"
    "- Категорія однакова, або одна з них — «Інше їстівне»\n"
    "- Якщо обидві кількості мають однакову одиницю (л, мл, г, кг, шт.) — склади їх\n"
    "- Якщо одна кількість порожня, а інша має значення → merged_quantity_text = непорожнє значення\n"
    "- Якщо обидві порожні → merged_quantity_text = \"\"\n"
    "- Не об'єднуй: різні важливі уточнення в назві («Вершки 18%» і «Вершки 30%»), різні одиниці\n"
    "- Не вигадуй кількості\n"
    "- У item_refs вказуй числа з рядків у форматі «#N»\n\n"
    "Якщо користувач НЕ просить об'єднати → {\"intent\": \"none\", \"merge_groups\": []}\n"
    "Якщо просить, але безпечних дублікатів немає → {\"intent\": \"merge_duplicates\", \"merge_groups\": []}\n\n"
    "Відповідай ТІЛЬКИ валідним JSON без жодного тексту:\n"
    "{\"intent\": \"merge_duplicates\", \"merge_groups\": [{\"item_refs\": [1, 2], \"merged_name\": \"Вершки\", \"merged_quantity_text\": \"\", \"merged_category\": \"Молочне та яйця\"}]}"
)

PENDING_PREVIEW_EDIT_PROMPT = (
    "Ти помічник для редагування pending preview списку товарів.\n"
    "Визнач намір (intent):\n"
    "- «edit_preview» — якщо користувач хоче змінити кількість, назву або категорію існуючих позицій\n"
    "- «merge_duplicates» — якщо хоче об'єднати однакові або дублюючі позиції\n"
    "- «none» — в усіх інших випадках\n\n"
    "Для edit_preview — поверни updates лише для позицій, які змінюються:\n"
    "- item_number — ціле число (номер у preview, від 1 до N)\n"
    "- name — нова назва або null якщо не змінюється\n"
    "- quantity_text — нова кількість (напр. «2 шт.», «500 г», «1,5 л») або null\n"
    "- category — нова категорія або null\n\n"
    "Не створюй нових позицій і не видаляй існуючих.\n"
    "Нормалізуй одиниці: «2 штуки» → «2 шт.», «500 грам» → «500 г», «1.5 л» → «1,5 л».\n\n"
    "Відповідай ТІЛЬКИ валідним JSON без жодного тексту:\n"
    "{\"intent\": \"edit_preview\", \"updates\": [{\"item_number\": 1, \"name\": null, \"quantity_text\": \"2 шт.\", \"category\": null}]}"
)

SAVED_LIST_EDIT_PROMPT = (
    "Ти помічник для роботи з відкритим збереженим списком товарів (список покупок або запасів).\n"
    "Визнач намір (intent):\n"
    "- «edit_saved_items» — якщо користувач хоче змінити кількість, назву або категорію наявних позицій\n"
    "- «merge_duplicates» — якщо хоче об'єднати однакові або дублюючі позиції\n"
    "- «start_action» — якщо хоче виконати дію над товарами зі списку: позначити купленими, "
    "видалити зі списку покупок або прибрати із запасів. Трактуй так само формулювання в минулому часі "
    "(«купив», «купили», «видалив», «прибрали») як запит на дію над поточним списком\n"
    "- «consume_inventory_quantity» — лише для контексту inventory_saved, якщо користувач повідомляє, "
    "що частково використав, з'їв, випив або витратив ЧАСТИНУ кількості товару, а не забрав/викинув/прибрав "
    "його повністю (напр. «Я з'їв 4 сосиски», «Використав одну приправу», «Випили 500 мл молока», "
    "«Витратив 200 г сиру», «Використав пів приправи до курки», «Випив пів літра молока», "
    "«З'їв половину пачки печива»). Ніколи не використовуй цей намір для shopping_saved. Якщо користувач хоче "
    "прибрати товар повністю («видали», «викинь», «прибери все, крім X») — це start_action з remove_inventory\n"
    "- «compound_inventory_operations» — лише для контексту inventory_saved, коли одне повідомлення "
    "поєднує КІЛЬКА РІЗНИХ дій одразу: часткове списання одних позицій, повне прибирання інших і/або "
    "додавання товару до списку покупок (напр. «Вершки зіпсувались, і я з'їв 4 сосиски, плюс додай молоко "
    "до покупок»). Використовуй цей намір лише коли повідомлення НЕ можна повністю описати одним із "
    "намірів вище. Ніколи не використовуй для shopping_saved\n"
    "- «reconcile_inventory_snapshot» — лише для контексту inventory_saved, коли користувач явно каже, "
    "що надсилає ПОВНИЙ актуальний список запасів замість поточного (напр. «Мої запаси виглядають зараз так», "
    "«Онови запаси за цим списком», «Звір мої запаси з цим списком», «Ось повний актуальний список запасів»), "
    "і після цієї фрази йде структурований перелік товарів. НІКОЛИ не використовуй цей намір для звичайної "
    "згадки продукту («Я люблю молоко»), питання («Що можна приготувати з сосисками?») чи одноразової покупки "
    "(«Сьогодні купив хліб.») — для цього є quick_add_to_inventory або none. Ніколи не використовуй для "
    "shopping_saved\n"
    "- «quick_add_to_inventory» — лише коли список порожній (позицій немає взагалі) і користувач "
    "повідомляє про продукти, які вже приніс/купив додому (напр. «Купив молоко і хліб», «Взяли сир»), "
    "навіть у минулому часі. Не використовуй цей намір, якщо є хоч одна позиція в списку, або текст — "
    "питання, план на майбутнє чи загальна фраза\n"
    "- «none» — в усіх інших випадках: додавання нових товарів у непорожній список, загальне питання, не стосується списку\n\n"
    "Для edit_saved_items — поверни updates лише для позицій, які змінюються:\n"
    "- item_number — ціле число (номер у списку, від 1 до N)\n"
    "- name — нова назва або null якщо не змінюється\n"
    "- quantity_text — нова кількість (напр. «2 шт.», «500 г», «1,5 л») або null\n"
    "- category — нова категорія або null\n\n"
    "Для merge_duplicates — поверни merge_groups: масив масивів item_number:\n"
    "- [[2, 4], [1, 3]] — кожна підгрупа містить номери позицій для об'єднання\n\n"
    "Для start_action — поверни action і selected_numbers:\n"
    "- action — одне з: «mark_bought» (позначити купленими — лише для списку покупок), "
    "«delete_shopping» (видалити зі списку покупок — лише для списку покупок), "
    "«remove_inventory» (прибрати із запасів — лише для запасів); обирай дію лише дозволену для поточного контексту\n"
    "- selected_numbers — номери обраних позицій за тими самими правилами, що й вибір позицій: "
    "«всі», «усе», «все куплено» тощо → всі номери; «все крім X» або «залиш X, решту...» → всі, крім X; "
    "числа й діапазони («1 2 3», «1-4»); назви або фрази → знайди відповідні позиції за назвою або змістом\n\n"
    "Для consume_inventory_quantity — поверни consumptions: масив об'єктів для позицій, з яких частково "
    "списується кількість:\n"
    "- item_number — ціле число (номер позиції)\n"
    "- quantity_value — додатне число, скільки саме використано\n"
    "- quantity_unit — одне з «шт.», «л», «мл», «г», «кг» — одиниця, у якій вказано використане\n\n"
    "Для compound_inventory_operations — поверни operations: масив об'єктів, кожен з полем type:\n"
    "- {\"type\": \"remove_inventory\", \"item_number\": N} — повністю прибрати позицію N із запасів\n"
    "- {\"type\": \"consume_inventory_quantity\", \"item_number\": N, \"quantity_value\": число, "
    "\"quantity_unit\": одиниця} — частково списати кількість із позиції N\n"
    "- {\"type\": \"add_to_shopping\", \"name\": назва, \"quantity_value\": число або null, "
    "\"quantity_unit\": одиниця або null, \"quantity_inferred\": true/false, \"category\": категорія, "
    "\"is_consumable\": true} — додати новий товар до списку покупок\n"
    "Також для compound_inventory_operations поверни unresolved_fragments — масив рядків з фрагментами "
    "тексту, які ти НЕ зміг однозначно перетворити на одну з дозволених операцій. Не мовчи і не пропускай "
    "незрозумілу частину — обов'язково додай її сюди замість того, щоб її ігнорувати\n\n"
    "Для quick_add_to_inventory — поверни items: масив нових товарів, кожен з полями:\n"
    "- name — назва товару\n"
    "- canonical_name — назва в нижньому регістрі\n"
    "- quantity_value — число або null, якщо кількість не вказана явно\n"
    "- quantity_unit — одне з «шт.», «л», «мл», «г», «кг», або null\n"
    "- quantity_inferred — true, якщо кількість не вказана явно (тоді quantity_value=1, quantity_unit=«шт.»)\n"
    "- category — категорія товару\n"
    "- is_consumable — true лише для їжі, напоїв, спецій та соусів; побутові товари → false\n"
    "Для quick_add_to_inventory не вигадуй кількість: якщо явно не вказано число й одиницю — став "
    "quantity_value=1, quantity_unit=«шт.», quantity_inferred=true. «Молоко 2 л» → quantity_value=2, "
    "quantity_unit=«л», quantity_inferred=false.\n\n"
    "Для reconcile_inventory_snapshot — поверни items: масив УСІХ товарів із надісланого повного списку "
    "запасів, кожен з полями:\n"
    "- name — назва товару\n"
    "- canonical_name — назва в нижньому регістрі\n"
    "- quantity_value — число або null, якщо кількість не вказана явно для цієї позиції\n"
    "- quantity_unit — одне з «шт.», «л», «мл», «г», «кг», або null\n"
    "- quantity_inferred — true, якщо кількість не вказана явно (тоді quantity_value=1, quantity_unit=«шт.»)\n"
    "- category — категорія товару\n"
    "- is_consumable — true лише для їжі, напоїв, спецій та соусів; побутові товари → false\n"
    "Ти лише розбираєш список у JSON — не рахуй суми між різними одиницями, не вигадуй об'єм чи вагу упаковки, "
    "не пиши жодного тексту поза JSON. Незрозумілі фрагменти списку клади в unresolved_fragments, а не мовчи\n\n"
    "Правила:\n"
    "- Не додавай нових позицій і не видаляй існуючих через edit_saved_items чи merge_duplicates\n"
    "- Для start_action не повертай updates і merge_groups\n"
    "- Для consume_inventory_quantity не вигадуй кількість — використовуй тільки те число, яке явно назвав "
    "користувач, і не повертай updates, merge_groups, action, selected_numbers, items\n"
    "- Для compound_inventory_operations кожен item_number може зустрічатися лише в одній операції "
    "(не можна одночасно прибрати й списати частково ту саму позицію); не вигадуй кількість; "
    "не повертай updates, merge_groups, action, selected_numbers, items, consumptions\n"
    "- Для quick_add_to_inventory не повертай updates, merge_groups, action, selected_numbers\n"
    "- Для reconcile_inventory_snapshot не вигадуй кількість заднім числом для позицій, які й раніше не мали "
    "вказаної кількості — якщо кількість не вказана явно в новому списку, став quantity_inferred=true і не "
    "намагайся вгадати число\n"
    "- Для reconcile_inventory_snapshot не повертай updates, merge_groups, action, selected_numbers, consumptions, operations\n"
    "- Нормалізуй одиниці: «2 штуки» → «2 шт.», «500 грам» → «500 г», «1.5 л» → «1,5 л»\n"
    "- Нормалізуй дробові кількості: «пів», «половина», «пів пачки», «половинку», «половину пачки», "
    "«півлітра» → quantity_value 0.5 з відповідною одиницею вихідного товару "
    "(«з'їв половину пачки печива» → 0,5 шт., «випив півлітра молока» → 0,5 л); "
    "ніколи не округлюй 0,5 до 1 і не вигадуй одиницю, якщо вона не випливає з контексту\n"
    "- Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON\n\n"
    "Приклад edit_saved_items:\n"
    "{\"intent\": \"edit_saved_items\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [{\"item_number\": 1, \"name\": null, \"quantity_text\": \"2 шт.\", \"category\": null}], "
    "\"merge_groups\": [], \"items\": []}\n"
    "Приклад start_action:\n"
    "{\"intent\": \"start_action\", \"action\": \"mark_bought\", \"selected_numbers\": [1, 3], "
    "\"updates\": [], \"merge_groups\": [], \"items\": []}\n"
    "Приклад consume_inventory_quantity:\n"
    "{\"intent\": \"consume_inventory_quantity\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], "
    "\"consumptions\": [{\"item_number\": 2, \"quantity_value\": 4, \"quantity_unit\": \"шт.\"}]}\n"
    "Приклад consume_inventory_quantity з половинною кількістю "
    "(«Я використав пів приправи до курки» для позиції «Приправа до курки — 2 шт.»):\n"
    "{\"intent\": \"consume_inventory_quantity\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], "
    "\"consumptions\": [{\"item_number\": 3, \"quantity_value\": 0.5, \"quantity_unit\": \"шт.\"}]}\n"
    "Приклад compound_inventory_operations:\n"
    "{\"intent\": \"compound_inventory_operations\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], \"consumptions\": [], "
    "\"operations\": ["
    "{\"type\": \"remove_inventory\", \"item_number\": 3}, "
    "{\"type\": \"consume_inventory_quantity\", \"item_number\": 2, \"quantity_value\": 0.5, \"quantity_unit\": \"шт.\"}, "
    "{\"type\": \"add_to_shopping\", \"name\": \"Приправа до курки\", \"quantity_value\": 1, "
    "\"quantity_unit\": \"шт.\", \"quantity_inferred\": false, \"category\": \"Соуси, спеції та бакалія\", "
    "\"is_consumable\": true}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад quick_add_to_inventory:\n"
    "{\"intent\": \"quick_add_to_inventory\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": ["
    "{\"name\": \"Молоко\", \"canonical_name\": \"молоко\", \"quantity_value\": 1, \"quantity_unit\": \"шт.\", "
    "\"quantity_inferred\": true, \"category\": \"Молочне та яйця\", \"is_consumable\": true}]}\n"
    "Приклад reconcile_inventory_snapshot:\n"
    "{\"intent\": \"reconcile_inventory_snapshot\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"consumptions\": [], \"operations\": [], "
    "\"items\": ["
    "{\"name\": \"Молоко\", \"canonical_name\": \"молоко\", \"quantity_value\": 5.5, \"quantity_unit\": \"л\", "
    "\"quantity_inferred\": false, \"category\": \"Молочне та яйця\", \"is_consumable\": true}, "
    "{\"name\": \"Йогурт\", \"canonical_name\": \"йогурт\", \"quantity_value\": 1, \"quantity_unit\": \"шт.\", "
    "\"quantity_inferred\": true, \"category\": \"Молочне та яйця\", \"is_consumable\": true}"
    "], \"unresolved_fragments\": []}"
)

# =========================
# KEYBOARDS
# =========================
MAIN_KEYBOARD = {
    "keyboard": [
        ["🛒 Покупки", "🧊 Запаси"],
        ["🍽️ Що приготувати", "ℹ️ Допомога"]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

SHOPPING_KEYBOARD = {
    "keyboard": [
        ["➕ Додати товар", "📋 Показати список"],
        ["✅ Позначити купленим", "🗑️ Видалити товар"],
        ["⬅️ Головне меню"]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

ADD_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Додати все", "✏️ Надіслати інший список"],
        ["✏️ Виправити позицію", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

INVENTORY_KEYBOARD = {
    "keyboard": [
        ["➕ Додати продукти", "📋 Показати запаси"],
        ["➖ Використати / прибрати", "⬅️ Головне меню"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

MARK_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Куплено + додати в запаси", "✅ Куплено, без запасів"],
        ["✏️ Змінити вибір", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

DELETE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, видалити"],
        ["✏️ Змінити вибір", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

REMOVE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, прибрати"],
        ["✏️ Змінити вибір", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

ADD_INVENTORY_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Додати все", "✏️ Надіслати інший список"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

MERGE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Об'єднати", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

SAVED_EDIT_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Підтвердити зміни"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

QUICK_PURCHASE_KEYBOARD = {
    "keyboard": [
        ["✅ Додати до запасів", "✏️ Змінити список"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

COMPOUND_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Підтвердити всі зміни"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

RECONCILIATION_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Підтвердити звіряння"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

# =========================
# FLASK APP
# =========================
app = Flask(__name__)

SEND_MESSAGE_TIMEOUT = 10  # seconds; keeps webhook() from stalling past Telegram's retry window

def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload, timeout=SEND_MESSAGE_TIMEOUT)

def call_gemini(history, system_prompt, temperature=0.7, model_url=None):
    if not GEMINI_API_KEY:
        return None
    if model_url is None:
        model_url = GEMINI_CHAT_URL
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature}
    }
    try:
        resp = requests.post(
            model_url,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        if not text or not text.strip():
            return None
        return text.strip()
    except Exception:
        return None

def parse_item_text(text):
    if "—" in text:
        parts = text.split("—", 1)
        return parts[0].strip(), parts[1].strip()
    return text.strip(), None

def format_grouped_list(items, header):
    lines = [header, ""]
    counter = 1
    for cat in CATEGORY_ORDER:
        cat_items = [it for it in items if (it.get("category") or DEFAULT_CATEGORY) == cat]
        if not cat_items:
            continue
        emoji = CATEGORY_EMOJIS.get(cat, "🛒")
        lines.append(f"{emoji} {cat}")
        for item in cat_items:
            label = item["name"]
            if item.get("was_corrected"):
                label += " (виправлено)"
            _, _, qty_display = _effective_quantity(item)
            if qty_display:
                lines.append(f"{counter}. {label} — {qty_display}")
            else:
                lines.append(f"{counter}. {label}")
            counter += 1
        lines.append("")
    return "\n".join(lines).rstrip()

def format_shopping_list(items):
    if not items:
        return "Список покупок поки порожній."
    return format_grouped_list(items, "🛒 Список покупок:")

def format_inventory_list(items):
    if not items:
        return "Запаси поки порожні."
    return format_grouped_list(items, "🧊 Запаси:")

def format_inventory_preview(items, ignored_items=None):
    header = f"🧊 Знайшов продуктів: {len(items)}"
    text = format_grouped_list(items, header)
    if ignored_items:
        text += "\n\nНе додано: " + ", ".join(ignored_items)
    return text

def get_household_and_user(user_id, display_name=None):
    household_id = get_or_create_household()
    user_db_id = get_or_create_user(user_id, household_id, display_name)
    return household_id, user_db_id

def clear_shopping_state(chat_id):
    shopping_mode.pop(chat_id, None)
    pending_batch.pop(chat_id, None)
    pending_mark_batch.pop(chat_id, None)
    pending_delete_batch.pop(chat_id, None)
    pending_merge.pop(chat_id, None)
    saved_list_context.pop(chat_id, None)
    pending_saved_edit.pop(chat_id, None)
    pending_quick_purchase.pop(chat_id, None)

def clear_inventory_state(chat_id):
    inventory_mode.pop(chat_id, None)
    pending_inventory_batch.pop(chat_id, None)
    pending_remove_batch.pop(chat_id, None)
    pending_merge.pop(chat_id, None)
    saved_list_context.pop(chat_id, None)
    pending_saved_edit.pop(chat_id, None)
    pending_quick_purchase.pop(chat_id, None)
    pending_inventory_consumption.pop(chat_id, None)
    pending_compound_inventory.pop(chat_id, None)
    pending_inventory_reconciliation.pop(chat_id, None)
    pending_inventory_reconciliation_clarify.pop(chat_id, None)

# =========================
# QUANTITY HELPERS (local)
# =========================
_MERGEABLE_UNITS_BOT = {"л", "мл", "г", "кг", "шт."}

def _parse_qty(qty_text):
    if not qty_text:
        return None, None
    normalized = qty_text.strip().replace(",", ".")
    parts = normalized.split()
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0]), parts[1]
    except ValueError:
        return None, None

# =========================
# STRUCTURED QUANTITY HELPERS
# =========================

_NAME_SYNONYMS = {
    "сливки": "вершки",
}

_UNIT_ALIASES = {
    "шт": "шт.", "шт.": "шт.", "штук": "шт.", "штуки": "шт.", "штука": "шт.",
    "л": "л", "літр": "л", "літри": "л", "літра": "л",
    "мл": "мл", "мілілітр": "мл", "мілілітри": "мл", "мілілітрів": "мл",
    "г": "г", "грам": "г", "грами": "г", "грама": "г", "грамів": "г",
    "кг": "кг", "кілограм": "кг", "кілограми": "кг", "кілограмів": "кг",
}

STRUCTURED_UNITS = {"шт.", "л", "мл", "г", "кг"}


def canonicalize_name(name):
    """Lowercase/trim a name and map known synonyms to one canonical form."""
    base = (name or "").strip().lower()
    return _NAME_SYNONYMS.get(base, base)


def _parse_structured_quantity(quantity_text):
    """Parse an unambiguous 'value unit' or bare-number quantity_text.

    Returns (value: float|None, unit: str|None). Never raises.
    """
    if not quantity_text or not quantity_text.strip():
        return None, None
    normalized = quantity_text.strip().replace(",", ".")
    parts = normalized.split()
    if len(parts) == 1:
        try:
            return float(parts[0]), None
        except ValueError:
            return None, None
    if len(parts) == 2:
        try:
            value = float(parts[0])
        except ValueError:
            return None, None
        unit = _UNIT_ALIASES.get(parts[1].lower().rstrip("."))
        if unit is None:
            return None, None
        return value, unit
    return None, None


def format_quantity_display(value, unit):
    """Format a numeric value+unit for display: comma decimal, no trailing .0."""
    if value is None:
        return ""
    if value == int(value):
        value_str = str(int(value))
    else:
        value_str = ("%g" % value).replace(".", ",")
    return f"{value_str} {unit}" if unit else value_str


def normalize_item_quantity(name, quantity_text, quantity_value=None, quantity_unit=None, allow_default_unit=False):
    """Compute canonical_name/quantity_value/quantity_unit/quantity_inferred/quantity_text for an item.

    If quantity_value+quantity_unit are already known, they're used as-is.
    Otherwise quantity_text is parsed locally when unambiguous. allow_default_unit=True
    applies the "1 шт." default only when quantity_text is genuinely blank (new
    items straight out of AI parsing) — never for edits or legacy-data backfill.
    """
    canonical_name = canonicalize_name(name)
    inferred = False
    if quantity_value is not None and quantity_unit is not None:
        value, unit = quantity_value, quantity_unit
    else:
        value, unit = _parse_structured_quantity(quantity_text)
        if value is None and not (quantity_text or "").strip() and allow_default_unit:
            value, unit, inferred = 1.0, "шт.", True
    display = format_quantity_display(value, unit) if value is not None else (quantity_text or "").strip()
    return {
        "canonical_name": canonical_name,
        "quantity_value": value,
        "quantity_unit": unit,
        "quantity_inferred": inferred,
        "quantity_text": display,
    }


def merge_quantity_values(value_a, unit_a, value_b, unit_b):
    """Return merged (value, unit) if two structured quantities can be safely
    summed, else None. Units must match and be one of the known structured units."""
    if value_a is None or value_b is None:
        return None
    if unit_a != unit_b:
        return None
    if unit_a not in STRUCTURED_UNITS:
        return None
    return round(value_a + value_b, 2), unit_a


def _effective_quantity(item):
    """Return (value, unit, display_text) for an item, preferring structured fields."""
    value = item.get("quantity_value")
    unit = item.get("quantity_unit")
    if value is not None:
        return value, unit, format_quantity_display(value, unit)
    return None, None, (item.get("quantity_text") or "")


def names_can_merge(item_a, item_b):
    """Same product (canonical_name) and compatible category (equal, or either default)."""
    canon_a = item_a.get("canonical_name") or canonicalize_name(item_a["name"])
    canon_b = item_b.get("canonical_name") or canonicalize_name(item_b["name"])
    if canon_a != canon_b:
        return False
    cat_a = item_a.get("category") or DEFAULT_CATEGORY
    cat_b = item_b.get("category") or DEFAULT_CATEGORY
    return cat_a == cat_b or cat_a == DEFAULT_CATEGORY or cat_b == DEFAULT_CATEGORY


def _compute_merged_quantity(merge_items):
    """Compute safe merged quantity_text for a group.

    both empty → ""; one empty → use non-empty; same mergeable unit → sum;
    different units or unparseable → None (group blocked).
    """
    qtys = [item.get("quantity_text") or "" for item in merge_items]
    non_empty = [q.strip() for q in qtys if q.strip()]

    if not non_empty:
        return ""
    if len(non_empty) == 1:
        return non_empty[0]

    parsed = [_parse_qty(q) for q in non_empty]

    if any(val is None for val, unit in parsed):
        unique = set(non_empty)
        return non_empty[0] if len(unique) == 1 else None

    units = set(unit for val, unit in parsed)
    if len(units) != 1:
        return None

    unit = next(iter(units))
    if unit not in _MERGEABLE_UNITS_BOT:
        unique = set(non_empty)
        return non_empty[0] if len(unique) == 1 else None

    total = round(sum(val for val, unit in parsed), 1)
    if total == int(total):
        return f"{int(total)} {unit}"
    return str(total).replace(".", ",") + f" {unit}"

# =========================
# MERGE HELPERS
# =========================

def _auto_merge_in_place(items):
    """Merge duplicate items within a pending list (pure Python, no Gemini).

    Same canonical_name (+ compatible category) with matching structured
    units are summed. Incompatible items are left separate — no guessed math.
    """
    result = []
    for item in items:
        target = None
        merged_qty = None
        for existing in result:
            if not names_can_merge(existing, item):
                continue
            val_a, unit_a, _ = _effective_quantity(existing)
            val_b, unit_b, _ = _effective_quantity(item)
            candidate = merge_quantity_values(val_a, unit_a, val_b, unit_b)
            if candidate is not None:
                target = existing
                merged_qty = candidate
                break
        if target is not None:
            value, unit = merged_qty
            target["quantity_value"] = value
            target["quantity_unit"] = unit
            target["quantity_text"] = format_quantity_display(value, unit)
            target["quantity_inferred"] = bool(target.get("quantity_inferred")) and bool(item.get("quantity_inferred"))
            if (target.get("category") or DEFAULT_CATEGORY) == DEFAULT_CATEGORY and item.get("category"):
                target["category"] = item["category"]
        else:
            result.append(dict(item))
    return result


def _apply_pending_merge(items, validated_groups):
    """Apply merge groups to a pending RAM list. Returns new filtered list."""
    items = list(items)
    for group in validated_groups:
        indices = group["item_indices"]
        main_idx = indices[0]
        if main_idx >= len(items) or items[main_idx] is None:
            continue
        items[main_idx] = dict(items[main_idx])
        items[main_idx]["name"] = group["merged_name"]
        items[main_idx]["quantity_text"] = group["merged_quantity_text"]
        items[main_idx]["category"] = group["merged_category"]
        for idx in indices[1:]:
            if idx < len(items):
                items[idx] = None
    return [it for it in items if it is not None]


def _validate_merge_groups(raw_groups, items_list, is_pending=False):
    """Validate Gemini merge suggestions against an ordered item list.

    raw_groups use sequential item_refs (#1, #2, ...).
    is_pending=True  → store item_indices (0-based list indices).
    is_pending=False → store item_ids (actual DB ids).
    """
    validated = []
    used_refs = set()
    items_by_ref = {i + 1: items_list[i] for i in range(len(items_list))}
    for group in raw_groups:
        refs = group.get("item_refs")
        if not isinstance(refs, list) or len(refs) < 2:
            continue
        try:
            refs = [int(r) for r in refs]
        except (TypeError, ValueError):
            continue
        if any(r in used_refs for r in refs):
            continue
        merge_items = [items_by_ref.get(r) for r in refs]
        if any(m is None for m in merge_items):
            continue

        categories = set(item.get("category") or DEFAULT_CATEGORY for item in merge_items)
        non_default = categories - {DEFAULT_CATEGORY}
        if len(non_default) > 1:
            continue

        merged_category = (group.get("merged_category") or "").strip()
        if merged_category not in VALID_CATEGORIES:
            merged_category = next(iter(non_default), DEFAULT_CATEGORY)

        merged_name = (group.get("merged_name") or "").strip()
        if not merged_name:
            continue

        merged_qty = _compute_merged_quantity(merge_items)
        if merged_qty is None:
            continue

        used_refs.update(refs)
        entry = {
            "merged_name": merged_name,
            "merged_quantity_text": merged_qty,
            "merged_category": merged_category,
            "items": merge_items,
        }
        if is_pending:
            entry["item_indices"] = [r - 1 for r in refs]
        else:
            entry["item_ids"] = [item["id"] for item in merge_items]
        validated.append(entry)
    return validated


def _ask_gemini_intent_router(user_text, items):
    """One Gemini call: detect merge intent and return merge groups (sequential #N refs)."""
    if len(items) < 2:
        return {"intent": "none", "merge_groups": []}
    lines = []
    for i, item in enumerate(items):
        label = f"#{i + 1}. {item['name']}"
        if item.get("quantity_text"):
            label += f" — {item['quantity_text']}"
        label += f" [{item.get('category') or DEFAULT_CATEGORY}]"
        lines.append(label)
    prompt = "Список:\n" + "\n".join(lines) + f"\n\nКористувач написав: {user_text}"
    raw = call_gemini([{"role": "user", "content": prompt}], INTENT_ROUTER_PROMPT, temperature=0.1)
    if not raw:
        return {"intent": "none", "merge_groups": []}
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "merge_groups": data.get("merge_groups") if isinstance(data.get("merge_groups"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"intent": "none", "merge_groups": []}


def _ask_gemini_preview_edit_router(user_text, items, context_type):
    """Gemini call: detect edit_preview or merge_duplicates for an active pending preview."""
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        item_qty = _effective_quantity(item)[2]
        if item_qty:
            label += f" — {item_qty}"
        label += f" [{item.get('category') or DEFAULT_CATEGORY}]"
        lines.append(label)
    prompt = (
        f"Контекст: {context_type}\n"
        "Товари у preview:\n" + "\n".join(lines)
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], PENDING_PREVIEW_EDIT_PROMPT, temperature=0.1)
    if not raw:
        return {"intent": "none", "updates": []}
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "updates": data.get("updates") if isinstance(data.get("updates"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"intent": "none", "updates": []}


def _validate_preview_updates(updates, items):
    """Validate Gemini preview edit updates. Returns list of valid updates or None."""
    if not isinstance(updates, list) or not updates:
        return None
    total = len(items)
    used_numbers = set()
    valid = []
    for upd in updates:
        if not isinstance(upd, dict):
            return None
        num = upd.get("item_number")
        if not isinstance(num, int) or num < 1 or num > total:
            return None
        if num in used_numbers:
            return None
        used_numbers.add(num)
        name = upd.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return None
        qty = upd.get("quantity_text")
        if qty is not None and (not isinstance(qty, str) or not qty.strip()):
            return None
        cat = upd.get("category")
        if cat is not None and cat not in VALID_CATEGORIES:
            return None
        valid.append({
            "item_number": num,
            "name": name,
            "quantity_text": qty,
            "category": cat,
        })
    return valid


def _apply_preview_updates(items, valid_updates):
    """Apply validated updates to items list. Returns new list without mutating originals."""
    result = [dict(item) for item in items]
    for upd in valid_updates:
        idx = upd["item_number"] - 1
        name_changed = upd.get("name") is not None
        qty_changed = upd.get("quantity_text") is not None
        if name_changed:
            result[idx]["name"] = str(upd["name"]).strip()
        if qty_changed:
            result[idx]["quantity_text"] = upd["quantity_text"].strip()
        if upd.get("category") is not None:
            result[idx]["category"] = upd["category"]
        if name_changed or qty_changed:
            normalized = normalize_item_quantity(result[idx]["name"], result[idx].get("quantity_text") or "")
            result[idx].update(normalized)
    return result


# =========================
# SAVED LIST EDIT HELPERS
# =========================

def _compute_saved_merged_quantity(group_items):
    """Compute merged quantity for saved list merging.

    All empty → count as 1 шт. each (e.g. Хліб + Хліб → 2 шт.).
    empty + "N шт." → (N+1) шт. (empty treated as 1 шт.).
    Same parseable non-шт. unit → sum (no empty items allowed for liquid/weight units).
    Returns quantity string or None if incompatible.
    """
    qtys = [_effective_quantity(item)[2] for item in group_items]
    non_empty = [q.strip() for q in qtys if q.strip()]
    empty_count = len(qtys) - len(non_empty)

    if not non_empty:
        return f"{len(group_items)} шт."

    parsed = []
    for q in non_empty:
        val, unit = _parse_qty(q)
        if val is None:
            return None
        parsed.append((val, unit))

    units = {u for _, u in parsed}
    if len(units) != 1:
        return None
    unit = next(iter(units))
    if unit not in _MERGEABLE_UNITS_BOT:
        return None

    if unit == "шт.":
        total = round(sum(v for v, _ in parsed) + empty_count, 1)
    else:
        if empty_count > 0:
            return None
        total = round(sum(v for v, _ in parsed), 1)

    if total == int(total):
        return f"{int(total)} {unit}"
    return str(total).replace(".", ",") + f" {unit}"


def _compute_saved_merge_groups(merge_groups_raw, items):
    """Convert Gemini [[2, 4], [1, 3]] merge_groups into validated groups for DB merge.

    Validates: same normalized name, compatible categories, safe quantity merge.
    Returns list of groups ready for execute_merge_shopping/inventory and _format_merge_preview.
    """
    if not isinstance(merge_groups_raw, list) or not merge_groups_raw:
        return []
    total = len(items)
    items_by_num = {i + 1: items[i] for i in range(total)}
    used_numbers = set()
    validated = []
    for group_raw in merge_groups_raw:
        if not isinstance(group_raw, list) or len(group_raw) < 2:
            continue
        try:
            nums = [int(n) for n in group_raw]
        except (TypeError, ValueError):
            continue
        if any(n < 1 or n > total for n in nums):
            continue
        if any(n in used_numbers for n in nums):
            continue
        group_items = [items_by_num[n] for n in nums]
        canonical_names = {it.get("canonical_name") or canonicalize_name(it["name"]) for it in group_items}
        if len(canonical_names) > 1:
            continue
        categories = {it.get("category") or DEFAULT_CATEGORY for it in group_items}
        non_default = categories - {DEFAULT_CATEGORY}
        if len(non_default) > 1:
            continue
        merged_category = next(iter(non_default), DEFAULT_CATEGORY)
        merged_qty = _compute_saved_merged_quantity(group_items)
        if merged_qty is None:
            continue
        merged_value, merged_unit = _parse_structured_quantity(merged_qty)
        used_numbers.update(nums)
        validated.append({
            "item_ids": [it["id"] for it in group_items],
            "merged_name": group_items[0]["name"],
            "merged_quantity_text": merged_qty,
            "merged_category": merged_category,
            "canonical_name": next(iter(canonical_names)),
            "merged_quantity_value": merged_value,
            "merged_quantity_unit": merged_unit,
            "items": group_items,
        })
    return validated


def _validate_saved_updates(updates, items):
    """Validate Gemini saved list edit updates. Returns list of valid updates (with
    item_id plus old_value/old_unit — the snapshot quantity at preview time, used to
    detect a stale precondition at confirm time) or None."""
    if not isinstance(updates, list) or not updates:
        return None
    total = len(items)
    used_numbers = set()
    valid = []
    for upd in updates:
        if not isinstance(upd, dict):
            return None
        num = upd.get("item_number")
        if not isinstance(num, int) or num < 1 or num > total:
            return None
        if num in used_numbers:
            return None
        used_numbers.add(num)
        name = upd.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return None
        qty = upd.get("quantity_text")
        if qty is not None and (not isinstance(qty, str) or not qty.strip()):
            return None
        cat = upd.get("category")
        if cat is not None and cat not in VALID_CATEGORIES:
            return None
        valid.append({
            "item_number": num,
            "item_id": items[num - 1]["id"],
            "name": name,
            "quantity_text": qty,
            "category": cat,
            "old_value": items[num - 1].get("quantity_value"),
            "old_unit": items[num - 1].get("quantity_unit"),
        })
    return valid


_ACTIONS_BY_CONTEXT = {
    "shopping_saved": {"mark_bought", "delete_shopping"},
    "inventory_saved": {"remove_inventory"},
}


def _validate_start_action(action, selected_numbers, context_type, items):
    """Validate a start_action router result for the current open list.

    Rejects any action not allowed for context_type, then validates
    selected_numbers the same way as button-triggered selection (dedup,
    order preserved, out-of-range dropped, empty rejected).
    Returns the ordered list of selected item dicts, or None if invalid.
    """
    if action not in _ACTIONS_BY_CONTEXT.get(context_type, set()):
        return None
    return _validate_selected_numbers(selected_numbers, items)


_UNIT_GROUP = {"л": "volume", "мл": "volume", "кг": "mass", "г": "mass", "шт.": "count"}
_UNIT_TO_CANONICAL_FACTOR = {
    "л": Decimal("1"), "мл": Decimal("0.001"),
    "кг": Decimal("1000"), "г": Decimal("1"),
    "шт.": Decimal("1"),
}
_CANONICAL_UNIT_FOR_GROUP = {"volume": "л", "mass": "г", "count": "шт."}


def _resolve_consumption(current_value, current_unit, consume_value, consume_unit):
    """Compute the remaining quantity after consuming part of an inventory item.

    Uses Decimal throughout (never float) for the subtraction/conversion. The
    remainder is always expressed in the group's canonical display unit (л for
    volume, г for mass, шт. for count), not necessarily current_unit — e.g.
    1 кг - 200 г is shown as 800 г, not 0,8 кг.

    Returns ("ok", remaining_decimal, remaining_unit), ("incompatible_units", None, None)
    if the two units aren't from the same group, or ("insufficient", None, None) if
    consume_value exceeds what's available.
    """
    current_group = _UNIT_GROUP.get(current_unit)
    consume_group = _UNIT_GROUP.get(consume_unit)
    if current_group is None or consume_group is None or current_group != consume_group:
        return "incompatible_units", None, None
    current_canonical = Decimal(str(current_value)) * _UNIT_TO_CANONICAL_FACTOR[current_unit]
    consume_canonical = Decimal(str(consume_value)) * _UNIT_TO_CANONICAL_FACTOR[consume_unit]
    if consume_canonical > current_canonical:
        return "insufficient", None, None
    remaining = current_canonical - consume_canonical
    return "ok", remaining, _CANONICAL_UNIT_FOR_GROUP[current_group]


def _validate_consumptions(consumptions, items):
    """Validate Gemini consume_inventory_quantity output against current inventory items.

    Returns one of:
      ("ok", [resolved...]) — each resolved dict has item_number, item_id, name,
          old_value, old_unit, old_display, new_value, new_unit, new_display,
          will_remove (True when the remainder is exactly zero).
      ("missing_quantity", item_name) — item has no structured quantity to subtract from.
      ("insufficient", (item_name, available_display, requested_display)) — not enough left.
      ("invalid", None) — malformed input, out-of-range/duplicate item_number, bad unit,
          non-positive quantity, or incompatible units.
    """
    if not isinstance(consumptions, list) or not consumptions:
        return "invalid", None
    total = len(items)
    used_numbers = set()
    resolved = []
    for entry in consumptions:
        if not isinstance(entry, dict):
            return "invalid", None
        num = entry.get("item_number")
        if not isinstance(num, int) or num < 1 or num > total:
            return "invalid", None
        if num in used_numbers:
            return "invalid", None
        used_numbers.add(num)
        value = entry.get("quantity_value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return "invalid", None
        unit = entry.get("quantity_unit")
        if unit not in STRUCTURED_UNITS:
            return "invalid", None
        item = items[num - 1]
        cur_value = item.get("quantity_value")
        cur_unit = item.get("quantity_unit")
        if cur_value is None or cur_unit is None:
            return "missing_quantity", item["name"]
        kind, remaining, remaining_unit = _resolve_consumption(cur_value, cur_unit, value, unit)
        if kind == "incompatible_units":
            return "invalid", None
        if kind == "insufficient":
            available_display = format_quantity_display(cur_value, cur_unit)
            requested_display = format_quantity_display(value, unit)
            return "insufficient", (item["name"], available_display, requested_display)
        will_remove = remaining == 0
        new_value = None if will_remove else float(remaining)
        new_unit = None if will_remove else remaining_unit
        resolved.append({
            "item_number": num,
            "item_id": item["id"],
            "name": item["name"],
            "old_value": cur_value,
            "old_unit": cur_unit,
            "old_display": format_quantity_display(cur_value, cur_unit),
            "new_value": new_value,
            "new_unit": new_unit,
            "new_display": None if will_remove else format_quantity_display(new_value, new_unit),
            "will_remove": will_remove,
        })
    return "ok", resolved


def _format_consumption_preview(resolved):
    lines = [f"🧊 Буде використано: {len(resolved)}", ""]
    for r in resolved:
        lines.append(f"{r['item_number']}. {r['name']} — {r['old_display']}")
        if r["will_remove"]:
            lines.append("   → буде прибрано із запасів")
        else:
            lines.append(f"   → {r['name']} — {r['new_display']}")
        lines.append("")
    return "\n".join(lines).rstrip()


_COMPOUND_OP_TYPES = {"remove_inventory", "consume_inventory_quantity", "add_to_shopping"}


def _validate_compound_operations(operations, unresolved_fragments, items):
    """Validate a compound_inventory_operations router result against current inventory items.

    Returns one of:
      ("unresolved", [fragment_str, ...]) — the router flagged part of the message as
          unclear; nothing should be applied.
      ("invalid", [reason_str, ...]) — one or more operations are malformed, conflicting,
          or unsafe; nothing should be applied (no partial preview, no partial apply).
      ("ok", {"inventory_changes": [...], "add_to_shopping": [...]}) — inventory_changes
          preserves the order operations were given in (remove_inventory and
          consume_inventory_quantity entries interleaved as given), each with
          item_number, item_id, name, old_value, old_unit, old_display, new_value,
          new_unit, new_display, will_remove, op_type ("remove"|"consume").
          add_to_shopping is a list of normalized+merged item dicts ready for
          add_shopping_items_batch-style insertion.
    """
    if unresolved_fragments:
        if not isinstance(unresolved_fragments, list):
            return "unresolved", ["(не вдалося розібрати частину повідомлення)"]
        fragments = [str(f).strip() for f in unresolved_fragments if str(f).strip()]
        return "unresolved", fragments or ["(не вдалося розібрати частину повідомлення)"]

    if not isinstance(operations, list) or not operations:
        return "invalid", ["Не знайшов жодної дії для виконання."]

    total = len(items)
    reasons = []
    used_item_numbers = set()
    inventory_changes = []
    shopping_raw = []

    for op in operations:
        if not isinstance(op, dict) or op.get("type") not in _COMPOUND_OP_TYPES:
            reasons.append("Незрозуміла дія.")
            continue
        op_type = op["type"]

        if op_type in ("remove_inventory", "consume_inventory_quantity"):
            num = op.get("item_number")
            if not isinstance(num, int) or num < 1 or num > total:
                reasons.append("Невідома позиція запасів.")
                continue
            item = items[num - 1]
            if num in used_item_numbers:
                reasons.append(f"«{item['name']}» — позиція задіяна в кількох операціях одночасно.")
                continue

            if op_type == "remove_inventory":
                used_item_numbers.add(num)
                inventory_changes.append({
                    "item_number": num, "item_id": item["id"], "name": item["name"],
                    "old_value": item.get("quantity_value"), "old_unit": item.get("quantity_unit"),
                    "old_display": format_quantity_display(item.get("quantity_value"), item.get("quantity_unit")),
                    "new_value": None, "new_unit": None, "new_display": None,
                    "will_remove": True, "op_type": "remove",
                })
                continue

            # consume_inventory_quantity
            value = op.get("quantity_value")
            unit = op.get("quantity_unit")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                reasons.append(f"«{item['name']}» — не можу безпечно визначити кількість для списання. Уточни, будь ласка.")
                continue
            if unit not in STRUCTURED_UNITS:
                reasons.append(f"«{item['name']}» — невідома одиниця вимірювання.")
                continue
            cur_value = item.get("quantity_value")
            cur_unit = item.get("quantity_unit")
            if cur_value is None or cur_unit is None:
                reasons.append(f"«{item['name']}» — не вказана точна кількість, не можна безпечно списати частину.")
                continue
            kind, remaining, remaining_unit = _resolve_consumption(cur_value, cur_unit, value, unit)
            if kind == "incompatible_units":
                reasons.append(f"«{item['name']}» — несумісні одиниці для списання.")
                continue
            if kind == "insufficient":
                available_display = format_quantity_display(cur_value, cur_unit)
                requested_display = format_quantity_display(value, unit)
                reasons.append(f"«{item['name']}» — у запасах лише {available_display}, а вказано {requested_display}.")
                continue
            used_item_numbers.add(num)
            will_remove = remaining == 0
            new_value = None if will_remove else float(remaining)
            new_unit = None if will_remove else remaining_unit
            inventory_changes.append({
                "item_number": num, "item_id": item["id"], "name": item["name"],
                "old_value": cur_value, "old_unit": cur_unit,
                "old_display": format_quantity_display(cur_value, cur_unit),
                "new_value": new_value, "new_unit": new_unit,
                "new_display": None if will_remove else format_quantity_display(new_value, new_unit),
                "will_remove": will_remove, "op_type": "consume",
            })
            continue

        # add_to_shopping
        name = op.get("name")
        if not isinstance(name, str) or not name.strip():
            reasons.append("Товар для покупок без назви.")
            continue
        name = name.strip()
        if not op.get("is_consumable", True):
            reasons.append(f"«{name}» — не їстівний товар, не можу додати до покупок.")
            continue
        cat = op.get("category")
        if not isinstance(cat, str) or cat not in VALID_CATEGORIES:
            cat = DEFAULT_CATEGORY
        qty_value = op.get("quantity_value")
        qty_unit = op.get("quantity_unit")
        if (
            not isinstance(qty_value, (int, float)) or isinstance(qty_value, bool)
            or qty_value <= 0
            or not isinstance(qty_unit, str) or qty_unit not in STRUCTURED_UNITS
        ):
            qty_value, qty_unit = None, None
        normalized = normalize_item_quantity(
            name, "", quantity_value=qty_value, quantity_unit=qty_unit, allow_default_unit=(qty_value is None)
        )
        shopping_item = {"name": name, "category": cat, "was_corrected": False}
        shopping_item.update(normalized)
        shopping_raw.append(shopping_item)

    if reasons:
        return "invalid", reasons
    if not inventory_changes and not shopping_raw:
        return "invalid", ["Не знайшов жодної безпечної дії."]

    add_to_shopping = _auto_merge_in_place(shopping_raw) if shopping_raw else []
    return "ok", {"inventory_changes": inventory_changes, "add_to_shopping": add_to_shopping}


def _format_compound_preview(resolved):
    changes = resolved["inventory_changes"]
    shopping = resolved["add_to_shopping"]
    lines = ["🧊 Буде змінено в запасах:", ""]
    for i, c in enumerate(changes, start=1):
        label = c["name"]
        if c["old_display"]:
            label += f" — {c['old_display']}"
        lines.append(f"{i}. {label}")
        if c["will_remove"]:
            lines.append("   → буде прибрано із запасів")
        else:
            new_label = c["name"]
            if c["new_display"]:
                new_label += f" — {c['new_display']}"
            lines.append(f"   → {new_label}")
        lines.append("")
    if shopping:
        lines.append("🛒 Буде додано до покупок:")
        lines.append("")
        for item in shopping:
            _, _, qty_display = _effective_quantity(item)
            label = item["name"]
            if qty_display:
                label += f" — {qty_display}"
            lines.append(f"• {label}")
    return "\n".join(lines).rstrip()


def _compound_snapshot_is_stale(inventory_changes, current_items):
    """True if any inventory_changes item no longer exists, or its quantity_value/unit
    changed since the compound preview was built (detects edits from another device)."""
    current_by_id = {it["id"]: it for it in current_items}
    for c in inventory_changes:
        cur = current_by_id.get(c["item_id"])
        if cur is None or cur.get("quantity_value") != c["old_value"] or cur.get("quantity_unit") != c["old_unit"]:
            return True
    return False


# =========================
# INVENTORY SNAPSHOT RECONCILIATION
# =========================

def _find_ambiguous_unit_group(raw_items):
    """Group reconciliation raw_items by canonical_name; return the first group
    (list of item dicts) whose quantity_unit values span more than one
    _UNIT_GROUP (e.g. л/мл vs шт. for the same product), or None if none exist.
    Items with quantity_unit not in _UNIT_GROUP are ignored for this check."""
    by_name = {}
    for it in raw_items:
        canon = it.get("canonical_name") or canonicalize_name(it.get("name", ""))
        by_name.setdefault(canon, []).append(it)
    for group in by_name.values():
        groups_seen = {_UNIT_GROUP[it["quantity_unit"]] for it in group if it.get("quantity_unit") in _UNIT_GROUP}
        if len(groups_seen) > 1:
            return group
    return None


def _sum_same_group_reconcile_items(group_items):
    """Sum a list of same-canonical-name, same-_UNIT_GROUP item dicts into one,
    using Decimal canonical-unit math (mirrors _resolve_consumption's conversion).
    Result's quantity_inferred is True only if every input entry was inferred.
    Caller must have already confirmed the group has no cross-group ambiguity
    (via _find_ambiguous_unit_group returning None for this canonical_name)."""
    valued = [it for it in group_items if it.get("quantity_unit") in _UNIT_GROUP]
    if not valued:
        return dict(group_items[0])
    unit_group = _UNIT_GROUP[valued[0]["quantity_unit"]]
    total = sum(
        (Decimal(str(it["quantity_value"])) * _UNIT_TO_CANONICAL_FACTOR[it["quantity_unit"]] for it in valued),
        Decimal("0"),
    )
    merged = dict(valued[0])
    merged["quantity_value"] = float(total)
    merged["quantity_unit"] = _CANONICAL_UNIT_FOR_GROUP[unit_group]
    merged["quantity_inferred"] = all(bool(it.get("quantity_inferred")) for it in valued)
    return merged


def _validate_reconcile_snapshot(raw_items, unresolved_fragments, list_items):
    """Validate/diff a reconcile_inventory_snapshot router result against the
    current full inventory (list_items). Pure — no DB access, no side effects.

    Returns:
      ("unresolved", [fragment_str, ...])
      ("ambiguous_unit_group", {"ambiguous_group": [...], "rest": [...]})
      ("invalid", [reason_str, ...])
      ("ok", {"updates": [...], "additions": [...], "deletes": [...], "unchanged": [...]})

    updates/deletes entries carry item_id/old_value/old_unit so they can be fed
    directly into _compound_snapshot_is_stale() for staleness checks at confirm time.
    """
    if unresolved_fragments:
        if not isinstance(unresolved_fragments, list):
            return "unresolved", ["(не вдалося розібрати частину повідомлення)"]
        fragments = [str(f).strip() for f in unresolved_fragments if str(f).strip()]
        return "unresolved", fragments or ["(не вдалося розібрати частину повідомлення)"]

    if not isinstance(raw_items, list) or not raw_items:
        return "invalid", ["Порожній список — нема з чим звіряти запаси."]

    cleaned = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if not raw.get("is_consumable", True):
            continue
        qty_value = raw.get("quantity_value")
        qty_unit = raw.get("quantity_unit")
        if (
            not isinstance(qty_value, (int, float)) or isinstance(qty_value, bool)
            or qty_value <= 0
            or not isinstance(qty_unit, str) or qty_unit not in STRUCTURED_UNITS
        ):
            qty_value, qty_unit = None, None
        cat = raw.get("category")
        if not isinstance(cat, str) or cat not in VALID_CATEGORIES:
            cat = DEFAULT_CATEGORY
        cleaned.append({
            "name": name.strip(),
            "canonical_name": raw.get("canonical_name") or canonicalize_name(name),
            "category": cat,
            "quantity_value": qty_value,
            "quantity_unit": qty_unit,
            "quantity_inferred": bool(raw.get("quantity_inferred")) or qty_value is None,
        })
    if not cleaned:
        return "invalid", ["Не знайшов жодного їстівного товару у надісланому списку."]

    ambiguous_group = _find_ambiguous_unit_group(cleaned)
    if ambiguous_group is not None:
        ids = {id(it) for it in ambiguous_group}
        rest = [it for it in cleaned if id(it) not in ids]
        return "ambiguous_unit_group", {"ambiguous_group": ambiguous_group, "rest": rest}

    new_by_canon = {}
    for it in cleaned:
        new_by_canon.setdefault(it["canonical_name"], []).append(it)
    for canon, group in new_by_canon.items():
        new_by_canon[canon] = _sum_same_group_reconcile_items(group) if len(group) > 1 else group[0]

    updates, additions, deletes, unchanged = [], [], [], []
    matched_canon = set()
    for cur in list_items:
        canon = cur.get("canonical_name") or canonicalize_name(cur["name"])
        new_item = new_by_canon.get(canon)
        old_value, old_unit = cur.get("quantity_value"), cur.get("quantity_unit")
        old_display = format_quantity_display(old_value, old_unit)
        if new_item is None:
            deletes.append({
                "item_id": cur["id"], "name": cur["name"],
                "old_value": old_value, "old_unit": old_unit, "old_display": old_display,
            })
            continue
        matched_canon.add(canon)
        if new_item["quantity_inferred"]:
            # New snapshot didn't restate a real quantity for this pre-existing item —
            # never overwrite a known quantity with a guessed default, and never
            # invent one for an item that was already unspecified.
            unchanged.append({"item_id": cur["id"], "name": cur["name"], "old_display": old_display})
            continue
        new_value, new_unit = new_item["quantity_value"], new_item["quantity_unit"]
        if new_value == old_value and new_unit == old_unit:
            unchanged.append({"item_id": cur["id"], "name": cur["name"], "old_display": old_display})
        else:
            updates.append({
                "item_id": cur["id"], "name": cur["name"],
                "old_value": old_value, "old_unit": old_unit, "old_display": old_display,
                "new_value": new_value, "new_unit": new_unit,
                "new_display": format_quantity_display(new_value, new_unit),
            })

    for canon, new_item in new_by_canon.items():
        if canon in matched_canon:
            continue
        additions.append({
            "name": new_item["name"], "canonical_name": canon, "category": new_item["category"],
            "quantity_value": new_item["quantity_value"], "quantity_unit": new_item["quantity_unit"],
            "quantity_inferred": new_item["quantity_inferred"],
            "quantity_text": format_quantity_display(new_item["quantity_value"], new_item["quantity_unit"]),
        })

    if not updates and not additions and not deletes:
        return "invalid", ["Нічого не змінилося — надісланий список повністю збігається з поточними запасами."]
    return "ok", {"updates": updates, "additions": additions, "deletes": deletes, "unchanged": unchanged}


_RECONCILE_KEEP_SEPARATE_PHRASES = {"залиш окремо", "залишити окремо", "окремо", "не об'єднуй"}


def _resolve_reconciliation_unit_clarification(ambiguous_group, text):
    """Resolve a same-product/different-unit-group ambiguity from the user's free-text
    reply. Reuses _parse_structured_quantity/STRUCTURED_UNITS — no new regex engine.

    Returns ("kept_separate", None), ("merged", [merged_item]), or ("invalid", None).
    Never guesses: anything that isn't the literal keep-separate phrase or an
    unambiguous "value unit" in the matching unit group is rejected (caller re-asks).
    Only auto-resolves the simple two-entry case (one «шт.» entry + one
    volume/mass entry) — anything more complex is rejected rather than guessed at.
    """
    normalized = (text or "").strip().lower()
    if normalized in _RECONCILE_KEEP_SEPARATE_PHRASES:
        return "kept_separate", None

    value, unit = _parse_structured_quantity(text)
    if value is None or unit is None or value <= 0:
        return "invalid", None

    count_entries = [it for it in ambiguous_group if _UNIT_GROUP.get(it.get("quantity_unit")) == "count"]
    other_entries = [it for it in ambiguous_group if _UNIT_GROUP.get(it.get("quantity_unit")) not in (None, "count")]
    if len(count_entries) != 1 or len(other_entries) != 1:
        return "invalid", None
    other = other_entries[0]
    if _UNIT_GROUP.get(unit) != _UNIT_GROUP.get(other["quantity_unit"]):
        return "invalid", None

    count_item = count_entries[0]
    per_unit_canonical = Decimal(str(value)) * _UNIT_TO_CANONICAL_FACTOR[unit]
    total_from_count = per_unit_canonical * Decimal(str(count_item["quantity_value"]))
    other_canonical = Decimal(str(other["quantity_value"])) * _UNIT_TO_CANONICAL_FACTOR[other["quantity_unit"]]
    merged_canonical = total_from_count + other_canonical
    merged_unit = _CANONICAL_UNIT_FOR_GROUP[_UNIT_GROUP[unit]]

    merged_item = dict(other)
    merged_item["quantity_value"] = float(merged_canonical)
    merged_item["quantity_unit"] = merged_unit
    merged_item["quantity_inferred"] = False
    return "merged", [merged_item]


def _format_reconciliation_preview(diff):
    lines = ["🔄 Буде звірено запаси", ""]
    if diff["updates"]:
        lines.append("✏️ Зміниться:")
        lines.append("")
        for u in diff["updates"]:
            lines.append(f"• {u['name']} — {u['old_display']}")
            lines.append(f"  → {u['name']} — {u['new_display']}")
        lines.append("")
    if diff["additions"]:
        lines.append("➕ Буде додано:")
        lines.append("")
        for a in diff["additions"]:
            label = a["name"]
            if a["quantity_text"]:
                label += f" — {a['quantity_text']}"
            if a["quantity_inferred"]:
                label += " (кількість не вказана)"
            lines.append(f"• {label}")
        lines.append("")
    if diff["deletes"]:
        lines.append("➖ Буде прибрано:")
        lines.append("")
        for d in diff["deletes"]:
            label = d["name"] + (f" — {d['old_display']}" if d["old_display"] else "")
            lines.append(f"• {label}")
        lines.append("")
    if diff["unchanged"]:
        lines.append("Без змін:")
        lines.append("")
        for u in diff["unchanged"]:
            label = u["name"] + (f" — {u['old_display']}" if u["old_display"] else "")
            lines.append(f"• {label}")
        lines.append("")
    lines.append(
        "Це повне звіряння: позиції, яких немає у надісланому списку, буде прибрано лише після підтвердження."
    )
    return "\n".join(lines).rstrip()


def _format_reconciliation_unit_clarify_question(ambiguous_group):
    name = ambiguous_group[0]["name"]
    parts = [format_quantity_display(it.get("quantity_value"), it.get("quantity_unit")) for it in ambiguous_group]
    lines = [f"Бачу дві позиції {name}:", ""]
    for p in parts:
        lines.append(f"• {name} — {p}")
    lines.append("")
    lines.append("Щоб об'єднати їх в одну позицію, мені треба знати об'єм цієї упаковки.")
    lines.append("")
    lines.append("Напиши, наприклад:")
    lines.append("• 1 л")
    lines.append("• 500 мл")
    lines.append("")
    lines.append("Або напиши:")
    lines.append("• залиш окремо")
    return "\n".join(lines)


def _validate_quick_add_items(raw_items):
    """Validate Gemini quick_add_to_inventory items for an empty shopping list.

    Drops non-consumable entries (returned separately as ignored names),
    never trusts Gemini's quantity_value/unit blindly — re-derives structured
    fields locally via normalize_item_quantity, defaulting to "1 шт." inferred
    only when no safe explicit quantity was given. Duplicate items are merged
    the same way pending-add batches already are.
    Returns (items, ignored_names) or None if nothing usable remains.
    """
    if not isinstance(raw_items, list) or not raw_items:
        return None
    valid = []
    ignored = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if not raw.get("is_consumable", True):
            ignored.append(name)
            continue
        cat = raw.get("category")
        if not isinstance(cat, str) or cat not in VALID_CATEGORIES:
            cat = DEFAULT_CATEGORY
        qty_value = raw.get("quantity_value")
        qty_unit = raw.get("quantity_unit")
        if (
            not isinstance(qty_value, (int, float)) or isinstance(qty_value, bool)
            or qty_value <= 0
            or not isinstance(qty_unit, str) or qty_unit not in STRUCTURED_UNITS
        ):
            qty_value, qty_unit = None, None
        normalized = normalize_item_quantity(
            name, "", quantity_value=qty_value, quantity_unit=qty_unit, allow_default_unit=(qty_value is None)
        )
        item = {"name": name, "category": cat, "was_corrected": False}
        item.update(normalized)
        valid.append(item)
    if not valid:
        return None
    return _auto_merge_in_place(valid), ignored


def _format_quick_purchase_preview(items, ignored_items=None):
    header = f"🧊 Буде додано до запасів: {len(items)}"
    text = format_grouped_list(items, header)
    if ignored_items:
        text += "\n\nНе додано: " + ", ".join(ignored_items)
    return text


_SAVED_LIST_ROUTER_FALLBACK = {
    "intent": "none", "action": None, "selected_numbers": [], "updates": [], "merge_groups": [], "items": [],
    "consumptions": [], "operations": [], "unresolved_fragments": [],
}


def _ask_gemini_saved_list_router(user_text, items, context_type):
    """Gemini call: detect edit_saved_items, merge_duplicates, start_action or
    quick_add_to_inventory (for an empty shopping list) for an active saved list."""
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        item_qty = _effective_quantity(item)[2]
        if item_qty:
            label += f" — {item_qty}"
        label += f" [{item.get('category') or DEFAULT_CATEGORY}]"
        lines.append(label)
    prompt = (
        f"Контекст: {context_type}\n"
        "Поточний список:\n" + "\n".join(lines)
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], SAVED_LIST_EDIT_PROMPT, temperature=0.1)
    if not raw:
        return dict(_SAVED_LIST_ROUTER_FALLBACK)
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "action": data.get("action"),
            "selected_numbers": data.get("selected_numbers") if isinstance(data.get("selected_numbers"), list) else [],
            "updates": data.get("updates") if isinstance(data.get("updates"), list) else [],
            "merge_groups": data.get("merge_groups") if isinstance(data.get("merge_groups"), list) else [],
            "items": data.get("items") if isinstance(data.get("items"), list) else [],
            "consumptions": data.get("consumptions") if isinstance(data.get("consumptions"), list) else [],
            "operations": data.get("operations") if isinstance(data.get("operations"), list) else [],
            "unresolved_fragments": data.get("unresolved_fragments") if isinstance(data.get("unresolved_fragments"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_SAVED_LIST_ROUTER_FALLBACK)


def _format_saved_edit_preview(items_snapshot, validated_updates, context_type):
    """Format before/after preview for a saved list edit."""
    icon = "🛒" if context_type == "shopping_saved" else "🧊"
    lines = [f"{icon} Буде змінено: {len(validated_updates)}", ""]
    for upd in validated_updates:
        idx = upd["item_number"] - 1
        old = items_snapshot[idx]
        old_label = old["name"]
        old_qty = _effective_quantity(old)[2]
        if old_qty:
            old_label += f" — {old_qty}"
        new_name = upd.get("name") or old["name"]
        new_qty = upd.get("quantity_text")
        if new_qty is None:
            new_qty = old_qty
        new_label = new_name
        if new_qty:
            new_label += f" — {new_qty}"
        lines.append(f"{upd['item_number']}. {old_label}")
        lines.append(f"   → {new_label}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_merge_preview(validated_groups):
    lines = [f"🧹 Буде об'єднано груп: {len(validated_groups)}", ""]
    for i, group in enumerate(validated_groups):
        parts = []
        for item in group["items"]:
            label = item["name"]
            item_qty = _effective_quantity(item)[2]
            if item_qty:
                label += f" — {item_qty}"
            parts.append(label)
        result = group["merged_name"]
        if group["merged_quantity_text"]:
            result += f" — {group['merged_quantity_text']}"
        lines.append(f"{i + 1}. {' + '.join(parts)}")
        lines.append(f"   → {result}")
    return "\n".join(lines)

# =========================
# SELECTION / PREVIEW HELPERS
# =========================

def _validate_selected_numbers(numbers, items):
    """Validate raw selected_numbers against the current items list.

    Returns an ordered (as given), deduped list of item dicts, dropping
    out-of-range numbers individually rather than invalidating the whole
    selection. Returns None if numbers isn't a list or nothing remains.
    """
    if not isinstance(numbers, list):
        return None
    total = len(items)
    seen = set()
    selected = []
    for n in numbers:
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= total and n not in seen:
            seen.add(n)
            selected.append(items[n - 1])
    return selected if selected else None


def _snapshot_is_stale(item_ids, current_items):
    """True if any snapshot item id is no longer present in the current list."""
    current_ids = {it["id"] for it in current_items}
    return not set(item_ids).issubset(current_ids)


def _snapshot_targets(items):
    """Build a {item_id, quantity_value, quantity_unit} snapshot-target list for the
    shared stale-precondition guard (database._verify_targets_in_tx). This is the one
    reusable mechanism every confirm-flow uses to describe "what did these target rows
    look like when the preview was built" — the actual check-and-mutate happens inside
    a single transaction on the database side, never as a separate pre-check.

    Accepts either raw inventory/shopping item dicts (id, quantity_value, quantity_unit,
    as returned by get_inventory_items/get_active_shopping_items) or already-resolved
    change dicts (item_id, old_value, old_unit, as built by _validate_consumptions/
    _validate_compound_operations/_validate_reconcile_snapshot/_validate_saved_updates)
    — whichever shape is present.
    """
    targets = []
    for it in items:
        item_id = it["item_id"] if "item_id" in it else it["id"]
        value = it["old_value"] if "old_value" in it else it.get("quantity_value")
        unit = it["old_unit"] if "old_unit" in it else it.get("quantity_unit")
        targets.append({"item_id": item_id, "quantity_value": value, "quantity_unit": unit})
    return targets


def _should_restore_persisted_context(chat_id):
    """True if there's no RAM saved_list_context and no other active preview
    or special mode that must take priority over restoring a persisted context.

    shopping_mode/inventory_mode and pending_batch/pending_inventory_batch are
    intentionally not checked here — they're already excluded by the time this
    is reached (handled earlier in webhook() with their own early returns, or
    by the outer if/elif around the saved_list_context branch).
    """
    if saved_list_context.get(chat_id) is not None:
        return False
    return not any(
        chat_id in d for d in (
            pending_mark_batch, pending_delete_batch, pending_remove_batch,
            pending_saved_edit, pending_quick_purchase, pending_merge,
            pending_inventory_consumption, pending_compound_inventory,
            pending_inventory_reconciliation, pending_inventory_reconciliation_clarify,
        )
    )


def _ask_gemini_for_selection(user_text, items, list_label, action_label):
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        item_qty = _effective_quantity(item)[2]
        if item_qty:
            label += f" — {item_qty}"
        if item.get("category"):
            label += f" [{item['category']}]"
        lines.append(label)
    prompt = (
        f"{list_label} (дія: {action_label}):\n"
        + "\n".join(lines)
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], SELECTION_PROMPT, temperature=0.1)
    if not raw:
        return None
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return _validate_selected_numbers(data.get("selected_numbers"), items)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None

def _show_mark_preview(chat_id, items, household_id, user_db_id):
    pending_mark_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    preview = format_grouped_list(items, f"🛒 Буде позначено купленими: {len(items)}")
    send_message(chat_id, preview + "\n\nЩо зробити з цими товарами?", reply_markup=MARK_PREVIEW_KEYBOARD)

def _show_delete_preview(chat_id, items, household_id, user_db_id):
    pending_delete_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    preview = format_grouped_list(items, f"🗑️ Буде видалено зі списку покупок: {len(items)}")
    send_message(chat_id, preview, reply_markup=DELETE_PREVIEW_KEYBOARD)

def _show_remove_preview(chat_id, items, household_id, user_db_id):
    pending_remove_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    preview = format_grouped_list(items, f"🧊 Буде прибрано із запасів: {len(items)}")
    send_message(chat_id, preview, reply_markup=REMOVE_PREVIEW_KEYBOARD)

def parse_shopping_list_with_gemini(text):
    history = [{"role": "user", "content": text}]
    raw = call_gemini(history, SHOPPING_PARSE_PROMPT, temperature=0.1)
    if not raw:
        return None
    cleaned = raw.strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if match:
            cleaned = match.group(1).strip()
    try:
        data = json.loads(cleaned)
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return None
        ignored = list(data.get("ignored_items") or [])
        consumable = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "").strip()
            if not name:
                continue
            if not item.get("is_consumable", True):
                ignored.append(name)
                continue
            cat = item.get("category", "").strip()
            if cat not in VALID_CATEGORIES:
                cat = DEFAULT_CATEGORY
            normalized = normalize_item_quantity(name, item.get("quantity_text", "").strip(), allow_default_unit=True)
            entry = {
                "name": name,
                "category": cat,
                "was_corrected": bool(item.get("was_corrected", False)),
            }
            entry.update(normalized)
            consumable.append(entry)
        if not consumable and not ignored:
            return None
        return {"items": consumable, "ignored_items": ignored}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

def format_batch_preview(items, ignored_items=None):
    header = f"🛒 Знайшов товарів: {len(items)}"
    text = format_grouped_list(items, header)
    if ignored_items:
        text += "\n\nНе додано: " + ", ".join(ignored_items)
    return text

@app.route("/")
def home():
    return "Bot is running"

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()

    if _is_duplicate_update(data.get("update_id")):
        return "ok"

    message = data.get("message")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if not text:
        return "ok"

    user_id = message.get("from", {}).get("id")
    display_name = message.get("from", {}).get("first_name")

    if text == "/myid":
        send_message(chat_id, f"Твій Telegram ID: {user_id}")
        return "ok"

    # =========================
    # ACCESS CHECK
    # =========================
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        send_message(chat_id, "Цей бот приватний і доступний лише для дозволених користувачів.")
        return "ok"

    # =========================
    # BUTTON HANDLERS
    # =========================

    if text == "✅ Об'єднати":
        if chat_id in pending_merge:
            merge_data = pending_merge.pop(chat_id)
            list_type = merge_data["list_type"]
            if list_type == "shopping_pending_add":
                batch = pending_batch.get(chat_id)
                if batch:
                    batch["items"] = _apply_pending_merge(batch["items"], merge_data["groups"])
                    preview = format_batch_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Список вже не в пам'яті.", reply_markup=SHOPPING_KEYBOARD)
            elif list_type == "inventory_pending_add":
                batch = pending_inventory_batch.get(chat_id)
                if batch:
                    batch["items"] = _apply_pending_merge(batch["items"], merge_data["groups"])
                    preview = format_inventory_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_INVENTORY_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Список вже не в пам'яті.", reply_markup=INVENTORY_KEYBOARD)
            elif list_type == "shopping_saved":
                try:
                    count = execute_merge_shopping(merge_data["household_id"], merge_data["groups"])
                    send_message(chat_id, f"✅ Об'єднано груп: {count}", reply_markup=SHOPPING_KEYBOARD)
                except Exception:
                    send_message(chat_id, "Не вдалося виконати об'єднання. Спробуйте ще раз.", reply_markup=SHOPPING_KEYBOARD)
            elif list_type == "inventory_saved":
                try:
                    count = execute_merge_inventory(merge_data["household_id"], merge_data["groups"])
                    send_message(chat_id, f"✅ Об'єднано груп: {count}", reply_markup=INVENTORY_KEYBOARD)
                except Exception:
                    send_message(chat_id, "Не вдалося виконати об'єднання. Спробуйте ще раз.", reply_markup=INVENTORY_KEYBOARD)
        return "ok"

    if text == "✅ Додати все":
        if chat_id in pending_inventory_batch:
            batch = pending_inventory_batch.pop(chat_id)
            try:
                count = add_inventory_items_batch(
                    batch["household_id"],
                    batch["user_db_id"],
                    batch["items"]
                )
                send_message(chat_id, f"✅ Додано до запасів: {count}", reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        elif chat_id in pending_batch:
            batch = pending_batch.pop(chat_id)
            try:
                count = add_shopping_items_batch(
                    batch["household_id"],
                    batch["user_db_id"],
                    batch["items"]
                )
                send_message(chat_id, f"✅ Додано товарів: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "✏️ Надіслати інший список":
        if chat_id in pending_inventory_batch:
            pending_inventory_batch.pop(chat_id, None)
            inventory_mode[chat_id] = "adding"
            send_message(chat_id, "Надішли один продукт або список продуктів. Можна кожен продукт з нового рядка.")
        else:
            pending_batch.pop(chat_id, None)
            shopping_mode[chat_id] = "adding"
            send_message(chat_id, "Надішли один товар або список товарів. Можна кожен товар з нового рядка.")
        return "ok"

    if text == "❌ Скасувати":
        if chat_id in pending_merge:
            merge_data = pending_merge.pop(chat_id)
            list_type = merge_data["list_type"]
            if list_type == "shopping_pending_add":
                batch = pending_batch.get(chat_id)
                if batch:
                    preview = format_batch_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Об'єднання скасовано.", reply_markup=SHOPPING_KEYBOARD)
            elif list_type == "inventory_pending_add":
                batch = pending_inventory_batch.get(chat_id)
                if batch:
                    preview = format_inventory_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_INVENTORY_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Об'єднання скасовано.", reply_markup=INVENTORY_KEYBOARD)
            else:
                keyboard = SHOPPING_KEYBOARD if list_type == "shopping_saved" else INVENTORY_KEYBOARD
                send_message(chat_id, "Об'єднання скасовано.", reply_markup=keyboard)
        elif chat_id in pending_inventory_batch:
            clear_inventory_state(chat_id)
            send_message(chat_id, "Додавання продуктів скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_mark_batch:
            pending_mark_batch.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=SHOPPING_KEYBOARD)
        elif chat_id in pending_delete_batch:
            pending_delete_batch.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=SHOPPING_KEYBOARD)
        elif chat_id in pending_remove_batch:
            pending_remove_batch.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_saved_edit:
            edit_data = pending_saved_edit.pop(chat_id)
            ctx = edit_data["context_type"]
            keyboard = SHOPPING_KEYBOARD if ctx == "shopping_saved" else INVENTORY_KEYBOARD
            send_message(chat_id, "Зміни скасовано.", reply_markup=keyboard)
        elif chat_id in pending_inventory_consumption:
            pending_inventory_consumption.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_compound_inventory:
            pending_compound_inventory.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_inventory_reconciliation:
            pending_inventory_reconciliation.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_inventory_reconciliation_clarify:
            pending_inventory_reconciliation_clarify.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_quick_purchase:
            pending_quick_purchase.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=SHOPPING_KEYBOARD)
        else:
            clear_shopping_state(chat_id)
            send_message(chat_id, "Додавання товарів скасовано.", reply_markup=SHOPPING_KEYBOARD)
        return "ok"

    if text == "✏️ Виправити позицію":
        if chat_id in pending_batch:
            n = len(pending_batch[chat_id]["items"])
            shopping_mode[chat_id] = "editing_number"
            send_message(chat_id, f"Напиши номер позиції для виправлення (1–{n}):")
        return "ok"

    if text == "✅ Куплено + додати в запаси":
        if chat_id in pending_mark_batch:
            mark_data = pending_mark_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in mark_data["items"]]
                targets = _snapshot_targets(mark_data["items"])
                count = mark_items_batch(mark_data["household_id"], item_ids, mark_data["user_db_id"], targets)
                for item in mark_data["items"]:
                    add_or_merge_inventory_item(
                        mark_data["household_id"],
                        mark_data["user_db_id"],
                        item["name"],
                        item.get("quantity_text", ""),
                        item.get("category", DEFAULT_CATEGORY),
                        canonical_name=item.get("canonical_name"),
                        quantity_value=item.get("quantity_value"),
                        quantity_unit=item.get("quantity_unit"),
                        quantity_inferred=item.get("quantity_inferred", False),
                    )
                send_message(chat_id, f"✅ Куплено й додано до запасів: {count}", reply_markup=SHOPPING_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, "Не вдалося завершити покупку. Спробуйте ще раз трохи пізніше.")
        return "ok"

    if text == "✅ Куплено, без запасів":
        if chat_id in pending_mark_batch:
            mark_data = pending_mark_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in mark_data["items"]]
                targets = _snapshot_targets(mark_data["items"])
                count = mark_items_batch(mark_data["household_id"], item_ids, mark_data["user_db_id"], targets)
                send_message(chat_id, f"✅ Позначено купленими: {count}", reply_markup=SHOPPING_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, "Не вдалося завершити покупку. Спробуйте ще раз трохи пізніше.")
        return "ok"

    if text == "✅ Так, видалити":
        if chat_id in pending_delete_batch:
            del_data = pending_delete_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in del_data["items"]]
                targets = _snapshot_targets(del_data["items"])
                count = delete_items_batch(del_data["household_id"], item_ids, targets)
                send_message(chat_id, f"🗑️ Видалено зі списку: {count}", reply_markup=SHOPPING_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "✅ Так, прибрати":
        if chat_id in pending_remove_batch:
            rem_data = pending_remove_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in rem_data["items"]]
                targets = _snapshot_targets(rem_data["items"])
                count = delete_inventory_items_batch(rem_data["household_id"], item_ids, targets)
                send_message(chat_id, f"✅ Прибрано із запасів: {count}", reply_markup=INVENTORY_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "✅ Додати до запасів":
        if chat_id in pending_quick_purchase:
            purchase = pending_quick_purchase.pop(chat_id)
            try:
                count = add_inventory_items_batch(
                    purchase["household_id"],
                    purchase["user_db_id"],
                    purchase["items"],
                )
                send_message(chat_id, f"✅ Додано до запасів: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "✏️ Змінити список":
        if chat_id in pending_quick_purchase:
            pending_quick_purchase.pop(chat_id, None)
            saved_list_context[chat_id] = "shopping_saved"
            send_message(chat_id, "Напиши, які товари ти купив:")
        return "ok"

    if text == "✅ Підтвердити зміни":
        if chat_id in pending_saved_edit:
            edit_data = pending_saved_edit.pop(chat_id)
            ctx = edit_data["context_type"]
            household_id = edit_data["household_id"]
            valid_updates = edit_data["validated_updates"]
            keyboard = SHOPPING_KEYBOARD if ctx == "shopping_saved" else INVENTORY_KEYBOARD
            try:
                if ctx == "shopping_saved":
                    update_shopping_items_batch(household_id, valid_updates)
                else:
                    update_inventory_items_batch(household_id, valid_updates)
                send_message(chat_id, "✅ Зміни застосовано.", reply_markup=keyboard)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=keyboard)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG if ctx == "shopping_saved" else INVENTORY_ERROR_MSG)
        elif chat_id in pending_inventory_consumption:
            consume_data = pending_inventory_consumption.pop(chat_id)
            household_id = consume_data["household_id"]
            resolved = consume_data["resolved"]
            try:
                targets = _snapshot_targets(resolved)
                updates = [
                    {
                        "item_id": r["item_id"],
                        "quantity_value": r["new_value"],
                        "quantity_unit": r["new_unit"],
                        "quantity_text": r["new_display"],
                    }
                    for r in resolved if not r["will_remove"]
                ]
                delete_ids = [r["item_id"] for r in resolved if r["will_remove"]]
                updated, deleted = apply_inventory_consumption(household_id, updates, delete_ids, targets)
                send_message(chat_id, f"✅ Оновлено запасів: {updated + deleted}", reply_markup=INVENTORY_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "✅ Підтвердити всі зміни":
        if chat_id in pending_compound_inventory:
            compound_data = pending_compound_inventory.pop(chat_id)
            household_id = compound_data["household_id"]
            user_db_id = compound_data["user_db_id"]
            inventory_changes = compound_data["inventory_changes"]
            add_to_shopping = compound_data["add_to_shopping"]
            try:
                targets = _snapshot_targets(inventory_changes)
                consume_updates = [
                    {
                        "item_id": c["item_id"],
                        "quantity_value": c["new_value"],
                        "quantity_unit": c["new_unit"],
                        "quantity_text": c["new_display"],
                    }
                    for c in inventory_changes if not c["will_remove"]
                ]
                delete_ids = [c["item_id"] for c in inventory_changes if c["will_remove"]]
                inv_updated, inv_deleted, shopping_added = apply_compound_inventory_operations(
                    household_id, user_db_id, consume_updates, delete_ids, add_to_shopping, targets
                )
                if shopping_added:
                    send_message(
                        chat_id,
                        f"✅ Зміни застосовано.\n\nОновлено запасів: {inv_updated + inv_deleted}\n"
                        f"Додано до покупок: {shopping_added}",
                        reply_markup=INVENTORY_KEYBOARD,
                    )
                else:
                    send_message(
                        chat_id,
                        f"✅ Зміни запасів застосовано: {inv_updated + inv_deleted}",
                        reply_markup=INVENTORY_KEYBOARD,
                    )
            except StaleSnapshotError:
                send_message(chat_id, "Список змінився з іншого пристрою. Онови запаси й повтори дію.", reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "✅ Підтвердити звіряння":
        if chat_id in pending_inventory_reconciliation:
            recon_data = pending_inventory_reconciliation.pop(chat_id)
            household_id = recon_data["household_id"]
            user_db_id = recon_data["user_db_id"]
            try:
                targets = _snapshot_targets(recon_data["updates"] + recon_data["deletes"])
                updates_for_db = [
                    {
                        "item_id": u["item_id"],
                        "quantity_value": u["new_value"],
                        "quantity_unit": u["new_unit"],
                        "quantity_text": u["new_display"],
                    }
                    for u in recon_data["updates"]
                ]
                delete_ids = [d["item_id"] for d in recon_data["deletes"]]
                apply_inventory_reconciliation(
                    household_id, user_db_id, updates_for_db, recon_data["additions"], delete_ids, targets
                )
                send_message(chat_id, "✅ Запаси звірено.", reply_markup=INVENTORY_KEYBOARD)
                send_message(chat_id, format_inventory_list(get_inventory_items(household_id)))
            except StaleSnapshotError:
                send_message(
                    chat_id,
                    "Список змінився з іншого пристрою. Онови запаси й повтори звіряння.",
                    reply_markup=INVENTORY_KEYBOARD,
                )
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "✏️ Змінити вибір":
        if chat_id in pending_mark_batch:
            mark_data = pending_mark_batch.pop(chat_id)
            try:
                items = get_active_shopping_items(mark_data["household_id"])
                shopping_mode[chat_id] = "marking"
                if not items:
                    send_message(chat_id, "Список покупок поки порожній.")
                else:
                    send_message(chat_id, format_shopping_list(items) + "\n\nНапиши, що саме купив:")
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        elif chat_id in pending_delete_batch:
            del_data = pending_delete_batch.pop(chat_id)
            try:
                items = get_active_shopping_items(del_data["household_id"])
                shopping_mode[chat_id] = "deleting"
                if not items:
                    send_message(chat_id, "Список покупок поки порожній.")
                else:
                    send_message(chat_id, format_shopping_list(items) + "\n\nНапиши, що видалити:")
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        elif chat_id in pending_remove_batch:
            rem_data = pending_remove_batch.pop(chat_id)
            try:
                items = get_inventory_items(rem_data["household_id"])
                inventory_mode[chat_id] = "removing"
                if not items:
                    send_message(chat_id, "Запаси поки порожні.")
                else:
                    send_message(chat_id, format_inventory_list(items) + "\n\nНапиши, що прибрати із запасів:")
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    # =========================
    # NAVIGATION BUTTONS
    # =========================

    if text == "/start":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context.pop(chat_id, None)
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        clear_list_context(chat_id)
        send_message(
            chat_id,
            "Привіт! Я твій домашній помічник 🏠\n\n"
            "Обери дію на клавіатурі або напиши будь-яке запитання — я відповім за допомогою AI.",
            reply_markup=MAIN_KEYBOARD
        )
        return "ok"

    if text == "/menu":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context.pop(chat_id, None)
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        clear_list_context(chat_id)
        send_message(chat_id, "Ось головне меню:", reply_markup=MAIN_KEYBOARD)
        return "ok"

    if text == "/help":
        send_message(
            chat_id,
            "ℹ️ Як користуватися ботом:\n\n"
            "🛒 Покупки — спільний список покупок\n"
            "🧊 Запаси — що є вдома\n"
            "🍽️ Що приготувати — ідеї страв на основі запасів\n"
            "ℹ️ Допомога — ця інструкція\n\n"
            "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
        )
        return "ok"

    if text == "🛒 Покупки":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context[chat_id] = "shopping"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        saved_list_context[chat_id] = "shopping_saved"
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            save_list_context(chat_id, household_id, "shopping_saved")
            items = get_active_shopping_items(household_id)
            send_message(chat_id, format_shopping_list(items), reply_markup=SHOPPING_KEYBOARD)
        except Exception:
            send_message(chat_id, DB_ERROR_MSG, reply_markup=SHOPPING_KEYBOARD)
        return "ok"

    if text == "➕ Додати товар":
        active_list_context[chat_id] = "shopping"
        clear_shopping_state(chat_id)
        shopping_mode[chat_id] = "adding"
        send_message(chat_id, "Надішли один товар або список товарів. Можна кожен товар з нового рядка.")
        return "ok"

    if text == "📋 Показати список":
        active_list_context[chat_id] = "shopping"
        clear_shopping_state(chat_id)
        saved_list_context[chat_id] = "shopping_saved"
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            save_list_context(chat_id, household_id, "shopping_saved")
            items = get_active_shopping_items(household_id)
            send_message(chat_id, format_shopping_list(items))
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "✅ Позначити купленим":
        active_list_context[chat_id] = "shopping"
        clear_shopping_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)
            if not items:
                send_message(chat_id, "Список покупок поки порожній.")
            else:
                send_message(chat_id, format_shopping_list(items) + "\n\nНапиши, що купив:")
                shopping_mode[chat_id] = "marking"
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "🗑️ Видалити товар":
        active_list_context[chat_id] = "shopping"
        clear_shopping_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)
            if not items:
                send_message(chat_id, "Список покупок поки порожній.")
            else:
                send_message(chat_id, format_shopping_list(items) + "\n\nНапиши, що видалити:")
                shopping_mode[chat_id] = "deleting"
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "⬅️ Головне меню":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context.pop(chat_id, None)
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        clear_list_context(chat_id)
        send_message(chat_id, "Ось головне меню:", reply_markup=MAIN_KEYBOARD)
        return "ok"

    if text == "🧊 Запаси":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context[chat_id] = "inventory"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        saved_list_context[chat_id] = "inventory_saved"
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            save_list_context(chat_id, household_id, "inventory_saved")
            items = get_inventory_items(household_id)
            send_message(chat_id, format_inventory_list(items), reply_markup=INVENTORY_KEYBOARD)
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG, reply_markup=INVENTORY_KEYBOARD)
        return "ok"

    if text == "➕ Додати продукти":
        active_list_context[chat_id] = "inventory"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        inventory_mode[chat_id] = "adding"
        send_message(chat_id, "Надішли один продукт або список продуктів. Можна кожен продукт з нового рядка.")
        return "ok"

    if text == "📋 Показати запаси":
        active_list_context[chat_id] = "inventory"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        saved_list_context[chat_id] = "inventory_saved"
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            save_list_context(chat_id, household_id, "inventory_saved")
            items = get_inventory_items(household_id)
            send_message(chat_id, format_inventory_list(items))
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "➖ Використати / прибрати":
        active_list_context[chat_id] = "inventory"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_inventory_items(household_id)
            if not items:
                send_message(chat_id, "Запаси поки порожні.")
            else:
                send_message(chat_id, format_inventory_list(items) + "\n\nНапиши, що прибрати із запасів:")
                inventory_mode[chat_id] = "removing"
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "🍽️ Що приготувати":
        active_list_context.pop(chat_id, None)
        clear_shopping_state(chat_id)
        waiting_for_ingredients[chat_id] = True
        send_message(chat_id, "Напиши, які продукти зараз є вдома, і я запропоную кілька страв.")
        return "ok"

    if text == "ℹ️ Допомога":
        send_message(
            chat_id,
            "ℹ️ Як користуватися ботом:\n\n"
            "🛒 Покупки — спільний список покупок\n"
            "🧊 Запаси — що є вдома\n"
            "🍽️ Що приготувати — ідеї страв на основі запасів\n"
            "ℹ️ Допомога — ця інструкція\n\n"
            "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
        )
        return "ok"

    # =========================
    # SHOPPING MODE
    # =========================
    mode = shopping_mode.pop(chat_id, None)

    if mode == "adding":
        result = parse_shopping_list_with_gemini(text)
        if result is None:
            shopping_mode[chat_id] = "adding"
            send_message(
                chat_id,
                "Не зміг точно розібрати список. Надішли товари ще раз, бажано кожен з нового рядка."
            )
            return "ok"
        items = result["items"]
        if not items:
            shopping_mode[chat_id] = "adding"
            ignored = result["ignored_items"]
            msg = "Не знайшов їстівних товарів у списку. Надішли ще раз."
            if ignored:
                msg += "\n\nНе додано: " + ", ".join(ignored)
            send_message(chat_id, msg)
            return "ok"
        items = _auto_merge_in_place(items)
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            pending_batch[chat_id] = {
                "items": items,
                "ignored_items": result["ignored_items"],
                "household_id": household_id,
                "user_db_id": user_db_id,
            }
            preview = format_batch_preview(items, result["ignored_items"])
            send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if mode == "editing_number":
        batch = pending_batch.get(chat_id)
        if not batch:
            return "ok"
        try:
            num = int(text.strip())
            if num < 1 or num > len(batch["items"]):
                shopping_mode[chat_id] = "editing_number"
                send_message(chat_id, f"Такого номера немає. Напиши число від 1 до {len(batch['items'])}:")
                return "ok"
            batch["edit_index"] = num - 1
            shopping_mode[chat_id] = "editing_text"
            send_message(chat_id, "Надішли нову назву або «назва — кількість»:")
        except ValueError:
            shopping_mode[chat_id] = "editing_number"
            send_message(chat_id, "Напиши номер позиції (числом):")
        return "ok"

    if mode == "editing_text":
        batch = pending_batch.get(chat_id)
        if not batch:
            return "ok"
        idx = batch.pop("edit_index", None)
        if idx is None or idx >= len(batch["items"]):
            return "ok"
        name, quantity_text = parse_item_text(text)
        batch["items"][idx]["name"] = name
        batch["items"][idx]["was_corrected"] = False
        normalized = normalize_item_quantity(name, quantity_text or "", allow_default_unit=True)
        batch["items"][idx].update(normalized)
        preview = format_batch_preview(batch["items"], batch.get("ignored_items"))
        send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
        return "ok"

    if mode == "marking":
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)
            if not items:
                send_message(chat_id, "Список покупок поки порожній.")
                return "ok"
            selected = _ask_gemini_for_selection(text, items, "Список покупок", "позначити купленими")
            if selected is None:
                send_message(chat_id, SELECTION_ERROR_MSG)
                shopping_mode[chat_id] = "marking"
            else:
                _show_mark_preview(chat_id, selected, household_id, user_db_id)
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if mode == "deleting":
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)
            if not items:
                send_message(chat_id, "Список покупок поки порожній.")
                return "ok"
            selected = _ask_gemini_for_selection(text, items, "Список покупок", "видалити зі списку")
            if selected is None:
                send_message(chat_id, SELECTION_ERROR_MSG)
                shopping_mode[chat_id] = "deleting"
            else:
                _show_delete_preview(chat_id, selected, household_id, user_db_id)
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    # =========================
    # INVENTORY MODE
    # =========================
    inv_mode = inventory_mode.pop(chat_id, None)

    if inv_mode == "adding":
        result = parse_shopping_list_with_gemini(text)
        if result is None:
            inventory_mode[chat_id] = "adding"
            send_message(
                chat_id,
                "Не зміг точно розібрати список. Надішли продукти ще раз, бажано кожен з нового рядка."
            )
            return "ok"
        items = result["items"]
        if not items:
            inventory_mode[chat_id] = "adding"
            ignored = result["ignored_items"]
            msg = "Не знайшов їстівних продуктів у списку. Надішли ще раз."
            if ignored:
                msg += "\n\nНе додано: " + ", ".join(ignored)
            send_message(chat_id, msg)
            return "ok"
        items = _auto_merge_in_place(items)
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            pending_inventory_batch[chat_id] = {
                "items": items,
                "ignored_items": result["ignored_items"],
                "household_id": household_id,
                "user_db_id": user_db_id,
            }
            preview = format_inventory_preview(items, result["ignored_items"])
            send_message(chat_id, preview, reply_markup=ADD_INVENTORY_PREVIEW_KEYBOARD)
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if inv_mode == "removing":
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            items = get_inventory_items(household_id)
            if not items:
                send_message(chat_id, "Запаси поки порожні.")
                return "ok"
            selected = _ask_gemini_for_selection(text, items, "Список запасів", "прибрати із запасів")
            if selected is None:
                send_message(chat_id, SELECTION_ERROR_MSG)
                inventory_mode[chat_id] = "removing"
            else:
                _show_remove_preview(chat_id, selected, household_id, user_db_id)
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    # =========================
    # PENDING PREVIEW ROUTER
    # Intercepts text when a pending add preview is active (shopping or inventory).
    # Priority: edit_preview → apply + show preview; merge_duplicates → local merge;
    # none → fall through to AI chat.
    # No DB writes until ✅ Додати все.
    # Saved list context (3rd branch): only merge_duplicates, unchanged.
    # =========================
    _preview_intercepted = False

    if chat_id in pending_batch:
        batch = pending_batch[chat_id]
        try:
            router_result = _ask_gemini_preview_edit_router(text, batch["items"], "shopping_pending_add")
            intent = router_result["intent"]
            if intent == "edit_preview":
                valid_updates = _validate_preview_updates(router_result["updates"], batch["items"])
                if valid_updates:
                    batch["items"] = _apply_preview_updates(batch["items"], valid_updates)
                    preview = format_batch_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
                _preview_intercepted = True
            elif intent == "merge_duplicates":
                merged = _auto_merge_in_place(batch["items"])
                if len(merged) < len(batch["items"]):
                    batch["items"] = merged
                    preview = format_batch_preview(merged, batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Не знайшов безпечних дублікатів для об'єднання.")
                _preview_intercepted = True
            # intent == "none": fall through to AI chat
        except Exception:
            send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
            _preview_intercepted = True

    elif chat_id in pending_inventory_batch:
        batch = pending_inventory_batch[chat_id]
        try:
            router_result = _ask_gemini_preview_edit_router(text, batch["items"], "inventory_pending_add")
            intent = router_result["intent"]
            if intent == "edit_preview":
                valid_updates = _validate_preview_updates(router_result["updates"], batch["items"])
                if valid_updates:
                    batch["items"] = _apply_preview_updates(batch["items"], valid_updates)
                    preview = format_inventory_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_INVENTORY_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
                _preview_intercepted = True
            elif intent == "merge_duplicates":
                merged = _auto_merge_in_place(batch["items"])
                if len(merged) < len(batch["items"]):
                    batch["items"] = merged
                    preview = format_inventory_preview(merged, batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_INVENTORY_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Не знайшов безпечних дублікатів для об'єднання.")
                _preview_intercepted = True
            # intent == "none": fall through to AI chat
        except Exception:
            send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
            _preview_intercepted = True

    elif chat_id in pending_inventory_reconciliation_clarify:
        clarify_data = pending_inventory_reconciliation_clarify[chat_id]
        kind, resolved = _resolve_reconciliation_unit_clarification(clarify_data["ambiguous_group"], text)
        if kind == "invalid":
            send_message(chat_id, _format_reconciliation_unit_clarify_question(clarify_data["ambiguous_group"]))
            _preview_intercepted = True
        else:
            pending_inventory_reconciliation_clarify.pop(chat_id, None)
            combined = clarify_data["rest"] + (resolved if kind == "merged" else clarify_data["ambiguous_group"])
            household_id = clarify_data["household_id"]
            user_db_id = clarify_data["user_db_id"]
            try:
                list_items = get_inventory_items(household_id)
                next_ambiguous = _find_ambiguous_unit_group(combined)
                if next_ambiguous is not None:
                    ids = {id(it) for it in next_ambiguous}
                    rest2 = [it for it in combined if id(it) not in ids]
                    pending_inventory_reconciliation_clarify[chat_id] = {
                        "ambiguous_group": next_ambiguous, "rest": rest2,
                        "household_id": household_id, "user_db_id": user_db_id,
                    }
                    send_message(chat_id, _format_reconciliation_unit_clarify_question(next_ambiguous))
                else:
                    kind2, payload2 = _validate_reconcile_snapshot(combined, [], list_items)
                    if kind2 == "ok":
                        pending_inventory_reconciliation[chat_id] = {
                            "updates": payload2["updates"], "additions": payload2["additions"],
                            "deletes": payload2["deletes"], "household_id": household_id, "user_db_id": user_db_id,
                        }
                        send_message(
                            chat_id, _format_reconciliation_preview(payload2), reply_markup=RECONCILIATION_PREVIEW_KEYBOARD
                        )
                    else:
                        send_message(
                            chat_id,
                            "Не зміг безпечно завершити звіряння запасів. Спробуй ще раз, надіславши повний список.",
                        )
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
            _preview_intercepted = True

    else:
        ctx = saved_list_context.get(chat_id)
        if _should_restore_persisted_context(chat_id):
            # Try restoring the last opened list from PostgreSQL — survives
            # restart/deploy, TTL 24h.
            try:
                household_id, _ = get_household_and_user(user_id, display_name)
                persisted = get_list_context(chat_id, household_id)
                if persisted in ("shopping_saved", "inventory_saved"):
                    ctx = persisted
                    saved_list_context[chat_id] = ctx
            except Exception:
                pass
        if ctx in ("shopping_saved", "inventory_saved"):
            try:
                household_id, user_db_id = get_household_and_user(user_id, display_name)
                list_items = (
                    get_active_shopping_items(household_id)
                    if ctx == "shopping_saved"
                    else get_inventory_items(household_id)
                )
                if list_items:
                    router_result = _ask_gemini_saved_list_router(text, list_items, ctx)
                    intent = router_result["intent"]
                    if intent == "edit_saved_items":
                        valid_updates = _validate_saved_updates(router_result["updates"], list_items)
                        if valid_updates:
                            pending_saved_edit[chat_id] = {
                                "items_snapshot": list_items,
                                "validated_updates": valid_updates,
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                                "context_type": ctx,
                            }
                            preview = _format_saved_edit_preview(list_items, valid_updates, ctx)
                            send_message(chat_id, preview, reply_markup=SAVED_EDIT_PREVIEW_KEYBOARD)
                        else:
                            send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
                        _preview_intercepted = True
                    elif intent == "merge_duplicates":
                        validated_groups = _compute_saved_merge_groups(router_result["merge_groups"], list_items)
                        if validated_groups:
                            pending_merge[chat_id] = {
                                "groups": validated_groups,
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                                "list_type": ctx,
                            }
                            send_message(chat_id, _format_merge_preview(validated_groups), reply_markup=MERGE_PREVIEW_KEYBOARD)
                        else:
                            send_message(chat_id, "Не знайшов безпечних дублікатів для об'єднання.")
                        _preview_intercepted = True
                    elif intent == "start_action":
                        selected = _validate_start_action(
                            router_result.get("action"), router_result.get("selected_numbers"), ctx, list_items
                        )
                        if selected is not None:
                            saved_list_context.pop(chat_id, None)
                            action = router_result.get("action")
                            if action == "mark_bought":
                                _show_mark_preview(chat_id, selected, household_id, user_db_id)
                            elif action == "delete_shopping":
                                _show_delete_preview(chat_id, selected, household_id, user_db_id)
                            elif action == "remove_inventory":
                                _show_remove_preview(chat_id, selected, household_id, user_db_id)
                        else:
                            send_message(chat_id, "Не зміг безпечно зрозуміти дію. Спробуй написати інакше.")
                        _preview_intercepted = True
                    elif intent == "consume_inventory_quantity" and ctx == "inventory_saved":
                        kind, payload = _validate_consumptions(router_result.get("consumptions"), list_items)
                        if kind == "ok":
                            pending_inventory_consumption[chat_id] = {
                                "resolved": payload,
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                            }
                            send_message(
                                chat_id, _format_consumption_preview(payload), reply_markup=SAVED_EDIT_PREVIEW_KEYBOARD
                            )
                        elif kind == "missing_quantity":
                            send_message(
                                chat_id,
                                f"Не можу безпечно відняти частину, бо для «{payload}» не вказана точна кількість. "
                                "Спочатку відредагуй кількість товару.",
                            )
                        elif kind == "insufficient":
                            name, available, requested = payload
                            send_message(
                                chat_id, f"У запасах є лише {available}, а ти вказав {requested}. Уточни кількість."
                            )
                        else:
                            send_message(
                                chat_id,
                                "Не можу безпечно визначити, яку саме кількість потрібно списати. Уточни, будь ласка.",
                            )
                        _preview_intercepted = True
                    elif intent == "compound_inventory_operations" and ctx == "inventory_saved":
                        kind, payload = _validate_compound_operations(
                            router_result.get("operations"), router_result.get("unresolved_fragments"), list_items
                        )
                        if kind == "ok":
                            pending_compound_inventory[chat_id] = {
                                "inventory_changes": payload["inventory_changes"],
                                "add_to_shopping": payload["add_to_shopping"],
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                            }
                            send_message(
                                chat_id, _format_compound_preview(payload), reply_markup=COMPOUND_PREVIEW_KEYBOARD
                            )
                        elif kind == "unresolved":
                            lines = [
                                "Я зрозумів частину повідомлення, але не хочу мовчки пропустити решту.",
                                "",
                                "Не зміг зрозуміти:",
                            ]
                            for frag in payload:
                                lines.append(f"• «{frag}»")
                            lines.append("")
                            lines.append("Спробуй уточнити все повідомлення.")
                            send_message(chat_id, "\n".join(lines))
                        else:
                            lines = [
                                "Не зміг безпечно обробити всі зміни. Нічого не було змінено.",
                                "",
                                "Не зрозумів або не можу виконати:",
                            ]
                            for reason in payload:
                                lines.append(f"• {reason}")
                            send_message(chat_id, "\n".join(lines))
                        _preview_intercepted = True
                    elif intent == "reconcile_inventory_snapshot" and ctx == "inventory_saved":
                        kind, payload = _validate_reconcile_snapshot(
                            router_result.get("items"), router_result.get("unresolved_fragments"), list_items
                        )
                        if kind == "ok":
                            pending_inventory_reconciliation[chat_id] = {
                                "updates": payload["updates"],
                                "additions": payload["additions"],
                                "deletes": payload["deletes"],
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                            }
                            send_message(
                                chat_id, _format_reconciliation_preview(payload), reply_markup=RECONCILIATION_PREVIEW_KEYBOARD
                            )
                        elif kind == "ambiguous_unit_group":
                            pending_inventory_reconciliation_clarify[chat_id] = {
                                "ambiguous_group": payload["ambiguous_group"],
                                "rest": payload["rest"],
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                            }
                            send_message(chat_id, _format_reconciliation_unit_clarify_question(payload["ambiguous_group"]))
                        elif kind == "unresolved":
                            lines = [
                                "Я зрозумів частину списку, але не хочу мовчки пропустити решту.",
                                "",
                                "Не зміг зрозуміти:",
                            ]
                            for frag in payload:
                                lines.append(f"• «{frag}»")
                            lines.append("")
                            lines.append("Спробуй надіслати весь список запасів ще раз.")
                            send_message(chat_id, "\n".join(lines))
                        else:
                            lines = [
                                "Не зміг безпечно звірити запаси. Нічого не було змінено.",
                                "",
                                "Причина:",
                            ]
                            for reason in payload:
                                lines.append(f"• {reason}")
                            send_message(chat_id, "\n".join(lines))
                        _preview_intercepted = True
                    # intent == "none": fall through to AI chat
                elif ctx == "shopping_saved":
                    router_result = _ask_gemini_saved_list_router(text, [], ctx)
                    if router_result["intent"] == "quick_add_to_inventory":
                        parsed = _validate_quick_add_items(router_result.get("items"))
                        if parsed is not None:
                            quick_items, ignored_names = parsed
                            saved_list_context.pop(chat_id, None)
                            pending_quick_purchase[chat_id] = {
                                "items": quick_items,
                                "ignored_items": ignored_names,
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                            }
                            preview = _format_quick_purchase_preview(quick_items, ignored_names)
                            send_message(chat_id, preview, reply_markup=QUICK_PURCHASE_KEYBOARD)
                        else:
                            send_message(chat_id, "Не зміг безпечно зрозуміти покупку. Спробуй написати інакше.")
                        _preview_intercepted = True
                    # intent == "none": fall through to AI chat
            except Exception:
                pass

    if _preview_intercepted:
        return "ok"

    # =========================
    # COOKING MODE
    # =========================
    if waiting_for_ingredients.pop(chat_id, False):
        cooking_history = [{"role": "user", "content": text}]
        answer = call_gemini(cooking_history, COOKING_SYSTEM_PROMPT, temperature=0.4, model_url=GEMINI_COOKING_URL)
        if answer is None:
            answer = call_gemini(cooking_history, COOKING_SYSTEM_PROMPT, temperature=0.4, model_url=GEMINI_CHAT_URL)
        if answer is None:
            answer = "AI-помічник тимчасово недоступний. Спробуйте ще раз трохи пізніше."
        send_message(chat_id, answer)
        return "ok"

    # =========================
    # AI CHAT (Gemini 3.1 Flash Lite)
    # =========================
    if chat_id not in user_history:
        user_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_history[chat_id].append({"role": "user", "content": text})
    user_history[chat_id] = user_history[chat_id][:1] + user_history[chat_id][-20:]

    gemini_history = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in user_history[chat_id][1:]
    ]
    answer = call_gemini(gemini_history, SYSTEM_PROMPT + "\n\n" + get_warsaw_datetime_context())

    if answer is not None:
        user_history[chat_id].append({"role": "assistant", "content": answer})
    else:
        answer = "AI-помічник тимчасово недоступний. Спробуйте ще раз трохи пізніше."

    send_message(chat_id, answer)
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
