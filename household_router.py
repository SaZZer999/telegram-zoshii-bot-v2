"""Global Household Router v1: one Gemini call that recognizes up to five
kinds of household operations in a single plain-text message — add_shopping,
add_inventory, consume_inventory, add_expense, delete_expense — regardless
of which menu (main/shopping/inventory/expenses/aliases) is currently open,
and builds one combined preview covering all of them.

This module must never import bot.py (that would create an import cycle,
since bot.py imports this module). Wherever this module needs a piece of
"bot infrastructure" — call_gemini, get_warsaw_datetime_context,
normalize_item_quantity, STRUCTURED_UNITS, _resolve_consumption,
VALID_CATEGORIES, DEFAULT_CATEGORY, _auto_merge_in_place,
_validate_selected_numbers, _effective_quantity, format_quantity_display,
the keyboards — it goes through the live `_bot` module reference handed in
via configure(), never a snapshotted import (mirrors expenses.py's own
`_bot` indirection, for the same reason).

`expenses.py` is imported directly (not through `_bot`): it never imports
bot.py or this module, so there is no cycle, and its amount/date/category
validators and formatters are reused as-is for the add_expense/delete_expense
side of every operation instead of being duplicated a second time.

Owns no Telegram/pending state of its own — bot.py stores
pending_global_household and performs the actual DB write via
database.apply_global_household_operations; this module only classifies
text into normalized operations and builds preview/clarification text, as a
set of pure functions plus the one Gemini call.
"""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import expenses

# =========================
# INJECTED DEPENDENCIES (see configure())
# =========================
_bot = None
active_list_context = None
saved_list_context = None
_KEYBOARDS = None  # {"main", "shopping", "inventory", "expenses", "aliases"}


def configure(bot_module, active_list_context_dict, saved_list_context_dict, keyboards):
    """Wire in bot.py's shared dependencies once, right after both modules
    finish importing. See the module docstring for why this indirection
    exists instead of a plain `import bot`.
    """
    global _bot, active_list_context, saved_list_context, _KEYBOARDS
    _bot = bot_module
    active_list_context = active_list_context_dict
    saved_list_context = saved_list_context_dict
    _KEYBOARDS = keyboards


# =========================
# ORIGIN HELPERS (mirrors expenses.py's _current_expense_origin/_expense_origin_keyboard,
# extended to cover every menu this router can fire from)
# =========================
def current_origin(chat_id):
    """Where a household command was issued from — which keyboard/menu to
    return to after confirm/cancel. Checked in the same order as
    bot._current_alias_origin: dedicated submenu first, then an open saved
    shopping/inventory list, otherwise the main menu ("global")."""
    if active_list_context.get(chat_id) == "aliases":
        return "aliases_menu"
    if active_list_context.get(chat_id) == "expenses":
        return "expenses_menu"
    ctx = saved_list_context.get(chat_id)
    if ctx in ("shopping_saved", "inventory_saved"):
        return ctx
    return "global"


def origin_keyboard(origin):
    """The correct persistent keyboard to explicitly (re-)send for a given
    household-command origin — ALWAYS a concrete keyboard, never None."""
    if origin == "aliases_menu":
        return _KEYBOARDS["aliases"]
    if origin == "expenses_menu":
        return _KEYBOARDS["expenses"]
    if origin == "shopping_saved":
        return _KEYBOARDS["shopping"]
    if origin == "inventory_saved":
        return _KEYBOARDS["inventory"]
    return _KEYBOARDS["main"]


# =========================
# LOCAL GATE (pure, no Gemini) — narrow phrasing the existing per-domain
# gates don't already own. Deliberately excludes a bare zł-amount (the
# existing expense-add gate's job) and an imperative delete ("видали"/
# "скасуй" + "витрат", the existing expense-delete gate's job) so this
# router never re-attempts a message the legacy gates already handle
# correctly today.
# =========================
_BUY_PLAN_RE = re.compile(r"планую купити|хочу купити|треба купити|потрібно купити", re.IGNORECASE)
_BOUGHT_RE = re.compile(r"купив|купила|купили|придбав|придбала", re.IGNORECASE)
_CONSUME_RE = re.compile(
    r"з[’']?їв|з[’']?їла|з[’']?їли|використав|використала|використали|"
    r"спожив|спожила|спожили|доїв|доїла",
    re.IGNORECASE,
)
_MISTAKE_EXPENSE_RE = re.compile(
    r"(випадков\w*|помилков\w*).{0,40}витрат\w*|витрат\w*.{0,40}(випадков\w*|помилков\w*)",
    re.IGNORECASE,
)


