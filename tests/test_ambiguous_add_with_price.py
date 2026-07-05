import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No test in this file calls real
# Gemini, Telegram, Render, or Supabase — every network-facing function
# (_ask_gemini_explicit_add_items, _ask_gemini_household_router,
# _ask_gemini_expense_router, call_gemini, get_household_and_user,
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
    pending_expense,
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


class _BaseAmbiguousAddTestCase(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
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

        patcher_expense_router = patch.object(bot, "_ask_gemini_expense_router")
        self.mock_expense_router = patcher_expense_router.start()
        self.addCleanup(patcher_expense_router.stop)

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
            pending_add_destination_clarification, pending_expense,
            active_list_context, saved_list_context,
        ):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


_GUARD_MSG_MARKER = "Команда «Додай ... за суму» неоднозначна."


class TestAmbiguousAddIsBlocked(_BaseAmbiguousAddTestCase):
    # Case 1 — bare "Додай ... за 10 zł" is blocked before the expense gate.
    def test_bare_add_with_full_zloty_marker_is_blocked(self):
        chat_id = 999001
        _call_webhook(_make_update(999000001, chat_id, "Додай молоко за 10 zł"))
        texts = self._sent_texts()
        self.assertTrue(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_expense)
        self.mock_apply.assert_not_called()

    # Case 2 — the short "z" marker is blocked exactly the same way.
    def test_bare_add_with_short_zloty_marker_is_blocked(self):
        chat_id = 999002
        _call_webhook(_make_update(999000002, chat_id, "Додай молоко за 10 z"))
        texts = self._sent_texts()
        self.assertTrue(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertNotIn(chat_id, pending_global_household)

    # Case 3 — an explicit destination phrase with an amount is blocked too,
    # regardless of destination.
    def test_explicit_shopping_destination_with_amount_is_blocked(self):
        chat_id = 999003
        _call_webhook(_make_update(999000003, chat_id, "Додай до покупок хліб за 5,50 zł"))
        texts = self._sent_texts()
        self.assertTrue(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertNotIn(chat_id, pending_global_household)

    def test_explicit_inventory_destination_with_amount_is_blocked(self):
        chat_id = 999004
        _call_webhook(_make_update(999000004, chat_id, "Додай в запаси молоко за 10 zł"))
        texts = self._sent_texts()
        self.assertTrue(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertNotIn(chat_id, pending_global_household)


class TestAmbiguousAddNoSideEffects(_BaseAmbiguousAddTestCase):
    # Case 4 — the guard never calls Gemini, the DB helper, or the expense
    # parser/router — it fires before any of them are ever reached.
    def test_guard_never_calls_gemini_db_or_expense_router(self):
        chat_id = 999010
        _call_webhook(_make_update(999000010, chat_id, "Додай молоко за 10 zł"))
        self.mock_call_gemini.assert_not_called()
        self.mock_items.assert_not_called()
        self.mock_hr.assert_not_called()
        self.mock_expense_router.assert_not_called()
        self.mock_get_user.assert_not_called()
        self.mock_apply.assert_not_called()


class TestExistingFlowsUnaffected(_BaseAmbiguousAddTestCase):
    # Case 5 — "Купив ... за суму" stays the existing compound (shopping/
    # inventory + expense) flow, untouched by the new guard.
    def test_bought_with_price_stays_compound_flow(self):
        chat_id = 999020
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": "2020-01-01"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(999000020, chat_id, "Купив молоко за 10 zł"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.assertIsNotNone(payload["new_expense"])

    def test_bought_with_short_zloty_marker_stays_compound_flow(self):
        chat_id = 999021
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
                {"type": "add_expense", "amount": "5", "currency": "PLN", "category": "Продукти",
                 "description": "Хліб", "expense_date": "2020-01-01"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(999000021, chat_id, "Купив хліб за 5 z"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_global_household)

    # Case 6 — a bare "Молоко 10 zł" (no "Додай" verb at all) stays the
    # existing plain expense-add flow.
    def test_bare_amount_stays_expense_flow(self):
        chat_id = 999022
        self.mock_expense_router.return_value = {
            "intent": "create_expense", "currency": "PLN", "amount": "10",
            "expense_date": "2020-01-01", "category": "Продукти", "description": "Молоко",
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(999000022, chat_id, "Молоко 10 zł"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_expense)

    def test_explicit_expense_command_stays_expense_flow(self):
        chat_id = 999023
        self.mock_expense_router.return_value = {
            "intent": "create_expense", "currency": "PLN", "amount": "120",
            "expense_date": "2020-01-01", "category": "Дім і рахунки", "description": "Інтернет",
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(999000023, chat_id, "Запиши 120 zł за інтернет"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_expense)

    # Case 7 — "Додай витрату 10 zł" explicitly means "add an expense", not
    # "add an item" — never blocked by the new guard, stays the expense flow.
    def test_add_expense_phrase_not_blocked(self):
        chat_id = 999024
        self.mock_expense_router.return_value = {
            "intent": "create_expense", "currency": "PLN", "amount": "10",
            "expense_date": "2020-01-01", "category": "Продукти", "description": "Витрата",
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(999000024, chat_id, "Додай витрату 10 zł"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_expense)

    # Explicit Add without a price, and bare "Додай молоко", both keep
    # working exactly as before — the guard only fires when an amount is
    # present.
    def test_explicit_add_without_price_unaffected(self):
        chat_id = 999025
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(999000025, chat_id, "Додай до покупок молоко"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_global_household)

    def test_bare_add_without_price_unaffected(self):
        chat_id = 999026
        active_list_context[chat_id] = "shopping"
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(999000026, chat_id, "Додай молоко"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_global_household)


class TestExistingPendingStateHasPriority(_BaseAmbiguousAddTestCase):
    # Case 8 — an already-active combined preview takes priority over the
    # new guard; the guard must never even be evaluated.
    def test_active_global_preview_blocks_the_new_guard(self):
        chat_id = 999030
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "new_expense": None, "delete_expense": None, "inventory_targets": [],
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(999000030, chat_id, "Додай молоко за 10 zł"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        self.assertIn(chat_id, pending_global_household)

    # Case 9 — an already-active destination clarification takes priority
    # over the new guard too.
    def test_active_destination_clarification_blocks_the_new_guard(self):
        chat_id = 999031
        pending_add_destination_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "validated_items": [{
                "name": "Хліб", "canonical_name": "хліб", "category": "Хліб і випічка",
                "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True,
            }],
        }
        _call_webhook(_make_update(999000031, chat_id, "Додай молоко за 10 zł"))
        texts = self._sent_texts()
        self.assertFalse(any(_GUARD_MSG_MARKER in t for t in texts))
        # An invalid destination answer re-asks the same question — the
        # pending clarification is never cleared, never replaced.
        self.assertIn(chat_id, pending_add_destination_clarification)


if __name__ == "__main__":
    unittest.main()
