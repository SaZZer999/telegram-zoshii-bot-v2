"""Voice Input V1 — webhook-level integration tests: a Telegram `voice`
message is downloaded, transcribed, and the transcript is handed into the
EXACT SAME message_dispatcher.dispatch(...) path a typed text message
already uses (bot.webhook() -> bot._handle_voice_message -> dispatch()).

No real Groq call and no real Telegram file download happens anywhere in
this file — bot._download_telegram_voice_to_temp and voice_input.
transcribe_audio_file are always mocked/faked. See
tests/test_voice_input_module.py for voice_input.py's own pure unit tests
(no Flask/Telegram/webhook involved there at all).
"""
import sys
import os
import tempfile
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
from bot import (  # noqa: E402
    pending_inventory_transform,
    pending_destructive_guard,
    pending_global_household,
)


def _make_voice_update(update_id, chat_id, file_id="voice_file_123", duration=5, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "voice": {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg"},
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class VoiceWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_inventory_transform.clear()
        pending_destructive_guard.clear()
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        pending_inventory_transform.clear()
        pending_destructive_guard.clear()
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 2. Basic routing: detected, downloaded with file_id, temp file cleaned up,
# transcript handed to the same dispatcher path (echoed as "🎙️ Розпізнав:").
# =========================
class TestVoiceRoutingBasics(VoiceWebhookTestCase):
    def test_voice_message_downloaded_with_file_id_and_temp_file_cleaned_up(self):
        chat_id = 991301
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/fake.oga") as mock_download:
            with patch.object(voice_input, "transcribe_audio_file", return_value="Що є вдома?") as mock_transcribe:
                with patch("os.remove") as mock_remove:
                    with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
                        with patch.object(bot, "get_inventory_items", return_value=[]):
                            _call_webhook(_make_voice_update(991301001, chat_id, file_id="voice_file_123"))
        mock_download.assert_called_once_with("voice_file_123")
        mock_transcribe.assert_called_once_with("/tmp/fake.oga")
        mock_remove.assert_called_once_with("/tmp/fake.oga")

    def test_temp_file_removed_from_disk_even_when_transcription_fails(self):
        chat_id = 991302
        fd, temp_path = tempfile.mkstemp(suffix=".oga")
        os.close(fd)
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value=temp_path):
            with patch.object(
                voice_input, "transcribe_audio_file",
                side_effect=voice_input.VoiceInputError(voice_input.TRANSCRIBE_FAILED_MSG),
            ):
                _call_webhook(_make_voice_update(991302001, chat_id))
        self.assertFalse(os.path.exists(temp_path))
        self.assertEqual(self._sent_texts(), [voice_input.TRANSCRIBE_FAILED_MSG])

    def test_transcript_echoed_before_normal_response(self):
        chat_id = 991303
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/fake2.oga"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="Що є вдома?"):
                with patch("os.remove"):
                    with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
                        with patch.object(bot, "get_inventory_items", return_value=[]):
                            _call_webhook(_make_voice_update(991303001, chat_id))
        texts = self._sent_texts()
        self.assertTrue(any("🎙️ Розпізнав:" in t and "«Що є вдома?»" in t for t in texts))


# =========================
# 3/4/6/7. Transcript reuses the exact same routes typed text already uses.
# =========================
class TestVoiceTranscriptReusesTextRoutes(VoiceWebhookTestCase):
    # 3. Read-only inventory overview question.
    def test_inventory_overview_question_routes_like_typed_text(self):
        chat_id = 991310
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f.oga"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="Що є вдома?"):
                with patch("os.remove"):
                    with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
                        with patch.object(bot, "get_inventory_items", return_value=[]) as mock_inv:
                            _call_webhook(_make_voice_update(991310001, chat_id))
        mock_inv.assert_called_once()

    # 4. A household-shaped add command creates the SAME preview a typed
    # command would (never writes before confirm) — same mocking pattern as
    # test_inventory_transform.TestExistingRoutesStillWinOverTransform.
    # test_bought_milk_still_works_via_household_router, just fed through
    # voice instead of typed text.
    def test_add_command_creates_same_preview_as_typed_text(self):
        chat_id = 991311
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f2.oga"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="Купив молоко і хліб"):
                with patch("os.remove"):
                    with patch.object(bot, "get_household_alias_map", return_value={}):
                        with patch.object(bot, "get_active_shopping_items", return_value=[]):
                            with patch.object(bot, "get_inventory_items", return_value=[]):
                                with patch.object(bot, "get_recent_expenses_for_deletion", return_value=[]):
                                    with patch("household_router._ask_gemini_household_router") as mock_gemini:
                                        mock_gemini.return_value = {
                                            "intent": "household_operations",
                                            "operations": [
                                                {"type": "add_inventory", "name": "Молоко", "quantity_text": "",
                                                 "category": "Молочне та яйця"},
                                            ],
                                            "unresolved_fragments": [],
                                        }
                                        with patch.object(bot, "apply_global_household_operations") as mock_db_write:
                                            _call_webhook(_make_voice_update(991311001, chat_id))
        self.assertIn(chat_id, pending_global_household)
        mock_db_write.assert_not_called()

    # 6. "Видали все" is caught by the existing destructive guard, not a
    # direct DB write.
    def test_destructive_command_routes_to_existing_guard(self):
        chat_id = 991312
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f3.oga"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="Видали все"):
                with patch("os.remove"):
                    with patch.object(bot, "call_gemini") as mock_gemini:
                        _call_webhook(_make_voice_update(991312001, chat_id))
        mock_gemini.assert_not_called()
        self.assertIn(chat_id, pending_destructive_guard)

    # 7. A normal, non-household question still reaches general AI fallback.
    def test_unrelated_question_reaches_general_ai(self):
        chat_id = 991313
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f4.oga"):
            with patch.object(
                voice_input, "transcribe_audio_file",
                return_value="Поясни коротко, чому молоко згортається в каві?",
            ):
                with patch("os.remove"):
                    with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн.") as mock_gemini:
                        _call_webhook(_make_voice_update(991313001, chat_id))
        # Unified Mini Action Planner V1's pre-gate rejects this text (an
        # explanatory question mentioning a product, not a household
        # action) before ever calling Gemini — only general AI-chat's own
        # single call happens.
        mock_gemini.assert_called_once()
        self.assertTrue(any("Бо це білок казеїн." == t for t in self._sent_texts()))


# =========================
# 5. Voice during an active pending_inventory_transform preview goes
# through Preview Edit V1, never general AI, never a DB write.
# =========================
class TestVoiceDuringActivePreview(VoiceWebhookTestCase):
    def _seed_transform_preview(self, chat_id):
        from decimal import Decimal
        pending_inventory_transform[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "source_item_ids": [50, 60],
            "targets": [
                {"item_id": 50, "name": "Сосиски", "quantity_value": Decimal("6"), "quantity_unit": "шт.",
                 "canonical_name": "сосиски", "category": "М'ясо та риба"},
                {"item_id": 60, "name": "Мисливські ковбаски", "quantity_value": Decimal("2"), "quantity_unit": "шт.",
                 "canonical_name": "мисливські ковбаски", "category": "М'ясо та риба"},
            ],
            "target_name": "М'ясні вироби", "target_canonical_name": "м'ясні вироби",
            "target_category": "М'ясо та риба",
            "target_quantity_value": Decimal("8"), "target_quantity_unit": "шт.",
            "target_quantity_text": "8 шт.",
        }

    def test_voice_edit_updates_transform_preview_no_general_ai(self):
        chat_id = 991320
        self._seed_transform_preview(chat_id)
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f5.oga"):
            with patch.object(
                voice_input, "transcribe_audio_file",
                return_value="так, тільки зроби М'ясні вироби 2 шт",
            ):
                with patch("os.remove"):
                    with patch.object(bot, "call_gemini") as mock_gemini:
                        with patch.object(bot, "execute_inventory_transform") as mock_db_write:
                            _call_webhook(_make_voice_update(991320001, chat_id))
        mock_gemini.assert_not_called()
        mock_db_write.assert_not_called()
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(entry["target_quantity_value"], 2)
        self.assertEqual(entry["target_quantity_unit"], "шт.")


# =========================
# 8/9. Duration guard and transcription-failure guard never call the
# dispatcher and never call the transcription provider unnecessarily.
# =========================
class TestVoiceErrorGuards(VoiceWebhookTestCase):
    # 8. Too-long voice: controlled error, no transcription call at all.
    def test_too_long_voice_never_calls_transcription(self):
        chat_id = 991330
        with patch.object(voice_input, "transcribe_audio_file") as mock_transcribe:
            with patch.object(bot, "_download_telegram_voice_to_temp") as mock_download:
                _call_webhook(_make_voice_update(991330001, chat_id, duration=999))
        mock_transcribe.assert_not_called()
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [bot.VOICE_TOO_LONG_MSG])

    # 9. Failed transcription: controlled Ukrainian error, no dispatcher call
    # (nothing household/AI-shaped gets triggered).
    def test_failed_transcription_sends_controlled_error_no_dispatch(self):
        chat_id = 991331
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f6.oga"):
            with patch.object(
                voice_input, "transcribe_audio_file",
                side_effect=voice_input.VoiceInputError(voice_input.TRANSCRIBE_FAILED_MSG),
            ):
                with patch("os.remove"):
                    with patch.object(bot, "call_gemini") as mock_gemini:
                        _call_webhook(_make_voice_update(991331001, chat_id))
        mock_gemini.assert_not_called()
        self.assertEqual(self._sent_texts(), [voice_input.TRANSCRIBE_FAILED_MSG])

    def test_empty_transcript_sends_controlled_error_no_dispatch(self):
        chat_id = 991332
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f7.oga"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="   "):
                with patch("os.remove"):
                    with patch.object(bot, "call_gemini") as mock_gemini:
                        _call_webhook(_make_voice_update(991332001, chat_id))
        mock_gemini.assert_not_called()
        self.assertEqual(self._sent_texts(), [voice_input.TRANSCRIBE_FAILED_MSG])

    def test_download_failure_sends_controlled_error_no_transcription_call(self):
        chat_id = 991333
        with patch.object(bot, "_download_telegram_voice_to_temp", side_effect=RuntimeError("network down")):
            with patch.object(voice_input, "transcribe_audio_file") as mock_transcribe:
                _call_webhook(_make_voice_update(991333001, chat_id))
        mock_transcribe.assert_not_called()
        self.assertEqual(self._sent_texts(), [bot.VOICE_DOWNLOAD_FAILED_MSG])

    def test_voice_disabled_sends_controlled_error_before_download(self):
        chat_id = 991334
        with patch.object(voice_input, "VOICE_INPUT_ENABLED", False):
            with patch.object(bot, "_download_telegram_voice_to_temp") as mock_download:
                _call_webhook(_make_voice_update(991334001, chat_id))
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [voice_input.VOICE_DISABLED_MSG])

    def test_missing_api_key_sends_controlled_error_before_download(self):
        chat_id = 991335
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            with patch.object(bot, "_download_telegram_voice_to_temp") as mock_download:
                _call_webhook(_make_voice_update(991335001, chat_id))
        mock_download.assert_not_called()
        self.assertEqual(self._sent_texts(), [voice_input.MISSING_API_KEY_MSG])

    def test_access_check_blocks_voice_before_any_download(self):
        chat_id = 991336
        with patch.dict(bot.__dict__, {"ALLOWED_USER_IDS": {123456}}):
            with patch.object(bot, "_download_telegram_voice_to_temp") as mock_download:
                _call_webhook(_make_voice_update(991336001, chat_id, user_id=999))
        mock_download.assert_not_called()


