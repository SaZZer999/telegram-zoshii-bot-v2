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
    normalize_item_quantity,
    format_quantity_display,
    _auto_merge_in_place,
)


class TestStructuredQuantities(unittest.TestCase):

    def _make_item(self, name, quantity_text, category="Інше їстівне", allow_default=False):
        item = {"name": name, "category": category, "was_corrected": False}
        item.update(normalize_item_quantity(name, quantity_text, allow_default_unit=allow_default))
        return item

    # 1. "3.5 л" нормалізується до "3,5 л", value 3.5, unit "л"
    def test_decimal_dot_normalized_to_comma(self):
        normalized = normalize_item_quantity("Молоко", "3.5 л")
        self.assertEqual(normalized["canonical_name"], "молоко")
        self.assertEqual(normalized["quantity_value"], 3.5)
        self.assertEqual(normalized["quantity_unit"], "л")
        self.assertEqual(normalized["quantity_text"], "3,5 л")

    # 2. "6 штук" нормалізується до "6 шт."
    def test_pieces_word_variant_normalized(self):
        normalized = normalize_item_quantity("Яйця", "6 штук")
        self.assertEqual(normalized["quantity_value"], 6.0)
        self.assertEqual(normalized["quantity_unit"], "шт.")
        self.assertEqual(normalized["quantity_text"], "6 шт.")

    # 3. Новий "Хліб" без кількості стає "1 шт." з quantity_inferred=True
    def test_new_item_without_quantity_defaults_to_1_piece_inferred(self):
        normalized = normalize_item_quantity("Хліб", "", allow_default_unit=True)
        self.assertEqual(normalized["quantity_value"], 1.0)
        self.assertEqual(normalized["quantity_unit"], "шт.")
        self.assertTrue(normalized["quantity_inferred"])
        self.assertEqual(normalized["quantity_text"], "1 шт.")

    # 4. "Хліб — 1 шт." + "Хліб — 1 шт." = "Хліб — 2 шт."
    def test_bread_plus_bread_merges_to_2_pieces(self):
        item1 = self._make_item("Хліб", "1 шт.")
        item2 = self._make_item("Хліб", "1 шт.")
        result = _auto_merge_in_place([item1, item2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["quantity_text"], "2 шт.")

    # 5. "Приправа до курки — 1 шт." + "Приправа до курки — 2 шт." = "3 шт."
    def test_seasoning_1_plus_2_pieces_merges_to_3(self):
        item1 = self._make_item("Приправа до курки", "1 шт.")
        item2 = self._make_item("Приправа до курки", "2 шт.")
        result = _auto_merge_in_place([item1, item2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["quantity_text"], "3 шт.")

    # 6. "Вершки — 1 шт." + "сливки — 1 шт." використовують спільний canonical_name і дають "2 шт."
    def test_vershky_and_slyvky_share_canonical_name_and_merge(self):
        item1 = self._make_item("Вершки", "1 шт.", category="Молочне та яйця")
        item2 = self._make_item("сливки", "1 шт.", category="Молочне та яйця")
        self.assertEqual(item1["canonical_name"], item2["canonical_name"])
        result = _auto_merge_in_place([item1, item2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["canonical_name"], "вершки")
        self.assertEqual(result[0]["quantity_text"], "2 шт.")

    # 7. "Молоко — 1 шт." + "Молоко — 2 л" не об'єднуються
    def test_milk_pieces_and_liters_not_merged(self):
        item1 = self._make_item("Молоко", "1 шт.")
        item2 = self._make_item("Молоко", "2 л")
        result = _auto_merge_in_place([item1, item2])
        self.assertEqual(len(result), 2)

    # 8. "Вершки 18%" і "Вершки 30%" не об'єднуються
    def test_different_cream_percentages_not_merged(self):
        item1 = self._make_item("Вершки 18%", "200 мл")
        item2 = self._make_item("Вершки 30%", "200 мл")
        result = _auto_merge_in_place([item1, item2])
        self.assertEqual(len(result), 2)

    # 9. Форматування 2.0 показує "2"
    def test_format_quantity_display_drops_trailing_zero(self):
        self.assertEqual(format_quantity_display(2.0, "шт."), "2 шт.")
        self.assertEqual(format_quantity_display(3.5, "л"), "3,5 л")

    # 10. Старий нерозбірливий quantity_text не викликає помилки
    def test_unparseable_legacy_quantity_text_does_not_raise(self):
        normalized = normalize_item_quantity("Сіль", "жменька")
        self.assertIsNone(normalized["quantity_value"])
        self.assertIsNone(normalized["quantity_unit"])
        self.assertEqual(normalized["quantity_text"], "жменька")
        self.assertFalse(normalized["quantity_inferred"])


if __name__ == '__main__':
    unittest.main()
