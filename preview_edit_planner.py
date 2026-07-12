"""Pending Preview Edit Planner V1.

Deterministic preview-edit handlers (preview_editing.py's quantity/rename
parser, text correction, price clarification) are cheap and safe, but they
match by exact/near-exact substring — they fail on genuine Ukrainian case/
wording differences a human reader would consider "obviously the same
thing" (e.g. the live bug this fixes: pending description "Подарунок для
сестри" [genitive] vs a correction naming "сестрі" [dative] — no substring
match at all, even though a person instantly sees these refer to the same
gift). This module is the LAST-RESORT semantic fallback, tried only after
every deterministic handler has already failed (see bot.py's
_try_apply_preview_edit_planner, the only caller) — never a replacement for
them, since deterministic parsing is free and instant when it works.

Flow: the CURRENT pending_global_household state (add_shopping_items/
add_inventory_items/new_expenses — never consume_changes/delete_expense,
out of scope V1) is summarized into numbered lists, handed to Gemini
alongside the user's correction text, and Gemini returns STRICT JSON
naming ONE of five operations:

    rename_expense_description  -> {"index": i, "new_value": "..."}
    rename_inventory_item       -> {"index": i, "new_value": "..."}
    rename_shopping_item        -> {"index": i, "new_value": "..."}
    no_change                   -> (no target recognized in the correction)
    ask_clarification           -> {"question": "..."} (2+ plausible targets)

Python then validates EVERYTHING before ever handing a result back to the
caller: the operation name itself, that `index` is a real position in the
CORRESPONDING list of the pending state (never guessed, never out of
range), and that `new_value`/`question` are non-empty strings. Gemini is
never asked for (and the schema has no field for) an amount/quantity/unit/
date/category/operation-type change — those simply don't exist in this
patch vocabulary, so there's nothing for Python to "reject": the rename
patch this module returns can only ever touch a name/description string.
Any failure at any step (no Gemini, malformed JSON, unknown operation,
out-of-range index, empty value) collapses to {"operation": "no_change"},
the same safe fallback as a Gemini call that genuinely found nothing to
rename — the caller (bot.py) then falls through to its own existing guard
message, exactly as if this module had never run.

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
"""
import json
import re

_bot = None


def configure(bot_module):
    global _bot
    _bot = bot_module


_ALLOWED_OPERATIONS = {
    "rename_expense_description", "rename_inventory_item", "rename_shopping_item",
    "no_change", "ask_clarification",
}

_RENAME_OPERATION_TO_LIST_KEY = {
    "rename_expense_description": "new_expenses",
    "rename_inventory_item": "add_inventory_items",
    "rename_shopping_item": "add_shopping_items",
}

_FALLBACK = {"operation": "no_change"}

PREVIEW_EDIT_PLANNER_PROMPT = (
    "Ти — редактор активного, ще НЕ підтвердженого плану змін (прев'ю) для приватного домашнього "
    "Telegram-бота одного господарства. Користувач щойно написав коротке виправлення до цього плану. Твоя "
    "ЄДИНА задача — визначити, який САМЕ рядок плану треба перейменувати (назву товару чи опис витрати), і "
    "повернути СТРОГО валідний JSON, без Markdown і без жодного тексту поза JSON.\n\n"
    "Дозволені дії (operation) — рівно одна з:\n"
    "- \"rename_expense_description\" — виправити опис ОДНІЄЇ витрати зі списку «Витрати». Поля: index "
    "(номер витрати зі списку, рахуючи з 0), new_value (новий короткий опис, БЕЗ суми).\n"
    "- \"rename_inventory_item\" — виправити назву ОДНОГО товару зі списку «Товари для запасів». Поля: "
    "index (з 0), new_value (нова назва товару).\n"
    "- \"rename_shopping_item\" — виправити назву ОДНОГО товару зі списку «Товари для покупок». Поля: "
    "index (з 0), new_value (нова назва товару).\n"
    "- \"no_change\" — виправлення не стосується жодного рядка з наданих списків, або незрозуміло, що "
    "саме змінити.\n"
    "- \"ask_clarification\" — ДВА чи більше рядків підходять під виправлення однаково добре (напр. "
    "кілька витрат чи товарів зі схожим ключовим словом) — Поле: question (одне коротке уточнююче "
    "запитання українською).\n\n"
    "СУВОРІ ПРАВИЛА:\n"
    "1. НІКОЛИ не змінюй суму, кількість, одиницю, дату чи категорію — таких полів немає в жодній "
    "дозволеній дії; ти працюєш ЛИШЕ з назвою/описом.\n"
    "2. НІКОЛИ не вигадуй рядок, якого немає у наданих списках — лише перейменовуй ІСНУЮЧИЙ рядок за його "
    "номером (index).\n"
    "3. Українські відмінки того самого слова/імені — це ОДНЕ й те саме (напр. «сестрі» і «для сестри» "
    "стосуються однієї й тієї ж людини) — не вважай відмінкову різницю причиною для no_change чи "
    "ask_clarification, якщо очевидно йдеться про той самий рядок.\n"
    "4. Якщо однозначно неясно, який САМЕ рядок виправити (кілька підходять однаково, або жоден) — обирай "
    "\"ask_clarification\" чи \"no_change\" відповідно, НІКОЛИ не вгадуй.\n\n"
    "Приклад (списки: Витрати: 0. Подарунок для сестри — 60,00 zł; виправлення: «Там має бути подарунок "
    "не сестрі, а подарунок дочці.»): "
    "{\"operation\": \"rename_expense_description\", \"index\": 0, \"new_value\": \"Подарунок дочці\"}\n"
    "Приклад (нічого підходящого): {\"operation\": \"no_change\"}\n"
    "Приклад (кілька підходять): {\"operation\": \"ask_clarification\", \"question\": \"У плані кілька "
    "витрат з схожою назвою — яку саме виправити?\"}"
)


