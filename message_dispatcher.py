"""Message Dispatcher V1/V2A.

The first explicit, ordered routing layer for incoming Telegram text. Owns
NOTHING new — it only formalizes the priority order bot.py's webhook()
already used for a specific slice of its dispatch chain:

V1: navigation, shopping/inventory menu buttons, shopping_mode/
    inventory_mode text dispatch (old Phase A2/A3/B).
V2A: the ten highest-priority pending-state/clarification/undo routes that
    used to open the single Pending Preview Router if/elif chain —
    pending_batch, pending_inventory_batch, pending_inventory_
    reconciliation_clarify, expense_delete_selection, the active expense
    preview guard, inventory quantity clarification, inventory
    representation clarification, the pending_global_household guard,
    add-destination clarification, and undo (old Phase C routes 6-15).

No pending state, no keyboards, no Gemini prompts live here; everything it
needs from the outside world is passed in via a `DispatcherDeps` container
built and owned by bot.py.

Deliberately NOT here (still bot.py-owned, unchanged): the shared confirm/
cancel button block, confirm handlers that write to the DB, every Phase C
route below undo (ambiguous add, explicit/bare add, the Global Household
Router, expense reports, expense-delete command, aliases, expense commands,
the saved-list router), cooking mode, general AI fallback, interaction_
state.py's own facade scope, and every state dict's ownership.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — legacy_shopping_flow.py/legacy_inventory_flow.py/action_history.py are
safe to import (none of them import bot.py), and this module reuses their
already existing containers instead of re-declaring every field here.
"""
from dataclasses import dataclass
from typing import Callable

import action_history
import legacy_shopping_flow
import legacy_inventory_flow


@dataclass
class PendingRouteDeps:
    """Injected dict references/callbacks for Dispatcher V2A's ten routes
    (old Phase C routes 6-15). Every dict field IS the same object its owner
    module (legacy_shopping_flow.py, legacy_inventory_flow.py, expenses.py,
    bot.py) already holds — no new state, no copies. Continuation handlers
    for reconciliation-clarify/quantity-clarification/representation-
    clarification/add-destination-clarification/undo stay as thin bot.py
    callback wrappers (their business logic touches the database/Gemini and
    would create a cyclic import or duplicate write behavior if moved
    here)."""
    pending_batch: dict
    pending_inventory_batch: dict
    pending_inventory_reconciliation_clarify: dict
    expense_delete_selection: dict
    pending_inventory_quantity_clarification: dict
    pending_inventory_representation_clarification: dict
    pending_global_household: dict
    pending_add_destination_clarification: dict
    pending_undo_action: dict
    has_active_expense_preview: Callable
    handle_expense_delete_selection_text: Callable
    continue_inventory_reconciliation_clarification: Callable
    continue_inventory_quantity_clarification: Callable
    continue_inventory_representation_clarification: Callable
    continue_add_destination_clarification: Callable
    start_undo_flow: Callable
    expense_preview_guard_msg: str
    global_household_preview_guard_msg: str


@dataclass
class DispatcherDeps:
    """Injected callbacks/values — no import of bot.py, ever."""
    send_message: Callable
    clear_interaction_state: Callable
    main_keyboard: dict
    help_text: str
    shopping_deps: legacy_shopping_flow.ShoppingFlowDeps
    inventory_deps: legacy_inventory_flow.InventoryFlowDeps
    # Optional so DispatcherDeps built before Dispatcher V2A existed (fake
    # test deps that only exercise V1's nav/menu/mode-text routes) keep
    # working unchanged — see the None-guard in _dispatch_pending_routes.
    pending_routes: PendingRouteDeps = None


def _dispatch_navigation(deps, chat_id, text):
    if text == "/start":
        deps.clear_interaction_state(chat_id)
        deps.send_message(
            chat_id,
            "Привіт! Я твій домашній помічник 🏠\n\n"
            "Обери дію на клавіатурі або напиши будь-яке запитання — я відповім за допомогою AI.",
            reply_markup=deps.main_keyboard,
        )
        return True

    if text == "/menu":
        deps.clear_interaction_state(chat_id)
        deps.send_message(chat_id, "Ось головне меню:", reply_markup=deps.main_keyboard)
        return True

    if text == "/help":
        deps.send_message(chat_id, deps.help_text)
        return True

    if text == "⬅️ Головне меню":
        deps.clear_interaction_state(chat_id)
        deps.send_message(chat_id, "Ось головне меню:", reply_markup=deps.main_keyboard)
        return True

    return False


