"""Product Name Modifier Preservation v1.

Fixes, as executable tests, the live bug reported for "Додай до покупок 1
шт. тестового чаю": the bot must never collapse a product name down to a
bare head noun ("Чай") when the original phrase carried a meaningful
adjective/modifier ("Тестовий чай"). Only quantity/package words (шт., л,
мл, г, кг, пачка, упаковка, пара/пару, word-numbers) may ever be stripped
from `name` — a leading descriptive adjective (colour, flavour, type: "
зелений", "кокосовий", "грецький", "червоний", "мисливські", "тестовий"
etc.) is part of the product name and must always survive.

Gemini is always mocked here (no real API call) — these tests fix Python's
side of the contract: once Gemini returns the correctly-separated
name/quantity_text pair (as the updated SHOPPING_PARSE_PROMPT/INVENTORY_
PARSE_PROMPT/HOUSEHOLD_ROUTER_PROMPT/EXPLICIT_ADD_ITEM_PROMPT instructions
in bot.py/household_router.py now ask for), nothing downstream in the
Python pipeline (resolve_item_name/canonicalize_name/normalize_item_
quantity/_validate_new_item_op/parse_shopping_list_with_gemini) may ever
shorten, lowercase-collapse, or otherwise mangle that name.
"""
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import household_router  # noqa: E402
import legacy_shopping_flow  # noqa: E402
from bot import pending_global_household  # noqa: E402


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


