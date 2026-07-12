"""Language Settings V1 — per-user voice-transcription language.

Two layers, mirroring the project's usual split:
- TestGetSetUserVoiceLanguage: pure DB-layer tests against the REAL
  database.py (loaded fresh, independent of sys.modules['database'] being
  mocked elsewhere in this suite), with a fake cursor/connection standing
  in for Postgres — same pattern as tests/test_household_aliases.py.
- Everything else: webhook-level integration tests (bot.webhook() ->
  message_dispatcher.dispatch(...) / bot._handle_voice_message), same
  pattern as tests/test_message_dispatcher_command_routes.py and
  tests/test_voice_input_routing.py. `database` is mocked wholesale there,
  so bot.get_user_voice_language/set_user_voice_language are patched
  per-test as needed.
"""
import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, same technique as test_household_aliases.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_language_tests", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

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


class FakeCursor:
    def __init__(self, fetchone_results=None):
        self.queries = []
        self._fetchone_results = list(fetchone_results or [])

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


# =========================
# DB layer — real database.py, fake connection.
# =========================
class TestGetSetUserVoiceLanguage(unittest.TestCase):
    def test_get_returns_stored_language(self):
        cur = FakeCursor(fetchone_results=[("pl",)])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_user_voice_language(555)
        self.assertEqual(result, "pl")
        sql, params = cur.queries[0]
        self.assertIn("WHERE telegram_user_id = %s", sql)
        self.assertEqual(params, (555,))

    def test_get_returns_none_when_no_row(self):
        cur = FakeCursor(fetchone_results=[None])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_user_voice_language(555)
        self.assertIsNone(result)

    def test_get_returns_none_when_column_is_null(self):
        cur = FakeCursor(fetchone_results=[(None,)])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_user_voice_language(555)
        self.assertIsNone(result)

    def test_set_valid_language_updates_and_commits(self):
        cur = FakeCursor()
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.set_user_voice_language(555, "uk")
        sql, params = cur.queries[0]
        self.assertIn("UPDATE users SET voice_language", sql)
        self.assertEqual(params, ("uk", 555))
        self.assertTrue(conn.committed)

    def test_set_rejects_invalid_language_without_touching_db(self):
        with patch.object(real_database, "get_connection") as mock_conn:
            with self.assertRaises(ValueError):
                real_database.set_user_voice_language(555, "de")
        mock_conn.assert_not_called()

    def test_all_four_supported_languages_are_valid(self):
        for code in ("uk", "pl", "en", "ru"):
            cur = FakeCursor()
            conn = FakeConnection(cur)
            with patch.object(real_database, "get_connection", return_value=conn):
                real_database.set_user_voice_language(555, code)
            self.assertTrue(conn.committed)


