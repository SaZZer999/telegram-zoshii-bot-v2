"""Explicit Inventory Multi-Add Stabilization V1 — confirmed live bug.

Live text: "Додай до запасів тестове автокрісло batch 2 шт, тестове печиво
batch 1 кг і тестовий хліб batch 2 шт" produced:

    Я зрозумів частину повідомлення, але не хочу мовчки пропустити решту.

    Не зміг зрозуміти:
    • «тестове автокрісло batch 2 шт»

    Спробуй уточнити все повідомлення.

No preview at all — the other two food items were silently swallowed by the
same all-or-nothing unresolved_fragments guard.

Root cause (confirmed by reading code, not guessed): this message goes
through household_router.detect_explicit_add_destination (a plain "Додай до
запасів ..." destination phrase) -> build_explicit_add_preview ->
_ask_gemini_explicit_add_items(EXPLICIT_ADD_ITEM_PROMPT). This is NOT
inventory_multi_target_route (a4214c4) — that route only fires on delete/
consume verbs (видали/прибери/забери/спиши), none of which are present here;
see TestInventoryMultiTargetDoesNotClaimAddCommands below. household_router.py
was not touched by a4214c4 at all, so this bug predates it.

The OLD EXPLICIT_ADD_ITEM_PROMPT gave Gemini a closed list of exclusively
FOOD categories and told it to put anything it "can't safely turn into a
product" into unresolved_fragments — with no exception for category
uncertainty. Since nothing in that list fits a car seat, Gemini classified
"тестове автокрісло batch 2 шт" as unresolvable, even though the Python-side
validation (_validate_new_item_op) already accepts ANY category string and
silently falls back to DEFAULT_CATEGORY ("Інше їстівне") when Gemini's
category is missing/invalid — the block was purely a prompt-level decision,
never a Python-code defect.

Fix: EXPLICIT_ADD_ITEM_PROMPT (household_router.py) now explicitly states
that запаси/purchases include general household items (not just food, with
the exact non-food examples from the task: автокрісло, пральний порошок,
лампочка, підгузки), that category is just an approximate grouping (pick the
closest one, or the existing "Інше їстівне" fallback if truly nothing fits),
and that category uncertainty is NEVER a reason for unresolved_fragments —
only genuinely unparsable name/quantity text is. No DB schema change, no new
category value, no change to VALID_CATEGORIES/DEFAULT_CATEGORY, no change to
_validate_new_item_op/_validate_explicit_add_items/build_add_preview_from_items
— those already had the right all-or-nothing/category-fallback contract; only
the prompt that decides what reaches them changed.

Since _ask_gemini_explicit_add_items is always mocked in tests (never a real
Gemini call), this file proves two separate things instead of "the prompt
change made Gemini smarter" (untestable without a live call):
  1. TestLiveBugReproduction — given the exact bad classification Gemini
     produced live (2 food items resolved + one unresolved fragment for the
     car seat), the code's existing all-or-nothing guard reproduces the
     exact live bot response, unchanged and still correct as a safety net.
  2. TestFixedGeminiOutputBuildsFullPreview / TestCategoryFallbackNeverBlocks
     — once Gemini (after the prompt fix) resolves all three items instead
     (any category, including one outside VALID_CATEGORIES for the non-food
     one), the resulting preview covers all three, DB write only happens
     after confirm, and cancel/confirm/no-partial-write all hold.
"""
import sys
import os
import itertools
import unittest
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import household_router  # noqa: E402
import inventory_multi_target  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    pending_inventory_quantity_clarification,
    active_list_context,
    saved_list_context,
)

_uid = itertools.count(881_000_000)

