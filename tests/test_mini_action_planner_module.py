"""Unified Mini Action Planner V1 — pure unit tests for
mini_action_planner.classify()'s Gemini-call + strict-JSON validation.
`bot.call_gemini` is patched with a raw string response (exactly what the
real HTTP call would hand back) so these tests exercise the REAL JSON
parsing/validation path, never a real Gemini/Telegram/DB call."""
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

import bot  # noqa: F401 — import side effect wires mini_action_planner.configure(...)
import mini_action_planner


class TestClassify(unittest.TestCase):
    def test_parses_add_to_shopping(self):
        raw = '{"action":"add_to_shopping","items":[{"name":"Молоко","quantity_text":"1 л"}]}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = mini_action_planner.classify("Додай молоко до покупок")
        self.assertEqual(result["action"], "add_to_shopping")
        self.assertEqual(result["items"], [{"name": "Молоко", "quantity_text": "1 л"}])

    def test_parses_add_to_inventory(self):
        raw = '{"action":"add_to_inventory","items":[{"name":"Сир","quantity_text":"500 г"}]}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = mini_action_planner.classify("Купив сир 500 г")
        self.assertEqual(result["action"], "add_to_inventory")
        self.assertEqual(result["items"], [{"name": "Сир", "quantity_text": "500 г"}])

    def test_parses_ask_inventory_with_empty_items(self):
        raw = '{"action":"ask_inventory","items":[]}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = mini_action_planner.classify("Що є вдома?")
        self.assertEqual(result["action"], "ask_inventory")
        self.assertEqual(result["items"], [])

    def test_parses_meal_ideas(self):
        raw = '{"action":"meal_ideas","items":[]}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = mini_action_planner.classify("Що зробити поїсти")
        self.assertEqual(result["action"], "meal_ideas")

    def test_handles_markdown_fenced_json(self):
        raw = '```json\n{"action":"add_to_shopping","items":[{"name":"Хліб","quantity_text":""}]}\n```'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = mini_action_planner.classify("Хліб закінчився, треба купити")
        self.assertEqual(result["action"], "add_to_shopping")
        self.assertEqual(result["items"], [{"name": "Хліб", "quantity_text": ""}])

    def test_invalid_json_falls_back_to_unknown(self):
        with patch.object(bot, "call_gemini", return_value="це не json взагалі"):
            result = mini_action_planner.classify("щось незрозуміле")
        self.assertEqual(result, {"action": "unknown", "items": []})

    def test_unknown_action_string_falls_back_to_unknown(self):
        raw = '{"action":"delete_everything","items":[]}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = mini_action_planner.classify("видали все")
        self.assertEqual(result, {"action": "unknown", "items": []})

    def test_non_dict_json_falls_back_to_unknown(self):
        with patch.object(bot, "call_gemini", return_value='["add_to_shopping"]'):
            result = mini_action_planner.classify("текст")
        self.assertEqual(result, {"action": "unknown", "items": []})

    def test_non_list_items_coerced_to_empty_list(self):
        raw = '{"action":"add_to_shopping","items":"молоко"}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = mini_action_planner.classify("додай молоко")
        self.assertEqual(result["action"], "add_to_shopping")
        self.assertEqual(result["items"], [])

    def test_gemini_returning_none_falls_back_to_unknown(self):
        with patch.object(bot, "call_gemini", return_value=None):
            result = mini_action_planner.classify("будь-що")
        self.assertEqual(result, {"action": "unknown", "items": []})

    def test_gemini_returning_empty_string_falls_back_to_unknown(self):
        with patch.object(bot, "call_gemini", return_value=""):
            result = mini_action_planner.classify("будь-що")
        self.assertEqual(result, {"action": "unknown", "items": []})

    def test_blank_text_never_calls_gemini(self):
        with patch.object(bot, "call_gemini") as mock_gemini:
            result = mini_action_planner.classify("   ")
        mock_gemini.assert_not_called()
        self.assertEqual(result, {"action": "unknown", "items": []})

    def test_non_string_text_never_calls_gemini(self):
        with patch.object(bot, "call_gemini") as mock_gemini:
            result = mini_action_planner.classify(None)
        mock_gemini.assert_not_called()
        self.assertEqual(result, {"action": "unknown", "items": []})


if __name__ == "__main__":
    unittest.main()