# =========================
# Low-level download helper: getFile + file download, both via requests.
# =========================
def _fake_get_file_response(file_path):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": True, "result": {"file_path": file_path}}
    return resp


def _fake_file_response(content):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.content = content
    return resp


class TestDownloadTelegramVoiceToTemp(unittest.TestCase):
    def test_downloads_via_get_file_then_file_endpoint_and_writes_bytes(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[_fake_get_file_response("voice/file_0.oga"), _fake_file_response(b"fake-ogg-bytes")],
        ) as mock_get:
            temp_path = bot._download_telegram_voice_to_temp("abc123")
        try:
            self.assertTrue(os.path.exists(temp_path))
            with open(temp_path, "rb") as f:
                self.assertEqual(f.read(), b"fake-ogg-bytes")
            self.assertEqual(mock_get.call_count, 2)
            first_call_kwargs = mock_get.call_args_list[0].kwargs
            self.assertEqual(first_call_kwargs.get("params"), {"file_id": "abc123"})
        finally:
            os.remove(temp_path)

    # 1. Telegram file_path with .oga is normalized to .ogg (the likely
    # live-bug fix: Groq's upload validation didn't recognize ".oga").
    def test_oga_extension_is_normalized_to_ogg(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[_fake_get_file_response("voice/file_0.oga"), _fake_file_response(b"bytes")],
        ):
            temp_path = bot._download_telegram_voice_to_temp("abc123")
        try:
            self.assertTrue(temp_path.endswith(".ogg"), temp_path)
        finally:
            os.remove(temp_path)

    # An already-".ogg" file_path is preserved as-is.
    def test_ogg_extension_is_preserved(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[_fake_get_file_response("voice/file_0.ogg"), _fake_file_response(b"bytes")],
        ):
            temp_path = bot._download_telegram_voice_to_temp("abc123")
        try:
            self.assertTrue(temp_path.endswith(".ogg"), temp_path)
        finally:
            os.remove(temp_path)

    # 2. A file_path with no extension at all defaults to .ogg.
    def test_missing_extension_defaults_to_ogg(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[_fake_get_file_response("voice/file_0"), _fake_file_response(b"bytes")],
        ):
            temp_path = bot._download_telegram_voice_to_temp("abc123")
        try:
            self.assertTrue(temp_path.endswith(".ogg"), temp_path)
        finally:
            os.remove(temp_path)

    # A recognized non-ogg extension (e.g. a forwarded audio file) is kept
    # unchanged, never forced to .ogg.
    def test_other_known_extension_is_preserved(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[_fake_get_file_response("voice/file_0.mp3"), _fake_file_response(b"bytes")],
        ):
            temp_path = bot._download_telegram_voice_to_temp("abc123")
        try:
            self.assertTrue(temp_path.endswith(".mp3"), temp_path)
        finally:
            os.remove(temp_path)

    # 3. A 0-byte downloaded body is treated as a download failure, never
    # silently handed to the transcription provider.
    def test_empty_download_body_raises(self):
        with patch.object(
            bot.requests, "get",
            side_effect=[_fake_get_file_response("voice/file_0.oga"), _fake_file_response(b"")],
        ):
            with self.assertRaises(Exception):
                bot._download_telegram_voice_to_temp("abc123")

    def test_get_file_success_and_download_success_are_logged_without_secrets(self):
        with self.assertLogs(bot.logger, level="INFO") as log_ctx:
            with patch.object(
                bot.requests, "get",
                side_effect=[_fake_get_file_response("voice/file_0.oga"), _fake_file_response(b"12345")],
            ):
                temp_path = bot._download_telegram_voice_to_temp("abc123")
        try:
            joined = "\n".join(log_ctx.output)
            self.assertIn("voice_get_file_success", joined)
            self.assertIn("voice_download_success", joined)
            self.assertNotIn("test_token", joined)  # TOKEN never logged
            self.assertNotIn("abc123", joined)  # file_id never logged
        finally:
            os.remove(temp_path)


