"""Pending Preview Edit Planner V3 — inventory/shopping QUANTITY
corrections. V1/V2 (see tests/test_preview_edit_planner.py) added rename
and expense-amount/context-note patches; V3 fixes the live bug where a
natural correction combining a rename AND a quantity fix on the SAME
pending inventory row ("Сир Гауда, не 2, а 400 грамів." on a pending
"SER GOUDA — 2 шт." row) got its rename applied but silently dropped the
quantity half — V1/V2's own prompt explicitly forbade ANY operation from
touching a quantity at all, so update_inventory_quantity/
update_shopping_quantity didn't exist yet.

Two layers of coverage, same posture as test_preview_edit_planner.py:
  - Pure unit tests for preview_edit_planner.plan_preview_edit itself.
  - Webhook-level integration tests proving bot.py applies the new
    quantity patches correctly and that nothing else regresses.
"""
import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import preview_edit_planner

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_preview_edit_planner_v3_test", _database_path)
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


def _ser_gouda_inventory_preview():
    """The exact live bug shape: a single pending inventory row, named
    exactly as the receipt printed it (untranslated Polish), with a
    quantity of "2 шт." — the OCR default when quantity was unclear."""
    return {
        "add_shopping_items": [],
        "add_inventory_items": [{
            "name": "SER GOUDA", "canonical_name": "ser gouda", "category": "Молочне та яйця",
            "quantity_value": Decimal("2"), "quantity_unit": "шт.", "quantity_text": "2 шт.",
            "quantity_inferred": True, "is_consumable": True,
        }],
        "consume_changes": [], "inventory_targets": [],
        "new_expenses": [], "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


def _two_cheese_items_preview():
    return {
        "add_shopping_items": [],
        "add_inventory_items": [
            {
                "name": "SER GOUDA", "canonical_name": "ser gouda", "category": "Молочне та яйця",
                "quantity_value": Decimal("2"), "quantity_unit": "шт.", "quantity_text": "2 шт.",
                "quantity_inferred": True, "is_consumable": True,
            },
            {
                "name": "SER EDAMSKI", "canonical_name": "ser edamski", "category": "Молочне та яйця",
                "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "is_consumable": True,
            },
        ],
        "consume_changes": [], "inventory_targets": [],
        "new_expenses": [], "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


def _milk_shopping_preview():
    return {
        "add_shopping_items": [{
            "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
            "quantity_inferred": True, "is_consumable": True,
        }],
        "add_inventory_items": [], "consume_changes": [], "inventory_targets": [],
        "new_expenses": [], "new_expense": None, "delete_expense": None,
        "household_id": 1, "user_db_id": 10, "origin": "global",
    }


def _milk_and_cheese_shopping_preview():
    """Reproduces the EXISTING deterministic quantity-edit fixture — "8"
    from test_preview_edit_planner.py's own regression coverage — so V3
    can independently prove that flow still never reaches Gemini."""
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
# Pure unit tests — preview_edit_planner.plan_preview_edit directly.
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


class PlanPreviewEditQuantityUnitTestCase(unittest.TestCase):
    def tearDown(self):
        preview_edit_planner.configure(bot)

    def test_rename_and_quantity_patches_together(self):
        stub = _StubBot(
            '{"patches": ['
            '{"operation": "rename_inventory_item", "target_id": "inv_1", "new_value": "Сир Гауда"},'
            '{"operation": "update_inventory_quantity", "target_id": "inv_1", "new_quantity": "400", "new_unit": "г"}'
            ']}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_ser_gouda_inventory_preview(), "Сир Гауда, не 2, а 400 грамів.")
        self.assertEqual(result["status"], "patches")
        by_op = {p["operation"]: p for p in result["patches"]}
        self.assertEqual(by_op["rename_inventory_item"]["new_value"], "Сир Гауда")
        self.assertEqual(by_op["update_inventory_quantity"]["new_quantity"], Decimal("400"))
        self.assertEqual(by_op["update_inventory_quantity"]["new_unit"], "г")

    def test_quantity_only_patch(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "400", "new_unit": "г"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_ser_gouda_inventory_preview(), "не 2, а 400 грамів")
        self.assertEqual(result, {
            "status": "patches",
            "patches": [{
                "operation": "update_inventory_quantity", "list_key": "add_inventory_items",
                "index": 0, "new_quantity": Decimal("400"), "new_unit": "г",
            }],
        })

    def test_shopping_quantity_patch(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_shopping_quantity", "target_id": "shop_1", '
            '"new_quantity": "2", "new_unit": "л"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_milk_shopping_preview(), "молока не 1, а 2 літри")
        self.assertEqual(result["status"], "patches")
        self.assertEqual(result["patches"][0]["list_key"], "add_shopping_items")
        self.assertEqual(result["patches"][0]["new_quantity"], Decimal("2"))

    def test_piece_unit_normalized_to_dotted_form(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "3", "new_unit": "шт"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_ser_gouda_inventory_preview(), "не 2, а 3 шт")
        self.assertEqual(result["patches"][0]["new_unit"], "шт.")

    def test_quantity_not_in_text_falls_back_to_clarification(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "999", "new_unit": "г"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_ser_gouda_inventory_preview(), "зроби сир Гауда легшим")
        self.assertEqual(result["status"], "ask_clarification")

    def test_unrecognized_unit_falls_back_to_clarification(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "400", "new_unit": "фунт"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_ser_gouda_inventory_preview(), "не 2, а 400 фунтів")
        self.assertEqual(result["status"], "ask_clarification")

    def test_non_positive_quantity_falls_back_to_clarification(self):
        stub = _StubBot(
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "0", "new_unit": "г"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_ser_gouda_inventory_preview(), "постав 0 г сиру")
        self.assertEqual(result["status"], "ask_clarification")

    def test_quantity_operation_targeting_wrong_list_falls_back(self):
        # inv_1 is a real id, but naming it in an update_shopping_quantity
        # patch (the wrong operation for that list) must never be trusted.
        stub = _StubBot(
            '{"patches": [{"operation": "update_shopping_quantity", "target_id": "inv_1", '
            '"new_quantity": "400", "new_unit": "г"}]}'
        )
        preview_edit_planner.configure(stub)
        result = preview_edit_planner.plan_preview_edit(_ser_gouda_inventory_preview(), "не 2, а 400 г")
        self.assertEqual(result["status"], "ask_clarification")


# =========================
# Webhook-level integration tests.
# =========================
class PreviewEditPlannerQuantityWebhookTestCase(unittest.TestCase):
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


# 4 — full live phrase: rename AND quantity fix together.
class TestRenameAndQuantityFullPhrase(PreviewEditPlannerQuantityWebhookTestCase):
    def test_rename_and_quantity_update_together(self):
        chat_id = 996201
        pending_global_household[chat_id] = _ser_gouda_inventory_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": ['
            '{"operation": "rename_inventory_item", "target_id": "inv_1", "new_value": "Сир Гауда"},'
            '{"operation": "update_inventory_quantity", "target_id": "inv_1", "new_quantity": "400", "new_unit": "г"}'
            ']}'
        )
        _call_webhook(_make_update(996201001, chat_id, "Сир Гауда, не 2, а 400 грамів."))
        data = pending_global_household[chat_id]
        item = data["add_inventory_items"][0]
        self.assertEqual(item["name"], "Сир Гауда")
        self.assertEqual(item["canonical_name"], "сир гауда")
        self.assertEqual(item["quantity_value"], Decimal("400"))
        self.assertEqual(item["quantity_unit"], "г")
        self.assertFalse(item["quantity_inferred"])
        texts = self._sent_texts()
        self.assertTrue(any("Сир Гауда" in t and "400" in t for t in texts))


# 5 — short phrase, quantity-only patch.
class TestQuantityOnlyShortPhrase(PreviewEditPlannerQuantityWebhookTestCase):
    def test_short_phrase_updates_quantity_only(self):
        chat_id = 996202
        pending_global_household[chat_id] = _ser_gouda_inventory_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "400", "new_unit": "г"}]}'
        )
        _call_webhook(_make_update(996202001, chat_id, "не 2, а 400 грамів"))
        item = pending_global_household[chat_id]["add_inventory_items"][0]
        self.assertEqual(item["name"], "SER GOUDA")
        self.assertEqual(item["quantity_value"], Decimal("400"))
        self.assertEqual(item["quantity_unit"], "г")


