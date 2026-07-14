"""Inventory Action Planner V1 — a single, narrow Gemini classifier for
inventory-restructuring phrasing that inventory_transform_route/
inventory_cleanup_route/inventory_admin_route's own deterministic regex
parsers (inventory.parse_inventory_transform_request/parse_inventory_
cleanup_request/parse_inventory_rename_request/parse_inventory_delete_
request) already tried and rejected for THIS message — see message_
dispatcher.py's CommandRouteDeps.action_planner_route: checked right after
those three routes, right before saved_list_router.

NOT the same module as mini_action_planner.py ("Unified Mini Action Planner
V1" — add_to_shopping/add_to_inventory/ask_inventory/meal_ideas/unknown,
Phase D last-resort right before general AI-chat). This module is called
"the Inventory Action Planner V1" everywhere in code/docs specifically to
avoid that confusion with the existing, unrelated planner: it owns a
completely different, narrower vocabulary (inventory_transform/inventory_
merge_duplicates/inventory_rename/inventory_delete/clarify/unsupported),
sits in a different part of the dispatch chain (command routes, not Phase
D), and is never invoked for the same message mini_action_planner.py would
also be invoked for — bot.py's _try_action_planner always returns True once
its own pre-gate matches (even for a "clarify"/"unsupported" outcome), so a
message it claims never falls through to Phase D (and therefore never
reaches mini_action_planner.py) at all. mini_action_planner.py itself is
untouched by this module.

    inventory_transform        -> bot.py's _start_inventory_transform (SAME
                                   resolver/preview/pending_inventory_
                                   transform/execute_inventory_transform path
                                   inventory_transform_route already uses).
    inventory_merge_duplicates -> bot.py's _start_inventory_cleanup (SAME
                                   pending_merge/execute_inventory_cleanup_
                                   merge path inventory_cleanup_route already
                                   uses).
    inventory_rename            -> bot.py's _start_inventory_rename (SAME
                                   pending_cleanup_admin/execute_inventory_
                                   rename path, including its no-op-rename
                                   guard).
    inventory_delete            -> bot.py's _start_inventory_delete (SAME
                                   pending_cleanup_admin/execute_inventory_
                                   delete path, including disambiguation and
                                   the natural-quantity matching the live
                                   "Видали молоко одна штука" fix relies on
                                   — see normalize_delete_quantity_hint's own
                                   docstring in inventory.py).
    clarify                     -> a single controlled clarification
                                   message, never a pending state, never a
                                   preview.
    unsupported                 -> a single controlled "couldn't safely
                                   understand this" message — never general
                                   AI-chat, never a guess.

Gemini is asked for STRICT JSON only (one system prompt, one call, no
conversation history) and every field is re-validated in Python before
ANYTHING downstream ever sees it (see _validate_plan): an unrecognized
action, a wrong version, malformed JSON, a Gemini call failure, an empty
response, a disallowed extra argument key (a DB id, SQL, code, an
executor/function name, ...), or a source_names list with fewer than two
entries all safely collapse to the same {"action": "unsupported", ...}
result — never guessed, never partially trusted. Gemini never sees and
never returns database ids, and never decides which inventory rows actually
exist or computes any quantity — it only extracts plain product-name
strings (plus, for inventory_delete, a raw natural-language quantity hint
string); bot.py's own _try_action_planner resolves those names against a
FRESH live inventory snapshot via the SAME inventory.resolve_inventory_
admin_candidates/disambiguation/unit-compatibility machinery inventory_
transform_route/inventory_cleanup_route/inventory_admin_route already use,
and never writes to the database before an explicit confirm.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — configure(bot_module) injects bot.py's own call_gemini at runtime,
same DI pattern household_router.py/mini_action_planner.py already use for
the exact same reason (patch.object(bot, "call_gemini", ...) in tests must
keep affecting this module's own Gemini call).

Pre-gate: looks_like_inventory_admin_or_transform(text), a cheap,
deterministic, no-Gemini check bot.py's _try_action_planner runs BEFORE
ever calling classify() — without it, every message that reaches this
route's position in the dispatch chain (i.e. every deterministic inventory
gate above already rejected it) would still cost a real Gemini call just to
be classified "unsupported". Deliberately high-recall, same "opt-in, never
exhaustive" posture mini_action_planner.looks_household_like already
established for its own last-resort slot: a false negative here only means
a genuinely inventory-restructuring message falls through to
saved_list_router/general AI-chat instead of this planner (no worse than
before this module existed); a false positive only costs one extra
classify() call that safely resolves to "unsupported".
"""
import json
import re

