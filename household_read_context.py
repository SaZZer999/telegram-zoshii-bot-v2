"""Household Read Context V1.

A small, isolated read-only layer that lets the bot answer questions about
the household's REAL inventory and active shopping list — "Чи є молоко?",
"Що є з молочного?", "Що є вдома?", "Що треба купити?" — without ever
writing to the database, opening a preview, or starting any pending state.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — every fact this module reports comes from `HouseholdReadDeps`
callbacks injected by bot.py, which own the real DB connection and the real
Gemini call. This module only decides WHETHER a message is a household
read-question and, if so, WHAT already-fetched rows answer it.

Two-layer recognition, cheapest first:
1. Deterministic regex/exact-phrase parsing (`_deterministic_parse`) — no
   Gemini call at all. Covers every phrasing this module is documented to
   support except the deliberately non-standard ones.
2. A narrow local topic gate (`_TOPIC_GATE_RE`) decides whether a message
   that deterministic parsing didn't recognize is even worth a Gemini call.
   Only messages that pass the gate reach `_classify_with_gemini` — an
   ordinary chat message ("Як справи?") never does. Gemini here is a
   classifier ONLY: it returns intent/product/category as strict JSON,
   never a user-facing answer, never SQL, never a quantity, never a write
   intent. Its output is validated the same way real DB rows are searched:
   exact canonical-name equality, no fuzzy matching, no invented products.

Two public entrypoints share the exact same deterministic parser and answer
builders (no duplicated logic):
- `try_handle_direct_household_read` — deterministic-only, no topic gate,
  no Gemini, ever. Meant to be checked BEFORE the saved-list router (see
  message_dispatcher.py's `DispatcherDeps.direct_household_read`) so an
  explicit "Що треба купити?" is answered even while a saved shopping/
  inventory list context is open, instead of being swallowed by the saved-
  list router's own AI edit-parser.
- `try_handle_household_read` — the full route: tries the same
  deterministic parser first, then falls back to the topic gate + Gemini
  classifier for non-standard phrasings. Meant to stay in its existing
  Phase D slot, after cooking mode and before the general AI fallback.

Item lookup never guesses: a query product is normalized through
`resolve_item_name` (household alias, then built-in synonym — the exact
same resolver used everywhere else in the project, so old raw rows like
"ser"/"mleko" are found without ever rewriting them in the DB) and compared
by exact canonical-name equality against every row's own (re-)normalized
canonical name. If several distinct rows share that canonical name, ALL of
them are shown — never just the first.

The "not found" answers here are deliberately narrow: this module only ever
states what IS or ISN'T in the two live tables it reads. It never claims an
absent shopping-list item was "already bought", never estimates what's
running low, and never suggests a recipe or a restock — none of that is in
scope for V1.
"""
import json
import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class HouseholdReadDeps:
    """Injected read-only callbacks — no import of bot.py or database.py,
    ever. Every DB-touching field is a thin runtime lambda-forward owned by
    bot.py (same `patch.object(bot, ...)` reasoning as every other
    dependency container in this project)."""
    get_household_and_user: Callable
    get_inventory_items: Callable
    get_active_shopping_items: Callable
    get_household_alias_map: Callable
    resolve_item_name: Callable
    canonicalize_name: Callable
    format_quantity_display: Callable
    format_inventory_list: Callable
    format_shopping_list: Callable
    call_gemini: Callable
    send_message: Callable
    category_order: list


# =========================
# Deterministic parsing — no Gemini involved at all.
# =========================

_VAGUE_FIRST_WORDS = {"щось", "що-небудь", "чогось", "нічого", "дещо", "будь-що"}

_OVERVIEW_PHRASES = {
    "що є вдома",
    "що є в запасах",
    "що є в нас вдома",
    "покажи що є вдома",
    "покажи що залишилось",
    "покажи що у нас є",
    "що залишилось у запасах",
    "що залишилось",
    "що лишилось",
}

