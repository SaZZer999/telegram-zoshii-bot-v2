"""Inventory Cleanup / Merge v1.

Covers: the pure text-classification/grouping helpers in inventory.py
(parse_inventory_cleanup_request, find_inventory_cleanup_candidates,
group_inventory_cleanup_candidates), and the webhook-level route in bot.py
(_route_inventory_cleanup / _start_inventory_cleanup) that reuses the
EXISTING pending_merge dict, MERGE_PREVIEW_KEYBOARD, and
database.execute_merge_inventory's own StaleSnapshotError-protected
transaction (same "✅ Об'єднати"/"❌ Скасувати" wiring already exercised by
tests/test_merge_stale_snapshot_protection.py for "inventory_saved" — this
file only adds the new "inventory_cleanup" list_type and the new global
route in front of it). No real Gemini, Telegram, Render, or Supabase call
happens anywhere in this file.
"""
import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import inventory

# Load the REAL database.py fresh, under its own module name — see
# tests/test_merge_stale_snapshot_protection.py's identical docstring for
# why (another test file in the same `unittest discover` process may already
# have replaced sys.modules['database'] with a MagicMock by import time).
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_inventory_cleanup_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
from bot import (  # noqa: E402
    pending_merge,
    STALE_PREVIEW_MSG,
    INVENTORY_KEYBOARD,
    MERGE_PREVIEW_KEYBOARD,
)


# =========================
# Pure helpers (inventory.py) — no DB, no Telegram.
# =========================
def _row(item_id, name, canonical_name, value, unit, category="Молочне та яйця"):
    return {
        "id": item_id, "name": name, "canonical_name": canonical_name, "category": category,
        "quantity_value": value, "quantity_unit": unit,
        "quantity_text": f"{value} {unit}".rstrip() if value is not None else "",
    }


class TestParseInventoryCleanupRequest(unittest.TestCase):
    def test_direct_request_with_location_suffix(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("об'єднай молоко в запасах"), (False, "молоко"))

    def test_direct_request_case_and_location_variant(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("об'єднай Молоко у Запасах"), (False, "молоко"))

    def test_direct_request_without_location_suffix(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("об'єднай сосиски"), (False, "сосиски"))

    def test_prybery_duplicates_of_a_product(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("прибери дублікати молока"), (False, "молока"))

    def test_followup_tsi_zapysy(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("об'єднай ці записи"), (True, None))

    def test_followup_yikh_ukrainian(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("об'єднай їх"), (True, None))

    def test_followup_ykh_common_typo(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("об'єднай их"), (True, None))

    def test_followup_prybery_tsi_duplikaty(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("прибери ці дублікати"), (True, None))

    def test_not_a_cleanup_phrase_returns_none(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("Купив молоко"), (None, None))

    def test_rename_phrase_out_of_v1_scope_returns_none(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("перейменуй ser на сир"), (None, None))

    def test_bare_prybery_does_not_trigger_cleanup(self):
        """"прибери молоко" is the existing consume/remove verb (➖
        Використати / прибрати) — must never be hijacked into a duplicate
        search just because it starts with "прибери"."""
        self.assertEqual(inventory.parse_inventory_cleanup_request("прибери молоко"), (None, None))

    def test_blank_text_returns_none(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request(""), (None, None))
        self.assertEqual(inventory.parse_inventory_cleanup_request(None), (None, None))


class TestFindInventoryCleanupCandidates(unittest.TestCase):
    def test_filters_by_canonical_name_regardless_of_category(self):
        rows = [
            _row(1, "mleko", "молоко", Decimal("1"), "шт."),
            _row(2, "Молоко", "молоко", Decimal("500"), "мл", category="Інше їстівне"),
            _row(3, "Сир", "сир", Decimal("1"), "шт."),
        ]
        result = inventory.find_inventory_cleanup_candidates(rows, "молоко")
        self.assertEqual([r["id"] for r in result], [1, 2])

    def test_sorted_by_id_ascending(self):
        rows = [
            _row(5, "Молоко", "молоко", Decimal("1"), "л"),
            _row(2, "Молоко", "молоко", Decimal("1"), "л"),
        ]
        result = inventory.find_inventory_cleanup_candidates(rows, "молоко")
        self.assertEqual([r["id"] for r in result], [2, 5])

    def test_no_match_returns_empty(self):
        self.assertEqual(inventory.find_inventory_cleanup_candidates([], "молоко"), [])