_bot = None


def configure(bot_module):
    global _bot
    _bot = bot_module


_ALLOWED_ACTIONS = {
    "inventory_transform", "inventory_merge_duplicates", "inventory_rename",
    "inventory_delete", "clarify", "unsupported",
}

# Strict per-action argument allowlist — an extra key (a DB id, a SQL
# fragment, a Python/executor name, a computed quantity, ...) invalidates
# the WHOLE plan (see _validate_plan) rather than being silently dropped, so
# nothing outside this closed vocabulary can ever reach a caller.
_ALLOWED_ARGUMENT_KEYS = {
    "inventory_transform": {"source_names", "target_name"},
    "inventory_merge_duplicates": {"product_name"},
    "inventory_rename": {"old_name", "new_name"},
    "inventory_delete": {"item_name", "quantity_hint"},
    "clarify": set(),
    "unsupported": set(),
}

_MIN_SOURCE_NAMES = 2
_MAX_SOURCE_NAMES = 10
_MAX_NAME_LENGTH = 200
_MAX_QUANTITY_HINT_LENGTH = 100
_MAX_CLARIFICATION_LENGTH = 500

_FALLBACK = {
    "version": 1, "action": "unsupported", "arguments": {},
    "confidence": 0.0, "clarification_question": None,
}

UNSUPPORTED_MSG = (
    "Не зміг безпечно розпізнати цю дію із запасами, або вона ще не підтримується.\n\n"
    "Спробуй написати конкретніше, наприклад:\n"
    "«Перейменуй ser на сир»\n"
    "«Об'єднай молоко в запасах»\n"
    "«Видали молоко одна штука»\n"
    "«Об'єднай сосиски і мисливські ковбаски в м'ясні вироби»"
)

