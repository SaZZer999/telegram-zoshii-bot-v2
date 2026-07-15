"""Expense domain: constants, Gemini prompts/router, validators, formatters,
routing gates, and Telegram handlers for adding, browsing, summarizing, and
deleting household expenses.

This module must never import bot.py (that would create an import cycle,
since bot.py imports this module). Wherever this module needs a piece of
"bot infrastructure" that existing tests patch as `bot.<name>` — send_message,
get_household_and_user, call_gemini, get_warsaw_datetime_context,
_validate_selected_numbers, _ask_gemini_expense_router (self-referenced by
other handlers here), and the expense database helpers (add_expense,
delete_expense, get_recent_expenses, get_recent_expenses_for_deletion,
get_expense_month_summary) — it goes through the live `_bot` module reference
handed in via configure(), not a snapshotted local import. That keeps every
existing test that does `patch.object(bot, "<name>", ...)` working exactly as
before, even though the real call site now lives here.

`active_list_context` and `MAIN_KEYBOARD` are shared, mutable-by-reference
objects owned by bot.py (used by many other flows too); they're injected once
via configure() and read directly (no `_bot.` indirection needed for a plain
dict/data lookup).
"""
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from database import StaleSnapshotError
from action_history import UNDO_BUTTON_TEXT

# =========================
# INJECTED DEPENDENCIES (see configure())
# =========================
_bot = None
active_list_context = None
MAIN_KEYBOARD = None


def configure(bot_module, active_list_context_dict, main_keyboard):
    """Wire in bot.py's shared dependencies once, right after both modules
    finish importing. See the module docstring for why this indirection
    exists instead of a plain `import bot`.
    """
    global _bot, active_list_context, MAIN_KEYBOARD
    _bot = bot_module
    active_list_context = active_list_context_dict
    MAIN_KEYBOARD = main_keyboard


# =========================
# PENDING STATE
# =========================
pending_expense = {}          # chat_id -> {household_id, user_db_id, amount, currency, category, description, expense_date, origin}
pending_expense_delete = {}   # chat_id -> {expense_id, household_id, snapshot: {amount, category, expense_date, description}, origin}
expense_delete_selection = {}  # chat_id -> {household_id, user_db_id, expenses: [snapshot list from get_recent_expenses_for_deletion], origin}


def clear_expense_state(chat_id):
    pending_expense.pop(chat_id, None)
    pending_expense_delete.pop(chat_id, None)
    expense_delete_selection.pop(chat_id, None)


# =========================
# CONSTANTS / CATEGORIES
# =========================
EXPENSES_INTRO_TEXT = (
    "💸 Витрати\n\n"
    "Напиши витрату, наприклад:\n"
    "• Biedronka 86,40 zł — продукти\n"
    "• Запиши 120 zł за інтернет\n"
    "• Кава 14 zł"
)

DEFAULT_EXPENSE_CATEGORY = "Інше"

EXPENSE_CATEGORIES = [
    "Продукти", "Дім і рахунки", "Транспорт", "Здоров’я",
    "Кафе / ресторани", "Побут", "Дитина", "Інше",
]

VALID_EXPENSE_CATEGORIES = set(EXPENSE_CATEGORIES)

EXPENSE_MAX_AMOUNT = Decimal("1000000")
EXPENSE_DESCRIPTION_MAX_LEN = 200

EXPENSE_GATE_UNRECOGNIZED_MSG = (
    "Не зміг зрозуміти витрату. Напиши, наприклад:\n\n"
    "Biedronka 86,40 zł — продукти"
)

EXPENSE_PREVIEW_GUARD_MSG = (
    "У тебе є незавершена дія з витратами.\n\n"
    "Підтвердь її або скасуй перед новою командою."
)

# Mirrors bot.py's STALE_PREVIEW_MSG of the same wording — duplicated on
# purpose (same reasoning as database.py's duplicated normalization
# constants): this module must not import bot.py, and the two copies only
# need to keep agreeing on the displayed text, not share code.
STALE_PREVIEW_MSG = "Список змінився з іншого пристрою. Онови список і повтори дію."

# Mirrors bot.py's _UA_WEEKDAYS/_UA_MONTHS_GENITIVE — duplicated on purpose,
# same reasoning as above. Only used by _format_expense_date_display.
_UA_WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
_UA_MONTHS_GENITIVE = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]
_UA_MONTHS_NOMINATIVE = [
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
]

