"""Pending Preview Text Correction V1 — while an active pending_global_
household preview is awaiting confirm/cancel, a short correction phrase
("не X, а Y", "заміни X на Y", "перейменуй X на Y", "там має бути X не A,
а X B") edits an item name or expense description already sitting in that
SAME preview, instead of being rejected with the generic "У тебе є
незавершений план змін..." guard message — that rejection was the live bug
this fixes.

Webhook-level integration tests (no real Gemini/Telegram/Supabase call
anywhere here — every network-facing function is patched per test), same
posture as tests/test_price_clarification.py. Pure parsing
(preview_editing.parse_text_correction/apply_text_correction/
find_text_correction_targets) is also covered directly, no webhook
involved.
"""
import sys
import os
import importlib.util
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import preview_editing

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_text_correction_test", _database_path)
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
    TEXT_CORRECTION_AMBIGUOUS_MSG,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _gift_expense_preview():
    """A pure expense-only preview (no addable items) — the exact live
    shape: a single "Подарунок сестрі" — 60 zł expense, awaiting confirm."""
    return {
        "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
        "inventory_targets": [],
        "new_expenses": [{
            "amount": Decimal("60.00"), "currency": "PLN", "category": "Інше",
            "category_was_defaulted": True, "description": "Подарунок сестрі",
            "expense_date": date(2026, 7, 12),
        }],
        "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


def _milk_and_cheese_shopping_preview():
    return {
        "add_shopping_items": [
            {
                "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "is_consumable": True,
            },
            {
                "name": "Сир", "canonical_name": "сир", "category": "Молочне та яйця",
                "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "is_consumable": True,
            },
        ],
        "add_inventory_items": [], "consume_changes": [], "inventory_targets": [],
        "new_expenses": [], "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


def _cookie_inventory_only_preview():
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


class TextCorrectionWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        # Pending Preview Edit Planner V1: a zero-match text correction now
        # defers to this Gemini-based fallback instead of being the final
        # word — patched here (unconfigured, like test_price_clarification.py)
        # so it never hits the network; its default MagicMock return value
        # safely fails JSON parsing and resolves to "no_change".
        patcher_gemini = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 1/2 — the exact live phrase and its short form both correct the
# expense description in place, keeping the amount unchanged.
# =========================
class TestTextCorrectionUpdatesExpenseDescription(TextCorrectionWebhookTestCase):
    def test_full_phrase_corrects_gift_recipient(self):
        chat_id = 995001
        pending_global_household[chat_id] = _gift_expense_preview()
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(
                995001001, chat_id, "Там має бути подарунок не сестрі, а подарунок дочці.",
            ))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("60.00"))
        texts = self._sent_texts()
        self.assertTrue(any("Подарунок дочці" in t and "60,00 zł" in t for t in texts))

    def test_short_contrast_phrase_corrects_gift_recipient(self):
        chat_id = 995002
        pending_global_household[chat_id] = _gift_expense_preview()
        _call_webhook(_make_update(995002001, chat_id, "не сестрі, а дочці"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("60.00"))

    def test_zamin_phrasing_also_corrects(self):
        chat_id = 995003
        pending_global_household[chat_id] = _gift_expense_preview()
        _call_webhook(_make_update(995003001, chat_id, "заміни сестрі на дочці"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")

    def test_pereymenuy_phrasing_also_corrects(self):
        chat_id = 995004
        pending_global_household[chat_id] = _gift_expense_preview()
        _call_webhook(_make_update(
            995004001, chat_id, "перейменуй Подарунок сестрі на Подарунок дочці",
        ))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")


# =========================
# 3 — two matching items/expenses -> ask which one, never guess.
# =========================
class TestAmbiguousCorrectionAsksWhichOne(TextCorrectionWebhookTestCase):
    def test_two_expenses_containing_the_same_fragment(self):
        chat_id = 995005
        data = _gift_expense_preview()
        data["new_expenses"].append({
            "amount": Decimal("15.00"), "currency": "PLN", "category": "Інше",
            "category_was_defaulted": True, "description": "Листівка для сестрі",
            "expense_date": data["new_expenses"][0]["expense_date"],
        })
        pending_global_household[chat_id] = data
        original = [dict(ne) for ne in data["new_expenses"]]
        _call_webhook(_make_update(995005001, chat_id, "не сестрі, а дочці"))
        self.assertEqual(pending_global_household[chat_id]["new_expenses"], original)
        self.assertTrue(any(TEXT_CORRECTION_AMBIGUOUS_MSG == t for t in self._sent_texts()))


# =========================
# 4 — no matching phrase at all -> deterministic correction defers to the
# semantic Pending Preview Edit Planner V1 fallback, which also finds
# nothing here (Gemini is mocked/unconfigured) -> the generic guard fires,
# no mutation.
# =========================
class TestNoMatchStaysSafe(TextCorrectionWebhookTestCase):
    def test_no_item_contains_the_old_fragment(self):
        chat_id = 995006
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(995006001, chat_id, "не сестрі, а дочці"))
        self.assertEqual(pending_global_household[chat_id], original)
        self.mock_call_gemini.assert_called_once()
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))


