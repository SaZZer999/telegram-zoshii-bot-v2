"""Pending Preview Edit Planner V1 — the semantic LAST-RESORT fallback tried
only after every deterministic preview-edit handler (quantity/rename
parser, text correction, price clarification) has already failed to
recognize the user's correction text. Fixes the live bug where a genuine
Ukrainian case/wording difference ("для сестри" [genitive] in the pending
expense description vs "сестрі" [dative] in the user's correction) defeats
deterministic substring matching entirely, even though a person instantly
sees both refer to the same gift/recipient.

Two layers of coverage, same posture as tests/test_price_clarification.py
and tests/test_text_correction.py:
  - Pure unit tests for preview_edit_planner.plan_preview_edit itself, no
    webhook, no bot.py involved — only preview_edit_planner.configure()'d
    with a tiny stub module.
  - Webhook-level integration tests (no real Gemini/Telegram/Supabase call
    anywhere — every network-facing function is patched per test) proving
    the planner is correctly wired into bot.py's existing preview-edit
    fallback chain, and that it never breaks anything upstream of it.
"""
import sys
import os
import importlib.util
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import preview_edit_planner

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_preview_edit_planner_test", _database_path)
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
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _gift_expense_preview_genitive():
    """The EXACT live bug shape: pending expense description uses the
    genitive "для сестри", while the user's correction below names the
    dative "сестрі" — no substring match exists between them at all, so
    every deterministic handler must fail before the planner ever runs."""
    return {
        "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
        "inventory_targets": [],
        "new_expenses": [{
            "amount": Decimal("60.00"), "currency": "PLN", "category": "Інше",
            "category_was_defaulted": True, "description": "Подарунок для сестри",
            "expense_date": date(2026, 7, 12),
        }],
        "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


def _two_gifts_preview():
    return {
        "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
        "inventory_targets": [],
        "new_expenses": [
            {
                "amount": Decimal("60.00"), "currency": "PLN", "category": "Інше",
                "category_was_defaulted": True, "description": "Подарунок для сестри",
                "expense_date": date(2026, 7, 12),
            },
            {
                "amount": Decimal("40.00"), "currency": "PLN", "category": "Інше",
                "category_was_defaulted": True, "description": "Подарунок для мами",
                "expense_date": date(2026, 7, 12),
            },
        ],
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


# =========================
# Pure unit tests — preview_edit_planner.plan_preview_edit directly, no
# webhook, no bot.py. A tiny stub module stands in for bot.py's call_gemini.
# =========================
class _StubBot:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls = 0

    def call_gemini(self, *args, **kwargs):
        self.calls += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


class PlanPreviewEditUnitTestCase(unittest.TestCase):
    def tearDown(self):
        # Restore the real bot module wiring (bot.py's own configure() call
        # at import time) — resetting to None here would break every OTHER
        # test file's webhook-level tests, which rely on preview_edit_
        # planner._bot still being the live bot module so patch.object(bot,
        # "call_gemini", ...) keeps affecting it.
        preview_edit_planner.configure(bot)

    def test_case_mismatch_renames_expense_description(self):
        stub = _StubBot('{"operation": "rename_expense_description", "index": 0, "new_value": "Подарунок дочці"}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"operation": "rename_expense_description", "index": 0, "new_value": "Подарунок дочці"})

    def test_markdown_fenced_json_is_accepted(self):
        stub = _StubBot('```json\n{"operation": "no_change"}\n```')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "щось геть інше")
        self.assertEqual(result, {"operation": "no_change"})

    def test_ask_clarification_passthrough(self):
        stub = _StubBot('{"operation": "ask_clarification", "question": "Яку саме витрату виправити?"}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_two_gifts_preview(), "не сестрі, а дочці")
        self.assertEqual(result, {"operation": "ask_clarification", "question": "Яку саме витрату виправити?"})

    def test_blank_ask_clarification_question_falls_back(self):
        stub = _StubBot('{"operation": "ask_clarification", "question": "   "}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_two_gifts_preview(), "не сестрі, а дочці")
        self.assertEqual(result, {"operation": "no_change"})

    def test_out_of_range_index_falls_back(self):
        stub = _StubBot('{"operation": "rename_expense_description", "index": 5, "new_value": "Х"}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"operation": "no_change"})

    def test_unknown_operation_falls_back(self):
        stub = _StubBot('{"operation": "change_amount", "index": 0, "new_value": "999"}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "зроби 999 zł")
        self.assertEqual(result, {"operation": "no_change"})

    def test_malformed_json_falls_back(self):
        stub = _StubBot('це не json')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"operation": "no_change"})

    def test_gemini_exception_falls_back(self):
        stub = _StubBot(raise_exc=RuntimeError("network down"))
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"operation": "no_change"})

    def test_empty_gemini_response_falls_back(self):
        stub = _StubBot(None)
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"operation": "no_change"})

    def test_blank_user_text_never_calls_gemini(self):
        stub = _StubBot('{"operation": "no_change"}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "   ")
        self.assertEqual(result, {"operation": "no_change"})
        self.assertEqual(stub.calls, 0)

    def test_no_field_for_amount_change_exists_in_schema(self):
        # There is no operation whose applied patch could ever touch amount/
        # quantity/unit/date/category — an "amount" key on a rename patch is
        # simply ignored by the caller (bot.py only ever reads index/new_value).
        stub = _StubBot('{"operation": "rename_expense_description", "index": 0, "new_value": "Дешевше", "amount": "1.00"}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "зроби дешевше")
        self.assertEqual(result, {"operation": "rename_expense_description", "index": 0, "new_value": "Дешевше"})
        self.assertNotIn("amount", result)


# =========================
# Webhook-level integration tests — proving the planner is wired into
# bot.py's existing preview-edit fallback chain correctly.
# =========================
class PreviewEditPlannerWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_gemini = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# 1 — full live phrase, case mismatch, resolved via the AI planner.
class TestFullLivePhraseResolvesViaPlanner(PreviewEditPlannerWebhookTestCase):
    def test_full_phrase_corrects_gift_recipient_despite_case_mismatch(self):
        chat_id = 997101
        pending_global_household[chat_id] = _gift_expense_preview_genitive()
        self.mock_call_gemini.return_value = (
            '{"operation": "rename_expense_description", "index": 0, "new_value": "Подарунок дочці"}'
        )
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(
                997101001, chat_id, "Там має бути подарунок не сестрі, а подарунок дочці.",
            ))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("60.00"))
        self.mock_call_gemini.assert_called_once()


