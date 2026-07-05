import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes. This lets us exercise the actual
# merge_quantity_values()/format_quantity_display()/_merge_or_insert_*_in_tx
# precision directly, with a fake connection/cursor standing in for
# Postgres — no real Supabase involved. Same pattern as
# tests/test_expense_delete.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_quantity_precision_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    merge_quantity_values as bot_merge_quantity_values,
    format_quantity_display as bot_format_quantity_display,
    _auto_merge_in_place,
    normalize_item_quantity,
)


# =========================
# FakeCursor/FakeConnection — same shape as tests/test_expense_delete.py and
# tests/test_global_household_operations.py, used to exercise the real
# _merge_or_insert_*_in_tx SQL path without a real Postgres.
# =========================
class FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.queries = []
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestMergeQuantityValuesPrecision(unittest.TestCase):
    """Case 1 — tiny quantities must sum exactly, not round to zero. Checked
    against BOTH existing copies of merge_quantity_values (bot.py and the
    real database.py)."""

    def test_bot_copy_sums_tiny_grams_without_rounding_to_zero(self):
        value, unit = bot_merge_quantity_values(0.0001, "г", 0.00001, "г")
        self.assertEqual(unit, "г")
        self.assertAlmostEqual(value, 0.00011, places=10)
        self.assertNotEqual(value, 0)

    def test_database_copy_sums_tiny_grams_without_rounding_to_zero(self):
        value, unit = real_database.merge_quantity_values(0.0001, "г", 0.00001, "г")
        self.assertEqual(unit, "г")
        self.assertAlmostEqual(value, 0.00011, places=10)
        self.assertNotEqual(value, 0)

    # Case 4 — ordinary quantities still merge exactly as before
    def test_ordinary_liters_still_merge_correctly(self):
        value, unit = bot_merge_quantity_values(1.5, "л", 2, "л")
        self.assertEqual(unit, "л")
        self.assertEqual(value, 3.5)

        value2, unit2 = real_database.merge_quantity_values(1.5, "л", 2, "л")
        self.assertEqual(unit2, "л")
        self.assertEqual(value2, 3.5)

    # Case 5 — incompatible units still refuse to merge
    def test_incompatible_units_still_do_not_merge(self):
        self.assertIsNone(bot_merge_quantity_values(1.5, "л", 2, "шт."))
        self.assertIsNone(real_database.merge_quantity_values(1.5, "л", 2, "шт."))

    def test_none_values_still_do_not_merge(self):
        self.assertIsNone(bot_merge_quantity_values(None, "л", 2, "л"))
        self.assertIsNone(real_database.merge_quantity_values(1.5, "л", None, "л"))

    def test_unit_outside_structured_units_does_not_merge(self):
        self.assertIsNone(bot_merge_quantity_values(1, "ящик", 2, "ящик"))

    # Decimal-safety: str(float) round-trip must not introduce artifacts
    def test_decimal_inputs_are_accepted_directly(self):
        value, unit = bot_merge_quantity_values(Decimal("0.0001"), "г", Decimal("0.00001"), "г")
        self.assertEqual(unit, "г")
        self.assertAlmostEqual(value, 0.00011, places=10)