class TestVoiceDownloadFailureLogging(VoiceWebhookTestCase):
    # 10. A download failure logs a sanitized error (no token/URL) and
    # sends the generic controlled message.
    def test_download_error_is_logged_without_leaking_token(self):
        chat_id = 991340
        with self.assertLogs(bot.logger, level="ERROR") as log_ctx:
            with patch.object(bot, "_download_telegram_voice_to_temp", side_effect=RuntimeError(
                f"404 for url: https://api.telegram.org/bot{bot.TOKEN}/getFile?file_id=abc",
            )):
                _call_webhook(_make_voice_update(991340001, chat_id))
        joined = "\n".join(log_ctx.output)
        self.assertIn("voice_download_error", joined)
        self.assertNotIn(bot.TOKEN, joined)
        self.assertEqual(self._sent_texts(), [bot.VOICE_DOWNLOAD_FAILED_MSG])

    # 11. Temp file cleanup still happens on transcription failure, and is
    # itself logged.
    def test_cleanup_logged_on_transcription_failure(self):
        chat_id = 991341
        fd, temp_path = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        with self.assertLogs(bot.logger, level="INFO") as log_ctx:
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value=temp_path):
                with patch.object(
                    voice_input, "transcribe_audio_file",
                    side_effect=voice_input.VoiceInputError(voice_input.TRANSCRIBE_FAILED_MSG),
                ):
                    _call_webhook(_make_voice_update(991341001, chat_id))
        self.assertFalse(os.path.exists(temp_path))
        self.assertTrue(any("voice_temp_cleanup_success" in msg for msg in log_ctx.output))


if __name__ == "__main__":
    unittest.main()
