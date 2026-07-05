import sys
import os
import importlib.util
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name — same pattern
# as tests/test_global_household_operations.py: exercises the actual
# apply_global_household_operations()/household_action_journal SQL shape
# directly, with a fake connection/cursor standing in for Postgres. No real
# Supabase/Gemini/Telegram call happens anywhere in this file.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_action_journal_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)


class FakeCursor:
    """Simple positional fake — same shape as test_global_household_operations.
    py's FakeCursor. Extra fetchall/fetchone calls beyond the provided lists
    harmlessly return []/None (matches "bucket is empty" for the new
    journal-snapshot queries this file doesn't care about the content of)."""
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.queries = []
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "DELETE" in sql:
            self.rowcount = len(params) - 1 if params else 0

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

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


def _new_item(name="Масло", category="Молочне та яйця"):
    return {
        "name": name, "category": category, "canonical_name": name.lower(),
        "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_inferred": True,
        "quantity_text": "1 шт.",
    }


def _journal_inserts(cursor):
    return [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]


class TestJournalWrittenOnSuccess(unittest.TestCase):
    """#1/#2: a successful Global Household Operation creates exactly one
    active journal record with the correct household/author."""

    def test_single_active_journal_record_with_correct_household_and_author(self):
        cursor = FakeCursor(fetchone_results=[(555,)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_global_household_operations(
                household_id=7, user_db_id=42,
                add_shopping_items=[_new_item("Булочка", "Хліб і випічка")],
            )
        self.assertTrue(conn.committed)
        inserts = _journal_inserts(cursor)
        self.assertEqual(len(inserts), 1)
        sql, params = inserts[0]
        self.assertIn("'active'", sql)
        self.assertIn("'global_household'", sql)
        # params: (household_id, actor_user_id, forward, inverse, before, post, summary)
        self.assertEqual(params[0], 7)
        self.assertEqual(params[1], 42)


class TestJournalNotWrittenOnFailure(unittest.TestCase):
    """#3: a stale-target error aborts the whole transaction — no data
    write and no journal record, since the exception fires before the
    `with get_connection()` block ever commits."""

    def test_stale_inventory_target_creates_no_journal_and_no_commit(self):
        cursor = FakeCursor(fetchall_results=[[(501, 10.0, "шт.")]])  # live 10 != snapshot 14
        conn = FakeConnection(cursor)
        targets = [{"item_id": 501, "quantity_value": 14.0, "quantity_unit": "шт."}]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_global_household_operations(
                    household_id=1, user_db_id=10,
                    add_shopping_items=[_new_item("Булочка", "Хліб і випічка")],
                    inventory_targets=targets,
                )
        self.assertFalse(conn.committed)
        self.assertEqual(_journal_inserts(cursor), [])
        self.assertFalse(any("INSERT INTO shopping_items" in sql for sql, _ in cursor.queries))

    def test_stale_expense_delete_creates_no_journal_and_no_commit(self):
        cursor = FakeCursor(fetchone_results=[None])  # row already gone
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_global_household_operations(
                    household_id=1, user_db_id=10,
                    delete_expense_id=999, delete_expense_snapshot=snapshot,
                )
        self.assertFalse(conn.committed)
        self.assertEqual(_journal_inserts(cursor), [])


class TestJournalNotWrittenForLegacyFlows(unittest.TestCase):
    """#4: legacy shopping/inventory/expense entry points never touch
    household_action_journal — only apply_global_household_operations does."""

    def test_add_shopping_items_batch_writes_no_journal(self):
        cursor = FakeCursor(fetchall_results=[[]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.add_shopping_items_batch(1, 10, [_new_item("Хліб", "Хліб і випічка")])
        self.assertEqual(_journal_inserts(cursor), [])

    def test_add_expense_writes_no_journal(self):
        cursor = FakeCursor(fetchone_results=[(1,)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.add_expense(1, 10, Decimal("5.00"), "PLN", "Продукти", "Хліб", date(2026, 7, 5))
        self.assertEqual(_journal_inserts(cursor), [])

    def test_delete_expense_writes_no_journal(self):
        snapshot = {"amount": Decimal("5.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 5), "description": "Хліб"}
        cursor = FakeCursor(fetchone_results=[(Decimal("5.00"), "Продукти", date(2026, 7, 5), "Хліб")])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.delete_expense(1, 99, snapshot)
        self.assertEqual(_journal_inserts(cursor), [])


class TestGetLatestUndoableAction(unittest.TestCase):
    """#5/#6: get_latest_undoable_action returns only the latest ACTIVE
    global_household action for THIS actor in THIS household — never
    another user's or another household's action."""

    def test_returns_action_id_and_summary(self):
        cursor = FakeCursor(fetchone_results=[(123, {"inventory": [], "shopping": [],
                                                       "expense_added": None, "expense_deleted": None})])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_latest_undoable_action(household_id=7, actor_user_id=42)
        self.assertEqual(result["id"], 123)
        sql, params = cursor.queries[0]
        self.assertIn("status='active'", sql)
        self.assertIn("operation_type='global_household'", sql)
        self.assertIn("ORDER BY created_at DESC, id DESC", sql)
        self.assertEqual(params, (7, 42))

    def test_returns_none_when_no_active_action(self):
        cursor = FakeCursor(fetchone_results=[None])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_latest_undoable_action(household_id=7, actor_user_id=42)
        self.assertIsNone(result)

    def test_query_scopes_by_both_household_and_actor(self):
        """Guards against ever fetching another household's or another
        user's last action — the WHERE clause must filter on both, not
        just one."""
        cursor = FakeCursor(fetchone_results=[None])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.get_latest_undoable_action(household_id=7, actor_user_id=42)
        sql, params = cursor.queries[0]
        self.assertIn("household_id=%s", sql)
        self.assertIn("actor_user_id=%s", sql)
        self.assertEqual(params, (7, 42))


if __name__ == "__main__":
    unittest.main()
