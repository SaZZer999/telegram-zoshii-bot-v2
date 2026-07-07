"""Household Read Context V1 — module boundary + behavior tests.

household_read_context.py imports nothing from bot.py/database.py/Flask/
Telegram/psycopg/any Gemini SDK — every dependency here is a plain fake
HouseholdReadDeps built directly in this file, no sys.modules mocking, no
real Gemini/Supabase/Telegram call anywhere.

Does NOT re-test resolve_item_name/canonicalize_name's own normalization
rules (that's test_name_quantity_normalization.py) or dispatcher routing
precedence (that's test_routing_precedence_contract.py/test_message_
dispatcher_v3_phase_d.py) — only household_read_context.py's own intent
recognition, fact lookup, and "never fabricate" guarantees.
"""
import unittest
from unittest.mock import MagicMock

import household_read_context as hrc

CATEGORY_ORDER = [
    "М'ясо та риба", "Молочне та яйця", "Овочі та зелень",
    "Фрукти та ягоди", "Хліб і випічка", "Крупи, макарони та борошно",
    "Соуси, спеції та бакалія", "Солодке та снеки",
    "Напої", "Заморожене", "Інше їстівне",
]

_NAME_SYNONYMS = {"ser": "сир", "mleko": "молоко"}


def _canonicalize_name(name):
    base = (name or "").strip().lower()
    return _NAME_SYNONYMS.get(base, base)


def _resolve_item_name(name, alias_map):
    key = (name or "").strip().lower()
    if alias_map and key in alias_map:
        entry = alias_map[key]
        return entry["target_display_name"], entry["target_canonical_name"]
    return name, _canonicalize_name(name)


def _format_quantity_display(value, unit):
    if value is None:
        return ""
    text = f"{value:g}"
    return f"{text} {unit}" if unit else text


def _make_deps(inventory_items=None, shopping_items=None, alias_map=None,
               call_gemini=None, **overrides):
    defaults = dict(
        get_household_and_user=MagicMock(return_value=(1, 10)),
        get_inventory_items=MagicMock(return_value=inventory_items or []),
        get_active_shopping_items=MagicMock(return_value=shopping_items or []),
        get_household_alias_map=MagicMock(return_value=alias_map or {}),
        resolve_item_name=_resolve_item_name,
        canonicalize_name=_canonicalize_name,
        format_quantity_display=_format_quantity_display,
        format_inventory_list=lambda items: (
            "Запаси поки порожні." if not items else "🧊 Запаси:\n" + "\n".join(i["name"] for i in items)
        ),
        format_shopping_list=lambda items: (
            "Список покупок поки порожній." if not items else "🛒 Список покупок:\n" + "\n".join(i["name"] for i in items)
        ),
        call_gemini=call_gemini or MagicMock(return_value=None),
        send_message=MagicMock(),
        category_order=CATEGORY_ORDER,
    )
    defaults.update(overrides)
    return hrc.HouseholdReadDeps(**defaults)


def _milk_row(**overrides):
    row = {"id": 1, "name": "Молоко", "category": "Молочне та яйця",
            "canonical_name": "молоко", "quantity_text": "1 л",
            "quantity_value": 1, "quantity_unit": "л", "quantity_inferred": False}
    row.update(overrides)
    return row


