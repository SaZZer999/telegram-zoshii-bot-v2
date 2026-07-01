import json
import os
import re
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
    execute_merge_shopping,
    execute_merge_inventory,
    update_shopping_items_batch,
    update_inventory_items_batch,
)

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

SYSTEM_PROMPT = "Ти корисний AI-помічник. Відповідай українською."

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
    "Ти помічник для редагування збереженого списку товарів.\n"
    "Визнач намір (intent):\n"
    "- «edit_saved_items» — якщо користувач хоче змінити кількість, назву або категорію наявних позицій\n"
    "- «merge_duplicates» — якщо хоче об'єднати однакові або дублюючі позиції\n"
    "- «none» — в усіх інших випадках: видалення, купівля, позначення купленим, додавання нових товарів, загальне питання\n\n"
    "Для edit_saved_items — поверни updates лише для позицій, які змінюються:\n"
    "- item_number — ціле число (номер у списку, від 1 до N)\n"
    "- name — нова назва або null якщо не змінюється\n"
    "- quantity_text — нова кількість (напр. «2 шт.», «500 г», «1,5 л») або null\n"
    "- category — нова категорія або null\n\n"
    "Для merge_duplicates — поверни merge_groups: масив масивів item_number:\n"
    "- [[2, 4], [1, 3]] — кожна підгрупа містить номери позицій для об'єднання\n\n"
    "Правила:\n"
    "- Не додавай нових позицій\n"
    "- Не видаляй існуючих позицій\n"
    "- Не позначай нічого купленим\n"
    "- Нормалізуй одиниці: «2 штуки» → «2 шт.», «500 грам» → «500 г», «1.5 л» → «1,5 л»\n\n"
    "Відповідай ТІЛЬКИ валідним JSON без жодного тексту:\n"
    "{\"intent\": \"edit_saved_items\", \"updates\": [{\"item_number\": 1, \"name\": null, \"quantity_text\": \"2 шт.\", \"category\": null}], \"merge_groups\": []}"
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

# =========================
# FLASK APP
# =========================
app = Flask(__name__)

def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload)

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
            if item.get("quantity_text"):
                lines.append(f"{counter}. {label} — {item['quantity_text']}")
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

