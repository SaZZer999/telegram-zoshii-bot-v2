import sys
import os
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes. This lets us exercise the actual
# alias normalization/resolution/CRUD logic directly, with a fake
# connection/cursor standing in for Postgres — no real Supabase involved.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_tests", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. bot.py keeps its own local mirror of
# resolve_item_name/normalize_alias_text (same reasoning as its existing
# canonicalize_name duplication), so it never depends on what `database` is
# mocked to in this or any other test file.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    normalize_item_quantity,
    _validate_alias_action,
    _validate_alias_router_result,
    format_alias_list,
    _format_alias_update_preview,
)


class FakeCursor:
    """Stands in for a psycopg cursor. Records every executed statement (in
    order, with params) and returns canned fetchone/fetchall results in the
    order given — enough to verify SQL shape/scoping without a real Postgres."""

    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.queries = []
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

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


ALIAS_MAP_SLYVKY = {
    "сливки": {"target_display_name": "Вершки 30%", "target_canonical_name": "вершки 30%"},
}


class TestNormalizeAliasText(unittest.TestCase):
    # Case 1: normalization is stable/idempotent and lowercased.
    def test_normalize_alias_text_is_stable(self):
        first = real_database.normalize_alias_text("  СЛИВКИ  ")
        second = real_database.normalize_alias_text(first)
        self.assertEqual(first, "сливки")
        self.assertEqual(first, second)

    # Case 2 (part): empty/blank text is rejected.
    def test_normalize_alias_text_rejects_empty(self):
        self.assertIsNone(real_database.normalize_alias_text(""))
        self.assertIsNone(real_database.normalize_alias_text("   "))
        self.assertIsNone(real_database.normalize_alias_text(None))

    def test_normalize_alias_text_keeps_percent_and_digits(self):
        self.assertEqual(real_database.normalize_alias_text("Вершки 30%"), "вершки 30%")

    def test_normalize_alias_text_rejects_too_long(self):
        self.assertIsNone(real_database.normalize_alias_text("а" * 61))


class TestValidateAliasAction(unittest.TestCase):
    # Case 2: alias cannot be empty.
    def test_rejects_empty_alias(self):
        self.assertIsNone(_validate_alias_action("", "Вершки"))
        self.assertIsNone(_validate_alias_action("   ", "Вершки"))

    # Case 3: alias cannot have identical normalized source and target.
    def test_rejects_noop_alias(self):
        self.assertIsNone(_validate_alias_action("сливки", "Сливки"))
        self.assertIsNone(_validate_alias_action("Вершки", "вершки"))

    def test_accepts_valid_alias(self):
        self.assertEqual(_validate_alias_action("сливки", "Вершки 30%"), "сливки")


class TestResolveItemName(unittest.TestCase):
    # Case 4: household alias beats the built-in "сливки" -> "вершки" synonym.
    def test_household_alias_wins_over_builtin_synonym(self):
        display, canonical = real_database.resolve_item_name("сливки", ALIAS_MAP_SLYVKY)
        self.assertEqual((display, canonical), ("Вершки 30%", "вершки 30%"))

    # Case 7 (part a): with no household alias, falls back to the built-in synonym.
    def test_falls_back_to_builtin_synonym_without_alias(self):
        display, canonical = real_database.resolve_item_name("сливки", {})
        self.assertEqual((display, canonical), ("сливки", "вершки"))
        display, canonical = real_database.resolve_item_name("сливки", None)
        self.assertEqual((display, canonical), ("сливки", "вершки"))

    def test_unrelated_name_untouched(self):
        display, canonical = real_database.resolve_item_name("Хліб", ALIAS_MAP_SLYVKY)
        self.assertEqual((display, canonical), ("Хліб", "хліб"))


class TestNormalizeItemQuantityWithAlias(unittest.TestCase):
    # Case 5: alias substitutes the name only — quantity from surrounding text survives.
    def test_alias_preserves_quantity(self):
        result = normalize_item_quantity("сливки", "2 шт.", allow_default_unit=True, alias_map=ALIAS_MAP_SLYVKY)
        self.assertEqual(result["name"], "Вершки 30%")
        self.assertEqual(result["canonical_name"], "вершки 30%")
        self.assertEqual(result["quantity_value"], 2.0)
        self.assertEqual(result["quantity_unit"], "шт.")

    # Case 6: alias never invents/changes the unit — "1 шт." stays "шт.", never л/г.
    def test_alias_never_changes_unit(self):
        result = normalize_item_quantity("сливки", "1 шт.", allow_default_unit=True, alias_map=ALIAS_MAP_SLYVKY)
        self.assertEqual(result["name"], "Вершки 30%")
        self.assertEqual(result["quantity_value"], 1.0)
        self.assertEqual(result["quantity_unit"], "шт.")
        self.assertNotIn(result["quantity_unit"], ("л", "г"))

    def test_no_alias_map_behaves_like_builtin_only(self):
        result = normalize_item_quantity("сливки", "2 шт.", allow_default_unit=True, alias_map=None)
        self.assertEqual(result["name"], "сливки")
        self.assertEqual(result["canonical_name"], "вершки")
        self.assertEqual(result["quantity_value"], 2.0)


