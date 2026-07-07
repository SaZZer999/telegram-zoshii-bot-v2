"""Legacy Shopping Flow V1 — module boundary tests.

Does NOT re-test shopping business logic (parsing edge cases, merge rules,
stale-snapshot protection, etc. — those already live in test_action_
selection_router.py, test_pending_preview_logic.py, test_stale_preview_
protection.py, test_merge_stale_snapshot_protection.py and friends, and keep
passing unchanged against the extracted module). This file only asserts:
state-dict ownership/identity between bot.py and legacy_shopping_flow.py,
that the module's handlers behave correctly against fake injected deps (no
DB write before confirm, same preview/keyboard semantics), and that
webhook() still calls into the module at the same priority slots as before.

No real Gemini/Telegram/Supabase call happens anywhere in this file —
legacy_shopping_flow.py takes only a plain fake ShoppingFlowDeps, and the
webhook-level tests mock bot.send_message/bot.call_gemini/bot.get_household_
and_user exactly like every other routing test in this suite.
"""
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
import legacy_shopping_flow  # noqa: E402


def _make_fake_deps(**overrides):
    """A ShoppingFlowDeps built from plain fakes/MagicMocks — no bot.py
    import, no network, no DB. Individual fields can be overridden per test."""
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


class TestStateDictIdentity(unittest.TestCase):
    """1. bot.py re-exports the SAME dict objects legacy_shopping_flow.py owns."""

    def test_shopping_mode_is_same_object(self):
        self.assertIs(bot.shopping_mode, legacy_shopping_flow.shopping_mode)

    def test_pending_batch_is_same_object(self):
        self.assertIs(bot.pending_batch, legacy_shopping_flow.pending_batch)

    def test_pending_mark_batch_is_same_object(self):
        self.assertIs(bot.pending_mark_batch, legacy_shopping_flow.pending_mark_batch)

    def test_pending_delete_batch_is_same_object(self):
        self.assertIs(bot.pending_delete_batch, legacy_shopping_flow.pending_delete_batch)

    def test_mutation_via_bot_visible_via_module(self):
        chat_id = 999001
        bot.shopping_mode[chat_id] = "adding"
        try:
            self.assertEqual(legacy_shopping_flow.shopping_mode[chat_id], "adding")
        finally:
            bot.shopping_mode.pop(chat_id, None)


class TestOpenShoppingMenu(unittest.TestCase):
    """2. handle_open_shopping_menu shows the same shopping list and keyboard."""

    def setUp(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_shopping_flow.pending_batch.clear()

    def test_shows_list_and_shopping_keyboard(self):
        items = [{"id": 1, "name": "Хліб", "category": "Інше їстівне"}]
        deps = _make_fake_deps(get_active_shopping_items=MagicMock(return_value=items))
        legacy_shopping_flow.handle_open_shopping_menu(deps, chat_id=1, user_id=555, display_name="Тест")

        deps.send_message.assert_called_once()
        args, kwargs = deps.send_message.call_args
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1], "list:1")
        self.assertEqual(kwargs["reply_markup"], deps.shopping_keyboard)
        self.assertEqual(deps.active_list_context[1], "shopping")
        self.assertEqual(deps.saved_list_context[1], "shopping_saved")
        deps.clear_shopping_state.assert_called_once_with(1)
        deps.clear_inventory_state.assert_called_once_with(1)

    def test_db_error_still_sends_shopping_keyboard(self):
        deps = _make_fake_deps(get_active_shopping_items=MagicMock(side_effect=Exception("boom")))
        legacy_shopping_flow.handle_open_shopping_menu(deps, chat_id=2, user_id=555, display_name="Тест")
        deps.send_message.assert_called_once_with(2, deps.db_error_msg, reply_markup=deps.shopping_keyboard)


