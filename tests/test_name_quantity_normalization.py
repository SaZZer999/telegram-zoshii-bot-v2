import sys
import os
import importlib.util
import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes. This lets us exercise the actual
# resolve_item_name()/canonicalize_name()/parse_structured_quantity() logic
# directly. Same pattern as tests/test_expense_delete.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_name_quantity_test", _database_path)
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

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
import household_router
import expenses  # noqa: F401 — imported by household_router, kept for parity with other test files


NOW = datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _sausage_item():
    return {"id": 701, "name": "Сосиски", "category": "М'ясо та риба",
             "quantity_value": 6.0, "quantity_unit": "шт.", "quantity_text": "6 шт."}


def _ser_item():
    return {"id": 801, "name": "ser", "category": "Молочне та яйця",
             "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": True}


class TestBuiltinSynonymTranslation(unittest.TestCase):
    """Cases 1, 4, 5, 6, 7 — new built-in Polish/Ukrainian synonym entries,
    checked against both copies (bot.py and the real database.py)."""

    def test_mleko_translates_to_moloko(self):
        self.assertEqual(bot.canonicalize_name("mleko"), "молоко")
        self.assertEqual(real_database.canonicalize_name("mleko"), "молоко")
        display, canonical = bot.resolve_item_name("mleko", {})
        self.assertEqual(display, "mleko")  # built-in synonym never rewrites display, only canonical
        self.assertEqual(canonical, "молоко")

    def test_ser_translates_to_syr(self):
        self.assertEqual(bot.canonicalize_name("ser"), "сир")
        self.assertEqual(real_database.canonicalize_name("ser"), "сир")

    def test_maslo_translates_to_maslo_uk(self):
        self.assertEqual(bot.canonicalize_name("maslo"), "масло")
        self.assertEqual(bot.canonicalize_name("masło"), "масло")
        self.assertEqual(real_database.canonicalize_name("masło"), "масло")

    def test_smietanka_translates_to_vershky(self):
        self.assertEqual(bot.canonicalize_name("smietanka"), "вершки")
        self.assertEqual(bot.canonicalize_name("śmietanka"), "вершки")
        self.assertEqual(real_database.canonicalize_name("śmietanka"), "вершки")

    def test_smietana_translates_to_smetana_not_vershky(self):
        self.assertEqual(bot.canonicalize_name("smietana"), "сметана")
        self.assertEqual(bot.canonicalize_name("śmietana"), "сметана")
        self.assertNotEqual(bot.canonicalize_name("śmietana"), "вершки")
        self.assertEqual(real_database.canonicalize_name("śmietana"), "сметана")


class TestMixedScriptRepair(unittest.TestCase):
    """Case 2, 3 — narrow Latin/Cyrillic homoglyph repair."""

    def test_mixed_script_mleko_translates_to_moloko(self):
        # "mlekо" — Latin m,l,e,k + a single Cyrillic "о" homoglyph.
        mixed = "mlek" + "о"  # Cyrillic о (U+043E), not Latin o
        self.assertNotEqual(mixed, "mleko")  # sanity: really is the mixed-script byte sequence
        self.assertEqual(bot.canonicalize_name(mixed), "молоко")
        self.assertEqual(real_database.canonicalize_name(mixed), "молоко")

    def test_pure_cyrillic_moloko_is_never_touched(self):
        self.assertEqual(bot.canonicalize_name("молоко"), "молоко")
        self.assertEqual(real_database.canonicalize_name("молоко"), "молоко")

    def test_pure_cyrillic_syr_is_never_touched(self):
        self.assertEqual(bot.canonicalize_name("сир"), "сир")
        self.assertEqual(real_database.canonicalize_name("сир"), "сир")

    def test_repair_helper_leaves_pure_latin_and_pure_cyrillic_alone(self):
        self.assertEqual(bot._repair_mixed_script("banana"), "banana")
        self.assertEqual(bot._repair_mixed_script("хліб"), "хліб")


class TestAliasPriorityOverBuiltinSynonym(unittest.TestCase):
    # Case 8
    def test_household_alias_wins_over_builtin_synonym_for_ser(self):
        alias_map = {"ser": {"target_display_name": "Сир пармезан", "target_canonical_name": "сир пармезан"}}
        display, canonical = bot.resolve_item_name("ser", alias_map)
        self.assertEqual((display, canonical), ("Сир пармезан", "сир пармезан"))

        display_db, canonical_db = real_database.resolve_item_name("ser", alias_map)
        self.assertEqual((display_db, canonical_db), ("Сир пармезан", "сир пармезан"))

    def test_no_alias_falls_back_to_builtin_ser(self):
        display, canonical = bot.resolve_item_name("ser", {})
        self.assertEqual((display, canonical), ("ser", "сир"))


