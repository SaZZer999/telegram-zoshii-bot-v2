"""Message Dispatcher V3B — confirm/cancel routing (final Dispatcher V3
wave).

Does NOT re-test the underlying business logic of any individual confirm/
cancel button (merge/mark/delete/undo/alias/expense/reconciliation DB
writes, StaleSnapshotError handling, exact messages — already covered by
test_stale_preview_protection.py, test_alias_bulk_actions_and_return_
context.py, test_expense_delete.py, test_safe_undo_global_action.py,
test_global_household_operations.py and friends, which keep passing
unchanged against the extracted callback). This file only asserts: the
confirm/cancel route's TOP priority over every other route in the chain
(navigation, special buttons, shopping/inventory mode, pending routes,
command/context routes), that it never reaches cooking mode or the general
AI fallback, module boundary, CommandRouteDeps-style single-callback
design, and that webhook() now contains no application route branches of
its own after the access check.

No real Gemini/Telegram/Supabase call happens anywhere in this file.
"""
import ast
import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, so
# bot.StaleSnapshotError can be reassigned to the real exception class for
# one test below — same reasoning/pattern as test_inventory_representation_
# guard.py and test_stale_preview_protection.py: other test files in this
# suite (run in the same process by `unittest discover`) may already have
# replaced sys.modules['database'] with a MagicMock, so bot.py's own
# `from database import StaleSnapshotError` would otherwise bind to a
# MagicMock attribute that `except StaleSnapshotError:` can't actually catch.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_confirm_cancel_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

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

CONFIRM_CANCEL_TEXTS = (
    "✅ Об'єднати",
    "✅ Додати все",
    "✏️ Надіслати інший список",
    "❌ Скасувати",
    "✏️ Виправити позицію",
    "✅ Куплено + додати в запаси",
    "✅ Куплено, без запасів",
    "✅ Так, видалити",
    "✅ Так, запам'ятати",
    "✅ Так, змінити",
    "✅ Так, додати",
    "✅ Так, застосувати",
    "✅ Так, скасувати",
    "✅ Так, прибрати",
    "✅ Додати до запасів",
    "✏️ Змінити список",
    "✅ Підтвердити зміни",
    "✅ Підтвердити всі зміни",
    "✅ Підтвердити звіряння",
    "✏️ Змінити вибір",
)


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
    """A DispatcherDeps with confirm_or_cancel/special_button/cooking_mode
    all WIRED — this is what exercises the full V3B confirm/cancel
    top-priority behavior."""
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
        confirm_or_cancel=MagicMock(return_value=False),
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


class TestSingleConfirmOrCancelCallback(unittest.TestCase):
    """2. DispatcherDeps has exactly one confirm_or_cancel callback, not a
    set of per-button callbacks."""

    def test_only_one_new_field_added(self):
        fields = set(message_dispatcher.DispatcherDeps.__dataclass_fields__.keys())
        self.assertIn("confirm_or_cancel", fields)
        # None of the 20 button texts leaked into DispatcherDeps as their
        # own dedicated field names.
        forbidden_field_hints = (
            "merge", "mark_bought", "delete_batch", "undo_confirm", "alias_confirm",
            "expense_confirm", "reconciliation_confirm", "quick_purchase",
        )
        for hint in forbidden_field_hints:
            self.assertFalse(
                any(hint in f for f in fields),
                f"found a per-button-ish field containing {hint!r}: {fields}",
            )


class TestConfirmCancelOutranksNavigation(unittest.TestCase):
    """3. Confirm/cancel has priority over navigation."""

    def test_confirm_cancel_wins_over_navigation_text(self):
        confirm_or_cancel = MagicMock(return_value=True)
        deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel)
        # "/start" would normally be a navigation route, but here the
        # confirm_or_cancel callback claims to have handled it (simulating
        # a rare/theoretical text collision) — it must win regardless.
        result = message_dispatcher.dispatch(deps, 1, 555, "Тест", "/start")
        self.assertEqual(result, RouteOutcome.HANDLED)
        confirm_or_cancel.assert_called_once_with(1, 555, "Тест", "/start")
        deps.clear_interaction_state.assert_not_called()


class TestConfirmCancelOutranksSpecialButton(unittest.TestCase):
    """4. Confirm/cancel has priority over a special button."""

    def test_confirm_cancel_wins_over_special_button_text(self):
        confirm_or_cancel = MagicMock(return_value=True)
        special_button = MagicMock(return_value=True)
        deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel, special_button=special_button)
        result = message_dispatcher.dispatch(deps, 2, 555, "Тест", "✅ Об'єднати")
        self.assertEqual(result, RouteOutcome.HANDLED)
        confirm_or_cancel.assert_called_once_with(2, 555, "Тест", "✅ Об'єднати")
        special_button.assert_not_called()


