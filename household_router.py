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
canonicalize_name, the keyboards — it goes through the live `_bot` module reference handed in
via configure(), never a snapshotted import (mirrors expenses.py's own
`_bot` indirection, for the same reason).

`expenses.py` is imported directly (not through `_bot`): it never imports
bot.py or this module, so there is no cycle, and its amount/date/category
validators and formatters are reused as-is for the add_expense/delete_expense
side of every operation instead of being duplicated a second time. Same
reasoning for `preview_editing.compute_quantity_multiplier` (Safe Discount
Calculation V1's unit-conversion math — never duplicated) and `quantities.
parse_structured_quantity` (discount_expense's unit_price_basis parsing) —
neither imports bot.py or this module either.

Owns no Telegram/pending state of its own — bot.py stores
pending_global_household and performs the actual DB write via
database.apply_global_household_operations; this module only classifies
text into normalized operations and builds preview/clarification text, as a
set of pure functions plus the one Gemini call.
"""
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import expenses
import preview_editing
from quantities import parse_structured_quantity

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
# Quantity + Price Intent Clarification V1 — "взял\w*"/"взяв" added (covers
# "взяли"/"взяла"/"взяв") so "Взяли сир 500 г за 12 zł" is recognized as the
# same compound purchase phrasing "Купив ..."/"Придбав ..." already is,
# matching shopping_action_planner.py's own _BOUGHT_OR_TAKEN_VERB_ROOTS
# (which already treats "взял"/"взяв" as a bought/taken verb root for its
# own, unrelated "already marked bought" shape).
_BOUGHT_RE = re.compile(r"купив|купила|купили|придбав|придбала|взял\w*|взяв", re.IGNORECASE)
_CONSUME_RE = re.compile(
    r"з[’']?їв|з[’']?їла|з[’']?їли|використав|використала|використали|"
    r"спожив|спожила|спожили|доїв|доїла",
    re.IGNORECASE,
)
_MISTAKE_EXPENSE_RE = re.compile(
    r"(випадков\w*|помилков\w*).{0,40}витрат\w*|витрат\w*.{0,40}(випадков\w*|помилков\w*)",
    re.IGNORECASE,
)

# Assumption-Based Purchase Preview V1 — a standalone message can describe a
# purchase/price/discount/free-item WITHOUT an explicit "купив"-style verb
# ("візочок коштував 3300, знижка 150", "автокрісло дісталось безкоштовно",
# or a mixed/English "we bought a komod ... bought it for 527") — these
# still need a real Gemini pass, not just messages phrased as an imperative
# purchase. Deliberately high-recall, same reasoning as gate()'s other
# patterns and mini_action_planner.looks_household_like's own pre-gate: a
# false positive here only costs one harmless extra Gemini call that
# resolves to intent="none".
_PRICE_CONTEXT_RE = re.compile(
    r"коштув\w*|безкоштовно|знижк\w*|скидк\w*|bought|paid|\bcost\b",
    re.IGNORECASE,
)

# Purchase Event Planner V1 — used ONLY for the "nothing else was produced
# at all" fallback note near the end of _validate_operations_detailed (see
# `has_discount_marker` there). It used to ALSO hard-block every add_expense
# op in a message that merely mentioned a discount/percentage ANYWHERE, even
# for unrelated items with their own clearly-stated final price — Assumption-
# Based Purchase Preview V1 removed that blanket block (see rule 5 in its
# work order: "do not hard-block the whole expense just because the word
# discount/знижка exists"). The remaining, narrower defenses are: (1)
# _amount_literally_in_text, still enforced on every add_expense amount —
# but a failure there is now a non-blocking note (see the add_expense
# branch), never a batch-wide "invalid"; (2) assumed_expense (below), the
# type Gemini is now instructed to use INSTEAD of add_expense whenever a
# flat original-price-minus-discount computation is actually needed — never
# computed by Gemini, always recomputed here in Python from literal pieces.
_DISCOUNT_MARKER_RE = re.compile(
    r"знижк\w*|знижен\w*|відсот\w*|скидк\w*|zniżk\w*|промо\w*|%",
    re.IGNORECASE,
)

# Referenced by tests documenting the Ukrainian phrasing this project treats
# as an explicit "this is what I actually paid" signal (see expenses._parse_
# expense_amount/add_expense's own description field and Assumption-Based
# Purchase Preview V1's prompt guidance) — no longer used to gate anything
# here in Python; the choice between add_expense (a clear final amount) and
# assumed_expense (a computed one) is now Gemini's own classification call,
# per rule 2 of that work order ("AI may suggest structured operations, but
# Python validates amounts" — not which op type was the right one to use).
_FINAL_AMOUNT_MARKER_RE = re.compile(
    r"заплатив\w*|оплатив\w*|сплатив\w*|фінальн\w*|фактично\s+сплач\w*|"
    r"загалом|у\s?підсумку|зрештою|вийшло",
    re.IGNORECASE,
)


def _parse_percent(raw_percent):
    """Parse a discount percent into an exact Decimal in (0, 100], or None
    if missing/unparseable/out of range — same never-float, never-negative
    contract as expenses._parse_expense_amount, reused for discount_
    expense's discount_percent field."""
    if raw_percent is None:
        return None
    if isinstance(raw_percent, (int, float)):
        raw_percent = str(raw_percent)
    if not isinstance(raw_percent, str):
        return None
    cleaned = raw_percent.strip().replace("%", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0 or value > 100:
        return None
    return value


def _normalize_display_name(name):
    """If `name`'s cleaned/lowercased form is EXACTLY a known product
    synonym key in _bot._NAME_SYNONYMS (e.g. "komod"->"комод", "auto
    carsel"->"автокрісло", "печення"->"печиво") — return the canonical form,
    capitalized for display; a name that ISN'T a synonym key is returned
    completely unchanged (never guessed/rewritten). Only used for expense
    descriptions (add_expense/assumed_expense) — unlike add_shopping/
    add_inventory item names, a plain expense description never otherwise
    goes through canonicalize_name/_NAME_SYNONYMS at all, so "we bought a
    komod for 527" would otherwise show "komod" verbatim in the preview."""
    if not isinstance(name, str) or not name.strip():
        return name
    base = _bot._repair_mixed_script(_bot._clean_unicode_whitespace(name)).strip().lower()
    synonym = _bot._NAME_SYNONYMS.get(base)
    if synonym is None:
        return name
    return _bot.inventory.capitalize_first(synonym)


def _clean_context_note(raw_note):
    """Cosmetic-only, Gemini-authored context for a plain add_expense entry
    (e.g. "Оригінальна ціна 650 zł, куплено за 570 zł") — no amount here is
    ever trusted/parsed/used for anything beyond display, since the actual
    expense amount was already independently validated as its own literal
    number (see add_expense's own `amount` field). Whitespace-collapsed and
    length-capped; None (not "") when missing/blank, so callers can use a
    simple truthiness check."""
    if not isinstance(raw_note, str):
        return None
    cleaned = re.sub(r"\s+", " ", raw_note).strip()
    return cleaned[:200] or None


def _format_assumption_note(original_price, discount_text, final_amount):
    """Assumption-Based Purchase Preview V1's per-item note for an
    assumed_expense entry (e.g. "Припущення: 3300 zł − 150 zł = 3150 zł.
    Якщо 3300 zł уже була фінальна ціна, зміни перед підтвердженням.") —
    always built HERE from the already-Python-computed final_amount, never
    from anything Gemini might have written itself (the whole point of
    assumed_expense is that Python, not Gemini, owns the arithmetic — see
    the op's own validation). `discount_text` is a pre-formatted display
    string for the subtracted piece — either an amount ("150,00 zł") or a
    percent ("50%"), decided by the caller."""
    original_display = expenses._format_expense_amount(original_price)
    final_display = expenses._format_expense_amount(final_amount)
    return (
        f"Припущення: {original_display} − {discount_text} = {final_display}. "
        f"Якщо {original_display} уже була фінальна ціна, зміни перед підтвердженням."
    )


def _format_discount_calculation_note(unit_price, discount_percent, basis_unit, discounted_unit_price, multiplier, final_amount):
    """The plain-text explanation shown alongside a Safe Discount
    Calculation V1 expense (e.g. "20,00 zł/кг − 50% = 10,00 zł/кг; 1 кг ×
    10,00 zł = 10,00 zł") — every number formatted with the SAME functions
    the rest of the app already uses (expenses._format_expense_amount,
    _bot.format_quantity_display), never a bespoke display format."""
    percent_display = _bot.format_quantity_display(discount_percent, None)
    unit_price_display = expenses._format_expense_amount(unit_price)
    discounted_display = expenses._format_expense_amount(discounted_unit_price)
    multiplier_display = _bot.format_quantity_display(multiplier, None)
    final_display = expenses._format_expense_amount(final_amount)
    return (
        f"{unit_price_display}/{basis_unit} − {percent_display}% = {discounted_display}/{basis_unit}; "
        f"{multiplier_display} {basis_unit} × {discounted_display} = {final_display}"
    )


def _dedupe_preserve_order(items):
    """First-occurrence-wins de-duplication for user-facing reason/fragment/
    note lists — several validation branches can independently produce the
    exact same wording for the same underlying problem (e.g. two ops both
    failing for the same reason); showing it twice is confusing noise, never
    new information, so every caller that builds one of these lists dedupes
    right before it reaches the user."""
    return list(dict.fromkeys(items))


def gate(text):
    """True if `text` contains phrasing this router is meant to handle —
    checked before any Gemini call, from anywhere, regardless of which menu
    is open. Narrow on purpose: a plain zł-tagged amount or an imperative
    "видали/скасуй витрату" never matches here (those stay on the existing
    narrow expense gates), so this router only ever gets a first look at
    messages the legacy gates don't already own. _PRICE_CONTEXT_RE (see its
    own comment) additionally covers a purchase/price/discount/free-item
    STORY with no imperative buy verb at all (Assumption-Based Purchase
    Preview V1) — e.g. "коштувало 650, знайшли за 570", "дісталось
    безкоштовно", or English "we bought it for 527".
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
        or _PRICE_CONTEXT_RE.search(stripped)
    )


# =========================
# GEMINI PROMPT
# =========================
_ALLOWED_OP_TYPES = {
    "add_shopping", "add_inventory", "consume_inventory", "add_expense", "delete_expense", "ambiguous_expense",
    "discount_expense", "assumed_expense",
}

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
    "Повідомлення часто описує КІЛЬКА окремих покупок одразу (напр. купили візочок, ліжечко, комод і подарунок "
    "в одному повідомленні) — обробляй КОЖНУ покупку НЕЗАЛЕЖНО: якщо сума для одного товару чітка, а для "
    "іншого неоднозначна — все одно додай чітку операцію (add_expense/discount_expense/assumed_expense) для "
    "першого товару, а для другого просто додай ambiguous_expense; неоднозначність одного товару НІКОЛИ не "
    "заважає іншим товарам у тому самому повідомленні.\n"
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
    "З name прибирай ЛИШЕ слова про кількість чи тару — прикметники й означення товару (колір, смак, вид: "
    "«зелений», «чорний», «кокосовий», «рисовий», «грецький», «червоний», «білий», «кисломолочний», "
    "«вершковий», «мисливські», «тестовий» тощо) є частиною назви товару і їх ЗАВЖДИ треба залишати в name "
    "(у називному відмінку): «зеленого чаю» → name «Зелений чай»; «кокосового молока» → name «Кокосове "
    "молоко»; «тестового чаю» → name «Тестовий чай»; «грецького йогурту» → name «Грецький йогурт»; "
    "«червоної квасолі» → name «Червона квасоля»; «мисливських ковбасок» → name «Мисливські ковбаски». "
    "НІКОЛИ не скорочуй name до одного загального іменника (напр. «Чай», «Молоко», «Йогурт»), якщо в "
    "оригінальному тексті перед іменником був прикметник чи означення.\n"
    "Якщо кількість написана словом із чітким числовим значенням («пів кілограма», «пів літра», «два "
    "кілограми») — переведи quantity_text у явний числовий вигляд («0,5 кг», «0,5 л», «2 кг»), а НЕ залишай "
    "словами (виняток — «пара»/«пару», для них лишай слово, як описано вище). Якщо той самий товар "
    "згадується в повідомленні кілька разів із кількістю (напр. «купив пів кілограма печива, а потім "
    "повернувся і купив ще пів») — додай ОКРЕМУ операцію add_inventory з тим самим name на КОЖНУ згадку "
    "(«0,5 кг» і «0,5 кг» окремо) — Python сам підсумує однакові товари; ти НІКОЛИ сам не підсумовуй "
    "кількості в одну операцію.\n"
    "Приклад (для «Купив пару сосисок»): {\"type\": \"add_inventory\", \"name\": \"Сосиски\", "
    "\"quantity_text\": \"пару\", \"category\": \"М'ясо та риба\"}\n"
    "Приклад (для «Купив дві пачки сосисок»): {\"type\": \"add_inventory\", \"name\": \"Сосиски\", "
    "\"quantity_text\": \"дві пачки\", \"category\": \"М'ясо та риба\"}\n"
    "Приклад (для «Додай до покупок 1 шт. тестового чаю»): {\"type\": \"add_shopping\", "
    "\"name\": \"Тестовий чай\", \"quantity_text\": \"1 шт.\", \"category\": \"Напої\"}\n"
    "3. Якщо повідомлення означає «купив X ЗА Y zł» — це ОДНА покупка з ціною: додай ОБИДВІ операції — "
    "add_inventory (сам товар) І add_expense (сума) в одному масиві operations. Але якщо ця сама ціна "
    "згадується РАЗОМ зі знижкою/відсотком (напр. «коштує 20 zł, але знижка 50%») — Y НЕ є сумою, яку "
    "фактично сплачено: не додавай add_expense у цьому випадку. Якщо явно вказано, за ЯКУ кількість ця ціна "
    "(«за кілограм», «за штуку», «за 1 л») — використай тип 8 (discount_expense) нижче для безпечного "
    "розрахунку. Якщо НЕ вказано, за яку кількість ціна — використай тип 7 (ambiguous_expense).\n"
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
    "контексту; ніколи не в майбутньому), необов'язкове context_note (короткий інформативний коментар без "
    "жодних сум для обчислення, напр. «Оригінальна/сайтова ціна 650 zł, куплено за 570 zł» — лише контекст, "
    "НІКОЛИ не сума, яку треба порахувати). Повідомлення може описувати КІЛЬКА окремих покупок із сумою "
    "(напр. «Купив молоко за 8 zł. Купив хліб за 5 zł») — тоді додай по ОДНІЙ операції add_expense на кожну "
    "таку покупку, у тому самому порядку, як вони згадані в тексті; ніколи не підсумовуй кілька сум в одну "
    "операцію. Якщо та сама сума повторюється двічі поспіль для ОДНІЄЇ покупки (напр. «60 за 60», «60 for "
    "60») — це ОДНА сума 60, а не дві окремі покупки чи подвоєна сума.\n"
    "Якщо користувач явно каже, що якесь число — це ОСТАТОЧНА/ФАКТИЧНО СПЛАЧЕНА сума (слова «заплатив», "
    "«оплатив», «сплатив», «фінально», «загалом», «зрештою», «вийшло», «paid», «bought it for», «bought for», "
    "«found it for», англ. «for») — це ЗАВЖДИ add_expense з тим числом, НАВІТЬ якщо десь у тому самому "
    "повідомленні згадана знижка чи первісна/сайтова ціна; використай число як є, ніколи нічого не "
    "перераховуй. Якщо разом з фінальною сумою згадана й оригінальна/сайтова ціна («на сайті коштувало 650, "
    "але знайшли за 570» / «cost 627, but we bought it for 527») — постав фінальну суму (570/527) в amount, "
    "а оригінальну ціну опиши в context_note, БЕЗ будь-якого обчислення.\n"
    "Якщо людина каже, що отримала товар БЕЗКОШТОВНО / нічого не заплатила («дісталось безкоштовно», «didn't "
    "pay anything for this») — НІКОЛИ не додавай жодної операції з сумою (ні add_expense, ні "
    "ambiguous_expense, ні assumed_expense) для цього товару: лише add_inventory/add_shopping для самого "
    "товару, без жодної згадки суми.\n"
    "6. «delete_expense» — людина каже, що ВИПАДКОВО чи ПОМИЛКОВО додала витрату і хоче її прибрати (напр. "
    "«Булочку до витрат я додав випадково», «Помилково записав ту витрату»). Повертай це ЛИШЕ якщо рівно "
    "ОДНА позиція з наданого списку останніх витрат явно відповідає опису; якщо жодна або кілька можуть "
    "підходити — не повертай цю операцію, опиши фрагмент у unresolved_fragments замість того, щоб вгадувати. "
    "Поле: selected_numbers — масив з РІВНО одним номером зі списку останніх витрат. Максимум одна операція "
    "delete_expense на повідомлення.\n"
    "7. «ambiguous_expense» — повідомлення згадує ціну/суму покупки, але її НЕ можна безпечно перетворити на "
    "add_expense, discount_expense чи assumed_expense: коли НЕЯСНО, до якого товару чи якої частини "
    "повідомлення сума взагалі відноситься, коли жодне число з тексту не можна безпечно вважати ні "
    "оригінальною ціною, ні знижкою, ні фінальною сумою, або коли є кілька можливих трактувань і жодне не "
    "переважає. НІКОЛИ не обчислюй і не вигадуй остаточну суму в такому випадку. Замість add_expense додай "
    "ОДНУ операцію {\"type\": \"ambiguous_expense\", \"note\": \"...\"}, де note — КОРОТКЕ УТОЧНЮЮЧЕ "
    "ЗАПИТАННЯ українською, що перелічує можливі варіанти суми (напр. «20 zł — це ціна за 1 кг, за 0,5 кг чи "
    "фінальна сума?»), а не просто констатація неоднозначності.\n"
    "8. «discount_expense» — БЕЗПЕЧНИЙ розрахунок ЦІНИ ЗА ОДИНИЦЮ (на кг/л/шт.) зі знижкою, коли товар "
    "куплено певною КІЛЬКІСТЮ: використовуй ЛИШЕ коли в тексті ЯВНО і ОДНОЗНАЧНО є ВСІ три частини разом: "
    "(а) ціна за одиницю товару, (б) ЗА ЯКУ САМЕ кількість ця ціна («за кілограм», «за 1 л», «за штуку» — "
    "якщо в тексті просто «за кілограм»/«за штуку» без числа, це означає «1 кг»/«1 шт.»), (в) відсоток "
    "знижки. Поля: unit_price (рядок, ціна ДО знижки, як у тексті), unit_price_basis (рядок, кількість+"
    "одиниця, ЗАВЖДИ цифрами — «1 кг», «1 л», «1 шт.» — ніколи словом), discount_percent (рядок, число "
    "відсотка без знака %, як у тексті), currency (завжди «PLN»). Python сам порахує остаточну суму (ціна за "
    "одиницю − знижка, помножена на куплену кількість того самого товару) — ти НІКОЛИ не вказуєш готову суму "
    "тут, лише ці три явні частини.\n"
    "9. «assumed_expense» — БЕЗПЕЧНЕ ПРИПУЩЕННЯ: товар куплено ЗА ОДНУ покупку (не по вазі/об'єму), відома "
    "ЯВНА оригінальна ціна і ЯВНА знижка (сумою або відсотком), АЛЕ немає слова про фактично сплачену/"
    "фінальну суму (див. тип 5) — напр. «Ми купили візочок за 3300 злотих, але нам зробили знижку 150 "
    "злотих» (немає «заплатили»/«вийшло», лише оригінальна ціна і знижка). Поля: description (назва товару), "
    "original_price (рядок, ціна ДО знижки, як у тексті), і ОДНЕ з: discount_amount (рядок, сума знижки в "
    "злотих, як у тексті) або discount_percent (рядок, відсоток знижки без знака %), currency (завжди "
    "«PLN»). Python сам порахує original_price − discount_amount (або original_price × (100−відсоток)/100) і "
    "покаже це як ПРИПУЩЕННЯ з поясненням та попередженням — ти НІКОЛИ не вказуєш готову суму тут.\n"
    "Різниця між discount_expense (8) і assumed_expense (9): 8 — коли ціна за ОДИНИЦЮ виміру (кг/л/шт.) і "
    "товар мають окрему кількість для множення; 9 — коли ціна й знижка стосуються ОДНІЄЇ конкретної покупки "
    "цілком (напр. один візочок), без множення на кількість. Якщо жодна з трьох частин (оригінальна ціна, "
    "знижка, кількість/одиниця) не зовсім явна — використай тип 7 (ambiguous_expense) замість 8 чи 9.\n\n"
    "Категорії товарів (для add_shopping/add_inventory): М'ясо та риба, Молочне та яйця, Овочі та зелень, "
    "Фрукти та ягоди, Хліб і випічка, Крупи, макарони та борошно, Соуси, спеції та бакалія, Солодке та "
    "снеки, Напої, Заморожене, Інше їстівне.\n"
    "Категорії витрат (для add_expense): Продукти, Дім і рахунки, Транспорт, Здоров'я, Кафе / ресторани, "
    "Побут, Дитина, Інше.\n\n"
    "Якщо частину повідомлення не можна безпечно перетворити на жодну з цих операцій — додай короткий опис "
    "цього фрагмента в unresolved_fragments (масив рядків) і НЕ вигадуй операцію для нього. Завжди повертай "
    "це поле, навіть порожнім масивом. Якщо unresolved_fragments непорожній — все одно поверни всі операції, "
    "які вдалося розпізнати впевнено (Python вирішить, блокувати їх чи ні).\n"
    "ВАЖЛИВО про unresolved_fragments: туди йдуть ЛИШЕ фрагменти, що виглядають як НАМІР дії (товар, "
    "кількість чи сума), який не вдалося безпечно розпізнати. НІКОЛИ не додавай у unresolved_fragments чисто "
    "оповідальні деталі, які не описують товар/кількість/суму (хто, де, яка погода, зовнішність людей, "
    "побічні деталі сюжету) — такі деталі просто ігноруй, вони не впливають на жодну операцію.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON:\n"
    "{\"intent\": \"household_operations\", \"operations\": ["
    "{\"type\": \"add_inventory\", \"name\": \"Масло\", \"quantity_text\": \"\", \"category\": \"Молочне та яйця\"}, "
    "{\"type\": \"add_expense\", \"amount\": \"10\", \"currency\": \"PLN\", \"category\": \"Продукти\", "
    "\"description\": \"Масло\", \"expense_date\": \"2026-07-05\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад none: {\"intent\": \"none\", \"operations\": [], \"unresolved_fragments\": []}\n"
    "Приклад ambiguous_expense (для «Печиво коштувало 20 zł, було 50% знижки, я купив пів кілограма і потім "
    "ще пів.» — ЦІНА без вказаної кількості, за яку вона): {\"intent\": \"household_operations\", "
    "\"operations\": ["
    "{\"type\": \"add_inventory\", \"name\": \"Печиво\", \"quantity_text\": \"0,5 кг\", \"category\": "
    "\"Солодке та снеки\"}, "
    "{\"type\": \"add_inventory\", \"name\": \"Печиво\", \"quantity_text\": \"0,5 кг\", \"category\": "
    "\"Солодке та снеки\"}, "
    "{\"type\": \"ambiguous_expense\", \"note\": \"20 zł — це ціна за 1 кг, за 0,5 кг чи фінальна сума?\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад discount_expense (для «Печиво коштувало 20 zł за кілограм, було 50% знижки, я купив пів "
    "кілограма і потім ще пів.» — ЦІНА ЯВНО за кілограм): {\"intent\": \"household_operations\", "
    "\"operations\": ["
    "{\"type\": \"add_inventory\", \"name\": \"Печиво\", \"quantity_text\": \"0,5 кг\", \"category\": "
    "\"Солодке та снеки\"}, "
    "{\"type\": \"add_inventory\", \"name\": \"Печиво\", \"quantity_text\": \"0,5 кг\", \"category\": "
    "\"Солодке та снеки\"}, "
    "{\"type\": \"discount_expense\", \"unit_price\": \"20\", \"unit_price_basis\": \"1 кг\", "
    "\"discount_percent\": \"50\", \"currency\": \"PLN\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад final-amount override (для «Печиво було зі знижкою, фінально заплатив 10 zł.» — ФАКТИЧНО "
    "СПЛАЧЕНА сума явно вказана словом «заплатив», незалежно від згадки знижки): {\"intent\": "
    "\"household_operations\", \"operations\": ["
    "{\"type\": \"add_expense\", \"amount\": \"10\", \"currency\": \"PLN\", \"category\": \"Продукти\", "
    "\"description\": \"Печиво\", \"expense_date\": \"2026-07-12\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад assumed_expense (для «Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 "
    "злотих.» — оригінальна ціна і знижка є, але немає слова «заплатили»/«вийшло»): {\"intent\": "
    "\"household_operations\", \"operations\": ["
    "{\"type\": \"assumed_expense\", \"description\": \"Візочок для дитини\", \"original_price\": \"3300\", "
    "\"discount_amount\": \"150\", \"currency\": \"PLN\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад final-amount override з контекстом (для «Ми купили дитяче ліжечко, яке на сайті коштувало 650, "
    "але ми знайшли його за 570.» — «знайшли за 570» це фактично сплачена сума, 650 — лише контекст): "
    "{\"intent\": \"household_operations\", \"operations\": ["
    "{\"type\": \"add_expense\", \"amount\": \"570\", \"currency\": \"PLN\", \"category\": \"Дім і рахунки\", "
    "\"description\": \"Дитяче ліжечко\", \"expense_date\": \"2026-07-12\", "
    "\"context_note\": \"Оригінальна/сайтова ціна 650 zł, куплено за 570 zł\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад безкоштовного товару (для «У нас також є автокрісло, але за нього ми нічого не заплатили.» — "
    "БЕЗ жодної суми, лише товар): {\"intent\": \"household_operations\", \"operations\": ["
    "{\"type\": \"add_inventory\", \"name\": \"Автокрісло\", \"quantity_text\": \"1\", \"category\": "
    "\"Інше їстівне\"}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад багатьох незалежних покупок в одному повідомленні (для «Ми купили візочок для дитини за 3300 "
    "злотих, але нам зробили знижку 150 злотих. Також купили дитяче ліжечко, яке на сайті коштувало 650, але "
    "ми знайшли його за 570. Купили комод, який коштував 627, але купили за 527. Ще є автокрісло, за яке ми "
    "нічого не заплатили. Ще купила подарунок для її сестри за 60, щоб подякувати за автокрісло.» — КОЖНА "
    "покупка обробляється незалежно своїм типом): {\"intent\": \"household_operations\", \"operations\": ["
    "{\"type\": \"assumed_expense\", \"description\": \"Візочок для дитини\", \"original_price\": \"3300\", "
    "\"discount_amount\": \"150\", \"currency\": \"PLN\"}, "
    "{\"type\": \"add_expense\", \"amount\": \"570\", \"currency\": \"PLN\", \"category\": \"Дім і рахунки\", "
    "\"description\": \"Дитяче ліжечко\", \"expense_date\": \"2026-07-12\", "
    "\"context_note\": \"Оригінальна/сайтова ціна 650 zł, куплено за 570 zł\"}, "
    "{\"type\": \"add_expense\", \"amount\": \"527\", \"currency\": \"PLN\", \"category\": \"Дім і рахунки\", "
    "\"description\": \"Комод\", \"expense_date\": \"2026-07-12\", "
    "\"context_note\": \"Оригінальна ціна 627 zł, куплено за 527 zł\"}, "
    "{\"type\": \"add_inventory\", \"name\": \"Автокрісло\", \"quantity_text\": \"1\", \"category\": "
    "\"Інше їстівне\"}, "
    "{\"type\": \"add_expense\", \"amount\": \"60\", \"currency\": \"PLN\", \"category\": \"Інше\", "
    "\"description\": \"Подарунок сестрі\", \"expense_date\": \"2026-07-12\"}"
    "], \"unresolved_fragments\": []}"
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


# An add_expense amount must be a number the user actually TYPED, never one
# Gemini computed (a discount percentage applied to a stated price, a sum of
# several mentioned prices, ...). "коштувало 20, але 50% знижки" must never
# silently become an invented expense of "10" — see
# _amount_literally_in_text's only caller, the add_expense branch of
# _validate_operations_detailed, for the full reasoning.
_NUMBER_TOKEN_RE = re.compile(r"\d+[.,]?\d*")


def _amount_literally_in_text(amount, source_text):
    """True if `amount` (a Decimal) appears as a literal number token
    somewhere in `source_text` — a genuine explicit amount ("Молоко 10 zł")
    always passes; a computed/derived one (a discount applied to a stated
    price, a sum of several prices) never does, since the computed number
    itself was never typed by the user. `source_text=None` (a caller that
    doesn't have it, e.g. _validate_operations' own pre-existing external
    callers) always returns True — never retroactively blocks a caller that
    can't provide the source text."""
    if source_text is None:
        return True
    if amount is None:
        return False
    for token in _NUMBER_TOKEN_RE.findall(source_text):
        try:
            token_value = Decimal(token.replace(",", "."))
        except InvalidOperation:
            continue
        if token_value == amount:
            return True
    return False


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


# =========================
# INVENTORY REPRESENTATION CLARIFICATION V2 — a conversational resolution
# for the ONE conflict shape apply_inventory_representation_guard above
# can't safely resolve on its own: an existing structured count ("шт.") row
# against an EXPLICIT (never inferred) incoming mass/volume quantity for
# the SAME product, in a Global Household Operation (add_inventory or
# consume_inventory). Never mass<->volume, never a text quantity, never an
# inferred incoming guess (that stays Inventory Quantity Clarification v1's
# job) — see inventory.detect_count_vs_mass_volume_conflict's own docstring
# for the exact narrow shape this fires on.
#
# Every function below is pure (no Telegram, no DB, no Gemini) — bot.py
# owns pending_inventory_representation_clarification (the RAM-only
# continuation state) and calls back into these for every choice/reply, the
# same split of responsibility as Inventory Quantity Clarification v1.
# =========================
_REPRESENTATION_V2_MASS_UNITS = {"г", "кг"}
_REPRESENTATION_V2_VOLUME_UNITS = {"л", "мл"}


def _build_consume_representation_conflict(item, value, unit):
    """A consume_inventory op whose requested mass/volume conflicts with an
    existing structured count ("шт.") row — Flow A. `item` is the existing
    inventory row (from the numbered snapshot), value/unit the requested
    consume quantity."""
    return {
        "kind": "consume",
        "canonical_name": item.get("canonical_name"), "category": item.get("category"),
        "name": item["name"],
        "existing": {
            "item_id": item["id"], "quantity_value": item["quantity_value"],
            "quantity_unit": item["quantity_unit"],
            "quantity_text": item.get("quantity_text") or _bot.format_quantity_display(item["quantity_value"], item["quantity_unit"]),
        },
        "requested_value": value, "requested_unit": unit,
        "requested_display": _bot.format_quantity_display(value, unit),
    }


def _build_add_representation_conflict(item, existing):
    """An add_inventory item whose explicit mass/volume quantity conflicts
    with an existing structured count ("шт.") row — Flow B. `item` is the
    already merged/deduplicated incoming item; `existing` is the
    conflicting row."""
    return {
        "kind": "add",
        "canonical_name": item.get("canonical_name"), "category": item.get("category"),
        "name": item["name"],
        "existing": {
            "item_id": existing["id"], "quantity_value": existing["quantity_value"],
            "quantity_unit": existing["quantity_unit"],
            "quantity_text": existing.get("quantity_text") or _bot.format_quantity_display(existing["quantity_value"], existing["quantity_unit"]),
        },
        "incoming_item": item,
        "incoming_value": item["quantity_value"], "incoming_unit": item["quantity_unit"],
        "incoming_display": item["quantity_text"],
    }


def format_representation_v2_consume_choice_message(conflict):
    """Flow A's first question — see module docstring above for the shape
    this fires on."""
    name = conflict["name"]
    existing_text = conflict["existing"]["quantity_text"]
    requested_text = conflict["requested_display"]
    return (
        f"У запасах є «{name} — {existing_text}», а ти хочеш списати {requested_text}.\n\n"
        "Що це означає?\n\n"
        "⚖️ Це частина наявного запасу\n"
        "📦 Це інший / не облікований продукт\n"
        "❌ Скасувати"
    )


def format_representation_v2_add_choice_message(conflict):
    """Flow B's first question."""
    name = conflict["name"]
    existing_text = conflict["existing"]["quantity_text"]
    incoming_text = conflict["incoming_display"]
    return (
        f"У запасах уже є «{name} — {existing_text}», а нова кількість — {incoming_text}.\n\n"
        f"Що означають ці {incoming_text}?\n\n"
        "📦 Це окрема упаковка — додати окремо\n"
        "⚖️ Це вага наявного запису — уточнити його\n"
        "❌ Скасувати"
    )


def format_representation_v2_total_quantity_question(conflict):
    """Flow A follow-up, after "⚖️ Це частина наявного запасу" — asks for
    the total mass/volume of the WHOLE existing stock (mass wording when
    the requested consume unit is mass, volume wording when it's volume)."""
    name = conflict["name"]
    if conflict["requested_unit"] in _REPRESENTATION_V2_MASS_UNITS:
        return f"Скільки важив увесь наявний запас «{name}»?\n\nНапиши, наприклад: «250 г»."
    return f"Скільки було всього наявного запасу «{name}»?\n\nНапиши, наприклад: «500 мл»."


_REPRESENTATION_V2_CONSUME_PART_ANSWERS = {
    "⚖️ це частина наявного запасу", "це частина наявного запасу", "частина наявного запасу",
}
_REPRESENTATION_V2_CONSUME_SKIP_ANSWERS = {
    "📦 це інший / не облікований продукт", "це інший / не облікований продукт",
    "інший / не облікований продукт", "інший продукт", "не облікований продукт",
}
_REPRESENTATION_V2_ADD_SEPARATE_ANSWERS = {
    "📦 це окрема упаковка — додати окремо", "це окрема упаковка — додати окремо", "окрема упаковка",
}
_REPRESENTATION_V2_ADD_RELABEL_ANSWERS = {
    "⚖️ це вага наявного запису — уточнити його", "це вага наявного запису — уточнити його", "вага наявного запису",
}


def parse_representation_v2_consume_choice(text):
    """Flow A's choice reply -> "part_of_existing"/"separate_product"/None
    (invalid) — fixed phrase set, never fuzzy matching or Gemini."""
    if not isinstance(text, str):
        return None
    normalized = text.strip().lower()
    if normalized in _REPRESENTATION_V2_CONSUME_PART_ANSWERS:
        return "part_of_existing"
    if normalized in _REPRESENTATION_V2_CONSUME_SKIP_ANSWERS:
        return "separate_product"
    return None


def parse_representation_v2_add_choice(text):
    """Flow B's choice reply -> "separate_package"/"relabel_existing"/None
    (invalid) — fixed phrase set, never fuzzy matching or Gemini."""
    if not isinstance(text, str):
        return None
    normalized = text.strip().lower()
    if normalized in _REPRESENTATION_V2_ADD_SEPARATE_ANSWERS:
        return "separate_package"
    if normalized in _REPRESENTATION_V2_ADD_RELABEL_ANSWERS:
        return "relabel_existing"
    return None


def validate_representation_v2_total_quantity(conflict, total_value, total_unit):
    """Validate a Flow A "скільки важив увесь запас" reply against the
    active consume-side conflict. Returns ("ok", remaining_value,
    remaining_unit) or ("invalid", None, None). total_unit must be in the
    SAME mass/volume group as the requested consume unit (never "шт.",
    never the other group), and total_value must be strictly greater than
    what's being consumed — never equal, never less."""
    requested_unit = conflict["requested_unit"]
    requested_value = conflict["requested_value"]
    same_group = (
        (requested_unit in _REPRESENTATION_V2_MASS_UNITS and total_unit in _REPRESENTATION_V2_MASS_UNITS)
        or (requested_unit in _REPRESENTATION_V2_VOLUME_UNITS and total_unit in _REPRESENTATION_V2_VOLUME_UNITS)
    )
    if not same_group or total_value <= requested_value:
        return "invalid", None, None
    kind, remaining, remaining_unit = _bot._resolve_consumption(total_value, total_unit, requested_value, requested_unit)
    if kind != "ok":
        return "invalid", None, None
    return "ok", remaining, remaining_unit


def resolve_representation_v2_consume_skip(conflict):
    """Flow A "📦 Це інший / не облікований продукт" choice: the existing
    row is untouched, nothing is consumed — a pure preview-only resolution
    entry, no DB effect at all."""
    return {
        "mode": "skip_consume",
        "item_id": conflict["existing"]["item_id"],
        "canonical_name": conflict["canonical_name"], "category": conflict["category"], "name": conflict["name"],
        "source_value": conflict["existing"]["quantity_value"], "source_unit": conflict["existing"]["quantity_unit"],
        "source_display": conflict["existing"]["quantity_text"],
        "consume_value": conflict["requested_value"], "consume_unit": conflict["requested_unit"],
        "consume_display": conflict["requested_display"],
    }


def resolve_representation_v2_consume_relabel(conflict, total_value, total_unit, remaining_value, remaining_unit):
    """Flow A "⚖️ Це частина наявного запасу" choice, after a valid total
    quantity reply: relabel the existing "шт." row to the given total, then
    consume the originally-requested amount from it. Returns
    (resolution_entry, consume_change_entry) — the consume_change entry
    feeds into the SAME consume_updates/consume_changes mechanism every
    other partial consumption already uses (never a second write path);
    the resolution entry is preview-only bookkeeping."""
    total_display = _bot.format_quantity_display(total_value, total_unit)
    remaining_display = _bot.format_quantity_display(remaining_value, remaining_unit)
    resolution = {
        "mode": "relabel_and_consume",
        "item_id": conflict["existing"]["item_id"],
        "canonical_name": conflict["canonical_name"], "category": conflict["category"], "name": conflict["name"],
        "source_value": conflict["existing"]["quantity_value"], "source_unit": conflict["existing"]["quantity_unit"],
        "source_display": conflict["existing"]["quantity_text"],
        "resolved_value": total_value, "resolved_unit": total_unit, "resolved_display": total_display,
        "consume_value": conflict["requested_value"], "consume_unit": conflict["requested_unit"],
        "consume_display": conflict["requested_display"],
        "remaining_value": remaining_value, "remaining_unit": remaining_unit, "remaining_display": remaining_display,
    }
    consume_change = {
        "item_id": conflict["existing"]["item_id"], "name": conflict["name"],
        "old_value": conflict["existing"]["quantity_value"], "old_unit": conflict["existing"]["quantity_unit"],
        "old_display": conflict["existing"]["quantity_text"],
        "new_value": float(remaining_value), "new_unit": remaining_unit, "new_display": remaining_display,
        "will_remove": False,
    }
    return resolution, consume_change


def resolve_representation_v2_add_separate(conflict):
    """Flow B "📦 Це окрема упаковка" choice: applies the EXISTING separate-
    representation behavior unchanged — the "шт." row stays untouched, the
    incoming item becomes its own row."""
    item = dict(conflict["incoming_item"])
    item["_representation_outcome"] = "separate"
    item["_representation_note"] = _bot.format_representation_separate_warning(
        conflict["name"], conflict["existing"]["quantity_text"], conflict["incoming_display"],
    )
    return item


def resolve_representation_v2_add_relabel(conflict):
    """Flow B "⚖️ Це вага наявного запису" choice: corrects the existing
    row's representation to the incoming quantity — no new row, treated as
    a representation fix, not a purchase. Returns (resolution_entry,
    consume_change_entry) — same split as resolve_representation_v2_consume_relabel."""
    resolved_display = conflict["incoming_display"]
    resolution = {
        "mode": "relabel_existing",
        "item_id": conflict["existing"]["item_id"],
        "canonical_name": conflict["canonical_name"], "category": conflict["category"], "name": conflict["name"],
        "source_value": conflict["existing"]["quantity_value"], "source_unit": conflict["existing"]["quantity_unit"],
        "source_display": conflict["existing"]["quantity_text"],
        "resolved_value": conflict["incoming_value"], "resolved_unit": conflict["incoming_unit"],
        "resolved_display": resolved_display,
    }
    consume_change = {
        "item_id": conflict["existing"]["item_id"], "name": conflict["name"],
        "old_value": conflict["existing"]["quantity_value"], "old_unit": conflict["existing"]["quantity_unit"],
        "old_display": conflict["existing"]["quantity_text"],
        "new_value": conflict["incoming_value"], "new_unit": conflict["incoming_unit"], "new_display": resolved_display,
        "will_remove": False,
    }
    return resolution, consume_change


def representation_v2_targets_still_fresh(resolutions, fresh_inventory_items):
    """True iff every representation resolution's target row still matches
    its captured source snapshot exactly — re-checked against a FRESH
    inventory snapshot right before building the final combined preview,
    never trusted from when the clarification started."""
    fresh_by_id = {item["id"]: item for item in fresh_inventory_items}
    for r in resolutions:
        fresh = fresh_by_id.get(r["item_id"])
        if fresh is None:
            return False
        if fresh.get("quantity_value") != r["source_value"] or fresh.get("quantity_unit") != r["source_unit"]:
            return False
    return True


def _legacy_single_expense(new_expenses):
    """Backward-compat derived value for the "new_expense" (singular) payload
    key every pre-Multi-Expense-Batch caller/test still reads: the one
    expense dict when new_expenses has exactly one entry, else None (0 or
    2+ entries — a multi-expense batch has no single "the" expense to show
    under the old key). Never the other way around: new_expenses (the list)
    is always the authoritative value; this is purely a read-only view onto
    it, so the two can never drift out of sync."""
    return new_expenses[0] if len(new_expenses) == 1 else None


def _validate_operations_detailed(router_result, inventory_items, recent_expenses, now, alias_map=None, source_text=None):
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
          snapshot + display label), expense_notes (Purchase Event Planner
          V1 — [str, ...], possibly empty: non-blocking warnings that a
          price/amount was mentioned but wasn't safe to turn into an
          add_expense, e.g. a discount; rendered by format_preview, never
          written to the database).
      ("unresolved", [fragment_str, ...]) — blocks the entire result.
      ("invalid", [reason_str, ...]) — blocks the entire result.
      ("ambiguous_expense", [note_str, ...]) — Purchase Event Planner V1,
          safe response B: the ONLY content recognized in the message was
          an ambiguous price (no item/quantity/other action at all) — a
          clarification, never a blocking error, never an invented amount.
      ("clarify", {"item_name", "canonical_name", "category", "existing_items",
                    "add_shopping_items", "add_inventory_items",
                    "consume_changes", "new_expense", "delete_expense"})
          — an inferred incoming inventory quantity conflicts with an
          existing row's representation; blocks the entire result (never a
          partial apply). Carries every operation already validated up to
          that point, so a caller can resolve the ambiguous quantity later
          and re-run apply_inventory_representation_guard without redoing
          any of this work or re-calling Gemini.
      ("clarify_representation", {"conflict", "queue", "add_shopping_items",
                    "add_inventory_items", "inventory_merge_targets",
                    "consume_changes", "new_expenses", "new_expense",
                    "delete_expense"})
          — Inventory Representation Clarification V2: a structured count
          ("шт.") row conflicts with an explicit incoming mass/volume
          quantity for the same product (add or consume side). Blocks the
          entire result (never a partial apply); "conflict" is the first
          one to ask about, "queue" holds any others found in the same
          message. Every other operation already validated is carried
          through untouched, so a caller can resolve conflicts one at a
          time and build the final combined preview without redoing any of
          this work or re-calling Gemini.
      ("none", None)
    """
    fragments = router_result.get("unresolved_fragments")
    if isinstance(fragments, list):
        cleaned_fragments = _dedupe_preserve_order(str(f).strip() for f in fragments if str(f).strip())
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
    consume_representation_conflicts = []
    used_inventory_numbers = set()
    new_expenses = []
    expense_notes = []
    discount_expense_ops = []
    delete_expense = None
    delete_expense_count = 0

    # Purchase Event Planner V1 — see _DISCOUNT_MARKER_RE's own comment: a
    # discount/percentage mention anywhere in the ORIGINAL message makes
    # every add_expense op in this batch ambiguous, regardless of whether
    # Gemini already followed the prompt's own "ambiguous_expense" instruction
    # — UNLESS has_final_amount_marker overrides it (Safe Discount Calculation
    # V1, example C: "фінально заплатив 10 zł" is a real final amount even
    # when a discount is mentioned elsewhere in the same message).
    has_discount_marker = bool(source_text) and bool(_DISCOUNT_MARKER_RE.search(source_text))
    has_final_amount_marker = bool(source_text) and bool(_FINAL_AMOUNT_MARKER_RE.search(source_text))

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
            if _bot.detect_count_vs_mass_volume_conflict(cur_value, cur_unit, value, unit, False):
                # Inventory Representation Clarification V2 (Flow A) — a
                # structured count row vs an explicit mass/volume consume
                # request is a conversational conflict, not a hard block;
                # deferred here (never added to reasons/consume_changes) so
                # the rest of the batch survives while this ONE item is
                # asked about later (see _validate_operations_detailed's
                # post-merge representation_conflict_queue handling below).
                used_inventory_numbers.add(num)
                consume_representation_conflicts.append(_build_consume_representation_conflict(item, value, unit))
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
            # Assumption-Based Purchase Preview V1: every failure below is a
            # non-blocking note attached to THIS item alone (never reasons,
            # which would block the entire batch — including every OTHER
            # item's perfectly fine expense/inventory add in the same multi-
            # item message; see the work order's rule 5/6). The amount-
            # literal check is still fully enforced — a note, not silence,
            # is what replaces a fabricated/mismatched amount now.
            description = _normalize_display_name(expenses._clean_expense_description(op.get("description"))) or "Покупка"
            currency = op.get("currency")
            if currency not in (None, "PLN"):
                expense_notes.append(f"«{description}» — не можу безпечно визначити валюту витрати.")
                continue
            amount = expenses._parse_expense_amount(op.get("amount"))
            if amount is None:
                expense_notes.append(
                    f"«{description}» — не можу безпечно визначити суму витрати. Напиши точну сплачену "
                    "суму, якщо хочеш додати цю витрату."
                )
                continue
            if not _amount_literally_in_text(amount, source_text):
                # The amount doesn't appear anywhere in what the user
                # actually typed — Gemini computed it (a discount applied to
                # a stated price, a sum of several mentioned prices, ...)
                # rather than reading it. Never silently invent a price.
                expense_notes.append(
                    f"«{description}» — сума неоднозначна ({expenses._format_expense_amount(amount)}?). "
                    "Напиши точну сплачену суму, якщо хочеш додати цю витрату."
                )
                continue
            expense_date = expenses._validate_expense_date(op.get("expense_date"), now=now)
            if expense_date is None:
                expense_notes.append(f"«{description}» — не можу безпечно визначити дату витрати.")
                continue
            category, category_was_defaulted = expenses._validate_expense_category(op.get("category"))
            new_expenses.append({
                "amount": amount, "currency": "PLN", "category": category,
                "category_was_defaulted": category_was_defaulted, "description": description,
                "expense_date": expense_date, "context_note": _clean_context_note(op.get("context_note")),
            })

        elif op_type == "assumed_expense":
            # Assumption-Based Purchase Preview V1 (see the prompt's type 9)
            # — a flat original-price-minus-discount computation, used when
            # the message states a price and a discount WITHOUT a clearer
            # final-paid-amount phrasing (see add_expense above/the prompt's
            # own "prefer a clear final amount" instruction — Gemini should
            # use plain add_expense whenever wording is that clear). Every
            # piece (original_price, discount_amount/discount_percent) must
            # be a literal number the user actually typed; the SUBTRACTION
            # ITSELF is always done here in Python, never trusted from
            # Gemini — see _format_assumption_note's own docstring. Any
            # missing/unparseable/non-literal piece becomes a non-blocking
            # note, exactly like add_expense above, never a batch-wide block.
            description = _normalize_display_name(expenses._clean_expense_description(op.get("description"))) or "Покупка"
            original_price = expenses._parse_expense_amount(op.get("original_price"))
            if original_price is None or not _amount_literally_in_text(original_price, source_text):
                expense_notes.append(
                    f"«{description}» — сума неоднозначна. Напиши точну сплачену суму, якщо хочеш додати "
                    "цю витрату."
                )
                continue
            discount_amount = None
            if op.get("discount_amount") is not None:
                discount_amount = expenses._parse_expense_amount(op.get("discount_amount"))
            discount_percent = None
            if op.get("discount_percent") is not None:
                discount_percent = _parse_percent(op.get("discount_percent"))

            final_amount = None
            assumption_note = None
            if (
                discount_amount is not None and _amount_literally_in_text(discount_amount, source_text)
                and discount_amount < original_price
            ):
                final_amount = (original_price - discount_amount).quantize(Decimal("0.01"))
                assumption_note = _format_assumption_note(
                    original_price, expenses._format_expense_amount(discount_amount), final_amount,
                )
            elif discount_percent is not None and _amount_literally_in_text(discount_percent, source_text):
                final_amount = (
                    original_price * (Decimal("100") - discount_percent) / Decimal("100")
                ).quantize(Decimal("0.01"))
                percent_display = _bot.format_quantity_display(discount_percent, None)
                assumption_note = _format_assumption_note(original_price, f"{percent_display}%", final_amount)

            if final_amount is None:
                expense_notes.append(
                    f"«{description}» — сума неоднозначна. Напиши точну сплачену суму, якщо хочеш додати "
                    "цю витрату."
                )
                continue

            expense_date = expenses._validate_expense_date(op.get("expense_date"), now=now) or now.date()
            category, category_was_defaulted = expenses._validate_expense_category(op.get("category"))
            new_expenses.append({
                "amount": final_amount, "currency": "PLN", "category": category,
                "category_was_defaulted": category_was_defaulted, "description": description,
                "expense_date": expense_date, "assumption_note": assumption_note,
            })

        elif op_type == "discount_expense":
            # Safe Discount Calculation V1 (see the prompt's type 8) — every
            # piece is validated INDIVIDUALLY here (unit_price/discount_
            # percent must be literal numbers actually typed by the user,
            # unit_price_basis must parse as an explicit structured
            # quantity); the actual multiply-by-purchased-quantity math only
            # happens once add_inventory_items is FINAL (after merge +
            # representation guard, below) — see discount_expense_ops'
            # processing near the end of this function. A piece that fails
            # here becomes a non-blocking note, exactly like ambiguous_
            # expense — never blocks the rest of the batch, never invents
            # a number.
            unit_price = expenses._parse_expense_amount(op.get("unit_price"))
            discount_percent = _parse_percent(op.get("discount_percent"))
            basis_value, basis_unit = parse_structured_quantity(op.get("unit_price_basis") or "")
            if (
                unit_price is None or not _amount_literally_in_text(unit_price, source_text)
                or discount_percent is None or not _amount_literally_in_text(discount_percent, source_text)
                or basis_value is None
            ):
                expense_notes.append(
                    "У повідомленні є розрахунок ціни зі знижкою, але я не можу безпечно його застосувати. "
                    "Напиши точну сплачену суму, якщо хочеш додати цю витрату."
                )
                continue
            discount_expense_ops.append({
                "unit_price": unit_price, "discount_percent": discount_percent,
                "basis_value": basis_value, "basis_unit": basis_unit,
            })

        elif op_type == "ambiguous_expense":
            # Gemini's own signal (see the prompt's type 7) that a price/
            # amount was mentioned but can't be safely turned into
            # add_expense — never blocking, never an invented amount, just
            # a note attached to whatever else in the message DID validate.
            note = op.get("note")
            if isinstance(note, str) and note.strip():
                expense_notes.append(note.strip())
            else:
                expense_notes.append("Сума покупки неоднозначна — уточни, скільки фактично сплачено.")

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
        return "invalid", _dedupe_preserve_order(reasons)

    add_shopping_items = _bot._auto_merge_in_place(add_shopping_raw) if add_shopping_raw else []
    add_inventory_items = _bot._auto_merge_in_place(add_inventory_raw) if add_inventory_raw else []

    # Inventory Representation Clarification V2 (Flow B) — pull out any
    # add_inventory item whose explicit mass/volume quantity conflicts with
    # a SINGLE existing structured count ("шт.") row, BEFORE the v1 guard
    # ever sees it, so v1 never gets the chance to silently file it as
    # "separate". Several candidate rows (a genuinely ambiguous case) is
    # deliberately left alone here — detect_add_representation_v2_conflict
    # itself returns None for anything but exactly one candidate.
    add_representation_conflicts = []
    filtered_add_inventory_items = []
    for item in add_inventory_items:
        existing = _bot.detect_add_representation_v2_conflict(
            inventory_items, item.get("canonical_name"), item.get("category"),
            item.get("quantity_value"), item.get("quantity_unit"), item.get("quantity_inferred", False),
            name_normalizer=_bot.canonicalize_name,
        )
        if existing is not None:
            add_representation_conflicts.append(_build_add_representation_conflict(item, existing))
        else:
            filtered_add_inventory_items.append(item)

    # Inventory Representation Guard v1 — runs on the FINAL (already
    # RAM-deduplicated) add_inventory_items, once per distinct product, so a
    # message mentioning the same product twice is checked against the live
    # inventory exactly once, using its combined quantity.
    guard_kind, guard_result = apply_inventory_representation_guard(filtered_add_inventory_items, inventory_items)
    if guard_kind == "clarify" and add_representation_conflicts:
        # A genuinely complex message — a v1 ambiguity elsewhere AND a V2-
        # shaped conflict at the same time. Never attempt both conversations
        # at once: fall back to the guard's ORIGINAL, unfiltered view (the
        # V2 item behaves exactly as it did before this feature existed — a
        # silent "separate" add) so the v1 clarify path sees precisely what
        # it always has, and nothing from the batch is lost.
        guard_kind, guard_result = apply_inventory_representation_guard(add_inventory_items, inventory_items)
        add_representation_conflicts = []
    if guard_kind == "clarify":
        # discount_expense_ops isn't resolvable yet here — add_inventory_
        # items may still change once this clarification is answered, so
        # any collected pieces are simply dropped (never computed against a
        # quantity that could still change); expense_notes already carries
        # every individually-invalid piece's own note from the loop above,
        # so nothing about "the amount needs clarification" is silently lost.
        return "clarify", {
            **guard_result,
            "add_shopping_items": add_shopping_items,
            "add_inventory_items": add_inventory_items,
            "consume_changes": consume_changes,
            "new_expenses": new_expenses,
            "new_expense": _legacy_single_expense(new_expenses),
            "delete_expense": delete_expense,
            "expense_notes": _dedupe_preserve_order(expense_notes),
        }
    add_inventory_items, inventory_merge_targets = guard_result

    representation_conflict_queue = consume_representation_conflicts + add_representation_conflicts
    if representation_conflict_queue:
        # Same reasoning as the guard_kind == "clarify" branch above.
        return "clarify_representation", {
            "conflict": representation_conflict_queue[0],
            "queue": representation_conflict_queue[1:],
            "add_shopping_items": add_shopping_items,
            "add_inventory_items": add_inventory_items,
            "inventory_merge_targets": inventory_merge_targets,
            "consume_changes": consume_changes,
            "new_expenses": new_expenses,
            "new_expense": _legacy_single_expense(new_expenses),
            "delete_expense": delete_expense,
            "expense_notes": _dedupe_preserve_order(expense_notes),
        }

    # Safe Discount Calculation V1 — add_inventory_items is FINAL now (past
    # merge + both representation-guard passes), so it's finally safe to
    # multiply a discount_expense op's per-unit price by how much of that
    # product was actually bought. Deliberately as narrow/conservative as
    # Price Clarification V1's own single-item rule: never guess which item
    # or which of several discounts a calculation belongs to.
    expense_calculation_note = None
    if discount_expense_ops:
        if new_expenses or len(add_inventory_items) != 1 or len(discount_expense_ops) != 1:
            for _ in discount_expense_ops:
                expense_notes.append(
                    "У повідомленні є розрахунок ціни зі знижкою, але я не можу безпечно застосувати його "
                    "до одного товару. Напиши точну сплачену суму, якщо хочеш додати цю витрату."
                )
        else:
            d = discount_expense_ops[0]
            item = add_inventory_items[0]
            multiplier = preview_editing.compute_quantity_multiplier(
                item.get("quantity_value"), item.get("quantity_unit"), d["basis_value"], d["basis_unit"],
            )
            if multiplier is None or item.get("quantity_inferred"):
                # Incompatible units (price-per-unit vs. quantity bought), or
                # the bought quantity is itself only a guessed default —
                # never invent a multiplier (see compute_quantity_
                # multiplier's own "never guessed" contract).
                expense_notes.append(
                    "Не можу безпечно порахувати суму: одиниця ціни не збігається з кількістю товару. "
                    "Напиши точну сплачену суму, якщо хочеш додати цю витрату."
                )
            else:
                discounted_unit_price = (
                    d["unit_price"] * (Decimal("100") - d["discount_percent"]) / Decimal("100")
                ).quantize(Decimal("0.01"))
                final_amount = (discounted_unit_price * multiplier).quantize(Decimal("0.01"))
                new_expenses.append({
                    "amount": final_amount, "currency": "PLN", "category": "Продукти",
                    "category_was_defaulted": True, "description": item["name"],
                    "expense_date": now.date(),
                })
                expense_calculation_note = _format_discount_calculation_note(
                    d["unit_price"], d["discount_percent"], d["basis_unit"],
                    discounted_unit_price, multiplier, final_amount,
                )

    if has_discount_marker and not expense_notes and not new_expenses:
        # A discount was mentioned but produced no add_expense, no
        # discount_expense calculation, AND no ambiguous_expense note at all
        # (e.g. Gemini silently dropped the price instead of following the
        # prompt's instructions) — still surface it, so "expense amount
        # needs clarification" is never silently lost.
        expense_notes.append(
            "У повідомленні згадано знижку — суму витрати не додано. Напиши точну сплачену суму, якщо "
            "хочеш додати цю витрату."
        )

    expense_notes = _dedupe_preserve_order(expense_notes)

    if not add_shopping_items and not add_inventory_items and not consume_changes and not new_expenses and not delete_expense:
        if expense_notes:
            # Purchase Event Planner V1, safe response B: the ONLY thing in
            # this message was an ambiguous price (no item/quantity/other
            # action recognized at all) — ask for clarification instead of
            # a blocking "invalid" error, and never invent/compute anything.
            return "ambiguous_expense", expense_notes
        return "invalid", ["Не знайшов жодної дії для виконання."]

    return "ok", {
        "add_shopping_items": add_shopping_items,
        "add_inventory_items": add_inventory_items,
        "consume_changes": consume_changes,
        "new_expenses": new_expenses,
        "new_expense": _legacy_single_expense(new_expenses),
        "delete_expense": delete_expense,
        "inventory_merge_targets": inventory_merge_targets,
        "expense_notes": expense_notes,
        "expense_calculation_note": expense_calculation_note,
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


def _expense_entry_note_lines(new_expense_entry):
    """Assumption-Based Purchase Preview V1 — the per-item note (if any)
    shown directly under ONE expense line, indented, never bulleted (so it
    reads as a sub-note of the line above it, not a new list item). At most
    ONE of "assumption_note" (a computed Припущення — see _format_
    assumption_note) or "context_note" (Gemini's own free-text context on a
    plain add_expense, e.g. an original-vs-paid comparison) is ever present
    on a single entry; deliberately per-item rather than one bulk warning
    footer, so multiple assumptions in the same multi-item preview stay
    attached to the item they actually describe instead of reading as
    generic, hard-to-place noise."""
    note = new_expense_entry.get("assumption_note") or new_expense_entry.get("context_note")
    return [f"  {note}"] if note else []


def format_preview(payload, header="План змін:", extra_note=None):
    """`header` defaults to the original preview header; Preview Edit V2
    passes "Оновив план:" instead when re-rendering an edited
    pending_global_household preview, without changing anything else about
    the layout (same convention as inventory.format_inventory_transform_
    preview's own `header` param).

    `extra_note` (Price Clarification V1) is an optional plain-text line —
    e.g. "5 zł за 0,5 кг × 2", explaining how a per-unit price clarification
    was turned into the expense amount now shown above — rendered in the
    same slot as expense_notes but WITHOUT the "⚠️" prefix (this is a
    resolved calculation, not an unresolved warning). None (the default)
    renders nothing extra, unchanged from every caller before this param
    existed."""
    lines = [header]

    if payload["add_shopping_items"]:
        lines.append("")
        lines.append("🛒 Покупки")
        for item in payload["add_shopping_items"]:
            lines.append(_format_new_item_line(item))

    representation_resolutions = payload.get("inventory_representation_resolutions") or []
    if payload["add_inventory_items"] or payload["consume_changes"] or representation_resolutions:
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
        # Inventory Representation Clarification V2 resolutions — rendered
        # BEFORE the normal consume_changes loop below (which skips any
        # entry tagged "_from_representation_resolution": those are the
        # SAME relabel_and_consume/relabel_existing entries, already shown
        # here with their own two-line/one-line wording, never twice).
        for r in representation_resolutions:
            if r["mode"] == "relabel_existing":
                lines.append(
                    f"• {r['name']} — {r['source_display']} → {r['resolved_display']} "
                    "(уточнено, без додавання нового товару)"
                )
            elif r["mode"] == "relabel_and_consume":
                lines.append(f"• {r['name']} — {r['source_display']} → {r['resolved_display']}")
                lines.append(
                    f"• {r['name']} — {r['resolved_display']} − {r['consume_display']} → "
                    f"буде {r['remaining_display']}"
                )
            elif r["mode"] == "skip_consume":
                lines.append(
                    f"⚠️ {r['name']} — {r['consume_display']} не списувати: "
                    "це окремий продукт, якого немає у запасах."
                )
        for c in payload["consume_changes"]:
            if c.get("_from_representation_resolution"):
                continue
            label = c["name"]
            if c["old_display"]:
                label += f" — {c['old_display']}"
            if c["will_remove"]:
                lines.append(f"• {label} → буде прибрано із запасів")
            else:
                lines.append(f"• {label} → {c['name']} — {c['new_display']}")

    new_expenses = payload.get("new_expenses")
    if new_expenses is None:
        legacy_new_expense = payload.get("new_expense")
        new_expenses = [legacy_new_expense] if legacy_new_expense else []
    delete_expense = payload["delete_expense"]
    if new_expenses or delete_expense:
        lines.append("")
        lines.append("💸 Витрати")
        if len(new_expenses) == 1:
            # Unchanged single-expense formatting — every existing caller/
            # test of the one-expense shape keeps seeing exactly this.
            ne = new_expenses[0]
            amount_display = expenses._format_expense_amount(ne["amount"])
            desc = ne["description"] or ne["category"]
            lines.append(f"• Додати {desc} — {amount_display}")
            lines.append(f"• Категорія: {ne['category']}")
            lines.extend(_expense_entry_note_lines(ne))
        else:
            for ne in new_expenses:
                amount_display = expenses._format_expense_amount(ne["amount"])
                desc = ne["description"] or ne["category"]
                lines.append(f"• {desc} — {amount_display}")
                lines.extend(_expense_entry_note_lines(ne))
        if delete_expense:
            lines.append(f"• Видалити {delete_expense['display']} — {delete_expense['amount_display']}")

    # Purchase Event Planner V1 — a non-blocking warning that a price/amount
    # was mentioned somewhere in the message but wasn't safe to turn into an
    # expense (a discount, a computed/derived sum, ...). Never blocks the
    # rest of the preview; the user can still confirm everything else as-is.
    expense_notes = payload.get("expense_notes") or []
    if expense_notes:
        lines.append("")
        for note in expense_notes:
            lines.append(f"⚠️ {note}")

    if extra_note:
        lines.append("")
        lines.append(extra_note)

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


def format_ambiguous_expense_message(notes):
    """Purchase Event Planner V1, safe response B — used when a message's
    ONLY content was an ambiguous price (no item/quantity/other action
    recognized at all): a clarification, never a blocking error and never
    an invented amount. Nothing is written; the user's next message can
    freely retry with an explicit paid amount."""
    lines = ["Не зовсім зрозуміло, скільки фактично сплачено.", ""]
    for note in notes:
        lines.append(f"• {note}")
    lines.append("")
    lines.append("Напиши точну суму, яку сплачено, якщо хочеш додати цю витрату.")
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
    return _validate_operations_detailed(
        router_result, inventory_items, recent_expenses, now, alias_map=alias_map, source_text=text,
    )


# =========================
# GLOBAL EXPLICIT ADD v1 — a message with an EXPLICIT, unambiguous
# destination phrase ("Додай до покупок ...", "Додай в запаси ...") adds to
# that list regardless of which menu is open. Destination is decided by
# Python BEFORE any Gemini call — deterministically, via the fixed phrase
# list below, never fuzzy matching — so Gemini is only ever asked to
# extract a plain item list from the ALREADY-cleaned text (destination
# phrase stripped) via EXPLICIT_ADD_ITEM_PROMPT. That prompt's JSON contract
# has no operation "type", no expense, no consumption, no deletion field at
# all — so there is structurally nothing for a misbehaving Gemini response
# to smuggle in beyond a malformed item (which _validate_explicit_add_items
# already rejects); build_explicit_add_preview always assigns every parsed
# item to the ONE destination bucket Python already decided.
# =========================
# Shared add-verb alternation — the imperative "Додай"/"Додайте" AND the
# infinitive "Додати" (a common live-bug shape: bot-preview-style action
# lines like "Додати Тестовий чай — 1 шт." use the infinitive, not the
# imperative). Every "^додай(?:те)?" anchor below also accepts "додати" for
# this reason.
_ADD_VERB_RE = r"(?:додай(?:те)?|додати)"

# A leading bullet ("•"/"-") plus add-verb on its OWN line — only ever
# stripped from the 2nd+ line of an already-destination-resolved multi-line
# item text (never the first line, which detect_explicit_add_destination/
# detect_bare_add/detect_header_add_destination already consumed via their
# own anchored match) — so a pasted-back multi-line preview like
# "Додати Тестовий чай — 1 шт.\nДодати Зелений чай — 1 шт." reaches Gemini
# as plain item lines instead of noisy repeated verbs.
_LINE_ADD_VERB_RE = re.compile(r"^[•\-]?\s*" + _ADD_VERB_RE + r"\s+", re.IGNORECASE)


def _strip_line_add_verbs(text):
    """Strip a leading bullet+add-verb from EACH line of `text` (see
    _LINE_ADD_VERB_RE) — a line with no such marker is returned unchanged.
    Pure text cleanup only, never touches item content itself."""
    return "\n".join(_LINE_ADD_VERB_RE.sub("", line, count=1) for line in text.splitlines())


# Every recognized destination phrase, shared between the LEADING form
# ("Додай до покупок молоко") and the TRAILING form ("Додай молоко до
# покупок") below — a live voice-transcript bug showed users very often
# say the destination LAST, not first, so both orders must recognize the
# exact same fixed phrase list.
_SHOPPING_DESTINATION_PHRASES = (
    r"до\s+покупок|в\s+покупки|у\s+покупки|"
    r"до\s+списку\s+покупок|в\s+список\s+покупок|у\s+список\s+покупок"
)
_INVENTORY_DESTINATION_PHRASES = r"до\s+запасів|в\s+запаси|у\s+запаси"

_EXPLICIT_SHOPPING_DESTINATION_RE = re.compile(
    r"^" + _ADD_VERB_RE + r"\s+(?:" + _SHOPPING_DESTINATION_PHRASES + r")[:\-]?\s+",
    re.IGNORECASE,
)
_EXPLICIT_INVENTORY_DESTINATION_RE = re.compile(
    r"^" + _ADD_VERB_RE + r"\s+(?:" + _INVENTORY_DESTINATION_PHRASES + r")[:\-]?\s+",
    re.IGNORECASE,
)

# TRAILING form — same add-verb, then the item text (non-greedy, so the
# destination phrase always wins the shortest possible match), then one of
# the SAME destination phrases, optionally followed by voice-transcript-
# style trailing punctuation (".", ",", "!", "?") and whitespace to end of
# string. Single-line only (see detect_explicit_add_destination's own
# multi-line guard) — a trailing-phrase match against a multi-line paste-
# back would be ambiguous, and that shape is already handled by
# detect_header_add_destination instead.
_TRAILING_SHOPPING_DESTINATION_RE = re.compile(
    r"^" + _ADD_VERB_RE + r"\s+(?P<items>.+?)\s+(?:" + _SHOPPING_DESTINATION_PHRASES + r")\s*[.,!?]*\s*$",
    re.IGNORECASE,
)
_TRAILING_INVENTORY_DESTINATION_RE = re.compile(
    r"^" + _ADD_VERB_RE + r"\s+(?P<items>.+?)\s+(?:" + _INVENTORY_DESTINATION_PHRASES + r")\s*[.,!?]*\s*$",
    re.IGNORECASE,
)

# A standalone destination HEADER as the very first line of a multi-line
# message — the exact main-menu button labels ("🛒 Покупки"/"🧊 Запаси") or a
# bare "до покупок"/"до запасів" line — followed by item lines below it.
# Deliberately requires the header to be the WHOLE first line (never a
# substring of a longer line) so it can never misfire on an ordinary
# sentence that happens to mention "покупки"/"запаси" in passing.
_HEADER_SHOPPING_LINE_RE = re.compile(r"^🛒\s*Покупки\s*$|^до\s+покупок\s*$", re.IGNORECASE)
_HEADER_INVENTORY_LINE_RE = re.compile(r"^🧊\s*Запаси\s*$|^до\s+запасів\s*$", re.IGNORECASE)

# U+FE0F/U+FE0E (emoji/text presentation variation selectors) — a Telegram
# client may or may not append one to "🛒"/"🧊" depending on client/cache
# (same reasoning as message_dispatcher.strip_variation_selectors, which
# this module can't import without creating a dependency on the dispatch
# layer — a two-codepoint local translate table is simpler than a cross-
# module call for this one narrow header-line comparison).
_HEADER_VARIATION_SELECTORS = "︎️"


def _strip_header_variation_selectors(line):
    return line.translate({ord(ch): None for ch in _HEADER_VARIATION_SELECTORS})


def detect_explicit_add_destination(text):
    """Deterministically detect an EXPLICIT shopping/inventory destination
    phrase in `text` — the fixed phrase list only (see module docstring
    above), never fuzzy matching or guessing at intent. Recognizes the
    phrase either LEADING ("Додай до покупок молоко і сир") or, as a
    fallback, TRAILING ("Додай молоко і сир до покупок.") — see
    _TRAILING_SHOPPING_DESTINATION_RE/_TRAILING_INVENTORY_DESTINATION_RE's
    own comment for why the trailing form exists.

    Returns ("add_shopping"|"add_inventory", item_text) with the phrase
    already stripped off, or (None, None) if no exact phrase matches, or
    if nothing but the phrase itself was sent (no item text to parse).
    """
    if not isinstance(text, str):
        return None, None
    stripped = text.strip()
    if not stripped:
        return None, None
    match = _EXPLICIT_SHOPPING_DESTINATION_RE.match(stripped)
    if match:
        rest = _strip_line_add_verbs(stripped[match.end():].strip())
        return ("add_shopping", rest) if rest else (None, None)
    match = _EXPLICIT_INVENTORY_DESTINATION_RE.match(stripped)
    if match:
        rest = _strip_line_add_verbs(stripped[match.end():].strip())
        return ("add_inventory", rest) if rest else (None, None)

    if "\n" not in stripped:
        match = _TRAILING_SHOPPING_DESTINATION_RE.match(stripped)
        if match:
            rest = match.group("items").strip()
            return ("add_shopping", rest) if rest else (None, None)
        match = _TRAILING_INVENTORY_DESTINATION_RE.match(stripped)
        if match:
            rest = match.group("items").strip()
            return ("add_inventory", rest) if rest else (None, None)

    return None, None


def detect_header_add_destination(text):
    """Deterministically detect a STANDALONE destination header line
    ("🛒 Покупки"/"🧊 Запаси"/"до покупок"/"до запасів" — see
    _HEADER_SHOPPING_LINE_RE/_HEADER_INVENTORY_LINE_RE) as the very first
    line of a multi-line message, followed by one or more item lines below
    it — e.g. a bot-preview-style block the user pasted back:
        🛒 Покупки
        Додати Тестовий чай — 1 шт.
    Never fuzzy — the header must be the ENTIRE first line, nothing else on
    it (see module docstring). Returns ("add_shopping"|"add_inventory",
    item_text) with the header line stripped off (and any per-line "Додати"
    verb noise on the remaining lines cleaned via _strip_line_add_verbs), or
    (None, None) if the first line isn't an exact header match or there's
    no remaining item text below it.
    """
    if not isinstance(text, str):
        return None, None
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None, None
    first_line = _strip_header_variation_selectors(lines[0].strip())
    rest = _strip_line_add_verbs("\n".join(lines[1:]).strip())
    if not rest:
        return None, None
    if _HEADER_SHOPPING_LINE_RE.match(first_line):
        return "add_shopping", rest
    if _HEADER_INVENTORY_LINE_RE.match(first_line):
        return "add_inventory", rest
    return None, None


EXPLICIT_ADD_ITEM_PROMPT = (
    "Розбий текст на список товарів для додавання. Це ЛИШЕ список товарів — ніколи не вигадуй суми грошей, "
    "витрати чи списання, навіть якщо текст на це схожий; якщо щось таке трапляється, опиши весь цей "
    "фрагмент у unresolved_fragments і не вигадуй для нього товар.\n"
    "Кожен товар має: name (сама назва, БЕЗ жодних слів про кількість чи тару), quantity_text (кількість як "
    "у тексті, або порожній рядок якщо кількість не вказана), category — одна з фіксованих категорій нижче.\n"
    "Правила відокремлення кількості від назви: якщо кількість — просто число без одиниці («2 банани») — "
    "quantity_text рівно «2», name — «Банани» (без числа). Якщо кількість — слово «пара»/«пару» («пару "
    "сосисок») — quantity_text рівно «пара» чи «пару» (як у тексті), name — «Сосиски» (без слова «пара»). "
    "Якщо кількість описана через тару («дві пачки сосисок») — усю фразу кількості («дві пачки») клади в "
    "quantity_text, а name — лише сам товар («Сосиски»); НІКОЛИ не залишай слова «пачка»/«упаковка»/"
    "числівники всередині name. Якщо безпечно відокремити кількість від назви не вдається — постав "
    "quantity_text порожнім рядком і опиши весь фрагмент у unresolved_fragments, а не вигадуй назву.\n"
    "З name прибирай ЛИШЕ слова про кількість чи тару — прикметники й означення товару (колір, смак, вид: "
    "«зелений», «чорний», «кокосовий», «рисовий», «грецький», «червоний», «білий», «кисломолочний», "
    "«вершковий», «мисливські», «тестовий» тощо) є частиною назви товару і їх ЗАВЖДИ треба залишати в name "
    "(у називному відмінку): «1 шт. зеленого чаю» → name «Зелений чай», quantity_text «1 шт.»; «2 л "
    "кокосового молока» → name «Кокосове молоко», quantity_text «2 л»; «1 шт. тестового чаю» → name "
    "«Тестовий чай», quantity_text «1 шт.». НІКОЛИ не скорочуй name до одного загального іменника (напр. "
    "«Чай», «Молоко», «Йогурт»), якщо в оригінальному тексті перед іменником був прикметник чи означення.\n"
    "Категорії: М'ясо та риба, Молочне та яйця, Овочі та зелень, Фрукти та ягоди, Хліб і випічка, "
    "Крупи, макарони та борошно, Соуси, спеції та бакалія, Солодке та снеки, Напої, Заморожене, "
    "Інше їстівне.\n"
    "Якщо частину тексту не можна безпечно перетворити на товар — опиши цей фрагмент у unresolved_fragments "
    "(масив рядків) і НЕ вигадуй товар для нього. Завжди повертай це поле, навіть порожнім масивом.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON:\n"
    '{"items":[{"name":"Молоко","quantity_text":"","category":"Молочне та яйця"},'
    '{"name":"Тестовий чай","quantity_text":"1 шт.","category":"Напої"}],"unresolved_fragments":[]}'
)

_EXPLICIT_ADD_FALLBACK = {"items": [], "unresolved_fragments": []}


def _ask_gemini_explicit_add_items(item_text):
    """ONE Gemini call for Global Explicit Add v1 — item_text already has
    the destination phrase stripped off by detect_explicit_add_destination.
    The JSON contract here (items + unresolved_fragments only) has no
    concept of destination, expense, consumption, or deletion at all."""
    raw = _bot.call_gemini([{"role": "user", "content": item_text}], EXPLICIT_ADD_ITEM_PROMPT, temperature=0.1)
    if not raw:
        return dict(_EXPLICIT_ADD_FALLBACK)
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "items": data.get("items") if isinstance(data.get("items"), list) else [],
            "unresolved_fragments": (
                data.get("unresolved_fragments") if isinstance(data.get("unresolved_fragments"), list) else []
            ),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_EXPLICIT_ADD_FALLBACK)


def _validate_explicit_add_items(items_raw, alias_map):
    """Validate a list of raw {name, quantity_text, category} dicts from
    _ask_gemini_explicit_add_items into normalized item dicts (same shape
    _validate_new_item_op produces for the main router). Returns None if
    ANY entry is malformed or still leaks a quantity/container phrase into
    name — the whole explicit-add request is rejected rather than silently
    dropping just the one bad item."""
    result = []
    for op in items_raw:
        if not isinstance(op, dict):
            return None
        leaked_name = op.get("name")
        if isinstance(leaked_name, str) and _looks_like_leaked_quantity_phrase(leaked_name):
            return None
        item = _validate_new_item_op(op, alias_map)
        if item is None:
            return None
        result.append(item)
    return result


def validate_mini_planner_add_items(items_raw, alias_map=None):
    """Public entrypoint for the Unified Mini Action Planner V1
    (mini_action_planner.py) — same all-or-nothing validation contract as
    _validate_explicit_add_items (reject the WHOLE batch if any item is
    malformed or still leaks a quantity/container phrase into name), so a
    planner-classified add_to_shopping/add_to_inventory action gets exactly
    the same safety guarantees Global Explicit Add v1 already has, without
    duplicating the validation logic. Returns a list of normalized item
    dicts (possibly empty), or None if `items_raw` itself isn't a list or
    any entry fails validation — the caller must treat None the same as an
    empty/unsafe result (fall back, never guess)."""
    if not isinstance(items_raw, list):
        return None
    return _validate_explicit_add_items(items_raw, alias_map)


def build_add_preview_from_items(destination, validated_items, inventory_items):
    """Shared tail of Global Explicit Add v1 and Global Bare Add v1: turns an
    already-validated item list plus a decided `destination` ("add_shopping"
    or "add_inventory") into the exact (kind, payload) shape
    _validate_operations_detailed produces — ("ok", payload) or
    ("clarify", {...}) for the inventory representation guard. Never touches
    Gemini — callers have already done that (or never needed to)."""
    if destination == "add_shopping":
        add_shopping_items = _bot._auto_merge_in_place(validated_items)
        return "ok", {
            "add_shopping_items": add_shopping_items,
            "add_inventory_items": [],
            "consume_changes": [],
            "new_expenses": [],
            "new_expense": None,
            "delete_expense": None,
            "inventory_merge_targets": [],
        }

    # destination == "add_inventory"
    add_inventory_items = _bot._auto_merge_in_place(validated_items)
    guard_kind, guard_result = apply_inventory_representation_guard(add_inventory_items, inventory_items)
    if guard_kind == "clarify":
        return "clarify", {
            **guard_result,
            "add_shopping_items": [],
            "add_inventory_items": add_inventory_items,
            "consume_changes": [],
            "new_expenses": [],
            "new_expense": None,
            "delete_expense": None,
        }
    add_inventory_items, inventory_merge_targets = guard_result
    return "ok", {
        "add_shopping_items": [],
        "add_inventory_items": add_inventory_items,
        "consume_changes": [],
        "new_expenses": [],
        "new_expense": None,
        "delete_expense": None,
        "inventory_merge_targets": inventory_merge_targets,
    }


def build_explicit_add_preview(destination, item_text, inventory_items, alias_map=None):
    """Top-level entry point for Global Explicit Add v1. `destination`
    ("add_shopping" or "add_inventory") and `item_text` (destination phrase
    already stripped) come from detect_explicit_add_destination — Gemini
    never sees the original destination phrase and never decides or
    changes the destination itself.

    Returns the exact same (kind, payload) shape as
    _validate_operations_detailed (see its docstring): ("ok", payload) /
    ("unresolved", [...]) / ("invalid", [...]) / ("clarify", {...}). Never
    returns "none" — once an explicit destination phrase with non-empty
    item text has been detected, the request is always either fulfilled or
    explicitly rejected, never silently ignored.
    """
    if expenses._EXPENSE_AMOUNT_RE.search(item_text):
        return "invalid", [
            "Для покупки з витратою напиши, наприклад:\n«Купив молоко за 10 zł»."
        ]

    router_result = _ask_gemini_explicit_add_items(item_text)
    fragments = router_result.get("unresolved_fragments")
    if isinstance(fragments, list):
        cleaned_fragments = [str(f).strip() for f in fragments if str(f).strip()]
        if cleaned_fragments:
            return "unresolved", cleaned_fragments

    raw_items = router_result.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return "invalid", ["Не зміг розпізнати жодного товару в повідомленні."]

    validated = _validate_explicit_add_items(raw_items, alias_map)
    if validated is None:
        return "invalid", ["Не зміг безпечно розпізнати товар."]

    return build_add_preview_from_items(destination, validated, inventory_items)


# =========================
# GLOBAL BARE ADD v1 — "Додай молоко"/"Додати молоко" with NO destination
# phrase at all. detect_bare_add strips just the bare add verb
# (deterministic, no Gemini — see _ADD_VERB_RE) and deliberately refuses any
# fragment carrying an expense-amount marker (zł/zl/pln/a bare "z"), so a
# message like "Додай молоко за 10 zł" is never treated as a bare add at all
# — it falls through unchanged to whichever existing gate already owns that
# phrasing (the expense-add gate), exactly like before this feature existed.
# Item parsing itself (parse_bare_add_items) reuses the SAME Gemini prompt/
# validation as Global Explicit Add v1 — there is no second parser — and is
# destination-agnostic, so callers can parse once, ask "покупки чи запаси?"
# if the active menu doesn't already answer that, and only then call
# build_add_preview_from_items without ever calling Gemini a second time.
# =========================
_BARE_ADD_RE = re.compile(r"^" + _ADD_VERB_RE + r"\s+", re.IGNORECASE)


def detect_bare_add(text):
    """Returns the item text (bare add verb stripped — "Додай"/"Додайте"/
    "Додати", see _ADD_VERB_RE) if `text` is a bare add command with no
    explicit destination and no expense-amount marker, or None otherwise.
    Caller must already have ruled out an explicit destination phrase
    (detect_explicit_add_destination) before calling this — a message that
    matches both is always handled as the explicit-add one. Any per-line
    add-verb noise on a 2nd+ line (e.g. a pasted-back multi-line preview
    where every line starts with "Додати") is cleaned via
    _strip_line_add_verbs before Gemini ever sees it.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    match = _BARE_ADD_RE.match(stripped)
    if not match:
        return None
    rest = _strip_line_add_verbs(stripped[match.end():].strip())
    if not rest:
        return None
    if expenses._EXPENSE_AMOUNT_RE.search(rest):
        return None
    return rest


def parse_bare_add_items(item_text, alias_map=None):
    """The destination-agnostic first half of Global Bare Add v1: ONE Gemini
    call (the same EXPLICIT_ADD_ITEM_PROMPT Global Explicit Add v1 already
    uses) plus the same validation, before any destination is known. Returns
    ("unresolved", [fragments]) / ("invalid", [reasons]) / ("items", [validated
    items]) — the caller decides the destination (from the active menu, or by
    asking) and then calls build_add_preview_from_items with the same
    validated items, never re-parsing.
    """
    router_result = _ask_gemini_explicit_add_items(item_text)
    fragments = router_result.get("unresolved_fragments")
    if isinstance(fragments, list):
        cleaned_fragments = [str(f).strip() for f in fragments if str(f).strip()]
        if cleaned_fragments:
            return "unresolved", cleaned_fragments

    raw_items = router_result.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return "invalid", ["Не зміг розпізнати жодного товару в повідомленні."]

    validated = _validate_explicit_add_items(raw_items, alias_map)
    if validated is None:
        return "invalid", ["Не зміг безпечно розпізнати товар."]

    return "items", validated


_DESTINATION_ANSWER_SHOPPING = {"до покупок", "у покупки", "в покупки", "покупки", "🛒 до покупок"}
_DESTINATION_ANSWER_INVENTORY = {"до запасів", "у запаси", "в запаси", "запаси", "🧊 до запасів"}


def parse_add_destination_answer(text):
    """Parses a reply to the Global Bare Add v1 "Куди додати ці позиції?"
    question into "add_shopping"/"add_inventory"/None (invalid). Deliberately
    a fixed phrase set, never fuzzy matching or Gemini — same determinism as
    detect_explicit_add_destination."""
    if not isinstance(text, str):
        return None
    normalized = text.strip().lower()
    if normalized in _DESTINATION_ANSWER_SHOPPING:
        return "add_shopping"
    if normalized in _DESTINATION_ANSWER_INVENTORY:
        return "add_inventory"
    return None
