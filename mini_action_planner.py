"""Unified Mini Action Planner V1.

A single, narrow, LAST-RESORT Gemini classifier — tried only after every
deterministic route, cooking mode, meal_ideas' own gate and household_read's
own gate+classifier have already had a chance at a message and reported
nothing to do (see message_dispatcher.dispatch()'s Phase D order and
bot.py's `_try_mini_action_planner`, the only caller). Its entire job is to
recognize a small, closed set of FIVE actions and hand off to code that
already exists — it never invents a sixth action, never writes to the
database itself, and never decides anything beyond "which of these five
things does this message look like".

    add_to_shopping   -> household_router.build_add_preview_from_items via
                          the SAME pending_global_household preview +
                          confirm/cancel flow every other household-router
                          result already uses (bot.py owns the DB write).
    add_to_inventory  -> same, destination="add_inventory".
    ask_inventory     -> household_read_context.answer_inventory_overview
                          (read-only, already exists).
    meal_ideas        -> meal_ideas.try_handle_meal_ideas(..., force=True)
                          (read-only, already exists).
    unknown           -> caller falls through to the existing general AI
                          chat fallback, exactly as before this planner
                          existed.

Gemini is asked for STRICT JSON only (one system prompt, one call, no
conversation history) and the result is validated in Python before ANYTHING
downstream ever sees it: an unrecognized action string, malformed JSON, a
Gemini call failure, or an empty Gemini response all safely collapse to
{"action": "unknown", "items": []} — never guessed, never partially
trusted. `items` (only ever populated for the two add_* actions) is
returned RAW here; item-level validation (name/quantity/category safety)
is a separate, existing responsibility — see household_router.
validate_mini_planner_add_items, reused rather than duplicated.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — `configure(bot_module)` injects bot.py's own `call_gemini` at runtime,
same DI pattern household_router.py already uses for the exact same reason
(patch.object(bot, "call_gemini", ...) in tests must keep affecting this
module's own Gemini call). `quantities` is safe to import directly (it also
never imports bot.py) — its own unit-word list is reused for the pre-gate
below instead of a second, drifting copy.

Pre-gate: `looks_household_like(text)`, a cheap, deterministic, no-Gemini
check `bot.py`'s `_try_mini_action_planner` runs BEFORE ever calling
`classify()`. Without it, every genuinely unrelated message (small talk,
"поясни, чому...", coding/history questions) would still cost a real
Gemini call just to be classified "unknown" — this bot already treats a
plain AI-chat answer as the cheap, common case, so the planner itself must
stay opt-in for messages that at least LOOK household-shaped. Deliberately
high-recall, not exhaustive: a false negative here only means a genuinely
household-shaped message falls through to general AI-chat instead of a
preview (exactly today's pre-planner behavior — never a regression, only a
missed opportunity); a false positive only costs one extra classify() call
that then safely resolves to "unknown". Matches on VERBS/CONTEXT words
(buying, missing, needing, cooking, home-inventory) and quantity/unit
patterns — deliberately NOT on bare product nouns ("молоко", "сир", ...),
so an explanatory question that happens to mention a product ("Поясни,
чому молоко згортається в каві?") never matches on the word "молоко" alone.
"""
import json
import re

import quantities

_bot = None


def configure(bot_module):
    global _bot
    _bot = bot_module


_ALLOWED_ACTIONS = {"add_to_shopping", "add_to_inventory", "ask_inventory", "meal_ideas", "unknown"}

_FALLBACK = {"action": "unknown", "items": []}

MINI_ACTION_PLANNER_PROMPT = (
    "Ти — розпізнавач наміру для приватного домашнього Telegram-бота одного господарства. Твоя ЄДИНА "
    "задача — визначити, яку з п'яти дій хоче користувач, і повернути СТРОГО валідний JSON, без Markdown "
    "і без жодного тексту поза JSON.\n\n"
    "Дії (action) — рівно одна з:\n"
    "- \"add_to_shopping\" — користувач хоче ДОДАТИ товар(и) до спільного списку покупок — включно з "
    "розмовними формами наміру купити («молока б докупити», «треба ще купити хліба», «докуплю сиру»), "
    "не лише прямим наказом «додай».\n"
    "- \"add_to_inventory\" — користувач хоче ЗАПИСАТИ товар(и) у запаси вдома — включно з декларативним "
    "твердженням про те, що ВЖЕ Є вдома, особливо з кількістю («у нас є 10 яєць і 2 літри молока», "
    "«маємо ще пів кілограма сиру») — це означає ЗАПИСАТИ ці товари в запаси, а не питання.\n"
    "- \"ask_inventory\" — користувач ПИТАЄ, що є вдома в запасах, без наміру щось додавати чи готувати "
    "(«Що є вдома?», «Чи є молоко?») — НЕ використовуй цю дію, якщо в тексті є слово про кількість, яку "
    "щойно з'явилась/куплено (тоді це add_to_inventory), і НЕ використовуй її для запитів про вечерю/"
    "обід/сніданок/готування (тоді це meal_ideas).\n"
    "- \"meal_ideas\" — користувач просить ідеї страв, що приготувати, або що зробити на вечерю/обід/"
    "сніданок із наявних продуктів («на вечерю щось з того що є», «що приготувати?») — навіть якщо "
    "фраза схожа на запитання про запаси, слово вечеря/обід/сніданок/готувати переважає над "
    "ask_inventory.\n"
    "- \"unknown\" — будь-що інше: звичайна розмова, пояснювальні питання («чому...», «як...», "
    "«поясни...»), витрати, знижки, розрахунки цін, чи якщо не впевнений щодо чотирьох дій вище.\n\n"
    "Для \"add_to_shopping\"/\"add_to_inventory\" заповни items — масив об'єктів {\"name\": назва товару "
    "БЕЗ слів про кількість чи тару, \"quantity_text\": кількість як у тексті, або порожній рядок якщо "
    "кількість не вказана}. Якщо товарів декілька — додай кожен окремим об'єктом. Для будь-якої іншої "
    "дії items завжди порожній масив [].\n"
    "Ніколи не вигадуй суми грошей, витрати, знижки чи списання запасів — якщо текст про це (навіть "
    "частково), обирай \"unknown\".\n"
    "Якщо не впевнений, що це саме одна з дій add_to_shopping/add_to_inventory/ask_inventory/meal_ideas — "
    "завжди обирай \"unknown\", ніколи не вгадуй.\n\n"
    "Приклади:\n"
    '"молока б докупити" -> {"action":"add_to_shopping","items":[{"name":"Молоко","quantity_text":""}]}\n'
    '"у нас є 10 яєць і 2 літри молока" -> {"action":"add_to_inventory","items":'
    '[{"name":"Яйця","quantity_text":"10"},{"name":"Молоко","quantity_text":"2 л"}]}\n'
    '"на вечерю щось з того що є" -> {"action":"meal_ideas","items":[]}\n'
    '"Що є вдома?" -> {"action":"ask_inventory","items":[]}\n'
    '"Поясни, чому молоко згортається в каві?" -> {"action":"unknown","items":[]}\n'
    '"я купив печиво зі знижкою 50%, воно коштувало 20" -> {"action":"unknown","items":[]}'
)