class TestGroupInventoryCleanupCandidates(unittest.TestCase):
    # Case: 9 л + 500 мл -> 9,5 л (safe cross-unit volume merge), mlekо 1
    # шт. stays separate — the exact example from the live bug report.
    def test_volume_group_merges_and_pieces_row_is_incompatible(self):
        rows = [
            _row(1, "mleko", "молоко", Decimal("1"), "шт."),
            _row(2, "Молоко", "молоко", Decimal("500"), "мл"),
            _row(3, "Молоко", "молоко", Decimal("9"), "л"),
        ]
        result = inventory.group_inventory_cleanup_candidates(rows)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual([r["id"] for r in group["rows"]], [3, 2])
        self.assertEqual(group["merged_value"], Decimal("9.5"))
        self.assertEqual(group["merged_unit"], "л")
        self.assertEqual([r["id"] for r in result["incompatible"]], [1])

    # 1 кг + 200 г -> 1,2 кг
    def test_mass_group_merges(self):
        rows = [
            _row(10, "Цукор", "цукор", Decimal("1"), "кг"),
            _row(11, "Цукор", "цукор", Decimal("200"), "г"),
        ]
        result = inventory.group_inventory_cleanup_candidates(rows)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["merged_value"], Decimal("1.2"))
        self.assertEqual(group["merged_unit"], "кг")
        self.assertEqual(result["incompatible"], [])

    # 2 шт. + 3 шт. = 5 шт.
    def test_count_group_merges(self):
        rows = [
            _row(20, "Йогурт", "йогурт", Decimal("2"), "шт."),
            _row(21, "Йогурт", "йогурт", Decimal("3"), "шт."),
        ]
        result = inventory.group_inventory_cleanup_candidates(rows)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["merged_value"], Decimal("5"))
        self.assertEqual(group["merged_unit"], "шт.")

    # 1 шт. + 500 мл: never auto-merged; both rows end up "incompatible"
    # (each is alone in its own unit family).
    def test_incompatible_units_are_never_merged(self):
        rows = [
            _row(30, "Молоко", "молоко", Decimal("1"), "шт."),
            _row(31, "Молоко", "молоко", Decimal("500"), "мл"),
        ]
        result = inventory.group_inventory_cleanup_candidates(rows)
        self.assertEqual(result["groups"], [])
        self.assertEqual({r["id"] for r in result["incompatible"]}, {30, 31})

    # Unparseable/text quantity (e.g. "дві пачки") never joins a merge group.
    def test_unparseable_quantity_is_incompatible(self):
        rows = [
            _row(40, "Сосиски", "сосиски", Decimal("2"), "шт."),
            {"id": 41, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": None, "quantity_unit": None, "quantity_text": "дві пачки"},
        ]
        result = inventory.group_inventory_cleanup_candidates(rows)
        self.assertEqual(result["groups"], [])
        self.assertEqual({r["id"] for r in result["incompatible"]}, {40, 41})

    def test_empty_input(self):
        result = inventory.group_inventory_cleanup_candidates([])
        self.assertEqual(result, {"groups": [], "incompatible": []})


# =========================
# Webhook-level routing (bot.py) — network/DB calls patched.
# =========================
def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _milk_rows():
    return [
        {"id": 1, "name": "mleko", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
        {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("500"), "quantity_unit": "мл", "quantity_text": "500 мл"},
        {"id": 3, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("9"), "quantity_unit": "л", "quantity_text": "9 л"},
    ]


class InventoryCleanupWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_merge.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_alias = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias.start()
        self.addCleanup(patcher_alias.stop)

    def tearDown(self):
        pending_merge.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestDirectCleanupRequest(InventoryCleanupWebhookTestCase):
    # 1. Direct cleanup request finds duplicate candidates and builds a safe
    # merge preview + keeps the incompatible row listed, not auto-merged.
    def test_direct_request_builds_safe_merge_preview(self):
        chat_id = 770001
        with patch.object(bot, "get_inventory_items", return_value=_milk_rows()):
            _call_webhook(_make_update(770000001, chat_id, "об'єднай молоко в запасах"))

        self.assertIn(chat_id, pending_merge)
        entry = pending_merge[chat_id]
        self.assertEqual(entry["list_type"], "inventory_cleanup")
        self.assertEqual(len(entry["groups"]), 1)
        group = entry["groups"][0]
        self.assertEqual(sorted(group["item_ids"]), [2, 3])
        self.assertEqual(group["merged_quantity_value"], Decimal("9.5"))
        self.assertEqual(group["merged_quantity_unit"], "л")

        texts = self._sent_texts()
        self.assertTrue(any("9,5 л" in t for t in texts))
        self.assertTrue(any("mleko" in t or "1 шт." in t for t in texts))
        reply_markups = [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]
        self.assertIn(MERGE_PREVIEW_KEYBOARD, reply_markups)

    def test_no_matching_rows_sends_not_found_message_without_pending_state(self):
        chat_id = 770002
        with patch.object(bot, "get_inventory_items", return_value=[]):
            _call_webhook(_make_update(770000002, chat_id, "об'єднай сир в запасах"))
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("Не знайшов" in t for t in self._sent_texts()))

    def test_only_one_row_matches_sends_no_duplicates_message(self):
        chat_id = 770003
        with patch.object(bot, "get_inventory_items", return_value=[_milk_rows()[2]]):
            _call_webhook(_make_update(770000003, chat_id, "об'єднай молоко в запасах"))
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("дублікатів немає" in t for t in self._sent_texts()))

    def test_all_candidates_incompatible_sends_listing_without_pending_state(self):
        chat_id = 770004
        rows = [_milk_rows()[0], _milk_rows()[1]]  # 1 шт. + 500 мл, never merge
        with patch.object(bot, "get_inventory_items", return_value=rows):
            _call_webhook(_make_update(770000004, chat_id, "об'єднай молоко в запасах"))
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("несумісні одиниці" in t for t in self._sent_texts()))


