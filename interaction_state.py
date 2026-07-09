"""Interaction State Facade V1.

A facade over pending-state dicts owned elsewhere (bot.py directly,
legacy_shopping_flow.py, legacy_inventory_flow.py, expenses.py) — NOT a new
owner of any state. This module holds no dict of its own; every dict it
touches is injected via `InteractionStateDeps` and popped/read in place, the
same objects bot.py/legacy_shopping_flow.py/legacy_inventory_flow.py/
expenses.py already own.

It centralizes exactly the cleanup/guard logic that used to be scattered as
module-level tuples and small predicate functions directly in bot.py:

- what to clear on clear_shopping_state/clear_inventory_state/
  clear_interaction_state (navigation "start over" semantics);
- which pending states block which gate (alias gate, expense-add gate,
  expense-report gate, expense-delete gate);
- whether an expense add/delete preview is currently active;
- whether a persisted list context is safe to restore.

Deliberately NOT here: state dict definitions/ownership, webhook routing,
message_dispatcher.py routes, shared confirm/cancel, the Pending Preview
Router, the Global Household Router, aliases/expenses/undo business logic,
saved-list router, reconciliation, clarification handlers, DB access,
Gemini calls. No import of bot.py, database.py, Flask, Telegram, psycopg or
any Gemini SDK — everything needed from the outside world is passed in via
`InteractionStateDeps`, built and owned by bot.py.
"""
from dataclasses import dataclass
from typing import Callable


@dataclass
class InteractionStateDeps:
    """Injected dict references/callbacks — no import of bot.py, ever.
    Every dict field below IS the same object its owner module (bot.py,
    legacy_shopping_flow.py, legacy_inventory_flow.py, expenses.py) already
    holds — this facade never copies or replaces any of them."""
    # legacy_shopping_flow.py-owned
    shopping_mode: dict
    pending_batch: dict
    pending_mark_batch: dict
    pending_delete_batch: dict
    # legacy_inventory_flow.py-owned
    inventory_mode: dict
    pending_inventory_batch: dict
    pending_remove_batch: dict
    # expenses.py-owned
    pending_expense: dict
    pending_expense_delete: dict
    expense_delete_selection: dict
    # bot.py-owned cross-cutting pending states
    pending_merge: dict
    pending_saved_edit: dict
    pending_quick_purchase: dict
    pending_inventory_consumption: dict
    pending_compound_inventory: dict
    pending_inventory_reconciliation: dict
    pending_inventory_reconciliation_clarify: dict
    pending_alias_action: dict
    pending_global_household: dict
    pending_inventory_quantity_clarification: dict
    pending_inventory_representation_clarification: dict
    pending_add_destination_clarification: dict
    # Inventory Cleanup Admin v1 — awaiting confirm/cancel on a rename/delete
    # preview for ONE inventory row.
    pending_cleanup_admin: dict
    pending_undo_action: dict
    # bot.py-owned shared context dicts
    active_list_context: dict
    saved_list_context: dict
    waiting_for_ingredients: dict
    # callbacks
    clear_expense_state: Callable
    clear_list_context: Callable


