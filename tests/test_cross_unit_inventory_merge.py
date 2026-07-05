import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes. Same pattern as
# tests/test_quantity_precision.py and tests/test_inventory_representation_guard.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_cross_unit_merge_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No test in this file calls real
# Gemini, Telegram, Render, or Supabase — every test below exercises pure
# functions (merge_quantity_values, resolve_inventory_representation,
# _auto_merge_in_place, _parse_explicit_clarification_quantity) or a
# fake-cursor DB write path, never a real connection.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
from bot import (
    merge_quantity_values as bot_merge_quantity_values,
    format_quantity_display as bot_format_quantity_display,
    resolve_inventory_representation,
    format_representation_merge_line,
    _auto_merge_in_place,
    _parse_explicit_clarification_quantity,
)


def _milk_liters_row(value=8.0):
    return {"id": 201, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": value, "quantity_unit": "л", "quantity_text": bot_format_quantity_display(value, "л"),
             "quantity_inferred": False}


def _milk_pieces_row():
    return {"id": 202, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False}


class TestCrossUnitMergeQuantityValues(unittest.TestCase):
    # Case 1 — 8 л + 500 мл -> 8,5 л, both bot.py's and database.py's copy agree.
    def test_liters_plus_milliliters(self):
        value, unit = bot_merge_quantity_values(8, "л", Decimal("500"), "мл")
        self.assertEqual(unit, "л")
        self.assertEqual(value, Decimal("8.5"))
        self.assertIsInstance(value, Decimal)
        value_db, unit_db = real_database.merge_quantity_values(8, "л", Decimal("500"), "мл")
        self.assertEqual((value_db, unit_db), (value, unit))

    # Case 2 — 750 мл + 1 л -> 1750 мл (existing мл row keeps its representation).
    def test_milliliters_plus_liters(self):
        value, unit = bot_merge_quantity_values(750, "мл", 1, "л")
        self.assertEqual(unit, "мл")
        self.assertEqual(value, Decimal("1750"))
        value_db, unit_db = real_database.merge_quantity_values(750, "мл", 1, "л")
        self.assertEqual((value_db, unit_db), (value, unit))

    # Case 3 — 1 кг + 500 г -> 1,5 кг.
    def test_kilograms_plus_grams(self):
        value, unit = bot_merge_quantity_values(1, "кг", 500, "г")
        self.assertEqual(unit, "кг")
        self.assertEqual(value, Decimal("1.5"))
        value_db, unit_db = real_database.merge_quantity_values(1, "кг", 500, "г")
        self.assertEqual((value_db, unit_db), (value, unit))

    # Case 4 — 500 г + 1 кг -> 1500 г (existing г row keeps its representation).
    def test_grams_plus_kilograms(self):
        value, unit = bot_merge_quantity_values(500, "г", 1, "кг")
        self.assertEqual(unit, "г")
        self.assertEqual(value, Decimal("1500"))
        value_db, unit_db = real_database.merge_quantity_values(500, "г", 1, "кг")
        self.assertEqual((value_db, unit_db), (value, unit))

    # Case 5 — л and шт. are never compatible, group or no group.
    def test_liters_and_pieces_never_merge(self):
        self.assertIsNone(bot_merge_quantity_values(8, "л", 1, "шт."))
        self.assertIsNone(real_database.merge_quantity_values(8, "л", 1, "шт."))

    # Decimal precision regression — same-unit tiny-value merge (saffron)
    # must stay exact after the cross-unit refactor.
    def test_same_unit_tiny_value_precision_not_regressed(self):
        value, unit = bot_merge_quantity_values(Decimal("0.0001"), "г", Decimal("0.00001"), "г")
        self.assertEqual(unit, "г")
        self.assertEqual(value, Decimal("0.00011"))
        value_db, unit_db = real_database.merge_quantity_values(Decimal("0.0001"), "г", Decimal("0.00001"), "г")
        self.assertEqual((value_db, unit_db), (value, unit))

    # Cross-unit conversion is exact even for a small value (power-of-10
    # factors never lose precision).
    def test_cross_unit_tiny_value_stays_exact(self):
        value, unit = bot_merge_quantity_values(Decimal("1"), "кг", Decimal("0.001"), "г")
        self.assertEqual(unit, "кг")
        self.assertEqual(value, Decimal("1.000001"))


