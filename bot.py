import json
import os
import re
import difflib
from flask import Flask, request
from dotenv import load_dotenv
from groq import Groq
import requests
from database import (
    init_db,
    get_or_create_household,
    get_or_create_user,
    add_shopping_item,
    get_active_shopping_items,
    mark_item_by_id,
    delete_item_by_id,
    add_shopping_items_batch,
    get_inventory_items,
    add_inventory_items_batch,
    delete_inventory_item_by_id,
    add_or_merge_inventory_item,
    mark_items_batch,
    delete_items_batch,
    delete_inventory_items_batch,
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
shopping_mode = {}      # chat_id -> "adding" | "marking" | "deleting"
pending_confirm = {}    # chat_id -> {action, item_id, item_name, item_quantity_text?, item_category?, household_id, user_db_id}
pending_candidates = {} # chat_id -> {action, items, household_id, user_db_id}
pending_batch = {}      # chat_id -> {items, ignored_items, household_id, user_db_id}
pending_mark_batch = {}   # chat_id -> {items, household_id, user_db_id}
pending_delete_batch = {} # chat_id -> {items, household_id, user_db_id}
inventory_mode = {}              # chat_id -> "adding" | "removing"
pending_inventory_batch = {}     # chat_id -> {items, ignored_items, household_id, user_db_id}
pending_inventory_confirm = {}   # chat_id -> {item_id, item_name, item_quantity_text, item_category, household_id, user_db_id}
pending_inventory_candidates = {} # chat_id -> {items, household_id, user_db_id}
pending_remove_batch = {}        # chat_id -> {items, household_id, user_db_id}

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

CONFIRM_KEYBOARD = {
    "keyboard": [["✅ Так", "❌ Ні"]],
    "resize_keyboard": True,
    "one_time_keyboard": True
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

# =========================
# SYNONYM DICTIONARY
# (easily extendable — keys and values must be lowercase)
# =========================
SYNONYMS = {
    "сливки":    "вершки",
    "вершки":    "вершки",
    "яйца":      "яйця",
    "яйця":      "яйця",
    "картошка":  "картопля",
    "картопля":  "картопля",
    "лук":       "цибуля",
    "цибуля":    "цибуля",
    "помидори":  "помідори",
    "помідори":  "помідори",
    "сыр":       "сир",
    "сир":       "сир",
}

_APOSTROPHE_RE = re.compile(r"[''`ʼ]")

_ALL_PHRASES = {
    "все", "усе", "всі",
    "всі товари", "всі продукти",
    "купив все", "купила все", "купили все", "куплено все",
    "всі куплені", "всі продукти куплені", "все куплено",
    "все купив", "все купила",
    "все використав", "все використала",
    "все прибрати",
}

SELECTION_PROMPT = (
    "Визнач, які позиції зі списку користувач хоче вибрати на основі свого запиту.\n"
    "Правила:\n"
    "- Відповідай ТІЛЬКИ валідним JSON: {\"selected_numbers\": [1, 3, 5]}\n"
    "- Вказуй тільки номери, які є в списку\n"
    "- Без дублікатів, відсортовані за зростанням\n"
    "- Якщо нічого підходящого — відповідай {\"selected_numbers\": []}\n"
    "- Не вигадуй номерів, яких немає в списку\n"
)

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
    pending_confirm.pop(chat_id, None)
    pending_candidates.pop(chat_id, None)
    pending_batch.pop(chat_id, None)
    pending_mark_batch.pop(chat_id, None)
    pending_delete_batch.pop(chat_id, None)

def clear_inventory_state(chat_id):
    inventory_mode.pop(chat_id, None)
    pending_inventory_batch.pop(chat_id, None)
    pending_inventory_confirm.pop(chat_id, None)
    pending_inventory_candidates.pop(chat_id, None)
    pending_remove_batch.pop(chat_id, None)

def normalize_name(text):
    text = _APOSTROPHE_RE.sub("'", text)
    text = " ".join(text.split())
    text = text.lower()
    return SYNONYMS.get(text, text)

def find_items_by_name(query, items):
    """Returns (exact_matches, fuzzy_matches). Exact list is non-empty OR fuzzy list is."""
    norm_query = normalize_name(query)
    exact = [item for item in items if normalize_name(item["name"]) == norm_query]
    if exact:
        return exact, []
    fuzzy = [
        item for item in items
        if difflib.SequenceMatcher(None, norm_query, normalize_name(item["name"])).ratio() >= 0.6
    ]
    return [], fuzzy

def _is_all_phrase(text):
    return text.strip().lower() in _ALL_PHRASES

def _parse_number_ranges(text, total):
    """Parse '1 2-4, 6' into sorted zero-based indices.

    Returns:
        sorted list of 0-based indices  — valid pure number/range input
        "out_of_range"                  — purely numeric but some numbers outside 1..total
        None                            — not a pure number/range pattern
    """
    cleaned = text.strip().replace(",", " ")
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return None
    indices = set()
    for token in tokens:
        if "-" in token:
            parts = token.split("-", 1)
            if len(parts) != 2:
                return None
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError:
                return None
            if start > end:
                return "out_of_range"
            if start < 1 or end > total:
                return "out_of_range"
            for i in range(start, end + 1):
                indices.add(i - 1)
        else:
            try:
                n = int(token)
            except ValueError:
                return None
            if n < 1 or n > total:
                return "out_of_range"
            indices.add(n - 1)
    return sorted(indices) if indices else None

def _ask_gemini_for_selection(user_text, items, list_label="Список"):
    """Ask Gemini which items the user wants to select.

    Returns sorted zero-based index list or None on failure.
    """
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        if item.get("quantity_text"):
            label += f" — {item['quantity_text']}"
        if item.get("category"):
            label += f" [{item['category']}]"
        lines.append(label)
    prompt = f"{list_label}:\n" + "\n".join(lines) + f"\n\nКористувач написав: {user_text}"
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
        numbers = data.get("selected_numbers")
        if not isinstance(numbers, list):
            return None
        total = len(items)
        seen = set()
        indices = []
        for n in numbers:
            n = int(n)
            if 1 <= n <= total and n not in seen:
                indices.append(n - 1)
                seen.add(n)
        return sorted(indices) if indices else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None

def _show_mark_preview(chat_id, items, household_id, user_db_id):
    pending_mark_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    n = len(items)
    preview = format_grouped_list(items, f"🛒 Буде позначено купленими: {n}")
    send_message(chat_id, preview + "\n\nЩо зробити з цими товарами?", reply_markup=MARK_PREVIEW_KEYBOARD)

def _show_delete_preview(chat_id, items, household_id, user_db_id):
    pending_delete_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    n = len(items)
    preview = format_grouped_list(items, f"🗑️ Буде видалено зі списку покупок: {n}")
    send_message(chat_id, preview, reply_markup=DELETE_PREVIEW_KEYBOARD)

def _show_remove_preview(chat_id, items, household_id, user_db_id):
    pending_remove_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    n = len(items)
    preview = format_grouped_list(items, f"🧊 Буде прибрано із запасів: {n}")
    send_message(chat_id, preview, reply_markup=REMOVE_PREVIEW_KEYBOARD)

def parse_shopping_list_with_gemini(text):
    """Call Gemini once to parse a free-form shopping list.

    Returns {"items": [...], "ignored_items": [...]} or None on failure.
    Each item: {name, quantity_text, category, was_corrected}.
    """
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

    # /myid works for everyone, before access check
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
    # COMMANDS & BUTTONS
    # =========================

    # Confirmation buttons — must come before any clear_shopping_state calls
    if text == "✅ Так":
        if chat_id in pending_inventory_confirm:
            confirm = pending_inventory_confirm.pop(chat_id)
            try:
                item = {
                    "id": confirm["item_id"],
                    "name": confirm["item_name"],
                    "quantity_text": confirm.get("item_quantity_text", ""),
                    "category": confirm.get("item_category", DEFAULT_CATEGORY),
                }
                _show_remove_preview(chat_id, [item], confirm["household_id"], confirm["user_db_id"])
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        elif chat_id in pending_confirm:
            confirm = pending_confirm.pop(chat_id)
            try:
                item = {
                    "id": confirm["item_id"],
                    "name": confirm["item_name"],
                    "quantity_text": confirm.get("item_quantity_text", ""),
                    "category": confirm.get("item_category", DEFAULT_CATEGORY),
                }
                if confirm["action"] == "marking":
                    _show_mark_preview(chat_id, [item], confirm["household_id"], confirm["user_db_id"])
                else:
                    _show_delete_preview(chat_id, [item], confirm["household_id"], confirm["user_db_id"])
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "❌ Ні":
        if chat_id in pending_inventory_confirm:
            confirm = pending_inventory_confirm.pop(chat_id)
            if confirm.get("household_id"):
                inventory_mode[chat_id] = "removing"
            send_message(chat_id, "Добре. Напиши номер або назву продукту.")
        elif chat_id in pending_confirm:
            confirm = pending_confirm.pop(chat_id)
            if confirm.get("action") in ("marking", "deleting") and confirm.get("household_id"):
                shopping_mode[chat_id] = confirm["action"]
            send_message(chat_id, "Добре. Напиши номер або назву товару.")
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
        if chat_id in pending_inventory_batch:
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
            data = pending_mark_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in data["items"]]
                count = mark_items_batch(item_ids, data["user_db_id"])
                for item in data["items"]:
                    add_or_merge_inventory_item(
                        data["household_id"],
                        data["user_db_id"],
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
            data = pending_mark_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in data["items"]]
                count = mark_items_batch(item_ids, data["user_db_id"])
                send_message(chat_id, f"✅ Позначено купленими: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, "Не вдалося завершити покупку. Спробуйте ще раз трохи пізніше.")
        return "ok"

    if text == "✅ Так, видалити":
        if chat_id in pending_delete_batch:
            data = pending_delete_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in data["items"]]
                count = delete_items_batch(item_ids)
                send_message(chat_id, f"🗑️ Видалено зі списку: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "✅ Так, прибрати":
        if chat_id in pending_remove_batch:
            data = pending_remove_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in data["items"]]
                count = delete_inventory_items_batch(item_ids)
                send_message(chat_id, f"✅ Прибрано із запасів: {count}", reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "✏️ Змінити вибір":
        if chat_id in pending_mark_batch:
            data = pending_mark_batch.pop(chat_id)
            try:
                items = get_active_shopping_items(data["household_id"])
                shopping_mode[chat_id] = "marking"
                if not items:
                    send_message(chat_id, "Список покупок поки порожній.")
                else:
                    send_message(
                        chat_id,
                        format_shopping_list(items) + '\n\nНапиши "все", кілька номерів, назву або фразу про куплені товари:'
                    )
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        elif chat_id in pending_delete_batch:
            data = pending_delete_batch.pop(chat_id)
            try:
                items = get_active_shopping_items(data["household_id"])
                shopping_mode[chat_id] = "deleting"
                if not items:
                    send_message(chat_id, "Список покупок поки порожній.")
                else:
                    send_message(
                        chat_id,
                        format_shopping_list(items) + '\n\nНапиши "все", кілька номерів або назву товарів для видалення:'
                    )
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        elif chat_id in pending_remove_batch:
            data = pending_remove_batch.pop(chat_id)
            try:
                items = get_inventory_items(data["household_id"])
                inventory_mode[chat_id] = "removing"
                if not items:
                    send_message(chat_id, "Запаси поки порожні.")
                else:
                    send_message(
                        chat_id,
                        format_inventory_list(items) + '\n\nНапиши "все", кілька номерів або назву продуктів для видалення із запасів:'
                    )
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "/start":
        waiting_for_ingredients.pop(chat_id, None)
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
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        send_message(chat_id, "Ось головне меню:", reply_markup=MAIN_KEYBOARD)
        return "ok"

    if text == "/help":
        send_message(
            chat_id,
            "ℹ️ Як користуватися ботом:\n\n"
            "🛒 Покупки — спільний список покупок\n"
            "🧊 Запаси — що є вдома (буде реалізовано)\n"
            "🍽️ Що приготувати — ідеї страв на основі запасів\n"
            "ℹ️ Допомога — ця інструкція\n\n"
            "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
        )
        return "ok"

    if text == "🛒 Покупки":
        waiting_for_ingredients.pop(chat_id, None)
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        send_message(chat_id, "🛒 Список покупок:", reply_markup=SHOPPING_KEYBOARD)
        return "ok"

    if text == "➕ Додати товар":
        clear_shopping_state(chat_id)
        shopping_mode[chat_id] = "adding"
        send_message(chat_id, "Надішли один товар або список товарів. Можна кожен товар з нового рядка.")
        return "ok"

    if text == "📋 Показати список":
        clear_shopping_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)
            send_message(chat_id, format_shopping_list(items))
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "✅ Позначити купленим":
        clear_shopping_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)
            if not items:
                send_message(chat_id, "Список покупок поки порожній.")
            else:
                send_message(
                    chat_id,
                    format_shopping_list(items) + '\n\nНапиши "все", кілька номерів, назву або фразу про куплені товари:'
                )
                shopping_mode[chat_id] = "marking"
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "🗑️ Видалити товар":
        clear_shopping_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)
            if not items:
                send_message(chat_id, "Список покупок поки порожній.")
            else:
                send_message(
                    chat_id,
                    format_shopping_list(items) + '\n\nНапиши "все", кілька номерів, назву або фразу про товари для видалення:'
                )
                shopping_mode[chat_id] = "deleting"
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "⬅️ Головне меню":
        waiting_for_ingredients.pop(chat_id, None)
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        send_message(chat_id, "Ось головне меню:", reply_markup=MAIN_KEYBOARD)
        return "ok"

    if text == "🧊 Запаси":
        waiting_for_ingredients.pop(chat_id, None)
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        send_message(chat_id, "🧊 Запаси:", reply_markup=INVENTORY_KEYBOARD)
        return "ok"

    if text == "➕ Додати продукти":
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        inventory_mode[chat_id] = "adding"
        send_message(chat_id, "Надішли один продукт або список продуктів. Можна кожен продукт з нового рядка.")
        return "ok"

    if text == "📋 Показати запаси":
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_inventory_items(household_id)
            send_message(chat_id, format_inventory_list(items))
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "➖ Використати / прибрати":
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            items = get_inventory_items(household_id)
            if not items:
                send_message(chat_id, "Запаси поки порожні.")
            else:
                send_message(
                    chat_id,
                    format_inventory_list(items) + '\n\nНапиши "все", кілька номерів або назву продуктів для видалення із запасів:'
                )
                inventory_mode[chat_id] = "removing"
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG)
        return "ok"

    if text == "🍽️ Що приготувати":
        clear_shopping_state(chat_id)
        waiting_for_ingredients[chat_id] = True
        send_message(chat_id, "Напиши, які продукти зараз є вдома, і я запропоную кілька страв.")
        return "ok"

    if text == "ℹ️ Допомога":
        send_message(
            chat_id,
            "ℹ️ Як користуватися ботом:\n\n"
            "🛒 Покупки — спільний список покупок\n"
            "🧊 Запаси — що є вдома (буде реалізовано)\n"
            "🍽️ Що приготувати — ідеї страв на основі запасів\n"
            "ℹ️ Допомога — ця інструкція\n\n"
            "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
        )
        return "ok"

    # =========================
    # SHOPPING MODE
    # =========================

    # Inventory candidates: user picks from a shortlist of fuzzy matches
    if chat_id in pending_inventory_candidates:
        cands = pending_inventory_candidates.pop(chat_id)
        candidate_items = cands["items"]
        try:
            num = int(text.strip())
            if num < 1 or num > len(candidate_items):
                send_message(chat_id, "Такого номера немає. Напиши номер зі списку або повну назву продукту.")
                pending_inventory_candidates[chat_id] = cands
            else:
                _show_remove_preview(
                    chat_id,
                    [candidate_items[num - 1]],
                    cands["household_id"],
                    cands["user_db_id"],
                )
        except ValueError:
            send_message(chat_id, "Напиши число — номер зі списку варіантів:")
            pending_inventory_candidates[chat_id] = cands
        return "ok"

    # Shopping candidates sub-list: user picks from a previously shown shortlist
    if chat_id in pending_candidates:
        cands = pending_candidates.pop(chat_id)
        candidate_items = cands["items"]
        action = cands["action"]
        user_db_id = cands["user_db_id"]
        try:
            num = int(text.strip())
            if num < 1 or num > len(candidate_items):
                send_message(chat_id, "Такого номера немає у списку. Напиши номер зі списку або повну назву товару.")
                pending_candidates[chat_id] = cands
            else:
                selected = candidate_items[num - 1]
                if action == "marking":
                    _show_mark_preview(chat_id, [selected], cands["household_id"], user_db_id)
                else:
                    _show_delete_preview(chat_id, [selected], cands["household_id"], user_db_id)
        except ValueError:
            send_message(chat_id, "Напиши число — номер зі списку варіантів:")
            pending_candidates[chat_id] = cands
        return "ok"

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
            # 1. "All" phrases
            if _is_all_phrase(text):
                _show_mark_preview(chat_id, items, household_id, user_db_id)
                return "ok"
            # 2. Numbers / ranges
            result = _parse_number_ranges(text, len(items))
            if result == "out_of_range":
                send_message(chat_id, f"Такий номер не існує. Напиши числа від 1 до {len(items)}.")
                shopping_mode[chat_id] = "marking"
                return "ok"
            if result is not None:
                _show_mark_preview(chat_id, [items[i] for i in result], household_id, user_db_id)
                return "ok"
            # 3. Exact or fuzzy name match
            exact, fuzzy = find_items_by_name(text, items)
            if exact:
                _show_mark_preview(chat_id, exact, household_id, user_db_id)
                return "ok"
            if fuzzy and len(fuzzy) == 1:
                item = fuzzy[0]
                pending_confirm[chat_id] = {
                    "action": "marking",
                    "item_id": item["id"],
                    "item_name": item["name"],
                    "item_quantity_text": item.get("quantity_text", ""),
                    "item_category": item.get("category", DEFAULT_CATEGORY),
                    "user_db_id": user_db_id,
                    "household_id": household_id,
                }
                send_message(chat_id, f"Маєш на увазі «{item['name']}»?", reply_markup=CONFIRM_KEYBOARD)
                return "ok"
            if fuzzy:
                pending_candidates[chat_id] = {
                    "action": "marking",
                    "items": [{"id": c["id"], "name": c["name"], "quantity_text": c.get("quantity_text", ""), "category": c.get("category", DEFAULT_CATEGORY)} for c in fuzzy],
                    "user_db_id": user_db_id,
                    "household_id": household_id,
                }
                lines = [f"{i + 1}. {c['name']}" for i, c in enumerate(fuzzy)]
                send_message(chat_id, "Знайшов кілька варіантів:\n\n" + "\n".join(lines) + "\n\nНапиши номер потрібного товару.")
                return "ok"
            # 4. Gemini for natural language
            indices = _ask_gemini_for_selection(text, items, "Список покупок")
            if indices is None:
                send_message(chat_id, 'Не зміг точно зрозуміти, які товари куплені. Напиши "все", номери або назви товарів.')
                shopping_mode[chat_id] = "marking"
            else:
                _show_mark_preview(chat_id, [items[i] for i in indices], household_id, user_db_id)
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
            # 1. "All" phrases
            if _is_all_phrase(text):
                _show_delete_preview(chat_id, items, household_id, user_db_id)
                return "ok"
            # 2. Numbers / ranges
            result = _parse_number_ranges(text, len(items))
            if result == "out_of_range":
                send_message(chat_id, f"Такий номер не існує. Напиши числа від 1 до {len(items)}.")
                shopping_mode[chat_id] = "deleting"
                return "ok"
            if result is not None:
                _show_delete_preview(chat_id, [items[i] for i in result], household_id, user_db_id)
                return "ok"
            # 3. Exact or fuzzy name match
            exact, fuzzy = find_items_by_name(text, items)
            if exact:
                _show_delete_preview(chat_id, exact, household_id, user_db_id)
                return "ok"
            if fuzzy and len(fuzzy) == 1:
                item = fuzzy[0]
                pending_confirm[chat_id] = {
                    "action": "deleting",
                    "item_id": item["id"],
                    "item_name": item["name"],
                    "item_quantity_text": item.get("quantity_text", ""),
                    "item_category": item.get("category", DEFAULT_CATEGORY),
                    "user_db_id": user_db_id,
                    "household_id": household_id,
                }
                send_message(chat_id, f"Маєш на увазі «{item['name']}»?", reply_markup=CONFIRM_KEYBOARD)
                return "ok"
            if fuzzy:
                pending_candidates[chat_id] = {
                    "action": "deleting",
                    "items": [{"id": c["id"], "name": c["name"], "quantity_text": c.get("quantity_text", ""), "category": c.get("category", DEFAULT_CATEGORY)} for c in fuzzy],
                    "user_db_id": user_db_id,
                    "household_id": household_id,
                }
                lines = [f"{i + 1}. {c['name']}" for i, c in enumerate(fuzzy)]
                send_message(chat_id, "Знайшов кілька варіантів:\n\n" + "\n".join(lines) + "\n\nНапиши номер потрібного товару.")
                return "ok"
            # 4. Gemini for natural language
            indices = _ask_gemini_for_selection(text, items, "Список покупок")
            if indices is None:
                send_message(chat_id, 'Не зміг точно зрозуміти вибір. Напиши "все", номери або назви товарів.')
                shopping_mode[chat_id] = "deleting"
            else:
                _show_delete_preview(chat_id, [items[i] for i in indices], household_id, user_db_id)
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
            # 1. "All" phrases
            if _is_all_phrase(text):
                _show_remove_preview(chat_id, items, household_id, user_db_id)
                return "ok"
            # 2. Numbers / ranges
            result = _parse_number_ranges(text, len(items))
            if result == "out_of_range":
                send_message(chat_id, f"Такий номер не існує. Напиши числа від 1 до {len(items)}.")
                inventory_mode[chat_id] = "removing"
                return "ok"
            if result is not None:
                _show_remove_preview(chat_id, [items[i] for i in result], household_id, user_db_id)
                return "ok"
            # 3. Exact or fuzzy name match
            exact, fuzzy = find_items_by_name(text, items)
            if exact:
                _show_remove_preview(chat_id, exact, household_id, user_db_id)
                return "ok"
            if fuzzy and len(fuzzy) == 1:
                item = fuzzy[0]
                pending_inventory_confirm[chat_id] = {
                    "item_id": item["id"],
                    "item_name": item["name"],
                    "item_quantity_text": item.get("quantity_text", ""),
                    "item_category": item.get("category", DEFAULT_CATEGORY),
                    "household_id": household_id,
                    "user_db_id": user_db_id,
                }
                send_message(chat_id, f"Маєш на увазі «{item['name']}»?", reply_markup=CONFIRM_KEYBOARD)
                return "ok"
            if fuzzy:
                pending_inventory_candidates[chat_id] = {
                    "items": [{"id": c["id"], "name": c["name"], "quantity_text": c.get("quantity_text", ""), "category": c.get("category", DEFAULT_CATEGORY)} for c in fuzzy],
                    "household_id": household_id,
                    "user_db_id": user_db_id,
                }
                lines = [f"{i + 1}. {c['name']}" for i, c in enumerate(fuzzy)]
                send_message(
                    chat_id,
                    "Знайшов кілька варіантів:\n\n" + "\n".join(lines) + "\n\nНапиши номер потрібного продукту."
                )
                return "ok"
            # 4. Gemini for natural language
            indices = _ask_gemini_for_selection(text, items, "Список запасів")
            if indices is None:
                send_message(chat_id, 'Не зміг точно зрозуміти вибір. Напиши "все", номери або назви продуктів.')
                inventory_mode[chat_id] = "removing"
            else:
                _show_remove_preview(chat_id, [items[i] for i in indices], household_id, user_db_id)
        except Exception:
            send_message(chat_id, INVENTORY_ERROR_MSG)
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
    # AI (Gemini 3.1 Flash Lite)
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
