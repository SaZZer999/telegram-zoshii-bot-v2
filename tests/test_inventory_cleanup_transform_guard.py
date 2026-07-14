"""Inventory Cleanup Merge vs Transform Guard v1 — focused tests for
inventory.parse_inventory_cleanup_request's new guard against swallowing a
transform-shaped message ("В запасах об'єднай сосиски і мисливські ковбаски
і запиши як м'ясні вироби") as a single-product duplicate-merge search, and
for inventory.normalize_delete_quantity_hint (the Inventory Action Planner
V1's own reuse of the confirmed natural-quantity delete fix). Pure unit
tests, no Telegram/DB/Gemini involved."""
import sys
import os
import unittest

sys.modules.setdefault('database', __import__('unittest.mock', fromlist=['MagicMock']).MagicMock())
sys.modules.setdefault('groq', __import__('unittest.mock', fromlist=['MagicMock']).MagicMock())
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import inventory  # noqa: E402


class TestCleanupGuardRejectsTransformShapes(unittest.TestCase):
    def test_prefixed_ob_yednay_zapyshy_yak_is_rejected(self):
        # The exact originally-reported live bug: a leading "В запасах"
        # prefix defeats parse_inventory_transform_request's anchored
        # grammar, and "і запиши як Z" isn't the "в"/"у"/"на" preposition it
        # requires either — this must now fall through (None, None) instead
        # of being claimed as a single-product cleanup search for "сосиски і
        # мисливські ковбаски і запиши як м'ясні вироби".
        result = inventory.parse_inventory_cleanup_request(
            "В запасах об'єднай сосиски і мисливські ковбаски і запиши як м'ясні вироби"
        )
        self.assertEqual(result, (None, None))

    def test_arrow_notation_after_trigger_is_rejected(self):
        result = inventory.parse_inventory_cleanup_request("об'єднай сосиски -> м'ясні вироби")
        self.assertEqual(result, (None, None))

    def test_plus_join_after_trigger_is_rejected(self):
        result = inventory.parse_inventory_cleanup_request("об'єднай сосиски + ковбаски")
        self.assertEqual(result, (None, None))

    def test_nazvy_yak_target_clause_is_rejected(self):
        result = inventory.parse_inventory_cleanup_request("об'єднай сосиски і ковбаски, назви як м'ясо")
        self.assertEqual(result, (None, None))

    def test_nazvy_tse_target_clause_is_rejected(self):
        result = inventory.parse_inventory_cleanup_request("об'єднай це в одну позицію")
        self.assertEqual(result, (None, None))

    def test_peretvory_shape_after_trigger_is_rejected(self):
        result = inventory.parse_inventory_cleanup_request("об'єднай, перетвори сосиски і ковбаски на м'ясо")
        self.assertEqual(result, (None, None))


class TestCleanupGuardDoesNotBreakNormalDuplicateMerge(unittest.TestCase):
    def test_ob_yednay_moloko_still_a_cleanup_search(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("Об'єднай молоко"), (False, "молоко"))

    def test_ob_yednay_usi_zapysy_moloka_still_a_cleanup_search(self):
        self.assertEqual(
            inventory.parse_inventory_cleanup_request("Об'єднай усі записи молока"),
            (False, "усі записи молока"),
        )

    def test_ob_yednay_dublikaty_moloka_v_zapasakh_still_a_cleanup_search(self):
        self.assertEqual(
            inventory.parse_inventory_cleanup_request("Об'єднай дублікати молока в запасах"),
            (False, "молока"),
        )

    def test_prybery_dublikaty_syru_still_a_cleanup_search(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("Прибери дублікати сиру"), (False, "сиру"))

    def test_referential_followup_still_recognized(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("Об'єднай их"), (True, None))

    def test_product_name_with_ordinary_preposition_not_affected(self):
        # A location-suffix "в запасах"/"у запасах" is already stripped
        # before the guard ever runs — a bare trailing preposition alone
        # (not "в/у одну позицію", not "перетвор...", not "запиши/назви як")
        # must never trip the guard.
        self.assertEqual(
            inventory.parse_inventory_cleanup_request("Об'єднай дублікати соусу для м'яса"),
            (False, "соусу для м'яса"),
        )


class TestNormalizeDeleteQuantityHint(unittest.TestCase):
    def test_odna_shtuka_phrase_normalizes_to_1_sht(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("одна штука"), "1 шт.")

    def test_odnu_shtuku_phrase_normalizes_to_1_sht(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("одну штуку"), "1 шт.")

    def test_bare_odna_normalizes_to_1_sht(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("одна"), "1 шт.")

    def test_bare_odne_normalizes_to_1_sht(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("одне"), "1 шт.")

    def test_bare_odyn_normalizes_to_1_sht(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("один"), "1 шт.")

    def test_numeric_1_sht_normalizes_to_canonical_form(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("1 шт"), "1 шт.")

    def test_numeric_liters_normalizes_to_canonical_form(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("14,5 л"), "14,5 л")

    def test_word_quantity_para_returned_unchanged(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("пару"), "пару")

    def test_none_input_returns_none(self):
        self.assertIsNone(inventory.normalize_delete_quantity_hint(None))

    def test_blank_input_returns_none(self):
        self.assertIsNone(inventory.normalize_delete_quantity_hint("   "))

    def test_unparseable_text_returned_unchanged(self):
        self.assertEqual(inventory.normalize_delete_quantity_hint("трохи"), "трохи")


if __name__ == "__main__":
    unittest.main()
