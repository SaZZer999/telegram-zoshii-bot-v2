"""Photo Receipt Input V1 — pure unit tests for photo_receipts.py. No real
Gemini call, no Telegram, no Flask, no temp-file download — see
tests/test_photo_receipt_routing.py for the webhook-level integration
tests (Telegram photo download + hand-off into the existing expense
preview flow)."""
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import photo_receipts


class TestEnsureReady(unittest.TestCase):
    def test_disabled_raises_controlled_error(self):
        with patch.object(photo_receipts, "PHOTO_INPUT_ENABLED", False):
            with self.assertRaises(photo_receipts.PhotoInputError) as ctx:
                photo_receipts.ensure_ready(api_key="sk-test")
            self.assertEqual(str(ctx.exception), photo_receipts.PHOTO_DISABLED_MSG)

    def test_unknown_provider_raises_controlled_error(self):
        with patch.object(photo_receipts, "PHOTO_PROVIDER", "disabled"):
            with self.assertRaises(photo_receipts.PhotoInputError) as ctx:
                photo_receipts.ensure_ready(api_key="sk-test")
            self.assertEqual(str(ctx.exception), photo_receipts.PHOTO_DISABLED_MSG)

    def test_missing_api_key_raises_controlled_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            with self.assertRaises(photo_receipts.PhotoInputError) as ctx:
                photo_receipts.ensure_ready(api_key=None)
            self.assertEqual(str(ctx.exception), photo_receipts.MISSING_API_KEY_MSG)

    def test_ready_when_enabled_gemini_and_key_present(self):
        photo_receipts.ensure_ready(api_key="sk-test")  # must not raise


def _receipt_json(**overrides):
    data = {
        "is_receipt": True, "merchant": "Biedronka", "total_amount": "86.40",
        "currency": "PLN", "date": "2026-07-10", "category": "grocery",
        "confidence": "high", "warnings": [],
    }
    data.update(overrides)
    return data


class TestParseReceiptJson(unittest.TestCase):
    def test_valid_json_parses_correctly(self):
        import json
        candidate = photo_receipts._parse_receipt_json(json.dumps(_receipt_json()))
        self.assertTrue(candidate.is_receipt)
        self.assertEqual(candidate.merchant, "Biedronka")
        self.assertEqual(candidate.amount, Decimal("86.40"))
        self.assertEqual(candidate.currency, "PLN")
        self.assertEqual(candidate.date, "2026-07-10")
        self.assertEqual(candidate.category_hint, "grocery")
        self.assertEqual(candidate.confidence, "high")

    def test_polish_decimal_comma_normalizes(self):
        self.assertEqual(photo_receipts._parse_amount("86,40"), Decimal("86.40"))
        self.assertEqual(photo_receipts._parse_amount("86,40 zł"), Decimal("86.40"))

    def test_currency_missing_defaults_to_pln(self):
        import json
        raw = json.dumps(_receipt_json(currency=None))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertEqual(candidate.currency, "PLN")

    def test_merchant_missing_falls_back_to_none(self):
        import json
        raw = json.dumps(_receipt_json(merchant=None))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertIsNone(candidate.merchant)

    def test_date_missing_allowed(self):
        import json
        raw = json.dumps(_receipt_json(date=None))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertIsNone(candidate.date)
        self.assertTrue(candidate.is_receipt)

    def test_malformed_json_returns_none(self):
        self.assertIsNone(photo_receipts._parse_receipt_json("not json at all {"))

    def test_non_dict_json_returns_none(self):
        self.assertIsNone(photo_receipts._parse_receipt_json("[1, 2, 3]"))

    def test_blank_text_returns_none(self):
        self.assertIsNone(photo_receipts._parse_receipt_json(""))

    def test_is_receipt_false_parses(self):
        import json
        raw = json.dumps(_receipt_json(is_receipt=False, total_amount=None, merchant=None))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertFalse(candidate.is_receipt)

    def test_missing_amount_parses_with_none_amount(self):
        import json
        raw = json.dumps(_receipt_json(total_amount=None, confidence="low"))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertTrue(candidate.is_receipt)
        self.assertIsNone(candidate.amount)

    def test_invalid_amount_string_returns_none_amount(self):
        import json
        raw = json.dumps(_receipt_json(total_amount="not a number"))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertIsNone(candidate.amount)

    def test_zero_or_negative_amount_rejected(self):
        self.assertIsNone(photo_receipts._parse_amount("0"))
        self.assertIsNone(photo_receipts._parse_amount("-5.00"))

    def test_unknown_category_defaults_to_other(self):
        import json
        raw = json.dumps(_receipt_json(category="something weird"))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertEqual(candidate.category_hint, "other")

    def test_low_confidence_preserved(self):
        import json
        raw = json.dumps(_receipt_json(confidence="low"))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertEqual(candidate.confidence, "low")

    def test_invalid_confidence_defaults_to_low(self):
        import json
        raw = json.dumps(_receipt_json(confidence="super sure"))
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertEqual(candidate.confidence, "low")

    def test_markdown_fenced_json_is_unwrapped(self):
        import json
        raw = "```json\n" + json.dumps(_receipt_json()) + "\n```"
        candidate = photo_receipts._parse_receipt_json(raw)
        self.assertTrue(candidate.is_receipt)
        self.assertEqual(candidate.amount, Decimal("86.40"))


