import json
import os
import re
import sys
import unicodedata
from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from flask import Flask, request
from dotenv import load_dotenv
from groq import Groq
import requests
from database import (
    init_db,
    get_or_create_household,
    get_or_create_user,
    get_active_shopping_items,
    add_shopping_items_batch,
    get_inventory_items,
    add_inventory_items_batch,
    add_or_merge_inventory_item,
    mark_items_batch,
    delete_items_batch,
    delete_inventory_items_batch,
    apply_inventory_consumption,
    apply_compound_inventory_operations,
    apply_inventory_reconciliation,
    execute_merge_shopping,
    execute_merge_inventory,
    execute_inventory_cleanup_merge,
    execute_inventory_rename,
    execute_inventory_delete,
    update_shopping_items_batch,
    update_inventory_items_batch,
    save_list_context,
    get_list_context,
    clear_list_context,
    StaleSnapshotError,
    get_household_alias_map,
    get_household_alias,
    list_household_aliases,
    create_or_update_household_alias,
    delete_household_alias,
    delete_household_aliases_batch,
    add_expense,
    get_recent_expenses,
    get_expense_month_summary,
    get_expense_day_total,
    get_recent_expenses_for_deletion,
    delete_expense,
    apply_global_household_operations,
    get_latest_undoable_action,
    apply_undo_action,
)
import expenses
import household_router
import action_history
import quantities
from quantities import STRUCTURED_UNITS, format_quantity_display, merge_quantity_values, _UNIT_ALIASES
import inventory
from inventory import (
    find_inventory_representation_matches,
    classify_inventory_representation,
    resolve_inventory_representation,
    format_representation_clarify_message,
    format_global_quantity_clarification_message,
    format_representation_separate_warning,
    format_representation_merge_line,
    format_representation_merge_quantity_fragment,
    detect_count_vs_mass_volume_conflict,
    detect_add_representation_v2_conflict,
    _resolve_consumption,
    _validate_consumptions,
    _format_consumption_preview,
    _UNIT_GROUP,
    _UNIT_TO_CANONICAL_FACTOR,
    _CANONICAL_UNIT_FOR_GROUP,
    _compound_snapshot_is_stale,
)
import list_editing
from list_editing import _compute_merged_quantity, _apply_pending_merge
import legacy_shopping_flow
import legacy_inventory_flow
import message_dispatcher
import interaction_state
import household_read_context
import meal_ideas

STALE_PREVIEW_MSG = "Список змінився з іншого пристрою. Онови список і повтори дію."

# =========================
# ENV
# =========================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = set(int(i.strip()) for i in _raw_allowed.split(",") if i.strip().isdigit())

print("GROQ LOADED:", GROQ_API_KEY is not None)
print("GEMINI LOADED:", GEMINI_API_KEY is not None)
print("ACCESS RESTRICTED:", bool(ALLOWED_USER_IDS))

try:
    init_db()
    print("DATABASE READY: True")
except Exception:
    print("DATABASE READY: False")

# =========================
# AI CLIENTS
# =========================
client = Groq(api_key=GROQ_API_KEY)

GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
GEMINI_COOKING_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# =========================
# MEMORY
# =========================
user_history = {}
waiting_for_ingredients = {}
active_list_context = {}      # chat_id -> "shopping" | "inventory"
# shopping_mode/pending_batch/pending_mark_batch/pending_delete_batch now live in
# legacy_shopping_flow.py — re-exported below (same dict objects, not copies) so
# every existing test that does `from bot import shopping_mode` etc. keeps working.
shopping_mode = legacy_shopping_flow.shopping_mode
pending_batch = legacy_shopping_flow.pending_batch
pending_mark_batch = legacy_shopping_flow.pending_mark_batch
pending_delete_batch = legacy_shopping_flow.pending_delete_batch
pending_merge = {}            # chat_id -> {groups, household_id, user_db_id, list_type}
# inventory_mode/pending_inventory_batch/pending_remove_batch now live in
# legacy_inventory_flow.py — re-exported below (same dict objects, not
# copies) so every existing test that does `from bot import inventory_mode`
# etc. keeps working.
inventory_mode = legacy_inventory_flow.inventory_mode
pending_inventory_batch = legacy_inventory_flow.pending_inventory_batch
pending_remove_batch = legacy_inventory_flow.pending_remove_batch
saved_list_context = {}       # chat_id -> "shopping_saved" | "inventory_saved"
pending_saved_edit = {}       # chat_id -> {items_snapshot, validated_updates, household_id, user_db_id, context_type}
pending_quick_purchase = {}   # chat_id -> {items, ignored_items, household_id, user_db_id}
pending_inventory_consumption = {}  # chat_id -> {resolved, household_id, user_db_id}
pending_compound_inventory = {}  # chat_id -> {inventory_changes, add_to_shopping, household_id, user_db_id}
pending_inventory_reconciliation = {}  # chat_id -> {updates, additions, deletes, household_id, user_db_id}
pending_inventory_reconciliation_clarify = {}  # chat_id -> {ambiguous_group, rest, household_id, user_db_id}
pending_alias_action = {}     # chat_id -> {kind: "create"|"update"|"delete", household_id, user_db_id, alias_text, target_display_name, alias_normalized (delete only)}
# Inventory Cleanup read-only-warning notice — set when "об'єднай X в
# запасах" finds duplicate rows but none are safely auto-mergeable (see
# _start_inventory_cleanup's "not validated_groups" branch): nothing was
# written and there's no pending_merge entry to confirm/cancel, but the very
# next "↩️ Скасувати останню дію"/"❌ Скасувати" press must acknowledge THIS
# read-only check instead of silently falling through to an unrelated older
# historical undo. Also doubles (Inventory Cleanup Admin v1) as a small
# contextual hint for an immediate follow-up rename/delete request ("прибери
# сосисок пару" right after this warning) — see _start_inventory_delete's
# candidate-narrowing fallback — though the normal live-inventory candidate
# search already resolves that exact case on its own.
pending_cleanup_notice = {}   # chat_id -> {"rows": [row, ...], "household_id": id}
# Inventory Cleanup Admin v1 — awaiting confirm/cancel on a rename/delete
# preview for ONE existing inventory row ("перейменуй ser на сир", "видали
# mlekо із запасів"). Same "✅ Так, застосувати"/"❌ Скасувати" button pair
# and journal/undo path as pending_global_household (see database.
# execute_inventory_rename/execute_inventory_delete) — a SEPARATE dict
# rather than folded into pending_global_household's own payload shape,
# since that shape (add/consume/expense operations) has nothing in common
# with a single-row rename/delete and _apply_global_household_confirm's
# existing contract shouldn't need to branch on a whole new operation kind.
pending_cleanup_admin = {}   # chat_id -> {action: "rename"|"delete", household_id, user_db_id, origin,
                              #             item_id, target, new_name (rename only), new_canonical_name (rename only)}
# Inventory Cleanup Admin v1 — awaiting a follow-up reply that disambiguates
# a rename/delete request that matched 2+ inventory rows (see
# _start_inventory_rename/_start_inventory_delete's "len(candidates) > 1"
# branch). Holds only the already-fetched candidate ROW SNAPSHOTS (never
# re-queried until the user's follow-up narrows it to exactly one — the
# eventual pending_cleanup_admin preview built from that one row still goes
# through the same StaleSnapshotError-protected write as every other rename/
# delete). A follow-up like "Mleko 1 шт"/"1 шт"/"№2"/"2" is resolved via
# inventory.resolve_cleanup_admin_disambiguation_reply; anything that still
# doesn't uniquely identify one row re-asks with the same candidate list
# instead of ever falling through to general AI-chat.
pending_cleanup_admin_disambiguation = {}  # chat_id -> {action: "rename"|"delete", candidates: [row, ...],
                              #             new_phrase (rename only, raw), household_id, user_db_id, origin}
# Destructive Bulk Household Request Guard v1 — awaiting a follow-up reply
# to the guard's own "покупки чи запаси?" clarification (see
# _route_destructive_bulk_guard). Deliberately tiny/ephemeral: no household_
# id/user_db_id (nothing is EVER written from this state, so there is
# nothing to re-verify at write time), just enough to restore the right
# keyboard. A recognized destination word ("покупки"/"запаси"/...) or a
# cancel is resolved by _continue_destructive_guard; any OTHER reply simply
# releases this context and lets normal routing continue for that SAME
# message, exactly like a real bulk-clear command never having been asked
# about in the first place.
pending_destructive_guard = {}  # chat_id -> {"origin": origin}
# Expense pending state now lives in expenses.py — re-exported here (same
# dict objects, not copies) so every existing bot.<name> reference/test
# keeps working unchanged.
pending_expense = expenses.pending_expense
pending_expense_delete = expenses.pending_expense_delete
expense_delete_selection = expenses.expense_delete_selection
# Global Household Router v1 — pending state lives in bot.py (household_router.py
# owns no Telegram/pending state of its own, see its module docstring).
pending_global_household = {}  # chat_id -> {add_shopping_items, add_inventory_items,
                                #             consume_changes, new_expense, delete_expense,
                                #             inventory_targets, household_id, user_db_id, origin}
# Inventory Quantity Clarification v1 — short-lived RAM-only continuation
# state for the ONE case where the Global Household Router blocked the
# whole request because an inferred incoming inventory quantity conflicted
# with every existing representation of that product (see
# household_router.apply_inventory_representation_guard's "clarify"
# outcome). Holds only structured data, never raw text/Gemini history, so
# the next plain-text reply (e.g. "1Л") can safely continue the ORIGINAL
# command instead of falling through to general AI-chat.
pending_inventory_quantity_clarification = {}  # chat_id -> {household_id, user_db_id, origin,
                                #             item_name, canonical_name, category,
                                #             add_shopping_items, add_inventory_items,
                                #             consume_changes, new_expense, delete_expense}

# Inventory Representation Clarification V2 — short-lived RAM-only
# continuation state for the ONE conflict shape the Inventory Representation
# Guard can't safely resolve on its own: an existing structured count
# ("шт.") row against an EXPLICIT incoming mass/volume quantity for the same
# product (add or consume side — see household_router.
# detect_count_vs_mass_volume_conflict/detect_add_representation_v2_conflict).
# Holds only structured data, never raw text/Gemini history, so a reply
# (a button choice, then optionally a total-quantity number) can safely
# continue the ORIGINAL command instead of falling through to general
# AI-chat. "queue" holds any further such conflicts found in the same
# message, resolved one at a time; "representation_resolutions" accumulates
# the resolved entries for the eventual combined preview.
pending_inventory_representation_clarification = {}  # chat_id -> {household_id, user_db_id, origin,
                                #             stage ("choice"|"awaiting_total"), conflict, queue,
                                #             add_shopping_items, add_inventory_items,
                                #             inventory_merge_targets, consume_changes,
                                #             new_expenses, new_expense, delete_expense,
                                #             representation_resolutions}

# Global Bare Add v1 — short-lived RAM-only continuation state for a bare
# "Додай молоко" (no destination phrase) typed from a menu that doesn't
# already imply one (main menu, aliases, expenses). Holds only the already
# Gemini-parsed-and-validated item payloads (household_router.
# parse_bare_add_items), never raw text/Gemini history/database ids, so the
# next reply ("До покупок"/"У запаси") can build the preview via
# household_router.build_add_preview_from_items without a second Gemini call.
pending_add_destination_clarification = {}  # chat_id -> {household_id, user_db_id, origin, validated_items}

# Action History + Safe Undo v1 — awaiting confirm/cancel on an "↩️ Скасувати
# останню дію" preview. Holds only the journal row id and the identity
# needed to re-verify it belongs to this same user/household at confirm
# time — the actual restore plan lives in the journal row itself
# (before_snapshot/post_action_snapshot), re-read fresh inside
# apply_undo_action's own transaction, never trusted from this dict.
pending_undo_action = {}  # chat_id -> {action_id, household_id, user_db_id}

# Gate-blocking pending-state groups and the guard predicates built on top of
# them now live in interaction_state.py (called via _interaction_state_deps,
# built further down once every dict/callback it needs exists) — these are
# thin compatibility wrappers so every existing call site/test patch of the
# same name keeps working unchanged.
def _has_blocking_pending_state(chat_id):
    return interaction_state.has_blocking_pending_state(_interaction_state_deps, chat_id)


def _has_blocking_pending_state_for_expense(chat_id):
    return interaction_state.has_blocking_pending_state_for_expense(_interaction_state_deps, chat_id)


def _has_blocking_pending_state_for_reports(chat_id):
    return interaction_state.has_blocking_pending_state_for_reports(_interaction_state_deps, chat_id)


def _has_blocking_pending_state_for_expense_delete(chat_id):
    return interaction_state.has_blocking_pending_state_for_expense_delete(_interaction_state_deps, chat_id)


def _has_active_expense_preview(chat_id):
    return interaction_state.has_active_expense_preview(_interaction_state_deps, chat_id)


# Undo-Button-Cancels-Active-Operation v1 — the exact "↩️ Скасувати останню
# дію" button must cancel an unfinished command/clarification/preview for
# THIS chat before it's ever allowed to open the historical undo preview.
# Deliberately narrower than interaction_state's alias-gate group: only the
# states reachable at the point message_dispatcher.py checks this (quantity/
# representation clarification, the combined global-household preview,
# add-destination clarification, a pending saved-list edit) — pending_batch/
# pending_inventory_batch/reconciliation-clarify/an active expense preview
# are already intercepted earlier in that same route order and never reach
# this check at all, so including them here would be dead code.
def _has_active_pending_clarification_or_preview(chat_id):
    return (
        chat_id in pending_cleanup_notice
        or chat_id in pending_inventory_quantity_clarification
        or chat_id in pending_inventory_representation_clarification
        or chat_id in pending_global_household
        or chat_id in pending_add_destination_clarification
        or chat_id in pending_saved_edit
        or chat_id in pending_cleanup_admin
        or chat_id in pending_cleanup_admin_disambiguation
        or chat_id in pending_destructive_guard
    )


def _cancel_active_pending_operation(chat_id):
    """Pops whichever of the states above is active for this chat and
    replies with one shared cancellation message — same pop()/keyboard
    choice as the matching "❌ Скасувати" branch in
    _try_handle_confirm_or_cancel, just without that branch's own
    per-flow-specific message text."""
    if chat_id in pending_cleanup_notice:
        # A read-only "об'єднай X в запасах" check found nothing safe to
        # auto-merge — no DB write happened, so acknowledge that instead of
        # the generic "Поточну дію скасовано." wording, which would wrongly
        # imply something was undone.
        pending_cleanup_notice.pop(chat_id, None)
        send_message(chat_id, CLEANUP_NOTICE_ACKNOWLEDGED_MSG, reply_markup=INVENTORY_KEYBOARD)
        return
    if chat_id in pending_destructive_guard:
        # The destructive guard's own "покупки чи запаси?" question — no DB
        # write happened, so acknowledge with its own dedicated wording
        # instead of the generic "Поточну дію скасовано.".
        data = pending_destructive_guard.pop(chat_id, None)
        keyboard = household_router.origin_keyboard((data or {}).get("origin", "global"))
        send_message(chat_id, DESTRUCTIVE_GUARD_CANCELLED_MSG, reply_markup=keyboard)
        return
    if chat_id in pending_inventory_quantity_clarification:
        data = pending_inventory_quantity_clarification.pop(chat_id, None)
        keyboard = household_router.origin_keyboard((data or {}).get("origin", "global"))
    elif chat_id in pending_inventory_representation_clarification:
        data = pending_inventory_representation_clarification.pop(chat_id, None)
        keyboard = household_router.origin_keyboard((data or {}).get("origin", "global"))
    elif chat_id in pending_global_household:
        data = pending_global_household.pop(chat_id, None)
        keyboard = household_router.origin_keyboard((data or {}).get("origin", "global"))
    elif chat_id in pending_add_destination_clarification:
        data = pending_add_destination_clarification.pop(chat_id, None)
        keyboard = household_router.origin_keyboard((data or {}).get("origin", "global"))
    elif chat_id in pending_saved_edit:
        edit_data = pending_saved_edit.pop(chat_id, None)
        ctx = (edit_data or {}).get("context_type")
        keyboard = SHOPPING_KEYBOARD if ctx == "shopping_saved" else INVENTORY_KEYBOARD
    elif chat_id in pending_cleanup_admin:
        data = pending_cleanup_admin.pop(chat_id, None)
        keyboard = household_router.origin_keyboard((data or {}).get("origin", "global"))
    elif chat_id in pending_cleanup_admin_disambiguation:
        data = pending_cleanup_admin_disambiguation.pop(chat_id, None)
        keyboard = household_router.origin_keyboard((data or {}).get("origin", "global"))
    else:
        keyboard = MAIN_KEYBOARD
    send_message(chat_id, "Поточну дію скасовано.", reply_markup=keyboard)


_SEEN_UPDATE_IDS_MAXLEN = 1000
_seen_update_ids = deque(maxlen=_SEEN_UPDATE_IDS_MAXLEN)   # oldest-first, bounded
_seen_update_ids_set = set()                               # O(1) membership


def _is_duplicate_update(update_id):
    """Test-and-set idempotency guard for Telegram update_id.

    Returns True if this update_id was already seen (caller should short-circuit
    without re-processing or re-sending anything). Returns False and records the
    id for a new update_id. Process-local, in-memory, bounded to the most recent
    _SEEN_UPDATE_IDS_MAXLEN ids (oldest evicted first).
    """
    if update_id is None:
        return False
    if update_id in _seen_update_ids_set:
        return True
    if len(_seen_update_ids) >= _seen_update_ids.maxlen:
        oldest = _seen_update_ids.popleft()
        _seen_update_ids_set.discard(oldest)
    _seen_update_ids.append(update_id)
    _seen_update_ids_set.add(update_id)
    return False


SYSTEM_PROMPT = (
    "Ти корисний AI-помічник. Відповідай українською.\n"
    "У тебе немає доступу до інтернету в реальному часі: ніколи не стверджуй, що маєш доступ до інтернету, "
    "і не вигадуй поточну погоду, новини, курси валют, розклади рейсів чи інші дані, що потребують "
    "актуального інтернет-джерела.\n"
    "Якщо запитують поточну дату або час — використовуй надану нижче актуальну дату й час Europe/Warsaw "
    "як єдине надійне джерело.\n"
    "Якщо запитують погоду чи інші актуальні зовнішні дані — чесно відповідай: "
    "«У цій версії бота я не маю доступу до актуального прогнозу чи інтернет-пошуку, тому не хочу вигадувати дані.»\n"
    "Ніколи не пиши «Я зафіксував», «Я зберіг» або «Я оновив запаси», якщо в цьому чаті реально не відбулася "
    "підтверджена операція над базою даних. Не вигадуй зміни в PostgreSQL, обсяги упаковок, перерахунки "
    "одиниць виміру чи суми між несумісними одиницями — якщо не впевнений, чесно скажи, що не можеш це визначити.\n"
    "Це стосується й домашніх назв товарів (aliases): ти НІКОЛИ не маєш права стверджувати, що «запам'ятав», "
    "«зберіг» чи «оновив» домашню назву товару, або що вона тепер діятиме надалі. Створення, зміну й видалення "
    "домашніх назв виконує лише код бота після явного підтвердження користувача кнопкою — якщо такого "
    "підтвердження в цьому чаті не було, чесно скажи, що не можеш це зробити в звичайній розмові.\n"
    "Це стосується й витрат: ти НІКОЛИ не маєш права стверджувати, що витрату «записав», «додав» чи "
    "«зберіг», якщо в цьому чаті реально не відбулося підтвердженого користувачем кнопкою запису витрати."
)

_UA_WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
_UA_MONTHS_GENITIVE = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]
def get_warsaw_datetime_context(now=None):
    """Authoritative Europe/Warsaw date/time string for the general AI chat prompt.

    Pure: if now is given (a tz-aware datetime), it's used as-is instead of the
    real clock — this is what makes it unit-testable without mocking time.
    """
    if now is None:
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
    weekday = _UA_WEEKDAYS[now.weekday()]
    month = _UA_MONTHS_GENITIVE[now.month - 1]
    return (
        f"Актуальна локальна дата й час: {now.day} {month} {now.year}, {weekday}, "
        f"{now.strftime('%H:%M')}, Europe/Warsaw.\n"
        "Це єдине надійне джерело поточного часу для відповіді."
    )

COOKING_SYSTEM_PROMPT = (
    "Ти кулінарний помічник. Користувач надсилає перелік продуктів, які є вдома. "
    "Запропонуй максимум 3 реалістичні страви з цих продуктів. "
    "Якщо є м'ясо, риба, яйця, сир, вершки або овочі — не пропонуй десерт, якщо користувач прямо не просить солодке. "
    "«Сливки» поруч із куркою, сиром або м'ясом трактуй як вершки. "
    "Не вигадуй продукти, яких немає в списку, крім солі, перцю, олії та води. "
    "Для кожної страви вкажи: назву, короткі кроки приготування, приблизний час. "
    "Не радь мити сиру курку або іншу сиру птицю під краном. "
    "Відповідай природною українською мовою."
)

DB_ERROR_MSG = "Не вдалося виконати дію зі списком покупок. Спробуйте ще раз трохи пізніше."
INVENTORY_ERROR_MSG = "Не вдалося виконати дію із запасами. Спробуйте ще раз трохи пізніше."
SELECTION_ERROR_MSG = "Не зміг точно зрозуміти, які товари ти маєш на увазі. Спробуй написати інакше."

ALIAS_INTRO_TEXT = (
    "🧠 Домашні назви товарів\n\n"
    "Тут можна запам'ятати ваші звичні назви продуктів.\n\n"
    "Приклади:\n"
    "• Запам'ятай, що сливки = Вершки\n"
    "• Зміни: сливки = Вершки 30%\n"
    "• Покажи мої назви\n"
    "• Забудь, що сливки"
)

DEFAULT_CATEGORY = "Інше їстівне"

VALID_CATEGORIES = {
    "М'ясо та риба", "Молочне та яйця", "Овочі та зелень",
    "Фрукти та ягоди", "Хліб і випічка", "Крупи, макарони та борошно",
    "Соуси, спеції та бакалія", "Солодке та снеки",
    "Напої", "Заморожене", "Інше їстівне",
}

CATEGORY_ORDER = [
    "М'ясо та риба", "Молочне та яйця", "Овочі та зелень",
    "Фрукти та ягоди", "Хліб і випічка", "Крупи, макарони та борошно",
    "Соуси, спеції та бакалія", "Солодке та снеки",
    "Напої", "Заморожене", "Інше їстівне",
]

CATEGORY_EMOJIS = {
    "М'ясо та риба":              "🥩",
    "Молочне та яйця":            "🥛",
    "Овочі та зелень":            "🥦",
    "Фрукти та ягоди":            "🍎",
    "Хліб і випічка":             "🍞",
    "Крупи, макарони та борошно": "🌾",
    "Соуси, спеції та бакалія":   "🧂",
    "Солодке та снеки":           "🍫",
    "Напої":                      "🥤",
    "Заморожене":                 "🧊",
    "Інше їстівне":               "🛒",
}

# Expense constants now live in expenses.py — re-exported here so existing
# bot.<name> references/tests keep working unchanged.
EXPENSES_INTRO_TEXT = expenses.EXPENSES_INTRO_TEXT
DEFAULT_EXPENSE_CATEGORY = expenses.DEFAULT_EXPENSE_CATEGORY
EXPENSE_CATEGORIES = expenses.EXPENSE_CATEGORIES
VALID_EXPENSE_CATEGORIES = expenses.VALID_EXPENSE_CATEGORIES
EXPENSE_MAX_AMOUNT = expenses.EXPENSE_MAX_AMOUNT
EXPENSE_DESCRIPTION_MAX_LEN = expenses.EXPENSE_DESCRIPTION_MAX_LEN
EXPENSE_GATE_UNRECOGNIZED_MSG = expenses.EXPENSE_GATE_UNRECOGNIZED_MSG
EXPENSE_PREVIEW_GUARD_MSG = expenses.EXPENSE_PREVIEW_GUARD_MSG

SHOPPING_PARSE_PROMPT = (
    "Розбий текст на список продуктів для покупки. Правила:\n"
    "- розділяй позиції за новими рядками, комами, крапками з комою або природними розділеннями;\n"
    "- «Мисливські ковбаски 4» — це ОДИН товар із кількістю «4 шт.», не два;\n"
    "- is_consumable: true лише для їжі, напоїв, спецій та соусів; "
    "навушники, батарейки, побутова хімія, засоби гігієни, посуд, інструменти, електроніка → false;\n"
    "- виправляй лише очевидні орфографічні помилки; was_corrected: true якщо виправив, інакше false;\n"
    "- не вигадуй товари, яких немає в тексті;\n"
    "- нормалізуй одиниці: «500 грам» → «500 г», «2 штуки» → «2 шт.», «1.5 л» → «1,5 л», «півтора літри» → «1,5 л»;\n"
    "- якщо вказано лише число, додавай одиницю тільки коли це очевидно: "
    "штучні товари (сосиски, яйця, ковбаски) → «шт.», рідини (молоко, вершки, кефір) → «л»; "
    "якщо неясно — лишай число без одиниці;\n"
    "- з name прибирай ЛИШЕ слова про кількість чи тару — прикметники й означення товару (колір, смак, вид: "
    "«зелений», «чорний», «кокосовий», «рисовий», «грецький», «червоний», «білий», «кисломолочний», "
    "«вершковий», «мисливські», «тестовий» тощо) є частиною назви товару і їх ЗАВЖДИ треба залишати в name "
    "(у називному відмінку): «зеленого чаю» → name «Зелений чай»; «кокосового молока» → name «Кокосове "
    "молоко»; «грецького йогурту» → name «Грецький йогурт»; «червоної квасолі» → name «Червона квасоля». "
    "НІКОЛИ не скорочуй name до одного загального іменника (напр. «Чай», «Молоко», «Йогурт»), якщо в "
    "оригінальному тексті перед іменником був прикметник чи означення;\n"
    "- category — одна з: М'ясо та риба, Молочне та яйця, Овочі та зелень, Фрукти та ягоди, "
    "Хліб і випічка, Крупи макарони та борошно, Соуси спеції та бакалія, Солодке та снеки, "
    "Напої, Заморожене, Інше їстівне;\n"
    "- ignored_items — оригінальні назви позицій з тексту, де is_consumable=false.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без жодного додаткового тексту:\n"
    '{"items":['
    '{"name":"Молоко","quantity_text":"1,5 л","category":"Молочне та яйця","was_corrected":false,"is_consumable":true},'
    '{"name":"Зелений чай","quantity_text":"1 шт.","category":"Напої","was_corrected":false,"is_consumable":true}'
    '],"ignored_items":["Навушники"]}'
)

# Inventory-only mirror of SHOPPING_PARSE_PROMPT (kept as a separate copy —
# shopping's own prompt/flow is deliberately untouched). Adds one extra rule
# (word-numbers and container/package words must stay verbatim in
# quantity_text, never converted to a digit or left inside name) plus
# matching examples, so a phrase like "дві пачки сосисок" is never silently
# turned into "Сосиски — 2 шт." — see normalize_item_quantity/
# _parse_structured_quantity for how quantity_text is turned into
# quantity_value/quantity_unit/quantity_inferred afterward in Python.
INVENTORY_PARSE_PROMPT = (
    "Розбий текст на список продуктів для запасів удома. Правила:\n"
    "- розділяй позиції за новими рядками, комами, крапками з комою або природними розділеннями;\n"
    "- «Мисливські ковбаски 4» — це ОДИН товар із кількістю «4 шт.», не два;\n"
    "- назва (name) НІКОЛИ не містить слів про кількість чи тару. Слово-числівник («дві», «три», «чотири», "
    "«пара», «пару» тощо) або назву тари («пачка», «пачки», «пачок», «упаковка», «упаковки», «упаковок») "
    "завжди лишай ТОЧНО як у тексті в quantity_text — ніколи не перетворюй на цифру і не вигадуй для них "
    "одиницю виміру («шт.», «г» тощо): «дві пачки сосисок» → name «Сосиски», quantity_text «дві пачки»; "
    "«пачка макаронів» → name «Макарони», quantity_text «пачка»; «три упаковки йогурту» → name «Йогурт», "
    "quantity_text «три упаковки»; «пару сосисок» → name «Сосиски», quantity_text «пару» (НЕ «2»);\n"
    "- прикметники й означення товару (колір, смак, вид: «зелений», «чорний», «кокосовий», «рисовий», "
    "«грецький», «червоний», «білий», «кисломолочний», «вершковий», «мисливські», «тестовий» тощо) — це "
    "частина назви товару, а не кількість чи тара, і їх ЗАВЖДИ треба залишати в name (у називному "
    "відмінку): «2 л кокосового молока» → name «Кокосове молоко», quantity_text «2 л»; «дві пачки "
    "мисливських ковбасок» → name «Мисливські ковбаски», quantity_text «дві пачки». НІКОЛИ не скорочуй "
    "name до одного загального іменника (напр. «Молоко», «Ковбаски»), якщо в оригінальному тексті перед "
    "іменником був прикметник чи означення;\n"
    "- is_consumable: true лише для їжі, напоїв, спецій та соусів; "
    "навушники, батарейки, побутова хімія, засоби гігієни, посуд, інструменти, електроніка → false;\n"
    "- виправляй лише очевидні орфографічні помилки; was_corrected: true якщо виправив, інакше false;\n"
    "- не вигадуй товари, яких немає в тексті;\n"
    "- нормалізуй одиниці: «500 грам» → «500 г», «2 штуки» → «2 шт.», «1.5 л» → «1,5 л», «півтора літри» → «1,5 л»;\n"
    "- якщо вказано лише число (без слова-тари), додавай одиницю тільки коли це очевидно: "
    "штучні товари (сосиски, яйця, ковбаски, банани) → «шт.», рідини (молоко, вершки, кефір) → «л»; "
    "якщо неясно — лишай число без одиниці;\n"
    "- category — одна з: М'ясо та риба, Молочне та яйця, Овочі та зелень, Фрукти та ягоди, "
    "Хліб і випічка, Крупи макарони та борошно, Соуси спеції та бакалія, Солодке та снеки, "
    "Напої, Заморожене, Інше їстівне;\n"
    "- ignored_items — оригінальні назви позицій з тексту, де is_consumable=false.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без жодного додаткового тексту:\n"
    '{"items":['
    '{"name":"Сосиски","quantity_text":"дві пачки","category":"М\'ясо та риба","was_corrected":false,"is_consumable":true},'
    '{"name":"Банани","quantity_text":"3","category":"Фрукти та ягоди","was_corrected":false,"is_consumable":true},'
    '{"name":"Мисливські ковбаски","quantity_text":"дві пачки","category":"М\'ясо та риба","was_corrected":false,"is_consumable":true}'
    '],"ignored_items":["Навушники"]}'
)

