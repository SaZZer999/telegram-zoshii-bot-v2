import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No test in this file calls real
# Gemini, Telegram, Render, or Supabase — every network-facing function
# (_ask_gemini_explicit_add_items, _ask_gemini_household_router, call_gemini,
# send_message, apply_global_household_operations) is patched per-test below.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
import household_router
from bot import (
    pending_global_household,
    pending_inventory_quantity_clarification,
    pending_add_destination_clarification,
    active_list_context,
    saved_list_context,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _milk_liters_row():
    return {"id": 201, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 7.0, "quantity_unit": "л", "quantity_text": "7 л", "quantity_inferred": False}


def _milk_pieces_row():
    return {"id": 202, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False}


def _milk_item():
    return {
        "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
        "unresolved_fragments": [],
    }


class _BaseBareAddTestCase(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_items = patch.object(household_router, "_ask_gemini_explicit_add_items")
        self.mock_items = patcher_items.start()
        self.addCleanup(patcher_items.stop)

        patcher_hr = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_hr = patcher_hr.start()
        self.addCleanup(patcher_hr.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

        patcher_inv = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory = patcher_inv.start()
        self.addCleanup(patcher_inv.stop)

    def tearDown(self):
        for d in (
            pending_global_household, pending_inventory_quantity_clarification,
            pending_add_destination_clarification, active_list_context, saved_list_context,
        ):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestBareAddMenuContext(_BaseBareAddTestCase):
    # Case 1 — shopping menu creates a shopping-only preview directly, no
    # clarification.
    def test_shopping_menu_creates_shopping_only_preview(self):
        chat_id = 998001
        active_list_context[chat_id] = "shopping"
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000001, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 1)
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.mock_hr.assert_not_called()

    # Case 2 — inventory menu creates an inventory-only preview directly, no
    # clarification.
    def test_inventory_menu_creates_inventory_only_preview(self):
        chat_id = 998002
        active_list_context[chat_id] = "inventory"
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000002, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_shopping_items"], [])
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.assertNotIn(chat_id, pending_add_destination_clarification)


class TestBareAddDestinationClarification(_BaseBareAddTestCase):
    # Case 3 — main menu creates a destination clarification, no preview, no
    # DB write.
    def test_main_menu_creates_destination_clarification(self):
        chat_id = 998010
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000010, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Куди додати ці позиції?" in t for t in texts))

    # Case 4 — expenses and aliases menus also create a destination
    # clarification (only "shopping"/"inventory" resolve directly).
    def test_expenses_and_aliases_menu_also_create_clarification(self):
        menu_chat_ids = {"expenses": 998011, "aliases": 998012}
        for menu, chat_id in menu_chat_ids.items():
            with self.subTest(menu=menu):
                active_list_context[chat_id] = menu
                self.mock_items.return_value = _milk_item()
                _call_webhook(_make_update(chat_id * 10, chat_id, "Додай молоко"))
                self.assertIn(chat_id, pending_add_destination_clarification)
                self.assertNotIn(chat_id, pending_global_household)


class TestBareAddDestinationResolution(_BaseBareAddTestCase):
    # Case 5 — "До покупок" resolves the clarification into a shopping
    # preview without a second Gemini parse.
    def test_shopping_choice_creates_preview_without_second_parse(self):
        chat_id = 998020
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000020, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)

        _call_webhook(_make_update(998000021, chat_id, "До покупок"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 1)
        self.mock_items.assert_called_once()

    # Case 6 — "У запаси" resolves the clarification into an inventory
    # preview without a second Gemini parse.
    def test_inventory_choice_creates_preview_without_second_parse(self):
        chat_id = 998021
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000022, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)

        _call_webhook(_make_update(998000023, chat_id, "У запаси"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.mock_items.assert_called_once()


class TestBareAddRepresentationConflict(_BaseBareAddTestCase):
    # Case 7 — after choosing "запаси", a representation conflict falls into
    # the existing Inventory Quantity Clarification v1, not a new mechanism.
    def test_inventory_conflict_after_destination_choice_uses_quantity_clarification(self):
        chat_id = 998030
        self.mock_inventory.return_value = [_milk_liters_row(), _milk_pieces_row()]
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000030, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)

        _call_webhook(_make_update(998000031, chat_id, "У запаси"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        texts = self._sent_texts()
        self.assertTrue(any("У запасах уже є кілька записів «Молоко»:" in t for t in texts))


class TestBareAddInvalidReplyAndReentrancy(_BaseBareAddTestCase):
    # Case 8 — an invalid reply during clarification never reaches general
    # AI-chat and never clears the pending state.
    def test_invalid_reply_does_not_reach_ai_chat_or_clear_state(self):
        chat_id = 998040
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000040, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)

        _call_webhook(_make_update(998000041, chat_id, "Не знаю"))
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_call_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Обери, куди додати ці позиції:" in t for t in texts))

    # Case 9 — a new command while the clarification is active never starts
    # a new preview or a new parse.
    def test_new_bare_add_during_clarification_does_not_start_new_preview(self):
        chat_id = 998041
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000042, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)
        calls_so_far = self.mock_items.call_count

        _call_webhook(_make_update(998000043, chat_id, "Додай хліб"))
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertEqual(self.mock_items.call_count, calls_so_far)


class TestBareAddCancelAndNavigation(_BaseBareAddTestCase):
    # Case 10 — cancel and "Головне меню" both clear the clarification state.
    def test_cancel_clears_clarification(self):
        chat_id = 998050
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000050, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)

        _call_webhook(_make_update(998000051, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        texts = self._sent_texts()
        self.assertTrue(any("Вибір місця додавання скасовано." in t for t in texts))

    def test_main_menu_button_clears_clarification(self):
        chat_id = 998051
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000052, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)

        _call_webhook(_make_update(998000053, chat_id, "⬅️ Головне меню"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)


class TestBareAddDoesNotAffectExplicitAdd(_BaseBareAddTestCase):
    # Case 11 — an explicit destination command behaves exactly as before.
    def test_explicit_destination_command_unaffected(self):
        chat_id = 998060
        self.mock_items.return_value = _milk_item()
        _call_webhook(_make_update(998000060, chat_id, "Додай до покупок молоко"))
        self.assertIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 1)


class TestBareAddExpenseAmountExcluded(_BaseBareAddTestCase):
    # Case 12 — a "10 zł"/"10 z" command never creates a destination
    # clarification (it isn't treated as a bare add at all).
    def test_zloty_amount_does_not_create_clarification(self):
        chat_id = 998070
        _call_webhook(_make_update(998000070, chat_id, "Додай молоко за 10 zł"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_items.assert_not_called()

    def test_short_zloty_marker_does_not_create_clarification(self):
        chat_id = 998071
        _call_webhook(_make_update(998000071, chat_id, "Додай молоко за 10 z"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.mock_items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
