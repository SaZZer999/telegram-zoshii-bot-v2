"""Preview Edit V1 — pure unit tests for preview_editing.py's deterministic
edit parser and patch validate/apply functions. No Flask/Telegram/DB/Gemini
involved at all — see tests/test_inventory_transform.py for the webhook-
level integration tests covering the full pending_inventory_transform edit
flow (routing, re-rendered preview, confirm/cancel)."""
import unittest
from decimal import Decimal

import preview_editing


class TestParseInventoryTransformEdit(unittest.TestCase):
    def test_yes_only_quantity_change(self):
        self.assertEqual(
            preview_editing.parse_inventory_transform_edit("так.тільки зроби М'ясних виробів — 2 шт"),
            {"action": "set_target_quantity", "quantity": "2 шт"},
        )

    def test_bare_zroby_name_and_quantity_only_changes_quantity(self):
        self.assertEqual(
            preview_editing.parse_inventory_transform_edit("зроби М'ясні вироби 2 шт"),
            {"action": "set_target_quantity", "quantity": "2 шт"},
        )

    def test_nazvy_tse_renames(self):
        self.assertEqual(
            preview_editing.parse_inventory_transform_edit("назви це М'ясо"),
            {"action": "set_target_name", "name": "М'ясо"},
        )

    def test_zamist_old_name_zroby_new_name_renames(self):
        self.assertEqual(
            preview_editing.parse_inventory_transform_edit("замість М'ясні вироби зроби М'ясо"),
            {"action": "set_target_name", "name": "М'ясо"},
        )

    def test_zamist_old_qty_zroby_new_qty_changes_quantity(self):
        self.assertEqual(
            preview_editing.parse_inventory_transform_edit("замість 8 шт зроби 2 шт"),
            {"action": "set_target_quantity", "quantity": "2 шт"},
        )

    def test_zroby_new_qty_zamist_old_qty_changes_quantity(self):
        self.assertEqual(
            preview_editing.parse_inventory_transform_edit("зроби 500 мл замість 0,5 л"),
            {"action": "set_target_quantity", "quantity": "500 мл"},
        )

    def test_unrecognized_text_returns_none(self):
        self.assertIsNone(preview_editing.parse_inventory_transform_edit("перейменуй ser на сир"))

    def test_destructive_text_returns_none(self):
        self.assertIsNone(preview_editing.parse_inventory_transform_edit("Видали все"))

    def test_blank_text_returns_none(self):
        self.assertIsNone(preview_editing.parse_inventory_transform_edit(""))

    def test_zroby_with_no_trailing_quantity_returns_none(self):
        # No safe trailing quantity to extract and "зроби" alone never
        # renames (see the module's own MAKE_RE docstring) — must not guess.
        self.assertIsNone(preview_editing.parse_inventory_transform_edit("зроби М'ясо"))


class TestValidateInventoryTransformPatch(unittest.TestCase):
    def test_unknown_action_rejected(self):
        ok, reason = preview_editing.validate_inventory_transform_patch({"action": "delete_target"})
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_not_a_dict_rejected(self):
        ok, reason = preview_editing.validate_inventory_transform_patch("set_target_quantity")
        self.assertFalse(ok)

    def test_set_target_quantity_missing_quantity_rejected(self):
        ok, reason = preview_editing.validate_inventory_transform_patch({"action": "set_target_quantity"})
        self.assertFalse(ok)

    def test_set_target_name_missing_name_rejected(self):
        ok, reason = preview_editing.validate_inventory_transform_patch({"action": "set_target_name", "name": "  "})
        self.assertFalse(ok)

    def test_set_target_requires_both_fields(self):
        ok, reason = preview_editing.validate_inventory_transform_patch({"action": "set_target", "name": "М'ясо"})
        self.assertFalse(ok)
        ok2, _ = preview_editing.validate_inventory_transform_patch(
            {"action": "set_target", "name": "М'ясо", "quantity": "2 шт"},
        )
        self.assertTrue(ok2)

    def test_valid_set_target_quantity_accepted(self):
        ok, reason = preview_editing.validate_inventory_transform_patch(
            {"action": "set_target_quantity", "quantity": "2 шт"},
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)


