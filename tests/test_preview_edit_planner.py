"""Pending Preview Edit Planner — the semantic LAST-RESORT fallback tried
only after every deterministic preview-edit handler (quantity/rename
parser, text correction, price clarification) has already failed to
recognize the user's correction text.

V1 fixed the live bug where a genuine Ukrainian case/wording difference
("для сестри" [genitive] in the pending expense description vs "сестрі"
[dative] in the user's correction) defeats deterministic substring
matching entirely, but only ever supported ONE rename patch per message
and had no way to touch an expense's amount at all.

V2 (this file) extends that to a LIST of patches per correction message
(so one message can rename one expense AND fix another expense's amount
together), and adds two new operations: update_expense_amount and
update_expense_context_note — fixing the second live bug, where "Я
згадала, що комод коштував оригінальна ціна 628, а ми купили його за
528." was rejected outright, since V1's own prompt explicitly forbade any
operation from ever touching an amount.

Two layers of coverage, same posture as tests/test_price_clarification.py:
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
    """The EXACT V1 live bug shape: pending expense description uses the
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


def _komod_expense_preview():
    """The EXACT V2 live bug shape: a pending "Комод" expense with an
    explicit original-vs-paid context_note, both of which need their
    numbers updated together by a single correction message."""
    return {
        "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
        "inventory_targets": [],
        "new_expenses": [{
            "amount": Decimal("527.00"), "currency": "PLN", "category": "Меблі",
            "category_was_defaulted": True, "description": "Комод",
            "expense_date": date(2026, 7, 12),
            "context_note": "Оригінальна ціна 627 zł, куплено за 527 zł",
        }],
        "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


def _gift_and_komod_preview():
    """The exact combined-correction live shape: two independent pending
    expenses, "Подарунок сестрі" and "Комод" — a single correction message
    can target both at once with two unrelated patches."""
    return {
        "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
        "inventory_targets": [],
        "new_expenses": [
            {
                "amount": Decimal("60.00"), "currency": "PLN", "category": "Інше",
                "category_was_defaulted": True, "description": "Подарунок сестрі",
                "expense_date": date(2026, 7, 12),
            },
            {
                "amount": Decimal("527.00"), "currency": "PLN", "category": "Меблі",
                "category_was_defaulted": True, "description": "Комод",
                "expense_date": date(2026, 7, 12),
                "context_note": "Оригінальна ціна 627 zł, куплено за 527 zł",
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
        stub = _StubBot(
            '{"patches": [{"operation": "rename_expense_description", "target_id": "exp_1", '
            '"new_value": "Подарунок дочці"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {
            "status": "patches",
            "patches": [{"operation": "rename_expense_description", "list_key": "new_expenses", "index": 0, "new_value": "Подарунок дочці"}],
        })

    def test_amount_and_context_note_patches_together(self):
        stub = _StubBot(
            '{"patches": ['
            '{"operation": "update_expense_amount", "target_id": "exp_1", "new_amount": "528"},'
            '{"operation": "update_expense_context_note", "target_id": "exp_1", '
            '"new_context_note": "Оригінальна ціна 628 zł, куплено за 528 zł"}'
            ']}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(
            _komod_expense_preview(),
            "Я згадала, що комод коштував оригінальна ціна 628, а ми купили його за 528.",
        )
        self.assertEqual(result["status"], "patches")
        self.assertEqual(len(result["patches"]), 2)
        amounts = {p["operation"]: p for p in result["patches"]}
        self.assertEqual(amounts["update_expense_amount"]["new_amount"], Decimal("528.00"))
        self.assertEqual(amounts["update_expense_amount"]["index"], 0)
        self.assertEqual(
            amounts["update_expense_context_note"]["new_context_note"],
            "Оригінальна ціна 628 zł, куплено за 528 zł",
        )

    def test_combined_rename_and_amount_patches_on_different_targets(self):
        stub = _StubBot(
            '{"patches": ['
            '{"operation": "rename_expense_description", "target_id": "exp_1", "new_value": "Подарунок дочці"},'
            '{"operation": "update_expense_amount", "target_id": "exp_2", "new_amount": "528"}'
            ']}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(
            _gift_and_komod_preview(),
            "Подарунок має бути не сестрі, а дочці, і ціна за комод не 527, а 528.",
        )
        self.assertEqual(result["status"], "patches")
        by_op = {p["operation"]: p for p in result["patches"]}
        self.assertEqual(by_op["rename_expense_description"]["index"], 0)
        self.assertEqual(by_op["rename_expense_description"]["new_value"], "Подарунок дочці")
        self.assertEqual(by_op["update_expense_amount"]["index"], 1)
        self.assertEqual(by_op["update_expense_amount"]["new_amount"], Decimal("528.00"))

    def test_amount_not_present_in_text_falls_back_to_clarification(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_expense_amount", "target_id": "exp_1", "new_amount": "999"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_komod_expense_preview(), "зроби комод дешевшим")
        self.assertEqual(result["status"], "ask_clarification")

    def test_non_positive_amount_falls_back_to_clarification(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_expense_amount", "target_id": "exp_1", "new_amount": "0"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_komod_expense_preview(), "постав 0 zł за комод")
        self.assertEqual(result["status"], "ask_clarification")

    def test_one_invalid_patch_discards_the_whole_batch(self):
        # rename is perfectly valid, but the amount patch's new_amount
        # ("999") never appears in the user's text — the ENTIRE batch is
        # discarded (see module docstring's "safer/simpler" choice), so the
        # otherwise-valid rename is never silently half-applied either.
        stub = _StubBot(
            '{"patches": ['
            '{"operation": "rename_expense_description", "target_id": "exp_1", "new_value": "Подарунок дочці"},'
            '{"operation": "update_expense_amount", "target_id": "exp_2", "new_amount": "999"}'
            ']}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(
            _gift_and_komod_preview(), "не сестрі, а дочці, і зроби комод дешевшим",
        )
        self.assertEqual(result["status"], "ask_clarification")

    def test_ask_clarification_passthrough(self):
        stub = _StubBot('{"patches": [{"operation": "ask_clarification", "question": "Яку саме витрату виправити?"}]}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_two_gifts_preview(), "не сестрі, а дочці")
        self.assertEqual(result, {"status": "ask_clarification", "question": "Яку саме витрату виправити?"})

    def test_ask_clarification_wins_even_mixed_with_a_real_patch(self):
        stub = _StubBot(
            '{"patches": ['
            '{"operation": "rename_expense_description", "target_id": "exp_1", "new_value": "Х"},'
            '{"operation": "ask_clarification", "question": "Який саме подарунок?"}'
            ']}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_two_gifts_preview(), "не сестрі, а дочці")
        self.assertEqual(result, {"status": "ask_clarification", "question": "Який саме подарунок?"})

    def test_blank_ask_clarification_question_falls_back(self):
        stub = _StubBot('{"patches": [{"operation": "ask_clarification", "question": "   "}]}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_two_gifts_preview(), "не сестрі, а дочці")
        self.assertEqual(result, {"status": "no_change"})

    def test_out_of_range_target_id_falls_back(self):
        stub = _StubBot('{"patches": [{"operation": "rename_expense_description", "target_id": "exp_5", "new_value": "Х"}]}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"status": "ask_clarification", "question": preview_edit_planner._GENERIC_CLARIFY_QUESTION})

    def test_target_id_wrong_list_falls_back(self):
        # exp_1 is a real id, but naming it in a rename_shopping_item patch
        # (the wrong operation for that list) must never be trusted.
        stub = _StubBot('{"patches": [{"operation": "rename_shopping_item", "target_id": "exp_1", "new_value": "Х"}]}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "щось")
        self.assertEqual(result["status"], "ask_clarification")

    def test_unknown_operation_falls_back(self):
        stub = _StubBot('{"patches": [{"operation": "change_amount", "target_id": "exp_1", "new_value": "999"}]}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "зроби 999 zł")
        self.assertEqual(result, {"status": "no_change"})

    def test_malformed_json_falls_back(self):
        stub = _StubBot('це не json')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"status": "no_change"})

    def test_markdown_fenced_json_is_accepted(self):
        stub = _StubBot('```json\n{"patches": [{"operation": "no_change"}]}\n```')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "щось геть інше")
        self.assertEqual(result, {"status": "no_change"})

    def test_gemini_exception_falls_back(self):
        stub = _StubBot(raise_exc=RuntimeError("network down"))
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"status": "no_change"})

    def test_empty_gemini_response_falls_back(self):
        stub = _StubBot(None)
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "не сестрі, а дочці")
        self.assertEqual(result, {"status": "no_change"})

    def test_blank_user_text_never_calls_gemini(self):
        stub = _StubBot('{"patches": [{"operation": "no_change"}]}')
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_gift_expense_preview_genitive(), "   ")
        self.assertEqual(result, {"status": "no_change"})
        self.assertEqual(stub.calls, 0)

    def test_nothing_patchable_never_calls_gemini(self):
        stub = _StubBot('{"patches": [{"operation": "no_change"}]}')
        preview_edit_planner.configure(stub)
        empty_pending = {
            "add_shopping_items": [], "add_inventory_items": [], "new_expenses": [],
        }
        result = preview_edit_planner.plan_preview_edit(empty_pending, "щось геть інше")
        self.assertEqual(result, {"status": "no_change"})
        self.assertEqual(stub.calls, 0)

    def test_currency_field_from_gemini_is_never_accepted(self):
        # There is no field in the validated patch for currency at all —
        # even if Gemini's raw response includes one, it's simply dropped.
        stub = _StubBot(
            '{"patches": [{"operation": "update_expense_amount", "target_id": "exp_1", '
            '"new_amount": "528", "currency": "EUR"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_komod_expense_preview(), "комод коштував 528")
        self.assertEqual(result["status"], "patches")
        self.assertNotIn("currency", result["patches"][0])


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


# 1 — the exact Komod live bug: amount + context note updated together.
class TestKomodAmountAndNoteCorrection(PreviewEditPlannerWebhookTestCase):
    def test_amount_and_context_note_both_update(self):
        chat_id = 997201
        pending_global_household[chat_id] = _komod_expense_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": ['
            '{"operation": "update_expense_amount", "target_id": "exp_1", "new_amount": "528"},'
            '{"operation": "update_expense_context_note", "target_id": "exp_1", '
            '"new_context_note": "Оригінальна ціна 628 zł, куплено за 528 zł"}'
            ']}'
        )
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(
                997201001, chat_id,
                "Я згадала, що комод коштував оригінальна ціна 628, а ми купили його за 528.",
            ))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("528.00"))
        self.assertEqual(data["new_expenses"][0]["context_note"], "Оригінальна ціна 628 zł, куплено за 528 zł")
        texts = self._sent_texts()
        self.assertTrue(any("528" in t and "Комод" in t for t in texts))


# 2/5 — combined correction in one message applies both patches; repeated
# single-purpose corrections across two messages also both land before
# confirmation.
class TestCombinedAndSequentialCorrections(PreviewEditPlannerWebhookTestCase):
    def test_combined_correction_applies_both_patches(self):
        chat_id = 997202
        pending_global_household[chat_id] = _gift_and_komod_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": ['
            '{"operation": "rename_expense_description", "target_id": "exp_1", "new_value": "Подарунок дочці"},'
            '{"operation": "update_expense_amount", "target_id": "exp_2", "new_amount": "528"}'
            ']}'
        )
        _call_webhook(_make_update(
            997202001, chat_id,
            "Подарунок має бути не сестрі, а дочці, і ціна за комод не 527, а 528.",
        ))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][1]["amount"], Decimal("528.00"))
        self.assertEqual(data["new_expenses"][1]["description"], "Комод")

    def test_sequential_single_purpose_corrections_both_land(self):
        chat_id = 997205
        pending_global_household[chat_id] = _gift_and_komod_preview()

        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "rename_expense_description", "target_id": "exp_1", "new_value": "Подарунок дочці"}]}'
        )
        _call_webhook(_make_update(997205001, chat_id, "не сестрі, а дочці"))

        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "update_expense_amount", "target_id": "exp_2", "new_amount": "528"}]}'
        )
        _call_webhook(_make_update(997205002, chat_id, "комод не 527, а 528"))

        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][1]["amount"], Decimal("528.00"))


