"""Photo Receipt Input V1 — webhook-level integration tests: a Telegram
photo/image-document is downloaded, sent to Gemini Vision (photo_receipts.
py), and — on a genuine receipt with a usable amount — turned into the
EXACT SAME pending_expense preview a typed "Biedronka 86,40 zł" command
would create (bot.webhook() -> bot._handle_photo_message ->
expenses.build_receipt_expense_preview). Unlike voice, a photo never
reaches message_dispatcher.dispatch(...) at all.

No real Gemini call and no real Telegram file download happens anywhere in
this file — bot._download_telegram_photo_to_temp and photo_receipts.
extract_receipt_from_image are always mocked/faked. See
tests/test_photo_receipts_module.py for photo_receipts.py's own pure unit
tests (no Flask/Telegram/webhook involved there at all).
"""
import sys
import os
import tempfile
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
import voice_input  # noqa: E402
from bot import (  # noqa: E402
    pending_expense,
    pending_inventory_transform,
    pending_global_household,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
)


def _make_photo_update(update_id, chat_id, sizes=None, user_id=555):
    if sizes is None:
        sizes = [
            {"file_id": "small_1", "width": 90, "height": 90, "file_size": 1000},
            {"file_id": "large_1", "width": 1280, "height": 1280, "file_size": 90000},
        ]
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "photo": sizes, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _make_document_update(update_id, chat_id, file_id="doc_1", mime_type="image/png", user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "document": {"file_id": file_id, "mime_type": mime_type, "file_size": 5000},
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _receipt_candidate(**overrides):
    fields = {
        "is_receipt": True, "merchant": "Biedronka", "amount": Decimal("86.40"),
        "currency": "PLN", "date": "2026-07-10", "category_hint": "grocery",
        "confidence": "high", "warnings": [],
    }
    fields.update(overrides)
    return photo_receipts.ReceiptCandidate(**fields)


class PhotoWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_expense.clear()
        pending_inventory_transform.clear()
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_expense.clear()
        pending_inventory_transform.clear()
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 2. Basic routing: largest photo size chosen, downloaded, temp cleaned up.
# =========================
class TestPhotoRoutingBasics(PhotoWebhookTestCase):
    def test_largest_photo_size_is_chosen_for_download(self):
        chat_id = 881001
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/fake.jpg") as mock_download:
            with patch.object(photo_receipts, "extract_receipt_from_image", return_value=_receipt_candidate()):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881001001, chat_id))
        mock_download.assert_called_once_with("large_1", None)

    def test_temp_file_cleaned_up_after_success(self):
        chat_id = 881002
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/fake2.jpg"):
            with patch.object(photo_receipts, "extract_receipt_from_image", return_value=_receipt_candidate()):
                with patch("os.remove") as mock_remove:
                    _call_webhook(_make_photo_update(881002001, chat_id))
        mock_remove.assert_called_once_with("/tmp/fake2.jpg")

    def test_temp_file_cleaned_up_on_extraction_failure(self):
        chat_id = 881003
        fd, temp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value=temp_path):
            with patch.object(
                photo_receipts, "extract_receipt_from_image",
                side_effect=photo_receipts.PhotoInputError(photo_receipts.MALFORMED_MSG),
            ):
                _call_webhook(_make_photo_update(881003001, chat_id))
        self.assertFalse(os.path.exists(temp_path))
        self.assertEqual(self._sent_texts(), [photo_receipts.MALFORMED_MSG])

    # image document (mime_type image/png) is also accepted.
    def test_image_document_is_accepted(self):
        chat_id = 881004
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/fake3.png") as mock_download:
            with patch.object(photo_receipts, "extract_receipt_from_image", return_value=_receipt_candidate()):
                with patch("os.remove"):
                    _call_webhook(_make_document_update(881004001, chat_id))
        mock_download.assert_called_once_with("doc_1", "image/png")

    def test_pdf_document_is_ignored(self):
        chat_id = 881005
        with patch.object(bot, "_download_telegram_photo_to_temp") as mock_download:
            _call_webhook(_make_document_update(881005001, chat_id, mime_type="application/pdf"))
        mock_download.assert_not_called()


# =========================
# temp file suffix handling (mirrors voice's own suffix tests).
# =========================
class TestPhotoSuffixNormalization(unittest.TestCase):
    def test_jpg_extension_preserved(self):
        self.assertEqual(bot._normalize_photo_suffix("photos/file_0.jpg"), ".jpg")

    def test_png_extension_preserved(self):
        self.assertEqual(bot._normalize_photo_suffix("photos/file_0.png"), ".png")

    def test_missing_extension_defaults_to_jpg(self):
        self.assertEqual(bot._normalize_photo_suffix("photos/file_0"), ".jpg")

    def test_missing_extension_uses_mime_type_hint(self):
        self.assertEqual(bot._normalize_photo_suffix("photos/file_0", mime_type="image/png"), ".png")


