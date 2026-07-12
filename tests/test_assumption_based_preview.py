"""Assumption-Based Purchase Preview V1 — a follow-up to Purchase Event
Planner V1 / Safe Discount Calculation V1. The bot used to be too
conservative for multi-item purchase stories with discounts: ANY discount/
percentage mention anywhere in a message blocked EVERY add_expense op in
that same message (even ones with a perfectly clear, unrelated final
amount), producing repeated "please clarify" notes instead of a usable
preview. This relaxes that: the bot now proposes the most likely plan per
item, marking genuine assumptions clearly, while still never writing to the
database before confirmation and never inventing a number that isn't
literally present in the text somewhere.

New op types (see household_router.py's HOUSEHOLD_ROUTER_PROMPT types 5/8/9
and _validate_operations_detailed):
- add_expense gained an optional context_note (purely informational, e.g.
  "original price 650 zł, bought for 570 zł") and is no longer redirected
  to a note just because a discount word appears elsewhere in the message.
- assumed_expense: original_price - discount_amount (or - discount_percent
  %), COMPUTED IN PYTHON from literal pieces only, rendered with a visible
  "Припущення: ..." note attached to that one item.
- discount_expense (Safe Discount Calculation V1, per-unit price × bought
  quantity) is UNCHANGED — still requires exactly one add_inventory item.

Covers, at the webhook level (household_router._ask_gemini_household_router
is mocked; no real Gemini/Telegram/Supabase call anywhere here):
1. The full live multi-item example from the work order.
2. "found it for X" -> plain add_expense, no assumption.
3. "bought it for X" with a Latin product name -> add_expense + product name
   normalized to "Комод" via the extended _NAME_SYNONYMS table.
4. "didn't pay anything" -> add_inventory only, no expense op at all.
5. Original price + flat discount, no final-amount wording -> assumed_
   expense with a visible warning.
6. "bought for X after a discount" -> plain add_expense with X directly,
   never X-minus-discount.
7. A genuinely unassociated amount still never becomes a real expense.
8. discount_expense (Safe Discount Calculation V1) still works unchanged.
9. Price Clarification V1 (pending-preview follow-up) still works unchanged.
10. Confirm/cancel still work on an assumption-based preview.
"""
import sys
import os
import importlib.util
import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import household_router  # noqa: E402
from bot import pending_global_household  # noqa: E402

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))

