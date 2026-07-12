"""Purchase Event Planner V1 — natural purchase stories (long narrated
messages describing a purchase, optionally with a discount/original price)
must never turn into a fabricated/computed expense preview, and must never
silently discard a purchase whose narrative filler happened to confuse the
old "unresolved_fragments blocks everything" behavior.

Covers, at the webhook level (household_router._ask_gemini_household_router
is mocked; no real Gemini/Telegram/Supabase call happens anywhere here):
1. The exact long cookie/discount story -> safe inventory-only preview
   (Печиво merged to 1 кг), no expense, no DB write before confirm.
1b. Same, but Gemini ignores the "ambiguous_expense" prompt instruction and
   emits a plain add_expense anyway — the Python-side discount-marker guard
   (household_router._DISCOUNT_MARKER_RE) must catch it regardless.
2. An explicit, undiscounted price ("купив печиво за 20 zł") still builds a
   normal inventory + expense compound preview, unaffected.
3. The same item purchased twice in one message with the same quantity
   merges into a single inventory preview line ("Печиво — 1 кг").
4/5. "молока б докупити" / "у нас є 10 яєць і 2 літри молока" still reach
   Mini Action Planner and build the same shopping/inventory previews as
   before this feature (household_router.gate() doesn't match either, so
   the Global Household Router path is never even entered).
6. A pure explanatory question still reaches general AI-chat, never the
   planner or the router.
7. Confirm/cancel still work exactly as before for one of these previews.

Plus pure (no webhook) tests directly against household_router for the
new "ambiguous_expense" kind (response B) and reason/fragment dedup.
"""
import sys
import os
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
import mini_action_planner  # noqa: E402
from bot import pending_global_household  # noqa: E402

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _todays_warsaw_date_iso():
    return datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


COOKIE_STORY_TEXT = (
    "Вчора після роботи я зайшов в магазин заді дому. Там дід з вусами продавав дуже смачне "
    "печиво. Воно в загальному коштує 20 злотих, але на нього було 50% знижки. Я купив пів "
    "кілограма, але воно було таке смачне, що я вернувся і купив ще пів."
)


class PurchaseEventPlannerWebhookTestCase(unittest.TestCase):
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
# 1/1b — the exact cookie/discount story.
# =========================
class TestCookieStory(PurchaseEventPlannerWebhookTestCase):
    def test_safe_inventory_only_preview_no_expense_no_db_write(self):
        chat_id = 990101
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "ambiguous_expense",
                 "note": "Ціна 20 zł зі знижкою 50% — неясно, скільки фактично сплачено"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(990101001, chat_id, COOKIE_STORY_TEXT))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["name"], "Печиво")
        self.assertEqual(data["add_inventory_items"][0]["quantity_value"], 1)
        self.assertEqual(data["new_expenses"], [])
        self.assertIsNone(data["new_expense"])
        texts = self._sent_texts()
        self.assertTrue(any("Печиво" in t and "1 кг" in t for t in texts))
        self.assertTrue(any("знижк" in t.lower() for t in texts))

    def test_literal_add_expense_with_discount_marker_present_now_allowed(self):
        # Superseded by Assumption-Based Purchase Preview V1 (see its work
        # order's rule 5: "do not hard-block the whole expense just because
        # the word discount/знижка exists") — a discount word ANYWHERE in
        # the message no longer redirects every add_expense to a note by
        # itself; Gemini is instead expected to use assumed_expense/
        # ambiguous_expense for genuinely computed amounts (see
        # tests/test_assumption_based_preview.py), while a plain add_expense
        # with a literal amount is trusted like any other. The one Python-
        # side guarantee that remains: an amount NOT literally typed by the
        # user (i.e. genuinely fabricated) still never reaches new_expenses
        # — see TestCookieStory's other test in this class, and
        # test_computed_discount_amount_is_rejected in
        # test_global_household_router.py.
        chat_id = 990102
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "add_expense", "amount": "20", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": _todays_warsaw_date_iso()},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(990102001, chat_id, COOKIE_STORY_TEXT))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("20.00"))
        self.assertEqual(data["add_inventory_items"][0]["quantity_value"], 1)


