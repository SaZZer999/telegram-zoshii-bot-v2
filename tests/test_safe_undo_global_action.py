import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_safe_undo_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

import action_history


class ScriptedCursor:
    """Fake cursor for apply_undo_action's varying, non-positional query
    shape (which/how-many bucket reads happen depends on how many canonical
    names a journal row touched). Each execute() call is matched against
    `handlers` — a list of (substring, fetchone_value, fetchall_value)
    consumed ONCE per match, in registration order, so the SAME substring
    can be registered twice to hand back two different results across two
    calls (never needed in these tests, but supported). No match ->
    fetchone()=None, fetchall()=[] (an unexpected extra query behaves like a
    genuinely empty result, never a crash) — every test here still asserts
    on the actual queries list, so a truly wrong/missing query is still caught.
    """
    def __init__(self, handlers=None):
        self.queries = []
        self._handlers = list(handlers or [])
        self._fetchone = None
        self._fetchall = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        for i, (substr, fetchone_val, fetchall_val) in enumerate(self._handlers):
            if substr in sql:
                self._fetchone = fetchone_val
                self._fetchall = fetchall_val if fetchall_val is not None else []
                del self._handlers[i]
                if "DELETE" in sql:
                    self.rowcount = len(params) - 1 if params else 0
                return
        self._fetchone = None
        self._fetchall = []

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

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


def _row(id_, name, canonical_name, qty_text, qty_value, qty_unit, inferred, category):
    return {
        "id": id_, "household_id": 1, "name": name, "canonical_name": canonical_name,
        "quantity_text": qty_text, "quantity_value": qty_value, "quantity_unit": qty_unit,
        "quantity_inferred": inferred, "category": category,
    }


def _row_tuple(row):
    return (
        row["id"], row["name"], row["canonical_name"], row["quantity_text"],
        Decimal(row["quantity_value"]) if row["quantity_value"] is not None else None,
        row["quantity_unit"], row["quantity_inferred"], row["category"],
    )


def _journal_handler(before_snapshot, post_snapshot, household_id=1, actor_user_id=10, status="active"):
    return (
        "FROM household_action_journal WHERE id=%s FOR UPDATE",
        (household_id, actor_user_id, status, before_snapshot, post_snapshot),
        None,
    )


def _empty_snapshot():
    return {"inventory_buckets": {}, "shopping_buckets": {}, "expense_add": None, "expense_delete": None}


class TestUndoPreviewIsReadOnly(unittest.TestCase):
    """#7: get_latest_undoable_action (what the undo preview is built from)
    never writes anything — only a single SELECT."""

    def test_no_write_queries(self):
        cursor = ScriptedCursor(handlers=[])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.get_latest_undoable_action(household_id=1, actor_user_id=10)
        for sql, _ in cursor.queries:
            self.assertNotIn("INSERT", sql)
            self.assertNotIn("UPDATE", sql)
            self.assertNotIn("DELETE", sql)
        self.assertFalse(conn.committed)


class TestUndoInventoryMergeAndInsert(unittest.TestCase):
    """#8/#9: add_inventory undo restores exact before-state for a merge,
    and removes only the inserted row for a brand-new insert."""

    def test_undo_merge_restores_before_quantity(self):
        before_row = _row(501, "Молоко", "молоко", "8 л", "8", "л", False, "Молочне та яйця")
        post_row = _row(501, "Молоко", "молоко", "8,5 л", "8.5", "л", False, "Молочне та яйця")
        before_snap = {"inventory_buckets": {"молоко": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {"молоко": [post_row]}, "shopping_buckets": {}, "expense_add": None}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM inventory_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(post_row)]),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn(Decimal("8"), update_queries[0][1])
        self.assertFalse(any("DELETE FROM inventory_items" in sql for sql, _ in cursor.queries))
        self.assertFalse(any("INSERT INTO inventory_items" in sql for sql, _ in cursor.queries))
        self.assertTrue(any("status='undone'" in sql for sql, _ in cursor.queries))

    def test_undo_insert_removes_only_the_new_row(self):
        post_row = _row(777, "Масло", "масло", "1 шт.", "1", "шт.", True, "Молочне та яйця")
        before_snap = {"inventory_buckets": {"масло": []}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {"масло": [post_row]}, "shopping_buckets": {}, "expense_add": None}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM inventory_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(post_row)]),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        delete_queries = [q for q in cursor.queries if "DELETE FROM inventory_items" in q[0]]
        self.assertEqual(len(delete_queries), 1)
        self.assertEqual(delete_queries[0][1][0], 777)
        self.assertFalse(any("UPDATE inventory_items" in sql for sql, _ in cursor.queries))
        self.assertFalse(any("INSERT INTO inventory_items" in sql for sql, _ in cursor.queries))


