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
    pending_inventory_representation_clarification,
    pending_add_destination_clarification,
    pending_undo_action,
    pending_expense,
    pending_saved_edit,
    active_list_context,
)

_ALL_PENDING_DICTS = (
    pending_global_household,
    pending_inventory_quantity_clarification,
    pending_inventory_representation_clarification,
    pending_add_destination_clarification,
    pending_undo_action,
    pending_expense,
    pending_saved_edit,
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
        # Pending Preview Edit Planner V1: household_router itself is still
        # never reached (the pending preview blocks a new command from ever
        # starting a fresh router pass) — but the deterministic preview-edit
        # handlers now hand off to a semantic Gemini fallback before the
        # guard message, so call_gemini IS invoked once (resolves safely to
        # "no_change" here — its return value isn't configured in this
        # test's setUp).
        self.mock_gemini.assert_called_once()
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


class TestUndoButtonVariationSelector(RoutingContractTestCase):
    """Telegram may send the undo button's label with or without its
    Unicode variation selector (U+FE0F) depending on client/cache — "↩
    Скасувати останню дію" must start the same undo flow as "↩️ Скасувати
    останню дію"."""

    def test_undo_button_without_variation_selector_starts_undo_flow(self):
        chat_id = 9015
        _call_webhook(_make_update(chat_id, chat_id, "↩ Скасувати останню дію"))
        self.mock_get_latest_undoable.assert_called_once()
        self.assertIn(action_history.NO_UNDOABLE_ACTION_MSG, self._sent_texts())

    def test_undo_button_with_variation_selector_still_starts_undo_flow(self):
        chat_id = 9016
        _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))
        self.mock_get_latest_undoable.assert_called_once()
        self.assertIn(action_history.NO_UNDOABLE_ACTION_MSG, self._sent_texts())


class TestUndoButtonCancelsQuantityClarification(RoutingContractTestCase):
    """Reproduces the live bug end-to-end: while an inventory quantity
    clarification is active ("У запасах уже є кілька записів «Молоко»..."),
    pressing the exact undo button must cancel THAT clarification instead
    of opening the historical undo preview."""

    _MILK_ITEM = {
        "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
        "quantity_value": 1.0, "quantity_unit": "шт.",
        "quantity_text": "1 шт.", "quantity_inferred": True, "was_corrected": False,
    }
    _MILK_LITERS_ROW = {
        "id": 201, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
        "quantity_value": 7.0, "quantity_unit": "л", "quantity_text": "7 л", "quantity_inferred": False,
    }
    _MILK_PIECES_ROW = {
        "id": 202, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
        "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False,
    }

    def _seed_milk_clarification(self, chat_id):
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [dict(self._MILK_ITEM)], "consume_changes": [],
            "new_expense": None, "delete_expense": None,
        }

    def test_button_with_variation_selector_cancels_clarification(self):
        chat_id = 9017
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))

        self.mock_get_latest_undoable.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        texts = self._sent_texts()
        self.assertNotIn(bot._GLOBAL_QUANTITY_CLARIFICATION_INVALID_MSG, texts)
        self.assertNotIn(action_history.NO_UNDOABLE_ACTION_MSG, texts)
        self.assertIn("Поточну дію скасовано.", texts)

    def test_button_without_variation_selector_cancels_clarification(self):
        chat_id = 9018
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(chat_id, chat_id, "↩ Скасувати останню дію"))

        self.mock_get_latest_undoable.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        texts = self._sent_texts()
        self.assertNotIn(bot._GLOBAL_QUANTITY_CLARIFICATION_INVALID_MSG, texts)
        self.assertIn("Поточну дію скасовано.", texts)

    def test_ordinary_reply_still_reaches_quantity_clarification(self):
        chat_id = 9019
        self._seed_milk_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[self._MILK_LITERS_ROW, self._MILK_PIECES_ROW]):
            _call_webhook(_make_update(chat_id, chat_id, "1 л"))

        self.mock_get_latest_undoable.assert_not_called()
        # The reply resolved the conflict normally (never got near undo) —
        # the clarification state is gone because it turned into a combined
        # preview, not because the undo-button cancel path touched it.
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(chat_id, pending_global_household)
        self.assertNotIn("Поточну дію скасовано.", self._sent_texts())

    def test_invalid_reply_still_shows_quantity_help_message(self):
        chat_id = 9020
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(chat_id, chat_id, "багато"))

        self.mock_get_latest_undoable.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(bot._GLOBAL_QUANTITY_CLARIFICATION_INVALID_MSG, self._sent_texts())


class TestUndoButtonCancelsRepresentationClarification(RoutingContractTestCase):
    """Same fix, representation-clarification side: an active count-vs-
    mass/volume conflict must be cancelled by the exact undo button too,
    never routed to historical undo."""

    def _seed_representation_clarification(self, chat_id):
        pending_inventory_representation_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "stage": "choice", "conflict": {}, "queue": [],
            "add_shopping_items": [], "add_inventory_items": [], "inventory_merge_targets": [],
            "consume_changes": [], "new_expenses": [], "new_expense": None,
            "delete_expense": None, "representation_resolutions": [],
        }

    def test_button_cancels_clarification(self):
        chat_id = 9021
        self._seed_representation_clarification(chat_id)
        _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))

        self.mock_get_latest_undoable.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)
        self.assertIn("Поточну дію скасовано.", self._sent_texts())


class TestUndoButtonCancelsGlobalHouseholdPreview(RoutingContractTestCase):
    """An active combined Global Household Router preview must be cancelled
    by the exact undo button, never routed to historical undo."""

    def test_button_cancels_preview(self):
        chat_id = 9022
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))

        self.mock_get_latest_undoable.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertIn("Поточну дію скасовано.", self._sent_texts())


class TestUndoButtonCancelsSavedEditPreview(RoutingContractTestCase):
    """An active saved-list edit preview must be cancelled by the exact
    undo button, never routed to historical undo."""

    def test_button_cancels_saved_edit_preview(self):
        chat_id = 9023
        pending_saved_edit[chat_id] = {
            "items_snapshot": [], "validated_updates": [], "household_id": 1,
            "user_db_id": 10, "context_type": "shopping_saved",
        }
        _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))

        self.mock_get_latest_undoable.assert_not_called()
        self.assertNotIn(chat_id, pending_saved_edit)
        self.assertIn("Поточну дію скасовано.", self._sent_texts())


class TestUndoButtonWithoutActivePendingOpensHistoricalUndo(RoutingContractTestCase):
    """With no active clarification/preview at all, the exact undo button
    still opens the normal historical undo preview, exactly as before."""

    def test_button_opens_historical_undo_when_nothing_pending(self):
        chat_id = 9024
        _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))

        self.mock_get_latest_undoable.assert_called_once()
        self.assertIn(action_history.NO_UNDOABLE_ACTION_MSG, self._sent_texts())
        self.assertNotIn("Поточну дію скасовано.", self._sent_texts())


if __name__ == "__main__":
    unittest.main()
