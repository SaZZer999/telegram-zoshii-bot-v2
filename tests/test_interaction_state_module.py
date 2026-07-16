"""Interaction State Facade V1 — module boundary tests.

Does NOT re-test full routing/business logic (already covered by
test_routing_precedence_contract.py, test_pending_preview_logic.py,
test_unresolved_fragments_safety.py, test_persistent_list_context.py,
test_stale_preview_protection.py, undo tests, test_global_household_
operations.py). This file only asserts: module boundary (no bot.py/
database.py/Flask/Telegram import), InteractionStateDeps working against
injected existing dict objects (never copies), identical before/after
cleanup snapshots for clear_shopping_state/clear_inventory_state/
clear_interaction_state, that clear_list_context is invoked exactly once by
clear_interaction_state, that bot.py's wrappers delegate into the facade,
that patch.object(bot, "clear_interaction_state") stays visible through
DispatcherDeps, and that every guard predicate matches the old inline logic
for representative state combinations.

No real Gemini/Telegram/Supabase call happens anywhere in this file.
"""
import ast
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import interaction_state  # noqa: E402
import message_dispatcher  # noqa: E402


def _make_fake_deps(**overrides):
    """An InteractionStateDeps built from plain fresh dicts/MagicMocks — no
    bot.py import, no network, no DB. Individual fields can be overridden
    per test."""
    defaults = dict(
        shopping_mode={},
        pending_batch={},
        pending_mark_batch={},
        pending_delete_batch={},
        inventory_mode={},
        pending_inventory_batch={},
        pending_remove_batch={},
        pending_expense={},
        pending_expense_delete={},
        expense_delete_selection={},
        pending_expense_batch_delete={},
        pending_merge={},
        pending_saved_edit={},
        pending_quick_purchase={},
        pending_inventory_consumption={},
        pending_compound_inventory={},
        pending_inventory_reconciliation={},
        pending_inventory_reconciliation_clarify={},
        pending_alias_action={},
        pending_global_household={},
        pending_inventory_quantity_clarification={},
        pending_inventory_representation_clarification={},
        pending_add_destination_clarification={},
        pending_cleanup_admin={},
        pending_cleanup_admin_disambiguation={},
        pending_destructive_guard={},
        pending_inventory_transform={},
        pending_quantity_price_intent={},
        pending_undo_action={},
        active_list_context={},
        saved_list_context={},
        waiting_for_ingredients={},
        clear_expense_state=MagicMock(),
        clear_list_context=MagicMock(),
    )
    defaults.update(overrides)
    return interaction_state.InteractionStateDeps(**defaults)


class TestModuleBoundary(unittest.TestCase):
    """1. interaction_state.py does not import bot.py/database.py/Flask/
    Telegram/psycopg/any Gemini SDK."""

    def test_no_forbidden_imports(self):
        source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "interaction_state.py")
        with open(source_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=source_path)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module.split(".")[0])
        forbidden = {"bot", "database", "flask", "psycopg", "telegram", "groq", "expenses"}
        self.assertFalse(imported_names & forbidden, f"forbidden imports found: {imported_names & forbidden}")


class TestFacadeUsesInjectedDicts(unittest.TestCase):
    """2. The facade mutates the SAME injected dict objects — never creates
    its own copies."""

    def test_clear_shopping_state_mutates_same_dict_objects(self):
        deps = _make_fake_deps()
        deps.shopping_mode[1] = "adding"
        deps.pending_batch[1] = {"items": []}
        shopping_mode_ref = deps.shopping_mode
        pending_batch_ref = deps.pending_batch

        interaction_state.clear_shopping_state(deps, 1)

        self.assertIs(deps.shopping_mode, shopping_mode_ref)
        self.assertIs(deps.pending_batch, pending_batch_ref)
        self.assertNotIn(1, deps.shopping_mode)
        self.assertNotIn(1, deps.pending_batch)


