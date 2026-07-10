"""Meal Ideas V1.

A small, isolated read-only layer that answers "Що приготувати?"-style
questions with 3-5 realistic meal ideas built from the household's REAL
inventory snapshot — never from a manually-typed product list, never by
writing to the database, opening a preview, or starting any pending state.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — every fact this module uses comes from `MealIdeasDeps` callbacks
injected by bot.py, which owns the real DB connection and the real Gemini
call. This module only decides WHETHER a message is a meal-ideas request
and, if so, builds the inventory snapshot and the one Gemini call that
turns it into suggestions.

Recognizes two shapes of the same request, both routed through the single
public entrypoint `try_handle_meal_ideas`:
1. The dedicated "🍽 Що приготувати" / "🍽️ Що приготувати" menu button
   (bot.py's special-button route calls this directly, before ever
   touching `waiting_for_ingredients`).
2. A small fixed set of natural-language phrasings ("Що можна
   приготувати?", "Що приготувати з того, що є?", "Що зробити на
   вечерю?", "Порадь щось на вечерю з того, що є.") — checked in Phase D,
   after household_read and before the general AI fallback, so it never
   intercepts a plain read-question ("Що треба купити?") or unrelated food
   talk ("Я люблю піцу", "Розкажи історію борщу", "Що таке карбонара?").

Gemini is given the raw inventory snapshot as confirmed fact and is
instructed never to claim a product exists that isn't in it — the snapshot
itself (not the model) is the only source of truth for what's "at home".
"""
import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class MealIdeasDeps:
    """Injected read-only callbacks — no import of bot.py or database.py,
    ever. Every DB-touching field is a thin runtime lambda-forward owned by
    bot.py (same `patch.object(bot, ...)` reasoning as every other
    dependency container in this project)."""
    get_household_and_user: Callable
    get_inventory_items: Callable
    format_quantity_display: Callable
    call_gemini: Callable
    send_message: Callable


# =========================
# Request recognition — deterministic only, no Gemini involved here.
# =========================

# Same two variation-selector codepoints message_dispatcher.strip_variation_
# selectors handles — duplicated locally on purpose (this module must never
# import message_dispatcher.py/bot.py) so "🍽️ Що приготувати" and "🍽 Що
# приготувати" are recognized identically regardless of which one a given
# Telegram client cache sent.
_VARIATION_SELECTORS = "️︎"

_MEAL_IDEAS_RE_LIST = [
    re.compile(r"^що\s+можна\s+приготувати\b", re.IGNORECASE),
    re.compile(r"^що\s+(б|ж)?\s*приготувати\b", re.IGNORECASE),
    re.compile(r"^що\s+зробити\s+на\s+(вечерю|обід|сніданок)\b", re.IGNORECASE),
    re.compile(r"^порадь.*на\s+(вечерю|обід|сніданок)\b", re.IGNORECASE),
    # Routing Stabilization v1 — "запропонуй вечерю"/"запропонуй вечерю з
    # того що є" (a live voice-transcript phrasing "порадь"/"зробити" above
    # didn't cover). Requires "вечерю"/"обід"/"сніданок" to appear
    # somewhere after "запропонуй" so a genuinely unrelated "запропонуй
    # щось цікаве" never matches.
    re.compile(r"^запропонуй\b.*\b(вечерю|обід|сніданок)\b", re.IGNORECASE),
]


def _strip_button_emoji_prefix(text):
    """Strip a leading "🍽"/"🍽️" button prefix (variation selector already
    removed) plus surrounding whitespace, so the button label and the
    equivalent typed phrase ("Що приготувати") share the exact same regex
    match below. Text with no such prefix is returned unchanged."""
    stripped = text.translate({ord(ch): None for ch in _VARIATION_SELECTORS}).strip()
    if stripped.startswith("🍽"):
        stripped = stripped[1:].strip()
    return stripped


def _looks_like_meal_ideas_request(text):
    """True if `text` is the dedicated button or one of the small fixed set
    of natural meal-ideas phrasings this module supports. Deliberately
    narrow — a false negative just falls through to the general AI
    fallback (or household_read, checked first); a false positive would
    wrongly hijack unrelated food talk or a plain "Що треба купити?"
    read-question, so every pattern anchors at the start of the (button-
    prefix-stripped) text."""
    if not isinstance(text, str) or not text.strip():
        return False
    candidate = _strip_button_emoji_prefix(text)
    normalized = candidate.strip().rstrip("?!.,").strip()
    if not normalized:
        return False
    return any(pattern.match(normalized) for pattern in _MEAL_IDEAS_RE_LIST)


