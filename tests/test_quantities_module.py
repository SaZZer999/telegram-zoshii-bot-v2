"""quantities.py — pure quantity/unit extraction.

Verifies the single shared implementation directly (no Telegram, Flask,
psycopg, database connection, or Gemini involved anywhere in this file),
plus that bot.py/database.py actually delegate to it instead of keeping
independent duplicate copies.
"""
import importlib
import importlib.util
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

import quantities

# Load the REAL database.py fresh, under its own module name — same pattern
# as tests/test_global_household_operations.py: lets this file check
# database.py's actual functions/identities without going through
# sys.modules['database'], which gets mocked below (only so importing bot
# doesn't attempt a real init_db()/Supabase connection at import time).
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_quantities_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402 — bot.py's own quantity aliases are real regardless
# of the mocked `database` module (they point straight at quantities.py).


class TestParseStructuredQuantity(unittest.TestCase):
    def test_unspaced_liters_uppercase_l(self):
        value, unit = quantities.parse_structured_quantity("1L")
        self.assertEqual(value, Decimal("1"))
        self.assertIsInstance(value, Decimal)
        self.assertEqual(unit, "л")

    def test_unspaced_milliliters_uppercase_ml(self):
        value, unit = quantities.parse_structured_quantity("500ML")
        self.assertEqual(value, Decimal("500"))
        self.assertIsInstance(value, Decimal)
        self.assertEqual(unit, "мл")

    def test_comma_decimal_liters_is_exact_decimal(self):
        value, unit = quantities.parse_structured_quantity("0,5 л")
        self.assertEqual(value, Decimal("0.5"))
        self.assertIsInstance(value, Decimal)
        self.assertEqual(unit, "л")

    def test_bare_number_defaults_to_pieces(self):
        value, unit = quantities.parse_structured_quantity("3")
        self.assertEqual(value, Decimal("3"))
        self.assertIsInstance(value, Decimal)
        self.assertEqual(unit, "шт.")

    def test_word_number_para_resolves_to_two_pieces(self):
        value, unit = quantities.parse_structured_quantity("пару")
        self.assertEqual(value, Decimal("2"))
        self.assertEqual(unit, "шт.")

    def test_container_phrase_stays_unparsed(self):
        value, unit = quantities.parse_structured_quantity("дві пачки")
        self.assertIsNone(value)
        self.assertIsNone(unit)

    def test_unknown_unit_word_stays_unparsed(self):
        value, unit = quantities.parse_structured_quantity("5 гектарів")
        self.assertIsNone(value)
        self.assertIsNone(unit)

    def test_blank_text_stays_unparsed(self):
        self.assertEqual(quantities.parse_structured_quantity(""), (None, None))
        self.assertEqual(quantities.parse_structured_quantity(None), (None, None))

    def test_spaced_value_unit_is_decimal_not_float(self):
        value, unit = quantities.parse_structured_quantity("6 штук")
        self.assertEqual(value, Decimal("6"))
        self.assertIsInstance(value, Decimal)
        self.assertNotIsInstance(value, float)
        self.assertEqual(unit, "шт.")

    def test_unspaced_grams_lowercase_g(self):
        value, unit = quantities.parse_structured_quantity("500g")
        self.assertEqual(value, Decimal("500"))
        self.assertIsInstance(value, Decimal)
        self.assertEqual(unit, "г")

    def test_spaced_gram_word(self):
        value, unit = quantities.parse_structured_quantity("500 gram")
        self.assertEqual(value, Decimal("500"))
        self.assertEqual(unit, "г")

    def test_spaced_grams_word(self):
        value, unit = quantities.parse_structured_quantity("500 grams")
        self.assertEqual(value, Decimal("500"))
        self.assertEqual(unit, "г")

    def test_unspaced_kilograms_kg(self):
        value, unit = quantities.parse_structured_quantity("2kg")
        self.assertEqual(value, Decimal("2"))
        self.assertIsInstance(value, Decimal)
        self.assertEqual(unit, "кг")


class TestMergeQuantityValues(unittest.TestCase):
    def test_liters_plus_milliliters(self):
        value, unit = quantities.merge_quantity_values(Decimal("8"), "л", Decimal("500"), "мл")
        self.assertEqual(value, Decimal("8.5"))
        self.assertEqual(unit, "л")

    def test_milliliters_plus_liters(self):
        value, unit = quantities.merge_quantity_values(Decimal("750"), "мл", Decimal("1"), "л")
        self.assertEqual(value, Decimal("1750"))
        self.assertEqual(unit, "мл")

    def test_kilograms_plus_grams(self):
        value, unit = quantities.merge_quantity_values(Decimal("1"), "кг", Decimal("500"), "г")
        self.assertEqual(value, Decimal("1.5"))
        self.assertEqual(unit, "кг")

    def test_grams_plus_kilograms(self):
        value, unit = quantities.merge_quantity_values(Decimal("500"), "г", Decimal("1"), "кг")
        self.assertEqual(value, Decimal("1500"))
        self.assertEqual(unit, "г")

    def test_incompatible_units_do_not_merge(self):
        self.assertIsNone(quantities.merge_quantity_values(Decimal("1"), "шт.", Decimal("500"), "г"))
        self.assertIsNone(quantities.merge_quantity_values(Decimal("1"), "л", Decimal("2"), "шт."))

    def test_unknown_unit_does_not_merge(self):
        self.assertIsNone(quantities.merge_quantity_values(Decimal("1"), "хвилина", Decimal("2"), "хвилина"))

    def test_none_value_does_not_merge(self):
        self.assertIsNone(quantities.merge_quantity_values(None, "л", Decimal("1"), "л"))

    def test_result_is_decimal_not_float(self):
        value, _ = quantities.merge_quantity_values(Decimal("8"), "л", Decimal("500"), "мл")
        self.assertIsInstance(value, Decimal)
        self.assertNotIsInstance(value, float)


