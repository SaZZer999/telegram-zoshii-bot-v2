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
    _validate_quick_add_items,
    _ask_gemini_saved_list_router,
    pending_quick_purchase,
)


class TestEmptyListQuickPurchase(unittest.TestCase):

    def _bread_raw(self):
        return {"name": "Хліб", "canonical_name": "хліб", "quantity_value": None, "quantity_unit": None,
                "quantity_inferred": True, "category": "Хліб і випічка", "is_consumable": True}

    def _milk_raw(self):
        return {"name": "Молоко", "canonical_name": "молоко", "quantity_value": 2, "quantity_unit": "л",
                "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True}

    def _batteries_raw(self):
        return {"name": "Батарейки", "canonical_name": "батарейки", "quantity_value": None, "quantity_unit": None,
                "quantity_inferred": False, "category": "Інше їстівне", "is_consumable": False}

    # 1. Порожній список покупок може створити quick purchase preview
    def test_empty_shopping_list_can_create_preview(self):
        result = _validate_quick_add_items([self._bread_raw(), self._milk_raw()])
        self.assertIsNotNone(result)
        items, ignored = result
        self.assertEqual(len(items), 2)
        self.assertEqual(ignored, [])

    # 2. Quick purchase preview не створює записів у покупках
    def test_preview_does_not_touch_shopping_records(self):
        _validate_quick_add_items([self._bread_raw()])
        self.assertFalse(bot.add_shopping_items_batch.called)

    # 3. Quick purchase preview не змінює запаси до підтвердження
    def test_preview_does_not_touch_inventory_before_confirmation(self):
        _validate_quick_add_items([self._bread_raw()])
        self.assertFalse(bot.add_inventory_items_batch.called)
        self.assertFalse(bot.add_or_merge_inventory_item.called)

    # 4. Новий "Хліб" без кількості має "1 шт." inferred
    def test_bread_without_quantity_defaults_to_1_piece_inferred(self):
        items, _ = _validate_quick_add_items([self._bread_raw()])
        self.assertEqual(items[0]["quantity_value"], 1.0)
        self.assertEqual(items[0]["quantity_unit"], "шт.")
        self.assertTrue(items[0]["quantity_inferred"])
        self.assertEqual(items[0]["quantity_text"], "1 шт.")

    # 5. "Молоко 2 л" зберігає "2 л"
    def test_milk_2l_keeps_explicit_quantity(self):
        items, _ = _validate_quick_add_items([self._milk_raw()])
        self.assertEqual(items[0]["quantity_value"], 2.0)
        self.assertEqual(items[0]["quantity_unit"], "л")
        self.assertFalse(items[0]["quantity_inferred"])
        self.assertEqual(items[0]["quantity_text"], "2 л")

    # 6. Неїстівний товар відкидається
    def test_non_consumable_item_dropped(self):
        items, ignored = _validate_quick_add_items([self._bread_raw(), self._batteries_raw()])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Хліб")
        self.assertEqual(ignored, ["Батарейки"])

    # 6b. Якщо всі товари неїстівні — preview не створюється
    def test_all_non_consumable_yields_none(self):
        self.assertIsNone(_validate_quick_add_items([self._batteries_raw()]))

    # 7. intent: none не створює preview
    def test_intent_none_creates_no_preview(self):
        with patch.object(bot, "call_gemini", return_value=None):
            router_result = _ask_gemini_saved_list_router("Я люблю хліб.", [], "shopping_saved")
        self.assertEqual(router_result["intent"], "none")
        self.assertEqual(router_result["items"], [])
        self.assertIsNone(_validate_quick_add_items(router_result["items"]))

    # 8. Повторне підтвердження не застосовує додавання двічі
    def test_repeated_confirmation_does_not_apply_twice(self):
        chat_id = 55555
        pending_quick_purchase[chat_id] = {
            "items": [self._bread_raw()],
            "ignored_items": [],
            "household_id": 1,
            "user_db_id": 1,
        }
        first = pending_quick_purchase.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_quick_purchase.pop(chat_id, None)
        self.assertIsNone(second)


if __name__ == '__main__':
    unittest.main()
