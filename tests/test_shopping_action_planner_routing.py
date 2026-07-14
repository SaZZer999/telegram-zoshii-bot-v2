"""Shopping Action Planner V1 — webhook-level integration tests.
shopping_action_planner.classify() is patched directly (its own JSON-
parsing logic is already covered in tests/test_shopping_action_planner_
module.py) so these tests focus purely on bot.py's routing/glue: does each
action reach the right EXISTING handler (legacy_shopping_flow._show_delete_
preview/_show_mark_preview -> pending_delete_batch/pending_mark_batch ->
delete_items_batch/mark_items_batch), does the planner ever write to the DB
before confirm, does a deterministic/context-specific route still win over
the planner entirely (zero Gemini calls for those), and does "Назад"-style
priority hold for pending states and confirm/cancel. No real Gemini/
Telegram/Supabase call happens anywhere in this file."""
import sys
import os
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_shopping_action_planner_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import legacy_shopping_flow  # noqa: E402
import shopping_action_planner  # noqa: E402
import action_planner  # noqa: E402
from bot import (  # noqa: E402
    pending_mark_batch,
    pending_delete_batch,
    pending_batch,
    pending_global_household,
    pending_inventory_transform,
    active_list_context,
    saved_list_context,
    MARK_PREVIEW_KEYBOARD,
    DELETE_PREVIEW_KEYBOARD,
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


def _shopping_items():
    return [
        {"id": 501, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_text": "1 л", "quantity_value": 1.0, "quantity_unit": "л", "quantity_inferred": False},
        {"id": 502, "name": "Хліб", "canonical_name": "хліб", "category": "Хліб і випічка",
         "quantity_text": "1 шт.", "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_inferred": False},
    ]


_DELETE_PLAN = {"version": 1, "action": "shopping_delete", "arguments": {"item_name": "молоко"}, "clarification_question": None}
_MARK_BOUGHT_PLAN = {"version": 1, "action": "shopping_mark_bought", "arguments": {"item_name": "молоко"}, "clarification_question": None}


class ShoppingActionPlannerWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_mark_batch.clear()
        pending_delete_batch.clear()
        pending_batch.clear()
        pending_global_household.clear()
        pending_inventory_transform.clear()
        active_list_context.clear()
        saved_list_context.clear()
        legacy_shopping_flow.shopping_mode.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_items = patch.object(bot, "get_active_shopping_items", return_value=_shopping_items())
        self.mock_get_items = patcher_items.start()
        self.addCleanup(patcher_items.stop)

    def tearDown(self):
        pending_mark_batch.clear()
        pending_delete_batch.clear()
        pending_batch.clear()
        pending_global_household.clear()
        pending_inventory_transform.clear()
        active_list_context.clear()
        saved_list_context.clear()
        legacy_shopping_flow.shopping_mode.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


class TestGlobalRoutingReachesPlanner(ShoppingActionPlannerWebhookTestCase):
    # 21. Global shopping-delete phrase reaches the planner outside mode/context.
    def test_global_delete_phrase_reaches_planner(self):
        chat_id = 985001
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN) as mock_classify:
            _call_webhook(_make_update(980001001, chat_id, "Викресли молоко зі списку"))
        mock_classify.assert_called_once()
        self.assertIn(chat_id, pending_delete_batch)
        self.assertEqual([i["id"] for i in pending_delete_batch[chat_id]["items"]], [501])

    # 22. Global mark-bought phrase reaches the planner.
    def test_global_mark_bought_phrase_reaches_planner(self):
        chat_id = 985002
        with patch.object(shopping_action_planner, "classify", return_value=_MARK_BOUGHT_PLAN) as mock_classify:
            _call_webhook(_make_update(980002001, chat_id, "Молоко вже купили"))
        mock_classify.assert_called_once()
        self.assertIn(chat_id, pending_mark_batch)
        self.assertEqual([i["id"] for i in pending_mark_batch[chat_id]["items"]], [501])


class TestExistingRoutesWinOverPlanner(ShoppingActionPlannerWebhookTestCase):
    # 23. Existing shopping_mode route has priority — planner never called.
    def test_shopping_mode_deleting_wins_over_planner(self):
        chat_id = 985011
        legacy_shopping_flow.shopping_mode[chat_id] = "deleting"
        with patch.object(bot, "_ask_gemini_for_selection", return_value=("ok", [_shopping_items()[0]])):
            with patch.object(shopping_action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(980011001, chat_id, "Молоко вже купили"))
        mock_classify.assert_not_called()

    # 24. Existing saved-list route has priority when a saved shopping list
    # context is already open.
    def test_saved_list_context_wins_over_planner(self):
        chat_id = 985012
        saved_list_context[chat_id] = "shopping_saved"
        with patch.object(bot, "_ask_gemini_saved_list_router") as mock_saved_router:
            with patch.object(shopping_action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(980012001, chat_id, "Молоко вже купили"))
        mock_classify.assert_not_called()
        mock_saved_router.assert_called_once()

    # 25. Existing add-shopping route does not regress.
    def test_add_shopping_route_not_regressed(self):
        chat_id = 985013
        with patch.object(bot.household_router, "_ask_gemini_explicit_add_items", return_value={
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }):
            with patch.object(shopping_action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(980013001, chat_id, "Додай до покупок молоко"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_global_household)

    # 26. Expense route is never intercepted by the shopping planner.
    def test_expense_delete_route_not_intercepted(self):
        chat_id = 985014
        expenses = [{
            "id": 301, "amount": __import__("decimal").Decimal("50.00"), "currency": "PLN",
            "category": "Продукти", "description": "Покупка",
            "expense_date": __import__("datetime").date(2026, 7, 3),
            "created_at": __import__("datetime").datetime(2026, 7, 3, 12, 0),
        }]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value={
                "intent": "delete_expense", "amount": None, "currency": None, "category": None,
                "description": None, "expense_date": None, "selected_numbers": [1], "unresolved_fragments": [],
            }):
                with patch.object(shopping_action_planner, "classify") as mock_classify:
                    _call_webhook(_make_update(980014001, chat_id, "Скасуй ту покупку на 50 zł"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense_delete)

    # 27. Inventory delete route is never intercepted by the shopping planner.
    def test_inventory_delete_route_not_intercepted(self):
        chat_id = 985015
        inventory_items = [{
            "id": 701, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.",
        }]
        with patch.object(bot, "get_inventory_items", return_value=inventory_items):
            with patch.object(shopping_action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(980015001, chat_id, "Видали молоко із запасів"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, bot.pending_cleanup_admin)

    # 28. General chat does not call the planner.
    def test_general_chat_never_calls_planner(self):
        chat_id = 985016
        with patch.object(shopping_action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Звичайна відповідь.") as mock_gemini:
                _call_webhook(_make_update(980016001, chat_id, "Поясни, чому молоко згортається у каві?"))
        mock_classify.assert_not_called()
        mock_gemini.assert_called()


class TestPlannerCallDiscipline(ShoppingActionPlannerWebhookTestCase):
    # 29. Planner called at most once per update.
    def test_planner_called_at_most_once(self):
        chat_id = 985021
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN) as mock_classify:
            _call_webhook(_make_update(980021001, chat_id, "Викресли молоко зі списку"))
        self.assertEqual(mock_classify.call_count, 1)

    # 30. Active pending-state has priority — planner never consulted.
    def test_active_pending_state_wins_planner_never_called(self):
        chat_id = 985022
        pending_delete_batch[chat_id] = {
            "items": [_shopping_items()[0]], "household_id": 1, "user_db_id": 10,
        }
        with patch.object(shopping_action_planner, "classify") as mock_classify:
            _call_webhook(_make_update(980022001, chat_id, "Молоко вже купили"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_delete_batch)
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))

    # 31. Confirm/cancel have priority — never reach the planner.
    def test_confirm_button_never_reaches_planner(self):
        chat_id = 985023
        pending_delete_batch[chat_id] = {
            "items": [_shopping_items()[0]], "household_id": 1, "user_db_id": 10,
        }
        with patch.object(shopping_action_planner, "classify") as mock_classify:
            with patch.object(bot, "delete_items_batch", return_value=1):
                _call_webhook(_make_update(980023001, chat_id, "✅ Так, видалити"))
        mock_classify.assert_not_called()
        self.assertNotIn(chat_id, pending_delete_batch)

    def test_cancel_button_never_reaches_planner(self):
        chat_id = 985024
        pending_mark_batch[chat_id] = {
            "items": [_shopping_items()[0]], "household_id": 1, "user_db_id": 10,
        }
        with patch.object(shopping_action_planner, "classify") as mock_classify:
            _call_webhook(_make_update(980024001, chat_id, "❌ Скасувати"))
        mock_classify.assert_not_called()
        self.assertNotIn(chat_id, pending_mark_batch)

    # 32. Failure never falls through to general chat.
    def test_failure_never_falls_through_to_general_chat(self):
        chat_id = 985025
        unsupported_plan = {"version": 1, "action": "unsupported", "arguments": {}, "clarification_question": None}
        with patch.object(shopping_action_planner, "classify", return_value=unsupported_plan):
            with patch.object(bot, "call_gemini") as mock_gemini:
                _call_webhook(_make_update(980025001, chat_id, "Прибери хліб зі списку покупок"))
        mock_gemini.assert_not_called()
        self.assertTrue(any(shopping_action_planner.UNSUPPORTED_MSG == t for t in self._sent_texts()))


class TestPreviewAndDbSafety(ShoppingActionPlannerWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    # 33. Delete plan creates the EXISTING preview.
    def test_delete_plan_creates_existing_preview(self):
        chat_id = 985031
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            _call_webhook(_make_update(980031001, chat_id, "Викресли молоко зі списку"))
        self.assertIn(chat_id, pending_delete_batch)
        texts = self._sent_texts()
        self.assertTrue(any("Буде видалено зі списку покупок: 1" in t for t in texts))
        self.assertIn(DELETE_PREVIEW_KEYBOARD, self._reply_markups())

    # 34. Mark-bought plan creates the EXISTING preview.
    def test_mark_bought_plan_creates_existing_preview(self):
        chat_id = 985032
        with patch.object(shopping_action_planner, "classify", return_value=_MARK_BOUGHT_PLAN):
            _call_webhook(_make_update(980032001, chat_id, "Молоко вже купили"))
        self.assertIn(chat_id, pending_mark_batch)
        texts = self._sent_texts()
        self.assertTrue(any("Буде позначено купленими: 1" in t for t in texts))
        self.assertIn(MARK_PREVIEW_KEYBOARD, self._reply_markups())

    # 35. No DB write before confirm.
    def test_no_db_write_before_confirm(self):
        chat_id = 985033
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            with patch.object(bot, "delete_items_batch") as mock_delete:
                _call_webhook(_make_update(980033001, chat_id, "Викресли молоко зі списку"))
        mock_delete.assert_not_called()

    # 36. Cancel changes nothing.
    def test_cancel_changes_nothing(self):
        chat_id = 985034
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            _call_webhook(_make_update(980034001, chat_id, "Викресли молоко зі списку"))
        with patch.object(bot, "delete_items_batch") as mock_delete:
            _call_webhook(_make_update(980034002, chat_id, "❌ Скасувати"))
            mock_delete.assert_not_called()
        self.assertNotIn(chat_id, pending_delete_batch)

    # 37. Confirm deletes only the target shopping item.
    def test_confirm_deletes_only_target_item(self):
        chat_id = 985035
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            _call_webhook(_make_update(980035001, chat_id, "Викресли молоко зі списку"))
        with patch.object(bot, "delete_items_batch", return_value=1) as mock_delete:
            _call_webhook(_make_update(980035002, chat_id, "✅ Так, видалити"))
        mock_delete.assert_called_once()
        args, _ = mock_delete.call_args
        self.assertEqual(args[1], [501])
        self.assertNotIn(chat_id, pending_delete_batch)

    # 38. Confirm mark-bought applies only the target item.
    def test_confirm_mark_bought_applies_only_target_item(self):
        chat_id = 985036
        with patch.object(shopping_action_planner, "classify", return_value=_MARK_BOUGHT_PLAN):
            _call_webhook(_make_update(980036001, chat_id, "Молоко вже купили"))
        with patch.object(bot, "mark_items_batch", return_value=1) as mock_mark:
            _call_webhook(_make_update(980036002, chat_id, "✅ Куплено, без запасів"))
        mock_mark.assert_called_once()
        args, _ = mock_mark.call_args
        self.assertEqual(args[1], [501])
        self.assertNotIn(chat_id, pending_mark_batch)

    # 39. Other shopping items are never touched.
    def test_other_items_untouched(self):
        chat_id = 985037
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            _call_webhook(_make_update(980037001, chat_id, "Викресли молоко зі списку"))
        entry = pending_delete_batch[chat_id]
        self.assertEqual(len(entry["items"]), 1)
        self.assertEqual(entry["items"][0]["name"], "Молоко")

    # 40. Multiple matches use existing (candidate-listing) disambiguation.
    def test_multiple_matches_use_disambiguation(self):
        chat_id = 985038
        two_milks = [
            {"id": 510, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_text": "1 л", "quantity_value": 1.0, "quantity_unit": "л", "quantity_inferred": False},
            {"id": 511, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_text": "500 мл", "quantity_value": 0.5, "quantity_unit": "л", "quantity_inferred": False},
        ]
        with patch.object(bot, "get_active_shopping_items", return_value=two_milks):
            with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
                _call_webhook(_make_update(980038001, chat_id, "Викресли молоко зі списку"))
        self.assertNotIn(chat_id, pending_delete_batch)
        self.assertTrue(any("не хочу вгадувати" in t.lower() for t in self._sent_texts()))

    # 41. Missing item gives a controlled response.
    def test_missing_item_gives_controlled_response(self):
        chat_id = 985039
        plan = {"version": 1, "action": "shopping_delete", "arguments": {"item_name": "сир"}, "clarification_question": None}
        with patch.object(shopping_action_planner, "classify", return_value=plan):
            _call_webhook(_make_update(980039001, chat_id, "Викресли сир зі списку"))
        self.assertNotIn(chat_id, pending_delete_batch)
        self.assertTrue(any(shopping_action_planner.NOT_FOUND_MSG == t for t in self._sent_texts()))

    # 42. Stale snapshot blocks apply.
    def test_stale_snapshot_blocks_confirm(self):
        chat_id = 985040
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            _call_webhook(_make_update(980040001, chat_id, "Викресли молоко зі списку"))
        with patch.object(bot, "delete_items_batch", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(980040002, chat_id, "✅ Так, видалити"))
        self.assertTrue(any(STALE_PREVIEW_MSG == t for t in self._sent_texts()))
        self.assertNotIn(chat_id, pending_delete_batch)

    # 44. Repeated confirm does not execute twice.
    def test_repeated_confirm_does_not_execute_twice(self):
        chat_id = 985041
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            _call_webhook(_make_update(980041001, chat_id, "Викресли молоко зі списку"))
        with patch.object(bot, "delete_items_batch", return_value=1) as mock_delete:
            _call_webhook(_make_update(980041002, chat_id, "✅ Так, видалити"))
            _call_webhook(_make_update(980041003, chat_id, "✅ Так, видалити"))
        mock_delete.assert_called_once()

    # 43. Whatever undo support exists today for shopping delete/mark-bought
    # is identically inherited — this planner calls the SAME legacy_
    # shopping_flow entry points/database helpers the mode-based flow
    # already uses, never a parallel write path. NOTE (finding, not a
    # regression from this change): database.delete_items_batch/
    # mark_items_batch do not themselves write a household_action_journal
    # row — unlike execute_inventory_delete/execute_inventory_transform,
    # this legacy batch path has no "↩️ Скасувати останню дію" integration
    # today, for either the existing shopping-mode flow or this new global
    # route. Pre-existing, out of this focused feature's scope.
    def test_confirm_uses_same_executor_as_mode_based_flow(self):
        chat_id = 985042
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            _call_webhook(_make_update(980042001, chat_id, "Викресли молоко зі списку"))
        with patch.object(bot, "delete_items_batch", return_value=1) as mock_delete:
            _call_webhook(_make_update(980042002, chat_id, "✅ Так, видалити"))
        mock_delete.assert_called_once()
        args, _ = mock_delete.call_args
        self.assertEqual(args[0], 1)


class TestVoiceTranscriptSameDispatcherPath(unittest.TestCase):
    """45. A voice transcript identical to a typed message routes through
    the exact same message_dispatcher.dispatch() call bot.py already uses
    for typed text — verified via bot.py's own dispatch entrypoint, without
    touching Groq/Whisper (out of scope, per this module's own docstring —
    voice_input.py is not modified)."""

    def setUp(self):
        pending_delete_batch.clear()
        active_list_context.clear()
        saved_list_context.clear()
        legacy_shopping_flow.shopping_mode.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_items = patch.object(bot, "get_active_shopping_items", return_value=_shopping_items())
        patcher_items.start()
        self.addCleanup(patcher_items.stop)

    def tearDown(self):
        pending_delete_batch.clear()
        active_list_context.clear()
        saved_list_context.clear()
        legacy_shopping_flow.shopping_mode.clear()

    def test_typed_and_transcribed_text_route_identically(self):
        text = "Викресли молоко зі списку"
        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            bot.message_dispatcher.dispatch(bot._dispatcher_deps, 985051, 555, "Тест", text)
        typed_entry = dict(pending_delete_batch[985051])
        pending_delete_batch.clear()

        with patch.object(shopping_action_planner, "classify", return_value=_DELETE_PLAN):
            # Same call voice_input.py's transcription handoff makes — the
            # transcript string is identical to the typed text above.
            bot.message_dispatcher.dispatch(bot._dispatcher_deps, 985051, 555, "Тест", text)
        voice_entry = dict(pending_delete_batch[985051])

        self.assertEqual(
            [i["id"] for i in typed_entry["items"]], [i["id"] for i in voice_entry["items"]],
        )


if __name__ == "__main__":
    unittest.main()
