"""Unified Mini Action Planner V1 — webhook-level integration tests.
mini_action_planner.classify() is patched directly (its own JSON-parsing
logic is already covered in tests/test_mini_action_planner_module.py) so
these tests focus purely on bot.py's routing/glue: does each of the five
actions reach the right existing handler, does add_to_shopping/add_to_
inventory ever write to the DB before confirm, and does a deterministic
route still win over the planner entirely. No real Gemini/Telegram/Supabase
call happens anywhere in this file."""
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
import mini_action_planner  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class MiniActionPlannerWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


class TestAddToShopping(MiniActionPlannerWebhookTestCase):
    def test_creates_pending_preview_without_db_write(self):
        chat_id = 991701
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_shopping",
            "items": [{"name": "Молоко", "quantity_text": "1 л"}],
        }):
            with patch.object(bot, "apply_global_household_operations") as mock_apply:
                _call_webhook(_make_update(991701001, chat_id, "треба докупити молока"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        entry = pending_global_household[chat_id]
        self.assertEqual(len(entry["add_shopping_items"]), 1)
        self.assertEqual(entry["add_shopping_items"][0]["name"], "Молоко")
        self.assertEqual(entry["add_inventory_items"], [])
        texts = self._sent_texts()
        self.assertTrue(any("Молоко" in t for t in texts))
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())

    def test_confirm_writes_planner_items(self):
        chat_id = 991702
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_shopping",
            "items": [{"name": "Молоко", "quantity_text": "1 л"}],
        }):
            _call_webhook(_make_update(991702001, chat_id, "треба докупити молока"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 1, "inventory_added": 0, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": None, "expense_deleted": False,
            }
            _call_webhook(_make_update(991702002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(kwargs["add_shopping_items"][0]["name"], "Молоко")
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_writes_nothing(self):
        chat_id = 991703
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_shopping",
            "items": [{"name": "Молоко", "quantity_text": "1 л"}],
        }):
            _call_webhook(_make_update(991703001, chat_id, "треба докупити молока"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(991703002, chat_id, "❌ Скасувати"))
        mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)

    def test_empty_items_falls_back_without_preview(self):
        chat_id = 991704
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_shopping", "items": [],
        }):
            with patch.object(bot, "call_gemini", return_value="Загальна відповідь.") as mock_gemini:
                _call_webhook(_make_update(991704001, chat_id, "щось геть незрозуміле"))
        self.assertNotIn(chat_id, pending_global_household)
        mock_gemini.assert_called_once()


