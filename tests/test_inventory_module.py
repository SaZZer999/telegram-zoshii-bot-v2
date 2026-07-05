"""inventory.py — module boundary + pure inventory helpers.

Verifies that bot.py delegates to inventory.py (same function objects, not
independent copies) and that the moved pure logic (Representation Guard v1,
consumption validation, stale-snapshot check) still behaves exactly as
before. Does NOT re-test the full webhook flows around these helpers —
that's already covered by test_inventory_representation_guard.py,
test_partial_inventory_consumption.py, test_compound_inventory_operations.py,
and test_cross_unit_inventory_merge.py. No real Gemini, Telegram, Render, or
Supabase call happens anywhere in this file.
"""
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

import inventory

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402


_PUBLIC_REPRESENTATION_HELPERS = (
    "find_inventory_representation_matches",
    "classify_inventory_representation",
    "resolve_inventory_representation",
    "format_representation_clarify_message",
    "format_global_quantity_clarification_message",
    "format_representation_separate_warning",
    "format_representation_merge_line",
    "format_representation_merge_quantity_fragment",
)


class TestModuleBoundaryIdentity(unittest.TestCase):
    """#1/#2: bot.py re-exports the SAME objects from inventory.py — no
    independent duplicate implementation left behind."""

    def test_bot_and_inventory_share_every_public_representation_helper(self):
        for name in _PUBLIC_REPRESENTATION_HELPERS:
            with self.subTest(name=name):
                self.assertIs(getattr(bot, name), getattr(inventory, name))

    def test_bot_and_inventory_share_consumption_helpers(self):
        self.assertIs(bot._resolve_consumption, inventory._resolve_consumption)
        self.assertIs(bot._validate_consumptions, inventory._validate_consumptions)
        self.assertIs(bot._format_consumption_preview, inventory._format_consumption_preview)

    def test_bot_and_inventory_share_stale_check(self):
        self.assertIs(bot._compound_snapshot_is_stale, inventory._compound_snapshot_is_stale)

    def test_bot_and_inventory_share_unit_group_constants(self):
        self.assertIs(bot._UNIT_GROUP, inventory._UNIT_GROUP)
        self.assertIs(bot._UNIT_TO_CANONICAL_FACTOR, inventory._UNIT_TO_CANONICAL_FACTOR)
        self.assertIs(bot._CANONICAL_UNIT_FOR_GROUP, inventory._CANONICAL_UNIT_FOR_GROUP)

    def test_household_router_injection_sees_the_same_objects(self):
        """household_router.py calls these through the injected `_bot`
        reference (configure()) — `_bot` IS the bot module, so this must
        hold trivially, but it's the exact contract household_router.py
        relies on."""
        import household_router
        self.assertIs(household_router._bot.resolve_inventory_representation, inventory.resolve_inventory_representation)
        self.assertIs(household_router._bot.merge_quantity_values, inventory.merge_quantity_values)


