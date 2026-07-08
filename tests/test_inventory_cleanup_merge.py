"""Inventory Cleanup / Merge v1.1.

Covers: the pure text-classification/grouping/alias/preview-formatting
helpers in inventory.py (parse_inventory_cleanup_request,
cleanup_canonical_name_candidates, find_inventory_cleanup_candidates,
group_inventory_cleanup_candidates, describe_cleanup_incompatibility_reason,
format_inventory_cleanup_preview), the webhook-level route in bot.py
(_route_inventory_cleanup / _start_inventory_cleanup / _apply_inventory_
cleanup_merge), and database.execute_inventory_cleanup_merge's Action
History journal integration (v1.1's undo fix — same journal table/
operation_type/restore path apply_global_household_operations already uses,
verified here against the REAL database.py). No real Gemini, Telegram,
Render, or Supabase call happens anywhere in this file.
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
    canonicalize_name,
    STALE_PREVIEW_MSG,
    INVENTORY_KEYBOARD,
    MERGE_PREVIEW_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
)


def _effective_quantity_stub(item):
    """Minimal stand-in for bot._effective_quantity in pure inventory.py
    tests — same (value, unit, display_text) contract, no bot.py import."""
    value = item.get("quantity_value")
    unit = item.get("quantity_unit")
    text = item.get("quantity_text") or ""
    return value, unit, text


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

    def test_followup_tsi(self):
        self.assertEqual(inventory.parse_inventory_cleanup_request("об'єднай ці"), (True, None))

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


class TestCleanupCanonicalNameCandidates(unittest.TestCase):
    """7. The cleanup-specific alias layer — required aliases from the task."""

    def test_mleko_latin_via_global_canonicalizer(self):
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "mleko")
        self.assertIn("молоко", result)

    def test_mleko_cyrillic_o_via_global_canonicalizer(self):
        # "mlekо" — trailing char is Cyrillic "о" (U+043E), not Latin "o".
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "mlekо")
        self.assertIn("молоко", result)

    def test_moloka_genitive_via_cleanup_alias(self):
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "молока")
        self.assertEqual(result[0], "молоко")

    def test_ser_via_global_canonicalizer(self):
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "ser")
        self.assertIn("сир", result)

    def test_syru_dative_via_cleanup_alias(self):
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "сиру")
        self.assertEqual(result[0], "сир")

    def test_sosysok_genitive_plural_via_cleanup_alias(self):
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "сосисок")
        self.assertEqual(result[0], "сосиски")

    def test_kurku_accusative_via_cleanup_alias(self):
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "курку")
        self.assertEqual(result[0], "курка")

    def test_no_duplicate_candidates_when_alias_and_canonical_agree(self):
        result = inventory.cleanup_canonical_name_candidates(canonicalize_name, "молоко")
        self.assertEqual(result, ["молоко"])


class TestFindInventoryCleanupCandidates(unittest.TestCase):
    def test_filters_by_canonical_name_regardless_of_category(self):
        rows = [
            _row(1, "Молоко", "молоко", Decimal("500"), "мл", category="Інше їстівне"),
            _row(2, "Сир", "сир", Decimal("1"), "шт."),
        ]
        result = inventory.find_inventory_cleanup_candidates(rows, ["молоко"], canonicalize_name)
        self.assertEqual([r["id"] for r in result], [1])

    def test_sorted_by_id_ascending(self):
        rows = [
            _row(5, "Молоко", "молоко", Decimal("1"), "л"),
            _row(2, "Молоко", "молоко", Decimal("1"), "л"),
        ]
        result = inventory.find_inventory_cleanup_candidates(rows, ["молоко"], canonicalize_name)
        self.assertEqual([r["id"] for r in result], [2, 5])

    def test_no_match_returns_empty(self):
        self.assertEqual(inventory.find_inventory_cleanup_candidates([], ["молоко"], canonicalize_name), [])

    # Stored canonical_name is stale/wrong (e.g. legacy row) but the row's
    # raw name still re-canonicalizes correctly — fresh re-derivation catches it.
    def test_matches_via_fresh_canonicalize_when_stored_value_is_stale(self):
        rows = [{"id": 9, "name": "mlekо", "canonical_name": "mlekо",
                 "category": "Молочне та яйця", "quantity_value": Decimal("1"), "quantity_unit": "шт."}]
        result = inventory.find_inventory_cleanup_candidates(rows, ["молоко"], canonicalize_name)
        self.assertEqual([r["id"] for r in result], [9])

    # "сосисок" (genitive plural) never gets canonicalized by the global
    # canonicalizer (no morphology) — the cleanup alias fallback on the raw
    # row name still finds it against the "сосиски" target.
    def test_matches_via_cleanup_alias_on_row_name(self):
        rows = [{"id": 12, "name": "сосисок", "canonical_name": "сосисок",
                 "category": "М'ясо та риба", "quantity_value": None, "quantity_unit": None}]
        result = inventory.find_inventory_cleanup_candidates(rows, ["сосиски"], canonicalize_name)
        self.assertEqual([r["id"] for r in result], [12])

    # Requirement #4: "об'єднай сосиски в запасах" must find BOTH
    # "Сосиски — 6 шт." and "сосисок — пару".
    def test_finds_both_numeric_and_text_quantity_sausage_rows(self):
        candidates = inventory.cleanup_canonical_name_candidates(canonicalize_name, "сосиски")
        rows = [
            {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": Decimal("6"), "quantity_unit": "шт."},
            {"id": 51, "name": "сосисок", "canonical_name": "сосисок", "category": "М'ясо та риба",
             "quantity_value": None, "quantity_unit": None, "quantity_text": "пару"},
        ]
        result = inventory.find_inventory_cleanup_candidates(rows, candidates, canonicalize_name)
        self.assertEqual({r["id"] for r in result}, {50, 51})


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

    # "пару" (text quantity) is never merged with a numeric "шт." row.
    def test_text_quantity_pair_never_merges_with_numeric_count(self):
        rows = [
            {"id": 60, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."},
            {"id": 61, "name": "сосисок", "canonical_name": "сосисок", "category": "М'ясо та риба",
             "quantity_value": None, "quantity_unit": None, "quantity_text": "пару"},
        ]
        result = inventory.group_inventory_cleanup_candidates(rows)
        self.assertEqual(result["groups"], [])
        self.assertEqual({r["id"] for r in result["incompatible"]}, {60, 61})

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


class TestDescribeCleanupIncompatibilityReason(unittest.TestCase):
    def test_numeric_vs_text_quantity(self):
        rows = [
            {"quantity_value": Decimal("6"), "quantity_unit": "шт."},
            {"quantity_value": None, "quantity_unit": None},
        ]
        self.assertEqual(
            inventory.describe_cleanup_incompatibility_reason(rows), "одна кількість числова, інша текстова",
        )

    def test_incompatible_unit_families(self):
        rows = [
            {"quantity_value": Decimal("1"), "quantity_unit": "шт."},
            {"quantity_value": Decimal("500"), "quantity_unit": "г"},
            {"quantity_value": Decimal("1"), "quantity_unit": "л"},
        ]
        self.assertEqual(inventory.describe_cleanup_incompatibility_reason(rows), "несумісні одиниці виміру")


class TestFormatInventoryCleanupPreview(unittest.TestCase):
    def test_compatible_group_and_incompatible_leftover(self):
        group = {
            "item_ids": [3, 2], "merged_name": "Молоко", "merged_quantity_text": "9,5 л",
            "merged_category": "Молочне та яйця", "canonical_name": "молоко",
            "merged_quantity_value": Decimal("9.5"), "merged_unit": "л", "merged_quantity_unit": "л",
            "items": [
                {"id": 3, "name": "Молоко", "quantity_value": Decimal("9"), "quantity_unit": "л", "quantity_text": "9 л"},
                {"id": 2, "name": "Молоко", "quantity_value": Decimal("500"), "quantity_unit": "мл", "quantity_text": "500 мл"},
            ],
        }
        incompatible = [{"id": 1, "name": "mlekо", "quantity_value": Decimal("1"),
                          "quantity_unit": "шт.", "quantity_text": "1 шт."}]
        text = inventory.format_inventory_cleanup_preview([group], incompatible, _effective_quantity_stub)
        self.assertIn("🧹 Можна безпечно об'єднати:", text)
        self.assertIn("Молоко — 9 л + Молоко — 500 мл", text)
        self.assertIn("→ Молоко — 9,5 л", text)
        self.assertIn("⚠️ Не об'єдную автоматично:", text)
        self.assertIn("mlekо — 1 шт.", text)
        self.assertIn("несумісна одиниця з л/мл", text)

    def test_no_compatible_groups_is_read_only_warning(self):
        incompatible = [
            {"id": 50, "name": "Сосиски", "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."},
            {"id": 51, "name": "сосисок", "quantity_value": None, "quantity_unit": None, "quantity_text": "пару"},
        ]
        text = inventory.format_inventory_cleanup_preview([], incompatible, _effective_quantity_stub)
        self.assertIn("🧹 Знайшов схожі записи:", text)
        self.assertIn("⚠️ Не можу безпечно об'єднати автоматично:", text)
        self.assertIn("Сосиски — 6 шт.", text)
        self.assertIn("сосисок — пару", text)
        self.assertIn("Причина: одна кількість числова, інша текстова.", text)
        self.assertNotIn("Можна безпечно об'єднати", text)


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
        {"id": 1, "name": "mlekо", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
        {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("500"), "quantity_unit": "мл", "quantity_text": "500 мл"},
        {"id": 3, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("9"), "quantity_unit": "л", "quantity_text": "9 л"},
    ]


def _sausage_rows():
    return [
        {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
         "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."},
        {"id": 51, "name": "сосисок", "canonical_name": "сосисок", "category": "М'ясо та риба",
         "quantity_value": None, "quantity_unit": None, "quantity_text": "пару"},
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

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


class TestDirectCleanupRequest(InventoryCleanupWebhookTestCase):
    # 3. Direct cleanup request finds duplicate candidates, builds a safe
    # merge preview, AND lists the skipped/incompatible row with a reason.
    def test_direct_request_builds_safe_merge_preview_with_skipped_row(self):
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
        self.assertTrue(any("🧹 Можна безпечно об'єднати:" in t for t in texts))
        self.assertTrue(any("9,5 л" in t for t in texts))
        self.assertTrue(any("⚠️ Не об'єдную автоматично:" in t and "1 шт." in t for t in texts))
        self.assertIn(MERGE_PREVIEW_KEYBOARD, self._reply_markups())

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

    # 4/5/6. "об'єднай сосиски в запасах" finds BOTH rows, never auto-merges
    # numeric шт. with text "пару", and shows no ✅ Об'єднати button.
    def test_sausage_rows_found_but_not_merged_and_no_confirm_button(self):
        chat_id = 770004
        with patch.object(bot, "get_inventory_items", return_value=_sausage_rows()):
            _call_webhook(_make_update(770000004, chat_id, "об'єднай сосиски в запасах"))
        self.assertNotIn(chat_id, pending_merge)
        texts = self._sent_texts()
        self.assertTrue(any("Сосиски — 6 шт." in t and "сосисок — пару" in t for t in texts))
        self.assertTrue(any("Причина: одна кількість числова, інша текстова." in t for t in texts))
        self.assertNotIn(MERGE_PREVIEW_KEYBOARD, self._reply_markups())

    # 11. A direct cleanup command while another pending preview/
    # clarification is active never silently falls through to general AI.
    def test_blocked_by_other_active_pending_state(self):
        chat_id = 770005
        bot.pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        try:
            with patch.object(bot, "get_inventory_items") as mock_items:
                _call_webhook(_make_update(770000005, chat_id, "об'єднай молоко в запасах"))
            mock_items.assert_not_called()
            self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG, self._sent_texts())
            self.assertNotIn(chat_id, pending_merge)
        finally:
            bot.pending_global_household.pop(chat_id, None)


class TestFollowupUsesPreviousContext(InventoryCleanupWebhookTestCase):
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

    # 8. Follow-up "об'єднай їх" uses the active cleanup context.
    def test_followup_yikh_confirms_existing_cleanup_preview(self):
        chat_id = 770010
        pending_merge[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_inventory_cleanup_merge", return_value=1) as mock_merge:
            _call_webhook(_make_update(770000010, chat_id, "об'єднай їх"))
        mock_merge.assert_called_once()
        called_household_id, called_actor_id = mock_merge.call_args[0][0], mock_merge.call_args[0][1]
        self.assertEqual((called_household_id, called_actor_id), (1, 10))
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("✅ Об'єднано груп: 1" == t for t in self._sent_texts()))

    # 9. Follow-up "об'єднай их" (common typo) also uses the active context.
    def test_followup_ykh_confirms_existing_cleanup_preview(self):
        chat_id = 770011
        pending_merge[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_inventory_cleanup_merge", return_value=1) as mock_merge:
            _call_webhook(_make_update(770000011, chat_id, "об'єднай их"))
        mock_merge.assert_called_once()
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("✅ Об'єднано груп: 1" == t for t in self._sent_texts()))

    # Follow-up "об'єднай их" without any prior context does not guess —
    # it asks the user to name the product, and never touches the DB.
    def test_followup_without_context_asks_for_clarification(self):
        chat_id = 770012
        with patch.object(bot, "execute_inventory_cleanup_merge") as mock_merge, \
                patch.object(bot, "get_inventory_items") as mock_items:
            _call_webhook(_make_update(770000012, chat_id, "об'єднай їх"))
        mock_merge.assert_not_called()
        mock_items.assert_not_called()
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("Напиши, який товар" in t for t in self._sent_texts()))


class TestConfirmAndCancel(InventoryCleanupWebhookTestCase):
    """Confirm/cancel go through the EXISTING "✅ Об'єднати"/"❌ Скасувати"
    button wiring in _try_handle_confirm_or_cancel — same code path already
    covered for list_type "inventory_saved" by
    tests/test_merge_stale_snapshot_protection.py, now extended with a
    dedicated "inventory_cleanup" branch that calls
    execute_inventory_cleanup_merge (records the undo journal row) instead
    of plain execute_merge_inventory."""

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

    # 6a. Confirm button applies only the previewed groups, via
    # execute_inventory_cleanup_merge (the undo-journal-recording write).
    def test_confirm_button_applies_merge_via_journal_recording_write(self):
        chat_id = 770020
        pending_merge[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_inventory_cleanup_merge", return_value=1) as mock_merge:
            _call_webhook(_make_update(770000020, chat_id, "✅ Об'єднати"))
        mock_merge.assert_called_once()
        called_household_id, called_actor_id, called_groups, called_targets = mock_merge.call_args[0]
        self.assertEqual(called_household_id, 1)
        self.assertEqual(called_actor_id, 10)
        self.assertEqual(len(called_groups), 1)
        self.assertEqual(len(called_targets), 2)
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("✅ Об'єднано груп: 1" == t for t in self._sent_texts()))

    # 6b. Confirm uses stale protection — a changed row aborts with no write.
    def test_confirm_aborts_on_stale_snapshot(self):
        chat_id = 770021
        pending_merge[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_inventory_cleanup_merge", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(770000021, chat_id, "✅ Об'єднати"))
        self.assertIn(STALE_PREVIEW_MSG, self._sent_texts())
        self.assertIn(INVENTORY_KEYBOARD, self._reply_markups())
        self.assertNotIn(chat_id, pending_merge)

    # 10. Cancel clears the cleanup pending context.
    def test_cancel_clears_pending_cleanup_context(self):
        chat_id = 770022
        pending_merge[chat_id] = self._pending_entry()
        _call_webhook(_make_update(770000022, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_merge)
        self.assertTrue(any("Об'єднання скасовано." in t for t in self._sent_texts()))


# =========================
# 1/2. Undo integration — execute_inventory_cleanup_merge (REAL database.py)
# records an Action History journal row, and apply_undo_action (the SAME
# generic restore every other global_household action already uses) can
# undo it. This is the direct fix for the live bug: confirming a cleanup
# merge must make it the LATEST undo-able action, not an older shopping one.
# =========================
class FakeCursor:
    """Same minimal fake as tests/test_cross_unit_inventory_merge.py's and
    tests/test_merge_stale_snapshot_protection.py's — queued fetchall()
    results consumed in call order, every execute() recorded verbatim."""

    def __init__(self, fetchall_results=None):
        self.queries = []
        self._fetchall_results = list(fetchall_results or [])

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _milk_group():
    return {
        "item_ids": [3, 2], "merged_name": "Молоко", "merged_quantity_text": "9,5 л",
        "merged_category": "Молочне та яйця", "canonical_name": "молоко",
        "merged_quantity_value": Decimal("9.5"), "merged_quantity_unit": "л",
        "items": [
            {"id": 3, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": Decimal("9"), "quantity_unit": "л"},
            {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": Decimal("500"), "quantity_unit": "мл"},
        ],
    }


def _milk_targets():
    return [
        {"item_id": 3, "quantity_value": Decimal("9"), "quantity_unit": "л",
         "canonical_name": "молоко", "category": "Молочне та яйця"},
        {"item_id": 2, "quantity_value": Decimal("500"), "quantity_unit": "мл",
         "canonical_name": "молоко", "category": "Молочне та яйця"},
    ]


class TestExecuteInventoryCleanupMergeRecordsJournal(unittest.TestCase):
    def test_inserts_global_household_journal_row(self):
        targets_verify_rows = [
            (3, Decimal("9"), "л", "молоко", "Молочне та яйця"),
            (2, Decimal("500"), "мл", "молоко", "Молочне та яйця"),
        ]
        before_bucket_rows = [
            (3, "Молоко", "молоко", "9 л", Decimal("9"), "л", False, "Молочне та яйця"),
            (2, "Молоко", "молоко", "500 мл", Decimal("500"), "мл", False, "Молочне та яйця"),
        ]
        after_bucket_rows = [
            (3, "Молоко", "молоко", "9,5 л", Decimal("9.5"), "л", False, "Молочне та яйця"),
        ]
        cursor = FakeCursor(fetchall_results=[targets_verify_rows, before_bucket_rows, after_bucket_rows])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.execute_inventory_cleanup_merge(1, 10, [_milk_group()], _milk_targets())

        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        insert_queries = [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]
        self.assertEqual(len(insert_queries), 1)
        sql, params = insert_queries[0]
        self.assertIn("'global_household'", sql)
        household_id, actor_user_id = params[0], params[1]
        self.assertEqual((household_id, actor_user_id), (1, 10))
        before_snapshot = params[3].obj
        post_action_snapshot = params[4].obj
        self.assertIn("молоко", before_snapshot["inventory_buckets"])
        self.assertIn("молоко", post_action_snapshot["inventory_buckets"])
        self.assertEqual(len(before_snapshot["inventory_buckets"]["молоко"]), 2)
        self.assertEqual(len(post_action_snapshot["inventory_buckets"]["молоко"]), 1)
        summary = params[5].obj
        self.assertTrue(summary["inventory"])


class TestApplyUndoActionRestoresCleanupMerge(unittest.TestCase):
    """apply_undo_action's restore is generic (keyed off before/post
    snapshot shape, not operation_type) — this proves it correctly reverses
    the EXACT snapshot shape execute_inventory_cleanup_merge produces (one
    row updated back, one row reinserted), same as it already does for
    apply_global_household_operations' own merges."""

    def test_undo_restores_both_rows(self):
        before_row_9l = {"id": 3, "household_id": 1, "name": "Молоко", "canonical_name": "молоко",
                          "quantity_text": "9 л", "quantity_value": "9", "quantity_unit": "л",
                          "quantity_inferred": False, "category": "Молочне та яйця"}
        before_row_500ml = {"id": 2, "household_id": 1, "name": "Молоко", "canonical_name": "молоко",
                             "quantity_text": "500 мл", "quantity_value": "500", "quantity_unit": "мл",
                             "quantity_inferred": False, "category": "Молочне та яйця"}
        post_row_merged = {"id": 3, "household_id": 1, "name": "Молоко", "canonical_name": "молоко",
                            "quantity_text": "9,5 л", "quantity_value": "9.5", "quantity_unit": "л",
                            "quantity_inferred": False, "category": "Молочне та яйця"}

        before_snapshot = {"inventory_buckets": {"молоко": [before_row_9l, before_row_500ml]},
                            "shopping_buckets": {}, "expense_delete": None}
        post_action_snapshot = {"inventory_buckets": {"молоко": [post_row_merged]},
                                 "shopping_buckets": {}, "expense_adds": []}

        journal_row = (1, 10, "active", before_snapshot, post_action_snapshot)
        current_bucket_rows = [
            (3, "Молоко", "молоко", "9,5 л", Decimal("9.5"), "л", False, "Молочне та яйця"),
        ]
        cursor = FakeCursor(fetchall_results=[current_bucket_rows])
        cursor.fetchone = lambda: journal_row
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items SET" in q[0]]
        insert_queries = [q for q in cursor.queries if "INSERT INTO inventory_items" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn(Decimal("9"), update_queries[0][1])
        self.assertEqual(len(insert_queries), 1)
        self.assertIn(Decimal("500"), insert_queries[0][1])
        self.assertTrue(any("status='undone'" in sql for sql, _ in cursor.queries))


class TestExistingRoutesStillWork(InventoryCleanupWebhookTestCase):
    """12. Spot-check a few unrelated routes this feature must never affect."""

    def test_household_read_question_still_works(self):
        chat_id = 770030
        with patch.object(bot, "get_active_shopping_items", return_value=[]):
            _call_webhook(_make_update(770000030, chat_id, "Що треба купити?"))
        texts = self._sent_texts()
        self.assertTrue(texts)
        self.assertNotIn(chat_id, pending_merge)

    def test_meal_ideas_question_still_works(self):
        chat_id = 770031
        with patch.object(bot.meal_ideas, "try_handle_meal_ideas", return_value=True) as mock_meal:
            _call_webhook(_make_update(770000031, chat_id, "Що можна приготувати?"))
        mock_meal.assert_called_once()

    def test_meal_ideas_button_still_works(self):
        chat_id = 770032
        with patch.object(bot.meal_ideas, "try_handle_meal_ideas", return_value=True) as mock_meal:
            _call_webhook(_make_update(770000032, chat_id, "🍽 Що приготувати"))
        mock_meal.assert_called_once()

    def test_undo_button_during_quantity_clarification_still_cancels_it(self):
        chat_id = 770033
        bot.pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "new_expense": None, "delete_expense": None,
        }
        try:
            _call_webhook(_make_update(770000033, chat_id, "↩️ Скасувати останню дію"))
            self.assertNotIn(chat_id, bot.pending_inventory_quantity_clarification)
            self.assertTrue(any("Поточну дію скасовано." in t for t in self._sent_texts()))
        finally:
            bot.pending_inventory_quantity_clarification.pop(chat_id, None)

    def test_emoji_variation_selector_cooking_button_still_works(self):
        chat_id = 770034
        with patch.object(bot, "waiting_for_ingredients", {}):
            _call_webhook(_make_update(770000034, chat_id, "🍽️ Що приготувати"))
        self.assertNotIn(chat_id, pending_merge)


if __name__ == "__main__":
    unittest.main()