class InventoryPresenceTests(unittest.TestCase):
    def test_chy_ie_molokо_finds_structured_row_without_gemini(self):
        call_gemini = MagicMock(return_value=None)
        deps = _make_deps(inventory_items=[_milk_row()], call_gemini=call_gemini)

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Чи є в нас молоко?")

        self.assertTrue(handled)
        call_gemini.assert_not_called()
        deps.send_message.assert_called_once()
        message = deps.send_message.call_args[0][1]
        self.assertIn("Так, є:", message)
        self.assertIn("Молоко — 1 л", message)

    def test_missing_product_gives_honest_not_found(self):
        deps = _make_deps(inventory_items=[])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Є сир?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("немає в запасах", message)
        self.assertIn("сир", message)

    def test_legacy_ser_row_found_as_syr_without_db_rewrite(self):
        row = _milk_row(id=2, name="ser", category="Молочне та яйця",
                         canonical_name=None, quantity_text="300 г",
                         quantity_value=300, quantity_unit="г")
        deps = _make_deps(inventory_items=[row])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Є сир?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("ser", message)  # raw legacy row name shown as-is, never rewritten
        self.assertNotIn("немає", message)

    def test_legacy_mleko_row_found_as_moloko(self):
        row = _milk_row(id=3, name="mleko", canonical_name=None,
                         quantity_text="", quantity_value=None, quantity_unit=None)
        deps = _make_deps(inventory_items=[row])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Чи є молоко?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("mleko", message)
        self.assertNotIn("немає", message)

    def test_household_alias_wins_over_builtin_synonym(self):
        alias_map = {"ser": {"target_display_name": "Сир пармезан", "target_canonical_name": "сир пармезан"}}
        parmesan_row = _milk_row(id=4, name="Сир пармезан", category="Молочне та яйця",
                                  canonical_name="сир пармезан", quantity_text="1 шт.",
                                  quantity_value=1, quantity_unit="шт.")
        plain_syr_row = _milk_row(id=5, name="Сир", canonical_name="сир",
                                    quantity_text="300 г", quantity_value=300, quantity_unit="г")
        deps = _make_deps(inventory_items=[parmesan_row, plain_syr_row], alias_map=alias_map)

        # User asks using the ALIAS TEXT itself ("ser") — without the alias,
        # the built-in synonym would translate "ser" -> "сир" and match the
        # plain "Сир" row instead; the alias must win and redirect to the
        # parmesan row only.
        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Чи є ser?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("Сир пармезан", message)
        self.assertNotIn("300 г", message)  # plain "Сир" row must NOT match — alias overrides built-in synonym

    def test_multiple_canonical_matches_all_shown(self):
        row_a = _milk_row(id=6, name="ser", canonical_name=None,
                           quantity_text="", quantity_value=None, quantity_unit=None)
        row_b = _milk_row(id=7, name="Сир", canonical_name="сир",
                           quantity_text="300 г", quantity_value=300, quantity_unit="г")
        deps = _make_deps(inventory_items=[row_a, row_b])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Є сир?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("Знайшов декілька позицій", message)
        self.assertIn("ser", message)
        self.assertIn("Сир", message)
        self.assertIn("300 г", message)


class InventoryCategoryTests(unittest.TestCase):
    def test_category_question_shows_only_matching_category(self):
        milk = _milk_row()
        meat = _milk_row(id=8, name="Курка", category="М'ясо та риба",
                          canonical_name="курка", quantity_text="", quantity_value=None, quantity_unit=None)
        deps = _make_deps(inventory_items=[milk, meat])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Що є з молочного?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("Молоко", message)
        self.assertNotIn("Курка", message)

    def test_empty_category_gives_honest_empty_message(self):
        milk = _milk_row()
        deps = _make_deps(inventory_items=[milk])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Яке м'ясо є вдома?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("нічого немає", message)


class OverviewTests(unittest.TestCase):
    def test_inventory_overview_uses_format_inventory_list(self):
        milk = _milk_row()
        deps = _make_deps(inventory_items=[milk])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Що є вдома?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("Запаси:", message)
        self.assertIn("Молоко", message)


class ShoppingListTests(unittest.TestCase):
    def test_shopping_overview_shows_active_list(self):
        bread = {"id": 9, "name": "Хліб", "category": "Хліб і випічка",
                  "canonical_name": "хліб", "quantity_text": "2 шт.",
                  "quantity_value": 2, "quantity_unit": "шт.", "quantity_inferred": False}
        deps = _make_deps(shopping_items=[bread])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Що треба купити?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("Список покупок:", message)
        self.assertIn("Хліб", message)

    def test_empty_shopping_list_gives_honest_empty_message(self):
        deps = _make_deps(shopping_items=[])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Що у списку покупок?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertEqual(message, "Список покупок поки порожній.")

    def test_bread_present_in_shopping_list(self):
        bread = {"id": 9, "name": "Хліб", "category": "Хліб і випічка",
                  "canonical_name": "хліб", "quantity_text": "2 шт.",
                  "quantity_value": 2, "quantity_unit": "шт.", "quantity_inferred": False}
        deps = _make_deps(shopping_items=[bread])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Чи є хліб у покупках?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("Так, у списку покупок є:", message)
        self.assertIn("Хліб", message)

    def test_bread_absent_from_shopping_list_never_claims_already_bought(self):
        deps = _make_deps(shopping_items=[])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Хліб ще є у списку покупок?")

        self.assertTrue(handled)
        message = deps.send_message.call_args[0][1]
        self.assertIn("немає", message)
        self.assertNotIn("купили", message)
        self.assertNotIn("куплений", message)


