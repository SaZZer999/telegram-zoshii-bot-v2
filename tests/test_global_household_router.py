import sys
import os
import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. Importing bot also runs
# household_router.configure(...) as a side effect (bot.py's own last lines),
# so household_router is already wired against the real bot.py quantity/
# consumption helpers by the time this file's tests run.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
import household_router
import expenses


NOW = datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _sausage_item():
    return {"id": 501, "name": "Ковбаски", "category": "М'ясо та риба",
             "quantity_value": 14.0, "quantity_unit": "шт.", "quantity_text": "14 шт."}


def _expense(expense_id, amount, category="Продукти", description="Булочка"):
    return {
        "id": expense_id, "amount": amount, "currency": "PLN", "category": category,
        "description": description, "expense_date": date(2026, 7, 3),
        "created_at": datetime(2026, 7, 3, 12, 0),
    }


class TestGate(unittest.TestCase):
    def test_buy_plan_phrases_match(self):
        self.assertTrue(household_router.gate("Планую купити булочку"))
        self.assertTrue(household_router.gate("Хочу купити молоко"))
        self.assertTrue(household_router.gate("Треба купити хліб"))

    def test_bought_phrases_match(self):
        self.assertTrue(household_router.gate("Купив масло за 10 zł"))
        self.assertTrue(household_router.gate("Купила хліб"))

    def test_consume_phrases_match(self):
        self.assertTrue(household_router.gate("З'їв 2 ковбаски"))
        self.assertTrue(household_router.gate("Використала 200 г масла"))

    def test_mistake_expense_phrases_match(self):
        self.assertTrue(household_router.gate("Булочку до витрат я додав випадково"))
        self.assertTrue(household_router.gate("Помилково записав ту витрату"))

    def test_bare_amount_does_not_match(self):
        self.assertFalse(household_router.gate("Biedronka 86,40 zł — продукти"))
        self.assertFalse(household_router.gate("86 zł"))

    def test_imperative_delete_does_not_match(self):
        self.assertFalse(household_router.gate("Видали витрату Biedronka 86,40 zł"))
        self.assertFalse(household_router.gate("Скасуй витрату"))

    def test_ordinary_question_does_not_match(self):
        self.assertFalse(household_router.gate("Яка сьогодні погода?"))

    def test_empty_text_does_not_match(self):
        self.assertFalse(household_router.gate(""))
        self.assertFalse(household_router.gate("   "))


class TestValidateOperations(unittest.TestCase):
    def test_intent_none_returns_none(self):
        router_result = {"intent": "none", "operations": [], "unresolved_fragments": []}
        kind, payload = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "none")
        self.assertIsNone(payload)

    def test_add_shopping_only_defaults_quantity_and_marks_inferred(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_shopping", "name": "Булочка", "quantity_text": "", "category": "Хліб і випічка"}],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["add_shopping_items"]), 1)
        item = payload["add_shopping_items"][0]
        self.assertEqual(item["name"], "Булочка")
        self.assertEqual(item["quantity_value"], 1.0)
        self.assertEqual(item["quantity_unit"], "шт.")
        self.assertTrue(item["quantity_inferred"])
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertIsNone(payload["new_expense"])

    def test_bought_with_price_produces_inventory_and_expense(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Масло", "quantity_text": "", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Масло", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.assertTrue(payload["add_inventory_items"][0]["quantity_inferred"])
        self.assertIsNotNone(payload["new_expense"])
        self.assertEqual(payload["new_expense"]["amount"], Decimal("10.00"))
        self.assertEqual(payload["new_expense"]["category"], "Продукти")

    def test_consume_inventory_resolves_against_snapshot(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "consume_inventory", "item_number": 1, "quantity_value": 2, "quantity_unit": "шт."}],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [_sausage_item()], [], NOW)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["consume_changes"]), 1)
        change = payload["consume_changes"][0]
        self.assertEqual(change["item_id"], 501)
        self.assertEqual(change["new_value"], 12.0)
        self.assertFalse(change["will_remove"])

    def test_consume_more_than_available_is_invalid(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "consume_inventory", "item_number": 1, "quantity_value": 100, "quantity_unit": "шт."}],
            "unresolved_fragments": [],
        }
        kind, reasons = household_router._validate_operations(router_result, [_sausage_item()], [], NOW)
        self.assertEqual(kind, "invalid")
        self.assertTrue(reasons)

    def test_delete_expense_resolves_exactly_one_match(self):
        expenses_list = [_expense(101, Decimal("4.00"), description="Булочка")]
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "delete_expense", "selected_numbers": [1]}],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [], expenses_list, NOW)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["delete_expense"]["expense_id"], 101)

    def test_delete_expense_ambiguous_is_invalid(self):
        expenses_list = [
            _expense(101, Decimal("4.00"), description="Булочка"),
            _expense(102, Decimal("4.00"), description="Пряник"),
        ]
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "delete_expense", "selected_numbers": [1, 2]}],
            "unresolved_fragments": [],
        }
        kind, reasons = household_router._validate_operations(router_result, [], expenses_list, NOW)
        self.assertEqual(kind, "invalid")
        self.assertTrue(reasons)

    def test_mixed_delete_and_add_shopping(self):
        expenses_list = [_expense(101, Decimal("4.00"), description="Булочка")]
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "delete_expense", "selected_numbers": [1]},
                {"type": "add_shopping", "name": "Булочка", "quantity_text": "", "category": "Хліб і випічка"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [], expenses_list, NOW)
        self.assertEqual(kind, "ok")
        self.assertIsNotNone(payload["delete_expense"])
        self.assertEqual(len(payload["add_shopping_items"]), 1)

    def test_unresolved_fragments_block_entire_compound_result(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_shopping", "name": "Булочка", "quantity_text": "", "category": "Хліб і випічка"}],
            "unresolved_fragments": ["щось незрозуміле"],
        }
        kind, fragments = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "unresolved")
        self.assertEqual(fragments, ["щось незрозуміле"])

    def test_more_than_one_add_expense_is_accepted_as_a_batch(self):
        """Multi-Expense Batch v1: several add_expense operations in one
        message are no longer rejected — they all land in new_expenses, in
        the same order Gemini returned them, and the legacy singular
        new_expense stays None since there's no longer a single "the"
        expense to show under that back-compat key."""
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "A", "expense_date": "2026-07-05"},
                {"type": "add_expense", "amount": "20", "currency": "PLN", "category": "Продукти",
                 "description": "B", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["new_expenses"]), 2)
        self.assertEqual(payload["new_expenses"][0]["description"], "A")
        self.assertEqual(payload["new_expenses"][1]["description"], "B")
        self.assertIsNone(payload["new_expense"])

    def test_empty_operations_list_is_none(self):
        router_result = {"intent": "household_operations", "operations": [], "unresolved_fragments": []}
        kind, payload = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "none")