LIVE_EXAMPLE_TEXT = (
    "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. "
    "Також ми купили дитячу ліжечку, яке на сайті оригінальному коштувало 650, але ми знайшли його за 570. "
    "We bought a komod, which cost 627, but we bought it for 527. We also have an auto-carsel, but we didn't "
    "pay anything for this. I only bought a gift for her sister for 60 for 60, to thank her for the "
    "auto-carsel."
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class AssumptionBasedPreviewWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_shopping_items = patch.object(bot, "get_active_shopping_items", return_value=[])
        patcher_shopping_items.start()
        self.addCleanup(patcher_shopping_items.stop)

        patcher_inventory_items = patch.object(bot, "get_inventory_items", return_value=[])
        patcher_inventory_items.start()
        self.addCleanup(patcher_inventory_items.stop)

        patcher_recent_expenses = patch.object(bot, "get_recent_expenses_for_deletion", return_value=[])
        patcher_recent_expenses.start()
        self.addCleanup(patcher_recent_expenses.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_household_router = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_household_router = patcher_household_router.start()
        self.addCleanup(patcher_household_router.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 1 — the full live multi-item example.
# =========================
class TestFullLiveExample(AssumptionBasedPreviewWebhookTestCase):
    def test_full_multi_item_story_produces_expected_preview(self):
        chat_id = 993001
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "assumed_expense", "description": "Візочок для дитини", "original_price": "3300",
                 "discount_amount": "150", "currency": "PLN"},
                {"type": "add_expense", "amount": "570", "currency": "PLN", "category": "Дім і рахунки",
                 "description": "Дитяче ліжечко", "expense_date": "2026-07-12",
                 "context_note": "Оригінальна/сайтова ціна 650 zł, куплено за 570 zł"},
                {"type": "add_expense", "amount": "527", "currency": "PLN", "category": "Дім і рахунки",
                 "description": "komod", "expense_date": "2026-07-12",
                 "context_note": "Оригінальна ціна 627 zł, куплено за 527 zł"},
                {"type": "add_inventory", "name": "Автокрісло", "quantity_text": "1", "category": "Інше їстівне"},
                {"type": "add_expense", "amount": "60", "currency": "PLN", "category": "Інше",
                 "description": "Подарунок сестрі", "expense_date": "2026-07-12"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(993001001, chat_id, LIVE_EXAMPLE_TEXT))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]

        by_desc = {ne["description"]: ne for ne in data["new_expenses"]}
        self.assertEqual(set(by_desc), {"Візочок для дитини", "Дитяче ліжечко", "Комод", "Подарунок сестрі"})
        self.assertEqual(by_desc["Візочок для дитини"]["amount"], Decimal("3150.00"))
        self.assertEqual(by_desc["Дитяче ліжечко"]["amount"], Decimal("570.00"))
        self.assertEqual(by_desc["Комод"]["amount"], Decimal("527.00"))
        self.assertEqual(by_desc["Подарунок сестрі"]["amount"], Decimal("60.00"))
        # "komod" was normalized to "Комод" for display (_NAME_SYNONYMS).
        self.assertNotIn("komod", by_desc)

        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["name"], "Автокрісло")
        self.assertEqual(data["add_inventory_items"][0]["quantity_value"], 1)
        # The car seat is inventory-only — never an expense of its own.
        self.assertNotIn("Автокрісло", by_desc)

        texts = self._sent_texts()
        joined = "\n".join(texts)
        self.assertIn("3150,00 zł", joined)
        self.assertIn("570,00 zł", joined)
        self.assertIn("527,00 zł", joined)
        self.assertIn("60,00 zł", joined)
        self.assertIn("Автокрісло", joined)
        # The stroller's assumption note is present and visible.
        self.assertIn("Припущення", joined)
        self.assertIn("3300", joined.replace(",00", ""))
        self.assertIn("150", joined.replace(",00", ""))


# =========================
# 2/3 — "found/bought it for X" is a plain, non-assumption add_expense.
# =========================
class TestFoundOrBoughtForIsFinalAmount(AssumptionBasedPreviewWebhookTestCase):
    def test_baby_bed_found_for_price_uses_final_amount(self):
        chat_id = 993002
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_expense", "amount": "570", "currency": "PLN", "category": "Дім і рахунки",
                 "description": "Дитяче ліжечко", "expense_date": "2026-07-12",
                 "context_note": "Оригінальна/сайтова ціна 650 zł, куплено за 570 zł"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(
            993002001, chat_id, "купили дитяче ліжечко, на сайті коштувало 650, але знайшли за 570",
        ))
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("570.00"))
        self.assertIsNone(data["new_expenses"][0].get("assumption_note"))

    def test_komod_bought_for_price_normalizes_name(self):
        chat_id = 993003
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_expense", "amount": "527", "currency": "PLN", "category": "Дім і рахунки",
                 "description": "komod", "expense_date": "2026-07-12",
                 "context_note": "Оригінальна ціна 627 zł, куплено за 527 zł"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(
            993003001, chat_id, "we bought a komod, which cost 627, but we bought it for 527",
        ))
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("527.00"))
        self.assertEqual(data["new_expenses"][0]["description"], "Комод")


# =========================
# 4 — a free item is inventory-only, never a 0 zł or ambiguous expense.
# =========================
class TestFreeItemNeverGetsAnExpense(AssumptionBasedPreviewWebhookTestCase):
    def test_car_seat_received_for_free(self):
        chat_id = 993004
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Автокрісло", "quantity_text": "1", "category": "Інше їстівне"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(993004001, chat_id, "автокрісло дісталось безкоштовно"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"], [])
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["name"], "Автокрісло")


