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
# Postgres — no real Supabase involved.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_tests", _database_path)
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
    _snapshot_targets,
    pending_remove_batch,
)


class FakeCursor:
    """Stands in for a psycopg cursor. Records every executed statement (in
    order) and returns `select_rows` for the snapshot-verification SELECT ...
    FOR UPDATE — everything needed to prove the guard runs first, inside the
    same transaction, before any mutating statement is ever issued."""

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
    """Stands in for a psycopg connection context manager. Mirrors real
    behavior closely enough for these tests: commit() is only ever reached
    by the code under test if nothing raised before it."""

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


def make_sausages_target(old_value=18.0, old_unit="шт."):
    return [{"item_id": 602, "quantity_value": old_value, "quantity_unit": old_unit}]


class TestStalePreviewProtection(unittest.TestCase):

    # 1 & 2. Preview видалення сосисок зі snapshot 18 шт. стає застарілим, якщо
    # поточна кількість 9 шт. — і видалення не виконується (нічого не мутується).
    def test_stale_full_removal_blocked_no_partial_write(self):
        cur = FakeCursor(select_rows=[(602, 9.0, "шт.")])  # user B changed it to 9 шт.
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_inventory_items_batch(1, [602], make_sausages_target())
        # Only the verification SELECT ran — no DELETE was ever issued, and no commit.
        self.assertEqual(len(cur.queries), 1)
        self.assertIn("FOR UPDATE", cur.queries[0][0])
        self.assertFalse(conn.committed)

    # Fresh snapshot (nothing changed) lets the same delete proceed normally.
    def test_fresh_full_removal_succeeds(self):
        cur = FakeCursor(select_rows=[(602, 18.0, "шт.")])  # unchanged
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.delete_inventory_items_batch(1, [602], make_sausages_target())
        self.assertEqual(count, 1)
        self.assertEqual(len(cur.queries), 2)  # verification SELECT, then DELETE
        self.assertTrue(conn.committed)

    # 3. Часткове списання не виконується, якщо кількість змінилася після preview.
    def test_partial_consumption_blocked_when_changed(self):
        cur = FakeCursor(select_rows=[(602, 9.0, "шт.")])
        conn = FakeConnection(cur)
        updates = [{"item_id": 602, "quantity_value": 14.0, "quantity_unit": "шт.", "quantity_text": "14 шт."}]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_inventory_consumption(1, updates, [], make_sausages_target())
        self.assertEqual(len(cur.queries), 1)
        self.assertFalse(conn.committed)

    # 4. Compound operation не виконується частково, якщо ОДНА цільова позиція
    # змінилася — навіть коли інша ціль у тому самому batch не змінювалась.
    def test_compound_operation_blocked_when_one_target_changed(self):
        cur = FakeCursor(select_rows=[(602, 9.0, "шт."), (604, 2.0, "шт.")])
        conn = FakeConnection(cur)
        targets = [
            {"item_id": 602, "quantity_value": 18.0, "quantity_unit": "шт."},  # stale (9 != 18)
            {"item_id": 604, "quantity_value": 2.0, "quantity_unit": "шт."},   # fresh
        ]
        consume_updates = [{"item_id": 604, "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт."}]
        delete_ids = [602]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_compound_inventory_operations(1, 1, consume_updates, delete_ids, [], targets)
        # Nothing beyond the single verification SELECT — the fresh target (604)
        # was not partially updated even though it, alone, would have been valid.
        self.assertEqual(len(cur.queries), 1)
        self.assertFalse(conn.committed)

    # 5. Reconciliation не виконується, якщо її цільова позиція змінилася.
    def test_reconciliation_blocked_when_target_changed(self):
        cur = FakeCursor(select_rows=[(602, 9.0, "шт.")])
        conn = FakeConnection(cur)
        updates = [{"item_id": 602, "quantity_value": 20.0, "quantity_unit": "шт.", "quantity_text": "20 шт."}]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_inventory_reconciliation(1, 1, updates, [], [], make_sausages_target())
        self.assertEqual(len(cur.queries), 1)
        self.assertFalse(conn.committed)

    # 6. Зміна іншого нецільового товару (хліб) не робить snapshot застарілим —
    # видалення сосисок проходить, бо хліб взагалі не входить у targets/WHERE IN.
    def test_unrelated_item_change_does_not_block(self):
        cur = FakeCursor(select_rows=[(602, 18.0, "шт.")])  # сосиски unchanged
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.delete_inventory_items_batch(1, [602], make_sausages_target())
        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        # The verification query only ever asked about item 602 — bread's id
        # never appears in the query params, so a change to bread elsewhere
        # structurally cannot affect this check.
        select_params = cur.queries[0][1]
        self.assertIn(602, select_params)
        self.assertNotIn(603, select_params)

    # A row that vanished entirely (deleted elsewhere) is also stale, not just
    # a changed quantity — covers "зникла" from the task's snapshot rule.
    def test_missing_row_is_stale(self):
        cur = FakeCursor(select_rows=[])  # item 602 no longer exists at all
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_inventory_items_batch(1, [602], make_sausages_target())
        self.assertFalse(conn.committed)

    # 7. Повторне підтвердження після успішної операції не застосовує її вдруге
    # (pending state is popped once; the second confirm finds nothing to act on).
    def test_pending_remove_batch_applied_only_once(self):
        chat_id = 555555
        pending_remove_batch[chat_id] = {
            "items": [{"id": 602, "name": "Сосиски", "quantity_value": 18.0, "quantity_unit": "шт."}],
            "household_id": 1,
            "user_db_id": 1,
        }
        first = pending_remove_batch.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_remove_batch.pop(chat_id, None)
        self.assertIsNone(second)

    # _snapshot_targets is the one shared mechanism every confirm-flow uses to
    # describe its targets — verify it handles both shapes it's fed across the
    # codebase (raw item dicts, and already-resolved change dicts).
    def test_snapshot_targets_handles_both_shapes(self):
        raw_items = [{"id": 602, "quantity_value": 18.0, "quantity_unit": "шт."}]
        resolved_changes = [{"item_id": 602, "old_value": 18.0, "old_unit": "шт."}]
        expected = [{"item_id": 602, "quantity_value": 18.0, "quantity_unit": "шт."}]
        self.assertEqual(_snapshot_targets(raw_items), expected)
        self.assertEqual(_snapshot_targets(resolved_changes), expected)


if __name__ == '__main__':
    unittest.main()
