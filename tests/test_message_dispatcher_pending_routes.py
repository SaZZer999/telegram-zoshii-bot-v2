"""Message Dispatcher V2A — pending states, clarifications and undo routing.

Does NOT re-test the underlying business logic of reconciliation/
quantity-clarification/representation-clarification/add-destination-
clarification/undo/expense-delete-selection (already covered by
test_inventory_quantity_clarification.py, test_inventory_representation_
clarification_v2.py, test_expense_delete.py, test_safe_undo_global_action.py
and friends, and keeps passing unchanged against the extracted routes).
This file only asserts: module boundary (no bot.py/database.py import),
DispatcherDeps nesting PendingRouteDeps instead of dozens of flat fields,
and the exact PRIORITY ORDER of Dispatcher V2A's ten routes (old Phase C
routes 6-15) against a plain fake PendingRouteDeps — plus a handful of
webhook-level integration checks (patch.object visibility, confirm/cancel
never reaching the dispatcher, unhandled text still reaching the old lower
router).

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
import message_dispatcher  # noqa: E402
import legacy_shopping_flow  # noqa: E402
import legacy_inventory_flow  # noqa: E402


def _make_fake_shopping_deps(**overrides):
    defaults = dict(
        send_message=MagicMock(),
        get_household_and_user=MagicMock(return_value=(1, 10)),
        get_household_alias_map=MagicMock(return_value={}),
        get_active_shopping_items=MagicMock(return_value=[]),
        save_list_context=MagicMock(),
        normalize_item_quantity=MagicMock(return_value={
            "quantity_text": "", "quantity_value": None, "quantity_unit": None,
            "quantity_inferred": True, "canonical_name": "молоко",
        }),
        parse_item_text=MagicMock(return_value=("Молоко", "")),
        call_gemini=MagicMock(return_value=None),
        ask_gemini_for_selection=MagicMock(return_value=("invalid", None)),
        ask_gemini_preview_edit_router=MagicMock(return_value={"intent": "none", "updates": []}),
        validate_preview_updates=MagicMock(return_value=[]),
        apply_preview_updates=MagicMock(side_effect=lambda items, updates, alias_map=None: items),
        auto_merge_in_place=MagicMock(side_effect=lambda items: items),
        format_shopping_list=MagicMock(side_effect=lambda items: f"list:{len(items)}"),
        format_batch_preview=MagicMock(side_effect=lambda items, ignored=None: f"preview:{len(items)}"),
        format_grouped_list=MagicMock(side_effect=lambda items, header: f"{header}:{len(items)}"),
        format_unresolved_fragments_message=MagicMock(return_value="unresolved"),
        clear_shopping_state=MagicMock(),
        clear_inventory_state=MagicMock(),
        active_list_context={},
        saved_list_context={},
        waiting_for_ingredients={},
        shopping_keyboard={"keyboard": "shopping"},
        add_preview_keyboard={"keyboard": "add_preview"},
        mark_preview_keyboard={"keyboard": "mark_preview"},
        delete_preview_keyboard={"keyboard": "delete_preview"},
        shopping_parse_prompt="SHOPPING_PROMPT",
        default_category="Інше їстівне",
        valid_categories={"Інше їстівне", "Молочне та яйця"},
        db_error_msg="DB_ERROR",
        selection_error_msg="SELECTION_ERROR",
    )
    defaults.update(overrides)
    return legacy_shopping_flow.ShoppingFlowDeps(**defaults)


def _make_fake_inventory_deps(**overrides):
    defaults = dict(
        send_message=MagicMock(),
        call_gemini=MagicMock(return_value=None),
        get_household_and_user=MagicMock(return_value=(1, 10)),
        get_inventory_items=MagicMock(return_value=[]),
        get_household_alias_map=MagicMock(return_value={}),
        save_list_context=MagicMock(),
        normalize_item_quantity=MagicMock(return_value={
            "quantity_text": "", "quantity_value": None, "quantity_unit": None,
            "quantity_inferred": True, "canonical_name": "молоко",
        }),
        canonicalize_name=MagicMock(side_effect=lambda name: (name or "").strip().lower()),
        parse_inventory_list_with_gemini=MagicMock(return_value=None),
        resolve_inventory_representation=MagicMock(return_value=("new", None)),
        format_representation_clarify_message=MagicMock(return_value="clarify"),
        format_representation_separate_warning=MagicMock(return_value="separate warning"),
        format_representation_merge_quantity_fragment=MagicMock(return_value="merged fragment"),
        merge_quantity_values=MagicMock(return_value=(None, None)),
        format_quantity_display=MagicMock(return_value=""),
        ask_gemini_for_selection=MagicMock(return_value=("invalid", None)),
        ask_gemini_preview_edit_router=MagicMock(return_value={"intent": "none", "updates": []}),
        validate_preview_updates=MagicMock(return_value=[]),
        apply_preview_updates=MagicMock(side_effect=lambda items, updates, alias_map=None: items),
        auto_merge_in_place=MagicMock(side_effect=lambda items: items),
        format_grouped_list=MagicMock(side_effect=lambda items, header: f"{header}:{len(items)}"),
        format_inventory_list=MagicMock(side_effect=lambda items: f"list:{len(items)}"),
        format_inventory_preview=MagicMock(side_effect=lambda items, ignored=None: f"preview:{len(items)}"),
        format_unresolved_fragments_message=MagicMock(return_value="unresolved"),
        resolve_numbered_inventory_delete_selection=MagicMock(return_value=(None, None)),
        format_numbered_delete_mismatch_message=MagicMock(return_value="mismatch"),
        clear_shopping_state=MagicMock(),
        clear_inventory_state=MagicMock(),
        active_list_context={},
        saved_list_context={},
        waiting_for_ingredients={},
        inventory_keyboard={"keyboard": "inventory"},
        add_inventory_preview_keyboard={"keyboard": "add_inventory_preview"},
        remove_preview_keyboard={"keyboard": "remove_preview"},
        inventory_parse_prompt="INVENTORY_PROMPT",
        default_category="Інше їстівне",
        valid_categories={"Інше їстівне", "Молочне та яйця"},
        inventory_error_msg="INVENTORY_ERROR",
        selection_error_msg="SELECTION_ERROR",
    )
    defaults.update(overrides)
    return legacy_inventory_flow.InventoryFlowDeps(**defaults)


def _make_fake_pending_route_deps(**overrides):
    defaults = dict(
        pending_batch={},
        pending_inventory_batch={},
        pending_inventory_reconciliation_clarify={},
        expense_delete_selection={},
        pending_inventory_quantity_clarification={},
        pending_inventory_representation_clarification={},
        pending_global_household={},
        pending_add_destination_clarification={},
        pending_undo_action={},
        has_active_expense_preview=MagicMock(return_value=False),
        handle_expense_delete_selection_text=MagicMock(),
        continue_inventory_reconciliation_clarification=MagicMock(),
        continue_inventory_quantity_clarification=MagicMock(),
        continue_inventory_representation_clarification=MagicMock(),
        continue_add_destination_clarification=MagicMock(),
        start_undo_flow=MagicMock(),
        expense_preview_guard_msg="EXPENSE_PREVIEW_GUARD",
        global_household_preview_guard_msg="GLOBAL_HOUSEHOLD_GUARD",
        has_active_pending_operation=MagicMock(return_value=False),
        cancel_active_pending_operation=MagicMock(),
    )
    defaults.update(overrides)
    return message_dispatcher.PendingRouteDeps(**defaults)


def _make_fake_dispatcher_deps(**overrides):
    defaults = dict(
        send_message=MagicMock(),
        clear_interaction_state=MagicMock(),
        main_keyboard={"keyboard": "main"},
        help_text="HELP_TEXT",
        shopping_deps=_make_fake_shopping_deps(),
        inventory_deps=_make_fake_inventory_deps(),
        pending_routes=_make_fake_pending_route_deps(),
    )
    defaults.update(overrides)
    return message_dispatcher.DispatcherDeps(**defaults)


class TestModuleBoundary(unittest.TestCase):
    """1. message_dispatcher.py does not import bot.py or database.py."""

    def test_no_forbidden_imports(self):
        source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "message_dispatcher.py")
        with open(source_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=source_path)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module.split(".")[0])
        forbidden = {"bot", "database", "flask", "psycopg", "telegram"}
        self.assertFalse(imported_names & forbidden, f"forbidden imports found: {imported_names & forbidden}")


class TestDispatcherDepsNesting(unittest.TestCase):
    """2. DispatcherDeps uses the nested PendingRouteDeps container, not
    dozens of new flat fields."""

    def test_pending_routes_is_nested_container_not_flattened(self):
        deps = _make_fake_dispatcher_deps()
        self.assertIsInstance(deps.pending_routes, message_dispatcher.PendingRouteDeps)
        dispatcher_fields = set(message_dispatcher.DispatcherDeps.__dataclass_fields__.keys())
        # DispatcherDeps itself must stay small — none of PendingRouteDeps's
        # own field names should be re-declared at the top level.
        pending_route_fields = set(message_dispatcher.PendingRouteDeps.__dataclass_fields__.keys())
        self.assertFalse(dispatcher_fields & pending_route_fields)
        self.assertIn("pending_routes", dispatcher_fields)


class TestPendingBatchOutranksInventoryBatch(unittest.TestCase):
    """3. pending_batch has priority over pending_inventory_batch."""

    def test_pending_batch_checked_first(self):
        chat_id = 1
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_batch={chat_id: {}}, pending_inventory_batch={chat_id: {}},
            ),
        )
        with patch.object(legacy_shopping_flow, "handle_pending_batch_edit_text", return_value=True) as mock_shop, \
                patch.object(legacy_inventory_flow, "handle_pending_inventory_batch_edit_text") as mock_inv:
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "щось")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        mock_shop.assert_called_once_with(deps.shopping_deps, chat_id, "щось")
        mock_inv.assert_not_called()


class TestPendingInventoryBatchDoesNotFallThroughToGlobalRouter(unittest.TestCase):
    """4. pending_inventory_batch does not let text fall through to any
    lower route within the same dispatch() call — intent "none" maps to
    RouteOutcome.DIRECT_GENERAL_AI_FALLBACK, which IS the final result."""

    def test_intent_none_returns_direct_general_ai_fallback_without_checking_lower_routes(self):
        chat_id = 2
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_inventory_batch={chat_id: {}}),
        )
        with patch.object(legacy_inventory_flow, "handle_pending_inventory_batch_edit_text", return_value=False) as mock_inv:
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Купив молоко за 10 zł")
        self.assertEqual(result, message_dispatcher.RouteOutcome.DIRECT_GENERAL_AI_FALLBACK)
        mock_inv.assert_called_once()
        deps.pending_routes.continue_inventory_quantity_clarification.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_not_called()


class TestReconciliationClarifyOutranksLowerRoutes(unittest.TestCase):
    """5. reconciliation clarification has priority over every route below
    it (expense_delete_selection, active expense preview, both
    clarifications, global household guard, add-destination clarification,
    undo)."""

    def test_reconciliation_clarify_wins_even_with_lower_states_also_active(self):
        chat_id = 3
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_inventory_reconciliation_clarify={chat_id: {}},
                expense_delete_selection={chat_id: {}},
                pending_global_household={chat_id: {}},
                pending_undo_action={chat_id: {}},
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "1 л")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_inventory_reconciliation_clarification.assert_called_once_with(chat_id, "1 л")
        deps.pending_routes.handle_expense_delete_selection_text.assert_not_called()
        deps.send_message.assert_not_called()


class TestExpenseDeleteSelectionOutranksActiveExpensePreview(unittest.TestCase):
    """6. expense_delete_selection has priority over the active expense
    preview guard."""

    def test_expense_delete_selection_wins(self):
        chat_id = 4
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                expense_delete_selection={chat_id: {}},
                has_active_expense_preview=MagicMock(return_value=True),
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "2")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.handle_expense_delete_selection_text.assert_called_once_with(chat_id, "2")
        deps.send_message.assert_not_called()


class TestActiveExpensePreviewBlocksUndo(unittest.TestCase):
    """7. the active expense preview guard blocks an undo command."""

    def test_undo_command_blocked_by_active_expense_preview(self):
        chat_id = 5
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(has_active_expense_preview=MagicMock(return_value=True)),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Скасувати останню дію")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.send_message.assert_called_once_with(chat_id, "EXPENSE_PREVIEW_GUARD")
        deps.pending_routes.start_undo_flow.assert_not_called()


class TestQuantityClarificationOutranksGlobalHousehold(unittest.TestCase):
    """8. pending quantity clarification has priority over
    pending_global_household."""

    def test_quantity_clarification_wins(self):
        chat_id = 6
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_inventory_quantity_clarification={chat_id: {}},
                pending_global_household={chat_id: {}},
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "1Л")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_inventory_quantity_clarification.assert_called_once_with(chat_id, "1Л")
        deps.send_message.assert_not_called()


class TestRepresentationClarificationOutranksGlobalHousehold(unittest.TestCase):
    """9. pending representation clarification has priority over
    pending_global_household."""

    def test_representation_clarification_wins(self):
        chat_id = 7
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_inventory_representation_clarification={chat_id: {}},
                pending_global_household={chat_id: {}},
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "окремо")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_inventory_representation_clarification.assert_called_once_with(chat_id, "окремо")
        deps.send_message.assert_not_called()


class TestGlobalHouseholdGuardBlocksNewCommand(unittest.TestCase):
    """10. pending_global_household blocks a new command and never invokes
    any continuation/undo callback (i.e. never re-runs Gemini)."""

    def test_global_household_guard_sends_message_only(self):
        chat_id = 8
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_global_household={chat_id: {}}),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Купив хліб за 20 zł")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.send_message.assert_called_once_with(chat_id, "GLOBAL_HOUSEHOLD_GUARD")
        deps.pending_routes.continue_inventory_quantity_clarification.assert_not_called()
        deps.pending_routes.continue_inventory_representation_clarification.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_not_called()


class TestAddDestinationClarificationOutranksUndo(unittest.TestCase):
    """11. pending_add_destination_clarification has priority over an undo
    command."""

    def test_add_destination_clarification_wins(self):
        chat_id = 9
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_add_destination_clarification={chat_id: {}}),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Скасувати останню дію")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_add_destination_clarification.assert_called_once_with(
            chat_id, "Скасувати останню дію"
        )
        deps.pending_routes.start_undo_flow.assert_not_called()


class TestUndoRouting(unittest.TestCase):
    """12. pending_undo_action and a plain undo command both reach the
    existing undo handling."""

    def test_pending_undo_action_sends_pending_undo_message(self):
        chat_id = 10
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_undo_action={chat_id: {}}),
        )
        with patch.object(message_dispatcher.action_history, "PENDING_UNDO_MSG", "PENDING_UNDO"):
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "будь-що")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.send_message.assert_called_once_with(chat_id, "PENDING_UNDO")
        deps.pending_routes.start_undo_flow.assert_not_called()

    def test_plain_undo_command_starts_undo_flow(self):
        chat_id = 11
        deps = _make_fake_dispatcher_deps()
        with patch.object(message_dispatcher.action_history, "is_undo_command", return_value=True):
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Скасувати останню дію")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.start_undo_flow.assert_called_once_with(chat_id, 555, "Тест")


class TestExactUndoButtonCancelsActivePendingOperation(unittest.TestCase):
    """The exact undo button label — with or without the U+FE0F variation
    selector — cancels an active pending clarification/preview via the
    injected has_active_pending_operation/cancel_active_pending_operation
    callbacks, instead of ever reaching historical undo. Only when has_
    active_pending_operation reports nothing active does the button fall
    through to plain undo routing. A plain, non-button reply must still
    reach the clarification handler unchanged."""

    def test_button_with_variation_selector_cancels_quantity_clarification(self):
        chat_id = 20
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_inventory_quantity_clarification={chat_id: {}},
                has_active_pending_operation=MagicMock(return_value=True),
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "↩️ Скасувати останню дію")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_inventory_quantity_clarification.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_not_called()
        deps.pending_routes.cancel_active_pending_operation.assert_called_once_with(chat_id)

    def test_button_without_variation_selector_cancels_quantity_clarification(self):
        chat_id = 21
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_inventory_quantity_clarification={chat_id: {}},
                has_active_pending_operation=MagicMock(return_value=True),
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "↩ Скасувати останню дію")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_inventory_quantity_clarification.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_not_called()
        deps.pending_routes.cancel_active_pending_operation.assert_called_once_with(chat_id)

    def test_ordinary_reply_still_reaches_quantity_clarification(self):
        chat_id = 22
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_inventory_quantity_clarification={chat_id: {}},
                has_active_pending_operation=MagicMock(return_value=True),
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "1 л")
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_inventory_quantity_clarification.assert_called_once_with(chat_id, "1 л")
        deps.pending_routes.cancel_active_pending_operation.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_not_called()

    def test_button_cancels_representation_clarification(self):
        chat_id = 23
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_inventory_representation_clarification={chat_id: {}},
                has_active_pending_operation=MagicMock(return_value=True),
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", message_dispatcher.action_history.UNDO_BUTTON_TEXT)
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.continue_inventory_representation_clarification.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_not_called()
        deps.pending_routes.cancel_active_pending_operation.assert_called_once_with(chat_id)

    def test_button_cancels_global_household_preview(self):
        chat_id = 24
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                pending_global_household={chat_id: {}},
                has_active_pending_operation=MagicMock(return_value=True),
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", message_dispatcher.action_history.UNDO_BUTTON_TEXT)
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.send_message.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_not_called()
        deps.pending_routes.cancel_active_pending_operation.assert_called_once_with(chat_id)

    def test_button_without_active_operation_falls_through_to_historical_undo(self):
        chat_id = 25
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(has_active_pending_operation=MagicMock(return_value=False)),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", message_dispatcher.action_history.UNDO_BUTTON_TEXT)
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.pending_routes.cancel_active_pending_operation.assert_not_called()
        deps.pending_routes.start_undo_flow.assert_called_once_with(chat_id, 555, "Тест")

    def test_button_still_blocked_by_active_expense_preview(self):
        """Active expense preview is checked earlier in the route order,
        well before has_active_pending_operation is ever consulted — same
        as the existing generic-undo-phrase behavior in
        TestActiveExpensePreviewBlocksUndo."""
        chat_id = 26
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(
                has_active_expense_preview=MagicMock(return_value=True),
                has_active_pending_operation=MagicMock(return_value=True),
            ),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", message_dispatcher.action_history.UNDO_BUTTON_TEXT)
        self.assertEqual(result, message_dispatcher.RouteOutcome.HANDLED)
        deps.send_message.assert_called_once_with(chat_id, "EXPENSE_PREVIEW_GUARD")
        deps.pending_routes.start_undo_flow.assert_not_called()
        deps.pending_routes.cancel_active_pending_operation.assert_not_called()


class TestUnhandledTextReturnsFalse(unittest.TestCase):
    """13. Unrecognized text returns RouteOutcome.CONTINUE (webhook falls
    through to the old lower router) and never calls send_message."""

    def test_unknown_text_not_handled(self):
        deps = _make_fake_dispatcher_deps()
        result = message_dispatcher.dispatch(deps, 12, 555, "Тест", "яка сьогодні погода?")
        self.assertEqual(result, message_dispatcher.RouteOutcome.CONTINUE)
        deps.send_message.assert_not_called()


class TestNoDirectDbAccess(unittest.TestCase):
    """15. Dispatcher never writes to the DB directly."""

    def test_no_db_write_function_names_in_module_source(self):
        source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "message_dispatcher.py")
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()
        forbidden_calls = (
            "add_shopping_items_batch", "add_inventory_items_batch", "update_shopping_items_batch",
            "update_inventory_items_batch", "delete_items_batch", "delete_inventory_items_batch",
            "apply_inventory_consumption", "apply_compound_inventory_operations",
            "apply_inventory_reconciliation", "execute_merge_shopping", "execute_merge_inventory",
            "add_expense", "delete_expense", "apply_undo_action", "apply_global_household_operations",
        )
        for name in forbidden_calls:
            self.assertNotIn(name, source)


def _make_update(chat_id, text, user_id=555, update_id=None):
    return {
        "update_id": update_id if update_id is not None else chat_id * 1000,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class TestWebhookIntegration(unittest.TestCase):
    """14/16. Runtime lambda callbacks are visible through patch.object(bot,
    ...) at the webhook level, and a shared confirm/cancel button never
    reaches Dispatcher V2A."""

    def setUp(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_merge.clear()
        bot.pending_global_household.clear()
        bot.pending_undo_action.clear()

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_gemini = patch.object(bot, "call_gemini", return_value=None)
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

        patcher_undoable = patch.object(bot, "get_latest_undoable_action", return_value=None)
        self.mock_undoable = patcher_undoable.start()
        self.addCleanup(patcher_undoable.stop)

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_merge.clear()
        bot.pending_global_household.clear()
        bot.pending_undo_action.clear()

    def test_pending_global_household_guard_uses_patched_send_message(self):
        chat_id = 9201
        bot.pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(chat_id, "Купив молоко за 10 zł"))
        self.mock_send.assert_called_once_with(chat_id, bot.GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG)
        # Pending Preview Edit Planner: every deterministic preview-edit
        # handler fails for this text (no addable items/expenses to even
        # target), and this pending preview has NOTHING patchable at all
        # (no shopping/inventory/expense rows to build a target id for) —
        # V2 skips the Gemini call entirely in that case (a pure cost
        # optimization; the result is always "no_change" either way), so
        # the guard fires without ever calling Gemini.
        self.mock_gemini.assert_not_called()

    def test_confirm_cancel_button_reaches_dispatch_exactly_once(self):
        # As of Dispatcher V3B, confirm/cancel is routed THROUGH dispatch()
        # (its own top-priority route) rather than being intercepted by an
        # inline webhook() branch before dispatch() is ever called — see
        # test_message_dispatcher_confirm_cancel.py for the dedicated V3B
        # contract tests.
        chat_id = 9202
        bot.pending_merge[chat_id] = {
            "groups": [], "household_id": 1, "user_db_id": 10, "list_type": "shopping_saved",
        }
        with patch.object(message_dispatcher, "dispatch") as mock_dispatch:
            _call_webhook(_make_update(chat_id, "✅ Об'єднати"))
            mock_dispatch.assert_called_once_with(bot._dispatcher_deps, chat_id, 555, "Тест", "✅ Об'єднати")

    def test_undo_command_reaches_dispatch_and_starts_undo_flow(self):
        chat_id = 9203
        with patch.object(message_dispatcher, "dispatch", wraps=message_dispatcher.dispatch) as spy:
            _call_webhook(_make_update(chat_id, "Скасувати останню дію"))
            spy.assert_called_once_with(bot._dispatcher_deps, chat_id, 555, "Тест", "Скасувати останню дію")
        self.mock_undoable.assert_called_once()


if __name__ == "__main__":
    unittest.main()