class TestUndoConsume(unittest.TestCase):
    """#10/#11: consume_inventory undo restores the pre-consume quantity,
    and consume-to-zero undo reinserts the deleted row."""

    def test_undo_partial_consume_restores_quantity(self):
        before_row = _row(501, "Молоко", "молоко", "14 шт.", "14", "шт.", False, "Молочне та яйця")
        post_row = _row(501, "Молоко", "молоко", "12 шт.", "12", "шт.", False, "Молочне та яйця")
        before_snap = {"inventory_buckets": {"молоко": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {"молоко": [post_row]}, "shopping_buckets": {}, "expense_add": None}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM inventory_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(post_row)]),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        update_queries = [q for q in cursor.queries if "UPDATE inventory_items" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn(Decimal("14"), update_queries[0][1])

    def test_undo_consume_to_zero_restores_deleted_item(self):
        before_row = _row(502, "Ковбаски", "ковбаски", "2 шт.", "2", "шт.", False, "М'ясо та риба")
        before_snap = {"inventory_buckets": {"ковбаски": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {"ковбаски": []}, "shopping_buckets": {}, "expense_add": None}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM inventory_items WHERE household_id=%s AND canonical_name=%s", None, []),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        insert_queries = [q for q in cursor.queries if "INSERT INTO inventory_items" in q[0]]
        self.assertEqual(len(insert_queries), 1)
        self.assertIn("Ковбаски", insert_queries[0][1])
        self.assertIn(Decimal("2"), insert_queries[0][1])


class TestUndoShopping(unittest.TestCase):
    """#12: add_shopping undo works for both an insert and a merge."""

    def test_undo_shopping_insert_removes_row(self):
        post_row = _row(901, "Хліб", "хліб", "1 шт.", "1", "шт.", True, "Хліб і випічка")
        before_snap = {"inventory_buckets": {}, "shopping_buckets": {"хліб": []}, "expense_delete": None}
        post_snap = {"inventory_buckets": {}, "shopping_buckets": {"хліб": [post_row]}, "expense_add": None}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM shopping_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(post_row)]),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        delete_queries = [q for q in cursor.queries if "DELETE FROM shopping_items" in q[0]]
        self.assertEqual(len(delete_queries), 1)
        self.assertEqual(delete_queries[0][1][0], 901)

    def test_undo_shopping_merge_restores_before_quantity(self):
        before_row = _row(902, "Хліб", "хліб", "1 шт.", "1", "шт.", True, "Хліб і випічка")
        post_row = _row(902, "Хліб", "хліб", "2 шт.", "2", "шт.", False, "Хліб і випічка")
        before_snap = {"inventory_buckets": {}, "shopping_buckets": {"хліб": [before_row]}, "expense_delete": None}
        post_snap = {"inventory_buckets": {}, "shopping_buckets": {"хліб": [post_row]}, "expense_add": None}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM shopping_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(post_row)]),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        update_queries = [q for q in cursor.queries if "UPDATE shopping_items" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn(Decimal("1"), update_queries[0][1])


class TestUndoExpense(unittest.TestCase):
    """#13/#14: add_expense undo deletes exactly the created expense;
    delete_expense undo restores exactly the deleted expense."""

    def test_undo_add_expense_deletes_created_row(self):
        expense_add = {
            "id": 555, "household_id": 1, "amount": "10.00", "currency": "PLN",
            "category": "Продукти", "description": "Масло", "expense_date": "2026-07-05",
        }
        before_snap = _empty_snapshot()
        post_snap = {**_empty_snapshot(), "expense_add": expense_add}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            (
                "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                (Decimal("10.00"), "PLN", "Продукти", "Масло", _FakeDate("2026-07-05")),
                None,
            ),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        delete_queries = [q for q in cursor.queries if "DELETE FROM expenses" in q[0]]
        self.assertEqual(len(delete_queries), 1)
        self.assertEqual(delete_queries[0][1][0], 555)
        self.assertTrue(conn.committed)

    def test_undo_delete_expense_reinserts_row(self):
        expense_delete = {
            "id": 101, "household_id": 1, "amount": "4.00", "currency": "PLN",
            "category": "Продукти", "description": "Булочка", "expense_date": "2026-07-03",
            "created_by_user_id": 10,
        }
        before_snap = {**_empty_snapshot(), "expense_delete": expense_delete}
        post_snap = _empty_snapshot()

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("SELECT id FROM expenses WHERE id=%s AND household_id=%s", None, []),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        insert_queries = [q for q in cursor.queries if "INSERT INTO expenses" in q[0]]
        self.assertEqual(len(insert_queries), 1)
        self.assertIn(Decimal("4.00"), insert_queries[0][1])


class TestUndoCompoundIsAtomic(unittest.TestCase):
    """#15: a compound action (inventory + shopping + expense together)
    restores everything in the SAME transaction and marks the journal
    row undone exactly once."""

    def test_compound_undo_applies_all_parts_and_commits_once(self):
        inv_before = _row(501, "Молоко", "молоко", "8 л", "8", "л", False, "Молочне та яйця")
        inv_post = _row(501, "Молоко", "молоко", "8,5 л", "8.5", "л", False, "Молочне та яйця")
        shop_post = _row(901, "Хліб", "хліб", "1 шт.", "1", "шт.", True, "Хліб і випічка")
        expense_add = {
            "id": 555, "household_id": 1, "amount": "10.00", "currency": "PLN",
            "category": "Продукти", "description": "Масло", "expense_date": "2026-07-05",
        }
        before_snap = {
            "inventory_buckets": {"молоко": [inv_before]},
            "shopping_buckets": {"хліб": []},
            "expense_delete": None,
        }
        post_snap = {
            "inventory_buckets": {"молоко": [inv_post]},
            "shopping_buckets": {"хліб": [shop_post]},
            "expense_add": expense_add,
        }

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM inventory_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(inv_post)]),
            ("FROM shopping_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(shop_post)]),
            (
                "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                (Decimal("10.00"), "PLN", "Продукти", "Масло", _FakeDate("2026-07-05")),
                None,
            ),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        self.assertEqual(len([q for q in cursor.queries if "UPDATE inventory_items" in q[0]]), 1)
        self.assertEqual(len([q for q in cursor.queries if "DELETE FROM shopping_items" in q[0]]), 1)
        self.assertEqual(len([q for q in cursor.queries if "DELETE FROM expenses" in q[0]]), 1)
        self.assertEqual(len([q for q in cursor.queries if "status='undone'" in q[0]]), 1)


class TestUndoBlockedByStaleData(unittest.TestCase):
    """#16/#17: any touched bucket or expense that changed since the
    forward action blocks undo ENTIRELY — no partial writes."""

    def test_changed_inventory_bucket_blocks_undo_completely(self):
        before_row = _row(501, "Молоко", "молоко", "8 л", "8", "л", False, "Молочне та яйця")
        post_row = _row(501, "Молоко", "молоко", "8,5 л", "8.5", "л", False, "Молочне та яйця")
        live_row = _row(501, "Молоко", "молоко", "20 л", "20", "л", False, "Молочне та яйця")  # changed since
        before_snap = {"inventory_buckets": {"молоко": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {"молоко": [post_row]}, "shopping_buckets": {}, "expense_add": None}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM inventory_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(live_row)]),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertFalse(conn.committed)
        self.assertFalse(any("UPDATE inventory_items" in sql for sql, _ in cursor.queries))
        self.assertFalse(any("DELETE FROM inventory_items" in sql for sql, _ in cursor.queries))
        self.assertFalse(any("status='undone'" in sql for sql, _ in cursor.queries))

    def test_changed_expense_blocks_undo_completely(self):
        expense_add = {
            "id": 555, "household_id": 1, "amount": "10.00", "currency": "PLN",
            "category": "Продукти", "description": "Масло", "expense_date": "2026-07-05",
        }
        before_snap = _empty_snapshot()
        post_snap = {**_empty_snapshot(), "expense_add": expense_add}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            (
                "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                (Decimal("99.00"), "PLN", "Продукти", "Масло", _FakeDate("2026-07-05")),  # amount changed
                None,
            ),
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertFalse(conn.committed)
        self.assertFalse(any("DELETE FROM expenses" in sql for sql, _ in cursor.queries))