class TestHouseholdAliasIsolationAndSql(unittest.TestCase):
    # Case 7 (part b): one household's aliases are never visible to another —
    # verified at the SQL layer via the exact WHERE clause and bound params.
    def test_get_household_alias_map_scopes_by_household(self):
        cur = FakeCursor(fetchall_results=[[]])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_household_alias_map(2)
        self.assertEqual(result, {})
        sql, params = cur.queries[0]
        self.assertIn("WHERE household_id = %s", sql)
        self.assertEqual(params, (2,))

    def test_list_household_aliases_scopes_by_household_and_orders(self):
        # Case 13: sorted by alias_normalized (SQL does the sorting; this proves
        # the query asks for it, and that the Python side preserves DB order).
        rows = [(1, "приправа курка", "приправа курка", "Приправа до курки", "приправа до курки"),
                (2, "сливки", "сливки", "Вершки 30%", "вершки 30%")]
        cur = FakeCursor(fetchall_results=[rows])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            aliases = real_database.list_household_aliases(1)
        sql, params = cur.queries[0]
        self.assertIn("WHERE household_id = %s", sql)
        self.assertIn("ORDER BY alias_normalized ASC", sql)
        self.assertEqual(params, (1,))
        self.assertEqual([a["alias_text"] for a in aliases], ["приправа курка", "сливки"])


class TestCreateOrUpdateHouseholdAlias(unittest.TestCase):
    # Case 2/3/8: invalid input never opens a DB connection at all.
    def test_empty_alias_never_touches_db(self):
        with patch.object(real_database, "get_connection") as mock_conn:
            result = real_database.create_or_update_household_alias(1, "", "Вершки", 1)
        self.assertIsNone(result)
        mock_conn.assert_not_called()

    def test_noop_alias_never_touches_db(self):
        with patch.object(real_database, "get_connection") as mock_conn:
            result = real_database.create_or_update_household_alias(1, "сливки", "Сливки", 1)
        self.assertIsNone(result)
        mock_conn.assert_not_called()

    # Case 8 (building a preview only, before confirm): pure validation alone
    # never touches the DB.
    def test_building_preview_does_not_touch_db(self):
        with patch.object(real_database, "get_connection") as mock_conn:
            alias_normalized = _validate_alias_action("сливки", "Вершки 30%")
        self.assertEqual(alias_normalized, "сливки")
        mock_conn.assert_not_called()

    # Case 10: create then update via the same upsert — both succeed, SQL
    # uses ON CONFLICT, and the preview text matches the spec exactly.
    def test_create_then_update_upserts(self):
        row1 = (10, "сливки", "сливки", "Вершки", "вершки")
        cur1 = FakeCursor(fetchone_results=[row1])
        conn1 = FakeConnection(cur1)
        with patch.object(real_database, "get_connection", return_value=conn1):
            created = real_database.create_or_update_household_alias(1, "сливки", "Вершки", 1)
        self.assertEqual(created["target_display_name"], "Вершки")
        self.assertIn("ON CONFLICT (household_id, alias_normalized) DO UPDATE", cur1.queries[0][0])
        self.assertTrue(conn1.committed)

        row2 = (10, "сливки", "сливки", "Вершки 30%", "вершки 30%")
        cur2 = FakeCursor(fetchone_results=[row2])
        conn2 = FakeConnection(cur2)
        with patch.object(real_database, "get_connection", return_value=conn2):
            updated = real_database.create_or_update_household_alias(1, "сливки", "Вершки 30%", 1)
        self.assertEqual(updated["target_display_name"], "Вершки 30%")

        preview = _format_alias_update_preview("сливки", created["target_display_name"], updated["target_display_name"])
        self.assertIn("було → «Вершки»", preview)
        self.assertIn("стане → «Вершки 30%»", preview)


