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


# =========================
# Numbered inventory delete selection (V1.1)
# =========================
def _item(item_id, name, category, quantity_text, quantity_value=None, quantity_unit=None):
    return {
        "id": item_id, "name": name, "category": category, "quantity_text": quantity_text,
        "quantity_value": quantity_value, "quantity_unit": quantity_unit, "was_corrected": False,
    }


_TEST_CATEGORY_ORDER = ["М'ясо та риба", "Молочне та яйця", "Фрукти та ягоди"]
_TEST_DEFAULT_CATEGORY = "Інше їстівне"


def _sample_items():
    return [
        _item(1, "Курка", "М'ясо та риба", "1 шт.", 1.0, "шт."),
        _item(2, "Ковбаски", "М'ясо та риба", "2 шт.", 2.0, "шт."),
        _item(3, "Молоко", "Молочне та яйця", "1 л", 1.0, "л"),
        _item(4, "Банани", "Фрукти та ягоди", "3 шт.", 3.0, "шт."),
    ]


class TestNumberedDeleteModuleBoundaryIdentity(unittest.TestCase):
    """#1/#2/#8: bot.py's numbered-delete wrappers are the same object (or
    return the same result) as the pure inventory.py helpers — no
    independent duplicate implementation left behind."""

    def test_bot_and_inventory_share_pure_zero_dependency_helpers(self):
        self.assertIs(bot._normalize_delete_match_text, inventory._normalize_delete_match_text)
        self.assertIs(bot._parse_numbered_delete_lines, inventory._parse_numbered_delete_lines)
        self.assertIs(bot._format_numbered_delete_mismatch_message, inventory._format_numbered_delete_mismatch_message)

    def test_pure_helper_needs_no_bot_import_and_wrapper_matches_it(self):
        """#1: the pure inventory.py helper works from plain items + locally
        supplied dependencies — no bot.py involved at all."""
        items = _sample_items()

        def local_effective_quantity(item):
            value = item.get("quantity_value")
            unit = item.get("quantity_unit")
            if value is not None:
                from quantities import format_quantity_display
                return value, unit, format_quantity_display(value, unit)
            return None, None, (item.get("quantity_text") or "")

        pure_result = inventory._numbered_inventory_display_items(items, _TEST_CATEGORY_ORDER, _TEST_DEFAULT_CATEGORY)
        self.assertEqual([n for n, _ in pure_result], [1, 2, 3, 4])

        # #2: bot.py's wrapper (real CATEGORY_ORDER/DEFAULT_CATEGORY/_effective_quantity)
        # returns the same shape/order as the pure helper called directly.
        bot_result = bot._numbered_inventory_display_items(items)
        wrapper_direct = inventory._numbered_inventory_display_items(items, bot.CATEGORY_ORDER, bot.DEFAULT_CATEGORY)
        self.assertEqual(bot_result, wrapper_direct)
        self.assertEqual([n for n, _ in bot_result], [1, 2, 3, 4])
        self.assertEqual(local_effective_quantity(items[0])[2], bot._effective_quantity(items[0])[2])


class TestNumberedDeleteCategoryOrderUnchanged(unittest.TestCase):
    """#3: category ordering in the numbered display is unchanged."""

    def test_items_are_numbered_in_category_order_not_input_order(self):
        # Deliberately out-of-order input list — output must follow
        # category_order, not the order items were passed in.
        shuffled = [
            _item(4, "Банани", "Фрукти та ягоди", "3 шт.", 3.0, "шт."),
            _item(1, "Курка", "М'ясо та риба", "1 шт.", 1.0, "шт."),
            _item(3, "Молоко", "Молочне та яйця", "1 л", 1.0, "л"),
            _item(2, "Ковбаски", "М'ясо та риба", "2 шт.", 2.0, "шт."),
        ]
        numbered = inventory._numbered_inventory_display_items(shuffled, _TEST_CATEGORY_ORDER, _TEST_DEFAULT_CATEGORY)
        self.assertEqual([item["id"] for _, item in numbered], [1, 2, 3, 4])