def _pending_data():
    return {
        "target_name": "М'ясні вироби", "target_canonical_name": "м'ясні вироби",
        "target_quantity_value": Decimal("8"), "target_quantity_unit": "шт.",
        "target_quantity_text": "8 шт.",
    }


def _canonicalize_stub(name):
    return name.strip().lower()


def _capitalize_stub(name):
    stripped = name.strip()
    return stripped[0].upper() + stripped[1:] if stripped else stripped


class TestApplyInventoryTransformPatch(unittest.TestCase):
    def test_set_target_quantity_updates_only_quantity_fields(self):
        data = _pending_data()
        ok, error = preview_editing.apply_inventory_transform_patch(
            data, {"action": "set_target_quantity", "quantity": "2 шт"}, _canonicalize_stub, _capitalize_stub,
        )
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(data["target_quantity_value"], Decimal("2"))
        self.assertEqual(data["target_quantity_unit"], "шт.")
        self.assertEqual(data["target_quantity_text"], "2 шт.")
        self.assertEqual(data["target_name"], "М'ясні вироби")

    def test_set_target_quantity_supports_unit_change(self):
        data = _pending_data()
        data["target_quantity_value"] = Decimal("0.5")
        data["target_quantity_unit"] = "л"
        data["target_quantity_text"] = "0,5 л"
        ok, error = preview_editing.apply_inventory_transform_patch(
            data, {"action": "set_target_quantity", "quantity": "500 мл"}, _canonicalize_stub, _capitalize_stub,
        )
        self.assertTrue(ok)
        self.assertEqual(data["target_quantity_value"], Decimal("500"))
        self.assertEqual(data["target_quantity_unit"], "мл")
        self.assertEqual(data["target_quantity_text"], "500 мл")

    def test_set_target_name_updates_only_name_fields(self):
        data = _pending_data()
        ok, error = preview_editing.apply_inventory_transform_patch(
            data, {"action": "set_target_name", "name": "М'ясо"}, _canonicalize_stub, _capitalize_stub,
        )
        self.assertTrue(ok)
        self.assertEqual(data["target_name"], "М'ясо")
        self.assertEqual(data["target_canonical_name"], "м'ясо")
        self.assertEqual(data["target_quantity_value"], Decimal("8"))
        self.assertEqual(data["target_quantity_text"], "8 шт.")

    def test_invalid_quantity_leaves_pending_data_unchanged(self):
        data = _pending_data()
        original = dict(data)
        ok, error = preview_editing.apply_inventory_transform_patch(
            data, {"action": "set_target_quantity", "quantity": "трохи"}, _canonicalize_stub, _capitalize_stub,
        )
        self.assertFalse(ok)
        self.assertTrue(error)
        self.assertEqual(data, original)

    def test_unknown_action_leaves_pending_data_unchanged(self):
        data = _pending_data()
        original = dict(data)
        ok, error = preview_editing.apply_inventory_transform_patch(
            data, {"action": "delete_target"}, _canonicalize_stub, _capitalize_stub,
        )
        self.assertFalse(ok)
        self.assertEqual(data, original)

    def test_unsupported_action_leaves_pending_data_unchanged(self):
        data = _pending_data()
        original = dict(data)
        ok, error = preview_editing.apply_inventory_transform_patch(
            data, {"action": "unsupported", "reason": "not sure"}, _canonicalize_stub, _capitalize_stub,
        )
        self.assertFalse(ok)
        self.assertEqual(error, "not sure")
        self.assertEqual(data, original)


def _shopping_items():
    """Two freshly-assumed shopping-add items, same shape household_router.
    build_add_preview_from_items/build_household_operations_preview produce:
    quantity_inferred=True, "1 шт." default."""
    return [
        {
            "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
            "quantity_inferred": True, "is_consumable": True,
        },
        {
            "name": "Сир", "canonical_name": "сир", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
            "quantity_inferred": True, "is_consumable": True,
        },
    ]