class TestCrossUnitRepresentationGuard(unittest.TestCase):
    # Case 5 — л vs шт. representation guard: explicit "1 шт." against an
    # existing "8 л" row is a safe separate record, never a guessed merge.
    def test_liters_row_and_incoming_pieces_is_separate_not_merge(self):
        outcome, existing = resolve_inventory_representation(
            [_milk_liters_row()], "молоко", "Молочне та яйця", 1.0, "шт.", False,
        )
        self.assertEqual(outcome, "separate")

    # Case 6 — an explicit "2 шт." never merges with an unparseable container
    # phrase like "дві пачки" (both stay as separate rows in a pending batch).
    def test_pieces_and_container_phrase_do_not_merge(self):
        items = [
            {"name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_text": "2 шт.", "quantity_inferred": False},
            {"name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": None, "quantity_unit": None, "quantity_text": "дві пачки", "quantity_inferred": False},
        ]
        result = _auto_merge_in_place(items)
        self.assertEqual(len(result), 2)

    # Case 7 — the preview line shows the final converted quantity ("буде
    # 8,5 л"), never the "окремою позицією" separate-record warning, for a
    # л/мл pair.
    def test_preview_shows_converted_quantity_not_separate_warning(self):
        outcome, existing = resolve_inventory_representation(
            [_milk_liters_row(8.0)], "молоко", "Молочне та яйця", Decimal("500"), "мл", False,
        )
        self.assertEqual(outcome, "merge")
        merged_value, merged_unit = bot_merge_quantity_values(
            existing["quantity_value"], existing["quantity_unit"], Decimal("500"), "мл",
        )
        line = format_representation_merge_line(
            "Молоко", existing["quantity_text"], "500 мл", bot_format_quantity_display(merged_value, merged_unit),
        )
        self.assertEqual(line, "• Молоко — 8 л + 500 мл → буде 8,5 л")
        self.assertNotIn("окремою позицією", line)


class TestCrossUnitClarificationLatinUnits(unittest.TestCase):
    # Case 8 — Latin L/ML (any case, spaced or not) are recognized as
    # equivalents of л/мл during Inventory Quantity Clarification replies.
    def test_latin_liter_variants(self):
        for text in ("0.5L", "0.5l", "1L", "1l"):
            with self.subTest(text=text):
                value, unit = _parse_explicit_clarification_quantity(text)
                self.assertEqual(unit, "л")
                self.assertIsInstance(value, Decimal)

    def test_latin_milliliter_variants(self):
        for text in ("500ML", "500ml", "500 Ml"):
            with self.subTest(text=text):
                value, unit = _parse_explicit_clarification_quantity(text)
                self.assertEqual(unit, "мл")
                self.assertEqual(value, Decimal("500"))

    def test_normalized_values_match_spec_examples(self):
        value, unit = _parse_explicit_clarification_quantity("0.5L")
        self.assertEqual((value, unit), (Decimal("0.5"), "л"))
        value, unit = _parse_explicit_clarification_quantity("500ML")
        self.assertEqual((value, unit), (Decimal("500"), "мл"))

    # Existing Cyrillic units and шт./г/кг must stay unaffected.
    def test_existing_cyrillic_units_unaffected(self):
        self.assertEqual(_parse_explicit_clarification_quantity("1 л"), (Decimal("1"), "л"))
        self.assertEqual(_parse_explicit_clarification_quantity("500 мл"), (Decimal("500"), "мл"))
        self.assertEqual(_parse_explicit_clarification_quantity("2 шт."), (Decimal("2"), "шт."))
        self.assertEqual(_parse_explicit_clarification_quantity("1 кг"), (Decimal("1"), "кг"))
        self.assertEqual(_parse_explicit_clarification_quantity("500 г"), (Decimal("500"), "г"))


# =========================
# database.py — DB-layer write path: preview and confirm must agree.
# =========================
class FakeCursor:
    def __init__(self, fetchall_results=None):
        self.queries = []
        self._fetchall_results = list(fetchall_results or [])

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

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


class TestCrossUnitMergeWritePath(unittest.TestCase):
    # Case 1 (DB side) — 8 л existing + 500 мл incoming: exactly one UPDATE,
    # no INSERT — a single remaining row, merged to "8,5 л".
    def test_liters_plus_milliliters_updates_single_row(self):
        cursor = FakeCursor(fetchall_results=[[(201, "Молочне та яйця", 8.0, "л", False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_inventory_in_tx(
                        cur, household_id=1, user_db_id=10, name="Молоко", qty_text="500 мл",
                        category="Молочне та яйця", canonical_name="молоко",
                        quantity_value=Decimal("500"), quantity_unit="мл", quantity_inferred=False,
                    )
        insert_queries = [q for q in cursor.queries if "INSERT INTO inventory_items" in q[0]]
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items SET" in q[0]]
        self.assertEqual(insert_queries, [])
        self.assertEqual(len(update_queries), 1)
        self.assertEqual(update_queries[0][1][0], "8,5 л")
        self.assertEqual(update_queries[0][1][-1], 201)

    # Case 3 (DB side) — 1 кг existing + 500 г incoming: single UPDATE to "1,5 кг".
    def test_kilograms_plus_grams_updates_single_row(self):
        cursor = FakeCursor(fetchall_results=[[(301, "Інше їстівне", 1.0, "кг", False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_inventory_in_tx(
                        cur, household_id=1, user_db_id=10, name="Цукор", qty_text="500 г",
                        category="Інше їстівне", canonical_name="цукор",
                        quantity_value=Decimal("500"), quantity_unit="г", quantity_inferred=False,
                    )
        insert_queries = [q for q in cursor.queries if "INSERT INTO inventory_items" in q[0]]
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items SET" in q[0]]
        self.assertEqual(insert_queries, [])
        self.assertEqual(len(update_queries), 1)
        self.assertEqual(update_queries[0][1][0], "1,5 кг")


if __name__ == "__main__":
    unittest.main()
