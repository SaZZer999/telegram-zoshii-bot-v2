"""Pending Preview Edit Planner — V1 added a single-patch, rename-only
semantic fallback for pending_global_household preview corrections that
deterministic substring matching can't bridge (a genuine Ukrainian case/
wording difference, e.g. pending description "Подарунок для сестри"
[genitive] vs a correction naming "сестрі" [dative] — no substring match at
all, even though a person instantly sees these refer to the same gift).

V2 extends that to a LIST of patches per correction message, and adds two
new operations for expense AMOUNT/context-note corrections (the other live
bug this fixes: "Я згадала, що комод коштував оригінальна ціна 628, а ми
купили його за 528." was rejected outright in V1 — its own prompt
explicitly forbade any operation from ever touching an amount, so a
correctly-identified target could still only ever come back as
"no_change"). V2 also supports combined corrections in ONE message ("Х не
А, а Х Б, і ціна за Y не N, а M") applying multiple patches together.

V3 adds update_inventory_quantity/update_shopping_quantity — the THIRD
live bug this fixes: "Сир Гауда, не 2, а 400 грамів." on a pending
"SER GOUDA — 2 шт." inventory row renamed the item (rename_inventory_item
was the only operation touching inventory rows at all) but silently
dropped the quantity half of the correction, since V1/V2's own prompt
explicitly forbade ANY operation from ever touching a quantity — the same
class of bug update_expense_amount already fixed for expenses, now fixed
for inventory/shopping quantities too. A single message combining a rename
and a quantity correction on the SAME row (the exact live example) now
returns two patches sharing one target_id, applied together.

Still the LAST-RESORT semantic fallback, tried only after every
deterministic handler (quantity/rename parser, text correction, price
clarification) has already failed (see bot.py's
_try_apply_preview_edit_planner, the only caller) — never a replacement
for them, since deterministic parsing is free and instant when it works.

Flow: the CURRENT pending_global_household state (add_shopping_items/
add_inventory_items/new_expenses — never consume_changes/delete_expense,
out of scope) is summarized into a numbered list where every row gets a
STABLE id (exp_1, exp_2, ... / inv_1, ... / shop_1, ...), handed to Gemini
alongside the user's correction text. Gemini returns STRICT JSON:

    {"patches": [ {"operation": ..., "target_id": "exp_1", ...}, ... ]}

naming one or more of nine operations:

    rename_expense_description  -> {"target_id": "exp_N", "new_value": "..."}
    rename_inventory_item       -> {"target_id": "inv_N", "new_value": "..."}
    rename_shopping_item        -> {"target_id": "shop_N", "new_value": "..."}
    update_expense_amount       -> {"target_id": "exp_N", "new_amount": "..."}
    update_expense_context_note -> {"target_id": "exp_N", "new_context_note": "..."}
    update_inventory_quantity   -> {"target_id": "inv_N", "new_quantity": "...", "new_unit": "..."}
    update_shopping_quantity    -> {"target_id": "shop_N", "new_quantity": "...", "new_unit": "..."}
    no_change                   -> (no target recognized in the correction)
    ask_clarification           -> {"question": "..."} (2+ plausible targets,
                                    or Gemini itself can't safely pick one)

Python validates EVERYTHING before ever handing a result back to the
caller, patch by patch: the operation name, that `target_id` names a REAL
row of the CORRESPONDING list in the current pending state (never guessed,
never out of range), that `new_value`/`new_context_note` are non-empty
strings, that `update_expense_amount`'s `new_amount` (a) parses to a
positive Decimal within expenses.EXPENSE_MAX_AMOUNT via expenses.
_parse_expense_amount, the exact same parser/bounds check the household
router itself uses for a freshly-typed expense amount, and (b) appears
LITERALLY as a number token somewhere in the user's own correction text
(see _amount_literally_in_text below — the same anti-hallucination
contract as household_router.py's own _amount_literally_in_text,
reimplemented locally here rather than imported, to keep this module free
of any dependency on household_router's own _bot-configured state), and
that a quantity patch's `new_quantity` is likewise a positive number
LITERALLY present in the user's text (the exact same anti-hallucination
check, reused for quantities too) with `new_unit` one of exactly шт/кг/г/
л/мл (quantities.STRUCTURED_UNITS' own short-form vocabulary — an
unrecognized unit is never trusted, no guessing). Currency is NEVER
accepted from Gemini for update_expense_amount — the existing expense's
currency is always left untouched, closing off that entire vector rather
than validating it.

Safety choice for a message with MULTIPLE patches (rule 6 of the work
order: "either apply only valid patches and show a warning, or ask
clarification without mutating; choose safer/simpler and document it"):
this module chose the SECOND option. If ANY patch in the batch fails
validation, or Gemini's own response mixes an ask_clarification/no_change
entry in with real patches, the ENTIRE batch is discarded — nothing is
ever partially applied — and the caller shows a clarification message
instead. This is strictly simpler than tracking a partial-success/partial-
failure UI state, and strictly safer: a user who asked for two changes in
one message and got only one applied, silently, could easily miss that the
second one never happened; asking again costs one extra message and never
risks a silently incomplete preview.

Any other failure at any step (no Gemini, malformed JSON, unknown
operation anywhere in the batch, an empty/whitespace-only `patches` list)
collapses to {"status": "no_change"}, the same safe fallback as a Gemini
call that genuinely found nothing to change — the caller (bot.py) then
falls through to its own existing guard message, exactly as if this module
had never run.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — `configure(bot_module)` injects bot.py's own `call_gemini` at
runtime, the same DI pattern mini_action_planner.py/household_router.py/
voice_transcript_normalizer.py already use for the exact same reason
(patch.object(bot, "call_gemini", ...) in tests must keep affecting this
module's own Gemini call — a plain injected function reference captured
once at import time would NOT see a later test-time patch of bot.call_
gemini, since patch.object replaces the ATTRIBUTE on the bot module, not
any function object a caller captured earlier; injecting the live module
and always reading _bot.call_gemini fresh at call time sidesteps that).
`expenses` IS imported directly (not injected) for `_parse_expense_amount`/
EXPENSE_MAX_AMOUNT — it's a plain domain module with no Flask/Telegram/
psycopg dependency of its own (household_router.py already imports it the
same way), so this stays a pure, easily-unit-tested function.
"""
import json
import re
from decimal import Decimal, InvalidOperation

