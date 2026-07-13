"""Inventory Delete By Name/Number v1 — fixes two live bugs:

1. "Видали сир из запасов" (mixed Ukrainian verb + Russian "из запасов"
   location phrase) failed to match "Сир — 1 шт." at all, because
   inventory._ADMIN_LOCATION_SUFFIX_RE only recognized the Ukrainian
   prepositions ("із"/"з"/"в"/"у"), never the Russian "из" — so the whole
   "сир из запасов" (location phrase included) was treated as the product
   name and matched nothing.

2. After that failed match, a bare "9" (row 9 of the just-viewed "🧊
   Запаси" listing) fell through every route to the general AI fallback —
   there was no mechanism at all connecting a visible list number back to
   an inventory row outside the dedicated "removing" numbered-multi-select
   mode.

Covers: inventory.py's new parse_inventory_delete_by_number/parse_bare_
inventory_number_reference/resolve_inventory_number_reference (pure), the
_ADMIN_LOCATION_SUFFIX_RE mixed-language fix, and the webhook-level routes
in bot.py (_route_inventory_admin / _start_inventory_delete_by_number /
_mark_inventory_list_shown / pending_inventory_number_context). No real
Gemini, Telegram, Render, or Supabase call happens anywhere in this file.
"""
import sys
import os
import importlib.util
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import inventory

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_inventory_delete_by_number_test", _database_path)
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
    pending_cleanup_admin,
    pending_cleanup_admin_disambiguation,
    pending_cleanup_notice,
    pending_inventory_number_context,
    INVENTORY_ADMIN_NOT_FOUND_MSG,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
)


# =========================
# Fixture: the exact live-bug inventory shape (household_read the report).
# 4 filler rows in "М'ясо та риба" (numbers 1-4), then "Молочне та яйця":
# 5. Вершки, 6. Масло — 1 шт., 7. Молоко — 1 шт., 8. Молоко — 14,5 л,
# 9. Сир — 1 шт. — matches the bug report's own numbering exactly.
# =========================
def _meat_filler_rows():
    return [
        {"id": 100 + i, "name": name, "canonical_name": name.lower(), "category": "М'ясо та риба",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."}
        for i, name in enumerate(["Курка", "Яловичина", "Риба", "Індичка"])
    ]


def _cheese_row():
    return {"id": 9, "name": "Сир", "canonical_name": "сир", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."}


def _live_bug_inventory():
    return _meat_filler_rows() + [
        {"id": 5, "name": "Вершки", "canonical_name": "вершки", "category": "Молочне та яйця",
         "quantity_value": None, "quantity_unit": None, "quantity_text": ""},
        {"id": 6, "name": "Масло", "canonical_name": "масло", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
        {"id": 7, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
        {"id": 8, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("14.5"), "quantity_unit": "л", "quantity_text": "14,5 л"},
        _cheese_row(),
    ]


def _milk_multi_rows():
    return [
        {"id": 30, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "л", "quantity_text": "1 л"},
        {"id": 31, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("2"), "quantity_unit": "л", "quantity_text": "2 л"},
    ]


# =========================
# Pure unit tests — inventory.py, no webhook.
# =========================
class TestAdminLocationSuffixMixedLanguage(unittest.TestCase):
    def test_ukrainian_iz_zapasiv_still_works(self):
        name, qty = inventory.parse_inventory_delete_request("видали сир із запасів")
        self.assertEqual(name, "сир")
        self.assertIsNone(qty)

    def test_russian_iz_zapasov_now_works(self):
        name, qty = inventory.parse_inventory_delete_request("видали сир из запасов")
        self.assertEqual(name, "сир")
        self.assertIsNone(qty)

    def test_ukrainian_z_zapasiv_short_form(self):
        name, qty = inventory.parse_inventory_delete_request("видали Сир")
        self.assertEqual(name, "Сир")
        self.assertIsNone(qty)


class TestParseInventoryDeleteByNumber(unittest.TestCase):
    def test_vydaly_number(self):
        self.assertEqual(inventory.parse_inventory_delete_by_number("видали 9"), 9)

    def test_vydaly_nomer_number(self):
        self.assertEqual(inventory.parse_inventory_delete_by_number("видали номер 9"), 9)

    def test_prybery_hash_number(self):
        self.assertEqual(inventory.parse_inventory_delete_by_number("прибери №9"), 9)

    def test_name_based_delete_is_not_matched(self):
        self.assertIsNone(inventory.parse_inventory_delete_by_number("видали сир"))

    def test_blank_text_returns_none(self):
        self.assertIsNone(inventory.parse_inventory_delete_by_number(""))


class TestParseBareInventoryNumberReference(unittest.TestCase):
    def test_bare_number(self):
        self.assertEqual(inventory.parse_bare_inventory_number_reference("9"), 9)

    def test_bare_number_with_hash(self):
        self.assertEqual(inventory.parse_bare_inventory_number_reference("№9"), 9)

    def test_word_is_not_a_number(self):
        self.assertIsNone(inventory.parse_bare_inventory_number_reference("сир"))

    def test_sentence_with_a_number_is_not_bare(self):
        self.assertIsNone(inventory.parse_bare_inventory_number_reference("видали 9"))


class TestResolveInventoryNumberReference(unittest.TestCase):
    def test_resolves_row_nine_to_cheese(self):
        items = _live_bug_inventory()
        row = inventory.resolve_inventory_number_reference(9, items, bot.CATEGORY_ORDER, bot.DEFAULT_CATEGORY)
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], 9)
        self.assertEqual(row["name"], "Сир")

    def test_unknown_number_returns_none(self):
        items = _live_bug_inventory()
        row = inventory.resolve_inventory_number_reference(999, items, bot.CATEGORY_ORDER, bot.DEFAULT_CATEGORY)
        self.assertIsNone(row)


# =========================
# Webhook-level integration tests.
# =========================
def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class InventoryDeleteWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_cleanup_admin.clear()
        pending_cleanup_admin_disambiguation.clear()
        pending_cleanup_notice.clear()
        pending_inventory_number_context.clear()
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
        pending_inventory_number_context.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# 1/2 — direct deletion by name, mixed Ukrainian/Russian wording.
class TestDeleteByNameMixedLanguage(InventoryDeleteWebhookTestCase):
    def test_ukrainian_iz_zapasiv_builds_preview(self):
        chat_id = 772001
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772001001, chat_id, "Видали сир із запасів"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["action"], "delete")
        self.assertEqual(entry["item_id"], 9)
        texts = self._sent_texts()
        self.assertTrue(any("Сир — 1 шт." in t for t in texts))

    def test_russian_iz_zapasov_builds_preview(self):
        chat_id = 772002
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772002001, chat_id, "Видали сир из запасов"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["item_id"], 9)
        texts = self._sent_texts()
        self.assertTrue(any("Сир — 1 шт." in t for t in texts))

    def test_short_form_vydaly_syr(self):
        chat_id = 772003
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772003001, chat_id, "видали Сир"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 9)

    def test_no_db_write_before_confirm(self):
        chat_id = 772004
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()), \
             patch.object(bot, "execute_inventory_delete") as mock_delete:
            _call_webhook(_make_update(772004001, chat_id, "Видали сир из запасов"))
        mock_delete.assert_not_called()


