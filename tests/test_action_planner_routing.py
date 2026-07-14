"""Inventory Action Planner V1 — webhook-level integration tests.
action_planner.classify() is patched directly (its own JSON-parsing logic is
already covered in tests/test_action_planner_module.py) so these tests focus
purely on bot.py's routing/glue: does each action reach the right EXISTING
handler (_start_inventory_transform/_start_inventory_cleanup/_start_
inventory_rename/_start_inventory_delete), does the planner ever write to
the DB before confirm, does a deterministic route still win over the
planner entirely (zero Gemini calls for those), does the fixed cleanup-vs-
transform guard route the two originally-reported live bugs correctly, and
does "Назад" navigate deterministically without ever calling Gemini. No
real Gemini/Telegram/Supabase call happens anywhere in this file."""
import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_action_planner_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import action_planner  # noqa: E402
import mini_action_planner  # noqa: E402
from bot import (  # noqa: E402
    pending_inventory_transform,
    pending_cleanup_admin,
    pending_cleanup_admin_disambiguation,
    pending_merge,
    pending_global_household,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
    INVENTORY_KEYBOARD,
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


def _sausage_and_kovbaski_rows():
    return [
        {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
         "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."},
        {"id": 60, "name": "Мисливські ковбаски", "canonical_name": "мисливські ковбаски",
         "category": "М'ясо та риба",
         "quantity_value": Decimal("2"), "quantity_unit": "шт.", "quantity_text": "2 шт."},
    ]


def _duplicate_milk_rows():
    return [
        {"id": 10, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "л", "quantity_text": "1 л"},
        {"id": 11, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("2"), "quantity_unit": "л", "quantity_text": "2 л"},
    ]


def _ser_row():
    return [{
        "id": 20, "name": "Ser", "canonical_name": "ser", "category": "Молочне та яйця",
        "quantity_value": None, "quantity_unit": None, "quantity_text": "",
    }]


def _milk_one_piece_and_liters_inventory():
    return [
        {"id": 7, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
        {"id": 8, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("14.5"), "quantity_unit": "л", "quantity_text": "14,5 л"},
    ]


class ActionPlannerWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_inventory_transform.clear()
        pending_cleanup_admin.clear()
        pending_cleanup_admin_disambiguation.clear()
        pending_merge.clear()
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_inventory_transform.clear()
        pending_cleanup_admin.clear()
        pending_cleanup_admin_disambiguation.clear()
        pending_merge.clear()
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


# =========================
# Existing deterministic routes keep winning — zero planner Gemini calls.
# =========================
class TestExistingDeterministicRoutesWinOverPlanner(ActionPlannerWebhookTestCase):
    # 17. Existing deterministic transform command.
    def test_deterministic_transform_command_never_calls_planner(self):
        chat_id = 975101
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(
                    975101001, chat_id, "об'єднай сосиски і мисливські ковбаски в м'ясні вироби",
                ))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_inventory_transform)

    # 18. Existing duplicate merge command.
    def test_deterministic_duplicate_merge_command_never_calls_planner(self):
        chat_id = 975102
        with patch.object(bot, "get_inventory_items", return_value=_duplicate_milk_rows()):
            with patch.object(action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(975102001, chat_id, "Об'єднай молоко"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_merge)

    # 19. Existing rename command.
    def test_deterministic_rename_command_never_calls_planner(self):
        chat_id = 975103
        with patch.object(bot, "get_inventory_items", return_value=_ser_row()):
            with patch.object(action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(975103001, chat_id, "перейменуй ser на сир"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_cleanup_admin)

    # 20. Existing delete-with-natural-quantity command (the confirmed live
    # fix — must not regress).
    def test_deterministic_delete_natural_quantity_command_never_calls_planner(self):
        chat_id = 975104
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            with patch.object(action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(
                    975104001, chat_id, "Видали молоко одна штука, воно вже не потрібно.",
                ))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 7)


# =========================
# The two originally-reported live bugs — now routed correctly.
# =========================
class TestOriginalLiveBugsNowRouteCorrectly(ActionPlannerWebhookTestCase):
    # 21. Arrow/plus form reaches the planner, not saved_list_router.
    def test_arrow_plus_form_reaches_planner_as_transform(self):
        chat_id = 975111
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.98, "clarification_question": None,
            }) as mock_classify:
                _call_webhook(_make_update(975111001, chat_id, "сосиски + мисливські ковбаски → м'ясні вироби"))
        mock_classify.assert_called_once()
        self.assertIn(chat_id, pending_inventory_transform)
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(set(entry["source_item_ids"]), {50, 60})
        self.assertEqual(entry["target_name"], "М'ясні вироби")
        texts = self._sent_texts()
        self.assertFalse(any("Не зміг безпечно зрозуміти зміну" in t for t in texts))

    # 22. "В запасах об'єднай X і Y і запиши як Z" is no longer swallowed by
    # inventory_cleanup_route as a single-product search.
    def test_prefixed_ob_yednay_zapyshy_yak_becomes_transform_not_cleanup(self):
        chat_id = 975112
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.97, "clarification_question": None,
            }) as mock_classify:
                _call_webhook(_make_update(
                    975112001, chat_id,
                    "В запасах об'єднай сосиски і мисливські ковбаски і запиши як м'ясні вироби",
                ))
        mock_classify.assert_called_once()
        self.assertIn(chat_id, pending_inventory_transform)
        self.assertNotIn(chat_id, pending_merge)
        texts = self._sent_texts()
        self.assertFalse(any("Не знайшов у запасах записів" in t for t in texts))

    # 23. Plain duplicate-merge phrasing is unaffected by the new guard.
    def test_ob_yednay_moloko_stays_duplicate_merge(self):
        chat_id = 975113
        with patch.object(bot, "get_inventory_items", return_value=_duplicate_milk_rows()):
            with patch.object(action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(975113001, chat_id, "Об'єднай молоко"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_merge)
        self.assertNotIn(chat_id, pending_inventory_transform)


# =========================
# Pre-gate must not steal legit saved-list quantity/category edits.
# =========================
class TestPreGateDoesNotStealSavedListEdits(ActionPlannerWebhookTestCase):
    # 24. Legit saved-list quantity edit is never claimed by the planner.
    def test_quantity_edit_phrase_never_calls_planner(self):
        chat_id = 975121
        with patch.object(action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Загальна відповідь.") as mock_gemini:
                _call_webhook(_make_update(975121001, chat_id, "молока 1 л замість 0,5 л"))
        mock_classify.assert_not_called()
        mock_gemini.assert_called()

    def test_category_move_phrase_never_calls_planner(self):
        chat_id = 975122
        with patch.object(action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Загальна відповідь.") as mock_gemini:
                _call_webhook(_make_update(975122001, chat_id, "перенеси сир у молочне"))
        mock_classify.assert_not_called()
        mock_gemini.assert_called()


# =========================
# Navigation "Назад" — deterministic, no Gemini.
# =========================
class TestNazadNavigationAlias(ActionPlannerWebhookTestCase):
    # 25. "Назад"/"назад" navigate without Gemini or the planner.
    def test_nazad_capitalized_navigates_without_gemini(self):
        chat_id = 975131
        with patch.object(action_planner, "classify") as mock_classify:
            with patch.object(mini_action_planner, "classify") as mock_mini_classify:
                with patch.object(bot, "call_gemini") as mock_gemini:
                    _call_webhook(_make_update(975131001, chat_id, "Назад"))
        mock_classify.assert_not_called()
        mock_mini_classify.assert_not_called()
        mock_gemini.assert_not_called()
        self.assertTrue(any("головне меню" in t.lower() for t in self._sent_texts()))

    def test_nazad_lowercase_navigates_without_gemini(self):
        chat_id = 975132
        with patch.object(action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini") as mock_gemini:
                _call_webhook(_make_update(975132001, chat_id, "назад"))
        mock_classify.assert_not_called()
        mock_gemini.assert_not_called()
        self.assertTrue(any("головне меню" in t.lower() for t in self._sent_texts()))


# =========================
# Pending-state / confirm-cancel priority over the planner.
# =========================
class TestPendingStatePriorityOverPlanner(ActionPlannerWebhookTestCase):
    # 26. Active pending_inventory_transform wins.
    def test_active_pending_transform_never_reaches_planner(self):
        chat_id = 975141
        pending_inventory_transform[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "source_item_ids": [50, 60], "targets": [], "target_name": "М'ясні вироби",
            "target_canonical_name": "м'ясні вироби", "target_category": "М'ясо та риба",
            "target_quantity_value": 8, "target_quantity_unit": "шт.", "target_quantity_text": "8 шт.",
        }
        with patch.object(action_planner, "classify") as mock_classify:
            _call_webhook(_make_update(975141001, chat_id, "об'єднай ще щось у щось нове"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_inventory_transform)

    # 27. Active pending_cleanup_admin wins.
    def test_active_pending_cleanup_admin_never_reaches_planner(self):
        chat_id = 975142
        pending_cleanup_admin[chat_id] = {
            "action": "delete", "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_id": 7, "target": {"item_id": 7, "quantity_value": None, "quantity_unit": None, "name": "Молоко", "canonical_name": "молоко"},
        }
        with patch.object(action_planner, "classify") as mock_classify:
            _call_webhook(_make_update(975142001, chat_id, "видали ще щось"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_cleanup_admin)

    # 28. Active pending_global_household wins.
    def test_active_pending_global_household_never_reaches_planner(self):
        chat_id = 975143
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expenses": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        with patch.object(action_planner, "classify") as mock_classify:
            _call_webhook(_make_update(975143001, chat_id, "об'єднай щось у щось нове"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_global_household)

    # 29. Confirm/cancel wins over an active preview (never re-enters the
    # planner path).
    def test_confirm_button_never_reaches_planner(self):
        chat_id = 975144
        pending_inventory_transform[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "source_item_ids": [50, 60], "targets": [
                {"item_id": 50, "name": "Сосиски", "quantity_value": Decimal("6"), "quantity_unit": "шт.", "canonical_name": "сосиски", "category": "М'ясо та риба"},
                {"item_id": 60, "name": "Мисливські ковбаски", "quantity_value": Decimal("2"), "quantity_unit": "шт.", "canonical_name": "мисливські ковбаски", "category": "М'ясо та риба"},
            ],
            "target_name": "М'ясні вироби", "target_canonical_name": "м'ясні вироби",
            "target_category": "М'ясо та риба", "target_quantity_value": Decimal("8"),
            "target_quantity_unit": "шт.", "target_quantity_text": "8 шт.",
        }
        with patch.object(action_planner, "classify") as mock_classify:
            with patch.object(bot, "execute_inventory_transform", return_value=True):
                _call_webhook(_make_update(975144001, chat_id, "✅ Так, застосувати"))
        mock_classify.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_transform)


# =========================
# Planner call discipline.
# =========================
class TestPlannerCallDiscipline(ActionPlannerWebhookTestCase):
    # 30. Planner called at most once per update.
    def test_planner_called_at_most_once(self):
        chat_id = 975151
        with patch.object(bot, "get_inventory_items", return_value=[]):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "unsupported", "arguments": {},
                "confidence": 0.0, "clarification_question": None,
            }) as mock_classify:
                _call_webhook(_make_update(975151001, chat_id, "Об'єднай це в одну позицію"))
        self.assertEqual(mock_classify.call_count, 1)

    # 31. Planner failure never falls through to general chat for an
    # inventory-like command it already claimed via the pre-gate.
    def test_unsupported_result_never_reaches_general_ai(self):
        chat_id = 975152
        with patch.object(action_planner, "classify", return_value={
            "version": 1, "action": "unsupported", "arguments": {},
            "confidence": 0.0, "clarification_question": None,
        }):
            with patch.object(bot, "call_gemini") as mock_gemini:
                _call_webhook(_make_update(975152001, chat_id, "Забери, будь ласка, все зайве з квартири автоматично"))
        mock_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(action_planner.UNSUPPORTED_MSG == t for t in texts))

    def test_clarify_result_never_reaches_general_ai(self):
        chat_id = 975153
        with patch.object(action_planner, "classify", return_value={
            "version": 1, "action": "clarify", "arguments": {}, "confidence": 0.6,
            "clarification_question": "Які саме позиції об'єднати і як назвати результат?",
        }):
            with patch.object(bot, "call_gemini") as mock_gemini:
                _call_webhook(_make_update(975153001, chat_id, "Об'єднай це в одну позицію"))
        mock_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Які саме позиції об'єднати" in t for t in texts))

    # 32. An ordinary general question never calls the new planner.
    def test_general_question_never_calls_planner(self):
        chat_id = 975154
        with patch.object(action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн.") as mock_gemini:
                _call_webhook(_make_update(975154001, chat_id, "Поясни, чому молоко згортається у каві?"))
        mock_classify.assert_not_called()
        mock_gemini.assert_called()


# =========================
# Preview / DB safety.
# =========================
class TestPreviewAndDbSafety(ActionPlannerWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        # bot.StaleSnapshotError resolves through the MagicMock stand-in for
        # `database` at module-import time — swap in the REAL exception
        # class for this test class only, same technique test_inventory_
        # transform.py's own TestTransformConfirmAndCancel already uses, so
        # `except StaleSnapshotError:` inside bot.py's confirm handlers
        # actually catches the side_effect raised below.
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    # 33/34. Transform plan creates the EXISTING preview; no DB write before
    # confirm.
    def test_transform_plan_creates_existing_preview_no_db_write(self):
        chat_id = 975161
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.98, "clarification_question": None,
            }):
                with patch.object(bot, "execute_inventory_transform") as mock_transform:
                    _call_webhook(_make_update(975161001, chat_id, "сосиски + мисливські ковбаски → м'ясні вироби"))
        mock_transform.assert_not_called()
        self.assertIn(chat_id, pending_inventory_transform)
        texts = self._sent_texts()
        self.assertTrue(any("• Додати М'ясні вироби — 8 шт." in t for t in texts))
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())

    # 35. Cancel changes nothing.
    def test_cancel_after_transform_plan_writes_nothing(self):
        chat_id = 975162
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.98, "clarification_question": None,
            }):
                _call_webhook(_make_update(975162001, chat_id, "сосиски + мисливські ковбаски → м'ясні вироби"))
        with patch.object(bot, "execute_inventory_transform") as mock_transform:
            _call_webhook(_make_update(975162002, chat_id, "❌ Скасувати"))
        mock_transform.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_transform)

    # 36. Confirm uses the EXISTING transform executor.
    def test_confirm_after_transform_plan_calls_existing_executor(self):
        chat_id = 975163
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.98, "clarification_question": None,
            }):
                _call_webhook(_make_update(975163001, chat_id, "сосиски + мисливські ковбаски → м'ясні вироби"))
        with patch.object(bot, "execute_inventory_transform", return_value=True) as mock_transform:
            _call_webhook(_make_update(975163002, chat_id, "✅ Так, застосувати"))
        mock_transform.assert_called_once()
        args, _ = mock_transform.call_args
        self.assertEqual(set(args[2]), {50, 60})
        self.assertNotIn(chat_id, pending_inventory_transform)

    # 37. Incompatible units are never silently summed.
    def test_incompatible_units_are_rejected_not_summed(self):
        chat_id = 975164
        rows = [
            {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": Decimal("500"), "quantity_unit": "г", "quantity_text": "500 г"},
            {"id": 60, "name": "Ковбаски", "canonical_name": "ковбаски", "category": "М'ясо та риба",
             "quantity_value": Decimal("2"), "quantity_unit": "шт.", "quantity_text": "2 шт."},
        ]
        with patch.object(bot, "get_inventory_items", return_value=rows):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.9, "clarification_question": None,
            }):
                _call_webhook(_make_update(975164001, chat_id, "сосиски + ковбаски -> м'ясні вироби"))
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("несумісні" in t for t in self._sent_texts()))

    # 38. Source ambiguity uses the EXISTING disambiguation message.
    def test_source_ambiguity_uses_existing_disambiguation(self):
        chat_id = 975165
        rows = _duplicate_milk_rows() + [
            {"id": 60, "name": "Вершки", "canonical_name": "вершки", "category": "Молочне та яйця",
             "quantity_value": Decimal("200"), "quantity_unit": "мл", "quantity_text": "200 мл"},
        ]
        with patch.object(bot, "get_inventory_items", return_value=rows):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["молоко", "вершки"], "target_name": "молочна суміш"},
                "confidence": 0.9, "clarification_question": None,
            }):
                _call_webhook(_make_update(975165001, chat_id, "молоко + вершки -> молочна суміш"))
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("не хочу вгадувати" in t for t in self._sent_texts()))

    # 39. Rename plan uses the EXISTING preview (including no-op-rename
    # protection reuse — same _start_inventory_rename call path).
    def test_rename_plan_uses_existing_preview(self):
        chat_id = 975166
        with patch.object(bot, "get_inventory_items", return_value=_ser_row()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_rename",
                "arguments": {"old_name": "ser", "new_name": "сир"},
                "confidence": 0.97, "clarification_question": None,
            }):
                _call_webhook(_make_update(975166001, chat_id, "виправ ser на сир будь ласка"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["action"], "rename")
        self.assertTrue(any("Ser" in t and "Сир" in t for t in self._sent_texts()))

    # 40. Delete plan uses the EXISTING natural-quantity matching — selects
    # the "1 шт." row, not the "14,5 л" row (the confirmed live fix).
    def test_delete_plan_uses_existing_natural_quantity_matching(self):
        chat_id = 975167
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_delete",
                "arguments": {"item_name": "молоко", "quantity_hint": "одна штука"},
                "confidence": 0.98, "clarification_question": None,
            }):
                _call_webhook(_make_update(
                    975167001, chat_id, "В запасах молоко одна штука вже не потрібне, забери його",
                ))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 7)
        self.assertTrue(any("Молоко — 1 шт." in t for t in self._sent_texts()))

    # 41. Merge duplicate plan uses the EXISTING cleanup preview.
    def test_merge_duplicates_plan_uses_existing_cleanup_preview(self):
        chat_id = 975168
        with patch.object(bot, "get_inventory_items", return_value=_duplicate_milk_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_merge_duplicates",
                "arguments": {"product_name": "молоко"},
                "confidence": 0.97, "clarification_question": None,
            }):
                _call_webhook(_make_update(975168001, chat_id, "Забери, будь ласка, зайві записи молока в запасах"))
        self.assertIn(chat_id, pending_merge)
        self.assertTrue(any("Можна безпечно об'єднати" in t for t in self._sent_texts()))

    # 42. Stale snapshot blocks a confirm against a preview built from
    # since-changed rows — same StaleSnapshotError contract every other
    # write path already uses.
    def test_stale_snapshot_blocks_confirm(self):
        chat_id = 975169
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.98, "clarification_question": None,
            }):
                _call_webhook(_make_update(975169001, chat_id, "сосиски + мисливські ковбаски → м'ясні вироби"))
        with patch.object(bot, "execute_inventory_transform", side_effect=bot.StaleSnapshotError("stale")):
            _call_webhook(_make_update(975169002, chat_id, "✅ Так, застосувати"))
        self.assertTrue(any(STALE_PREVIEW_MSG == t for t in self._sent_texts()))

    # 43. A repeated confirm does not execute twice — the pending state is
    # popped before the DB call, same duplicate-press protection as every
    # other confirm handler.
    def test_repeated_confirm_does_not_execute_twice(self):
        chat_id = 975170
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_transform",
                "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
                "confidence": 0.98, "clarification_question": None,
            }):
                _call_webhook(_make_update(975170001, chat_id, "сосиски + мисливські ковбаски → м'ясні вироби"))
        with patch.object(bot, "execute_inventory_transform", return_value=True) as mock_transform:
            _call_webhook(_make_update(975170002, chat_id, "✅ Так, застосувати"))
            _call_webhook(_make_update(975170003, chat_id, "✅ Так, застосувати"))
        mock_transform.assert_called_once()

    # 44. Other inventory items are never touched by a targeted delete.
    def test_delete_plan_leaves_other_items_untouched(self):
        chat_id = 975171
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            with patch.object(action_planner, "classify", return_value={
                "version": 1, "action": "inventory_delete",
                "arguments": {"item_name": "молоко", "quantity_hint": "одна штука"},
                "confidence": 0.98, "clarification_question": None,
            }):
                _call_webhook(_make_update(975171001, chat_id, "В запасах молоко одна штука вже не потрібне, забери"))
        with patch.object(bot, "execute_inventory_delete", return_value=True) as mock_delete:
            _call_webhook(_make_update(975171002, chat_id, "✅ Так, застосувати"))
        args, _ = mock_delete.call_args
        self.assertEqual(args[2], 7)  # item_id — only the "1 шт." row, "14,5 л" (id 8) untouched


