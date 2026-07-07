"""Household Language Behavior Contract v1.

Fixes, as executable tests, how the bot is expected to understand a set of
typical natural-language phrases a household member would actually type —
NOT whether Gemini "understands Ukrainian" (Gemini is always mocked here;
these are unit tests, not an LLM eval). Each scenario is:

    natural phrase -> expected route -> expected preview/clarification/
    block/fallback -> no unexpected DB writes

Only observable behavior is asserted: sent messages, pending state, preview
payload shape, and whether general AI-chat / Gemini / a DB write helper was
called or not. No test here depends on line numbers, local variable names,
or the exact if/elif structure of webhook() — only on bot.py's existing
public-ish surface (pending_* dicts, patch points already used throughout
this test suite).

Does NOT re-test routing PRECEDENCE between pending states (that's
test_routing_precedence_contract.py) or the full business logic of any one
flow (that's test_global_household_operations.py, test_global_bare_add.py,
test_global_explicit_add.py, test_ambiguous_add_with_price.py,
test_inventory_quantity_clarification.py, test_safe_undo_global_action.py).
This file is the user-facing table of "phrase -> behavior" contracts.

No real Gemini, Telegram, Supabase, or Render call happens anywhere here.
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
import household_router  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    pending_inventory_quantity_clarification,
    pending_inventory_representation_clarification,
    pending_add_destination_clarification,
    pending_undo_action,
    pending_expense,
    active_list_context,
    saved_list_context,
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


class HouseholdLanguageContractTestCase(unittest.TestCase):
    """Shared setup: every network-facing / DB-facing call bot.py could
    reach is patched, so each scenario only has to configure the ONE mock
    it actually needs and assert on what got called."""

    def setUp(self):
        for d in (
            pending_global_household, pending_inventory_quantity_clarification,
            pending_inventory_representation_clarification,
            pending_add_destination_clarification, pending_undo_action, pending_expense,
        ):
            d.clear()
        active_list_context.clear()
        saved_list_context.clear()

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_hr = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_hr = patcher_hr.start()
        self.addCleanup(patcher_hr.stop)

        patcher_items = patch.object(household_router, "_ask_gemini_explicit_add_items")
        self.mock_items = patcher_items.start()
        self.addCleanup(patcher_items.stop)

        patcher_expense_router = patch.object(bot, "_ask_gemini_expense_router")
        self.mock_expense_router = patcher_expense_router.start()
        self.addCleanup(patcher_expense_router.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

        patcher_undo_action = patch.object(bot, "apply_undo_action")
        self.mock_undo_action = patcher_undo_action.start()
        self.addCleanup(patcher_undo_action.stop)

        patcher_get_latest_undoable = patch.object(bot, "get_latest_undoable_action", return_value=None)
        self.mock_get_latest_undoable = patcher_get_latest_undoable.start()
        self.addCleanup(patcher_get_latest_undoable.stop)

        patcher_inventory = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory = patcher_inventory.start()
        self.addCleanup(patcher_inventory.stop)

        patcher_shopping = patch.object(bot, "get_active_shopping_items", return_value=[])
        self.mock_shopping = patcher_shopping.start()
        self.addCleanup(patcher_shopping.stop)

        patcher_recent_expenses = patch.object(bot, "get_recent_expenses_for_deletion", return_value=[])
        patcher_recent_expenses.start()
        self.addCleanup(patcher_recent_expenses.stop)

    def tearDown(self):
        for d in (
            pending_global_household, pending_inventory_quantity_clarification,
            pending_inventory_representation_clarification,
            pending_add_destination_clarification, pending_undo_action, pending_expense,
        ):
            d.clear()
        active_list_context.clear()
        saved_list_context.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# Global household commands — independent of which menu is open
# =========================
class TestGlobalHouseholdCommandsWorkFromAnywhere(HouseholdLanguageContractTestCase):
    # Scenario 1: "Купив молоко за 10 zł" -> one compound preview (inventory + expense)
    def test_bought_with_full_zloty_marker_builds_compound_preview(self):
        chat_id = 700001
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(700000001, chat_id, "Купив молоко за 10 zł"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.assertIsNotNone(payload["new_expense"])
        texts = self._sent_texts()
        self.assertTrue(any("🧊 Запаси" in t and "💸 Витрати" in t for t in texts))

    # Scenario 2: "Купив хліб за 5 z" (short marker) -> same compound flow, correct amount
    def test_bought_with_short_zloty_marker_builds_compound_preview_with_correct_amount(self):
        chat_id = 700002
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
                {"type": "add_expense", "amount": "5", "currency": "PLN", "category": "Продукти",
                 "description": "Хліб", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(700000002, chat_id, "Купив хліб за 5 z"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["new_expense"]["amount"], 5)

    # Scenario 3: "З'їв 200 г сиру" -> global inventory consume flow, not general AI
    def test_consume_phrase_builds_consumption_preview_not_general_ai(self):
        chat_id = 700003
        self.mock_inventory.return_value = [{
            "id": 601, "name": "Сир", "category": "Молочне та яйця",
            "quantity_value": 500.0, "quantity_unit": "г", "quantity_text": "500 г",
        }]
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [{"type": "consume_inventory", "item_number": 1,
                             "quantity_value": 200, "quantity_unit": "г"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(700000003, chat_id, "З'їв 200 г сиру"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["consume_changes"]), 1)
        self.assertEqual(payload["consume_changes"][0]["new_value"], 300.0)
        self.mock_call_gemini.assert_not_called()

    # Scenario 3b (Inventory Representation Clarification V2): "З'їв 200 г
    # сиру" against an existing "1 шт." row -> a representation
    # clarification, never a hard "несумісні одиниці" block, never general AI.
    def test_consume_mass_against_count_only_row_starts_representation_clarification(self):
        chat_id = 700015
        self.mock_inventory.return_value = [{
            "id": 602, "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
            "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False,
        }]
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [{"type": "consume_inventory", "item_number": 1,
                             "quantity_value": 200, "quantity_unit": "г"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(700000015, chat_id, "З'їв 200 г сиру"))
        self.assertIn(chat_id, pending_inventory_representation_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_call_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Що це означає?" in t for t in texts))

    # Scenario 4: "Додай до покупок молоко і хліб" -> shopping preview only
    def test_explicit_shopping_destination_builds_shopping_only_preview(self):
        chat_id = 700004
        self.mock_items.return_value = {
            "items": [
                {"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(700000004, chat_id, "Додай до покупок молоко і хліб"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 2)
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertIsNone(payload["new_expense"])

    # Scenario 5: "Додай в запаси 2 банани" -> inventory preview only
    def test_explicit_inventory_destination_builds_inventory_only_preview(self):
        chat_id = 700005
        self.mock_items.return_value = {
            "items": [{"name": "Банани", "quantity_text": "2", "category": "Фрукти та ягоди"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(700000005, chat_id, "Додай в запаси 2 банани"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_shopping_items"], [])
        self.assertEqual(len(payload["add_inventory_items"]), 1)

    # Scenario 5b (Multi-Expense Batch v1): two purchases-with-price plus a
    # plain inventory add, all in one message -> ONE combined preview with
    # BOTH new expenses, never a "лише одну витрату" error, never a general
    # AI fallback.
    def test_multiple_purchases_with_price_build_one_combined_preview(self):
        chat_id = 700020
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "8", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": "2020-01-01"},
                {"type": "add_inventory", "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
                {"type": "add_expense", "amount": "5", "currency": "PLN", "category": "Продукти",
                 "description": "Хліб", "expense_date": "2020-01-01"},
                {"type": "add_inventory", "name": "Сосиски", "quantity_text": "пару", "category": "М'ясо та риба"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(
            700000020, chat_id,
            "Купив 1 л молока за 8 zł\nКупив хліб за 5 zł\nДодай до запасів пару сосисок",
        ))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["new_expenses"]), 2)
        self.assertEqual(len(payload["add_inventory_items"]), 3)
        texts = self._sent_texts()
        self.assertTrue(any("🧊 Запаси" in t and "💸 Витрати" in t for t in texts))
        self.assertFalse(any("лише одну нову витрату" in t for t in texts))
        self.mock_call_gemini.assert_not_called()

    # Scenario 5c (Inventory Representation Clarification V2): "Купив 250 г
    # сиру" against an existing "1 шт." row -> an add-side representation
    # clarification, never a silent "separate row" insert.
    def test_add_mass_against_count_only_row_starts_representation_clarification(self):
        chat_id = 700021
        self.mock_inventory.return_value = [{
            "id": 603, "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
            "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False,
        }]
        self.mock_hr.return_value = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Сир", "quantity_text": "250 г",
                             "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(700000021, chat_id, "Купив 250 г сиру"))
        self.assertIn(chat_id, pending_inventory_representation_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Що означають ці 250 г?" in t for t in texts))


# =========================
# Contextual bare add — same "Додай молоко" resolves differently by menu
# =========================
class TestContextualBareAdd(HouseholdLanguageContractTestCase):
    def _milk_item(self):
        return {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }

    # Scenario 6: bare "Додай молоко" in shopping context -> shopping preview
    def test_bare_add_in_shopping_context_builds_shopping_preview(self):
        chat_id = 700006
        active_list_context[chat_id] = "shopping"
        self.mock_items.return_value = self._milk_item()
        _call_webhook(_make_update(700000006, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 1)
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertNotIn(chat_id, pending_add_destination_clarification)

    # Scenario 7: bare "Додай молоко" in inventory context -> inventory preview
    def test_bare_add_in_inventory_context_builds_inventory_preview(self):
        chat_id = 700007
        active_list_context[chat_id] = "inventory"
        self.mock_items.return_value = self._milk_item()
        _call_webhook(_make_update(700000007, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_shopping_items"], [])
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.assertNotIn(chat_id, pending_add_destination_clarification)

    # Scenario 8: bare "Додай молоко" from the main menu -> destination
    # clarification, no DB write.
    def test_bare_add_from_main_menu_asks_for_destination(self):
        chat_id = 700008
        self.mock_items.return_value = self._milk_item()
        _call_webhook(_make_update(700000008, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()

    # Scenario 9: bare "Додай молоко" from expenses/aliases menus -> also a
    # destination clarification, no DB write (only shopping/inventory
    # contexts resolve directly — scenarios 6/7).
    def test_bare_add_from_expenses_or_aliases_menu_asks_for_destination(self):
        for menu, chat_id in (("expenses", 700009), ("aliases", 700010)):
            with self.subTest(menu=menu):
                active_list_context[chat_id] = menu
                self.mock_items.return_value = self._milk_item()
                _call_webhook(_make_update(chat_id, chat_id, "Додай молоко"))
                self.assertIn(chat_id, pending_add_destination_clarification)
                self.assertNotIn(chat_id, pending_global_household)
                self.mock_apply.assert_not_called()

    # Scenario 10: answering "У запаси" resolves the clarification using the
    # already-parsed payload — no second Gemini parse.
    def test_destination_answer_reuses_already_parsed_payload(self):
        chat_id = 700011
        self.mock_items.return_value = self._milk_item()
        _call_webhook(_make_update(700000011, chat_id, "Додай молоко"))
        self.assertIn(chat_id, pending_add_destination_clarification)

        _call_webhook(_make_update(700000012, chat_id, "У запаси"))
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.mock_items.assert_called_once()
        self.mock_call_gemini.assert_not_called()


# =========================
# Safety / ambiguity
# =========================
class TestSafetyAndAmbiguity(HouseholdLanguageContractTestCase):
    _GUARD_MSG_MARKER = "Команда «Додай ... за суму» неоднозначна."

    # Scenario 11: "Додай молоко за 10 zł" -> local block, no Gemini, no DB
    # write, no expense preview, no destination clarification.
    def test_bare_add_with_price_is_blocked_locally(self):
        chat_id = 700012
        _call_webhook(_make_update(700000013, chat_id, "Додай молоко за 10 zł"))
        texts = self._sent_texts()
        self.assertTrue(any(self._GUARD_MSG_MARKER in t for t in texts))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_expense)
        self.mock_call_gemini.assert_not_called()
        self.mock_hr.assert_not_called()
        self.mock_items.assert_not_called()
        self.mock_expense_router.assert_not_called()
        self.mock_apply.assert_not_called()

    # Scenario 12: an explicit-destination add with a price is blocked the
    # same way, regardless of destination.
    def test_explicit_destination_add_with_price_is_blocked_locally(self):
        chat_id = 700013
        _call_webhook(_make_update(700000014, chat_id, "Додай до покупок хліб за 5 z"))
        texts = self._sent_texts()
        self.assertTrue(any(self._GUARD_MSG_MARKER in t for t in texts))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_hr.assert_not_called()
        self.mock_items.assert_not_called()

    # Scenario 13: an ordinary question during quantity clarification never
    # reaches general AI-chat.
    def test_ordinary_question_during_quantity_clarification_does_not_reach_ai(self):
        chat_id = 700014
        pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "new_expense": None, "delete_expense": None,
        }
        _call_webhook(_make_update(700000015, chat_id, "Яка погода?"))
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, pending_inventory_quantity_clarification)

    # Scenario 14: "Купив молоко" while an undo confirm is pending never
    # starts a new household flow.
    def test_household_phrase_during_pending_undo_does_not_start_new_flow(self):
        chat_id = 700015
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(700000016, chat_id, "Купив молоко"))
        self.mock_hr.assert_not_called()
        self.mock_call_gemini.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertIn(chat_id, pending_undo_action)

    # Scenario 15: with no pending state at all, an ordinary question
    # reaches general AI-chat, never a household DB flow.
    def test_ordinary_question_with_no_pending_state_reaches_general_ai(self):
        chat_id = 700016
        _call_webhook(_make_update(700000017, chat_id, "Яка погода?"))
        self.mock_call_gemini.assert_called()
        self.mock_hr.assert_not_called()
        self.mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)


# =========================
# Undo
# =========================
class TestUndoNaturalLanguagePhrasings(HouseholdLanguageContractTestCase):
    """Scenarios 16-18: all three recognized undo phrasings start the same
    undo-preview route when a household action is available to undo."""

    def _undoable_action(self):
        return {
            "id": 555,
            "summary": {"inventory": [], "shopping": [], "expense_added": None, "expense_deleted": None},
        }

    def test_each_phrasing_starts_the_same_undo_preview(self):
        for i, phrase in enumerate((
            "Скасувати останню дію", "Повернути останню дію", "Верни зміни назад",
        )):
            chat_id = 700020 + i
            with self.subTest(phrase=phrase):
                self.mock_get_latest_undoable.return_value = self._undoable_action()
                _call_webhook(_make_update(chat_id, chat_id, phrase))

                self.assertIn(chat_id, pending_undo_action)
                self.assertEqual(pending_undo_action[chat_id]["action_id"], 555)
                self.mock_call_gemini.assert_not_called()
                self.mock_hr.assert_not_called()
                self.mock_apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
