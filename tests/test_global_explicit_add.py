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


def _milk_and_bread_items():
    return {
        "items": [
            {"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
            {"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
        ],
        "unresolved_fragments": [],
    }


def _two_bananas_item():
    return {
        "items": [{"name": "Банани", "quantity_text": "2", "category": "Фрукти та ягоди"}],
        "unresolved_fragments": [],
    }


class _BaseExplicitAddTestCase(unittest.TestCase):
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
        for d in (pending_global_household, pending_inventory_quantity_clarification, active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestExplicitShoppingAddMenuIndependence(_BaseExplicitAddTestCase):
    # Case 1 — main menu
    def test_main_menu_creates_shopping_only_preview(self):
        chat_id = 997001
        self.mock_items.return_value = _milk_and_bread_items()
        _call_webhook(_make_update(997000001, chat_id, "Додай до покупок молоко і хліб"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 2)
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertIsNone(payload["new_expense"])
        texts = self._sent_texts()
        self.assertTrue(any(
            "🛒 Покупки" in t and "Додати Хліб — 1 шт. (припущення)" in t
            and "Додати Молоко — 1 шт. (припущення)" in t and "🧊 Запаси" not in t
            for t in texts
        ))

    # Case 2 — same command from inventory/expenses/aliases menus creates the
    # exact same shopping preview, unaffected by active_list_context.
    def test_other_menus_create_the_same_shopping_preview(self):
        menu_chat_ids = {"inventory": 997002, "expenses": 997003, "aliases": 997004}
        for menu, chat_id in menu_chat_ids.items():
            with self.subTest(menu=menu):
                active_list_context[chat_id] = menu
                self.mock_items.return_value = _milk_and_bread_items()
                _call_webhook(_make_update(chat_id * 10, chat_id, "Додай до покупок молоко і хліб"))
                self.assertIn(chat_id, pending_global_household)
                payload = pending_global_household[chat_id]
                self.assertEqual(len(payload["add_shopping_items"]), 2)
                self.assertEqual(payload["add_inventory_items"], [])
                self.assertIsNone(payload["new_expense"])


class TestExplicitInventoryAdd(_BaseExplicitAddTestCase):
    # Case 3 — inventory-only preview from any menu
    def test_inventory_destination_creates_inventory_only_preview(self):
        chat_id = 997010
        active_list_context[chat_id] = "expenses"
        self.mock_items.return_value = _two_bananas_item()
        _call_webhook(_make_update(997000010, chat_id, "Додай в запаси 2 банани"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_shopping_items"], [])
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.assertIsNone(payload["new_expense"])
        texts = self._sent_texts()
        self.assertTrue(any("🧊 Запаси" in t and "Додати Банани — 2 шт." in t and "🛒 Покупки" not in t for t in texts))


class TestExplicitAddCannotSmuggleOtherOperations(_BaseExplicitAddTestCase):
    # Case 4 — an explicit SHOPPING command can never produce an inventory
    # or expense operation, no matter what Gemini's mocked response looks
    # like — the destination bucket is hardcoded by Python, not by Gemini.
    def test_shopping_command_never_creates_inventory_or_expense(self):
        chat_id = 997020
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
            # Adversarial extra fields a misbehaving Gemini response might
            # include — build_explicit_add_preview's JSON contract has no
            # slot for any of these, so they must be silently ignored.
            "type": "add_expense", "amount": "999", "operations": [{"type": "consume_inventory"}],
        }
        _call_webhook(_make_update(997000020, chat_id, "Додай до покупок молоко"))
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertIsNone(payload["new_expense"])
        self.assertIsNone(payload["delete_expense"])
        self.assertEqual(payload["consume_changes"], [])

    # Case 5 — an explicit INVENTORY command can never produce a shopping
    # or expense operation either.
    def test_inventory_command_never_creates_shopping_or_expense(self):
        chat_id = 997021
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
            "type": "add_shopping", "amount": "999",
        }
        _call_webhook(_make_update(997000021, chat_id, "Додай в запаси молоко"))
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_shopping_items"], [])
        self.assertIsNone(payload["new_expense"])
        self.assertIsNone(payload["delete_expense"])


class TestExplicitAddQuantityClarification(_BaseExplicitAddTestCase):
    # Case 6 — representation conflict reuses the existing quantity
    # clarification instead of creating a new record.
    def test_representation_conflict_triggers_existing_clarification(self):
        chat_id = 997030
        self.mock_inventory.return_value = [_milk_liters_row(), _milk_pieces_row()]
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(997000030, chat_id, "Додай до запасів молоко"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any(
            "У запасах уже є кілька записів «Молоко»:" in t and "• 7 л" in t and "• 1 шт." in t
            for t in texts
        ))

    # Case 7 — a valid clarification reply produces the normal global preview
    def test_valid_clarification_reply_produces_global_preview(self):
        chat_id = 997031
        self.mock_inventory.return_value = [_milk_liters_row(), _milk_pieces_row()]
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(997000031, chat_id, "Додай до запасів молоко"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)

        _call_webhook(_make_update(997000032, chat_id, "1 л"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 7 л + 1 л → буде 8 л" in t for t in texts))


class TestExplicitAddUnresolvedFragments(_BaseExplicitAddTestCase):
    # Case 8 — a non-empty unresolved_fragments blocks the WHOLE preview
    def test_unresolved_fragment_blocks_entire_preview(self):
        chat_id = 997040
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": ["щось незрозуміле"],
        }
        _call_webhook(_make_update(997000040, chat_id, "Додай до покупок молоко і щось незрозуміле"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.mock_call_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("щось незрозуміле" in t for t in texts))


class TestExplicitAddWithAmountIsBlocked(_BaseExplicitAddTestCase):
    # Case 9 — an amount in the message blocks it deterministically, before
    # Gemini is even called, rather than creating an incomplete record.
    def test_amount_in_explicit_inventory_command_is_blocked(self):
        chat_id = 997050
        _call_webhook(_make_update(997000050, chat_id, "Додай в запаси молоко за 10 zł"))
        self.mock_items.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(
            "Для покупки з витратою напиши, наприклад:" in t and "«Купив молоко за 10 zł»" in t
            for t in texts
        ))

    def test_amount_in_explicit_shopping_command_is_blocked(self):
        chat_id = 997051
        _call_webhook(_make_update(997000051, chat_id, "Додай до покупок молоко за 10 zł"))
        self.mock_items.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)

    # Short zloty marker "z" — cases 1/2 from the "z" recognition fix: the
    # explicit-add guard must catch it exactly like "zł", before Gemini.
    def test_short_zloty_marker_in_explicit_inventory_command_is_blocked(self):
        chat_id = 997052
        _call_webhook(_make_update(997000052, chat_id, "Додай в запаси молоко за 10 z"))
        self.mock_items.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(
            "Для покупки з витратою напиши, наприклад:" in t and "«Купив молоко за 10 zł»" in t
            for t in texts
        ))

    def test_short_zloty_marker_with_decimal_in_explicit_shopping_command_is_blocked(self):
        chat_id = 997053
        _call_webhook(_make_update(997000053, chat_id, "Додай до покупок хліб за 10,50 z"))
        self.mock_items.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)


class TestExplicitAddPriority(_BaseExplicitAddTestCase):
    # Case 10 — an active preview/clarification always wins over the
    # explicit-add route; the route must not even be attempted.
    def test_active_global_preview_blocks_explicit_add_route(self):
        chat_id = 997060
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(997000060, chat_id, "Додай до покупок хліб"))
        self.mock_items.assert_not_called()
        self.assertIn(chat_id, pending_global_household)

    def test_active_quantity_clarification_blocks_explicit_add_route(self):
        chat_id = 997061
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [{
                "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
                "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "was_corrected": False,
            }],
            "consume_changes": [], "new_expense": None, "delete_expense": None,
        }
        _call_webhook(_make_update(997000061, chat_id, "Додай до покупок хліб"))
        self.mock_items.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)


class TestBareAddUnchanged(unittest.TestCase):
    # Case 11 — bare "Додай молоко" (no destination phrase) is untouched by
    # this route: detect_explicit_add_destination must find nothing at all.
    def test_bare_add_has_no_explicit_destination(self):
        destination, item_text = household_router.detect_explicit_add_destination("Додай молоко")
        self.assertIsNone(destination)
        self.assertIsNone(item_text)

    def test_try_global_explicit_add_returns_false_for_bare_add(self):
        with patch.object(bot, "get_household_and_user") as mock_get_user:
            result = bot._try_global_explicit_add(999999, 555, "Тест", "Додай молоко")
            self.assertFalse(result)
            mock_get_user.assert_not_called()


if __name__ == '__main__':
    unittest.main()
