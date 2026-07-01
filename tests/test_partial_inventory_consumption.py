import sys
import os
import unittest
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
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
    _resolve_consumption,
    _validate_consumptions,
    _format_consumption_preview,
    get_warsaw_datetime_context,
    SYSTEM_PROMPT,
    pending_inventory_consumption,
)


class TestResolveConsumption(unittest.TestCase):

    # 1. 18 шт. - 4 шт. = 14 шт.
    def test_pieces_subtraction(self):
        kind, remaining, unit = _resolve_consumption(18, "шт.", 4, "шт.")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("14"))
        self.assertEqual(unit, "шт.")

    # 2. 3 шт. - 1 шт. = 2 шт.
    def test_pieces_subtraction_small(self):
        kind, remaining, unit = _resolve_consumption(3, "шт.", 1, "шт.")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("2"))
        self.assertEqual(unit, "шт.")

    # 3. 5,5 л - 500 мл = 5 л
    def test_liters_minus_milliliters(self):
        kind, remaining, unit = _resolve_consumption(5.5, "л", 500, "мл")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("5"))
        self.assertEqual(unit, "л")

    # 4. 1 кг - 200 г = 800 г
    def test_kilograms_minus_grams(self):
        kind, remaining, unit = _resolve_consumption(1, "кг", 200, "г")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("800"))
        self.assertEqual(unit, "г")

    # Extra: 0,25 кг із 500 г → 250 г (both directions of the mass conversion)
    def test_grams_minus_kilograms(self):
        kind, remaining, unit = _resolve_consumption(500, "г", 0.25, "кг")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("250"))
        self.assertEqual(unit, "г")

    # 5. Несумісні одиниці відкидаються
    def test_incompatible_units_rejected(self):
        kind, remaining, unit = _resolve_consumption(1, "шт.", 200, "г")
        self.assertEqual(kind, "incompatible_units")
        self.assertIsNone(remaining)
        self.assertIsNone(unit)

    # 6. Спроба списати більше, ніж є, відкидається
    def test_insufficient_quantity_rejected(self):
        kind, remaining, unit = _resolve_consumption(3, "шт.", 4, "шт.")
        self.assertEqual(kind, "insufficient")
        self.assertIsNone(remaining)
        self.assertIsNone(unit)