class TestFollowupUsesPreviousContext(InventoryCleanupWebhookTestCase):
    # 4. Follow-up "об'єднай их" uses the previous duplicate-search context.
    def test_followup_confirms_existing_cleanup_preview(self):
        chat_id = 770010
        pending_merge[chat_id] = {
            "groups": [{
                "item_ids": [2, 3], "merged_name": "Молоко", "merged_quantity_text": "9,5 л",
                "merged_category": "Молочне та яйця", "canonical_name": "молоко",
                "merged_quantity_value": Decimal("9.5"), "merged_quantity_unit": "л",
                "items": [
                    {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                     "quantity_value": Decimal("500"), "quantity_unit": "мл"},
                    {"id": 3, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                     "quantity_value": Decimal("9"), "quantity_unit": "л"},
                ],
            }],
            "targets": [
                {"item_id": 2, "quantity_value": Decimal("500"), "quantity_unit": "мл",
                 "canonical_name": "молоко", "category": "Молочне та яйця"},
                {"item_id": 3, "quantity_value": Decimal("9"), "quantity_unit": "л",
                 "canonical_name": "молоко", "category": "Молочне та яйця"},
            ],
            "household_id": 1, "user_db_id": 10, "list_type": "inventory_cleanup",
        }
        with patch.object(bot, "execute_merge_inventory", return_value=1) as mock_merge:
            _call_webhook(_make_update(770000010, chat_id, "об'єднай их"))
        mock_merge.assert_called_once()
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("✅ Об'єднано груп: 1" == t for t in self._sent_texts()))

    # 5. Follow-up "об'єднай их" without any prior context does not guess —
    # it asks the user to name the product, and never touches the DB.
    def test_followup_without_context_asks_for_clarification(self):
        chat_id = 770011
        with patch.object(bot, "execute_merge_inventory") as mock_merge, \
                patch.object(bot, "get_inventory_items") as mock_items:
            _call_webhook(_make_update(770000011, chat_id, "об'єднай їх"))
        mock_merge.assert_not_called()
        mock_items.assert_not_called()
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("Напиши, який товар" in t for t in self._sent_texts()))


