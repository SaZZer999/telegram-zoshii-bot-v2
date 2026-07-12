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
module's own Gemini call).
"""
import json
import re

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
    "- \"add_to_shopping\" — додати товар(и) до спільного списку покупок.\n"
    "- \"add_to_inventory\" — додати товар(и) до запасів удома.\n"
    "- \"ask_inventory\" — користувач питає, що є вдома в запасах (без наміру щось додавати чи змінювати).\n"
    "- \"meal_ideas\" — користувач просить ідеї страв або що приготувати.\n"
    "- \"unknown\" — будь-що інше, або якщо не впевнений щодо чотирьох дій вище.\n\n"
    "Для \"add_to_shopping\"/\"add_to_inventory\" заповни items — масив об'єктів {\"name\": назва товару "
    "БЕЗ слів про кількість чи тару, \"quantity_text\": кількість як у тексті, або порожній рядок якщо "
    "кількість не вказана}. Для будь-якої іншої дії items завжди порожній масив [].\n"
    "Ніколи не вигадуй суми грошей, витрати чи списання запасів — якщо текст про це, обирай \"unknown\".\n"
    "Якщо не впевнений, що це саме одна з дій add_to_shopping/add_to_inventory/ask_inventory/meal_ideas — "
    "завжди обирай \"unknown\", ніколи не вгадуй.\n\n"
    "Відповідай ТІЛЬКИ JSON, наприклад:\n"
    '{"action":"add_to_shopping","items":[{"name":"Молоко","quantity_text":"1 л"}]}\n'
    '{"action":"ask_inventory","items":[]}\n'
    '{"action":"unknown","items":[]}'
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