def clear_inventory_state(chat_id):
    inventory_mode.pop(chat_id, None)
    pending_inventory_batch.pop(chat_id, None)
    pending_remove_batch.pop(chat_id, None)
    pending_merge.pop(chat_id, None)
    saved_list_context.pop(chat_id, None)
    pending_saved_edit.pop(chat_id, None)

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

    Groups by (lowercase name, category). Compatible quantities are summed.
    Incompatible quantities block the merge — those items stay separate.
    """
    from collections import OrderedDict
    seen = OrderedDict()
    for item in items:
        key = (item["name"].strip().lower(), item.get("category") or DEFAULT_CATEGORY)
        seen.setdefault(key, []).append(item)
    result = []
    for group in seen.values():
        if len(group) == 1:
            result.append(group[0])
            continue
        merged_qty = _compute_merged_quantity(group)
        if merged_qty is None:
            result.extend(group)
            continue
        merged = dict(group[0])
        merged["quantity_text"] = merged_qty
        result.append(merged)
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
        if item.get("quantity_text"):
            label += f" — {item['quantity_text']}"
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
        if upd.get("name") is not None:
            result[idx]["name"] = str(upd["name"]).strip()
        if upd.get("quantity_text") is not None:
            result[idx]["quantity_text"] = upd["quantity_text"].strip()
        if upd.get("category") is not None:
            result[idx]["category"] = upd["category"]
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
    qtys = [item.get("quantity_text") or "" for item in group_items]
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
        if len({it["name"].strip().lower() for it in group_items}) > 1:
            continue
        categories = {it.get("category") or DEFAULT_CATEGORY for it in group_items}
        non_default = categories - {DEFAULT_CATEGORY}
        if len(non_default) > 1:
            continue
        merged_category = next(iter(non_default), DEFAULT_CATEGORY)
        merged_qty = _compute_saved_merged_quantity(group_items)
        if merged_qty is None:
            continue
        used_numbers.update(nums)
        validated.append({
            "item_ids": [it["id"] for it in group_items],
            "merged_name": group_items[0]["name"],
            "merged_quantity_text": merged_qty,
            "merged_category": merged_category,
            "items": group_items,
        })
    return validated


def _validate_saved_updates(updates, items):
    """Validate Gemini saved list edit updates. Returns list of valid updates (with item_id) or None."""
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
        })
    return valid


def _ask_gemini_saved_list_router(user_text, items, context_type):
    """Gemini call: detect edit_saved_items or merge_duplicates for an active saved list."""
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        if item.get("quantity_text"):
            label += f" — {item['quantity_text']}"
        label += f" [{item.get('category') or DEFAULT_CATEGORY}]"
        lines.append(label)
    prompt = (
        f"Контекст: {context_type}\n"
        "Поточний список:\n" + "\n".join(lines)
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], SAVED_LIST_EDIT_PROMPT, temperature=0.1)
    if not raw:
        return {"intent": "none", "updates": [], "merge_groups": []}
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
            "merge_groups": data.get("merge_groups") if isinstance(data.get("merge_groups"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"intent": "none", "updates": [], "merge_groups": []}


def _format_saved_edit_preview(items_snapshot, validated_updates, context_type):
    """Format before/after preview for a saved list edit."""
    icon = "🛒" if context_type == "shopping_saved" else "🧊"
    lines = [f"{icon} Буде змінено: {len(validated_updates)}", ""]
    for upd in validated_updates:
        idx = upd["item_number"] - 1
        old = items_snapshot[idx]
        old_label = old["name"]
        if old.get("quantity_text"):
            old_label += f" — {old['quantity_text']}"
        new_name = upd.get("name") or old["name"]
        new_qty = upd.get("quantity_text")
        if new_qty is None:
            new_qty = old.get("quantity_text") or ""
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
            if item.get("quantity_text"):
                label += f" — {item['quantity_text']}"
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


def _ask_gemini_for_selection(user_text, items, list_label, action_label):
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        if item.get("quantity_text"):
            label += f" — {item['quantity_text']}"
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
            consumable.append({
                "name": name,
                "quantity_text": item.get("quantity_text", "").strip(),
                "category": cat,
                "was_corrected": bool(item.get("was_corrected", False)),
            })
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
                current_items = get_active_shopping_items(mark_data["household_id"])
                item_ids = [item["id"] for item in mark_data["items"]]
                if _snapshot_is_stale(item_ids, current_items):
                    send_message(chat_id, "Список змінився з іншого пристрою. Онови список і повтори дію.", reply_markup=SHOPPING_KEYBOARD)
                    return "ok"
                count = mark_items_batch(item_ids, mark_data["user_db_id"])
                for item in mark_data["items"]:
                    add_or_merge_inventory_item(
                        mark_data["household_id"],
                        mark_data["user_db_id"],
                        item["name"],
                        item.get("quantity_text", ""),
                        item.get("category", DEFAULT_CATEGORY),
                    )
                send_message(chat_id, f"✅ Куплено й додано до запасів: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, "Не вдалося завершити покупку. Спробуйте ще раз трохи пізніше.")
        return "ok"

    if text == "✅ Куплено, без запасів":
        if chat_id in pending_mark_batch:
            mark_data = pending_mark_batch.pop(chat_id)
            try:
                current_items = get_active_shopping_items(mark_data["household_id"])
                item_ids = [item["id"] for item in mark_data["items"]]
                if _snapshot_is_stale(item_ids, current_items):
                    send_message(chat_id, "Список змінився з іншого пристрою. Онови список і повтори дію.", reply_markup=SHOPPING_KEYBOARD)
                    return "ok"
                count = mark_items_batch(item_ids, mark_data["user_db_id"])
                send_message(chat_id, f"✅ Позначено купленими: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, "Не вдалося завершити покупку. Спробуйте ще раз трохи пізніше.")
        return "ok"

    if text == "✅ Так, видалити":
        if chat_id in pending_delete_batch:
            del_data = pending_delete_batch.pop(chat_id)
            try:
                current_items = get_active_shopping_items(del_data["household_id"])
                item_ids = [item["id"] for item in del_data["items"]]
                if _snapshot_is_stale(item_ids, current_items):
                    send_message(chat_id, "Список змінився з іншого пристрою. Онови список і повтори дію.", reply_markup=SHOPPING_KEYBOARD)
                    return "ok"
                count = delete_items_batch(item_ids)
                send_message(chat_id, f"🗑️ Видалено зі списку: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "✅ Так, прибрати":
        if chat_id in pending_remove_batch:
            rem_data = pending_remove_batch.pop(chat_id)
            try:
                current_items = get_inventory_items(rem_data["household_id"])
                item_ids = [item["id"] for item in rem_data["items"]]
                if _snapshot_is_stale(item_ids, current_items):
                    send_message(chat_id, "Список змінився з іншого пристрою. Онови список і повтори дію.", reply_markup=INVENTORY_KEYBOARD)
                    return "ok"
                count = delete_inventory_items_batch(item_ids)
                send_message(chat_id, f"✅ Прибрано із запасів: {count}", reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "✅ Підтвердити зміни":
        if chat_id in pending_saved_edit:
            edit_data = pending_saved_edit.pop(chat_id)
            ctx = edit_data["context_type"]
            household_id = edit_data["household_id"]
            valid_updates = edit_data["validated_updates"]
            keyboard = SHOPPING_KEYBOARD if ctx == "shopping_saved" else INVENTORY_KEYBOARD
            try:
                current_items = (
                    get_active_shopping_items(household_id)
                    if ctx == "shopping_saved"
                    else get_inventory_items(household_id)
                )
                current_ids = {it["id"] for it in current_items}
                required_ids = {upd["item_id"] for upd in valid_updates}
                if not required_ids.issubset(current_ids):
                    send_message(
                        chat_id,
                        "Список змінився з іншого пристрою. Онови список і повтори дію.",
                        reply_markup=keyboard,
                    )
                    return "ok"
                if ctx == "shopping_saved":
                    update_shopping_items_batch(household_id, valid_updates)
                else:
                    update_inventory_items_batch(household_id, valid_updates)
                send_message(chat_id, "✅ Зміни застосовано.", reply_markup=keyboard)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG if ctx == "shopping_saved" else INVENTORY_ERROR_MSG)
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
        send_message(chat_id, "🛒 Список покупок:", reply_markup=SHOPPING_KEYBOARD)
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
        send_message(chat_id, "Ось головне меню:", reply_markup=MAIN_KEYBOARD)
        return "ok"

    if text == "🧊 Запаси":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context[chat_id] = "inventory"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        send_message(chat_id, "🧊 Запаси:", reply_markup=INVENTORY_KEYBOARD)
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
        batch["items"][idx]["quantity_text"] = quantity_text or ""
        batch["items"][idx]["was_corrected"] = False
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

    else:
        ctx = saved_list_context.get(chat_id)
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
    answer = call_gemini(gemini_history, SYSTEM_PROMPT)

    if answer is not None:
        user_history[chat_id].append({"role": "assistant", "content": answer})
    else:
        answer = "AI-помічник тимчасово недоступний. Спробуйте ще раз трохи пізніше."

    send_message(chat_id, answer)
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
