"""Voice Input V1 — pure unit tests for voice_input.py. No real Groq call,
no Telegram, no Flask, no temp-file download — see
tests/test_voice_input_routing.py for the webhook-level integration tests
(Telegram voice download + hand-off into message_dispatcher.dispatch)."""
import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import voice_input


class TestEnsureReady(unittest.TestCase):
    def test_disabled_raises_controlled_error(self):
        with patch.object(voice_input, "VOICE_INPUT_ENABLED", False):
            with self.assertRaises(voice_input.VoiceInputError) as ctx:
                voice_input.ensure_ready(api_key="sk-test")
            self.assertEqual(str(ctx.exception), voice_input.VOICE_DISABLED_MSG)

    def test_unknown_provider_raises_controlled_error(self):
        with patch.object(voice_input, "VOICE_TRANSCRIBER", "disabled"):
            with self.assertRaises(voice_input.VoiceInputError) as ctx:
                voice_input.ensure_ready(api_key="sk-test")
            self.assertEqual(str(ctx.exception), voice_input.VOICE_DISABLED_MSG)

    def test_missing_api_key_raises_controlled_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            with self.assertRaises(voice_input.VoiceInputError) as ctx:
                voice_input.ensure_ready(api_key=None)
            self.assertEqual(str(ctx.exception), voice_input.MISSING_API_KEY_MSG)

    def test_ready_when_enabled_groq_and_key_present(self):
        voice_input.ensure_ready(api_key="sk-test")  # must not raise