def _extract_json(raw):
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    return json.loads(cleaned)


def _ask_gemini(text):
    """ONE Gemini call, classifier-only. Never raises — any failure at any
    step (no API key, network error, empty response, malformed JSON, wrong
    top-level shape) collapses to the same safe `_FALLBACK` dict."""
    raw = _bot.call_gemini([{"role": "user", "content": text}], MINI_ACTION_PLANNER_PROMPT, temperature=0.0)
    if not raw:
        return dict(_FALLBACK)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_FALLBACK)
    if not isinstance(data, dict):
        return dict(_FALLBACK)

    action = data.get("action")
    if action not in _ALLOWED_ACTIONS:
        action = "unknown"

    items = data.get("items")
    if not isinstance(items, list):
        items = []

    return {"action": action, "items": items}


def classify(text):
    """Public entrypoint. Returns {"action": one of _ALLOWED_ACTIONS,
    "items": [...]}  — `items` is the RAW (not yet item-validated) list
    Gemini returned, only ever meaningful for add_to_shopping/
    add_to_inventory. Never calls Gemini for blank/non-string input — that
    case is unambiguous and doesn't need a network call to resolve."""
    if not isinstance(text, str) or not text.strip():
        return dict(_FALLBACK)
    return _ask_gemini(text)


# =========================
# PRE-GATE — see this module's own docstring ("Pre-gate") for the full
# reasoning. Pure/local, never calls Gemini.
# =========================

# Quantity/unit pattern ("1 л", "500г", "2 шт.") — reuses quantities.py's
# own unit-word list (Ukrainian + English aliases already maintained there)
# instead of a second, drifting copy. Longest-first so e.g. "грамів" is
# tried before "грам" would otherwise short-circuit inside a longer word.
_QUANTITY_UNIT_WORDS = sorted(
    set(quantities._UNIT_ALIASES.keys()) | quantities.STRUCTURED_UNITS, key=len, reverse=True,
)
_QUANTITY_PATTERN_RE = re.compile(
    r"\d+[.,]?\d*\s*(?:" + "|".join(re.escape(u) for u in _QUANTITY_UNIT_WORDS) + r")\b",
    re.IGNORECASE,
)

# Household/shopping/inventory/meal VERB and CONTEXT roots — deliberately
# substrings, not whole-word regexes, so common Ukrainian/Russian/Polish
# inflections ("купити"/"купив"/"купила"/"докупити", "kupić"/"kupiłem")
# all match through a single short root without an exhaustive conjugation
# list. Deliberately EXCLUDES bare product/food nouns ("молоко", "сир",
# "хліб", ...) — see the module docstring's worked example
# ("Поясни, чому молоко згортається в каві?" must NOT match on "молоко").
_HOUSEHOLD_VOCAB_SUBSTRINGS = (
    # Ukrainian — buying/needing/missing/adding/home-inventory
    "купи", "треба", "потрібн", "закінч", "немає", "нема ", "дода",
    "запас", "покупк", "холодильник", "комор", "продукт", "вдома",
    # Ukrainian — cooking/meal
    "вечер", "обід", "сніданок", "готу", "страв", "рецепт",
    # Polish
    "kupi", "brakuj", "potrzeb", "zakup", "lodówk", "spiżarni",
    "obiad", "kolacj", "śniadani", "gotow",
    # Russian
    "нужно", "надо", "законч", "ужин", "обед", "завтрак", "готов",
)


def looks_household_like(text):
    """True if `text` plausibly names a household/shopping/inventory/meal
    request and is therefore worth one real `classify()` Gemini call; False
    means the caller should skip straight to general AI-chat without ever
    calling Gemini here. High-recall by design (see module docstring) — a
    quantity/unit pattern OR any one vocabulary root is enough."""
    if not isinstance(text, str) or not text.strip():
        return False
    normalized = text.strip().lower()
    if _QUANTITY_PATTERN_RE.search(normalized):
        return True
    return any(root in normalized for root in _HOUSEHOLD_VOCAB_SUBSTRINGS)
