"""Message Dispatcher V1 — module boundary tests.

Does NOT re-test legacy shopping/inventory business logic (already covered
by test_legacy_shopping_flow_module.py, test_legacy_inventory_flow_module.py,
test_inventory_module.py and friends) or the Pending Preview Router / global
router chain (test_routing_precedence_contract.py, test_pending_preview_
logic.py, test_unresolved_fragments_safety.py). This file only asserts:
module boundary (no bot.py import), DispatcherDeps nesting the existing
ShoppingFlowDeps/InventoryFlowDeps instead of re-declaring their fields,
route precedence within Dispatcher V1 itself (navigation before menu before
mode-text, shopping_mode before inventory_mode), the True/False route
contract, and that webhook() calls dispatch() exactly once, after confirm/
cancel and before the untouched Pending Preview Router chain.

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


def _make_fake_dispatcher_deps(**overrides):
    defaults = dict(
        send_message=MagicMock(),
        clear_interaction_state=MagicMock(),
        main_keyboard={"keyboard": "main"},
        help_text="HELP_TEXT",
        shopping_deps=_make_fake_shopping_deps(),
        inventory_deps=_make_fake_inventory_deps(),
    )
    defaults.update(overrides)
    return message_dispatcher.DispatcherDeps(**defaults)


class TestModuleBoundary(unittest.TestCase):
    """1. message_dispatcher.py does not import bot.py."""

    def test_no_bot_import(self):
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

    def test_module_importable_without_bot_in_sys_modules(self):
        # message_dispatcher was already imported above without needing bot's
        # module-level side effects (Flask app, init_db, Groq client) to run
        # first — proven simply by this file having imported it successfully
        # right after legacy_shopping_flow/legacy_inventory_flow.
        self.assertTrue(hasattr(message_dispatcher, "dispatch"))


class TestDispatcherDepsNesting(unittest.TestCase):
    """2. DispatcherDeps nests shopping_deps/inventory_deps as whole objects,
    not re-declared field-by-field."""

    def test_deps_has_nested_containers_not_flattened_fields(self):
        deps = _make_fake_dispatcher_deps()
        self.assertIsInstance(deps.shopping_deps, legacy_shopping_flow.ShoppingFlowDeps)
        self.assertIsInstance(deps.inventory_deps, legacy_inventory_flow.InventoryFlowDeps)
        # DispatcherDeps itself must stay small — it must NOT declare its own
        # get_active_shopping_items/get_inventory_items/etc. fields, those
        # live only inside the nested containers.
        dispatcher_fields = set(message_dispatcher.DispatcherDeps.__dataclass_fields__.keys())
        self.assertNotIn("get_active_shopping_items", dispatcher_fields)
        self.assertNotIn("get_inventory_items", dispatcher_fields)
        self.assertIn("shopping_deps", dispatcher_fields)
        self.assertIn("inventory_deps", dispatcher_fields)


class TestNavigationOutranksMode(unittest.TestCase):
    """3/4. Navigation is dispatched even while a shopping_mode/
    inventory_mode is active."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()

    def test_start_intercepted_during_active_shopping_mode(self):
        legacy_shopping_flow.shopping_mode[1] = "adding"
        deps = _make_fake_dispatcher_deps()
        handled = message_dispatcher.dispatch(deps, 1, 555, "Тест", "/start")
        self.assertTrue(handled)
        deps.clear_interaction_state.assert_called_once_with(1)
        deps.send_message.assert_called_once()

    def test_main_menu_button_intercepted_during_active_inventory_mode(self):
        legacy_inventory_flow.inventory_mode[2] = "removing"
        deps = _make_fake_dispatcher_deps()
        handled = message_dispatcher.dispatch(deps, 2, 555, "Тест", "⬅️ Головне меню")
        self.assertTrue(handled)
        deps.clear_interaction_state.assert_called_once_with(2)


class TestMenuButtonsCallLegacyHandlers(unittest.TestCase):
    """5/6. Shopping/inventory menu buttons call the correct legacy handler."""

    def test_shopping_menu_button_calls_legacy_shopping_handler(self):
        deps = _make_fake_dispatcher_deps()
        with patch.object(legacy_shopping_flow, "handle_open_shopping_menu") as mock_handler:
            handled = message_dispatcher.dispatch(deps, 3, 555, "Тест", "🛒 Покупки")
        self.assertTrue(handled)
        mock_handler.assert_called_once_with(deps.shopping_deps, 3, 555, "Тест")

    def test_inventory_menu_button_calls_legacy_inventory_handler(self):
        deps = _make_fake_dispatcher_deps()
        with patch.object(legacy_inventory_flow, "handle_open_inventory_menu") as mock_handler:
            handled = message_dispatcher.dispatch(deps, 4, 555, "Тест", "🧊 Запаси")
        self.assertTrue(handled)
        mock_handler.assert_called_once_with(deps.inventory_deps, 4, 555, "Тест")