def _dispatch_shopping_menu(deps, chat_id, user_id, display_name, text):
    if text == "🛒 Покупки":
        legacy_shopping_flow.handle_open_shopping_menu(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    if text == "➕ Додати товар":
        legacy_shopping_flow.handle_start_shopping_add(deps.shopping_deps, chat_id)
        return True

    if text == "📋 Показати список":
        legacy_shopping_flow.handle_show_shopping_list(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    if text == "✅ Позначити купленим":
        legacy_shopping_flow.handle_start_mark_bought(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    if text == "🗑️ Видалити товар":
        legacy_shopping_flow.handle_start_delete(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    return False


def _dispatch_inventory_menu(deps, chat_id, user_id, display_name, text):
    if text == "🧊 Запаси":
        legacy_inventory_flow.handle_open_inventory_menu(deps.inventory_deps, chat_id, user_id, display_name)
        return True

    if text == "➕ Додати продукти":
        legacy_inventory_flow.handle_start_inventory_add(deps.inventory_deps, chat_id)
        return True

    if text == "📋 Показати запаси":
        legacy_inventory_flow.handle_show_inventory_list(deps.inventory_deps, chat_id, user_id, display_name)
        return True

    if text == "➖ Використати / прибрати":
        legacy_inventory_flow.handle_start_inventory_remove(deps.inventory_deps, chat_id, user_id, display_name)
        return True

    return False


def _dispatch_pending_routes(deps, chat_id, user_id, display_name, text):
    """Dispatcher V2A, old Phase C routes 6-15 — checked in this exact
    order, each returning immediately on match (mirrors the single elif
    chain this replaces: once a route matches, none of the others are ever
    evaluated for the same message).

    Routes 6/7 (pending_batch/pending_inventory_batch) are the only ones
    that can return False even though they matched (Gemini's preview-edit-
    router intent was "none") — bot.py's remaining Phase C code explicitly
    re-checks membership in these same two dicts before its own routes, so
    that False here still means "fall straight through to cooking-mode/
    general AI-chat", never "try the next Phase C route", exactly as the
    original single elif chain did.
    """
    routes = deps.pending_routes
    if routes is None:
        return False

    if chat_id in routes.pending_batch:
        return legacy_shopping_flow.handle_pending_batch_edit_text(deps.shopping_deps, chat_id, text)

    if chat_id in routes.pending_inventory_batch:
        return legacy_inventory_flow.handle_pending_inventory_batch_edit_text(deps.inventory_deps, chat_id, text)

    if chat_id in routes.pending_inventory_reconciliation_clarify:
        routes.continue_inventory_reconciliation_clarification(chat_id, text)
        return True

    if chat_id in routes.expense_delete_selection:
        # Dedicated "pick which expense to delete" mode — a numbered list is
        # already on screen, so ANY text here is resolved against that SAME
        # stored list, never a fresh one.
        routes.handle_expense_delete_selection_text(chat_id, text)
        return True

    if routes.has_active_expense_preview(chat_id):
        # An expense add-preview or delete-preview is awaiting confirm/
        # cancel — no other plain text may start a new expense router,
        # replace the pending preview, touch the database, or reach general
        # AI-chat until it's confirmed or cancelled.
        deps.send_message(chat_id, routes.expense_preview_guard_msg)
        return True

    if chat_id in routes.pending_inventory_quantity_clarification:
        routes.continue_inventory_quantity_clarification(chat_id, text)
        return True

    if chat_id in routes.pending_inventory_representation_clarification:
        routes.continue_inventory_representation_clarification(chat_id, text)
        return True

    if chat_id in routes.pending_global_household:
        # A combined Global Household Router preview is awaiting confirm/
        # cancel — no new text (including one that would otherwise match
        # household_router.gate(text)) can start a new global router pass
        # while a plan of changes is still awaiting confirmation.
        deps.send_message(chat_id, routes.global_household_preview_guard_msg)
        return True

    if chat_id in routes.pending_add_destination_clarification:
        routes.continue_add_destination_clarification(chat_id, text)
        return True

    if chat_id in routes.pending_undo_action or action_history.is_undo_command(text):
        # Action History + Safe Undo v1 — while pending_undo_action is
        # already set, ANY other text here is intercepted too (never
        # replaces the pending undo, never touches the database, never
        # calls Gemini).
        if chat_id in routes.pending_undo_action:
            deps.send_message(chat_id, action_history.PENDING_UNDO_MSG)
        else:
            routes.start_undo_flow(chat_id, user_id, display_name)
        return True

    return False


def dispatch(deps, chat_id, user_id, display_name, text):
    """Route contract: True = fully handled (webhook returns "ok"
    immediately); False = not recognized by Dispatcher V1/V2A (webhook
    falls through, unchanged, to every remaining bot.py-owned branch — the
    aliases/expenses/cooking-mode buttons still living inline in bot.py,
    the remaining Phase C routes below undo, then general AI-chat).

    Exact internal order — mirrors old Phase A2/A3/B/C(6-15): navigation,
    shopping menu, inventory menu, shopping_mode text, inventory_mode text,
    then the ten pending/clarification/undo routes. Navigation is checked
    first so /start, /menu, /help and "⬅️ Головне меню" always work even
    while a shopping_mode/inventory_mode or any pending state is active.
    If shopping_mode and inventory_mode were ever both active for the same
    chat_id, shopping_mode wins — same as before this module existed.
    """
    if _dispatch_navigation(deps, chat_id, text):
        return True

    if _dispatch_shopping_menu(deps, chat_id, user_id, display_name, text):
        return True

    if _dispatch_inventory_menu(deps, chat_id, user_id, display_name, text):
        return True

    if legacy_shopping_flow.handle_shopping_mode_text(deps.shopping_deps, chat_id, user_id, display_name, text):
        return True

    if legacy_inventory_flow.handle_inventory_mode_text(deps.inventory_deps, chat_id, user_id, display_name, text):
        return True

    return _dispatch_pending_routes(deps, chat_id, user_id, display_name, text)
