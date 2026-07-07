"""Message Dispatcher V3A — special buttons, cooking mode, general AI
fallback (Phase D ownership).

Does NOT re-test the underlying business logic of aliases/expenses intro
texts, cooking-mode Gemini calls, or the general AI-chat conversation loop
(already covered by test_household_language_contract.py and the webhook-
level integration tests already living in test_message_dispatcher_module.py/
test_message_dispatcher_pending_routes.py/test_message_dispatcher_command_
routes.py, which keep passing unchanged — see the module-boundary-only
edits explained in this refactor's commit message). This file only asserts:
the special-button route's priority (above shopping/inventory mode, below
navigation), the exact five texts it fires for, and dispatch()'s new Phase D
ownership (DIRECT_GENERAL_AI_FALLBACK skips cooking mode; CONTINUE tries
cooking mode first and only calls the fallback once if that declines).

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
    """A DispatcherDeps with cooking_mode/special_button WIRED (unlike the
    pre-V3A fake-deps builders in the sibling test files) — this is what
    exercises the full V3A Phase-D-owning dispatch() behavior."""
    defaults = dict(
        send_message=MagicMock(),
        clear_interaction_state=MagicMock(),
        main_keyboard={"keyboard": "main"},
        help_text="HELP_TEXT",
        shopping_deps=_make_fake_shopping_deps(),
        inventory_deps=_make_fake_inventory_deps(),
        pending_routes=_make_fake_pending_route_deps(),
        command_routes=_make_fake_command_route_deps(),
        special_button=MagicMock(return_value=False),
        cooking_mode=MagicMock(return_value=False),
    )
    defaults.update(overrides)
    return message_dispatcher.DispatcherDeps(**defaults)


class TestModuleBoundary(unittest.TestCase):
    """10. message_dispatcher.py does not import bot.py or database.py."""

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


class TestSpecialButtonOutranksShoppingMode(unittest.TestCase):
    """1. Special button route has priority over active shopping_mode."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()

    def test_special_button_wins_over_shopping_mode(self):
        chat_id = 1
        legacy_shopping_flow.shopping_mode[chat_id] = "adding"
        deps = _make_fake_dispatcher_deps(special_button=MagicMock(return_value=True))
        with patch.object(legacy_shopping_flow, "handle_shopping_mode_text") as mock_shop:
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "💸 Витрати")
        self.assertEqual(result, RouteOutcome.HANDLED)
        deps.special_button.assert_called_once_with(chat_id, 555, "Тест", "💸 Витрати")
        mock_shop.assert_not_called()


class TestSpecialButtonOutranksInventoryMode(unittest.TestCase):
    """2. Special button route has priority over active inventory_mode."""

    def tearDown(self):
        legacy_inventory_flow.inventory_mode.clear()

    def test_special_button_wins_over_inventory_mode(self):
        chat_id = 2
        legacy_inventory_flow.inventory_mode[chat_id] = "removing"
        deps = _make_fake_dispatcher_deps(special_button=MagicMock(return_value=True))
        with patch.object(legacy_inventory_flow, "handle_inventory_mode_text") as mock_inv:
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "🍽️ Що приготувати")
        self.assertEqual(result, RouteOutcome.HANDLED)
        deps.special_button.assert_called_once_with(chat_id, 555, "Тест", "🍽️ Що приготувати")
        mock_inv.assert_not_called()


class TestAllFiveSpecialTextsCallInjectedCallback(unittest.TestCase):
    """3. All five special texts call the injected special_button callback."""

    def test_each_special_text_invokes_callback(self):
        texts = (
            "🧠 Назви товарів", "📋 Показати назви", "💸 Витрати",
            "🍽️ Що приготувати", "ℹ️ Допомога",
        )
        for i, text in enumerate(texts):
            with self.subTest(text=text):
                special_button = MagicMock(return_value=True)
                deps = _make_fake_dispatcher_deps(special_button=special_button)
                result = message_dispatcher.dispatch(deps, 100 + i, 555, "Тест", text)
                self.assertEqual(result, RouteOutcome.HANDLED)
                special_button.assert_called_once_with(100 + i, 555, "Тест", text)


class TestUnknownTextDoesNotCallSpecialButton(unittest.TestCase):
    """4. Unknown text does not call the special_button callback."""

    def test_unrecognized_text_skips_special_button(self):
        special_button = MagicMock(return_value=False)
        deps = _make_fake_dispatcher_deps(special_button=special_button)
        message_dispatcher.dispatch(deps, 3, 555, "Тест", "яка сьогодні погода?")
        special_button.assert_called_once_with(3, 555, "Тест", "яка сьогодні погода?")


class TestDirectGeneralAiFallbackSkipsCookingMode(unittest.TestCase):
    """5/6. DIRECT_GENERAL_AI_FALLBACK never calls cooking_mode and calls
    general_ai_fallback exactly once."""

    def test_pending_batch_none_intent_skips_cooking_mode(self):
        chat_id = 4
        cooking_mode = MagicMock(return_value=False)
        general_ai_fallback = MagicMock()
        deps = _make_fake_dispatcher_deps(
            pending_routes=_make_fake_pending_route_deps(pending_batch={chat_id: {}}),
            command_routes=_make_fake_command_route_deps(general_ai_fallback=general_ai_fallback),
            cooking_mode=cooking_mode,
        )
        with patch.object(legacy_shopping_flow, "handle_pending_batch_edit_text", return_value=False):
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "щось")
        self.assertEqual(result, RouteOutcome.HANDLED)
        cooking_mode.assert_not_called()
        general_ai_fallback.assert_called_once_with(chat_id, "щось")