class TestAddToInventory(MiniActionPlannerWebhookTestCase):
    def test_creates_pending_preview_without_db_write(self):
        chat_id = 991711
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_inventory",
            "items": [{"name": "Сир", "quantity_text": "500 г"}],
        }):
            with patch.object(bot, "get_inventory_items", return_value=[]):
                with patch.object(bot, "apply_global_household_operations") as mock_apply:
                    _call_webhook(_make_update(991711001, chat_id, "треба докупити сиру"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        entry = pending_global_household[chat_id]
        self.assertEqual(entry["add_shopping_items"], [])
        self.assertEqual(len(entry["add_inventory_items"]), 1)
        self.assertEqual(entry["add_inventory_items"][0]["name"], "Сир")
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())


class TestAskInventory(MiniActionPlannerWebhookTestCase):
    def test_routes_to_existing_readonly_handler(self):
        chat_id = 991721
        items = [{
            "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "л", "quantity_text": "1 л",
        }]
        with patch.object(mini_action_planner, "classify", return_value={"action": "ask_inventory", "items": []}):
            with patch.object(bot, "get_inventory_items", return_value=items) as mock_items:
                with patch.object(bot, "apply_global_household_operations") as mock_apply:
                    _call_webhook(_make_update(991721001, chat_id, "цікаво що там в холодильнику"))
        mock_items.assert_called_once()
        mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко" in t for t in texts))


class TestMealIdeas(MiniActionPlannerWebhookTestCase):
    def test_routes_to_existing_meal_ideas_handler_with_force(self):
        # This text deliberately does NOT match meal_ideas' own deterministic
        # gate (_looks_like_meal_ideas_request) — Phase D's EARLIER,
        # ungated meal_ideas slot must therefore decline it on its own, and
        # only the planner's force=True call (this test's actual subject)
        # can produce a meal-ideas answer. Using the REAL try_handle_meal_
        # ideas (only its DB/Gemini dependencies mocked) instead of mocking
        # the function itself is what makes that distinction meaningful.
        chat_id = 991731
        text = "щось незвичне про вечерю"
        items = [{"name": "Яйця", "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."}]
        with patch.object(mini_action_planner, "classify", return_value={"action": "meal_ideas", "items": []}):
            with patch.object(bot, "get_inventory_items", return_value=items) as mock_items:
                with patch.object(bot, "call_gemini", return_value="🍽️ Ідеї з того, що є вдома:\n\n1. Омлет") as mock_gemini:
                    _call_webhook(_make_update(991731001, chat_id, text))
        mock_items.assert_called_once()
        mock_gemini.assert_called_once()
        self.assertTrue(any("Омлет" in t for t in self._sent_texts()))


class TestUnknownFallsBackToGeneralAi(MiniActionPlannerWebhookTestCase):
    def test_unknown_action_falls_through_to_general_ai(self):
        # Household-like enough to pass the pre-gate (so classify()'s own
        # mocked "unknown" result is what's actually exercised here) but
        # genuinely ambiguous — a realistic case for Gemini itself to
        # classify as "unknown".
        chat_id = 991741
        text = "треба щось на вечерю, ще не знаю що саме"
        with patch.object(mini_action_planner, "classify", return_value={"action": "unknown", "items": []}) as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Звичайна відповідь.") as mock_gemini:
                _call_webhook(_make_update(991741001, chat_id, text))
        mock_classify.assert_called_once()
        mock_gemini.assert_called_once()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Звичайна відповідь." == t for t in self._sent_texts()))

    def test_ungated_text_never_reaches_classify(self):
        chat_id = 991743
        with patch.object(mini_action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Звичайна відповідь.") as mock_gemini:
                _call_webhook(_make_update(991743001, chat_id, "Яка сьогодні погода?"))
        mock_classify.assert_not_called()
        mock_gemini.assert_called_once()

    def test_invalid_gemini_json_falls_back_safely(self):
        # No mocking of classify() itself here — call_gemini returns
        # unparseable text for BOTH the planner call and the general-chat
        # call, exercising the real end-to-end fallback path. Text is
        # household-like enough to pass the pre-gate on purpose, and
        # deliberately avoids household_read's OWN (broader, pre-existing)
        # topic gate — "закінч"/"холодильник" aren't in it, unlike "купити"/
        # "запас"/"вдома" — so exactly 2 calls happen (planner + general
        # chat), not a 3rd from household_read's own classifier.
        chat_id = 991742
        with patch.object(bot, "call_gemini", return_value="це геть не json") as mock_gemini:
            _call_webhook(_make_update(991742001, chat_id, "закінчився сир, не пам'ятаю що робити"))
        self.assertEqual(mock_gemini.call_count, 2)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("це геть не json" == t for t in self._sent_texts()))


class TestDeterministicRouteWinsOverPlanner(MiniActionPlannerWebhookTestCase):
    def test_explicit_add_never_reaches_planner(self):
        chat_id = 991751
        with patch.object(bot.household_router, "_ask_gemini_explicit_add_items", return_value={
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }):
            with patch.object(mini_action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(991751001, chat_id, "Додай до покупок молоко"))
        mock_classify.assert_not_called()
        self.assertIn(chat_id, pending_global_household)

    def test_active_pending_preview_never_reaches_planner(self):
        chat_id = 991752
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expenses": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        try:
            with patch.object(mini_action_planner, "classify") as mock_classify:
                _call_webhook(_make_update(991752001, chat_id, "щось нове про молоко"))
            mock_classify.assert_not_called()
        finally:
            pending_global_household.pop(chat_id, None)


class TestPreGateWebhookLevel(MiniActionPlannerWebhookTestCase):
    """The four exact cases from the Mini Action Planner pre-gate work
    order — webhook-level, real (unmocked) looks_household_like."""

    def test_greeting_does_not_call_planner_reaches_general_ai(self):
        chat_id = 991761
        with patch.object(mini_action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Привіт! Усе добре.") as mock_gemini:
                _call_webhook(_make_update(991761001, chat_id, "Привіт, як справи?"))
        mock_classify.assert_not_called()
        mock_gemini.assert_called_once()
        self.assertTrue(any("Привіт! Усе добре." == t for t in self._sent_texts()))

    def test_explanatory_product_question_does_not_call_planner(self):
        chat_id = 991762
        with patch.object(mini_action_planner, "classify") as mock_classify:
            with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн.") as mock_gemini:
                _call_webhook(_make_update(991762001, chat_id, "Поясни, чому молоко згортається в каві?"))
        mock_classify.assert_not_called()
        mock_gemini.assert_called_once()

    def test_buying_intent_calls_planner(self):
        chat_id = 991763
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_shopping", "items": [{"name": "Молоко", "quantity_text": ""}],
        }) as mock_classify:
            _call_webhook(_make_update(991763001, chat_id, "молока б докупити"))
        mock_classify.assert_called_once()
        self.assertIn(chat_id, pending_global_household)

    def test_dinner_request_calls_planner_and_routes_to_meal_ideas(self):
        chat_id = 991764
        items = [{"name": "Яйця", "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."}]
        with patch.object(mini_action_planner, "classify", return_value={"action": "meal_ideas", "items": []}) as mock_classify:
            with patch.object(bot, "get_inventory_items", return_value=items):
                with patch.object(bot, "call_gemini", return_value="🍽️ Ідеї з того, що є вдома:\n\n1. Омлет"):
                    _call_webhook(_make_update(991764001, chat_id, "щось треба на вечерю"))
        mock_classify.assert_called_once()
        self.assertTrue(any("Омлет" in t for t in self._sent_texts()))