class TestConfirmAndCancel(InventoryCleanupWebhookTestCase):
    """Confirm/cancel go through the EXISTING "✅ Об'єднати"/"❌ Скасувати"
    button wiring in _try_handle_confirm_or_cancel — same code path already
    covered for list_type "inventory_saved" by
    tests/test_merge_stale_snapshot_protection.py, now extended to also
    accept "inventory_cleanup"."""

    @classmethod
    def setUpClass(cls):
        # Same monkeypatch as test_merge_stale_snapshot_protection.py:
        # bot.StaleSnapshotError is bound to a bare MagicMock attribute
        # (sys.modules['database'] was mocked at bot import time), not a
        # real exception class — rebind it to the real one for this class.
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def _pending_entry(self):
        return {
            "groups": [{
                "item_ids": [2, 3], "merged_name": "Молоко", "merged_quantity_text": "9,5 л",
                "merged_category": "Молочне та яйця", "canonical_name": "молоко",
                "merged_quantity_value": Decimal("9.5"), "merged_quantity_unit": "л",
                "items": [
                    {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                     "quantity_value": Decimal("500"), "quantity_unit": "мл"},
                    {"id": 3, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
                     "quantity_value": Decimal("9"), "quantity_unit": "л"},
                ],
            }],
            "targets": [
                {"item_id": 2, "quantity_value": Decimal("500"), "quantity_unit": "мл",
                 "canonical_name": "молоко", "category": "Молочне та яйця"},
                {"item_id": 3, "quantity_value": Decimal("9"), "quantity_unit": "л",
                 "canonical_name": "молоко", "category": "Молочне та яйця"},
            ],
            "household_id": 1, "user_db_id": 10, "list_type": "inventory_cleanup",
        }

    # 6a. Confirm button applies only the previewed groups.
    def test_confirm_button_applies_merge(self):
        chat_id = 770020
        pending_merge[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_merge_inventory", return_value=1) as mock_merge:
            _call_webhook(_make_update(770000020, chat_id, "✅ Об'єднати"))
        mock_merge.assert_called_once()
        called_household_id, called_groups, called_targets = mock_merge.call_args[0]
        self.assertEqual(called_household_id, 1)
        self.assertEqual(len(called_groups), 1)
        self.assertEqual(len(called_targets), 2)
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("✅ Об'єднано груп: 1" == t for t in self._sent_texts()))

    # 6b. Confirm uses stale protection — a changed row aborts with no write.
    def test_confirm_aborts_on_stale_snapshot(self):
        chat_id = 770021
        pending_merge[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_merge_inventory", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(770000021, chat_id, "✅ Об'єднати"))
        self.assertIn(STALE_PREVIEW_MSG, self._sent_texts())
        reply_markups = [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]
        self.assertIn(INVENTORY_KEYBOARD, reply_markups)
        self.assertNotIn(chat_id, pending_merge)

    # 7. Cancel clears the cleanup pending context.
    def test_cancel_clears_pending_cleanup_context(self):
        chat_id = 770022
        pending_merge[chat_id] = self._pending_entry()
        _call_webhook(_make_update(770000022, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("Об'єднання скасовано." in t for t in self._sent_texts()))


class TestExistingRoutesStillWork(InventoryCleanupWebhookTestCase):
    """8. Spot-check a few unrelated routes this feature must never affect."""

    def test_household_read_question_still_works(self):
        chat_id = 770030
        with patch.object(bot, "get_active_shopping_items", return_value=[]):
            _call_webhook(_make_update(770000030, chat_id, "Що треба купити?"))
        texts = self._sent_texts()
        self.assertTrue(texts)
        self.assertNotIn(chat_id, pending_merge)

    def test_meal_ideas_button_still_works(self):
        chat_id = 770031
        with patch.object(bot.meal_ideas, "try_handle_meal_ideas", return_value=True) as mock_meal:
            _call_webhook(_make_update(770000031, chat_id, "🍽 Що приготувати"))
        mock_meal.assert_called_once()

    def test_undo_button_during_quantity_clarification_still_cancels_it(self):
        chat_id = 770032
        bot.pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "new_expense": None, "delete_expense": None,
        }
        try:
            _call_webhook(_make_update(770000032, chat_id, "↩️ Скасувати останню дію"))
            self.assertNotIn(chat_id, bot.pending_inventory_quantity_clarification)
            self.assertTrue(any("Поточну дію скасовано." in t for t in self._sent_texts()))
        finally:
            bot.pending_inventory_quantity_clarification.pop(chat_id, None)

    def test_emoji_variation_selector_cooking_button_still_works(self):
        chat_id = 770033
        with patch.object(bot, "waiting_for_ingredients", {}):
            _call_webhook(_make_update(770000033, chat_id, "🍽️ Що приготувати"))
        self.assertNotIn(chat_id, pending_merge)


if __name__ == "__main__":
    unittest.main()