# 2 — short contrast phrase, same case-mismatch bug, same resolution.
class TestShortPhraseResolvesViaPlanner(PreviewEditPlannerWebhookTestCase):
    def test_short_contrast_phrase_corrects_gift_recipient(self):
        chat_id = 997102
        pending_global_household[chat_id] = _gift_expense_preview_genitive()
        self.mock_call_gemini.return_value = (
            '{"operation": "rename_expense_description", "index": 0, "new_value": "Подарунок дочці"}'
        )
        _call_webhook(_make_update(997102001, chat_id, "не сестрі, а дочці"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("60.00"))


# 3 — two plausible expense targets -> the planner itself asks for
# clarification (distinct from the deterministic substring-ambiguity path,
# which never even fires here since "сестри"/"мами" don't share a fragment).
class TestPlannerAsksClarificationOnAmbiguousTarget(PreviewEditPlannerWebhookTestCase):
    def test_two_plausible_gifts_ask_which_one(self):
        chat_id = 997103
        pending_global_household[chat_id] = _two_gifts_preview()
        original = [dict(ne) for ne in pending_global_household[chat_id]["new_expenses"]]
        self.mock_call_gemini.return_value = (
            '{"operation": "ask_clarification", "question": "У плані два подарунки — який саме виправити?"}'
        )
        _call_webhook(_make_update(997103001, chat_id, "виправ подарунок, там інший отримувач"))
        self.assertEqual(pending_global_household[chat_id]["new_expenses"], original)
        self.assertTrue(any("який саме виправити" in t for t in self._sent_texts()))


# 4 — Gemini tries to smuggle an amount change; the schema has no such
# field, so only the rename actually applies and the amount never moves.
class TestAmountChangeIsNeverApplied(PreviewEditPlannerWebhookTestCase):
    def test_amount_field_in_gemini_response_is_ignored(self):
        chat_id = 997104
        pending_global_household[chat_id] = _gift_expense_preview_genitive()
        self.mock_call_gemini.return_value = (
            '{"operation": "rename_expense_description", "index": 0, '
            '"new_value": "Подарунок дочці", "amount": "999.00"}'
        )
        _call_webhook(_make_update(997104001, chat_id, "не сестрі, а дочці, і зроби 999 zł"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("60.00"))

    def test_unrecognized_operation_name_never_mutates_pending(self):
        chat_id = 997105
        pending_global_household[chat_id] = _gift_expense_preview_genitive()
        original = dict(pending_global_household[chat_id]["new_expenses"][0])
        self.mock_call_gemini.return_value = '{"operation": "change_amount", "new_amount": "999.00"}'
        _call_webhook(_make_update(997105001, chat_id, "зроби 999 zł"))
        self.assertEqual(pending_global_household[chat_id]["new_expenses"][0], original)
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))


# 5/6 — existing deterministic flows never reach the planner at all.
class TestExistingDeterministicFlowsNeverCallGemini(PreviewEditPlannerWebhookTestCase):
    def test_quantity_edit_still_works_without_gemini(self):
        chat_id = 997106
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(997106001, chat_id, "молока 1 л, а сиру 500 г"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(data["add_shopping_items"][1]["quantity_text"], "500 г")
        self.mock_call_gemini.assert_not_called()

    def test_price_clarification_still_works_without_gemini(self):
        chat_id = 997107
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(997107001, chat_id, "за пів кілограма 5 zl"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))
        self.mock_call_gemini.assert_not_called()


# 7/8 — confirm/cancel still behave correctly after an AI-planner edit.
class TestConfirmCancelAfterPlannerEdit(PreviewEditPlannerWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def test_confirm_after_planner_edit_writes_corrected_description(self):
        chat_id = 997108
        pending_global_household[chat_id] = _gift_expense_preview_genitive()
        self.mock_call_gemini.return_value = (
            '{"operation": "rename_expense_description", "index": 0, "new_value": "Подарунок дочці"}'
        )
        _call_webhook(_make_update(997108001, chat_id, "не сестрі, а дочці"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 0, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook(_make_update(997108002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(kwargs["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("60.00"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_after_planner_edit_writes_nothing(self):
        chat_id = 997109
        pending_global_household[chat_id] = _gift_expense_preview_genitive()
        self.mock_call_gemini.return_value = (
            '{"operation": "rename_expense_description", "index": 0, "new_value": "Подарунок дочці"}'
        )
        _call_webhook(_make_update(997109001, chat_id, "не сестрі, а дочці"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(997109002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# 9 — generic unrelated text during a pending preview still gets the guard
# (planner safely resolves to no_change since call_gemini's response here
# isn't configured to look like a real patch).
class TestUnrelatedTextDuringPendingStillGuarded(PreviewEditPlannerWebhookTestCase):
    def test_unrelated_question_still_shows_guard_message(self):
        chat_id = 997110
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(997110001, chat_id, "Яка сьогодні погода?"))
        self.assertEqual(pending_global_household[chat_id], original)
        self.mock_call_gemini.assert_called_once()
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