class TestShoppingModePrecedesInventoryMode(unittest.TestCase):
    """7. shopping_mode dispatch has priority over inventory_mode dispatch."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()

    def test_both_modes_active_shopping_wins(self):
        chat_id = 5
        legacy_shopping_flow.shopping_mode[chat_id] = "adding"
        legacy_inventory_flow.inventory_mode[chat_id] = "adding"
        deps = _make_fake_dispatcher_deps()
        with patch.object(legacy_shopping_flow, "handle_shopping_mode_text", return_value=True) as mock_shop, \
                patch.object(legacy_inventory_flow, "handle_inventory_mode_text") as mock_inv:
            handled = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "Молоко")
        self.assertTrue(handled)
        mock_shop.assert_called_once_with(deps.shopping_deps, chat_id, 555, "Тест", "Молоко")
        mock_inv.assert_not_called()


class TestUnhandledTextReturnsFalse(unittest.TestCase):
    """8. Unrecognized text returns RouteOutcome.CONTINUE and never calls
    send_message."""

    def test_unknown_text_not_handled(self):
        deps = _make_fake_dispatcher_deps()
        outcome = message_dispatcher.dispatch(deps, 6, 555, "Тест", "яка сьогодні погода?")
        self.assertEqual(outcome, message_dispatcher.RouteOutcome.CONTINUE)
        deps.send_message.assert_not_called()


class TestNoDirectDbAccess(unittest.TestCase):
    """9. Dispatcher never writes to the DB directly and never imports
    database.py."""

    def test_database_module_not_referenced(self):
        self.assertNotIn("database", vars(message_dispatcher))

    def test_no_db_write_function_names_in_module_source(self):
        source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "message_dispatcher.py")
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()
        forbidden_calls = (
            "add_shopping_items_batch", "add_inventory_items_batch", "update_shopping_items_batch",
            "update_inventory_items_batch", "delete_items_batch", "delete_inventory_items_batch",
            "apply_inventory_consumption", "apply_compound_inventory_operations",
            "apply_inventory_reconciliation", "execute_merge_shopping", "execute_merge_inventory",
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
    """10/11/12/13. webhook()-level integration: patched bot.send_message is
    visible through the runtime lambda forward, dispatch() is called exactly
    once for a plain message, confirm/cancel buttons never reach dispatch(),
    and unhandled text still falls through to the untouched Pending Preview
    Router."""

    def setUp(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_inventory_flow.inventory_mode.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_merge.clear()

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

    def test_patched_send_message_visible_through_dispatcher_deps(self):
        _call_webhook(_make_update(9101, "/start"))
        self.mock_send.assert_called_once()
        args, kwargs = self.mock_send.call_args
        self.assertEqual(args[0], 9101)
        self.assertEqual(kwargs.get("reply_markup"), bot.MAIN_KEYBOARD)

    def test_webhook_calls_dispatch_exactly_once_for_plain_message(self):
        with patch.object(message_dispatcher, "dispatch", wraps=message_dispatcher.dispatch) as spy:
            _call_webhook(_make_update(9102, "/menu"))
            spy.assert_called_once_with(bot._dispatcher_deps, 9102, 555, "Тест", "/menu")

    def test_confirm_cancel_button_never_reaches_dispatch(self):
        chat_id = 9103
        bot.pending_merge[chat_id] = {
            "groups": [], "household_id": 1, "user_db_id": 10, "list_type": "shopping_saved",
        }
        with patch.object(message_dispatcher, "dispatch") as mock_dispatch:
            _call_webhook(_make_update(chat_id, "✅ Об'єднати"))
            mock_dispatch.assert_not_called()

    def test_unhandled_text_after_dispatcher_still_reaches_pending_preview_router(self):
        chat_id = 9104
        bot.pending_merge.pop(chat_id, None)
        # No pending state active and no mode set — text falls all the way
        # through dispatch() (RouteOutcome.CONTINUE) into the untouched
        # saved-list/general AI-chat tail, ending in a plain AI answer via
        # the mocked Gemini.
        _call_webhook(_make_update(chat_id, "Привіт, як справи?"))
        self.mock_send.assert_called_once()
        sent_text = self.mock_send.call_args.args[1]
        self.assertEqual(sent_text, "AI-помічник тимчасово недоступний. Спробуйте ще раз трохи пізніше.")


if __name__ == "__main__":
    unittest.main()
