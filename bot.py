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
pending_confirm = {}    # chat_id -> {action, item_id, item_name, user_db_id}
pending_candidates = {} # chat_id -> {action, items, user_db_id}
pending_batch = {}      # chat_id -> {items, household_id, user_db_id}

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

SHOPPING_PARSE_PROMPT = (
    "Розбий текст на список товарів для покупки. Правила:\n"
    "- розділяй товари за новими рядками, комами, крапками з комою або природними розділеннями;\n"
    "- «Мисливські ковбаски 4» — це ОДИН товар із кількістю «4», а не два окремих;\n"
    "- виправляй лише очевидні орфографічні помилки;\n"
    "- не вигадуй товари, яких немає в тексті;\n"
    "- не вигадуй одиниці виміру, якщо вони не вказані явно;\n"
    "- число або кількість зберігай у quantity_text як рядок (наприклад: «6», «2 л», «1.5»); "
    "якщо кількість не вказана — порожній рядок;\n"
    "- якщо виправив орфографічну помилку, вкажи її у correction_note у форматі "
    "«Виправлено «оригінал» → «виправлено»»; інакше — порожній рядок.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без жодного додаткового тексту:\n"
    '{"items": ['
    '{"name": "Вершки", "quantity_text": "", "original_text": "Виршки", "correction_note": "Виправлено «Виршки» → «Вершки»"}, '
    '{"name": "Молоко", "quantity_text": "1.5", "original_text": "Молоко 1.5", "correction_note": ""}'
    "]}"
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
        ["❌ Скасувати"]
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True
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

def format_shopping_list(items):
    if not items:
        return "Список покупок поки порожній."
    lines = []
    for i, item in enumerate(items, 1):
        if item["quantity_text"]:
            lines.append(f"{i}. {item['name']} — {item['quantity_text']}")
        else:
            lines.append(f"{i}. {item['name']}")
    return "🛒 Список покупок:\n\n" + "\n".join(lines)

def get_household_and_user(user_id, display_name=None):
    household_id = get_or_create_household()
    user_db_id = get_or_create_user(user_id, household_id, display_name)
    return household_id, user_db_id

def clear_shopping_state(chat_id):
    shopping_mode.pop(chat_id, None)
    pending_confirm.pop(chat_id, None)
    pending_candidates.pop(chat_id, None)
    pending_batch.pop(chat_id, None)

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

def _execute_action_by_id(chat_id, action, item, user_db_id):
    """Perform mark or delete by item dict {id, name}. Handles race condition."""
    if action == "marking":
        result = mark_item_by_id(item["id"], user_db_id)
        if result is None:
            send_message(chat_id, "Цей товар уже зник зі списку. Онови список покупок.")
        else:
            send_message(chat_id, f"✅ Куплено: {result}")
    else:  # deleting
        result = delete_item_by_id(item["id"])
        if result is None:
            send_message(chat_id, "Цей товар уже зник зі списку. Онови список покупок.")
        else:
            send_message(chat_id, f"🗑️ Видалено: {result}")

def parse_shopping_list_with_gemini(text):
    """Call Gemini once to parse a free-form shopping list into structured items.

    Returns a list of dicts with keys: name, quantity_text, original_text, correction_note.
    Returns None if the response is missing or invalid.
    """
    history = [{"role": "user", "content": text}]
    raw = call_gemini(history, SHOPPING_PARSE_PROMPT, temperature=0.1)
    if not raw:
        return None
    cleaned = raw.strip()
    # Strip optional markdown code fences
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if match:
            cleaned = match.group(1).strip()
    try:
        data = json.loads(cleaned)
        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return None
        result = []
        for item in raw_items:
            if not isinstance(item, dict):
                return None
            name = item.get("name", "").strip()
            if not name:
                return None
            result.append({
                "name": name,
                "quantity_text": item.get("quantity_text", "").strip(),
                "original_text": item.get("original_text", "").strip(),
                "correction_note": item.get("correction_note", "").strip(),
            })
        return result
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

def format_batch_preview(items):
    lines = [f"🛒 Знайшов товарів: {len(items)}\n"]
    for i, item in enumerate(items, 1):
        if item["quantity_text"]:
            lines.append(f"{i}. {item['name']} — {item['quantity_text']}")
        else:
            lines.append(f"{i}. {item['name']}")
        if item["correction_note"]:
            lines.append(f"   {item['correction_note']}")
    return "\n".join(lines)

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
        if chat_id in pending_confirm:
            confirm = pending_confirm.pop(chat_id)
            try:
                item = {"id": confirm["item_id"], "name": confirm["item_name"]}
                _execute_action_by_id(chat_id, confirm["action"], item, confirm["user_db_id"])
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "❌ Ні":
        if chat_id in pending_confirm:
            pending_confirm.pop(chat_id)
            send_message(
                chat_id,
                "Добре. Напиши номер зі списку або повну назву товару.",
                reply_markup=SHOPPING_KEYBOARD
            )
        return "ok"

    if text == "✅ Додати все":
        if chat_id in pending_batch:
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
        pending_batch.pop(chat_id, None)
        shopping_mode[chat_id] = "adding"
        send_message(chat_id, "Надішли один товар або список товарів. Можна кожен товар з нового рядка.")
        return "ok"

    if text == "❌ Скасувати":
        clear_shopping_state(chat_id)
        send_message(chat_id, "Додавання товарів скасовано.", reply_markup=SHOPPING_KEYBOARD)
        return "ok"

    if text == "/start":
        waiting_for_ingredients.pop(chat_id, None)
        clear_shopping_state(chat_id)
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
                    format_shopping_list(items) + "\n\nНапиши номер або назву товару, який ти купив:"
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
                    format_shopping_list(items) + "\n\nНапиши номер або назву товару, який потрібно видалити:"
                )
                shopping_mode[chat_id] = "deleting"
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if text == "⬅️ Головне меню":
        waiting_for_ingredients.pop(chat_id, None)
        clear_shopping_state(chat_id)
        send_message(chat_id, "Ось головне меню:", reply_markup=MAIN_KEYBOARD)
        return "ok"

    if text == "🧊 Запаси":
        send_message(chat_id, "Облік запасів буде доданий після підключення постійної бази даних.")
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

    # Candidates sub-list: user picks from a previously shown shortlist
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
                _execute_action_by_id(chat_id, action, candidate_items[num - 1], user_db_id)
        except ValueError:
            send_message(chat_id, "Напиши число — номер зі списку варіантів:")
            pending_candidates[chat_id] = cands
        return "ok"

    mode = shopping_mode.pop(chat_id, None)

    if mode == "adding":
        parsed_items = parse_shopping_list_with_gemini(text)
        if parsed_items is None:
            shopping_mode[chat_id] = "adding"
            send_message(
                chat_id,
                "Не зміг точно розібрати список. Надішли товари ще раз, бажано кожен з нового рядка."
            )
            return "ok"
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            pending_batch[chat_id] = {
                "items": parsed_items,
                "household_id": household_id,
                "user_db_id": user_db_id,
            }
            preview = format_batch_preview(parsed_items)
            send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
        return "ok"

    if mode in ("marking", "deleting"):
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            items = get_active_shopping_items(household_id)

            # Try numeric first
            try:
                num = int(text.strip())
                if num < 1 or num > len(items):
                    send_message(chat_id, "Такого номера немає у списку. Напиши номер зі списку або повну назву товару.")
                    shopping_mode[chat_id] = mode
                    return "ok"
                _execute_action_by_id(chat_id, mode, items[num - 1], user_db_id)
                return "ok"
            except ValueError:
                pass  # not a number — try name search

            # Name search with normalization and fuzzy matching
            exact, fuzzy = find_items_by_name(text, items)
            candidates = exact if exact else fuzzy

            if not candidates:
                send_message(chat_id, "Не знайшов такого товару. Напиши номер зі списку або повну назву товару.")
                shopping_mode[chat_id] = mode
            elif exact and len(candidates) == 1:
                _execute_action_by_id(chat_id, mode, candidates[0], user_db_id)
            elif fuzzy and len(candidates) == 1:
                item = candidates[0]
                pending_confirm[chat_id] = {
                    "action": mode,
                    "item_id": item["id"],
                    "item_name": item["name"],
                    "user_db_id": user_db_id,
                }
                send_message(chat_id, f"Маєш на увазі «{item['name']}»?", reply_markup=CONFIRM_KEYBOARD)
            else:
                pending_candidates[chat_id] = {
                    "action": mode,
                    "items": [{"id": c["id"], "name": c["name"]} for c in candidates],
                    "user_db_id": user_db_id,
                }
                lines = [f"{i + 1}. {c['name']}" for i, c in enumerate(candidates)]
                send_message(
                    chat_id,
                    "Знайшов кілька варіантів:\n\n" + "\n".join(lines) + "\n\nНапиши номер потрібного товару."
                )

        except Exception:
            send_message(chat_id, DB_ERROR_MSG)
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