ACTION_PLANNER_PROMPT = (
    "Ти — розпізнавач наміру для приватного домашнього Telegram-бота одного господарства. Користувач "
    "написав повідомлення про ЗАПАСИ вдома (inventory), яке звичайні прості правила бота не змогли "
    "розпізнати. Твоя ЄДИНА задача — визначити, яку з шести дій хоче користувач, і повернути СТРОГО "
    "валідний JSON, без Markdown і без жодного тексту поза JSON.\n\n"
    "Дії (action) — рівно одна з:\n"
    "- \"inventory_transform\" — користувач хоче об'єднати ДВІ АБО БІЛЬШЕ РІЗНИХ позицій запасів в ОДНУ "
    "НОВУ узагальнену позицію з новою назвою (напр. «сосиски + мисливські ковбаски → м'ясні вироби», "
    "«об'єднай молоко і вершки в молочну суміш», «перетвори X і Y на Z»). Ознаки: кілька РІЗНИХ товарів "
    "як джерело, і явна НОВА назва результату (після «→», «в», «на», «запиши як», «назви як/це»).\n"
    "- \"inventory_merge_duplicates\" — користувач хоче об'єднати кілька записів ОДНІЄЇ й тієї самої "
    "позиції (дублікати), без нової назви (напр. «Об'єднай усі записи молока», «Об'єднай дублікати молока "
    "в запасах», «Прибери дублікати сиру»). Якщо назва результату НЕ згадана і йдеться про ОДИН товар — це "
    "завжди inventory_merge_duplicates, НІКОЛИ не inventory_transform.\n"
    "- \"inventory_rename\" — користувач хоче перейменувати ОДИН існуючий запис запасів, без зміни "
    "кількості чи товару (напр. «Перейменуй ser на сир», «Виправ назву mlekо на молоко»).\n"
    "- \"inventory_delete\" — користувач хоче видалити/прибрати ОДИН запис запасів (напр. «Видали молоко "
    "одна штука, воно вже не потрібно», «В запасах молоко одна штука вже не потрібне, забери його»). Якщо "
    "в тексті є природна кількість («одна штука», «1 шт», «14,5 л», «пара») — постав її в quantity_hint "
    "рівно так, як написано в тексті (не переводь у число сам, Python сам нормалізує), інакше "
    "quantity_hint — null. Пояснювальні фрази («воно вже не потрібно», «бо зіпсувалося») НЕ входять у "
    "item_name чи quantity_hint.\n"
    "- \"clarify\" — намір зрозумілий лише частково: видно, що це якась дія із запасами (об'єднання, "
    "видалення, перейменування), але бракує конкретних назв або зрозуміло, ЩО зробити, та незрозуміло, З "
    "ЧИМ саме (напр. «Об'єднай це в одну позицію» без жодної назви товару). Постав коротке конкретне "
    "уточнювальне запитання українською в clarification_question.\n"
    "- \"unsupported\" — будь-що інше: дія із запасами, яку цей бот не підтримує (напр. «Зроби повну "
    "інвентаризацію квартири автоматично»), звичайна розмова, читання/перегляд запасів, покупки, витрати, "
    "рецепти, або якщо не впевнений щодо жодної з п'яти дій вище.\n\n"
    "ВАЖЛИВО:\n"
    "- Ти НІКОЛИ не повертаєш ID записів бази даних, SQL, код чи назви функцій — лише звичайні текстові "
    "назви товарів, як їх написав користувач (у називному відмінку, без слів про кількість чи тару).\n"
    "- Ти НІКОЛИ не вирішуєш, які записи РЕАЛЬНО існують у запасах, і не вигадуєш кількість — Python "
    "окремо звірить кожну названу позицію з актуальним станом запасів і сам розрахує будь-яку арифметику "
    "кількостей; твоя робота — лише розпізнати намір і назви.\n"
    "- Якщо не впевнений — обирай \"clarify\" або \"unsupported\", ніколи не вгадуй назву товару чи дію.\n\n"
    "Формат відповіді (arguments — рівно ті поля, що описані для кожної дії, і жодних інших):\n"
    "{\"version\": 1, \"action\": \"inventory_transform\", \"arguments\": {\"source_names\": [\"...\", "
    "\"...\"], \"target_name\": \"...\"}, \"confidence\": 0.9, \"clarification_question\": null}\n"
    "{\"version\": 1, \"action\": \"inventory_merge_duplicates\", \"arguments\": {\"product_name\": "
    "\"...\"}, \"confidence\": 0.9, \"clarification_question\": null}\n"
    "{\"version\": 1, \"action\": \"inventory_rename\", \"arguments\": {\"old_name\": \"...\", "
    "\"new_name\": \"...\"}, \"confidence\": 0.9, \"clarification_question\": null}\n"
    "{\"version\": 1, \"action\": \"inventory_delete\", \"arguments\": {\"item_name\": \"...\", "
    "\"quantity_hint\": \"...\" або null}, \"confidence\": 0.9, \"clarification_question\": null}\n"
    "{\"version\": 1, \"action\": \"clarify\", \"arguments\": {}, \"confidence\": 0.5, "
    "\"clarification_question\": \"...\"}\n"
    "{\"version\": 1, \"action\": \"unsupported\", \"arguments\": {}, \"confidence\": 0.0, "
    "\"clarification_question\": null}\n\n"
    "Приклади:\n"
    "\"сосиски + мисливські ковбаски → м'ясні вироби\" -> {\"version\": 1, \"action\": "
    "\"inventory_transform\", \"arguments\": {\"source_names\": [\"сосиски\", \"мисливські ковбаски\"], "
    "\"target_name\": \"м'ясні вироби\"}, \"confidence\": 0.98, \"clarification_question\": null}\n"
    "\"В запасах об'єднай сосиски і мисливські ковбаски і запиши як м'ясні вироби\" -> {\"version\": 1, "
    "\"action\": \"inventory_transform\", \"arguments\": {\"source_names\": [\"сосиски\", \"мисливські "
    "ковбаски\"], \"target_name\": \"м'ясні вироби\"}, \"confidence\": 0.97, \"clarification_question\": "
    "null}\n"
    "\"Об'єднай усі записи молока\" -> {\"version\": 1, \"action\": \"inventory_merge_duplicates\", "
    "\"arguments\": {\"product_name\": \"молоко\"}, \"confidence\": 0.97, \"clarification_question\": "
    "null}\n"
    "\"Перейменуй ser на сир\" -> {\"version\": 1, \"action\": \"inventory_rename\", \"arguments\": "
    "{\"old_name\": \"ser\", \"new_name\": \"сир\"}, \"confidence\": 0.97, \"clarification_question\": "
    "null}\n"
    "\"Видали молоко одна штука, воно вже не потрібно\" -> {\"version\": 1, \"action\": "
    "\"inventory_delete\", \"arguments\": {\"item_name\": \"молоко\", \"quantity_hint\": \"одна штука\"}, "
    "\"confidence\": 0.98, \"clarification_question\": null}\n"
    "\"Об'єднай це в одну позицію\" -> {\"version\": 1, \"action\": \"clarify\", \"arguments\": {}, "
    "\"confidence\": 0.6, \"clarification_question\": \"Які саме позиції об'єднати і як назвати "
    "результат?\"}\n"
    "\"Зроби повну інвентаризацію квартири автоматично\" -> {\"version\": 1, \"action\": \"unsupported\", "
    "\"arguments\": {}, \"confidence\": 0.0, \"clarification_question\": null}"
)


