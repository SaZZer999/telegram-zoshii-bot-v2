import sys
import os
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes, so a plain `import database` here
# is not reliable. This lets the test exercise the actual transactional
# snapshot guard directly, with a fake connection/cursor standing in for
# Postgres — no real Supabase involved. Same pattern as
# tests/test_stale_preview_protection.py and
# tests/test_alias_bulk_actions_and_return_context.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_merge_tests", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    _merge_snapshot_targets,
    pending_merge,
    STALE_PREVIEW_MSG,
    SHOPPING_KEYBOARD,
    INVENTORY_KEYBOARD,
)


class FakeCursor:
    """Stands in for a psycopg cursor. Records every executed statement (in
    order) and returns `select_rows` for the snapshot-verification SELECT ...
    FOR UPDATE — everything needed to prove the guard runs first, inside the
    same transaction, before any mutating statement is ever issued. Identical
    to tests/test_stale_preview_protection.py's FakeCursor — already general
    enough to handle a wider SELECT (more columns) with no changes."""

    def __init__(self, select_rows):
        self.select_rows = select_rows
        self.queries = []
        self.rowcount = 0
        self._last_result = None

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "SELECT" in sql and "FOR UPDATE" in sql:
            self._last_result = list(self.select_rows)
        else:
            self.rowcount = 1
            self._last_result = None

    def fetchall(self):
        return self._last_result or []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """Stands in for a psycopg connection context manager. commit() is only
    ever reached by the code under test if nothing raised before it."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _bread_group(main_id=901, dup_id=902):
    """One merge group: two "Хліб" rows (900g each) being combined into one."""
    return {
        "item_ids": [main_id, dup_id],
        "merged_name": "Хліб",
        "merged_quantity_text": "2 шт.",
        "merged_category": "Хліб і випічка",
        "canonical_name": "хліб",
        "merged_quantity_value": 2.0,
        "merged_quantity_unit": "шт.",
        "items": [
            {"id": main_id, "name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка",
             "canonical_name": "хліб", "quantity_value": 1.0, "quantity_unit": "шт."},
            {"id": dup_id, "name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка",
             "canonical_name": "хліб", "quantity_value": 1.0, "quantity_unit": "шт."},
        ],
    }


def _snapshot_rows_for(group, overrides=None):
    """Build FakeCursor select_rows (id, quantity_value, quantity_unit,
    canonical_name, category) matching a group's items exactly, unless
    `overrides` (item_id -> partial tuple dict) says otherwise."""
    overrides = overrides or {}
    rows = []
    for it in group["items"]:
        row = {
            "quantity_value": it["quantity_value"],
            "quantity_unit": it["quantity_unit"],
            "canonical_name": it["canonical_name"],
            "category": it["category"],
        }
        row.update(overrides.get(it["id"], {}))
        rows.append((it["id"], row["quantity_value"], row["quantity_unit"], row["canonical_name"], row["category"]))
    return rows


class TestMergeSnapshotTargetsHelper(unittest.TestCase):
    """Pure bot.py helper — no DB involved."""

    def test_builds_expected_dicts_across_groups(self):
        group_a = _bread_group(901, 902)
        group_b = {
            "item_ids": [903, 904],
            "merged_name": "Молоко",
            "merged_quantity_text": "3 л",
            "merged_category": "Молочне та яйця",
            "canonical_name": "молоко",
            "merged_quantity_value": 3.0,
            "merged_quantity_unit": "л",
            "items": [
                {"id": 903, "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця",
                 "canonical_name": "молоко", "quantity_value": 1.0, "quantity_unit": "л"},
                {"id": 904, "name": "Молоко", "quantity_text": "2 л", "category": "Молочне та яйця",
                 "canonical_name": "молоко", "quantity_value": 2.0, "quantity_unit": "л"},
            ],
        }
        targets = _merge_snapshot_targets([group_a, group_b])
        self.assertEqual(len(targets), 4)
        by_id = {t["item_id"]: t for t in targets}
        self.assertEqual(by_id[901], {
            "item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.",
            "canonical_name": "хліб", "category": "Хліб і випічка",
        })
        self.assertEqual(by_id[904], {
            "item_id": 904, "quantity_value": 2.0, "quantity_unit": "л",
            "canonical_name": "молоко", "category": "Молочне та яйця",
        })

    def test_falls_back_when_canonical_name_or_category_missing(self):
        group = {
            "items": [
                {"id": 5, "name": "Сіль", "quantity_value": None, "quantity_unit": None},
            ],
        }
        targets = _merge_snapshot_targets([group])
        self.assertEqual(targets, [{
            "item_id": 5, "quantity_value": None, "quantity_unit": None,
            "canonical_name": "сіль", "category": "Інше їстівне",
        }])


class TestExecuteMergeShoppingStaleness(unittest.TestCase):
    """execute_merge_shopping against the real database.py, fake cursor/connection."""

    # 1. Unchanged merge preview successfully merges.
    def test_unchanged_merge_succeeds(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=_snapshot_rows_for(group))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.execute_merge_shopping(1, [group], targets)
        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        # verify SELECT, then UPDATE (main_id), then DELETE (rest_ids)
        self.assertEqual(len(cur.queries), 3)
        self.assertIn("FOR UPDATE", cur.queries[0][0])
        self.assertIn("UPDATE shopping_items", cur.queries[1][0])
        self.assertIn("DELETE FROM shopping_items", cur.queries[2][0])

    # 2. Quantity change on one merge-position after preview blocks merge.
    def test_quantity_change_blocks_merge(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=_snapshot_rows_for(group, overrides={902: {"quantity_value": 5.0}}))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_shopping(1, [group], targets)
        self.assertEqual(len(cur.queries), 1)
        self.assertFalse(conn.committed)

    # 3. Unit change on a merge-position blocks merge.
    def test_unit_change_blocks_merge(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=_snapshot_rows_for(group, overrides={902: {"quantity_unit": "кг"}}))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_shopping(1, [group], targets)
        self.assertEqual(len(cur.queries), 1)
        self.assertFalse(conn.committed)

    # 4a. canonical_name change on a merge-position blocks merge.
    def test_canonical_name_change_blocks_merge(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=_snapshot_rows_for(group, overrides={902: {"canonical_name": "багет"}}))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_shopping(1, [group], targets)
        self.assertEqual(len(cur.queries), 1)
        self.assertFalse(conn.committed)

    # 4b. Category change on a merge-position blocks merge.
    def test_category_change_blocks_merge(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=_snapshot_rows_for(group, overrides={902: {"category": "Інше їстівне"}}))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_shopping(1, [group], targets)
        self.assertEqual(len(cur.queries), 1)
        self.assertFalse(conn.committed)

    # 5. Change to a non-target item does not block merge — it's simply never
    # selected, since the verification SELECT only asks about targets' ids.
    def test_unrelated_item_change_does_not_block_merge(self):
        group = _bread_group()
        # select_rows only returns the two target rows — item 999 (unrelated,
        # changed elsewhere) never appears in targets, so it's structurally
        # impossible for it to affect the WHERE id IN (...) verification query.
        cur = FakeCursor(select_rows=_snapshot_rows_for(group))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.execute_merge_shopping(1, [group], targets)
        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        select_params = cur.queries[0][1]
        self.assertNotIn(999, select_params)

    # 6. On stale conflict, no position is changed or deleted — only the
    # verification SELECT ever runs; nothing partial.
    def test_stale_conflict_no_partial_write(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=_snapshot_rows_for(group, overrides={902: {"quantity_value": 9.0}}))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_shopping(1, [group], targets)
        self.assertEqual(len(cur.queries), 1)
        for sql, _ in cur.queries:
            self.assertNotIn("UPDATE shopping_items", sql)
            self.assertNotIn("DELETE FROM shopping_items", sql)
        self.assertFalse(conn.committed)

    # A row that vanished entirely (deleted elsewhere) is also stale.
    def test_missing_row_is_stale(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=_snapshot_rows_for(group)[:1])  # 902 gone
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_shopping(1, [group], targets)
        self.assertFalse(conn.committed)

    # No targets (e.g. legacy caller) — no-ops the guard, merge still runs.
    def test_no_targets_skips_guard(self):
        group = _bread_group()
        cur = FakeCursor(select_rows=[])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.execute_merge_shopping(1, [group], None)
        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        # No verification SELECT at all — only UPDATE + DELETE.
        self.assertEqual(len(cur.queries), 2)


# 8. Shopping and inventory are checked separately — this class proves the
# inventory guard is independently wired (its own table name, its own function).
class TestExecuteMergeInventoryStaleness(unittest.TestCase):

    def test_unchanged_merge_succeeds(self):
        group = _bread_group(801, 802)
        cur = FakeCursor(select_rows=_snapshot_rows_for(group))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 801, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 802, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.execute_merge_inventory(1, [group], targets)
        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        self.assertIn("FOR UPDATE", cur.queries[0][0])
        self.assertIn("inventory_items", cur.queries[0][0])
        self.assertIn("UPDATE inventory_items", cur.queries[1][0])
        self.assertIn("DELETE FROM inventory_items", cur.queries[2][0])

    def test_stale_snapshot_blocks_inventory_merge_independently(self):
        group = _bread_group(801, 802)
        cur = FakeCursor(select_rows=_snapshot_rows_for(group, overrides={802: {"quantity_value": 7.0}}))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 801, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 802, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_inventory(1, [group], targets)
        self.assertEqual(len(cur.queries), 1)
        self.assertIn("inventory_items", cur.queries[0][0])
        self.assertFalse(conn.committed)

    def test_category_change_blocks_inventory_merge(self):
        group = _bread_group(801, 802)
        cur = FakeCursor(select_rows=_snapshot_rows_for(group, overrides={801: {"category": "Заморожене"}}))
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 801, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
            {"item_id": 802, "quantity_value": 1.0, "quantity_unit": "шт.", "canonical_name": "хліб", "category": "Хліб і випічка"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_merge_inventory(1, [group], targets)
        self.assertFalse(conn.committed)


class TestPendingMergeIdempotency(unittest.TestCase):

    # 7. Repeated confirm after a successful merge does not merge twice —
    # pending_merge is popped once; the second confirm finds nothing to act on.
    def test_pending_merge_applied_only_once(self):
        chat_id = 660001
        pending_merge[chat_id] = {
            "groups": [_bread_group()],
            "targets": [{"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.",
                         "canonical_name": "хліб", "category": "Хліб і випічка"}],
            "household_id": 1,
            "user_db_id": 1,
            "list_type": "shopping_saved",
        }
        first = pending_merge.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_merge.pop(chat_id, None)
        self.assertIsNone(second)


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


class TestWebhookStaleMergeMessage(unittest.TestCase):
    """Webhook-level dispatch, everything network-facing patched. Reassigns
    bot.StaleSnapshotError to the REAL exception class (bot.py's own import
    binds the name to whatever `database` was mocked to at import time, which
    is a bare MagicMock attribute here, not a real Exception subclass — this
    mirrors the exact caveat documented in
    tests/test_alias_bulk_actions_and_return_context.py, resolved here by
    monkeypatching the module-level name bot.py's `except StaleSnapshotError:`
    resolves at call time)."""

    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def setUp(self):
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        pending_merge.clear()

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def test_shopping_merge_conflict_shows_stale_message(self):
        chat_id = 660010
        pending_merge[chat_id] = {
            "groups": [_bread_group()],
            "targets": [{"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.",
                         "canonical_name": "хліб", "category": "Хліб і випічка"}],
            "household_id": 1, "user_db_id": 1, "list_type": "shopping_saved",
        }
        with patch.object(bot, "execute_merge_shopping", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(660000010, chat_id, "✅ Об'єднати"))
        self.assertIn(STALE_PREVIEW_MSG, self._sent_texts())
        self.assertIn(SHOPPING_KEYBOARD, self._reply_markups())
        self.assertNotIn(chat_id, pending_merge)

    def test_inventory_merge_conflict_shows_stale_message(self):
        chat_id = 660011
        pending_merge[chat_id] = {
            "groups": [_bread_group()],
            "targets": [{"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.",
                         "canonical_name": "хліб", "category": "Хліб і випічка"}],
            "household_id": 1, "user_db_id": 1, "list_type": "inventory_saved",
        }
        with patch.object(bot, "execute_merge_inventory", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(660000011, chat_id, "✅ Об'єднати"))
        self.assertIn(STALE_PREVIEW_MSG, self._sent_texts())
        self.assertIn(INVENTORY_KEYBOARD, self._reply_markups())

    def test_success_path_still_sends_merged_count(self):
        chat_id = 660012
        pending_merge[chat_id] = {
            "groups": [_bread_group()],
            "targets": [{"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.",
                         "canonical_name": "хліб", "category": "Хліб і випічка"}],
            "household_id": 1, "user_db_id": 1, "list_type": "shopping_saved",
        }
        with patch.object(bot, "execute_merge_shopping", return_value=1) as mock_merge:
            _call_webhook(_make_update(660000012, chat_id, "✅ Об'єднати"))
            mock_merge.assert_called_once()
        self.assertTrue(any("✅ Об'єднано груп: 1" == t for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
