"""Shopping Action Planner V1 — a single, narrow Gemini classifier for
GLOBAL natural-language shopping-list admin phrasing (delete an item / mark
an item bought) that works even when neither `shopping_mode` nor a saved
shopping-list context (`saved_list_context == "shopping_saved"`) is active
— see message_dispatcher.py's CommandRouteDeps.shopping_action_planner_route:
checked right after `global_expense_command`, right before the three
deterministic inventory gates and `action_planner.py`.

NOT `action_planner.py` (Inventory Action Planner V1 — inventory_transform/
inventory_merge_duplicates/inventory_rename/inventory_delete/clarify/
unsupported, a completely different domain, its own dispatcher slot, its own
module) and NOT `mini_action_planner.py` (Unified Mini Action Planner V1 —
add_to_shopping/add_to_inventory/ask_inventory/meal_ideas/unknown, Phase D
last-resort). This module owns a narrower, shopping-only, two-action
vocabulary (plus clarify/unsupported), sits in a different part of the
dispatch chain (command routes, not Phase D, not after the inventory gates),
and is never invoked for the same message either of those would also claim
— bot.py's `_try_shopping_action_planner` always returns True once its own
pre-gate matches (even for a "clarify"/"unsupported" outcome), so a message
it claims never falls through to any inventory route, action_planner.py,
mini_action_planner.py, or general AI-chat.

    shopping_delete      -> bot.py's own thin wrapper around
                             legacy_shopping_flow._show_delete_preview (SAME
                             pending_delete_batch/delete_items_batch path
                             the shopping-mode "deleting" flow and the
                             saved-list router's own start_action/
                             delete_shopping intent already use).
    shopping_mark_bought -> same, around legacy_shopping_flow._show_mark_
                             preview (SAME pending_mark_batch/
                             mark_items_batch path shopping-mode "marking"
                             and saved-list start_action/mark_bought
                             already use).
    clarify               -> a single controlled clarification message,
                             never a pending state, never a preview.
    unsupported            -> a single controlled "couldn't safely
                             understand this" message — never general
                             AI-chat, never a guess.

Gemini is asked for STRICT JSON only (one system prompt, one call, no
conversation history) and every field is re-validated in Python before
ANYTHING downstream ever sees it (see _validate_plan): an unrecognized
action, a wrong version, malformed JSON, a Gemini call failure, an empty
response, a disallowed extra argument key (a DB id, SQL, code, an
executor/function name, ...) all safely collapse to the same
{"action": "unsupported", ...} result — never guessed, never partially
trusted. Gemini never sees and never returns database ids, and never
decides which shopping row actually matches or how many candidates exist —
it only extracts a plain product-name string; bot.py's own
_try_shopping_action_planner resolves that name against a FRESH live
shopping snapshot via resolve_shopping_candidates (below, reusing
preview_editing._name_token_matches — the SAME declension-tolerant
free-text-vs-item matcher the household add-preview edit flow already uses,
never a second, parallel matcher), and never writes to the database before
an explicit confirm.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — configure(bot_module) injects bot.py's own call_gemini at runtime,
same DI pattern household_router.py/mini_action_planner.py/action_planner.py
already use for the exact same reason (patch.object(bot, "call_gemini", ...)
in tests must keep affecting this module's own Gemini call). `preview_
editing` is safe to import directly (it never imports bot.py either) for
its shared name-matching helper.

Pre-gate: looks_like_global_shopping_admin(text), a cheap, deterministic,
no-Gemini check bot.py's _try_shopping_action_planner runs BEFORE ever
calling classify() — see that function's own docstring for the full
reasoning, same "opt-in, high-recall, never exhaustive" posture action_
planner.looks_like_inventory_admin_or_transform/mini_action_planner.
looks_household_like already established for their own slots.
"""
import json
import re

import preview_editing

_bot = None


def configure(bot_module):
    global _bot
    _bot = bot_module


_ALLOWED_ACTIONS = {"shopping_delete", "shopping_mark_bought", "clarify", "unsupported"}

_ALLOWED_ARGUMENT_KEYS = {
    "shopping_delete": {"item_name"},
    "shopping_mark_bought": {"item_name"},
    "clarify": set(),
    "unsupported": set(),
}

_MAX_NAME_LENGTH = 200
_MAX_CLARIFICATION_LENGTH = 500

_FALLBACK = {
    "version": 1, "action": "unsupported", "arguments": {}, "clarification_question": None,
}

UNSUPPORTED_MSG = (
    "Не зрозумів, яку позицію зі списку покупок змінити. "
    "Напиши, наприклад: «Викресли молоко зі списку»."
)

NOT_FOUND_MSG = "Не знайшов такої позиції у списку покупок."