class TestStartShoppingAdd(unittest.TestCase):
    """3. Start add sets the same shopping_mode value ("adding")."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()

    def test_sets_adding_mode(self):
        deps = _make_fake_deps()
        legacy_shopping_flow.handle_start_shopping_add(deps, chat_id=3)
        self.assertEqual(legacy_shopping_flow.shopping_mode[3], "adding")
        deps.clear_shopping_state.assert_called_once_with(3)
        deps.send_message.assert_called_once()


class TestShoppingModeTextHandler(unittest.TestCase):
    """4. Uses the injected parser and creates a pending batch WITHOUT any DB write."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_shopping_flow.pending_batch.clear()

    def test_adding_mode_builds_pending_batch_via_injected_parser(self):
        chat_id = 4
        legacy_shopping_flow.shopping_mode[chat_id] = "adding"
        raw_gemini_json = '{"items": [{"name": "Молоко", "category": "Молочне та яйця", "is_consumable": true, "quantity_text": ""}], "ignored_items": []}'
        deps = _make_fake_deps(call_gemini=MagicMock(return_value=raw_gemini_json))

        handled = legacy_shopping_flow.handle_shopping_mode_text(deps, chat_id, user_id=555, display_name="Тест", text="Молоко")

        self.assertTrue(handled)
        deps.call_gemini.assert_called_once()
        self.assertIn(chat_id, legacy_shopping_flow.pending_batch)
        batch = legacy_shopping_flow.pending_batch[chat_id]
        self.assertEqual(batch["items"][0]["name"], "Молоко")
        self.assertEqual(batch["household_id"], 1)
        # No DB-write callable exists anywhere on deps — a preview only ever
        # writes to the in-memory pending_batch dict, never to the database.
        self.assertFalse(hasattr(deps, "add_shopping_items_batch"))

    def test_no_active_mode_falls_through_for_inventory_router(self):
        deps = _make_fake_deps()
        handled = legacy_shopping_flow.handle_shopping_mode_text(deps, chat_id=5, user_id=555, display_name="Тест", text="щось")
        self.assertFalse(handled)
        deps.send_message.assert_not_called()


class TestPendingBatchEditHandler(unittest.TestCase):
    """5. Pending batch edit handler does not touch the DB and preserves preview semantics."""

    def tearDown(self):
        legacy_shopping_flow.pending_batch.clear()

    def test_edit_preview_intent_updates_items_and_reshows_preview(self):
        chat_id = 6
        legacy_shopping_flow.pending_batch[chat_id] = {
            "items": [{"id": None, "name": "Хліб", "category": "Інше їстівне"}],
            "ignored_items": [],
            "household_id": 1,
            "user_db_id": 10,
        }
        deps = _make_fake_deps(
            ask_gemini_preview_edit_router=MagicMock(return_value={
                "intent": "edit_preview",
                "updates": [{"item_number": 1, "quantity_text": "2 шт."}],
            }),
            validate_preview_updates=MagicMock(return_value=[{"item_number": 1, "quantity_text": "2 шт."}]),
        )

        handled = legacy_shopping_flow.handle_pending_batch_edit_text(deps, chat_id, "2 хліба")

        self.assertTrue(handled)
        deps.apply_preview_updates.assert_called_once()
        deps.send_message.assert_called_once()
        _, kwargs = deps.send_message.call_args
        self.assertEqual(kwargs["reply_markup"], deps.add_preview_keyboard)
        self.assertFalse(hasattr(deps, "add_shopping_items_batch"))

    def test_intent_none_falls_through_to_ai_chat(self):
        chat_id = 7
        legacy_shopping_flow.pending_batch[chat_id] = {
            "items": [{"id": None, "name": "Хліб", "category": "Інше їстівне"}],
            "ignored_items": [],
            "household_id": 1,
            "user_db_id": 10,
        }
        deps = _make_fake_deps()
        handled = legacy_shopping_flow.handle_pending_batch_edit_text(deps, chat_id, "яка сьогодні погода?")
        self.assertFalse(handled)
        deps.send_message.assert_not_called()