class TestClearShoppingStateSnapshot(unittest.TestCase):
    """3. clear_shopping_state produces the same before/after snapshot as
    the old inline logic: shopping_mode, pending_batch, pending_mark_batch,
    pending_delete_batch, pending_merge, saved_list_context, pending_saved_
    edit, pending_quick_purchase, pending_alias_action all popped for this
    chat_id; clear_expense_state called once; nothing else touched."""

    def test_pops_exactly_the_expected_dicts(self):
        chat_id = 42
        untouched_chat_id = 99
        deps = _make_fake_deps()
        touched = (
            deps.shopping_mode, deps.pending_batch, deps.pending_mark_batch, deps.pending_delete_batch,
            deps.pending_merge, deps.saved_list_context, deps.pending_saved_edit, deps.pending_quick_purchase,
            deps.pending_alias_action,
        )
        for d in touched:
            d[chat_id] = "marker"
            d[untouched_chat_id] = "marker"
        # A dict NOT in clear_shopping_state's scope must survive untouched.
        deps.pending_inventory_batch[chat_id] = "marker"

        interaction_state.clear_shopping_state(deps, chat_id)

        for d in touched:
            self.assertNotIn(chat_id, d)
            self.assertIn(untouched_chat_id, d)
        self.assertIn(chat_id, deps.pending_inventory_batch)
        deps.clear_expense_state.assert_called_once_with(chat_id)


class TestClearInventoryStateSnapshot(unittest.TestCase):
    """4. clear_inventory_state produces the same before/after snapshot as
    the old inline logic."""

    def test_pops_exactly_the_expected_dicts(self):
        chat_id = 43
        untouched_chat_id = 100
        deps = _make_fake_deps()
        touched = (
            deps.inventory_mode, deps.pending_inventory_batch, deps.pending_remove_batch,
            deps.pending_merge, deps.saved_list_context, deps.pending_saved_edit, deps.pending_quick_purchase,
            deps.pending_inventory_consumption, deps.pending_compound_inventory,
            deps.pending_inventory_reconciliation, deps.pending_inventory_reconciliation_clarify,
            deps.pending_alias_action,
        )
        for d in touched:
            d[chat_id] = "marker"
            d[untouched_chat_id] = "marker"
        # A dict NOT in clear_inventory_state's scope must survive untouched.
        deps.pending_batch[chat_id] = "marker"

        interaction_state.clear_inventory_state(deps, chat_id)

        for d in touched:
            self.assertNotIn(chat_id, d)
            self.assertIn(untouched_chat_id, d)
        self.assertIn(chat_id, deps.pending_batch)
        deps.clear_expense_state.assert_called_once_with(chat_id)


class TestClearInteractionStateSnapshot(unittest.TestCase):
    """5. clear_interaction_state clears every state clear_shopping_state/
    clear_inventory_state cover PLUS waiting_for_ingredients,
    active_list_context, pending_global_household, both clarification
    states, pending_add_destination_clarification, pending_undo_action —
    and calls clear_list_context exactly once."""

    def test_clears_every_expected_state_and_calls_clear_list_context_once(self):
        chat_id = 44
        deps = _make_fake_deps()
        all_dicts = (
            deps.waiting_for_ingredients, deps.active_list_context,
            deps.shopping_mode, deps.pending_batch, deps.pending_mark_batch, deps.pending_delete_batch,
            deps.inventory_mode, deps.pending_inventory_batch, deps.pending_remove_batch,
            deps.pending_merge, deps.saved_list_context, deps.pending_saved_edit, deps.pending_quick_purchase,
            deps.pending_inventory_consumption, deps.pending_compound_inventory,
            deps.pending_inventory_reconciliation, deps.pending_inventory_reconciliation_clarify,
            deps.pending_alias_action, deps.pending_global_household,
            deps.pending_inventory_quantity_clarification, deps.pending_inventory_representation_clarification,
            deps.pending_add_destination_clarification, deps.pending_undo_action,
            deps.pending_cleanup_admin_disambiguation, deps.pending_destructive_guard,
        )
        for d in all_dicts:
            d[chat_id] = "marker"

        interaction_state.clear_interaction_state(deps, chat_id)

        for d in all_dicts:
            self.assertNotIn(chat_id, d)
        deps.clear_list_context.assert_called_once_with(chat_id)
        deps.clear_expense_state.assert_called()


