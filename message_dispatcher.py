"""Message Dispatcher V1/V2A/V2B/V3A/V3B.

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
V3A: the five exact special buttons (aliases intro, alias list, expenses
    intro, cooking-mode start, help — checked right after navigation, ahead
    of the shopping/inventory menu buttons and mode dispatch) plus full
    ownership of Phase D (cooking mode, then general AI fallback). Before
    V3A, bot.py's webhook() branched on `dispatch()`'s return value to run
    Phase D itself; now `dispatch()` runs it internally and webhook() just
    calls dispatch() once and returns "ok".
V3B: the highest-priority route of all — the shared confirm/cancel button
    block (all 20 exact texts: merge/add-all/mark-bought/delete/undo/alias/
    expense/reconciliation confirm-or-cancel buttons). Checked before
    everything else in `_resolve_route_outcome`, exactly where it used to
    sit as webhook()'s BUTTON HANDLERS section, above navigation. After
    V3B, webhook() contains no application routing at all — only update
    parsing, deduplication, `/myid`, the access check, and the single
    `dispatch(...)` call.
Household Read Context V1: two read-only slots, both optional. A direct/
    deterministic-only slot (`DispatcherDeps.direct_household_read`) inside
    `_dispatch_command_routes`, checked right after Global Bare Add v1 but
    BEFORE the Global Household Router, every expense/alias route, and
    saved_list_router — so an explicit read-question like "Що треба
    купити?" is answered without ever reaching household_router.gate()
    (whose own local regex, e.g. "треба купити", matches such questions as a
    bare substring and would otherwise burn a real Gemini call and
    sometimes claim the message itself) and without being swallowed by the
    saved-list router's own AI edit-parser, even while a saved shopping/
    inventory list context is open. A second, fuller slot
    (`DispatcherDeps.household_read`) in Phase D, tried after cooking mode
    and before the general AI fallback, covering non-standard phrasings via
    a local topic gate + Gemini classifier. Neither slot claims a message
    any earlier route (confirm/cancel, navigation, special buttons, menus,
    modes, pending states, ambiguous-add/explicit-add/bare-add) already
    claimed, since all of those return before either slot is ever reached.
Meal Ideas V1: one read-only optional slot (`DispatcherDeps.meal_ideas`) in
    Phase D, tried after `household_read` and before the general AI
    fallback, so a plain read-question ("Що треба купити?") is always
    answered by household_read first, never reinterpreted as a request for
    meal suggestions. The dedicated "🍽 Що приготувати"/"🍽️ Що приготувати"
    button does NOT go through this slot — bot.py's special-button route
    calls `meal_ideas.try_handle_meal_ideas` directly, before this slot (or
    any other Phase D route) is ever reached for that message.

Route contract: `dispatch(...)` returns a `RouteOutcome` — kept for the
pre-V3A test suite and for `_resolve_route_outcome`'s own internal
bookkeeping, but as of V3A the return value is no longer meaningful to
callers that have Phase D wired (see `dispatch()`'s own docstring): every
call with a real `cooking_mode` callback fully completes Phase D as a side
effect and the caller never needs to branch on the result. This exists
because pending_batch/pending_inventory_batch's edit-router can match
(chat_id present) yet still report "nothing to do" (Gemini intent "none");
in the ORIGINAL single elif chain that already "claimed" the message,
skipping every remaining Phase C route (16-26) AND cooking mode, landing
directly on general AI-chat. A plain bool can't express "matched, but skip
cooking mode too, unlike an ordinary CONTINUE" — RouteOutcome.DIRECT_
GENERAL_AI_FALLBACK makes that exact, pre-existing semantic explicit.

No pending state, no keyboards, no Gemini prompts live here; everything it
needs from the outside world is passed in via a `DispatcherDeps` container
built and owned by bot.py.

Deliberately NOT here (still bot.py-owned, unchanged): every confirm/
cancel/special-button/cooking-mode/general-AI-fallback business-logic
implementation — all DB writes, StaleSnapshotError handling, messages,
state pop()/clear() order — only ever called through here as thin injected
callbacks; interaction_state.py's own facade scope; and every state dict's
ownership.

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

# Unicode variation selectors a Telegram client may or may not append to an
# emoji button label (U+FE0F requests emoji presentation, U+FE0E requests
# text presentation) — "🍽 Що приготувати" and "🍽️ Що приготувати" must be
# treated as the exact same button regardless of which one a given client
# cache sent. Deliberately just these two codepoints, nothing else, and
# deliberately for EXACT route/button comparisons only — never applied to
# free text forwarded to Gemini or to any outgoing bot message.
_VARIATION_SELECTORS = "︎️"


def strip_variation_selectors(text):
    """Remove U+FE0F/U+FE0E from `text`, nothing else. Safe to call on any
    string (returns non-str input unchanged) — used only where a message is
    about to be compared against a fixed button/route label, never on text
    that continues on to Gemini or gets echoed back to the user."""
    if not isinstance(text, str):
        return text
    return text.translate({ord(ch): None for ch in _VARIATION_SELECTORS})


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
    # Undo-Button-Cancels-Active-Operation v1 — both optional (default None)
    # so DispatcherDeps built before this existed keeps working unchanged,
    # same reasoning as every Optional field on DispatcherDeps itself.
    # has_active_pending_operation(chat_id) is a thin bot.py wrapper telling
    # the exact undo button apart from a plain "nothing pending" undo press;
    # cancel_active_pending_operation(chat_id) then pops whichever state
    # that check found active and sends the cancellation message itself.
    # Deliberately scoped to quantity/representation clarification, the
    # global household preview, add-destination clarification and a pending
    # saved-list edit — not pending_batch/pending_inventory_batch/
    # reconciliation-clarify/an active expense preview, which are already
    # intercepted earlier in this same route order and never reach this
    # check at all.
    has_active_pending_operation: Callable = None
    cancel_active_pending_operation: Callable = None


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
    # Optional so DispatcherDeps built before Dispatcher V2A/V2B/V3A/V3B
    # existed (fake test deps that only exercise a subset of the routing
    # chain) keep working unchanged — see the None-guards in _dispatch_
    # pending_routes/_dispatch_command_routes/_dispatch_special_buttons/
    # _dispatch_confirm_or_cancel and in dispatch() itself (cooking_mode is
    # None -> Phase D stays bot.py-owned, exactly like before V3A).
    pending_routes: PendingRouteDeps = None
    command_routes: CommandRouteDeps = None
    special_button: Callable = None
    cooking_mode: Callable = None
    confirm_or_cancel: Callable = None
    # Household Read Context V1 — optional single nested callback (thin
    # lambda-forward to try_handle_household_read), tried in Phase D after
    # cooking_mode and before general_ai_fallback. Kept optional (default
    # None) so DispatcherDeps built before this existed keeps working
    # unchanged, same reasoning as every other Optional field above.
    household_read: Callable = None
    # Household Read Context V1 — direct/deterministic-only routing fix.
    # Thin lambda-forward to try_handle_direct_household_read (no topic
    # gate, no Gemini). Checked inside _dispatch_command_routes right after
    # Global Bare Add v1 — BEFORE the Global Household Router (whose local
    # gate matches a plain read-question like "Що треба купити?" as a bare
    # substring) and before saved_list_router, so the question is answered
    # deterministically instead of burning a Gemini call or being swallowed
    # by the saved-list router's own AI edit-parser, even while a saved
    # shopping/inventory list context is open. Optional for the same reason
    # as every other field above.
    direct_household_read: Callable = None
    # Meal Ideas V1 — optional single nested callback (thin lambda-forward
    # to meal_ideas.try_handle_meal_ideas), tried in Phase D after
    # household_read and before general_ai_fallback (see dispatch()'s own
    # docstring). The dedicated "🍽 Що приготувати" button calls
    # meal_ideas.try_handle_meal_ideas directly through bot.py's special-
    # button route instead, never through this slot — this slot exists
    # only for the small set of natural-language phrasings ("Що можна
    # приготувати?", "Що зробити на вечерю?", ...). Optional for the same
    # reason as every other field above.
    meal_ideas: Callable = None


def _dispatch_confirm_or_cancel(deps, chat_id, user_id, display_name, text):
    """Dispatcher V3B confirm/cancel route — one thin callback covering all
    20 exact confirm/cancel button texts. Checked FIRST, ahead of
    navigation, special buttons, menu buttons, mode dispatch, pending
    routes and command routes: an exact confirm/cancel text always wins,
    regardless of any other active state, exactly like the old inline
    button block that used to open webhook()'s BUTTON HANDLERS section
    before anything else ever ran."""
    if deps.confirm_or_cancel is None:
        return False
    return deps.confirm_or_cancel(chat_id, user_id, display_name, text)


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


def _dispatch_special_buttons(deps, chat_id, user_id, display_name, text):
    """Dispatcher V3A special-button route — one thin callback covering the
    five exact texts (aliases intro, alias list, expenses intro,
    cooking-mode start, help). Checked right after navigation and ahead of
    the shopping/inventory menu buttons and mode dispatch, same reasoning
    as every other exact-text route in this chain: mutually-exclusive
    literal matches, so their exact position relative to each other never
    matters — only their position ahead of anything that consumes
    arbitrary free text does."""
    if deps.special_button is None:
        return False
    return deps.special_button(chat_id, user_id, display_name, text)


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

    # Computed here (ahead of the quantity/representation/global-household
    # clarification checks below) so the EXACT undo button label — with or
    # without the U+FE0F variation selector Telegram may or may not append —
    # is never swallowed as an invalid answer/new command by any of them.
    # Deliberately narrower than action_history.is_undo_command(text): the
    # natural-language undo phrasings ("скасувати останню дію" without the
    # arrow, etc.) still queue up behind those clarifications and behind
    # pending_add_destination_clarification below, unchanged — only the
    # literal button text gets this special handling.
    _undo_button_match = (
        strip_variation_selectors(text or "").strip().lower()
        == strip_variation_selectors(action_history.UNDO_BUTTON_TEXT).strip().lower()
    )

    if (
        _undo_button_match
        and routes.has_active_pending_operation is not None
        and routes.has_active_pending_operation(chat_id)
    ):
        # An unfinished command (clarification/preview/global-household
        # operation) is open for this chat — the button cancels THAT
        # instead of ever reaching historical undo below, exactly like
        # pressing "❌ Скасувати" would, just with one shared message.
        routes.cancel_active_pending_operation(chat_id)
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

    if chat_id in routes.pending_undo_action or action_history.is_undo_command(text) or _undo_button_match:
        # Action History + Safe Undo v1 — while pending_undo_action is
        # already set, ANY other text here is intercepted too (never
        # replaces the pending undo, never touches the database, never
        # calls Gemini). The variation-selector-stripped comparison exists
        # only because Telegram may send the undo button's label with or
        # without U+FE0F depending on client/cache — action_history.
        # is_undo_command(text) alone already covers the exact-label and
        # natural-language-phrase cases.
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

    if deps.direct_household_read and deps.direct_household_read(chat_id, user_id, display_name, text):
        # Household Read Context V1 — direct/deterministic read-questions
        # only (no topic gate, no Gemini). Checked here, ahead of the Global
        # Household Router, because household_router.gate()'s own local
        # regex (e.g. "треба купити") matches plain read-questions like "Що
        # треба купити?" as a bare substring and would otherwise burn a real
        # Gemini call — and, worse, occasionally "claim" the message itself
        # — before this deterministic, side-effect-free check ever ran. A
        # narrow, regex-only match here can never misfire on a genuine write
        # command (see household_read_context.py's own deterministic
        # patterns), so moving it ahead of every remaining command/context
        # route (including saved_list_router below) is safe.
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


def _resolve_route_outcome(deps, chat_id, user_id, display_name, text):
    """Pure routing decision — returns a `RouteOutcome` without running
    Phase D itself (cooking mode / general AI fallback). `dispatch()`
    (below) is the public entrypoint that completes Phase D exactly once
    based on this result. Exact internal order — mirrors old Phase A2/A3/
    B/C(6-26) plus V3A's special buttons plus V3B's confirm/cancel:
    confirm/cancel, navigation, special buttons, shopping menu, inventory
    menu, shopping_mode text, inventory_mode text, the ten pending/
    clarification/undo routes, then the eleven command/context routes.
    Confirm/cancel is checked FIRST — an exact confirm/cancel text always
    wins over everything else in this chain, same as the old inline BUTTON
    HANDLERS section that used to open webhook() before anything else ever
    ran. Navigation is checked next so /start, /menu, /help and "⬅️ Головне
    меню" always work even while a shopping_mode/inventory_mode or any
    pending state is active; special buttons are checked right after
    navigation so they always win over an active shopping_mode/inventory_
    mode too. If shopping_mode and inventory_mode were ever both active for
    the same chat_id, shopping_mode wins — same as before this module
    existed.
    """
    if _dispatch_confirm_or_cancel(deps, chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    if _dispatch_navigation(deps, chat_id, text):
        return RouteOutcome.HANDLED

    if _dispatch_special_buttons(deps, chat_id, user_id, display_name, text):
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


def dispatch(deps, chat_id, user_id, display_name, text):
    """Public entrypoint. Resolves the route (see `_resolve_route_outcome`)
    and, as of V3A, also completes Phase D itself:

    - HANDLED: a route already fully handled the message — nothing else
      to do.
    - DIRECT_GENERAL_AI_FALLBACK: an active shopping/inventory add-preview's
      edit-router matched but reported intent "none" — cooking mode is
      skipped entirely, the pending preview is left untouched, and
      `deps.command_routes.general_ai_fallback` runs directly.
    - CONTINUE: `deps.cooking_mode` is tried first (so an active
      `waiting_for_ingredients` state is always consumed by cooking mode
      itself, never left dangling because household_read/meal_ideas
      answered instead); if it reports it handled the message, nothing
      else runs. Otherwise `deps.household_read` (Household Read Context
      V1, optional) is tried next; then `deps.meal_ideas` (Meal Ideas V1,
      optional); if either handled the message, general AI fallback never
      runs. Otherwise `deps.command_routes.general_ai_fallback` runs
      exactly once.

    The return value is kept as a `RouteOutcome` for callers/tests built
    before Phase D moved here (see `DispatcherDeps.cooking_mode`'s
    docstring) — when `deps.cooking_mode` is None, `dispatch()` returns the
    raw `_resolve_route_outcome(...)` result unchanged and runs no Phase D
    side effect at all, exactly like before V3A. When `deps.cooking_mode`
    is wired (the real bot.py deps, always, after V3A), `dispatch()` always
    returns `RouteOutcome.HANDLED` — Phase D always produces a response one
    way or another, so callers no longer need to branch on the result.
    """
    outcome = _resolve_route_outcome(deps, chat_id, user_id, display_name, text)

    if deps.cooking_mode is None:
        return outcome

    if outcome == RouteOutcome.HANDLED:
        return RouteOutcome.HANDLED

    if outcome == RouteOutcome.DIRECT_GENERAL_AI_FALLBACK:
        deps.command_routes.general_ai_fallback(chat_id, text)
        return RouteOutcome.HANDLED

    # outcome == RouteOutcome.CONTINUE
    if deps.cooking_mode(chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    if deps.household_read is not None and deps.household_read(chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    if deps.meal_ideas is not None and deps.meal_ideas(chat_id, user_id, display_name, text):
        return RouteOutcome.HANDLED

    deps.command_routes.general_ai_fallback(chat_id, text)
    return RouteOutcome.HANDLED
