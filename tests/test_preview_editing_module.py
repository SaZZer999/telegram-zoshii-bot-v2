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


if __name__ == "__main__":
    unittest.main()
