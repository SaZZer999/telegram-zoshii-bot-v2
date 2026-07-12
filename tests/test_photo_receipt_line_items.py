"""Receipt V2 — line items. Extends Photo Receipt Input V1 (see
tests/test_photo_receipt_routing.py, which stays untouched and fully
green) with product line-item extraction: when a receipt photo yields 1+
usable line items, bot._handle_photo_message builds ONE combined
pending_global_household preview (💸 Витрати for the receipt total, when
present, plus 🧊 Запаси for the line items) instead of the single-expense
pending_expense preview V1 always used — reusing the exact same
confirm/cancel/apply_global_household_operations/Pending Preview Edit
Planner machinery a typed mixed household command already has.

A receipt with NO line items at all (candidate.line_items == [], the
default for every ReceiptCandidate built without passing that kwarg) is
completely unaffected by any of this — see test_photo_receipt_routing.py
and test_photo_receipts_module.py, both still fully green with zero
changes.

Two layers of coverage:
  - Pure unit tests for photo_receipts._parse_line_items/_parse_line_item
    (no webhook) — the REAL proof that a discount/deposit/bag line never
    survives into a ReceiptCandidate's line_items at all.
  - Webhook-level integration tests (no real Gemini/Telegram/Supabase call
    anywhere here — every network-facing function is patched per test),
    same posture as tests/test_photo_receipt_routing.py. These build
    line_items via photo_receipts._parse_line_items(raw_gemini_shaped_
    dicts) rather than hand-rolling an already-filtered list, so the
    webhook tests exercise the SAME parsing/filtering code path a real
    receipt would.
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
import photo_receipts  # noqa: E402
from bot import pending_expense, pending_global_household  # noqa: E402


# =========================
# Pure unit tests — photo_receipts._parse_line_items/_parse_line_item, no
# webhook, no bot.py involved.
# =========================
class TestParseLineItems(unittest.TestCase):
    def test_clean_item_parses_with_quantity_and_price(self):
        items = photo_receipts._parse_line_items([
            {"name": "Mleko", "quantity": "2", "unit": "л", "line_price": "8.00"},
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Mleko")
        self.assertEqual(items[0]["quantity_text"], "2 л")
        self.assertEqual(items[0]["line_price"], Decimal("8.00"))

    def test_whole_number_quantity_never_becomes_scientific_notation(self):
        items = photo_receipts._parse_line_items([
            {"name": "Jajka", "quantity": "10", "unit": "шт", "line_price": "12.00"},
        ])
        self.assertEqual(items[0]["quantity_text"], "10 шт")

    def test_discount_line_is_dropped(self):
        items = photo_receipts._parse_line_items([
            {"name": "Mleko", "quantity": "1", "unit": "л", "line_price": "4.00"},
            {"name": "Rabat -10%", "quantity": None, "unit": None, "line_price": "1.00"},
        ])
        names = [i["name"] for i in items]
        self.assertEqual(names, ["Mleko"])

    def test_kaucja_deposit_line_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "Kaucja butelka", "quantity": None, "unit": None, "line_price": "0.50"}])
        self.assertEqual(items, [])

    def test_reklamowka_bag_line_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "Reklamówka", "quantity": "1", "unit": "шт", "line_price": "0.40"}])
        self.assertEqual(items, [])

    def test_vat_summary_line_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "PTU VAT A 23%", "quantity": None, "unit": None, "line_price": None}])
        self.assertEqual(items, [])

    def test_unclear_quantity_or_unit_falls_back_to_blank_quantity_text(self):
        items = photo_receipts._parse_line_items([{"name": "Chleb", "quantity": None, "unit": None, "line_price": "5.00"}])
        self.assertEqual(items[0]["quantity_text"], "")

    def test_unrecognized_unit_falls_back_to_blank_quantity_text(self):
        items = photo_receipts._parse_line_items([{"name": "Chleb", "quantity": "1", "unit": "bochenek", "line_price": "5.00"}])
        self.assertEqual(items[0]["quantity_text"], "")

    def test_blank_name_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "  ", "quantity": "1", "unit": "шт", "line_price": "5.00"}])
        self.assertEqual(items, [])

    def test_non_list_input_returns_empty(self):
        self.assertEqual(photo_receipts._parse_line_items(None), [])
        self.assertEqual(photo_receipts._parse_line_items("not a list"), [])

    def test_non_dict_entry_is_skipped(self):
        items = photo_receipts._parse_line_items(["not a dict", {"name": "Mleko", "quantity": "1", "unit": "л", "line_price": "4.00"}])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Mleko")


class TestDecideReceiptOutcomeWithLineItems(unittest.TestCase):
    def test_total_and_items_gives_ok_with_items(self):
        candidate = photo_receipts.ReceiptCandidate(
            is_receipt=True, amount=Decimal("30.00"),
            line_items=[{"name": "Mleko", "quantity_text": "2 л", "line_price": Decimal("8.00")}],
        )
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "ok_with_items")
        self.assertEqual(payload["amount"], Decimal("30.00"))
        self.assertEqual(len(payload["line_items"]), 1)

    def test_items_without_total_gives_items_only(self):
        candidate = photo_receipts.ReceiptCandidate(
            is_receipt=True, amount=None, confidence="low",
            line_items=[{"name": "Mleko", "quantity_text": "2 л", "line_price": Decimal("8.00")}],
        )
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "items_only")
        self.assertNotIn("amount", payload)
        self.assertEqual(len(payload["line_items"]), 1)

    def test_total_without_items_gives_old_ok_shape_unchanged(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=Decimal("30.00"), line_items=[])
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "ok")
        self.assertEqual(set(payload.keys()), {"amount", "merchant", "expense_date", "category_hint", "confidence", "warnings"})

    def test_neither_total_nor_items_gives_missing_amount(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=None, line_items=[])
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "missing_amount")
        self.assertIsNone(payload)


# =========================
# Webhook-level integration tests.
# =========================
def _make_photo_update(update_id, chat_id, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "photo": [{"file_id": "large_1", "width": 1280, "height": 1280, "file_size": 90000}],
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _receipt_candidate(**overrides):
    fields = {
        "is_receipt": True, "merchant": "Biedronka", "amount": Decimal("30.00"),
        "currency": "PLN", "date": "2026-07-10", "category_hint": "grocery",
        "confidence": "high", "warnings": [], "line_items": [],
    }
    fields.update(overrides)
    return photo_receipts.ReceiptCandidate(**fields)


def _milk_and_eggs_raw_items():
    """Raw Gemini-shaped line items — routed through the REAL
    photo_receipts._parse_line_items in every fixture below, so these
    webhook tests exercise the actual parsing/filtering code, not a
    hand-rolled shortcut."""
    return [
        {"name": "Mleko", "quantity": "2", "unit": "л", "line_price": "8.00"},
        {"name": "Jajka", "quantity": "10", "unit": "шт", "line_price": "12.00"},
    ]


class PhotoLineItemsWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_expense.clear()
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_alias = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias.start()
        self.addCleanup(patcher_alias.stop)
        patcher_inventory = patch.object(bot, "get_inventory_items", return_value=[])
        patcher_inventory.start()
        self.addCleanup(patcher_inventory.stop)
        self._download_patcher = patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/receipt.jpg")
        self._download_patcher.start()
        self.addCleanup(self._download_patcher.stop)
        self._remove_patcher = patch("os.remove")
        self._remove_patcher.start()
        self.addCleanup(self._remove_patcher.stop)

    def tearDown(self):
        pending_expense.clear()
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _send_photo(self, update_id, chat_id, candidate):
        with patch.object(photo_receipts, "extract_receipt_from_image", return_value=candidate):
            _call_webhook(_make_photo_update(update_id, chat_id))


# 1 — total + line items -> combined expense + inventory preview.
class TestCombinedExpenseAndInventoryPreview(PhotoLineItemsWebhookTestCase):
    def test_total_and_line_items_build_combined_preview(self):
        chat_id = 998001
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998001001, chat_id, candidate)

        self.assertNotIn(chat_id, pending_expense)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("30.00"))
        self.assertEqual(data["new_expenses"][0]["description"], "Biedronka")
        names = {item["canonical_name"] for item in data["add_inventory_items"]}
        self.assertIn("молоко", names)
        self.assertIn("jajka", names)
        texts = self._sent_texts()
        self.assertTrue(any("30,00 zł" in t and "🧊" in t and "💸" in t for t in texts))


# 2 — discounts/rabat lines never become inventory (end-to-end, real filter).
class TestDiscountsNeverBecomeInventory(PhotoLineItemsWebhookTestCase):
    def test_discount_and_deposit_lines_are_dropped(self):
        chat_id = 998002
        raw_items = [
            {"name": "Mleko", "quantity": "1", "unit": "л", "line_price": "4.00"},
            {"name": "Rabat -10%", "quantity": None, "unit": None, "line_price": "1.00"},
            {"name": "Kaucja butelka", "quantity": None, "unit": None, "line_price": "0.50"},
        ]
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(raw_items))
        self._send_photo(998002001, chat_id, candidate)

        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        names = [item["name"] for item in data["add_inventory_items"]]
        self.assertEqual(len(names), 1)
        self.assertNotIn("Rabat -10%", names)
        self.assertNotIn("Kaucja butelka", names)


# 3 — unclear quantity gets a safe default (1 шт., flagged as an
# assumption in the preview) rather than being invented or rejected.
class TestUnclearQuantityGetsSafeDefault(PhotoLineItemsWebhookTestCase):
    def test_missing_quantity_defaults_to_one_piece_with_note(self):
        chat_id = 998003
        raw_items = [{"name": "Chleb", "quantity": None, "unit": None, "line_price": "5.00"}]
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(raw_items))
        self._send_photo(998003001, chat_id, candidate)

        data = pending_global_household[chat_id]
        item = data["add_inventory_items"][0]
        self.assertEqual(item["quantity_value"], Decimal("1"))
        self.assertEqual(item["quantity_unit"], "шт.")
        self.assertTrue(item["quantity_inferred"])
        self.assertTrue(any("(припущення)" in t for t in self._sent_texts()))


# 4 — total present but zero usable line items -> old V1 behavior unchanged.
class TestNoLineItemsKeepsOldBehavior(PhotoLineItemsWebhookTestCase):
    def test_total_without_line_items_uses_old_single_expense_flow(self):
        chat_id = 998004
        candidate = _receipt_candidate(line_items=[])
        self._send_photo(998004001, chat_id, candidate)

        self.assertIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertEqual(pending_expense[chat_id]["amount"], Decimal("30.00"))

    def test_only_denylisted_line_items_also_uses_old_single_expense_flow(self):
        # Every raw line item is filtered out (discount/deposit only) —
        # candidate.line_items ends up empty after the real photo_
        # receipts._parse_line_items filtering, same as if Gemini had
        # reported none at all.
        chat_id = 998005
        raw_items = [
            {"name": "Rabat -10%", "quantity": None, "unit": None, "line_price": "1.00"},
            {"name": "Kaucja butelka", "quantity": None, "unit": None, "line_price": "0.50"},
        ]
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(raw_items))
        self._send_photo(998005001, chat_id, candidate)
        self.assertIn(chat_id, pending_expense)


# 5 — line items but unclear total -> inventory-only preview, no invented expense.
class TestUnclearTotalGivesInventoryOnlyPreview(PhotoLineItemsWebhookTestCase):
    def test_missing_total_with_line_items_never_invents_an_expense(self):
        chat_id = 998006
        candidate = _receipt_candidate(
            amount=None, confidence="low",
            line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()),
        )
        self._send_photo(998006001, chat_id, candidate)

        self.assertNotIn(chat_id, pending_expense)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"], [])
        self.assertTrue(len(data["add_inventory_items"]) >= 1)


# 6/7 — confirm writes only after ✅, cancel writes nothing.
class TestConfirmCancelAfterLineItemsPreview(PhotoLineItemsWebhookTestCase):
    def test_confirm_writes_expense_and_inventory(self):
        chat_id = 998007
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998007001, chat_id, candidate)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 2, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook({
                "update_id": 998007002,
                "message": {"chat": {"id": chat_id}, "text": "✅ Так, застосувати", "from": {"id": 555, "first_name": "Тест"}},
            })
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(len(kwargs["add_inventory_items"]), 2)
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("30.00"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_writes_nothing(self):
        chat_id = 998008
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998008001, chat_id, candidate)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook({
                "update_id": 998008002,
                "message": {"chat": {"id": chat_id}, "text": "❌ Скасувати", "from": {"id": 555, "first_name": "Тест"}},
            })
        mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)


# Pending Preview Edit Planner should still be able to edit the resulting
# combined preview where possible (existing deterministic quantity edit).
class TestPreviewEditPlannerStillEditsResult(PhotoLineItemsWebhookTestCase):
    def test_quantity_edit_still_works_on_receipt_preview(self):
        chat_id = 998009
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998009001, chat_id, candidate)
        _call_webhook({
            "update_id": 998009002,
            "message": {"chat": {"id": chat_id}, "text": "молока 1 л", "from": {"id": 555, "first_name": "Тест"}},
        })
        data = pending_global_household[chat_id]
        quantities = {item["canonical_name"]: item["quantity_text"] for item in data["add_inventory_items"]}
        self.assertEqual(quantities.get("молоко"), "1 л")


if __name__ == "__main__":
    unittest.main()