import expenses

_bot = None


def configure(bot_module):
    global _bot
    _bot = bot_module


_RENAME_OPERATIONS = {
    "rename_expense_description": "new_expenses",
    "rename_inventory_item": "add_inventory_items",
    "rename_shopping_item": "add_shopping_items",
}

_AMOUNT_OPERATION = "update_expense_amount"
_CONTEXT_NOTE_OPERATION = "update_expense_context_note"
_EXPENSE_ONLY_OPERATIONS = {_AMOUNT_OPERATION, _CONTEXT_NOTE_OPERATION}

_QUANTITY_OPERATIONS = {
    "update_inventory_quantity": "add_inventory_items",
    "update_shopping_quantity": "add_shopping_items",
}
# Same short-form vocabulary Gemini is asked for in the prompt below — kept
# local rather than imported (see module docstring's "no heavy imports"
# policy); an unrecognized unit is never trusted, the whole patch simply
# fails validation rather than guessing. Values map to quantities.
# STRUCTURED_UNITS' own canonical spelling ("шт." WITH the dot — every
# other item in this codebase carries that exact string, never bare "шт").
_VALID_QUANTITY_UNITS = {"шт", "кг", "г", "л", "мл"}
_QUANTITY_UNIT_CANONICAL = {"шт": "шт.", "кг": "кг", "г": "г", "л": "л", "мл": "мл"}

_REAL_OPERATIONS = set(_RENAME_OPERATIONS) | _EXPENSE_ONLY_OPERATIONS | set(_QUANTITY_OPERATIONS)
_ALL_OPERATIONS = _REAL_OPERATIONS | {"no_change", "ask_clarification"}