class GeminiClassifierTests(unittest.TestCase):
    def test_nonstandard_phrase_uses_topic_gate_and_mocked_classifier(self):
        call_gemini = MagicMock(return_value='{"intent": "inventory_presence", "product": "молоко", "category": null}')
        deps = _make_deps(inventory_items=[_milk_row()], call_gemini=call_gemini)

        handled = hrc.try_handle_household_read(
            deps, 1, 555, "Тест", "Молока у нас ще хоч трохи лишилося?"
        )

        self.assertTrue(handled)
        call_gemini.assert_called_once()
        message = deps.send_message.call_args[0][1]
        self.assertIn("Так, є:", message)

    def test_invalid_classifier_output_returns_false(self):
        call_gemini = MagicMock(return_value="not valid json")
        deps = _make_deps(inventory_items=[_milk_row()], call_gemini=call_gemini)

        handled = hrc.try_handle_household_read(
            deps, 1, 555, "Тест", "Молока у нас ще хоч трохи лишилося?"
        )

        self.assertFalse(handled)
        deps.send_message.assert_not_called()

    def test_gemini_unavailable_returns_false(self):
        call_gemini = MagicMock(return_value=None)
        deps = _make_deps(inventory_items=[_milk_row()], call_gemini=call_gemini)

        handled = hrc.try_handle_household_read(
            deps, 1, 555, "Тест", "Молока у нас ще хоч трохи лишилося?"
        )

        self.assertFalse(handled)
        deps.send_message.assert_not_called()

    def test_ambiguous_product_never_fabricated(self):
        call_gemini = MagicMock(return_value='{"intent": "none", "product": null, "category": null}')
        deps = _make_deps(inventory_items=[_milk_row()], call_gemini=call_gemini)

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Чи є щось до кави?")

        self.assertFalse(handled)
        deps.send_message.assert_not_called()

    def test_ordinary_chat_never_reaches_classifier(self):
        call_gemini = MagicMock(return_value=None)
        deps = _make_deps(call_gemini=call_gemini)

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Розкажи анекдот")

        self.assertFalse(handled)
        call_gemini.assert_not_called()
        deps.send_message.assert_not_called()


class RoutingBoundaryTests(unittest.TestCase):
    def test_write_command_text_is_not_a_household_read_question(self):
        """household_read_context.py itself never claims a write-shaped
        message — the real routing-precedence guarantee (that write routes
        are tried first and household_read is never reached at all while
        they still apply) lives in message_dispatcher.py/bot.py's dispatcher
        wiring, exercised by test_routing_precedence_contract.py."""
        deps = _make_deps(inventory_items=[_milk_row()])

        handled = hrc.try_handle_household_read(deps, 1, 555, "Тест", "Купив хліб за 10 zł")

        self.assertFalse(handled)
        deps.send_message.assert_not_called()

    def test_no_db_writes_no_new_state_no_real_network(self):
        deps = _make_deps(inventory_items=[_milk_row()])

        hrc.try_handle_household_read(deps, 1, 555, "Тест", "Що є вдома?")

        for name in ("get_household_and_user", "get_inventory_items", "get_active_shopping_items",
                     "get_household_alias_map"):
            mock = getattr(deps, name)
            for call in mock.call_args_list:
                self.assertNotIn("INSERT", str(call).upper())
                self.assertNotIn("UPDATE", str(call).upper())
                self.assertNotIn("DELETE", str(call).upper())


if __name__ == "__main__":
    unittest.main()