SHOPPING_ACTION_PLANNER_PROMPT = (
    "Ти — розпізнавач наміру для приватного домашнього Telegram-бота одного господарства. Користувач "
    "написав повідомлення про СПИСОК ПОКУПОК (shopping list), яке звичайні прості правила бота не змогли "
    "розпізнати. Твоя ЄДИНА задача — визначити, яку з чотирьох дій хоче користувач, і повернути СТРОГО "
    "валідний JSON, без Markdown і без жодного тексту поза JSON.\n\n"
    "Дії (action) — рівно одна з:\n"
    "- \"shopping_delete\" — користувач хоче прибрати/викреслити ОДНУ позицію зі списку покупок, "
    "НЕ тому що вже купив її, а тому що вона більше не потрібна (напр. «Викресли молоко зі списку», "
    "«Прибери хліб зі списку покупок», «Каву більше не треба купувати»).\n"
    "- \"shopping_mark_bought\" — користувач повідомляє, що ОДНУ позицію зі списку покупок вже купив/"
    "принесли додому, і її треба прибрати зі списку як куплену (напр. «Молоко вже купили», «Сир уже взяли, "
    "забери його зі списку»).\n"
    "- \"clarify\" — видно, що це якась дія зі списком покупок (видалити чи позначити купленим), але "
    "незрозуміло, яку саме позицію, або незрозуміло, яку з двох дій виконати. Постав ОДНЕ коротке "
    "уточнювальне запитання українською в clarification_question.\n"
    "- \"unsupported\" — повідомлення НЕ є дією зі списком покупок (додавання нового товару до покупок, "
    "дія із запасами, витрата, звичайна розмова) — ти НІКОЛИ не створюєш нову дію з іншого домену.\n\n"
    "Для shopping_delete/shopping_mark_bought заповни arguments.item_name — сама назва товару, як написав "
    "користувач, БЕЗ слів про кількість чи тару, у називному відмінку.\n"
    "ВАЖЛИВО:\n"
    "- Ти НІКОЛИ не повертаєш ID записів бази даних, SQL, код чи назви функцій — лише звичайну текстову "
    "назву товару.\n"
    "- Ти НІКОЛИ не вирішуєш, яка позиція РЕАЛЬНО є у списку покупок — Python окремо звірить назву з "
    "актуальним станом списку.\n"
    "- Ти НІКОЛИ сам не підтверджуєш дію і не пишеш у базу даних.\n"
    "- Якщо не впевнений — обирай \"clarify\" або \"unsupported\", ніколи не вгадуй.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON:\n"
    "{\"version\": 1, \"action\": \"shopping_delete\", \"arguments\": {\"item_name\": \"молоко\"}, "
    "\"clarification_question\": null}\n\n"
    "Приклади:\n"
    "\"Викресли молоко зі списку\" -> {\"version\": 1, \"action\": \"shopping_delete\", \"arguments\": "
    "{\"item_name\": \"молоко\"}, \"clarification_question\": null}\n"
    "\"Прибери хліб зі списку покупок\" -> {\"version\": 1, \"action\": \"shopping_delete\", \"arguments\": "
    "{\"item_name\": \"хліб\"}, \"clarification_question\": null}\n"
    "\"Каву більше не треба купувати\" -> {\"version\": 1, \"action\": \"shopping_delete\", \"arguments\": "
    "{\"item_name\": \"кава\"}, \"clarification_question\": null}\n"
    "\"Молоко вже купили\" -> {\"version\": 1, \"action\": \"shopping_mark_bought\", \"arguments\": "
    "{\"item_name\": \"молоко\"}, \"clarification_question\": null}\n"
    "\"Сир уже взяли, забери його зі списку\" -> {\"version\": 1, \"action\": \"shopping_mark_bought\", "
    "\"arguments\": {\"item_name\": \"сир\"}, \"clarification_question\": null}\n"
    "\"Прибери це зі списку\" (немає назви товару) -> {\"version\": 1, \"action\": \"clarify\", "
    "\"arguments\": {}, \"clarification_question\": \"Яку саме позицію зі списку покупок ти маєш на "
    "увазі?\"}\n"
    "\"Додай молоко до покупок\" -> {\"version\": 1, \"action\": \"unsupported\", \"arguments\": {}, "
    "\"clarification_question\": null}"
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
    trim/too long). Never accepts a number, a dict, a list, or any other
    JSON type Gemini might return instead of a string."""
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
    if action in ("shopping_delete", "shopping_mark_bought"):
        item_name = _clean_name(raw_arguments.get("item_name"))
        if item_name is None:
            return None
        return {"item_name": item_name}
    # clarify / unsupported — no arguments at all.
    return {}


def _validate_plan(data):
    """Full V1 JSON-schema validation. Returns a normalized plan dict
    (version/action/arguments/clarification_question) or None if anything is
    unsafe/malformed; the caller (_ask_gemini) collapses None to _FALLBACK,
    exactly like every other failure mode."""
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

    return {
        "version": 1, "action": action, "arguments": arguments,
        "clarification_question": clarification_question,
    }


def _ask_gemini(text):
    """ONE Gemini call. Never raises — any failure at any step (no API key,
    network error, timeout, empty response, malformed JSON, wrong top-level
    shape, an invalid/unsafe plan) collapses to the same safe _FALLBACK
    dict."""
    raw = _bot.call_gemini([{"role": "user", "content": text}], SHOPPING_ACTION_PLANNER_PROMPT, temperature=0.0)
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

# Group A: verbs that remove something from a list/collection — deliberately
# ONLY combined with Group B (an actual "список"/"списку" reference) below,
# never sufficient alone: "видали"/"прибери"/"забери" are also the trigger
# verbs for inventory-delete and expense-delete, so requiring the list
# reference too is what keeps "Прибери молоко із запасів" (inventory) and
# "Видали витрату за молоко" (expense) from ever matching here.
_LIST_REMOVAL_VERB_ROOTS = ("викресл", "прибери", "прибрати", "видали", "видалити", "забери", "забрати")
_SHOPPING_LIST_REFERENCE_ROOTS = ("спис",)

# Group C: "already bought/taken" — an adverb ("вже"/"уже") PAIRED with a
# bought/taken verb root. Requiring the adverb too is what keeps a plain
# "Купив молоко за 10 zł" (a NEW purchase, handled by the Global Household
# Router) from matching just because it contains "купи".
_ALREADY_ADVERB_ROOTS = ("вже", "уже")
_BOUGHT_OR_TAKEN_VERB_ROOTS = ("купи", "взял", "взяв")

# Group D: "no longer need to buy" — a negated need-phrase PAIRED with the
# "buy" verb root, e.g. "більше не треба купувати"/"вже не потрібно купувати".
_NO_LONGER_NEED_RE = re.compile(r"не\s+(?:треба|потрібно)", re.IGNORECASE)
_BUY_VERB_ROOT = "купува"


def looks_like_global_shopping_admin(text):
    """True if `text` plausibly names a shopping-list delete/mark-bought
    request and is therefore worth one real classify() Gemini call; False
    means the caller should skip straight to the next route (inventory
    gates, saved_list_router, general AI-chat, ...) without ever calling
    Gemini here. High-recall by design (see module docstring) — any ONE of
    the three compositional signal groups below is enough; a false positive
    only costs one harmless extra classify() call that safely resolves to
    "unsupported"."""
    if not isinstance(text, str) or not text.strip():
        return False
    normalized = text.strip().lower()

    has_removal_verb = any(root in normalized for root in _LIST_REMOVAL_VERB_ROOTS)
    has_list_reference = any(root in normalized for root in _SHOPPING_LIST_REFERENCE_ROOTS)
    if has_removal_verb and has_list_reference:
        return True

    has_already = any(root in normalized for root in _ALREADY_ADVERB_ROOTS)
    has_bought_taken_verb = any(root in normalized for root in _BOUGHT_OR_TAKEN_VERB_ROOTS)
    if has_already and has_bought_taken_verb:
        return True

    if _NO_LONGER_NEED_RE.search(normalized) and _BUY_VERB_ROOT in normalized:
        return True

    return False


# =========================
# CANDIDATE RESOLUTION — pure, no Gemini, no DB. `items` is a live
# get_active_shopping_items() snapshot (raw dicts with "id"/"name"/
# "canonical_name"/"quantity_text"/...); reuses preview_editing._name_
# token_matches (the SAME declension-tolerant free-text-vs-item matcher the
# household add-preview edit flow already uses) instead of a second,
# parallel matcher.
# =========================

def resolve_shopping_candidates(item_name, items):
    """Every active shopping item whose name/canonical_name plausibly
    matches `item_name`, sorted by id (same ordering convention as
    inventory.py's own candidate-search helpers). Never fuzzy beyond what
    preview_editing._name_token_matches already does."""
    matches = [item for item in items if preview_editing._name_token_matches(item_name, item)]
    matches.sort(key=lambda it: it["id"])
    return matches


def format_shopping_admin_ambiguous_message(candidates):
    """Multiple rows matched the same shopping delete/mark-bought request —
    never guess; list every candidate (numbered, same style as inventory.
    format_inventory_admin_ambiguous_message) and ask for a more precise
    reference. No dedicated disambiguation pending-state — the next message
    just re-enters this same planner, exactly like the "clarify" action
    already works structurally."""
    lines = ["Знайшов кілька позицій у списку покупок, не хочу вгадувати:", ""]
    for i, item in enumerate(candidates, start=1):
        label = item["name"]
        qty = item.get("quantity_text")
        if qty:
            label += f" — {qty}"
        lines.append(f"{i}. {label}")
    lines.append("")
    lines.append("Напиши точніше, яку саме позицію ти маєш на увазі.")
    return "\n".join(lines)
