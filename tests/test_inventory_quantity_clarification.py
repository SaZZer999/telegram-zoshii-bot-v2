import sys
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No test in this file calls real
# Gemini, Telegram, Render, or Supabase — every network-facing function
# (_ask_gemini_household_router, call_gemini, send_message,
# apply_global_household_operations) is patched per-test below.
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


def _cheese_pieces_row():
    return {"id": 301, "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
             "quantity_value": 3.0, "quantity_unit": "шт.", "quantity_text": "3 шт.", "quantity_inferred": False}


def _cheese_grams_row():
    return {"id": 302, "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
             "quantity_value": 500.0, "quantity_unit": "г", "quantity_text": "500 г", "quantity_inferred": False}


def _bare_milk_router_result():
    return {
        "intent": "household_operations",
        "operations": [{"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
        "unresolved_fragments": [],
    }


def _milk_and_cheese_and_expense_router_result():
    return {
        "intent": "household_operations",
        "operations": [
            {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
            {"type": "add_inventory", "name": "Сир", "quantity_text": "", "category": "Молочне та яйця"},
            {"type": "add_expense", "amount": "15", "currency": "PLN", "category": "Продукти",
             "description": "Молоко і сир", "expense_date": "2026-07-05"},
        ],
        "unresolved_fragments": [],
    }


def _milk_item(quantity_value, quantity_unit, quantity_text, quantity_inferred):
    return {
        "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
        "quantity_value": quantity_value, "quantity_unit": quantity_unit,
        "quantity_text": quantity_text, "quantity_inferred": quantity_inferred, "was_corrected": False,
    }


def _cheese_item_bare():
    return {
        "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
        "quantity_value": 1.0, "quantity_unit": "шт.",
        "quantity_text": "1 шт.", "quantity_inferred": True, "was_corrected": False,
    }


class _BaseGlobalRouterTestCase(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_shopping = patch.object(bot, "get_active_shopping_items", return_value=[])
        patcher_shopping.start()
        self.addCleanup(patcher_shopping.stop)

        patcher_expenses = patch.object(bot, "get_recent_expenses_for_deletion", return_value=[])
        patcher_expenses.start()
        self.addCleanup(patcher_expenses.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_hr = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_hr = patcher_hr.start()
        self.addCleanup(patcher_hr.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

    def tearDown(self):
        for d in (pending_global_household, pending_inventory_quantity_clarification, active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestClarificationTrigger(_BaseGlobalRouterTestCase):
    # Case 1 — conflict creates clarification state, not a preview
    def test_conflict_creates_clarification_state_not_preview(self):
        chat_id = 996001
        with patch.object(bot, "get_inventory_items", return_value=[_milk_liters_row(), _milk_pieces_row()]):
            self.mock_hr.return_value = _bare_milk_router_result()
            _call_webhook(_make_update(996000001, chat_id, "Купив молоко"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any(
            "У запасах уже є кілька записів «Молоко»:" in t
            and "• 7 л" in t and "• 1 шт." in t
            and "Не хочу вгадувати, до якого запису додати нову покупку." in t
            and "«1 л» або «500 мл»" in t
            for t in texts
        ))

    # Case 2 — no DB write before the reply
    def test_no_db_write_before_reply(self):
        chat_id = 996002
        with patch.object(bot, "get_inventory_items", return_value=[_milk_liters_row(), _milk_pieces_row()]):
            self.mock_hr.return_value = _bare_milk_router_result()
            _call_webhook(_make_update(996000002, chat_id, "Купив молоко"))
        self.mock_apply.assert_not_called()


class TestClarificationQuantityParsing(unittest.TestCase):
    # Case 3 — "1Л" normalizes to Decimal("1") and "л"
    def test_no_space_form_normalizes_to_decimal_and_unit(self):
        value, unit = bot._parse_explicit_clarification_quantity("1Л")
        self.assertEqual(value, Decimal("1"))
        self.assertEqual(unit, "л")
        self.assertIsInstance(value, Decimal)

    def test_various_accepted_forms(self):
        cases = [
            ("1 л", Decimal("1"), "л"),
            ("500мл", Decimal("500"), "мл"),
            ("500 мл", Decimal("500"), "мл"),
            ("0,5 л", Decimal("0.5"), "л"),
            ("2 шт.", Decimal("2"), "шт."),
        ]
        for text, expected_value, expected_unit in cases:
            with self.subTest(text=text):
                value, unit = bot._parse_explicit_clarification_quantity(text)
                self.assertEqual(value, expected_value)
                self.assertEqual(unit, expected_unit)

    def test_bare_number_without_unit_is_rejected(self):
        value, unit = bot._parse_explicit_clarification_quantity("2")
        self.assertIsNone(value)
        self.assertIsNone(unit)

    def test_garbage_is_rejected(self):
        value, unit = bot._parse_explicit_clarification_quantity("щось незрозуміле")
        self.assertIsNone(value)
        self.assertIsNone(unit)


class TestClarificationContinuation(_BaseGlobalRouterTestCase):
    def _seed_milk_clarification(self, chat_id, extra_add_inventory_items=None, new_expense=None):
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [],
            "add_inventory_items": [_milk_item(1.0, "шт.", "1 шт.", True)] + (extra_add_inventory_items or []),
            "consume_changes": [],
            "new_expense": new_expense,
            "delete_expense": None,
        }

    # Case 4/5 — "1Л" resolves the conflict: preview shows the honest merge
    # line for the liters row, and the pieces row is never touched.
    def test_valid_reply_produces_merge_preview_and_leaves_pieces_row_alone(self):
        chat_id = 996003
        self._seed_milk_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_milk_liters_row(), _milk_pieces_row()]):
            _call_webhook(_make_update(996000003, chat_id, "1Л"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 7 л + 1 л → буде 8 л" in t for t in texts))
        targets = pending_global_household[chat_id]["inventory_targets"]
        self.assertEqual(targets, [{"item_id": 201, "quantity_value": 7.0, "quantity_unit": "л"}])

    # Case 6 — an invalid reply never reaches general AI-chat and keeps the
    # clarification state active.
    def test_invalid_reply_does_not_reach_ai_chat_and_keeps_state(self):
        chat_id = 996004
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(996000004, chat_id, "багато"))
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        texts = self._sent_texts()
        self.assertTrue(any(
            "Потрібна точна кількість з одиницею." in t
            and "«1 л», «500 мл» або «2 шт.»" in t
            for t in texts
        ))

    # Case 7 — a new household-shaped command does not create another preview
    def test_new_household_command_does_not_start_a_new_router_pass(self):
        chat_id = 996005
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(996000005, chat_id, "Купив банани"))
        self.mock_hr.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.assertNotIn(chat_id, pending_global_household)

    # Case 8 — an ordinary question never reaches general AI-chat either
    def test_ordinary_question_does_not_reach_ai_chat(self):
        chat_id = 996006
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(996000006, chat_id, "Яка сьогодні погода?"))
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)

    # Case 9 — cancel and main-menu navigation clear the clarification state
    def test_cancel_clears_clarification_state(self):
        chat_id = 996007
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(996000007, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.assertTrue(any("Уточнення скасовано." in t for t in self._sent_texts()))

    def test_main_menu_navigation_clears_clarification_state(self):
        chat_id = 996008
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(996000008, chat_id, "⬅️ Головне меню"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)

    def test_start_command_clears_clarification_state(self):
        chat_id = 996009
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(996000009, chat_id, "/start"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)

    def test_menu_command_clears_clarification_state(self):
        chat_id = 996010
        self._seed_milk_clarification(chat_id)
        _call_webhook(_make_update(996000010, chat_id, "/menu"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)

    # Case 10 — compound command: milk clarification blocks the whole
    # request; a valid reply then produces ONE combined preview with the
    # milk update, the cheese addition, and the expense together.
    def test_compound_request_blocks_atomically_then_produces_one_combined_preview(self):
        chat_id = 996011
        with patch.object(bot, "get_inventory_items", return_value=[_milk_liters_row(), _milk_pieces_row()]):
            self.mock_hr.return_value = _milk_and_cheese_and_expense_router_result()
            _call_webhook(_make_update(996000011, chat_id, "Купив молоко і сир за 15 zł"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        stored = pending_inventory_quantity_clarification[chat_id]
        self.assertIsNotNone(stored["new_expense"])
        self.assertEqual(len(stored["add_inventory_items"]), 2)

        with patch.object(bot, "get_inventory_items", return_value=[_milk_liters_row(), _milk_pieces_row()]):
            _call_webhook(_make_update(996000012, chat_id, "1 л"))
        self.mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertIsNotNone(payload["new_expense"])
        self.assertEqual(len(payload["add_inventory_items"]), 2)
        texts = self._sent_texts()
        self.assertTrue(any(
            "Молоко — 7 л + 1 л → буде 8 л" in t and "Сир" in t and "💸 Витрати" in t
            for t in texts
        ))

    # Case 11 — representation changes between question and answer: a new
    # conflicting row appears for the OTHER compound item (Сир), so the
    # fresh re-check must keep blocking (now clarifying Сир) rather than
    # building an unsafe preview.
    def test_new_conflict_appearing_before_the_reply_keeps_blocking(self):
        chat_id = 996013
        with patch.object(bot, "get_inventory_items", return_value=[_milk_liters_row(), _milk_pieces_row()]):
            self.mock_hr.return_value = _milk_and_cheese_and_expense_router_result()
            _call_webhook(_make_update(996000013, chat_id, "Купив молоко і сир за 15 zł"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)

        # Between question and answer, two INCOMPATIBLE "Сир" rows appear.
        fresh_with_new_cheese_conflict = [
            _milk_liters_row(), _milk_pieces_row(), _cheese_pieces_row(), _cheese_grams_row(),
        ]
        with patch.object(bot, "get_inventory_items", return_value=fresh_with_new_cheese_conflict):
            _call_webhook(_make_update(996000014, chat_id, "1 л"))

        self.mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        stored = pending_inventory_quantity_clarification[chat_id]
        self.assertEqual(stored["item_name"], "Сир")
        self.assertEqual(stored["canonical_name"], "сир")
        texts = self._sent_texts()
        self.assertTrue(any("Сир" in t and "3 шт." in t and "500 г" in t for t in texts))


def _milk_single_row():
    return {"id": 401, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 9.5, "quantity_unit": "л", "quantity_text": "9,5 л", "quantity_inferred": False}


class TestSingleRowQuantityClarificationWording(_BaseGlobalRouterTestCase):
    """V1.2 bugfix: with exactly ONE existing row, the clarification must
    ask "Скільки додати?" — never "до якого запису" (that phrasing is only
    correct when there are 2+ candidate rows)."""

    # 9. Exactly one existing row asks "how much", never "which record".
    def test_single_row_asks_how_much_not_which_record(self):
        chat_id = 996020
        with patch.object(bot, "get_inventory_items", return_value=[_milk_single_row()]):
            self.mock_hr.return_value = _bare_milk_router_result()
            _call_webhook(_make_update(996000020, chat_id, "Купив молоко"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        texts = self._sent_texts()
        self.assertTrue(any(
            "У запасах уже є «Молоко — 9,5 л»." in t and "Скільки додати?" in t
            for t in texts
        ))
        self.assertFalse(any("до якого запису" in t for t in texts))


class TestSingleRowBareNumberQuantity(_BaseGlobalRouterTestCase):
    """V1.2 optional improvement: a bare number reply during a single-row
    quantity clarification is accepted using that row's existing unit."""

    def _seed_single_row_clarification(self, chat_id):
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [],
            "add_inventory_items": [_milk_item(1.0, "шт.", "1 шт.", True)],
            "consume_changes": [],
            "new_expense": None,
            "delete_expense": None,
        }

    # 10. Explicit "2л" still works exactly as before for the single-row case.
    def test_explicit_quantity_reply_still_works(self):
        chat_id = 996021
        self._seed_single_row_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_milk_single_row()]):
            _call_webhook(_make_update(996000021, chat_id, "2л"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 9,5 л + 2 л → буде 11,5 л" in t for t in texts))

    # 11. Bare "2" (no unit) during a single-row clarification is accepted
    # as "2 л" — the existing row's own unit.
    def test_bare_number_defaults_to_existing_rows_unit(self):
        chat_id = 996022
        self._seed_single_row_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_milk_single_row()]):
            _call_webhook(_make_update(996000022, chat_id, "2"))
        self.assertNotIn(chat_id, pending_inventory_quantity_clarification)
        self.assertIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 9,5 л + 2 л → буде 11,5 л" in t for t in texts))

    # A bare number stays rejected when 2+ rows make the unit ambiguous —
    # the fallback only ever applies to the unambiguous single-row case.
    def test_bare_number_still_rejected_with_multiple_rows(self):
        chat_id = 996023
        self._seed_single_row_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_milk_liters_row(), _milk_pieces_row()]):
            _call_webhook(_make_update(996000023, chat_id, "2"))
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        self.mock_call_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Потрібна точна кількість з одиницею." in t for t in texts))

    # A genuinely invalid reply is still rejected without ever touching the
    # database (no get_inventory_items patch needed/expected here).
    def test_garbage_reply_still_rejected_without_db_call(self):
        chat_id = 996024
        self._seed_single_row_clarification(chat_id)
        with patch.object(bot, "get_inventory_items") as mock_get_items:
            _call_webhook(_make_update(996000024, chat_id, "багато"))
            mock_get_items.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)
        texts = self._sent_texts()
        self.assertTrue(any("Потрібна точна кількість з одиницею." in t for t in texts))


if __name__ == '__main__':
    unittest.main()