def _format_pending_summary(pending):
    """Numbered, Gemini-facing summary of the three renamable lists in
    `pending` (never consume_changes/delete_expense — out of scope V1).
    Purely descriptive text; Gemini refers back to a row ONLY by its
    0-based number, never by re-typing its own guess at the current text."""
    lines = []

    shopping = pending.get("add_shopping_items") or []
    if shopping:
        lines.append("Товари для покупок (add_shopping_items):")
        for i, item in enumerate(shopping):
            lines.append(f"{i}. {item.get('name')}")

    inventory = pending.get("add_inventory_items") or []
    if inventory:
        lines.append("Товари для запасів (add_inventory_items):")
        for i, item in enumerate(inventory):
            lines.append(f"{i}. {item.get('name')}")

    new_expenses = pending.get("new_expenses") or []
    if new_expenses:
        lines.append("Витрати (new_expenses):")
        for i, ne in enumerate(new_expenses):
            lines.append(f"{i}. {ne.get('description')} — {ne.get('amount')} zł")

    return "\n".join(lines) if lines else "(порожньо)"


def _extract_json(raw):
    cleaned = (raw or "").strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    return json.loads(cleaned)


def plan_preview_edit(pending, user_text):
    """ONE Gemini call, planner-only. Returns one of:
      {"operation": "rename_expense_description"|"rename_inventory_item"|
       "rename_shopping_item", "index": int, "new_value": str}
          — `index` is already bounds-checked against the CORRESPONDING
          list in `pending` (new_expenses/add_inventory_items/
          add_shopping_items respectively) — safe to apply directly.
      {"operation": "ask_clarification", "question": str}
      {"operation": "no_change"}

    Never raises. Never touches the database or `pending` itself (the
    caller applies the patch). Any failure — a non-dict/blank `pending` or
    `user_text`, no Gemini response, malformed JSON, an unrecognized
    operation, a missing/wrong-type index or new_value/question, or an
    index outside the target list's actual bounds — collapses to
    {"operation": "no_change"}, never guessed, never partially trusted.
    """
    if not isinstance(user_text, str) or not user_text.strip():
        return dict(_FALLBACK)
    if not isinstance(pending, dict):
        return dict(_FALLBACK)

    summary = _format_pending_summary(pending)
    prompt_text = f"{summary}\n\nВиправлення користувача: {user_text.strip()}"
    try:
        raw = _bot.call_gemini([{"role": "user", "content": prompt_text}], PREVIEW_EDIT_PLANNER_PROMPT, temperature=0.0)
    except Exception:
        return dict(_FALLBACK)
    if not raw:
        return dict(_FALLBACK)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_FALLBACK)
    if not isinstance(data, dict):
        return dict(_FALLBACK)

    operation = data.get("operation")
    if operation not in _ALLOWED_OPERATIONS:
        return dict(_FALLBACK)

    if operation == "no_change":
        return {"operation": "no_change"}

    if operation == "ask_clarification":
        question = data.get("question")
        if not isinstance(question, str) or not question.strip():
            return dict(_FALLBACK)
        return {"operation": "ask_clarification", "question": question.strip()}

    # One of the three rename_* operations.
    target_list = pending.get(_RENAME_OPERATION_TO_LIST_KEY[operation])
    if not isinstance(target_list, list) or not target_list:
        return dict(_FALLBACK)
    index = data.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(target_list):
        return dict(_FALLBACK)
    new_value = data.get("new_value")
    if not isinstance(new_value, str) or not new_value.strip():
        return dict(_FALLBACK)

    return {"operation": operation, "index": index, "new_value": new_value.strip()}