# =========================
# GATE-BLOCKING PENDING-STATE GROUPS
# =========================
def _alias_gate_blocking_states(deps):
    """Every OTHER flow's pending preview/confirm state — deliberately
    excludes pending_alias_action itself (a new global alias command is
    allowed to overwrite an already-pending alias action, same as the
    dedicated aliases submenu already does) and pending_batch/
    pending_inventory_batch/pending_inventory_reconciliation_clarify
    (already checked earlier in the same if/elif chain the gate lives in,
    so reaching the gate already implies they're inactive for this chat —
    listed anyway for robustness against future reordering)."""
    return (
        deps.pending_batch, deps.pending_inventory_batch, deps.pending_mark_batch, deps.pending_delete_batch,
        deps.pending_remove_batch, deps.pending_merge, deps.pending_saved_edit, deps.pending_quick_purchase,
        deps.pending_inventory_consumption, deps.pending_compound_inventory,
        deps.pending_inventory_reconciliation, deps.pending_inventory_reconciliation_clarify,
        # The Global Household Router's own combined preview — included here
        # (rather than only in the report-gate group below) so every other
        # gate's blocking group, all built by extending this one, also
        # treats it as an active preview to defer to.
        deps.pending_global_household,
        # Inventory Quantity Clarification v1's own continuation state — same
        # reasoning: while active, no other gate/flow may start a new
        # preview, touch the database, or reach general AI-chat.
        deps.pending_inventory_quantity_clarification,
        # Inventory Representation Clarification V2's own continuation state
        # — same reasoning: while a count-vs-mass/volume conflict is
        # unresolved, no other gate/flow may start a new preview, touch the
        # database, or reach general AI-chat.
        deps.pending_inventory_representation_clarification,
        # Global Bare Add v1's own continuation state — same reasoning:
        # while a "куди додати?" question is unanswered, no other gate/flow
        # may start a new preview, touch the database, or reach general
        # AI-chat.
        deps.pending_add_destination_clarification,
        # Inventory Cleanup Admin v1's own rename/delete preview — same
        # reasoning: while a "✅ Так, застосувати"/"❌ Скасувати" decision is
        # pending, no other gate/flow may start a new preview, touch the
        # database, or reach general AI-chat.
        deps.pending_cleanup_admin,
    )


def _expense_gate_blocking_states(deps):
    """Every OTHER flow's pending preview/confirm state that must block the
    global expense command gate — everything the alias gate already guards
    against, PLUS an active alias action, PLUS an in-progress
    expense-deletion flow (selection mode or delete preview). Unlike the
    alias gate (which deliberately allows a new global alias command to
    overwrite its own already-pending alias action), the expense-add gate
    must never override an alias preview or a deletion in progress: per
    spec, those have priority over a new "add expense" command.
    pending_expense's own state is deliberately excluded, same reasoning as
    the alias gate excluding its own."""
    return _alias_gate_blocking_states(deps) + (
        deps.pending_alias_action, deps.pending_expense_delete, deps.expense_delete_selection,
    )


def _report_gate_blocking_states(deps):
    """Every pending preview/confirm state, expense-add and
    expense-deletion included. The two read-only expense report commands
    must never fire while ANY operation has an unconfirmed preview open —
    "показати останні витрати" is not worth silently discarding a
    half-finished purchase/inventory/alias/expense edit or deletion."""
    return _expense_gate_blocking_states(deps) + (deps.pending_expense,)


def _expense_delete_gate_blocking_states(deps):
    """Every OTHER flow's pending preview/confirm state that must block the
    global expense-DELETE gate — everything the report gate already guards
    against (base flows, alias action, expense-add preview), EXCLUDING its
    own two states (pending_expense_delete, expense_delete_selection): a new
    global delete command, or free text typed mid-selection, is allowed to
    keep progressing its own flow rather than being blocked by itself —
    same reasoning as every other gate here."""
    return _alias_gate_blocking_states(deps) + (
        deps.pending_alias_action, deps.pending_expense,
    )


def _active_expense_preview_states(deps):
    """The two states an unconfirmed expense preview can be in —
    deliberately excludes expense_delete_selection (the earlier "pick a
    number" stage, which already has its own correct handling: any text
    there resolves against the shown list, and that behavior is unchanged
    by this guard)."""
    return (deps.pending_expense, deps.pending_expense_delete)


def has_blocking_pending_state(deps, chat_id):
    """True if some other flow's pending preview/confirm is currently
    active for this chat — the global alias command gate must never
    interrupt it."""
    return any(chat_id in d for d in _alias_gate_blocking_states(deps))


def has_blocking_pending_state_for_expense(deps, chat_id):
    """True if some other flow's pending preview/confirm — including an
    active alias action or an in-progress expense deletion — is currently
    active for this chat."""
    return any(chat_id in d for d in _expense_gate_blocking_states(deps))


def has_blocking_pending_state_for_reports(deps, chat_id):
    """True if ANY flow's pending preview/confirm is currently active for
    this chat — the expense report gate must never override any of them."""
    return any(chat_id in d for d in _report_gate_blocking_states(deps))