def _extract_json(raw):
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    return json.loads(cleaned)


def _clean_name(value, max_len=_MAX_NAME_LENGTH):
    """Whitespace-collapsed, trimmed, length-capped string, or None if
    `value` isn't a non-blank string at all (missing/wrong type/empty after
    trim/too long) — used for every plain product-name field a plan can
    carry. Never accepts a number, a dict, a list, or any other JSON type
    Gemini might return instead of a string."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned or len(cleaned) > max_len:
        return None
    return cleaned


def _validate_arguments(action, raw_arguments):
    """Returns a normalized arguments dict for `action`, or None if anything
    about the shape/content is unsafe. Caller (_validate_plan) has already
    rejected any argument key outside _ALLOWED_ARGUMENT_KEYS[action]."""
    if action == "inventory_transform":
        raw_sources = raw_arguments.get("source_names")
        if not isinstance(raw_sources, list) or not raw_sources:
            return None
        if len(raw_sources) > _MAX_SOURCE_NAMES:
            return None
        sources = []
        for raw_name in raw_sources:
            cleaned = _clean_name(raw_name)
            if cleaned is None:
                return None
            sources.append(cleaned)
        if len(sources) < _MIN_SOURCE_NAMES:
            return None
        target = _clean_name(raw_arguments.get("target_name"))
        if target is None:
            return None
        return {"source_names": sources, "target_name": target}

    if action == "inventory_merge_duplicates":
        product = _clean_name(raw_arguments.get("product_name"))
        if product is None:
            return None
        return {"product_name": product}

    if action == "inventory_rename":
        old_name = _clean_name(raw_arguments.get("old_name"))
        new_name = _clean_name(raw_arguments.get("new_name"))
        if old_name is None or new_name is None:
            return None
        return {"old_name": old_name, "new_name": new_name}

    if action == "inventory_delete":
        item_name = _clean_name(raw_arguments.get("item_name"))
        if item_name is None:
            return None
        raw_hint = raw_arguments.get("quantity_hint")
        quantity_hint = None
        if raw_hint is not None:
            quantity_hint = _clean_name(raw_hint, max_len=_MAX_QUANTITY_HINT_LENGTH)
        return {"item_name": item_name, "quantity_hint": quantity_hint}

    # clarify / unsupported — no arguments at all.
    return {}


def _validate_plan(data):
    """Full V1 JSON-schema validation. Returns a normalized plan dict (same
    shape as _FALLBACK's — version/action/arguments/confidence/
    clarification_question) or None if anything is unsafe/malformed; the
    caller (_ask_gemini) collapses None to _FALLBACK, exactly like every
    other failure mode. `confidence` is never used to skip or relax any
    check here — it is returned purely as metadata."""
    if not isinstance(data, dict):
        return None
    if data.get("version") != 1:
        return None
    action = data.get("action")
    if action not in _ALLOWED_ACTIONS:
        return None

    raw_arguments = data.get("arguments")
    if raw_arguments is None:
        raw_arguments = {}
    if not isinstance(raw_arguments, dict):
        return None
    if set(raw_arguments.keys()) - _ALLOWED_ARGUMENT_KEYS[action]:
        return None

    arguments = _validate_arguments(action, raw_arguments)
    if arguments is None:
        return None

    clarification_question = None
    if action == "clarify":
        raw_question = data.get("clarification_question")
        if not isinstance(raw_question, str) or not raw_question.strip():
            return None
        clarification_question = re.sub(r"\s+", " ", raw_question).strip()[:_MAX_CLARIFICATION_LENGTH]

    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        confidence = None

    return {
        "version": 1, "action": action, "arguments": arguments,
        "confidence": confidence, "clarification_question": clarification_question,
    }


def _ask_gemini(text):
    """ONE Gemini call. Never raises — any failure at any step (no API key,
    network error, timeout, empty response, malformed JSON, wrong top-level
    shape, an invalid/unsafe plan) collapses to the same safe _FALLBACK
    dict."""
    raw = _bot.call_gemini([{"role": "user", "content": text}], ACTION_PLANNER_PROMPT, temperature=0.0)
    if not raw:
        return dict(_FALLBACK)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_FALLBACK)
    plan = _validate_plan(data)
    if plan is None:
        return dict(_FALLBACK)
    return plan


def classify(text):
    """Public entrypoint. Returns a validated plan dict — see _validate_plan
    for its exact shape. Never calls Gemini for blank/non-string input —
    that case is unambiguous and doesn't need a network call to resolve."""
    if not isinstance(text, str) or not text.strip():
        return dict(_FALLBACK)
    return _ask_gemini(text)