_SHOPPING_OVERVIEW_PHRASES = {
    "що у списку покупок",
    "що в списку покупок",
    "що треба купити",
    "що купити",
}

# Deterministic keyword -> exact category name lookup. Substring containment
# only (no edit-distance/fuzzy matching) against the same fixed category set
# bot.py's CATEGORY_ORDER already defines — this table only maps a
# conversational adjective ("молочного", "м'ясо") to that exact noun-phrase
# category string.
_CATEGORY_KEYWORDS = {
    "м'ясо": "М'ясо та риба", "мясо": "М'ясо та риба", "рибн": "М'ясо та риба",
    "молочн": "Молочне та яйця", "яйц": "Молочне та яйця",
    "овоч": "Овочі та зелень", "зелен": "Овочі та зелень",
    "фрукт": "Фрукти та ягоди", "ягод": "Фрукти та ягоди",
    "хліб": "Хліб і випічка", "випічк": "Хліб і випічка",
    "круп": "Крупи, макарони та борошно", "макарон": "Крупи, макарони та борошно",
    "борошн": "Крупи, макарони та борошно",
    "соус": "Соуси, спеції та бакалія", "спец": "Соуси, спеції та бакалія",
    "бакалі": "Соуси, спеції та бакалія",
    "солодк": "Солодке та снеки", "снек": "Солодке та снеки",
    "нап": "Напої",
    "заморожен": "Заморожене",
}

# Genitive short labels for "X зараз нічого немає" / "Ось що є з X" messages
# — a small static dictionary (same style as CATEGORY_EMOJIS in bot.py), not
# a grammar engine.
_CATEGORY_GENITIVE_LABELS = {
    "М'ясо та риба": "м'яса",
    "Молочне та яйця": "молочного",
    "Овочі та зелень": "овочів",
    "Фрукти та ягоди": "фруктів",
    "Хліб і випічка": "хліба",
    "Крупи, макарони та борошно": "круп",
    "Соуси, спеції та бакалія": "соусів",
    "Солодке та снеки": "солодкого",
    "Напої": "напоїв",
    "Заморожене": "замороженого",
    "Інше їстівне": "іншого",
}

_SHOPPING_PRESENCE_RE_LIST = [
    re.compile(r"^чи\s+є\s+(?P<product>.+?)\s+(?:у|в)\s+покупках\s*\??$", re.IGNORECASE),
    re.compile(r"^(?P<product>.+?)\s+ще\s+є\s+у\s+списку\s+покупок\s*\??$", re.IGNORECASE),
]

_CATEGORY_RE_LIST = [
    re.compile(r"^що\s+є\s+з\s+(?P<category>.+?)\s*\??$", re.IGNORECASE),
    re.compile(r"^як(?:е|ий|а|і)\s+(?P<category>.+?)\s+є(?:\s+вдома)?\s*\??$", re.IGNORECASE),
    re.compile(r"^які\s+(?P<category>.+?)\s+залиш(?:илися|ились|илась)\s*\??$", re.IGNORECASE),
]

_PRESENCE_RE_LIST = [
    re.compile(r"^чи\s+є\s+(?:в\s+нас\s+|у\s+нас\s+|вдома\s+)?(?P<product>.+?)\s*\??$", re.IGNORECASE),
    re.compile(r"^є\s+(?:в\s+нас\s+|у\s+нас\s+|вдома\s+)?(?P<product>.+?)\s*\??$", re.IGNORECASE),
    re.compile(r"^чи\s+залиш(?:ився|илася|илось|илися)\s+(?P<product>.+?)\s*\??$", re.IGNORECASE),
]

# Broad ON/OFF switch for whether a message is even worth a Gemini call once
# deterministic parsing gave up. Deliberately not narrowed to exact
# grammar — false positives here only cost one extra Gemini call that will
# itself return "none"; false negatives just fall through to the unchanged
# general AI fallback. Never fuzzy-matched, just a fixed keyword-stem list.
_TOPIC_GATE_RE = re.compile(
    r"чи\s+є|(?<!\w)є(?!\w)|лиш(ил|ається)\w*|залиш\w*|вдома|запас\w*|покуп\w*|купити|списк\w*",
    re.IGNORECASE,
)