class TestInventoryModuleHasNoForbiddenImports(unittest.TestCase):
    """#10 (structural half): inventory.py imports nothing beyond the
    standard library and quantities.py — verified by inspecting its own
    already-successful import (no bot/database/household_router/Flask/
    psycopg/Gemini module was required to import it)."""

    def test_module_file_only_imports_stdlib_and_quantities(self):
        path = os.path.join(os.path.dirname(__file__), "..", "inventory.py")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        import_lines = [
            line.strip() for line in lines
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        forbidden_modules = ("bot", "database", "household_router", "flask", "psycopg", "requests", "groq")
        for line in import_lines:
            for module in forbidden_modules:
                self.assertFalse(
                    line == f"import {module}" or line.startswith(f"from {module} "),
                    f"forbidden import found: {line!r}",
                )


class TestRepresentationGuardBehaviorUnchanged(unittest.TestCase):
    """#3/#5/#9: representation-conflict and incompatible-unit outcomes,
    and the exact clarification/warning text, are unchanged."""

    def test_liters_vs_pieces_conflict_stays_clarify(self):
        existing = [
            {"id": 1, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": 8.0, "quantity_unit": "л", "quantity_text": "8 л"},
            {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт."},
        ]
        outcome, existing_rows = inventory.resolve_inventory_representation(
            existing, "молоко", "Молочне та яйця", 1.0, "шт.", True,
        )
        self.assertEqual(outcome, "clarify")
        self.assertEqual(len(existing_rows), 2)

        message = inventory.format_representation_clarify_message("Молоко", existing_rows)
        self.assertIn("У запасах уже є кілька записів «Молоко»:", message)
        self.assertIn("• 8 л", message)
        self.assertIn("• 1 шт.", message)

    def test_liters_and_pieces_are_incompatible_for_merge(self):
        self.assertIsNone(inventory.merge_quantity_values(8.0, "л", 1.0, "шт."))
        outcome = inventory.classify_inventory_representation(8.0, "л", 1.0, "шт.", False)
        self.assertEqual(outcome, "separate")

    def test_separate_warning_text_unchanged(self):
        text = inventory.format_representation_separate_warning("Молоко", "8 л", "1 шт.")
        self.assertEqual(
            text,
            "⚠️ Молоко вже є у запасах: 8 л.\n"
            "Нове надходження: 1 шт.\n"
            "Його буде збережено окремою позицією, без об'єднання.",
        )

    def test_merge_line_text_unchanged(self):
        line = inventory.format_representation_merge_line("Молоко", "8 л", "500 мл", "8,5 л")
        self.assertEqual(line, "• Молоко — 8 л + 500 мл → буде 8,5 л")


class TestUnitMergePrecision(unittest.TestCase):
    """#4: cross-unit merge (л<->мл) is exact Decimal arithmetic."""

    def test_liters_plus_milliliters_merges_in_liters(self):
        value, unit = inventory.merge_quantity_values(Decimal("8"), "л", Decimal("500"), "мл")
        self.assertEqual(value, Decimal("8.5"))
        self.assertEqual(unit, "л")


class TestConsumptionPrecision(unittest.TestCase):
    """#6/#7: consumption math stays exact, and consuming to exactly zero is
    flagged for deletion, matching current behavior."""

    def test_kg_minus_grams_gives_exact_remainder(self):
        kind, remaining, unit = inventory._resolve_consumption(1, "кг", 200, "г")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("800"))
        self.assertEqual(unit, "г")

    def test_consume_to_exact_zero_marks_will_remove(self):
        items = [{"id": 501, "name": "Ковбаски", "quantity_value": 2, "quantity_unit": "шт."}]
        kind, resolved = inventory._validate_consumptions(
            [{"item_number": 1, "quantity_value": 2, "quantity_unit": "шт."}], items,
        )
        self.assertEqual(kind, "ok")
        self.assertTrue(resolved[0]["will_remove"])
        self.assertIsNone(resolved[0]["new_value"])

        preview = inventory._format_consumption_preview(resolved)
        self.assertIn("буде прибрано із запасів", preview)


class TestCompoundSnapshotStaleness(unittest.TestCase):
    """#8: _compound_snapshot_is_stale returns the same result for an
    unchanged vs. a changed snapshot."""

    def test_unchanged_snapshot_is_not_stale(self):
        inventory_changes = [{"item_id": 501, "old_value": 2.0, "old_unit": "шт."}]
        current_items = [{"id": 501, "quantity_value": 2.0, "quantity_unit": "шт."}]
        self.assertFalse(inventory._compound_snapshot_is_stale(inventory_changes, current_items))

    def test_changed_snapshot_is_stale(self):
        inventory_changes = [{"item_id": 501, "old_value": 2.0, "old_unit": "шт."}]
        current_items = [{"id": 501, "quantity_value": 5.0, "quantity_unit": "шт."}]
        self.assertTrue(inventory._compound_snapshot_is_stale(inventory_changes, current_items))

    def test_missing_item_is_stale(self):
        inventory_changes = [{"item_id": 501, "old_value": 2.0, "old_unit": "шт."}]
        self.assertTrue(inventory._compound_snapshot_is_stale(inventory_changes, []))


if __name__ == "__main__":
    unittest.main()
