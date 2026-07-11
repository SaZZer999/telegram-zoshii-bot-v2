"""Preview Edit V2 — webhook-level integration tests for text edits to an
ACTIVE pending_global_household "add" preview (shopping_add/inventory_add
style operations only — see preview_editing.py's own "PREVIEW EDIT V2"
section for the pure parser/apply functions, already covered directly in
tests/test_preview_editing_module.py). No real Gemini/Telegram/Supabase call
happens anywhere in this file — every network-facing function is patched per
test, same posture as tests/test_inventory_transform.py's own
TestPreviewEditV1/TestPreviewEditV1ConfirmCancelUndo."""
import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import preview_editing

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_global_household_preview_edit_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
    STALE_PREVIEW_MSG,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _milk_and_cheese_shopping_preview():
    """Two freshly-assumed shopping-add items, same shape
    household_router.build_add_preview_from_items produces for "Додай
    молоко і сир до покупок." — quantity_inferred=True, "1 шт." default."""
    return {
        "add_shopping_items": [
            {
                "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "is_consumable": True,
            },
            {
                "name": "Сир", "canonical_name": "сир", "category": "Молочне та яйця",
                "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "is_consumable": True,
            },
        ],
        "add_inventory_items": [], "consume_changes": [], "inventory_targets": [],
        "new_expenses": [], "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


class GlobalHouseholdPreviewEditWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


class TestPreviewEditV2ShoppingEdits(GlobalHouseholdPreviewEditWebhookTestCase):
    # 2. "молока 1 л, а сиру 500 г" updates both items and re-renders.
    def test_named_edit_with_conjunction_updates_both_items(self):
        chat_id = 881501
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(881501001, chat_id, "молока 1 л, а сиру 500 г"))
        self.mock_call_gemini.assert_not_called()
        entry = pending_global_household[chat_id]
        self.assertEqual(entry["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(entry["add_shopping_items"][1]["quantity_text"], "500 г")
        texts = self._sent_texts()
        self.assertTrue(any("Оновив план:" in t for t in texts))
        self.assertTrue(any("Молоко — 1 л" in t for t in texts))
        self.assertTrue(any("Сир — 500 г" in t for t in texts))
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())

    # 3. "молоко 1 л, сир 500 г" — plain form, no conjunction.
    def test_named_edit_plain_form(self):
        chat_id = 881502
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(881502001, chat_id, "молоко 1 л, сир 500 г"))
        entry = pending_global_household[chat_id]
        self.assertEqual(entry["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(entry["add_shopping_items"][1]["quantity_text"], "500 г")

    # 4. Positional shorthand "1 л, 500 г" maps by order.
    def test_positional_shorthand_maps_by_order(self):
        chat_id = 881503
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(881503001, chat_id, "1 л, 500 г"))
        entry = pending_global_household[chat_id]
        self.assertEqual(entry["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(entry["add_shopping_items"][1]["quantity_text"], "500 г")

    # 5. Positional shorthand with mangled STT English units "1L, 500g".
    def test_positional_shorthand_english_units(self):
        chat_id = 881504
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(881504001, chat_id, "1L, 500g"))
        entry = pending_global_household[chat_id]
        self.assertEqual(entry["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(entry["add_shopping_items"][1]["quantity_text"], "500 г")

    # 6. Word-number quantities aren't overbuilt — falls back to the
    # existing "unfinished plan" guard, preview left unchanged.
    def test_word_number_quantities_fall_back_to_guard_message(self):
        chat_id = 881505
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(881505001, chat_id, "один літр, пʼятсот грам"))
        self.assertEqual(pending_global_household[chat_id], original)
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))

    # 9. Invalid edit ("молока багато") — controlled message, unchanged.
    def test_invalid_quantity_leaves_preview_unchanged(self):
        chat_id = 881506
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(881506001, chat_id, "молока багато"))
        self.assertEqual(pending_global_household[chat_id], original)
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))

    # 10. Ambiguous name match (two items resolve the same token) — asks to
    # clarify, never guesses.
    def test_ambiguous_item_name_asks_to_clarify(self):
        chat_id = 881507
        data = _milk_and_cheese_shopping_preview()
        data["add_shopping_items"][1] = dict(data["add_shopping_items"][0])  # both "Молоко" now
        pending_global_household[chat_id] = data
        original = [dict(it) for it in data["add_shopping_items"]]
        _call_webhook(_make_update(881507001, chat_id, "молоко 1 л"))
        self.assertEqual(pending_global_household[chat_id]["add_shopping_items"], original)
        self.assertTrue(any(preview_editing.HOUSEHOLD_EDIT_AMBIGUOUS_MSG == t for t in self._sent_texts()))

    # 11. Active inventory add preview — same edit shape works there too.
    def test_inventory_add_preview_supports_edits(self):
        chat_id = 881508
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [{
                "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "is_consumable": True,
            }],
            "consume_changes": [], "inventory_targets": [],
            "new_expenses": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(881508001, chat_id, "молока 1 л"))
        entry = pending_global_household[chat_id]
        self.assertEqual(entry["add_inventory_items"][0]["quantity_text"], "1 л")
        self.assertEqual(entry["add_inventory_items"][0]["quantity_inferred"], False)

    # 12. An unsupported pending type (expense preview) — controlled
    # message, never general AI. Uses the shared expense-preview guard, not
    # this preview's edit parser at all (different pending dict entirely).
    def test_unsupported_pending_type_still_uses_its_own_guard(self):
        chat_id = 881509
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("10.00"), "currency": "PLN",
            "category": "Інше", "description": "Тест", "expense_date": None, "origin": "global",
        }
        try:
            _call_webhook(_make_update(881509001, chat_id, "молока 1 л"))
            self.mock_call_gemini.assert_not_called()
            self.assertTrue(any(bot.EXPENSE_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))
        finally:
            bot.pending_expense.pop(chat_id, None)


class TestPreviewEditV2ConfirmCancel(GlobalHouseholdPreviewEditWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    # 7. Confirming an EDITED preview writes the edited quantities, not the
    # original "1 шт." assumptions.
    def test_confirm_after_edit_writes_edited_values(self):
        chat_id = 881510
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(881510001, chat_id, "молока 1 л, а сиру 500 г"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 2, "inventory_added": 0, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": None, "expense_deleted": False,
            }
            _call_webhook(_make_update(881510002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        written_items = kwargs["add_shopping_items"]
        self.assertEqual(written_items[0]["quantity_text"], "1 л")
        self.assertEqual(written_items[1]["quantity_text"], "500 г")
        self.assertNotIn(chat_id, pending_global_household)

    # 8. Cancelling after an edit writes nothing.
    def test_cancel_after_edit_writes_nothing(self):
        chat_id = 881511
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(881511001, chat_id, "молока 1 л, а сиру 500 г"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(881511002, chat_id, "❌ Скасувати"))
        mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