# =========================
# PRE-GATE — see this module's own docstring ("Pre-gate") for the full
# reasoning. Pure/local, never calls Gemini.
# =========================
_ARROW_RE = re.compile(r"→|->")
_PLUS_JOIN_RE = re.compile(r"\S\s*\+\s*\S")
_TARGET_CLAUSE_RE = re.compile(
    r"запиши(?:те)?\s+як|назви(?:те)?\s+як|назви(?:те)?\s+це|зроби\s+з\b|"
    r"(?:в|у)\s+одну\s+позиц\w*|перетвор\w*",
    re.IGNORECASE,
)
# Merge/rename/delete VERB roots — deliberately substrings, not whole-word
# regexes, so common Ukrainian inflections ("об'єднай"/"об'єднати"/
# "об'єднайте") all match through a single short root without an exhaustive
# conjugation list, same posture as mini_action_planner._HOUSEHOLD_VOCAB_
# SUBSTRINGS. Both the ASCII (') and typographic (’) apostrophe spellings of
# "об'єднай" are listed since either can appear in real Telegram input.
_MERGE_RENAME_ROOTS = ("об'єдна", "об’єдна", "перейменуй", "виправ назв", "заміни назв", "зміни назв", "дублікат")
_DELETE_ROOTS = ("видали", "прибери", "забери")


def looks_like_inventory_admin_or_transform(text):
    """True if `text` plausibly names an inventory transform/merge-
    duplicates/rename/delete request and is therefore worth one real
    classify() Gemini call; False means the caller should skip straight to
    saved_list_router/general AI-chat without ever calling Gemini here.
    High-recall by design (see module docstring) — any ONE signal (arrow/
    plus notation, an explicit target clause, or a merge/rename/delete verb
    root) is enough. Deliberately does NOT match on a bare quantity/category
    edit ("молока 1 л замість 0,5 л", "перенеси сир у молочне") — those
    carry none of these signals and must keep falling through to
    saved_list_router's own existing quantity/name/category edit-parser
    unaffected."""
    if not isinstance(text, str) or not text.strip():
        return False
    normalized = text.strip().lower()
    if _ARROW_RE.search(normalized) or _PLUS_JOIN_RE.search(normalized) or _TARGET_CLAUSE_RE.search(normalized):
        return True
    return any(root in normalized for root in _MERGE_RENAME_ROOTS + _DELETE_ROOTS)