class TestTelegramTranscriptRegressions(MiniActionPlannerWebhookTestCase):
    """The exact live-Telegram bug reports from the "prioritize action
    intents before inventory reads" work order. household_read/meal_ideas
    are REAL here (not mocked) — these tests prove the routing fix itself
    (household_read's new _looks_like_stronger_action_intent override, see
    tests/test_household_read_context.py for that module's own unit tests),
    not just that the planner CAN handle these actions in isolation."""

    # Case 3 — "молока б докупити": used to answer "Знайшов декілька
    # позицій «молоко»..." (household_read misreading it as an inventory
    # presence lookup). Must now build a shopping preview instead.
    def test_dokupyty_milk_builds_shopping_preview_not_presence_lookup(self):
        chat_id = 991771
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_shopping", "items": [{"name": "Молоко", "quantity_text": ""}],
        }):
            with patch.object(bot, "apply_global_household_operations") as mock_apply:
                _call_webhook(_make_update(991771001, chat_id, "молока б докупити"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        entry = pending_global_household[chat_id]
        self.assertEqual(entry["add_shopping_items"][0]["name"], "Молоко")
        texts = self._sent_texts()
        self.assertFalse(any("Знайшов декілька позицій" in t for t in texts))
        self.assertFalse(any(t.startswith("Ні, «") for t in texts))

    # Case 4 — "у нас є 10 яєць і 2 літри молока": used to answer "Ні, «яйця,
    # молоко» зараз немає в запасах." (household_read misreading the
    # declarative statement as a presence question). Must now build an
    # inventory preview instead.
    def test_declarative_have_builds_inventory_preview_not_presence_lookup(self):
        chat_id = 991772
        with patch.object(mini_action_planner, "classify", return_value={
            "action": "add_to_inventory",
            "items": [{"name": "Яйця", "quantity_text": "10"}, {"name": "Молоко", "quantity_text": "2 л"}],
        }):
            with patch.object(bot, "get_inventory_items", return_value=[]):
                with patch.object(bot, "apply_global_household_operations") as mock_apply:
                    _call_webhook(_make_update(991772001, chat_id, "у нас є 10 яєць і 2 літри молока"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        names = {item["name"] for item in pending_global_household[chat_id]["add_inventory_items"]}
        self.assertEqual(names, {"Яйця", "Молоко"})
        texts = self._sent_texts()
        self.assertFalse(any(t.startswith("Ні, «") for t in texts))

    # Case 5 — "на вечерю щось з того що є": used to answer with a full
    # inventory overview (household_read misreading "що є" as
    # inventory_overview). Must now route to meal ideas instead.
    def test_dinner_from_what_we_have_routes_to_meal_ideas_not_overview(self):
        chat_id = 991773
        items = [{"name": "Яйця", "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."}]
        with patch.object(mini_action_planner, "classify", return_value={"action": "meal_ideas", "items": []}):
            with patch.object(bot, "get_inventory_items", return_value=items) as mock_items:
                with patch.object(bot, "call_gemini", return_value="🍽️ Ідеї з того, що є вдома:\n\n1. Омлет"):
                    _call_webhook(_make_update(991773001, chat_id, "на вечерю щось з того що є"))
        mock_items.assert_called_once()
        texts = self._sent_texts()
        self.assertTrue(any("Омлет" in t for t in texts))
        self.assertFalse(any(t.startswith("🧊 Запаси") for t in texts))

    # Case 6 — the complex cookie/discount/past-date prompt: household_
    # router.gate() matches ("купив"), so this is handled entirely by the
    # (pre-existing) Global Household Router command route, BEFORE Phase D
    # / mini_action_planner is ever reached. Must reject the invented
    # discount-computed expense — no preview, no DB write — rather than
    # silently accepting fabricated prices.
    def test_complex_discount_prompt_never_creates_expense_preview(self):
        chat_id = 991774
        text = (
            "Вчора в магазині позаду дому я купив печиво по знижці, воно коштувало 20, "
            "але на нього було 50% знижки. Тому я взяв пів кілограма, але потім вернувся "
            "і докупив ще раз так само."
        )
        fake_router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "пів кілограма",
                 "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "пів кілограма",
                 "category": "Солодке та снеки"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": "2026-07-11"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": "2026-07-11"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot.household_router, "_ask_gemini_household_router", return_value=fake_router_result):
            with patch.object(mini_action_planner, "classify") as mock_classify:
                with patch.object(bot, "apply_global_household_operations") as mock_apply:
                    _call_webhook(_make_update(991774001, chat_id, text))
        mock_apply.assert_not_called()
        mock_classify.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)


if __name__ == "__main__":
    unittest.main()