class TestAmountMustBeLiterallyTyped(unittest.TestCase):
    """Live-bug regression: Gemini must never turn a discount calculation
    or a summed-up price into an add_expense amount the user never typed —
    see _amount_literally_in_text's own docstring. Only
    _validate_operations_detailed (via build_household_operations_preview,
    which HAS the original text) enforces this; the legacy
    _validate_operations wrapper (no source_text) is untouched — see
    test_empty_operations_list_is_none and every other TestValidateOperations
    case above, none of which pass source_text and all of which still pass."""

    def test_computed_discount_amount_is_rejected(self):
        # Purchase Event Planner V1: a discount-computed amount is no longer
        # a blocking "invalid" result — the item itself (Печиво) is still
        # safe to preview, so this is now "ok" with an empty new_expenses
        # and a non-blocking expense_notes warning instead. The core safety
        # guarantee this test exists for is unchanged: the fabricated "10"
        # NEVER reaches new_expenses/DB, discount or not.
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Печиво", "quantity_text": "0,5 кг",
                 "category": "Солодке та снеки"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Печиво", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        text = (
            "Вчора в магазині позаду дому я купив печиво по знижці, воно коштувало 20, "
            "але на нього було 50% знижки. Тому я взяв пів кілограма, але потім вернувся "
            "і докупив ще раз так само."
        )
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text=text,
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["new_expenses"], [])
        self.assertIsNone(payload["new_expense"])
        self.assertTrue(payload["expense_notes"])
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        self.assertEqual(payload["add_inventory_items"][0]["name"], "Печиво")

    def test_explicit_typed_amount_is_accepted(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text="Купив молоко за 10 zł",
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["new_expenses"][0]["amount"], Decimal("10.00"))

    def test_decimal_typed_amount_with_comma_is_accepted(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_expense", "amount": "12.40", "currency": "PLN", "category": "Продукти",
                 "description": "Хліб", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(
            router_result, [], [], NOW, source_text="Хліб 12,40 zł",
        )
        self.assertEqual(kind, "ok")

    def test_no_source_text_never_retroactively_blocks_legacy_callers(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_expense", "amount": "999", "currency": "PLN", "category": "Продукти",
                 "description": "X", "expense_date": "2026-07-05"},
            ],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations_detailed(router_result, [], [], NOW)
        self.assertEqual(kind, "ok")


class TestFormatPreview(unittest.TestCase):
    def test_preview_contains_expected_sections(self):
        payload = {
            "add_shopping_items": [],
            "add_inventory_items": [{
                "name": "Масло", "category": "Молочне та яйця",
                "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_inferred": True,
                "quantity_text": "1 шт.",
            }],
            "consume_changes": [],
            "new_expense": {
                "amount": Decimal("10.00"), "currency": "PLN", "category": "Продукти",
                "category_was_defaulted": False, "description": "Масло", "expense_date": date(2026, 7, 5),
            },
            "delete_expense": None,
        }
        text = household_router.format_preview(payload)
        self.assertIn("План змін:", text)
        self.assertIn("🧊 Запаси", text)
        self.assertIn("Додати Масло", text)
        self.assertIn("(припущення)", text)
        self.assertIn("💸 Витрати", text)
        self.assertIn("10,00 zł", text)
        self.assertIn("✅ Так, застосувати", text)
        self.assertIn("❌ Скасувати", text)


class TestPartALocalExpenseMatch(unittest.TestCase):
    def test_single_exact_match_resolves_locally(self):
        expenses_list = [_expense(101, Decimal("4.00"), description="Масло")]
        match = expenses._find_exact_expense_match("Масло", expenses_list)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 101)

    def test_multiple_matches_do_not_resolve(self):
        expenses_list = [
            _expense(101, Decimal("4.00"), description="Масло"),
            _expense(102, Decimal("9.00"), description="Масло"),
        ]
        self.assertIsNone(expenses._find_exact_expense_match("Масло", expenses_list))

    def test_no_match_returns_none(self):
        expenses_list = [_expense(101, Decimal("4.00"), description="Булочка")]
        self.assertIsNone(expenses._find_exact_expense_match("Масло", expenses_list))

    def test_case_and_whitespace_insensitive(self):
        expenses_list = [_expense(101, Decimal("4.00"), description="Масло")]
        self.assertIsNotNone(expenses._find_exact_expense_match("  МАСЛО  ", expenses_list))

    def test_bare_number_never_matches(self):
        expenses_list = [_expense(101, Decimal("4.00"), description="Масло")]
        self.assertIsNone(expenses._find_exact_expense_match("2", expenses_list))


if __name__ == '__main__':
    unittest.main()
