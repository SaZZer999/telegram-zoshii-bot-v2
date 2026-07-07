"""Message Dispatcher V1/V2A/V2B.

The explicit, ordered routing layer for incoming Telegram text. Owns
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
V2B: the eleven remaining command/context routes below undo — ambiguous
    add, explicit/bare global add, the Global Household Router, expense
    reports, expense-delete command, aliases context, global alias
    command, expenses context, global expense command, and the saved-list
    router (old Phase C routes 16-26).

Route contract: `dispatch(...)` returns a `RouteOutcome`, not a bool — see
its docstring. This exists because pending_batch/pending_inventory_batch's
edit-router can match (chat_id present) yet still report "nothing to do"
(Gemini intent "none"); in the ORIGINAL single elif chain that already
"claimed" the message, skipping every remaining Phase C route (16-26) AND
cooking mode, landing directly on general AI-chat. A plain bool can't
express "matched, but skip cooking mode too, unlike an ordinary CONTINUE" —
RouteOutcome.DIRECT_GENERAL_AI_FALLBACK makes that exact, pre-existing
semantic explicit instead of leaving a special-case guard in bot.py.

No pending state, no keyboards, no Gemini prompts live here; everything it
needs from the outside world is passed in via a `DispatcherDeps` container
built and owned by bot.py.

Deliberately NOT here (still bot.py-owned, unchanged): the shared confirm/
cancel button block, confirm handlers that write to the DB, the special
buttons still above the dispatcher call (aliases/expenses/cooking-mode/help
menu entries), cooking mode, general AI fallback's own implementation,
interaction_state.py's own facade scope, and every state dict's ownership.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — legacy_shopping_flow.py/legacy_inventory_flow.py/action_history.py are
safe to import (none of them import bot.py), and this module reuses their
already existing containers instead of re-declaring every field here.
household_router.gate(text)'s local check happens inside bot.py's
_route_global_household wrapper, not here, so this module never needs to
import household_router.py at all.
"""
import enum
from dataclasses import dataclass
from typing import Callable

import action_history
import legacy_shopping_flow
import legacy_inventory_flow


class RouteOutcome(enum.Enum):
    """dispatch()'s route contract.

    HANDLED
        A route fully handled the message — webhook() returns "ok"
        immediately.
    CONTINUE
        Dispatcher did not recognize the text — webhook() proceeds only to
        the not-yet-migrated Phase D (cooking mode, then general AI
        fallback), unchanged.
    DIRECT_GENERAL_AI_FALLBACK
        An active shopping/inventory add-preview's edit-router matched but
        reported intent "none" — every remaining command/context route
        must be skipped, the pending preview is left untouched, and
        webhook() must run the EXACT SAME general AI fallback CONTINUE
        would eventually reach, but skipping cooking mode (which CONTINUE
        would still check first).
    """
    HANDLED = "handled"
    CONTINUE = "continue"
    DIRECT_GENERAL_AI_FALLBACK = "direct_general_ai_fallback"


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
class CommandRouteDeps:
    """Injected callbacks for Dispatcher V2B's eleven command/context routes
    (old Phase C routes 16-26). Each callback is a thin bot.py-owned wrapper
    that fully encapsulates one route's own gate-check-and-handle decision
    (business logic, DB reads, Gemini calls all stay in bot.py — this
    module only decides WHETHER and in WHAT ORDER to call them).

    Every callback except the two active-context ones returns a plain bool:
    True = fully handled (stop here), False = not applicable (try the next
    route). `active_aliases_context`/`active_expenses_context` return None
    when the context itself doesn't match (try the next route) or True/False
    when it does (stop here either way — matching the context already
    claimed this message in the old elif chain, same reasoning as every
    dict-membership route in Dispatcher V2A)."""
    ambiguous_add_route: Callable
    explicit_global_add: Callable
    bare_global_add: Callable
    global_household_router: Callable
    expense_report_route: Callable
    expense_delete_command_route: Callable
    active_aliases_context: Callable
    global_alias_command: Callable
    active_expenses_context: Callable
    global_expense_command: Callable
    saved_list_router: Callable
    general_ai_fallback: Callable