EXPENSE_ROUTER_PROMPT = (
    "Ти помічник, який розпізнає повідомлення про побутову витрату для одного домашнього господарства "
    "(наприклад «Biedronka 86,40 zł», «Запиши 120 zł за інтернет», «Кава 14 zł»). "
    "Тобі надається поточна локальна дата й час Europe/Warsaw як єдине надійне джерело часу, і іноді — "
    "нумерований список останніх записаних витрат (номер, дата, опис, сума, категорія).\n"
    "Визнач намір (intent):\n"
    "- «create_expense» — повідомлення описує одну НОВУ конкретну витрату з сумою в злотих\n"
    "- «delete_expense» — користувач хоче видалити/скасувати ОДНУ вже записану витрату зі списку останніх "
    "витрат, наданого нижче (напр. «Видали витрату за булочку 4 zł», «Скасуй витрату Biedronka», «2»)\n"
    "- «none» — повідомлення не описує ні нову витрату, ні видалення існуючої\n\n"
    "Для create_expense поверни:\n"
    "- amount — сума як рядок з крапкою або комою (наприклад «86.40» або «86,40»); ніколи не округлюй "
    "і не вигадуй суму, якої немає в тексті\n"
    "- currency — завжди «PLN»\n"
    "- category — ОБОВ'ЯЗКОВО одна з рівно цих восьми: Продукти, Дім і рахунки, Транспорт, Здоров'я, "
    "Кафе / ресторани, Побут, Дитина, Інше; якщо не можеш впевнено визначити категорію — постав «Інше»\n"
    "- description — короткий опис (назва магазину/товару/послуги), без суми й категорії всередині тексту\n"
    "- expense_date — дата у форматі YYYY-MM-DD; якщо в тексті не вказано дату явно — використовуй сьогоднішню "
    "дату з наданого контексту; ніколи не вигадуй дату в майбутньому\n\n"
    "Для delete_expense поверни selected_numbers — масив номерів позицій з наданого списку останніх витрат, "
    "які відповідають описаній витраті: якщо підходить рівно одна позиція — один номер; якщо запит "
    "неоднозначний (може підходити кілька позицій) або жодна позиція явно не підходить — залиш "
    "selected_numbers порожнім масивом і опиши це в unresolved_fragments. Ніколи не вигадуй номер, якого "
    "немає у наданому списку, і ніколи не повертай більше одного номера.\n\n"
    "Якщо в повідомленні немає жодної явної суми в злотих і воно явно не про видалення існуючої витрати — "
    "поверни «none». Якщо щось важливе неоднозначне чи суперечливе — додай короткий опис незрозумілого "
    "фрагмента в unresolved_fragments (масив рядків) замість того, щоб вгадувати.\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON:\n"
    "{\"intent\": \"create_expense\", \"amount\": \"86.40\", \"currency\": \"PLN\", \"category\": \"Продукти\", "
    "\"description\": \"Biedronka\", \"expense_date\": \"2026-07-03\", \"selected_numbers\": [], "
    "\"unresolved_fragments\": []}\n"
    "Приклад delete_expense (зі списком «1. 03.07 — Булочка — 4,00 zł [Продукти]», "
    "«2. 03.07 — Biedronka — 86,40 zł [Продукти]» і повідомленням «Видали булочку 4 zł»):\n"
    "{\"intent\": \"delete_expense\", \"amount\": null, \"currency\": null, \"category\": null, "
    "\"description\": null, \"expense_date\": null, \"selected_numbers\": [1], \"unresolved_fragments\": []}\n"
    "Приклад none:\n"
    "{\"intent\": \"none\", \"amount\": null, \"currency\": null, \"category\": null, \"description\": null, "
    "\"expense_date\": null, \"selected_numbers\": [], \"unresolved_fragments\": []}"
)