_NO_CHANGE = {"status": "no_change"}
_GENERIC_CLARIFY_QUESTION = (
    "Не можу безпечно застосувати одну з правок у цьому повідомленні. "
    "Напиши, будь ласка, точніше — по одній зміні за раз, або уточни деталі."
)

_ID_PREFIX_BY_LIST_KEY = {
    "new_expenses": "exp",
    "add_inventory_items": "inv",
    "add_shopping_items": "shop",
}

# Reused verbatim from household_router.py's own _amount_literally_in_text
# (kept local — see module docstring — rather than imported).
_NUMBER_TOKEN_RE = re.compile(r"\d+[.,]?\d*")


def _amount_literally_in_text(amount, source_text):
    """True if `amount` (a Decimal) appears as a literal number token
    somewhere in `source_text` — a genuinely typed new price ("528")
    always passes; a number Gemini invented or silently carried over from
    somewhere else never does. Also used for a quantity patch's
    `new_quantity` — the exact same anti-hallucination contract applies
    equally well to "400" in "не 2, а 400 грамів" as it does to a price."""
    for token in _NUMBER_TOKEN_RE.findall(source_text or ""):
        try:
            token_value = Decimal(token.replace(",", "."))
        except InvalidOperation:
            continue
        if token_value == amount:
            return True
    return False


def _parse_quantity_value(raw_quantity):
    """A quantity patch's `new_quantity` -> a positive Decimal, or None if
    missing/unparseable/non-positive. Deliberately separate from expenses.
    _parse_expense_amount (money-specific bounds/currency-text stripping
    don't belong on a plain quantity number)."""
    if isinstance(raw_quantity, bool):
        return None
    if isinstance(raw_quantity, (int, float)):
        raw_quantity = str(raw_quantity)
    if not isinstance(raw_quantity, str):
        return None
    cleaned = raw_quantity.strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value


