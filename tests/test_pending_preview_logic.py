import sys
import os
import unittest
from unittest.mock import MagicMock

# Mock database and groq before importing bot to avoid real connections
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import _validate_preview_updates, _apply_preview_updates


class TestPendingPreviewLogic(unittest.TestCase):

    def _make_items(self):
        return [
            {"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка", "was_corrected": False},
            {"name": "Приправа до курки", "quantity_text": "2 шт.", "category": "Соуси, спеції та бакалія", "was_corrected": False},
        ]

    def test_valid_update_adds_quantity_to_bread(self):
        """Хліб без кількості → Хліб — 2 шт."""
        items = self._make_items()
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        valid = _validate_preview_updates(updates, items)
        self.assertIsNotNone(valid)
        result = _apply_preview_updates(items, valid)
        self.assertEqual(result[0]["name"], "Хліб")
        self.assertEqual(result[0]["quantity_text"], "2 шт.")

    def test_valid_update_changes_seasoning_quantity(self):
        """Приправа до курки — 2 шт. → 3 шт."""
        items = self._make_items()
        updates = [{"item_number": 2, "name": None, "quantity_text": "3 шт.", "category": None}]
        valid = _validate_preview_updates(updates, items)
        self.assertIsNotNone(valid)
        result = _apply_preview_updates(items, valid)
        self.assertEqual(result[1]["quantity_text"], "3 шт.")
        self.assertEqual(result[1]["name"], "Приправа до курки")

    def test_nonexistent_item_number_rejected(self):
        """item_number поза межами списку → validate повертає None."""
        items = self._make_items()
        updates = [{"item_number": 99, "name": None, "quantity_text": "2 шт.", "category": None}]
        valid = _validate_preview_updates(updates, items)
        self.assertIsNone(valid)

    def test_duplicate_item_number_in_updates_rejected(self):
        """Два різні update для одного item_number → validate повертає None."""
        items = self._make_items()
        updates = [
            {"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None},
            {"item_number": 1, "name": "Багет", "quantity_text": None, "category": None},
        ]
        valid = _validate_preview_updates(updates, items)
        self.assertIsNone(valid)

    def test_apply_updates_does_not_mutate_original(self):
        """Preview редагується лише в пам'яті — оригінальний список не змінюється."""
        items = self._make_items()
        updates = [{"item_number": 1, "name": "Чіабата", "quantity_text": "1 шт.", "category": None}]
        valid = _validate_preview_updates(updates, items)
        self.assertIsNotNone(valid)
        result = _apply_preview_updates(items, valid)
        # Оригінал не змінено
        self.assertEqual(items[0]["name"], "Хліб")
        self.assertEqual(items[0]["quantity_text"], "")
        # Результат оновлено
        self.assertEqual(result[0]["name"], "Чіабата")
        self.assertEqual(result[0]["quantity_text"], "1 шт.")

    def test_intent_none_empty_updates_does_not_change_preview(self):
        """intent: 'none' → порожній updates → validate None → preview без змін."""
        items = self._make_items()
        valid = _validate_preview_updates([], items)
        self.assertIsNone(valid)
        # Items залишаються незмінними
        self.assertEqual(items[0]["quantity_text"], "")
        self.assertEqual(items[1]["quantity_text"], "2 шт.")


if __name__ == '__main__':
    unittest.main()