class TestParseHouseholdAddPreviewEdit(unittest.TestCase):
    def test_two_named_edits_with_a_conjunction(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("молока 1 л, а сиру 500 г", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")
        self.assertEqual(items[0]["quantity_inferred"], False)
        self.assertEqual(items[1]["quantity_text"], "500 г")
        self.assertEqual(items[1]["quantity_inferred"], False)

    def test_two_named_edits_plain(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("молоко 1 л, сир 500 г", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")
        self.assertEqual(items[1]["quantity_text"], "500 г")

    def test_tilky_prefix_with_conjunction(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("тільки молока 1 л, а сиру 500 г", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")
        self.assertEqual(items[1]["quantity_text"], "500 г")

    def test_positional_shorthand_maps_by_order(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("1 л, 500 г", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")
        self.assertEqual(items[1]["quantity_text"], "500 г")

    def test_positional_shorthand_english_units(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("1L, 500g", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")
        self.assertEqual(items[1]["quantity_text"], "500 г")

    def test_zroby_single_item_no_name_needed(self):
        items = _shopping_items()[:1]
        ok, edits = preview_editing.parse_household_add_preview_edit("зроби молоко 1 л", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")

    def test_zamist_old_qty_zroby_new_qty(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("замість молока 1 шт зроби 1 л", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")
        self.assertEqual(items[1]["quantity_text"], "1 шт.")  # untouched

    def test_zamist_zroby_renames(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("замість сир зроби творог", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[1]["name"], "Творог")
        self.assertEqual(items[1]["canonical_name"], "творог")
        self.assertEqual(items[0]["name"], "Молоко")  # untouched

    def test_pereimenuy_na_renames(self):
        items = _shopping_items()
        ok, edits = preview_editing.parse_household_add_preview_edit("перейменуй сир на творог", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[1]["name"], "Творог")

    def test_word_numbers_are_not_overbuilt_returns_unparseable(self):
        items = _shopping_items()
        ok, result = preview_editing.parse_household_add_preview_edit("один літр, пʼятсот грам", items)
        self.assertFalse(ok)
        self.assertIsNone(result)

    def test_invalid_quantity_word_returns_unparseable(self):
        items = _shopping_items()
        original = [dict(it) for it in items]
        ok, result = preview_editing.parse_household_add_preview_edit("молока багато", items)
        self.assertFalse(ok)
        self.assertIsNone(result)
        self.assertEqual(items, original)

    def test_ambiguous_name_matching_two_items_asks_to_clarify(self):
        items = [
            {"name": "Молоко", "canonical_name": "молоко", "quantity_value": Decimal("1"),
             "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": True},
            {"name": "Молоко", "canonical_name": "молоко", "quantity_value": Decimal("1"),
             "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": True},
        ]
        original = [dict(it) for it in items]
        ok, message = preview_editing.parse_household_add_preview_edit("молоко 1 л", items)
        self.assertFalse(ok)
        self.assertEqual(message, preview_editing.HOUSEHOLD_EDIT_AMBIGUOUS_MSG)
        self.assertEqual(items, original)

    def test_positional_count_mismatch_does_not_guess(self):
        items = _shopping_items()
        original = [dict(it) for it in items]
        ok, message = preview_editing.parse_household_add_preview_edit("1 л, 500 г, 2 шт", items)
        self.assertFalse(ok)
        self.assertEqual(message, preview_editing.HOUSEHOLD_EDIT_POSITIONAL_MISMATCH_MSG)
        self.assertEqual(items, original)

    def test_item_not_found_returns_controlled_message(self):
        items = _shopping_items()
        ok, message = preview_editing.parse_household_add_preview_edit("банан 1 л", items)
        self.assertFalse(ok)
        self.assertEqual(message, preview_editing.HOUSEHOLD_EDIT_NOT_FOUND_MSG)

    def test_inventory_add_preview_single_item_edit(self):
        items = [{
            "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
            "quantity_inferred": True,
        }]
        ok, edits = preview_editing.parse_household_add_preview_edit("молока 1 л", items)
        self.assertTrue(ok)
        preview_editing.apply_household_add_preview_edits(items, edits, _canonicalize_stub, _capitalize_stub)
        self.assertEqual(items[0]["quantity_text"], "1 л")
        self.assertEqual(items[0]["quantity_inferred"], False)

    def test_blank_text_unparseable(self):
        ok, result = preview_editing.parse_household_add_preview_edit("", _shopping_items())
        self.assertFalse(ok)
        self.assertIsNone(result)

    def test_unrelated_text_unparseable(self):
        ok, result = preview_editing.parse_household_add_preview_edit("Купив банани", _shopping_items())
        self.assertFalse(ok)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