SELECTION_PROMPT = (
    "Визнач, які позиції зі списку користувач хоче вибрати.\n"
    "Правила інтерпретації:\n"
    "- «всі», «все», «усе», «прибери все», «видали все», «все купив» тощо → всі номери списку\n"
    "- «все крім X» або «залиш X, решту прибери» → всі номери, крім позицій, що відповідають X\n"
    "- числа і діапазони: «1 2 3», «1-4», «2, 5-7» → відповідні номери\n"
    "- назви або фрази → знайди відповідні позиції за назвою або змістом\n"
    "Правила відповіді:\n"
    "- Відповідай ТІЛЬКИ валідним JSON без жодного тексту: "
    "{\"selected_numbers\": [1, 3, 5], \"unresolved_fragments\": []}\n"
    "- Вказуй тільки номери, які є в списку; без дублікатів; за зростанням\n"
    "- Якщо нічого не підходить — відповідай {\"selected_numbers\": [], \"unresolved_fragments\": []}\n"
    "- Якщо частину повідомлення не можна однозначно перетворити на позицію(ї) зі списку — постав ці "
    "фрагменти в unresolved_fragments (масив рядків) і не вгадуй позицію. Завжди повертай це поле, "
    "навіть порожнім масивом\n"
    "Приклад із нерозпізнаним фрагментом («Видали молоко і те довге м'ясо», де в списку є лише "
    "позиція 1 «Молоко»):\n"
    "{\"selected_numbers\": [1], \"unresolved_fragments\": [\"те довге м'ясо\"]}\n"
)

INTENT_ROUTER_PROMPT = (
    "Ти аналізуєш список товарів і визначаєш:\n"
    "1. Чи хоче користувач об'єднати однакові або дублюючі позиції?\n"
    "2. Якщо так — які позиції можна безпечно об'єднати?\n\n"
    "Фрази об'єднання: «об'єднай», «злий дублікати», «прибери дублікати», «згрупуй повтори», "
    "«зроби однакові однією позицією» та подібні за змістом.\n\n"
    "Правила об'єднання:\n"
    "- Об'єднуй лише якщо назви означають той самий продукт\n"
    "- Категорія однакова, або одна з них — «Інше їстівне»\n"
    "- Якщо обидві кількості мають однакову одиницю (л, мл, г, кг, шт.) — склади їх\n"
    "- Якщо одна кількість порожня, а інша має значення → merged_quantity_text = непорожнє значення\n"
    "- Якщо обидві порожні → merged_quantity_text = \"\"\n"
    "- Не об'єднуй: різні важливі уточнення в назві («Вершки 18%» і «Вершки 30%»), різні одиниці\n"
    "- Не вигадуй кількості\n"
    "- У item_refs вказуй числа з рядків у форматі «#N»\n\n"
    "Якщо користувач НЕ просить об'єднати → {\"intent\": \"none\", \"merge_groups\": []}\n"
    "Якщо просить, але безпечних дублікатів немає → {\"intent\": \"merge_duplicates\", \"merge_groups\": []}\n\n"
    "Відповідай ТІЛЬКИ валідним JSON без жодного тексту:\n"
    "{\"intent\": \"merge_duplicates\", \"merge_groups\": [{\"item_refs\": [1, 2], \"merged_name\": \"Вершки\", \"merged_quantity_text\": \"\", \"merged_category\": \"Молочне та яйця\"}]}"
)

PENDING_PREVIEW_EDIT_PROMPT = (
    "Ти помічник для редагування pending preview списку товарів.\n"
    "Визнач намір (intent):\n"
    "- «edit_preview» — якщо користувач хоче змінити кількість, назву або категорію існуючих позицій\n"
    "- «merge_duplicates» — якщо хоче об'єднати однакові або дублюючі позиції\n"
    "- «none» — в усіх інших випадках\n\n"
    "Для edit_preview — поверни updates лише для позицій, які змінюються:\n"
    "- item_number — ціле число (номер у preview, від 1 до N)\n"
    "- name — нова назва або null якщо не змінюється\n"
    "- quantity_text — нова кількість (напр. «2 шт.», «500 г», «1,5 л») або null\n"
    "- category — нова категорія або null\n\n"
    "Не створюй нових позицій і не видаляй існуючих.\n"
    "Нормалізуй одиниці: «2 штуки» → «2 шт.», «500 грам» → «500 г», «1.5 л» → «1,5 л».\n\n"
    "Відповідай ТІЛЬКИ валідним JSON без жодного тексту:\n"
    "{\"intent\": \"edit_preview\", \"updates\": [{\"item_number\": 1, \"name\": null, \"quantity_text\": \"2 шт.\", \"category\": null}]}"
)

SAVED_LIST_EDIT_PROMPT = (
    "Ти помічник для роботи з відкритим збереженим списком товарів (список покупок або запасів).\n"
    "Визнач намір (intent):\n"
    "- «edit_saved_items» — якщо користувач хоче змінити кількість, назву або категорію наявних позицій\n"
    "- «merge_duplicates» — якщо хоче об'єднати однакові або дублюючі позиції\n"
    "- «start_action» — якщо хоче виконати дію над товарами зі списку: позначити купленими, "
    "видалити зі списку покупок або прибрати із запасів. Трактуй так само формулювання в минулому часі "
    "(«купив», «купили», «видалив», «прибрали») як запит на дію над поточним списком\n"
    "- «consume_inventory_quantity» — лише для контексту inventory_saved, якщо користувач повідомляє, "
    "що частково використав, з'їв, випив або витратив ЧАСТИНУ кількості товару, а не забрав/викинув/прибрав "
    "його повністю (напр. «Я з'їв 4 сосиски», «Використав одну приправу», «Випили 500 мл молока», "
    "«Витратив 200 г сиру», «Використав пів приправи до курки», «Випив пів літра молока», "
    "«З'їв половину пачки печива»). Ніколи не використовуй цей намір для shopping_saved. Якщо користувач хоче "
    "прибрати товар повністю («видали», «викинь», «прибери все, крім X») — це start_action з remove_inventory\n"
    "- «compound_inventory_operations» — лише для контексту inventory_saved, коли одне повідомлення "
    "поєднує КІЛЬКА РІЗНИХ дій одразу: часткове списання одних позицій, повне прибирання інших і/або "
    "додавання товару до списку покупок (напр. «Вершки зіпсувались, і я з'їв 4 сосиски, плюс додай молоко "
    "до покупок»). Використовуй цей намір лише коли повідомлення НЕ можна повністю описати одним із "
    "намірів вище. Ніколи не використовуй для shopping_saved\n"
    "- «reconcile_inventory_snapshot» — лише для контексту inventory_saved, коли користувач явно каже, "
    "що надсилає ПОВНИЙ актуальний список запасів замість поточного (напр. «Мої запаси виглядають зараз так», "
    "«Онови запаси за цим списком», «Звір мої запаси з цим списком», «Ось повний актуальний список запасів»), "
    "і після цієї фрази йде структурований перелік товарів. НІКОЛИ не використовуй цей намір для звичайної "
    "згадки продукту («Я люблю молоко»), питання («Що можна приготувати з сосисками?») чи одноразової покупки "
    "(«Сьогодні купив хліб.») — для цього є quick_add_to_inventory або none. Ніколи не використовуй для "
    "shopping_saved\n"
    "- «quick_add_to_inventory» — лише коли список порожній (позицій немає взагалі) і користувач "
    "повідомляє про продукти, які вже приніс/купив додому (напр. «Купив молоко і хліб», «Взяли сир»), "
    "навіть у минулому часі. Не використовуй цей намір, якщо є хоч одна позиція в списку, або текст — "
    "питання, план на майбутнє чи загальна фраза\n"
    "- «none» — в усіх інших випадках: додавання нових товарів у непорожній список, загальне питання, не стосується списку\n\n"
    "Для edit_saved_items — поверни updates лише для позицій, які змінюються:\n"
    "- item_number — ціле число (номер у списку, від 1 до N)\n"
    "- name — нова назва або null якщо не змінюється\n"
    "- quantity_text — нова кількість (напр. «2 шт.», «500 г», «1,5 л») або null\n"
    "- category — нова категорія або null\n\n"
    "Для merge_duplicates — поверни merge_groups: масив масивів item_number:\n"
    "- [[2, 4], [1, 3]] — кожна підгрупа містить номери позицій для об'єднання\n\n"
    "Для start_action — поверни action і selected_numbers:\n"
    "- action — одне з: «mark_bought» (позначити купленими — лише для списку покупок), "
    "«delete_shopping» (видалити зі списку покупок — лише для списку покупок), "
    "«remove_inventory» (прибрати із запасів — лише для запасів); обирай дію лише дозволену для поточного контексту\n"
    "- selected_numbers — номери обраних позицій за тими самими правилами, що й вибір позицій: "
    "«всі», «усе», «все куплено» тощо → всі номери; «все крім X» або «залиш X, решту...» → всі, крім X; "
    "числа й діапазони («1 2 3», «1-4»); назви або фрази → знайди відповідні позиції за назвою або змістом\n"
    "- Якщо частину повідомлення не можна однозначно перетворити на позицію(ї) для дії (наприклад "
    "«Видали молоко і те довге м'ясо», де впізнано лише молоко) — постав ці фрагменти в "
    "unresolved_fragments (масив рядків) і НЕ вгадуй позицію. Завжди повертай це поле для start_action, "
    "навіть порожнім масивом\n\n"
    "Для consume_inventory_quantity — поверни consumptions: масив об'єктів для позицій, з яких частково "
    "списується кількість:\n"
    "- item_number — ціле число (номер позиції)\n"
    "- quantity_value — додатне число, скільки саме використано\n"
    "- quantity_unit — одне з «шт.», «л», «мл», «г», «кг» — одиниця, у якій вказано використане\n"
    "- Якщо частину повідомлення не можна однозначно перетворити на consumptions — постав ці фрагменти "
    "в unresolved_fragments (масив рядків) і не вгадуй кількість. Завжди повертай це поле для "
    "consume_inventory_quantity, навіть порожнім масивом\n\n"
    "Для compound_inventory_operations — поверни operations: масив об'єктів, кожен з полем type:\n"
    "- {\"type\": \"remove_inventory\", \"item_number\": N} — повністю прибрати позицію N із запасів\n"
    "- {\"type\": \"consume_inventory_quantity\", \"item_number\": N, \"quantity_value\": число, "
    "\"quantity_unit\": одиниця} — частково списати кількість із позиції N\n"
    "- {\"type\": \"add_to_shopping\", \"name\": назва, \"quantity_value\": число або null, "
    "\"quantity_unit\": одиниця або null, \"quantity_inferred\": true/false, \"category\": категорія, "
    "\"is_consumable\": true} — додати новий товар до списку покупок\n"
    "Також для compound_inventory_operations поверни unresolved_fragments — масив рядків з фрагментами "
    "тексту, які ти НЕ зміг однозначно перетворити на одну з дозволених операцій. Не мовчи і не пропускай "
    "незрозумілу частину — обов'язково додай її сюди замість того, щоб її ігнорувати\n\n"
    "Для quick_add_to_inventory — поверни items: масив нових товарів, кожен з полями:\n"
    "- name — назва товару\n"
    "- canonical_name — назва в нижньому регістрі\n"
    "- quantity_value — число або null, якщо кількість не вказана явно\n"
    "- quantity_unit — одне з «шт.», «л», «мл», «г», «кг», або null\n"
    "- quantity_inferred — true, якщо кількість не вказана явно (тоді quantity_value=1, quantity_unit=«шт.»)\n"
    "- category — категорія товару\n"
    "- is_consumable — true лише для їжі, напоїв, спецій та соусів; побутові товари → false\n"
    "Для quick_add_to_inventory не вигадуй кількість: якщо явно не вказано число й одиницю — став "
    "quantity_value=1, quantity_unit=«шт.», quantity_inferred=true. «Молоко 2 л» → quantity_value=2, "
    "quantity_unit=«л», quantity_inferred=false.\n\n"
    "Для reconcile_inventory_snapshot — поверни items: масив УСІХ товарів із надісланого повного списку "
    "запасів, кожен з полями:\n"
    "- name — назва товару\n"
    "- canonical_name — назва в нижньому регістрі\n"
    "- quantity_value — число або null, якщо кількість не вказана явно для цієї позиції\n"
    "- quantity_unit — одне з «шт.», «л», «мл», «г», «кг», або null\n"
    "- quantity_inferred — true, якщо кількість не вказана явно (тоді quantity_value=1, quantity_unit=«шт.»)\n"
    "- category — категорія товару\n"
    "- is_consumable — true лише для їжі, напоїв, спецій та соусів; побутові товари → false\n"
    "Ти лише розбираєш список у JSON — не рахуй суми між різними одиницями, не вигадуй об'єм чи вагу упаковки, "
    "не пиши жодного тексту поза JSON. Незрозумілі фрагменти списку клади в unresolved_fragments, а не мовчи\n\n"
    "Правила:\n"
    "- Не додавай нових позицій і не видаляй існуючих через edit_saved_items чи merge_duplicates\n"
    "- Для start_action не повертай updates і merge_groups\n"
    "- Для consume_inventory_quantity не вигадуй кількість — використовуй тільки те число, яке явно назвав "
    "користувач, і не повертай updates, merge_groups, action, selected_numbers, items\n"
    "- Для compound_inventory_operations кожен item_number може зустрічатися лише в одній операції "
    "(не можна одночасно прибрати й списати частково ту саму позицію); не вигадуй кількість; "
    "не повертай updates, merge_groups, action, selected_numbers, items, consumptions\n"
    "- Для quick_add_to_inventory не повертай updates, merge_groups, action, selected_numbers\n"
    "- Для reconcile_inventory_snapshot не вигадуй кількість заднім числом для позицій, які й раніше не мали "
    "вказаної кількості — якщо кількість не вказана явно в новому списку, став quantity_inferred=true і не "
    "намагайся вгадати число\n"
    "- Для reconcile_inventory_snapshot не повертай updates, merge_groups, action, selected_numbers, consumptions, operations\n"
    "- Нормалізуй одиниці: «2 штуки» → «2 шт.», «500 грам» → «500 г», «1.5 л» → «1,5 л»\n"
    "- Нормалізуй дробові кількості: «пів», «половина», «пів пачки», «половинку», «половину пачки», "
    "«півлітра» → quantity_value 0.5 з відповідною одиницею вихідного товару "
    "(«з'їв половину пачки печива» → 0,5 шт., «випив півлітра молока» → 0,5 л); "
    "ніколи не округлюй 0,5 до 1 і не вигадуй одиницю, якщо вона не випливає з контексту\n"
    "- Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON\n\n"
    "Приклад edit_saved_items:\n"
    "{\"intent\": \"edit_saved_items\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [{\"item_number\": 1, \"name\": null, \"quantity_text\": \"2 шт.\", \"category\": null}], "
    "\"merge_groups\": [], \"items\": []}\n"
    "Приклад start_action:\n"
    "{\"intent\": \"start_action\", \"action\": \"mark_bought\", \"selected_numbers\": [1, 3], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], \"unresolved_fragments\": []}\n"
    "Приклад start_action із нерозпізнаним фрагментом "
    "(«Видали молоко і те довге м'ясо», де в списку є лише позиція 1 «Молоко»):\n"
    "{\"intent\": \"start_action\", \"action\": \"delete_shopping\", \"selected_numbers\": [1], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], \"unresolved_fragments\": [\"те довге м'ясо\"]}\n"
    "Приклад consume_inventory_quantity:\n"
    "{\"intent\": \"consume_inventory_quantity\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], "
    "\"consumptions\": [{\"item_number\": 2, \"quantity_value\": 4, \"quantity_unit\": \"шт.\"}], "
    "\"unresolved_fragments\": []}\n"
    "Приклад consume_inventory_quantity з половинною кількістю "
    "(«Я використав пів приправи до курки» для позиції «Приправа до курки — 2 шт.»):\n"
    "{\"intent\": \"consume_inventory_quantity\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], "
    "\"consumptions\": [{\"item_number\": 3, \"quantity_value\": 0.5, \"quantity_unit\": \"шт.\"}], "
    "\"unresolved_fragments\": []}\n"
    "Приклад compound_inventory_operations:\n"
    "{\"intent\": \"compound_inventory_operations\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": [], \"consumptions\": [], "
    "\"operations\": ["
    "{\"type\": \"remove_inventory\", \"item_number\": 3}, "
    "{\"type\": \"consume_inventory_quantity\", \"item_number\": 2, \"quantity_value\": 0.5, \"quantity_unit\": \"шт.\"}, "
    "{\"type\": \"add_to_shopping\", \"name\": \"Приправа до курки\", \"quantity_value\": 1, "
    "\"quantity_unit\": \"шт.\", \"quantity_inferred\": false, \"category\": \"Соуси, спеції та бакалія\", "
    "\"is_consumable\": true}"
    "], \"unresolved_fragments\": []}\n"
    "Приклад quick_add_to_inventory:\n"
    "{\"intent\": \"quick_add_to_inventory\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"items\": ["
    "{\"name\": \"Молоко\", \"canonical_name\": \"молоко\", \"quantity_value\": 1, \"quantity_unit\": \"шт.\", "
    "\"quantity_inferred\": true, \"category\": \"Молочне та яйця\", \"is_consumable\": true}]}\n"
    "Приклад reconcile_inventory_snapshot:\n"
    "{\"intent\": \"reconcile_inventory_snapshot\", \"action\": null, \"selected_numbers\": [], "
    "\"updates\": [], \"merge_groups\": [], \"consumptions\": [], \"operations\": [], "
    "\"items\": ["
    "{\"name\": \"Молоко\", \"canonical_name\": \"молоко\", \"quantity_value\": 5.5, \"quantity_unit\": \"л\", "
    "\"quantity_inferred\": false, \"category\": \"Молочне та яйця\", \"is_consumable\": true}, "
    "{\"name\": \"Йогурт\", \"canonical_name\": \"йогурт\", \"quantity_value\": 1, \"quantity_unit\": \"шт.\", "
    "\"quantity_inferred\": true, \"category\": \"Молочне та яйця\", \"is_consumable\": true}"
    "], \"unresolved_fragments\": []}"
)

ALIAS_ROUTER_PROMPT = (
    "Ти помічник для керування домашніми назвами товарів (aliases) — персональними правилами "
    "«моя назва → цільова назва товару» для одного домашнього господарства.\n"
    "Тобі може бути наданий поточний список збережених домашніх назв (номер, назва, ціль).\n"
    "Визнач намір (intent):\n"
    "- «create_or_update» — користувач хоче запам'ятати або змінити ОДНЕ правило "
    "(напр. «Запам'ятай, що сливки = Вершки», «Зміни: сливки = Вершки 30%», «сливки — це вершки»)\n"
    "- «delete» — користувач хоче видалити ОДНЕ правило за назвою "
    "(напр. «Забудь, що сливки», «Видали назву сливки»)\n"
    "- «delete_aliases» — користувач хоче видалити КІЛЬКА або ВСІ домашні назви одразу "
    "(напр. «Видали всі назви», «Забудь усі домашні назви», «Видали всі назви, крім сливки», "
    "«Залиш тільки сливки, решту домашніх назв видали», «Видали назви 1 і 3»)\n"
    "- «list» — користувач хоче побачити список збережених назв (напр. «Покажи мої назви», «Який список назв?»)\n"
    "- «none» — повідомлення не стосується керування назвами товарів\n\n"
    "Для create_or_update і delete поверни:\n"
    "- alias_text — назва, яку вводить користувач (наприклад «сливки»)\n"
    "- target_display_name — цільова назва товару, на яку замінюється alias_text "
    "(лише для create_or_update; для delete залиш null)\n\n"
    "Для delete_aliases поверни selected_numbers — масив номерів позицій з наданого списку, які треба "
    "видалити: «всі»/«усі» → номери всіх позицій; «всі, крім X» або «залиш X, решту видали» → номери "
    "всіх позицій, крім тієї що відповідає X; конкретні номери чи діапазони («1 і 3», «1-2») → відповідні "
    "номери. Ніколи не вигадуй номер, якого немає у наданому списку.\n\n"
    "Це ПРОСТО правило заміни назви. Ніколи не вигадуй і не змінюй кількість чи одиницю виміру товару — "
    "alias_text і target_display_name це лише текстові назви, без чисел кількості.\n"
    "Якщо ти не можеш однозначно визначити alias_text, target_display_name або selected_numbers — не вгадуй. "
    "Додай незрозумілий фрагмент тексту в unresolved_fragments (масив рядків) і залиш інші поля порожніми.\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON:\n"
    "{\"intent\": \"create_or_update\", \"alias_text\": \"сливки\", \"target_display_name\": \"Вершки\", "
    "\"selected_numbers\": [], \"unresolved_fragments\": []}\n"
    "Приклад delete:\n"
    "{\"intent\": \"delete\", \"alias_text\": \"сливки\", \"target_display_name\": null, "
    "\"selected_numbers\": [], \"unresolved_fragments\": []}\n"
    "Приклад delete_aliases (зі списком «1. сливки → Вершки», «2. приправа курка → Приправа до курки», "
    "«3. вершки для пасти → Вершки 30%» і повідомленням «Видали всі назви, крім сливки»):\n"
    "{\"intent\": \"delete_aliases\", \"alias_text\": null, \"target_display_name\": null, "
    "\"selected_numbers\": [2, 3], \"unresolved_fragments\": []}\n"
    "Приклад list:\n"
    "{\"intent\": \"list\", \"alias_text\": null, \"target_display_name\": null, "
    "\"selected_numbers\": [], \"unresolved_fragments\": []}\n"
    "Приклад none:\n"
    "{\"intent\": \"none\", \"alias_text\": null, \"target_display_name\": null, "
    "\"selected_numbers\": [], \"unresolved_fragments\": []}"
)

# Expense router prompt now lives in expenses.py — re-exported here.
EXPENSE_ROUTER_PROMPT = expenses.EXPENSE_ROUTER_PROMPT