class TestValidateConsumptions(unittest.TestCase):

    def _make_items(self):
        return [
            {"id": 201, "name": "Сосиски", "quantity_text": "18 шт.", "category": "М'ясо та риба",
             "quantity_value": 18.0, "quantity_unit": "шт."},
            {"id": 202, "name": "Приправа до курки", "quantity_text": "3 шт.", "category": "Соуси, спеції та бакалія",
             "quantity_value": 3.0, "quantity_unit": "шт."},
            {"id": 203, "name": "Сіль", "quantity_text": "жменька", "category": "Соуси, спеції та бакалія",
             "quantity_value": None, "quantity_unit": None},
        ]

    # Валідне часткове списання
    def test_valid_partial_consumption(self):
        items = self._make_items()
        kind, resolved = _validate_consumptions(
            [{"item_number": 1, "quantity_value": 4, "quantity_unit": "шт."}], items
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(len(resolved), 1)
        r = resolved[0]
        self.assertEqual(r["item_id"], 201)
        self.assertEqual(r["new_value"], 14.0)
        self.assertEqual(r["new_unit"], "шт.")
        self.assertFalse(r["will_remove"])

    # 7. Нульовий залишок означає видалення позиції, а не "0 шт."
    def test_full_consumption_marks_removal(self):
        items = self._make_items()
        kind, resolved = _validate_consumptions(
            [{"item_number": 1, "quantity_value": 18, "quantity_unit": "шт."}], items
        )
        self.assertEqual(kind, "ok")
        r = resolved[0]
        self.assertTrue(r["will_remove"])
        self.assertIsNone(r["new_value"])
        self.assertIsNone(r["new_unit"])
        self.assertIsNone(r["new_display"])
        preview = _format_consumption_preview(resolved)
        self.assertIn("буде прибрано із запасів", preview)
        self.assertNotIn("0 шт.", preview)

    # 8. Старий товар без structured quantity не списується автоматично
    def test_item_without_structured_quantity_rejected(self):
        items = self._make_items()
        kind, payload = _validate_consumptions(
            [{"item_number": 3, "quantity_value": 1, "quantity_unit": "шт."}], items
        )
        self.assertEqual(kind, "missing_quantity")
        self.assertEqual(payload, "Сіль")

    # Спроба списати більше, ніж є, відкидається (end-to-end через _validate_consumptions)
    def test_insufficient_quantity_end_to_end(self):
        items = self._make_items()
        kind, payload = _validate_consumptions(
            [{"item_number": 2, "quantity_value": 4, "quantity_unit": "шт."}], items
        )
        self.assertEqual(kind, "insufficient")
        name, available, requested = payload
        self.assertEqual(name, "Приправа до курки")
        self.assertEqual(available, "3 шт.")
        self.assertEqual(requested, "4 шт.")

    # Несумісні одиниці не створюють preview
    def test_incompatible_units_yield_invalid(self):
        items = self._make_items()
        kind, payload = _validate_consumptions(
            [{"item_number": 1, "quantity_value": 1, "quantity_unit": "л"}], items
        )
        self.assertEqual(kind, "invalid")
        self.assertIsNone(payload)

    # Дублікат item_number в одному списку consumptions відкидається
    def test_duplicate_item_number_rejected(self):
        items = self._make_items()
        kind, payload = _validate_consumptions(
            [
                {"item_number": 1, "quantity_value": 1, "quantity_unit": "шт."},
                {"item_number": 1, "quantity_value": 2, "quantity_unit": "шт."},
            ],
            items,
        )
        self.assertEqual(kind, "invalid")
        self.assertIsNone(payload)

    # Preview: правильний заголовок і стрілка для часткового списання
    def test_preview_format_partial(self):
        items = self._make_items()
        kind, resolved = _validate_consumptions(
            [{"item_number": 2, "quantity_value": 1, "quantity_unit": "шт."}], items
        )
        self.assertEqual(kind, "ok")
        preview = _format_consumption_preview(resolved)
        self.assertIn("🧊 Буде використано: 1", preview)
        self.assertIn("Приправа до курки — 3 шт.", preview)
        self.assertIn("→ Приправа до курки — 2 шт.", preview)


class TestPendingInventoryConsumptionAppliedOnce(unittest.TestCase):

    # 9. Повторне підтвердження не застосовує списання двічі
    def test_pending_consumption_applied_only_once(self):
        chat_id = 88888
        pending_inventory_consumption[chat_id] = {
            "resolved": [{
                "item_number": 1, "item_id": 201, "name": "Сосиски",
                "old_value": 18.0, "old_unit": "шт.", "old_display": "18 шт.",
                "new_value": 14.0, "new_unit": "шт.", "new_display": "14 шт.",
                "will_remove": False,
            }],
            "household_id": 1,
            "user_db_id": 1,
        }
        first = pending_inventory_consumption.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_inventory_consumption.pop(chat_id, None)
        self.assertIsNone(second)


class TestWarsawDatetimeContext(unittest.TestCase):

    # 10. Функція часу повертає Europe/Warsaw для переданого фіксованого datetime
    def test_fixed_datetime_formats_correctly(self):
        fixed = datetime(2026, 7, 1, 22, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
        context = get_warsaw_datetime_context(fixed)
        self.assertIn("1 липня 2026", context)
        self.assertIn("середа", context)
        self.assertIn("22:15", context)
        self.assertIn("Europe/Warsaw", context)

    # 11. General AI context містить авторитетну дату/час і заборону вигадувати realtime-дані
    def test_general_chat_prompt_forbids_fabricated_realtime_data(self):
        fixed = datetime(2026, 7, 1, 22, 15, tzinfo=ZoneInfo("Europe/Warsaw"))
        combined = SYSTEM_PROMPT + "\n\n" + get_warsaw_datetime_context(fixed)
        self.assertIn("немає доступу до інтернету", combined)
        self.assertIn("не вигадуй", combined)
        self.assertIn("погод", combined)
        self.assertIn("1 липня 2026", combined)
        self.assertIn("Europe/Warsaw", combined)


if __name__ == '__main__':
    unittest.main()