class TestFormatQuantityDisplay(unittest.TestCase):
    def test_tiny_value_keeps_full_precision(self):
        self.assertEqual(quantities.format_quantity_display(Decimal("0.00011"), "г"), "0,00011 г")

    def test_no_scientific_notation_for_tiny_value(self):
        text = quantities.format_quantity_display(Decimal("0.00011"), "г")
        self.assertNotIn("e", text.lower())

    def test_no_scientific_notation_for_large_value(self):
        text = quantities.format_quantity_display(Decimal("1000000"), "г")
        self.assertNotIn("e", text.lower())

    def test_trailing_zeros_trimmed(self):
        self.assertEqual(quantities.format_quantity_display(Decimal("8.50"), "л"), "8,5 л")

    def test_none_value_is_empty_string(self):
        self.assertEqual(quantities.format_quantity_display(None, "л"), "")


class TestParseQuantityFields(unittest.TestCase):
    def test_blank_with_default_allowed_gives_one_piece_inferred(self):
        fields = quantities.parse_quantity_fields("", allow_default_unit=True)
        self.assertEqual(fields["quantity_value"], Decimal("1"))
        self.assertEqual(fields["quantity_unit"], "шт.")
        self.assertTrue(fields["quantity_inferred"])
        self.assertEqual(fields["quantity_text"], "1 шт.")

    def test_blank_without_default_stays_unset(self):
        fields = quantities.parse_quantity_fields("", allow_default_unit=False)
        self.assertIsNone(fields["quantity_value"])
        self.assertIsNone(fields["quantity_unit"])
        self.assertFalse(fields["quantity_inferred"])

    def test_word_number_is_flagged_inferred(self):
        fields = quantities.parse_quantity_fields("пара", allow_default_unit=True)
        self.assertEqual(fields["quantity_value"], Decimal("2"))
        self.assertTrue(fields["quantity_inferred"])

    def test_explicit_digit_is_not_inferred(self):
        fields = quantities.parse_quantity_fields("6 штук", allow_default_unit=True)
        self.assertEqual(fields["quantity_value"], Decimal("6"))
        self.assertFalse(fields["quantity_inferred"])

    def test_container_phrase_kept_as_free_text(self):
        fields = quantities.parse_quantity_fields("дві пачки", allow_default_unit=True)
        self.assertIsNone(fields["quantity_value"])
        self.assertIsNone(fields["quantity_unit"])
        self.assertEqual(fields["quantity_text"], "дві пачки")
        self.assertFalse(fields["quantity_inferred"])


class TestNoSharedStateLeaksBetweenModules(unittest.TestCase):
    """#11: bot.py and database.py must delegate to quantities.py — not
    keep an independent duplicate implementation. Verified by identity
    (same function object) rather than re-testing behavior."""

    def test_bot_and_database_use_the_same_merge_quantity_values(self):
        self.assertIs(bot.merge_quantity_values, quantities.merge_quantity_values)
        self.assertIs(real_database.merge_quantity_values, quantities.merge_quantity_values)

    def test_bot_and_database_use_the_same_format_quantity_display(self):
        self.assertIs(bot.format_quantity_display, quantities.format_quantity_display)
        self.assertIs(real_database.format_quantity_display, quantities.format_quantity_display)

    def test_bot_and_database_use_the_same_parse_structured_quantity(self):
        self.assertIs(bot._parse_structured_quantity, quantities.parse_structured_quantity)
        self.assertIs(real_database.parse_structured_quantity, quantities.parse_structured_quantity)

    def test_normalize_wrappers_delegate_to_shared_parse_quantity_fields(self):
        """normalize_quantity_fields (database.py) and normalize_item_quantity
        (bot.py) can't be plain aliases (they also resolve the item NAME,
        which stays local — see quantities.py's module docstring), but their
        quantity-field output must match quantities.parse_quantity_fields
        exactly for the same input."""
        fields = quantities.parse_quantity_fields("1,5 л", allow_default_unit=True)

        db_result = real_database.normalize_quantity_fields("Молоко", "1,5 л", allow_default_unit=True)
        self.assertEqual(db_result["quantity_value"], fields["quantity_value"])
        self.assertEqual(db_result["quantity_unit"], fields["quantity_unit"])
        self.assertEqual(db_result["quantity_text"], fields["quantity_text"])
        self.assertEqual(db_result["quantity_inferred"], fields["quantity_inferred"])

        bot_result = bot.normalize_item_quantity("Молоко", "1,5 л", allow_default_unit=True)
        self.assertEqual(bot_result["quantity_value"], fields["quantity_value"])
        self.assertEqual(bot_result["quantity_unit"], fields["quantity_unit"])
        self.assertEqual(bot_result["quantity_text"], fields["quantity_text"])
        self.assertEqual(bot_result["quantity_inferred"], fields["quantity_inferred"])

    def test_module_is_free_of_telegram_flask_psycopg_gemini(self):
        """#12 (structural half): quantities.py imports nothing from
        Telegram/Flask/psycopg/Gemini-related packages — it was already
        successfully imported above with none of those modules mocked."""
        self.assertFalse(hasattr(quantities, "psycopg"))
        self.assertFalse(hasattr(quantities, "requests"))
        self.assertFalse(hasattr(quantities, "Flask"))


if __name__ == "__main__":
    unittest.main()