class TestClearExpenseStateInjectedCallback(unittest.TestCase):
    """6. expenses.clear_expense_state is invoked only through the injected
    callback, never imported/called directly."""

    def test_shopping_and_inventory_clear_both_call_injected_callback(self):
        deps = _make_fake_deps()
        interaction_state.clear_shopping_state(deps, 1)
        interaction_state.clear_inventory_state(deps, 1)
        self.assertEqual(deps.clear_expense_state.call_count, 2)


class TestBotWrappersDelegateToFacade(unittest.TestCase):
    """7. bot.py's clear_shopping_state/clear_inventory_state/clear_
    interaction_state/_has_blocking_pending_state*/_has_active_expense_
    preview/_should_restore_persisted_context all delegate into
    interaction_state.py via bot._interaction_state_deps."""

    def test_clear_shopping_state_delegates(self):
        with patch.object(interaction_state, "clear_shopping_state") as mock_fn:
            bot.clear_shopping_state(7)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 7)

    def test_clear_inventory_state_delegates(self):
        with patch.object(interaction_state, "clear_inventory_state") as mock_fn:
            bot.clear_inventory_state(8)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 8)

    def test_clear_interaction_state_delegates(self):
        with patch.object(interaction_state, "clear_interaction_state") as mock_fn:
            bot.clear_interaction_state(9)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 9)

    def test_has_blocking_pending_state_delegates(self):
        with patch.object(interaction_state, "has_blocking_pending_state", return_value=True) as mock_fn:
            result = bot._has_blocking_pending_state(10)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 10)
            self.assertTrue(result)

    def test_has_blocking_pending_state_for_expense_delegates(self):
        with patch.object(interaction_state, "has_blocking_pending_state_for_expense", return_value=True) as mock_fn:
            bot._has_blocking_pending_state_for_expense(11)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 11)

    def test_has_blocking_pending_state_for_reports_delegates(self):
        with patch.object(interaction_state, "has_blocking_pending_state_for_reports", return_value=True) as mock_fn:
            bot._has_blocking_pending_state_for_reports(12)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 12)

    def test_has_blocking_pending_state_for_expense_delete_delegates(self):
        with patch.object(interaction_state, "has_blocking_pending_state_for_expense_delete", return_value=True) as mock_fn:
            bot._has_blocking_pending_state_for_expense_delete(13)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 13)

    def test_has_active_expense_preview_delegates(self):
        with patch.object(interaction_state, "has_active_expense_preview", return_value=True) as mock_fn:
            bot._has_active_expense_preview(14)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 14)

    def test_should_restore_persisted_context_delegates(self):
        with patch.object(interaction_state, "should_restore_persisted_context", return_value=True) as mock_fn:
            bot._should_restore_persisted_context(15)
            mock_fn.assert_called_once_with(bot._interaction_state_deps, 15)


class TestPatchCompatibilityThroughDispatcherDeps(unittest.TestCase):
    """8. patch.object(bot, "clear_interaction_state", ...) is still visible
    through DispatcherDeps's runtime lambda-forward."""

    def test_patched_clear_interaction_state_seen_by_dispatcher_deps(self):
        with patch.object(bot, "clear_interaction_state") as mock_fn:
            bot._dispatcher_deps.clear_interaction_state(16)
            mock_fn.assert_called_once_with(16)