# =========================
# 2 — explicit, undiscounted price still builds a normal compound preview.
# =========================
class TestExplicitPriceNoDiscount(PurchaseEventPlannerWebhookTestCase):
    def test_explicit_price_creates_inventory_and_expense_preview(self):
        chat_id = 990103
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "", "category": "Солодке та снеки"},
                {"type": "add_expense", "amount": "20", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": _todays_warsaw_date_iso()},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(990103001, chat_id, "купив печиво за 20 zł"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertIsNotNone(data["new_expense"])
        self.assertEqual(data["new_expense"]["amount"], Decimal("20"))
        self.assertEqual(len(data["new_expenses"]), 1)


# =========================
# 3 — repeated same-item, same-quantity purchase merges into one line.
# =========================
class TestRepeatedPurchaseMerges(PurchaseEventPlannerWebhookTestCase):
    def test_two_half_kilos_merge_into_one_kilo(self):
        chat_id = 990104
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(990104001, chat_id, "вчора купив пів кілограма печива і потім ще пів"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["quantity_value"], 1)
        self.assertEqual(data["new_expenses"], [])
        texts = self._sent_texts()
        self.assertTrue(any("Печиво" in t and "1 кг" in t for t in texts))


# =========================
# 4/5 — deterministic gates un-touched by this feature: household_router.
# gate() doesn't match either message, so Mini Action Planner still handles
# them exactly as before.
# =========================
class TestUnaffectedMiniPlannerRoutes(PurchaseEventPlannerWebhookTestCase):
    def test_shopping_intent_still_builds_shopping_preview(self):
        chat_id = 990105
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_shopping",
            "items": [{"name": "Молоко", "quantity_text": ""}],
        }):
            with patch.object(bot, "apply_global_household_operations") as mock_apply:
                _call_webhook(_make_update(990105001, chat_id, "молока б докупити"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_shopping_items"]), 1)
        self.assertEqual(data["add_inventory_items"], [])

    def test_declarative_have_still_builds_inventory_preview(self):
        chat_id = 990106
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_inventory",
            "items": [
                {"name": "Яйця", "quantity_text": "10"},
                {"name": "Молоко", "quantity_text": "2 л"},
            ],
        }):
            with patch.object(bot, "apply_global_household_operations") as mock_apply:
                _call_webhook(_make_update(990106001, chat_id, "у нас є 10 яєць і 2 літри молока"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 2)
        self.assertEqual(data["add_shopping_items"], [])


# =========================
# 6 — a pure explanatory question never reaches the planner or the router.
# =========================
class TestExplanatoryQuestionReachesGeneralAi(PurchaseEventPlannerWebhookTestCase):
    def test_explains_milk_question_reaches_general_ai_only(self):
        chat_id = 990107
        with patch.object(mini_action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн.") as mock_gemini:
                _call_webhook(_make_update(990107001, chat_id, "Поясни, чому молоко згортається в каві?"))
        mock_classify.assert_not_called()
        self.mock_household_router.assert_not_called()
        mock_gemini.assert_called_once()
        self.assertTrue(any("Бо це білок казеїн." == t for t in self._sent_texts()))


# =========================
# 7 — confirm/cancel still work for one of these new-shape previews.
# =========================
class TestConfirmCancelStillWork(PurchaseEventPlannerWebhookTestCase):
    def test_confirm_applies_inventory_only_preview(self):
        chat_id = 990108
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "ambiguous_expense", "note": "Ціна зі знижкою — неясно, скільки сплачено"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(990108001, chat_id, COOKIE_STORY_TEXT))
        self.assertIn(chat_id, pending_global_household)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": None, "expense_deleted": False,
            }
            _call_webhook(_make_update(990108002, chat_id, "✅ Так, застосувати"))
            mock_apply.assert_called_once()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("✅ Зміни застосовано." in t for t in self._sent_texts()))

    def test_cancel_applies_nothing(self):
        chat_id = 990109
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
                {"type": "ambiguous_expense", "note": "Ціна зі знижкою — неясно, скільки сплачено"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(990109001, chat_id, COOKIE_STORY_TEXT))
        self.assertIn(chat_id, pending_global_household)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(990109002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# =========================
# Pure (no webhook) household_router-level coverage: response B
# (ambiguous_expense-only clarification) and reason/fragment dedup.
# =========================
class TestAmbiguousExpenseOnlyClarification(unittest.TestCase):
    def test_pure_price_ambiguity_with_no_item_asks_for_clarification(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "ambiguous_expense", "note": "Ціна 20 zł зі знижкою — неясно, скільки сплачено"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text="Печиво коштує 20 zł, знижка 50%",
        )
        self.assertEqual(kind, "ambiguous_expense")
        self.assertEqual(payload, ["Ціна 20 zł зі знижкою — неясно, скільки сплачено"])
        message = household_router.format_ambiguous_expense_message(payload)
        self.assertIn("Не зовсім зрозуміло", message)
        self.assertIn("Ціна 20 zł зі знижкою", message)


class TestDedupReasonsAndFragments(unittest.TestCase):
    def test_duplicate_invalid_reasons_are_deduped(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_shopping", "name": "", "quantity_text": ""},
                {"type": "add_shopping", "name": "", "quantity_text": ""},
            ],
            "unresolved_fragments": [],
        }
        kind, reasons = household_router._validate_operations_detailed(router_result, [], [], NOW)
        self.assertEqual(kind, "invalid")
        self.assertEqual(reasons, ["Товар для покупок без назви."])

    def test_duplicate_unresolved_fragments_are_deduped(self):
        router_result = {
            "intent": "household_operations",
            "operations": [],
            "unresolved_fragments": ["щось незрозуміле", "щось незрозуміле"],
        }
        kind, fragments = household_router._validate_operations_detailed(router_result, [], [], NOW)
        self.assertEqual(kind, "unresolved")
        self.assertEqual(fragments, ["щось незрозуміле"])


if __name__ == "__main__":
    unittest.main()