class TestFormatQuantityDisplayPrecision(unittest.TestCase):
    # Case 2 — small nonzero values never display as "0 г"
    def test_small_nonzero_value_never_shown_as_zero(self):
        self.assertEqual(bot_format_quantity_display(0.0001, "г"), "0,0001 г")
        self.assertEqual(bot_format_quantity_display(0.00001, "г"), "0,00001 г")
        self.assertEqual(bot_format_quantity_display(0.00011, "г"), "0,00011 г")
        self.assertEqual(real_database.format_quantity_display(0.00001, "г"), "0,00001 г")

    # Case 3 — never scientific notation
    def test_no_scientific_notation(self):
        for value in (0.0001, 0.00001, 0.000001, 1e-8):
            text = bot_format_quantity_display(value, "г")
            self.assertNotIn("e", text.lower())
            text_db = real_database.format_quantity_display(value, "г")
            self.assertNotIn("e", text_db.lower())

    def test_trailing_zeros_trimmed_but_value_not_rounded(self):
        self.assertEqual(bot_format_quantity_display(3.5000, "л"), "3,5 л")
        self.assertEqual(bot_format_quantity_display(12.000, "шт."), "12 шт.")
        self.assertEqual(bot_format_quantity_display(0.00011, "г"), "0,00011 г")

    def test_actual_zero_still_shows_as_zero(self):
        self.assertEqual(bot_format_quantity_display(0, "г"), "0 г")
        self.assertEqual(bot_format_quantity_display(0.0, "г"), "0 г")

    def test_none_value_returns_empty_string(self):
        self.assertEqual(bot_format_quantity_display(None, "г"), "")


class TestAutoMergeInPlacePrecision(unittest.TestCase):
    """Case 6 (shopping/RAM side) — bot.py's own duplicate-merge path
    (_auto_merge_in_place, used for both shopping and inventory pending
    previews) must preserve the same precision."""

    def _saffron_item(self, quantity_text):
        item = {"name": "Шафран", "category": "Інше їстівне", "was_corrected": False}
        item.update(normalize_item_quantity("Шафран", quantity_text))
        return item

    def test_two_tiny_saffron_additions_do_not_merge_to_zero(self):
        item1 = self._saffron_item("0,0001 г")
        item2 = self._saffron_item("0,00001 г")
        result = _auto_merge_in_place([item1, item2])
        self.assertEqual(len(result), 1)
        self.assertNotEqual(result[0]["quantity_value"], 0)
        self.assertEqual(result[0]["quantity_text"], "0,00011 г")


class TestInventoryAndShoppingMergeInTxPrecision(unittest.TestCase):
    """Case 6 (DB side) — the real _merge_or_insert_inventory_in_tx and
    _merge_or_insert_shopping_in_tx, exercised end-to-end against a fake
    cursor, must write the exact summed value/text, not a rounded zero.
    This is the exact reproduction of the confirmed saffron bug."""

    def test_inventory_merge_preserves_tiny_quantity(self):
        cursor = FakeCursor(fetchall_results=[[(501, "Інше їстівне", Decimal("0.0001"), "г", False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_inventory_in_tx(
                        cur, household_id=1, user_db_id=10, name="Шафран", qty_text="0,00001 г",
                        category="Інше їстівне", canonical_name="шафран",
                        quantity_value=0.00001, quantity_unit="г", quantity_inferred=False,
                    )
        update_sql, update_params = cursor.queries[-1]
        self.assertIn("UPDATE inventory_items", update_sql)
        quantity_text, quantity_value, quantity_unit = update_params[0], update_params[1], update_params[2]
        self.assertEqual(quantity_text, "0,00011 г")
        self.assertNotEqual(quantity_value, 0)
        self.assertAlmostEqual(quantity_value, 0.00011, places=10)
        self.assertEqual(quantity_unit, "г")

    def test_shopping_merge_preserves_tiny_quantity(self):
        cursor = FakeCursor(fetchall_results=[[(701, "Інше їстівне", Decimal("0.0001"), "г", False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_shopping_in_tx(
                        cur, household_id=1, user_db_id=10, name="Шафран", qty_text="0,00001 г",
                        category="Інше їстівне", canonical_name="шафран",
                        quantity_value=0.00001, quantity_unit="г", quantity_inferred=False,
                    )
        update_sql, update_params = cursor.queries[-1]
        self.assertIn("UPDATE shopping_items", update_sql)
        quantity_text, quantity_value = update_params[0], update_params[1]
        self.assertEqual(quantity_text, "0,00011 г")
        self.assertNotEqual(quantity_value, 0)
        self.assertAlmostEqual(quantity_value, 0.00011, places=10)


if __name__ == '__main__':
    unittest.main()