# =========================
# Webhook/menu layer.
# =========================
def _make_update(chat_id, text, user_id=555, update_id=None):
    return {
        "update_id": update_id if update_id is not None else chat_id * 1000,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _make_voice_update(chat_id, user_id=555, update_id=None, file_id="voice_1", duration=5):
    return {
        "update_id": update_id if update_id is not None else chat_id * 1000,
        "message": {
            "chat": {"id": chat_id},
            "voice": {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg"},
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class LanguageSettingsTestCase(unittest.TestCase):
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

    def _sent(self):
        return [(call.args[0], call.args[1], call.kwargs.get("reply_markup")) for call in self.mock_send.call_args_list]


class TestSettingsMenuOpens(LanguageSettingsTestCase):
    def test_settings_button_shows_settings_keyboard(self):
        chat_id = 881001
        _call_webhook(_make_update(chat_id, "⚙️ Налаштування"))
        sent = self._sent()
        self.assertEqual(len(sent), 1)
        _, _, keyboard = sent[0]
        self.assertEqual(keyboard, bot.SETTINGS_KEYBOARD)

    def test_language_button_shows_language_keyboard(self):
        chat_id = 881002
        with patch.object(bot, "get_user_voice_language", return_value=None):
            _call_webhook(_make_update(chat_id, "🌐 Мова"))
        sent = self._sent()
        self.assertEqual(len(sent), 1)
        _, _, keyboard = sent[0]
        self.assertEqual(keyboard, bot.LANGUAGE_KEYBOARD)

    def test_language_menu_shows_current_choice_when_set(self):
        chat_id = 881003
        with patch.object(bot, "get_user_voice_language", return_value="pl"):
            _call_webhook(_make_update(chat_id, "🌐 Мова"))
        texts = [t for _, t, _ in self._sent()]
        self.assertTrue(any("🇵🇱 Polski" in t for t in texts))


class TestSelectingLanguageSavesPreference(LanguageSettingsTestCase):
    def test_each_language_button_saves_correct_code(self):
        cases = [
            ("🇺🇦 Українська", "uk"),
            ("🇵🇱 Polski", "pl"),
            ("🇬🇧 English", "en"),
            ("🇷🇺 Русский", "ru"),
        ]
        for i, (button_text, code) in enumerate(cases):
            chat_id = 882000 + i
            with patch.object(bot, "get_household_and_user", return_value=(1, 10)) as mock_ensure:
                with patch.object(bot, "set_user_voice_language") as mock_set:
                    _call_webhook(_make_update(chat_id, button_text, user_id=chat_id))
            mock_ensure.assert_called_once()
            mock_set.assert_called_once_with(chat_id, code)
            texts = [t for _, t, _ in self._sent()]
            self.assertTrue(any(button_text in t for t in texts))

    def test_save_failure_sends_controlled_error_not_raw_exception(self):
        chat_id = 882100
        with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
            with patch.object(bot, "set_user_voice_language", side_effect=RuntimeError("db down")):
                _call_webhook(_make_update(chat_id, "🇺🇦 Українська", user_id=chat_id))
        texts = [t for _, t, _ in self._sent()]
        self.assertTrue(any("Не вдалося зберегти мову" in t for t in texts))


class TestInvalidLanguageSafelyIgnored(LanguageSettingsTestCase):
    # Free text that merely resembles a language name is not a fixed button
    # label, so it must never reach set_user_voice_language at all — it
    # falls through to the ordinary text-dispatch routes instead.
    def test_arbitrary_text_never_calls_set_language(self):
        chat_id = 882200
        with patch.object(bot, "set_user_voice_language") as mock_set:
            with patch.object(bot, "call_gemini", return_value="ok"):
                _call_webhook(_make_update(chat_id, "українська мова", user_id=chat_id))
        mock_set.assert_not_called()

    # database.py itself rejects out-of-range codes defensively.
    def test_database_layer_rejects_invalid_code(self):
        with self.assertRaises(ValueError):
            real_database.set_user_voice_language(1, "xx")

    # A corrupted/unsupported stored value must never be forwarded to Groq —
    # _resolve_voice_language treats it exactly like "unset".
    def test_resolve_voice_language_ignores_unsupported_stored_value(self):
        with patch.object(bot, "get_user_voice_language", return_value="xx"):
            self.assertIsNone(bot._resolve_voice_language(123))


class TestVoiceTranscriptionUsesSelectedLanguage(LanguageSettingsTestCase):
    # Reuses the exact transcript already proven (test_voice_input_routing.py,
    # test_unrelated_question_reaches_general_ai) to reach general AI
    # fallback with only call_gemini mocked — these tests are about which
    # `language` kwarg reaches transcribe_audio_file, not about downstream
    # routing, so the routing path is kept identical/known-safe throughout.
    _SAFE_TRANSCRIPT = "Поясни коротко, чому молоко згортається в каві?"

    def test_selected_language_is_forwarded_to_transcription(self):
        chat_id = 883001
        with patch.object(bot, "get_user_voice_language", return_value="pl"):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f.oga"):
                with patch.object(
                    voice_input, "transcribe_audio_file", return_value=self._SAFE_TRANSCRIPT,
                ) as mock_transcribe:
                    with patch("os.remove"):
                        with patch.object(bot, "call_gemini", return_value="ok"):
                            _call_webhook(_make_voice_update(chat_id, user_id=chat_id))
        mock_transcribe.assert_called_once_with("/tmp/f.oga", language="pl")

    def test_missing_preference_falls_back_to_no_language_hint(self):
        chat_id = 883002
        with patch.object(bot, "get_user_voice_language", return_value=None):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f2.oga"):
                with patch.object(
                    voice_input, "transcribe_audio_file", return_value=self._SAFE_TRANSCRIPT,
                ) as mock_transcribe:
                    with patch("os.remove"):
                        with patch.object(bot, "call_gemini", return_value="ok"):
                            _call_webhook(_make_voice_update(chat_id, user_id=chat_id))
        mock_transcribe.assert_called_once_with("/tmp/f2.oga")

    def test_unrecognized_stored_language_falls_back_to_no_hint(self):
        chat_id = 883003
        with patch.object(bot, "get_user_voice_language", return_value="de"):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f3.oga"):
                with patch.object(
                    voice_input, "transcribe_audio_file", return_value=self._SAFE_TRANSCRIPT,
                ) as mock_transcribe:
                    with patch("os.remove"):
                        with patch.object(bot, "call_gemini", return_value="ok"):
                            _call_webhook(_make_voice_update(chat_id, user_id=chat_id))
        mock_transcribe.assert_called_once_with("/tmp/f3.oga")

    def test_db_lookup_failure_falls_back_to_no_hint(self):
        chat_id = 883004
        with patch.object(bot, "get_user_voice_language", side_effect=RuntimeError("db down")):
            with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/f4.oga"):
                with patch.object(
                    voice_input, "transcribe_audio_file", return_value=self._SAFE_TRANSCRIPT,
                ) as mock_transcribe:
                    with patch("os.remove"):
                        with patch.object(bot, "call_gemini", return_value="ok"):
                            _call_webhook(_make_voice_update(chat_id, user_id=chat_id))
        mock_transcribe.assert_called_once_with("/tmp/f4.oga")


if __name__ == "__main__":
    unittest.main()