class TestNumberedDeleteSelectionBehavior(unittest.TestCase):
    """#4-#7: exact-match selection, whole-batch blocking on mismatch,
    deduplication, and protection against deleting the wrong item — pure
    inventory.py behavior, unchanged from before the extraction."""

    def _resolve(self, text, items):
        return inventory._resolve_numbered_inventory_delete_selection(
            text, items, bot._effective_quantity, _TEST_CATEGORY_ORDER, _TEST_DEFAULT_CATEGORY,
        )

    def test_exact_name_selects_the_right_item(self):
        items = _sample_items()
        kind, selected = self._resolve("1. Курка — 1 шт.", items)
        self.assertEqual(kind, "ok")
        self.assertEqual([it["id"] for it in selected], [1])

    def test_number_with_wrong_name_blocks_whole_batch(self):
        items = _sample_items()
        kind, payload = self._resolve("1. Курка — 1 шт.\n2. Молоко — 1 л", items)
        self.assertEqual(kind, "mismatch")
        # number 2 is actually "Ковбаски — 2 шт.", not "Молоко — 1 л" ->
        # blocks everything, including the otherwise-valid line 1.
        number, exists = payload
        self.assertEqual(number, 2)
        self.assertTrue(exists)

    def test_repeated_number_does_not_duplicate_delete_target(self):
        items = _sample_items()
        kind, selected = self._resolve("1. Курка — 1 шт.\n1. Курка — 1 шт.", items)
        self.assertEqual(kind, "ok")
        self.assertEqual([it["id"] for it in selected], [1])

    def test_invalid_number_cannot_delete_a_different_item(self):
        items = _sample_items()
        kind, payload = self._resolve("99. Курка", items)
        self.assertEqual(kind, "mismatch")
        number, exists = payload
        self.assertEqual(number, 99)
        self.assertFalse(exists)

    def test_mismatch_message_text_unchanged(self):
        self.assertEqual(
            inventory._format_numbered_delete_mismatch_message(5, False),
            "Не можу безпечно підтвердити вибір.\n\n"
            "Номер 5 не існує в поточному списку запасів.\n"
            "Покажи список запасів ще раз і вибери актуальний номер.",
        )
        self.assertEqual(
            inventory._format_numbered_delete_mismatch_message(5, True),
            "Не можу безпечно підтвердити вибір.\n\n"
            "Номер 5 зараз відповідає іншій позиції у запасах.\n"
            "Покажи список запасів ще раз і вибери актуальний номер."
        )


# =========================
# Compound inventory planning (V1.2)
# =========================
def _compound_items():
    return [
        {"id": 401, "name": "Вершки", "category": "Молочне та яйця",
         "quantity_value": None, "quantity_unit": None, "quantity_text": ""},
        {"id": 402, "name": "Приправа до курки", "category": "Соуси, спеції та бакалія",
         "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_text": "2 шт."},
        {"id": 403, "name": "Сосиски", "category": "М'ясо та риба",
         "quantity_value": 14.0, "quantity_unit": "шт.", "quantity_text": "14 шт."},
    ]


def _mixed_compound_operations():
    return [
        {"type": "remove_inventory", "item_number": 1},
        {"type": "consume_inventory_quantity", "item_number": 2, "quantity_value": 0.5, "quantity_unit": "шт."},
        {"type": "add_to_shopping", "name": "Приправа до курки", "quantity_value": 1, "quantity_unit": "шт.",
         "quantity_inferred": False, "category": "Соуси, спеції та бакалія", "is_consumable": True},
    ]


class TestCompoundPlanningModuleBoundaryIdentity(unittest.TestCase):
    """#1/#2: bot.py's compound-planning wrappers return exactly what the
    pure inventory.py functions return when given bot.py's own
    dependencies directly — no independent duplicate implementation."""

    def test_validate_wrapper_matches_pure_function_with_injected_deps(self):
        items = _compound_items()
        operations = _mixed_compound_operations()

        bot_result = bot._validate_compound_operations(operations, [], items)
        pure_result = inventory.validate_compound_operations(
            operations, [], items,
            bot.normalize_item_quantity, bot._auto_merge_in_place,
            bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY,
        )
        self.assertEqual(bot_result, pure_result)

    def test_preview_wrapper_matches_pure_function_with_injected_deps(self):
        items = _compound_items()
        operations = _mixed_compound_operations()
        _, payload = bot._validate_compound_operations(operations, [], items)

        bot_preview = bot._format_compound_preview(payload)
        pure_preview = inventory.format_compound_preview(payload, bot._effective_quantity)
        self.assertEqual(bot_preview, pure_preview)