PREVIEW_EDIT_PLANNER_PROMPT = (
    "Ти — редактор активного, ще НЕ підтвердженого плану змін (прев'ю) для приватного домашнього "
    "Telegram-бота одного господарства. Користувач щойно написав коротке виправлення до цього плану — "
    "можливо, ОДНЕ виправлення, можливо КІЛЬКА одразу в одному повідомленні. Твоя ЄДИНА задача — визначити, "
    "які САМЕ рядки плану треба виправити, і повернути СТРОГО валідний JSON, без Markdown і без жодного "
    "тексту поза JSON, у форматі:\n"
    "{\"patches\": [ {\"operation\": \"...\", ...}, ... ]}\n\n"
    "Кожен рядок плану має власний стабільний ідентифікатор (exp_1, exp_2, ... для витрат; inv_1, ... для "
    "товарів запасів; shop_1, ... для товарів покупок) — використовуй ЛИШЕ ці id як \"target_id\", ніколи "
    "не вигадуй новий.\n\n"
    "Дозволені дії (operation) в масиві \"patches\":\n"
    "- \"rename_expense_description\" — {\"target_id\": \"exp_N\", \"new_value\": \"новий опис БЕЗ суми\"}\n"
    "- \"rename_inventory_item\" — {\"target_id\": \"inv_N\", \"new_value\": \"нова назва товару\"}\n"
    "- \"rename_shopping_item\" — {\"target_id\": \"shop_N\", \"new_value\": \"нова назва товару\"}\n"
    "- \"update_expense_amount\" — {\"target_id\": \"exp_N\", \"new_amount\": \"нова сума, ЛИШЕ число, як "
    "у тексті користувача, напр. \\\"528\\\"\"} — використовуй ТІЛЬКИ якщо користувач явно назвав нове "
    "число; ніколи нічого не рахуй сам.\n"
    "- \"update_expense_context_note\" — {\"target_id\": \"exp_N\", \"new_context_note\": \"короткий "
    "оновлений коментар у тому ж стилі, що й старий, з новими числами\"} — використовуй РАЗОМ із "
    "update_expense_amount, коли у витрати вже є примітка (наприклад «Оригінальна ціна 627 zł, куплено за "
    "527 zł») і виправлення стосується чисел усередині неї — онови ЛИШЕ числа, стиль формулювання лиши "
    "тим самим.\n"
    "- \"update_inventory_quantity\" — {\"target_id\": \"inv_N\", \"new_quantity\": \"нове число, ЛИШЕ як "
    "у тексті користувача, напр. \\\"400\\\"\", \"new_unit\": \"одне з: шт, кг, г, л, мл\"} — виправити "
    "кількість ОДНОГО товару зі списку «Товари для запасів».\n"
    "- \"update_shopping_quantity\" — те саме, що update_inventory_quantity, але для \"shop_N\" зі списку "
    "«Товари для покупок».\n"
    "- \"no_change\" — жодне з виправлень не стосується жодного рядка з наданих списків. Якщо повертаєш "
    "\"no_change\", це МАЄ бути єдиний елемент у \"patches\".\n"
    "- \"ask_clarification\" — {\"question\": \"одне коротке уточнююче запитання українською\"} — коли "
    "два чи більше рядків підходять під виправлення однаково добре, або незрозуміло, що саме змінити. Якщо "
    "повертаєш \"ask_clarification\", це МАЄ бути єдиний елемент у \"patches\".\n\n"
    "СУВОРІ ПРАВИЛА:\n"
    "1. НІКОЛИ не змінюй дату чи категорію — таких полів немає в жодній дозволеній дії. Суму витрати "
    "змінюй ЛИШЕ через update_expense_amount, а кількість товару ЛИШЕ через update_inventory_quantity/"
    "update_shopping_quantity — ніколи неявно через rename.\n"
    "2. НІКОЛИ не вигадуй target_id, якого немає у наданих списках.\n"
    "3. Українські відмінки того самого слова/імені — це ОДНЕ й те саме (напр. «сестрі» і «для сестри» "
    "стосуються однієї й тієї ж людини; так само «SER GOUDA» і «Сир Гауда» можуть стосуватися ОДНОГО й "
    "того самого товару) — не вважай мовну чи відмінкову різницю причиною для no_change чи ask_clarification, "
    "якщо очевидно йдеться про той самий рядок.\n"
    "4. Для update_expense_amount/update_inventory_quantity/update_shopping_quantity вказуй ЛИШЕ число, яке "
    "користувач явно написав у своєму повідомленні — ніколи не рахуй його сам і ніколи не вигадуй.\n"
    "5. Одне повідомлення користувача може містити кілька незалежних виправлень одразу (наприклад, "
    "перейменування товару РАЗОМ зі зміною його кількості, або перейменування ОДНІЄЇ витрати і зміну суми "
    "ІНШОЇ) — тоді поверни кілька елементів у \"patches\" з ОДНАКОВИМ target_id (якщо це той самий рядок) "
    "чи різними (якщо це різні рядки).\n"
    "6. Якщо однозначно неясно, який САМЕ рядок виправити (кілька підходять однаково, або жоден) — обирай "
    "\"ask_clarification\" чи \"no_change\" відповідно, НІКОЛИ не вгадуй.\n\n"
    "Приклад (одна зміна суми й примітки): Витрати: exp_1. Комод — 527,00 zł; примітка: Оригінальна ціна "
    "627 zł, куплено за 527 zł; виправлення: «Я згадала, що комод коштував оригінальна ціна 628, а ми "
    "купили його за 528.»:\n"
    "{\"patches\": [{\"operation\": \"update_expense_amount\", \"target_id\": \"exp_1\", \"new_amount\": "
    "\"528\"}, {\"operation\": \"update_expense_context_note\", \"target_id\": \"exp_1\", "
    "\"new_context_note\": \"Оригінальна ціна 628 zł, куплено за 528 zł\"}]}\n"
    "Приклад (дві незалежні зміни одразу): Витрати: exp_1. Подарунок для сестри — 60,00 zł; exp_2. Комод — "
    "527,00 zł; виправлення: «Подарунок має бути не сестрі, а дочці, і ціна за комод не 527, а 528.»:\n"
    "{\"patches\": [{\"operation\": \"rename_expense_description\", \"target_id\": \"exp_1\", "
    "\"new_value\": \"Подарунок дочці\"}, {\"operation\": \"update_expense_amount\", \"target_id\": "
    "\"exp_2\", \"new_amount\": \"528\"}]}\n"
    "Приклад (перейменування ТА зміна кількості одного й того самого товару): Товари для запасів: inv_1. "
    "SER GOUDA; виправлення: «Сир Гауда, не 2, а 400 грамів.»:\n"
    "{\"patches\": [{\"operation\": \"rename_inventory_item\", \"target_id\": \"inv_1\", \"new_value\": "
    "\"Сир Гауда\"}, {\"operation\": \"update_inventory_quantity\", \"target_id\": \"inv_1\", "
    "\"new_quantity\": \"400\", \"new_unit\": \"г\"}]}\n"
    "Приклад (нічого підходящого): {\"patches\": [{\"operation\": \"no_change\"}]}\n"
    "Приклад (кілька підходять): {\"patches\": [{\"operation\": \"ask_clarification\", \"question\": "
    "\"У плані кілька витрат з схожою назвою — яку саме виправити?\"}]}"
)


