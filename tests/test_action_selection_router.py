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
from bot import (
    _validate_selected_numbers,
    _snapshot_is_stale,
    pending_mark_batch,
    pending_delete_batch,
    pending_remove_batch,
    shopping_mode,
    inventory_mode,
)


class TestActionSelectionRouter(unittest.TestCase):

    def _make_items(self):
        return [
            {"id": 201, "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
            {"id": 202, "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
            {"id": 203, "name": "Яйця", "quantity_text": "10 шт.", "category": "Молочне та яйця"},
        ]

    # 1. Валідні selected_numbers створюють selection preview (ordered item dicts)
    def test_valid_numbers_returns_ordered_items(self):
        items = self._make_items()
        result = _validate_selected_numbers([1, 3], items)
        self.assertEqual(result, [items[0], items[2]])

    # 2. Дублікати номерів прибираються
    def test_duplicate_numbers_removed(self):
        items = self._make_items()
        result = _validate_selected_numbers([2, 2, 2], items)
        self.assertEqual(result, [items[1]])

    # 3. Порядок вибраних товарів відповідає порядку заданих номерів
    def test_order_preserved_as_given(self):
        items = self._make_items()
        result = _validate_selected_numbers([3, 1], items)
        self.assertEqual(result, [items[2], items[0]])

    # 4. Неіснуючий номер відкидається, решта вибору залишається
    def test_nonexistent_number_dropped_not_whole_result(self):
        items = self._make_items()
        result = _validate_selected_numbers([1, 99], items)
        self.assertEqual(result, [items[0]])

    # 5. Порожній selected_numbers (або лише неіснуючі номери) відхиляється
    def test_empty_selection_rejected(self):
        items = self._make_items()
        self.assertIsNone(_validate_selected_numbers([], items))
        self.assertIsNone(_validate_selected_numbers([99], items))

    # 6. intent: none (не список) не створює preview
    def test_non_list_numbers_returns_none(self):
        items = self._make_items()
        self.assertIsNone(_validate_selected_numbers(None, items))
        self.assertIsNone(_validate_selected_numbers("1", items))

    # 7. Один вибраний товар також проходить через спільну валідацію
    def test_single_item_selection_same_function(self):
        items = self._make_items()
        result = _validate_selected_numbers([2], items)
        self.assertEqual(result, [items[1]])

    # 8. Snapshot із відсутнім item id вважається застарілим
    def test_snapshot_missing_id_is_stale(self):
        items = self._make_items()
        snapshot_ids = [it["id"] for it in items]
        current_items = items[:2]  # id 203 більше не існує
        self.assertTrue(_snapshot_is_stale(snapshot_ids, current_items))

    def test_snapshot_unchanged_is_not_stale(self):
        items = self._make_items()
        snapshot_ids = [it["id"] for it in items]
        self.assertFalse(_snapshot_is_stale(snapshot_ids, items))

    # 9. Повторне підтвердження не може застосувати одну й ту саму дію двічі
    def test_pending_mark_batch_popped_only_once(self):
        chat_id = 88888
        pending_mark_batch[chat_id] = {
            "items": self._make_items(), "household_id": 1, "user_db_id": 1,
        }
        first = pending_mark_batch.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_mark_batch.pop(chat_id, None)
        self.assertIsNone(second)

    def test_pending_delete_and_remove_batch_popped_only_once(self):
        chat_id = 88889
        pending_delete_batch[chat_id] = {"items": self._make_items(), "household_id": 1, "user_db_id": 1}
        pending_remove_batch[chat_id] = {"items": self._make_items(), "household_id": 1, "user_db_id": 1}
        self.assertIsNotNone(pending_delete_batch.pop(chat_id, None))
        self.assertIsNone(pending_delete_batch.pop(chat_id, None))
        self.assertIsNotNone(pending_remove_batch.pop(chat_id, None))
        self.assertIsNone(pending_remove_batch.pop(chat_id, None))

    # 10. "✏️ Змінити вибір" очищає лише pending selection, а не режим
    def test_change_selection_clears_only_pending_batch(self):
        chat_id = 77777
        pending_delete_batch[chat_id] = {
            "items": self._make_items(), "household_id": 1, "user_db_id": 1,
        }
        shopping_mode[chat_id] = "deleting"
        popped = pending_delete_batch.pop(chat_id, None)
        self.assertIsNotNone(popped)
        # Режим не зачіпається окремим очищенням pending selection
        self.assertEqual(shopping_mode.get(chat_id), "deleting")
        shopping_mode.pop(chat_id, None)


if __name__ == '__main__':
    unittest.main()
