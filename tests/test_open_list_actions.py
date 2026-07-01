import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot to avoid real connections
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    _validate_start_action,
    _ask_gemini_saved_list_router,
)


class TestOpenListActions(unittest.TestCase):

    def _make_items(self):
        return [
            {"id": 301, "name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка"},
            {"id": 302, "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
            {"id": 303, "name": "Сосиски", "quantity_text": "6 шт.", "category": "М'ясо та риба"},
        ]

    # 1. start_action + mark_bought дозволений лише для shopping_saved
    def test_mark_bought_allowed_only_in_shopping_saved(self):
        items = self._make_items()
        self.assertIsNotNone(_validate_start_action("mark_bought", [1], "shopping_saved", items))
        self.assertIsNone(_validate_start_action("mark_bought", [1], "inventory_saved", items))

    # 2. start_action + delete_shopping дозволений лише для shopping_saved
    def test_delete_shopping_allowed_only_in_shopping_saved(self):
        items = self._make_items()
        self.assertIsNotNone(_validate_start_action("delete_shopping", [1], "shopping_saved", items))
        self.assertIsNone(_validate_start_action("delete_shopping", [1], "inventory_saved", items))

    # 3. start_action + remove_inventory дозволений лише для inventory_saved
    def test_remove_inventory_allowed_only_in_inventory_saved(self):
        items = self._make_items()
        self.assertIsNotNone(_validate_start_action("remove_inventory", [1], "inventory_saved", items))
        self.assertIsNone(_validate_start_action("remove_inventory", [1], "shopping_saved", items))

    # 4. Неіснуючий номер відкидається
    def test_nonexistent_number_dropped(self):
        items = self._make_items()
        result = _validate_start_action("mark_bought", [1, 99], "shopping_saved", items)
        self.assertEqual(result, [items[0]])

    # 5. Дублікати номерів прибираються зі збереженням порядку
    def test_duplicate_numbers_removed_order_preserved(self):
        items = self._make_items()
        result = _validate_start_action("remove_inventory", [3, 1, 1, 3], "inventory_saved", items)
        self.assertEqual(result, [items[2], items[0]])

    # 6. Порожній selected_numbers відкидається
    def test_empty_selected_numbers_rejected(self):
        items = self._make_items()
        self.assertIsNone(_validate_start_action("mark_bought", [], "shopping_saved", items))
        self.assertIsNone(_validate_start_action("mark_bought", [999], "shopping_saved", items))

    # 7. intent: none не створює action preview (router fallback carries no actionable data)
    def test_intent_none_yields_no_action(self):
        with patch.object(bot, "call_gemini", return_value=None):
            router_result = _ask_gemini_saved_list_router("яка погода?", self._make_items(), "shopping_saved")
        self.assertEqual(router_result["intent"], "none")
        self.assertIsNone(router_result["action"])
        self.assertEqual(router_result["selected_numbers"], [])
        # Feeding this fallback into validation must never produce an action preview
        self.assertIsNone(_validate_start_action(router_result["action"], router_result["selected_numbers"], "shopping_saved", self._make_items()))

    # 8. start_action не застосовує зміни без підтвердження (validation has zero DB side effects)
    def test_start_action_validation_has_no_side_effects(self):
        items = self._make_items()
        result = _validate_start_action("mark_bought", [1, 2], "shopping_saved", items)
        self.assertIsNotNone(result)
        self.assertFalse(bot.mark_items_batch.called)
        self.assertFalse(bot.delete_items_batch.called)
        self.assertFalse(bot.delete_inventory_items_batch.called)
        self.assertFalse(bot.add_or_merge_inventory_item.called)

    # 9. Валідний start_action дає однозначний результат для перехоплення (не падає у звичайний AI-chat)
    def test_valid_start_action_result_is_actionable_not_none(self):
        items = self._make_items()
        result = _validate_start_action("delete_shopping", [1, 2, 3], "shopping_saved", items)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 3)


if __name__ == '__main__':
    unittest.main()