_ALLOWED_INTENTS = {
    "inventory_presence", "inventory_category", "inventory_overview",
    "shopping_overview", "shopping_presence", "none",
}

# Typographic quote characters a message may be wrapped in ("«Що треба
# купити?»", a lone opening "«Що треба купити?" from a copy-paste, etc.).
# Deliberately does NOT include any apostrophe variant (' or ’) — those are
# never stripped, since Ukrainian words like "м'ясо" use an apostrophe as a
# real letter, never as a wrapping quote.
_WRAPPING_QUOTE_CHARS = "«»„“”\""


def _strip_wrapping_quotes(text):
    """Strip a leading/trailing typographic quote character (if present)
    plus surrounding whitespace. Each side is checked independently, so an
    unbalanced opening quote with no closing quote is handled too. Never
    touches an apostrophe anywhere in the text — only the small fixed
    quote-character set above, and only at the very start/end."""
    stripped = (text or "").strip()
    if stripped and stripped[0] in _WRAPPING_QUOTE_CHARS:
        stripped = stripped[1:].strip()
    if stripped and stripped[-1] in _WRAPPING_QUOTE_CHARS:
        stripped = stripped[:-1].strip()
    return stripped


def _normalize_phrase(text):
    normalized = (text or "").strip().lower()
    normalized = re.sub(r"[,.!?]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _clean_capture(raw):
    if not raw:
        return None
    cleaned = raw.strip().strip("?!.,")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _is_vague(product):
    first_word = product.split(" ", 1)[0].lower().strip("?!.,")
    return first_word in _VAGUE_FIRST_WORDS


def _resolve_category_keyword(category_raw, category_order):
    normalized = category_raw.lower()
    for stem, category_name in _CATEGORY_KEYWORDS.items():
        if stem in normalized and category_name in category_order:
            return category_name
    return None


def _deterministic_parse(text, category_order):
    stripped = _strip_wrapping_quotes(text)
    normalized_full = _normalize_phrase(stripped)

    if normalized_full in _OVERVIEW_PHRASES:
        return {"intent": "inventory_overview"}
    if normalized_full in _SHOPPING_OVERVIEW_PHRASES:
        return {"intent": "shopping_overview"}

    for pattern in _SHOPPING_PRESENCE_RE_LIST:
        m = pattern.match(stripped)
        if m:
            product = _clean_capture(m.group("product"))
            if product and not _is_vague(product):
                return {"intent": "shopping_presence", "product": product}

    for pattern in _CATEGORY_RE_LIST:
        m = pattern.match(stripped)
        if m:
            category_raw = _clean_capture(m.group("category"))
            if category_raw:
                category = _resolve_category_keyword(category_raw, category_order)
                if category:
                    return {"intent": "inventory_category", "category": category}

    for pattern in _PRESENCE_RE_LIST:
        m = pattern.match(stripped)
        if m:
            product = _clean_capture(m.group("product"))
            if product and not _is_vague(product):
                return {"intent": "inventory_presence", "product": product}

    return None


# =========================
# Gemini classifier — narrow JSON intent-only fallback.
# =========================

_CLASSIFIER_SYSTEM_PROMPT = (
    "Ти вузький класифікатор наміру для read-only питань одного домашнього господарства про його "
    "запаси (inventory) або активний список покупок (shopping list). Ти НІКОЛИ не відповідаєш "
    "користувачу фактами, не пишеш SQL, не маєш доступу до бази даних і ніколи не повертаєш "
    "write-наміри (додати/купити/списати/видалити).\n\n"
    "Визнач намір рівно одним із: inventory_presence, inventory_category, inventory_overview, "
    "shopping_overview, shopping_presence, none.\n"
    "- inventory_presence / shopping_presence — лише якщо користувач явно називає КОНКРЕТНИЙ товар "
    "(поле product). Якщо товар не названий явно (напр. розмовне «щось», «якісь продукти») — "
    "поверни none.\n"
    "- inventory_category — лише якщо категорія явно названа або однозначно випливає з тексту "
    "(поле category, напр. «молочне», «м'ясо», «фрукти»).\n"
    "- inventory_overview / shopping_overview — загальний огляд, без конкретного товару чи категорії.\n\n"
    "Ніколи не вигадуй товар чи категорію, яких немає в тексті користувача. Ніколи не повертай "
    "кількість і жодної готової відповіді користувачу. У сумнівному випадку завжди повертай "
    "intent «none».\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON:\n"
    "{\"intent\": \"inventory_presence\", \"product\": \"молоко\", \"category\": null}\n"
    "Приклад none: {\"intent\": \"none\", \"product\": null, \"category\": null}"
)


def _extract_json(raw):
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    return json.loads(cleaned)


def _classify_with_gemini(deps, text, category_order):
    """One narrow Gemini call, classifier-only. Returns a parsed intent dict
    (same shape as `_deterministic_parse`) or None for anything invalid,
    unavailable, or explicitly "none" — callers treat None as unhandled and
    fall through to the general AI fallback, never fabricating an answer."""
    try:
        raw = deps.call_gemini(
            [{"role": "user", "content": text}],
            _CLASSIFIER_SYSTEM_PROMPT,
            temperature=0.0,
        )
        if not raw:
            return None
        data = _extract_json(raw)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in _ALLOWED_INTENTS or intent == "none":
        return None

    if intent in ("inventory_presence", "shopping_presence"):
        product = data.get("product")
        if not isinstance(product, str) or not _clean_capture(product):
            return None
        return {"intent": intent, "product": _clean_capture(product)}

    if intent == "inventory_category":
        category_raw = data.get("category")
        if not isinstance(category_raw, str) or not _clean_capture(category_raw):
            return None
        category = _resolve_category_keyword(_clean_capture(category_raw), category_order)
        if not category:
            return None
        return {"intent": intent, "category": category}

    # inventory_overview / shopping_overview need no extra fields.
    return {"intent": intent}


# =========================
# Fact lookup — exact canonical-name equality only, never fuzzy.
# =========================

def _row_canonical(deps, row):
    stored = row.get("canonical_name")
    base = stored if stored else row.get("name", "")
    return deps.canonicalize_name(base) if base else ""


def _quantity_text(deps, item):
    value = item.get("quantity_value")
    unit = item.get("quantity_unit")
    if value is not None:
        return deps.format_quantity_display(value, unit)
    return item.get("quantity_text") or ""


def _format_item_line(deps, item):
    qty = _quantity_text(deps, item)
    return f"• {item['name']} — {qty}" if qty else f"• {item['name']}"


def _find_matches(deps, items, product, alias_map):
    _, query_canonical = deps.resolve_item_name(product, alias_map)
    if not query_canonical:
        return []
    return [item for item in items if _row_canonical(deps, item) == query_canonical]


# =========================
# Answer builders — one send_message call each, no writes.
# =========================

def _answer_presence(deps, chat_id, household_id, product, get_items, found_header, not_found_msg):
    alias_map = deps.get_household_alias_map(household_id)
    items = get_items(household_id)
    matches = _find_matches(deps, items, product, alias_map)
    if not matches:
        deps.send_message(chat_id, not_found_msg.format(product=product))
        return
    lines = [_format_item_line(deps, item) for item in matches]
    if len(matches) == 1:
        deps.send_message(chat_id, found_header + "\n" + "\n".join(lines))
    else:
        deps.send_message(chat_id, f"Знайшов декілька позицій «{product}»:\n" + "\n".join(lines))


def _answer_inventory_presence(deps, chat_id, household_id, product):
    _answer_presence(
        deps, chat_id, household_id, product,
        deps.get_inventory_items,
        "Так, є:",
        "Ні, «{product}» зараз немає в запасах.",
    )


def _answer_shopping_presence(deps, chat_id, household_id, product):
    _answer_presence(
        deps, chat_id, household_id, product,
        deps.get_active_shopping_items,
        "Так, у списку покупок є:",
        "У активному списку покупок «{product}» немає.",
    )


def _answer_inventory_category(deps, chat_id, household_id, category):
    items = deps.get_inventory_items(household_id)
    matches = [item for item in items if (item.get("category") or "") == category]
    label = _CATEGORY_GENITIVE_LABELS.get(category, category)
    if not matches:
        deps.send_message(chat_id, f"З {label} зараз нічого немає.")
        return
    lines = [_format_item_line(deps, item) for item in matches]
    deps.send_message(chat_id, f"Ось що є з {label}:\n" + "\n".join(lines))


def _answer_inventory_overview(deps, chat_id, household_id):
    items = deps.get_inventory_items(household_id)
    deps.send_message(chat_id, deps.format_inventory_list(items))


def _answer_shopping_overview(deps, chat_id, household_id):
    items = deps.get_active_shopping_items(household_id)
    deps.send_message(chat_id, deps.format_shopping_list(items))


def _dispatch_parsed(deps, chat_id, user_id, display_name, parsed):
    """Shared by both public entrypoints: resolves household_id once and
    calls the matching answer builder for an already-recognized intent
    dict (from either `_deterministic_parse` or `_classify_with_gemini`).
    Returns True (an answer was sent) or False (unrecognized intent —
    defensive, should not happen for either caller)."""
    household_id, _ = deps.get_household_and_user(user_id, display_name)
    intent = parsed["intent"]

    if intent == "inventory_presence":
        _answer_inventory_presence(deps, chat_id, household_id, parsed["product"])
    elif intent == "shopping_presence":
        _answer_shopping_presence(deps, chat_id, household_id, parsed["product"])
    elif intent == "inventory_category":
        _answer_inventory_category(deps, chat_id, household_id, parsed["category"])
    elif intent == "inventory_overview":
        _answer_inventory_overview(deps, chat_id, household_id)
    elif intent == "shopping_overview":
        _answer_shopping_overview(deps, chat_id, household_id)
    else:
        return False

    return True


def try_handle_direct_household_read(deps, chat_id, user_id, display_name, text):
    """Direct/deterministic-only entrypoint — no local topic gate, no
    Gemini call, ever, and no new state. Meant to be checked BEFORE the
    saved-list router (see message_dispatcher.py's `DispatcherDeps.
    direct_household_read`) so an explicit read-question like "Що треба
    купити?" is answered even while a saved shopping/inventory list context
    is open. Returns True only when a deterministic pattern matched and an
    answer has already been sent via `deps.send_message`; False otherwise
    (caller must continue, e.g. into saved_list_router)."""
    if not isinstance(text, str) or not text.strip():
        return False

    parsed = _deterministic_parse(text, deps.category_order)
    if parsed is None:
        return False

    return _dispatch_parsed(deps, chat_id, user_id, display_name, parsed)


def try_handle_household_read(deps, chat_id, user_id, display_name, text):
    """Public entrypoint. Returns True if `text` was a household read-
    question and an answer has already been sent via `deps.send_message`;
    False if it wasn't (caller should continue to the general AI fallback).
    Never writes to the DB, never opens a preview, never stores any new
    state of its own. Tries the same deterministic parser
    `try_handle_direct_household_read` uses first — only a message that
    parser doesn't recognize ever reaches the local topic gate + Gemini
    classifier fallback."""
    if not isinstance(text, str) or not text.strip():
        return False

    parsed = _deterministic_parse(text, deps.category_order)
    if parsed is None:
        if not _TOPIC_GATE_RE.search(text):
            return False
        parsed = _classify_with_gemini(deps, text, deps.category_order)
        if parsed is None:
            return False

    return _dispatch_parsed(deps, chat_id, user_id, display_name, parsed)
