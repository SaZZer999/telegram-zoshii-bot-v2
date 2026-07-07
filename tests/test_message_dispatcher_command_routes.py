"""Message Dispatcher V2B — command/context routes + RouteOutcome contract.

Does NOT re-test the underlying business logic of explicit/bare add, the
Global Household Router, expense reports/delete, aliases/expenses commands,
or the saved-list router (already covered by test_global_household_
operations.py, test_global_explicit_add.py, test_global_bare_add.py,
test_expense_delete.py, test_saved_list_ai_router.py and friends, and keeps
passing unchanged against the extracted routes). This file only asserts:
the RouteOutcome contract itself (HANDLED/CONTINUE/DIRECT_GENERAL_AI_
FALLBACK), module boundary, CommandRouteDeps nesting, and the exact
PRIORITY ORDER of Dispatcher V2B's eleven command/context routes (old
Phase C routes 16-26) against a plain fake CommandRouteDeps.

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
from message_dispatcher import RouteOutcome  # noqa: E402


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
    )
    defaults.update(overrides)
    return message_dispatcher.PendingRouteDeps(**defaults)


def _make_fake_command_route_deps(**overrides):
    defaults = dict(
        ambiguous_add_route=MagicMock(return_value=False),
        explicit_global_add=MagicMock(return_value=False),
        bare_global_add=MagicMock(return_value=False),
        global_household_router=MagicMock(return_value=False),
        expense_report_route=MagicMock(return_value=False),
        expense_delete_command_route=MagicMock(return_value=False),
        active_aliases_context=MagicMock(return_value=None),
        global_alias_command=MagicMock(return_value=False),
        active_expenses_context=MagicMock(return_value=None),
        global_expense_command=MagicMock(return_value=False),
        saved_list_router=MagicMock(return_value=False),
        general_ai_fallback=MagicMock(),
    )
    defaults.update(overrides)
    return message_dispatcher.CommandRouteDeps(**defaults)


def _make_fake_dispatcher_deps(**overrides):
    defaults = dict(
        send_message=MagicMock(),
        clear_interaction_state=MagicMock(),
        main_keyboard={"keyboard": "main"},
        help_text="HELP_TEXT",
        shopping_deps=_make_fake_shopping_deps(),
        inventory_deps=_make_fake_inventory_deps(),
        pending_routes=_make_fake_pending_route_deps(),
        command_routes=_make_fake_command_route_deps(),
    )
    defaults.update(overrides)
    return message_dispatcher.DispatcherDeps(**defaults)


class TestModuleBoundary(unittest.TestCase):
    """14. message_dispatcher.py does not import bot.py/database.py."""

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


class TestCommandRouteDepsNesting(unittest.TestCase):
    """CommandRouteDeps is a small nested container, not a flat explosion
    of new DispatcherDeps fields."""

    def test_command_routes_is_nested_container(self):
        deps = _make_fake_dispatcher_deps()
        self.assertIsInstance(deps.command_routes, message_dispatcher.CommandRouteDeps)
        dispatcher_fields = set(message_dispatcher.DispatcherDeps.__dataclass_fields__.keys())
        command_route_fields = set(message_dispatcher.CommandRouteDeps.__dataclass_fields__.keys())
        self.assertFalse(dispatcher_fields & command_route_fields)
        self.assertIn("command_routes", dispatcher_fields)


class TestRouteOutcomeContract(unittest.TestCase):
    """1. dispatch() returns a RouteOutcome, not a bool."""

    def test_dispatch_returns_route_outcome_instance(self):
        deps = _make_fake_dispatcher_deps()
        result = message_dispatcher.dispatch(deps, 1, 555, "Тест", "будь-що")
        self.assertIsInstance(result, RouteOutcome)


class TestPendingBatchNoneReturnsDirectFallback(unittest.TestCase):
    """2. pending shopping batch with intent "none" returns
    DIRECT_GENERAL_AI_FALLBACK."""

    def test_shopping_batch_none_intent(self):
        chat_id = 2
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_batch={chat_id: {}}),
        )
        with patch.object(legacy_shopping_flow, "handle_pending_batch_edit_text", return_value=False):
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "щось")
        self.assertEqual(result, RouteOutcome.DIRECT_GENERAL_AI_FALLBACK)


class TestPendingInventoryBatchNoneReturnsDirectFallback(unittest.TestCase):
    """3. pending inventory batch with intent "none" returns
    DIRECT_GENERAL_AI_FALLBACK."""

    def test_inventory_batch_none_intent(self):
        chat_id = 3
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_inventory_batch={chat_id: {}}),
        )
        with patch.object(legacy_inventory_flow, "handle_pending_inventory_batch_edit_text", return_value=False):
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "щось")
        self.assertEqual(result, RouteOutcome.DIRECT_GENERAL_AI_FALLBACK)


class TestDirectFallbackSkipsEveryCommandRoute(unittest.TestCase):
    """4. DIRECT_GENERAL_AI_FALLBACK never invokes ambiguous/explicit/bare
    add, the Global Household Router, aliases, expenses, or the saved-list
    router. 5. The preview stays untouched (no DB-write helper is even
    reachable — the fake deps carry only MagicMocks, none of which are
    called)."""

    def test_no_command_route_callback_invoked(self):
        chat_id = 4
        command_routes = _make_fake_command_route_deps()
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_batch={chat_id: {}}),
            command_routes=command_routes,
        )
        with patch.object(legacy_shopping_flow, "handle_pending_batch_edit_text", return_value=False):
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Купив молоко за 10 zł")
        self.assertEqual(result, RouteOutcome.DIRECT_GENERAL_AI_FALLBACK)
        command_routes.ambiguous_add_route.assert_not_called()
        command_routes.explicit_global_add.assert_not_called()
        command_routes.bare_global_add.assert_not_called()
        command_routes.global_household_router.assert_not_called()
        command_routes.active_aliases_context.assert_not_called()
        command_routes.global_alias_command.assert_not_called()
        command_routes.active_expenses_context.assert_not_called()
        command_routes.global_expense_command.assert_not_called()
        command_routes.saved_list_router.assert_not_called()
        # The dispatcher itself never calls the AI fallback — bot.py does,
        # only after receiving RouteOutcome.DIRECT_GENERAL_AI_FALLBACK.
        command_routes.general_ai_fallback.assert_not_called()


class TestUnknownTextWithoutPendingStateReturnsContinue(unittest.TestCase):
    """6. Unknown text without any active pending state returns CONTINUE."""

    def test_continue_returned(self):
        deps = _make_fake_dispatcher_deps()
        result = message_dispatcher.dispatch(deps, 5, 555, "Тест", "яка сьогодні погода?")
        self.assertEqual(result, RouteOutcome.CONTINUE)
        deps.send_message.assert_not_called()


class TestAmbiguousAddOutranksExplicitAdd(unittest.TestCase):
    """7. Ambiguous add has priority over explicit add."""

    def test_ambiguous_add_wins(self):
        chat_id = 6
        command_routes = _make_fake_command_route_deps(
            ambiguous_add_route=MagicMock(return_value=True),
            explicit_global_add=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Додай молоко за 10 zł")
        self.assertEqual(result, RouteOutcome.HANDLED)
        command_routes.ambiguous_add_route.assert_called_once()
        command_routes.explicit_global_add.assert_not_called()


class TestExplicitAddOutranksBareAdd(unittest.TestCase):
    """8. Explicit add has priority over bare add."""

    def test_explicit_add_wins(self):
        chat_id = 7
        command_routes = _make_fake_command_route_deps(
            explicit_global_add=MagicMock(return_value=True),
            bare_global_add=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Додай до покупок молоко")
        self.assertEqual(result, RouteOutcome.HANDLED)
        command_routes.explicit_global_add.assert_called_once()
        command_routes.bare_global_add.assert_not_called()


class TestGlobalHouseholdRouterOutranksLowerGates(unittest.TestCase):
    """9. Global Household Router has priority over expense report/delete/
    alias/expense gates."""

    def test_household_router_wins(self):
        chat_id = 8
        command_routes = _make_fake_command_route_deps(
            global_household_router=MagicMock(return_value=True),
            expense_report_route=MagicMock(return_value=True),
            expense_delete_command_route=MagicMock(return_value=True),
            global_alias_command=MagicMock(return_value=True),
            global_expense_command=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Купив молоко")
        self.assertEqual(result, RouteOutcome.HANDLED)
        command_routes.global_household_router.assert_called_once()
        command_routes.expense_report_route.assert_not_called()
        command_routes.expense_delete_command_route.assert_not_called()
        command_routes.global_alias_command.assert_not_called()
        command_routes.global_expense_command.assert_not_called()


class TestExpenseReportOutranksExpenseDeleteCommand(unittest.TestCase):
    """10. Expense report has priority over expense delete command."""

    def test_expense_report_wins(self):
        chat_id = 9
        command_routes = _make_fake_command_route_deps(
            expense_report_route=MagicMock(return_value=True),
            expense_delete_command_route=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Покажи останні витрати")
        self.assertEqual(result, RouteOutcome.HANDLED)
        command_routes.expense_report_route.assert_called_once()
        command_routes.expense_delete_command_route.assert_not_called()


class TestActiveAliasesContextOutranksGlobalAliasCommand(unittest.TestCase):
    """11. Active aliases context has priority over the global alias
    command gate."""

    def test_active_aliases_context_wins_when_handled(self):
        chat_id = 10
        command_routes = _make_fake_command_route_deps(
            active_aliases_context=MagicMock(return_value=True),
            global_alias_command=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Молоко -> Mleko")
        self.assertEqual(result, RouteOutcome.HANDLED)
        command_routes.active_aliases_context.assert_called_once()
        command_routes.global_alias_command.assert_not_called()

    def test_active_aliases_context_declines_still_stops_chain(self):
        """Matching the context but the handler reporting intent "none"
        must still stop the whole command-route chain (CONTINUE), never
        fall through to the global alias gate — same as the old elif chain
        where matching active_list_context == "aliases" already claimed the
        branch regardless of _handle_alias_command's own result."""
        chat_id = 11
        command_routes = _make_fake_command_route_deps(
            active_aliases_context=MagicMock(return_value=False),
            global_alias_command=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "якийсь текст")
        self.assertEqual(result, RouteOutcome.CONTINUE)
        command_routes.global_alias_command.assert_not_called()


