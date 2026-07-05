"""Routing Contract v1.

Does NOT re-test any router/flow's full business logic (that's already
covered by test_global_household_operations.py, test_inventory_quantity_
clarification.py, test_global_bare_add.py, test_safe_undo_global_action.py,
etc.) — only the PRECEDENCE order between pending states in bot.py's
webhook() dispatch chain, and that navigation (/start, /menu, "⬅️ Головне
меню") clears every interaction state via clear_interaction_state() so the
next command is always treated as new. No real Gemini/Telegram/Supabase
call happens anywhere in this file.
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
import action_history  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    pending_inventory_quantity_clarification,
    pending_add_destination_clarification,
    pending_undo_action,
    pending_expense,
    active_list_context,
)

_ALL_PENDING_DICTS = (
    pending_global_household,
    pending_inventory_quantity_clarification,
    pending_add_destination_clarification,
    pending_undo_action,
    pending_expense,
)


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
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class RoutingContractTestCase(unittest.TestCase):
    """Shared setup: mock every side-effecting/external call so a
    precedence test only ever asserts "was this reached or not", never
    depends on a real DB/Gemini/Telegram response."""

    def setUp(self):
        for d in _ALL_PENDING_DICTS:
            d.clear()
        active_list_context.clear()

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini = patch.object(bot, "call_gemini")
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

        patcher_household_router = patch.object(bot.household_router, "_ask_gemini_household_router")
        self.mock_household_router = patcher_household_router.start()
        self.addCleanup(patcher_household_router.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_get_latest_undoable = patch.object(bot, "get_latest_undoable_action", return_value=None)
        self.mock_get_latest_undoable = patcher_get_latest_undoable.start()
        self.addCleanup(patcher_get_latest_undoable.stop)

    def tearDown(self):
        for d in _ALL_PENDING_DICTS:
            d.clear()
        active_list_context.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestActiveGlobalPreviewBlocksNewCommand(RoutingContractTestCase):
    """#1: an active combined preview blocks a brand-new household command —
    the Global Router never gets a second, conflicting pass while one is
    already awaiting confirm/cancel."""

    def test_new_household_phrase_is_blocked_by_pending_preview(self):
        chat_id = 9001
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(chat_id, chat_id, "Купив молоко за 10 zł"))

        self.mock_household_router.assert_not_called()
        self.mock_gemini.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        self.assertIn(bot.GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG, self._sent_texts())


class TestQuantityClarificationPriority(RoutingContractTestCase):
    """#2: pending quantity clarification outranks both the Global Router
    and general AI-chat — any other text just re-asks the clarification."""

    def test_household_phrase_does_not_reach_router_or_ai(self):
        chat_id = 9002
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "new_expense": None, "delete_expense": None,
        }
        _call_webhook(_make_update(chat_id, chat_id, "Купив молоко за 10 zł"))

        self.mock_household_router.assert_not_called()
        self.mock_gemini.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(bot._GLOBAL_QUANTITY_CLARIFICATION_INVALID_MSG, self._sent_texts())


class TestDestinationClarificationPriority(RoutingContractTestCase):
    """#3: pending destination clarification outranks explicit add, the
    Global Router, and general AI-chat."""

    def test_explicit_add_phrase_does_not_reach_router_or_ai(self):
        chat_id = 9003
        pending_add_destination_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global", "validated_items": [],
        }
        # "Додай в запаси молоко" both looks like an explicit-destination add
        # AND is not one of the fixed "До покупок"/"У запаси" answer phrases
        # parse_add_destination_answer accepts — proves the clarification
        # branch (not explicit add) is what actually handled it.
        _call_webhook(_make_update(chat_id, chat_id, "Додай в запаси молоко"))

        self.mock_household_router.assert_not_called()
        self.mock_gemini.assert_not_called()
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertIn(bot.ADD_DESTINATION_CLARIFICATION_INVALID_MSG, self._sent_texts())


class TestPendingUndoPriority(RoutingContractTestCase):
    """#4: pending undo outranks household commands and general AI-chat —
    while an undo confirm/cancel is awaited, nothing else can run."""

    def test_household_phrase_does_not_reach_router_or_ai(self):
        chat_id = 9004
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(chat_id, chat_id, "Купив молоко за 10 zł"))

        self.mock_household_router.assert_not_called()
        self.mock_gemini.assert_not_called()
        self.assertIn(chat_id, pending_undo_action)
        self.assertIn(action_history.PENDING_UNDO_MSG, self._sent_texts())


class TestExpensePreviewOutranksUndoCommand(RoutingContractTestCase):
    """#5: an active expense preview outranks even a natural-text undo
    command — "Скасувати останню дію" typed mid-expense-preview must not
    silently start a different flow."""

    def test_undo_phrase_is_blocked_by_expense_preview(self):
        chat_id = 9005
        pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": "10.00", "currency": "PLN",
            "category": "Продукти", "description": "Молоко", "expense_date": "2026-07-05",
            "origin": "global",
        }
        _call_webhook(_make_update(chat_id, chat_id, "Скасувати останню дію"))

        self.mock_get_latest_undoable.assert_not_called()
        self.assertNotIn(chat_id, pending_undo_action)
        self.assertIn(chat_id, pending_expense)
        self.assertIn(bot.EXPENSE_PREVIEW_GUARD_MSG, self._sent_texts())


class TestNavigationClearsInteractionState(RoutingContractTestCase):
    """#6-#9: /start, /menu, and "⬅️ Головне меню" all clear every pending
    interaction state via the single clear_interaction_state() helper —
    including pending_global_household, which none of the 3 old duplicated
    cleanup blocks used to clear at all."""

    def test_main_menu_button_clears_global_preview(self):
        chat_id = 9006
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(chat_id, chat_id, "⬅️ Головне меню"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_menu_command_clears_quantity_clarification(self):
        chat_id = 9007
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "new_expense": None, "delete_expense": None,
        }
        _call_webhook(_make_update(chat_id, chat_id, "/menu"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)

    def test_start_command_clears_destination_clarification(self):
        chat_id = 9008
        pending_add_destination_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global", "validated_items": [],
        }
        _call_webhook(_make_update(chat_id, chat_id, "/start"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)

    def test_navigation_clears_pending_undo(self):
        for i, (text, chat_id) in enumerate((
            ("/start", 9009), ("/menu", 9010), ("⬅️ Головне меню", 9011),
        )):
            pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
            _call_webhook(_make_update(chat_id, chat_id, text))
            self.assertNotIn(chat_id, pending_undo_action)


class TestNavigationStartsCleanSlate(RoutingContractTestCase):
    """#10: after navigation, the next message is handled as a brand-new
    command, never as a continuation of whatever was pending before —
    proven by NOT getting the destination-clarification "invalid answer"
    reply for a message sent right after "⬅️ Головне меню" cleared it."""

    def test_message_after_navigation_does_not_continue_old_clarification(self):
        chat_id = 9012
        pending_add_destination_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global", "validated_items": [],
        }
        _call_webhook(_make_update(chat_id, chat_id, "⬅️ Головне меню"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)

        self.mock_send.reset_mock()
        _call_webhook(_make_update(chat_id + 1, chat_id, "Привіт, як справи?"))

        self.assertNotIn(bot.ADD_DESTINATION_CLARIFICATION_INVALID_MSG, self._sent_texts())
        self.assertNotIn(chat_id, pending_add_destination_clarification)


class TestExistingConfirmCancelStillWork(RoutingContractTestCase):
    """#11: consolidating the 3 navigation cleanup blocks into
    clear_interaction_state() must not touch confirm/cancel handling for
    any flow — spot-checked here for the two flows this task's cleanup fix
    directly concerns (global preview, undo)."""

    def test_global_preview_cancel_button_still_clears_it(self):
        chat_id = 9013
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(chat_id, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertIn("Зміни скасовано.", self._sent_texts())

    def test_undo_cancel_button_still_clears_it(self):
        chat_id = 9014
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(chat_id, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_undo_action)
        self.assertIn(action_history.UNDO_CANCELLED_MSG, self._sent_texts())


if __name__ == "__main__":
    unittest.main()
