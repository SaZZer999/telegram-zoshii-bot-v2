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
    _validate_compound_operations,
    _format_compound_preview,
    _compound_snapshot_is_stale,
    pending_compound_inventory,
)


def make_items():
    return [
        {"id": 401, "name": "Вершки", "category": "Молочне та яйця",
         "quantity_value": None, "quantity_unit": None, "quantity_text": ""},
        {"id": 402, "name": "Приправа до курки", "category": "Соуси, спеції та бакалія",
         "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_text": "2 шт."},
        {"id": 403, "name": "Сосиски", "category": "М'ясо та риба",
         "quantity_value": 14.0, "quantity_unit": "шт.", "quantity_text": "14 шт."},
        {"id": 404, "name": "Мисливські ковбаски", "category": "М'ясо та риба",
         "quantity_value": 8.0, "quantity_unit": "шт.", "quantity_text": "8 шт."},
    ]


class TestCompoundPreview(unittest.TestCase):

    # 1. Compound preview містить часткове списання, повне видалення та додавання до покупок
    def test_mixed_operations_produce_full_preview(self):
        items = make_items()
        operations = [
            {"type": "remove_inventory", "item_number": 1},
            {"type": "consume_inventory_quantity", "item_number": 2, "quantity_value": 0.5, "quantity_unit": "шт."},
            {"type": "consume_inventory_quantity", "item_number": 3, "quantity_value": 1, "quantity_unit": "шт."},
            {"type": "consume_inventory_quantity", "item_number": 4, "quantity_value": 0.5, "quantity_unit": "шт."},
            {"type": "add_to_shopping", "name": "Приправа до курки", "quantity_value": 1, "quantity_unit": "шт.",
             "quantity_inferred": False, "category": "Соуси, спеції та бакалія", "is_consumable": True},
        ]
        kind, payload = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "ok")
        changes = payload["inventory_changes"]
        self.assertEqual(len(changes), 4)
        self.assertTrue(changes[0]["will_remove"])
        self.assertEqual(changes[0]["name"], "Вершки")
        self.assertEqual(len(payload["add_to_shopping"]), 1)

        preview = _format_compound_preview(payload)
        self.assertIn("🧊 Буде змінено в запасах:", preview)
        self.assertIn("1. Вершки", preview)
        self.assertIn("буде прибрано із запасів", preview)
        self.assertIn("🛒 Буде додано до покупок:", preview)
        self.assertIn("• Приправа до курки — 1 шт.", preview)

    # 2. 2 шт. - 0,5 шт. = 1,5 шт.
    def test_seasoning_half_consumed(self):
        items = make_items()
        operations = [{"type": "consume_inventory_quantity", "item_number": 2, "quantity_value": 0.5, "quantity_unit": "шт."}]
        kind, payload = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "ok")
        change = payload["inventory_changes"][0]
        self.assertEqual(change["new_value"], 1.5)
        self.assertEqual(change["new_unit"], "шт.")
        self.assertFalse(change["will_remove"])

    # 3. 14 шт. - 1 шт. = 13 шт.
    def test_sausage_one_consumed(self):
        items = make_items()
        operations = [{"type": "consume_inventory_quantity", "item_number": 3, "quantity_value": 1, "quantity_unit": "шт."}]
        kind, payload = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["inventory_changes"][0]["new_value"], 13.0)

    # 4. 8 шт. - 0,5 шт. = 7,5 шт.
    def test_kovbasky_half_consumed(self):
        items = make_items()
        operations = [{"type": "consume_inventory_quantity", "item_number": 4, "quantity_value": 0.5, "quantity_unit": "шт."}]
        kind, payload = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["inventory_changes"][0]["new_value"], 7.5)

    # 5. Одна позиція не може бути і видалена, і частково списана
    def test_item_cannot_be_removed_and_consumed(self):
        items = make_items()
        operations = [
            {"type": "remove_inventory", "item_number": 2},
            {"type": "consume_inventory_quantity", "item_number": 2, "quantity_value": 1, "quantity_unit": "шт."},
        ]
        kind, reasons = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "invalid")
        self.assertTrue(reasons)

    # 6. Одна позиція не може списуватися двічі в одному повідомленні
    def test_item_cannot_be_consumed_twice(self):
        items = make_items()
        operations = [
            {"type": "consume_inventory_quantity", "item_number": 3, "quantity_value": 1, "quantity_unit": "шт."},
            {"type": "consume_inventory_quantity", "item_number": 3, "quantity_value": 2, "quantity_unit": "шт."},
        ]
        kind, reasons = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "invalid")
        self.assertTrue(reasons)

    # 7. Невалідна операція не створює частковий preview
    def test_invalid_operation_blocks_entire_batch(self):
        items = make_items()
        operations = [
            {"type": "remove_inventory", "item_number": 1},
            # Only 2 шт. available, requesting 10 шт. → insufficient
            {"type": "consume_inventory_quantity", "item_number": 2, "quantity_value": 10, "quantity_unit": "шт."},
        ]
        kind, reasons = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "invalid")
        self.assertTrue(any("Приправа" in r for r in reasons))

    # 8. Непорожній unresolved_fragments блокує застосування всіх операцій
    def test_unresolved_fragments_block_everything(self):
        items = make_items()
        operations = [{"type": "remove_inventory", "item_number": 1}]
        kind, fragments = _validate_compound_operations(operations, ["щось незрозуміле"], items)
        self.assertEqual(kind, "unresolved")
        self.assertEqual(fragments, ["щось незрозуміле"])

    # 9. До підтвердження база не змінюється (validation is pure — no DB helper calls)
    def test_validation_has_no_db_side_effects(self):
        items = make_items()
        operations = [
            {"type": "remove_inventory", "item_number": 1},
            {"type": "consume_inventory_quantity", "item_number": 2, "quantity_value": 1, "quantity_unit": "шт."},
            {"type": "add_to_shopping", "name": "Молоко", "quantity_value": 1, "quantity_unit": "л",
             "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
        ]
        kind, payload = _validate_compound_operations(operations, [], items)
        self.assertEqual(kind, "ok")
        self.assertFalse(bot.apply_compound_inventory_operations.called)
        self.assertFalse(bot.delete_inventory_items_batch.called)
        self.assertFalse(bot.add_shopping_items_batch.called)
        self.assertFalse(bot.get_inventory_items.called)

    # 10. Повторне підтвердження не застосовує зміни двічі
    def test_pending_compound_applied_only_once(self):
        chat_id = 77777
        pending_compound_inventory[chat_id] = {
            "inventory_changes": [{
                "item_number": 1, "item_id": 401, "name": "Вершки",
                "old_value": None, "old_unit": None, "old_display": "",
                "new_value": None, "new_unit": None, "new_display": None,
                "will_remove": True, "op_type": "remove",
            }],
            "add_to_shopping": [],
            "household_id": 1,
            "user_db_id": 1,
        }
        first = pending_compound_inventory.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_compound_inventory.pop(chat_id, None)
        self.assertIsNone(second)

    # 11. Snapshot із зміненою кількістю вважається застарілим
    def test_stale_snapshot_detected(self):
        inventory_changes = [{"item_id": 402, "old_value": 2.0, "old_unit": "шт."}]
        unchanged = [{"id": 402, "quantity_value": 2.0, "quantity_unit": "шт."}]
        self.assertFalse(_compound_snapshot_is_stale(inventory_changes, unchanged))

        changed_value = [{"id": 402, "quantity_value": 1.0, "quantity_unit": "шт."}]
        self.assertTrue(_compound_snapshot_is_stale(inventory_changes, changed_value))

        missing = []
        self.assertTrue(_compound_snapshot_is_stale(inventory_changes, missing))

    # 12. Існуюча покупка об'єднується лише за чинними безпечними правилами
    def test_add_to_shopping_merges_only_when_compatible(self):
        items = make_items()
        compatible_ops = [
            {"type": "add_to_shopping", "name": "Молоко", "quantity_value": 1, "quantity_unit": "л",
             "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
            {"type": "add_to_shopping", "name": "Молоко", "quantity_value": 0.5, "quantity_unit": "л",
             "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
        ]
        kind, payload = _validate_compound_operations(compatible_ops, [], items)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["add_to_shopping"]), 1)
        self.assertEqual(payload["add_to_shopping"][0]["quantity_value"], 1.5)

        incompatible_ops = [
            {"type": "add_to_shopping", "name": "Молоко", "quantity_value": 1, "quantity_unit": "шт.",
             "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
            {"type": "add_to_shopping", "name": "Молоко", "quantity_value": 2, "quantity_unit": "л",
             "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
        ]
        kind2, payload2 = _validate_compound_operations(incompatible_ops, [], items)
        self.assertEqual(kind2, "ok")
        self.assertEqual(len(payload2["add_to_shopping"]), 2)


if __name__ == '__main__':
    unittest.main()