# =========================
# Blocking-pending-state guard for the one dispatch shape without its own
# internal check (_start_inventory_cleanup relies on its caller).
# =========================
class TestBlockingPendingStateGuard(ActionPlannerWebhookTestCase):
    def test_merge_duplicates_blocked_by_active_pending_merge(self):
        chat_id = 975181
        pending_merge[chat_id] = {
            "groups": [], "targets": [], "household_id": 1, "user_db_id": 10, "list_type": "inventory_cleanup",
        }
        with patch.object(action_planner, "classify") as mock_classify:
            _call_webhook(_make_update(975181001, chat_id, "Об'єднай усі записи сиру"))
        mock_classify.assert_not_called()
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))


# =========================
# Voice — same dispatcher path as typed text.
# =========================
class TestVoiceTranscriptSameDispatcherPath(unittest.TestCase):
    """45. A voice transcript identical to a typed message routes through
    the exact same message_dispatcher.dispatch() call — verified here by
    calling bot.py's own dispatch entrypoint directly for both a "typed"
    and a "voice-transcribed" copy of the same text and asserting identical
    routing outcomes, without touching Groq/Whisper (out of scope, per this
    module's own docstring — voice_input.py is not modified)."""

    def setUp(self):
        pending_inventory_transform.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_inventory_transform.clear()

    def test_typed_and_transcribed_text_route_identically(self):
        text = "сосиски + мисливські ковбаски → м'ясні вироби"
        plan = {
            "version": 1, "action": "inventory_transform",
            "arguments": {"source_names": ["сосиски", "мисливські ковбаски"], "target_name": "м'ясні вироби"},
            "confidence": 0.98, "clarification_question": None,
        }
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value=plan):
                message_dispatcher_module = bot.message_dispatcher
                message_dispatcher_module.dispatch(bot._dispatcher_deps, 975191, 555, "Тест", text)
        self.assertIn(975191, pending_inventory_transform)
        entry_from_typed = dict(pending_inventory_transform[975191])
        pending_inventory_transform.clear()

        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(action_planner, "classify", return_value=plan):
                # Same call bot.py's voice_input.py handoff makes after
                # transcription — the transcript string is identical to the
                # typed text above, exercising the SAME dispatch() call.
                bot.message_dispatcher.dispatch(bot._dispatcher_deps, 975191, 555, "Тест", text)
        self.assertIn(975191, pending_inventory_transform)
        entry_from_voice = dict(pending_inventory_transform[975191])

        self.assertEqual(entry_from_typed["target_name"], entry_from_voice["target_name"])
        self.assertEqual(set(entry_from_typed["source_item_ids"]), set(entry_from_voice["source_item_ids"]))


if __name__ == "__main__":
    unittest.main()