class TestTranscribeAudioFile(unittest.TestCase):
    def setUp(self):
        fd, self.temp_path = tempfile.mkstemp(suffix=".oga")
        with os.fdopen(fd, "wb") as f:
            f.write(b"fake-ogg-bytes")
        self.addCleanup(lambda: os.path.exists(self.temp_path) and os.remove(self.temp_path))

    def test_disabled_raises_before_touching_groq(self):
        with patch.object(voice_input, "VOICE_INPUT_ENABLED", False):
            with patch.object(voice_input, "Groq") as mock_groq_cls:
                with self.assertRaises(voice_input.VoiceInputError) as ctx:
                    voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
                self.assertEqual(str(ctx.exception), voice_input.VOICE_DISABLED_MSG)
            mock_groq_cls.assert_not_called()

    def test_missing_api_key_raises_before_touching_groq(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            with patch.object(voice_input, "Groq") as mock_groq_cls:
                with self.assertRaises(voice_input.VoiceInputError) as ctx:
                    voice_input.transcribe_audio_file(self.temp_path, api_key=None)
                self.assertEqual(str(ctx.exception), voice_input.MISSING_API_KEY_MSG)
            mock_groq_cls.assert_not_called()

    def test_success_returns_cleaned_transcript(self):
        fake_response = MagicMock()
        fake_response.text = "  додай молоко і хліб до покупок  "
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "Groq", return_value=mock_client) as mock_groq_cls:
            result = voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertEqual(result, "додай молоко і хліб до покупок")
        mock_groq_cls.assert_called_once_with(api_key="sk-test")
        _, kwargs = mock_client.audio.transcriptions.create.call_args
        self.assertEqual(kwargs["model"], voice_input.VOICE_TRANSCRIBER_MODEL)
        self.assertIn("prompt", kwargs)
        self.assertNotIn("language", kwargs)  # VOICE_LANGUAGE unset by default

    def test_success_with_plain_string_response(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = "Що є вдома?"
        with patch.object(voice_input, "Groq", return_value=mock_client):
            result = voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertEqual(result, "Що є вдома?")

    def test_empty_transcript_returns_empty_string_not_error(self):
        fake_response = MagicMock()
        fake_response.text = "   "
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "Groq", return_value=mock_client):
            result = voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertEqual(result, "")

    def test_provider_error_raises_controlled_message_not_raw_exception(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = RuntimeError("groq 500: internal error, key=sk-secret")
        with patch.object(voice_input, "Groq", return_value=mock_client):
            with self.assertRaises(voice_input.VoiceInputError) as ctx:
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertEqual(str(ctx.exception), voice_input.TRANSCRIBE_FAILED_MSG)
        self.assertNotIn("sk-secret", str(ctx.exception))

    def test_language_env_var_forwarded_when_set(self):
        fake_response = MagicMock()
        fake_response.text = "hello"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "VOICE_LANGUAGE", "uk"):
            with patch.object(voice_input, "Groq", return_value=mock_client):
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        _, kwargs = mock_client.audio.transcriptions.create.call_args
        self.assertEqual(kwargs["language"], "uk")

    # Language Settings V1 — the `language` kwarg (per-user selected
    # language) is what actually reaches Groq, taking priority over the
    # process-wide VOICE_LANGUAGE env default.
    def test_language_param_overrides_env_var(self):
        fake_response = MagicMock()
        fake_response.text = "hello"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "VOICE_LANGUAGE", "en"):
            with patch.object(voice_input, "Groq", return_value=mock_client):
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test", language="pl")
        _, kwargs = mock_client.audio.transcriptions.create.call_args
        self.assertEqual(kwargs["language"], "pl")

    def test_language_param_used_when_env_unset(self):
        fake_response = MagicMock()
        fake_response.text = "cześć"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "VOICE_LANGUAGE", None):
            with patch.object(voice_input, "Groq", return_value=mock_client):
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test", language="pl")
        _, kwargs = mock_client.audio.transcriptions.create.call_args
        self.assertEqual(kwargs["language"], "pl")

    # If Groq rejects/fails the language-hinted request, transcribe_audio_file
    # retries ONCE with no language hint (auto-detect) before giving up — a
    # rejected language code must never surface as a silent/uncaught failure.
    def test_failed_language_hinted_request_retries_once_without_language(self):
        fake_response = MagicMock()
        fake_response.text = "auto-detected text"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = [
            RuntimeError("groq 400: invalid language code"),
            fake_response,
        ]
        with patch.object(voice_input, "Groq", return_value=mock_client):
            result = voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test", language="uk")
        self.assertEqual(result, "auto-detected text")
        self.assertEqual(mock_client.audio.transcriptions.create.call_count, 2)
        first_kwargs = mock_client.audio.transcriptions.create.call_args_list[0].kwargs
        second_kwargs = mock_client.audio.transcriptions.create.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["language"], "uk")
        self.assertNotIn("language", second_kwargs)

    # If BOTH the language-hinted attempt and the auto-detect retry fail,
    # the existing controlled TRANSCRIBE_FAILED_MSG is still raised —
    # exactly the same failure contract as before Language Settings V1.
    def test_language_hinted_request_final_failure_after_retry_raises_controlled_error(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = RuntimeError("groq 500: internal error")
        with patch.object(voice_input, "Groq", return_value=mock_client):
            with self.assertRaises(voice_input.VoiceInputError) as ctx:
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test", language="ru")
        self.assertEqual(str(ctx.exception), voice_input.TRANSCRIBE_FAILED_MSG)
        self.assertEqual(mock_client.audio.transcriptions.create.call_count, 2)

    # No language hint at all (both `language` param and VOICE_LANGUAGE env
    # unset) means there is nothing to retry without — a single failure
    # raises immediately, exactly like the pre-Language-Settings-V1 behavior.
    def test_no_language_hint_never_retries(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = RuntimeError("network error")
        with patch.object(voice_input, "VOICE_LANGUAGE", None):
            with patch.object(voice_input, "Groq", return_value=mock_client):
                with self.assertRaises(voice_input.VoiceInputError):
                    voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertEqual(mock_client.audio.transcriptions.create.call_count, 1)

    def test_language_retry_is_logged_without_secrets(self):
        fake_response = MagicMock()
        fake_response.text = "ok"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = [
            RuntimeError("groq 400: bad language, key=sk-secret"),
            fake_response,
        ]
        with patch.object(voice_input, "Groq", return_value=mock_client):
            with self.assertLogs(voice_input.logger, level="WARNING") as log_ctx:
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-secret", language="uk")
        joined = "\n".join(log_ctx.output)
        self.assertIn("voice_transcription_language_retry", joined)
        self.assertNotIn("sk-secret", joined)

    # 4. Groq receives a filename ending in the SAME suffix as the local
    # file (bot.py's own _normalize_voice_suffix already turned Telegram's
    # ".oga" into ".ogg" before this function ever runs — this only proves
    # transcribe_audio_file passes that suffix straight through, never
    # drops or rewrites it).
    def test_groq_receives_filename_with_expected_suffix(self):
        fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
        with os.fdopen(fd, "wb") as f:
            f.write(b"fake-ogg-bytes")
        self.addCleanup(lambda: os.path.exists(ogg_path) and os.remove(ogg_path))

        fake_response = MagicMock()
        fake_response.text = "Що є вдома?"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "Groq", return_value=mock_client):
            voice_input.transcribe_audio_file(ogg_path, api_key="sk-test")
        _, kwargs = mock_client.audio.transcriptions.create.call_args
        sent_filename, sent_bytes = kwargs["file"]
        self.assertTrue(sent_filename.endswith(".ogg"), sent_filename)
        self.assertEqual(sent_bytes, b"fake-ogg-bytes")

    def test_groq_receives_response_format_json_and_temperature_zero(self):
        fake_response = MagicMock()
        fake_response.text = "hello"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "Groq", return_value=mock_client):
            voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        _, kwargs = mock_client.audio.transcriptions.create.call_args
        self.assertEqual(kwargs["response_format"], "json")
        self.assertEqual(kwargs["temperature"], 0)

    # 6. A dict response shaped like {"text": "..."} is accepted.
    def test_dict_response_with_text_key_is_accepted(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = {"text": "додай хліб"}
        with patch.object(voice_input, "Groq", return_value=mock_client):
            result = voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertEqual(result, "додай хліб")

    def test_unsupported_response_shape_returns_empty_and_logs_warning(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = 12345  # not str/dict/has-no-.text object
        with patch.object(voice_input, "Groq", return_value=mock_client):
            with self.assertLogs(voice_input.logger, level="WARNING") as log_ctx:
                result = voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertEqual(result, "")
        self.assertTrue(any("unsupported response shape" in msg for msg in log_ctx.output))

    # 5/8/10. Structured, sanitized diagnostics — no secrets in any log line.
    def test_success_logs_start_and_success_without_leaking_transcript(self):
        fake_response = MagicMock()
        fake_response.text = "додай молоко"
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "Groq", return_value=mock_client):
            with self.assertLogs(voice_input.logger, level="INFO") as log_ctx:
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        joined = "\n".join(log_ctx.output)
        self.assertIn("voice_transcription_start", joined)
        self.assertIn("voice_transcription_success", joined)
        self.assertNotIn("додай молоко", joined)  # transcript content itself never logged
        self.assertNotIn("sk-test", joined)  # api key never appears in logs

    def test_empty_transcript_logs_empty_event(self):
        fake_response = MagicMock()
        fake_response.text = "   "
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = fake_response
        with patch.object(voice_input, "Groq", return_value=mock_client):
            with self.assertLogs(voice_input.logger, level="INFO") as log_ctx:
                voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        self.assertTrue(any("voice_transcription_empty" in msg for msg in log_ctx.output))

    def test_provider_error_logs_sanitized_exception_no_api_key(self):
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = RuntimeError(
            "groq 401: invalid_api_key key=sk-test",
        )
        with patch.object(voice_input, "Groq", return_value=mock_client):
            with self.assertLogs(voice_input.logger, level="ERROR") as log_ctx:
                with self.assertRaises(voice_input.VoiceInputError):
                    voice_input.transcribe_audio_file(self.temp_path, api_key="sk-test")
        joined = "\n".join(log_ctx.output)
        self.assertIn("voice_transcription_error", joined)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("sk-test", joined)


class TestEnvDefaults(unittest.TestCase):
    """Reload the module with a clean environment to verify the documented
    safe defaults (VOICE_INPUT_ENABLED=true, VOICE_TRANSCRIBER=groq,
    VOICE_TRANSCRIBER_MODEL=whisper-large-v3-turbo, VOICE_SHOW_TRANSCRIPT=
    true, VOICE_MAX_SECONDS=60) apply when the optional env vars are
    entirely absent — never a hard failure at import time."""

    def test_defaults_when_env_vars_absent(self):
        env_keys = [
            "VOICE_INPUT_ENABLED", "VOICE_TRANSCRIBER", "VOICE_TRANSCRIBER_MODEL",
            "VOICE_SHOW_TRANSCRIPT", "VOICE_MAX_SECONDS", "VOICE_LANGUAGE",
        ]
        saved = {k: os.environ.pop(k, None) for k in env_keys}
        try:
            reloaded = importlib.reload(voice_input)
            self.assertTrue(reloaded.VOICE_INPUT_ENABLED)
            self.assertEqual(reloaded.VOICE_TRANSCRIBER, "groq")
            self.assertEqual(reloaded.VOICE_TRANSCRIBER_MODEL, "whisper-large-v3-turbo")
            self.assertTrue(reloaded.VOICE_SHOW_TRANSCRIPT)
            self.assertEqual(reloaded.VOICE_MAX_SECONDS, 60)
            self.assertIsNone(reloaded.VOICE_LANGUAGE)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(voice_input)


if __name__ == "__main__":
    unittest.main()