# =========================
# 5/6 — existing quantity-preview-edit and price-clarification flows are
# unaffected by the new correction parser.
# =========================
class TestExistingFlowsUnaffected(TextCorrectionWebhookTestCase):
    def test_quantity_edit_still_works(self):
        chat_id = 995007
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(995007001, chat_id, "молока 1 л, а сиру 500 г"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(data["add_shopping_items"][1]["quantity_text"], "500 г")

    def test_price_clarification_still_works(self):
        chat_id = 995008
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(995008001, chat_id, "за пів кілограма 5 zl"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))


# =========================
# 7/8 — confirm/cancel still work correctly after a text correction.
# =========================
class TestConfirmCancelAfterCorrection(TextCorrectionWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def test_confirm_after_correction_writes_corrected_description(self):
        chat_id = 995009
        pending_global_household[chat_id] = _gift_expense_preview()
        _call_webhook(_make_update(995009001, chat_id, "не сестрі, а дочці"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 0, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook(_make_update(995009002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(kwargs["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("60.00"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_after_correction_writes_nothing(self):
        chat_id = 995010
        pending_global_household[chat_id] = _gift_expense_preview()
        _call_webhook(_make_update(995010001, chat_id, "не сестрі, а дочці"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(995010002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# =========================
# Pure (no webhook) coverage for preview_editing's new parser/helpers.
# =========================
class TestParseTextCorrection(unittest.TestCase):
    def test_full_live_phrase(self):
        result = preview_editing.parse_text_correction(
            "Там має бути подарунок не сестрі, а подарунок дочці.",
        )
        self.assertEqual(result, {"old": "сестрі", "new": "дочці"})

    def test_short_contrast_phrase(self):
        self.assertEqual(preview_editing.parse_text_correction("не сестрі, а дочці"), {"old": "сестрі", "new": "дочці"})

    def test_tse_ne_a_phrase(self):
        result = preview_editing.parse_text_correction("це не сестрі, а дочці")
        self.assertEqual(result["old"], "сестрі")
        self.assertEqual(result["new"], "дочці")

    def test_maie_buty_ne_a_phrase(self):
        result = preview_editing.parse_text_correction("має бути не сестрі, а дочці")
        self.assertEqual(result["old"], "сестрі")
        self.assertEqual(result["new"], "дочці")

    def test_zaminy_na_phrase(self):
        self.assertEqual(
            preview_editing.parse_text_correction("заміни сестрі на дочці"),
            {"old": "сестрі", "new": "дочці"},
        )

    def test_pereymenuy_na_phrase(self):
        self.assertEqual(
            preview_editing.parse_text_correction("перейменуй Подарунок сестрі на Подарунок дочці"),
            {"old": "Подарунок сестрі", "new": "Подарунок дочці"},
        )

    def test_blank_text_returns_none(self):
        self.assertIsNone(preview_editing.parse_text_correction(""))
        self.assertIsNone(preview_editing.parse_text_correction(None))

    def test_unrelated_text_returns_none(self):
        self.assertIsNone(preview_editing.parse_text_correction("Купив молоко і хліб."))

    def test_quantity_edit_phrase_returns_none(self):
        self.assertIsNone(preview_editing.parse_text_correction("молока 1 л, а сиру 500 г"))

    def test_price_clarification_phrase_returns_none(self):
        self.assertIsNone(preview_editing.parse_text_correction("за пів кілограма 5 zl"))


class TestApplyTextCorrection(unittest.TestCase):
    def test_replaces_first_case_insensitive_occurrence_only(self):
        self.assertEqual(
            preview_editing.apply_text_correction("Подарунок сестрі", "сестрі", "дочці"),
            "Подарунок дочці",
        )

    def test_preserves_surrounding_text(self):
        self.assertEqual(
            preview_editing.apply_text_correction("Комод (сірий)", "сірий", "коричневий"),
            "Комод (коричневий)",
        )


class TestFindTextCorrectionTargets(unittest.TestCase):
    def test_single_match(self):
        candidates = [("item", 0, "Автокрісло"), ("expense", 0, "Подарунок сестрі")]
        self.assertEqual(
            preview_editing.find_text_correction_targets("сестрі", candidates),
            [("expense", 0, "Подарунок сестрі")],
        )

    def test_no_match(self):
        candidates = [("item", 0, "Автокрісло")]
        self.assertEqual(preview_editing.find_text_correction_targets("сестрі", candidates), [])

    def test_multiple_matches(self):
        candidates = [("expense", 0, "Подарунок сестрі"), ("expense", 1, "Листівка для сестрі")]
        self.assertEqual(len(preview_editing.find_text_correction_targets("сестрі", candidates)), 2)


if __name__ == "__main__":
    unittest.main()