class TestDownloadTelegramPhotoToTemp(unittest.TestCase):
    def _fake_get_file_response(self, file_path):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"ok": True, "result": {"file_path": file_path}}
        return resp

    def _fake_file_response(self, content):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.content = content
        return resp

    def test_downloads_and_writes_bytes_with_preserved_suffix(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[self._fake_get_file_response("photos/file_0.jpg"), self._fake_file_response(b"jpeg-bytes")],
        ):
            temp_path = bot._download_telegram_photo_to_temp("abc123")
        try:
            self.assertTrue(temp_path.endswith(".jpg"))
            with open(temp_path, "rb") as f:
                self.assertEqual(f.read(), b"jpeg-bytes")
        finally:
            os.remove(temp_path)

    # 7. Zero-byte download handled as a failure.
    def test_empty_download_body_raises(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[self._fake_get_file_response("photos/file_0.jpg"), self._fake_file_response(b"")],
        ):
            with self.assertRaises(Exception):
                bot._download_telegram_photo_to_temp("abc123")


# =========================
# 3/9/10/11. Extraction success creates the expense preview, confirm
# writes, cancel doesn't, non-receipt/malformed/too-large handled.
# =========================
class TestPhotoReceiptToExpensePreview(PhotoWebhookTestCase):
    def test_receipt_extraction_creates_expense_preview_no_db_write(self):
        chat_id = 881010
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f.jpg"):
            with patch.object(photo_receipts, "extract_receipt_from_image", return_value=_receipt_candidate()):
                with patch("os.remove"):
                    with patch.object(bot, "add_expense") as mock_add_expense:
                        _call_webhook(_make_photo_update(881010001, chat_id))
        mock_add_expense.assert_not_called()
        self.assertIn(chat_id, pending_expense)
        entry = pending_expense[chat_id]
        self.assertEqual(entry["amount"], Decimal("86.40"))
        self.assertEqual(entry["category"], "Продукти")
        self.assertEqual(entry["description"], "Biedronka")
        texts = self._sent_texts()
        self.assertTrue(any("📸 Розпізнав чек:" in t and "Biedronka" in t for t in texts))
        self.assertTrue(any("Додати витрату?" in t for t in texts))

    def test_confirm_writes_via_existing_expense_confirm_path(self):
        chat_id = 881011
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f2.jpg"):
            with patch.object(photo_receipts, "extract_receipt_from_image", return_value=_receipt_candidate()):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881011001, chat_id))
        with patch.object(bot, "add_expense") as mock_add_expense:
            _call_webhook({
                "update_id": 881011002,
                "message": {"chat": {"id": chat_id}, "text": "✅ Так, додати", "from": {"id": 555, "first_name": "Тест"}},
            })
        mock_add_expense.assert_called_once()
        self.assertNotIn(chat_id, pending_expense)

    def test_cancel_writes_nothing(self):
        chat_id = 881012
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f3.jpg"):
            with patch.object(photo_receipts, "extract_receipt_from_image", return_value=_receipt_candidate()):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881012001, chat_id))
        with patch.object(bot, "add_expense") as mock_add_expense:
            _call_webhook({
                "update_id": 881012002,
                "message": {"chat": {"id": chat_id}, "text": "❌ Скасувати", "from": {"id": 555, "first_name": "Тест"}},
            })
        mock_add_expense.assert_not_called()
        self.assertNotIn(chat_id, pending_expense)

    def test_non_receipt_photo_returns_controlled_message(self):
        chat_id = 881013
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f4.jpg"):
            with patch.object(
                photo_receipts, "extract_receipt_from_image",
                return_value=_receipt_candidate(is_receipt=False, amount=None, merchant=None),
            ):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881013001, chat_id))
        self.assertEqual(self._sent_texts(), [photo_receipts.NOT_A_RECEIPT_MSG])
        self.assertNotIn(chat_id, pending_expense)

    def test_missing_amount_asks_user_to_type_no_preview(self):
        chat_id = 881014
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f5.jpg"):
            with patch.object(
                photo_receipts, "extract_receipt_from_image",
                return_value=_receipt_candidate(amount=None, confidence="low"),
            ):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881014001, chat_id))
        self.assertEqual(self._sent_texts(), [photo_receipts.MISSING_AMOUNT_MSG])
        self.assertNotIn(chat_id, pending_expense)

    def test_low_confidence_shows_warning_in_preview(self):
        chat_id = 881015
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f6.jpg"):
            with patch.object(photo_receipts, "extract_receipt_from_image", return_value=_receipt_candidate(confidence="low")):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881015001, chat_id))
        texts = self._sent_texts()
        self.assertTrue(any(photo_receipts.LOW_CONFIDENCE_WARNING in t for t in texts))

    def test_pharmacy_category_hint_maps_to_health_category(self):
        chat_id = 881016
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f7.jpg"):
            with patch.object(
                photo_receipts, "extract_receipt_from_image",
                return_value=_receipt_candidate(merchant="Rossmann", category_hint="pharmacy"),
            ):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881016001, chat_id))
        self.assertEqual(pending_expense[chat_id]["category"], "Здоров'я")

    def test_unknown_category_hint_defaults_to_inshe(self):
        chat_id = 881017
        with patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/f8.jpg"):
            with patch.object(
                photo_receipts, "extract_receipt_from_image",
                return_value=_receipt_candidate(category_hint="other"),
            ):
                with patch("os.remove"):
                    _call_webhook(_make_photo_update(881017001, chat_id))
        self.assertEqual(pending_expense[chat_id]["category"], "Інше")

    # 8. Too-large photo blocked before Gemini call.
    def test_too_large_photo_blocked_before_gemini_call(self):
        chat_id = 881018
        fd, temp_path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(b"x" * 1000)
        try:
            with patch.object(bot, "_download_telegram_photo_to_temp", return_value=temp_path):
                with patch.object(photo_receipts, "PHOTO_MAX_SIZE_MB", 0.0000001):
                    with patch.object(photo_receipts, "extract_receipt_from_image") as mock_extract:
                        _call_webhook(_make_photo_update(881018001, chat_id))
            mock_extract.assert_not_called()
            self.assertEqual(self._sent_texts(), [bot.PHOTO_TOO_LARGE_MSG])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_download_failure_sends_controlled_error_no_gemini_call(self):
        chat_id = 881019
        with patch.object(bot, "_download_telegram_photo_to_temp", side_effect=RuntimeError("network down")):
            with patch.object(photo_receipts, "extract_receipt_from_image") as mock_extract:
                _call_webhook(_make_photo_update(881019001, chat_id))
        mock_extract.assert_not_called()
        self.assertEqual(self._sent_texts(), [bot.PHOTO_DOWNLOAD_FAILED_MSG])

    def test_photo_input_disabled_sends_controlled_message(self):
        chat_id = 881020
        with patch.object(photo_receipts, "PHOTO_INPUT_ENABLED", False):
            with patch.object(bot, "_download_telegram_photo_to_temp") as mock_download:
                _call_webhook(_make_photo_update(881020001, chat_id))
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [photo_receipts.PHOTO_DISABLED_MSG])

    def test_missing_gemini_api_key_sends_controlled_message(self):
        chat_id = 881021
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            with patch.object(bot, "_download_telegram_photo_to_temp") as mock_download:
                _call_webhook(_make_photo_update(881021001, chat_id))
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [photo_receipts.MISSING_API_KEY_MSG])


