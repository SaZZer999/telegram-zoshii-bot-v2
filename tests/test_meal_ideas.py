"""Meal Ideas V1.

Fixes, as executable tests, the new "🍽 Що приготувати" behavior: the bot
must read the REAL household inventory and ask Gemini for meal ideas built
from it, instead of asking the user to manually type a product list
(`waiting_for_ingredients`/cooking-mode). Covers both entry points (the
dedicated button and a small fixed set of natural phrasings), the read-only
contract (no DB write, no preview/confirm/undo state), the empty-inventory
short-circuit, the Gemini-failure fallback, and routing priority against
cooking_mode/household_read/write commands.

No real Gemini, Telegram, or Supabase call happens anywhere in this file.
"""
import ast
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
import meal_ideas  # noqa: E402
import household_read_context  # noqa: E402
import household_router  # noqa: E402
import legacy_shopping_flow  # noqa: E402


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _inventory_rows():
    return [
        {"id": 1, "name": "Курка", "quantity_text": "1 шт.", "category": "М'ясо та риба",
         "canonical_name": "курка", "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_inferred": False},
        {"id": 2, "name": "Молоко", "quantity_text": "500 мл", "category": "Молочне та яйця",
         "canonical_name": "молоко", "quantity_value": 500.0, "quantity_unit": "мл", "quantity_inferred": False},
        {"id": 3, "name": "ser", "quantity_text": "1 шт.", "category": "Молочне та яйця",
         "canonical_name": "сир", "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_inferred": True},
        {"id": 4, "name": "mlekо", "quantity_text": "2 л", "category": "Молочне та яйця",
         "canonical_name": "молоко", "quantity_value": 2.0, "quantity_unit": "л", "quantity_inferred": False},
        {"id": 5, "name": "Шафран", "quantity_text": "0,00011 г", "category": "Соуси, спеції та бакалія",
         "canonical_name": "шафран", "quantity_value": 0.00011, "quantity_unit": "г", "quantity_inferred": False},
    ]


# =========================
# 10/11 — module boundary
# =========================
class TestModuleBoundary(unittest.TestCase):
    def test_no_forbidden_imports(self):
        source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meal_ideas.py")
        with open(source_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=source_path)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module.split(".")[0])
        forbidden = {"bot", "database", "flask", "psycopg", "telegram"}
        self.assertFalse(imported_names & forbidden, f"forbidden imports found: {imported_names & forbidden}")

    def test_no_db_write_function_names_in_module_source(self):
        source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meal_ideas.py")
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()
        forbidden_calls = (
            "add_shopping_items_batch", "add_inventory_items_batch", "update_shopping_items_batch",
            "update_inventory_items_batch", "delete_items_batch", "delete_inventory_items_batch",
            "apply_inventory_consumption", "apply_compound_inventory_operations",
            "apply_inventory_reconciliation", "execute_merge_shopping", "execute_merge_inventory",
            "add_expense", "delete_expense", "apply_undo_action", "apply_global_household_operations",
            "create_or_update_household_alias", "delete_household_alias", "init_db",
        )
        for name in forbidden_calls:
            self.assertNotIn(name, source)


# =========================
# 7 — inventory snapshot content (unit-level, no webhook)
# =========================
class TestInventorySnapshot(unittest.TestCase):
    def test_snapshot_includes_every_row_verbatim_with_quantity(self):
        deps = meal_ideas.MealIdeasDeps(
            get_household_and_user=MagicMock(),
            get_inventory_items=MagicMock(),
            format_quantity_display=MagicMock(side_effect=lambda v, u: f"{v:g} {u}"),
            call_gemini=MagicMock(),
            send_message=MagicMock(),
        )
        snapshot = meal_ideas._build_inventory_snapshot(deps, _inventory_rows())
        self.assertTrue(snapshot.startswith("Inventory:\n"))
        # Legacy raw names ("ser", "mlekо") appear untouched, never merged
        # or rewritten, and every row keeps its own quantity.
        self.assertIn("- Курка — 1 шт.", snapshot)
        self.assertIn("- Молоко — 500 мл", snapshot)
        self.assertIn("- ser — 1 шт.", snapshot)
        self.assertIn("- mlekо — 2 л", snapshot)
        self.assertIn("- Шафран — 0.00011 г", snapshot)
        self.assertEqual(snapshot.count("\n- "), len(_inventory_rows()))

    def test_row_without_any_quantity_has_no_dash_suffix(self):
        deps = meal_ideas.MealIdeasDeps(
            get_household_and_user=MagicMock(), get_inventory_items=MagicMock(),
            format_quantity_display=MagicMock(), call_gemini=MagicMock(), send_message=MagicMock(),
        )
        snapshot = meal_ideas._build_inventory_snapshot(deps, [{"name": "Сіль", "quantity_text": ""}])
        self.assertIn("- Сіль", snapshot)
        self.assertNotIn("Сіль —", snapshot)