class TestBareNumberAndWordNumberQuantities(unittest.TestCase):
    # Case 9
    def test_bare_number_defaults_to_pieces_not_inferred(self):
        normalized = bot.normalize_item_quantity("Банани", "3", allow_default_unit=True)
        self.assertEqual(normalized["quantity_value"], Decimal("3"))
        self.assertEqual(normalized["quantity_unit"], "шт.")
        self.assertEqual(normalized["quantity_text"], "3 шт.")
        self.assertFalse(normalized["quantity_inferred"])

        db_value, db_unit = real_database.parse_structured_quantity("3")
        self.assertEqual(db_value, Decimal("3"))
        self.assertEqual(db_unit, "шт.")

    # Case 10
    def test_para_word_resolves_to_two_pieces_inferred(self):
        for word in ("пара", "пару"):
            with self.subTest(word=word):
                normalized = bot.normalize_item_quantity("Сосиски", word, allow_default_unit=True)
                self.assertEqual(normalized["quantity_value"], Decimal("2"))
                self.assertEqual(normalized["quantity_unit"], "шт.")
                self.assertEqual(normalized["quantity_text"], "2 шт.")
                self.assertTrue(normalized["quantity_inferred"])

                db_value, db_unit = real_database.parse_structured_quantity(word)
                self.assertEqual(db_value, Decimal("2"))
                self.assertEqual(db_unit, "шт.")

    def test_bare_number_result_is_decimal_not_float(self):
        value, _ = real_database.parse_structured_quantity("3")
        self.assertIsInstance(value, Decimal)
        self.assertNotIsInstance(value, float)

    def test_ordinary_value_unit_quantity_still_works(self):
        normalized = bot.normalize_item_quantity("Молоко", "6 штук", allow_default_unit=True)
        self.assertEqual(normalized["quantity_value"], 6.0)
        self.assertEqual(normalized["quantity_unit"], "шт.")
        self.assertFalse(normalized["quantity_inferred"])


class TestContainerPhrasesNotConvertedToPieces(unittest.TestCase):
    # Case 12
    def test_container_phrase_stays_unparsed_no_conversion(self):
        value, unit = real_database.parse_structured_quantity("дві пачки")
        self.assertIsNone(value)
        self.assertIsNone(unit)

        value2, unit2 = bot._parse_structured_quantity("дві пачки")
        self.assertIsNone(value2)
        self.assertIsNone(unit2)

    def test_bare_container_word_alone_stays_unparsed(self):
        for word in ("пачка", "пачки", "упаковка", "упаковки"):
            with self.subTest(word=word):
                value, unit = real_database.parse_structured_quantity(word)
                self.assertIsNone(value)
                self.assertIsNone(unit)

    def test_normalize_quantity_fields_keeps_container_phrase_as_free_text(self):
        normalized = real_database.normalize_quantity_fields("Сосиски", "дві пачки", allow_default_unit=True)
        self.assertIsNone(normalized["quantity_value"])
        self.assertIsNone(normalized["quantity_unit"])
        self.assertEqual(normalized["quantity_text"], "дві пачки")
        self.assertFalse(normalized["quantity_inferred"])


class TestLeakedQuantityPhraseBlocksPreview(unittest.TestCase):
    # Case 11
    def test_two_packs_phrase_does_not_become_canonical_item_name(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{
                "type": "add_inventory", "name": "дві пачки сосисок",
                "quantity_text": "дві пачки", "category": "М'ясо та риба",
            }],
            "unresolved_fragments": [],
        }
        kind, reasons = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "invalid")
        self.assertTrue(any("дві пачки сосисок" in r for r in reasons))

    def test_correctly_separated_pair_phrase_still_builds_a_preview(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{
                "type": "add_inventory", "name": "Сосиски",
                "quantity_text": "пару", "category": "М'ясо та риба",
            }],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "ok")
        item = payload["add_inventory_items"][0]
        self.assertEqual(item["name"], "Сосиски")
        self.assertEqual(item["quantity_value"], Decimal("2"))
        self.assertTrue(item["quantity_inferred"])


class TestConsumeInventoryNormalizationHint(unittest.TestCase):
    # Case 13
    def test_hint_shown_for_untranslated_raw_name_without_mutating_item(self):
        item = _ser_item()
        original = dict(item)
        lines = household_router._numbered_item_lines([item], alias_map={}, with_normalization_hint=True)
        self.assertEqual(lines, ["1. ser [normalized: сир] — 1 шт."])
        self.assertEqual(item, original)  # the actual stored item dict is untouched

    def test_no_hint_for_already_ukrainian_name(self):
        lines = household_router._numbered_item_lines(
            [{"name": "Молоко", "quantity_text": "2 л"}], alias_map={}, with_normalization_hint=True,
        )
        self.assertEqual(lines, ["1. Молоко — 2 л"])

    def test_shopping_snapshot_never_gets_the_hint(self):
        lines = household_router._numbered_item_lines([_ser_item()])
        self.assertEqual(lines, ["1. ser — 1 шт."])


class TestIncompatibleUnitsStillBlockSafely(unittest.TestCase):
    # Case 14
    def test_grams_against_pieces_only_item_is_blocked(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "consume_inventory", "item_number": 1, "quantity_value": 200, "quantity_unit": "г"}],
            "unresolved_fragments": [],
        }
        kind, reasons = household_router._validate_operations(router_result, [_ser_item()], [], NOW)
        self.assertEqual(kind, "invalid")
        self.assertTrue(any("несумісні одиниці" in r for r in reasons))


if __name__ == '__main__':
    unittest.main()
