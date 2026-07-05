"""list_editing.py — module boundary + shared shopping/inventory merge helpers.

Verifies that bot.py delegates to list_editing.py (same objects for the
zero-dependency helpers, thin wrappers injecting bot.py's own
canonicalize_name/_effective_quantity/normalize_item_quantity/
VALID_CATEGORIES/DEFAULT_CATEGORY for the rest) and that the moved pure
logic (merge-duplicates, preview-edit validation, saved-list merge, merge
snapshot targets) still behaves exactly as before. Does NOT re-test the
full webhook flows around these helpers — that's already covered by
test_pending_preview_logic.py, test_merge_stale_snapshot_protection.py,
test_noop_household_edits.py, test_saved_list_ai_router.py, and the
compound-inventory/global-router test files. No real Gemini, Telegram,
Render, or Supabase call happens anywhere in this file.
"""
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

import list_editing

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402


def _item(item_id, name, category, quantity_text, quantity_value=None, quantity_unit=None, canonical_name=None):
    return {
        "id": item_id, "name": name, "category": category, "quantity_text": quantity_text,
        "quantity_value": quantity_value, "quantity_unit": quantity_unit,
        "canonical_name": canonical_name or name.lower(), "quantity_inferred": False,
    }


class TestModuleHasNoForbiddenImports(unittest.TestCase):
    def test_module_file_only_imports_stdlib_and_quantities(self):
        path = os.path.join(os.path.dirname(__file__), "..", "list_editing.py")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        import_lines = [
            line.strip() for line in lines
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        forbidden_modules = ("bot", "database", "inventory", "household_router", "flask", "psycopg", "requests", "groq")
        for line in import_lines:
            for module in forbidden_modules:
                self.assertFalse(
                    line == f"import {module}" or line.startswith(f"from {module} "),
                    f"forbidden import found: {line!r}",
                )


class TestModuleBoundaryIdentity(unittest.TestCase):
    """#1/#10: bot.py's wrappers return exactly what list_editing.py's pure
    functions return when given bot.py's own dependencies directly — no
    independent duplicate implementation left in bot.py."""

    def test_zero_dependency_helpers_are_the_same_object(self):
        self.assertIs(bot._compute_merged_quantity, list_editing._compute_merged_quantity)
        self.assertIs(bot._apply_pending_merge, list_editing._apply_pending_merge)

    def test_auto_merge_in_place_wrapper_matches_pure_function(self):
        items = [
            {"name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "л", "quantity_text": "1 л", "quantity_inferred": False},
            {"name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 500.0, "quantity_unit": "мл", "quantity_text": "500 мл", "quantity_inferred": False},
        ]
        bot_result = bot._auto_merge_in_place(items)
        pure_result = list_editing._auto_merge_in_place(items, bot._effective_quantity, bot.canonicalize_name, bot.DEFAULT_CATEGORY)
        self.assertEqual(bot_result, pure_result)

    def test_names_can_merge_wrapper_matches_pure_function(self):
        item_a = {"name": "Молоко", "category": "Молочне та яйця"}
        item_b = {"name": "молоко", "category": "Молочне та яйця"}
        bot_result = bot.names_can_merge(item_a, item_b)
        pure_result = list_editing.names_can_merge(item_a, item_b, bot.canonicalize_name, bot.DEFAULT_CATEGORY)
        self.assertEqual(bot_result, pure_result)
        self.assertTrue(bot_result)

    def test_validate_merge_groups_wrapper_matches_pure_function(self):
        items = [
            {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "quantity_text": "1 л"},
            {"id": 2, "name": "Молоко", "category": "Молочне та яйця", "quantity_text": "500 мл"},
        ]
        raw_groups = [{"item_refs": [1, 2], "merged_name": "Молоко", "merged_category": "Молочне та яйця"}]
        bot_result = bot._validate_merge_groups(raw_groups, items)
        pure_result = list_editing._validate_merge_groups(raw_groups, items, bot.VALID_CATEGORIES, bot.DEFAULT_CATEGORY)
        self.assertEqual(bot_result, pure_result)


class TestSharedMergeBehaviorAcrossDomains(unittest.TestCase):
    """#2/#3/#4/#5: shopping and inventory candidates share the exact same
    merge outcome — compatible structured quantities merge without losing
    Decimal precision, incompatible units never merge, and text quantities
    are never silently promoted to structured ones."""

    def test_shopping_and_inventory_shaped_items_merge_the_same_way(self):
        shopping_items = [
            {"name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "л", "quantity_text": "1 л", "quantity_inferred": False},
            {"name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 500.0, "quantity_unit": "мл", "quantity_text": "500 мл", "quantity_inferred": False},
        ]
        inventory_items = [dict(it) for it in shopping_items]  # same shape, different domain

        shopping_result = bot._auto_merge_in_place(shopping_items)
        inventory_result = bot._auto_merge_in_place(inventory_items)
        self.assertEqual(len(shopping_result), 1)
        self.assertEqual(len(inventory_result), 1)
        self.assertEqual(shopping_result[0]["quantity_text"], inventory_result[0]["quantity_text"])
        self.assertEqual(shopping_result[0]["quantity_text"], "1,5 л")

    def test_compatible_quantities_merge_without_losing_decimal_precision(self):
        items = [
            {"name": "Шафран", "category": "Інше їстівне", "canonical_name": "шафран",
             "quantity_value": Decimal("0.00011"), "quantity_unit": "г", "quantity_text": "0,00011 г", "quantity_inferred": False},
            {"name": "Шафран", "category": "Інше їстівне", "canonical_name": "шафран",
             "quantity_value": Decimal("0.00011"), "quantity_unit": "г", "quantity_text": "0,00011 г", "quantity_inferred": False},
        ]
        result = bot._auto_merge_in_place(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["quantity_value"], Decimal("0.00022"))
        self.assertEqual(result[0]["quantity_text"], "0,00022 г")

    def test_incompatible_units_do_not_merge(self):
        items = [
            {"name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "л", "quantity_text": "1 л", "quantity_inferred": False},
            {"name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False},
        ]
        result = bot._auto_merge_in_place(items)
        self.assertEqual(len(result), 2)

    def test_text_quantities_are_not_silently_promoted_to_structured(self):
        merge_items = [
            {"name": "Сосиски", "quantity_text": "дві пачки"},
            {"name": "Сосиски", "quantity_text": "дві пачки"},
        ]
        # Both text and identical -> allowed to stay as the same free text,
        # never silently turned into a structured quantity.
        result = list_editing._compute_merged_quantity(merge_items)
        self.assertEqual(result, "дві пачки")

        conflicting = [
            {"name": "Сосиски", "quantity_text": "дві пачки"},
            {"name": "Сосиски", "quantity_text": "три пачки"},
        ]
        self.assertIsNone(list_editing._compute_merged_quantity(conflicting))


class TestSavedMergeUsesInjectedCanonicalizeName(unittest.TestCase):
    """#6: saved-list merge grouping relies on the injected canonicalize_name
    for items that don't already carry their own canonical_name."""

    def test_items_without_canonical_name_are_grouped_via_injected_callback(self):
        calls = []

        def spy_canonicalize(name):
            calls.append(name)
            return bot.canonicalize_name(name)

        items = [
            {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "quantity_text": ""},
            {"id": 2, "name": "молоко", "category": "Молочне та яйця", "quantity_text": ""},
        ]
        groups = list_editing._compute_saved_merge_groups(
            [[1, 2]], items, spy_canonicalize, bot._effective_quantity, bot.DEFAULT_CATEGORY,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["canonical_name"], "молоко")
        self.assertEqual(calls, ["Молоко", "молоко"])

    def test_bot_wrapper_matches_pure_function(self):
        items = [
            {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "quantity_text": ""},
            {"id": 2, "name": "молоко", "category": "Молочне та яйця", "quantity_text": ""},
        ]
        bot_result = bot._compute_saved_merge_groups([[1, 2]], items)
        pure_result = list_editing._compute_saved_merge_groups(
            [[1, 2]], items, bot.canonicalize_name, bot._effective_quantity, bot.DEFAULT_CATEGORY,
        )
        self.assertEqual(bot_result, pure_result)


class TestPreviewUpdatesRejectInvalidInput(unittest.TestCase):
    """#7: preview-edit updates reject an invalid category or a
    nonexistent target item_number — the whole update batch is rejected,
    not silently partially applied."""

    def test_invalid_category_rejects_the_whole_update(self):
        items = [{"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"}]
        updates = [{"item_number": 1, "category": "Неіснуюча категорія"}]
        result = bot._validate_preview_updates(updates, items)
        self.assertIsNone(result)

    def test_nonexistent_item_number_rejects_the_whole_update(self):
        items = [{"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"}]
        updates = [{"item_number": 5, "name": "Батон"}]
        result = bot._validate_preview_updates(updates, items)
        self.assertIsNone(result)

    def test_valid_update_is_accepted(self):
        items = [{"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"}]
        updates = [{"item_number": 1, "quantity_text": "2 шт."}]
        result = bot._validate_preview_updates(updates, items)
        self.assertEqual(result, [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}])


class TestMergeGroupMismatchBlocksWholePlan(unittest.TestCase):
    """#8: a malformed/mismatched merge group reference never produces a
    partial group list — it's simply dropped, never guessed."""

    def test_out_of_range_ref_drops_the_whole_group_not_a_partial_one(self):
        items = [
            {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "quantity_text": "1 л"},
        ]
        raw_groups = [{"item_refs": [1, 99], "merged_name": "Молоко", "merged_category": "Молочне та яйця"}]
        result = bot._validate_merge_groups(raw_groups, items)
        self.assertEqual(result, [])

    def test_incompatible_categories_drop_the_group(self):
        items = [
            {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "quantity_text": "1 л"},
            {"id": 2, "name": "Молоко", "category": "Соуси, спеції та бакалія", "quantity_text": "500 мл"},
        ]
        raw_groups = [{"item_refs": [1, 2], "merged_name": "Молоко", "merged_category": "Молочне та яйця"}]
        result = bot._validate_merge_groups(raw_groups, items)
        self.assertEqual(result, [])


class TestMergeSnapshotTargetsStable(unittest.TestCase):
    """#9: snapshot targets built from a saved-merge group stay stable
    (same values) for an unchanged item set — the shared stale-guard
    contract every confirm-flow relies on."""

    def test_targets_reflect_exact_current_values(self):
        validated_groups = [{
            "items": [
                _item(1, "Молоко", "Молочне та яйця", "1 л", 1.0, "л"),
                _item(2, "Молоко", "Молочне та яйця", "500 мл", 500.0, "мл"),
            ],
        }]
        targets = bot._merge_snapshot_targets(validated_groups)
        self.assertEqual(targets, [
            {"item_id": 1, "quantity_value": 1.0, "quantity_unit": "л", "canonical_name": "молоко", "category": "Молочне та яйця"},
            {"item_id": 2, "quantity_value": 500.0, "quantity_unit": "мл", "canonical_name": "молоко", "category": "Молочне та яйця"},
        ])
        # Calling again with the exact same input produces identical targets.
        self.assertEqual(targets, bot._merge_snapshot_targets(validated_groups))


if __name__ == "__main__":
    unittest.main()
