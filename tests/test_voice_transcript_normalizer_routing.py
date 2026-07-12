"""Voice Transcript Normalizer V1 — webhook-level integration tests: a
Telegram `voice` message is downloaded and transcribed (both mocked, no
real Groq/Telegram call), and the resulting transcript is normalized
(voice_transcript_normalizer.normalize, exercised for real here — only its
own Gemini call, bot.call_gemini, is mocked) BEFORE both the "🎙️ Розпізнав:"
echo and the text handed to message_dispatcher.dispatch(...). See
tests/test_voice_transcript_normalizer_module.py for normalize()'s own pure
unit tests (no webhook/Telegram involved there at all).
"""
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
import voice_input  # noqa: E402
import voice_transcript_normalizer  # noqa: E402
from bot import pending_global_household  # noqa: E402

RAW_MIXED_TRANSCRIPT = (
    "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. "
    "We bought a komod, which cost 627, but we bought it for 527. We also have an auto-carsel, "
    "but we didn't pay anything for this. Gift for her sister for 60."
)

NORMALIZED_TRANSCRIPT = (
    "Ми купили візочок для дитини за 3300 злотих, але нам зробили знижку 150 злотих. "
    "Ми купили комод, який коштував 627, але купили за 527. Ще маємо автокрісло, за яке нічого не заплатили. "
    "Подарунок для її сестри за 60."
)


def _make_voice_update(update_id, chat_id, user_id=555, duration=5):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "voice": {"file_id": "voice_file_1", "duration": duration, "mime_type": "audio/ogg"},
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class VoiceNormalizerWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 1/2/3 — a mixed-language transcript with "uk" selected gets normalized;
# the normalized text (not the raw one) is both echoed AND handed to the
# dispatcher/planner. Amounts are preserved.
# =========================
class TestMixedTranscriptNormalized(VoiceNormalizerWebhookTestCase):
    def test_normalized_transcript_is_echoed_and_passed_to_dispatcher(self):
        # voice_transcript_normalizer.normalize is mocked directly (rather
        # than bot.call_gemini) so this test isolates "does _handle_voice_
        # message use the normalizer's result for the echo" from whatever
        # Gemini calls the SUBSEQUENT dispatch/routing of the (now normal-
        # looking Ukrainian) text happens to make — that's covered by
        # test_normalized_transcript_reaches_the_purchase_planner_not_raw_text
        # below, with the downstream router fully mocked instead.
        chat_id = 994001
        with patch.object(bot, "get_user_voice_language", return_value="uk"):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f.oga"):
                with patch.object(voice_input, "transcribe_audio_file", return_value=RAW_MIXED_TRANSCRIPT):
                    with patch("os.remove"):
                        with patch.object(
                            voice_transcript_normalizer, "normalize",
                            return_value=(NORMALIZED_TRANSCRIPT, True),
                        ) as mock_normalize:
                            with patch.object(bot, "call_gemini", return_value=None):
                                _call_webhook(_make_voice_update(994001001, chat_id, user_id=chat_id))
        mock_normalize.assert_called_once_with(RAW_MIXED_TRANSCRIPT, "uk")
        texts = self._sent_texts()
        # The echo shows the NORMALIZED transcript, never the raw English fragments.
        echo = next(t for t in texts if t.startswith("🎙️ Розпізнав:"))
        self.assertIn("комод", echo)
        self.assertIn("автокрісло", echo)
        self.assertNotIn("komod", echo)
        self.assertNotIn("auto-carsel", echo)
        # Amounts survived normalization unchanged.
        for amount in ("3300", "150", "627", "527", "60"):
            self.assertIn(amount, echo)

    def test_normalized_transcript_reaches_the_purchase_planner_not_raw_text(self):
        chat_id = 994002
        fake_gemini_normalize_response = '{"normalized": "%s"}' % NORMALIZED_TRANSCRIPT
        fake_router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "assumed_expense", "description": "Візочок для дитини", "original_price": "3300",
                 "discount_amount": "150", "currency": "PLN"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "get_user_voice_language", return_value="uk"):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f2.oga"):
                with patch.object(voice_input, "transcribe_audio_file", return_value=RAW_MIXED_TRANSCRIPT):
                    with patch("os.remove"):
                        # Two DIFFERENT Gemini calls happen here: the normalizer's own
                        # call, then household_router's own call — side_effect
                        # returns the right payload for each in order.
                        with patch.object(bot, "call_gemini", return_value=fake_gemini_normalize_response):
                            with patch.object(
                                bot.household_router, "_ask_gemini_household_router",
                                return_value=fake_router_result,
                            ) as mock_router:
                                with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
                                    with patch.object(bot, "get_active_shopping_items", return_value=[]):
                                        with patch.object(bot, "get_inventory_items", return_value=[]):
                                            with patch.object(
                                                bot, "get_recent_expenses_for_deletion", return_value=[],
                                            ):
                                                with patch.object(
                                                    bot, "get_household_alias_map", return_value={},
                                                ):
                                                    _call_webhook(
                                                        _make_voice_update(994002001, chat_id, user_id=chat_id),
                                                    )
        # household_router received the NORMALIZED text, not the raw one —
        # its own _ask_gemini_household_router call's positional `text` arg.
        mock_router.assert_called_once()
        called_text = mock_router.call_args.args[0]
        self.assertIn("комод", called_text)
        self.assertNotIn("komod", called_text)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"].to_eng_string(), "3150.00")