# 3 — deletion by visible number after inventory list.
class TestDeleteByVisibleNumber(InventoryDeleteWebhookTestCase):
    def test_vydaly_9_after_viewing_list(self):
        chat_id = 772010
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772010001, chat_id, "🧊 Запаси"))
            _call_webhook(_make_update(772010002, chat_id, "видали 9"))
        self.assertIn(chat_id, pending_cleanup_admin)
        entry = pending_cleanup_admin[chat_id]
        self.assertEqual(entry["item_id"], 9)
        texts = self._sent_texts()
        self.assertTrue(any("Сир — 1 шт." in t for t in texts))

    def test_vydaly_nomer_9(self):
        chat_id = 772011
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772011001, chat_id, "🧊 Запаси"))
            _call_webhook(_make_update(772011002, chat_id, "видали номер 9"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 9)

    def test_number_not_in_list_shows_controlled_error(self):
        chat_id = 772012
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772012001, chat_id, "видали 999"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertTrue(any("Номер 999" in t for t in self._sent_texts()))


# 4/5 — bare number continuation after a list view or a failed delete, and
# the negative case (no recent context at all).
class TestBareNumberContinuation(InventoryDeleteWebhookTestCase):
    def test_bare_9_after_viewing_inventory_list(self):
        chat_id = 772020
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772020001, chat_id, "🧊 Запаси"))
            _call_webhook(_make_update(772020002, chat_id, "9"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 9)

    def test_bare_9_after_failed_name_delete(self):
        chat_id = 772021
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772021001, chat_id, "Видали неіснуючий товар"))
            self.assertNotIn(chat_id, pending_cleanup_admin)
            self.assertTrue(any(INVENTORY_ADMIN_NOT_FOUND_MSG == t for t in self._sent_texts()))
            _call_webhook(_make_update(772021002, chat_id, "9"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 9)

    def test_bare_9_with_no_recent_context_does_not_delete(self):
        chat_id = 772022
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()), \
             patch.object(bot, "execute_inventory_delete") as mock_delete, \
             patch.object(bot, "call_gemini", return_value=None):
            _call_webhook(_make_update(772022001, chat_id, "9"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        mock_delete.assert_not_called()

    def test_expired_context_does_not_delete(self):
        chat_id = 772023
        pending_inventory_number_context[chat_id] = {
            "household_id": 1, "ts": datetime.now() - bot.INVENTORY_NUMBER_CONTEXT_TTL - timedelta(minutes=1),
        }
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()), \
             patch.object(bot, "execute_inventory_delete") as mock_delete, \
             patch.object(bot, "call_gemini", return_value=None):
            _call_webhook(_make_update(772023001, chat_id, "9"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        mock_delete.assert_not_called()


# 6 — multiple matching rows ask for clarification, never a silent delete.
class TestMultipleMatchesAskForClarification(InventoryDeleteWebhookTestCase):
    def test_vydaly_moloko_asks_which_one(self):
        chat_id = 772030
        with patch.object(bot, "get_inventory_items", return_value=_milk_multi_rows()):
            _call_webhook(_make_update(772030001, chat_id, "видали молоко"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        texts = self._sent_texts()
        self.assertTrue(any("не хочу вгадувати" in t for t in texts))
        self.assertTrue(any("Молоко — 1 л" in t for t in texts))
        self.assertTrue(any("Молоко — 2 л" in t for t in texts))


# 7/8/9 — confirm/cancel/undo after a number-based delete.
class TestConfirmCancelUndoAfterNumberDelete(InventoryDeleteWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def test_confirm_deletes_exactly_row_nine(self):
        chat_id = 772040
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772040001, chat_id, "видали 9"))
        with patch.object(bot, "execute_inventory_delete", return_value=True) as mock_delete:
            _call_webhook(_make_update(772040002, chat_id, "✅ Так, застосувати"))
        mock_delete.assert_called_once()
        args, _ = mock_delete.call_args
        self.assertEqual(args[2], 9)  # item_id
        self.assertNotIn(chat_id, pending_cleanup_admin)

    def test_cancel_deletes_nothing(self):
        chat_id = 772041
        with patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory()):
            _call_webhook(_make_update(772041001, chat_id, "видали 9"))
        with patch.object(bot, "execute_inventory_delete") as mock_delete:
            _call_webhook(_make_update(772041002, chat_id, "❌ Скасувати"))
        mock_delete.assert_not_called()
        self.assertNotIn(chat_id, pending_cleanup_admin)

    def test_undo_restores_row_via_real_journal(self):
        """Same journal/undo path name-based delete already uses (database.
        execute_inventory_delete -> household_action_journal -> apply_undo_
        action) — proves the number-based entry point writes an identical
        journal row and that apply_undo_action can restore it, using the
        REAL database.py against the same minimal queued-fetchall fake
        cursor tests/test_inventory_cleanup_admin.py's own DB-level tests
        use (FakeCursor/FakeConnection there)."""
        household_id, user_db_id, item_id = 1, 10, 9
        target = {"item_id": item_id, "quantity_value": Decimal("1"), "quantity_unit": "шт.",
                  "name": "Сир", "canonical_name": "сир"}

        class FakeCursor:
            def __init__(self, fetchall_results=None):
                self.queries = []
                self._fetchall_results = list(fetchall_results or [])
                self._fetchone_result = None

            def execute(self, sql, params=None):
                self.queries.append((sql, params))

            def fetchall(self):
                return self._fetchall_results.pop(0) if self._fetchall_results else []

            def fetchone(self):
                return self._fetchone_result

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

        verify_rows = [(item_id, Decimal("1"), "шт.", "Сир", "сир")]
        before_bucket_rows = [(item_id, "Сир", "сир", "1 шт.", Decimal("1"), "шт.", False, "Молочне та яйця")]
        after_bucket_rows = []
        cursor = FakeCursor(fetchall_results=[verify_rows, before_bucket_rows, after_bucket_rows])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.execute_inventory_delete(household_id, user_db_id, item_id, target)

        self.assertTrue(result)
        self.assertTrue(conn.committed)
        delete_queries = [q for q in cursor.queries if "DELETE FROM inventory_items" in q[0]]
        self.assertEqual(len(delete_queries), 1)
        self.assertIn(item_id, delete_queries[0][1])
        insert_queries = [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]
        self.assertEqual(len(insert_queries), 1)
        _, params = insert_queries[0]
        before_snapshot = params[3].obj
        post_action_snapshot = params[4].obj
        self.assertEqual(len(before_snapshot["inventory_buckets"]["сир"]), 1)
        self.assertEqual(len(post_action_snapshot["inventory_buckets"]["сир"]), 0)

        # Undo: same journal row restores the deleted "Сир" row via the
        # existing apply_undo_action machinery — never a parallel path.
        journal_row = (household_id, user_db_id, "active", before_snapshot, post_action_snapshot)
        undo_cursor = FakeCursor(fetchall_results=[[]])
        undo_cursor._fetchone_result = journal_row
        undo_conn = FakeConnection(undo_cursor)
        with patch.object(real_database, "get_connection", return_value=undo_conn):
            real_database.apply_undo_action(action_id=1, household_id=household_id, actor_user_id=user_db_id)
        self.assertTrue(undo_conn.committed)
        insert_back_queries = [q for q in undo_cursor.queries if "INSERT INTO inventory_items" in q[0]]
        self.assertEqual(len(insert_back_queries), 1)
        self.assertIn("Сир", insert_back_queries[0][1])


if __name__ == "__main__":
    unittest.main()
