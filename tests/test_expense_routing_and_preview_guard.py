import sys
import os
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No real Gemini/Telegram/Supabase
# call happens anywhere in this file — every network-facing bot.py function
# is patched per-test.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    """Invoke the real webhook() dispatch (routing priority and all) inside a
    Flask test request context — no actual HTTP server involved."""
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _add_router_result(amount="14.00", category="Кафе / ресторани", description="Кава", expense_date="2026-07-03"):
    return {
        "intent": "create_expense", "amount": amount, "currency": "PLN", "category": category,
        "description": description, "expense_date": expense_date,
        "selected_numbers": [], "unresolved_fragments": [],
    }


def _delete_router_result(selected_numbers):
    return {
        "intent": "delete_expense", "amount": None, "currency": None, "category": None,
        "description": None, "expense_date": None,
        "selected_numbers": selected_numbers, "unresolved_fragments": [],
    }


def _expense_dict(expense_id, amount, category="Продукти", description="Булочка", expense_date=date(2026, 7, 3)):
    return {
        "id": expense_id, "amount": amount, "currency": "PLN", "category": category,
        "description": description, "expense_date": expense_date, "created_at": None,
    }


class TestExpenseRoutingAndPreviewGuard(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_saved_router = patch.object(bot, "_ask_gemini_saved_list_router")
        self.mock_saved_router = patcher_saved_router.start()
        self.addCleanup(patcher_saved_router.stop)

    def tearDown(self):
        # Defensive cleanup in case a test fails before reaching its own cleanup.
        for d in (bot.pending_expense, bot.pending_expense_delete, bot.expense_delete_selection,
                  bot.pending_delete_batch, bot.pending_alias_action,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 1 — explicit delete phrase from the main menu goes to the delete
    # flow (router called WITH recent_expenses context), never the create router.
    def test_explicit_delete_from_main_menu_goes_to_delete_flow_not_create_router(self):
        chat_id = 960001
        expenses = [_expense_dict(101, Decimal("4.00"))]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])) as mock_router:
                _call_webhook(_make_update(960000001, chat_id, "Видали витрату за булочку 4 zł"))
        mock_router.assert_called_once()
        self.assertEqual(mock_router.call_args.kwargs.get("recent_expenses"), expenses)
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 101)
        self.assertNotIn(chat_id, bot.pending_expense)
        self.mock_call_gemini.assert_not_called()

    # Case 2 — an ordinary amount command still goes to the create flow
    # (router called with NO recent_expenses kwarg — the create-router call shape).
    def test_ordinary_amount_command_still_goes_to_create_flow(self):
        chat_id = 960002
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_add_router_result()) as mock_router:
            _call_webhook(_make_update(960000002, chat_id, "Кава 14 zł"))
        mock_router.assert_called_once()
        self.assertEqual(mock_router.call_args.kwargs, {})
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["amount"], Decimal("14.00"))
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # Case 3 — a bare "Видали булочку" (no mention of "витрат...") outside the
    # expenses menu never starts the delete flow.
    def test_plain_delete_phrase_without_expense_word_does_not_start_delete_flow(self):
        chat_id = 960003
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            with patch.object(bot, "get_recent_expenses_for_deletion") as mock_recent:
                _call_webhook(_make_update(960000003, chat_id, "Видали булочку"))
        mock_router.assert_not_called()
        mock_recent.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertNotIn(chat_id, bot.expense_delete_selection)

    # Case 4 — an active delete preview blocks a new "add"-looking text: no
    # create router call, no replacement of the pending delete preview.
    def test_add_text_does_not_interrupt_active_delete_preview(self):
        chat_id = 960004
        bot.pending_expense_delete[chat_id] = {
            "expense_id": 101, "household_id": 1,
            "snapshot": {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"},
            "origin": "global",
        }
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            _call_webhook(_make_update(960000004, chat_id, "Кава 14 zł"))
        mock_router.assert_not_called()
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 101)
        self.assertNotIn(chat_id, bot.pending_expense)
        self.assertTrue(any("незавершена дія з витратами" in t for t in self._sent_texts()))

    # Case 5 — an active add preview blocks a new "delete"-looking text: no
    # delete router call (no recent-expenses fetch either), no replacement of
    # the pending add preview. Also covers the exact combined repro from the
    # bug report: an add preview shown from the expenses submenu used to fall
    # through into _handle_expense_command's unconditional branch.
    def test_delete_text_does_not_interrupt_active_add_preview(self):
        chat_id = 960005
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("14.00"), "currency": "PLN",
            "category": "Кафе / ресторани", "description": "Кава", "expense_date": date(2026, 7, 3),
            "origin": "expenses_menu",
        }
        bot.active_list_context[chat_id] = "expenses"
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            with patch.object(bot, "get_recent_expenses_for_deletion") as mock_recent:
                _call_webhook(_make_update(960000005, chat_id, "Видали витрату за булочку 4 zł"))
        mock_router.assert_not_called()
        mock_recent.assert_not_called()
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["description"], "Кава")
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertTrue(any("незавершена дія з витратами" in t for t in self._sent_texts()))

    # Case 6 — confirm after such a blocked message still performs the
    # ORIGINAL pending action (delete).
    def test_confirm_after_blocked_text_performs_original_delete(self):
        chat_id = 960006
        bot.pending_expense_delete[chat_id] = {
            "expense_id": 101, "household_id": 1,
            "snapshot": {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"},
            "origin": "global",
        }
        _call_webhook(_make_update(960000006, chat_id, "Кава 14 zł"))  # blocked, no-op
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(960000007, chat_id, "✅ Так, видалити"))
            mock_delete.assert_called_once_with(
                1, 101, {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"}
            )
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # Case 6 (add side) — confirm after a blocked message performs the
    # ORIGINAL pending action (add).
    def test_confirm_after_blocked_text_performs_original_add(self):
        chat_id = 960008
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("14.00"), "currency": "PLN",
            "category": "Кафе / ресторани", "description": "Кава", "expense_date": date(2026, 7, 3),
            "origin": "global",
        }
        _call_webhook(_make_update(960000008, chat_id, "Видали витрату за булочку 4 zł"))  # blocked, no-op
        with patch.object(bot, "add_expense") as mock_add:
            _call_webhook(_make_update(960000009, chat_id, "✅ Так, додати"))
            mock_add.assert_called_once_with(
                1, 10, Decimal("14.00"), "PLN", "Кафе / ресторани", "Кава", date(2026, 7, 3)
            )
        self.assertNotIn(chat_id, bot.pending_expense)

    # Case 7 — cancel after a blocked message clears the ORIGINAL pending
    # preview (delete), and nothing was ever deleted.
    def test_cancel_after_blocked_text_clears_original_delete_preview(self):
        chat_id = 960010
        bot.pending_expense_delete[chat_id] = {
            "expense_id": 101, "household_id": 1,
            "snapshot": {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"},
            "origin": "global",
        }
        _call_webhook(_make_update(960000010, chat_id, "Кава 14 zł"))  # blocked, no-op
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(960000011, chat_id, "❌ Скасувати"))
            mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertTrue(any("Видалення витрати скасовано." in t for t in self._sent_texts()))

    # Case 7 (add side) — cancel after a blocked message clears the ORIGINAL
    # pending preview (add), and nothing was ever added.
    def test_cancel_after_blocked_text_clears_original_add_preview(self):
        chat_id = 960012
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("14.00"), "currency": "PLN",
            "category": "Кафе / ресторани", "description": "Кава", "expense_date": date(2026, 7, 3),
            "origin": "global",
        }
        _call_webhook(_make_update(960000012, chat_id, "Видали витрату за булочку 4 zł"))  # blocked, no-op
        with patch.object(bot, "add_expense") as mock_add:
            _call_webhook(_make_update(960000013, chat_id, "❌ Скасувати"))
            mock_add.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense)
        self.assertTrue(any("Додавання витрати скасовано." in t for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