class TestConfirmCancelOutranksShoppingMode(unittest.TestCase):
    """5. Confirm/cancel has priority over shopping_mode."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()

    def test_confirm_cancel_wins_over_shopping_mode(self):
        chat_id = 3
        legacy_shopping_flow.shopping_mode[chat_id] = "adding"
        confirm_or_cancel = MagicMock(return_value=True)
        deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel)
        with patch.object(legacy_shopping_flow, "handle_shopping_mode_text") as mock_shop:
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "✅ Так, видалити")
        self.assertEqual(result, RouteOutcome.HANDLED)
        confirm_or_cancel.assert_called_once_with(chat_id, 555, "Тест", "✅ Так, видалити")
        mock_shop.assert_not_called()


class TestConfirmCancelOutranksInventoryMode(unittest.TestCase):
    """6. Confirm/cancel has priority over inventory_mode."""

    def tearDown(self):
        legacy_inventory_flow.inventory_mode.clear()

    def test_confirm_cancel_wins_over_inventory_mode(self):
        chat_id = 4
        legacy_inventory_flow.inventory_mode[chat_id] = "removing"
        confirm_or_cancel = MagicMock(return_value=True)
        deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel)
        with patch.object(legacy_inventory_flow, "handle_inventory_mode_text") as mock_inv:
            result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "✅ Так, прибрати")
        self.assertEqual(result, RouteOutcome.HANDLED)
        confirm_or_cancel.assert_called_once_with(chat_id, 555, "Тест", "✅ Так, прибрати")
        mock_inv.assert_not_called()


class TestConfirmCancelOutranksPendingRoutes(unittest.TestCase):
    """7. Confirm/cancel has priority over pending/clarification routes."""

    def test_confirm_cancel_wins_over_pending_global_household(self):
        chat_id = 5
        confirm_or_cancel = MagicMock(return_value=True)
        deps = _make_fake_dispatcher_deps(
            confirm_or_cancel=confirm_or_cancel,
            pending_routes=_make_fake_pending_route_deps(pending_global_household={chat_id: {}}),
        )
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "✅ Так, застосувати")
        self.assertEqual(result, RouteOutcome.HANDLED)
        confirm_or_cancel.assert_called_once_with(chat_id, 555, "Тест", "✅ Так, застосувати")
        deps.send_message.assert_not_called()


class TestConfirmCancelOutranksCommandRoutes(unittest.TestCase):
    """8. Confirm/cancel has priority over command/context routes."""

    def test_confirm_cancel_wins_over_saved_list_router(self):
        chat_id = 6
        confirm_or_cancel = MagicMock(return_value=True)
        command_routes = _make_fake_command_route_deps(saved_list_router=MagicMock(return_value=True))
        deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel, command_routes=command_routes)
        result = message_dispatcher.dispatch(deps, chat_id, 555, "Тест", "✏️ Змінити вибір")
        self.assertEqual(result, RouteOutcome.HANDLED)
        confirm_or_cancel.assert_called_once_with(chat_id, 555, "Тест", "✏️ Змінити вибір")
        command_routes.saved_list_router.assert_not_called()


class TestConfirmCancelNeverReachesCookingMode(unittest.TestCase):
    """9. Confirm/cancel never triggers cooking mode."""

    def test_cooking_mode_not_called(self):
        cooking_mode = MagicMock(return_value=False)
        confirm_or_cancel = MagicMock(return_value=True)
        deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel, cooking_mode=cooking_mode)
        result = message_dispatcher.dispatch(deps, 7, 555, "Тест", "❌ Скасувати")
        self.assertEqual(result, RouteOutcome.HANDLED)
        cooking_mode.assert_not_called()


class TestConfirmCancelNeverReachesAiFallback(unittest.TestCase):
    """10. Confirm/cancel never triggers the general AI fallback."""

    def test_fallback_not_called(self):
        general_ai_fallback = MagicMock()
        confirm_or_cancel = MagicMock(return_value=True)
        deps = _make_fake_dispatcher_deps(
            confirm_or_cancel=confirm_or_cancel,
            command_routes=_make_fake_command_route_deps(general_ai_fallback=general_ai_fallback),
        )
        result = message_dispatcher.dispatch(deps, 8, 555, "Тест", "✏️ Виправити позицію")
        self.assertEqual(result, RouteOutcome.HANDLED)
        general_ai_fallback.assert_not_called()


class TestNonConfirmTextFallsThroughToNextRoutes(unittest.TestCase):
    """11. Non-confirm text falls through to the next dispatcher routes
    (confirm_or_cancel returns False, navigation is tried next)."""

    def test_non_confirm_text_reaches_navigation(self):
        confirm_or_cancel = MagicMock(return_value=False)
        deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel)
        result = message_dispatcher.dispatch(deps, 9, 555, "Тест", "/menu")
        self.assertEqual(result, RouteOutcome.HANDLED)
        confirm_or_cancel.assert_called_once_with(9, 555, "Тест", "/menu")
        deps.clear_interaction_state.assert_called_once_with(9)


class TestRuntimeLambdaVisibility(unittest.TestCase):
    """12. Runtime lambda callback sees patch.object(bot, "_try_handle_
    confirm_or_cancel", ...)."""

    def test_patched_confirm_or_cancel_visible_through_dispatcher_deps(self):
        with patch.object(bot, "_try_handle_confirm_or_cancel", return_value=True) as mock_confirm:
            result = bot._dispatcher_deps.confirm_or_cancel(1, 555, "Тест", "✅ Так, додати")
            self.assertTrue(result)
            mock_confirm.assert_called_once_with(1, 555, "Тест", "✅ Так, додати")


class TestAllTwentyButtonsAreTopPriority(unittest.TestCase):
    """13. All 20 exact button texts are passed to the callback as the
    top-priority route (parameterized)."""

    def test_each_confirm_cancel_text_invokes_callback_first(self):
        for i, text in enumerate(CONFIRM_CANCEL_TEXTS):
            with self.subTest(text=text):
                confirm_or_cancel = MagicMock(return_value=True)
                deps = _make_fake_dispatcher_deps(confirm_or_cancel=confirm_or_cancel)
                result = message_dispatcher.dispatch(deps, 200 + i, 555, "Тест", text)
                self.assertEqual(result, RouteOutcome.HANDLED)
                confirm_or_cancel.assert_called_once_with(200 + i, 555, "Тест", text)


class TestNoDirectDbAccess(unittest.TestCase):
    """17. Dispatcher never writes to the DB directly."""

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
            "create_or_update_household_alias", "delete_household_alias", "mark_items_batch",
            "add_or_merge_inventory_item", "delete_inventory_items_batch", "delete_household_aliases_batch",
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
    """12/14/15/16. Webhook-level integration: patched bot._try_handle_
    confirm_or_cancel is visible through dispatch(); StaleSnapshotError
    behavior in the confirm-flow does not regress; webhook() calls
    dispatch() exactly once with no application route branches of its own
    after the access check; /myid and the access check stay above the
    dispatcher.

    bot.StaleSnapshotError is reassigned to the REAL exception class for
    this test class only — bot.py's own import binds the name to whatever
    `database` was mocked to at import time (a bare MagicMock attribute
    here, not a real Exception subclass), so `except StaleSnapshotError:`
    inside _try_handle_confirm_or_cancel couldn't otherwise match a raised
    instance. Same caveat/fix as test_inventory_representation_guard.py/
    test_stale_preview_protection.py."""

    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

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

    def test_patched_confirm_or_cancel_seen_at_webhook_level(self):
        chat_id = 9501
        with patch.object(bot, "_try_handle_confirm_or_cancel", return_value=True) as mock_confirm:
            _call_webhook(_make_update(chat_id, "✅ Так, видалити"))
            mock_confirm.assert_called_once_with(chat_id, 555, "Тест", "✅ Так, видалити")

    def test_stale_snapshot_error_still_produces_stale_message(self):
        """14. StaleSnapshotError behavior in the confirm-flow does not
        regress — a real StaleSnapshotError raised from the DB helper still
        produces STALE_PREVIEW_MSG, unchanged."""
        chat_id = 9502
        bot.pending_remove_batch[chat_id] = {
            "items": [{"id": 1, "name": "Молоко"}], "household_id": 1, "user_db_id": 10,
        }
        try:
            with patch.object(bot, "delete_inventory_items_batch", side_effect=bot.StaleSnapshotError("stale")):
                _call_webhook(_make_update(chat_id, "✅ Так, прибрати"))
        finally:
            bot.pending_remove_batch.pop(chat_id, None)
        self.mock_send.assert_called_once_with(chat_id, bot.STALE_PREVIEW_MSG, reply_markup=bot.INVENTORY_KEYBOARD)

    def test_webhook_calls_dispatch_exactly_once_no_route_branches(self):
        chat_id = 9503
        with patch.object(message_dispatcher, "dispatch", wraps=message_dispatcher.dispatch) as spy:
            _call_webhook(_make_update(chat_id, "✅ Так, скасувати"))
            spy.assert_called_once_with(bot._dispatcher_deps, chat_id, 555, "Тест", "✅ Так, скасувати")

    def test_myid_and_access_check_stay_above_dispatcher(self):
        chat_id = 9504
        with patch.object(message_dispatcher, "dispatch") as mock_dispatch:
            _call_webhook(_make_update(chat_id, "/myid"))
            mock_dispatch.assert_not_called()
        self.mock_send.assert_called_once()
        sent_text = self.mock_send.call_args.args[1]
        self.assertIn(str(555), sent_text)


if __name__ == "__main__":
    unittest.main()