# =========================
# KEYBOARDS
# =========================
MAIN_KEYBOARD = {
    "keyboard": [
        ["🛒 Покупки", "🧊 Запаси"],
        ["🍽️ Що приготувати", "ℹ️ Допомога"],
        ["🧠 Назви товарів", "💸 Витрати"],
        [action_history.UNDO_BUTTON_TEXT],
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

SHOPPING_KEYBOARD = {
    "keyboard": [
        ["➕ Додати товар", "📋 Показати список"],
        ["✅ Позначити купленим", "🗑️ Видалити товар"],
        [action_history.UNDO_BUTTON_TEXT],
        ["⬅️ Головне меню"]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

ADD_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Додати все", "✏️ Надіслати інший список"],
        ["✏️ Виправити позицію", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

INVENTORY_KEYBOARD = {
    "keyboard": [
        ["➕ Додати продукти", "📋 Показати запаси"],
        [action_history.UNDO_BUTTON_TEXT],
        ["➖ Використати / прибрати", "⬅️ Головне меню"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

MARK_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Куплено + додати в запаси", "✅ Куплено, без запасів"],
        ["✏️ Змінити вибір", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

DELETE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, видалити"],
        ["✏️ Змінити вибір", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

REMOVE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, прибрати"],
        ["✏️ Змінити вибір", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

ADD_INVENTORY_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Додати все", "✏️ Надіслати інший список"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

MERGE_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Об'єднати", "❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

SAVED_EDIT_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Підтвердити зміни"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

QUICK_PURCHASE_KEYBOARD = {
    "keyboard": [
        ["✅ Додати до запасів", "✏️ Змінити список"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

COMPOUND_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Підтвердити всі зміни"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, застосувати"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG = (
    "У тебе є незавершений план змін.\n\n"
    "Підтвердь його або скасуй перед новою командою."
)

UNDO_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Так, скасувати"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

ADD_DESTINATION_CLARIFICATION_KEYBOARD = {
    "keyboard": [
        ["🛒 До покупок", "🧊 До запасів"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

ADD_DESTINATION_CLARIFICATION_QUESTION = (
    "Куди додати ці позиції?\n\n"
    "🛒 До покупок\n"
    "🧊 До запасів"
)

ADD_DESTINATION_CLARIFICATION_INVALID_MSG = (
    "Обери, куди додати ці позиції:\n\n"
    "🛒 До покупок\n"
    "🧊 До запасів"
)

# Inventory Representation Clarification V2 — see household_router.py's own
# "INVENTORY REPRESENTATION CLARIFICATION V2" section for the pure
# formatting/parsing/resolution logic; this is only the Telegram-facing
# keyboards and the fixed messages that never need conflict-specific data.
REPRESENTATION_V2_CONSUME_CHOICE_KEYBOARD = {
    "keyboard": [
        ["⚖️ Це частина наявного запасу"],
        ["📦 Це інший / не облікований продукт"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

REPRESENTATION_V2_ADD_CHOICE_KEYBOARD = {
    "keyboard": [
        ["📦 Це окрема упаковка — додати окремо"],
        ["⚖️ Це вага наявного запису — уточнити його"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

REPRESENTATION_V2_PREVIEW_GUARD_MSG = (
    "Є незавершене уточнення щодо запасів.\n\n"
    "Вибери варіант або скасуй його."
)

_REPRESENTATION_V2_TOTAL_QUANTITY_INVALID_MSG = (
    "Потрібна загальна вага або об’єм наявного запасу.\n\n"
    "Напиши значення більше за те, що списується. Наприклад: «250 г»."
)

_REPRESENTATION_V2_STALE_MSG = (
    "Запаси змінилися, тому це уточнення вже неактуальне.\n\n"
    "Нічого не було змінено. Спробуй команду ще раз."
)

RECONCILIATION_PREVIEW_KEYBOARD = {
    "keyboard": [
        ["✅ Підтвердити звіряння"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

ALIASES_KEYBOARD = {
    "keyboard": [
        ["📋 Показати назви"],
        [action_history.UNDO_BUTTON_TEXT],
        ["⬅️ Головне меню"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

ALIAS_CREATE_CONFIRM_KEYBOARD = {
    "keyboard": [
        ["✅ Так, запам'ятати"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

ALIAS_UPDATE_CONFIRM_KEYBOARD = {
    "keyboard": [
        ["✅ Так, змінити"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

ALIAS_DELETE_CONFIRM_KEYBOARD = {
    "keyboard": [
        ["✅ Так, видалити"],
        ["❌ Скасувати"],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

# Expense keyboards now live in expenses.py (EXPENSES_KEYBOARD already
# includes the undo row) — re-exported here.
EXPENSES_KEYBOARD = expenses.EXPENSES_KEYBOARD
EXPENSE_PREVIEW_KEYBOARD = expenses.EXPENSE_PREVIEW_KEYBOARD
EXPENSE_DELETE_PREVIEW_KEYBOARD = expenses.EXPENSE_DELETE_PREVIEW_KEYBOARD

# =========================
# FLASK APP
# =========================
app = Flask(__name__)

SEND_MESSAGE_TIMEOUT = 10  # seconds; keeps webhook() from stalling past Telegram's retry window

def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload, timeout=SEND_MESSAGE_TIMEOUT)

def call_gemini(history, system_prompt, temperature=0.7, model_url=None):
    if not GEMINI_API_KEY:
        return None
    if model_url is None:
        model_url = GEMINI_CHAT_URL
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature}
    }
    try:
        resp = requests.post(
            model_url,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        if not text or not text.strip():
            return None
        return text.strip()
    except Exception:
        return None

def parse_item_text(text):
    if "—" in text:
        parts = text.split("—", 1)
        return parts[0].strip(), parts[1].strip()
    return text.strip(), None

# Numbered inventory-delete selection cluster now lives in inventory.py —
# these are thin compatibility wrappers (same names/signatures as before)
# that inject bot.py's own _effective_quantity/CATEGORY_ORDER/
# DEFAULT_CATEGORY, so no business logic is duplicated here.
def _numbered_inventory_display_items(items):
    return inventory._numbered_inventory_display_items(items, CATEGORY_ORDER, DEFAULT_CATEGORY)


def _render_inventory_item_label(item):
    return inventory._render_inventory_item_label(item, _effective_quantity)


_normalize_delete_match_text = inventory._normalize_delete_match_text
_parse_numbered_delete_lines = inventory._parse_numbered_delete_lines
_format_numbered_delete_mismatch_message = inventory._format_numbered_delete_mismatch_message


def _resolve_numbered_inventory_delete_selection(text, items):
    return inventory._resolve_numbered_inventory_delete_selection(
        text, items, _effective_quantity, CATEGORY_ORDER, DEFAULT_CATEGORY,
    )


def format_grouped_list(items, header):
    lines = [header, ""]
    counter = 1
    for cat in CATEGORY_ORDER:
        cat_items = [it for it in items if (it.get("category") or DEFAULT_CATEGORY) == cat]
        if not cat_items:
            continue
        emoji = CATEGORY_EMOJIS.get(cat, "🛒")
        lines.append(f"{emoji} {cat}")
        for item in cat_items:
            label = item["name"]
            if item.get("was_corrected"):
                label += " (виправлено)"
            _, _, qty_display = _effective_quantity(item)
            if qty_display:
                lines.append(f"{counter}. {label} — {qty_display}")
            else:
                lines.append(f"{counter}. {label}")
            counter += 1
        lines.append("")
    return "\n".join(lines).rstrip()

def format_shopping_list(items):
    if not items:
        return "Список покупок поки порожній."
    return format_grouped_list(items, "🛒 Список покупок:")

def format_inventory_list(items):
    if not items:
        return "Запаси поки порожні."
    return format_grouped_list(items, "🧊 Запаси:")

def format_alias_list(aliases):
    if not aliases:
        return "Домашніх назв поки немає."
    lines = ["🧠 Домашні назви товарів:", ""]
    lines += [f"{i}. {a['alias_text']} → {a['target_display_name']}" for i, a in enumerate(aliases, start=1)]
    return "\n".join(lines)

def _format_alias_create_preview(alias_text, target_display_name):
    return (f"🧠 Запам'ятати домашню назву?\n\n"
            f"«{alias_text}» → «{target_display_name}»\n\n"
            f"✅ Так, запам'ятати\n❌ Скасувати")

def _format_alias_update_preview(alias_text, old_target, new_target):
    return (f"🧠 Змінити домашню назву?\n\n"
            f"«{alias_text}»\nбуло → «{old_target}»\nстане → «{new_target}»\n\n"
            f"✅ Так, змінити\n❌ Скасувати")

def _format_alias_delete_preview(alias_text, target_display_name):
    return (f"🧠 Видалити домашню назву?\n\n"
            f"«{alias_text}» → «{target_display_name}»\n\n"
            f"✅ Так, видалити\n❌ Скасувати")


def _format_alias_bulk_delete_preview(selected, remaining):
    lines = [f"🧠 Буде видалено домашніх назв: {len(selected)}", ""]
    lines += [f"{i}. {a['alias_text']} → {a['target_display_name']}" for i, a in enumerate(selected, start=1)]
    lines.append("")
    if remaining:
        lines.append("Залишиться:")
        lines.append("")
        lines += [f"• {a['alias_text']} → {a['target_display_name']}" for a in remaining]
        lines.append("")
    else:
        lines.append("Не залишиться жодної домашньої назви.")
        lines.append("")
    lines.append("✅ Так, видалити\n❌ Скасувати")
    return "\n".join(lines)


ALIAS_GATE_UNRECOGNIZED_MSG = (
    "Не зміг зрозуміти домашню назву. Напиши, наприклад:\n\n"
    "Запам'ятай, що сливки = Вершки"
)


def _current_alias_origin(chat_id):
    """Which context an alias command was issued from — the single source of
    truth for where to return the user after confirm/cancel. This is the
    real fix for the old "Додавання товарів скасовано" bug: that bug wasn't
    a stray flag, it was this exact information never being tracked
    precisely enough to know whether to send MAIN_KEYBOARD, SHOPPING_KEYBOARD
    or INVENTORY_KEYBOARD back, so no explicit keyboard was sent at all and a
    stale one-time alias-confirm keyboard was left visible client-side to be
    pressed again after the flow had already completed.

    Checked in this order: the dedicated aliases submenu; an open saved
    shopping/inventory list; otherwise the main menu (covers the help screen
    too, since it sets no special context of its own). The main-menu case is
    reported as "global" — kept from the previous iteration's naming for this
    one value since existing tests assert it literally; it is otherwise
    exactly what the rest of this codebase calls "main menu".
    """
    if active_list_context.get(chat_id) == "aliases":
        return "aliases_menu"
    ctx = saved_list_context.get(chat_id)
    if ctx in ("shopping_saved", "inventory_saved"):
        return ctx
    return "global"


def _alias_origin_keyboard(origin):
    """The correct persistent keyboard to explicitly (re-)send for a given
    alias-command origin — ALWAYS a concrete keyboard, never None. Sending an
    explicit keyboard after every alias confirm/cancel (success, failure, or
    stale-mismatch) is what prevents a stale one-time alias-confirm keyboard
    from lingering client-side to be pressed again once the flow is done."""
    if origin == "aliases_menu":
        return ALIASES_KEYBOARD
    if origin == "shopping_saved":
        return SHOPPING_KEYBOARD
    if origin == "inventory_saved":
        return INVENTORY_KEYBOARD
    return MAIN_KEYBOARD


# Origin helpers now live in expenses.py — re-exported here.
_current_expense_origin = expenses._current_expense_origin
_expense_origin_keyboard = expenses._expense_origin_keyboard


def _reply_after_alias_action(chat_id, household_id, origin, message):
    """After a confirmed alias create/update/delete: full refreshed list +
    ALIASES_KEYBOARD when the action originated from the dedicated submenu;
    the short confirmation plus an explicit return-to-origin keyboard
    otherwise (main menu / open shopping list / open inventory list)."""
    if origin == "aliases_menu":
        send_message(chat_id, message)
        send_message(chat_id, format_alias_list(list_household_aliases(household_id)), reply_markup=ALIASES_KEYBOARD)
    else:
        send_message(chat_id, message, reply_markup=_alias_origin_keyboard(origin))


def _alias_command_gate(text):
    """Narrow, local gate for global alias commands — usable outside the
    dedicated aliases submenu (main menu, help, open shopping/inventory
    lists). Recognizes only unambiguous alias-management phrasing; it never
    parses alias_text/target/selected aliases itself and never calls Gemini
    — that remains entirely the job of the existing Gemini alias router.
    Deliberately NOT a big hand-enumerated phrase list: create/update mapping
    syntax, the "forget" verb (unambiguous in this bot's domain), and an
    explicit reference to the aliases topic itself (домашні назви / aliases /
    синоніми, or "покажи .../мої/товарів назви") are the only signals — a bare
    "Видали всі" never matches, since it says nothing about names/aliases.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith(("запам'ятай", "запам’ятай")) and "=" in stripped:
        return True
    if lowered.startswith("зміни") and "=" in stripped:
        return True
    if lowered.startswith("забудь"):
        remainder = stripped[len("забудь"):].strip(" ,:.-")
        if remainder:
            return True
    if "alias" in lowered or "синонім" in lowered:
        return True
    if "назв" in lowered and ("домашн" in lowered or "товар" in lowered or "покажи" in lowered or "мої" in lowered):
        return True
    return False


# Expense routing gates now live in expenses.py — re-exported here.
_expense_command_gate = expenses._expense_command_gate
_expense_report_gate = expenses._expense_report_gate
_expense_delete_command_gate = expenses._expense_delete_command_gate


def _handle_alias_command(chat_id, user_id, display_name, text):
    """Shared alias-router handling for both the dedicated aliases submenu
    and the global alias command gate. Origin is derived once via
    _current_alias_origin and threaded through pending_alias_action so
    confirm/cancel always know exactly where to return the user.

    Returns True if the message was fully handled here (caller must not fall
    through to general AI-chat). Returns False only when intent is "none" and
    origin == "aliases_menu" — the one case allowed to fall through, matching
    every other router in this file. A global-gate command is never allowed
    to fall through to AI-chat, even on "none"/"invalid" — the gate already
    confirmed the text looks like an alias command.
    """
    origin = _current_alias_origin(chat_id)
    keyboard = _alias_origin_keyboard(origin)
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        aliases = list_household_aliases(household_id)
        router_result = _ask_gemini_alias_router(text, aliases)
        kind, payload = _validate_alias_router_result(router_result, aliases)
        if kind == "unresolved":
            lines = ["Не зрозумів частину повідомлення:", ""]
            lines += [f"• «{f}»" for f in payload]
            lines.append("")
            lines.append("Спробуй сформулювати інакше, наприклад: «сливки — це вершки».")
            send_message(chat_id, "\n".join(lines), reply_markup=keyboard)
        elif kind == "list":
            send_message(chat_id, format_alias_list(aliases), reply_markup=keyboard)
        elif kind == "invalid":
            if origin == "aliases_menu":
                send_message(chat_id, "Не зміг безпечно зрозуміти правило. Спробуй написати, наприклад: «сливки — це вершки».")
            else:
                send_message(chat_id, ALIAS_GATE_UNRECOGNIZED_MSG, reply_markup=keyboard)
        elif kind == "create_or_update":
            alias_text = router_result["alias_text"].strip()
            target_display_name = router_result["target_display_name"].strip()
            existing = get_household_alias(household_id, payload)
            if existing:
                pending_alias_action[chat_id] = {
                    "kind": "update", "household_id": household_id, "user_db_id": user_db_id,
                    "alias_text": alias_text, "target_display_name": target_display_name, "origin": origin,
                }
                send_message(
                    chat_id,
                    _format_alias_update_preview(alias_text, existing["target_display_name"], target_display_name),
                    reply_markup=ALIAS_UPDATE_CONFIRM_KEYBOARD,
                )
            else:
                pending_alias_action[chat_id] = {
                    "kind": "create", "household_id": household_id, "user_db_id": user_db_id,
                    "alias_text": alias_text, "target_display_name": target_display_name, "origin": origin,
                }
                send_message(
                    chat_id,
                    _format_alias_create_preview(alias_text, target_display_name),
                    reply_markup=ALIAS_CREATE_CONFIRM_KEYBOARD,
                )
        elif kind == "delete":
            existing = get_household_alias(household_id, payload)
            if existing is None:
                send_message(chat_id, "Не знайшов такого правила серед домашніх назв.", reply_markup=keyboard)
            else:
                pending_alias_action[chat_id] = {
                    "kind": "delete", "household_id": household_id, "user_db_id": user_db_id,
                    "alias_normalized": payload, "alias_text": existing["alias_text"],
                    "target_display_name": existing["target_display_name"], "origin": origin,
                }
                send_message(
                    chat_id,
                    _format_alias_delete_preview(existing["alias_text"], existing["target_display_name"]),
                    reply_markup=ALIAS_DELETE_CONFIRM_KEYBOARD,
                )
        elif kind == "delete_aliases":
            selected = payload
            selected_ids = {a["id"] for a in selected}
            remaining = [a for a in aliases if a["id"] not in selected_ids]
            pending_alias_action[chat_id] = {
                "kind": "bulk_delete", "household_id": household_id, "user_db_id": user_db_id,
                "targets": [
                    {"id": a["id"], "target_display_name": a["target_display_name"], "target_canonical_name": a["target_canonical_name"]}
                    for a in selected
                ],
                "origin": origin,
            }
            send_message(
                chat_id,
                _format_alias_bulk_delete_preview(selected, remaining),
                reply_markup=ALIAS_DELETE_CONFIRM_KEYBOARD,
            )
        elif kind == "none":
            if origin == "aliases_menu":
                return False
            send_message(chat_id, ALIAS_GATE_UNRECOGNIZED_MSG, reply_markup=keyboard)
        return True
    except Exception:
        send_message(chat_id, "Не вдалося виконати дію з домашніми назвами. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)
        return True


def format_inventory_preview(items, ignored_items=None):
    header = f"🧊 Знайшов продуктів: {len(items)}"
    text = format_grouped_list(items, header)
    if ignored_items:
        text += "\n\nНе додано: " + ", ".join(ignored_items)
    return text

def get_household_and_user(user_id, display_name=None):
    household_id = get_or_create_household()
    user_db_id = get_or_create_user(user_id, household_id, display_name)
    return household_id, user_db_id

# clear_shopping_state/clear_inventory_state/clear_interaction_state's real
# logic now lives in interaction_state.py (called via _interaction_state_deps,
# built further down); these stay as thin compatibility wrappers so every
# existing call site/test patch of the same name keeps working unchanged —
# including DispatcherDeps's runtime lambda-forward of clear_interaction_state.
def clear_shopping_state(chat_id):
    interaction_state.clear_shopping_state(_interaction_state_deps, chat_id)


def clear_inventory_state(chat_id):
    interaction_state.clear_inventory_state(_interaction_state_deps, chat_id)


def clear_alias_state(chat_id):
    pending_alias_action.pop(chat_id, None)

# Expense state clearing now lives in expenses.py — re-exported here.
clear_expense_state = expenses.clear_expense_state

def clear_interaction_state(chat_id):
    interaction_state.clear_interaction_state(_interaction_state_deps, chat_id)

# _parse_qty/_MERGEABLE_UNITS_BOT now live in list_editing.py — imported above.

# =========================
# STRUCTURED QUANTITY HELPERS
#
# Pure quantity parsing/merging/formatting (STRUCTURED_UNITS, _UNIT_ALIASES,
# parse_structured_quantity, merge_quantity_values, format_quantity_display)
# now lives in quantities.py — the single source of truth database.py also
# imports from. Only product-name synonym rules (_NAME_SYNONYMS) stay here,
# a self-contained mirror of database.py's identical copy on purpose (this
# file must not depend on database.py's own RAM-only pending-preview code
# paths going through a different module).
# =========================

_NAME_SYNONYMS = {
    "сливки": "вершки",
    "mleko": "молоко",
    "ser": "сир",
    "maslo": "масло",
    "masło": "масло",
    "smietanka": "вершки",
    "śmietanka": "вершки",
    "smietana": "сметана",
    "śmietana": "сметана",
}

# Narrow, deterministic Latin/Cyrillic homoglyph whitelist — mirrors
# database.py's identical copy. Only the classic ASCII-lookalike Cyrillic
# letters; never used to transliterate real Ukrainian/Polish words (see
# _repair_mixed_script_token below).
_CYRILLIC_HOMOGLYPH_TO_LATIN = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "і": "i",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X", "І": "I",
}


def _clean_unicode_whitespace(text):
    """Step 2 of name normalization: Unicode NFKC normalization + whitespace
    collapse. Pure cleanup — never translates or transliterates anything.
    Mirrors database.py's identical copy."""
    normalized = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", normalized.strip())


def _char_script(c):
    """Classify one character for _repair_mixed_script_token — see
    database.py's identical copy for the authoritative docstring."""
    if c in _CYRILLIC_HOMOGLYPH_TO_LATIN:
        return "homoglyph"
    if "CYRILLIC" in unicodedata.name(c, ""):
        return "cyrillic_only"
    if c.isascii() and c.isalpha():
        return "latin"
    return "other"


def _repair_mixed_script_token(token):
    """Step 3 of name normalization: repair ONE otherwise-pure-Latin word
    with Cyrillic look-alike letters mixed in (e.g. "mlekо" -> "mleko").
    Never touches a token with any genuine Cyrillic-only letter — see
    database.py's identical copy for the authoritative docstring."""
    scripts = [_char_script(c) for c in token]
    if "latin" in scripts and "homoglyph" in scripts and "cyrillic_only" not in scripts:
        return "".join(_CYRILLIC_HOMOGLYPH_TO_LATIN.get(c, c) for c in token)
    return token


def _repair_mixed_script(text):
    """Apply _repair_mixed_script_token to each word of `text` independently."""
    if not text:
        return text
    return " ".join(_repair_mixed_script_token(tok) for tok in text.split(" "))


def canonicalize_name(name):
    """Lowercase/trim a name, repair narrow Latin/Cyrillic mixed-script
    homoglyphs, and map known synonyms to one canonical form. Household
    alias resolution lives in resolve_item_name, checked before this."""
    cleaned = _repair_mixed_script(_clean_unicode_whitespace(name or ""))
    base = cleaned.strip().lower()
    return _NAME_SYNONYMS.get(base, base)


def _normalize_display_name_for_exact_match(name):
    """Case-insensitive, Latin/Cyrillic-homoglyph-tolerant normalization of
    an inventory row's OWN visible name — same mixed-script repair
    canonicalize_name uses, but WITHOUT the global _NAME_SYNONYMS table, so
    two textually different products (e.g. "mlekо" and "Молоко") never
    collapse into the same key the way canonicalize_name's synonym mapping
    would. Used only for Inventory Cleanup Admin's exact visible-row-name
    matching priority (see inventory.resolve_inventory_admin_candidates and
    inventory.resolve_cleanup_admin_disambiguation_reply)."""
    return _repair_mixed_script(_clean_unicode_whitespace(name or "")).strip().lower()


# Self-contained mirror of database.py's alias-resolution logic. Duplicated on
# purpose, same reasoning as the structured-quantity helpers above: these are
# pure functions with no DB access, and bot.py's own copy is what every
# pending-preview/RAM code path (and the wider test suite, which mocks the
# `database` module in ways this file must not depend on) actually exercises.
ALIAS_TEXT_MAX_LEN = 60


def normalize_alias_text(text):
    """Pure normalization for alias TEXT — see database.py's identical copy
    for the authoritative docstring. Must stay in sync in behavior."""
    if not isinstance(text, str):
        return None
    collapsed = re.sub(r"\s+", " ", text.strip())
    if not collapsed:
        return None
    cleaned = re.sub(r"[^\w%\-\s]", "", collapsed.lower(), flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) > ALIAS_TEXT_MAX_LEN:
        return None
    return cleaned


def resolve_item_name(name, alias_map):
    """THE shared resolver (bot.py-local mirror of database.py's copy).
    Resolution order — see database.py's identical copy for the
    authoritative docstring: (1) household alias lookup using the name
    as-is; (2-3) Unicode cleanup + narrow mixed-script repair; (4) household
    alias lookup again against the cleaned name; (5-6) built-in generic
    synonym via canonicalize_name(), else plain lowercasing. Household
    aliases always win over the built-in dictionary. Returns
    (display_name, canonical_name). Never raises."""
    old_key = normalize_alias_text(name)
    if alias_map and old_key is not None and old_key in alias_map:
        entry = alias_map[old_key]
        return entry["target_display_name"], entry["target_canonical_name"]

    cleaned = _repair_mixed_script(_clean_unicode_whitespace(name or ""))
    new_key = normalize_alias_text(cleaned)
    if alias_map and new_key is not None and new_key != old_key and new_key in alias_map:
        entry = alias_map[new_key]
        return entry["target_display_name"], entry["target_canonical_name"]

    return name, canonicalize_name(name)


# Bare-name alias so every existing call site (_parse_structured_quantity(...))
# keeps working unchanged — the implementation itself now lives in
# quantities.py, the single source of truth database.py also imports from.
_parse_structured_quantity = quantities.parse_structured_quantity


def _split_number_and_unit_no_space(text):
    """Insert a space between a leading numeral and an immediately-following
    unit word (e.g. "1Л" -> "1 Л", "500мл" -> "500 мл") so a plain
    number+unit reply can be split like any other structured quantity.
    Only touches a single leading numeral+unit token — never guesses at
    anything more complex; returns text unchanged if it doesn't match."""
    match = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*([^\d\s]+)\s*$", text or "")
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return text


def _parse_explicit_clarification_quantity(text):
    """Parse an Inventory Quantity Clarification reply (see
    pending_inventory_quantity_clarification) into an explicit
    (Decimal value, unit) pair, or (None, None) if the reply isn't an
    unambiguous number+unit. Deliberately NEVER falls back to the bare-
    number-defaults-to-"шт." behavior _parse_structured_quantity's own
    single-token branch has for other flows — a clarification reply's whole
    purpose is to remove an inferred guess, so the unit must always be
    explicit here (a bare "2" is rejected, "2 шт." is accepted). Handles
    both spaced ("1 л") and unspaced ("1Л") forms; comma or dot decimals.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    normalized = _split_number_and_unit_no_space(stripped).replace(",", ".")
    parts = normalized.split()
    if len(parts) != 2:
        return None, None
    try:
        value = Decimal(parts[0])
    except InvalidOperation:
        return None, None
    if value <= 0:
        return None, None
    unit = _UNIT_ALIASES.get(parts[1].lower().rstrip("."))
    if unit is None:
        return None, None
    return value, unit


def _parse_representation_v2_total_quantity_reply(text, requested_unit):
    """Parse a reply to Inventory Representation Clarification V2's "скільки
    важив/було увесь наявний запас?" substage (see
    pending_inventory_representation_clarification's "awaiting_total"
    stage) — the ONE place a bare number (no unit) is accepted, since the
    unit is unambiguous here: it's always the SAME unit as the already-known
    consume request (`requested_unit`), never just its mass/volume group
    (e.g. consuming "200 г" and replying bare "300" means "300 г", never
    "0.3 кг"). Every explicit form (_parse_explicit_clarification_quantity)
    is tried first and still works unchanged; this only adds a fallback for
    a bare number, and ONLY here — _parse_explicit_clarification_quantity
    itself, used by every other clarification flow, is untouched."""
    value, unit = _parse_explicit_clarification_quantity(text)
    if value is not None and unit is not None:
        return value, unit
    stripped = (text or "").strip().replace(",", ".")
    try:
        bare_value = Decimal(stripped)
    except InvalidOperation:
        return None, None
    if bare_value <= 0:
        return None, None
    return bare_value, requested_unit


def normalize_item_quantity(name, quantity_text, quantity_value=None, quantity_unit=None, allow_default_unit=False, alias_map=None):
    """Compute name/canonical_name/quantity_value/quantity_unit/quantity_inferred/quantity_text for an item.

    Thin wrapper: name/canonical_name come from this module's own
    resolve_item_name (household alias lookup + product-name synonym
    rules, out of quantities.py's scope); the quantity fields themselves
    are computed by quantities.parse_quantity_fields, the single shared
    implementation database.py's normalize_quantity_fields also calls.

    If quantity_value+quantity_unit are already known, they're used as-is.
    Otherwise quantity_text is parsed locally when unambiguous. allow_default_unit=True
    applies the "1 шт." default only when quantity_text is genuinely blank (new
    items straight out of AI parsing) — never for edits or legacy-data backfill.
    alias_map (household alias lookup, fetched once per request) takes priority
    over the built-in generic synonym when resolving the display/canonical name;
    it never affects quantity parsing, which is computed independently below.
    """
    resolved_name, canonical_name = resolve_item_name(name, alias_map or {})
    if quantity_value is not None and quantity_unit is not None:
        value, unit, inferred = quantity_value, quantity_unit, False
        display = quantities.format_quantity_display(value, unit)
    else:
        fields = quantities.parse_quantity_fields(quantity_text, allow_default_unit=allow_default_unit)
        value = fields["quantity_value"]
        unit = fields["quantity_unit"]
        inferred = fields["quantity_inferred"]
        display = fields["quantity_text"]
    return {
        "name": resolved_name,
        "canonical_name": canonical_name,
        "quantity_value": value,
        "quantity_unit": unit,
        "quantity_inferred": inferred,
        "quantity_text": display,
    }


# Inventory Representation Guard v1 itself (find/classify/resolve_inventory_
# representation, format_representation_*) now lives in inventory.py —
# imported above and used directly here for the normal/legacy inventory add
# flow, and via the injected `_bot` reference from household_router.py for
# the Global Household Router.
_GLOBAL_QUANTITY_CLARIFICATION_INVALID_MSG = (
    "Потрібна точна кількість з одиницею.\n\n"
    "Напиши, наприклад: «1 л», «500 мл» або «2 шт.»."
)


def _effective_quantity(item):
    """Return (value, unit, display_text) for an item, preferring structured fields."""
    value = item.get("quantity_value")
    unit = item.get("quantity_unit")
    if value is not None:
        return value, unit, format_quantity_display(value, unit)
    return None, None, (item.get("quantity_text") or "")


# names_can_merge/_auto_merge_in_place need bot.py's own canonicalize_name/
# _effective_quantity/DEFAULT_CATEGORY injected — thin wrappers over
# list_editing.py, the single source of truth. _compute_merged_quantity/
# _apply_pending_merge have no bot-specific dependency, so they're already
# bound directly via the `from list_editing import ...` above — no
# redefinition needed here.
def names_can_merge(item_a, item_b):
    return list_editing.names_can_merge(item_a, item_b, canonicalize_name, DEFAULT_CATEGORY)


def _auto_merge_in_place(items):
    return list_editing._auto_merge_in_place(items, _effective_quantity, canonicalize_name, DEFAULT_CATEGORY)


def _validate_merge_groups(raw_groups, items_list, is_pending=False):
    return list_editing._validate_merge_groups(raw_groups, items_list, VALID_CATEGORIES, DEFAULT_CATEGORY, is_pending=is_pending)


def _ask_gemini_intent_router(user_text, items):
    """One Gemini call: detect merge intent and return merge groups (sequential #N refs)."""
    if len(items) < 2:
        return {"intent": "none", "merge_groups": []}
    lines = []
    for i, item in enumerate(items):
        label = f"#{i + 1}. {item['name']}"
        if item.get("quantity_text"):
            label += f" — {item['quantity_text']}"
        label += f" [{item.get('category') or DEFAULT_CATEGORY}]"
        lines.append(label)
    prompt = "Список:\n" + "\n".join(lines) + f"\n\nКористувач написав: {user_text}"
    raw = call_gemini([{"role": "user", "content": prompt}], INTENT_ROUTER_PROMPT, temperature=0.1)
    if not raw:
        return {"intent": "none", "merge_groups": []}
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "merge_groups": data.get("merge_groups") if isinstance(data.get("merge_groups"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"intent": "none", "merge_groups": []}


def _ask_gemini_preview_edit_router(user_text, items, context_type):
    """Gemini call: detect edit_preview or merge_duplicates for an active pending preview."""
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        item_qty = _effective_quantity(item)[2]
        if item_qty:
            label += f" — {item_qty}"
        label += f" [{item.get('category') or DEFAULT_CATEGORY}]"
        lines.append(label)
    prompt = (
        f"Контекст: {context_type}\n"
        "Товари у preview:\n" + "\n".join(lines)
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], PENDING_PREVIEW_EDIT_PROMPT, temperature=0.1)
    if not raw:
        return {"intent": "none", "updates": []}
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "updates": data.get("updates") if isinstance(data.get("updates"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"intent": "none", "updates": []}


def _validate_preview_updates(updates, items):
    return list_editing._validate_preview_updates(updates, items, VALID_CATEGORIES)


def _apply_preview_updates(items, valid_updates, alias_map=None):
    return list_editing._apply_preview_updates(items, valid_updates, normalize_item_quantity, alias_map=alias_map)


# =========================
# SAVED LIST EDIT HELPERS
# =========================

def _compute_saved_merged_quantity(group_items):
    return list_editing._compute_saved_merged_quantity(group_items, _effective_quantity)


def _compute_saved_merge_groups(merge_groups_raw, items):
    return list_editing._compute_saved_merge_groups(
        merge_groups_raw, items, canonicalize_name, _effective_quantity, DEFAULT_CATEGORY,
    )


def _validate_alias_action(alias_text, target_display_name):
    """Pure validation for create_or_update: rejects empty/too-long alias_text
    and the alias≈target no-op case. Returns alias_normalized on success, or
    None. Does not compute target_canonical_name — database.py's
    create_or_update_household_alias re-derives that independently and never
    trusts this pre-check alone."""
    if not isinstance(target_display_name, str) or not target_display_name.strip():
        return None
    alias_normalized = normalize_alias_text(alias_text)
    if alias_normalized is None:
        return None
    if alias_normalized == normalize_alias_text(target_display_name):
        return None
    return alias_normalized


def _validate_alias_bulk_delete(selected_numbers, aliases):
    """Pure validation for delete_aliases against the current household alias
    list (already household-scoped by the caller via list_household_aliases).
    Checks: list non-empty, every number exists, duplicates removed, order
    matches the CURRENT alias list order (not whatever order Gemini gave).
    Returns ("ok", [alias dicts in list order]) or ("invalid", None).
    """
    if not isinstance(selected_numbers, list) or not selected_numbers:
        return "invalid", None
    total = len(aliases)
    seen = set()
    for n in selected_numbers:
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > total:
            return "invalid", None
        seen.add(n)
    if not seen:
        return "invalid", None
    selected = [aliases[i - 1] for i in range(1, total + 1) if i in seen]
    return "ok", selected


def _validate_alias_router_result(router_result, aliases=None):
    """Pure decision logic for the alias router's JSON. Returns one of:
      ("unresolved", [fragment,...])   -- blocks any change regardless of intent
      ("list", None)
      ("create_or_update", alias_normalized)
      ("delete", alias_normalized)
      ("delete_aliases", [alias dicts, in current list order])
      ("invalid", None)                -- create_or_update/delete/delete_aliases with unusable input
      ("none", None)

    `aliases` (the current household's alias list) is only required for
    delete_aliases — optional/defaulted so every other intent keeps working
    exactly as before for callers that don't pass it.
    """
    fragments = router_result.get("unresolved_fragments")
    if isinstance(fragments, list):
        cleaned = [str(f).strip() for f in fragments if str(f).strip()]
        if cleaned:
            return "unresolved", cleaned
    intent = router_result.get("intent")
    if intent == "list":
        return "list", None
    if intent == "create_or_update":
        alias_normalized = _validate_alias_action(router_result.get("alias_text"), router_result.get("target_display_name"))
        return ("create_or_update", alias_normalized) if alias_normalized else ("invalid", None)
    if intent == "delete":
        alias_normalized = normalize_alias_text(router_result.get("alias_text"))
        return ("delete", alias_normalized) if alias_normalized else ("invalid", None)
    if intent == "delete_aliases":
        kind, payload = _validate_alias_bulk_delete(router_result.get("selected_numbers"), aliases or [])
        return ("delete_aliases", payload) if kind == "ok" else ("invalid", None)
    return "none", None


def _validate_saved_updates(updates, items):
    """Validate Gemini saved list edit updates. Returns list of valid updates (with
    item_id plus old_value/old_unit — the snapshot quantity at preview time, used to
    detect a stale precondition at confirm time) or None."""
    if not isinstance(updates, list) or not updates:
        return None
    total = len(items)
    used_numbers = set()
    valid = []
    for upd in updates:
        if not isinstance(upd, dict):
            return None
        num = upd.get("item_number")
        if not isinstance(num, int) or num < 1 or num > total:
            return None
        if num in used_numbers:
            return None
        used_numbers.add(num)
        name = upd.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return None
        qty = upd.get("quantity_text")
        if qty is not None and (not isinstance(qty, str) or not qty.strip()):
            return None
        cat = upd.get("category")
        if cat is not None and cat not in VALID_CATEGORIES:
            return None
        valid.append({
            "item_number": num,
            "item_id": items[num - 1]["id"],
            "name": name,
            "quantity_text": qty,
            "category": cat,
            "old_value": items[num - 1].get("quantity_value"),
            "old_unit": items[num - 1].get("quantity_unit"),
        })
    return valid


# Package/container words the saved-edit router must never be allowed to
# silently turn into a piece count ("шт.") — a "пачка"/"paczka" is not a
# fixed number of items. Ukrainian + Polish forms only, exactly as specified;
# no fuzzy matching or extra inflections guessed beyond this list.
_PACKAGE_WORD_RE = re.compile(
    r"\b(пачка|пачки|пачок|упаковка|упаковки|упаковок|paczka|paczki|opakowanie|opakowania)\b",
    re.IGNORECASE,
)
_PACKAGE_NUMBER_WORDS = (
    "два", "дві", "три", "чотири", "п'ять", "п’ять", "пара", "пару",
    "dwa", "dwie", "trzy", "cztery", "jeden", "jedna",
)


def _extract_package_phrase(text):
    """Find the first package/container word in `text` and return it
    together with an immediately preceding quantity word/number when
    present (e.g. "дві пачки", "2 paczki"), for the user-facing safety
    message. Returns None if no package word is present anywhere in text."""
    match = _PACKAGE_WORD_RE.search(text or "")
    if not match:
        return None
    prefix = text[:match.start()]
    prefix_match = re.search(
        r"(\d+|" + "|".join(re.escape(w) for w in _PACKAGE_NUMBER_WORDS) + r")\s*$",
        prefix, re.IGNORECASE,
    )
    if prefix_match:
        return text[prefix_match.start():match.end()]
    return match.group(0)


def _saved_edit_text_has_unsafe_package_conversion(text, valid_updates):
    """True if the raw user text mentions a package/container phrase while
    at least one validated saved-edit candidate would set a structured
    piece count ("шт."). This is exactly the dangerous conversion the
    Gemini edit router must not be allowed to make on its own — a package
    is not a fixed number of pieces — so the whole edit request is blocked
    rather than guessing which single candidate the phrase referred to (no
    fuzzy matching). Ordinary edits that set "шт." without any package word
    in the text (e.g. "Сосиски — 2 шт.") are never affected.
    """
    if not _PACKAGE_WORD_RE.search(text or ""):
        return False
    for upd in valid_updates:
        qty = upd.get("quantity_text")
        if not qty:
            continue
        value, unit = _parse_structured_quantity(qty)
        if unit == "шт." and value is not None:
            return True
    return False


def _format_package_conversion_blocked_message(text):
    phrase = _extract_package_phrase(text) or "пачки"
    return (
        f"Не можу безпечно перетворити «{phrase}» на штуки.\n\n"
        "Пачка не дорівнює певній кількості товару.\n"
        "Для зміни кількості в штуках напиши, наприклад: «Сосиски — 2 шт.».\n\n"
        "Щоб додати нове надходження пачками, напиши: «Купив дві пачки сосисок»."
    )


def _quantity_values_equal(a, b):
    """Decimal-exact comparison for two possibly-None, possibly-float/Decimal
    quantity values. Never compares via bare float equality — converting
    both through Decimal(str(...)) first avoids binary-float noise making a
    genuinely-identical quantity look changed (e.g. 0.1 + 0.2 artifacts)."""
    if a is None or b is None:
        return a is None and b is None
    dec_a = a if isinstance(a, Decimal) else Decimal(str(a))
    dec_b = b if isinstance(b, Decimal) else Decimal(str(b))
    return dec_a == dec_b


def _saved_update_is_noop(old_item, upd, alias_map=None):
    """True if applying a validated saved-list edit candidate `upd` to
    `old_item` would leave every significant field unchanged: name/
    canonical_name, quantity_value, quantity_unit, quantity_text,
    quantity_inferred, category. Mirrors exactly what
    update_inventory_items_batch/update_shopping_items_batch (database.py)
    computes and writes per field at confirm time, so "no real change" here
    means no real change in the database either — no DB call needed to
    know this ahead of time. A field left as None in `upd` means "not being
    changed" and is skipped, same as the DB layer's own
    `if upd.get(...) is not None` gating for that field.
    """
    if upd.get("name") is not None:
        new_name, new_canonical = resolve_item_name(upd["name"], alias_map or {})
        old_canonical = old_item.get("canonical_name") or canonicalize_name(old_item.get("name", ""))
        if new_name != old_item.get("name") or new_canonical != old_canonical:
            return False
    if upd.get("quantity_text") is not None:
        new_value, new_unit = _parse_structured_quantity(upd["quantity_text"])
        new_text = format_quantity_display(new_value, new_unit) if new_value is not None else (upd["quantity_text"] or None)
        if not _quantity_values_equal(new_value, old_item.get("quantity_value")):
            return False
        if new_unit != old_item.get("quantity_unit"):
            return False
        if (new_text or None) != (old_item.get("quantity_text") or None):
            return False
        if old_item.get("quantity_inferred", False):
            # The DB layer always sets quantity_inferred=FALSE whenever
            # quantity_text is provided — if the existing row was inferred,
            # confirming would still flip that flag, so it's a real change.
            return False
    if upd.get("category") is not None and upd["category"] != old_item.get("category"):
        return False
    return True


def _split_noop_saved_updates(valid_updates, items_snapshot, alias_map=None):
    """Partition validated saved-list edit updates into (real, noop).
    No-op updates must never reach pending_saved_edit or the DB confirm
    path — see _saved_update_is_noop for what "no-op" means field by
    field."""
    real, noop = [], []
    for upd in valid_updates:
        old_item = items_snapshot[upd["item_number"] - 1]
        if _saved_update_is_noop(old_item, upd, alias_map):
            noop.append(upd)
        else:
            real.append(upd)
    return real, noop


def _pluralize_positions_uk(n):
    """Ukrainian plural for the word "позиція" for a given count n."""
    n_mod_100 = n % 100
    n_mod_10 = n % 10
    if 11 <= n_mod_100 <= 14:
        return "позицій"
    if n_mod_10 == 1:
        return "позиція"
    if 2 <= n_mod_10 <= 4:
        return "позиції"
    return "позицій"


def _format_noop_saved_edit_message(noop_updates, items_snapshot, context_type):
    """Message shown when every recognized saved-list edit candidate turned
    out to be a no-op after normalization (see _saved_update_is_noop) — the
    user's command was understood, but the current data already matches
    it, so no preview/pending state/DB call is created."""
    lines = [
        "Не бачу змін, які можна безпечно застосувати.",
        "",
        "Поточні дані вже відповідають розпізнаній команді. Сформулюй зміну точніше.",
    ]
    if context_type == "inventory_saved":
        for upd in noop_updates:
            old_item = items_snapshot[upd["item_number"] - 1]
            label = old_item["name"]
            qty = _effective_quantity(old_item)[2]
            if qty:
                label += f" — {qty}"
            lines.append("")
            lines.append(f"Поточний запис: {label}")
    return "\n".join(lines)


_ACTIONS_BY_CONTEXT = {
    "shopping_saved": {"mark_bought", "delete_shopping"},
    "inventory_saved": {"remove_inventory"},
}


def _format_unresolved_fragments_message(fragments):
    """Ukrainian clarification message shown when part of a command couldn't
    be resolved to a list item — used by start_action, consume_inventory_quantity,
    and the standalone SELECTION_PROMPT flow."""
    if len(fragments) == 1:
        header = f"Не зміг зрозуміти частину команди: «{fragments[0]}»."
    else:
        header = "Не зміг зрозуміти частину команди:\n" + "\n".join(f"• «{f}»" for f in fragments)
    return header + "\n\nУточни назву товару або напиши його номер зі списку."


def _validate_start_action(action, selected_numbers, context_type, items):
    """Validate a start_action router result for the current open list.

    Rejects any action not allowed for context_type, then validates
    selected_numbers the same way as button-triggered selection (dedup,
    order preserved, out-of-range dropped, empty rejected).
    Returns the ordered list of selected item dicts, or None if invalid.

    Callers must check for unresolved_fragments (see
    _check_unresolved_fragments) before calling this — this function only
    validates action/selected_numbers, unchanged from before that check existed.
    """
    if action not in _ACTIONS_BY_CONTEXT.get(context_type, set()):
        return None
    return _validate_selected_numbers(selected_numbers, items)


def _check_unresolved_fragments(router_result):
    """Shared unresolved_fragments gate for start_action and
    consume_inventory_quantity results from _ask_gemini_saved_list_router.

    Unlike compound_inventory_operations/reconcile_inventory_snapshot (which
    treat a missing field as "nothing unresolved" and must keep doing so —
    not touched here), these destructive/selection intents must never
    silently proceed when the router omitted the field — so a missing field
    is treated as a block too.

    Returns (True, [fragment_str, ...]) if blocked — the fragments list is
    empty when the field was simply missing (no fragment text to show).
    Returns (False, None) when clear to proceed with normal validation.
    """
    if not router_result.get("unresolved_fragments_present"):
        return True, []
    fragments = [str(f).strip() for f in router_result.get("unresolved_fragments") or [] if str(f).strip()]
    if fragments:
        return True, fragments
    return False, None


# _resolve_consumption/_validate_consumptions/_format_consumption_preview and
# their unit-group constants, plus compound inventory planning
# (validate_compound_operations/format_compound_preview) and
# _compound_snapshot_is_stale, now live in inventory.py — imported above.
# These two thin wrappers keep the old names/signatures, injecting bot.py's
# own normalize_item_quantity/_auto_merge_in_place/_effective_quantity/
# VALID_CATEGORIES/DEFAULT_CATEGORY — no business logic duplicated here.
def _validate_compound_operations(operations, unresolved_fragments, items, alias_map=None):
    return inventory.validate_compound_operations(
        operations, unresolved_fragments, items,
        normalize_item_quantity, _auto_merge_in_place,
        VALID_CATEGORIES, DEFAULT_CATEGORY,
        alias_map=alias_map,
    )


def _format_compound_preview(resolved):
    return inventory.format_compound_preview(resolved, _effective_quantity)


# =========================
# INVENTORY SNAPSHOT RECONCILIATION
# =========================

def _find_ambiguous_unit_group(raw_items):
    """Group reconciliation raw_items by canonical_name; return the first group
    (list of item dicts) whose quantity_unit values span more than one
    _UNIT_GROUP (e.g. л/мл vs шт. for the same product), or None if none exist.
    Items with quantity_unit not in _UNIT_GROUP are ignored for this check."""
    by_name = {}
    for it in raw_items:
        canon = it.get("canonical_name") or canonicalize_name(it.get("name", ""))
        by_name.setdefault(canon, []).append(it)
    for group in by_name.values():
        groups_seen = {_UNIT_GROUP[it["quantity_unit"]] for it in group if it.get("quantity_unit") in _UNIT_GROUP}
        if len(groups_seen) > 1:
            return group
    return None


def _sum_same_group_reconcile_items(group_items):
    """Sum a list of same-canonical-name, same-_UNIT_GROUP item dicts into one,
    using Decimal canonical-unit math (mirrors _resolve_consumption's conversion).
    Result's quantity_inferred is True only if every input entry was inferred.
    Caller must have already confirmed the group has no cross-group ambiguity
    (via _find_ambiguous_unit_group returning None for this canonical_name)."""
    valued = [it for it in group_items if it.get("quantity_unit") in _UNIT_GROUP]
    if not valued:
        return dict(group_items[0])
    unit_group = _UNIT_GROUP[valued[0]["quantity_unit"]]
    total = sum(
        (Decimal(str(it["quantity_value"])) * _UNIT_TO_CANONICAL_FACTOR[it["quantity_unit"]] for it in valued),
        Decimal("0"),
    )
    merged = dict(valued[0])
    merged["quantity_value"] = float(total)
    merged["quantity_unit"] = _CANONICAL_UNIT_FOR_GROUP[unit_group]
    merged["quantity_inferred"] = all(bool(it.get("quantity_inferred")) for it in valued)
    return merged


def _validate_reconcile_snapshot(raw_items, unresolved_fragments, list_items, alias_map=None):
    """Validate/diff a reconcile_inventory_snapshot router result against the
    current full inventory (list_items). Pure — no DB access, no side effects.

    Returns:
      ("unresolved", [fragment_str, ...])
      ("ambiguous_unit_group", {"ambiguous_group": [...], "rest": [...]})
      ("invalid", [reason_str, ...])
      ("ok", {"updates": [...], "additions": [...], "deletes": [...], "unchanged": [...]})

    updates/deletes entries carry item_id/old_value/old_unit so they can be fed
    directly into _compound_snapshot_is_stale() for staleness checks at confirm time.
    """
    if unresolved_fragments:
        if not isinstance(unresolved_fragments, list):
            return "unresolved", ["(не вдалося розібрати частину повідомлення)"]
        fragments = [str(f).strip() for f in unresolved_fragments if str(f).strip()]
        return "unresolved", fragments or ["(не вдалося розібрати частину повідомлення)"]

    if not isinstance(raw_items, list) or not raw_items:
        return "invalid", ["Порожній список — нема з чим звіряти запаси."]

    cleaned = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if not raw.get("is_consumable", True):
            continue
        qty_value = raw.get("quantity_value")
        qty_unit = raw.get("quantity_unit")
        if (
            not isinstance(qty_value, (int, float)) or isinstance(qty_value, bool)
            or qty_value <= 0
            or not isinstance(qty_unit, str) or qty_unit not in STRUCTURED_UNITS
        ):
            qty_value, qty_unit = None, None
        cat = raw.get("category")
        if not isinstance(cat, str) or cat not in VALID_CATEGORIES:
            cat = DEFAULT_CATEGORY
        resolved_name, canonical_name = resolve_item_name(name.strip(), alias_map or {})
        cleaned.append({
            "name": resolved_name,
            "canonical_name": canonical_name,
            "category": cat,
            "quantity_value": qty_value,
            "quantity_unit": qty_unit,
            "quantity_inferred": bool(raw.get("quantity_inferred")) or qty_value is None,
        })
    if not cleaned:
        return "invalid", ["Не знайшов жодного їстівного товару у надісланому списку."]

    ambiguous_group = _find_ambiguous_unit_group(cleaned)
    if ambiguous_group is not None:
        ids = {id(it) for it in ambiguous_group}
        rest = [it for it in cleaned if id(it) not in ids]
        return "ambiguous_unit_group", {"ambiguous_group": ambiguous_group, "rest": rest}

    new_by_canon = {}
    for it in cleaned:
        new_by_canon.setdefault(it["canonical_name"], []).append(it)
    for canon, group in new_by_canon.items():
        new_by_canon[canon] = _sum_same_group_reconcile_items(group) if len(group) > 1 else group[0]

    updates, additions, deletes, unchanged = [], [], [], []
    matched_canon = set()
    for cur in list_items:
        canon = cur.get("canonical_name") or canonicalize_name(cur["name"])
        new_item = new_by_canon.get(canon)
        old_value, old_unit = cur.get("quantity_value"), cur.get("quantity_unit")
        old_display = format_quantity_display(old_value, old_unit)
        if new_item is None:
            deletes.append({
                "item_id": cur["id"], "name": cur["name"],
                "old_value": old_value, "old_unit": old_unit, "old_display": old_display,
            })
            continue
        matched_canon.add(canon)
        if new_item["quantity_inferred"]:
            # New snapshot didn't restate a real quantity for this pre-existing item —
            # never overwrite a known quantity with a guessed default, and never
            # invent one for an item that was already unspecified.
            unchanged.append({"item_id": cur["id"], "name": cur["name"], "old_display": old_display})
            continue
        new_value, new_unit = new_item["quantity_value"], new_item["quantity_unit"]
        if new_value == old_value and new_unit == old_unit:
            unchanged.append({"item_id": cur["id"], "name": cur["name"], "old_display": old_display})
        else:
            updates.append({
                "item_id": cur["id"], "name": cur["name"],
                "old_value": old_value, "old_unit": old_unit, "old_display": old_display,
                "new_value": new_value, "new_unit": new_unit,
                "new_display": format_quantity_display(new_value, new_unit),
            })

    for canon, new_item in new_by_canon.items():
        if canon in matched_canon:
            continue
        additions.append({
            "name": new_item["name"], "canonical_name": canon, "category": new_item["category"],
            "quantity_value": new_item["quantity_value"], "quantity_unit": new_item["quantity_unit"],
            "quantity_inferred": new_item["quantity_inferred"],
            "quantity_text": format_quantity_display(new_item["quantity_value"], new_item["quantity_unit"]),
        })

    if not updates and not additions and not deletes:
        return "invalid", ["Нічого не змінилося — надісланий список повністю збігається з поточними запасами."]
    return "ok", {"updates": updates, "additions": additions, "deletes": deletes, "unchanged": unchanged}


_RECONCILE_KEEP_SEPARATE_PHRASES = {"залиш окремо", "залишити окремо", "окремо", "не об'єднуй"}


def _resolve_reconciliation_unit_clarification(ambiguous_group, text):
    """Resolve a same-product/different-unit-group ambiguity from the user's free-text
    reply. Reuses _parse_structured_quantity/STRUCTURED_UNITS — no new regex engine.

    Returns ("kept_separate", None), ("merged", [merged_item]), or ("invalid", None).
    Never guesses: anything that isn't the literal keep-separate phrase or an
    unambiguous "value unit" in the matching unit group is rejected (caller re-asks).
    Only auto-resolves the simple two-entry case (one «шт.» entry + one
    volume/mass entry) — anything more complex is rejected rather than guessed at.
    """
    normalized = (text or "").strip().lower()
    if normalized in _RECONCILE_KEEP_SEPARATE_PHRASES:
        return "kept_separate", None

    value, unit = _parse_structured_quantity(text)
    if value is None or unit is None or value <= 0:
        return "invalid", None

    count_entries = [it for it in ambiguous_group if _UNIT_GROUP.get(it.get("quantity_unit")) == "count"]
    other_entries = [it for it in ambiguous_group if _UNIT_GROUP.get(it.get("quantity_unit")) not in (None, "count")]
    if len(count_entries) != 1 or len(other_entries) != 1:
        return "invalid", None
    other = other_entries[0]
    if _UNIT_GROUP.get(unit) != _UNIT_GROUP.get(other["quantity_unit"]):
        return "invalid", None

    count_item = count_entries[0]
    per_unit_canonical = Decimal(str(value)) * _UNIT_TO_CANONICAL_FACTOR[unit]
    total_from_count = per_unit_canonical * Decimal(str(count_item["quantity_value"]))
    other_canonical = Decimal(str(other["quantity_value"])) * _UNIT_TO_CANONICAL_FACTOR[other["quantity_unit"]]
    merged_canonical = total_from_count + other_canonical
    merged_unit = _CANONICAL_UNIT_FOR_GROUP[_UNIT_GROUP[unit]]

    merged_item = dict(other)
    merged_item["quantity_value"] = float(merged_canonical)
    merged_item["quantity_unit"] = merged_unit
    merged_item["quantity_inferred"] = False
    return "merged", [merged_item]


def _format_reconciliation_preview(diff):
    lines = ["🔄 Буде звірено запаси", ""]
    if diff["updates"]:
        lines.append("✏️ Зміниться:")
        lines.append("")
        for u in diff["updates"]:
            lines.append(f"• {u['name']} — {u['old_display']}")
            lines.append(f"  → {u['name']} — {u['new_display']}")
        lines.append("")
    if diff["additions"]:
        lines.append("➕ Буде додано:")
        lines.append("")
        for a in diff["additions"]:
            label = a["name"]
            if a["quantity_text"]:
                label += f" — {a['quantity_text']}"
            if a["quantity_inferred"]:
                label += " (кількість не вказана)"
            lines.append(f"• {label}")
        lines.append("")
    if diff["deletes"]:
        lines.append("➖ Буде прибрано:")
        lines.append("")
        for d in diff["deletes"]:
            label = d["name"] + (f" — {d['old_display']}" if d["old_display"] else "")
            lines.append(f"• {label}")
        lines.append("")
    if diff["unchanged"]:
        lines.append("Без змін:")
        lines.append("")
        for u in diff["unchanged"]:
            label = u["name"] + (f" — {u['old_display']}" if u["old_display"] else "")
            lines.append(f"• {label}")
        lines.append("")
    lines.append(
        "Це повне звіряння: позиції, яких немає у надісланому списку, буде прибрано лише після підтвердження."
    )
    return "\n".join(lines).rstrip()


def _format_reconciliation_unit_clarify_question(ambiguous_group):
    name = ambiguous_group[0]["name"]
    parts = [format_quantity_display(it.get("quantity_value"), it.get("quantity_unit")) for it in ambiguous_group]
    lines = [f"Бачу дві позиції {name}:", ""]
    for p in parts:
        lines.append(f"• {name} — {p}")
    lines.append("")
    lines.append("Щоб об'єднати їх в одну позицію, мені треба знати об'єм цієї упаковки.")
    lines.append("")
    lines.append("Напиши, наприклад:")
    lines.append("• 1 л")
    lines.append("• 500 мл")
    lines.append("")
    lines.append("Або напиши:")
    lines.append("• залиш окремо")
    return "\n".join(lines)


def _continue_inventory_reconciliation_clarification(chat_id, text):
    """Continuation for pending_inventory_reconciliation_clarify — moved out
    of webhook()'s inline elif body unchanged so message_dispatcher.py can
    call it as a thin injected callback (Dispatcher V2A route 8) without
    duplicating this business logic or touching the database/Gemini
    directly itself."""
    clarify_data = pending_inventory_reconciliation_clarify[chat_id]
    kind, resolved = _resolve_reconciliation_unit_clarification(clarify_data["ambiguous_group"], text)
    if kind == "invalid":
        send_message(chat_id, _format_reconciliation_unit_clarify_question(clarify_data["ambiguous_group"]))
        return
    pending_inventory_reconciliation_clarify.pop(chat_id, None)
    combined = clarify_data["rest"] + (resolved if kind == "merged" else clarify_data["ambiguous_group"])
    household_id = clarify_data["household_id"]
    user_db_id = clarify_data["user_db_id"]
    try:
        list_items = get_inventory_items(household_id)
        next_ambiguous = _find_ambiguous_unit_group(combined)
        if next_ambiguous is not None:
            ids = {id(it) for it in next_ambiguous}
            rest2 = [it for it in combined if id(it) not in ids]
            pending_inventory_reconciliation_clarify[chat_id] = {
                "ambiguous_group": next_ambiguous, "rest": rest2,
                "household_id": household_id, "user_db_id": user_db_id,
            }
            send_message(chat_id, _format_reconciliation_unit_clarify_question(next_ambiguous))
        else:
            alias_map = get_household_alias_map(household_id)
            kind2, payload2 = _validate_reconcile_snapshot(combined, [], list_items, alias_map=alias_map)
            if kind2 == "ok":
                pending_inventory_reconciliation[chat_id] = {
                    "updates": payload2["updates"], "additions": payload2["additions"],
                    "deletes": payload2["deletes"], "household_id": household_id, "user_db_id": user_db_id,
                }
                send_message(
                    chat_id, _format_reconciliation_preview(payload2), reply_markup=RECONCILIATION_PREVIEW_KEYBOARD
                )
            else:
                send_message(
                    chat_id,
                    "Не зміг безпечно завершити звіряння запасів. Спробуй ще раз, надіславши повний список.",
                )
    except Exception:
        send_message(chat_id, INVENTORY_ERROR_MSG)


def _validate_quick_add_items(raw_items, alias_map=None):
    """Validate Gemini quick_add_to_inventory items for an empty shopping list.

    Drops non-consumable entries (returned separately as ignored names),
    never trusts Gemini's quantity_value/unit blindly — re-derives structured
    fields locally via normalize_item_quantity, defaulting to "1 шт." inferred
    only when no safe explicit quantity was given. Duplicate items are merged
    the same way pending-add batches already are.
    Returns (items, ignored_names) or None if nothing usable remains.
    """
    if not isinstance(raw_items, list) or not raw_items:
        return None
    valid = []
    ignored = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if not raw.get("is_consumable", True):
            ignored.append(name)
            continue
        cat = raw.get("category")
        if not isinstance(cat, str) or cat not in VALID_CATEGORIES:
            cat = DEFAULT_CATEGORY
        qty_value = raw.get("quantity_value")
        qty_unit = raw.get("quantity_unit")
        if (
            not isinstance(qty_value, (int, float)) or isinstance(qty_value, bool)
            or qty_value <= 0
            or not isinstance(qty_unit, str) or qty_unit not in STRUCTURED_UNITS
        ):
            qty_value, qty_unit = None, None
        normalized = normalize_item_quantity(
            name, "", quantity_value=qty_value, quantity_unit=qty_unit, allow_default_unit=(qty_value is None),
            alias_map=alias_map,
        )
        item = {"name": name, "category": cat, "was_corrected": False}
        item.update(normalized)
        valid.append(item)
    if not valid:
        return None
    return _auto_merge_in_place(valid), ignored


def _format_quick_purchase_preview(items, ignored_items=None):
    header = f"🧊 Буде додано до запасів: {len(items)}"
    text = format_grouped_list(items, header)
    if ignored_items:
        text += "\n\nНе додано: " + ", ".join(ignored_items)
    return text


_SAVED_LIST_ROUTER_FALLBACK = {
    "intent": "none", "action": None, "selected_numbers": [], "updates": [], "merge_groups": [], "items": [],
    "consumptions": [], "operations": [], "unresolved_fragments": [], "unresolved_fragments_present": False,
}


def _ask_gemini_saved_list_router(user_text, items, context_type):
    """Gemini call: detect edit_saved_items, merge_duplicates, start_action or
    quick_add_to_inventory (for an empty shopping list) for an active saved list."""
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        item_qty = _effective_quantity(item)[2]
        if item_qty:
            label += f" — {item_qty}"
        label += f" [{item.get('category') or DEFAULT_CATEGORY}]"
        lines.append(label)
    prompt = (
        f"Контекст: {context_type}\n"
        "Поточний список:\n" + "\n".join(lines)
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], SAVED_LIST_EDIT_PROMPT, temperature=0.1)
    if not raw:
        return dict(_SAVED_LIST_ROUTER_FALLBACK)
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        raw_fragments = data.get("unresolved_fragments")
        return {
            "intent": data.get("intent", "none"),
            "action": data.get("action"),
            "selected_numbers": data.get("selected_numbers") if isinstance(data.get("selected_numbers"), list) else [],
            "updates": data.get("updates") if isinstance(data.get("updates"), list) else [],
            "merge_groups": data.get("merge_groups") if isinstance(data.get("merge_groups"), list) else [],
            "items": data.get("items") if isinstance(data.get("items"), list) else [],
            "consumptions": data.get("consumptions") if isinstance(data.get("consumptions"), list) else [],
            "operations": data.get("operations") if isinstance(data.get("operations"), list) else [],
            "unresolved_fragments": raw_fragments if isinstance(raw_fragments, list) else [],
            # Distinct from the coerced "unresolved_fragments" above: this tracks whether
            # Gemini actually included the field (vs. omitted/malformed), which start_action
            # and consume_inventory_quantity treat as a hard block. compound/reconcile keep
            # reading only the coerced key above and are unaffected.
            "unresolved_fragments_present": isinstance(raw_fragments, list),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_SAVED_LIST_ROUTER_FALLBACK)


_ALIAS_ROUTER_FALLBACK = {
    "intent": "none", "alias_text": None, "target_display_name": None,
    "selected_numbers": [], "unresolved_fragments": [],
}


def _ask_gemini_alias_router(user_text, aliases=None):
    """ONE Gemini call per message in aliases mode (or via the global gate).
    `aliases` (from list_household_aliases, optional) is the current
    household's alias list — passed so Gemini can select existing aliases by
    number for bulk delete_aliases actions. Gemini never touches SQL — it
    only classifies intent and extracts alias_text/target_display_name/
    selected_numbers."""
    aliases = aliases or []
    lines = [f"{i}. {a['alias_text']} → {a['target_display_name']}" for i, a in enumerate(aliases, start=1)]
    prompt = (
        ("Поточні домашні назви:\n" + "\n".join(lines) if lines else "Домашніх назв поки немає.")
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], ALIAS_ROUTER_PROMPT, temperature=0.1)
    if not raw:
        return dict(_ALIAS_ROUTER_FALLBACK)
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
        return {
            "intent": data.get("intent", "none"),
            "alias_text": data.get("alias_text"),
            "target_display_name": data.get("target_display_name"),
            "selected_numbers": data.get("selected_numbers") if isinstance(data.get("selected_numbers"), list) else [],
            "unresolved_fragments": data.get("unresolved_fragments") if isinstance(data.get("unresolved_fragments"), list) else [],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_ALIAS_ROUTER_FALLBACK)


# Expense Gemini router, validators, formatters, and Telegram handlers now
# live in expenses.py — re-exported here so existing bot.<name> references
# and tests keep working unchanged.
_ask_gemini_expense_router = expenses._ask_gemini_expense_router
_parse_expense_amount = expenses._parse_expense_amount
_validate_expense_date = expenses._validate_expense_date
_validate_expense_category = expenses._validate_expense_category
_clean_expense_description = expenses._clean_expense_description
_validate_expense_router_result = expenses._validate_expense_router_result
_format_expense_amount = expenses._format_expense_amount
_format_expense_date_display = expenses._format_expense_date_display
_format_expense_preview = expenses._format_expense_preview
_format_recent_expenses = expenses._format_recent_expenses
_format_expense_month_summary = expenses._format_expense_month_summary
_format_expenses_hub = expenses._format_expenses_hub
_handle_expense_report_command = expenses._handle_expense_report_command
_handle_expenses_hub = expenses._handle_expenses_hub
_handle_expense_command = expenses._handle_expense_command
_format_expense_delete_list = expenses._format_expense_delete_list
_format_expense_delete_preview = expenses._format_expense_delete_preview
_resolve_expense_delete_selection = expenses._resolve_expense_delete_selection
_handle_expense_delete_button = expenses._handle_expense_delete_button
_handle_expense_delete_global_command = expenses._handle_expense_delete_global_command
_handle_expense_delete_selection_text = expenses._handle_expense_delete_selection_text


def _format_saved_edit_preview(items_snapshot, validated_updates, context_type):
    """Format before/after preview for a saved list edit."""
    icon = "🛒" if context_type == "shopping_saved" else "🧊"
    lines = [f"{icon} Буде змінено: {len(validated_updates)}", ""]
    for upd in validated_updates:
        idx = upd["item_number"] - 1
        old = items_snapshot[idx]
        old_label = old["name"]
        old_qty = _effective_quantity(old)[2]
        if old_qty:
            old_label += f" — {old_qty}"
        new_name = upd.get("name") or old["name"]
        new_qty = upd.get("quantity_text")
        if new_qty is None:
            new_qty = old_qty
        new_label = new_name
        if new_qty:
            new_label += f" — {new_qty}"
        lines.append(f"{upd['item_number']}. {old_label}")
        lines.append(f"   → {new_label}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_merge_preview(validated_groups):
    return list_editing._format_merge_preview(validated_groups, _effective_quantity)

# =========================
# SELECTION / PREVIEW HELPERS
# =========================

def _validate_selected_numbers(numbers, items):
    """Validate raw selected_numbers against the current items list.

    Returns an ordered (as given), deduped list of item dicts, dropping
    out-of-range numbers individually rather than invalidating the whole
    selection. Returns None if numbers isn't a list or nothing remains.
    """
    if not isinstance(numbers, list):
        return None
    total = len(items)
    seen = set()
    selected = []
    for n in numbers:
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= total and n not in seen:
            seen.add(n)
            selected.append(items[n - 1])
    return selected if selected else None


def _snapshot_is_stale(item_ids, current_items):
    """True if any snapshot item id is no longer present in the current list."""
    current_ids = {it["id"] for it in current_items}
    return not set(item_ids).issubset(current_ids)


def _snapshot_targets(items):
    """Build a {item_id, quantity_value, quantity_unit} snapshot-target list for the
    shared stale-precondition guard (database._verify_targets_in_tx). This is the one
    reusable mechanism every confirm-flow uses to describe "what did these target rows
    look like when the preview was built" — the actual check-and-mutate happens inside
    a single transaction on the database side, never as a separate pre-check.

    Accepts either raw inventory/shopping item dicts (id, quantity_value, quantity_unit,
    as returned by get_inventory_items/get_active_shopping_items) or already-resolved
    change dicts (item_id, old_value, old_unit, as built by _validate_consumptions/
    _validate_compound_operations/_validate_reconcile_snapshot/_validate_saved_updates)
    — whichever shape is present.
    """
    targets = []
    for it in items:
        item_id = it["item_id"] if "item_id" in it else it["id"]
        value = it["old_value"] if "old_value" in it else it.get("quantity_value")
        unit = it["old_unit"] if "old_unit" in it else it.get("quantity_unit")
        targets.append({"item_id": item_id, "quantity_value": value, "quantity_unit": unit})
    return targets


def _merge_snapshot_targets(validated_groups):
    return list_editing._merge_snapshot_targets(validated_groups, canonicalize_name, DEFAULT_CATEGORY)


CLEANUP_NOTICE_ACKNOWLEDGED_MSG = "Цю перевірку скасовано. Я нічого не змінював."


# =========================
# INVENTORY CLEANUP / MERGE v1.1
# =========================
# Global-scope route (works regardless of which menu/context is open,
# same reasoning as household_router.py's own gate) for "об'єднай молоко в
# запасах"-style duplicate cleanup requests. inventory.py owns the pure
# text-classification/grouping/cleanup-alias/preview-formatting; this reuses
# the EXISTING pending_merge dict and "✅ Об'єднати"/"❌ Скасувати" wiring
# already in _try_handle_confirm_or_cancel (list_type "inventory_cleanup")
# and database.execute_inventory_cleanup_merge's own StaleSnapshotError-
# protected transaction — no new pending dict, no new write path, no new
# keyboard. execute_inventory_cleanup_merge (unlike plain
# execute_merge_inventory) also records an Action History journal row, so a
# confirmed cleanup merge becomes the latest undo-able "↩️ Скасувати останню
# дію" action — same journal table/operation_type/restore path
# apply_global_household_operations already uses, not a new one.
def _start_inventory_cleanup(chat_id, user_id, display_name, product_phrase):
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        canonical_name_candidates = inventory.cleanup_canonical_name_candidates(canonicalize_name, product_phrase)
        items = get_inventory_items(household_id)
    except Exception:
        send_message(chat_id, INVENTORY_ERROR_MSG)
        return

    canonical_name = canonical_name_candidates[0]
    candidates = inventory.find_inventory_cleanup_candidates(items, canonical_name_candidates, canonicalize_name)
    if len(candidates) < 2:
        if candidates:
            qty = _effective_quantity(candidates[0])[2]
            label = candidates[0]["name"] + (f" — {qty}" if qty else "")
            send_message(chat_id, f"У запасах лише один запис «{label}», дублікатів немає.", reply_markup=INVENTORY_KEYBOARD)
        else:
            send_message(chat_id, f"Не знайшов у запасах записів «{product_phrase.strip()}».", reply_markup=INVENTORY_KEYBOARD)
        return

    grouping = inventory.group_inventory_cleanup_candidates(candidates)
    validated_groups = []
    for group in grouping["groups"]:
        base = group["rows"][0]
        validated_groups.append({
            "item_ids": [r["id"] for r in group["rows"]],
            "merged_name": base["name"],
            "merged_quantity_text": format_quantity_display(group["merged_value"], group["merged_unit"]),
            "merged_category": base.get("category") or DEFAULT_CATEGORY,
            "canonical_name": canonical_name,
            "merged_quantity_value": group["merged_value"],
            "merged_quantity_unit": group["merged_unit"],
            "items": group["rows"],
        })

    preview = inventory.format_inventory_cleanup_preview(validated_groups, grouping["incompatible"], _effective_quantity)

    if not validated_groups:
        # Nothing safe to auto-merge — read-only warning, no pending_merge
        # entry (nothing to confirm/cancel), no merge keyboard. Still record
        # a small ephemeral notice so the very next "↩️ Скасувати останню
        # дію"/"❌ Скасувати" press acknowledges THIS read-only check
        # instead of silently opening an unrelated older historical undo —
        # and so an immediate follow-up rename/delete request has the shown
        # candidate rows available as a contextual hint (see
        # _start_inventory_delete).
        pending_cleanup_notice[chat_id] = {"rows": grouping["incompatible"], "household_id": household_id}
        send_message(chat_id, preview, reply_markup=INVENTORY_KEYBOARD)
        return

    pending_merge[chat_id] = {
        "groups": validated_groups,
        "targets": _merge_snapshot_targets(validated_groups),
        "household_id": household_id,
        "user_db_id": user_db_id,
        "list_type": "inventory_cleanup",
    }
    send_message(chat_id, preview, reply_markup=MERGE_PREVIEW_KEYBOARD)


def _apply_inventory_cleanup_merge(chat_id, merge_data):
    """Same DB write/stale-protection as pressing "✅ Об'єднати" — reached
    either from that button (_try_handle_confirm_or_cancel) or a follow-up
    text confirm ("об'єднай их"). execute_inventory_cleanup_merge records
    the Action History journal row that makes this undo-able."""
    try:
        count = execute_inventory_cleanup_merge(
            merge_data["household_id"], merge_data["user_db_id"], merge_data["groups"], merge_data.get("targets"),
        )
        send_message(chat_id, f"✅ Об'єднано груп: {count}", reply_markup=INVENTORY_KEYBOARD)
    except StaleSnapshotError:
        send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=INVENTORY_KEYBOARD)
    except Exception:
        send_message(chat_id, "Не вдалося виконати об'єднання. Спробуйте ще раз.", reply_markup=INVENTORY_KEYBOARD)


def _route_inventory_cleanup(chat_id, user_id, display_name, text):
    followup, product_phrase = inventory.parse_inventory_cleanup_request(text)
    if followup is None:
        return False

    if followup:
        cleanup = pending_merge.get(chat_id)
        if cleanup and cleanup.get("list_type") == "inventory_cleanup":
            merge_data = pending_merge.pop(chat_id)
            _apply_inventory_cleanup_merge(chat_id, merge_data)
        else:
            send_message(chat_id, "Напиши, який товар об'єднати, наприклад: «Об'єднай молоко в запасах».")
        return True

    if _has_blocking_pending_state_for_reports(chat_id):
        # Another flow's preview/clarification is already open — never
        # silently fall through to general AI; same guard message the
        # Global Household Router already uses for the same situation.
        send_message(chat_id, GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG)
        return True
    _start_inventory_cleanup(chat_id, user_id, display_name, product_phrase)
    return True


INVENTORY_ADMIN_NOT_FOUND_MSG = "Не знайшов такого запису в запасах."


# =========================
# INVENTORY CLEANUP ADMIN v1 — deterministic rename/delete of ONE existing
# inventory row ("перейменуй ser на сир", "видали mlekо із запасів", "прибери
# сосисок — пару"). Global-scope route (works regardless of which menu is
# open, same reasoning as _route_inventory_cleanup), checked right after it
# so cleanup's own "прибери дублікат..." trigger always wins for that exact
# phrase (see inventory.parse_inventory_delete_request's docstring). Reuses
# pending_cleanup_admin (preview/confirm/cancel) + database.
# execute_inventory_rename/execute_inventory_delete (stale-protected,
# journal-recorded exactly like execute_inventory_cleanup_merge — same
# undo path, no new one) — no DB write happens before an explicit "✅ Так,
# застосувати" confirm.
# =========================
def _resolve_inventory_admin_candidates(chat_id, household_id, items, name_phrase, quantity_hint):
    """Live-inventory candidate search (inventory.resolve_inventory_admin_
    candidates), then — only if that alone is still ambiguous (2+ rows) — a
    best-effort narrowing against this chat's active Inventory Cleanup
    read-only-warning context (pending_cleanup_notice), if one exists and
    actually narrows it down to exactly one row. Never used to override an
    already-unique live-inventory match, never guesses beyond what's safe."""
    canonical_name_candidates = inventory.cleanup_canonical_name_candidates(canonicalize_name, name_phrase)
    candidates = inventory.resolve_inventory_admin_candidates(
        items, canonical_name_candidates, canonicalize_name, quantity_hint=quantity_hint,
        name_phrase=name_phrase, name_normalizer=_normalize_display_name_for_exact_match,
    )
    if len(candidates) > 1:
        context = pending_cleanup_notice.get(chat_id)
        context_rows = (context or {}).get("rows") if isinstance(context, dict) else None
        if context_rows:
            context_ids = {r["id"] for r in context_rows}
            narrowed = [c for c in candidates if c["id"] in context_ids]
            if len(narrowed) == 1:
                candidates = narrowed
    return candidates


def _inventory_admin_target(row):
    return {
        "item_id": row["id"],
        "quantity_value": row.get("quantity_value"),
        "quantity_unit": row.get("quantity_unit"),
        "name": row.get("name"),
        "canonical_name": row.get("canonical_name"),
    }


def _start_inventory_rename(chat_id, user_id, display_name, old_phrase, new_phrase):
    origin = household_router.current_origin(chat_id)
    if _has_blocking_pending_state_for_reports(chat_id):
        send_message(chat_id, GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG)
        return
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        items = get_inventory_items(household_id)
    except Exception:
        send_message(chat_id, INVENTORY_ERROR_MSG)
        return

    candidates = _resolve_inventory_admin_candidates(chat_id, household_id, items, old_phrase, None)
    if not candidates:
        send_message(chat_id, INVENTORY_ADMIN_NOT_FOUND_MSG, reply_markup=INVENTORY_KEYBOARD)
        return
    if len(candidates) > 1:
        pending_cleanup_admin_disambiguation[chat_id] = {
            "action": "rename", "candidates": candidates, "new_phrase": new_phrase,
            "household_id": household_id, "user_db_id": user_db_id, "origin": origin,
        }
        send_message(
            chat_id, inventory.format_inventory_admin_ambiguous_message(candidates, _effective_quantity),
            reply_markup=INVENTORY_KEYBOARD,
        )
        return

    row = candidates[0]
    new_name = inventory.capitalize_first(new_phrase)
    new_canonical_name = canonicalize_name(new_name)
    if inventory.is_noop_rename(row, new_name, new_canonical_name, _normalize_display_name_for_exact_match):
        send_message(chat_id, inventory.format_noop_rename_message(row["name"]), reply_markup=INVENTORY_KEYBOARD)
        return
    quantity_text = _effective_quantity(row)[2]
    pending_cleanup_notice.pop(chat_id, None)
    pending_cleanup_admin[chat_id] = {
        "action": "rename",
        "household_id": household_id, "user_db_id": user_db_id, "origin": origin,
        "item_id": row["id"], "new_name": new_name, "new_canonical_name": new_canonical_name,
        "target": _inventory_admin_target(row),
    }
    preview = inventory.format_inventory_rename_preview(row["name"], quantity_text, new_name)
    send_message(chat_id, preview, reply_markup=GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD)


def _start_inventory_delete(chat_id, user_id, display_name, name_phrase, quantity_hint):
    origin = household_router.current_origin(chat_id)
    if _has_blocking_pending_state_for_reports(chat_id):
        send_message(chat_id, GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG)
        return
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        items = get_inventory_items(household_id)
    except Exception:
        send_message(chat_id, INVENTORY_ERROR_MSG)
        return

    candidates = _resolve_inventory_admin_candidates(chat_id, household_id, items, name_phrase, quantity_hint)
    if not candidates:
        send_message(chat_id, INVENTORY_ADMIN_NOT_FOUND_MSG, reply_markup=INVENTORY_KEYBOARD)
        return
    if len(candidates) > 1:
        pending_cleanup_admin_disambiguation[chat_id] = {
            "action": "delete", "candidates": candidates, "new_phrase": None,
            "household_id": household_id, "user_db_id": user_db_id, "origin": origin,
        }
        send_message(
            chat_id, inventory.format_inventory_admin_ambiguous_message(candidates, _effective_quantity),
            reply_markup=INVENTORY_KEYBOARD,
        )
        return

    row = candidates[0]
    quantity_text = _effective_quantity(row)[2]
    pending_cleanup_notice.pop(chat_id, None)
    pending_cleanup_admin[chat_id] = {
        "action": "delete",
        "household_id": household_id, "user_db_id": user_db_id, "origin": origin,
        "item_id": row["id"], "target": _inventory_admin_target(row),
    }
    preview = inventory.format_inventory_delete_preview(row["name"], quantity_text)
    send_message(chat_id, preview, reply_markup=GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD)


def _route_inventory_admin(chat_id, user_id, display_name, text):
    old_phrase, new_phrase = inventory.parse_inventory_rename_request(text)
    if old_phrase is not None:
        _start_inventory_rename(chat_id, user_id, display_name, old_phrase, new_phrase)
        return True

    name_phrase, quantity_hint = inventory.parse_inventory_delete_request(text)
    if name_phrase is not None:
        _start_inventory_delete(chat_id, user_id, display_name, name_phrase, quantity_hint)
        return True

    return False


def _continue_cleanup_admin_disambiguation(chat_id, text):
    """Follow-up reply to an Inventory Cleanup Admin ambiguous-candidates
    message ("Mleko 1 шт", "1 шт", "№2", "2", ...) — resolved via
    inventory.resolve_cleanup_admin_disambiguation_reply against the
    candidate rows stored in pending_cleanup_admin_disambiguation. A unique
    match opens the SAME rename/delete preview (pending_cleanup_admin,
    awaiting "✅ Так, застосувати"/"❌ Скасувати") the direct single-match
    path would have built; anything still ambiguous re-asks with the same
    candidate list instead of ever falling through to general AI-chat."""
    data = pending_cleanup_admin_disambiguation.get(chat_id)
    if not data:
        return
    candidates = data["candidates"]
    selected = inventory.resolve_cleanup_admin_disambiguation_reply(
        text, candidates, _normalize_display_name_for_exact_match,
    )
    if selected is None:
        send_message(
            chat_id, inventory.format_inventory_admin_ambiguous_message(candidates, _effective_quantity),
            reply_markup=INVENTORY_KEYBOARD,
        )
        return

    pending_cleanup_admin_disambiguation.pop(chat_id, None)
    household_id = data["household_id"]
    user_db_id = data["user_db_id"]
    origin = data["origin"]
    quantity_text = _effective_quantity(selected)[2]
    pending_cleanup_notice.pop(chat_id, None)

    if data["action"] == "rename":
        new_name = inventory.capitalize_first(data["new_phrase"])
        new_canonical_name = canonicalize_name(new_name)
        if inventory.is_noop_rename(selected, new_name, new_canonical_name, _normalize_display_name_for_exact_match):
            send_message(
                chat_id, inventory.format_noop_rename_message(selected["name"]), reply_markup=INVENTORY_KEYBOARD,
            )
            return
        pending_cleanup_admin[chat_id] = {
            "action": "rename",
            "household_id": household_id, "user_db_id": user_db_id, "origin": origin,
            "item_id": selected["id"], "new_name": new_name, "new_canonical_name": new_canonical_name,
            "target": _inventory_admin_target(selected),
        }
        preview = inventory.format_inventory_rename_preview(selected["name"], quantity_text, new_name)
    else:
        pending_cleanup_admin[chat_id] = {
            "action": "delete",
            "household_id": household_id, "user_db_id": user_db_id, "origin": origin,
            "item_id": selected["id"], "target": _inventory_admin_target(selected),
        }
        preview = inventory.format_inventory_delete_preview(selected["name"], quantity_text)
    send_message(chat_id, preview, reply_markup=GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD)


def _apply_cleanup_admin_confirm(chat_id):
    """"✅ Так, застосувати" button for a pending_cleanup_admin preview. Pops
    the pending action BEFORE the database call, same duplicate-press
    protection as _apply_global_household_confirm."""
    if chat_id not in pending_cleanup_admin:
        send_message(chat_id, "Немає активної дії для підтвердження.")
        return
    data = pending_cleanup_admin.pop(chat_id)
    origin = data.get("origin", "global")
    keyboard = household_router.origin_keyboard(origin)
    try:
        if data["action"] == "rename":
            execute_inventory_rename(
                data["household_id"], data["user_db_id"], data["item_id"],
                data["new_name"], data["new_canonical_name"], data["target"],
            )
        else:
            execute_inventory_delete(data["household_id"], data["user_db_id"], data["item_id"], data["target"])
        send_message(chat_id, "✅ Зміни застосовано.", reply_markup=keyboard)
    except StaleSnapshotError:
        send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=keyboard)
    except Exception:
        send_message(chat_id, "Не вдалося застосувати зміни. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


# _should_restore_persisted_context's real logic now lives in
# interaction_state.py (called via _interaction_state_deps); thin
# compatibility wrapper, same reasoning as the cleanup/guard wrappers above.
def _should_restore_persisted_context(chat_id):
    return interaction_state.should_restore_persisted_context(_interaction_state_deps, chat_id)


def _ask_gemini_for_selection(user_text, items, list_label, action_label):
    """Gemini call for the standalone selection flow (shopping_mode "marking"/
    "deleting", inventory_mode "removing").

    Returns one of:
      ("unresolved", [fragment_str, ...]) — part of the message couldn't be
          resolved to a list item, or Gemini omitted unresolved_fragments
          entirely; nothing should be selected.
      ("invalid", None) — call failed, malformed JSON, or no valid selection.
      ("ok", [item, ...]) — the ordered list of selected item dicts.
    """
    lines = []
    for i, item in enumerate(items):
        label = f"{i + 1}. {item['name']}"
        item_qty = _effective_quantity(item)[2]
        if item_qty:
            label += f" — {item_qty}"
        if item.get("category"):
            label += f" [{item['category']}]"
        lines.append(label)
    prompt = (
        f"{list_label} (дія: {action_label}):\n"
        + "\n".join(lines)
        + f"\n\nКористувач написав: {user_text}"
    )
    raw = call_gemini([{"role": "user", "content": prompt}], SELECTION_PROMPT, temperature=0.1)
    if not raw:
        return "invalid", None
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, TypeError):
        return "invalid", None
    raw_fragments = data.get("unresolved_fragments")
    if not isinstance(raw_fragments, list):
        return "invalid", None
    fragments = [str(f).strip() for f in raw_fragments if str(f).strip()]
    if fragments:
        return "unresolved", fragments
    selected = _validate_selected_numbers(data.get("selected_numbers"), items)
    if selected is None:
        return "invalid", None
    return "ok", selected

# _show_remove_preview/parse_inventory_list_with_gemini (real logic) now live
# in legacy_inventory_flow.py. bot.py keeps a thin compatibility wrapper below
# for parse_inventory_list_with_gemini, since it's also exposed as an
# InventoryFlowDeps callback (deps.parse_inventory_list_with_gemini resolves
# this exact bot.py name at call time) — patch.object(bot,
# "parse_inventory_list_with_gemini", ...) in existing tests must keep
# affecting the real inventory_mode "adding" webhook flow, not just direct
# calls.
def parse_inventory_list_with_gemini(text, alias_map=None):
    return legacy_inventory_flow.parse_inventory_list_with_gemini(_inventory_deps, text, alias_map=alias_map)


def format_batch_preview(items, ignored_items=None):
    header = f"🛒 Знайшов товарів: {len(items)}"
    text = format_grouped_list(items, header)
    if ignored_items:
        text += "\n\nНе додано: " + ", ".join(ignored_items)
    return text

# =========================
# GLOBAL HOUSEHOLD ROUTER v1 — thin bot.py-side dispatch. household_router.py
# does the actual Gemini call + validation (pure, no Telegram/pending state
# of its own); this function only fetches the live snapshots it needs,
# stores pending_global_household, and sends the resulting message.
# =========================
def _handle_household_router_result(chat_id, kind, payload, household_id, user_db_id, origin, keyboard):
    """Shared tail for both _try_global_household_router and
    _try_global_explicit_add — both call a household_router builder that
    returns the exact same (kind, payload) shape, so the unresolved/
    invalid/clarify/ok dispatch (preview, pending_global_household,
    Inventory Quantity Clarification v1 continuation state) only needs to
    exist once. Always returns True (message fully handled) — "none" is
    handled by each caller itself, before this function is ever called,
    since what counts as "nothing to do" differs slightly between them.
    """
    if kind == "unresolved":
        send_message(chat_id, household_router.format_unresolved_message(payload), reply_markup=keyboard)
        return True
    if kind == "invalid":
        send_message(chat_id, household_router.format_invalid_message(payload), reply_markup=keyboard)
        return True
    if kind == "clarify":
        # Inventory Representation Guard: an inferred incoming quantity
        # conflicts with an existing row's representation — block the
        # whole compound preview, nothing is written, and start the
        # Inventory Quantity Clarification v1 continuation state instead
        # of a dead-end message, so the next plain-text reply (e.g.
        # "1Л") can safely continue THIS command.
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": household_id,
            "user_db_id": user_db_id,
            "origin": origin,
            "item_name": payload["item_name"],
            "canonical_name": payload["canonical_name"],
            "category": payload["category"],
            "add_shopping_items": payload["add_shopping_items"],
            "add_inventory_items": payload["add_inventory_items"],
            "consume_changes": payload["consume_changes"],
            "new_expenses": payload["new_expenses"],
            "new_expense": payload["new_expense"],
            "delete_expense": payload["delete_expense"],
        }
        send_message(
            chat_id,
            format_global_quantity_clarification_message(payload["item_name"], payload["existing_items"]),
            reply_markup=keyboard,
        )
        return True
    if kind == "clarify_representation":
        # Inventory Representation Clarification V2: a structured count row
        # conflicts with an explicit incoming mass/volume quantity for the
        # same product — block the whole compound preview, nothing is
        # written, and start its own continuation state instead of a
        # dead-end message, so the next reply (a button choice, then
        # optionally a total-quantity number) can safely continue THIS
        # command.
        pending_inventory_representation_clarification[chat_id] = {
            "household_id": household_id, "user_db_id": user_db_id, "origin": origin,
            "stage": "choice",
            "conflict": payload["conflict"],
            "queue": payload["queue"],
            "add_shopping_items": payload["add_shopping_items"],
            "add_inventory_items": payload["add_inventory_items"],
            "inventory_merge_targets": payload["inventory_merge_targets"],
            "consume_changes": payload["consume_changes"],
            "new_expenses": payload["new_expenses"],
            "new_expense": payload["new_expense"],
            "delete_expense": payload["delete_expense"],
            "representation_resolutions": [],
        }
        _send_representation_v2_choice_message(chat_id, payload["conflict"])
        return True
    # kind == "ok"
    inventory_targets = _snapshot_targets(payload["consume_changes"]) + payload["inventory_merge_targets"]
    pending_global_household[chat_id] = {
        "add_shopping_items": payload["add_shopping_items"],
        "add_inventory_items": payload["add_inventory_items"],
        "consume_changes": payload["consume_changes"],
        "inventory_targets": inventory_targets,
        "new_expenses": payload["new_expenses"],
        "new_expense": payload["new_expense"],
        "delete_expense": payload["delete_expense"],
        "household_id": household_id,
        "user_db_id": user_db_id,
        "origin": origin,
    }
    send_message(chat_id, household_router.format_preview(payload), reply_markup=GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD)
    return True


def _try_global_household_router(chat_id, user_id, display_name, text):
    """Returns True if the message was fully handled (a preview, an
    unresolved/invalid clarification, or an error message was sent) — the
    caller must not fall through to the legacy gates in that case. Returns
    False only for a genuine intent=="none" (nothing was sent), letting the
    caller fall through to the existing legacy gates/AI-chat exactly as
    before this router existed."""
    origin = household_router.current_origin(chat_id)
    keyboard = household_router.origin_keyboard(origin)
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        shopping_items = get_active_shopping_items(household_id)
        inventory_items = get_inventory_items(household_id)
        recent_expenses = get_recent_expenses_for_deletion(household_id, limit=20)
        alias_map = get_household_alias_map(household_id)
        kind, payload = household_router.build_household_operations_preview(
            text, shopping_items, inventory_items, recent_expenses, alias_map=alias_map,
        )
        if kind == "none":
            return False
        return _handle_household_router_result(chat_id, kind, payload, household_id, user_db_id, origin, keyboard)
    except Exception:
        send_message(chat_id, "Не вдалося обробити команду. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)
        return True


# =========================
# AMBIGUOUS "ДОДАЙ ... ЗА СУМУ" GUARD — a message starting with the same bare
# "Додай"/"Додайте" verb Global Explicit Add v1/Global Bare Add v1 both react
# to, but that ALSO carries a recognized expense amount (zł/zl/pln/a bare
# "z" — the exact same expenses._EXPENSE_AMOUNT_RE both of those routes
# already use, never a second copy), is genuinely ambiguous: did the user
# want a shopping/inventory item, or an expense record? Rather than silently
# picking one (the old bug: it fell through to the plain expense-add gate and
# created an expense-only preview for what was clearly meant as an item
# add), this is intercepted BEFORE Explicit Add/Bare Add/the Global
# Household Router/the expense gate — no Gemini call, no DB read, no
# preview, no clarification — and the user is told to disambiguate with an
# existing, already-supported phrasing instead.
#
# Deliberately excludes "Додай витрату ..." (the next word is a form of
# "витрата") — that phrasing already unambiguously means "add an expense",
# not "add an item", so it must keep working through the existing expense-add
# flow untouched.
# =========================
_AMBIGUOUS_ADD_PREFIX_RE = re.compile(r"^(?:додай(?:те)?|додати)\s+", re.IGNORECASE)
_AMBIGUOUS_ADD_EXPENSE_WORD_RE = re.compile(r"^витрат\w*", re.IGNORECASE)

AMBIGUOUS_ADD_WITH_PRICE_MSG = (
    "Команда «Додай ... за суму» неоднозначна.\n\n"
    "Щоб додати товар у запаси та записати витрату, напиши:\n"
    "«Купив молоко за 10 zł».\n\n"
    "Щоб додати лише витрату, напиши:\n"
    "«Молоко 10 zł»."
)


def _is_ambiguous_add_with_price(text):
    """True if `text` is a bare "Додай"/"Додайте" command (with or without
    an explicit destination phrase — this fires before either route ever
    inspects it) whose remainder carries a recognized expense amount, and
    isn't the explicit "Додай витрату ..." expense-add phrasing. Pure/local,
    no Gemini, no DB — same amount regex Global Explicit Add v1/Global Bare
    Add v1 already use (expenses._EXPENSE_AMOUNT_RE), never duplicated.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    match = _AMBIGUOUS_ADD_PREFIX_RE.match(stripped)
    if not match:
        return False
    rest = stripped[match.end():].strip()
    if not rest:
        return False
    if _AMBIGUOUS_ADD_EXPENSE_WORD_RE.match(rest):
        return False
    return bool(expenses._EXPENSE_AMOUNT_RE.search(rest))


def _handle_ambiguous_add_with_price(chat_id):
    """Sends the disambiguation message on the caller's current keyboard —
    pure RAM lookups only (household_router.current_origin/origin_keyboard),
    never a DB read."""
    origin = household_router.current_origin(chat_id)
    keyboard = household_router.origin_keyboard(origin)
    send_message(chat_id, AMBIGUOUS_ADD_WITH_PRICE_MSG, reply_markup=keyboard)


def _try_global_explicit_add(chat_id, user_id, display_name, text):
    """Global Explicit Add v1 — a message with an EXPLICIT destination
    phrase ("Додай до покупок ...", "Додай в запаси ...", see
    household_router.detect_explicit_add_destination) OR a standalone
    destination HEADER line ("🛒 Покупки"/"🧊 Запаси"/"до покупок"/"до
    запасів" as the whole first line, see
    household_router.detect_header_add_destination) adds to that list
    regardless of which menu is open. Returns True if handled (a preview,
    clarification, or invalid/unresolved message was sent). Returns False
    only when neither shape matches at all, letting the caller fall through
    to _try_global_bare_add (Global Bare Add v1, see below) exactly as
    before this route existed for every OTHER message shape.
    """
    destination, item_text = household_router.detect_explicit_add_destination(text)
    if destination is None:
        destination, item_text = household_router.detect_header_add_destination(text)
    if destination is None:
        return False
    origin = household_router.current_origin(chat_id)
    keyboard = household_router.origin_keyboard(origin)
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        inventory_items = get_inventory_items(household_id) if destination == "add_inventory" else []
        alias_map = get_household_alias_map(household_id)
        kind, payload = household_router.build_explicit_add_preview(
            destination, item_text, inventory_items, alias_map=alias_map,
        )
        return _handle_household_router_result(chat_id, kind, payload, household_id, user_db_id, origin, keyboard)
    except Exception:
        send_message(chat_id, "Не вдалося обробити команду. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)
        return True


def _finish_global_bare_add(chat_id, destination, validated_items, household_id, user_db_id, origin, keyboard):
    """Shared tail for both an immediately-resolved bare add (menu already
    implies a destination) and a resolved destination-clarification reply —
    never calls Gemini, only household_router.build_add_preview_from_items
    on already-validated items, then the same clarify/ok dispatch every
    other household-router-shaped result uses."""
    inventory_items = get_inventory_items(household_id) if destination == "add_inventory" else []
    kind, payload = household_router.build_add_preview_from_items(destination, validated_items, inventory_items)
    return _handle_household_router_result(chat_id, kind, payload, household_id, user_db_id, origin, keyboard)


def _try_global_bare_add(chat_id, user_id, display_name, text):
    """Global Bare Add v1 — "Додай молоко" with NO destination phrase at all
    (see household_router.detect_bare_add). Returns True if handled: either
    a shopping/inventory preview was built directly (active menu is
    "shopping"/"inventory"), or a "Куди додати ці позиції?" destination
    clarification was started (pending_add_destination_clarification).
    Returns False only when the text isn't a bare add at all (no "Додай"
    verb, or it carries an expense-amount marker like "за 10 zł" — see
    detect_bare_add's docstring), letting the caller fall through to the
    existing legacy gates/AI-chat exactly as before this route existed.
    """
    item_text = household_router.detect_bare_add(text)
    if item_text is None:
        return False
    origin = household_router.current_origin(chat_id)
    keyboard = household_router.origin_keyboard(origin)
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        alias_map = get_household_alias_map(household_id)
        kind, data = household_router.parse_bare_add_items(item_text, alias_map=alias_map)
        if kind == "unresolved":
            send_message(chat_id, household_router.format_unresolved_message(data), reply_markup=keyboard)
            return True
        if kind == "invalid":
            send_message(chat_id, household_router.format_invalid_message(data), reply_markup=keyboard)
            return True
        # kind == "items" — parsed and validated, destination not yet known.
        validated_items = data
        menu = active_list_context.get(chat_id)
        if menu == "shopping":
            return _finish_global_bare_add(
                chat_id, "add_shopping", validated_items, household_id, user_db_id, origin, keyboard,
            )
        if menu == "inventory":
            return _finish_global_bare_add(
                chat_id, "add_inventory", validated_items, household_id, user_db_id, origin, keyboard,
            )
        pending_add_destination_clarification[chat_id] = {
            "household_id": household_id,
            "user_db_id": user_db_id,
            "origin": origin,
            "validated_items": validated_items,
        }
        send_message(chat_id, ADD_DESTINATION_CLARIFICATION_QUESTION, reply_markup=ADD_DESTINATION_CLARIFICATION_KEYBOARD)
        return True
    except Exception:
        send_message(chat_id, "Не вдалося обробити команду. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)
        return True


def _continue_add_destination_clarification(chat_id, text):
    """Handle a plain-text reply while pending_add_destination_clarification
    is active for this chat (Global Bare Add v1). Never re-calls Gemini —
    works entirely off the validated item payloads captured when the
    clarification started. Always returns having fully handled the message
    (the caller must not fall through to anything else)."""
    destination = household_router.parse_add_destination_answer(text)
    if destination is None:
        send_message(chat_id, ADD_DESTINATION_CLARIFICATION_INVALID_MSG)
        return
    data = pending_add_destination_clarification.pop(chat_id)
    household_id = data["household_id"]
    user_db_id = data["user_db_id"]
    origin = data.get("origin", "global")
    keyboard = household_router.origin_keyboard(origin)
    try:
        _finish_global_bare_add(
            chat_id, destination, data["validated_items"], household_id, user_db_id, origin, keyboard,
        )
    except Exception:
        send_message(chat_id, "Не вдалося обробити команду. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


def _apply_global_household_confirm(chat_id):
    """"✅ Так, застосувати" button. Pops the pending combined preview BEFORE
    the database call, so a duplicate/late button press can never apply it
    twice; performs every operation in exactly one transaction."""
    if chat_id not in pending_global_household:
        send_message(chat_id, "Немає активної дії для підтвердження.")
        return
    data = pending_global_household.pop(chat_id)
    origin = data.get("origin", "global")
    keyboard = household_router.origin_keyboard(origin)
    consume_changes = data["consume_changes"]
    consume_updates = [
        {
            "item_id": c["item_id"], "quantity_value": c["new_value"],
            "quantity_unit": c["new_unit"], "quantity_text": c["new_display"],
        }
        for c in consume_changes if not c["will_remove"]
    ]
    consume_delete_ids = [c["item_id"] for c in consume_changes if c["will_remove"]]
    # Boundary normalization: a pending_global_household entry built (or
    # hand-seeded in a test) before Multi-Expense Batch v1 only ever carries
    # the legacy singular "new_expense" key — normalize that into a
    # one-element list here so everything below only ever deals with
    # new_expenses (the list), never a mix of both shapes.
    new_expenses = data.get("new_expenses")
    if new_expenses is None:
        legacy_new_expense = data.get("new_expense")
        new_expenses = [legacy_new_expense] if legacy_new_expense else []
    delete_expense_data = data["delete_expense"]
    try:
        apply_global_household_operations(
            data["household_id"], data["user_db_id"],
            add_shopping_items=data["add_shopping_items"],
            add_inventory_items=data["add_inventory_items"],
            consume_updates=consume_updates,
            consume_delete_ids=consume_delete_ids,
            inventory_targets=data["inventory_targets"],
            new_expenses=[
                {k: v for k, v in ne.items() if k != "category_was_defaulted"}
                for ne in new_expenses
            ],
            delete_expense_id=delete_expense_data["expense_id"] if delete_expense_data else None,
            delete_expense_snapshot=delete_expense_data["snapshot"] if delete_expense_data else None,
        )
        send_message(chat_id, "✅ Зміни застосовано.", reply_markup=keyboard)
    except StaleSnapshotError:
        send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=keyboard)
    except Exception:
        send_message(chat_id, "Не вдалося застосувати зміни. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


def _start_undo_flow(chat_id, user_id, display_name):
    """"↩️ Скасувати останню дію" button or one of the recognized natural
    phrasings. Looks up ONLY this user's own latest active Global Household
    Operation in their household (never a partner's) and shows a preview —
    no database write happens here."""
    try:
        household_id, user_db_id = get_household_and_user(user_id, display_name)
        action = get_latest_undoable_action(household_id, user_db_id)
    except Exception:
        send_message(chat_id, "Не вдалося перевірити дії для скасування. Спробуй ще раз трохи пізніше.")
        return
    if action is None:
        send_message(chat_id, action_history.NO_UNDOABLE_ACTION_MSG)
        return
    pending_undo_action[chat_id] = {
        "action_id": action["id"], "household_id": household_id, "user_db_id": user_db_id,
    }
    send_message(chat_id, action_history.format_undo_preview(action["summary"]), reply_markup=UNDO_PREVIEW_KEYBOARD)


def _apply_undo_confirm(chat_id):
    """"✅ Так, скасувати" button. Pops pending_undo_action BEFORE the
    database call, so a duplicate/late button press can never apply the
    inverse twice — apply_undo_action itself also re-verifies status='active'
    inside its own transaction as a second, authoritative guard."""
    if chat_id not in pending_undo_action:
        send_message(chat_id, "Немає активної дії для підтвердження.")
        return
    data = pending_undo_action.pop(chat_id)
    try:
        apply_undo_action(data["action_id"], data["household_id"], data["user_db_id"])
        send_message(chat_id, action_history.UNDO_APPLIED_MSG, reply_markup=MAIN_KEYBOARD)
    except StaleSnapshotError:
        send_message(chat_id, action_history.UNDO_STALE_MSG, reply_markup=MAIN_KEYBOARD)
    except Exception:
        send_message(chat_id, "Не вдалося скасувати дію. Спробуй ще раз трохи пізніше.", reply_markup=MAIN_KEYBOARD)


def _continue_inventory_quantity_clarification(chat_id, text):
    """Handle a plain-text reply while pending_inventory_quantity_
    clarification is active for this chat (Inventory Quantity Clarification
    v1). Never re-calls Gemini and never hand-builds a sentence to send back
    through the router — works entirely off the structured payload captured
    when the clarification started. Always returns having fully handled the
    message (the caller must not fall through to anything else)."""
    data = pending_inventory_quantity_clarification[chat_id]
    value, unit = _parse_explicit_clarification_quantity(text)

    # Bare-number fallback: a reply that's just a positive number (no unit)
    # can't be resolved to a definite (value, unit) pair without checking
    # inventory first (see below) — captured here as a plain Decimal
    # candidate, purely local, no DB call yet, so a genuinely invalid reply
    # ("багато", "щось незрозуміле") is rejected immediately below without
    # ever touching the database, exactly as before this fallback existed.
    bare_value = None
    if value is None or unit is None:
        bare_stripped = (text or "").strip().replace(",", ".")
        try:
            candidate = Decimal(bare_stripped)
        except InvalidOperation:
            candidate = None
        if candidate is not None and candidate > 0:
            bare_value = candidate

    if value is None and bare_value is None:
        send_message(chat_id, _GLOBAL_QUANTITY_CLARIFICATION_INVALID_MSG)
        return

    # Boundary normalization: this state may have been seeded with only the
    # legacy singular "new_expense" key (pre-Multi-Expense-Batch-v1 shape) —
    # normalize once here so every branch below only ever carries the list
    # forward, deriving the backward-compat singular key from it.
    new_expenses = data.get("new_expenses")
    if new_expenses is None:
        legacy_new_expense = data.get("new_expense")
        new_expenses = [legacy_new_expense] if legacy_new_expense else []
    legacy_new_expense = new_expenses[0] if len(new_expenses) == 1 else None

    household_id = data["household_id"]
    keyboard = household_router.origin_keyboard(data.get("origin", "global"))
    canonical_name = data["canonical_name"]
    category = data["category"]

    try:
        add_inventory_items = data["add_inventory_items"]
        match_index = next(
            (i for i, it in enumerate(add_inventory_items)
             if it.get("canonical_name") == canonical_name and it.get("category") == category),
            None,
        )
        if match_index is None:
            pending_inventory_quantity_clarification.pop(chat_id, None)
            send_message(chat_id, "Не зміг безпечно продовжити цю дію. Спробуй написати команду ще раз.", reply_markup=keyboard)
            return

        # Свіжий стан: re-fetch inventory now, never reuse the snapshot
        # shown when the clarification question was asked — a conflicting
        # row could have appeared (or disappeared) in the meantime. Fetched
        # once here and reused below by the representation guard call too.
        fresh_inventory_items = get_inventory_items(household_id)

        if value is None:
            # Bare-number reply — only safe to resolve when EXACTLY one
            # existing row matches this item (same "unambiguous single row"
            # condition format_global_quantity_clarification_message uses
            # for its own "Скільки додати?" wording): a bare "2" then
            # unambiguously means "2 <that row's unit>". With 0 or 2+
            # matching rows the unit is NOT unambiguous, so the reply is
            # still rejected, same as before this fallback existed.
            single_matches = find_inventory_representation_matches(fresh_inventory_items, canonical_name, category)
            if len(single_matches) != 1:
                send_message(chat_id, _GLOBAL_QUANTITY_CLARIFICATION_INVALID_MSG)
                return
            _, fallback_unit, _ = _effective_quantity(single_matches[0])
            value, unit = bare_value, fallback_unit

        resolved_item = dict(add_inventory_items[match_index])
        resolved_item["quantity_value"] = value
        resolved_item["quantity_unit"] = unit
        resolved_item["quantity_text"] = format_quantity_display(value, unit)
        resolved_item["quantity_inferred"] = False
        resolved_item.pop("_representation_outcome", None)
        resolved_item.pop("_representation_note", None)
        updated_items = list(add_inventory_items)
        updated_items[match_index] = resolved_item

        guard_kind, guard_result = household_router.apply_inventory_representation_guard(
            updated_items, fresh_inventory_items,
        )

        if guard_kind == "clarify":
            # Still ambiguous (possibly a DIFFERENT item this time) — never
            # build a preview or apply anything; keep clarifying with the
            # freshest representation list instead of a dead end.
            pending_inventory_quantity_clarification[chat_id] = {
                "household_id": household_id,
                "user_db_id": data["user_db_id"],
                "origin": data.get("origin", "global"),
                "item_name": guard_result["item_name"],
                "canonical_name": guard_result["canonical_name"],
                "category": guard_result["category"],
                "add_shopping_items": data["add_shopping_items"],
                "add_inventory_items": updated_items,
                "consume_changes": data["consume_changes"],
                "new_expenses": new_expenses,
                "new_expense": legacy_new_expense,
                "delete_expense": data["delete_expense"],
            }
            send_message(
                chat_id,
                format_global_quantity_clarification_message(guard_result["item_name"], guard_result["existing_items"]),
            )
            return

        # guard_kind == "ok" — resolved cleanly; build the normal combined
        # preview exactly like a fresh (non-clarification) Global Router
        # "ok" result would.
        pending_inventory_quantity_clarification.pop(chat_id, None)
        final_items, inventory_merge_targets = guard_result
        payload = {
            "add_shopping_items": data["add_shopping_items"],
            "add_inventory_items": final_items,
            "consume_changes": data["consume_changes"],
            "new_expenses": new_expenses,
            "new_expense": legacy_new_expense,
            "delete_expense": data["delete_expense"],
            "inventory_merge_targets": inventory_merge_targets,
        }
        inventory_targets = _snapshot_targets(payload["consume_changes"]) + payload["inventory_merge_targets"]
        pending_global_household[chat_id] = {
            "add_shopping_items": payload["add_shopping_items"],
            "add_inventory_items": payload["add_inventory_items"],
            "consume_changes": payload["consume_changes"],
            "inventory_targets": inventory_targets,
            "new_expenses": payload["new_expenses"],
            "new_expense": payload["new_expense"],
            "delete_expense": payload["delete_expense"],
            "household_id": household_id,
            "user_db_id": data["user_db_id"],
            "origin": data.get("origin", "global"),
        }
        send_message(chat_id, household_router.format_preview(payload), reply_markup=GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD)
    except Exception:
        pending_inventory_quantity_clarification.pop(chat_id, None)
        send_message(chat_id, "Не вдалося обробити команду. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


def _send_representation_v2_choice_message(chat_id, conflict):
    """Sends Flow A's or Flow B's first question (see household_router.py's
    "INVENTORY REPRESENTATION CLARIFICATION V2" section), matching the
    choice keyboard to the conflict's kind."""
    if conflict["kind"] == "consume":
        send_message(
            chat_id, household_router.format_representation_v2_consume_choice_message(conflict),
            reply_markup=REPRESENTATION_V2_CONSUME_CHOICE_KEYBOARD,
        )
    else:
        send_message(
            chat_id, household_router.format_representation_v2_add_choice_message(conflict),
            reply_markup=REPRESENTATION_V2_ADD_CHOICE_KEYBOARD,
        )


def _advance_representation_v2_queue(chat_id, data, resolution, extra_consume_change=None):
    """Shared tail for every Inventory Representation Clarification V2
    choice: records the resolution (if any) and the extra consume_change
    (if any — tagged "_from_representation_resolution" so format_preview's
    normal consume_changes loop skips it, since it's already rendered via
    inventory_representation_resolutions), then either asks about the NEXT
    queued conflict or — once the queue is empty — re-validates every
    resolved target against a FRESH inventory snapshot and builds the final
    combined Global preview. Never re-calls Gemini."""
    origin = data.get("origin", "global")
    keyboard = household_router.origin_keyboard(origin)

    if resolution is not None:
        data["representation_resolutions"] = data["representation_resolutions"] + [resolution]
    if extra_consume_change is not None:
        extra_consume_change = dict(extra_consume_change)
        extra_consume_change["_from_representation_resolution"] = True
        data["consume_changes"] = data["consume_changes"] + [extra_consume_change]

    queue = data["queue"]
    if queue:
        data["conflict"] = queue[0]
        data["queue"] = queue[1:]
        data["stage"] = "choice"
        pending_inventory_representation_clarification[chat_id] = data
        _send_representation_v2_choice_message(chat_id, data["conflict"])
        return

    household_id = data["household_id"]
    try:
        fresh_inventory_items = get_inventory_items(household_id)
    except Exception:
        pending_inventory_representation_clarification.pop(chat_id, None)
        send_message(chat_id, "Не вдалося обробити команду. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)
        return

    if not household_router.representation_v2_targets_still_fresh(
        data["representation_resolutions"], fresh_inventory_items,
    ):
        pending_inventory_representation_clarification.pop(chat_id, None)
        send_message(chat_id, _REPRESENTATION_V2_STALE_MSG, reply_markup=keyboard)
        return

    pending_inventory_representation_clarification.pop(chat_id, None)
    payload = {
        "add_shopping_items": data["add_shopping_items"],
        "add_inventory_items": data["add_inventory_items"],
        "consume_changes": data["consume_changes"],
        "new_expenses": data["new_expenses"],
        "new_expense": data["new_expense"],
        "delete_expense": data["delete_expense"],
        "inventory_representation_resolutions": data["representation_resolutions"],
    }
    inventory_targets = _snapshot_targets(payload["consume_changes"]) + data["inventory_merge_targets"]
    pending_global_household[chat_id] = {
        "add_shopping_items": payload["add_shopping_items"],
        "add_inventory_items": payload["add_inventory_items"],
        "consume_changes": payload["consume_changes"],
        "inventory_targets": inventory_targets,
        "new_expenses": payload["new_expenses"],
        "new_expense": payload["new_expense"],
        "delete_expense": payload["delete_expense"],
        "inventory_representation_resolutions": payload["inventory_representation_resolutions"],
        "household_id": household_id,
        "user_db_id": data["user_db_id"],
        "origin": origin,
    }
    send_message(chat_id, household_router.format_preview(payload), reply_markup=GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD)


def _continue_inventory_representation_clarification(chat_id, text):
    """Handle a plain-text reply while pending_inventory_representation_
    clarification is active for this chat (Inventory Representation
    Clarification V2). Never re-calls Gemini and never hand-builds a
    sentence to send back through the router — works entirely off the
    structured payload captured when the clarification started. Always
    returns having fully handled the message (the caller must not fall
    through to anything else)."""
    data = pending_inventory_representation_clarification[chat_id]
    origin = data.get("origin", "global")
    keyboard = household_router.origin_keyboard(origin)
    conflict = data["conflict"]

    try:
        if data["stage"] == "awaiting_total":
            value, unit = _parse_representation_v2_total_quantity_reply(text, conflict["requested_unit"])
            if value is None or unit is None:
                send_message(chat_id, _REPRESENTATION_V2_TOTAL_QUANTITY_INVALID_MSG)
                return
            kind, remaining, remaining_unit = household_router.validate_representation_v2_total_quantity(
                conflict, value, unit,
            )
            if kind != "ok":
                send_message(chat_id, _REPRESENTATION_V2_TOTAL_QUANTITY_INVALID_MSG)
                return
            resolution, consume_change = household_router.resolve_representation_v2_consume_relabel(
                conflict, value, unit, remaining, remaining_unit,
            )
            _advance_representation_v2_queue(chat_id, data, resolution, extra_consume_change=consume_change)
            return

        # stage == "choice"
        if conflict["kind"] == "consume":
            choice = household_router.parse_representation_v2_consume_choice(text)
            if choice is None:
                send_message(chat_id, REPRESENTATION_V2_PREVIEW_GUARD_MSG)
                return
            if choice == "part_of_existing":
                data["stage"] = "awaiting_total"
                pending_inventory_representation_clarification[chat_id] = data
                send_message(chat_id, household_router.format_representation_v2_total_quantity_question(conflict))
                return
            # choice == "separate_product"
            resolution = household_router.resolve_representation_v2_consume_skip(conflict)
            _advance_representation_v2_queue(chat_id, data, resolution)
            return

        # conflict["kind"] == "add"
        choice = household_router.parse_representation_v2_add_choice(text)
        if choice is None:
            send_message(chat_id, REPRESENTATION_V2_PREVIEW_GUARD_MSG)
            return
        if choice == "separate_package":
            item = household_router.resolve_representation_v2_add_separate(conflict)
            data["add_inventory_items"] = data["add_inventory_items"] + [item]
            _advance_representation_v2_queue(chat_id, data, resolution=None)
            return
        # choice == "relabel_existing"
        resolution, consume_change = household_router.resolve_representation_v2_add_relabel(conflict)
        _advance_representation_v2_queue(chat_id, data, resolution, extra_consume_change=consume_change)
    except Exception:
        pending_inventory_representation_clarification.pop(chat_id, None)
        send_message(chat_id, "Не вдалося обробити команду. Спробуй ще раз трохи пізніше.", reply_markup=keyboard)


# Legacy Shopping Flow V1 — legacy_shopping_flow.py owns shopping_mode/
# pending_batch/pending_mark_batch/pending_delete_batch and the shopping-only
# handlers; it has no import of bot.py, so everything it needs from here is
# passed once via this injected dependency container.
#
# Every function-valued field below is a thin `lambda *a, **kw: name(*a, **kw)`
# forward rather than a direct reference. That's deliberate, not decoration:
# these lambdas live in bot.py's module namespace, so `name` inside each one
# is resolved fresh against bot.py's globals on every call — exactly like a
# plain top-level call in bot.py already worked before this extraction.
# Passing the bare function object instead would freeze in the pre-patch
# reference at import time, silently breaking every existing test that does
# `patch.object(bot, "send_message"/"call_gemini"/"get_household_and_user"/...)`.
_shopping_deps = legacy_shopping_flow.ShoppingFlowDeps(
    send_message=lambda *a, **kw: send_message(*a, **kw),
    get_household_and_user=lambda *a, **kw: get_household_and_user(*a, **kw),
    get_household_alias_map=lambda *a, **kw: get_household_alias_map(*a, **kw),
    get_active_shopping_items=lambda *a, **kw: get_active_shopping_items(*a, **kw),
    save_list_context=lambda *a, **kw: save_list_context(*a, **kw),
    normalize_item_quantity=lambda *a, **kw: normalize_item_quantity(*a, **kw),
    parse_item_text=lambda *a, **kw: parse_item_text(*a, **kw),
    call_gemini=lambda *a, **kw: call_gemini(*a, **kw),
    ask_gemini_for_selection=lambda *a, **kw: _ask_gemini_for_selection(*a, **kw),
    ask_gemini_preview_edit_router=lambda *a, **kw: _ask_gemini_preview_edit_router(*a, **kw),
    validate_preview_updates=lambda *a, **kw: _validate_preview_updates(*a, **kw),
    apply_preview_updates=lambda *a, **kw: _apply_preview_updates(*a, **kw),
    auto_merge_in_place=lambda *a, **kw: _auto_merge_in_place(*a, **kw),
    format_shopping_list=lambda *a, **kw: format_shopping_list(*a, **kw),
    format_batch_preview=lambda *a, **kw: format_batch_preview(*a, **kw),
    format_grouped_list=lambda *a, **kw: format_grouped_list(*a, **kw),
    format_unresolved_fragments_message=lambda *a, **kw: _format_unresolved_fragments_message(*a, **kw),
    clear_shopping_state=lambda *a, **kw: clear_shopping_state(*a, **kw),
    clear_inventory_state=lambda *a, **kw: clear_inventory_state(*a, **kw),
    active_list_context=active_list_context,
    saved_list_context=saved_list_context,
    waiting_for_ingredients=waiting_for_ingredients,
    shopping_keyboard=SHOPPING_KEYBOARD,
    add_preview_keyboard=ADD_PREVIEW_KEYBOARD,
    mark_preview_keyboard=MARK_PREVIEW_KEYBOARD,
    delete_preview_keyboard=DELETE_PREVIEW_KEYBOARD,
    shopping_parse_prompt=SHOPPING_PARSE_PROMPT,
    default_category=DEFAULT_CATEGORY,
    valid_categories=VALID_CATEGORIES,
    db_error_msg=DB_ERROR_MSG,
    selection_error_msg=SELECTION_ERROR_MSG,
)

# Legacy Inventory Flow V1 — legacy_inventory_flow.py owns inventory_mode/
# pending_inventory_batch/pending_remove_batch and the inventory-only
# handlers; it has no import of bot.py, so everything it needs from here is
# passed once via this injected dependency container. Same lambda-forward
# reasoning as _shopping_deps above — every function-valued field resolves
# the name fresh against bot.py's globals on every call, so
# `patch.object(bot, "...")` in existing tests keeps working, including
# patch.object(bot, "parse_inventory_list_with_gemini", ...) at the webhook
# level (see the wrapper of that name above).
_inventory_deps = legacy_inventory_flow.InventoryFlowDeps(
    send_message=lambda *a, **kw: send_message(*a, **kw),
    call_gemini=lambda *a, **kw: call_gemini(*a, **kw),
    get_household_and_user=lambda *a, **kw: get_household_and_user(*a, **kw),
    get_inventory_items=lambda *a, **kw: get_inventory_items(*a, **kw),
    get_household_alias_map=lambda *a, **kw: get_household_alias_map(*a, **kw),
    save_list_context=lambda *a, **kw: save_list_context(*a, **kw),
    normalize_item_quantity=lambda *a, **kw: normalize_item_quantity(*a, **kw),
    canonicalize_name=lambda *a, **kw: canonicalize_name(*a, **kw),
    parse_inventory_list_with_gemini=lambda *a, **kw: parse_inventory_list_with_gemini(*a, **kw),
    resolve_inventory_representation=lambda *a, **kw: resolve_inventory_representation(*a, **kw),
    format_representation_clarify_message=lambda *a, **kw: format_representation_clarify_message(*a, **kw),
    format_representation_separate_warning=lambda *a, **kw: format_representation_separate_warning(*a, **kw),
    format_representation_merge_quantity_fragment=lambda *a, **kw: format_representation_merge_quantity_fragment(*a, **kw),
    merge_quantity_values=lambda *a, **kw: merge_quantity_values(*a, **kw),
    format_quantity_display=lambda *a, **kw: format_quantity_display(*a, **kw),
    ask_gemini_for_selection=lambda *a, **kw: _ask_gemini_for_selection(*a, **kw),
    ask_gemini_preview_edit_router=lambda *a, **kw: _ask_gemini_preview_edit_router(*a, **kw),
    validate_preview_updates=lambda *a, **kw: _validate_preview_updates(*a, **kw),
    apply_preview_updates=lambda *a, **kw: _apply_preview_updates(*a, **kw),
    auto_merge_in_place=lambda *a, **kw: _auto_merge_in_place(*a, **kw),
    format_grouped_list=lambda *a, **kw: format_grouped_list(*a, **kw),
    format_inventory_list=lambda *a, **kw: format_inventory_list(*a, **kw),
    format_inventory_preview=lambda *a, **kw: format_inventory_preview(*a, **kw),
    format_unresolved_fragments_message=lambda *a, **kw: _format_unresolved_fragments_message(*a, **kw),
    resolve_numbered_inventory_delete_selection=lambda *a, **kw: _resolve_numbered_inventory_delete_selection(*a, **kw),
    format_numbered_delete_mismatch_message=lambda *a, **kw: _format_numbered_delete_mismatch_message(*a, **kw),
    clear_shopping_state=lambda *a, **kw: clear_shopping_state(*a, **kw),
    clear_inventory_state=lambda *a, **kw: clear_inventory_state(*a, **kw),
    active_list_context=active_list_context,
    saved_list_context=saved_list_context,
    waiting_for_ingredients=waiting_for_ingredients,
    inventory_keyboard=INVENTORY_KEYBOARD,
    add_inventory_preview_keyboard=ADD_INVENTORY_PREVIEW_KEYBOARD,
    remove_preview_keyboard=REMOVE_PREVIEW_KEYBOARD,
    inventory_parse_prompt=INVENTORY_PARSE_PROMPT,
    default_category=DEFAULT_CATEGORY,
    valid_categories=VALID_CATEGORIES,
    inventory_error_msg=INVENTORY_ERROR_MSG,
    selection_error_msg=SELECTION_ERROR_MSG,
)

# Interaction State Facade V1 — interaction_state.py owns the cleanup/guard
# logic (clear_shopping_state/clear_inventory_state/clear_interaction_state,
# the gate-blocking pending-state groups, _has_active_expense_preview,
# _should_restore_persisted_context) but NOT any of the dicts themselves —
# every dict field below is the SAME object its owner module (bot.py,
# legacy_shopping_flow.py, legacy_inventory_flow.py, expenses.py) already
# holds. clear_expense_state/clear_list_context are lambda-forwards for the
# same patchability reason as every other callback in this file.
_interaction_state_deps = interaction_state.InteractionStateDeps(
    shopping_mode=shopping_mode,
    pending_batch=pending_batch,
    pending_mark_batch=pending_mark_batch,
    pending_delete_batch=pending_delete_batch,
    inventory_mode=inventory_mode,
    pending_inventory_batch=pending_inventory_batch,
    pending_remove_batch=pending_remove_batch,
    pending_expense=pending_expense,
    pending_expense_delete=pending_expense_delete,
    expense_delete_selection=expense_delete_selection,
    pending_merge=pending_merge,
    pending_saved_edit=pending_saved_edit,
    pending_quick_purchase=pending_quick_purchase,
    pending_inventory_consumption=pending_inventory_consumption,
    pending_compound_inventory=pending_compound_inventory,
    pending_inventory_reconciliation=pending_inventory_reconciliation,
    pending_inventory_reconciliation_clarify=pending_inventory_reconciliation_clarify,
    pending_alias_action=pending_alias_action,
    pending_global_household=pending_global_household,
    pending_inventory_quantity_clarification=pending_inventory_quantity_clarification,
    pending_inventory_representation_clarification=pending_inventory_representation_clarification,
    pending_add_destination_clarification=pending_add_destination_clarification,
    pending_cleanup_admin=pending_cleanup_admin,
    pending_cleanup_admin_disambiguation=pending_cleanup_admin_disambiguation,
    pending_destructive_guard=pending_destructive_guard,
    pending_undo_action=pending_undo_action,
    active_list_context=active_list_context,
    saved_list_context=saved_list_context,
    waiting_for_ingredients=waiting_for_ingredients,
    clear_expense_state=lambda *a, **kw: clear_expense_state(*a, **kw),
    clear_list_context=lambda *a, **kw: clear_list_context(*a, **kw),
)

# Dispatcher V2A — message_dispatcher.py owns the ordered priority of the ten
# highest-priority pending-state/clarification/undo routes (old Phase C
# routes 6-15); state dicts stay owned exactly where they already live
# (legacy_shopping_flow.py/legacy_inventory_flow.py/expenses.py/bot.py —
# referenced directly here, never copied). Continuation callbacks are
# lambda-forwards for the same patch.object(bot, ...) reason as every other
# callback in this file.
_pending_route_deps = message_dispatcher.PendingRouteDeps(
    pending_batch=pending_batch,
    pending_inventory_batch=pending_inventory_batch,
    pending_inventory_reconciliation_clarify=pending_inventory_reconciliation_clarify,
    expense_delete_selection=expense_delete_selection,
    pending_inventory_quantity_clarification=pending_inventory_quantity_clarification,
    pending_inventory_representation_clarification=pending_inventory_representation_clarification,
    pending_global_household=pending_global_household,
    pending_add_destination_clarification=pending_add_destination_clarification,
    pending_undo_action=pending_undo_action,
    has_active_expense_preview=lambda *a, **kw: _has_active_expense_preview(*a, **kw),
    handle_expense_delete_selection_text=lambda *a, **kw: _handle_expense_delete_selection_text(*a, **kw),
    continue_inventory_reconciliation_clarification=lambda *a, **kw: _continue_inventory_reconciliation_clarification(*a, **kw),
    continue_inventory_quantity_clarification=lambda *a, **kw: _continue_inventory_quantity_clarification(*a, **kw),
    continue_inventory_representation_clarification=lambda *a, **kw: _continue_inventory_representation_clarification(*a, **kw),
    continue_add_destination_clarification=lambda *a, **kw: _continue_add_destination_clarification(*a, **kw),
    start_undo_flow=lambda *a, **kw: _start_undo_flow(*a, **kw),
    expense_preview_guard_msg=EXPENSE_PREVIEW_GUARD_MSG,
    global_household_preview_guard_msg=GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
    has_active_pending_operation=lambda *a, **kw: _has_active_pending_clarification_or_preview(*a, **kw),
    cancel_active_pending_operation=lambda *a, **kw: _cancel_active_pending_operation(*a, **kw),
    pending_cleanup_admin_disambiguation=pending_cleanup_admin_disambiguation,
    continue_cleanup_admin_disambiguation=lambda *a, **kw: _continue_cleanup_admin_disambiguation(*a, **kw),
    pending_destructive_guard=pending_destructive_guard,
    continue_destructive_guard=lambda *a, **kw: _continue_destructive_guard(*a, **kw),
)

# =========================
# DISPATCHER V2B — COMMAND/CONTEXT ROUTE WRAPPERS (old Phase C routes 16-26)
# Each function below is a thin, self-contained wrapper around already-
# existing business logic (household router, expense/alias gates, the
# saved-list router) so message_dispatcher.py can call it without ever
# importing bot.py, touching the database, or calling Gemini itself. Every
# one returns True if it fully handled the message, False if it does not
# apply (dispatcher tries the next route) — except the two active-context
# routes (aliases/expenses), which return None when the context itself
# doesn't match (try next route) vs True/False when it does (stop here
# regardless of the handler's own outcome, exactly like the old elif chain
# where matching the context already claimed the branch).
# =========================
def _route_ambiguous_add(chat_id, user_id, display_name, text):
    if not _is_ambiguous_add_with_price(text):
        return False
    _handle_ambiguous_add_with_price(chat_id)
    return True


def _route_global_explicit_add(chat_id, user_id, display_name, text):
    if _has_blocking_pending_state_for_reports(chat_id):
        return False
    return _try_global_explicit_add(chat_id, user_id, display_name, text)


def _route_global_bare_add(chat_id, user_id, display_name, text):
    if _has_blocking_pending_state_for_reports(chat_id):
        return False
    return _try_global_bare_add(chat_id, user_id, display_name, text)


def _route_global_household(chat_id, user_id, display_name, text):
    if _has_blocking_pending_state_for_reports(chat_id):
        return False
    if not household_router.gate(text):
        return False
    return _try_global_household_router(chat_id, user_id, display_name, text)


def _route_expense_report(chat_id, user_id, display_name, text):
    if _has_blocking_pending_state_for_reports(chat_id):
        return False
    kind = _expense_report_gate(text)
    if not kind:
        return False
    _handle_expense_report_command(chat_id, user_id, display_name, kind)
    return True


def _route_expense_delete_command(chat_id, user_id, display_name, text):
    if _has_blocking_pending_state_for_expense_delete(chat_id):
        return False
    if not _expense_delete_command_gate(text):
        return False
    if text.strip() == "🗑️ Видалити витрату":
        _handle_expense_delete_button(chat_id, user_id, display_name)
    else:
        _handle_expense_delete_global_command(chat_id, user_id, display_name, text)
    return True


def _route_active_aliases_context(chat_id, user_id, display_name, text):
    """Returns None if active_list_context isn't "aliases" (try next route);
    otherwise returns True/False (stop here either way — matching the
    context already claimed this message in the old elif chain, same as
    every dict-membership route in Dispatcher V2A)."""
    if active_list_context.get(chat_id) != "aliases":
        return None
    return _handle_alias_command(chat_id, user_id, display_name, text)


def _route_global_alias_command(chat_id, user_id, display_name, text):
    if _has_blocking_pending_state(chat_id):
        return False
    if not _alias_command_gate(text):
        return False
    _handle_alias_command(chat_id, user_id, display_name, text)
    return True


def _route_active_expenses_context(chat_id, user_id, display_name, text):
    """Same None/True/False contract as _route_active_aliases_context."""
    if active_list_context.get(chat_id) != "expenses":
        return None
    return _handle_expense_command(chat_id, user_id, display_name, text)


def _route_global_expense_command(chat_id, user_id, display_name, text):
    if _has_blocking_pending_state_for_expense(chat_id):
        return False
    if not _expense_command_gate(text):
        return False
    _handle_expense_command(chat_id, user_id, display_name, text)
    return True


def _route_saved_list_router(chat_id, user_id, display_name, text):
    """Old Phase C route 26 (saved-list router), extracted verbatim from
    webhook()'s final `else` branch. Returns True if this route fully
    handled the message, False if it did not apply or Gemini's intent was
    "none" (message_dispatcher.py's dispatch() then returns CONTINUE, and
    webhook() falls through to the unchanged Phase D)."""
    ctx = saved_list_context.get(chat_id)
    if _should_restore_persisted_context(chat_id):
        # Try restoring the last opened list from PostgreSQL — survives
        # restart/deploy, TTL 24h.
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            persisted = get_list_context(chat_id, household_id)
            if persisted in ("shopping_saved", "inventory_saved"):
                ctx = persisted
                saved_list_context[chat_id] = ctx
        except Exception:
            pass
    if ctx in ("shopping_saved", "inventory_saved"):
        try:
            household_id, user_db_id = get_household_and_user(user_id, display_name)
            alias_map = get_household_alias_map(household_id)
            list_items = (
                get_active_shopping_items(household_id)
                if ctx == "shopping_saved"
                else get_inventory_items(household_id)
            )
            if list_items:
                router_result = _ask_gemini_saved_list_router(text, list_items, ctx)
                intent = router_result["intent"]
                if intent == "edit_saved_items":
                    valid_updates = _validate_saved_updates(router_result["updates"], list_items)
                    if valid_updates and _saved_edit_text_has_unsafe_package_conversion(text, valid_updates):
                        send_message(chat_id, _format_package_conversion_blocked_message(text))
                    elif valid_updates:
                        real_updates, noop_updates = _split_noop_saved_updates(valid_updates, list_items, alias_map)
                        if not real_updates:
                            send_message(chat_id, _format_noop_saved_edit_message(noop_updates, list_items, ctx))
                        else:
                            pending_saved_edit[chat_id] = {
                                "items_snapshot": list_items,
                                "validated_updates": real_updates,
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                                "context_type": ctx,
                            }
                            preview = _format_saved_edit_preview(list_items, real_updates, ctx)
                            if noop_updates:
                                note = f"Без змін: {len(noop_updates)} {_pluralize_positions_uk(len(noop_updates))}."
                                preview = note + "\n\n" + preview
                            send_message(chat_id, preview, reply_markup=SAVED_EDIT_PREVIEW_KEYBOARD)
                    else:
                        send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
                    return True
                elif intent == "merge_duplicates":
                    validated_groups = _compute_saved_merge_groups(router_result["merge_groups"], list_items)
                    if validated_groups:
                        pending_merge[chat_id] = {
                            "groups": validated_groups,
                            "targets": _merge_snapshot_targets(validated_groups),
                            "household_id": household_id,
                            "user_db_id": user_db_id,
                            "list_type": ctx,
                        }
                        send_message(chat_id, _format_merge_preview(validated_groups), reply_markup=MERGE_PREVIEW_KEYBOARD)
                    else:
                        send_message(chat_id, "Не знайшов безпечних дублікатів для об'єднання.")
                    return True
                elif intent == "start_action":
                    blocked, fragments = _check_unresolved_fragments(router_result)
                    if blocked:
                        if fragments:
                            send_message(chat_id, _format_unresolved_fragments_message(fragments))
                        else:
                            send_message(chat_id, "Не зміг безпечно зрозуміти дію. Спробуй написати інакше.")
                    else:
                        selected = _validate_start_action(
                            router_result.get("action"), router_result.get("selected_numbers"), ctx, list_items
                        )
                        if selected is not None:
                            saved_list_context.pop(chat_id, None)
                            action = router_result.get("action")
                            if action == "mark_bought":
                                legacy_shopping_flow._show_mark_preview(_shopping_deps, chat_id, selected, household_id, user_db_id)
                            elif action == "delete_shopping":
                                legacy_shopping_flow._show_delete_preview(_shopping_deps, chat_id, selected, household_id, user_db_id)
                            elif action == "remove_inventory":
                                legacy_inventory_flow._show_remove_preview(_inventory_deps, chat_id, selected, household_id, user_db_id)
                        else:
                            send_message(chat_id, "Не зміг безпечно зрозуміти дію. Спробуй написати інакше.")
                    return True
                elif intent == "consume_inventory_quantity" and ctx == "inventory_saved":
                    blocked, fragments = _check_unresolved_fragments(router_result)
                    if blocked:
                        if fragments:
                            send_message(chat_id, _format_unresolved_fragments_message(fragments))
                        else:
                            send_message(
                                chat_id,
                                "Не можу безпечно визначити, яку саме кількість потрібно списати. Уточни, будь ласка.",
                            )
                    else:
                        kind, payload = _validate_consumptions(router_result.get("consumptions"), list_items)
                        if kind == "ok":
                            pending_inventory_consumption[chat_id] = {
                                "resolved": payload,
                                "household_id": household_id,
                                "user_db_id": user_db_id,
                            }
                            send_message(
                                chat_id, _format_consumption_preview(payload), reply_markup=SAVED_EDIT_PREVIEW_KEYBOARD
                            )
                        elif kind == "missing_quantity":
                            send_message(
                                chat_id,
                                f"Не можу безпечно відняти частину, бо для «{payload}» не вказана точна кількість. "
                                "Спочатку відредагуй кількість товару.",
                            )
                        elif kind == "insufficient":
                            name, available, requested = payload
                            send_message(
                                chat_id, f"У запасах є лише {available}, а ти вказав {requested}. Уточни кількість."
                            )
                        else:
                            send_message(
                                chat_id,
                                "Не можу безпечно визначити, яку саме кількість потрібно списати. Уточни, будь ласка.",
                            )
                    return True
                elif intent == "compound_inventory_operations" and ctx == "inventory_saved":
                    kind, payload = _validate_compound_operations(
                        router_result.get("operations"), router_result.get("unresolved_fragments"), list_items,
                        alias_map=alias_map,
                    )
                    if kind == "ok":
                        pending_compound_inventory[chat_id] = {
                            "inventory_changes": payload["inventory_changes"],
                            "add_to_shopping": payload["add_to_shopping"],
                            "household_id": household_id,
                            "user_db_id": user_db_id,
                        }
                        send_message(
                            chat_id, _format_compound_preview(payload), reply_markup=COMPOUND_PREVIEW_KEYBOARD
                        )
                    elif kind == "unresolved":
                        lines = [
                            "Я зрозумів частину повідомлення, але не хочу мовчки пропустити решту.",
                            "",
                            "Не зміг зрозуміти:",
                        ]
                        for frag in payload:
                            lines.append(f"• «{frag}»")
                        lines.append("")
                        lines.append("Спробуй уточнити все повідомлення.")
                        send_message(chat_id, "\n".join(lines))
                    else:
                        lines = [
                            "Не зміг безпечно обробити всі зміни. Нічого не було змінено.",
                            "",
                            "Не зрозумів або не можу виконати:",
                        ]
                        for reason in payload:
                            lines.append(f"• {reason}")
                        send_message(chat_id, "\n".join(lines))
                    return True
                elif intent == "reconcile_inventory_snapshot" and ctx == "inventory_saved":
                    kind, payload = _validate_reconcile_snapshot(
                        router_result.get("items"), router_result.get("unresolved_fragments"), list_items,
                        alias_map=alias_map,
                    )
                    if kind == "ok":
                        pending_inventory_reconciliation[chat_id] = {
                            "updates": payload["updates"],
                            "additions": payload["additions"],
                            "deletes": payload["deletes"],
                            "household_id": household_id,
                            "user_db_id": user_db_id,
                        }
                        send_message(
                            chat_id, _format_reconciliation_preview(payload), reply_markup=RECONCILIATION_PREVIEW_KEYBOARD
                        )
                    elif kind == "ambiguous_unit_group":
                        pending_inventory_reconciliation_clarify[chat_id] = {
                            "ambiguous_group": payload["ambiguous_group"],
                            "rest": payload["rest"],
                            "household_id": household_id,
                            "user_db_id": user_db_id,
                        }
                        send_message(chat_id, _format_reconciliation_unit_clarify_question(payload["ambiguous_group"]))
                    elif kind == "unresolved":
                        lines = [
                            "Я зрозумів частину списку, але не хочу мовчки пропустити решту.",
                            "",
                            "Не зміг зрозуміти:",
                        ]
                        for frag in payload:
                            lines.append(f"• «{frag}»")
                        lines.append("")
                        lines.append("Спробуй надіслати весь список запасів ще раз.")
                        send_message(chat_id, "\n".join(lines))
                    else:
                        lines = [
                            "Не зміг безпечно звірити запаси. Нічого не було змінено.",
                            "",
                            "Причина:",
                        ]
                        for reason in payload:
                            lines.append(f"• {reason}")
                        send_message(chat_id, "\n".join(lines))
                    return True
                # intent == "none": fall through to AI chat
            elif ctx == "shopping_saved":
                router_result = _ask_gemini_saved_list_router(text, [], ctx)
                if router_result["intent"] == "quick_add_to_inventory":
                    parsed = _validate_quick_add_items(router_result.get("items"), alias_map=alias_map)
                    if parsed is not None:
                        quick_items, ignored_names = parsed
                        saved_list_context.pop(chat_id, None)
                        pending_quick_purchase[chat_id] = {
                            "items": quick_items,
                            "ignored_items": ignored_names,
                            "household_id": household_id,
                            "user_db_id": user_db_id,
                        }
                        preview = _format_quick_purchase_preview(quick_items, ignored_names)
                        send_message(chat_id, preview, reply_markup=QUICK_PURCHASE_KEYBOARD)
                    else:
                        send_message(chat_id, "Не зміг безпечно зрозуміти покупку. Спробуй написати інакше.")
                    return True
                # intent == "none": fall through to AI chat
        except Exception:
            pass
    return False


# Household-Action-Line Fallback Guard v1 — a last-resort, LOCAL (no Gemini)
# safety net checked right before general AI-chat. Every deterministic
# add-flow route (ambiguous-add-price guard, Global Explicit/Header/Bare
# Add, household_router.gate()) has already had first crack at the message
# by this point in Phase D, so an "Додати"/"Додай"/"Додайте" line has
# ALWAYS already been fully handled upstream — this only ever fires for
# verb shapes V1.2 has no dedicated deterministic route for yet
# ("Прибрати"/"Використати" bot-preview-style lines), so THOSE never fall
# through to the general AI-chat reply, which would otherwise (correctly,
# but confusingly for the user) explain it has no database access.
_UNROUTED_HOUSEHOLD_ACTION_LINE_RE = re.compile(
    r"^[•\-]?\s*(?:додай(?:те)?|додати|прибери(?:ть)?|прибрати|використай(?:те)?|використати)\s+\S",
    re.IGNORECASE,
)


# Destructive Bulk Household Request Guard v1 — a last-resort, LOCAL (no
# Gemini) safety net checked right before the household-action-line guard
# above (and, transitively, before general AI-chat). A bare "Видали все"/
# "Очисти запаси" names no specific product and no existing deterministic
# route (inventory admin's parse_inventory_delete_request explicitly excludes
# a bulk "все"/"всі" pronoun, see _DELETE_BULK_PRONOUN_RE; the aliases
# submenu's own numbered bulk-delete only ever fires while that submenu is
# open, via active_aliases_context, checked earlier in the same dispatch
# chain) claims it — without this guard it would silently reach Gemini
# general chat, which (correctly, per SYSTEM_PROMPT) explains it has no
# direct database access, a confusing non-answer for what is clearly meant
# as a destructive household command. Anchored to the WHOLE message (after
# stripping) on purpose — never a substring match inside a longer sentence —
# so it only ever catches exactly this bare-imperative shape.
_DESTRUCTIVE_BULK_HOUSEHOLD_RE = re.compile(
    r"^\s*(?:видал\w*|приб\w*|очист\w*|стерт\w*)\s+(?:все|всі|усе|усі)"
    r"(?:\s+(?:запас\w*|покупк\w*))?\s*[.!?]*\s*$"
    r"|^\s*очист\w*\s+(?:запас\w*|покупк\w*)\s*[.!?]*\s*$",
    re.IGNORECASE,
)

DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG = (
    "Що саме очистити: покупки чи запаси?\n"
    "Таку дію можна зробити тільки через окремий preview і підтвердження."
)

# Destructive Bulk Household Request Guard v1.4 — a destination reply to the
# guard's own "покупки чи запаси?" question (pending_destructive_guard) must
# never fall into the ordinary shopping/inventory read-list route (that's
# what the live bug looked like: "Видали все" -> "покупки" -> shopping list
# shown, as if it were "Що треба купити?"). Deliberately a small, EXACT
# (whole-message) match — never a substring inside a longer reply — so an
# unrelated follow-up is never misread as answering this question.
_DESTRUCTIVE_GUARD_SHOPPING_RE = re.compile(r"^\s*(?:покупки|покупок|список\s+покупок)\s*[.!?]*\s*$", re.IGNORECASE)
_DESTRUCTIVE_GUARD_INVENTORY_RE = re.compile(r"^\s*(?:запаси|запасів|інвентар\w*)\s*[.!?]*\s*$", re.IGNORECASE)

DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG = (
    "Масове очищення через текст ще не реалізоване. "
    "Можеш видалити конкретні позиції або скористатися меню, де є preview і підтвердження."
)
DESTRUCTIVE_GUARD_CANCELLED_MSG = "Очищення скасовано. Я нічого не змінював."


def _looks_like_destructive_bulk_household_request(text):
    """True if `text` (stripped) is ENTIRELY a bare destructive bulk-clear
    imperative naming no specific product (see _DESTRUCTIVE_BULK_HOUSEHOLD_RE)
    — pure/local, never calls Gemini."""
    if not isinstance(text, str):
        return False
    return bool(_DESTRUCTIVE_BULK_HOUSEHOLD_RE.match(text.strip()))


def _route_destructive_bulk_guard(chat_id, user_id, display_name, text):
    """message_dispatcher.py's CommandRouteDeps.destructive_bulk_guard — the
    command-routes-level check that actually keeps a bare "Видали все"/
    "Очисти запаси" from ever reaching household_read's Phase-D Gemini
    classifier (see that field's own docstring). _run_general_ai_fallback
    below runs the SAME guard again for the DIRECT_GENERAL_AI_FALLBACK path
    (an active batch-edit-router reporting intent "none"), which skips this
    command-routes slice entirely. Stores a small ephemeral pending_
    destructive_guard context so a destination follow-up ("покупки"/
    "запаси"/...) is resolved by _continue_destructive_guard instead of
    ever reaching the ordinary read-list route.

    V1.4.1: an already-active pending write preview/clarification for this
    chat (e.g. a cleanup-admin rename/delete preview awaiting "✅ Так,
    застосувати"/"❌ Скасувати") must win over a NEW destructive-guard
    question — same _has_blocking_pending_state_for_reports guard every
    other route-starting function in this file already checks first (see
    _start_inventory_rename/_start_inventory_delete/_start_inventory_
    cleanup) — so this never overwrites/competes with that preview, and
    never opens a pending_destructive_guard context on top of it. That
    blocked branch deliberately sends NO reply_markup at all (never
    replaces whatever preview keyboard is already on screen), while the
    normal "no active preview" branch below attaches MAIN_KEYBOARD (V1.4.2
    Telegram reply-keyboard persistence fix) — a controlled clarification
    must never leave the user with no visible keyboard."""
    if not _looks_like_destructive_bulk_household_request(text):
        return False
    if _has_blocking_pending_state_for_reports(chat_id):
        send_message(chat_id, GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG)
        return True
    origin = household_router.current_origin(chat_id)
    pending_destructive_guard[chat_id] = {"origin": origin}
    send_message(chat_id, DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG, reply_markup=MAIN_KEYBOARD)
    return True


def _continue_destructive_guard(chat_id, text):
    """Follow-up reply to the Destructive Bulk Household Request Guard's own
    "покупки чи запаси?" clarification (pending_destructive_guard). A
    recognized destination word is intercepted here so it NEVER falls into
    the ordinary shopping/inventory read-list route below it in the dispatch
    chain — there is no existing safe bulk-delete-by-text preview flow to
    route to (V1.4 scope), so this always replies with a controlled "not
    implemented, use specific items or the menu" message; never executes a
    DB write, never calls general AI. Returns True (message_dispatcher.py's
    caller treats the message as fully handled) when a destination was
    recognized, False otherwise. Any OTHER reply (unrelated to this
    clarification) simply releases the ephemeral context here and returns
    False, so the caller falls through and normal routing continues for
    that SAME message — a real bulk-clear question was asked and just went
    unanswered, not a blocking preview."""
    data = pending_destructive_guard.get(chat_id)
    if not data:
        return False
    stripped = (text or "").strip()
    if _DESTRUCTIVE_GUARD_SHOPPING_RE.match(stripped):
        pending_destructive_guard.pop(chat_id, None)
        send_message(chat_id, DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG, reply_markup=SHOPPING_KEYBOARD)
        return True
    if _DESTRUCTIVE_GUARD_INVENTORY_RE.match(stripped):
        pending_destructive_guard.pop(chat_id, None)
        send_message(chat_id, DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG, reply_markup=INVENTORY_KEYBOARD)
        return True
    pending_destructive_guard.pop(chat_id, None)
    return False


def _looks_like_unrouted_household_action(text):
    """True if ANY line of `text` starts with a household-action verb (see
    _UNROUTED_HOUSEHOLD_ACTION_LINE_RE) followed by content — pure/local,
    never calls Gemini."""
    if not isinstance(text, str):
        return False
    return any(_UNROUTED_HOUSEHOLD_ACTION_LINE_RE.match(line.strip()) for line in text.splitlines())


UNROUTED_HOUSEHOLD_ACTION_MSG = (
    "Здається, це побутова дія, але я не зміг однозначно її розпізнати.\n\n"
    "Спробуй написати конкретніше, наприклад:\n"
    "«Купив молоко» — додати в запаси\n"
    "«Треба купити молоко» — додати в покупки\n"
    "«Використав 500 мл молока» — списати із запасів"
)


def _run_general_ai_fallback(chat_id, text):
    """Exact, unchanged general AI-chat fallback (Gemini 3.1 Flash Lite) —
    extracted from webhook()'s final block into a named function so it can
    be invoked directly for RouteOutcome.DIRECT_GENERAL_AI_FALLBACK (which
    must skip cooking mode entirely) as well as normally at the end of
    Phase D for RouteOutcome.CONTINUE. Household-Action-Line Fallback Guard
    v1 (see above) runs first — every household-shaped line still reaching
    here is asked to be more specific instead of ever prompting Gemini.

    Telegram reply-keyboard persistence fix: every reply from this function
    attaches MAIN_KEYBOARD — UNLESS an active pending preview/clarification
    is still open for this chat (only reachable here via RouteOutcome.
    DIRECT_GENERAL_AI_FALLBACK, i.e. pending_batch/pending_inventory_batch's
    own edit-router reported intent "none"), in which case reply_markup
    stays None so that preview's OWN keyboard is never silently replaced.
    Without this, a plain AI-chat answer (or this function's own destructive-
    guard/unrouted-household-action replies) left reply_markup unset
    entirely, and after any one-time keyboard elsewhere had already
    collapsed, the user was left with no reply keyboard at all until some
    OTHER handler happened to resend one."""
    keyboard = None if _has_blocking_pending_state_for_reports(chat_id) else MAIN_KEYBOARD

    if _looks_like_destructive_bulk_household_request(text):
        send_message(chat_id, DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG, reply_markup=keyboard)
        return

    if _looks_like_unrouted_household_action(text):
        send_message(chat_id, UNROUTED_HOUSEHOLD_ACTION_MSG, reply_markup=keyboard)
        return

    if chat_id not in user_history:
        user_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_history[chat_id].append({"role": "user", "content": text})
    user_history[chat_id] = user_history[chat_id][:1] + user_history[chat_id][-20:]

    gemini_history = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in user_history[chat_id][1:]
    ]
    answer = call_gemini(gemini_history, SYSTEM_PROMPT + "\n\n" + get_warsaw_datetime_context())

    if answer is not None:
        user_history[chat_id].append({"role": "assistant", "content": answer})
    else:
        answer = "AI-помічник тимчасово недоступний. Спробуйте ще раз трохи пізніше."

    send_message(chat_id, answer, reply_markup=keyboard)


def _try_handle_special_button(chat_id, user_id, display_name, text):
    """Dispatcher V3A special-button route — encapsulates the five exact
    menu-entry buttons (aliases intro, alias list, expenses intro,
    cooking-mode start, help) 1:1 with their old inline webhook() bodies.
    Returns True if `text` matched one of them (state already cleared/
    updated, message already sent), False otherwise.

    `text` is compared with variation selectors (U+FE0F/U+FE0E) stripped —
    Telegram may send "🍽️ Що приготувати"/"ℹ️ Допомога" with or without one
    depending on client/cache, and both must route identically. Only this
    local comparison variable is normalized; nothing sent onward (messages,
    Gemini calls) ever sees the stripped text."""
    text = message_dispatcher.strip_variation_selectors(text)
    if text == "🧠 Назви товарів":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context[chat_id] = "aliases"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        clear_alias_state(chat_id)
        send_message(chat_id, ALIAS_INTRO_TEXT, reply_markup=ALIASES_KEYBOARD)
        return True

    if text == "📋 Показати назви":
        active_list_context[chat_id] = "aliases"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        try:
            household_id, _ = get_household_and_user(user_id, display_name)
            send_message(chat_id, format_alias_list(list_household_aliases(household_id)), reply_markup=ALIASES_KEYBOARD)
        except Exception:
            send_message(chat_id, "Не вдалося показати домашні назви. Спробуй ще раз трохи пізніше.", reply_markup=ALIASES_KEYBOARD)
        return True

    if text == "💸 Витрати":
        waiting_for_ingredients.pop(chat_id, None)
        active_list_context[chat_id] = "expenses"
        clear_shopping_state(chat_id)
        clear_inventory_state(chat_id)
        clear_expense_state(chat_id)
        _handle_expenses_hub(chat_id, user_id, display_name)
        return True

    if text == "🍽 Що приготувати":
        active_list_context.pop(chat_id, None)
        clear_shopping_state(chat_id)
        waiting_for_ingredients.pop(chat_id, None)
        meal_ideas.try_handle_meal_ideas(_meal_ideas_deps, chat_id, user_id, display_name, text)
        return True

    if text == "ℹ Допомога":
        send_message(
            chat_id,
            "ℹ️ Як користуватися ботом:\n\n"
            "🛒 Покупки — спільний список покупок\n"
            "🧊 Запаси — що є вдома\n"
            "🍽️ Що приготувати — ідеї страв на основі запасів\n"
            "ℹ️ Допомога — ця інструкція\n\n"
            "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
        )
        return True

    return False


def _try_handle_cooking_mode(chat_id, user_id, display_name, text):
    """Dispatcher V3A cooking-mode route — encapsulates the old inline
    COOKING MODE webhook() branch 1:1. Returns True if cooking mode was
    active for this chat_id (already popped here) and handled the message,
    False otherwise (so dispatch() then falls through to the general AI
    fallback exactly once)."""
    if not waiting_for_ingredients.pop(chat_id, False):
        return False
    cooking_history = [{"role": "user", "content": text}]
    answer = call_gemini(cooking_history, COOKING_SYSTEM_PROMPT, temperature=0.4, model_url=GEMINI_COOKING_URL)
    if answer is None:
        answer = call_gemini(cooking_history, COOKING_SYSTEM_PROMPT, temperature=0.4, model_url=GEMINI_CHAT_URL)
    if answer is None:
        answer = "AI-помічник тимчасово недоступний. Спробуйте ще раз трохи пізніше."
    send_message(chat_id, answer)
    return True


def _try_handle_confirm_or_cancel(chat_id, user_id, display_name, text):
    """Dispatcher V3B confirm/cancel route — encapsulates all 20 exact
    confirm/cancel button texts 1:1 with their old inline webhook() bodies
    (same DB calls, same StaleSnapshotError handling, same messages, same
    pop()/clear_*() order, same behavior for a stale button with no active
    pending state). Returns True the moment `text` matches one of the 20
    exact texts — even when the matching branch finds no pending state and
    only sends a "nothing to confirm" message, exactly like the old inline
    `return "ok"` did regardless of whether anything actually changed.
    Returns False for any other text.

    `text` is compared with variation selectors (U+FE0F/U+FE0E) stripped —
    Telegram may send a pencil/emoji button label with or without one
    depending on client/cache (e.g. "✏️ Змінити список" vs "✏ Змінити
    список"), and both must route identically. Only this local comparison
    variable is normalized; nothing sent onward (messages, DB writes) ever
    sees the stripped text."""
    text = message_dispatcher.strip_variation_selectors(text)
    if text == "✅ Об'єднати":
        if chat_id in pending_merge:
            merge_data = pending_merge.pop(chat_id)
            list_type = merge_data["list_type"]
            if list_type == "shopping_pending_add":
                batch = pending_batch.get(chat_id)
                if batch:
                    batch["items"] = _apply_pending_merge(batch["items"], merge_data["groups"])
                    preview = format_batch_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Список вже не в пам'яті.", reply_markup=SHOPPING_KEYBOARD)
            elif list_type == "inventory_pending_add":
                batch = pending_inventory_batch.get(chat_id)
                if batch:
                    batch["items"] = _apply_pending_merge(batch["items"], merge_data["groups"])
                    preview = format_inventory_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_INVENTORY_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Список вже не в пам'яті.", reply_markup=INVENTORY_KEYBOARD)
            elif list_type == "shopping_saved":
                try:
                    count = execute_merge_shopping(merge_data["household_id"], merge_data["groups"], merge_data.get("targets"))
                    send_message(chat_id, f"✅ Об'єднано груп: {count}", reply_markup=SHOPPING_KEYBOARD)
                except StaleSnapshotError:
                    send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=SHOPPING_KEYBOARD)
                except Exception:
                    send_message(chat_id, "Не вдалося виконати об'єднання. Спробуйте ще раз.", reply_markup=SHOPPING_KEYBOARD)
            elif list_type == "inventory_saved":
                try:
                    count = execute_merge_inventory(merge_data["household_id"], merge_data["groups"], merge_data.get("targets"))
                    send_message(chat_id, f"✅ Об'єднано груп: {count}", reply_markup=INVENTORY_KEYBOARD)
                except StaleSnapshotError:
                    send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=INVENTORY_KEYBOARD)
                except Exception:
                    send_message(chat_id, "Не вдалося виконати об'єднання. Спробуйте ще раз.", reply_markup=INVENTORY_KEYBOARD)
            elif list_type == "inventory_cleanup":
                _apply_inventory_cleanup_merge(chat_id, merge_data)
        return True

    if text == "✅ Додати все":
        if chat_id in pending_inventory_batch:
            batch = pending_inventory_batch.pop(chat_id)
            try:
                count = add_inventory_items_batch(
                    batch["household_id"],
                    batch["user_db_id"],
                    batch["items"],
                    targets=batch.get("inventory_targets"),
                )
                send_message(chat_id, f"✅ Додано до запасів: {count}", reply_markup=INVENTORY_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        elif chat_id in pending_batch:
            batch = pending_batch.pop(chat_id)
            try:
                count = add_shopping_items_batch(
                    batch["household_id"],
                    batch["user_db_id"],
                    batch["items"]
                )
                send_message(chat_id, f"✅ Додано товарів: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        return True

    if text == "✏ Надіслати інший список":
        if chat_id in pending_inventory_batch:
            pending_inventory_batch.pop(chat_id, None)
            inventory_mode[chat_id] = "adding"
            send_message(chat_id, "Надішли один продукт або список продуктів. Можна кожен продукт з нового рядка.")
        else:
            pending_batch.pop(chat_id, None)
            shopping_mode[chat_id] = "adding"
            send_message(chat_id, "Надішли один товар або список товарів. Можна кожен товар з нового рядка.")
        return True

    if text == "❌ Скасувати":
        if chat_id in pending_cleanup_notice:
            # A read-only cleanup check (no safe merge found) — nothing was
            # written, so acknowledge it instead of falling into any of the
            # branches below.
            pending_cleanup_notice.pop(chat_id, None)
            send_message(chat_id, CLEANUP_NOTICE_ACKNOWLEDGED_MSG, reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_merge:
            merge_data = pending_merge.pop(chat_id)
            list_type = merge_data["list_type"]
            if list_type == "shopping_pending_add":
                batch = pending_batch.get(chat_id)
                if batch:
                    preview = format_batch_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Об'єднання скасовано.", reply_markup=SHOPPING_KEYBOARD)
            elif list_type == "inventory_pending_add":
                batch = pending_inventory_batch.get(chat_id)
                if batch:
                    preview = format_inventory_preview(batch["items"], batch.get("ignored_items"))
                    send_message(chat_id, preview, reply_markup=ADD_INVENTORY_PREVIEW_KEYBOARD)
                else:
                    send_message(chat_id, "Об'єднання скасовано.", reply_markup=INVENTORY_KEYBOARD)
            else:
                keyboard = SHOPPING_KEYBOARD if list_type == "shopping_saved" else INVENTORY_KEYBOARD
                send_message(chat_id, "Об'єднання скасовано.", reply_markup=keyboard)
        elif chat_id in pending_inventory_batch:
            clear_inventory_state(chat_id)
            send_message(chat_id, "Додавання продуктів скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_mark_batch:
            pending_mark_batch.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=SHOPPING_KEYBOARD)
        elif chat_id in pending_delete_batch:
            pending_delete_batch.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=SHOPPING_KEYBOARD)
        elif chat_id in pending_remove_batch:
            pending_remove_batch.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_saved_edit:
            edit_data = pending_saved_edit.pop(chat_id)
            ctx = edit_data["context_type"]
            keyboard = SHOPPING_KEYBOARD if ctx == "shopping_saved" else INVENTORY_KEYBOARD
            send_message(chat_id, "Зміни скасовано.", reply_markup=keyboard)
        elif chat_id in pending_inventory_consumption:
            pending_inventory_consumption.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_compound_inventory:
            pending_compound_inventory.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_inventory_reconciliation:
            pending_inventory_reconciliation.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_inventory_reconciliation_clarify:
            pending_inventory_reconciliation_clarify.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=INVENTORY_KEYBOARD)
        elif chat_id in pending_quick_purchase:
            pending_quick_purchase.pop(chat_id, None)
            send_message(chat_id, "Дію скасовано.", reply_markup=SHOPPING_KEYBOARD)
        elif chat_id in pending_alias_action:
            alias_data = pending_alias_action.pop(chat_id, None)
            origin = (alias_data or {}).get("origin", "global")
            send_message(chat_id, "Дію з домашніми назвами скасовано.", reply_markup=_alias_origin_keyboard(origin))
        elif chat_id in pending_expense or chat_id in pending_expense_delete or chat_id in expense_delete_selection:
            expenses.handle_cancel(chat_id)
        elif chat_id in pending_global_household:
            data = pending_global_household.pop(chat_id, None)
            origin = (data or {}).get("origin", "global")
            send_message(chat_id, "Зміни скасовано.", reply_markup=household_router.origin_keyboard(origin))
        elif chat_id in pending_inventory_quantity_clarification:
            data = pending_inventory_quantity_clarification.pop(chat_id, None)
            origin = (data or {}).get("origin", "global")
            send_message(chat_id, "Уточнення скасовано.", reply_markup=household_router.origin_keyboard(origin))
        elif chat_id in pending_inventory_representation_clarification:
            data = pending_inventory_representation_clarification.pop(chat_id, None)
            origin = (data or {}).get("origin", "global")
            send_message(chat_id, "Уточнення скасовано.", reply_markup=household_router.origin_keyboard(origin))
        elif chat_id in pending_add_destination_clarification:
            data = pending_add_destination_clarification.pop(chat_id, None)
            origin = (data or {}).get("origin", "global")
            send_message(chat_id, "Вибір місця додавання скасовано.", reply_markup=household_router.origin_keyboard(origin))
        elif chat_id in pending_cleanup_admin:
            data = pending_cleanup_admin.pop(chat_id, None)
            origin = (data or {}).get("origin", "global")
            send_message(chat_id, "Зміни скасовано.", reply_markup=household_router.origin_keyboard(origin))
        elif chat_id in pending_cleanup_admin_disambiguation:
            data = pending_cleanup_admin_disambiguation.pop(chat_id, None)
            origin = (data or {}).get("origin", "global")
            send_message(chat_id, "Зміни скасовано.", reply_markup=household_router.origin_keyboard(origin))
        elif chat_id in pending_destructive_guard:
            data = pending_destructive_guard.pop(chat_id, None)
            origin = (data or {}).get("origin", "global")
            send_message(chat_id, DESTRUCTIVE_GUARD_CANCELLED_MSG, reply_markup=household_router.origin_keyboard(origin))
        elif chat_id in pending_undo_action:
            pending_undo_action.pop(chat_id, None)
            send_message(chat_id, action_history.UNDO_CANCELLED_MSG, reply_markup=MAIN_KEYBOARD)
        else:
            clear_shopping_state(chat_id)
            send_message(chat_id, "Додавання товарів скасовано.", reply_markup=SHOPPING_KEYBOARD)
        return True

    if text == "✏ Виправити позицію":
        if chat_id in pending_batch:
            n = len(pending_batch[chat_id]["items"])
            shopping_mode[chat_id] = "editing_number"
            send_message(chat_id, f"Напиши номер позиції для виправлення (1–{n}):")
        return True

    if text == "✅ Куплено + додати в запаси":
        if chat_id in pending_mark_batch:
            mark_data = pending_mark_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in mark_data["items"]]
                targets = _snapshot_targets(mark_data["items"])
                count = mark_items_batch(mark_data["household_id"], item_ids, mark_data["user_db_id"], targets)
                for item in mark_data["items"]:
                    add_or_merge_inventory_item(
                        mark_data["household_id"],
                        mark_data["user_db_id"],
                        item["name"],
                        item.get("quantity_text", ""),
                        item.get("category", DEFAULT_CATEGORY),
                        canonical_name=item.get("canonical_name"),
                        quantity_value=item.get("quantity_value"),
                        quantity_unit=item.get("quantity_unit"),
                        quantity_inferred=item.get("quantity_inferred", False),
                    )
                send_message(chat_id, f"✅ Куплено й додано до запасів: {count}", reply_markup=SHOPPING_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, "Не вдалося завершити покупку. Спробуйте ще раз трохи пізніше.")
        return True

    if text == "✅ Куплено, без запасів":
        if chat_id in pending_mark_batch:
            mark_data = pending_mark_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in mark_data["items"]]
                targets = _snapshot_targets(mark_data["items"])
                count = mark_items_batch(mark_data["household_id"], item_ids, mark_data["user_db_id"], targets)
                send_message(chat_id, f"✅ Позначено купленими: {count}", reply_markup=SHOPPING_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, "Не вдалося завершити покупку. Спробуйте ще раз трохи пізніше.")
        return True

    if text == "✅ Так, видалити":
        if chat_id in pending_delete_batch:
            del_data = pending_delete_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in del_data["items"]]
                targets = _snapshot_targets(del_data["items"])
                count = delete_items_batch(del_data["household_id"], item_ids, targets)
                send_message(chat_id, f"🗑️ Видалено зі списку: {count}", reply_markup=SHOPPING_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        elif chat_id in pending_alias_action:
            data = pending_alias_action.pop(chat_id)
            origin = data.get("origin", "global")
            kind = data.get("kind")
            if kind == "delete":
                try:
                    delete_household_alias(data["household_id"], data["alias_normalized"])
                    _reply_after_alias_action(chat_id, data["household_id"], origin, "✅ Видалив.")
                except Exception:
                    send_message(chat_id, "Не вдалося видалити назву. Спробуй ще раз трохи пізніше.", reply_markup=_alias_origin_keyboard(origin))
            elif kind == "bulk_delete":
                try:
                    count = delete_household_aliases_batch(data["household_id"], data["targets"])
                    _reply_after_alias_action(chat_id, data["household_id"], origin, f"✅ Видалено домашніх назв: {count}.")
                except StaleSnapshotError:
                    send_message(
                        chat_id,
                        "Список домашніх назв змінився з іншого пристрою. Онови список і повтори дію.",
                        reply_markup=_alias_origin_keyboard(origin),
                    )
                except Exception:
                    send_message(chat_id, "Не вдалося видалити назви. Спробуй ще раз трохи пізніше.", reply_markup=_alias_origin_keyboard(origin))
            else:
                send_message(chat_id, "Ця дія вже не актуальна. Спробуй ще раз.", reply_markup=_alias_origin_keyboard(origin))
        elif chat_id in pending_expense_delete:
            expenses.handle_delete_confirm(chat_id)
        else:
            send_message(chat_id, "Немає активної дії для підтвердження.")
        return True

    if text == "✅ Так, запам'ятати":
        if chat_id in pending_alias_action:
            data = pending_alias_action.pop(chat_id)
            origin = data.get("origin", "global")
            if data.get("kind") == "create":
                try:
                    create_or_update_household_alias(data["household_id"], data["alias_text"], data["target_display_name"], data["user_db_id"])
                    _reply_after_alias_action(chat_id, data["household_id"], origin, "✅ Запам'ятав.")
                except Exception:
                    send_message(chat_id, "Не вдалося зберегти назву. Спробуй ще раз трохи пізніше.", reply_markup=_alias_origin_keyboard(origin))
            else:
                send_message(chat_id, "Ця дія вже не актуальна. Спробуй ще раз.", reply_markup=_alias_origin_keyboard(origin))
        else:
            send_message(chat_id, "Немає активної дії для підтвердження.")
        return True

    if text == "✅ Так, змінити":
        if chat_id in pending_alias_action:
            data = pending_alias_action.pop(chat_id)
            origin = data.get("origin", "global")
            if data.get("kind") == "update":
                try:
                    create_or_update_household_alias(data["household_id"], data["alias_text"], data["target_display_name"], data["user_db_id"])
                    _reply_after_alias_action(chat_id, data["household_id"], origin, "✅ Змінив.")
                except Exception:
                    send_message(chat_id, "Не вдалося змінити назву. Спробуй ще раз трохи пізніше.", reply_markup=_alias_origin_keyboard(origin))
            else:
                send_message(chat_id, "Ця дія вже не актуальна. Спробуй ще раз.", reply_markup=_alias_origin_keyboard(origin))
        else:
            send_message(chat_id, "Немає активної дії для підтвердження.")
        return True

    if text == "✅ Так, додати":
        expenses.handle_add_confirm(chat_id)
        return True

    if text == "✅ Так, застосувати":
        if chat_id in pending_cleanup_admin:
            _apply_cleanup_admin_confirm(chat_id)
        else:
            _apply_global_household_confirm(chat_id)
        return True

    if text == "✅ Так, скасувати":
        _apply_undo_confirm(chat_id)
        return True

    if text == "✅ Так, прибрати":
        if chat_id in pending_remove_batch:
            rem_data = pending_remove_batch.pop(chat_id)
            try:
                item_ids = [item["id"] for item in rem_data["items"]]
                targets = _snapshot_targets(rem_data["items"])
                count = delete_inventory_items_batch(rem_data["household_id"], item_ids, targets)
                send_message(chat_id, f"✅ Прибрано із запасів: {count}", reply_markup=INVENTORY_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return True

    if text == "✅ Додати до запасів":
        if chat_id in pending_quick_purchase:
            purchase = pending_quick_purchase.pop(chat_id)
            try:
                count = add_inventory_items_batch(
                    purchase["household_id"],
                    purchase["user_db_id"],
                    purchase["items"],
                )
                send_message(chat_id, f"✅ Додано до запасів: {count}", reply_markup=SHOPPING_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return True

    if text == "✏ Змінити список":
        if chat_id in pending_quick_purchase:
            pending_quick_purchase.pop(chat_id, None)
            saved_list_context[chat_id] = "shopping_saved"
            send_message(chat_id, "Напиши, які товари ти купив:")
        return True

    if text == "✅ Підтвердити зміни":
        if chat_id in pending_saved_edit:
            edit_data = pending_saved_edit.pop(chat_id)
            ctx = edit_data["context_type"]
            household_id = edit_data["household_id"]
            valid_updates = edit_data["validated_updates"]
            keyboard = SHOPPING_KEYBOARD if ctx == "shopping_saved" else INVENTORY_KEYBOARD
            try:
                if ctx == "shopping_saved":
                    update_shopping_items_batch(household_id, valid_updates)
                else:
                    update_inventory_items_batch(household_id, valid_updates)
                send_message(chat_id, "✅ Зміни застосовано.", reply_markup=keyboard)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=keyboard)
            except Exception:
                send_message(chat_id, DB_ERROR_MSG if ctx == "shopping_saved" else INVENTORY_ERROR_MSG)
        elif chat_id in pending_inventory_consumption:
            consume_data = pending_inventory_consumption.pop(chat_id)
            household_id = consume_data["household_id"]
            resolved = consume_data["resolved"]
            try:
                targets = _snapshot_targets(resolved)
                updates = [
                    {
                        "item_id": r["item_id"],
                        "quantity_value": r["new_value"],
                        "quantity_unit": r["new_unit"],
                        "quantity_text": r["new_display"],
                    }
                    for r in resolved if not r["will_remove"]
                ]
                delete_ids = [r["item_id"] for r in resolved if r["will_remove"]]
                updated, deleted = apply_inventory_consumption(household_id, updates, delete_ids, targets)
                send_message(chat_id, f"✅ Оновлено запасів: {updated + deleted}", reply_markup=INVENTORY_KEYBOARD)
            except StaleSnapshotError:
                send_message(chat_id, STALE_PREVIEW_MSG, reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return True

    if text == "✅ Підтвердити всі зміни":
        if chat_id in pending_compound_inventory:
            compound_data = pending_compound_inventory.pop(chat_id)
            household_id = compound_data["household_id"]
            user_db_id = compound_data["user_db_id"]
            inventory_changes = compound_data["inventory_changes"]
            add_to_shopping = compound_data["add_to_shopping"]
            try:
                targets = _snapshot_targets(inventory_changes)
                consume_updates = [
                    {
                        "item_id": c["item_id"],
                        "quantity_value": c["new_value"],
                        "quantity_unit": c["new_unit"],
                        "quantity_text": c["new_display"],
                    }
                    for c in inventory_changes if not c["will_remove"]
                ]
                delete_ids = [c["item_id"] for c in inventory_changes if c["will_remove"]]
                inv_updated, inv_deleted, shopping_added = apply_compound_inventory_operations(
                    household_id, user_db_id, consume_updates, delete_ids, add_to_shopping, targets
                )
                if shopping_added:
                    send_message(
                        chat_id,
                        f"✅ Зміни застосовано.\n\nОновлено запасів: {inv_updated + inv_deleted}\n"
                        f"Додано до покупок: {shopping_added}",
                        reply_markup=INVENTORY_KEYBOARD,
                    )
                else:
                    send_message(
                        chat_id,
                        f"✅ Зміни запасів застосовано: {inv_updated + inv_deleted}",
                        reply_markup=INVENTORY_KEYBOARD,
                    )
            except StaleSnapshotError:
                send_message(chat_id, "Список змінився з іншого пристрою. Онови запаси й повтори дію.", reply_markup=INVENTORY_KEYBOARD)
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return True

    if text == "✅ Підтвердити звіряння":
        if chat_id in pending_inventory_reconciliation:
            recon_data = pending_inventory_reconciliation.pop(chat_id)
            household_id = recon_data["household_id"]
            user_db_id = recon_data["user_db_id"]
            try:
                targets = _snapshot_targets(recon_data["updates"] + recon_data["deletes"])
                updates_for_db = [
                    {
                        "item_id": u["item_id"],
                        "quantity_value": u["new_value"],
                        "quantity_unit": u["new_unit"],
                        "quantity_text": u["new_display"],
                    }
                    for u in recon_data["updates"]
                ]
                delete_ids = [d["item_id"] for d in recon_data["deletes"]]
                apply_inventory_reconciliation(
                    household_id, user_db_id, updates_for_db, recon_data["additions"], delete_ids, targets
                )
                send_message(chat_id, "✅ Запаси звірено.", reply_markup=INVENTORY_KEYBOARD)
                send_message(chat_id, format_inventory_list(get_inventory_items(household_id)))
            except StaleSnapshotError:
                send_message(
                    chat_id,
                    "Список змінився з іншого пристрою. Онови запаси й повтори звіряння.",
                    reply_markup=INVENTORY_KEYBOARD,
                )
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return True

    if text == "✏ Змінити вибір":
        if chat_id in pending_mark_batch:
            mark_data = pending_mark_batch.pop(chat_id)
            try:
                items = get_active_shopping_items(mark_data["household_id"])
                shopping_mode[chat_id] = "marking"
                if not items:
                    send_message(chat_id, "Список покупок поки порожній.")
                else:
                    send_message(chat_id, format_shopping_list(items) + "\n\nНапиши, що саме купив:")
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        elif chat_id in pending_delete_batch:
            del_data = pending_delete_batch.pop(chat_id)
            try:
                items = get_active_shopping_items(del_data["household_id"])
                shopping_mode[chat_id] = "deleting"
                if not items:
                    send_message(chat_id, "Список покупок поки порожній.")
                else:
                    send_message(chat_id, format_shopping_list(items) + "\n\nНапиши, що видалити:")
            except Exception:
                send_message(chat_id, DB_ERROR_MSG)
        elif chat_id in pending_remove_batch:
            rem_data = pending_remove_batch.pop(chat_id)
            try:
                items = get_inventory_items(rem_data["household_id"])
                inventory_mode[chat_id] = "removing"
                if not items:
                    send_message(chat_id, "Запаси поки порожні.")
                else:
                    send_message(chat_id, format_inventory_list(items) + "\n\nНапиши, що прибрати із запасів:")
            except Exception:
                send_message(chat_id, INVENTORY_ERROR_MSG)
        return True

    return False


# Dispatcher V2B — message_dispatcher.py owns the ordered priority of the
# eleven remaining command/context routes (old Phase C routes 16-26); every
# callback is a lambda-forward to one of the thin wrapper functions above,
# same patch.object(bot, ...) reasoning as every other callback container.
_command_route_deps = message_dispatcher.CommandRouteDeps(
    ambiguous_add_route=lambda *a, **kw: _route_ambiguous_add(*a, **kw),
    explicit_global_add=lambda *a, **kw: _route_global_explicit_add(*a, **kw),
    bare_global_add=lambda *a, **kw: _route_global_bare_add(*a, **kw),
    global_household_router=lambda *a, **kw: _route_global_household(*a, **kw),
    expense_report_route=lambda *a, **kw: _route_expense_report(*a, **kw),
    expense_delete_command_route=lambda *a, **kw: _route_expense_delete_command(*a, **kw),
    active_aliases_context=lambda *a, **kw: _route_active_aliases_context(*a, **kw),
    global_alias_command=lambda *a, **kw: _route_global_alias_command(*a, **kw),
    active_expenses_context=lambda *a, **kw: _route_active_expenses_context(*a, **kw),
    global_expense_command=lambda *a, **kw: _route_global_expense_command(*a, **kw),
    inventory_cleanup_route=lambda *a, **kw: _route_inventory_cleanup(*a, **kw),
    inventory_admin_route=lambda *a, **kw: _route_inventory_admin(*a, **kw),
    destructive_bulk_guard=lambda *a, **kw: _route_destructive_bulk_guard(*a, **kw),
    saved_list_router=lambda *a, **kw: _route_saved_list_router(*a, **kw),
    general_ai_fallback=lambda *a, **kw: _run_general_ai_fallback(*a, **kw),
)

# Household Read Context V1 — household_read_context.py owns a single
# read-only Phase D slot (see message_dispatcher.py's DispatcherDeps.
# household_read docstring). Same lambda-forward reasoning as every other
# callback container: every field here is a thin runtime forward to a
# bot.py name so patch.object(bot, "get_inventory_items"/"call_gemini"/...)
# keeps working through this container too. No new DB helper, no new
# formatter — every field already exists and is used elsewhere in bot.py.
_household_read_deps = household_read_context.HouseholdReadDeps(
    get_household_and_user=lambda *a, **kw: get_household_and_user(*a, **kw),
    get_inventory_items=lambda *a, **kw: get_inventory_items(*a, **kw),
    get_active_shopping_items=lambda *a, **kw: get_active_shopping_items(*a, **kw),
    get_household_alias_map=lambda *a, **kw: get_household_alias_map(*a, **kw),
    resolve_item_name=lambda *a, **kw: resolve_item_name(*a, **kw),
    canonicalize_name=lambda *a, **kw: canonicalize_name(*a, **kw),
    format_quantity_display=lambda *a, **kw: format_quantity_display(*a, **kw),
    format_inventory_list=lambda *a, **kw: format_inventory_list(*a, **kw),
    format_shopping_list=lambda *a, **kw: format_shopping_list(*a, **kw),
    call_gemini=lambda *a, **kw: call_gemini(*a, **kw),
    send_message=lambda *a, **kw: send_message(*a, **kw),
    category_order=CATEGORY_ORDER,
)

# Meal Ideas V1 — meal_ideas.py owns a single read-only Phase D slot (see
# message_dispatcher.py's DispatcherDeps.meal_ideas docstring) plus the
# dedicated "🍽 Що приготувати" button (called directly from
# _try_handle_special_button, never through waiting_for_ingredients). Same
# lambda-forward reasoning as _household_read_deps above — every field here
# is a thin runtime forward to a bot.py name so patch.object(bot, "get_
# inventory_items"/"call_gemini"/...) keeps working through this container
# too. No new DB helper, no new formatter — every field already exists and
# is used elsewhere in bot.py.
_meal_ideas_deps = meal_ideas.MealIdeasDeps(
    get_household_and_user=lambda *a, **kw: get_household_and_user(*a, **kw),
    get_inventory_items=lambda *a, **kw: get_inventory_items(*a, **kw),
    format_quantity_display=lambda *a, **kw: format_quantity_display(*a, **kw),
    call_gemini=lambda *a, **kw: call_gemini(*a, **kw),
    send_message=lambda *a, **kw: send_message(*a, **kw),
)

# Message Dispatcher V1/V2A/V2B/V3A/V3B — message_dispatcher.py owns the
# confirm/cancel route (highest priority) plus the ordered navigation/
# special-button/menu/mode-text dispatch slice (old Phase A2/A3/B plus
# special buttons) plus the pending-route slice (Phase C routes 6-15) plus
# the command/context-route slice (Phase C routes 16-26) plus Phase D
# (cooking mode, then general AI fallback); it has no import of bot.py, so
# everything it needs is passed once via this injected dependency
# container, which simply nests the already-built _shopping_deps/
# _inventory_deps/_pending_route_deps/_command_route_deps instead of
# re-declaring their fields. Same lambda-forward reasoning as those
# containers — patch.object(bot, "send_message"/"clear_interaction_state"/
# "_try_handle_special_button"/"_try_handle_cooking_mode"/
# "_try_handle_confirm_or_cancel") keeps working through here too.
_dispatcher_deps = message_dispatcher.DispatcherDeps(
    send_message=lambda *a, **kw: send_message(*a, **kw),
    clear_interaction_state=lambda *a, **kw: clear_interaction_state(*a, **kw),
    main_keyboard=MAIN_KEYBOARD,
    help_text=(
        "ℹ️ Як користуватися ботом:\n\n"
        "🛒 Покупки — спільний список покупок\n"
        "🧊 Запаси — що є вдома\n"
        "🍽️ Що приготувати — ідеї страв на основі запасів\n"
        "ℹ️ Допомога — ця інструкція\n\n"
        "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
    ),
    shopping_deps=_shopping_deps,
    inventory_deps=_inventory_deps,
    pending_routes=_pending_route_deps,
    command_routes=_command_route_deps,
    special_button=lambda *a, **kw: _try_handle_special_button(*a, **kw),
    cooking_mode=lambda *a, **kw: _try_handle_cooking_mode(*a, **kw),
    confirm_or_cancel=lambda *a, **kw: _try_handle_confirm_or_cancel(*a, **kw),
    household_read=lambda *a, **kw: household_read_context.try_handle_household_read(_household_read_deps, *a, **kw),
    direct_household_read=lambda *a, **kw: household_read_context.try_handle_direct_household_read(_household_read_deps, *a, **kw),
    meal_ideas=lambda *a, **kw: meal_ideas.try_handle_meal_ideas(_meal_ideas_deps, *a, **kw),
)


@app.route("/")
def home():
    return "Bot is running"

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()

    if _is_duplicate_update(data.get("update_id")):
        return "ok"

    message = data.get("message")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if not text:
        return "ok"

    user_id = message.get("from", {}).get("id")
    display_name = message.get("from", {}).get("first_name")

    if text == "/myid":
        send_message(chat_id, f"Твій Telegram ID: {user_id}")
        return "ok"

    # =========================
    # ACCESS CHECK
    # =========================
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        send_message(chat_id, "Цей бот приватний і доступний лише для дозволених користувачів.")
        return "ok"

    # =========================
    # MESSAGE DISPATCHER V1/V2A/V2B/V3A/V3B (message_dispatcher.py)
    # Confirm/cancel (all 20 exact button texts), navigation, special
    # buttons (aliases/expenses/cooking-mode/help menu entries), shopping/
    # inventory menu buttons, shopping_mode/inventory_mode text dispatch,
    # the ten pending/clarification/undo routes, the eleven command/context
    # routes (ambiguous/explicit/bare add, Global Household Router, expense
    # reports, expense-delete command, aliases/expenses context, global
    # alias/expense command, saved-list router), then Phase D (cooking
    # mode, then general AI fallback) — exact same priority order and
    # behavior as the old inline branches this replaces. Confirm/cancel is
    # now the dispatcher's own top-priority route (see message_dispatcher.
    # RouteOutcome's docstring): if it matches, nothing else below it ever
    # runs for that message. dispatch() owns Phase D itself too:
    # DIRECT_GENERAL_AI_FALLBACK skips cooking mode entirely, CONTINUE
    # tries cooking mode first, then the fallback — either way general AI
    # fallback runs at most once, and webhook() no
    # longer needs to branch on the outcome at all.
    # =========================
    message_dispatcher.dispatch(_dispatcher_deps, chat_id, user_id, display_name, text)
    return "ok"


# Wire expenses.py's dependencies now that everything it needs (send_message,
# get_household_and_user, call_gemini, get_warsaw_datetime_context,
# _validate_selected_numbers, the expense database helpers, active_list_context,
# MAIN_KEYBOARD) is defined above. Must run before any webhook request is
# handled; expenses.py itself never imports bot.py (see its module docstring).
expenses.configure(sys.modules[__name__], active_list_context, MAIN_KEYBOARD)

# Wire household_router.py's dependencies the same way — it never imports
# bot.py either (see its module docstring); owns no pending state of its own.
household_router.configure(
    sys.modules[__name__], active_list_context, saved_list_context,
    {
        "main": MAIN_KEYBOARD, "shopping": SHOPPING_KEYBOARD, "inventory": INVENTORY_KEYBOARD,
        "expenses": EXPENSES_KEYBOARD, "aliases": ALIASES_KEYBOARD,
    },
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