# =========================
# Webhook-level integration
# =========================
class MealIdeasWebhookTestCase(unittest.TestCase):
    def setUp(self):
        bot.waiting_for_ingredients.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_global_household.clear()
        legacy_shopping_flow.shopping_mode.clear()

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_inventory = patch.object(bot, "get_inventory_items", return_value=_inventory_rows())
        self.mock_inventory = patcher_inventory.start()
        self.addCleanup(patcher_inventory.stop)

        patcher_shopping = patch.object(bot, "get_active_shopping_items", return_value=[])
        patcher_shopping.start()
        self.addCleanup(patcher_shopping.stop)

        patcher_gemini = patch.object(bot, "call_gemini", return_value="🍽️ Ідеї з того, що є вдома:\n\n1. Курка з молоком.")
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

    def tearDown(self):
        bot.waiting_for_ingredients.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()
        bot.pending_global_household.clear()
        legacy_shopping_flow.shopping_mode.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# 1. Button without variation selector
class TestButtonWithoutVariationSelector(MealIdeasWebhookTestCase):
    def test_button_calls_meal_ideas_and_never_sets_waiting_for_ingredients(self):
        chat_id = 850001
        _call_webhook(_make_update(850000001, chat_id, "🍽 Що приготувати"))
        texts = self._sent_texts()
        self.assertTrue(any("Ідеї з того" in t for t in texts))
        self.assertNotIn(chat_id, bot.waiting_for_ingredients)
        self.assertFalse(any("Напиши, які продукти" in t for t in texts))


# 2. Button with variation selector
class TestButtonWithVariationSelector(MealIdeasWebhookTestCase):
    def test_button_variant_works_the_same(self):
        chat_id = 850002
        _call_webhook(_make_update(850000002, chat_id, "🍽️ Що приготувати"))
        texts = self._sent_texts()
        self.assertTrue(any("Ідеї з того" in t for t in texts))
        self.assertNotIn(chat_id, bot.waiting_for_ingredients)


# 3. Natural text before general AI fallback
class TestNaturalTextTriggersMealIdeas(MealIdeasWebhookTestCase):
    def test_natural_phrase_triggers_meal_ideas(self):
        chat_id = 850003
        _call_webhook(_make_update(850000003, chat_id, "Що можна приготувати?"))
        texts = self._sent_texts()
        self.assertTrue(any("Ідеї з того" in t for t in texts))
        snapshot_sent = self.mock_gemini.call_args.args[0][0]["content"]
        self.assertTrue(snapshot_sent.startswith("Inventory:\n"))


# 4. "Що треба купити?" is not handled by meal_ideas
class TestBuyListQuestionNotHandledByMealIdeas(MealIdeasWebhookTestCase):
    def test_buy_list_question_goes_to_household_read_not_meal_ideas(self):
        chat_id = 850004
        with patch.object(meal_ideas, "try_handle_meal_ideas") as mock_meal_ideas:
            _call_webhook(_make_update(850000004, chat_id, "Що треба купити?"))
            mock_meal_ideas.assert_not_called()
        self.mock_gemini.assert_not_called()


# 5. "Привіт" is not handled by meal_ideas
class TestGreetingNotHandledByMealIdeas(MealIdeasWebhookTestCase):
    def test_greeting_reaches_general_ai_fallback_not_meal_ideas(self):
        # meal_ideas is still checked at its Phase D priority slot (real,
        # unmocked function) — it must correctly DECLINE ("Привіт" never
        # even touches inventory) and let general AI fallback answer, never
        # send a meal-ideas-flavored reply of its own.
        chat_id = 850005
        _call_webhook(_make_update(850000005, chat_id, "Привіт"))
        # get_inventory_items is the one call only meal_ideas' body (past
        # its gate) would ever make — proving the gate declined "Привіт"
        # without meal_ideas doing any work, real fallback's own DB/Gemini
        # calls are unaffected.
        self.mock_inventory.assert_not_called()
        self.mock_gemini.assert_called()
        for call in self.mock_gemini.call_args_list:
            system_prompt = call.args[1] if len(call.args) > 1 else call.kwargs.get("system_prompt")
            self.assertNotEqual(system_prompt, meal_ideas.MEAL_IDEAS_SYSTEM_PROMPT)


# 6. Empty inventory
class TestEmptyInventory(MealIdeasWebhookTestCase):
    def test_empty_inventory_sends_fixed_message_and_skips_gemini(self):
        chat_id = 850006
        self.mock_inventory.return_value = []
        _call_webhook(_make_update(850000006, chat_id, "🍽 Що приготувати"))
        texts = self._sent_texts()
        self.assertIn(
            "У запасах зараз нічого не знайшов. Додай продукти в запаси або напиши мені вручну, що є вдома.",
            texts,
        )
        self.mock_gemini.assert_not_called()