def _build_id_map(pending):
    """{"exp_1": ("new_expenses", 0), "inv_1": ("add_inventory_items", 0), ...}
    — 1-based ids in the id itself, 0-based index in the tuple (ready to use
    directly against the corresponding list)."""
    id_map = {}
    for list_key, prefix in _ID_PREFIX_BY_LIST_KEY.items():
        rows = pending.get(list_key) or []
        for i, _row in enumerate(rows):
            id_map[f"{prefix}_{i + 1}"] = (list_key, i)
    return id_map


def _format_pending_summary(pending):
    """Numbered, Gemini-facing summary of the three renamable/editable lists
    in `pending` (never consume_changes/delete_expense — out of scope).
    Every row is labeled with its stable id (exp_N/inv_N/shop_N) — Gemini
    refers back to a row ONLY by that id, never by re-typing its own guess
    at the current text."""
    lines = []

    shopping = pending.get("add_shopping_items") or []
    if shopping:
        lines.append("Товари для покупок:")
        for i, item in enumerate(shopping):
            qty = item.get("quantity_text")
            suffix = f" — {qty}" if qty else ""
            lines.append(f"shop_{i + 1}. {item.get('name')}{suffix}")

    inventory = pending.get("add_inventory_items") or []
    if inventory:
        lines.append("Товари для запасів:")
        for i, item in enumerate(inventory):
            qty = item.get("quantity_text")
            suffix = f" — {qty}" if qty else ""
            lines.append(f"inv_{i + 1}. {item.get('name')}{suffix}")

    new_expenses = pending.get("new_expenses") or []
    if new_expenses:
        lines.append("Витрати:")
        for i, ne in enumerate(new_expenses):
            line = f"exp_{i + 1}. {ne.get('description')} — {ne.get('amount')} zł"
            lines.append(line)
            note = ne.get("context_note")
            if note:
                lines.append(f"  примітка: {note}")

    return "\n".join(lines) if lines else "(порожньо)"


def _extract_json(raw):
    cleaned = (raw or "").strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    return json.loads(cleaned)