class TestDecideReceiptOutcome(unittest.TestCase):
    def test_not_a_receipt(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=False)
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "not_a_receipt")
        self.assertIsNone(payload)

    def test_missing_amount_asks_user_to_type(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=None, confidence="low")
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "missing_amount")
        self.assertIsNone(payload)

    def test_ok_payload_defaults_merchant_to_chek(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=Decimal("10.00"), merchant=None)
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["merchant"], "Чек")

    def test_ok_payload_uses_given_merchant(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=Decimal("10.00"), merchant="Lidl")
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(payload["merchant"], "Lidl")

    def test_ok_payload_defaults_date_to_today_when_missing(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=Decimal("10.00"), date=None)
        kind, payload = photo_receipts.decide_receipt_outcome(candidate, now=now)
        self.assertEqual(payload["expense_date"], now.date())

    def test_ok_payload_uses_given_date(self):
        from datetime import date, datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=Decimal("10.00"), date="2026-07-05")
        kind, payload = photo_receipts.decide_receipt_outcome(candidate, now=now)
        self.assertEqual(payload["expense_date"], date(2026, 7, 5))

    def test_ok_payload_future_date_clamped_to_today(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=Decimal("10.00"), date="2099-01-01")
        kind, payload = photo_receipts.decide_receipt_outcome(candidate, now=now)
        self.assertEqual(payload["expense_date"], now.date())

    def test_ok_payload_carries_confidence_and_category_hint(self):
        candidate = photo_receipts.ReceiptCandidate(
            is_receipt=True, amount=Decimal("10.00"), confidence="low", category_hint="pharmacy",
        )
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(payload["confidence"], "low")
        self.assertEqual(payload["category_hint"], "pharmacy")


class TestExtractReceiptFromImage(unittest.TestCase):
    def setUp(self):
        fd, self.temp_path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(b"fake-jpeg-bytes")
        self.addCleanup(lambda: os.path.exists(self.temp_path) and os.remove(self.temp_path))

    def test_disabled_raises_before_touching_gemini(self):
        with patch.object(photo_receipts, "PHOTO_INPUT_ENABLED", False):
            with patch.object(photo_receipts, "requests") as mock_requests:
                with self.assertRaises(photo_receipts.PhotoInputError) as ctx:
                    photo_receipts.extract_receipt_from_image(self.temp_path, api_key="sk-test")
                self.assertEqual(str(ctx.exception), photo_receipts.PHOTO_DISABLED_MSG)
            mock_requests.post.assert_not_called()

    def test_missing_api_key_raises_before_touching_gemini(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            with patch.object(photo_receipts, "requests") as mock_requests:
                with self.assertRaises(photo_receipts.PhotoInputError) as ctx:
                    photo_receipts.extract_receipt_from_image(self.temp_path, api_key=None)
                self.assertEqual(str(ctx.exception), photo_receipts.MISSING_API_KEY_MSG)
            mock_requests.post.assert_not_called()

    def _mock_gemini_response(self, text):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        return response

    def test_success_returns_receipt_candidate(self):
        import json
        raw = json.dumps(_receipt_json())
        with patch.object(photo_receipts, "requests") as mock_requests:
            mock_requests.post.return_value = self._mock_gemini_response(raw)
            candidate = photo_receipts.extract_receipt_from_image(self.temp_path, api_key="sk-test")
        self.assertTrue(candidate.is_receipt)
        self.assertEqual(candidate.amount, Decimal("86.40"))
        _, kwargs = mock_requests.post.call_args
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "sk-test")

    def test_malformed_response_raises_controlled_error(self):
        with patch.object(photo_receipts, "requests") as mock_requests:
            mock_requests.post.return_value = self._mock_gemini_response("not valid json")
            with self.assertRaises(photo_receipts.PhotoInputError) as ctx:
                photo_receipts.extract_receipt_from_image(self.temp_path, api_key="sk-test")
        self.assertEqual(str(ctx.exception), photo_receipts.MALFORMED_MSG)

    def test_provider_error_raises_controlled_message_no_key_leak(self):
        with patch.object(photo_receipts, "requests") as mock_requests:
            mock_requests.post.side_effect = RuntimeError("gemini 401: key=sk-test")
            with self.assertRaises(photo_receipts.PhotoInputError) as ctx:
                photo_receipts.extract_receipt_from_image(self.temp_path, api_key="sk-test")
        self.assertEqual(str(ctx.exception), photo_receipts.MALFORMED_MSG)
        self.assertNotIn("sk-test", str(ctx.exception))

    def test_success_logs_without_leaking_key(self):
        import json
        raw = json.dumps(_receipt_json())
        with patch.object(photo_receipts, "requests") as mock_requests:
            mock_requests.post.return_value = self._mock_gemini_response(raw)
            with self.assertLogs(photo_receipts.logger, level="INFO") as log_ctx:
                photo_receipts.extract_receipt_from_image(self.temp_path, api_key="sk-test")
        joined = "\n".join(log_ctx.output)
        self.assertIn("photo_receipt_extraction_start", joined)
        self.assertIn("photo_receipt_extraction_success", joined)
        self.assertNotIn("sk-test", joined)


if __name__ == "__main__":
    unittest.main()