# =========================
# Inventory snapshot — real DB rows, verbatim. Never merged, never
# renamed, never hidden, even for a weird legacy row like "ser"/"mleko".
# =========================

def _item_quantity_text(deps, item):
    value = item.get("quantity_value")
    unit = item.get("quantity_unit")
    if value is not None:
        return deps.format_quantity_display(value, unit)
    return item.get("quantity_text") or ""


def _build_inventory_snapshot(deps, items):
    lines = []
    for item in items:
        qty = _item_quantity_text(deps, item)
        name = item.get("name", "")
        lines.append(f"- {name} — {qty}" if qty else f"- {name}")
    return "Inventory:\n" + "\n".join(lines)


# =========================
# Gemini prompt — free-form Ukrainian text back to the user, never JSON.
# =========================

MEAL_IDEAS_SYSTEM_PROMPT = (
    "Ти домашній кулінарний помічник одного господарства. Відповідай ЛИШЕ українською мовою, "
    "звичайним текстом для користувача (не JSON, не Markdown-код).\n\n"
    "Тобі надано реальний список продуктів, які зараз є вдома (Inventory) — це ЄДИНЕ джерело "
    "правди про наявні продукти.\n"
    "- Вважай підтвердженим наявним лише те, що прямо є у наданому списку Inventory.\n"
    "- Ніколи не стверджуй, що вдома є продукт, якого немає у списку.\n"
    "- Можеш запропонувати додатковий інгредієнт, якого немає у списку, але ЗАВЖДИ явно познач "
    "його як «докупити / опціонально» — ніколи не змішуй його з наявними продуктами без позначки.\n"
    "- Віддавай перевагу простим домашнім стравам.\n"
    "- Запропонуй від 3 до 5 ідей.\n"
    "- Для кожної ідеї вкажи: назву страви; які продукти зі списку вона використовує; короткий "
    "спосіб приготування; за потреби — опціональні інгредієнти, яких бракує.\n"
    "- Якщо запасів мало або вони дивні — прямо скажи, що вибір обмежений, і запропонуй "
    "найпростіші можливі варіанти з того, що є.\n"
    "- Ніколи не пиши, що не маєш доступу до холодильника чи запасів — список продуктів тобі вже "
    "надано нижче.\n\n"
    "Формат відповіді (приклад):\n"
    "🍽️ Ідеї з того, що є вдома:\n\n"
    "1. Курка з хлібом і соусом\n"
    "   Використаєш: курка, хліб, соус.\n"
    "   Як зробити: коротко...\n\n"
    "2. Омлет із сиром\n"
    "   Використаєш: яйця, сир.\n"
    "   Опціонально докупити: зелень."
)

_EMPTY_INVENTORY_MSG = (
    "У запасах зараз нічого не знайшов. Додай продукти в запаси або напиши мені вручну, що є вдома."
)

_MEAL_IDEAS_FALLBACK_MSG = "Не зміг зараз придумати страви. Спробуй ще раз трохи пізніше."


def try_handle_meal_ideas(deps, chat_id, user_id, display_name, text):
    """Public entrypoint. Returns True if `text` was a meal-ideas request
    and a message has already been sent via `deps.send_message` (an idea
    list, the empty-inventory message, or the Gemini-failure fallback);
    False if it wasn't (caller should continue, e.g. to the general AI
    fallback). Never writes to the DB, never opens a preview, never stores
    any new state of its own — read-only, same as household_read_context.py."""
    if not _looks_like_meal_ideas_request(text):
        return False

    household_id, _ = deps.get_household_and_user(user_id, display_name)
    items = deps.get_inventory_items(household_id)
    if not items:
        deps.send_message(chat_id, _EMPTY_INVENTORY_MSG)
        return True

    snapshot = _build_inventory_snapshot(deps, items)
    answer = deps.call_gemini([{"role": "user", "content": snapshot}], MEAL_IDEAS_SYSTEM_PROMPT, temperature=0.5)
    if not answer:
        answer = _MEAL_IDEAS_FALLBACK_MSG
    deps.send_message(chat_id, answer)
    return True