@dataclass
class DispatcherDeps:
    """Injected callbacks/values — no import of bot.py, ever."""
    send_message: Callable
    clear_interaction_state: Callable
    main_keyboard: dict
    help_text: str
    shopping_deps: legacy_shopping_flow.ShoppingFlowDeps
    inventory_deps: legacy_inventory_flow.InventoryFlowDeps
    # Optional so DispatcherDeps built before Dispatcher V2A/V2B existed
    # (fake test deps that only exercise V1's nav/menu/mode-text routes)
    # keep working unchanged — see the None-guards in _dispatch_pending_
    # routes/_dispatch_command_routes.
    pending_routes: PendingRouteDeps = None
    command_routes: CommandRouteDeps = None


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

    Returns None if no route in this slice applies at all (dispatch()
    should try Dispatcher V2B's command routes next). Routes 6/7
    (pending_batch/pending_inventory_batch) are the only ones that can
    match yet still report "nothing to do" (Gemini's preview-edit-router
    intent was "none") — that now maps explicitly to
    RouteOutcome.DIRECT_GENERAL_AI_FALLBACK, never CONTINUE, so no command
    route below ever re-evaluates a message a higher-priority route already
    claimed, and cooking mode is skipped too.
    """
    routes = deps.pending_routes
    if routes is None:
        return None

    if chat_id in routes.pending_batch:
        handled = legacy_shopping_flow.handle_pending_batch_edit_text(deps.shopping_deps, chat_id, text)
        return RouteOutcome.HANDLED if handled else RouteOutcome.DIRECT_GENERAL_AI_FALLBACK

    if chat_id in routes.pending_inventory_batch:
        handled = legacy_inventory_flow.handle_pending_inventory_batch_edit_text(deps.inventory_deps, chat_id, text)
        return RouteOutcome.HANDLED if handled else RouteOutcome.DIRECT_GENERAL_AI_FALLBACK

    if chat_id in routes.pending_inventory_reconciliation_clarify:
        routes.continue_inventory_reconciliation_clarification(chat_id, text)
        return RouteOutcome.HANDLED

    if chat_id in routes.expense_delete_selection:
        # Dedicated "pick which expense to delete" mode — a numbered list is
        # already on screen, so ANY text here is resolved against that SAME
        # stored list, never a fresh one.
        routes.handle_expense_delete_selection_text(chat_id, text)
        return RouteOutcome.HANDLED

    if routes.has_active_expense_preview(chat_id):
        # An expense add-preview or delete-preview is awaiting confirm/
        # cancel — no other plain text may start a new expense router,
        # replace the pending preview, touch the database, or reach general
        # AI-chat until it's confirmed or cancelled.
        deps.send_message(chat_id, routes.expense_preview_guard_msg)
        return RouteOutcome.HANDLED

    if chat_id in routes.pending_inventory_quantity_clarification:
        routes.continue_inventory_quantity_clarification(chat_id, text)
        return RouteOutcome.HANDLED

    if chat_id in routes.pending_inventory_representation_clarification:
        routes.continue_inventory_representation_clarification(chat_id, text)
        return RouteOutcome.HANDLED

    if chat_id in routes.pending_global_household:
        # A combined Global Household Router preview is awaiting confirm/
        # cancel — no new text (including one that would otherwise match
        # household_router.gate(text)) can start a new global router pass
        # while a plan of changes is still awaiting confirmation.
        deps.send_message(chat_id, routes.global_household_preview_guard_msg)
        return RouteOutcome.HANDLED

    if chat_id in routes.pending_add_destination_clarification:
        routes.continue_add_destination_clarification(chat_id, text)
        return RouteOutcome.HANDLED

    if chat_id in routes.pending_undo_action or action_history.is_undo_command(text):
        # Action History + Safe Undo v1 — while pending_undo_action is
        # already set, ANY other text here is intercepted too (never
        # replaces the pending undo, never touches the database, never
        # calls Gemini).
        if chat_id in routes.pending_undo_action:
            deps.send_message(chat_id, action_history.PENDING_UNDO_MSG)
        else:
            routes.start_undo_flow(chat_id, user_id, display_name)
        return RouteOutcome.HANDLED

    return None


def _dispatch_command_routes(deps, chat_id, user_id, display_name, text):
    """Dispatcher V2B, old Phase C routes 16-26 — checked in this exact
    order, each returning immediately on match. Returns None if nothing in
    this slice applies at all (dispatch() then returns CONTINUE)."""
    routes = deps.command_routes
    if routes is None:
        return None

    if routes.ambiguous_add_route(chat_id, user_id, display_name, text):
        # Ambiguous "Додай ... за суму" guard — a bare "Додай молоко за 10
        # zł" (or an explicit-destination "Додай в запаси/до покупок ... за
        # суму") can never fall through into Explicit Add/Bare Add/the
        # Global Household Router/the expense gate below.
        return RouteOutcome.HANDLED

    if routes.explicit_global_add(chat_id, user_id, display_name, text):
        # Global Explicit Add v1 — a message with an EXPLICIT destination
        # phrase adds to that list regardless of which menu is open.
        # Checked ahead of the household_router gate so an explicit
        # destination always wins.
        return RouteOutcome.HANDLED

    if routes.bare_global_add(chat_id, user_id, display_name, text):
        # Global Bare Add v1 — a bare "Додай молоко" with NO destination
        # phrase. Checked right after explicit add and ahead of the
        # household_router gate below.
        return RouteOutcome.HANDLED

    if routes.global_household_router(chat_id, user_id, display_name, text):
        # Global Household Router v1 — narrow local gate (no Gemini)
        # checked first; only messages matching it even attempt a Gemini
        # call.
        return RouteOutcome.HANDLED

    if routes.expense_report_route(chat_id, user_id, display_name, text):
        # Expense report gate — narrow, local, no Gemini call for the
        # routing decision itself, checked ahead of the expenses submenu/
        # expense-add branches.
        return RouteOutcome.HANDLED

    if routes.expense_delete_command_route(chat_id, user_id, display_name, text):
        # Expense-delete gate — checked ahead of the aliases/expenses-
        # submenu/expense-add branches so a delete phrase is never
        # misrouted into creating a NEW expense.
        return RouteOutcome.HANDLED

    aliases_result = routes.active_aliases_context(chat_id, user_id, display_name, text)
    if aliases_result is not None:
        return RouteOutcome.HANDLED if aliases_result else RouteOutcome.CONTINUE

    if routes.global_alias_command(chat_id, user_id, display_name, text):
        # Global alias command gate — fires from anywhere but never
        # overrides an active preview/confirm/mode and never touches
        # saved_list_context.
        return RouteOutcome.HANDLED

    expenses_result = routes.active_expenses_context(chat_id, user_id, display_name, text)
    if expenses_result is not None:
        return RouteOutcome.HANDLED if expenses_result else RouteOutcome.CONTINUE

    if routes.global_expense_command(chat_id, user_id, display_name, text):
        # Global expense command gate — fires from anywhere but never
        # overrides an active preview/confirm from ANY other flow, aliases
        # included (aliases has priority over a new expense command).
        return RouteOutcome.HANDLED

    if routes.saved_list_router(chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    return None


def dispatch(deps, chat_id, user_id, display_name, text):
    """Route contract: returns a `RouteOutcome` (see its docstring) — never
    a bare bool. Exact internal order — mirrors old Phase A2/A3/B/C(6-26):
    navigation, shopping menu, inventory menu, shopping_mode text,
    inventory_mode text, the ten pending/clarification/undo routes, then
    the eleven command/context routes. Navigation is checked first so
    /start, /menu, /help and "⬅️ Головне меню" always work even while a
    shopping_mode/inventory_mode or any pending state is active. If
    shopping_mode and inventory_mode were ever both active for the same
    chat_id, shopping_mode wins — same as before this module existed.
    """
    if _dispatch_navigation(deps, chat_id, text):
        return RouteOutcome.HANDLED

    if _dispatch_shopping_menu(deps, chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    if _dispatch_inventory_menu(deps, chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    if legacy_shopping_flow.handle_shopping_mode_text(deps.shopping_deps, chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    if legacy_inventory_flow.handle_inventory_mode_text(deps.inventory_deps, chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    pending_outcome = _dispatch_pending_routes(deps, chat_id, user_id, display_name, text)
    if pending_outcome is not None:
        return pending_outcome

    command_outcome = _dispatch_command_routes(deps, chat_id, user_id, display_name, text)
    if command_outcome is not None:
        return command_outcome

    return RouteOutcome.CONTINUE