def has_blocking_pending_state_for_expense_delete(deps, chat_id):
    """True if some other flow's pending preview/confirm — including an
    active alias action or a pending expense-add preview — is currently
    active for this chat."""
    return any(chat_id in d for d in _expense_delete_gate_blocking_states(deps))


def has_active_expense_preview(deps, chat_id):
    """True if an expense add-preview or delete-preview is awaiting
    confirm/cancel for this chat. While true, no OTHER plain text may start
    a new expense router, replace the pending preview, touch the database,
    or reach general AI-chat."""
    return any(chat_id in d for d in _active_expense_preview_states(deps))


# =========================
# CLEANUP
# =========================
def clear_shopping_state(deps, chat_id):
    deps.shopping_mode.pop(chat_id, None)
    deps.pending_batch.pop(chat_id, None)
    deps.pending_mark_batch.pop(chat_id, None)
    deps.pending_delete_batch.pop(chat_id, None)
    deps.pending_merge.pop(chat_id, None)
    deps.saved_list_context.pop(chat_id, None)
    deps.pending_saved_edit.pop(chat_id, None)
    deps.pending_quick_purchase.pop(chat_id, None)
    deps.pending_alias_action.pop(chat_id, None)
    deps.clear_expense_state(chat_id)


def clear_inventory_state(deps, chat_id):
    deps.inventory_mode.pop(chat_id, None)
    deps.pending_inventory_batch.pop(chat_id, None)
    deps.pending_remove_batch.pop(chat_id, None)
    deps.pending_merge.pop(chat_id, None)
    deps.saved_list_context.pop(chat_id, None)
    deps.pending_saved_edit.pop(chat_id, None)
    deps.pending_quick_purchase.pop(chat_id, None)
    deps.pending_inventory_consumption.pop(chat_id, None)
    deps.pending_compound_inventory.pop(chat_id, None)
    deps.pending_inventory_reconciliation.pop(chat_id, None)
    deps.pending_inventory_reconciliation_clarify.pop(chat_id, None)
    deps.pending_alias_action.pop(chat_id, None)
    deps.clear_expense_state(chat_id)


def clear_interaction_state(deps, chat_id):
    """Routing Contract v1: the single place that decides what "start over"
    means for navigation (/start, /menu, "⬅️ Головне меню"). Clears every
    pending preview/confirm/clarification state across every flow, so the
    next command is always treated as new, never a continuation of
    whatever was open before navigation. Composes clear_shopping_state/
    clear_inventory_state (which already cover shopping_mode, inventory_mode,
    every legacy pending_* batch/merge/consumption/reconciliation state,
    aliases, and expenses) and adds the Global Household Router/Action
    History states."""
    deps.waiting_for_ingredients.pop(chat_id, None)
    deps.active_list_context.pop(chat_id, None)
    clear_shopping_state(deps, chat_id)
    clear_inventory_state(deps, chat_id)
    deps.clear_list_context(chat_id)
    deps.pending_global_household.pop(chat_id, None)
    deps.pending_inventory_quantity_clarification.pop(chat_id, None)
    deps.pending_inventory_representation_clarification.pop(chat_id, None)
    deps.pending_add_destination_clarification.pop(chat_id, None)
    deps.pending_undo_action.pop(chat_id, None)


# =========================
# PERSISTED CONTEXT RESTORE GUARD
# =========================
def should_restore_persisted_context(deps, chat_id):
    """True if there's no RAM saved_list_context and no other active preview
    or special mode that must take priority over restoring a persisted
    context.

    shopping_mode/inventory_mode and pending_batch/pending_inventory_batch
    are intentionally not checked here — they're already excluded by the
    time this is reached (handled earlier in webhook() with their own early
    returns, or by the outer if/elif around the saved_list_context branch)."""
    if deps.saved_list_context.get(chat_id) is not None:
        return False
    return not any(
        chat_id in d for d in (
            deps.pending_mark_batch, deps.pending_delete_batch, deps.pending_remove_batch,
            deps.pending_saved_edit, deps.pending_quick_purchase, deps.pending_merge,
            deps.pending_inventory_consumption, deps.pending_compound_inventory,
            deps.pending_inventory_reconciliation, deps.pending_inventory_reconciliation_clarify,
            deps.pending_alias_action,
        )
    )
