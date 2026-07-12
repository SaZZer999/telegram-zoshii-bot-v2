"""Voice Transcript Normalizer V1 — pure unit tests for
voice_transcript_normalizer.py. No real Gemini call, no Telegram, no Flask,
no temp-file download — see tests/test_voice_transcript_normalizer_routing.py
for the webhook-level integration tests (bot._handle_voice_message calling
normalize() and using its result for both the echo and the dispatched
text)."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import voice_transcript_normalizer as vtn  # noqa: E402

MIXED_TRANSCRIPT = (
    "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. "
    "We bought a komod, which cost 627, but we bought it for 527. We also have an auto-carsel, "
    "but we didn't pay anything for this."
)

NORMALIZED_TRANSCRIPT = (
    "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. "
    "Ми купили комод, який коштував 627, але купили за 527. Ще маємо автокрісло, за яке нічого не заплатили."
)


class TestNeedsNormalization(unittest.TestCase):
    def test_uk_language_with_latin_text_needs_normalization(self):
        self.assertTrue(vtn.needs_normalization(MIXED_TRANSCRIPT, "uk"))

    def test_uk_language_with_pure_ukrainian_does_not_need_it(self):
        self.assertFalse(vtn.needs_normalization("Купив молоко і хліб.", "uk"))

    def test_other_languages_never_need_it_even_with_latin_text(self):
        for lang in ("pl", "en", "ru", None):
            with self.subTest(lang=lang):
                self.assertFalse(vtn.needs_normalization(MIXED_TRANSCRIPT, lang))

    def test_blank_transcript_never_needs_it(self):
        self.assertFalse(vtn.needs_normalization("", "uk"))
        self.assertFalse(vtn.needs_normalization("   ", "uk"))
        self.assertFalse(vtn.needs_normalization(None, "uk"))

    def test_short_latin_runs_do_not_trigger_it(self):
        # Unit abbreviations like "l"/"kg" are 1-2 Latin letters — not
        # worth a Gemini call by themselves.
        self.assertFalse(vtn.needs_normalization("Купив 2 l молока", "uk"))


class TestNumbersPreserved(unittest.TestCase):
    def test_identical_numbers_pass(self):
        self.assertTrue(vtn._numbers_preserved("коштувало 627, купили за 527", "коштувало 627, купили за 527"))

    def test_reordered_numbers_still_pass(self):
        self.assertTrue(vtn._numbers_preserved("627 і 527", "527 і 627"))

    def test_missing_number_fails(self):
        self.assertFalse(vtn._numbers_preserved("627 і 527", "627"))

    def test_altered_number_fails(self):
        self.assertFalse(vtn._numbers_preserved("коштувало 627", "коштувало 600"))

    def test_extra_invented_number_fails(self):
        self.assertFalse(vtn._numbers_preserved("627", "627 і 100"))

    def test_repeated_amount_both_copies_required(self):
        self.assertTrue(vtn._numbers_preserved("60 за 60", "60 за 60"))
        self.assertFalse(vtn._numbers_preserved("60 за 60", "60"))


class TestNormalize(unittest.TestCase):
    def test_skips_gemini_call_when_not_needed(self):
        with patch.object(bot, "call_gemini") as mock_gemini:
            result, changed = vtn.normalize("Купив молоко і хліб.", "uk")
        mock_gemini.assert_not_called()
        self.assertEqual(result, "Купив молоко і хліб.")
        self.assertFalse(changed)

    def test_skips_gemini_call_for_non_uk_language(self):
        with patch.object(bot, "call_gemini") as mock_gemini:
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "pl")
        mock_gemini.assert_not_called()
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)

    def test_successful_normalization_returns_gemini_result(self):
        fake_response = '{"normalized": "%s"}' % NORMALIZED_TRANSCRIPT
        with patch.object(bot, "call_gemini", return_value=fake_response) as mock_gemini:
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        mock_gemini.assert_called_once()
        self.assertTrue(changed)
        self.assertEqual(result, NORMALIZED_TRANSCRIPT)
        self.assertNotIn("komod", result)
        self.assertNotIn("auto-carsel", result)
        self.assertIn("комод", result)
        self.assertIn("автокрісло", result)

    def test_number_mismatch_falls_back_to_raw(self):
        # Gemini altered "627" to "600" — never trusted.
        bad_response = '{"normalized": "Купили комод за 600."}'
        with patch.object(bot, "call_gemini", return_value=bad_response):
            result, changed = vtn.normalize("Купили komod за 627.", "uk")
        self.assertEqual(result, "Купили komod за 627.")
        self.assertFalse(changed)

    def test_malformed_json_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", return_value="not valid json"):
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)

    def test_empty_gemini_response_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", return_value=None):
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)

    def test_missing_normalized_field_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", return_value='{"other_field": "x"}'):
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)

    def test_blank_normalized_field_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", return_value='{"normalized": "   "}'):
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)

    def test_gemini_exception_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", side_effect=RuntimeError("network down")):
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)

    def test_markdown_fenced_json_is_accepted(self):
        fenced = '```json\n{"normalized": "%s"}\n```' % NORMALIZED_TRANSCRIPT
        with patch.object(bot, "call_gemini", return_value=fenced):
            result, changed = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertTrue(changed)
        self.assertEqual(result, NORMALIZED_TRANSCRIPT)


if __name__ == "__main__":
    unittest.main()