class TestContinueTriesCookingModeFirst(unittest.TestCase):
    """7. CONTINUE calls try_handle_cooking_mode first."""

    def test_cooking_mode_called_for_continue(self):
        chat_id = 5
        cooking_mode = MagicMock(return_value=False)
        deps = _make_fake_dispatcher_deps(cooking_mode=cooking_mode)
        message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "молоко, яйця")
        cooking_mode.assert_called_once_with(chat_id, 555, "Тест", "молоко, яйця")


class TestCookingModeHandledSkipsFallback(unittest.TestCase):
    """8. If cooking_mode returns True, general_ai_fallback is not called."""

    def test_fallback_not_called_when_cooking_mode_handles_it(self):
        chat_id = 6
        cooking_mode = MagicMock(return_value=True)
        general_ai_fallback = MagicMock()
        deps = _make_fake_dispatcher_deps(
            command_routes=_make_fake_command_route_deps(general_ai_fallback=general_ai_fallback),
            cooking_mode=cooking_mode,
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "молоко, яйця")
        self.assertEqual(result, RouteOutcome.HANDLED)
        general_ai_fallback.assert_not_called()


class TestCookingModeDeclinedCallsFallbackOnce(unittest.TestCase):
    """9. If cooking_mode returns False, general_ai_fallback is called
    exactly once."""

    def test_fallback_called_once_when_cooking_mode_declines(self):
        chat_id = 7
        cooking_mode = MagicMock(return_value=False)
        general_ai_fallback = MagicMock()
        deps = _make_fake_dispatcher_deps(
            command_routes=_make_fake_command_route_deps(general_ai_fallback=general_ai_fallback),
            cooking_mode=cooking_mode,
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "яка сьогодні погода?")
        self.assertEqual(result, RouteOutcome.HANDLED)
        general_ai_fallback.assert_called_once_with(chat_id, "яка сьогодні погода?")


class TestNoDirectDbAccess(unittest.TestCase):
    """11. Dispatcher never writes to the DB directly."""

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
    """12/13/14/15/16. Runtime lambda callbacks are visible through
    patch.object(bot, "_try_handle_special_button"/"_try_handle_cooking_
    mode", ...) at the webhook level; confirm/cancel never reaches
    special-button or cooking routes; webhook() calls dispatch() exactly
    once with no inline special/cooking/fallback route branches of its
    own; /myid and access check stay above the dispatcher."""

    def setUp(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_merge.clear()
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

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_merge.clear()
        bot.waiting_for_ingredients.clear()

    def test_patched_special_button_visible_through_dispatcher_deps(self):
        chat_id = 9401
        with patch.object(bot, "_try_handle_special_button", return_value=True) as mock_special:
            _call_webhook(_make_update(chat_id, "💸 Витрати"))
            mock_special.assert_called_once_with(chat_id, 555, "Тест", "💸 Витрати")

    def test_patched_cooking_mode_visible_through_dispatcher_deps(self):
        chat_id = 9402
        with patch.object(bot, "_try_handle_cooking_mode", return_value=True) as mock_cooking:
            _call_webhook(_make_update(chat_id, "молоко, яйця, борошно"))
            mock_cooking.assert_called_once_with(chat_id, 555, "Тест", "молоко, яйця, борошно")

    def test_confirm_cancel_button_never_reaches_special_or_cooking_route(self):
        chat_id = 9403
        bot.pending_merge[chat_id] = {
            "groups": [], "household_id": 1, "user_db_id": 10, "list_type": "shopping_saved",
        }
        with patch.object(bot, "_try_handle_special_button") as mock_special, \
                patch.object(bot, "_try_handle_cooking_mode") as mock_cooking:
            _call_webhook(_make_update(chat_id, "✅ Об'єднати"))
            mock_special.assert_not_called()
            mock_cooking.assert_not_called()

    def test_webhook_calls_dispatch_exactly_once_with_no_inline_route_branches(self):
        chat_id = 9404
        with patch.object(message_dispatcher, "dispatch", wraps=message_dispatcher.dispatch) as spy:
            _call_webhook(_make_update(chat_id, "💸 Витрати"))
            spy.assert_called_once_with(bot._dispatcher_deps, chat_id, 555, "Тест", "💸 Витрати")
        # Special-button behavior itself (state/keyboard/message) still
        # happens — just now reached only via dispatch(), never via an
        # inline webhook() branch above it.
        self.mock_send.assert_called_once()

    def test_myid_and_access_check_stay_above_dispatcher(self):
        chat_id = 9405
        with patch.object(message_dispatcher, "dispatch") as mock_dispatch:
            _call_webhook(_make_update(chat_id, "/myid"))
            mock_dispatch.assert_not_called()
        self.mock_send.assert_called_once()
        sent_text = self.mock_send.call_args.args[1]
        self.assertIn(str(555), sent_text)


if __name__ == "__main__":
    unittest.main()