# 8. Gemini response is sent to the user verbatim
class TestGeminiResponseIsSentVerbatim(MealIdeasWebhookTestCase):
    def test_gemini_answer_forwarded_to_user(self):
        chat_id = 850008
        self.mock_gemini.return_value = "🍽️ Ідеї з того, що є вдома:\n\n1. Тестова страва."
        _call_webhook(_make_update(850000008, chat_id, "🍽 Що приготувати"))
        texts = self._sent_texts()
        self.assertIn("🍽️ Ідеї з того, що є вдома:\n\n1. Тестова страва.", texts)


# 9. Gemini failure -> polite fallback
class TestGeminiFailureFallback(MealIdeasWebhookTestCase):
    def test_gemini_failure_sends_polite_fallback(self):
        chat_id = 850009
        self.mock_gemini.return_value = None
        _call_webhook(_make_update(850000009, chat_id, "🍽 Що приготувати"))
        texts = self._sent_texts()
        self.assertIn("Не зміг зараз придумати страви. Спробуй ще раз трохи пізніше.", texts)


# 12. Active cooking_mode state (waiting_for_ingredients) has priority
class TestCookingModeHasPriorityOverMealIdeas(MealIdeasWebhookTestCase):
    def test_active_waiting_for_ingredients_wins_over_meal_ideas(self):
        chat_id = 850012
        bot.waiting_for_ingredients[chat_id] = True
        with patch.object(meal_ideas, "try_handle_meal_ideas") as mock_meal_ideas:
            _call_webhook(_make_update(850000012, chat_id, "Що можна приготувати?"))
            mock_meal_ideas.assert_not_called()
        self.assertNotIn(chat_id, bot.waiting_for_ingredients)


# 13. Household read has priority over meal_ideas
class TestMealIdeasHasPriorityOverHouseholdRead(MealIdeasWebhookTestCase):
    """Routing Stabilization v1: meal_ideas is now tried BEFORE household_
    read (was the reverse before this fix — see message_dispatcher.dispatch's
    own docstring for the live-bug this reordering fixes). A meal-ideas-
    shaped phrase must claim the message via meal_ideas' own narrow
    deterministic gate before household_read.try_handle_household_read is
    ever called at all — never the other way around."""
    def test_meal_ideas_claims_the_message_before_household_read_runs(self):
        chat_id = 850013
        with patch.object(meal_ideas, "try_handle_meal_ideas", return_value=True), \
                patch.object(household_read_context, "try_handle_household_read") as mock_household_read:
            _call_webhook(_make_update(850000013, chat_id, "Що можна приготувати?"))
            mock_household_read.assert_not_called()

    # A plain read-question that does NOT match meal_ideas' own gate still
    # reaches household_read's Phase-D slot exactly as before — meal_ideas'
    # REAL gate (not mocked here) correctly falls through for this text, so
    # this exercises the actual _looks_like_meal_ideas_request logic, not a
    # stubbed-out always-true/false mock. Uses a non-standard phrasing that
    # needs household_read's Gemini-classifier fallback (see
    # tests/test_household_read_context.py's GeminiClassifierTests) rather
    # than "Що треба купити?" — that exact phrase is already claimed by the
    # EARLIER direct_household_read command-route slot before Phase D (and
    # therefore before both meal_ideas and household_read) is ever reached
    # at all, so it can't tell the two Phase-D routes' relative order apart.
    def test_plain_read_question_still_reaches_household_read(self):
        chat_id = 850015
        with patch.object(household_read_context, "try_handle_household_read", return_value=True) as mock_household_read:
            _call_webhook(_make_update(850000015, chat_id, "Молока у нас ще хоч трохи лишилося?"))
            mock_household_read.assert_called_once()


# 14. Write command "Купив хліб за 10 zł" never reaches meal_ideas
class TestWriteCommandNeverReachesMealIdeas(MealIdeasWebhookTestCase):
    def test_purchase_with_price_never_reaches_meal_ideas(self):
        chat_id = 850014
        with patch.object(household_router, "_ask_gemini_household_router") as mock_hr, \
                patch.object(bot, "apply_global_household_operations") as mock_apply, \
                patch.object(meal_ideas, "try_handle_meal_ideas") as mock_meal_ideas:
            mock_hr.return_value = {
                "intent": "household_operations",
                "operations": [
                    {"type": "add_inventory", "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
                    {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                     "description": "Хліб", "expense_date": "2026-07-05"},
                ],
                "unresolved_fragments": [],
            }
            _call_webhook(_make_update(850000014, chat_id, "Купив хліб за 10 zł"))
            mock_meal_ideas.assert_not_called()
            mock_apply.assert_not_called()  # read-only preview only, no write before confirm
        self.assertIn(chat_id, bot.pending_global_household)


if __name__ == "__main__":
    unittest.main()