# =========================
# 3. Pending guard: photo is blocked while another flow's preview is open.
# =========================
class TestPhotoBlockedByActivePreview(PhotoWebhookTestCase):
    def test_blocked_by_active_inventory_transform_preview(self):
        chat_id = 881030
        pending_inventory_transform[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "source_item_ids": [1, 2], "targets": [],
            "target_name": "X", "target_canonical_name": "x", "target_category": "Інше",
            "target_quantity_value": Decimal("1"), "target_quantity_unit": "шт.", "target_quantity_text": "1 шт.",
        }
        with patch.object(bot, "_download_telegram_photo_to_temp") as mock_download:
            _call_webhook(_make_photo_update(881030001, chat_id))
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG])
        self.assertIn(chat_id, pending_inventory_transform)

    def test_blocked_by_active_global_household_preview(self):
        chat_id = 881031
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        with patch.object(bot, "_download_telegram_photo_to_temp") as mock_download:
            _call_webhook(_make_photo_update(881031001, chat_id))
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG])

    def test_blocked_by_active_expense_preview(self):
        chat_id = 881032
        pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("10.00"), "currency": "PLN",
            "category": "Інше", "description": "Тест", "expense_date": None, "origin": "global",
        }
        with patch.object(bot, "_download_telegram_photo_to_temp") as mock_download:
            _call_webhook(_make_photo_update(881032001, chat_id))
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG])


# =========================
# 4. Regression: voice/text/expenses/inventory transform edit/general AI
# still work; document/photo detection doesn't interfere with plain text.
# =========================
class TestRegression(PhotoWebhookTestCase):
    def test_voice_still_works_alongside_photo_support(self):
        chat_id = 881040
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/v.oga"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="Що є вдома?"):
                with patch("os.remove"):
                    with patch.object(bot, "get_inventory_items", return_value=[]):
                        _call_webhook({
                            "update_id": 881040001,
                            "message": {
                                "chat": {"id": chat_id}, "voice": {"file_id": "v1", "duration": 3},
                                "from": {"id": 555, "first_name": "Тест"},
                            },
                        })
        self.assertTrue(any("🎙️ Розпізнав:" in t for t in self._sent_texts()))

    def test_plain_text_message_without_photo_or_voice_still_dispatches(self):
        chat_id = 881041
        with patch.object(bot, "call_gemini", return_value="Відповідь.") as mock_gemini:
            _call_webhook({
                "update_id": 881041001,
                "message": {"chat": {"id": chat_id}, "text": "Привіт!", "from": {"id": 555, "first_name": "Тест"}},
            })
        # Unified Mini Action Planner V1's pre-gate rejects "Привіт!" (no
        # household vocabulary/quantity signal) before ever calling Gemini
        # — only general AI-chat's own single call happens.
        mock_gemini.assert_called_once()


if __name__ == "__main__":
    unittest.main()
