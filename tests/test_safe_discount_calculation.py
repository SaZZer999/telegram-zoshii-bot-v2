"""Safe Discount Calculation V1 — a follow-up to Purchase Event Planner V1.

Previously ANY discount/percentage mention anywhere in a purchase message
made the bot refuse to compute an expense at all (safe but too strict — see
household_router._DISCOUNT_MARKER_RE's original blanket behavior). This
adds ONE safe, narrow calculation path: when the message explicitly states
a per-unit price, what unit that price is for, and a discount percent, the
bot may compute the final expense — but only in a preview (never a DB
write before confirm), and only when every piece is literally present in
the text (never trusting Gemini's own arithmetic — see household_router.
_format_discount_calculation_note/discount_expense's own validation).

Covers, at the webhook level (household_router._ask_gemini_household_router
is mocked; no real Gemini/Telegram/Supabase call anywhere here):
A. Safe calculation: explicit unit price + unit + discount + quantity ->
   real expense with an explanation note, no DB write before confirm.
B. Ambiguous: price without a stated basis -> inventory preview only, a
   targeted clarifying question, never a computed expense.
C. Explicit final paid amount ("фінально заплатив 10 zł") -> used directly,
   even with a discount mentioned elsewhere in the same message.
D. Confirm/cancel still work on a calculated-discount preview.
E. Price Clarification V1 (pending-preview follow-up) still works —
   already covered end-to-end in tests/test_price_clarification.py; this
   file only re-confirms the two flows don't interfere with each other.

Plus pure (no webhook) tests directly against household_router for the
math itself (_parse_percent, _format_discount_calculation_note) and the
has_discount_marker/has_final_amount_marker interaction.
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
from bot import pending_global_household  # noqa: E402

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


TEXT_A_SAFE = (
    "Печиво коштувало 20 zł за кілограм, було 50% знижки, я купив пів кілограма і потім ще пів"
)
TEXT_B_AMBIGUOUS = (
    "Печиво коштувало 20 zł, було 50% знижки, я купив пів кілограма і потім ще пів"
)
TEXT_C_FINAL = "Печиво було зі знижкою, фінально заплатив 10 zł"


def _two_half_kilo_cookie_inventory_ops():
    return [
        {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
        {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг", "category": "Солодке та снеки"},
    ]


class SafeDiscountCalculationWebhookTestCase(unittest.TestCase):
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
# A — safe unit-price discount calculation.
# =========================
class TestSafeUnitPriceCalculation(SafeDiscountCalculationWebhookTestCase):
    def test_explicit_unit_price_and_basis_computes_expense(self):
        chat_id = 992001
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(992001001, chat_id, TEXT_A_SAFE))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["quantity_value"], 1)
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))
        self.assertEqual(data["new_expenses"][0]["description"], "Печиво")
        texts = self._sent_texts()
        self.assertTrue(any("10,00 zł" in t for t in texts))
        self.assertTrue(any("20,00 zł/кг" in t and "50" in t and "1 кг" in t for t in texts))

    def test_python_side_recomputes_even_if_gemini_math_disagrees(self):
        # Gemini must never supply a ready-made amount for discount_expense
        # (see the prompt's own "ти НІКОЛИ не вказуєш готову суму тут"
        # instruction) — but even if a rogue/older client somehow attached
        # one, the schema simply doesn't have an "amount" field for this op
        # type, so Python always computes it fresh from unit_price/percent.
        chat_id = 992002
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN", "amount": "999"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(992002001, chat_id, TEXT_A_SAFE))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))


# =========================
# B — ambiguous price (no stated basis) never computes an expense.
# =========================
class TestAmbiguousDiscountAsksClarification(SafeDiscountCalculationWebhookTestCase):
    def test_ambiguous_expense_note_is_the_clarifying_question(self):
        chat_id = 992003
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "ambiguous_expense", "note": "20 zł — це ціна за 1 кг, за 0,5 кг чи фінальна сума?"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(992003001, chat_id, TEXT_B_AMBIGUOUS))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"], [])
        self.assertIsNone(data["new_expense"])
        self.assertEqual(len(data["add_inventory_items"]), 1)
        texts = self._sent_texts()
        self.assertTrue(any("20 zł" in t and "0,5 кг" in t and "фінальна сума" in t for t in texts))

    def test_missing_basis_on_discount_expense_op_falls_back_to_note(self):
        # A malformed discount_expense (Gemini forgot unit_price_basis
        # despite the prompt) never invents a multiplier — it degrades to
        # the same non-blocking note as ambiguous_expense.
        chat_id = 992004
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(992004001, chat_id, TEXT_B_AMBIGUOUS))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"], [])


# =========================
# C — an explicit final paid amount is used directly, discount or not.
# =========================
class TestExplicitFinalAmountOverridesDiscountMarker(SafeDiscountCalculationWebhookTestCase):
    def test_final_amount_verb_bypasses_discount_block(self):
        # Exercised directly against _validate_operations_detailed (not the
        # full webhook): household_router.gate() deliberately doesn't match
        # a bare "заплатив ... zł" with no buy/plan/consume verb (see gate's
        # own docstring — "a plain zł-tagged amount ... stays on the
        # existing narrow expense gates") — that gate-level routing choice
        # is unrelated to what this test is actually verifying, which is
        # has_discount_marker/has_final_amount_marker's interaction INSIDE
        # the validator once household_router IS the one processing a
        # message (e.g. combined with a buy verb elsewhere in a longer
        # message, as TestSafeUnitPriceCalculation's own tests already do).
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": "2026-07-12"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text=TEXT_C_FINAL,
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["new_expenses"]), 1)
        self.assertEqual(payload["new_expenses"][0]["amount"], Decimal("10.00"))

    def test_without_final_amount_verb_literal_add_expense_still_allowed(self):
        # Superseded by Assumption-Based Purchase Preview V1: a plain
        # "20 zł" WITHOUT заплатив/фінально/... is no longer blocked just
        # because a discount marker is present elsewhere in the message —
        # see household_router._DISCOUNT_MARKER_RE's updated docstring.
        # Gemini is now expected to reach for assumed_expense/
        # ambiguous_expense itself when a computation is actually needed
        # (see TestSafeUnitPriceCalculation/TestAmbiguousDiscountAsksClarification
        # above); a bare add_expense with a literal amount is trusted.
        chat_id = 992006
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "add_expense", "amount": "20", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": "2026-07-12"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(992006001, chat_id, TEXT_B_AMBIGUOUS))
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("20.00"))


# =========================
# D — confirm/cancel still work on a calculated-discount preview.
# =========================
class TestConfirmCancelOnCalculatedPreview(SafeDiscountCalculationWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        _database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
        _spec = importlib.util.spec_from_file_location(
            "real_database_for_safe_discount_calc_test", _database_path,
        )
        real_database = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(real_database)
        cls._real_database = real_database
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def test_confirm_writes_inventory_and_calculated_expense(self):
        chat_id = 992007
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(992007001, chat_id, TEXT_A_SAFE))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook(_make_update(992007002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(len(kwargs["add_inventory_items"]), 1)
        self.assertEqual(len(kwargs["new_expenses"]), 1)
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("10.00"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_writes_nothing(self):
        chat_id = 992008
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(992008001, chat_id, TEXT_A_SAFE))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(992008002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# =========================
# E — Price Clarification V1 (pending-preview follow-up) is unaffected: an
# ambiguous-discount preview (example B shape) can still be resolved via a
# "за пів кілограма 5 zl"-style reply.
# =========================
class TestPriceClarificationStillWorksAfterAmbiguousDiscount(SafeDiscountCalculationWebhookTestCase):
    def test_pending_preview_price_clarification_still_computes(self):
        chat_id = 992009
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "ambiguous_expense", "note": "20 zł — це ціна за 1 кг, за 0,5 кг чи фінальна сума?"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(992009001, chat_id, TEXT_B_AMBIGUOUS))
        self.assertEqual(pending_global_household[chat_id]["new_expenses"], [])
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(992009002, chat_id, "за пів кілограма 5 zl"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))


# =========================
# Pure (no webhook) coverage for the math/marker helpers themselves.
# =========================
class TestParsePercent(unittest.TestCase):
    def test_valid_percent(self):
        self.assertEqual(household_router._parse_percent("50"), Decimal("50"))
        self.assertEqual(household_router._parse_percent("12,5"), Decimal("12.5"))

    def test_zero_and_over_100_rejected(self):
        self.assertIsNone(household_router._parse_percent("0"))
        self.assertIsNone(household_router._parse_percent("101"))

    def test_non_numeric_rejected(self):
        self.assertIsNone(household_router._parse_percent("багато"))
        self.assertIsNone(household_router._parse_percent(None))


class TestFinalAmountMarker(unittest.TestCase):
    def test_final_amount_verbs_match(self):
        for text in ("фінально заплатив 10 zł", "оплатив 10 zł", "в результаті вийшло 10 zł", "загалом 10 zł"):
            with self.subTest(text=text):
                self.assertTrue(household_router._FINAL_AMOUNT_MARKER_RE.search(text))

    def test_plain_price_statement_does_not_match(self):
        self.assertFalse(household_router._FINAL_AMOUNT_MARKER_RE.search("коштувало 20 zł, знижка 50%"))


class TestDiscountExpenseDirectValidation(unittest.TestCase):
    def test_ok_kind_carries_expense_calculation_note(self):
        router_result = {
            "intent": "household_operations",
            "operations": _two_half_kilo_cookie_inventory_ops() + [
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text=TEXT_A_SAFE,
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(
            payload["expense_calculation_note"],
            "20,00 zł/кг − 50% = 10,00 zł/кг; 1 кг × 10,00 zł = 10,00 zł",
        )

    def test_multiple_items_makes_discount_expense_ambiguous(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "1 кг", "category": "Солодке та снеки"},
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text="Печиво і молоко, 20 zł за кг з 50% знижкою",
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["new_expenses"], [])
        self.assertTrue(payload["expense_notes"])

    def test_unit_mismatch_never_invents_multiplier(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "3 шт.", "category": "Солодке та снеки"},
                {"type": "discount_expense", "unit_price": "20", "unit_price_basis": "1 кг",
                 "discount_percent": "50", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text="Печиво 3 шт, 20 zł за кг з 50% знижкою",
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["new_expenses"], [])
        self.assertTrue(payload["expense_notes"])


if __name__ == "__main__":
    unittest.main()
