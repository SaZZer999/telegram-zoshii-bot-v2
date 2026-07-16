"""Quantity + Price Intent Clarification V1 — end-to-end webhook-level
coverage.

A message naming a product, an EXPLICIT quantity, AND a money amount all at
once ("Молоко 1 л 4,99 zł") is genuinely ambiguous. Context Intent Safety V1
(6054fe2) and Active List Context Routing Stabilization V1 (d885160) already
block this from silently becoming an item with a bogus "4,99 шт." quantity
or a silent expense-only preview — but only ever sent a static refusal
asking the user to type two separate messages. This feature upgrades that
into an actionable four-choice clarification
(pending_quantity_price_intent) that resolves directly into an EXISTING
domain preview:

  🛒 Додати до покупок -> household_router.build_add_preview_from_items
                           ("add_shopping", ...) -> pending_global_household
                           -> apply_global_household_operations. Price is
                           never stored anywhere.
  💸 Записати витрату    -> expenses.build_receipt_expense_preview ->
                           pending_expense -> add_expense. No shopping/
                           inventory item is ever created.
  ✅ Уже купив            -> household_router.build_add_preview_from_items
                           ("add_inventory", ...) PLUS new_expenses set on
                           the SAME payload -> pending_global_household (ONE
                           combined preview) -> apply_global_household_
                           operations (ONE transaction).
  ❌ Скасувати            -> existing shared cancel handler.

A purchase verb ("купив"/"взяли") or an explicit "Додай"/"Запиши" verb
bypasses the clarification entirely and reaches its own existing route.

No real Gemini/Telegram/Supabase call happens anywhere in this file —
database is mocked at import time, every Gemini-facing bot.py function is
patched per-test (and asserted NOT called, since this whole feature is
deterministic).
"""
import sys
import os
import unittest
from decimal import Decimal
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
    saved_list_context,
    active_list_context,
    pending_quantity_price_intent,
    pending_global_household,
    pending_expense,
    pending_delete_batch,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class QuantityPriceClarificationTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(saved_list_context.clear)
        self.addCleanup(active_list_context.clear)
        self.addCleanup(pending_quantity_price_intent.clear)
        self.addCleanup(pending_global_household.clear)
        self.addCleanup(pending_expense.clear)
        self.addCleanup(pending_delete_batch.clear)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_user.start()
        self.addCleanup(patcher_user.stop)

        patcher_alias = patch.object(bot, "get_household_alias_map", return_value={})
        self.mock_alias_map = patcher_alias.start()
        self.addCleanup(patcher_alias.stop)

        patcher_inventory_items = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory_items = patcher_inventory_items.start()
        self.addCleanup(patcher_inventory_items.stop)

        patcher_shopping_items = patch.object(bot, "get_active_shopping_items", return_value=[])
        self.mock_shopping_items = patcher_shopping_items.start()
        self.addCleanup(patcher_shopping_items.stop)

        patcher_gemini = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

        patcher_expense_router = patch.object(bot, "_ask_gemini_expense_router")
        self.mock_expense_router = patcher_expense_router.start()
        self.addCleanup(patcher_expense_router.stop)

        patcher_apply_global = patch.object(
            bot, "apply_global_household_operations",
            return_value={"shopping_added": 1, "inventory_added": 1, "inventory_updated": 0, "inventory_removed": 0,
                          "expense_added_id": 1, "expense_added_ids": [1], "expense_deleted": False},
        )
        self.mock_apply_global = patcher_apply_global.start()
        self.addCleanup(patcher_apply_global.stop)

        patcher_add_expense = patch.object(bot, "add_expense", return_value=None)
        self.mock_add_expense = patcher_add_expense.start()
        self.addCleanup(patcher_add_expense.stop)

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestClarificationTrigger(QuantityPriceClarificationTestCase):
    # 1. Shopping context + "Молоко 1 л 4,99 zł" -> clarification.
    def test_shopping_context_triggers_clarification(self):
        chat_id = 980001
        saved_list_context[chat_id] = "shopping_saved"
        _call_webhook(_make_update(970001001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        data = pending_quantity_price_intent[chat_id]
        self.assertEqual(data["item_name"], "Молоко")
        self.assertEqual(data["quantity_text"], "1 л")
        self.assertEqual(data["amount"], Decimal("4.99"))
        self.assertTrue(any("товар" in t and "кількість" in t and "ціну" in t for t in self._sent_texts()))

    # 2. Inventory context + same phrase -> clarification.
    def test_inventory_context_triggers_clarification(self):
        chat_id = 980002
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(970002001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertIn(chat_id, pending_quantity_price_intent)

    # 3. Main menu (no context at all) + same phrase -> clarification.
    def test_main_menu_triggers_clarification(self):
        chat_id = 980003
        _call_webhook(_make_update(970003001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        self.mock_expense_router.assert_not_called()

    # 4. No DB write before a choice is made.
    def test_no_db_write_before_choice(self):
        chat_id = 980004
        _call_webhook(_make_update(970004001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertFalse(self.mock_apply_global.called)
        self.assertFalse(self.mock_add_expense.called)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_expense)

    # 5. Cancel clears the clarification and creates nothing.
    def test_cancel_clears_clarification(self):
        chat_id = 980005
        _call_webhook(_make_update(970005001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        _call_webhook(_make_update(970005002, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, pending_expense)
        self.assertFalse(self.mock_apply_global.called)
        self.assertFalse(self.mock_add_expense.called)

    # 20. Zero Gemini calls to trigger the clarification itself.
    def test_zero_gemini_calls_to_trigger(self):
        chat_id = 980006
        _call_webhook(_make_update(970006001, chat_id, "Молоко 1 л 4,99 zł"))
        self.mock_call_gemini.assert_not_called()
        self.mock_expense_router.assert_not_called()


class TestAddToShoppingChoice(QuantityPriceClarificationTestCase):
    # 6/7/9. "Додати до покупок" -> existing shopping preview "Молоко — 1 л",
    # price never becomes quantity, no expense created.
    def test_choice_builds_shopping_preview_without_price(self):
        chat_id = 980101
        _call_webhook(_make_update(970101001, chat_id, "Молоко 1 л 4,99 zł"))
        _call_webhook(_make_update(970101002, chat_id, "🛒 Додати до покупок"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_shopping_items"]), 1)
        item = data["add_shopping_items"][0]
        self.assertEqual(item["name"], "Молоко")
        self.assertEqual(item["quantity_text"], "1 л")
        self.assertNotEqual(item["quantity_text"], "4,99 шт.")
        self.assertEqual(data["add_inventory_items"], [])
        self.assertEqual(data["new_expenses"], [])
        preview_texts = self._sent_texts()
        self.assertTrue(any("Молоко" in t and "1 л" in t for t in preview_texts))

    def test_choice_then_confirm_writes_only_shopping_item(self):
        chat_id = 980102
        _call_webhook(_make_update(970102001, chat_id, "Молоко 1 л 4,99 zł"))
        _call_webhook(_make_update(970102002, chat_id, "🛒 Додати до покупок"))
        _call_webhook(_make_update(970102003, chat_id, "✅ Так, застосувати"))
        self.mock_apply_global.assert_called_once()
        _, kwargs = self.mock_apply_global.call_args
        self.assertEqual(len(kwargs.get("add_shopping_items") or []), 1)
        self.assertFalse(kwargs.get("add_inventory_items"))
        self.assertFalse(kwargs.get("new_expenses"))
        self.assertFalse(self.mock_add_expense.called)


class TestRecordExpenseChoice(QuantityPriceClarificationTestCase):
    # 8/9. "Записати витрату" -> existing expense preview "4,99 zł", no
    # shopping/inventory item created.
    def test_choice_builds_expense_preview(self):
        chat_id = 980201
        _call_webhook(_make_update(970201001, chat_id, "Молоко 1 л 4,99 zł"))
        _call_webhook(_make_update(970201002, chat_id, "💸 Записати витрату"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_expense)
        data = pending_expense[chat_id]
        self.assertEqual(data["amount"], Decimal("4.99"))
        self.assertIn("Молоко", data["description"])
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("4,99" in t for t in self._sent_texts()))

    def test_choice_then_confirm_writes_only_expense(self):
        chat_id = 980202
        _call_webhook(_make_update(970202001, chat_id, "Молоко 1 л 4,99 zł"))
        _call_webhook(_make_update(970202002, chat_id, "💸 Записати витрату"))
        _call_webhook(_make_update(970202003, chat_id, "✅ Так, додати"))
        self.mock_add_expense.assert_called_once()
        self.assertFalse(self.mock_apply_global.called)


class TestAlreadyBoughtChoice(QuantityPriceClarificationTestCase):
    # 10. "Уже купив" -> existing compound preview (inventory add + expense)
    # reusing apply_global_household_operations, the SAME executor "Купив X
    # за Y zł" already uses.
    def test_choice_builds_combined_inventory_and_expense_preview(self):
        chat_id = 980301
        _call_webhook(_make_update(970301001, chat_id, "Молоко 1 л 4,99 zł"))
        _call_webhook(_make_update(970301002, chat_id, "✅ Уже купив"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["name"], "Молоко")
        self.assertEqual(data["add_shopping_items"], [])
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("4.99"))

    def test_choice_then_confirm_writes_both_in_one_transaction(self):
        chat_id = 980302
        _call_webhook(_make_update(970302001, chat_id, "Молоко 1 л 4,99 zł"))
        _call_webhook(_make_update(970302002, chat_id, "✅ Уже купив"))
        _call_webhook(_make_update(970302003, chat_id, "✅ Так, застосувати"))
        self.mock_apply_global.assert_called_once()
        _, kwargs = self.mock_apply_global.call_args
        self.assertEqual(len(kwargs.get("add_inventory_items") or []), 1)
        self.assertEqual(len(kwargs.get("new_expenses") or []), 1)
        self.assertFalse(self.mock_add_expense.called)

    # 11. If the representation guard can't safely combine them, a
    # controlled message is sent and NOTHING is written (never a partial
    # inventory-only or expense-only write for this choice). Directly forces
    # the Inventory Representation Guard's "clarify" outcome (its own exact
    # trigger conditions are covered by inventory.py's own test suite —
    # this test only verifies THIS choice handler's reaction to a non-"ok"
    # kind) rather than trying to reconstruct the precise inventory snapshot
    # shape that guard needs.
    def test_representation_conflict_blocks_without_partial_write(self):
        chat_id = 980303
        _call_webhook(_make_update(970303001, chat_id, "Молоко 1 л 4,99 zł"))
        with patch.object(
            household_router, "build_add_preview_from_items",
            return_value=("clarify", {"item_name": "Молоко", "canonical_name": "молоко",
                                       "category": "Молочне та яйця", "existing_items": []}),
        ):
            _call_webhook(_make_update(970303002, chat_id, "✅ Уже купив"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertFalse(self.mock_apply_global.called)
        self.assertFalse(self.mock_add_expense.called)
        self.assertTrue(any("окремими командами" in t for t in self._sent_texts()))


class TestExplicitVerbsBypassClarification(QuantityPriceClarificationTestCase):
    # 12. Explicit purchase verb uses purchase-intent, no clarification.
    def test_purchase_verb_bypasses_clarification(self):
        chat_id = 980401
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "4.99", "currency": "PLN", "category": "Продукти", '
            '"description": "Молоко", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(970401001, chat_id, "Купив молоко 1 л за 4,99 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_called_once()
        self.assertIn(chat_id, pending_global_household)

    def test_alternate_purchase_verb_взяли_bypasses_clarification(self):
        chat_id = 980402
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Сир", "quantity_text": "500 г", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "12", "currency": "PLN", "category": "Продукти", '
            '"description": "Сир", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(970402001, chat_id, "Взяли сир 500 г за 12 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_called_once()
        self.assertIn(chat_id, pending_global_household)

    def test_purchase_verb_bypasses_clarification_in_shopping_context(self):
        chat_id = 980403
        saved_list_context[chat_id] = "shopping_saved"
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка"}, '
            '{"type": "add_expense", "amount": "5", "currency": "PLN", "category": "Продукти", '
            '"description": "Хліб", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(970403001, chat_id, "Придбав хліб 1 шт за 5 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_called_once()
        self.assertIn(chat_id, pending_global_household)

    def test_purchase_verb_bypasses_clarification_in_inventory_context(self):
        chat_id = 980404
        saved_list_context[chat_id] = "inventory_saved"
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "4.99", "currency": "PLN", "category": "Продукти", '
            '"description": "Молоко", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(970404001, chat_id, "Купив молоко 1 л за 4,99 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_called_once()
        self.assertIn(chat_id, pending_global_household)

    # 13. Voice transcript uses the same route (bot.webhook()'s text
    # dispatcher is the shared entrypoint voice_input.py already forwards
    # its transcript into).
    def test_voice_transcript_uses_same_route(self):
        chat_id = 980405
        _call_webhook(_make_update(970405001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertIn(chat_id, pending_quantity_price_intent)


class TestExplicitOneDomainCommandsSkipClarification(QuantityPriceClarificationTestCase):
    # 14. "Додай молоко 1 л до покупок" -> existing shopping add, no
    # clarification (no money marker at all in this phrase).
    def test_explicit_shopping_add_no_clarification(self):
        chat_id = 980501
        with patch.object(household_router, "_ask_gemini_explicit_add_items") as mock_items:
            mock_items.return_value = {
                "items": [{"name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}],
                "unresolved_fragments": [],
            }
            _call_webhook(_make_update(970501001, chat_id, "Додай молоко 1 л до покупок"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)

    # 15. "Запиши 4,99 zł за молоко 1 л" -> existing expense add, no
    # clarification.
    def test_explicit_expense_verb_no_clarification(self):
        chat_id = 980502
        self.mock_expense_router.return_value = {
            "intent": "create_expense", "amount": "4.99", "currency": "PLN",
            "category": "Продукти", "description": "Молоко 1 л", "expense_date": "2026-07-16",
            "selected_numbers": [], "unresolved_fragments": [],
        }
        _call_webhook(_make_update(970502001, chat_id, "Запиши 4,99 zł за молоко 1 л"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_expense)

    # 16. "Додай молоко 1 л до запасів" -> existing inventory add, no
    # clarification.
    def test_explicit_inventory_add_no_clarification(self):
        chat_id = 980503
        with patch.object(household_router, "_ask_gemini_explicit_add_items") as mock_items:
            mock_items.return_value = {
                "items": [{"name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}],
                "unresolved_fragments": [],
            }
            _call_webhook(_make_update(970503001, chat_id, "Додай молоко 1 л до запасів"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)


class TestRoutingRegressions(QuantityPriceClarificationTestCase):
    # 17. Confirm/cancel have priority — a stray confirm/cancel with no
    # matching pending state is handled by the existing generic guard, never
    # mistaken for a new clarification trigger.
    def test_confirm_button_alone_does_not_trigger_clarification(self):
        chat_id = 980601
        _call_webhook(_make_update(970601001, chat_id, "✅ Так, застосувати"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)

    # 18. Active preview has priority — an already-open pending_global_
    # household preview blocks a brand new ambiguous message from starting
    # a second clarification.
    def test_active_preview_blocks_new_clarification(self):
        chat_id = 980602
        pending_global_household[chat_id] = {
            "add_shopping_items": [{"name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка",
                                     "canonical_name": "хліб", "quantity_value": 1.0, "quantity_unit": "шт.",
                                     "quantity_inferred": False, "was_corrected": False}],
            "add_inventory_items": [], "consume_changes": [], "inventory_targets": [],
            "new_expenses": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(970602001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        # Original preview untouched.
        self.assertEqual(len(pending_global_household[chat_id]["add_shopping_items"]), 1)

    # 19. Repeated choice press does not apply the action twice.
    def test_repeated_choice_press_does_not_double_apply(self):
        chat_id = 980603
        _call_webhook(_make_update(970603001, chat_id, "Молоко 1 л 4,99 zł"))
        _call_webhook(_make_update(970603002, chat_id, "💸 Записати витрату"))
        self.assertIn(chat_id, pending_expense)
        # Pending state already popped — a second identical choice press
        # (e.g. a duplicate/late Telegram delivery) is now just plain text
        # with no active clarification and no active expense preview
        # guard... but the expense preview IS active (has_active_expense_
        # preview), so it must be guarded, never silently re-triggered.
        _call_webhook(_make_update(970603003, chat_id, "💸 Записати витрату"))
        self.assertEqual(len(pending_expense), 1)


if __name__ == "__main__":
    unittest.main()