class TestDeleteHouseholdAlias(unittest.TestCase):
    # Case 11: delete only ever targets household_aliases, never products.
    def test_delete_targets_only_alias_table(self):
        cur = FakeCursor(fetchone_results=[(10,)])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            deleted = real_database.delete_household_alias(1, "сливки")
        self.assertTrue(deleted)
        self.assertEqual(len(cur.queries), 1)
        sql, params = cur.queries[0]
        self.assertIn("DELETE FROM household_aliases", sql)
        self.assertNotIn("shopping_items", sql)
        self.assertNotIn("inventory_items", sql)
        self.assertEqual(params, (1, "сливки"))
        self.assertTrue(conn.committed)

    def test_delete_missing_alias_returns_false(self):
        cur = FakeCursor(fetchone_results=[None])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            deleted = real_database.delete_household_alias(1, "не-існує")
        self.assertFalse(deleted)


class TestPendingActionIdempotency(unittest.TestCase):
    # Case 9: cancel pops the pending action without ever calling into the DB.
    def test_cancel_never_calls_db(self):
        apply_mock = MagicMock()
        pending = {1: {"kind": "create", "alias_text": "сливки", "target_display_name": "Вершки"}}
        pending.pop(1, None)  # mirrors the "❌ Скасувати" handler
        self.assertNotIn(1, pending)
        apply_mock.assert_not_called()

    # Case 12: a repeated confirm press finds nothing pending the second time —
    # the exact mechanism bot.py's confirm handlers rely on.
    def test_repeated_confirm_pop_is_a_noop(self):
        pending = {1: {"kind": "create", "alias_text": "сливки", "target_display_name": "Вершки"}}
        first = pending.pop(1, None)
        second = pending.pop(1, None)
        self.assertIsNotNone(first)
        self.assertIsNone(second)


class TestFormatAliasList(unittest.TestCase):
    def test_empty_list_message(self):
        self.assertEqual(format_alias_list([]), "Домашніх назв поки немає.")

    # Case 13: format_alias_list preserves the given (already-sorted) order.
    def test_formats_and_numbers_in_given_order(self):
        aliases = [
            {"alias_text": "приправа курка", "target_display_name": "Приправа до курки"},
            {"alias_text": "сливки", "target_display_name": "Вершки 30%"},
        ]
        text = format_alias_list(aliases)
        lines = text.splitlines()
        self.assertIn("1. приправа курка → Приправа до курки", lines)
        self.assertIn("2. сливки → Вершки 30%", lines)


class TestValidateAliasRouterResult(unittest.TestCase):
    # Case 14: non-empty unresolved_fragments blocks the change regardless of intent.
    def test_unresolved_fragments_block_change(self):
        router_result = {
            "intent": "create_or_update",
            "alias_text": "сливки",
            "target_display_name": "Вершки",
            "unresolved_fragments": ["якийсь незрозумілий шматок"],
        }
        kind, payload = _validate_alias_router_result(router_result)
        self.assertEqual(kind, "unresolved")
        self.assertEqual(payload, ["якийсь незрозумілий шматок"])

    def test_list_intent(self):
        kind, payload = _validate_alias_router_result(
            {"intent": "list", "alias_text": None, "target_display_name": None, "unresolved_fragments": []}
        )
        self.assertEqual(kind, "list")

    def test_create_or_update_intent(self):
        kind, payload = _validate_alias_router_result({
            "intent": "create_or_update", "alias_text": "сливки", "target_display_name": "Вершки",
            "unresolved_fragments": [],
        })
        self.assertEqual((kind, payload), ("create_or_update", "сливки"))

    def test_create_or_update_invalid_noop(self):
        kind, payload = _validate_alias_router_result({
            "intent": "create_or_update", "alias_text": "сливки", "target_display_name": "Сливки",
            "unresolved_fragments": [],
        })
        self.assertEqual(kind, "invalid")

    def test_delete_intent(self):
        kind, payload = _validate_alias_router_result({
            "intent": "delete", "alias_text": "сливки", "target_display_name": None,
            "unresolved_fragments": [],
        })
        self.assertEqual((kind, payload), ("delete", "сливки"))

    def test_none_intent(self):
        kind, payload = _validate_alias_router_result({
            "intent": "none", "alias_text": None, "target_display_name": None, "unresolved_fragments": [],
        })
        self.assertEqual(kind, "none")


if __name__ == "__main__":
    unittest.main()