# 6 — two plausible targets -> ask clarification, no mutation.
class TestAmbiguousQuantityTargetAsksClarification(PreviewEditPlannerQuantityWebhookTestCase):
    def test_two_cheese_items_ask_which_one(self):
        chat_id = 996203
        pending_global_household[chat_id] = _two_cheese_items_preview()
        original = [dict(item) for item in pending_global_household[chat_id]["add_inventory_items"]]
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "ask_clarification", '
            '"question": "У плані два сирні товари — який саме на 400 г?"}]}'
        )
        _call_webhook(_make_update(996203001, chat_id, "Сир Гауда, не 2, а 400 грамів."))
        self.assertEqual(pending_global_household[chat_id]["add_inventory_items"], original)
        self.assertTrue(any("який саме" in t for t in self._sent_texts()))


# 7 — quantity patch names a number never present in the user's text.
class TestQuantityNotInTextIsRejected(PreviewEditPlannerQuantityWebhookTestCase):
    def test_invented_quantity_is_never_applied(self):
        chat_id = 996204
        pending_global_household[chat_id] = _ser_gouda_inventory_preview()
        original = dict(pending_global_household[chat_id]["add_inventory_items"][0])
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "999", "new_unit": "г"}]}'
        )
        _call_webhook(_make_update(996204001, chat_id, "зроби сир Гауда легшим"))
        self.assertEqual(pending_global_household[chat_id]["add_inventory_items"][0], original)