class TestUndoAuthorizationAndDoubleConfirm(unittest.TestCase):
    """#6/#18: another household's/user's action can't be undone, and a
    repeated confirm on an already-undone action never re-applies it."""

    def test_wrong_household_is_rejected(self):
        cursor = ScriptedCursor(handlers=[_journal_handler(_empty_snapshot(), _empty_snapshot(), household_id=999)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)
        self.assertFalse(conn.committed)

    def test_wrong_actor_is_rejected(self):
        cursor = ScriptedCursor(handlers=[_journal_handler(_empty_snapshot(), _empty_snapshot(), actor_user_id=999)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)
        self.assertFalse(conn.committed)

    def test_already_undone_action_is_rejected(self):
        cursor = ScriptedCursor(handlers=[_journal_handler(_empty_snapshot(), _empty_snapshot(), status="undone")])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)
        self.assertFalse(conn.committed)


class _FakeDate:
    """Minimal stand-in for a real `date` — only .isoformat() is used by
    apply_undo_action when comparing the live expense_date to the snapshot."""
    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


class TestOldActionsWithoutJournalCannotBeUndone(unittest.TestCase):
    """#21: with no journal row at all, get_latest_undoable_action returns
    None — there is no "best effort" reconstruction of a pre-journal action."""

    def test_no_journal_row_means_nothing_to_undo(self):
        cursor = ScriptedCursor(handlers=[])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_latest_undoable_action(household_id=1, actor_user_id=10)
        self.assertIsNone(result)


# =========================
# bot.py-level: pending_undo_action gating, navigation clearing
# =========================
import sys  # noqa: E402

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import expenses  # noqa: E402
from bot import (  # noqa: E402
    pending_undo_action, MAIN_KEYBOARD, SHOPPING_KEYBOARD, INVENTORY_KEYBOARD,
    ALIASES_KEYBOARD, EXPENSES_KEYBOARD,
)


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


class TestPendingUndoBlocksEverythingElse(unittest.TestCase):
    """#19: while pending_undo_action is set, any ordinary text is
    intercepted — no Gemini call, no household router, no DB call, no
    general AI-chat, and the pending state itself is left untouched."""

    def setUp(self):
        pending_undo_action.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_gemini = patch.object(bot, "call_gemini")
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)
        patcher_household_router = patch.object(bot.household_router, "_ask_gemini_household_router")
        self.mock_household_router = patcher_household_router.start()
        self.addCleanup(patcher_household_router.stop)
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

    def tearDown(self):
        pending_undo_action.clear()

    def test_ordinary_text_is_intercepted_and_pending_state_unchanged(self):
        chat_id = 8801
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(chat_id, chat_id, "Купив молоко за 10 zł"))

        self.assertIn(chat_id, pending_undo_action)
        self.assertEqual(pending_undo_action[chat_id], {"action_id": 1, "household_id": 1, "user_db_id": 10})
        self.mock_gemini.assert_not_called()
        self.mock_household_router.assert_not_called()
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertIn(action_history.PENDING_UNDO_MSG, sent_texts)