# =========================
# KEYBOARDS
# =========================
EXPENSES_KEYBOARD = {
    "keyboard": [
        ["🧾 Останні витрати", "📊 Цей місяць"],
        ["🗑️ Видалити витрату"],
        [UNDO_BUTTON_TEXT],
        ["⬅️ Головне меню"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

EXPENSE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, додати"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

EXPENSE_DELETE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, видалити"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}


# =========================
# ORIGIN HELPERS
# =========================
def _current_expense_origin(chat_id):
    """Where an expense command was issued from — the two return destinations
    the expense flow supports: the dedicated expenses submenu, or the main
    menu (covers everywhere else — help, open shopping/inventory lists —
    since expenses never sets saved_list_context of its own)."""
    if active_list_context.get(chat_id) == "expenses":
        return "expenses_menu"
    return "global"


def _expense_origin_keyboard(origin):
    """The correct persistent keyboard to explicitly (re-)send for a given
    expense-command origin — ALWAYS a concrete keyboard, never None."""
    if origin == "expenses_menu":
        return EXPENSES_KEYBOARD
    return MAIN_KEYBOARD


# =========================
# ROUTING GATES (pure, no Gemini)
# =========================
# The bare "z" alternative additionally requires that no other digit follows
# it (even across whitespace) — this is what keeps "2 z 3" from matching as
# an amount+currency pair while still accepting "10 z"/"10,50 z" at the end
# of a message. The other markers (zł/zl/pln) already can't false-positive
# on a longer word like "zebra"/"zloty" thanks to their own \b, so they keep
# their original (shared, implicit) boundary behavior unchanged. "злот\w*"/
# "зл\b" (Context Intent Safety V1) cover the Cyrillic spellings ("14
# злотих", "10 зл") the Latin-only markers above never matched — same \b
# boundary discipline, so "золото" or "зліва" never false-positive.
_EXPENSE_AMOUNT_RE = re.compile(r"\d[\d\s.,]*\s*(zł\b|zl\b|pln\b|злот\w*|зл\b|z\b(?!\s*\d))", re.IGNORECASE)


def _expense_command_gate(text):
    """Narrow, local gate for explicit expense commands — usable outside the
    dedicated expenses submenu (main menu, help, open shopping/inventory
    lists). Recognizes only unambiguous expense phrasing: an amount tagged
    with zł/zl/PLN, or the explicit "Запиши витрату" prefix. Never parses
    amount/category/date itself — that remains entirely the job of the
    Gemini expense router.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith("запиши витрату"):
        return True
    if _EXPENSE_AMOUNT_RE.search(lowered):
        return True
    return False


def _expense_report_gate(text):
    """Narrow, local gate for the two read-only expense report commands —
    recognizes both the dedicated expenses-submenu buttons and free-text
    equivalents from anywhere. Returns "recent", "monthly", or None. Never
    calls Gemini — these are plain read-only lookups, not something that
    needs interpretation.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if stripped == "🧾 Останні витрати":
        return "recent"
    if stripped == "📊 Цей місяць":
        return "monthly"
    lowered = stripped.lower()
    if "останні витрати" in lowered:
        return "recent"
    if "підсумок за цей місяць" in lowered or "скільки витратили цього місяця" in lowered:
        return "monthly"
    return None


# "викресли"/"викреслити" — an additional delete/cancel verb synonym, same
# semantic slot as "видали"/"прибери" (shopping_action_planner.py's own
# pre-gate already treats it as a "remove from a list" verb for the
# shopping domain — see its _LIST_REMOVAL_VERB_ROOTS); added here too so the
# SAME verb is recognized for expense deletion, not just shopping. Safe to
# add to the GLOBAL gate below: it still only fires combined with "витрат"
# or a _EXPENSE_FINANCIAL_REFERENCE_STEMS stem, and "покупок" deliberately
# doesn't contain the "покупк" stem (see that constant's own comment), so
# "Викресли хліб зі списку покупок" still never matches the global gate.
_EXPENSE_DELETE_VERBS = (
    "видали", "видалити", "скасуй", "скасувати", "прибери", "прибрати", "викресли", "викреслити",
)

# Financial-reference stems — a delete/cancel verb ALONE never triggers this
# gate (see _expense_delete_command_gate's own docstring: "Скасуй зустріч"
# must never match) — only when paired with one of these words naming a
# past financial operation is a delete/cancel verb treated as an expense-
# deletion command instead of, say, a shopping-list removal ("Прибери молоко
# зі списку покупок"). Deliberately a compact set of STEMS, never a list of
# full phrases: "плат" alone covers both "платіж"/"платежу" and
# "оплата"/"оплату" (no separate entry needed for each), and Ukrainian's own
# genitive-plural "покупок" ("of purchases") happens to NOT contain the
# "покупк" stem (по-куп-ок vs по-купк-...), so "зі списку покупок" is never
# mistaken for "ту покупку" — a real grammatical distinction, not a manually
# tuned exception. A short stem like "чек" can in principle collide with an
# unrelated word (e.g. "чекаю") — same accepted, low-severity tradeoff every
# other cheap pre-gate in this codebase already makes (see action_planner.
# py's own module docstring): a false positive here only costs one extra
# call to the EXISTING expense-delete Gemini router, which itself never
# guesses/deletes without an unambiguous match.
_EXPENSE_FINANCIAL_REFERENCE_STEMS = ("покупк", "плат", "транзакц", "чек", "списанн")


def _expense_delete_command_gate(text):
    """Narrow, local gate for explicit expense-deletion commands — usable
    both as the dedicated "🗑️ Видалити витрату" button and as free text from
    anywhere. GATE BOUNDARY: this function only decides whether to hand the
    text to the EXISTING expense-delete Gemini router — it never itself
    identifies which expense, never returns a DB id, never deletes anything,
    never bypasses preview, and never touches pending state directly; all of
    that stays exactly where it already lives (_ask_gemini_expense_router,
    _resolve_expense_delete_selection, pending_expense_delete/
    expense_delete_selection, the existing preview/confirm/undo flow).

    Two independent trigger shapes both require a delete/cancel verb — a
    bare "Видали булочку" (no financial signal at all) never matches, since
    that's plausibly about the shopping list instead. A bare zł-tagged
    amount alone is deliberately NOT a trigger either — "Видали булочку
    4 zł" must stay just as ambiguous as "Видали булочку" (a priced shopping
    item, not obviously an expense); a real financial-reference word is
    required, exactly like the original "витрат" shape:
      1. (original) delete/cancel verb + the literal word "витрат(а/и/у)"
         anywhere in the text.
      2. (new) the SAME delete/cancel verb + a financial-reference stem
         (see _EXPENSE_FINANCIAL_REFERENCE_STEMS) — covers "Скасуй ту
         покупку на 50 zł", "Прибери останній платіж", "Видали останню
         оплату за інтернет" without requiring the word "витрата" at all.
         The verb is still REQUIRED in this shape too: a bare financial
         word alone ("Я оплатив інтернет 120 zł", "Запиши покупку на
         50 zł") never matches — those are add-expense phrasings, not
         delete ones, and stay on their own existing routes unaffected.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped == "🗑️ Видалити витрату":
        return True
    lowered = stripped.lower()
    if not any(verb in lowered for verb in _EXPENSE_DELETE_VERBS):
        return False
    if "витрат" in lowered:
        return True
    return any(stem in lowered for stem in _EXPENSE_FINANCIAL_REFERENCE_STEMS)


# Explicit shopping-list marker ("покупки"/"покупку"/"покупок" all share this
# stem) — deliberately a DIFFERENT, broader stem than _EXPENSE_FINANCIAL_
# REFERENCE_STEMS's "покупк" above, which exists for the opposite purpose
# (recognizing "ту покупку" as a financial reference while NOT matching
# "список покупок" — see that constant's own comment on the genitive-plural
# quirk). This one is used only to detect when a message names the shopping
# list BY NAME, so it can never be misread as an expense action even while
# the active expenses submenu context is open (see
# _looks_like_shopping_list_reference below).
_EXPLICIT_SHOPPING_LIST_STEM = "покуп"


def _looks_like_shopping_list_reference(text):
    """True if `text` explicitly names the shopping list/purchases (e.g. "зі
    списку покупок"), as opposed to just "the list" in general (e.g. "зі
    списку", which — while the active expenses submenu context is open —
    unambiguously means THIS, the expenses list currently on screen). Used
    ONLY as a narrow domain-boundary guard by bot.py's active-expenses-
    context route: a delete-like phrase that ALSO matches this must never be
    read as an expense deletion, so that route falls through instead,
    leaving room for the Shopping Action Planner (or any other later route)
    to handle it. Never touches the database, never calls Gemini."""
    if not isinstance(text, str):
        return False
    return _EXPLICIT_SHOPPING_LIST_STEM in text.strip().lower()


def _expense_delete_active_context_gate(text):
    """Looser delete-intent gate for use ONLY while the active expenses
    submenu context is already open (active_list_context[chat_id] ==
    "expenses") — see bot.py's _route_active_expenses_context, which checks
    _looks_like_shopping_list_reference first. Unlike
    _expense_delete_command_gate (which additionally requires the word
    "витрат" or a financial-reference stem, since THAT gate fires from
    anywhere and must stay unambiguous on its own), a bare delete/cancel
    verb is enough here: being inside the expenses submenu already
    establishes "this message is about an expense", exactly the same
    reasoning _route_active_aliases_context's shared alias router already
    relies on (no "alias" keyword required there either). Still never
    itself identifies WHICH expense, never returns a DB id, never deletes
    anything, never bypasses preview — same GATE BOUNDARY as
    _expense_delete_command_gate.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped == "🗑️ Видалити витрату":
        return True
    lowered = stripped.lower()
    return any(verb in lowered for verb in _EXPENSE_DELETE_VERBS)


_EXPENSE_DELETE_LEADING_VERB_RE = re.compile(
    r"^(?:" + "|".join(_EXPENSE_DELETE_VERBS) + r")\s+(?:витрат[ауи]\s+)?", re.IGNORECASE,
)
_EXPENSE_DELETE_TRAILING_LIST_RE = re.compile(
    r"\s+(?:з|зі|із)\s+списк[ау](?:\s+витрат)?\s*$", re.IGNORECASE,
)


def _strip_delete_command_wrapper(text):
    """Strip a recognized leading delete/cancel verb and a trailing "зі
    списку"/"із списку(...витрат)" phrase, if present — used ONLY to widen
    _find_exact_expense_match's existing local (no-Gemini) exact-name fast
    path, so a full sentence like "Викресли тестова кава зі списку" can
    still exactly match the bare description "тестова кава" it's already
    comparing against, without loosening that comparison itself (still exact
    equality, never fuzzy). Same plain-regex-strip technique
    _clean_expense_description already uses for the opposite (add-expense)
    direction — not a parser, not a second matcher. Never applied on the
    Gemini fallback path, which already handles full sentences (and
    declined forms like "тестову каву") on its own.
    """
    if not isinstance(text, str):
        return text
    stripped = _EXPENSE_DELETE_LEADING_VERB_RE.sub("", text.strip())
    stripped = _EXPENSE_DELETE_TRAILING_LIST_RE.sub("", stripped)
    return stripped.strip()


# =========================
# GEMINI ROUTER
# =========================
_EXPENSE_ROUTER_FALLBACK = {
    "intent": "none", "amount": None, "currency": None, "category": None,
    "description": None, "expense_date": None, "selected_numbers": [], "unresolved_fragments": [],
}


def _ask_gemini_expense_router(user_text, recent_expenses=None):
    """ONE Gemini call per message for expense parsing — covers both adding a
    new expense (expenses submenu / the create-expense global gate) and
    identifying an existing expense to delete (expenses submenu / the
    delete-expense global gate). `recent_expenses` (optional, shaped like
    get_recent_expenses's return value) is the household's numbered
    recent-expense list; pass it whenever a delete command might be in
    play — Gemini uses it only to pick selected_numbers, never to invent
    amount/category/date for a NEW expense. Gemini never touches SQL — every
    field is re-validated in Python before anything is shown as a preview."""
    prompt_parts = [_bot.get_warsaw_datetime_context()]
    if recent_expenses:
        lines = [
            f"{i}. {exp['expense_date'].strftime('%d.%m')} — {exp['description'] or exp['category']} — "
            f"{_format_expense_amount(exp['amount'])} [{exp['category']}]"
            for i, exp in enumerate(recent_expenses, start=1)
        ]
        prompt_parts.append("Останні витрати цього household:\n" + "\n".join(lines))
    prompt_parts.append(f"Користувач написав: {user_text}")
    prompt = "\n\n".join(prompt_parts)
    raw = _bot.call_gemini([{"role": "user", "content": prompt}], EXPENSE_ROUTER_PROMPT, temperature=0.1)
    if not raw:
        return dict(_EXPENSE_ROUTER_FALLBACK)
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "amount": data.get("amount"),
            "currency": data.get("currency"),
            "category": data.get("category"),
            "description": data.get("description"),
            "expense_date": data.get("expense_date"),
            "selected_numbers": data.get("selected_numbers") if isinstance(data.get("selected_numbers"), list) else [],
            "unresolved_fragments": data.get("unresolved_fragments") if isinstance(data.get("unresolved_fragments"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_EXPENSE_ROUTER_FALLBACK)


# =========================
# VALIDATORS (pure)
# =========================
_BARE_Z_CURRENCY_RE = re.compile(r"\s*\bz\b(?!\s*\d)")


def _parse_expense_amount(raw_amount):
    """Parse a Gemini-provided amount into an exact Decimal — never float.
    Accepts comma or dot decimal separators and stray currency text/spaces.
    Returns a Decimal rounded to 2 places, or None if unparseable,
    non-positive, or larger than EXPENSE_MAX_AMOUNT.
    """
    if raw_amount is None:
        return None
    if isinstance(raw_amount, (int, float)):
        # Never trust float precision from Gemini directly — route through
        # str() first so e.g. 86.4 becomes "86.4", not a binary-float artifact.
        raw_amount = str(raw_amount)
    if not isinstance(raw_amount, str):
        return None
    cleaned = raw_amount.strip().lower()
    cleaned = cleaned.replace("zł", "").replace("zl", "").replace("pln", "")
    # Bare "z" marker (e.g. "10 z", "10,50 z") — same \b + "not followed by
    # another digit" safeguard as _EXPENSE_AMOUNT_RE, so this never strips a
    # "z" that's part of something else. Applied after the zł/zl/pln
    # replacements above, on whatever text they left behind.
    cleaned = _BARE_Z_CURRENCY_RE.sub("", cleaned)
    cleaned = cleaned.replace(" ", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0 or amount > EXPENSE_MAX_AMOUNT:
        return None
    return amount.quantize(Decimal("0.01"))


def _validate_expense_date(raw_date, now=None):
    """Parse+validate an ISO (YYYY-MM-DD) expense_date string against "not in
    the future", using the same Europe/Warsaw clock as the rest of the
    expense flow. Returns a date object, or None if missing/invalid/future.
    """
    if not isinstance(raw_date, str) or not raw_date.strip():
        return None
    try:
        parsed = datetime.strptime(raw_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    if now is None:
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
    if parsed > now.date():
        return None
    return parsed


def _validate_expense_category(raw_category):
    """Category must exactly match one of the fixed EXPENSE_CATEGORIES.
    Anything else silently falls back to DEFAULT_EXPENSE_CATEGORY (never
    blocks the expense) — the caller surfaces this fallback in the preview.
    Returns (category, was_defaulted).
    """
    if isinstance(raw_category, str) and raw_category.strip() in VALID_EXPENSE_CATEGORIES:
        return raw_category.strip(), False
    return DEFAULT_EXPENSE_CATEGORY, True


# V1.4.2 defensive description cleanup — Gemini is EXPECTED to already
# return a clean description (see EXPENSE_ROUTER_PROMPT's own "без суми й
# категорії всередині тексту" instruction), but a live bug showed it can
# instead return the WHOLE raw command ("Запиши 120 zł за інтернет") as
# description. These three patterns are applied, in order, as the single
# Python-side safety net that prevents that from ever being stored as the
# expense name:
#   1. a leading command verb ("Запиши"/"Додай"/...), optionally followed
#      by "витрату"/"витрата"/"витрати";
#   2. an amount+currency span ANYWHERE in the remaining text ("120 zł",
#      "86,40 zł", "39 злотих") — not just leading, since stripping the
#      verb can leave the amount at the start;
#   3. a leftover leading preposition ("за"/"на") once the amount is gone.
_EXPENSE_LEADING_COMMAND_VERB_RE = re.compile(
    r"^(?:запиши(?:ть)?|додай(?:те)?|занотуй(?:те)?|зафіксуй(?:те)?)\s+(?:витрат[ауи]\s+)?",
    re.IGNORECASE,
)
_EXPENSE_DESCRIPTION_AMOUNT_SPAN_RE = re.compile(
    r"\d[\d\s.,]*\s*(?:zł|zl|pln|злот\w*|z\b(?!\s*\d))",
    re.IGNORECASE,
)
_EXPENSE_LEADING_PREPOSITION_RE = re.compile(r"^(?:за|на)\s+", re.IGNORECASE)


def _clean_expense_description(raw_description):
    """Collapse whitespace, cap length, and strip a leading command verb /
    any amount+currency span / a leftover leading preposition (see the
    module-level comment above) — never raises, never None. A description
    that's ALREADY clean (the normal case) passes through unchanged, since
    none of the three patterns match plain text."""
    if not isinstance(raw_description, str):
        return ""
    cleaned = raw_description.strip()
    cleaned = _EXPENSE_LEADING_COMMAND_VERB_RE.sub("", cleaned)
    cleaned = _EXPENSE_DESCRIPTION_AMOUNT_SPAN_RE.sub("", cleaned).strip()
    cleaned = _EXPENSE_LEADING_PREPOSITION_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned.strip())
    return cleaned[:EXPENSE_DESCRIPTION_MAX_LEN]


def _validate_expense_router_result(router_result, now=None):
    """Pure decision logic for the expense router's JSON. Returns one of:
      ("unresolved", [fragment,...])  -- blocks preview regardless of intent
      ("ok", payload)                 -- payload: amount/currency/category/
                                          category_was_defaulted/description/expense_date
      ("delete", [number,...])        -- delete_expense intent; raw selected_numbers,
                                          still to be matched against the shown list by the caller
      ("invalid", None)               -- create_expense/delete_expense with unusable fields
      ("none", None)
    """
    fragments = router_result.get("unresolved_fragments")
    if isinstance(fragments, list):
        cleaned = [str(f).strip() for f in fragments if str(f).strip()]
        if cleaned:
            return "unresolved", cleaned
    intent = router_result.get("intent")
    if intent == "delete_expense":
        numbers = router_result.get("selected_numbers")
        return ("delete", numbers) if isinstance(numbers, list) else ("invalid", None)
    if intent != "create_expense":
        return "none", None
    currency = router_result.get("currency")
    if currency not in (None, "PLN"):
        return "invalid", None
    amount = _parse_expense_amount(router_result.get("amount"))
    if amount is None:
        return "invalid", None
    expense_date = _validate_expense_date(router_result.get("expense_date"), now=now)
    if expense_date is None:
        return "invalid", None
    category, category_was_defaulted = _validate_expense_category(router_result.get("category"))
    description = _clean_expense_description(router_result.get("description"))
    return "ok", {
        "amount": amount,
        "currency": "PLN",
        "category": category,
        "category_was_defaulted": category_was_defaulted,
        "description": description,
        "expense_date": expense_date,
    }


# =========================
# FORMATTERS (pure)
# =========================
def _format_expense_amount(amount):
    """Format a Decimal amount as Ukrainian-locale PLN display: comma
    decimal, always two decimal places (money, unlike item quantities)."""
    return f"{amount:.2f}".replace(".", ",") + " zł"


def _format_expense_date_display(expense_date, now=None):
    if now is None:
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
    if expense_date == now.date():
        return "сьогодні"
    weekday = _UA_WEEKDAYS[expense_date.weekday()]
    month = _UA_MONTHS_GENITIVE[expense_date.month - 1]
    return f"{expense_date.day} {month} {expense_date.year}"


def _format_expense_preview(payload, now=None):
    lines = [
        "💸 Додати витрату?",
        "",
        f"Сума: {_format_expense_amount(payload['amount'])}",
        f"Категорія: {payload['category']}" + (" (не вдалося визначити точно)" if payload["category_was_defaulted"] else ""),
    ]
    if payload["description"]:
        lines.append(f"Опис: {payload['description']}")
    lines.append(f"Дата: {_format_expense_date_display(payload['expense_date'], now=now)}")
    lines.append("")
    lines.append("✅ Так, додати")
    lines.append("❌ Скасувати")
    return "\n".join(lines)


def _format_recent_expenses(expenses):
    """Render up to 10 most-recent expenses (already sorted by the DB
    helper) plus their sum. `expenses` items come from get_recent_expenses."""
    if not expenses:
        return "Витрат поки немає."
    lines = ["💸 Останні витрати:", ""]
    total = Decimal("0")
    for i, exp in enumerate(expenses, start=1):
        total += exp["amount"]
        date_str = exp["expense_date"].strftime("%d.%m")
        label = exp["description"] or exp["category"]
        lines.append(f"{i}. {date_str} — {label} — {_format_expense_amount(exp['amount'])}")
        lines.append(f"   {exp['category']}")
    lines.append("")
    lines.append(f"Разом: {_format_expense_amount(total)}")
    return "\n".join(lines)


def _format_expenses_hub(today_total, month_total, recent_expenses):
    """Render the Expenses Hub V1 read-only dashboard shown by the "💸
    Витрати" button — today's total, this month's total, and up to the 5
    most recent expenses (already sorted newest-first by get_recent_
    expenses's own ORDER BY), followed by the same add-expense examples
    EXPENSES_INTRO_TEXT used to show alone. Pure formatter; `today_total`/
    `month_total` are Decimals, `recent_expenses` is get_recent_expenses's
    return shape (already limited to at most 5 by the caller)."""
    lines = [
        "💸 Витрати",
        "",
        f"Сьогодні: {_format_expense_amount(today_total)}",
        f"Цього місяця: {_format_expense_amount(month_total)}",
        "",
    ]
    if recent_expenses:
        lines.append("Останні витрати:")
        for i, exp in enumerate(recent_expenses, start=1):
            label = exp["description"] or exp["category"]
            lines.append(f"{i}. {label} — {_format_expense_amount(exp['amount'])}")
    else:
        lines.append("Останніх витрат ще немає.")
    lines.append("")
    lines.append("Щоб додати витрату, напиши, наприклад:")
    lines.append("• Кава 14 zł")
    lines.append("• Запиши 120 zł за інтернет")
    lines.append("• Biedronka 86,40 zł — продукти")
    return "\n".join(lines)


def _format_expense_month_summary(summary, year, month):
    """Render the current-month category breakdown. `summary` comes from
    get_expense_month_summary: {"total": Decimal, "by_category": {category: Decimal}}.
    Categories sorted by amount descending, then name ascending on ties;
    zero-amount categories (never actually produced by SUM over positive
    amounts, but checked defensively) are skipped."""
    header = f"📊 Витрати за {_UA_MONTHS_NOMINATIVE[month - 1]} {year}"
    by_category = summary["by_category"]
    if not by_category:
        return f"{header}\n\nВитрат за цей місяць поки немає."
    lines = [header, "", f"Разом: {_format_expense_amount(summary['total'])}", ""]
    ordered = sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))
    for category, amount in ordered:
        if amount == 0:
            continue
        lines.append(f"{category} — {_format_expense_amount(amount)}")
    return "\n".join(lines)


def _format_expense_delete_list(expenses):
    """Numbered recent-expense list shown before/while picking one to
    delete — the exact numbering the expense router's selected_numbers and
    _validate_selected_numbers resolve against."""
    lines = ["🗑️ Яку витрату видалити?", ""]
    for i, exp in enumerate(expenses, start=1):
        date_str = exp["expense_date"].strftime("%d.%m")
        label = exp["description"] or exp["category"]
        lines.append(f"{i}. {date_str} — {label} — {_format_expense_amount(exp['amount'])}")
    lines.append("")
    lines.append("Напиши номер або, наприклад:")
    lines.append("• Видали булочку 4 zł")
    lines.append("• Видали витрату Biedronka 86,40 zł")
    return "\n".join(lines)


def _format_expense_delete_preview(expense):
    label = expense["description"] or expense["category"]
    date_str = expense["expense_date"].strftime("%d.%m")
    lines = [
        "💸 Видалити витрату?",
        "",
        f"{date_str} — {label} — {_format_expense_amount(expense['amount'])}",
        f"Категорія: {expense['category']}",
        "",
        "✅ Так, видалити",
        "❌ Скасувати",
    ]
    return "\n".join(lines)


# =========================
# HANDLERS
# =========================
def _handle_expense_report_command(chat_id, user_id, display_name, kind):
    """Shared handler for both read-only expense report commands ("recent"/
    "monthly"). Never touches pending state or active_list_context — a pure
    read, safe to run from anywhere without disturbing whatever flow the
    chat is currently in. Never calls Gemini.
    """
    origin = _current_expense_origin(chat_id)
    keyboard = _expense_origin_keyboard(origin)
    try:
        household_id, _ = _bot.get_household_and_user(user_id, display_name)
        if kind == "recent":
            expenses = _bot.get_recent_expenses(household_id, limit=10)
            _bot.send_message(chat_id, _format_recent_expenses(expenses), reply_markup=keyboard)
        else:
            now = datetime.now(ZoneInfo("Europe/Warsaw"))
            summary = _bot.get_expense_month_summary(household_id, now.year, now.month)
            _bot.send_message(chat_id, _format_expense_month_summary(summary, now.year, now.month), reply_markup=keyboard)
    except Exception:
        _bot.send_message(chat_id, "Не вдалося отримати витрати. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


def _handle_expenses_hub(chat_id, user_id, display_name):
    """Expenses Hub V1 — the "💸 Витрати" button's response: a READ-ONLY
    dashboard (today's total, this month's total, last 5 expenses) instead
    of only the plain instructions EXPENSES_INTRO_TEXT used to show alone.
    Never calls Gemini, never writes to the database — active_list_context/
    pending-state clearing for the dedicated expenses submenu stays bot.py's
    job (see bot.py's "💸 Витрати" branch), unchanged from before this
    existed. Always sends EXPENSES_KEYBOARD, success or failure, so the
    submenu's own navigation ("⬅️ Головне меню") is never lost.
    """
    try:
        household_id, _ = _bot.get_household_and_user(user_id, display_name)
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
        today_total = _bot.get_expense_day_total(household_id, now.date())
        month_summary = _bot.get_expense_month_summary(household_id, now.year, now.month)
        recent = _bot.get_recent_expenses(household_id, limit=5)
        _bot.send_message(
            chat_id, _format_expenses_hub(today_total, month_summary["total"], recent), reply_markup=EXPENSES_KEYBOARD,
        )
    except Exception:
        _bot.send_message(chat_id, "Не вдалося показати витрати. Спробуй ще раз трохи пізніше.", reply_markup=EXPENSES_KEYBOARD)


def _handle_expense_command(chat_id, user_id, display_name, text):
    """Shared expense-router handling for both the dedicated expenses submenu
    and the global expense command gate. Mirrors _handle_alias_command:
    returns True if the message was fully handled here (caller must not fall
    through to general AI-chat). Returns False only when intent is "none" and
    origin == "expenses_menu" — the one case allowed to fall through, matching
    every other router in this file. A global-gate command (origin=="global")
    is never allowed to fall through, even on "none"/"invalid"/"unresolved" —
    the gate already confirmed the text looks like an expense command.
    """
    origin = _current_expense_origin(chat_id)
    keyboard = _expense_origin_keyboard(origin)
    try:
        household_id, user_db_id = _bot.get_household_and_user(user_id, display_name)
        router_result = _bot._ask_gemini_expense_router(text)
        kind, payload = _validate_expense_router_result(router_result)
        if kind == "unresolved":
            lines = ["Не зрозумів частину витрати:", ""]
            lines += [f"• «{f}»" for f in payload]
            lines.append("")
            lines.append("Спробуй сформулювати інакше, наприклад: «Biedronka 86,40 zł — продукти».")
            _bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)
        elif kind == "invalid":
            _bot.send_message(chat_id, EXPENSE_GATE_UNRECOGNIZED_MSG, reply_markup=keyboard)
        elif kind in ("none", "delete"):
            # "delete" here means Gemini classified this as delete_expense
            # despite no recent-expenses context being given (the dedicated
            # expense-delete gate normally intercepts genuine delete phrasing
            # before it ever reaches this add-expense router) — treated the
            # same as "none" rather than assuming an add-expense payload shape.
            if origin == "expenses_menu":
                return False
            _bot.send_message(chat_id, EXPENSE_GATE_UNRECOGNIZED_MSG, reply_markup=keyboard)
        else:
            pending_expense[chat_id] = {
                "household_id": household_id, "user_db_id": user_db_id,
                "amount": payload["amount"], "currency": payload["currency"],
                "category": payload["category"], "description": payload["description"],
                "expense_date": payload["expense_date"], "origin": origin,
            }
            _bot.send_message(chat_id, _format_expense_preview(payload), reply_markup=EXPENSE_PREVIEW_KEYBOARD)
        return True
    except Exception:
        _bot.send_message(chat_id, "Не вдалося обробити витрату. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)
        return True


# =========================
# PHOTO RECEIPT INPUT V1 — reuses the SAME pending_expense preview/confirm/
# cancel a typed expense command already builds (see _handle_expense_
# command's own "ok" branch above); no parallel confirm/cancel system,
# no new DB-write path. bot.py's photo_receipts.py integration calls this
# ONLY after re-validating every field in Python (a positive Decimal
# amount, a category from EXPENSE_CATEGORIES, a real non-future date) —
# never a raw Gemini payload.
# =========================
def build_receipt_expense_preview(chat_id, household_id, user_db_id, origin, amount, category, description,
                                   expense_date, category_was_defaulted=False):
    """Build a pending_expense preview from an already-validated receipt
    photo extraction and send it with the EXACT existing EXPENSE_PREVIEW_
    KEYBOARD ("✅ Так, додати"/"❌ Скасувати") — handle_add_confirm/
    handle_cancel (unchanged) then apply/discard it exactly like any other
    pending_expense entry, DB write included."""
    pending_expense[chat_id] = {
        "household_id": household_id, "user_db_id": user_db_id,
        "amount": amount, "currency": "PLN", "category": category,
        "description": description, "expense_date": expense_date, "origin": origin,
    }
    payload = {
        "amount": amount, "category": category, "category_was_defaulted": category_was_defaulted,
        "description": description, "expense_date": expense_date,
    }
    _bot.send_message(chat_id, _format_expense_preview(payload), reply_markup=EXPENSE_PREVIEW_KEYBOARD)


def _normalize_expense_match_text(s):
    """Lower/trim/collapse whitespace and strip punctuation — used only for
    exact-equality comparison, never substring/fuzzy matching."""
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _find_exact_expense_match(text, recent_expenses):
    """Local (no-Gemini) exact-name match against the SAME label already
    shown on screen by _format_expense_delete_list (description, falling
    back to category). Tries both the raw text AND the same text with a
    recognized delete-verb/"зі списку" wrapper stripped (see
    _strip_delete_command_wrapper) — so a bare "тестова кава" (numbered-
    selection mode) AND a full sentence like "Викресли тестова кава зі
    списку" (active expenses-context free text) both get a fast, no-Gemini
    match against the exact same label. Returns the single matching expense
    dict, or None if there is no match or more than one — deliberately
    never fuzzy: a declined phrase like "Видали Biedronka 86,40 zł" or
    "Викресли тестову каву зі списку" (which never equals a bare label
    exactly, wrapper stripped or not) still falls through to the Gemini-
    based resolution below.
    """
    candidates = {_normalize_expense_match_text(text), _normalize_expense_match_text(_strip_delete_command_wrapper(text))}
    candidates.discard("")
    if not candidates:
        return None
    matches = [
        exp for exp in recent_expenses
        if _normalize_expense_match_text(exp.get("description") or exp.get("category") or "") in candidates
    ]
    return matches[0] if len(matches) == 1 else None


def _build_delete_preview_from_match(chat_id, household_id, origin, expense):
    """Shared final step once exactly one expense has been identified for
    deletion, whether by the local exact-name match or by the Gemini router —
    builds the pending_expense_delete preview and exits selection mode."""
    pending_expense_delete[chat_id] = {
        "expense_id": expense["id"], "household_id": household_id,
        "snapshot": {
            "amount": expense["amount"], "category": expense["category"],
            "expense_date": expense["expense_date"], "description": expense["description"],
        },
        "origin": origin,
    }
    expense_delete_selection.pop(chat_id, None)
    _bot.send_message(chat_id, _format_expense_delete_preview(expense), reply_markup=EXPENSE_DELETE_PREVIEW_KEYBOARD)


def _resolve_expense_delete_selection(chat_id, household_id, user_db_id, origin, keyboard, text, recent_expenses):
    """Shared resolution step for both the global expense-delete gate and the
    dedicated selection mode (chat_id in expense_delete_selection). First
    tries a local exact-name match (no Gemini call) against the numbered
    list already shown; only if that doesn't resolve to exactly one match
    does it call the expense router with `recent_expenses` as context and
    either build the delete preview (exactly one match) or re-show the
    numbered list and stay in selection mode (zero matches, more than one
    match, or an unresolved/invalid/none router result — never guesses).
    Always fully handles the message; never falls through to AI-chat.
    """
    local_match = _find_exact_expense_match(text, recent_expenses)
    if local_match is not None:
        _build_delete_preview_from_match(chat_id, household_id, origin, local_match)
        return

    router_result = _bot._ask_gemini_expense_router(text, recent_expenses=recent_expenses)
    kind, payload = _validate_expense_router_result(router_result)
    matched = _bot._validate_selected_numbers(payload, recent_expenses) if kind == "delete" else None
    if matched is not None and len(matched) == 1:
        _build_delete_preview_from_match(chat_id, household_id, origin, matched[0])
        return
    # Zero matches, more than one match, or the router didn't produce a
    # usable delete selection (unresolved/invalid/none) — never guess; stay
    # in selection mode and ask the user to pick a number from the list.
    expense_delete_selection[chat_id] = {
        "household_id": household_id, "user_db_id": user_db_id,
        "expenses": recent_expenses, "origin": origin,
    }
    _bot.send_message(
        chat_id,
        "Не зміг однозначно визначити витрату.\n\n" + _format_expense_delete_list(recent_expenses),
        reply_markup=keyboard,
    )


def _handle_expense_delete_button(chat_id, user_id, display_name):
    """Entry point for the "🗑️ Видалити витрату" button — no Gemini call at
    this stage (a bare button press carries no target description), just
    shows up to 10 numbered recent expenses and enters selection mode."""
    origin = _current_expense_origin(chat_id)
    keyboard = _expense_origin_keyboard(origin)
    try:
        household_id, user_db_id = _bot.get_household_and_user(user_id, display_name)
        expenses = _bot.get_recent_expenses_for_deletion(household_id, limit=10)
        if not expenses:
            _bot.send_message(chat_id, "Витрат поки немає.", reply_markup=keyboard)
            return
        expense_delete_selection[chat_id] = {
            "household_id": household_id, "user_db_id": user_db_id,
            "expenses": expenses, "origin": origin,
        }
        _bot.send_message(chat_id, _format_expense_delete_list(expenses), reply_markup=keyboard)
    except Exception:
        _bot.send_message(chat_id, "Не вдалося отримати витрати. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


def _handle_expense_delete_global_command(chat_id, user_id, display_name, text):
    """Free-text expense-delete handler — fetches a fresh recent-expenses
    list (there is no pre-shown numbered list yet) and resolves it via the
    expense router, given that live list as candidates. Falls back to
    showing the list and entering selection mode if ambiguous, exactly like
    the dedicated button does.

    Two callers: bot.py's global expense-delete gate route
    (_expense_delete_command_gate matched, chat anywhere) AND bot.py's
    active-expenses-context route (_expense_delete_active_context_gate
    matched, active_list_context[chat_id]=="expenses") — both need the
    exact same "fetch live candidates, resolve, preview" behavior, so the
    active-context route reuses this function directly instead of
    duplicating it. _current_expense_origin(chat_id) below already resolves
    correctly for either caller.
    """
    origin = _current_expense_origin(chat_id)
    keyboard = _expense_origin_keyboard(origin)
    try:
        household_id, user_db_id = _bot.get_household_and_user(user_id, display_name)
        recent_expenses = _bot.get_recent_expenses_for_deletion(household_id, limit=10)
        if not recent_expenses:
            _bot.send_message(chat_id, "Витрат поки немає.", reply_markup=keyboard)
            return
        _resolve_expense_delete_selection(
            chat_id, household_id, user_db_id, origin, keyboard, text, recent_expenses
        )
    except Exception:
        _bot.send_message(chat_id, "Не вдалося обробити видалення витрати. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


def _handle_expense_delete_selection_text(chat_id, text):
    """Free text while a numbered recent-expense list is already on screen
    (button press, or an earlier ambiguous global-gate attempt) — resolved
    against that SAME stored list so numbering never shifts mid-conversation."""
    data = expense_delete_selection.pop(chat_id, None)
    if data is None:
        return
    _resolve_expense_delete_selection(
        chat_id, data["household_id"], data["user_db_id"], data["origin"],
        _expense_origin_keyboard(data["origin"]), text, data["expenses"],
    )


# =========================
# CONFIRM / CANCEL BUTTON HANDLERS
# =========================
def handle_add_confirm(chat_id):
    """"✅ Так, додати" button. Pops the pending add-preview BEFORE the
    database write, so a duplicate/late button press can never create a
    second expense; performs the insert exactly once."""
    if chat_id in pending_expense:
        data = pending_expense.pop(chat_id)
        origin = data.get("origin", "global")
        keyboard = _expense_origin_keyboard(origin)
        try:
            _bot.add_expense(
                data["household_id"], data["user_db_id"], data["amount"], data["currency"],
                data["category"], data["description"], data["expense_date"],
            )
            _bot.send_message(chat_id, "✅ Витрату додано.", reply_markup=keyboard)
        except Exception:
            _bot.send_message(chat_id, "Не вдалося зберегти витрату. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)
    else:
        _bot.send_message(chat_id, "Немає активної дії для підтвердження.")


def handle_delete_confirm(chat_id):
    """"✅ Так, видалити" button, expense-delete branch. Caller (bot.py) only
    invokes this when chat_id is already known to be in pending_expense_delete
    — pops the pending delete preview before the DB check-and-delete
    transaction, so a duplicate/late button press can never delete twice."""
    data = pending_expense_delete.pop(chat_id)
    origin = data.get("origin", "global")
    keyboard = _expense_origin_keyboard(origin)
    try:
        _bot.delete_expense(data["household_id"], data["expense_id"], data["snapshot"])
        _bot.send_message(chat_id, "✅ Витрату видалено.", reply_markup=keyboard)
    except StaleSnapshotError:
        _bot.send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=keyboard)
    except Exception:
        _bot.send_message(chat_id, "Не вдалося видалити витрату. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


def handle_cancel(chat_id):
    """"❌ Скасувати" button. Caller (bot.py) only invokes this when chat_id
    is already known to be in one of pending_expense/pending_expense_delete/
    expense_delete_selection; replicates the exact original 3-way check order
    (add preview, delete preview, delete-selection mode)."""
    if chat_id in pending_expense:
        expense_data = pending_expense.pop(chat_id, None)
        origin = (expense_data or {}).get("origin", "global")
        _bot.send_message(chat_id, "Додавання витрати скасовано.", reply_markup=_expense_origin_keyboard(origin))
    elif chat_id in pending_expense_delete:
        delete_data = pending_expense_delete.pop(chat_id, None)
        origin = (delete_data or {}).get("origin", "global")
        _bot.send_message(chat_id, "Видалення витрати скасовано.", reply_markup=_expense_origin_keyboard(origin))
    elif chat_id in expense_delete_selection:
        selection_data = expense_delete_selection.pop(chat_id, None)
        origin = (selection_data or {}).get("origin", "global")
        _bot.send_message(chat_id, "Видалення витрати скасовано.", reply_markup=_expense_origin_keyboard(origin))