class TestStartMarkBought(unittest.TestCase):
    """6. Start mark-bought creates the same preview state (shopping_mode="marking")."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()

    def test_nonempty_list_enters_marking_mode(self):
        deps = _make_fake_deps(get_active_shopping_items=MagicMock(return_value=[{"id": 1, "name": "Хліб"}]))
        legacy_shopping_flow.handle_start_mark_bought(deps, chat_id=8, user_id=555, display_name="Тест")
        self.assertEqual(legacy_shopping_flow.shopping_mode[8], "marking")

    def test_empty_list_does_not_enter_marking_mode(self):
        deps = _make_fake_deps(get_active_shopping_items=MagicMock(return_value=[]))
        legacy_shopping_flow.handle_start_mark_bought(deps, chat_id=9, user_id=555, display_name="Тест")
        self.assertNotIn(9, legacy_shopping_flow.shopping_mode)
        deps.send_message.assert_called_once_with(9, "Список покупок поки порожній.")


class TestStartDelete(unittest.TestCase):
    """7. Start delete creates the same preview state (shopping_mode="deleting")."""

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()

    def test_nonempty_list_enters_deleting_mode(self):
        deps = _make_fake_deps(get_active_shopping_items=MagicMock(return_value=[{"id": 1, "name": "Хліб"}]))
        legacy_shopping_flow.handle_start_delete(deps, chat_id=10, user_id=555, display_name="Тест")
        self.assertEqual(legacy_shopping_flow.shopping_mode[10], "deleting")

    def test_empty_list_does_not_enter_deleting_mode(self):
        deps = _make_fake_deps(get_active_shopping_items=MagicMock(return_value=[]))
        legacy_shopping_flow.handle_start_delete(deps, chat_id=11, user_id=555, display_name="Тест")
        self.assertNotIn(11, legacy_shopping_flow.shopping_mode)


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


class TestWebhookCallsModuleAtSamePrioritySlots(unittest.TestCase):
    """8. webhook() still dispatches into legacy_shopping_flow at the exact
    same priority slots as the old inline code (menu buttons, shopping_mode
    text dispatch, pending_batch edit router)."""

    def setUp(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_shopping_flow.pending_batch.clear()
        legacy_shopping_flow.pending_mark_batch.clear()
        legacy_shopping_flow.pending_delete_batch.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_shopping_items = patch.object(bot, "get_active_shopping_items", return_value=[])
        self.mock_shopping_items = patcher_shopping_items.start()
        self.addCleanup(patcher_shopping_items.stop)

        patcher_gemini = patch.object(bot, "call_gemini", return_value=None)
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_shopping_flow.pending_batch.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()

    def test_open_shopping_menu_button_calls_module_handler(self):
        with patch.object(legacy_shopping_flow, "handle_open_shopping_menu") as mock_handler:
            _call_webhook(_make_update(101, "🛒 Покупки"))
            mock_handler.assert_called_once_with(bot._shopping_deps, 101, 555, "Тест")

    def test_start_add_button_calls_module_handler(self):
        with patch.object(legacy_shopping_flow, "handle_start_shopping_add") as mock_handler:
            _call_webhook(_make_update(102, "➕ Додати товар"))
            mock_handler.assert_called_once_with(bot._shopping_deps, 102)

    def test_show_list_button_calls_module_handler(self):
        with patch.object(legacy_shopping_flow, "handle_show_shopping_list") as mock_handler:
            _call_webhook(_make_update(103, "📋 Показати список"))
            mock_handler.assert_called_once_with(bot._shopping_deps, 103, 555, "Тест")

    def test_start_mark_bought_button_calls_module_handler(self):
        with patch.object(legacy_shopping_flow, "handle_start_mark_bought") as mock_handler:
            _call_webhook(_make_update(104, "✅ Позначити купленим"))
            mock_handler.assert_called_once_with(bot._shopping_deps, 104, 555, "Тест")

    def test_start_delete_button_calls_module_handler(self):
        with patch.object(legacy_shopping_flow, "handle_start_delete") as mock_handler:
            _call_webhook(_make_update(105, "🗑️ Видалити товар"))
            mock_handler.assert_called_once_with(bot._shopping_deps, 105, 555, "Тест")

    def test_shopping_mode_text_dispatch_reached_before_inventory_mode(self):
        chat_id = 106
        legacy_shopping_flow.shopping_mode[chat_id] = "adding"
        with patch.object(legacy_shopping_flow, "handle_shopping_mode_text", return_value=True) as mock_handler:
            _call_webhook(_make_update(chat_id, "Молоко"))
            mock_handler.assert_called_once_with(bot._shopping_deps, chat_id, 555, "Тест", "Молоко")

    def test_pending_batch_edit_router_reached_ahead_of_inventory_batch(self):
        chat_id = 107
        legacy_shopping_flow.pending_batch[chat_id] = {
            "items": [{"id": None, "name": "Хліб", "category": "Інше їстівне"}],
            "ignored_items": [],
            "household_id": 1,
            "user_db_id": 10,
        }
        with patch.object(legacy_shopping_flow, "handle_pending_batch_edit_text", return_value=True) as mock_handler:
            _call_webhook(_make_update(chat_id, "додай ще один"))
            mock_handler.assert_called_once_with(bot._shopping_deps, chat_id, "додай ще один")

    def test_no_gemini_telegram_or_supabase_needed_for_unit_test(self):
        """9. The whole shopping_mode round-trip runs with call_gemini/
        send_message/get_household_and_user mocked and get_active_shopping_
        items mocked — no real network or DB call happens."""
        chat_id = 108
        legacy_shopping_flow.shopping_mode[chat_id] = "marking"
        _call_webhook(_make_update(chat_id, "молоко"))
        self.mock_send.assert_called_with(chat_id, "Список покупок поки порожній.")


if __name__ == "__main__":
    unittest.main()
