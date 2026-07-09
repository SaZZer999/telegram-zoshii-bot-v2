"""Inventory Cleanup Admin v1 — deterministic rename/delete of ONE existing
inventory row ("перейменуй ser на сир", "видали mlekо із запасів", "прибери
сосисок — пару").

Covers: the pure text-classification/candidate-resolution/preview-formatting
helpers in inventory.py (parse_inventory_rename_request,
parse_inventory_delete_request, resolve_inventory_admin_candidates,
capitalize_first, format_inventory_rename_preview,
format_inventory_delete_preview, format_inventory_admin_ambiguous_message),
the webhook-level route in bot.py (_route_inventory_admin /
_start_inventory_rename / _start_inventory_delete /
_apply_cleanup_admin_confirm), and database.execute_inventory_rename/
execute_inventory_delete's Action History journal integration (same
operation_type/restore path apply_global_household_operations and
execute_inventory_cleanup_merge already use, verified here against the REAL
database.py). No real Gemini, Telegram, Render, or Supabase call happens
anywhere in this file.
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
_spec = importlib.util.spec_from_file_location("real_database_for_inventory_admin_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import action_history  # noqa: E402
import bot  # noqa: E402
from bot import (  # noqa: E402
    pending_cleanup_admin,
    pending_cleanup_admin_disambiguation,
    pending_cleanup_notice,
    pending_merge,
    pending_destructive_guard,
    canonicalize_name,
    _normalize_display_name_for_exact_match,
    STALE_PREVIEW_MSG,
    INVENTORY_KEYBOARD,
    SHOPPING_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
    INVENTORY_ADMIN_NOT_FOUND_MSG,
    DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG,
    DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG,
    DESTRUCTIVE_GUARD_CANCELLED_MSG,
)


def _effective_quantity_stub(item):
    value = item.get("quantity_value")
    unit = item.get("quantity_unit")
    text = item.get("quantity_text") or ""
    return value, unit, text


def _milk_dirty_row():
    return {"id": 1, "name": "mlekо", "canonical_name": "молоко", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."}


def _cheese_dirty_row():
    return {"id": 5, "name": "ser", "canonical_name": "сир", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."}


def _moloko_row():
    """A DIFFERENT row (id 2) that merely shares canonical_name "молоко" with
    _milk_dirty_row()'s "mlekо" (id 1) — the live bug: a rename targeting
    "mlekо" must never also match this row just because both canonicalize
    to the same product."""
    return {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "quantity_value": Decimal("11.5"), "quantity_unit": "л", "quantity_text": "11,5 л"}


def _sausage_rows():
    return [
        {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
         "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."},
        {"id": 51, "name": "сосисок", "canonical_name": "сосисок", "category": "М'ясо та риба",
         "quantity_value": None, "quantity_unit": None, "quantity_text": "пару"},
    ]


def _milk_multi_rows():
    """Two DIFFERENT "Молоко" rows — used for the ambiguous-candidates case."""
    return [
        {"id": 10, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "л", "quantity_text": "1 л"},
        {"id": 11, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("2"), "quantity_unit": "л", "quantity_text": "2 л"},
    ]


def _milk_litres_and_pieces_rows():
    """The exact V1.4 live-bug inventory: "Молоко — 12,5 л" and
    "Молоко — 1 шт." — a delete-by-name+quantity request must hit ONLY the
    "1 шт." row, never the litres one."""
    return [
        {"id": 20, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("12.5"), "quantity_unit": "л", "quantity_text": "12,5 л"},
        {"id": 21, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
    ]


# =========================
# Pure helpers (inventory.py) — no DB, no Telegram.
# =========================
class TestParseInventoryRenameRequest(unittest.TestCase):
    def test_perejmenuj_with_location_suffix(self):
        self.assertEqual(
            inventory.parse_inventory_rename_request("перейменуй mlekо на молоко в запасах"),
            ("mlekо", "молоко"),
        )

    def test_perejmenuj_without_location_suffix(self):
        self.assertEqual(inventory.parse_inventory_rename_request("перейменуй mlekо на молоко"), ("mlekо", "молоко"))

    def test_perejmenuj_ser_na_syr(self):
        self.assertEqual(inventory.parse_inventory_rename_request("перейменуй ser на сир"), ("ser", "сир"))

    def test_vypravy_mlekо_na_moloko(self):
        self.assertEqual(inventory.parse_inventory_rename_request("виправ mlekо на молоко"), ("mlekо", "молоко"))

    def test_vypravy_ser_na_syr(self):
        self.assertEqual(inventory.parse_inventory_rename_request("виправ ser на сир"), ("ser", "сир"))

    def test_zminy_nazvu(self):
        self.assertEqual(inventory.parse_inventory_rename_request("зміни назву mlekо на молоко"), ("mlekо", "молоко"))

    # Unified Household AI Action Planner V1 — "заміни X на Y" is a common
    # colloquial rename phrasing that wasn't in the original trigger list.
    def test_zamin_ser_na_syr(self):
        self.assertEqual(inventory.parse_inventory_rename_request("заміни ser на сир"), ("ser", "сир"))

    def test_not_a_rename_phrase_returns_none(self):
        self.assertEqual(inventory.parse_inventory_rename_request("Купив молоко"), (None, None))

    def test_blank_text_returns_none(self):
        self.assertEqual(inventory.parse_inventory_rename_request(""), (None, None))


class TestParseInventoryDeleteRequest(unittest.TestCase):
    def test_vydaly_with_location_suffix(self):
        self.assertEqual(inventory.parse_inventory_delete_request("видали mlekо із запасів"), ("mlekо", None))

    def test_prybery_with_location_suffix(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери mlekо із запасів"), ("mlekо", None))

    def test_vydaly_ser_iz_zapasiv(self):
        self.assertEqual(inventory.parse_inventory_delete_request("видали ser із запасів"), ("ser", None))

    def test_vydaly_bare_text_quantity(self):
        self.assertEqual(inventory.parse_inventory_delete_request("видали сосисок пару"), ("сосисок", "пару"))

    def test_prybery_dash_text_quantity(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери сосисок — пару"), ("сосисок", "пару"))

    def test_prybery_zapys_dash_text_quantity(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери запис сосисок — пару"), ("сосисок", "пару"))

    def test_multi_word_product_name_never_mis_split(self):
        self.assertEqual(inventory.parse_inventory_delete_request("видали кокосове молоко"), ("кокосове молоко", None))

    def test_cleanup_duplicate_phrase_not_rejected_here_caller_must_try_cleanup_first(self):
        # This function has no "дублікат" special-case — bot.py's dispatch
        # order (cleanup route BEFORE admin route) is what actually prevents
        # a collision; documented in the function's own docstring.
        self.assertEqual(
            inventory.parse_inventory_delete_request("прибери дублікати молока"), ("дублікати молока", None),
        )

    def test_bare_bulk_pronoun_returns_none(self):
        self.assertEqual(inventory.parse_inventory_delete_request("Видали всі"), (None, None))
        self.assertEqual(inventory.parse_inventory_delete_request("видали все"), (None, None))
        self.assertEqual(inventory.parse_inventory_delete_request("прибери усі, крім молока"), (None, None))

    def test_shopping_list_location_returns_none(self):
        self.assertEqual(inventory.parse_inventory_delete_request("видали молоко зі списку покупок"), (None, None))

    def test_not_a_delete_phrase_returns_none(self):
        self.assertEqual(inventory.parse_inventory_delete_request("Використати молоко 500 мл"), (None, None))

    def test_blank_text_returns_none(self):
        self.assertEqual(inventory.parse_inventory_delete_request(""), (None, None))


class TestResolveInventoryAdminCandidates(unittest.TestCase):
    # 6/7. "сосисок пару" narrows down to exactly the text-quantity row, not
    # the numeric "Сосиски — 6 шт." row.
    def test_quantity_hint_narrows_to_exact_row(self):
        candidates = inventory.resolve_inventory_admin_candidates(
            _sausage_rows(), inventory.cleanup_canonical_name_candidates(canonicalize_name, "сосисок"),
            canonicalize_name, quantity_hint="пару",
        )
        self.assertEqual([c["id"] for c in candidates], [51])

    def test_no_quantity_hint_returns_every_candidate(self):
        candidates = inventory.resolve_inventory_admin_candidates(
            _sausage_rows(), inventory.cleanup_canonical_name_candidates(canonicalize_name, "сосисок"),
            canonicalize_name, quantity_hint=None,
        )
        self.assertEqual({c["id"] for c in candidates}, {50, 51})

    def test_quantity_hint_matching_nothing_falls_back_to_full_list(self):
        candidates = inventory.resolve_inventory_admin_candidates(
            _sausage_rows(), inventory.cleanup_canonical_name_candidates(canonicalize_name, "сосисок"),
            canonicalize_name, quantity_hint="10 кг",
        )
        self.assertEqual({c["id"] for c in candidates}, {50, 51})


class TestExactVisibleRowNameMatchWinsOverAlias(unittest.TestCase):
    """V1.3 fix: "mlekо" must resolve to the row literally named "mlekо",
    never also to a DIFFERENT "Молоко" row sharing the same canonical_name —
    the exact live bug reported after the v1 cleanup-admin release."""

    def _rows(self):
        return [_milk_dirty_row(), _moloko_row()]

    def test_exact_row_wins_no_ambiguity(self):
        rows = self._rows()
        candidates = inventory.resolve_inventory_admin_candidates(
            rows, inventory.cleanup_canonical_name_candidates(canonicalize_name, "mlekо"),
            canonicalize_name, name_phrase="mlekо", name_normalizer=_normalize_display_name_for_exact_match,
        )
        self.assertEqual([c["id"] for c in candidates], [1])

    def test_confusable_o_and_case_variants_all_resolve_to_the_same_row(self):
        rows = self._rows()
        for phrase in ("mleko", "Mleko", "mlekо", "Mlekо"):
            with self.subTest(phrase=phrase):
                candidates = inventory.resolve_inventory_admin_candidates(
                    rows, inventory.cleanup_canonical_name_candidates(canonicalize_name, phrase),
                    canonicalize_name, name_phrase=phrase, name_normalizer=_normalize_display_name_for_exact_match,
                )
                self.assertEqual([c["id"] for c in candidates], [1])

    def test_alias_fallback_still_works_when_no_exact_visible_row_exists(self):
        # No row is literally named "молока" (genitive) — only the cleanup
        # alias table's "молока" -> "молоко" mapping finds the "Молоко" row
        # via its canonical_name, exactly like before this fix existed.
        rows = [_moloko_row()]
        candidates = inventory.resolve_inventory_admin_candidates(
            rows, inventory.cleanup_canonical_name_candidates(canonicalize_name, "молока"),
            canonicalize_name, name_phrase="молока", name_normalizer=_normalize_display_name_for_exact_match,
        )
        self.assertEqual([c["id"] for c in candidates], [2])

    def test_omitting_name_phrase_keeps_old_behavior(self):
        # A caller that doesn't pass name_phrase/name_normalizer at all (the
        # pre-fix call shape) still goes straight to the alias/canonical
        # pool — both rows share canonical_name "молоко", so both are
        # returned (still ambiguous), same as before this fix.
        rows = self._rows()
        candidates = inventory.resolve_inventory_admin_candidates(
            rows, inventory.cleanup_canonical_name_candidates(canonicalize_name, "молоко"), canonicalize_name,
        )
        self.assertEqual({c["id"] for c in candidates}, {1, 2})


class TestResolveCleanupAdminDisambiguationReply(unittest.TestCase):
    """Pure follow-up resolver for a previously-shown ambiguous-candidates
    list — no webhook, no pending state, just the deterministic matching
    rules against a hand-built candidate list."""

    def _candidates(self):
        return [_milk_dirty_row(), _moloko_row()]

    def test_name_and_quantity_fragment_selects_the_matching_candidate(self):
        for text in ("Mleko 1 шт", "mlekо 1 шт", "mlekо — 1 шт"):
            with self.subTest(text=text):
                selected = inventory.resolve_cleanup_admin_disambiguation_reply(
                    text, self._candidates(), _normalize_display_name_for_exact_match,
                )
                self.assertIsNotNone(selected)
                self.assertEqual(selected["id"], 1)

    def test_bare_quantity_selects_the_unique_matching_candidate(self):
        selected = inventory.resolve_cleanup_admin_disambiguation_reply(
            "1 шт", self._candidates(), _normalize_display_name_for_exact_match,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["id"], 1)

    def test_numbered_selector_selects_by_position(self):
        candidates = self._candidates()
        self.assertEqual(
            inventory.resolve_cleanup_admin_disambiguation_reply("№1", candidates, _normalize_display_name_for_exact_match)["id"],
            1,
        )
        self.assertEqual(
            inventory.resolve_cleanup_admin_disambiguation_reply("2", candidates, _normalize_display_name_for_exact_match)["id"],
            2,
        )

    def test_numbered_selector_out_of_range_returns_none(self):
        selected = inventory.resolve_cleanup_admin_disambiguation_reply(
            "5", self._candidates(), _normalize_display_name_for_exact_match,
        )
        self.assertIsNone(selected)

    def test_still_ambiguous_reply_returns_none(self):
        # Neither name nor quantity fragment narrows the pool at all.
        selected = inventory.resolve_cleanup_admin_disambiguation_reply(
            "щось незрозуміле", self._candidates(), _normalize_display_name_for_exact_match,
        )
        self.assertIsNone(selected)

    def test_blank_reply_returns_none(self):
        self.assertIsNone(
            inventory.resolve_cleanup_admin_disambiguation_reply("", self._candidates(), _normalize_display_name_for_exact_match)
        )


class TestCapitalizeFirst(unittest.TestCase):
    def test_single_word(self):
        self.assertEqual(inventory.capitalize_first("молоко"), "Молоко")

    def test_multi_word_only_first_letter(self):
        self.assertEqual(inventory.capitalize_first("зелений чай"), "Зелений чай")

    def test_blank(self):
        self.assertEqual(inventory.capitalize_first(""), "")


class TestFormatters(unittest.TestCase):
    def test_rename_preview(self):
        text = inventory.format_inventory_rename_preview("mlekо", "1 шт.", "Молоко")
        self.assertIn("План змін:", text)
        self.assertIn("🧊 Запаси", text)
        self.assertIn("• mlekо — 1 шт. → Молоко — 1 шт.", text)

    def test_delete_preview(self):
        text = inventory.format_inventory_delete_preview("сосисок", "пару")
        self.assertIn("• Прибрати сосисок — пару", text)

    def test_ambiguous_message_lists_every_candidate(self):
        text = inventory.format_inventory_admin_ambiguous_message(_milk_multi_rows(), _effective_quantity_stub)
        self.assertIn("Молоко — 1 л", text)
        self.assertIn("Молоко — 2 л", text)
        self.assertIn("не хочу вгадувати", text)


class TestIsNoopRename(unittest.TestCase):
    """V1.4: renaming a row to the name it ALREADY has must never be treated
    as a real change — e.g. re-running "перейменуй ser на сир" after "ser"
    was already renamed to "Сир" (alias/canonical fallback still finds that
    row, since its canonical_name is "сир")."""

    def _normalizer(self, s):
        return _normalize_display_name_for_exact_match(s)

    def test_already_renamed_row_is_noop(self):
        row = {"name": "Сир", "canonical_name": "сир"}
        self.assertTrue(inventory.is_noop_rename(row, "Сир", "сир", self._normalizer))

    def test_case_and_homoglyph_insensitive_noop(self):
        row = {"name": "mlekо", "canonical_name": "молоко"}
        self.assertTrue(inventory.is_noop_rename(row, "Mleko", "молоко", self._normalizer))

    def test_real_rename_is_not_noop(self):
        row = {"name": "ser", "canonical_name": "сир"}
        self.assertFalse(inventory.is_noop_rename(row, "Сир", "сир", self._normalizer))

    def test_same_display_name_different_canonical_is_not_noop(self):
        # Defensive: a same-looking display name but a genuinely different
        # canonical_name target is still a real change.
        row = {"name": "Сир", "canonical_name": "сир"}
        self.assertFalse(inventory.is_noop_rename(row, "Сир", "інший_сир", self._normalizer))

    def test_format_noop_rename_message(self):
        text = inventory.format_noop_rename_message("Сир")
        self.assertIn("Сир", text)
        self.assertIn("Змін не потрібно", text)


class TestParseInventoryDeleteRequestNumericQuantity(unittest.TestCase):
    """V1.4: "прибери/видали <name> <numeric quantity>" (with or without a
    dash separator, with or without a trailing unit dot, "штуку" included)
    must split into (name, canonical quantity hint) — never treated as one
    long unmatched name phrase."""

    def test_no_dash_numeric_count(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери Молоко 1 шт"), ("Молоко", "1 шт."))

    def test_no_dash_video_variant(self):
        self.assertEqual(inventory.parse_inventory_delete_request("видали Молоко 1 шт"), ("Молоко", "1 шт."))

    def test_dash_numeric_count(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери Молоко — 1 шт"), ("Молоко", "1 шт."))

    def test_lowercase_name(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери молоко 1 шт"), ("молоко", "1 шт."))

    def test_stuku_accusative_unit_word(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери молоко 1 штуку"), ("молоко", "1 шт."))

    def test_trailing_dot_after_unit_is_stripped_before_parsing(self):
        self.assertEqual(inventory.parse_inventory_delete_request("прибери молоко 1 шт."), ("молоко", "1 шт."))

    def test_multi_word_name_with_trailing_quantity(self):
        self.assertEqual(
            inventory.parse_inventory_delete_request("прибери кокосове молоко 2 шт"), ("кокосове молоко", "2 шт."),
        )

    def test_word_number_quantity_still_takes_priority_and_stays_unnormalized(self):
        # Regression: "пару" must never be re-rendered into "2 шт." — that
        # would break the existing "сосисок — пару"/"сосисок пару" matching,
        # since the row's OWN quantity_text is the literal word "пару".
        self.assertEqual(inventory.parse_inventory_delete_request("прибери сосисок пару"), ("сосисок", "пару"))
        self.assertEqual(inventory.parse_inventory_delete_request("прибери сосисок — пару"), ("сосисок", "пару"))


# =========================
# Webhook-level routing (bot.py) — network/DB calls patched.
# =========================
def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class InventoryAdminWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_cleanup_admin.clear()
        pending_cleanup_admin_disambiguation.clear()
        pending_cleanup_notice.clear()
        pending_merge.clear()
        pending_destructive_guard.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_cleanup_admin.clear()
        pending_cleanup_admin_disambiguation.clear()
        pending_cleanup_notice.clear()
        pending_merge.clear()
        pending_destructive_guard.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


class TestRenamePreview(InventoryAdminWebhookTestCase):
    # 1. "перейменуй mlekо на молоко в запасах" creates a rename preview.
    def test_rename_creates_preview(self):
        chat_id = 771001
        with patch.object(bot, "get_inventory_items", return_value=[_milk_dirty_row()]):
            _call_webhook(_make_update(771000001, chat_id, "перейменуй mlekо на молоко в запасах"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["action"], "rename")
        self.assertEqual(entry["item_id"], 1)
        self.assertEqual(entry["new_name"], "Молоко")
        self.assertEqual(entry["new_canonical_name"], "молоко")
        texts = self._sent_texts()
        self.assertTrue(any("mlekо — 1 шт. → Молоко — 1 шт." in t for t in texts))
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())

    # V1.3 live bug: "mlekо" (mixed-script) must target ONLY the row
    # literally named "mlekо" — never also "Молоко — 11,5 л" just because
    # both canonicalize to the same product.
    def test_rename_exact_row_wins_over_alias_sibling_no_ambiguity(self):
        chat_id = 771005
        with patch.object(bot, "get_inventory_items", return_value=[_milk_dirty_row(), _moloko_row()]):
            _call_webhook(_make_update(771000005, chat_id, "перейменуй mlekо на молоко в запасах"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["item_id"], 1)
        texts = self._sent_texts()
        self.assertTrue(any("mlekо — 1 шт. → Молоко — 1 шт." in t for t in texts))
        self.assertFalse(any("не хочу вгадувати" in t for t in texts))
        self.assertFalse(any("11,5 л" in t for t in texts))

    # Unified Household AI Action Planner V1 — "заміни ser на сир" creates
    # the same rename preview as "перейменуй ser на сир" via the existing
    # safe deterministic flow (no new write path).
    def test_zamin_ser_na_syr_creates_rename_preview(self):
        chat_id = 771009
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_dirty_row()]):
            _call_webhook(_make_update(771000009, chat_id, "заміни ser на сир"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["action"], "rename")
        self.assertEqual(entry["new_name"], "Сир")
        texts = self._sent_texts()
        self.assertTrue(any("ser — 1 шт. → Сир — 1 шт." in t for t in texts))

    # 5. "перейменуй ser на сир" works the same way.
    def test_rename_ser_na_syr(self):
        chat_id = 771002
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_dirty_row()]):
            _call_webhook(_make_update(771000002, chat_id, "перейменуй ser на сир"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["new_name"], "Сир")
        texts = self._sent_texts()
        self.assertTrue(any("ser — 1 шт. → Сир — 1 шт." in t for t in texts))

    # 11. No matching row at all.
    def test_rename_not_found(self):
        chat_id = 771003
        with patch.object(bot, "get_inventory_items", return_value=[]):
            _call_webhook(_make_update(771000003, chat_id, "перейменуй сир на Сир"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any(INVENTORY_ADMIN_NOT_FOUND_MSG == t for t in self._sent_texts()))

    # V1.4 live bug: "ser" no longer exists (already renamed to "Сир") — the
    # alias/canonical fallback still finds "Сир" (its canonical_name is
    # "сир"), but renaming it to itself must never create a preview.
    def test_noop_rename_does_not_create_preview(self):
        chat_id = 771006
        already_renamed_row = {
            "id": 5, "name": "Сир", "canonical_name": "сир", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
        }
        with patch.object(bot, "get_inventory_items", return_value=[already_renamed_row]):
            _call_webhook(_make_update(771000006, chat_id, "перейменуй ser на сир"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        texts = self._sent_texts()
        self.assertTrue(any("Змін не потрібно" in t for t in texts))
        self.assertFalse(any("Сир — 1 шт. → Сир — 1 шт." in t for t in texts))

    # No journal-writing call may ever happen for a no-op rename — the
    # regression was an empty "Буде повернено:" undo preview created by a
    # meaningless UPDATE+journal-INSERT for an unchanged row.
    def test_noop_rename_never_calls_execute_inventory_rename(self):
        chat_id = 771007
        already_renamed_row = {
            "id": 5, "name": "Сир", "canonical_name": "сир", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
        }
        with patch.object(bot, "get_inventory_items", return_value=[already_renamed_row]):
            with patch.object(bot, "execute_inventory_rename") as mock_rename:
                _call_webhook(_make_update(771000007, chat_id, "перейменуй ser на сир"))
        mock_rename.assert_not_called()

    # 10. Multiple matching rows — never guess.
    def test_rename_ambiguous_asks_for_clarification(self):
        chat_id = 771004
        with patch.object(bot, "get_inventory_items", return_value=_milk_multi_rows()):
            _call_webhook(_make_update(771000004, chat_id, "перейменуй молоко на Молоко3.2%"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        texts = self._sent_texts()
        self.assertTrue(any("не хочу вгадувати" in t for t in texts))
        # Both rows are literally named "Молоко" — an exact-name tie, still
        # genuinely ambiguous even after the V1.3 exact-row-match fix — so a
        # pending disambiguation context must be stored for a follow-up.
        self.assertIn(chat_id, pending_cleanup_admin_disambiguation)
        entry = pending_cleanup_admin_disambiguation[chat_id]
        self.assertEqual(entry["action"], "rename")
        self.assertEqual({c["id"] for c in entry["candidates"]}, {10, 11})
        self.assertEqual(entry["new_phrase"], "Молоко3.2%")


class TestRenameConfirmAndCancel(InventoryAdminWebhookTestCase):
    # Same monkeypatch as test_inventory_cleanup_merge.py's TestConfirmAndCancel:
    # bot.StaleSnapshotError is bound to a bare MagicMock attribute
    # (sys.modules['database'] was mocked at bot import time), not a real
    # exception class — rebind it to the real one for this class.
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def _pending_rename_entry(self):
        return {
            "action": "rename", "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_id": 1, "new_name": "Молоко", "new_canonical_name": "молоко",
            "target": {"item_id": 1, "quantity_value": Decimal("1"), "quantity_unit": "шт.",
                       "name": "mlekо", "canonical_name": "молоко"},
        }

    # 2. Confirming rename updates only that row.
    def test_confirm_applies_rename_via_journal_recording_write(self):
        chat_id = 771010
        pending_cleanup_admin[chat_id] = self._pending_rename_entry()
        with patch.object(bot, "execute_inventory_rename", return_value=True) as mock_rename:
            _call_webhook(_make_update(771000010, chat_id, "✅ Так, застосувати"))
        mock_rename.assert_called_once_with(1, 10, 1, "Молоко", "молоко", self._pending_rename_entry()["target"])
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("✅ Зміни застосовано." == t for t in self._sent_texts()))

    # 3. Rename uses stale protection.
    def test_confirm_aborts_on_stale_snapshot(self):
        chat_id = 771011
        pending_cleanup_admin[chat_id] = self._pending_rename_entry()
        with patch.object(bot, "execute_inventory_rename", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(771000011, chat_id, "✅ Так, застосувати"))
        self.assertIn(STALE_PREVIEW_MSG, self._sent_texts())
        self.assertNotIn(chat_id, pending_cleanup_admin)

    # 13. Cancel clears the pending rename.
    def test_cancel_clears_pending_rename(self):
        chat_id = 771012
        pending_cleanup_admin[chat_id] = self._pending_rename_entry()
        _call_webhook(_make_update(771000012, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


class TestDeletePreview(InventoryAdminWebhookTestCase):
    # 6. "прибери сосисок пару" creates a delete preview for exactly
    # "сосисок — пару" (never the numeric "Сосиски — 6 шт." row).
    def test_delete_creates_preview_for_exact_row(self):
        chat_id = 771020
        with patch.object(bot, "get_inventory_items", return_value=_sausage_rows()):
            _call_webhook(_make_update(771000020, chat_id, "прибери сосисок пару"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["action"], "delete")
        self.assertEqual(entry["item_id"], 51)
        texts = self._sent_texts()
        self.assertTrue(any("• Прибрати сосисок — пару" in t for t in texts))
        self.assertFalse(any("6 шт." in t for t in texts))

    def test_delete_with_dash_creates_preview_for_exact_row(self):
        chat_id = 771021
        with patch.object(bot, "get_inventory_items", return_value=_sausage_rows()):
            _call_webhook(_make_update(771000021, chat_id, "прибери сосисок — пару"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 51)

    # 11. No matching row.
    def test_delete_not_found(self):
        chat_id = 771022
        with patch.object(bot, "get_inventory_items", return_value=[]):
            _call_webhook(_make_update(771000022, chat_id, "видали mlekо із запасів"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any(INVENTORY_ADMIN_NOT_FOUND_MSG == t for t in self._sent_texts()))

    # 10. Multiple matching rows with no quantity hint — never guess.
    def test_delete_ambiguous_asks_for_clarification(self):
        chat_id = 771023
        with patch.object(bot, "get_inventory_items", return_value=_milk_multi_rows()):
            _call_webhook(_make_update(771000023, chat_id, "видали молоко"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        texts = self._sent_texts()
        self.assertTrue(any("не хочу вгадувати" in t for t in texts))
        self.assertTrue(any("Молоко — 1 л" in t for t in texts))
        self.assertTrue(any("Молоко — 2 л" in t for t in texts))

    # 12. Contextual follow-up: after "об'єднай сосиски в запасах" shows the
    # no-safe-merge warning, "прибери сосисок пару" still previews delete
    # for the exact row (whether resolved directly from live inventory or
    # via the cleanup-notice context — either way the end result must be
    # the single correct row).
    def test_followup_after_cleanup_warning_previews_delete(self):
        chat_id = 771024
        with patch.object(bot, "get_household_alias_map", return_value={}):
            with patch.object(bot, "get_inventory_items", return_value=_sausage_rows()):
                _call_webhook(_make_update(771000024, chat_id, "об'єднай сосиски в запасах"))
        self.assertIn(chat_id, pending_cleanup_notice)
        self.mock_send.reset_mock()

        with patch.object(bot, "get_inventory_items", return_value=_sausage_rows()):
            _call_webhook(_make_update(771000025, chat_id, "прибери сосисок пару"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 51)
        # The cleanup notice is consumed by the follow-up admin action —
        # a LATER undo press must reach normal historical undo, not the
        # (now stale) "cleanup check" acknowledgement.
        self.assertNotIn(chat_id, pending_cleanup_notice)


class TestDeleteByNameAndNumericQuantity(InventoryAdminWebhookTestCase):
    """V1.4 live bug: "прибери Молоко 1 шт" (numeric count, no dash) failed
    to find "Молоко — 1 шт." next to "Молоко — 12,5 л" — parse_inventory_
    delete_request only split off a WORD-number quantity ("пару"), never a
    numeric one."""

    def _assert_targets_only_pieces_row(self, chat_id, update_id, text):
        with patch.object(bot, "get_inventory_items", return_value=_milk_litres_and_pieces_rows()):
            _call_webhook(_make_update(update_id, chat_id, text))
        self.assertIn(chat_id, pending_cleanup_admin, f"{text!r} did not create a pending delete")
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["action"], "delete")
        self.assertEqual(entry["item_id"], 21)
        texts = self._sent_texts()
        self.assertTrue(any("Прибрати Молоко — 1 шт." in t for t in texts))
        self.assertFalse(any("12,5" in t for t in texts))

    def test_prybery_no_dash(self):
        self._assert_targets_only_pieces_row(771091, 771091001, "прибери Молоко 1 шт")

    def test_prybery_dash(self):
        self._assert_targets_only_pieces_row(771092, 771092001, "прибери Молоко — 1 шт")

    def test_vydaly_no_dash(self):
        self._assert_targets_only_pieces_row(771093, 771093001, "видали Молоко 1 шт")

    def test_vydaly_dash(self):
        self._assert_targets_only_pieces_row(771094, 771094001, "видали Молоко — 1 шт")

    def test_lowercase_name_stuku(self):
        self._assert_targets_only_pieces_row(771095, 771095001, "прибери молоко 1 штуку")

    def test_trailing_dot_after_unit(self):
        self._assert_targets_only_pieces_row(771096, 771096001, "прибери молоко 1 шт.")

    def test_does_not_match_litres_row(self):
        chat_id = 771099
        with patch.object(bot, "get_inventory_items", return_value=_milk_litres_and_pieces_rows()):
            _call_webhook(_make_update(771099001, chat_id, "прибери Молоко 12,5 л"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 20)

    # If quantity narrowing still leaves 2+ rows, ask for clarification —
    # never silently guess.
    def test_still_ambiguous_without_narrowing_quantity(self):
        chat_id = 771098
        with patch.object(bot, "get_inventory_items", return_value=_milk_multi_rows()):
            _call_webhook(_make_update(771098001, chat_id, "прибери Молоко 5 л"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("не хочу вгадувати" in t for t in self._sent_texts()))


class TestDeleteConfirmAndCancel(InventoryAdminWebhookTestCase):
    # Same monkeypatch as above — bot.StaleSnapshotError needs rebinding to
    # the real exception class for assertRaises/side_effect to work.
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def _pending_delete_entry(self):
        return {
            "action": "delete", "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_id": 51,
            "target": {"item_id": 51, "quantity_value": None, "quantity_unit": None,
                       "name": "сосисок", "canonical_name": "сосисок"},
        }

    # 7. Confirming delete removes only "сосисок — пару".
    def test_confirm_applies_delete_via_journal_recording_write(self):
        chat_id = 771030
        pending_cleanup_admin[chat_id] = self._pending_delete_entry()
        with patch.object(bot, "execute_inventory_delete", return_value=True) as mock_delete:
            _call_webhook(_make_update(771000030, chat_id, "✅ Так, застосувати"))
        mock_delete.assert_called_once_with(1, 10, 51, self._pending_delete_entry()["target"])
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("✅ Зміни застосовано." == t for t in self._sent_texts()))

    # 8. Delete uses stale protection.
    def test_confirm_aborts_on_stale_snapshot(self):
        chat_id = 771031
        pending_cleanup_admin[chat_id] = self._pending_delete_entry()
        with patch.object(bot, "execute_inventory_delete", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(771000031, chat_id, "✅ Так, застосувати"))
        self.assertIn(STALE_PREVIEW_MSG, self._sent_texts())
        self.assertNotIn(chat_id, pending_cleanup_admin)

    # 13. Cancel clears the pending delete.
    def test_cancel_clears_pending_delete(self):
        chat_id = 771032
        pending_cleanup_admin[chat_id] = self._pending_delete_entry()
        _call_webhook(_make_update(771000032, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


class TestBlockedByOtherActivePendingState(InventoryAdminWebhookTestCase):
    def test_rename_blocked_by_other_active_pending_state(self):
        chat_id = 771040
        bot.pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        try:
            _call_webhook(_make_update(771000040, chat_id, "перейменуй mlekо на молоко"))
            self.assertNotIn(chat_id, pending_cleanup_admin)
            self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))
        finally:
            bot.pending_global_household.pop(chat_id, None)


class TestActivePendingBlocksNewActionRoutes(InventoryAdminWebhookTestCase):
    """V1.4.1 live bug: with an active pending_cleanup_admin preview
    (e.g. "прибери Молоко 1 шт" awaiting confirm/cancel), a NEW destructive/
    action command must never start a competing route — the Destructive
    Bulk Household Request Guard used to fire regardless of an already-
    active preview, overwriting the user's unconfirmed pending state's
    "next reply" context with its own "покупки чи запаси?" question."""

    def _set_active_delete_preview(self, chat_id):
        pending_cleanup_admin[chat_id] = {
            "action": "delete", "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_id": 21, "target": {
                "item_id": 21, "quantity_value": Decimal("1"), "quantity_unit": "шт.",
                "name": "Молоко", "canonical_name": "молоко",
            },
        }

    # 1. "Видали все" must not start the destructive guard while a preview
    # is pending — no Gemini call, no pending_destructive_guard, the
    # original preview survives untouched.
    def test_destructive_guard_blocked_by_active_preview(self):
        chat_id = 771200
        self._set_active_delete_preview(chat_id)
        with patch.object(bot, "call_gemini") as mock_gemini:
            _call_webhook(_make_update(771200001, chat_id, "Видали все"))
        mock_gemini.assert_not_called()
        self.assertEqual(self._sent_texts(), [GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG])
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["action"], "delete")
        self.assertNotIn(chat_id, pending_destructive_guard)

    # 2. Since "Видали все" is now blocked and never opens pending_
    # destructive_guard, a later "покупки" is just ordinary text — it must
    # not be treated as a destructive-guard follow-up, and (per existing
    # project policy — general AI, not a deterministic shopping-list route,
    # already answers a bare "покупки") must not show the shopping list.
    def test_pokupky_after_blocked_guard_does_not_show_shopping_list(self):
        chat_id = 771201
        self._set_active_delete_preview(chat_id)
        _call_webhook(_make_update(771201001, chat_id, "Видали все"))
        self.mock_send.reset_mock()
        with patch.object(bot, "get_active_shopping_items") as mock_shopping_items:
            _call_webhook(_make_update(771201002, chat_id, "покупки"))
        mock_shopping_items.assert_not_called()
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["action"], "delete")

    # 3. "перейменуй ser на сир" must not start a new rename preview.
    def test_rename_blocked_by_active_preview(self):
        chat_id = 771202
        self._set_active_delete_preview(chat_id)
        _call_webhook(_make_update(771202001, chat_id, "перейменуй ser на сир"))
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))
        self.assertEqual(pending_cleanup_admin[chat_id]["action"], "delete")
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 21)

    # 4. "об'єднай молоко в запасах" must not start a cleanup-merge preview.
    def test_cleanup_merge_blocked_by_active_preview(self):
        chat_id = 771203
        self._set_active_delete_preview(chat_id)
        with patch.object(bot, "get_household_alias_map", return_value={}):
            _call_webhook(_make_update(771203001, chat_id, "об'єднай молоко в запасах"))
        self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))
        self.assertNotIn(chat_id, pending_merge)
        self.assertEqual(pending_cleanup_admin[chat_id]["action"], "delete")

    # 5. "❌ Скасувати" must still cancel the active preview normally.
    def test_cancel_still_cancels_active_preview(self):
        chat_id = 771204
        self._set_active_delete_preview(chat_id)
        _call_webhook(_make_update(771204001, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))

    # 6. "✅ Так, застосувати" must still confirm the active preview normally.
    def test_confirm_still_confirms_active_preview(self):
        chat_id = 771205
        self._set_active_delete_preview(chat_id)
        with patch.object(bot, "execute_inventory_delete", return_value=True) as mock_delete:
            _call_webhook(_make_update(771205001, chat_id, "✅ Так, застосувати"))
        mock_delete.assert_called_once()
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("✅ Зміни застосовано." == t for t in self._sent_texts()))

    # 7. "↩️ Скасувати останню дію" must cancel the CURRENT pending preview
    # first, never fall through to historical undo.
    def test_undo_button_cancels_active_preview_first(self):
        chat_id = 771206
        self._set_active_delete_preview(chat_id)
        _call_webhook(_make_update(771206001, chat_id, "↩️ Скасувати останню дію"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("Поточну дію скасовано." in t for t in self._sent_texts()))

    # 8. After the preview is cancelled, "Видали все" works normally again.
    def test_destructive_clarification_works_again_after_cancel(self):
        chat_id = 771207
        self._set_active_delete_preview(chat_id)
        _call_webhook(_make_update(771207001, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.mock_send.reset_mock()
        _call_webhook(_make_update(771207002, chat_id, "Видали все"))
        self.assertEqual(self._sent_texts(), [DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG])
        self.assertIn(chat_id, pending_destructive_guard)


class TestCleanupAdminDisambiguationFollowup(InventoryAdminWebhookTestCase):
    """V1.3: a rename/delete request matching 2+ rows stores a pending
    disambiguation context, and a short follow-up reply ("1 л", "№1", "1")
    continues that SAME command instead of ever falling through to general
    AI-chat."""

    def _trigger_ambiguous_rename(self, chat_id):
        with patch.object(bot, "get_inventory_items", return_value=_milk_multi_rows()):
            _call_webhook(_make_update(chat_id * 10 + 1, chat_id, "перейменуй молоко на Молоко3.2%"))
        self.mock_send.reset_mock()

    def _trigger_ambiguous_delete(self, chat_id):
        with patch.object(bot, "get_inventory_items", return_value=_milk_multi_rows()):
            _call_webhook(_make_update(chat_id * 10 + 1, chat_id, "видали молоко"))
        self.mock_send.reset_mock()

    # 5. "Mleko 1 шт"-style follow-up (quantity fragment alone is enough
    # here since both candidates are literally named "Молоко").
    def test_quantity_followup_selects_candidate_and_shows_rename_preview(self):
        chat_id = 771060
        self._trigger_ambiguous_rename(chat_id)
        _call_webhook(_make_update(chat_id * 10 + 2, chat_id, "1 л"))
        self.assertNotIn(chat_id, pending_cleanup_admin_disambiguation)
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["action"], "rename")
        self.assertEqual(entry["item_id"], 10)
        self.assertEqual(entry["new_name"], "Молоко3.2%")
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 1 л → Молоко3.2% — 1 л" in t for t in texts))

    # 6. A follow-up quantity that matches nothing keeps it ambiguous.
    def test_nonmatching_followup_asks_again_not_general_ai(self):
        chat_id = 771061
        self._trigger_ambiguous_rename(chat_id)
        _call_webhook(_make_update(chat_id * 10 + 2, chat_id, "5 л"))
        self.assertIn(chat_id, pending_cleanup_admin_disambiguation)
        self.assertNotIn(chat_id, pending_cleanup_admin)
        texts = self._sent_texts()
        self.assertTrue(any("не хочу вгадувати" in t for t in texts))

    # 7. "№1"/"2" numbered selection (candidates numbered in the SAME order
    # as format_inventory_admin_ambiguous_message shows them).
    def test_numbered_followup_selects_by_position(self):
        chat_id = 771062
        self._trigger_ambiguous_rename(chat_id)
        _call_webhook(_make_update(chat_id * 10 + 2, chat_id, "№2"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 11)

    def test_bare_numbered_followup_selects_by_position(self):
        chat_id = 771063
        self._trigger_ambiguous_rename(chat_id)
        _call_webhook(_make_update(chat_id * 10 + 2, chat_id, "1"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 10)

    # Delete-side follow-up works the same way.
    def test_quantity_followup_selects_candidate_and_shows_delete_preview(self):
        chat_id = 771064
        self._trigger_ambiguous_delete(chat_id)
        _call_webhook(_make_update(chat_id * 10 + 2, chat_id, "2 л"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["action"], "delete")
        self.assertEqual(entry["item_id"], 11)

    # 9. Cancel clears the pending disambiguation.
    def test_cancel_clears_pending_disambiguation(self):
        chat_id = 771065
        self._trigger_ambiguous_rename(chat_id)
        _call_webhook(_make_update(chat_id * 10 + 2, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_cleanup_admin_disambiguation)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))

    # Undo-button-cancels-active-operation v1 must reach this state too.
    def test_undo_button_cancels_pending_disambiguation(self):
        chat_id = 771066
        self._trigger_ambiguous_rename(chat_id)
        _call_webhook(_make_update(chat_id * 10 + 2, chat_id, "↩️ Скасувати останню дію"))
        self.assertNotIn(chat_id, pending_cleanup_admin_disambiguation)
        self.assertTrue(any("Поточну дію скасовано." in t for t in self._sent_texts()))


# =========================
# Undo integration — execute_inventory_rename/execute_inventory_delete
# (REAL database.py) record an Action History journal row, and
# apply_undo_action (the SAME generic restore every other global_household
# action already uses) can undo them.
# =========================
class FakeCursor:
    """Same minimal fake as tests/test_inventory_cleanup_merge.py's own —
    queued fetchall() results consumed in call order, every execute()
    recorded verbatim."""

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


def _milk_target():
    return {"item_id": 1, "quantity_value": Decimal("1"), "quantity_unit": "шт.",
            "name": "mlekо", "canonical_name": "молоко"}


class TestExecuteInventoryRenameRecordsJournal(unittest.TestCase):
    # 2/4 (DB side). A same-canonical-name rename is a single-bucket write
    # (canonical_name unchanged: "mlekо"'s stored canonical_name is already
    # "молоко" — only the display name was dirty).
    def test_inserts_global_household_journal_row(self):
        verify_rows = [(1, Decimal("1"), "шт.", "mlekо", "молоко")]
        before_bucket_rows = [(1, "mlekо", "молоко", "1 шт.", Decimal("1"), "шт.", False, "Молочне та яйця")]
        after_bucket_rows = [(1, "Молоко", "молоко", "1 шт.", Decimal("1"), "шт.", False, "Молочне та яйця")]
        cursor = FakeCursor(fetchall_results=[verify_rows, before_bucket_rows, after_bucket_rows])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.execute_inventory_rename(1, 10, 1, "Молоко", "молоко", _milk_target())

        self.assertTrue(result)
        self.assertTrue(conn.committed)
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items SET name=" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn("Молоко", update_queries[0][1])
        insert_queries = [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]
        self.assertEqual(len(insert_queries), 1)
        sql, params = insert_queries[0]
        self.assertIn("'global_household'", sql)
        self.assertEqual((params[0], params[1]), (1, 10))
        before_snapshot = params[3].obj
        post_action_snapshot = params[4].obj
        self.assertEqual(before_snapshot["inventory_buckets"]["молоко"][0]["name"], "mlekо")
        self.assertEqual(post_action_snapshot["inventory_buckets"]["молоко"][0]["name"], "Молоко")

    # 3 (DB side). A concurrently-changed row aborts with no write.
    def test_stale_target_raises_before_any_write(self):
        # DB now shows a DIFFERENT name than the snapshot the preview was
        # built from — _verify_targets_in_tx must reject this before any
        # UPDATE/INSERT happens.
        verify_rows = [(1, Decimal("1"), "шт.", "щось інше", "молоко")]
        cursor = FakeCursor(fetchall_results=[verify_rows])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_inventory_rename(1, 10, 1, "Молоко", "молоко", _milk_target())
        self.assertFalse(conn.committed)
        self.assertFalse(any("UPDATE inventory_items SET name=" in sql for sql, _ in cursor.queries))


class TestApplyUndoActionRestoresRename(unittest.TestCase):
    def test_undo_restores_old_name(self):
        before_row = {"id": 1, "household_id": 1, "name": "mlekо", "canonical_name": "молоко",
                      "quantity_text": "1 шт.", "quantity_value": "1", "quantity_unit": "шт.",
                      "quantity_inferred": False, "category": "Молочне та яйця"}
        post_row = {"id": 1, "household_id": 1, "name": "Молоко", "canonical_name": "молоко",
                    "quantity_text": "1 шт.", "quantity_value": "1", "quantity_unit": "шт.",
                    "quantity_inferred": False, "category": "Молочне та яйця"}
        before_snapshot = {"inventory_buckets": {"молоко": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_action_snapshot = {"inventory_buckets": {"молоко": [post_row]}, "shopping_buckets": {}, "expense_adds": []}
        journal_row = (1, 10, "active", before_snapshot, post_action_snapshot)
        current_bucket_rows = [(1, "Молоко", "молоко", "1 шт.", Decimal("1"), "шт.", False, "Молочне та яйця")]
        cursor = FakeCursor(fetchall_results=[current_bucket_rows])
        cursor.fetchone = lambda: journal_row
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items SET" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn("mlekо", update_queries[0][1])
        self.assertTrue(any("status='undone'" in sql for sql, _ in cursor.queries))

    # Undo-preview TEXT rendering for a rename (action_history.py's diff_
    # bucket/_format_bucket_line old_name extension).
    def test_undo_preview_shows_old_and_new_name(self):
        before_row = {"id": 1, "household_id": 1, "name": "mlekо", "canonical_name": "молоко",
                      "quantity_text": "1 шт.", "quantity_value": "1", "quantity_unit": "шт.",
                      "quantity_inferred": False, "category": "Молочне та яйця"}
        post_row = {"id": 1, "household_id": 1, "name": "Молоко", "canonical_name": "молоко",
                    "quantity_text": "1 шт.", "quantity_value": "1", "quantity_unit": "шт.",
                    "quantity_inferred": False, "category": "Молочне та яйця"}
        before_snapshot = {"inventory_buckets": {"молоко": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_action_snapshot = {"inventory_buckets": {"молоко": [post_row]}, "shopping_buckets": {}, "expense_adds": []}
        summary = action_history.build_operation_summary(before_snapshot, post_action_snapshot)
        preview = action_history.format_undo_preview(summary)
        self.assertIn("Молоко → mlekо", preview)

    # A PLAIN quantity update (no name change) keeps its existing rendering
    # exactly as before the rename "old_name" extension.
    def test_plain_quantity_update_rendering_unaffected(self):
        before_row = {"id": 3, "household_id": 1, "name": "Молоко", "canonical_name": "молоко",
                      "quantity_text": "7 л", "quantity_value": "7", "quantity_unit": "л",
                      "quantity_inferred": False, "category": "Молочне та яйця"}
        post_row = {"id": 3, "household_id": 1, "name": "Молоко", "canonical_name": "молоко",
                    "quantity_text": "8 л", "quantity_value": "8", "quantity_unit": "л",
                    "quantity_inferred": False, "category": "Молочне та яйця"}
        before_snapshot = {"inventory_buckets": {"молоко": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_action_snapshot = {"inventory_buckets": {"молоко": [post_row]}, "shopping_buckets": {}, "expense_adds": []}
        summary = action_history.build_operation_summary(before_snapshot, post_action_snapshot)
        preview = action_history.format_undo_preview(summary)
        self.assertIn("Молоко — 8 л → 7 л", preview)


def _sausage_target():
    return {"item_id": 51, "quantity_value": None, "quantity_unit": None,
            "name": "сосисок", "canonical_name": "сосисок"}


class TestExecuteInventoryDeleteRecordsJournal(unittest.TestCase):
    # 7/9 (DB side).
    def test_inserts_global_household_journal_row(self):
        verify_rows = [(51, None, None, "сосисок", "сосисок")]
        before_bucket_rows = [(51, "сосисок", "сосисок", "пару", None, None, False, "М'ясо та риба")]
        after_bucket_rows = []
        cursor = FakeCursor(fetchall_results=[verify_rows, before_bucket_rows, after_bucket_rows])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.execute_inventory_delete(1, 10, 51, _sausage_target())

        self.assertTrue(result)
        self.assertTrue(conn.committed)
        delete_queries = [q for q in cursor.queries if "DELETE FROM inventory_items" in q[0]]
        self.assertEqual(len(delete_queries), 1)
        self.assertIn(51, delete_queries[0][1])
        insert_queries = [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]
        self.assertEqual(len(insert_queries), 1)
        sql, params = insert_queries[0]
        self.assertIn("'global_household'", sql)
        before_snapshot = params[3].obj
        post_action_snapshot = params[4].obj
        self.assertEqual(len(before_snapshot["inventory_buckets"]["сосисок"]), 1)
        self.assertEqual(len(post_action_snapshot["inventory_buckets"]["сосисок"]), 0)

    # 8 (DB side).
    def test_stale_target_raises_before_any_write(self):
        verify_rows = [(51, None, None, "щось інше", "сосисок")]
        cursor = FakeCursor(fetchall_results=[verify_rows])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_inventory_delete(1, 10, 51, _sausage_target())
        self.assertFalse(conn.committed)
        self.assertFalse(any("DELETE FROM inventory_items" in sql for sql, _ in cursor.queries))


class TestApplyUndoActionRestoresDelete(unittest.TestCase):
    # 9 (undo restores the deleted row).
    def test_undo_reinserts_deleted_row(self):
        before_row = {"id": 51, "household_id": 1, "name": "сосисок", "canonical_name": "сосисок",
                      "quantity_text": "пару", "quantity_value": None, "quantity_unit": None,
                      "quantity_inferred": False, "category": "М'ясо та риба"}
        before_snapshot = {"inventory_buckets": {"сосисок": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_action_snapshot = {"inventory_buckets": {"сосисок": []}, "shopping_buckets": {}, "expense_adds": []}
        journal_row = (1, 10, "active", before_snapshot, post_action_snapshot)
        current_bucket_rows = []  # row is gone — matches the post_action snapshot
        cursor = FakeCursor(fetchall_results=[current_bucket_rows])
        cursor.fetchone = lambda: journal_row
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        insert_queries = [q for q in cursor.queries if "INSERT INTO inventory_items" in q[0]]
        self.assertEqual(len(insert_queries), 1)
        self.assertIn("сосисок", insert_queries[0][1])
        self.assertTrue(any("status='undone'" in sql for sql, _ in cursor.queries))


# =========================
# 14. Spot-check a few unrelated routes/flows this feature must never affect.
# =========================
class TestExistingRoutesStillWork(InventoryAdminWebhookTestCase):
    def test_household_read_question_still_works(self):
        chat_id = 771050
        with patch.object(bot, "get_active_shopping_items", return_value=[]):
            _call_webhook(_make_update(771000050, chat_id, "Що треба купити?"))
        self.assertTrue(self._sent_texts())
        self.assertNotIn(chat_id, pending_cleanup_admin)

    # V1.4 regression check: none of the new no-op-rename/numeric-quantity/
    # destructive-guard-followup changes touch general AI-chat routing.
    def test_general_ai_still_answers_unrelated_questions(self):
        chat_id = 771055
        with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн реагує на кислоту.") as mock_gemini:
            _call_webhook(_make_update(771000055, chat_id, "Поясни коротко, чому молоко згортається в каві?"))
        mock_gemini.assert_called_once()
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertNotIn(chat_id, pending_cleanup_admin)

    def test_meal_ideas_question_still_works(self):
        chat_id = 771051
        with patch.object(bot.meal_ideas, "try_handle_meal_ideas", return_value=True) as mock_meal:
            _call_webhook(_make_update(771000051, chat_id, "Що можна приготувати?"))
        mock_meal.assert_called_once()

    def test_inventory_cleanup_merge_still_works(self):
        chat_id = 771052
        milk_rows = [
            {"id": 2, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": Decimal("500"), "quantity_unit": "мл", "quantity_text": "500 мл"},
            {"id": 3, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": Decimal("9"), "quantity_unit": "л", "quantity_text": "9 л"},
        ]
        with patch.object(bot, "get_household_alias_map", return_value={}):
            with patch.object(bot, "get_inventory_items", return_value=milk_rows):
                _call_webhook(_make_update(771000052, chat_id, "об'єднай молоко в запасах"))
        self.assertIn(chat_id, pending_merge)
        self.assertNotIn(chat_id, pending_cleanup_admin)

    def test_household_action_lines_with_headers_still_work(self):
        chat_id = 771053
        with patch.object(bot, "get_household_alias_map", return_value={}):
            with patch("household_router._ask_gemini_explicit_add_items") as mock_items:
                mock_items.return_value = {
                    "items": [{"name": "Тестовий чай", "quantity_text": "1 шт.", "category": "Напої"}],
                    "unresolved_fragments": [],
                }
                _call_webhook(_make_update(771000053, chat_id, "🛒 Покупки\nДодати Тестовий чай — 1 шт."))
        self.assertIn(chat_id, bot.pending_global_household)
        bot.pending_global_household.pop(chat_id, None)

    def test_undo_button_during_quantity_clarification_still_cancels_it(self):
        chat_id = 771054
        bot.pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "new_expense": None, "delete_expense": None,
        }
        try:
            _call_webhook(_make_update(771000054, chat_id, "↩️ Скасувати останню дію"))
            self.assertNotIn(chat_id, bot.pending_inventory_quantity_clarification)
            self.assertTrue(any("Поточну дію скасовано." in t for t in self._sent_texts()))
        finally:
            bot.pending_inventory_quantity_clarification.pop(chat_id, None)


class TestDestructiveBulkHouseholdGuard(InventoryAdminWebhookTestCase):
    """V1.3: a bare destructive bulk-clear imperative ("Видали все", "Очисти
    запаси", ...) must never reach general AI-chat (Gemini) — no DB write,
    no confusing "I don't have DB access" answer, just a controlled
    Ukrainian clarification."""

    def _assert_guarded(self, text, chat_id):
        with patch.object(bot, "call_gemini") as mock_gemini:
            _call_webhook(_make_update(chat_id * 100 + 1, chat_id, text))
        mock_gemini.assert_not_called()
        self.assertEqual(self._sent_texts(), [DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG])
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertNotIn(chat_id, pending_cleanup_admin_disambiguation)
        # V1.4: the guard now stores a pending context so a destination
        # follow-up ("покупки"/"запаси") is intercepted too.
        self.assertIn(chat_id, pending_destructive_guard)

    def test_vydaly_vse(self):
        self._assert_guarded("Видали все", 771070)

    def test_vydaly_vsi(self):
        self._assert_guarded("видали всі", 771071)

    def test_prybery_vse(self):
        self._assert_guarded("прибери все", 771072)

    def test_ochysty_vse(self):
        self._assert_guarded("очисти все", 771073)

    def test_sterty_vse(self):
        self._assert_guarded("стерти все", 771074)

    def test_vydalyty_vse(self):
        self._assert_guarded("видалити все", 771075)

    def test_ochystyty_zapasy(self):
        self._assert_guarded("очистити запаси", 771076)

    def test_ochystyty_pokupky(self):
        self._assert_guarded("очистити покупки", 771077)

    def test_vydaly_vsi_zapasy(self):
        self._assert_guarded("видали всі запаси", 771078)

    def test_vydaly_vsi_pokupky(self):
        self._assert_guarded("видали всі покупки", 771079)

    # A genuine single-row delete must still be unaffected (goes through
    # inventory_admin_route long before this guard is ever reached).
    def test_specific_delete_request_is_not_guarded(self):
        chat_id = 771080
        with patch.object(bot, "get_inventory_items", return_value=[_milk_dirty_row()]):
            _call_webhook(_make_update(771080001, chat_id, "видали mlekо із запасів"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertFalse(any(t == DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG for t in self._sent_texts()))


class TestDestructiveGuardFollowup(InventoryAdminWebhookTestCase):
    """V1.4 live bug: after "Видали все" -> "покупки чи запаси?", replying
    "покупки" fell into the ordinary shopping read-list route instead of a
    controlled destructive-guard response."""

    def _trigger_guard(self, chat_id, update_id):
        _call_webhook(_make_update(update_id, chat_id, "Видали все"))
        self.assertIn(chat_id, pending_destructive_guard)
        self.mock_send.reset_mock()

    def test_pokupky_followup_does_not_show_shopping_list(self):
        chat_id = 771100
        self._trigger_guard(chat_id, 771100001)
        with patch.object(bot, "get_active_shopping_items") as mock_shopping_items:
            _call_webhook(_make_update(771100002, chat_id, "покупки"))
        mock_shopping_items.assert_not_called()
        self.assertNotIn(chat_id, pending_destructive_guard)
        texts = self._sent_texts()
        self.assertEqual(texts, [DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG])

    def test_zapasy_followup_does_not_show_inventory_list(self):
        chat_id = 771101
        self._trigger_guard(chat_id, 771101001)
        with patch.object(bot, "get_inventory_items") as mock_inventory_items:
            _call_webhook(_make_update(771101002, chat_id, "запаси"))
        mock_inventory_items.assert_not_called()
        self.assertNotIn(chat_id, pending_destructive_guard)
        texts = self._sent_texts()
        self.assertEqual(texts, [DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG])

    def test_spysok_pokupok_followup_recognized(self):
        chat_id = 771102
        self._trigger_guard(chat_id, 771102001)
        _call_webhook(_make_update(771102002, chat_id, "список покупок"))
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertEqual(self._sent_texts(), [DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG])

    def test_inventar_followup_recognized(self):
        chat_id = 771103
        self._trigger_guard(chat_id, 771103001)
        _call_webhook(_make_update(771103002, chat_id, "інвентар"))
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertEqual(self._sent_texts(), [DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG])

    def test_followup_never_writes_db(self):
        chat_id = 771104
        self._trigger_guard(chat_id, 771104001)
        with patch.object(bot, "delete_items_batch") as mock_delete_shopping, \
             patch.object(bot, "execute_inventory_delete") as mock_delete_inventory:
            _call_webhook(_make_update(771104002, chat_id, "покупки"))
        mock_delete_shopping.assert_not_called()
        mock_delete_inventory.assert_not_called()

    def test_cancel_button_clears_context(self):
        chat_id = 771105
        self._trigger_guard(chat_id, 771105001)
        _call_webhook(_make_update(771105002, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertEqual(self._sent_texts(), [DESTRUCTIVE_GUARD_CANCELLED_MSG])

    def test_undo_button_clears_context(self):
        chat_id = 771106
        self._trigger_guard(chat_id, 771106001)
        _call_webhook(_make_update(771106002, chat_id, "↩️ Скасувати останню дію"))
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertEqual(self._sent_texts(), [DESTRUCTIVE_GUARD_CANCELLED_MSG])

    # After the context clears (unrecognized follow-up), ordinary routing
    # must still work normally for that SAME message.
    def test_unrelated_followup_clears_context_and_routes_normally(self):
        chat_id = 771107
        self._trigger_guard(chat_id, 771107001)
        with patch.object(bot, "get_active_shopping_items", return_value=[]):
            _call_webhook(_make_update(771107002, chat_id, "Що треба купити?"))
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertTrue(self._sent_texts())
        self.assertNotEqual(self._sent_texts(), [DESTRUCTIVE_BULK_NOT_IMPLEMENTED_MSG])

    # After the context clears, a fresh cleanup-admin request still works.
    def test_after_cleared_ordinary_admin_route_still_works(self):
        chat_id = 771108
        self._trigger_guard(chat_id, 771108001)
        _call_webhook(_make_update(771108002, chat_id, "якийсь інший текст"))
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.mock_send.reset_mock()
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_dirty_row()]):
            _call_webhook(_make_update(771108003, chat_id, "перейменуй ser на сир"))
        self.assertIn(chat_id, pending_cleanup_admin)


if __name__ == "__main__":
    unittest.main()