class TestCompoundPlanningBehaviorUnchanged(unittest.TestCase):
    """#3/#4: a valid add+consume(+remove) plan produces the same preview as
    before, and an invalid part blocks the entire plan (all-or-nothing)."""

    def _validate(self, operations, items, unresolved_fragments=None):
        return inventory.validate_compound_operations(
            operations, unresolved_fragments or [], items,
            bot.normalize_item_quantity, bot._auto_merge_in_place,
            bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY,
        )

    def test_valid_add_and_consume_produce_expected_preview(self):
        items = _compound_items()
        operations = _mixed_compound_operations()
        kind, payload = self._validate(operations, items)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["inventory_changes"]), 2)
        self.assertTrue(payload["inventory_changes"][0]["will_remove"])
        self.assertEqual(len(payload["add_to_shopping"]), 1)

        preview = inventory.format_compound_preview(payload, bot._effective_quantity)
        self.assertIn("🧊 Буде змінено в запасах:", preview)
        self.assertIn("1. Вершки", preview)
        self.assertIn("буде прибрано із запасів", preview)
        self.assertIn("🛒 Буде додано до покупок:", preview)
        self.assertIn("• Приправа до курки — 1 шт.", preview)

    def test_invalid_part_blocks_the_whole_plan(self):
        items = _compound_items()
        operations = [
            {"type": "remove_inventory", "item_number": 1},
            # item_number 99 doesn't exist -> the whole plan is invalid,
            # including the otherwise-valid removal above.
            {"type": "consume_inventory_quantity", "item_number": 99, "quantity_value": 1, "quantity_unit": "шт."},
        ]
        kind, reasons = self._validate(operations, items)
        self.assertEqual(kind, "invalid")
        self.assertIn("Невідома позиція запасів.", reasons)


class TestCompoundPlanningDependencyInjection(unittest.TestCase):
    """#5/#6: the injected callbacks are actually used, and only where the
    original logic used them."""

    def test_normalize_item_quantity_callback_is_used_for_shopping_item(self):
        items = _compound_items()
        operations = [
            {"type": "add_to_shopping", "name": "Молоко", "quantity_value": None, "quantity_unit": None,
             "category": "Молочне та яйця", "is_consumable": True},
        ]
        calls = []

        def spy_normalize(name, quantity_text, quantity_value=None, quantity_unit=None,
                           allow_default_unit=False, alias_map=None):
            calls.append(name)
            return bot.normalize_item_quantity(
                name, quantity_text, quantity_value=quantity_value, quantity_unit=quantity_unit,
                allow_default_unit=allow_default_unit, alias_map=alias_map,
            )

        kind, payload = inventory.validate_compound_operations(
            operations, [], items,
            spy_normalize, bot._auto_merge_in_place,
            bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY,
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(calls, ["Молоко"])

    def test_merge_callback_is_not_called_when_nothing_goes_to_shopping(self):
        items = _compound_items()
        operations = [{"type": "remove_inventory", "item_number": 1}]
        merge_calls = []

        def spy_merge(shopping_raw):
            merge_calls.append(shopping_raw)
            return bot._auto_merge_in_place(shopping_raw)

        kind, payload = inventory.validate_compound_operations(
            operations, [], items,
            bot.normalize_item_quantity, spy_merge,
            bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY,
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["add_to_shopping"], [])
        self.assertEqual(merge_calls, [])

    def test_merge_callback_is_called_when_shopping_items_exist(self):
        items = _compound_items()
        operations = [
            {"type": "add_to_shopping", "name": "Молоко", "quantity_value": None, "quantity_unit": None,
             "category": "Молочне та яйця", "is_consumable": True},
        ]
        merge_calls = []

        def spy_merge(shopping_raw):
            merge_calls.append(len(shopping_raw))
            return bot._auto_merge_in_place(shopping_raw)

        inventory.validate_compound_operations(
            operations, [], items,
            bot.normalize_item_quantity, spy_merge,
            bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY,
        )
        self.assertEqual(merge_calls, [1])


class TestCompoundSnapshotStalenessForPlanningOutput(unittest.TestCase):
    """#7/#8: _compound_snapshot_is_stale, fed the exact inventory_changes
    shape validate_compound_operations produces, is unaffected by the
    extraction — unchanged state is not stale, a changed item is."""

    def test_unchanged_snapshot_from_a_real_plan_is_not_stale(self):
        items = _compound_items()
        _, payload = inventory.validate_compound_operations(
            [{"type": "remove_inventory", "item_number": 1}], [], items,
            bot.normalize_item_quantity, bot._auto_merge_in_place,
            bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY,
        )
        self.assertFalse(inventory._compound_snapshot_is_stale(payload["inventory_changes"], items))

    def test_changed_relevant_item_from_a_real_plan_is_stale(self):
        items = _compound_items()
        _, payload = inventory.validate_compound_operations(
            [{"type": "remove_inventory", "item_number": 1}], [], items,
            bot.normalize_item_quantity, bot._auto_merge_in_place,
            bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY,
        )
        changed_items = [dict(it) for it in items]
        changed_items[0]["quantity_value"] = 5.0
        changed_items[0]["quantity_unit"] = "шт."
        self.assertTrue(inventory._compound_snapshot_is_stale(payload["inventory_changes"], changed_items))


if __name__ == "__main__":
    unittest.main()