LIVE_BUG_TEXT = (
    "Додай до запасів тестове автокрісло batch 2 шт, тестове печиво batch 1 кг "
    "і тестовий хліб batch 2 шт"
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _live_bug_gemini_response_broken():
    """Exactly reproduces what live Gemini returned for the confirmed bug:
    the two food items resolved fine, the car seat pushed into
    unresolved_fragments."""
    return {
        "items": [
            {"name": "Тестове печиво batch", "quantity_text": "1 кг", "category": "Хліб і випічка"},
            {"name": "Тестовий хліб batch", "quantity_text": "2 шт.", "category": "Хліб і випічка"},
        ],
        "unresolved_fragments": ["тестове автокрісло batch 2 шт"],
    }


def _live_bug_gemini_response_fixed():
    """What Gemini should return once it stops treating "non-food" as
    unresolvable: all three items, the car seat with a category outside the
    fixed food list (simulating Gemini picking something that doesn't fit —
    Python must still accept it via the existing DEFAULT_CATEGORY fallback)."""
    return {
        "items": [
            {"name": "Тестове автокрісло batch", "quantity_text": "2 шт.", "category": "Автотовари"},
            {"name": "Тестове печиво batch", "quantity_text": "1 кг", "category": "Хліб і випічка"},
            {"name": "Тестовий хліб batch", "quantity_text": "2 шт.", "category": "Хліб і випічка"},
        ],
        "unresolved_fragments": [],
    }


class _BaseTestCase(unittest.TestCase):
    def setUp(self):
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

        patcher_hr = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_hr = patcher_hr.start()
        self.addCleanup(patcher_hr.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

        patcher_inv = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory = patcher_inv.start()
        self.addCleanup(patcher_inv.stop)

    def tearDown(self):
        for d in (pending_global_household, pending_inventory_quantity_clarification, active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 1. Exact live-bug reproduction, unchanged as a safety guarantee.
# =========================
class TestLiveBugReproduction(_BaseTestCase):
    def test_exact_live_text_with_broken_gemini_output_blocks_whole_preview(self):
        chat_id = 998001
        self.mock_items.return_value = _live_bug_gemini_response_broken()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(
            "Я зрозумів частину повідомлення, але не хочу мовчки пропустити решту." in t
            and "тестове автокрісло batch 2 шт" in t
            and "Спробуй уточнити все повідомлення." in t
            for t in texts
        ))
        # The two food fragments must never leak into the blocking message —
        # all-or-nothing means nothing partial is even mentioned as pending.
        self.assertFalse(any("Тестове печиво batch" in t for t in texts))
        self.assertFalse(any("Тестовий хліб batch" in t for t in texts))


# =========================
# 2. Fixed Gemini output builds one full three-item preview.
# =========================
class TestFixedGeminiOutputBuildsFullPreview(_BaseTestCase):
    def test_all_three_items_in_one_preview(self):
        chat_id = 998010
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        names = [it["name"] for it in payload["add_inventory_items"]]
        self.assertEqual(len(payload["add_inventory_items"]), 3)
        self.assertIn("Тестове автокрісло batch", names)
        self.assertIn("Тестове печиво batch", names)
        self.assertIn("Тестовий хліб batch", names)
        texts = self._sent_texts()
        joined = "\n".join(texts)
        self.assertIn("Тестове автокрісло batch", joined)
        self.assertIn("2 шт", joined)
        self.assertIn("Тестове печиво batch", joined)
        self.assertIn("1 кг", joined)
        self.assertIn("Тестовий хліб batch", joined)

    # 3. Nothing is written to the DB before confirm.
    def test_no_db_write_before_confirm(self):
        chat_id = 998011
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        self.mock_apply.assert_not_called()

    # 4. Cancel adds nothing.
    def test_cancel_adds_nothing(self):
        chat_id = 998012
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        _call_webhook(_make_update(next(_uid), chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()

    # 5. Confirm adds exactly three items, in one call.
    def test_confirm_adds_exactly_three_items(self):
        chat_id = 998013
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        self.mock_apply.assert_called_once()
        _, kwargs = self.mock_apply.call_args
        self.assertEqual(len(kwargs["add_inventory_items"]), 3)
        self.assertEqual(kwargs["add_shopping_items"], [])
        self.assertNotIn(chat_id, pending_global_household)

    # 6. Exactly one compound pending state, not three separate ones.
    def test_single_compound_pending_state(self):
        chat_id = 998014
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        self.assertEqual(len(pending_global_household), 1)
        self.assertIn(chat_id, pending_global_household)

    # 7. "batch" is preserved identically across all three names, never
    # stripped selectively for just one item.
    def test_batch_word_preserved_consistently(self):
        chat_id = 998015
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        payload = pending_global_household[chat_id]
        for item in payload["add_inventory_items"]:
            self.assertTrue(item["name"].endswith("batch"), item["name"])

    # 15. At most one Gemini call for the whole command.
    def test_at_most_one_gemini_call(self):
        chat_id = 998016
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        self.mock_items.assert_called_once()
        self.mock_call_gemini.assert_not_called()
        self.mock_hr.assert_not_called()


# =========================
# 8. Non-food item never rejected merely for its category — proven directly
# against household_router's own validation, independent of any particular
# Gemini mock, and independent of the prompt-string change above.
# =========================
class TestCategoryFallbackNeverBlocks(unittest.TestCase):
    def test_unknown_category_falls_back_to_default_and_is_not_dropped(self):
        item = household_router._validate_new_item_op(
            {"name": "Тестове автокрісло", "quantity_text": "2 шт.", "category": "Автотовари"},
            alias_map={},
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["name"], "Тестове автокрісло")
        self.assertEqual(item["category"], bot.DEFAULT_CATEGORY)

    def test_missing_category_falls_back_to_default_and_is_not_dropped(self):
        item = household_router._validate_new_item_op(
            {"name": "Пральний порошок", "quantity_text": "1 кг", "category": ""},
            alias_map={},
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["category"], bot.DEFAULT_CATEGORY)

    def test_build_explicit_add_preview_keeps_non_food_item_with_fallback_category(self):
        with patch.object(household_router, "_ask_gemini_explicit_add_items") as mock_items:
            mock_items.return_value = {
                "items": [{"name": "Лампочка", "quantity_text": "2 шт.", "category": "Не існуюча категорія"}],
                "unresolved_fragments": [],
            }
            kind, payload = household_router.build_explicit_add_preview("add_inventory", "лампочку 2 шт", [])
        self.assertEqual(kind, "ok")
        self.assertEqual(len(payload["add_inventory_items"]), 1)
        item = payload["add_inventory_items"][0]
        self.assertEqual(item["name"], "Лампочка")
        self.assertEqual(item["category"], bot.DEFAULT_CATEGORY)


# =========================
# 9. Different non-food example set from the task spec, one preview.
# =========================
class TestOtherNonFoodExamples(_BaseTestCase):
    def test_lightbulb_milk_powder_one_preview(self):
        chat_id = 998020
        self.mock_items.return_value = {
            "items": [
                {"name": "Лампочка", "quantity_text": "2 шт.", "category": "Побутове"},
                {"name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
                {"name": "Порошок", "quantity_text": "1 кг", "category": "Побутове"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(
            next(_uid), chat_id, "Додай до запасів лампочку 2 шт, молоко 1 л і порошок 1 кг",
        ))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        names = [it["name"] for it in payload["add_inventory_items"]]
        self.assertEqual(len(payload["add_inventory_items"]), 3)
        self.assertIn("Лампочка", names)
        self.assertIn("Молоко", names)
        self.assertIn("Порошок", names)
        # Non-food items still get SOME valid category (fallback), never
        # dropped and never crashing the preview formatter.
        for item in payload["add_inventory_items"]:
            self.assertIn(item["category"], bot.VALID_CATEGORIES)


# =========================
# 10/11/12 — existing add flows must not regress.
# =========================
class TestExistingAddFlowsNotRegressed(_BaseTestCase):
    # 10. Pure food multi-add, unchanged.
    def test_food_only_multi_add_inventory_unchanged(self):
        chat_id = 998030
        self.mock_items.return_value = {
            "items": [
                {"name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
                {"name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до запасів молоко 1 л і хліб"))
        self.assertIn(chat_id, pending_global_household)
        self.assertEqual(len(pending_global_household[chat_id]["add_inventory_items"]), 2)

    # 11. Shopping multi-add, unchanged, unaffected by inventory-side fix.
    def test_shopping_multi_add_unchanged(self):
        chat_id = 998031
        self.mock_items.return_value = {
            "items": [
                {"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до покупок молоко і хліб"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 2)
        self.assertEqual(payload["add_inventory_items"], [])

    # 12. Single-target inventory add, unchanged.
    def test_single_item_inventory_add_unchanged(self):
        chat_id = 998032
        self.mock_items.return_value = {
            "items": [{"name": "Банани", "quantity_text": "2", "category": "Фрукти та ягоди"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай в запаси 2 банани"))
        self.assertIn(chat_id, pending_global_household)
        self.assertEqual(len(pending_global_household[chat_id]["add_inventory_items"]), 1)


# =========================
# 13/14 — a genuinely invalid item (leaked quantity phrase in name) still
# blocks the WHOLE batch, all-or-nothing preserved after the prompt change.
# =========================
class TestInvalidItemStillBlocksWholeBatch(_BaseTestCase):
    def test_leaked_quantity_phrase_blocks_entire_preview(self):
        chat_id = 998040
        self.mock_items.return_value = {
            "items": [
                {"name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
                {"name": "дві пачки печива", "quantity_text": "", "category": "Хліб і випічка"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до запасів молоко 1 л і дві пачки печива"))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Не зміг безпечно розпізнати товар." in t for t in texts))


# =========================
# 16. inventory_multi_target_route must never claim an add command — proves
# routing was not affected by a4214c4 and stays that way.
# =========================
class TestInventoryMultiTargetDoesNotClaimAddCommands(_BaseTestCase):
    def test_pre_gate_rejects_the_live_add_text(self):
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target(LIVE_BUG_TEXT))

    def test_try_inventory_multi_target_returns_false_for_add_command(self):
        result = bot._try_inventory_multi_target(998050, 555, "Тест", LIVE_BUG_TEXT)
        self.assertFalse(result)
        self.assertNotIn(998050, pending_global_household)

    def test_webhook_still_builds_add_preview_not_multi_target_delete(self):
        chat_id = 998051
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["consume_changes"], [])
        self.assertEqual(len(payload["add_inventory_items"]), 3)


# =========================
# Golden smoke-test set — small regression coverage for the core add flows,
# not a wide refactor.
# =========================
class TestGoldenAddFlowsSmoke(_BaseTestCase):
    def test_add_one_item_to_shopping(self):
        chat_id = 998060
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до покупок молоко"))
        self.assertEqual(len(pending_global_household[chat_id]["add_shopping_items"]), 1)

    def test_add_several_items_to_shopping(self):
        chat_id = 998061
        self.mock_items.return_value = {
            "items": [
                {"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до покупок молоко і хліб"))
        self.assertEqual(len(pending_global_household[chat_id]["add_shopping_items"]), 2)

    def test_add_one_item_to_inventory(self):
        chat_id = 998062
        self.mock_items.return_value = {
            "items": [{"name": "Сир", "quantity_text": "500 г", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до запасів сир 500 г"))
        self.assertEqual(len(pending_global_household[chat_id]["add_inventory_items"]), 1)

    def test_add_several_items_to_inventory(self):
        chat_id = 998063
        self.mock_items.return_value = {
            "items": [
                {"name": "Сир", "quantity_text": "500 г", "category": "Молочне та яйця"},
                {"name": "Масло", "quantity_text": "1 шт.", "category": "Молочне та яйця"},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до запасів сир 500 г і масло"))
        self.assertEqual(len(pending_global_household[chat_id]["add_inventory_items"]), 2)

    def test_add_food_and_non_food_to_inventory(self):
        chat_id = 998064
        self.mock_items.return_value = _live_bug_gemini_response_fixed()
        _call_webhook(_make_update(next(_uid), chat_id, LIVE_BUG_TEXT))
        self.assertEqual(len(pending_global_household[chat_id]["add_inventory_items"]), 3)

    def test_explicit_quantity_and_unit_preserved(self):
        chat_id = 998065
        self.mock_items.return_value = {
            "items": [{"name": "Олія", "quantity_text": "0,5 л", "category": "Інше їстівне"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до запасів олія 0,5 л"))
        item = pending_global_household[chat_id]["add_inventory_items"][0]
        self.assertEqual(item["quantity_unit"], "л")

    def test_cancel(self):
        chat_id = 998066
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до покупок молоко"))
        _call_webhook(_make_update(next(_uid), chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()

    def test_confirm(self):
        chat_id = 998067
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до покупок молоко"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        self.mock_apply.assert_called_once()

    def test_no_partial_write_on_unresolved_fragment(self):
        chat_id = 998068
        self.mock_items.return_value = {
            "items": [{"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": ["щось незрозуміле"],
        }
        _call_webhook(_make_update(next(_uid), chat_id, "Додай до покупок молоко і щось незрозуміле"))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
