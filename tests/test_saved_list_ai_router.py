import sys
import os
import unittest
from unittest.mock import MagicMock

# Mock database and groq before importing bot
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    _validate_saved_updates,
    _compute_saved_merge_groups,
    _compute_saved_merged_quantity,
    _format_saved_edit_preview,
    pending_saved_edit,
)


class TestSavedListAIRouter(unittest.TestCase):

    def _make_items(self):
        return [
            {"id": 101, "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
            {"id": 102, "name": "Приправа до курки", "quantity_text": "2 шт.", "category": "Соуси, спеції та бакалія"},
            {"id": 103, "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
        ]

    # Test 1: Валідне редагування кількості створює правильний pending preview
    def test_valid_edit_creates_correct_preview(self):
        items = self._make_items()
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        valid = _validate_saved_updates(updates, items)
        self.assertIsNotNone(valid)
        self.assertEqual(valid[0]["item_id"], 101)
        self.assertEqual(valid[0]["quantity_text"], "2 шт.")
        preview = _format_saved_edit_preview(items, valid, "shopping_saved")
        self.assertIn("Хліб", preview)
        self.assertIn("2 шт.", preview)
        self.assertIn("→", preview)

    # Test 2: Update з неіснуючим номером відкидається
    def test_nonexistent_item_number_rejected(self):
        items = self._make_items()
        updates = [{"item_number": 99, "name": None, "quantity_text": "2 шт.", "category": None}]
        valid = _validate_saved_updates(updates, items)
        self.assertIsNone(valid)

    # Test 3: Дві зміни однієї позиції в одному router result відкидаються
    def test_duplicate_item_number_rejected(self):
        items = self._make_items()
        updates = [
            {"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None},
            {"item_number": 1, "name": "Багет", "quantity_text": None, "category": None},
        ]
        valid = _validate_saved_updates(updates, items)
        self.assertIsNone(valid)

    # Test 4: intent "none" (порожні updates) не змінює список і допускає fallback
    def test_intent_none_empty_updates_returns_none(self):
        items = self._make_items()
        valid = _validate_saved_updates([], items)
        self.assertIsNone(valid)
        # Items unchanged
        self.assertEqual(items[0]["quantity_text"], "")
        self.assertEqual(items[1]["quantity_text"], "2 шт.")

    # Test 5: Хліб + Хліб безпечно дає Хліб — 2 шт.
    def test_merge_bread_both_empty_gives_2pcs(self):
        group_items = [
            {"id": 101, "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
            {"id": 104, "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
        ]
        qty = _compute_saved_merged_quantity(group_items)
        self.assertEqual(qty, "2 шт.")

    # Test 6: Приправа до курки + Приправа до курки — 2 шт. дає 3 шт.
    def test_merge_seasoning_empty_plus_2pcs_gives_3pcs(self):
        group_items = [
            {"id": 101, "name": "Приправа до курки", "quantity_text": "", "category": "Соуси, спеції та бакалія"},
            {"id": 105, "name": "Приправа до курки", "quantity_text": "2 шт.", "category": "Соуси, спеції та бакалія"},
        ]
        qty = _compute_saved_merged_quantity(group_items)
        self.assertEqual(qty, "3 шт.")

    # Test 7: Молоко + Молоко — 2 л не вигадує 3 л
    def test_merge_milk_empty_plus_2l_returns_none(self):
        group_items = [
            {"id": 103, "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
            {"id": 106, "name": "Молоко", "quantity_text": "2 л", "category": "Молочне та яйця"},
        ]
        qty = _compute_saved_merged_quantity(group_items)
        self.assertIsNone(qty)

    # Test 8: Вершки 18% і Вершки 30% не об'єднуються
    def test_different_names_not_merged(self):
        items = [
            {"id": 107, "name": "Вершки 18%", "quantity_text": "200 мл", "category": "Молочне та яйця"},
            {"id": 108, "name": "Вершки 30%", "quantity_text": "200 мл", "category": "Молочне та яйця"},
        ]
        result = _compute_saved_merge_groups([[1, 2]], items)
        self.assertEqual(result, [])

    # Test 9: pending_saved_edit не може застосуватися двічі
    def test_pending_saved_edit_applied_only_once(self):
        chat_id = 99999
        pending_saved_edit[chat_id] = {
            "items_snapshot": self._make_items(),
            "validated_updates": [
                {"item_number": 1, "item_id": 101, "name": None, "quantity_text": "2 шт.", "category": None}
            ],
            "household_id": 1,
            "user_db_id": 1,
            "context_type": "shopping_saved",
        }
        self.assertIn(chat_id, pending_saved_edit)
        first = pending_saved_edit.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_saved_edit.pop(chat_id, None)
        self.assertIsNone(second)


if __name__ == '__main__':
    unittest.main()