def gate(text):
    """True if `text` contains phrasing this router is meant to handle —
    checked before any Gemini call, from anywhere, regardless of which menu
    is open. Narrow on purpose: a plain zł-tagged amount or an imperative
    "видали/скасуй витрату" never matches here (those stay on the existing
    narrow expense gates), so this router only ever gets a first look at
    messages the legacy gates don't already own.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    return bool(
        _BUY_PLAN_RE.search(stripped)
        or _BOUGHT_RE.search(stripped)
        or _CONSUME_RE.search(stripped)
        or _MISTAKE_EXPENSE_RE.search(stripped)
    )


# =========================
# GEMINI PROMPT
# =========================
_ALLOWED_OP_TYPES = {"add_shopping", "add_inventory", "consume_inventory", "add_expense", "delete_expense"}

HOUSEHOLD_ROUTER_PROMPT = (
    "Ти помічник одного домашнього господарства. Користувач пише одне повідомлення про побутові справи: "
    "покупки, запаси або витрати. Тобі надається поточна локальна дата й час Europe/Warsaw, нумерований "
    "список активних покупок, нумерований список запасів і нумерований список останніх витрат.\n\n"
    "Визнач намір (intent):\n"
    "- «household_operations» — повідомлення описує одну або кілька побутових дій із переліку нижче\n"
    "- «none» — повідомлення не описує жодної з цих дій (сюди віднось і всі дії, яких немає в переліку: "
    "позначити покупку купленою, видалити товар зі списку покупок чи запасів, відредагувати товар, "
    "домашні назви (aliases), рецепти, чеки/фото, довільні звіти — для всього цього завжди повертай «none»)\n\n"
    "Дозволені типи операцій (поле operations — масив, може містити кілька елементів одного повідомлення):\n"
    "1. «add_shopping» — новий товар, який людина ЩЕ ТІЛЬКИ планує купити (напр. «Планую купити булочку», "
    "«Треба купити молоко»). Поля: name, quantity_text (як у тексті, або порожній рядок якщо кількість не "
    "вказана), category — одна з фіксованих категорій нижче.\n"
    "2. «add_inventory» — товар, який людина ВЖЕ купила і він зараз є вдома (напр. «Купив масло», «Купила "
    "хліб»), БЕЗ суми. Ті самі поля, що й add_shopping.\n"
    "Правила відокремлення кількості від назви (для add_shopping/add_inventory): name — ЛИШЕ сама назва "
    "товару, БЕЗ жодних слів про кількість; quantity_text — уся кількість, як у тексті. Якщо кількість — "
    "просто число без одиниці («3 банани») — quantity_text рівно «3», name — «Банани» (без числа). Якщо "
    "кількість — слово «пара»/«пару» («пару сосисок») — quantity_text рівно «пара» чи «пару» (як у тексті), "
    "name — «Сосиски» (без слова «пара»). Якщо кількість описана через тару («дві пачки сосисок», «упаковка "
    "яєць») — усю фразу кількості («дві пачки») клади в quantity_text, а name — лише сам товар («Сосиски»); "
    "НІКОЛИ не залишай слова «пачка»/«упаковка»/числівники всередині name. Якщо безпечно відокремити "
    "кількість від назви не вдається — постав quantity_text порожнім рядком і опиши весь фрагмент у "
    "unresolved_fragments, а не вигадуй назву.\n"
    "Приклад (для «Купив пару сосисок»): {\"type\": \"add_inventory\", \"name\": \"Сосиски\", "
    "\"quantity_text\": \"пару\", \"category\": \"М'ясо та риба\"}\n"
    "Приклад (для «Купив дві пачки сосисок»): {\"type\": \"add_inventory\", \"name\": \"Сосиски\", "
    "\"quantity_text\": \"дві пачки\", \"category\": \"М'ясо та риба\"}\n"
    "3. Якщо повідомлення означає «купив X ЗА Y zł» — це ОДНА покупка з ціною: додай ОБИДВІ операції — "
    "add_inventory (сам товар) І add_expense (сума) в одному масиві operations.\n"
    "4. «consume_inventory» — людина з'їла/використала частину запасів (напр. «З'їв 2 ковбаски», "
    "«Використала 200 г масла»). Повертай це ЛИШЕ коли кількість чітко вказана числом і одиницею. Якщо "
    "кількість неясна («з'їв трохи молока») — не вигадуй operations для цього фрагмента, а опиши фрагмент у "
    "unresolved_fragments. Поля: item_number — номер ІСНУЮЧОЇ позиції з наданого списку запасів (обов'язково "
    "має існувати в списку, інакше не повертай цю операцію), quantity_value (число), quantity_unit (одна з: "
    "шт., л, мл, г, кг).\n"
    "5. «add_expense» — нова витрата із сумою в злотих. Поля: amount (рядок з крапкою або комою, ніколи не "
    "округлюй і не вигадуй суму, якої немає в тексті), currency (завжди «PLN»), category — одна з фіксованих "
    "категорій витрат нижче (якщо не впевнений — «Інше»), description (короткий опис без суми й категорії "
    "всередині), expense_date (YYYY-MM-DD; якщо дата не вказана явно — сьогоднішня дата з наданого "
    "контексту; ніколи не в майбутньому). Максимум одна операція add_expense на повідомлення.\n"
    "6. «delete_expense» — людина каже, що ВИПАДКОВО чи ПОМИЛКОВО додала витрату і хоче її прибрати (напр. "
    "«Булочку до витрат я додав випадково», «Помилково записав ту витрату»). Повертай це ЛИШЕ якщо рівно "
    "ОДНА позиція з наданого списку останніх витрат явно відповідає опису; якщо жодна або кілька можуть "
    "підходити — не повертай цю операцію, опиши фрагмент у unresolved_fragments замість того, щоб вгадувати. "
    "Поле: selected_numbers — масив з РІВНО одним номером зі списку останніх витрат. Максимум одна операція "
    "delete_expense на повідомлення.\n\n"
    "Категорії товарів (для add_shopping/add_inventory): М'ясо та риба, Молочне та яйця, Овочі та зелень, "
    "Фрукти та ягоди, Хліб і випічка, Крупи, макарони та борошно, Соуси, спеції та бакалія, Солодке та "
    "снеки, Напої, Заморожене, Інше їстівне.\n"
    "Категорії витрат (для add_expense): Продукти, Дім і рахунки, Транспорт, Здоров'я, Кафе / ресторани, "
    "Побут, Дитина, Інше.\n\n"
    "Якщо частину повідомлення не можна безпечно перетворити на жодну з цих операцій — додай короткий опис "
    "цього фрагмента в unresolved_fragments (масив рядків) і НЕ вигадуй операцію для нього. Завжди повертай "
    "це поле, навіть порожнім масивом. Якщо unresolved_fragments непорожній — все одно поверни всі операції, "
    "які вдалося розпізнати впевнено (Python вирішить, блокувати їх чи ні).\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON:\n"
    "{\"intent\": \"household_operations\", \"operations\": ["
    "{\"type\": \"add_inventory\", \"name\": \"Масло\", \"quantity_text\": \"\", \"category\": \"Молочне та яйця\"}, "
    "{\"type\": \"add_expense\", \"amount\": \"10\", \"currency\": \"PLN\", \"category\": \"Продукти\", "
    "\"description\": \"Масло\", \"expense_date\": \"2026-07-05\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад none: {\"intent\": \"none\", \"operations\": [], \"unresolved_fragments\": []}"
)


def _numbered_item_lines(items, alias_map=None, with_normalization_hint=False):
    """Shared formatter for both the shopping and inventory numbered
    snapshots — same shape (name + quantity_text) for both.

    with_normalization_hint=True (used only for the inventory snapshot, so
    consume_inventory has a chance to match an old untranslated raw name
    like "ser" against a message like "З'їв сиру") appends a hidden
    "[normalized: ...]" hint built from resolve_item_name's canonical form —
    shown to Gemini only, never part of the actual inventory list text the
    user sees (that stays format_inventory_list's job, untouched here).
    """
    lines = []
    for i, item in enumerate(items, start=1):
        raw_name = item["name"]
        label = f"{i}. {raw_name}"
        if with_normalization_hint:
            _, normalized = _bot.resolve_item_name(raw_name, alias_map or {})
            if normalized and normalized != raw_name.strip().lower():
                label += f" [normalized: {normalized}]"
        qty = item.get("quantity_text")
        if qty:
            label += f" — {qty}"
        lines.append(label)
    return lines


def _numbered_expense_lines(recent_expenses):
    lines = []
    for i, exp in enumerate(recent_expenses, start=1):
        label = exp["description"] or exp["category"]
        date_str = exp["expense_date"].strftime("%d.%m")
        lines.append(
            f"{i}. {date_str} — {label} — {expenses._format_expense_amount(exp['amount'])} [{exp['category']}]"
        )
    return lines


_HOUSEHOLD_ROUTER_FALLBACK = {"intent": "none", "operations": [], "unresolved_fragments": []}


def _ask_gemini_household_router(text, now_context, shopping_items, inventory_items, recent_expenses, alias_map=None):
    """ONE Gemini call for the global household router. Snapshots are numbered
    lists built from live DB data (never raw ids) — Gemini only ever refers
    back to them by 1-based number. Only the inventory snapshot carries the
    hidden normalization hint (consume_inventory matching); the shopping
    snapshot is unaffected."""
    shopping_lines = _numbered_item_lines(shopping_items)
    inventory_lines = _numbered_item_lines(inventory_items, alias_map=alias_map, with_normalization_hint=True)
    expense_lines = _numbered_expense_lines(recent_expenses)
    prompt_parts = [
        now_context,
        "Активні покупки:\n" + ("\n".join(shopping_lines) if shopping_lines else "(порожньо)"),
        "Запаси:\n" + ("\n".join(inventory_lines) if inventory_lines else "(порожньо)"),
        "Останні витрати:\n" + ("\n".join(expense_lines) if expense_lines else "(порожньо)"),
        f"Користувач написав: {text}",
    ]
    prompt = "\n\n".join(prompt_parts)
    raw = _bot.call_gemini([{"role": "user", "content": prompt}], HOUSEHOLD_ROUTER_PROMPT, temperature=0.1)
    if not raw:
        return dict(_HOUSEHOLD_ROUTER_FALLBACK)
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "operations": data.get("operations") if isinstance(data.get("operations"), list) else [],
            "unresolved_fragments": (
                data.get("unresolved_fragments") if isinstance(data.get("unresolved_fragments"), list) else []
            ),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_HOUSEHOLD_ROUTER_FALLBACK)


# =========================
# VALIDATION (pure)
# =========================

# Detects a quantity/container phrase leaking into the front of `name` —
# a sign that Gemini failed to separate the quantity from the product name
# (e.g. "дві пачки сосисок" as the whole name, quantity_text left empty).
# Deliberately narrow: only blocks on these exact leading words, never a
# broader NLP guess.
_LEAKED_QUANTITY_PREFIX_RE = re.compile(
    r"^(пара|пару|два|дві|три|чотири|п['’]?ять|пачка|пачки|пачок|упаковка|упаковки|упаковок)\b",
    re.IGNORECASE,
)


def _looks_like_leaked_quantity_phrase(name):
    """True if `name` still starts with a quantity/container word that
    should have been separated into quantity_text instead. Never guessed at
    beyond this exact whitelist — blocks the whole compound preview and asks
    for clarification instead of storing a broken canonical name like
    "дві пачки сосисок"."""
    return bool(_LEAKED_QUANTITY_PREFIX_RE.match((name or "").strip()))


def _validate_new_item_op(op, alias_map):
    """Shared add_shopping/add_inventory validation. Returns a normalized
    item dict (name/canonical_name/quantity_value/quantity_unit/
    quantity_inferred/quantity_text/category) or None if invalid."""
    name = op.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    cat = op.get("category")
    if not isinstance(cat, str) or cat not in _bot.VALID_CATEGORIES:
        cat = _bot.DEFAULT_CATEGORY
    quantity_text = op.get("quantity_text")
    if not isinstance(quantity_text, str):
        quantity_text = ""
    normalized = _bot.normalize_item_quantity(name, quantity_text, allow_default_unit=True, alias_map=alias_map)
    item = {"name": name, "category": cat, "was_corrected": False}
    item.update(normalized)
    return item


def apply_inventory_representation_guard(add_inventory_items, inventory_items):
    """Run the Inventory Representation Guard v1 pass over add_inventory_items
    against a snapshot of existing inventory_items — extracted out of
    _validate_operations_detailed so it can be re-run on its own later, with
    a FRESH inventory_items snapshot, once a quantity clarification reply
    has replaced one item's previously-inferred quantity with an explicit
    one (see bot.py's pending_inventory_quantity_clarification continuation
    flow). Never mutates the input list/dicts in place — returns new dicts —
    so calling it again for the same items is always safe and never re-runs
    Gemini or re-parses the original message.

    Returns:
      ("ok", (updated_items, inventory_merge_targets))
      ("clarify", {"item_name", "canonical_name", "category", "existing_items"})
          — existing_items is the full list of conflicting rows (see
          resolve_inventory_representation's own "clarify" docstring).
    """
    updated_items = []
    inventory_merge_targets = []
    for item in add_inventory_items:
        item = dict(item)
        outcome, existing = _bot.resolve_inventory_representation(
            inventory_items, item.get("canonical_name"), item.get("category"),
            item.get("quantity_value"), item.get("quantity_unit"), item.get("quantity_inferred", False),
        )
        if outcome == "clarify":
            # Blocks the WHOLE compound preview — never apply the rest of
            # the operations partially just because one item is ambiguous.
            return "clarify", {
                "item_name": item["name"],
                "canonical_name": item.get("canonical_name"),
                "category": item.get("category"),
                "existing_items": existing,
            }
        if outcome == "merge":
            merged_value, merged_unit = _bot.merge_quantity_values(
                existing["quantity_value"], existing["quantity_unit"],
                item["quantity_value"], item["quantity_unit"],
            )
            item["_representation_outcome"] = "merge"
            item["_representation_note"] = _bot.format_representation_merge_line(
                item["name"], existing["quantity_text"], item["quantity_text"],
                _bot.format_quantity_display(merged_value, merged_unit),
            )
            inventory_merge_targets.append({
                "item_id": existing["id"], "quantity_value": existing["quantity_value"],
                "quantity_unit": existing["quantity_unit"],
            })
        elif outcome == "separate":
            item["_representation_outcome"] = "separate"
            item["_representation_note"] = _bot.format_representation_separate_warning(
                item["name"], existing["quantity_text"], item["quantity_text"],
            )
        else:
            item.pop("_representation_outcome", None)
            item.pop("_representation_note", None)
        updated_items.append(item)
    return "ok", (updated_items, inventory_merge_targets)


def _validate_operations_detailed(router_result, inventory_items, recent_expenses, now, alias_map=None):
    """Same validation as _validate_operations, but the "clarify" outcome
    carries full structured detail instead of a formatted message — used by
    build_household_operations_preview (and, through it, bot.py's
    _try_global_household_router) to set up a continuation state instead of
    just displaying a dead-end message. _validate_operations itself stays a
    thin wrapper around this function so its own external contract (and
    every existing test against it) is completely unchanged.

    Returns one of:
      ("ok", payload) — payload has add_shopping_items, add_inventory_items
          (each add_inventory item may carry "_representation_outcome"
          "merge"/"separate" plus a "_representation_note" preview line —
          see apply_inventory_representation_guard above),
          inventory_merge_targets ([{item_id, quantity_value, quantity_unit}]
          snapshots for every "merge" outcome, to be folded into the
          caller's inventory_targets so confirm-time re-verifies them),
          consume_changes (resolved dicts, see _resolve_consumption shape),
          new_expense (dict or None), delete_expense (dict or None: id +
          snapshot + display label).
      ("unresolved", [fragment_str, ...]) — blocks the entire result.
      ("invalid", [reason_str, ...]) — blocks the entire result.
      ("clarify", {"item_name", "canonical_name", "category", "existing_items",
                    "add_shopping_items", "add_inventory_items",
                    "consume_changes", "new_expense", "delete_expense"})
          — an inferred incoming inventory quantity conflicts with an
          existing row's representation; blocks the entire result (never a
          partial apply). Carries every operation already validated up to
          that point, so a caller can resolve the ambiguous quantity later
          and re-run apply_inventory_representation_guard without redoing
          any of this work or re-calling Gemini.
      ("none", None)
    """
    fragments = router_result.get("unresolved_fragments")
    if isinstance(fragments, list):
        cleaned_fragments = [str(f).strip() for f in fragments if str(f).strip()]
        if cleaned_fragments:
            return "unresolved", cleaned_fragments

    if router_result.get("intent") != "household_operations":
        return "none", None

    operations = router_result.get("operations")
    if not isinstance(operations, list) or not operations:
        return "none", None

    reasons = []
    add_shopping_raw = []
    add_inventory_raw = []
    consume_changes = []
    used_inventory_numbers = set()
    new_expense = None
    new_expense_count = 0
    delete_expense = None
    delete_expense_count = 0

    total_inventory = len(inventory_items)

    for op in operations:
        if not isinstance(op, dict) or op.get("type") not in _ALLOWED_OP_TYPES:
            reasons.append("Незрозуміла дія.")
            continue
        op_type = op["type"]

        if op_type == "add_shopping":
            leaked_name = op.get("name")
            if isinstance(leaked_name, str) and _looks_like_leaked_quantity_phrase(leaked_name):
                reasons.append(f"«{leaked_name.strip()}» — не можу безпечно відокремити кількість від назви товару.")
                continue
            item = _validate_new_item_op(op, alias_map)
            if item is None:
                reasons.append("Товар для покупок без назви.")
                continue
            add_shopping_raw.append(item)

        elif op_type == "add_inventory":
            leaked_name = op.get("name")
            if isinstance(leaked_name, str) and _looks_like_leaked_quantity_phrase(leaked_name):
                reasons.append(f"«{leaked_name.strip()}» — не можу безпечно відокремити кількість від назви товару.")
                continue
            item = _validate_new_item_op(op, alias_map)
            if item is None:
                reasons.append("Товар для запасів без назви.")
                continue
            add_inventory_raw.append(item)

        elif op_type == "consume_inventory":
            num = op.get("item_number")
            if not isinstance(num, int) or num < 1 or num > total_inventory:
                reasons.append("Невідома позиція запасів для списання.")
                continue
            if num in used_inventory_numbers:
                reasons.append(f"«{inventory_items[num - 1]['name']}» — позиція задіяна в кількох діях одночасно.")
                continue
            value = op.get("quantity_value")
            unit = op.get("quantity_unit")
            item = inventory_items[num - 1]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                reasons.append(f"«{item['name']}» — не можу безпечно визначити кількість для списання.")
                continue
            if unit not in _bot.STRUCTURED_UNITS:
                reasons.append(f"«{item['name']}» — невідома одиниця вимірювання.")
                continue
            cur_value = item.get("quantity_value")
            cur_unit = item.get("quantity_unit")
            if cur_value is None or cur_unit is None:
                reasons.append(f"«{item['name']}» — не вказана точна кількість, не можна безпечно списати частину.")
                continue
            kind, remaining, remaining_unit = _bot._resolve_consumption(cur_value, cur_unit, value, unit)
            if kind == "incompatible_units":
                reasons.append(f"«{item['name']}» — несумісні одиниці для списання.")
                continue
            if kind == "insufficient":
                available = _bot.format_quantity_display(cur_value, cur_unit)
                requested = _bot.format_quantity_display(value, unit)
                reasons.append(f"«{item['name']}» — у запасах лише {available}, а вказано {requested}.")
                continue
            used_inventory_numbers.add(num)
            will_remove = remaining == 0
            new_value = None if will_remove else float(remaining)
            new_unit = None if will_remove else remaining_unit
            consume_changes.append({
                "item_number": num, "item_id": item["id"], "name": item["name"],
                "old_value": cur_value, "old_unit": cur_unit,
                "old_display": _bot.format_quantity_display(cur_value, cur_unit),
                "new_value": new_value, "new_unit": new_unit,
                "new_display": None if will_remove else _bot.format_quantity_display(new_value, new_unit),
                "will_remove": will_remove,
            })

        elif op_type == "add_expense":
            new_expense_count += 1
            if new_expense_count > 1:
                reasons.append("Можна додати лише одну нову витрату за раз.")
                continue
            currency = op.get("currency")
            if currency not in (None, "PLN"):
                reasons.append("Не можу безпечно визначити валюту витрати.")
                continue
            amount = expenses._parse_expense_amount(op.get("amount"))
            if amount is None:
                reasons.append("Не можу безпечно визначити суму витрати.")
                continue
            expense_date = expenses._validate_expense_date(op.get("expense_date"), now=now)
            if expense_date is None:
                reasons.append("Не можу безпечно визначити дату витрати.")
                continue
            category, category_was_defaulted = expenses._validate_expense_category(op.get("category"))
            description = expenses._clean_expense_description(op.get("description"))
            new_expense = {
                "amount": amount, "currency": "PLN", "category": category,
                "category_was_defaulted": category_was_defaulted, "description": description,
                "expense_date": expense_date,
            }

        elif op_type == "delete_expense":
            delete_expense_count += 1
            if delete_expense_count > 1:
                reasons.append("Можна видалити лише одну витрату за раз.")
                continue
            numbers = op.get("selected_numbers")
            matched = _bot._validate_selected_numbers(numbers, recent_expenses) if isinstance(numbers, list) else None
            if matched is None or len(matched) != 1:
                reasons.append("Не знайшов однозначної витрати для видалення.")
                continue
            expense = matched[0]
            delete_expense = {
                "expense_id": expense["id"],
                "snapshot": {
                    "amount": expense["amount"], "category": expense["category"],
                    "expense_date": expense["expense_date"], "description": expense["description"],
                },
                "display": expense["description"] or expense["category"],
                "amount_display": expenses._format_expense_amount(expense["amount"]),
            }

    if reasons:
        return "invalid", reasons

    add_shopping_items = _bot._auto_merge_in_place(add_shopping_raw) if add_shopping_raw else []
    add_inventory_items = _bot._auto_merge_in_place(add_inventory_raw) if add_inventory_raw else []

    # Inventory Representation Guard v1 — runs on the FINAL (already
    # RAM-deduplicated) add_inventory_items, once per distinct product, so a
    # message mentioning the same product twice is checked against the live
    # inventory exactly once, using its combined quantity.
    guard_kind, guard_result = apply_inventory_representation_guard(add_inventory_items, inventory_items)
    if guard_kind == "clarify":
        return "clarify", {
            **guard_result,
            "add_shopping_items": add_shopping_items,
            "add_inventory_items": add_inventory_items,
            "consume_changes": consume_changes,
            "new_expense": new_expense,
            "delete_expense": delete_expense,
        }
    add_inventory_items, inventory_merge_targets = guard_result

    if not add_shopping_items and not add_inventory_items and not consume_changes and not new_expense and not delete_expense:
        return "invalid", ["Не знайшов жодної дії для виконання."]

    return "ok", {
        "add_shopping_items": add_shopping_items,
        "add_inventory_items": add_inventory_items,
        "consume_changes": consume_changes,
        "new_expense": new_expense,
        "delete_expense": delete_expense,
        "inventory_merge_targets": inventory_merge_targets,
    }


def _validate_operations(router_result, inventory_items, recent_expenses, now, alias_map=None):
    """Validate the global household router's JSON against live snapshots.

    Thin wrapper around _validate_operations_detailed that formats its
    structured "clarify" payload down to a plain message string — this is
    the exact same external contract this function has always had, kept
    unchanged on purpose so every existing caller/test of THIS function
    (as opposed to build_household_operations_preview, which calls the
    detailed version directly) keeps working without modification.

    Returns one of:
      ("ok", payload) — see _validate_operations_detailed.
      ("unresolved", [fragment_str, ...]) — blocks the entire result.
      ("invalid", [reason_str, ...]) — blocks the entire result.
      ("clarify", message_str) — an inferred incoming inventory quantity
          conflicts with an existing row's representation; blocks the
          entire result (never a partial apply) and asks the user to state
          an explicit quantity instead of guessing.
      ("none", None)
    """
    kind, result = _validate_operations_detailed(router_result, inventory_items, recent_expenses, now, alias_map=alias_map)
    if kind == "clarify":
        return "clarify", _bot.format_representation_clarify_message(result["item_name"], result["existing_items"])
    return kind, result


# =========================
# FORMATTERS (pure)
# =========================
def _format_new_item_line(item):
    _, _, qty_display = _bot._effective_quantity(item)
    label = item["name"]
    if qty_display:
        label += f" — {qty_display}"
        if item.get("quantity_inferred"):
            label += " (припущення)"
    return f"• Додати {label}"


def format_preview(payload):
    lines = ["План змін:"]

    if payload["add_shopping_items"]:
        lines.append("")
        lines.append("🛒 Покупки")
        for item in payload["add_shopping_items"]:
            lines.append(_format_new_item_line(item))

    if payload["add_inventory_items"] or payload["consume_changes"]:
        separate_warnings = [
            item["_representation_note"] for item in payload["add_inventory_items"]
            if item.get("_representation_outcome") == "separate"
        ]
        for warning in separate_warnings:
            lines.append("")
            lines.append(warning)
        lines.append("")
        lines.append("🧊 Запаси")
        for item in payload["add_inventory_items"]:
            if item.get("_representation_outcome") == "merge":
                lines.append(item["_representation_note"])
            else:
                lines.append(_format_new_item_line(item))
        for c in payload["consume_changes"]:
            label = c["name"]
            if c["old_display"]:
                label += f" — {c['old_display']}"
            if c["will_remove"]:
                lines.append(f"• {label} → буде прибрано із запасів")
            else:
                lines.append(f"• {label} → {c['name']} — {c['new_display']}")

    new_expense = payload["new_expense"]
    delete_expense = payload["delete_expense"]
    if new_expense or delete_expense:
        lines.append("")
        lines.append("💸 Витрати")
        if new_expense:
            amount_display = expenses._format_expense_amount(new_expense["amount"])
            desc = new_expense["description"] or new_expense["category"]
            lines.append(f"• Додати {desc} — {amount_display}")
            lines.append(f"• Категорія: {new_expense['category']}")
        if delete_expense:
            lines.append(f"• Видалити {delete_expense['display']} — {delete_expense['amount_display']}")

    lines.append("")
    lines.append("✅ Так, застосувати")
    lines.append("❌ Скасувати")
    return "\n".join(lines)


def format_unresolved_message(fragments):
    lines = ["Я зрозумів частину повідомлення, але не хочу мовчки пропустити решту.", "", "Не зміг зрозуміти:"]
    for frag in fragments:
        lines.append(f"• «{frag}»")
    lines.append("")
    lines.append("Спробуй уточнити все повідомлення.")
    return "\n".join(lines)


def format_invalid_message(reasons):
    lines = ["Не зміг безпечно обробити всі дії. Нічого не було змінено.", "", "Причина:"]
    for reason in reasons:
        lines.append(f"• {reason}")
    return "\n".join(lines)


# =========================
# TOP-LEVEL ENTRY POINT
# =========================
def build_household_operations_preview(text, shopping_items, inventory_items, recent_expenses, alias_map=None):
    """Runs the Gemini classification + Python validation pipeline for one
    message. Caller must already have checked gate(text) and the
    "any active preview/selection" guard before calling this — this function
    always attempts a Gemini call.
    """
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    now_context = _bot.get_warsaw_datetime_context(now)
    router_result = _ask_gemini_household_router(
        text, now_context, shopping_items, inventory_items, recent_expenses, alias_map=alias_map,
    )
    # Uses the _detailed variant (not _validate_operations itself) so a
    # "clarify" outcome carries full structured detail — see
    # _validate_operations_detailed's docstring — which bot.py's
    # _try_global_household_router needs to set up
    # pending_inventory_quantity_clarification instead of just displaying a
    # dead-end message.
    return _validate_operations_detailed(router_result, inventory_items, recent_expenses, now, alias_map=alias_map)
