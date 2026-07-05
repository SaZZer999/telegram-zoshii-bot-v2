import sys
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No real Gemini/Telegram/Supabase
# call happens anywhere in this file — every network-facing function is
# patched per-test.
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
    active_list_context,
    saved_list_context,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
    MAIN_KEYBOARD,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _pending_milk_preview(chat_id):
    """A pending combined preview identical in shape to what
    _try_global_household_router would have stored after a confirmed "Купив
    молоко за 10 zł"-style compound command."""
    pending_global_household[chat_id] = {
        "add_shopping_items": [], "add_inventory_items": [{
            "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
            "quantity_value": Decimal("1"), "quantity_unit": "л", "quantity_text": "1 л",
            "quantity_inferred": False, "was_corrected": False,
        }],
        "consume_changes": [], "inventory_targets": [],
        "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


class TestPendingGlobalHouseholdBlocksNewText(unittest.TestCase):
    """Bug 2: while pending_global_household is active for a chat, ANY text
    that isn't the exact confirm/cancel button must be fully intercepted —
    never reach the household router, any legacy flow, the database, or
    general AI-chat, and never silently replace the pending preview."""

    def setUp(self):
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_saved_router = patch.object(bot, "_ask_gemini_saved_list_router")
        self.mock_saved_router = patcher_saved_router.start()
        self.addCleanup(patcher_saved_router.stop)

        patcher_hr = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_hr = patcher_hr.start()
        self.addCleanup(patcher_hr.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

    def tearDown(self):
        for d in (pending_global_household, active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 6 — plain quantity-looking text never reaches general AI-chat
    def test_bare_quantity_text_does_not_reach_ai_chat(self):
        chat_id = 991001
        _pending_milk_preview(chat_id)
        _call_webhook(_make_update(991000001, chat_id, "1 Л"))
        self.mock_call_gemini.assert_not_called()
        self.mock_saved_router.assert_not_called()
        self.mock_hr.assert_not_called()
        self.mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))

    # Case 7 — a new household-shaped command does not start another preview
    def test_new_household_command_does_not_create_another_preview(self):
        chat_id = 991002
        _pending_milk_preview(chat_id)
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(991000002, chat_id, "Купив банани"))
        self.mock_hr.assert_not_called()
        self.assertEqual(pending_global_household[chat_id], original)
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))

    def test_guard_never_touches_the_database(self):
        chat_id = 991003
        _pending_milk_preview(chat_id)
        with patch.object(bot, "get_inventory_items") as mock_inv, \
             patch.object(bot, "get_active_shopping_items") as mock_shop:
            _call_webhook(_make_update(991000003, chat_id, "Купив банани і сир"))
            mock_inv.assert_not_called()
            mock_shop.assert_not_called()

    # Case 8 — confirm still works exactly as before with the guard in place
    def test_confirm_still_applies_pending_preview(self):
        chat_id = 991004
        _pending_milk_preview(chat_id)
        self.mock_apply.return_value = {
            "shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
            "inventory_removed": 0, "expense_added_id": None, "expense_deleted": False,
        }
        _call_webhook(_make_update(991000004, chat_id, "✅ Так, застосувати"))
        self.mock_apply.assert_called_once()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("✅ Зміни застосовано." in t for t in self._sent_texts()))

    # Case 8 — cancel still works exactly as before with the guard in place
    def test_cancel_still_clears_pending_preview(self):
        chat_id = 991005
        _pending_milk_preview(chat_id)
        _call_webhook(_make_update(991000005, chat_id, "❌ Скасувати"))
        self.mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))

    # Sanity — without an active pending_global_household, the guard must
    # not fire and ordinary text still reaches AI-chat as before.
    def test_no_pending_preview_reaches_ai_chat_normally(self):
        chat_id = 991006
        _call_webhook(_make_update(991000006, chat_id, "Яка сьогодні погода?"))
        self.mock_call_gemini.assert_called_once()
        self.assertTrue(all(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG != t for t in self._sent_texts()))


if __name__ == '__main__':
    unittest.main()
