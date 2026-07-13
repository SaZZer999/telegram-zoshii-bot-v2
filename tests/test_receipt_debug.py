"""Receipt Debug/Explain V1 — a short, user-facing Ukrainian explanation of
what the receipt parser saw for every raw Gemini line_items row and why
each ended up kept or dropped, shown ONLY on an explicit request ("покажи
розбір чеку" / "чому так?" / "чому сир 2 штуки?" / "debug чек") during (or
without) an active receipt-built pending_global_household preview.

Two layers of coverage:
  - Pure unit tests for photo_receipts.format_receipt_debug_summary and
    _parse_line_items_with_debug (no webhook).
  - Webhook-level integration tests proving bot.py stores the debug
    summary on the pending preview and answers the trigger phrases
    correctly in every state (active receipt preview / active non-receipt
    preview / no active preview), without breaking confirm/cancel.
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
# Pure unit tests — no webhook, no bot.py involved.
# =========================
class TestParseLineItemsWithDebug(unittest.TestCase):
    def test_kept_and_dropped_rows_both_get_debug_entries(self):
        raw_items = [
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "9.98"},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": None},
            {"name": "Rabat -10%", "quantity": None, "unit": None, "line_price": "1.00"},
        ]
        items, debug_rows = photo_receipts._parse_line_items_with_debug(raw_items)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(debug_rows), 3)

        kept_row, dropped_dup_row, dropped_discount_row = debug_rows
        self.assertEqual(kept_row["raw_name"], "SER GOUDA")
        self.assertEqual(kept_row["normalized_name"], "Сир Гауда")
        self.assertEqual(kept_row["line_price"], Decimal("9.98"))
        self.assertTrue(kept_row["kept"])
        self.assertIsNotNone(kept_row["dedupe_reason"])

        self.assertFalse(dropped_dup_row["kept"])
        self.assertIsNotNone(dropped_dup_row["drop_reason"])
        self.assertIsNotNone(dropped_dup_row["dedupe_reason"])

        self.assertFalse(dropped_discount_row["kept"])
        self.assertIsNotNone(dropped_discount_row["drop_reason"])

    def test_non_list_input_returns_empty_debug_too(self):
        items, debug_rows = photo_receipts._parse_line_items_with_debug(None)
        self.assertEqual(items, [])
        self.assertEqual(debug_rows, [])

    def test_plain_parse_line_items_unaffected_by_debug_plumbing(self):
        # _parse_line_items (used everywhere else) must keep returning just
        # the items list, unchanged, never a tuple.
        items = photo_receipts._parse_line_items([
            {"name": "Mleko", "quantity": "2", "unit": "л", "line_price": "8.00"},
        ])
        self.assertEqual(items, [{"name": "Молоко", "quantity_text": "2 л", "line_price": Decimal("8.00")}])


class TestFormatReceiptDebugSummary(unittest.TestCase):
    def test_empty_debug_rows_gives_no_data_message(self):
        self.assertEqual(photo_receipts.format_receipt_debug_summary([]), photo_receipts.NO_RECEIPT_DEBUG_DATA_MSG)
        self.assertEqual(photo_receipts.format_receipt_debug_summary(None), photo_receipts.NO_RECEIPT_DEBUG_DATA_MSG)

    def test_summary_includes_raw_normalized_quantity_and_status(self):
        _, debug_rows = photo_receipts._parse_line_items_with_debug([
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "9.98"},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": None},
        ])
        summary = photo_receipts.format_receipt_debug_summary(debug_rows)
        self.assertIn("SER GOUDA", summary)
        self.assertIn("Сир Гауда", summary)
        self.assertIn("✅ додано", summary)
        self.assertIn("❌ відкинуто", summary)
        # Never a raw image byte/secret — just names/prices/reasons.
        self.assertNotIn("GEMINI_API_KEY", summary)


# =========================
# Webhook-level integration tests.
# =========================
def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


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
        "is_receipt": True, "merchant": "Żabka", "amount": Decimal("27.28"),
        "currency": "PLN", "date": "2026-07-10", "category_hint": "grocery",
        "confidence": "high", "warnings": [], "line_items": [],
    }
    fields.update(overrides)
    return photo_receipts.ReceiptCandidate(**fields)


def _cheese_receipt_raw_items():
    return [
        {"name": "OLEJ BARTEK", "quantity": "1", "unit": "л", "line_price": "6.50"},
        {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "9.98"},
        {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "-2.00"},
        {"name": "CZOSNEK", "quantity": "1", "unit": "шт", "line_price": "1.80"},
    ]


class ReceiptDebugWebhookTestCase(unittest.TestCase):
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

    def _send_receipt_photo(self, update_id, chat_id, raw_items=None, **candidate_overrides):
        line_items, line_item_debug = photo_receipts._parse_line_items_with_debug(
            raw_items if raw_items is not None else _cheese_receipt_raw_items()
        )
        candidate = _receipt_candidate(line_items=line_items, line_item_debug=line_item_debug, **candidate_overrides)
        with patch.object(photo_receipts, "extract_receipt_from_image", return_value=candidate):
            _call_webhook(_make_photo_update(update_id, chat_id))


# 1 — receipt with kept and dropped line items stores debug summary.
class TestReceiptDebugStoredOnPending(ReceiptDebugWebhookTestCase):
    def test_debug_summary_stored_in_pending_state(self):
        chat_id = 999101
        self._send_receipt_photo(999101001, chat_id)
        data = pending_global_household[chat_id]
        self.assertIn("receipt_debug", data)
        debug_rows = data["receipt_debug"]
        self.assertEqual(len(debug_rows), 4)
        kept = [r for r in debug_rows if r["kept"]]
        dropped = [r for r in debug_rows if not r["kept"]]
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(dropped), 1)
        self.assertIsNotNone(dropped[0]["drop_reason"])


# 2/3 — the debug command returns readable info with the required fields.
class TestReceiptDebugCommandDuringActivePreview(ReceiptDebugWebhookTestCase):
    def test_show_receipt_breakdown_phrase_returns_debug_info(self):
        chat_id = 999102
        self._send_receipt_photo(999102001, chat_id)
        _call_webhook(_make_update(999102002, chat_id, "покажи розбір чеку"))
        texts = self._sent_texts()
        debug_text = texts[-1]
        self.assertIn("SER GOUDA", debug_text)
        self.assertIn("Сир Гауда", debug_text)
        self.assertIn("✅ додано", debug_text)
        self.assertIn("❌ відкинуто", debug_text)

    def test_why_so_phrase_returns_debug_info(self):
        chat_id = 999103
        self._send_receipt_photo(999103001, chat_id)
        _call_webhook(_make_update(999103002, chat_id, "чому так?"))
        self.assertIn("Розбір чека", self._sent_texts()[-1])

    def test_why_cheese_two_pieces_phrase_returns_debug_info(self):
        chat_id = 999104
        self._send_receipt_photo(999104001, chat_id)
        _call_webhook(_make_update(999104002, chat_id, "чому сир 2 штуки?"))
        self.assertIn("Розбір чека", self._sent_texts()[-1])

    def test_debug_word_phrase_returns_debug_info(self):
        chat_id = 999105
        self._send_receipt_photo(999105001, chat_id)
        _call_webhook(_make_update(999105002, chat_id, "debug чек"))
        self.assertIn("Розбір чека", self._sent_texts()[-1])


# 4 — debug command during a non-receipt preview.
class TestReceiptDebugCommandDuringNonReceiptPreview(ReceiptDebugWebhookTestCase):
    def test_says_no_receipt_debug_for_typed_preview(self):
        chat_id = 999106
        pending_global_household[chat_id] = {
            "add_shopping_items": [{
                "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "is_consumable": True,
            }],
            "add_inventory_items": [], "consume_changes": [], "inventory_targets": [],
            "new_expenses": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        _call_webhook(_make_update(999106001, chat_id, "покажи розбір чеку"))
        self.assertEqual(self._sent_texts()[-1], bot.NO_RECEIPT_DEBUG_FOR_PREVIEW_MSG)


# 5 — debug command with no active pending preview at all.
class TestReceiptDebugCommandWithoutActivePreview(ReceiptDebugWebhookTestCase):
    def test_says_no_active_preview(self):
        chat_id = 999107
        _call_webhook(_make_update(999107001, chat_id, "чому так?"))
        self.assertEqual(self._sent_texts()[-1], bot.NO_RECEIPT_DEBUG_PENDING_MSG)


# 6 — confirm/cancel still work after a debug command.
class TestConfirmCancelStillWorkAfterDebugCommand(ReceiptDebugWebhookTestCase):
    def test_confirm_after_debug_command_still_writes(self):
        chat_id = 999108
        self._send_receipt_photo(999108001, chat_id)
        _call_webhook(_make_update(999108002, chat_id, "покажи розбір чеку"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 3, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook(_make_update(999108003, chat_id, "✅ Так, застосувати"))
        mock_apply.assert_called_once()
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_after_debug_command_writes_nothing(self):
        chat_id = 999109
        self._send_receipt_photo(999109001, chat_id)
        _call_webhook(_make_update(999109002, chat_id, "чому так?"))
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(999109003, chat_id, "❌ Скасувати"))
        mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)


if __name__ == "__main__":
    unittest.main()