class TestActiveExpensePreviewGuard(unittest.TestCase):
    """9. _has_active_expense_preview gives the same result for
    pending_expense and pending_expense_delete (and False otherwise)."""

    def test_true_when_pending_expense_active(self):
        deps = _make_fake_deps()
        deps.pending_expense[20] = {}
        self.assertTrue(interaction_state.has_active_expense_preview(deps, 20))

    def test_true_when_pending_expense_delete_active(self):
        deps = _make_fake_deps()
        deps.pending_expense_delete[21] = {}
        self.assertTrue(interaction_state.has_active_expense_preview(deps, 21))

    def test_false_when_only_expense_delete_selection_active(self):
        deps = _make_fake_deps()
        deps.expense_delete_selection[22] = {}
        self.assertFalse(interaction_state.has_active_expense_preview(deps, 22))

    def test_false_when_nothing_active(self):
        deps = _make_fake_deps()
        self.assertFalse(interaction_state.has_active_expense_preview(deps, 23))


class TestBlockingGuardsRepresentativeCombinations(unittest.TestCase):
    """10. Each blocking guard matches the old inline logic for
    representative state combinations."""

    def test_alias_gate_blocked_by_pending_merge(self):
        deps = _make_fake_deps()
        deps.pending_merge[30] = {}
        self.assertTrue(interaction_state.has_blocking_pending_state(deps, 30))

    def test_alias_gate_not_blocked_by_its_own_pending_alias_action(self):
        deps = _make_fake_deps()
        deps.pending_alias_action[31] = {}
        self.assertFalse(interaction_state.has_blocking_pending_state(deps, 31))

    def test_expense_gate_blocked_by_alias_action(self):
        deps = _make_fake_deps()
        deps.pending_alias_action[32] = {}
        self.assertTrue(interaction_state.has_blocking_pending_state_for_expense(deps, 32))

    def test_expense_gate_not_blocked_by_its_own_pending_expense(self):
        deps = _make_fake_deps()
        deps.pending_expense[33] = {}
        self.assertFalse(interaction_state.has_blocking_pending_state_for_expense(deps, 33))

    def test_report_gate_blocked_by_pending_expense(self):
        deps = _make_fake_deps()
        deps.pending_expense[34] = {}
        self.assertTrue(interaction_state.has_blocking_pending_state_for_reports(deps, 34))

    def test_expense_delete_gate_blocked_by_pending_expense(self):
        deps = _make_fake_deps()
        deps.pending_expense[35] = {}
        self.assertTrue(interaction_state.has_blocking_pending_state_for_expense_delete(deps, 35))

    def test_expense_delete_gate_not_blocked_by_its_own_states(self):
        deps = _make_fake_deps()
        deps.pending_expense_delete[36] = {}
        deps.expense_delete_selection[36] = {}
        self.assertFalse(interaction_state.has_blocking_pending_state_for_expense_delete(deps, 36))

    def test_no_guard_blocked_when_nothing_pending(self):
        deps = _make_fake_deps()
        self.assertFalse(interaction_state.has_blocking_pending_state(deps, 37))
        self.assertFalse(interaction_state.has_blocking_pending_state_for_expense(deps, 37))
        self.assertFalse(interaction_state.has_blocking_pending_state_for_reports(deps, 37))
        self.assertFalse(interaction_state.has_blocking_pending_state_for_expense_delete(deps, 37))


class TestShouldRestorePersistedContext(unittest.TestCase):
    """11. should_restore_persisted_context does not regress."""

    def test_true_when_nothing_active(self):
        deps = _make_fake_deps()
        self.assertTrue(interaction_state.should_restore_persisted_context(deps, 40))

    def test_false_when_saved_list_context_already_set(self):
        deps = _make_fake_deps()
        deps.saved_list_context[41] = "shopping_saved"
        self.assertFalse(interaction_state.should_restore_persisted_context(deps, 41))

    def test_false_when_blocking_state_active(self):
        deps = _make_fake_deps()
        deps.pending_remove_batch[42] = {}
        self.assertFalse(interaction_state.should_restore_persisted_context(deps, 42))


# 12. No real Gemini/Telegram/Render/Supabase call happens anywhere in this
# file — every test above uses plain dicts/MagicMocks, and TestPatchCompat-
# ibilityThroughDispatcherDeps / TestBotWrappersDelegateToFacade are the only
# classes touching `bot`/`message_dispatcher`, both patched or delegated
# through mocks.


if __name__ == "__main__":
    unittest.main()