class TestNavigationClearsPendingUndo(unittest.TestCase):
    """#20: /start, /menu, and "⬅️ Головне меню" all clear pending_undo_action."""

    def setUp(self):
        pending_undo_action.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        pending_undo_action.clear()

    def test_start_clears_pending_undo(self):
        chat_id = 8802
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(chat_id, chat_id, "/start"))
        self.assertNotIn(chat_id, pending_undo_action)

    def test_menu_clears_pending_undo(self):
        chat_id = 8803
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(chat_id, chat_id, "/menu"))
        self.assertNotIn(chat_id, pending_undo_action)

    def test_main_menu_button_clears_pending_undo(self):
        chat_id = 8804
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(chat_id, chat_id, "⬅️ Головне меню"))
        self.assertNotIn(chat_id, pending_undo_action)

    def test_cancel_button_clears_pending_undo(self):
        chat_id = 8805
        pending_undo_action[chat_id] = {"action_id": 1, "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(chat_id, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_undo_action)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertIn(action_history.UNDO_CANCELLED_MSG, sent_texts)


def _keyboard_buttons(keyboard):
    return [b for row in keyboard["keyboard"] for b in row]


class TestUndoButtonPresentInEverySubmenu(unittest.TestCase):
    """#1-4 (UX fix): the same undo button shown on the main menu is also
    present on every submenu keyboard — shopping, inventory, expenses,
    aliases — none of the existing buttons on those keyboards were removed."""

    def test_present_in_shopping_keyboard(self):
        self.assertIn(action_history.UNDO_BUTTON_TEXT, _keyboard_buttons(SHOPPING_KEYBOARD))
        self.assertIn("➕ Додати товар", _keyboard_buttons(SHOPPING_KEYBOARD))
        self.assertIn("⬅️ Головне меню", _keyboard_buttons(SHOPPING_KEYBOARD))

    def test_present_in_inventory_keyboard(self):
        self.assertIn(action_history.UNDO_BUTTON_TEXT, _keyboard_buttons(INVENTORY_KEYBOARD))
        self.assertIn("➕ Додати продукти", _keyboard_buttons(INVENTORY_KEYBOARD))
        self.assertIn("⬅️ Головне меню", _keyboard_buttons(INVENTORY_KEYBOARD))

    def test_present_in_expenses_keyboard(self):
        self.assertIn(action_history.UNDO_BUTTON_TEXT, _keyboard_buttons(EXPENSES_KEYBOARD))
        self.assertIn("🗑️ Видалити витрату", _keyboard_buttons(EXPENSES_KEYBOARD))
        self.assertIn("⬅️ Головне меню", _keyboard_buttons(EXPENSES_KEYBOARD))

    def test_present_in_aliases_keyboard(self):
        self.assertIn(action_history.UNDO_BUTTON_TEXT, _keyboard_buttons(ALIASES_KEYBOARD))
        self.assertIn("📋 Показати назви", _keyboard_buttons(ALIASES_KEYBOARD))
        self.assertIn("⬅️ Головне меню", _keyboard_buttons(ALIASES_KEYBOARD))


class TestExpenseKeyboardOwnedByExpensesModule(unittest.TestCase):
    """Refactor guard: EXPENSES_KEYBOARD is a plain literal defined and
    owned by expenses.py itself (no bot.py import-time mutation) — exactly
    one undo button, positioned right before "⬅️ Головне меню", and stable
    across a module reload (nothing appends onto a shared list anymore, so
    there is nothing left to duplicate)."""

    def test_exactly_one_undo_button(self):
        buttons = _keyboard_buttons(expenses.EXPENSES_KEYBOARD)
        self.assertEqual(buttons.count(action_history.UNDO_BUTTON_TEXT), 1)

    def test_undo_button_immediately_precedes_main_menu_row(self):
        rows = expenses.EXPENSES_KEYBOARD["keyboard"]
        self.assertEqual(rows[-2], [action_history.UNDO_BUTTON_TEXT])
        self.assertEqual(rows[-1], ["⬅️ Головне меню"])

    def test_bot_and_expenses_share_the_same_keyboard_object(self):
        self.assertIs(bot.EXPENSES_KEYBOARD, expenses.EXPENSES_KEYBOARD)

    def test_reloading_expenses_module_does_not_duplicate_the_button(self):
        import importlib
        reloaded = importlib.reload(expenses)
        try:
            buttons = _keyboard_buttons(reloaded.EXPENSES_KEYBOARD)
            self.assertEqual(buttons.count(action_history.UNDO_BUTTON_TEXT), 1)
        finally:
            # Restore bot.py's live wiring (configure() + shared dict
            # identity) so later tests in this process aren't affected by
            # the reload — reload() replaces expenses' module globals, which
            # would otherwise leave _bot/MAIN_KEYBOARD unset for the rest of
            # the suite.
            reloaded.configure(bot, bot.active_list_context, bot.MAIN_KEYBOARD)


class TestUndoButtonFromSubmenuTriggersSameFlow(unittest.TestCase):
    """#5: pressing the undo button from a submenu (not just the main menu)
    goes through the exact same _start_undo_flow as the main-menu button —
    no separate handler, no new pending state."""

    def setUp(self):
        pending_undo_action.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

    def tearDown(self):
        pending_undo_action.clear()
        bot.active_list_context.clear()

    def test_button_from_inventory_submenu_starts_undo_preview(self):
        chat_id = 8901
        bot.active_list_context[chat_id] = "inventory"
        with patch.object(bot, "get_latest_undoable_action", return_value=None) as mock_get_action:
            _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))
        mock_get_action.assert_called_once_with(1, 10)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertIn(action_history.NO_UNDOABLE_ACTION_MSG, sent_texts)
        self.assertNotIn(chat_id, pending_undo_action)

    def test_button_from_expenses_submenu_starts_undo_preview_with_action(self):
        chat_id = 8902
        bot.active_list_context[chat_id] = "expenses"
        action = {"id": 321, "summary": {"inventory": [], "shopping": [], "expense_added": None, "expense_deleted": None}}
        with patch.object(bot, "get_latest_undoable_action", return_value=action):
            _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))
        self.assertIn(chat_id, pending_undo_action)
        self.assertEqual(pending_undo_action[chat_id], {"action_id": 321, "household_id": 1, "user_db_id": 10})

    def test_button_from_aliases_submenu_starts_undo_preview(self):
        chat_id = 8903
        bot.active_list_context[chat_id] = "aliases"
        with patch.object(bot, "get_latest_undoable_action", return_value=None) as mock_get_action:
            _call_webhook(_make_update(chat_id, chat_id, action_history.UNDO_BUTTON_TEXT))
        mock_get_action.assert_called_once_with(1, 10)


class TestTextUndoCommandsStillWork(unittest.TestCase):
    """#6: the three existing natural-language undo phrasings still resolve
    to the same flow, unaffected by the submenu keyboard change."""

    def setUp(self):
        pending_undo_action.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

    def tearDown(self):
        pending_undo_action.clear()
        bot.active_list_context.clear()

    def test_each_phrasing_still_triggers_undo_lookup(self):
        for i, phrase in enumerate((
            "Скасувати останню дію", "Повернути останню дію", "Верни зміни назад",
        )):
            chat_id = 8910 + i
            with patch.object(bot, "get_latest_undoable_action", return_value=None) as mock_get_action:
                _call_webhook(_make_update(chat_id, chat_id, phrase))
            mock_get_action.assert_called_once_with(1, 10)
            sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
            self.assertIn(action_history.NO_UNDOABLE_ACTION_MSG, sent_texts)


if __name__ == "__main__":
    unittest.main()