# 8 — existing preview edits still pass unaffected (expense rename/amount,
# price clarification, deterministic quantity edit).
class TestExistingPreviewEditsUnaffected(PreviewEditPlannerQuantityWebhookTestCase):
    def test_deterministic_quantity_edit_still_works_without_gemini(self):
        chat_id = 996205
        pending_global_household[chat_id] = _milk_and_cheese_shopping_preview()
        _call_webhook(_make_update(996205001, chat_id, "молока 1 л, а сиру 500 г"))
        data = pending_global_household[chat_id]
        self.assertEqual(data["add_shopping_items"][0]["quantity_text"], "1 л")
        self.assertEqual(data["add_shopping_items"][1]["quantity_text"], "500 г")
        self.mock_call_gemini.assert_not_called()

    def test_price_clarification_still_works_without_gemini(self):
        chat_id = 996206
        pending_global_household[chat_id] = _cookie_inventory_only_preview()
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(996206001, chat_id, "за пів кілограма 5 zl"))
        mock_apply.assert_not_called()
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("10.00"))
        self.mock_call_gemini.assert_not_called()


# 9/10 — confirm/cancel after a quantity-corrected preview.
class TestConfirmCancelAfterQuantityEdit(PreviewEditPlannerQuantityWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def test_confirm_writes_corrected_quantity(self):
        chat_id = 996207
        pending_global_household[chat_id] = _ser_gouda_inventory_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": ['
            '{"operation": "rename_inventory_item", "target_id": "inv_1", "new_value": "Сир Гауда"},'
            '{"operation": "update_inventory_quantity", "target_id": "inv_1", "new_quantity": "400", "new_unit": "г"}'
            ']}'
        )
        _call_webhook(_make_update(996207001, chat_id, "Сир Гауда, не 2, а 400 грамів."))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": None, "expense_deleted": False,
            }
            _call_webhook(_make_update(996207002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(kwargs["add_inventory_items"][0]["name"], "Сир Гауда")
        self.assertEqual(kwargs["add_inventory_items"][0]["quantity_value"], Decimal("400"))
        self.assertEqual(kwargs["add_inventory_items"][0]["quantity_unit"], "г")
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_writes_nothing(self):
        chat_id = 996208
        pending_global_household[chat_id] = _ser_gouda_inventory_preview()
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "400", "new_unit": "г"}]}'
        )
        _call_webhook(_make_update(996208001, chat_id, "не 2, а 400 грамів"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(996208002, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# Live bug: a stale Inventory Representation Guard warning ("Нове
# надходження: 2 шт.") must never survive a quantity edit that changes the
# outcome (here: "separate" -> "merge", since 400 г + 130 г are the same
# mergeable unit) — see bot._refresh_inventory_representation_warnings.
class TestStaleRepresentationWarningRefreshedAfterQuantityEdit(PreviewEditPlannerQuantityWebhookTestCase):
    def _pending_with_stale_separate_warning(self):
        data = _ser_gouda_inventory_preview()
        item = data["add_inventory_items"][0]
        item["_representation_outcome"] = "separate"
        item["_representation_note"] = (
            "⚠️ Сир Гауда вже є у запасах: 400 г.\n"
            "Нове надходження: 2 шт.\n"
            "Його буде збережено окремою позицією, без об'єднання."
        )
        return data

    def _existing_cheese_row(self):
        return [{
            "id": 501, "name": "Сир Гауда", "canonical_name": "сир гауда",
            "category": "Молочне та яйця",
            "quantity_value": Decimal("400"), "quantity_unit": "г", "quantity_text": "400 г",
        }]

    def test_stale_2_pieces_warning_disappears_after_edit_to_grams(self):
        chat_id = 996209
        pending_global_household[chat_id] = self._pending_with_stale_separate_warning()
        self.mock_call_gemini.return_value = (
            '{"patches": ['
            '{"operation": "rename_inventory_item", "target_id": "inv_1", "new_value": "Сир Гауда"},'
            '{"operation": "update_inventory_quantity", "target_id": "inv_1", "new_quantity": "130", "new_unit": "г"}'
            ']}'
        )
        with patch.object(bot, "get_inventory_items", return_value=self._existing_cheese_row()):
            _call_webhook(_make_update(996209001, chat_id, "Сир Гауда, не 2, а 130 грамів."))

        texts = self._sent_texts()
        self.assertTrue(texts)
        last_text = texts[-1]
        self.assertNotIn("2 шт", last_text)
        self.assertNotIn("Нове надходження", last_text)

        item = pending_global_household[chat_id]["add_inventory_items"][0]
        self.assertEqual(item.get("_representation_outcome"), "merge")
        self.assertNotIn("2 шт", item["_representation_note"])
        self.assertIn("130 г", item["_representation_note"])

    def test_confirm_after_stale_warning_refresh_writes_130g_not_2_pieces(self):
        chat_id = 996210
        pending_global_household[chat_id] = self._pending_with_stale_separate_warning()
        self.mock_call_gemini.return_value = (
            '{"patches": [{"operation": "update_inventory_quantity", "target_id": "inv_1", '
            '"new_quantity": "130", "new_unit": "г"}]}'
        )
        with patch.object(bot, "get_inventory_items", return_value=self._existing_cheese_row()):
            _call_webhook(_make_update(996210001, chat_id, "не 2, а 130 грамів."))
            with patch.object(bot, "apply_global_household_operations") as mock_apply:
                mock_apply.return_value = {
                    "shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
                    "inventory_removed": 0, "expense_added_id": None, "expense_deleted": False,
                }
                _call_webhook(_make_update(996210002, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(kwargs["add_inventory_items"][0]["quantity_value"], Decimal("130"))
        self.assertEqual(kwargs["add_inventory_items"][0]["quantity_unit"], "г")


if __name__ == "__main__":
    unittest.main()