# 3 — amount patch names a number never present in the user's text.
class TestAmountNotInTextIsRejected(PreviewEditPlannerWebhookTestCase):
    def test_invented_amount_is_never_applied(self):
        chat_id = 997203
        pending_global_household[chat_id] = _komod_expense_preview()
        original = dict(pending_global_household[chat_id]["new_expenses"][0])
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "update_expense_amount", "target_id": "exp_1", "new_amount": "999"}]}'
        )
        _call_webhook(_make_update(997203001, chat_id, "зроби комод трохи дешевшим"))
        self.assertEqual(pending_global_household[chat_id]["new_expenses"][0], original)


# 4 — amount patch's target itself is ambiguous between two expenses;
# Gemini asks for clarification instead of guessing.
class TestAmbiguousAmountTargetAsksClarification(PreviewEditPlannerWebhookTestCase):
    def test_two_plausible_expenses_ask_which_one(self):
        chat_id = 997204
        pending_global_household[chat_id] = _gift_and_komod_preview()
        original = [dict(ne) for ne in pending_global_household[chat_id]["new_expenses"]]
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "ask_clarification", "question": "У плані дві витрати — яку саме змінити на 528 zł?"}]}'
        )
        _call_webhook(_make_update(997204001, chat_id, "зроби 528 zł"))
        self.assertEqual(pending_global_household[chat_id]["new_expenses"], original)
        self.assertTrue(any("яку саме змінити" in t for t in self._sent_texts()))


