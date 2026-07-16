"""Word-number Quantity + Price V1 — a small, deterministic Ukrainian/
Russian number-word parser (quantities.py: parse_word_quantity/parse_word_
money_amount/normalize_word_number_measurements) plus an operational
fallback guard (bot.py) that keeps a household quantity+price message from
ever falling into general AI-chat with a fabricated "I can't write to the
database" answer.

Live bug: "Тестове молоко один літр за чотири дев'яносто дев'ять злотих"
fell all the way through to general AI-chat — neither the digit-based
quantities.looks_like_money_amount/looks_like_explicit_item_quantity
detection nor bot.py's Quantity + Price Intent Clarification V1 (545113e)
recognize spelled-out numbers at all, both only ever look for an actual
digit.

Fix: quantities.normalize_word_number_measurements rewrites a spelled-out
quantity/money phrase into the SAME digit+unit/digit+currency shape the
existing numeric pipeline already understands, applied ONCE at the very
top of message_dispatcher.dispatch() (DispatcherDeps.normalize_word_
numbers) — before confirm/cancel, navigation, every pending/command route,
AND Phase D (cooking_mode/meal_ideas/household_read/mini_action_planner/
general_ai_fallback) — so every existing route, including the purchase-verb
"Купив X за Y zł" Global Household Router flow, sees ordinary digit text.
Zero new Gemini calls, zero new pending-state shapes, zero new Gemini
prompts. A new operational fallback guard
(_looks_like_unparsed_quantity_price_household_text) is the safety net for
whatever still doesn't fully normalize.

No real Gemini/Telegram/Supabase call happens anywhere in this file —
database is mocked at import time, every Gemini-facing bot.py function is
patched per-test (and asserted NOT called where this feature is supposed to
stay fully deterministic).
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
import quantities  # noqa: E402
import household_router  # noqa: E402
from bot import (  # noqa: E402
    saved_list_context,
    pending_quantity_price_intent,
    pending_global_household,
    pending_delete_batch,
)
# pending_expense is deliberately NOT imported as a bare name: expenses.py
# can be importlib.reload()-ed by other test files in this same process
# (see test_safe_undo_global_action.py), which rebinds bot.pending_expense
# to a brand-new dict object — a bare `from bot import pending_expense`
# captured at THIS module's import time would then silently go stale for
# any test running after that reload. Referenced as `bot.pending_expense`
# everywhere below instead, which always resolves to whatever dict is
# currently live.


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


# =========================
# Pure parser tests — no webhook, no mocks needed beyond the module itself.
# =========================
class TestWordQuantityParsing(unittest.TestCase):
    # 1. "один літр" -> 1 л.
    def test_one_liter(self):
        value, unit, start, end = quantities.parse_word_quantity("один літр")
        self.assertEqual(value, Decimal("1"))
        self.assertEqual(unit, "л")

    # 2. "одна штука" -> 1 шт.
    def test_one_piece(self):
        value, unit, start, end = quantities.parse_word_quantity("одна штука")
        self.assertEqual(value, Decimal("1"))
        self.assertEqual(unit, "шт.")

    # 3. "дві штуки" -> 2 шт.
    def test_two_pieces(self):
        value, unit, start, end = quantities.parse_word_quantity("дві штуки")
        self.assertEqual(value, Decimal("2"))
        self.assertEqual(unit, "шт.")

    # 4. "пів літра" -> 0,5 л.
    def test_half_liter_piv(self):
        value, unit, start, end = quantities.parse_word_quantity("пів літра")
        self.assertEqual(value, Decimal("0.5"))
        self.assertEqual(unit, "л")

    # Extra: "половина літра" -> 0,5 л (explicitly required alternate half word).
    def test_half_liter_polovyna(self):
        value, unit, start, end = quantities.parse_word_quantity("половина літра")
        self.assertEqual(value, Decimal("0.5"))
        self.assertEqual(unit, "л")

    # 5. "п'ятсот грамів" -> 500 г.
    def test_five_hundred_grams(self):
        value, unit, start, end = quantities.parse_word_quantity("п’ятсот грамів")
        self.assertEqual(value, Decimal("500"))
        self.assertEqual(unit, "г")

    # 6. "двісті п'ятдесят грамів" -> 250 г.
    def test_two_hundred_fifty_grams(self):
        value, unit, start, end = quantities.parse_word_quantity("двісті п’ятдесят грамів")
        self.assertEqual(value, Decimal("250"))
        self.assertEqual(unit, "г")

    # 7. "один кілограм" -> 1 кг.
    def test_one_kilogram(self):
        value, unit, start, end = quantities.parse_word_quantity("один кілограм")
        self.assertEqual(value, Decimal("1"))
        self.assertEqual(unit, "кг")

    # Required Russian Whisper form: "один литр" -> 1 л.
    def test_one_liter_russian_spelling(self):
        value, unit, start, end = quantities.parse_word_quantity("один литр")
        self.assertEqual(value, Decimal("1"))
        self.assertEqual(unit, "л")

    # 8. Ordinary question with a number-word but no measurement context ->
    # no false positive.
    def test_no_measurement_context_no_match(self):
        self.assertIsNone(quantities.parse_word_quantity("Скільки коштує один квиток?"))
        self.assertIsNone(quantities.parse_word_quantity("У мене два питання"))

    # Comma right after the matched phrase survives (regression guard for
    # the exact bug this feature's own implementation hit and fixed —
    # inventory.py's "одна штука, бо ..."/"одна штука, воно вже не
    # потрібно" causal-tail parsing depends on that comma staying put).
    def test_trailing_punctuation_not_swallowed(self):
        result = quantities.normalize_word_number_measurements("Видали молоко одна штука, бо воно зіпсувалося")
        self.assertEqual(result, "Видали молоко 1 шт., бо воно зіпсувалося")


class TestWordMoneyParsing(unittest.TestCase):
    # 9. "чотири дев'яносто дев'ять злотих" -> 4,99 zł.
    def test_four_ninety_nine(self):
        amount, start, end = quantities.parse_word_money_amount("чотири дев’яносто дев’ять злотих")
        self.assertEqual(amount, Decimal("4.99"))

    # 10. "дванадцять злотих" -> 12,00 zł.
    def test_twelve_zloty(self):
        amount, start, end = quantities.parse_word_money_amount("дванадцять злотих")
        self.assertEqual(amount, Decimal("12"))

    # 11. "п'ятдесят один злотий двадцять три гроші" -> 51,23 zł.
    def test_fifty_one_twenty_three_grosze(self):
        amount, start, end = quantities.parse_word_money_amount("п’ятдесят один злотий двадцять три гроші")
        self.assertEqual(amount, Decimal("51.23"))

    # Extra required example: "п'ять сорок дев'ять" -> 5,49 zł, but ONLY
    # once a "за" context already established "this is a price"
    # (require_currency_marker=False) — never trusted standalone.
    def test_bare_two_number_price_requires_za_context(self):
        amount, start, end = quantities.parse_word_money_amount("п’ять сорок дев’ять", require_currency_marker=False)
        self.assertEqual(amount, Decimal("5.49"))
        self.assertIsNone(quantities.parse_word_money_amount("п’ять сорок дев’ять"))

    # 12. Incomplete/unclear amount -> safe no-match, never an invented
    # number.
    def test_incomplete_amount_no_match(self):
        self.assertIsNone(quantities.parse_word_money_amount("злотих"))
        self.assertIsNone(quantities.parse_word_money_amount("дуже дорого"))


class TestNormalizeWordNumberMeasurements(unittest.TestCase):
    # "за" separates quantity (before) from price (after); item name is
    # never touched.
    def test_full_phrase_normalizes_with_za_split(self):
        result = quantities.normalize_word_number_measurements(
            "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"
        )
        self.assertEqual(result, "Тестове молоко 1 л за 4,99 zł")

    def test_purchase_verb_phrase_normalizes(self):
        result = quantities.normalize_word_number_measurements(
            "Купив тестовий йогурт п’ятсот грамів за дванадцять злотих"
        )
        self.assertEqual(result, "Купив тестовий йогурт 500 г за 12,00 zł")

    def test_vzialy_phrase_normalizes(self):
        result = quantities.normalize_word_number_measurements(
            "Взяли сир п’ятсот грамів за дванадцять злотих"
        )
        self.assertEqual(result, "Взяли сир 500 г за 12,00 zł")

    # Digit-only text round-trips unchanged.
    def test_numeric_text_unchanged(self):
        self.assertEqual(quantities.normalize_word_number_measurements("Молоко 1 л 4,99 zł"), "Молоко 1 л 4,99 zł")

    # No word-number phrase at all -> unchanged.
    def test_unrelated_text_unchanged(self):
        text = "Чому молоко коштує дорожче?"
        self.assertEqual(quantities.normalize_word_number_measurements(text), text)


# =========================
# Webhook-level end-to-end tests.
# =========================
class WordNumberWebhookTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(saved_list_context.clear)
        self.addCleanup(pending_quantity_price_intent.clear)
        self.addCleanup(pending_global_household.clear)
        self.addCleanup(bot.pending_expense.clear)
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

        patcher_recent_expenses = patch.object(bot, "get_recent_expenses_for_deletion", return_value=[])
        self.mock_recent_expenses = patcher_recent_expenses.start()
        self.addCleanup(patcher_recent_expenses.stop)

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


class TestExactLiveScenario(WordNumberWebhookTestCase):
    # 13. Main menu word-number quantity+price -> existing four-choice
    # clarification.
    def test_main_menu_word_number_triggers_clarification(self):
        chat_id = 776001
        _call_webhook(_make_update(775001001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        self.assertTrue(any("товар" in t and "кількість" in t and "ціну" in t for t in self._sent_texts()))
        self.mock_call_gemini.assert_not_called()

    # 14. Structured state has the exact expected fields.
    def test_structured_state_fields(self):
        chat_id = 776002
        _call_webhook(_make_update(775002001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        data = pending_quantity_price_intent[chat_id]
        self.assertEqual(data["item_name"], "Тестове молоко")
        self.assertEqual(data["quantity_value"], Decimal("1"))
        self.assertEqual(data["quantity_unit"], "л")
        self.assertEqual(data["amount"], Decimal("4.99"))

    # 15. No DB write before a choice is made.
    def test_no_db_write_before_choice(self):
        chat_id = 776003
        _call_webhook(_make_update(775003001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        self.assertFalse(self.mock_apply_global.called)
        self.assertFalse(self.mock_add_expense.called)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, bot.pending_expense)

    # 16. Cancel creates nothing.
    def test_cancel_creates_nothing(self):
        chat_id = 776004
        _call_webhook(_make_update(775004001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        _call_webhook(_make_update(775004002, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, bot.pending_expense)
        self.assertFalse(self.mock_apply_global.called)
        self.assertFalse(self.mock_add_expense.called)

    # 17. Shopping choice creates the existing shopping preview.
    def test_shopping_choice_creates_existing_shopping_preview(self):
        chat_id = 776005
        _call_webhook(_make_update(775005001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        _call_webhook(_make_update(775005002, chat_id, "🛒 Додати до покупок"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_shopping_items"]), 1)
        self.assertEqual(data["add_shopping_items"][0]["name"], "Тестове молоко")
        self.assertEqual(data["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(data["new_expenses"], [])

    # 18. Expense choice creates the existing expense preview.
    def test_expense_choice_creates_existing_expense_preview(self):
        chat_id = 776006
        _call_webhook(_make_update(775006001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        _call_webhook(_make_update(775006002, chat_id, "💸 Записати витрату"))
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["amount"], Decimal("4.99"))
        self.assertNotIn(chat_id, pending_global_household)

    # 19. Already-bought choice creates the existing compound preview.
    def test_already_bought_choice_creates_compound_preview(self):
        chat_id = 776007
        _call_webhook(_make_update(775007001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        _call_webhook(_make_update(775007002, chat_id, "✅ Уже купив"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["name"], "Тестове молоко")
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("4.99"))


class TestExplicitPurchaseWordNumbers(WordNumberWebhookTestCase):
    # 20. "Купив молоко один літр за чотири дев'яносто дев'ять злотих" ->
    # existing purchase flow, no clarification.
    def test_purchase_verb_word_numbers_no_clarification(self):
        chat_id = 776101
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "4.99", "currency": "PLN", "category": "Продукти", '
            '"description": "Молоко", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(775101001, chat_id, "Купив молоко один літр за чотири дев’яносто дев’ять злотих"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)
        self.mock_call_gemini.assert_called_once()

    # 21. "Взяли сир п'ятсот грамів за дванадцять злотих" -> existing
    # purchase flow.
    def test_vzialy_word_numbers_no_clarification(self):
        chat_id = 776102
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Сир", "quantity_text": "500 г", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "12", "currency": "PLN", "category": "Продукти", '
            '"description": "Сир", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(775102001, chat_id, "Взяли сир п’ятсот грамів за дванадцять злотих"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)
        self.mock_call_gemini.assert_called_once()

    # 22. Voice transcript text goes through message_dispatcher.dispatch()
    # exactly like typed text (the SAME entrypoint voice_input.py already
    # forwards its transcript into) — so the SAME normalization applies
    # without any voice-specific code.
    def test_voice_transcript_text_uses_same_normalizer(self):
        chat_id = 776103
        _call_webhook(_make_update(775103001, chat_id, "Тестове молоко один літр за чотири дев’яносто дев’ять злотих"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        data = pending_quantity_price_intent[chat_id]
        self.assertEqual(data["item_name"], "Тестове молоко")


class TestFallbackSafety(WordNumberWebhookTestCase):
    # 23/24/26. Partially-unparseable household quantity+price text never
    # reaches general AI-chat and gets a specific, honest hint instead of
    # any fabricated "can't write to the database" answer.
    def test_partial_word_number_never_reaches_general_ai(self):
        chat_id = 776201
        _call_webhook(_make_update(775201001, chat_id, "Тестове масло п’ятсот грамів чотири дев’яносто дев’ять"))
        self.mock_call_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("не зміг точно розібрати числа" in t for t in texts))
        self.assertTrue(any("цифрами" in t for t in texts))
        self.assertFalse(any("не можу самостійно записати" in t for t in texts))

    # 25. General informational questions about prices/liters are not
    # blocked and still reach the real AI-chat fallback.
    def test_price_information_question_not_blocked(self):
        chat_id = 776202
        self.mock_call_gemini.return_value = "Через інфляцію та курс валют."
        _call_webhook(_make_update(775202001, chat_id, "Чому молоко коштує дорожче?"))
        self.mock_call_gemini.assert_called_once()
        self.assertTrue(any("інфляцію" in t for t in self._sent_texts()))

    def test_liters_information_question_not_blocked(self):
        chat_id = 776203
        self.mock_call_gemini.return_value = "Приблизно 1,5-2 літри на день."
        _call_webhook(_make_update(775203001, chat_id, "Скільки літрів води треба пити?"))
        # Not blocked by the new guard — Gemini is still reached (whether
        # via mini_action_planner's own pre-existing household-like gate
        # first, or general_ai_fallback directly, is unrelated to this
        # feature); the guard's OWN controlled message must never appear.
        self.assertTrue(self.mock_call_gemini.called)
        texts = self._sent_texts()
        self.assertFalse(any("не зміг точно розібрати числа" in t for t in texts))
        self.assertTrue(any("1,5-2 літри" in t for t in texts))

    # 27. Exactly one Gemini call per update, even for the explicit
    # purchase word-number path (the household router's own single call).
    def test_at_most_one_gemini_call_for_purchase_flow(self):
        chat_id = 776204
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "4.99", "currency": "PLN", "category": "Продукти", '
            '"description": "Молоко", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(775204001, chat_id, "Купив молоко один літр за чотири дев’яносто дев’ять злотих"))
        self.assertEqual(self.mock_call_gemini.call_count, 1)


class TestRegressionNumericPathsStillWork(WordNumberWebhookTestCase):
    # 28. Numeric quantity+price still works unchanged.
    def test_numeric_quantity_price_still_triggers_clarification(self):
        chat_id = 776301
        _call_webhook(_make_update(775301001, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        data = pending_quantity_price_intent[chat_id]
        self.assertEqual(data["item_name"], "Молоко")
        self.assertEqual(data["amount"], Decimal("4.99"))

    # 29. Numeric explicit purchase still works unchanged.
    def test_numeric_explicit_purchase_still_works(self):
        chat_id = 776302
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Йогурт", "quantity_text": "500 г", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "7.99", "currency": "PLN", "category": "Продукти", '
            '"description": "Йогурт", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(775302001, chat_id, "Купив тестовий йогурт 500 г за 7,99 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)


if __name__ == "__main__":
    unittest.main()