def _validate_real_patch(raw_patch, id_map, user_text):
    """One rename_*/update_expense_* patch, fully validated against the
    CURRENT pending state and (for amounts) the user's own text. Returns a
    normalized dict ready for the caller to apply directly, or None if
    anything about this single patch doesn't check out (the caller then
    discards the ENTIRE batch — see module docstring's safety rationale)."""
    operation = raw_patch.get("operation")
    target_id = raw_patch.get("target_id")
    if not isinstance(target_id, str):
        return None
    entry = id_map.get(target_id)
    if entry is None:
        return None
    list_key, index = entry

    if operation in _RENAME_OPERATIONS:
        if _RENAME_OPERATIONS[operation] != list_key:
            return None
        new_value = raw_patch.get("new_value")
        if not isinstance(new_value, str) or not new_value.strip():
            return None
        return {"operation": operation, "list_key": list_key, "index": index, "new_value": new_value.strip()}

    if operation in _EXPENSE_ONLY_OPERATIONS and list_key != "new_expenses":
        return None

    if operation == _AMOUNT_OPERATION:
        amount = expenses._parse_expense_amount(raw_patch.get("new_amount"))
        if amount is None or not _amount_literally_in_text(amount, user_text):
            return None
        return {"operation": operation, "list_key": list_key, "index": index, "new_amount": amount}

    if operation == _CONTEXT_NOTE_OPERATION:
        new_note = raw_patch.get("new_context_note")
        if not isinstance(new_note, str) or not new_note.strip():
            return None
        return {"operation": operation, "list_key": list_key, "index": index, "new_context_note": new_note.strip()}

    if operation in _QUANTITY_OPERATIONS:
        if _QUANTITY_OPERATIONS[operation] != list_key:
            return None
        new_quantity = _parse_quantity_value(raw_patch.get("new_quantity"))
        if new_quantity is None or not _amount_literally_in_text(new_quantity, user_text):
            return None
        new_unit = raw_patch.get("new_unit")
        if not isinstance(new_unit, str) or new_unit.strip().lower() not in _VALID_QUANTITY_UNITS:
            return None
        return {
            "operation": operation, "list_key": list_key, "index": index,
            "new_quantity": new_quantity, "new_unit": _QUANTITY_UNIT_CANONICAL[new_unit.strip().lower()],
        }

    return None


def plan_preview_edit(pending, user_text):
    """ONE Gemini call, planner-only. Returns one of:
      {"status": "patches", "patches": [ {...}, ... ]}
          — a non-empty list of fully-validated, ready-to-apply patch
          dicts (each has "operation"/"list_key"/"index" plus whatever
          value field its operation needs) — safe to apply directly, in
          order, to `pending`.
      {"status": "ask_clarification", "question": str}
      {"status": "no_change"}

    Never raises. Never touches `pending` itself (the caller applies the
    patches). See the module docstring for the full "discard the whole
    batch on any single failure" safety rationale.
    """
    if not isinstance(user_text, str) or not user_text.strip():
        return dict(_NO_CHANGE)
    if not isinstance(pending, dict):
        return dict(_NO_CHANGE)

    id_map = _build_id_map(pending)
    if not id_map:
        return dict(_NO_CHANGE)

    summary = _format_pending_summary(pending)
    prompt_text = f"{summary}\n\nВиправлення користувача: {user_text.strip()}"
    try:
        raw = _bot.call_gemini([{"role": "user", "content": prompt_text}], PREVIEW_EDIT_PLANNER_PROMPT, temperature=0.0)
    except Exception:
        return dict(_NO_CHANGE)
    if not raw:
        return dict(_NO_CHANGE)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_NO_CHANGE)
    if not isinstance(data, dict):
        return dict(_NO_CHANGE)

    raw_patches = data.get("patches")
    if not isinstance(raw_patches, list) or not raw_patches:
        return dict(_NO_CHANGE)

    real_patches = []
    for raw_patch in raw_patches:
        if not isinstance(raw_patch, dict):
            return dict(_NO_CHANGE)
        operation = raw_patch.get("operation")
        if operation not in _ALL_OPERATIONS:
            return dict(_NO_CHANGE)
        if operation == "ask_clarification":
            question = raw_patch.get("question")
            if not isinstance(question, str) or not question.strip():
                return dict(_NO_CHANGE)
            # Immediate abort — see module docstring: a mixed batch never
            # partially mutates, an explicit ask always wins outright.
            return {"status": "ask_clarification", "question": question.strip()}
        if operation == "no_change":
            continue
        real_patches.append(raw_patch)

    if not real_patches:
        # Every entry was "no_change" (or the list was somehow emptied
        # above without an early return) — nothing to do.
        return dict(_NO_CHANGE)

    validated = []
    for raw_patch in real_patches:
        result = _validate_real_patch(raw_patch, id_map, user_text.strip())
        if result is None:
            return {"status": "ask_clarification", "question": _GENERIC_CLARIFY_QUESTION}
        validated.append(result)

    return {"status": "patches", "patches": validated}