# 6/7 — confirm/cancel still behave correctly after AI-planner edits.
class TestConfirmCancelAfterPlannerEdit(PreviewEditPlannerWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def test_confirm_after_planner_edit_writes_corrected_values(self):
        chat_id = 997206
        pending_global_household[chat_id] = _komod_expense_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": ['
            '{"operation": "update_expense_amount", "target_id": "exp_1", "new_amount": "528"},'
            '{"operation": "update_expense_context_note", "target_id": "exp_1", '
            '"new_context_note": "Оригінальна ціна 628 zł, куплено за 528 zł"}'
            ']}'
        )
        _call_webhook(_make_update(
            997206001, chat_id,
            "Я згадала, що комод коштував оригінальна ціна 628, а ми купили його за 528.",
        ))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 0, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook(_make_update(997206002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("528.00"))
        # context_note is presentation-only (see bot.py's confirm handler,
        # which strips it before apply_global_household_operations — it has
        # no column in the DB schema) — the corrected NOTE is only ever
        # visible in the pending preview itself, already asserted above.
        self.assertNotIn("context_note", kwargs["new_expenses"][0])
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_after_planner_edit_writes_nothing(self):
        chat_id = 997207
        pending_global_household[chat_id] = _komod_expense_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "update_expense_amount", "target_id": "exp_1", "new_amount": "528"}]}'
        )
        _call_webhook(_make_update(997207001, chat_id, "комод коштував 528"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(997207002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# 8 — existing V1 rename-only correction still works via the planner.
class TestExistingRenameOnlyCorrectionStillWorks(PreviewEditPlannerWebhookTestCase):
    def test_case_mismatch_rename_still_resolves(self):
        chat_id = 997208
        pending_global_household[chat_id] = _gift_expense_preview_genitive()
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "rename_expense_description", "target_id": "exp_1", "new_value": "Подарунок дочці"}]}'
        )
        _call_webhook(_make_update(997208001, chat_id, "не сестрі, а дочці"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"][0]["description"], "Подарунок дочці")
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("60.00"))


# 9/10 — existing deterministic flows never reach the planner at all.
class TestExistingDeterministicFlowsNeverCallGemini(PreviewEditPlannerWebhookTestCase):
    def test_price_clarification_still_works_without_gemini(self):
        chat_id = 997209
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(997209001, chat_id, "за пів кілограма 5 zl"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))
        self.mock_call_gemini.assert_not_called()

    def test_quantity_edit_still_works_without_gemini(self):
        chat_id = 997210
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(997210001, chat_id, "молока 1 л, а сиру 500 г"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(data["add_shopping_items"][1]["quantity_text"], "500 г")
        self.mock_call_gemini.assert_not_called()


# 11 — generic unrelated text during a pending preview still doesn't mutate
# it (planner safely resolves to no_change/ask_clarification since
# call_gemini's response here isn't configured to look like a real patch).
class TestUnrelatedTextDuringPendingNeverMutates(PreviewEditPlannerWebhookTestCase):
    def test_unrelated_question_never_mutates_preview(self):
        chat_id = 997211
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        original = dict(pending_global_household[chat_id])
        _call_webhook(_make_update(997211001, chat_id, "Яка сьогодні погода?"))
        self.assertEqual(pending_global_household[chat_id], original)
        self.mock_call_gemini.assert_called_once()
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
