"""Price Clarification V1 — a follow-up to Purchase Event Planner V1: while
a pending_global_household preview has an inventory add but no expense yet
(the amount was left ambiguous on purpose — a discount, an original/
per-unit price, or omitted entirely), a short reply like "за пів кілограма
5 zl" or "заплатив 10 zł" must update the SAME preview with a real expense
instead of being rejected with the generic "У тебе є незавершений план
змін..." guard message — that rejection was the live bug this fixes.

Webhook-level integration tests (no real Gemini/Telegram/Supabase call
anywhere here — every network-facing function is patched per test), same
posture as tests/test_global_household_preview_edit.py. Pure parsing/math
(preview_editing.parse_price_clarification / compute_quantity_multiplier)
is also covered directly, no webhook involved.
"""
import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import preview_editing

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_price_clarification_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
    PRICE_CLARIFICATION_AMBIGUOUS_MSG,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _cookie_inventory_only_preview():
    """The exact live shape Purchase Event Planner V1 produces for the
    cookie/discount story: one merged inventory-add item, explicit (not
    inferred) quantity, no expense at all yet."""
    return {
        "add_shopping_items": [],
        "add_inventory_items": [{
            "name": "Печиво", "canonical_name": "печиво", "category": "Солодке та снеки",
            "quantity_value": Decimal("1"), "quantity_unit": "кг", "quantity_text": "1 кг",
            "quantity_inferred": False, "is_consumable": True,
        }],
        "consume_changes": [], "inventory_targets": [],
        "new_expenses": [], "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


class PriceClarificationWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 1/2 — recognized clarifications update the SAME preview with a real
# expense, never blocked by the generic pending-plan guard.
# =========================
class TestPriceClarificationUpdatesPreview(PriceClarificationWebhookTestCase):
    def test_unit_price_clarification_computes_total_and_updates_preview(self):
        chat_id = 991601
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(991601001, chat_id, "за пів кілограма 5 zl"))
        mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))
        self.assertEqual(data["new_expenses"][0]["description"], "Печиво")
        self.assertEqual(len(data["add_inventory_items"]), 1)  # unchanged
        self.assertEqual(data["add_inventory_items"][0]["quantity_text"], "1 кг")
        texts = self._sent_texts()
        self.assertTrue(any("10,00 zł" in t for t in texts))
        self.assertTrue(any("0,5 кг" in t and "2" in t for t in texts))
        self.assertNotIn(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG, texts)

    def test_alternate_unit_price_phrasings_all_compute_the_same_total(self):
        phrasings = ["за 0,5 кг 5 zł", "пів кіло було 5 злотих"]
        for i, text in enumerate(phrasings):
            with self.subTest(text=text):
                chat_id = 991602 + i
                pending_global_household[chat_id] = _cookie_inventory_only_preview()
                _call_webhook(_make_update(991602000 + i * 10 + 1, chat_id, text))
                data = pending_global_household[chat_id]
                self.assertEqual(len(data["new_expenses"]), 1)
                self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))

    def test_total_paid_clarification_uses_amount_directly(self):
        chat_id = 991603
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(991603001, chat_id, "заплатив 10 zł"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))
        texts = self._sent_texts()
        self.assertTrue(any("10,00 zł" in t for t in texts))


