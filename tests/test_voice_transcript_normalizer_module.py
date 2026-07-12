"""Voice Transcript Normalizer V1 (+ number-preservation fix) — pure unit
tests for voice_transcript_normalizer.py. No real Gemini call, no Telegram,
no Flask, no temp-file download — see
tests/test_voice_transcript_normalizer_routing.py for the webhook-level
integration tests (bot._handle_voice_message calling normalize() and using
its result for both the echo and the dispatched text)."""
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

# The exact live transcript from the regression report — includes the
# "for 60 for 60" stutter that made _numbers_preserved wrongly reject a
# correct normalization and silently fall back to the raw, still-mixed text.
LIVE_TRANSCRIPT = (
    "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. Також ми купили "
    "дитячу ліжечку, яке на сайті оригінальному коштувало 650, але ми знайшли його за 570. We bought a "
    "komod, which cost 627, but we bought it for 527. We also have an auto-carsel, but we didn't pay "
    "anything for this. I only bought a gift for her sister for 60 for 60, to thank her for the "
    "auto-carsel."
)

LIVE_TRANSCRIPT_NORMALIZED = (
    "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. Також ми купили "
    "дитяче ліжечко, яке на сайті оригінальному коштувало 650, але ми знайшли його за 570. Ми купили "
    "комод, який коштував 627, але купили за 527. Ще маємо автокрісло, але за нього нічого не заплатили. "
    "Я купила подарунок для її сестри за 60, щоб подякувати за автокрісло."
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


class TestCollapseAdjacentNumberStutters(unittest.TestCase):
    def test_adjacent_stutter_collapses(self):
        self.assertEqual(
            vtn._collapse_adjacent_number_stutters("gift for her sister for 60 for 60, to thank her"),
            ["60"],
        )

    def test_far_apart_repeat_of_same_number_not_collapsed(self):
        # "150" sits between the two "3300"s — never a stutter.
        self.assertEqual(
            vtn._collapse_adjacent_number_stutters("коштувало 3300, знижка 150, а машина теж 3300"),
            ["3300", "150", "3300"],
        )

    def test_different_adjacent_numbers_not_collapsed(self):
        self.assertEqual(vtn._collapse_adjacent_number_stutters("627 і 527"), ["627", "527"])

    def test_no_numbers_returns_empty(self):
        self.assertEqual(vtn._collapse_adjacent_number_stutters("нічого числового тут"), [])

    def test_gap_too_wide_not_collapsed(self):
        # More than max_gap_words words between the two "60"s — not a
        # recognizer stutter, kept as two separate occurrences.
        self.assertEqual(
            vtn._collapse_adjacent_number_stutters("60 zł а потім ще раз згадаю суму 60 zł", max_gap_words=2),
            ["60", "60"],
        )


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

    def test_stutter_collapsed_in_normalized_is_accepted(self):
        # The live regression: raw has "60 for 60" (a stutter); a normalized
        # text that naturally collapses it to one "60" must still pass.
        self.assertTrue(vtn._numbers_preserved("gift for 60 for 60", "подарунок за 60"))

    def test_stutter_preserved_verbatim_is_also_accepted(self):
        # Gemini choosing NOT to collapse the stutter is equally valid.
        self.assertTrue(vtn._numbers_preserved("60 за 60", "60 за 60"))

    def test_genuinely_separate_repeat_still_requires_both_occurrences(self):
        # "150" between the two "3300"s means they're NOT a stutter — both
        # must still survive in the normalized text.
        self.assertFalse(
            vtn._numbers_preserved("коштувало 3300, знижка 150, а машина теж 3300", "коштувало 3300, знижка 150")
        )

    def test_live_transcript_stutter_does_not_block_preservation(self):
        self.assertTrue(vtn._numbers_preserved(
            "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. Також ми купили "
            "дитячу ліжечку, яке на сайті оригінальному коштувало 650, але ми знайшли його за 570. We bought a "
            "komod, which cost 627, but we bought it for 527. We also have an auto-carsel, but we didn't pay "
            "anything for this. I only bought a gift for her sister for 60 for 60, to thank her for the "
            "auto-carsel.",
            "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. Також ми купили "
            "дитяче ліжечко, яке на сайті оригінальному коштувало 650, але ми знайшли його за 570. Ми купили "
            "комод, який коштував 627, але купили за 527. Ще маємо автокрісло, але за нього нічого не заплатили. "
            "Я купила подарунок для її сестри за 60, щоб подякувати за автокрісло.",
        ))


class TestNormalize(unittest.TestCase):
    def test_skips_gemini_call_when_not_needed(self):
        with patch.object(bot, "call_gemini") as mock_gemini:
            result, changed, status = vtn.normalize("Купив молоко і хліб.", "uk")
        mock_gemini.assert_not_called()
        self.assertEqual(result, "Купив молоко і хліб.")
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_SKIPPED_NO_LATIN)

    def test_skips_gemini_call_for_non_uk_language(self):
        with patch.object(bot, "call_gemini") as mock_gemini:
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "pl")
        mock_gemini.assert_not_called()
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_SKIPPED_NOT_UK)

    def test_successful_normalization_returns_gemini_result(self):
        fake_response = '{"normalized": "%s"}' % NORMALIZED_TRANSCRIPT
        with patch.object(bot, "call_gemini", return_value=fake_response) as mock_gemini:
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        mock_gemini.assert_called_once()
        self.assertTrue(changed)
        self.assertEqual(status, vtn.STATUS_CHANGED)
        self.assertEqual(result, NORMALIZED_TRANSCRIPT)
        self.assertNotIn("komod", result)
        self.assertNotIn("auto-carsel", result)
        self.assertIn("комод", result)
        self.assertIn("автокрісло", result)

    def test_number_mismatch_retries_once_then_falls_back_to_raw(self):
        # Gemini altered "627" to "600" on BOTH attempts (a real change,
        # not a stutter) — never trusted, even after the retry.
        bad_response = '{"normalized": "Купили комод за 600."}'
        with patch.object(bot, "call_gemini", return_value=bad_response) as mock_gemini:
            result, changed, status = vtn.normalize("Купили komod за 627.", "uk")
        self.assertEqual(result, "Купили komod за 627.")
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_FALLBACK_RAW)
        self.assertEqual(mock_gemini.call_count, 2)  # first attempt + one retry

    def test_number_mismatch_on_first_attempt_succeeds_on_retry(self):
        bad_response = '{"normalized": "Купили комод за 600."}'
        good_response = '{"normalized": "Купили комод за 627."}'
        with patch.object(bot, "call_gemini", side_effect=[bad_response, good_response]) as mock_gemini:
            result, changed, status = vtn.normalize("Купили komod за 627.", "uk")
        self.assertEqual(result, "Купили комод за 627.")
        self.assertTrue(changed)
        self.assertEqual(status, vtn.STATUS_CHANGED)
        self.assertEqual(mock_gemini.call_count, 2)

    def test_stuttered_amount_does_not_trigger_a_retry_at_all(self):
        # A CORRECT normalization that naturally collapses "60 for 60" into
        # one "60" must pass on the FIRST attempt — no retry needed.
        good_response = '{"normalized": "Подарунок за 60."}'
        with patch.object(bot, "call_gemini", return_value=good_response) as mock_gemini:
            result, changed, status = vtn.normalize("Gift for 60 for 60.", "uk")
        self.assertEqual(result, "Подарунок за 60.")
        self.assertTrue(changed)
        self.assertEqual(status, vtn.STATUS_CHANGED)
        mock_gemini.assert_called_once()

    def test_malformed_json_falls_back_to_raw_without_retry(self):
        with patch.object(bot, "call_gemini", return_value="not valid json") as mock_gemini:
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_INVALID_JSON)
        mock_gemini.assert_called_once()  # malformed JSON never retries

    def test_empty_gemini_response_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", return_value=None):
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_GEMINI_ERROR)

    def test_missing_normalized_field_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", return_value='{"other_field": "x"}'):
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_INVALID_JSON)

    def test_blank_normalized_field_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", return_value='{"normalized": "   "}'):
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_INVALID_JSON)

    def test_gemini_exception_falls_back_to_raw(self):
        with patch.object(bot, "call_gemini", side_effect=RuntimeError("network down")):
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertEqual(result, MIXED_TRANSCRIPT)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_GEMINI_ERROR)

    def test_markdown_fenced_json_is_accepted(self):
        fenced = '```json\n{"normalized": "%s"}\n```' % NORMALIZED_TRANSCRIPT
        with patch.object(bot, "call_gemini", return_value=fenced):
            result, changed, status = vtn.normalize(MIXED_TRANSCRIPT, "uk")
        self.assertTrue(changed)
        self.assertEqual(status, vtn.STATUS_CHANGED)
        self.assertEqual(result, NORMALIZED_TRANSCRIPT)

    def test_already_ukrainian_text_returns_unchanged_status(self):
        # A transcript with Latin content that Gemini decides is already
        # fully Ukrainian (rule 6) is returned identically — changed=False,
        # but distinguishable from a failure via status=STATUS_UNCHANGED.
        text_with_short_latin = "Купив 2 l молока і трохи xyz"
        response = '{"normalized": "%s"}' % text_with_short_latin
        with patch.object(bot, "call_gemini", return_value=response):
            result, changed, status = vtn.normalize(text_with_short_latin, "uk")
        self.assertEqual(result, text_with_short_latin)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_UNCHANGED)

    # =========================
    # The exact live regression transcript.
    # =========================
    def test_live_transcript_normalizes_instead_of_returning_raw(self):
        response = '{"normalized": "%s"}' % LIVE_TRANSCRIPT_NORMALIZED
        with patch.object(bot, "call_gemini", return_value=response) as mock_gemini:
            result, changed, status = vtn.normalize(LIVE_TRANSCRIPT, "uk")
        mock_gemini.assert_called_once()  # succeeds on the first attempt, no retry needed
        self.assertTrue(changed)
        self.assertEqual(status, vtn.STATUS_CHANGED)
        self.assertNotEqual(result, LIVE_TRANSCRIPT)
        self.assertNotIn("We bought", result)
        self.assertNotIn("auto-carsel", result)
        self.assertNotIn("gift for her sister", result)
        self.assertIn("комод", result)
        self.assertIn("автокрісло", result)
        self.assertIn("подарунок", result.lower())
        for amount in ("3300", "150", "650", "570", "627", "527", "60"):
            self.assertIn(amount, result)

    def test_live_transcript_with_stutter_survives_number_check(self):
        # Direct proof the stutter alone is never sufficient to trigger a
        # fallback: the collapsed-and-preserved live example passes
        # _numbers_preserved outright.
        self.assertTrue(vtn._numbers_preserved(LIVE_TRANSCRIPT, LIVE_TRANSCRIPT_NORMALIZED))

    def test_gemini_changing_a_real_number_in_live_transcript_still_falls_back(self):
        # A genuinely altered amount (627 -> 600) must still be rejected —
        # even embedded in an otherwise-correct live-transcript normalization
        # — after the retry also reproduces the same wrong number.
        bad_normalized = LIVE_TRANSCRIPT_NORMALIZED.replace("627", "600")
        bad_response = '{"normalized": "%s"}' % bad_normalized
        with patch.object(bot, "call_gemini", return_value=bad_response) as mock_gemini:
            result, changed, status = vtn.normalize(LIVE_TRANSCRIPT, "uk")
        self.assertEqual(result, LIVE_TRANSCRIPT)
        self.assertFalse(changed)
        self.assertEqual(status, vtn.STATUS_FALLBACK_RAW)
        self.assertEqual(mock_gemini.call_count, 2)


if __name__ == "__main__":
    unittest.main()