# =========================
# 5/6 — assumed_expense (no final-amount wording) vs. add_expense ("bought
# for X after discount", a clear final amount).
# =========================
class TestAssumptionVsFinalAmount(AssumptionBasedPreviewWebhookTestCase):
    def test_price_and_discount_without_final_wording_is_an_assumption(self):
        chat_id = 993005
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "assumed_expense", "description": "Візочок", "original_price": "3300",
                 "discount_amount": "150", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(993005001, chat_id, "візочок коштував 3300, знижка 150"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        ne = data["new_expenses"][0]
        self.assertEqual(ne["amount"], Decimal("3150.00"))
        self.assertIsNotNone(ne["assumption_note"])
        self.assertIn("Припущення", ne["assumption_note"])
        texts = self._sent_texts()
        self.assertTrue(any("Припущення" in t and "3150" in t.replace(",00", "") for t in texts))

    def test_bought_for_amount_after_discount_uses_that_amount_directly(self):
        chat_id = 993006
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_expense", "amount": "3300", "currency": "PLN", "category": "Дім і рахунки",
                 "description": "Візочок", "expense_date": "2026-07-12"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(993006001, chat_id, "візочок купили за 3300 після знижки 150"))
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("3300.00"))
        self.assertIsNone(data["new_expenses"][0].get("assumption_note"))


# =========================
# 7 — a genuinely unassociated/fabricated amount never becomes a real
# expense, regardless of the relaxed discount handling.
# =========================
class TestUnassociatedAmountStaysSafe(AssumptionBasedPreviewWebhookTestCase):
    def test_amount_not_literally_in_text_never_becomes_an_expense(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "1 кг", "category": "Солодке та снеки"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": "2026-07-12"},
            ],
            "unresolved_fragments": [],
        }
        text = "Печиво коштувало 20 zł, було 50% знижки, я купив кілограм"
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text=text,
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["new_expenses"], [])
        self.assertTrue(payload["expense_notes"])

    def test_assumed_expense_with_non_literal_pieces_stays_safe(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "assumed_expense", "description": "Візочок", "original_price": "9999",
                 "discount_amount": "1", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text="візочок коштував 3300, знижка 150",
        )
        self.assertEqual(kind, "ambiguous_expense")
        self.assertTrue(payload)


# =========================
# 8 — discount_expense (Safe Discount Calculation V1) is unchanged.
# =========================
class TestDiscountExpenseUnchanged(AssumptionBasedPreviewWebhookTestCase):
    def test_explicit_unit_price_and_basis_still_computes(self):
        chat_id = 993008
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(
                993008001, chat_id,
                "Печиво коштувало 20 zł за кілограм, було 50% знижки, я купив пів кілограма і потім ще пів",
            ))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))


# =========================
# 9 — Price Clarification V1 (pending-preview follow-up) is unaffected.
# =========================
class TestPriceClarificationStillWorks(AssumptionBasedPreviewWebhookTestCase):
    def test_pending_preview_price_clarification_still_computes(self):
        chat_id = 993009
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "ambiguous_expense", "note": "20 zł — це ціна за 1 кг, за 0,5 кг чи фінальна сума?"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(
            993009001, chat_id, "Печиво коштувало 20 zł, було 50% знижки, я купив пів кілограма і потім ще пів",
        ))
        self.assertEqual(pending_global_household[chat_id]["new_expenses"], [])
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(993009002, chat_id, "за пів кілограма 5 zl"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))


# =========================
# 10 — confirm/cancel still work on an assumption-based preview.
# =========================
class TestConfirmCancel(AssumptionBasedPreviewWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        _database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
        _spec = importlib.util.spec_from_file_location(
            "real_database_for_assumption_based_preview_test", _database_path,
        )
        real_database = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(real_database)
        cls._real_database = real_database
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def _seed_multi_item_preview(self, chat_id):
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "assumed_expense", "description": "Візочок для дитини", "original_price": "3300",
                 "discount_amount": "150", "currency": "PLN"},
                {"type": "add_inventory", "name": "Автокрісло", "quantity_text": "1", "category": "Інше їстівне"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(chat_id * 1000, chat_id, LIVE_EXAMPLE_TEXT))

    def test_confirm_writes_assumption_expense_and_free_item(self):
        chat_id = 993010
        self._seed_multi_item_preview(chat_id)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook(_make_update(993010001, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(len(kwargs["add_inventory_items"]), 1)
        self.assertEqual(len(kwargs["new_expenses"]), 1)
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("3150.00"))
        # Cosmetic-only fields never reach the DB-write call.
        self.assertNotIn("assumption_note", kwargs["new_expenses"][0])
        self.assertNotIn("context_note", kwargs["new_expenses"][0])
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_writes_nothing(self):
        chat_id = 993011
        self._seed_multi_item_preview(chat_id)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(993011002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