# =========================
# 3 — a genuinely ambiguous clarification never invents an expense.
# =========================
class TestAmbiguousClarificationNeverInventsExpense(PriceClarificationWebhookTestCase):
    def test_bare_bulo_with_no_quantity_does_not_add_expense(self):
        # "було 5" has no leading quantity phrase before "було" and isn't a
        # recognized price-clarification shape at all, so it's still
        # offered to the EXISTING Preview Edit V2 item-quantity parser
        # first (unchanged priority — see _handle_global_household_edit_
        # text) — that parser reads it as "set quantity 5 on item «було»",
        # finds no matching item, and asks a targeted clarification
        # (HOUSEHOLD_EDIT_NOT_FOUND_MSG). Either way: no expense is ever
        # invented, and the pending preview is left completely unchanged.
        chat_id = 991604
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(991604001, chat_id, "було 5"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"], original["new_expenses"])
        self.assertEqual(data, original)
        texts = self._sent_texts()
        self.assertTrue(texts)
        self.assertFalse(any("zł" in t for t in texts))

    def test_incompatible_unit_asks_clarification_instead_of_guessing(self):
        chat_id = 991605
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        original_expenses = list(pending_global_household[chat_id]["new_expenses"])
        # Pending item is in "кг" — "за 1 шт 5 zł" has no compatible unit to
        # divide against.
        _call_webhook(_make_update(991605001, chat_id, "за 1 шт 5 zł"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"], original_expenses)
        self.assertTrue(any(PRICE_CLARIFICATION_AMBIGUOUS_MSG == t for t in self._sent_texts()))


# =========================
# 4/5 — confirm/cancel still work correctly on the updated preview.
# =========================
class TestConfirmCancelAfterClarification(PriceClarificationWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def test_confirm_after_clarification_writes_inventory_and_expense(self):
        chat_id = 991606
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        _call_webhook(_make_update(991606001, chat_id, "за пів кілограма 5 zl"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook(_make_update(991606002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(len(kwargs["add_inventory_items"]), 1)
        self.assertEqual(kwargs["add_inventory_items"][0]["quantity_text"], "1 кг")
        self.assertEqual(len(kwargs["new_expenses"]), 1)
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("10.00"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_after_clarification_writes_nothing(self):
        chat_id = 991607
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        _call_webhook(_make_update(991607001, chat_id, "заплатив 10 zł"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(991607002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# =========================
# 7 — generic unrelated text while a preview is pending is still blocked
# with the existing pending-plan message (never general AI, never a new
# command).
# =========================
class TestUnrelatedTextStillBlocked(PriceClarificationWebhookTestCase):
    def test_unrelated_text_still_shows_guard_message(self):
        # Pending Preview Edit Planner V1: once every deterministic preview-
        # edit handler fails, a semantic Gemini fallback now gets one try
        # before the guard message — call_gemini's default (unconfigured)
        # mock return value fails JSON parsing and safely resolves to
        # "no_change", so the guard still fires and nothing is mutated,
        # exactly as before this planner existed.
        chat_id = 991608
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(991608001, chat_id, "Яка сьогодні погода?"))
        self.assertEqual(pending_global_household[chat_id], original)
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))

    def test_new_purchase_command_does_not_start_a_new_router_pass(self):
        chat_id = 991609
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        with patch.object(bot.household_router, "_ask_gemini_household_router") as mock_router:
            _call_webhook(_make_update(991609001, chat_id, "Купив молоко за 5 zł"))
        mock_router.assert_not_called()
        self.assertIn(chat_id, pending_global_household)


# =========================
# Pure (no webhook) coverage for preview_editing's new parser/math.
# =========================
class TestParsePriceClarification(unittest.TestCase):
    def test_za_phrasing_with_word_fraction(self):
        result = preview_editing.parse_price_clarification("за пів кілограма 5 zl")
        self.assertEqual(result["kind"], "unit_price")
        self.assertEqual(result["unit_quantity_value"], Decimal("0.5"))
        self.assertEqual(result["unit_quantity_unit"], "кг")
        self.assertEqual(result["unit_amount"], Decimal("5.00"))

    def test_bulo_phrasing_with_zloty_word(self):
        result = preview_editing.parse_price_clarification("пів кіло було 5 злотих")
        self.assertEqual(result["kind"], "unit_price")
        self.assertEqual(result["unit_amount"], Decimal("5.00"))

    def test_total_paid_verb(self):
        result = preview_editing.parse_price_clarification("заплатив 10 zł")
        self.assertEqual(result, {"kind": "total_paid", "amount": Decimal("10.00")})

    def test_bare_bulo_with_no_quantity_returns_none(self):
        self.assertIsNone(preview_editing.parse_price_clarification("було 5"))

    def test_za_with_no_quantity_returns_none(self):
        self.assertIsNone(preview_editing.parse_price_clarification("за 5 zl"))

    def test_blank_text_returns_none(self):
        self.assertIsNone(preview_editing.parse_price_clarification(""))
        self.assertIsNone(preview_editing.parse_price_clarification(None))


class TestComputeQuantityMultiplier(unittest.TestCase):
    def test_same_unit(self):
        self.assertEqual(
            preview_editing.compute_quantity_multiplier(Decimal("1"), "кг", Decimal("0.5"), "кг"),
            Decimal("2"),
        )

    def test_cross_unit_same_group(self):
        self.assertEqual(
            preview_editing.compute_quantity_multiplier(Decimal("1"), "кг", Decimal("500"), "г"),
            Decimal("2"),
        )

    def test_incompatible_groups_returns_none(self):
        self.assertIsNone(
            preview_editing.compute_quantity_multiplier(Decimal("1"), "кг", Decimal("1"), "шт.")
        )

    def test_missing_pending_value_returns_none(self):
        self.assertIsNone(
            preview_editing.compute_quantity_multiplier(None, "кг", Decimal("0.5"), "кг")
        )

    def test_zero_unit_value_returns_none(self):
        self.assertIsNone(
            preview_editing.compute_quantity_multiplier(Decimal("1"), "кг", Decimal("0"), "кг")
        )


if __name__ == "__main__":
    unittest.main()