# =========================
# 4 — if the normalizer fails for any reason, the RAW transcript is used
# (echoed and dispatched) instead — voice input never breaks because of it.
# =========================
class TestNormalizerFailureFallsBackToRaw(VoiceNormalizerWebhookTestCase):
    def test_gemini_failure_falls_back_to_raw_transcript(self):
        chat_id = 994003
        with patch.object(bot, "get_user_voice_language", return_value="uk"):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f3.oga"):
                with patch.object(voice_input, "transcribe_audio_file", return_value=RAW_MIXED_TRANSCRIPT):
                    with patch("os.remove"):
                        with patch.object(bot, "call_gemini", side_effect=RuntimeError("network down")):
                            _call_webhook(_make_voice_update(994003001, chat_id, user_id=chat_id))
        texts = self._sent_texts()
        echo = next(t for t in texts if t.startswith("🎙️ Розпізнав:"))
        self.assertIn("komod", echo)  # raw, unnormalized fragment survives the fallback

    def test_number_altering_response_falls_back_to_raw(self):
        chat_id = 994004
        bad_response = '{"normalized": "Купили комод за 600."}'
        with patch.object(bot, "get_user_voice_language", return_value="uk"):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f4.oga"):
                with patch.object(voice_input, "transcribe_audio_file", return_value="Купили komod за 627."):
                    with patch("os.remove"):
                        with patch.object(bot, "call_gemini", return_value=bad_response):
                            _call_webhook(_make_voice_update(994004001, chat_id, user_id=chat_id))
        texts = self._sent_texts()
        echo = next(t for t in texts if t.startswith("🎙️ Розпізнав:"))
        self.assertIn("627", echo)
        self.assertNotIn("600", echo)

    def test_no_language_selected_never_calls_normalizer_gemini(self):
        # No voice_language selected -> voice_transcript_normalizer.
        # needs_normalization() is False, so normalize() must return the
        # raw transcript unchanged WITHOUT ever calling Gemini itself —
        # verified directly via a spy, since the raw (still English-mixed)
        # text naturally triggers OTHER, unrelated Gemini calls further
        # downstream (household_router's own gate() still matches "купили")
        # that have nothing to do with the normalizer's own behavior.
        chat_id = 994005
        with patch.object(bot, "get_user_voice_language", return_value=None):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f5.oga"):
                with patch.object(voice_input, "transcribe_audio_file", return_value=RAW_MIXED_TRANSCRIPT):
                    with patch("os.remove"):
                        with patch.object(
                            voice_transcript_normalizer, "normalize",
                            wraps=voice_transcript_normalizer.normalize,
                        ) as spy_normalize:
                            with patch.object(bot, "call_gemini", return_value=None):
                                _call_webhook(_make_voice_update(994005001, chat_id, user_id=chat_id))
        spy_normalize.assert_called_once_with(RAW_MIXED_TRANSCRIPT, None)
        texts = self._sent_texts()
        echo = next(t for t in texts if t.startswith("🎙️ Розпізнав:"))
        self.assertIn("komod", echo)


if __name__ == "__main__":
    unittest.main()