class TestActiveExpensesContextOutranksGlobalExpenseCommand(unittest.TestCase):
    """12. Active expenses context has priority over the global expense
    command gate."""

    def test_active_expenses_context_wins_when_handled(self):
        chat_id = 12
        command_routes = _make_fake_command_route_deps(
            active_expenses_context=MagicMock(return_value=True),
            global_expense_command=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Молоко 10 zł")
        self.assertEqual(result, RouteOutcome.HANDLED)
        command_routes.active_expenses_context.assert_called_once()
        command_routes.global_expense_command.assert_not_called()

    def test_active_expenses_context_declines_still_stops_chain(self):
        chat_id = 13
        command_routes = _make_fake_command_route_deps(
            active_expenses_context=MagicMock(return_value=False),
            global_expense_command=MagicMock(return_value=True),
        )
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "якийсь текст")
        self.assertEqual(result, RouteOutcome.CONTINUE)
        command_routes.global_expense_command.assert_not_called()


class TestSavedListRouterIsLastCommandRoute(unittest.TestCase):
    """13. Saved-list router is the last command route — nothing else is
    checked after it, and if it declines the overall result is CONTINUE."""

    def test_saved_list_router_checked_last(self):
        chat_id = 14
        command_routes = _make_fake_command_route_deps(saved_list_router=MagicMock(return_value=True))
        deps = _make_fake_dispatcher_deps(command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "2 хліби")
        self.assertEqual(result, RouteOutcome.HANDLED)
        command_routes.saved_list_router.assert_called_once()

    def test_saved_list_router_decline_returns_continue(self):
        chat_id = 15
        deps = _make_fake_dispatcher_deps()
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "2 хліби")
        self.assertEqual(result, RouteOutcome.CONTINUE)


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
            "create_or_update_household_alias", "delete_household_alias",
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
    """16/17/18/19. Runtime lambda callbacks are visible through
    patch.object(bot, ...) at the webhook level, confirm/cancel and special
    buttons above the dispatcher never reach command routes, cooking mode/
    AI fallback behavior is unchanged for CONTINUE, and the existing
    routing precedence contract stays green (covered separately by
    test_routing_precedence_contract.py, run in the same check suite)."""

    def setUp(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_merge.clear()
        bot.pending_global_household.clear()
        bot.waiting_for_ingredients.clear()

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_gemini = patch.object(bot, "call_gemini", return_value=None)
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        self.mock_alias_map = patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_shopping_items = patch.object(bot, "get_active_shopping_items", return_value=[])
        self.mock_shopping_items = patcher_shopping_items.start()
        self.addCleanup(patcher_shopping_items.stop)

        patcher_inventory_items = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory_items = patcher_inventory_items.start()
        self.addCleanup(patcher_inventory_items.stop)

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_merge.clear()
        bot.pending_global_household.clear()
        bot.waiting_for_ingredients.clear()

    def test_confirm_cancel_button_never_reaches_dispatch(self):
        chat_id = 9301
        bot.pending_merge[chat_id] = {
            "groups": [], "household_id": 1, "user_db_id": 10, "list_type": "shopping_saved",
        }
        with patch.object(message_dispatcher, "dispatch") as mock_dispatch:
            _call_webhook(_make_update(chat_id, "✅ Об'єднати"))
            mock_dispatch.assert_not_called()

    def test_special_button_above_dispatcher_never_reaches_command_routes(self):
        chat_id = 9302
        with patch.object(bot, "_route_ambiguous_add") as mock_route, \
                patch.object(bot, "_route_global_household") as mock_household:
            _call_webhook(_make_update(chat_id, "💸 Витрати"))
            mock_route.assert_not_called()
            mock_household.assert_not_called()

    def test_plain_message_reaches_ai_fallback_via_patched_send_message(self):
        chat_id = 9303
        _call_webhook(_make_update(chat_id, "Привіт, як справи?"))
        self.mock_send.assert_called_once()
        sent_text = self.mock_send.call_args.args[1]
        self.assertEqual(sent_text, "AI-помічник тимчасово недоступний. Спробуйте ще раз трохи пізніше.")

    def test_cooking_mode_still_checked_before_ai_fallback_for_continue(self):
        chat_id = 9304
        bot.waiting_for_ingredients[chat_id] = True
        _call_webhook(_make_update(chat_id, "молоко, яйця, борошно"))
        self.mock_send.assert_called_once()
        self.assertNotIn(chat_id, bot.waiting_for_ingredients)

    def test_direct_general_ai_fallback_skips_cooking_mode(self):
        """A pending shopping batch with Gemini intent "none" must reach
        general AI-chat directly, even if waiting_for_ingredients also
        happens to be set — cooking mode must never fire in that case.
        waiting_for_ingredients[chat_id] is deliberately left untouched
        (True) by this path: cooking mode's own `.pop(...)` call is the only
        thing that would ever clear it, and that code must never run here."""
        chat_id = 9305
        bot.waiting_for_ingredients[chat_id] = True
        legacy_shopping_flow.pending_batch[chat_id] = {
            "items": [{"id": None, "name": "Хліб", "category": "Інше їстівне"}],
            "ignored_items": [], "household_id": 1, "user_db_id": 10,
        }
        try:
            with patch.object(legacy_shopping_flow, "handle_pending_batch_edit_text", return_value=False), \
                    patch.object(bot, "_run_general_ai_fallback") as mock_fallback:
                _call_webhook(_make_update(chat_id, "яка сьогодні погода?"))
        finally:
            legacy_shopping_flow.pending_batch.pop(chat_id, None)
        # Cooking mode was never reached: waiting_for_ingredients still True.
        self.assertTrue(bot.waiting_for_ingredients.get(chat_id))
        mock_fallback.assert_called_once_with(chat_id, "яка сьогодні погода?")
        self.mock_gemini.assert_not_called()


if __name__ == "__main__":
    unittest.main()
