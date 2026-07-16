"""Informational Question Guard For Quantity + Price Intent V1.

Live bug: "Чому один літр молока коштує п'ять злотих?" is normalized by
Word-number Quantity + Price V1's own quantities.normalize_word_number_
measurements into "Чому 1 л молока коштує 5,00 zł?" — a plain digit-based
text that then matched BOTH quantities.looks_like_money_amount and
quantities.looks_like_explicit_item_quantity, so Quantity + Price Intent
Clarification V1 (545113e) wrongly created pending_quantity_price_intent
("Бачу товар, кількість і ціну. Що зробити?") for what is really an
informational price question, not a household command.

Fix: a narrow, deterministic, compositional guard
(bot._looks_like_quantity_price_informational_question — a question word/
construction "чому"/"скільки"/"яка"/"чи" together with a price/cost verb
stem "кошту"/"цін"/"варт"/"дорог"/"дешев" anywhere in the text, UNLESS an
explicit operational action verb is also present) is checked in TWO places:

  1. bot._route_quantity_price_clarification — right before it would create
     pending_quantity_price_intent, so the message falls through to the
     rest of the pipeline instead.
  2. bot._looks_like_unparsed_quantity_price_household_text — the operational
     fallback guard general_ai_fallback/mini_action_planner already check
     right before Gemini — without this same exception, a normalized
     informational question still carries both signals and would get the
     static QUANTITY_PRICE_PARSE_FAILURE_MSG instead of a real answer.

Neither word-number normalization, the four clarification choices, DB
schema, executors, nor any existing routing file (household_router.py,
legacy_shopping_flow.py, legacy_inventory_flow.py, message_dispatcher.py)
were changed for this fix — see this file's own test classes for the exact
regressions checked.

No real Gemini/Telegram/Supabase call happens anywhere in this file —
database is mocked at import time, every Gemini-facing bot.py function is
patched per-test (and asserted called/not-called as documented per test).
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


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


# =========================
# Pure guard-function tests — no webhook, no mocks needed beyond the
# module itself.
# =========================
class TestInformationalGuardPureFunction(unittest.TestCase):
    def test_exact_live_scenario_after_normalization(self):
        normalized = quantities.normalize_word_number_measurements(
            "Чому один літр молока коштує п’ять злотих?"
        )
        self.assertEqual(normalized, "Чому 1 л молока коштує 5,00 zł?")
        self.assertTrue(bot._looks_like_quantity_price_informational_question(normalized))

    def test_informational_questions_detected(self):
        for text in [
            "Скільки коштує молоко 1 л?",
            "Яка ціна молока 1 л?",
            "Чому молоко 1 л коштує 5 zł?",
            "Чи дорого 5 zł за літр молока?",
            "Скільки буде коштувати 2 л молока?",
        ]:
            with self.subTest(text=text):
                self.assertTrue(bot._looks_like_quantity_price_informational_question(text))

    def test_operational_text_not_flagged_informational(self):
        for text in [
            "Молоко 1 л 5 zł",
            "Тестове молоко 1 л за 5,00 zł",
            "Запиши молоко 1 л за 5 zł",
            "Додай молоко 1 л до покупок",
            "Купив молоко 1 л за 5 zł",
        ]:
            with self.subTest(text=text):
                self.assertFalse(bot._looks_like_quantity_price_informational_question(text))

    # Explicit action verb always wins, even inside a polite question with
    # "?" and no anchored verb prefix (household_router.gate's own
    # _BOUGHT_RE / _AMBIGUOUS_ADD_PREFIX_RE / _EXPLICIT_EXPENSE_VERB_RE only
    # match an anchored prefix, so this guard's own broader, non-anchored
    # action-verb check is what protects this exact case).
    def test_explicit_action_verb_wins_over_question_mark(self):
        self.assertFalse(
            bot._looks_like_quantity_price_informational_question("Можеш записати молоко 1 л за 5 zł?")
        )

    # A bare informational question with no quantity/price at all (no
    # digits) never matches — the guard is only ever consulted alongside
    # the existing money+quantity detection anyway, but it should still be
    # false-safe on its own.
    def test_plain_question_without_quantity_price_untouched(self):
        self.assertFalse(bot._looks_like_quantity_price_informational_question("Привіт, як справи?"))


# =========================
# Webhook-level end-to-end tests.
# =========================
class InformationalGuardWebhookTestCase(unittest.TestCase):
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


class TestExactLiveScenario(InformationalGuardWebhookTestCase):
    # 1-4. Exact live scenario: normalizes, no pending_quantity_price_intent,
    # no shopping/inventory/expense preview, reaches existing general AI
    # fallback, general AI called exactly once.
    def test_exact_live_scenario_reaches_general_ai_once(self):
        chat_id = 8471001
        self.mock_call_gemini.return_value = "Ціна залежить від виробника та об'єму пакування."
        _call_webhook(_make_update(8472001001, chat_id, "Чому один літр молока коштує п’ять злотих?"))

        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertNotIn(chat_id, bot.pending_expense)
        self.mock_call_gemini.assert_called_once()
        texts = self._sent_texts()
        self.assertTrue(any("Ціна залежить" in t for t in texts))
        self.assertFalse(any("Бачу товар, кількість і ціну" in t for t in texts))
        self.assertFalse(any("не зміг точно розібрати числа" in t for t in texts))


class TestInformationalQuestionsReachGeneralAI(InformationalGuardWebhookTestCase):
    # 5-9. Informational price/cost questions -> general AI, never a
    # clarification.
    def _assert_reaches_general_ai(self, chat_id, update_id, text):
        self.mock_call_gemini.return_value = "Ось відповідь від AI."
        _call_webhook(_make_update(update_id, chat_id, text))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_called_once()
        self.assertTrue(any("Ось відповідь від AI." in t for t in self._sent_texts()))

    def test_skilky_koshtuye(self):
        self._assert_reaches_general_ai(8471101, 8472101001, "Скільки коштує молоко 1 л?")

    def test_yaka_tsina(self):
        self._assert_reaches_general_ai(8471102, 8472102001, "Яка ціна молока 1 л?")

    def test_chomu_koshtuye(self):
        self._assert_reaches_general_ai(8471103, 8472103001, "Чому молоко 1 л коштує 5 zł?")

    def test_chy_dorogo(self):
        self._assert_reaches_general_ai(8471104, 8472104001, "Чи дорого 5 zł за літр молока?")

    def test_skilky_bude_koshtuvaty(self):
        self._assert_reaches_general_ai(8471105, 8472105001, "Скільки буде коштувати 2 л молока?")

    # 10. Same informational question, spelled out as word-numbers,
    # normalized to digits before the guard runs, still reaches general AI.
    def test_word_number_form_reaches_general_ai(self):
        self._assert_reaches_general_ai(
            8471106, 8472106001, "Скільки коштує молоко один літр за п’ять злотих?"
        )


class TestOperationalCommandsNotBlocked(InformationalGuardWebhookTestCase):
    # 11. Numeric quantity+price still triggers the existing four-choice
    # clarification.
    def test_numeric_quantity_price_still_clarifies(self):
        chat_id = 8471201
        _call_webhook(_make_update(8472201001, chat_id, "Молоко 1 л 5 zł"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_not_called()

    # 12. Word-number quantity+price still triggers the existing
    # clarification.
    def test_word_number_quantity_price_still_clarifies(self):
        chat_id = 8471202
        _call_webhook(_make_update(8472202001, chat_id, "Тестове молоко один літр за п’ять злотих"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_not_called()

    # 13. Explicit "Запиши..." expense verb keeps its own existing route.
    def test_explicit_zapysh_verb_expense_route(self):
        chat_id = 8471203
        self.mock_expense_router.return_value = {
            "intent": "create_expense", "amount": "5", "currency": "PLN",
            "category": "Продукти", "description": "Молоко 1 л", "expense_date": "2026-07-16",
            "selected_numbers": [], "unresolved_fragments": [],
        }
        _call_webhook(_make_update(8472203001, chat_id, "Запиши молоко 1 л за 5 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, bot.pending_expense)

    # 14. Explicit "Додай..." shopping route unchanged.
    def test_explicit_dodai_shopping_route(self):
        chat_id = 8471204
        with patch.object(household_router, "_ask_gemini_explicit_add_items") as mock_items:
            mock_items.return_value = {
                "items": [{"name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}],
                "unresolved_fragments": [],
            }
            _call_webhook(_make_update(8472204001, chat_id, "Додай молоко 1 л до покупок"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)

    # 15. Explicit purchase verb keeps the existing compound purchase
    # preview.
    def test_explicit_purchase_verb_compound_preview(self):
        chat_id = 8471205
        self.mock_call_gemini.return_value = (
            '{"intent": "household_operations", "operations": ['
            '{"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}, '
            '{"type": "add_expense", "amount": "5", "currency": "PLN", "category": "Продукти", '
            '"description": "Молоко", "expense_date": "2026-07-16"}'
            '], "unresolved_fragments": []}'
        )
        _call_webhook(_make_update(8472205001, chat_id, "Купив молоко 1 л за 5 zł"))
        self.assertNotIn(chat_id, pending_quantity_price_intent)
        self.assertIn(chat_id, pending_global_household)
        self.mock_call_gemini.assert_called_once()

    # 16/17. A polite question that also carries an explicit operational
    # action verb stays operational — the four-choice clarification, not
    # general AI-chat — even though it ends in "?" and the verb isn't at
    # the very start of the sentence (so the explicit action verb has
    # priority over the "?"/question phrasing).
    def test_polite_question_with_action_verb_stays_operational(self):
        chat_id = 8471206
        _call_webhook(_make_update(8472206001, chat_id, "Можеш записати молоко 1 л за 5 zł?"))
        self.assertIn(chat_id, pending_quantity_price_intent)
        self.mock_call_gemini.assert_not_called()


class TestOperationalNoMatchGuardDoesNotBlockInformational(InformationalGuardWebhookTestCase):
    # 22. The pre-existing operational "couldn't parse numbers" fallback
    # guard (_looks_like_unparsed_quantity_price_household_text, Word-number
    # Quantity + Price V1) must not intercept an informational question that
    # still carries both a quantity and a money signal — it must reach the
    # real general AI-chat answer, not the static QUANTITY_PRICE_PARSE_
    # FAILURE_MSG.
    def test_informational_question_not_caught_by_parse_failure_guard(self):
        chat_id = 8471301
        self.mock_call_gemini.return_value = "Молоко коштує по-різному залежно від магазину."
        _call_webhook(_make_update(8472301001, chat_id, "Чому молоко 1 л коштує 5 zł?"))
        self.mock_call_gemini.assert_called_once()
        texts = self._sent_texts()
        self.assertFalse(any("не зміг точно розібрати числа" in t for t in texts))
        self.assertTrue(any("Молоко коштує по-різному" in t for t in texts))

    # Genuinely unparseable operational text (not a question) still gets
    # the static guard message, unchanged.
    def test_genuinely_unparseable_operational_text_still_guarded(self):
        chat_id = 8471302
        _call_webhook(_make_update(8472302001, chat_id, "Тестове масло п’ятсот грамів чотири дев’яносто дев’ять"))
        self.mock_call_gemini.assert_not_called()
        self.assertTrue(any("не зміг точно розібрати числа" in t for t in self._sent_texts()))


class TestRegressionInformationalWithoutQuantityPrice(InformationalGuardWebhookTestCase):
    # 21. Plain informational questions with no attached quantity/money at
    # all keep working exactly as before this fix.
    def test_plain_informational_question_unaffected(self):
        chat_id = 8471401
        self.mock_call_gemini.return_value = "Через інфляцію та курс валют."
        _call_webhook(_make_update(8472401001, chat_id, "Чому молоко коштує дорожче?"))
        self.mock_call_gemini.assert_called_once()
        self.assertTrue(any("інфляцію" in t for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
