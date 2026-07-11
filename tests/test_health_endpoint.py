"""/health — a lightweight uptime-ping endpoint. Must return 200 without
calling Gemini, Groq, Postgres/Supabase or Telegram."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402


class TestHealthEndpoint(unittest.TestCase):
    def test_health_returns_200(self):
        client = bot.app.test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)

    def test_health_returns_ok_true(self):
        client = bot.app.test_client()
        response = client.get("/health")
        self.assertEqual(response.get_json(), {"ok": True})

    def test_health_calls_no_ai_or_db(self):
        with patch.object(bot, "call_gemini") as mock_gemini, \
             patch.object(bot, "get_household_and_user") as mock_get_user, \
             patch.object(bot, "send_message") as mock_send:
            client = bot.app.test_client()
            client.get("/health")
            mock_gemini.assert_not_called()
            mock_get_user.assert_not_called()
            mock_send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