# =========================
# Global Explicit Add ("Додай до покупок ...", "Додай до запасів ...")
# =========================
class TestGlobalExplicitAddPreservesModifiers(unittest.TestCase):
    def setUp(self):
        pending_global_household.clear()

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_items = patch.object(household_router, "_ask_gemini_explicit_add_items")
        self.mock_items = patcher_items.start()
        self.addCleanup(patcher_items.stop)

        patcher_inv = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory = patcher_inv.start()
        self.addCleanup(patcher_inv.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

    def tearDown(self):
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 1 — the exact reported bug: "Додай до покупок 1 шт. тестового чаю"
    def test_shopping_add_keeps_test_adjective(self):
        chat_id = 800001
        self.mock_items.return_value = {
            "items": [{"name": "Тестовий чай", "quantity_text": "1 шт.", "category": "Напої"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(869000001, chat_id, "Додай до покупок 1 шт. тестового чаю"))
        payload = pending_global_household[chat_id]
        item = payload["add_shopping_items"][0]
        self.assertEqual(item["name"], "Тестовий чай")
        self.assertNotEqual(item["name"], "Чай")
        self.assertEqual(item["quantity_value"], Decimal("1"))
        self.assertEqual(item["quantity_unit"], "шт.")
        texts = self._sent_texts()
        self.assertTrue(any("Тестовий чай — 1 шт." in t for t in texts))

    # Case 3 — "Додай до запасів 2 л кокосового молока"
    def test_inventory_add_keeps_coconut_modifier(self):
        chat_id = 800003
        self.mock_items.return_value = {
            "items": [{"name": "Кокосове молоко", "quantity_text": "2 л", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(869000003, chat_id, "Додай до запасів 2 л кокосового молока"))
        payload = pending_global_household[chat_id]
        item = payload["add_inventory_items"][0]
        self.assertEqual(item["name"], "Кокосове молоко")
        self.assertNotEqual(item["name"], "Молоко")
        self.assertEqual(item["quantity_value"], Decimal("2"))
        self.assertEqual(item["quantity_unit"], "л")
        self.assertEqual(item["category"], "Молочне та яйця")

    # Case 4 — "Додай до покупок 500 г червоної квасолі"
    def test_shopping_add_keeps_red_bean_modifier(self):
        chat_id = 800004
        self.mock_items.return_value = {
            "items": [{"name": "Червона квасоля", "quantity_text": "500 г", "category": "Крупи, макарони та борошно"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(869000004, chat_id, "Додай до покупок 500 г червоної квасолі"))
        payload = pending_global_household[chat_id]
        item = payload["add_shopping_items"][0]
        self.assertEqual(item["name"], "Червона квасоля")
        self.assertNotEqual(item["name"], "Квасоля")
        self.assertEqual(item["quantity_value"], Decimal("500"))
        self.assertEqual(item["quantity_unit"], "г")

    # Case 5 — "Додай до запасів 1 пачка грецького йогурту": the package word
    # ("пачка") goes to quantity_text, the adjective stays in name.
    def test_inventory_add_keeps_greek_yogurt_modifier_and_separates_package_word(self):
        chat_id = 800005
        self.mock_items.return_value = {
            "items": [{"name": "Грецький йогурт", "quantity_text": "1 пачка", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(869000005, chat_id, "Додай до запасів 1 пачка грецького йогурту"))
        payload = pending_global_household[chat_id]
        item = payload["add_inventory_items"][0]
        self.assertEqual(item["name"], "Грецький йогурт")
        self.assertNotEqual(item["name"], "Йогурт")
        self.assertNotIn("пачка", item["name"].lower())

    # Case 6 — "Додай до запасів дві пачки мисливських ковбасок": word-number
    # package phrase stays in quantity_text, plural adjective stays in name.
    def test_inventory_add_keeps_hunter_sausage_modifier_and_separates_word_number_package(self):
        chat_id = 800006
        self.mock_items.return_value = {
            "items": [{"name": "Мисливські ковбаски", "quantity_text": "дві пачки", "category": "М'ясо та риба"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(869000006, chat_id, "Додай до запасів дві пачки мисливських ковбасок"))
        payload = pending_global_household[chat_id]
        item = payload["add_inventory_items"][0]
        self.assertEqual(item["name"], "Мисливські ковбаски")
        self.assertNotEqual(item["name"], "Ковбаски")
        self.assertEqual(item["quantity_text"], "дві пачки")


# =========================
# Legacy shopping add via the dedicated "🛒 Покупки" -> "➕ Додати товар" menu
# =========================
class TestLegacyShoppingMenuAddPreservesModifiers(unittest.TestCase):
    def setUp(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_shopping_flow.pending_batch.clear()

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_gemini = patch.object(bot, "call_gemini")
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

    def tearDown(self):
        legacy_shopping_flow.shopping_mode.clear()
        legacy_shopping_flow.pending_batch.clear()

    # Case 2 — "1 шт. зеленого чаю" typed after "➕ Додати товар"
    def test_green_tea_keeps_adjective_through_legacy_menu_flow(self):
        chat_id = 800002
        legacy_shopping_flow.shopping_mode[chat_id] = "adding"
        self.mock_gemini.return_value = json.dumps({
            "items": [{
                "name": "Зелений чай", "quantity_text": "1 шт.", "category": "Напої",
                "was_corrected": False, "is_consumable": True,
            }],
            "ignored_items": [],
        })
        _call_webhook(_make_update(869000002, chat_id, "1 шт. зеленого чаю"))
        batch = legacy_shopping_flow.pending_batch[chat_id]
        item = batch["items"][0]
        self.assertEqual(item["name"], "Зелений чай")
        self.assertNotEqual(item["name"], "Чай")
        self.assertEqual(item["quantity_value"], Decimal("1"))
        self.assertEqual(item["quantity_unit"], "шт.")


# =========================
# Regression checks — existing behavior around the same code paths must be
# unaffected by the prompt/example changes.
# =========================
class TestRegressionsAroundModifierFix(unittest.TestCase):
    # Case 7 — built-in Polish/Ukrainian synonyms still translate.
    def test_synonyms_still_translate(self):
        self.assertEqual(bot.canonicalize_name("ser"), "сир")
        self.assertEqual(bot.canonicalize_name("mleko"), "молоко")

    # Case 8 — a household alias still wins over the built-in synonym.
    def test_alias_still_wins_over_builtin_synonym(self):
        alias_map = {"ser": {"target_display_name": "Сир пармезан", "target_canonical_name": "сир пармезан"}}
        display, canonical = bot.resolve_item_name("ser", alias_map)
        self.assertEqual((display, canonical), ("Сир пармезан", "сир пармезан"))

    # Case 9 — plain quantity extraction (no modifier involved) is unchanged.
    def test_plain_quantities_still_parse(self):
        for text, value, unit in (("1 шт.", Decimal("1"), "шт."), ("2 л", Decimal("2"), "л"), ("500 г", Decimal("500"), "г")):
            with self.subTest(text=text):
                normalized = bot.normalize_item_quantity("Молоко", text, allow_default_unit=True)
                self.assertEqual(normalized["quantity_value"], value)
                self.assertEqual(normalized["quantity_unit"], unit)

    # Case 10 — category assignment is untouched by the modifier fix: a
    # category the router already validated survives _validate_new_item_op,
    # an invalid one still falls back to DEFAULT_CATEGORY.
    def test_category_assignment_unaffected(self):
        op = {"name": "Зелений чай", "quantity_text": "1 шт.", "category": "Напої"}
        item = household_router._validate_new_item_op(op, {})
        self.assertEqual(item["category"], "Напої")

        bad_op = {"name": "Зелений чай", "quantity_text": "1 шт.", "category": "Не існує"}
        bad_item = household_router._validate_new_item_op(bad_op, {})
        self.assertEqual(bad_item["category"], bot.DEFAULT_CATEGORY)


if __name__ == "__main__":
    unittest.main()
