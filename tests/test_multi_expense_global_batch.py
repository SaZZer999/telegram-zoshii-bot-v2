"""Global Multi-Expense Batch v1 — several new expenses from ONE message
(e.g. "Купив молоко за 8 zł. Купив хліб за 5 zł.") land in one combined
preview, one confirm, one journal record, one Undo. No real Gemini,
Telegram, Render, or Supabase call happens anywhere in this file — the
Gemini router call and every DB-facing bot.py helper are patched, and the
apply_global_household_operations()/apply_undo_action() DB-layer tests run
against the real database.py loaded fresh with a fake psycopg
connection/cursor standing in for Postgres (same pattern as
tests/test_global_household_operations.py and
tests/test_safe_undo_global_action.py).
"""
import sys
import os
import importlib.util
import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_multi_expense_batch_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402 — import side effect wires household_router.configure(...)
import household_router  # noqa: E402
import action_history  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    active_list_context,
    saved_list_context,
)


def _todays_warsaw_date_iso():
    return datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _milk_bread_sausages_router_result():
    """Mimics the confirmed scenario: "Купив 1 л молока за 8 zł. Купив хліб
    за 5 zł. Додай до запасів пару сосисок." — two new expenses (in message
    order) plus three inventory adds."""
    return {
        "intent": "household_operations",
        "operations": [
            {"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
            {"type": "add_expense", "amount": "8", "currency": "PLN", "category": "Продукти",
             "description": "Молоко", "expense_date": _todays_warsaw_date_iso()},
            {"type": "add_inventory", "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
            {"type": "add_expense", "amount": "5", "currency": "PLN", "category": "Продукти",
             "description": "Хліб", "expense_date": _todays_warsaw_date_iso()},
            {"type": "add_inventory", "name": "Сосиски", "quantity_text": "пару", "category": "М'ясо та риба"},
        ],
        "unresolved_fragments": [],
    }


# =========================
# Webhook-level: preview + confirm + no-partial-on-error
# =========================
class _BaseWebhookTestCase(unittest.TestCase):
    def setUp(self):
        for d in (pending_global_household, active_list_context, saved_list_context):
            d.clear()

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_shopping = patch.object(bot, "get_active_shopping_items", return_value=[])
        self.mock_shopping = patcher_shopping.start()
        self.addCleanup(patcher_shopping.stop)

        patcher_inventory = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory = patcher_inventory.start()
        self.addCleanup(patcher_inventory.stop)

        patcher_recent_expenses = patch.object(bot, "get_recent_expenses_for_deletion", return_value=[])
        self.mock_recent_expenses = patcher_recent_expenses.start()
        self.addCleanup(patcher_recent_expenses.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        self.mock_alias_map = patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_household_router = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_household_router = patcher_household_router.start()
        self.addCleanup(patcher_household_router.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

    def tearDown(self):
        for d in (pending_global_household, active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestMultiExpensePreview(_BaseWebhookTestCase):
    # #1/#2: two "Купив ... за ..." in one message build ONE combined
    # preview with both expenses, with no DB write before confirm.
    def test_two_purchases_build_one_preview_with_both_expenses(self):
        chat_id = 990001
        self.mock_household_router.return_value = _milk_bread_sausages_router_result()
        _call_webhook(_make_update(800000001, chat_id, "Купив 1 л молока за 8 zł. Купив хліб за 5 zł. Додай до запасів пару сосисок."))

        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 2)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("8"))
        self.assertEqual(data["new_expenses"][1]["amount"], Decimal("5"))
        # Legacy singular key has no single answer for a 2-expense batch.
        self.assertIsNone(data["new_expense"])
        self.assertEqual(len(data["add_inventory_items"]), 3)

        texts = self._sent_texts()
        self.assertTrue(any(
            "💸 Витрати" in t and "8,00 zł" in t and "5,00 zł" in t and "Молоко" in t and "Хліб" in t
            for t in texts
        ))
        self.mock_apply.assert_not_called()

    # #6: a plain single purchase ("Купив молоко за 10 zł") is unaffected —
    # exactly the old single-expense payload/preview shape.
    def test_single_purchase_does_not_regress(self):
        chat_id = 990002
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": _todays_warsaw_date_iso()},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(800000002, chat_id, "Купив молоко за 10 zł"))

        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertIsNotNone(data["new_expense"])
        self.assertEqual(data["new_expense"]["amount"], Decimal("10.00"))
        texts = self._sent_texts()
        # Old single-expense preview lines, unchanged: "Додати ..." + "Категорія: ...".
        self.assertTrue(any("• Додати Молоко — 10,00 zł" in t for t in texts))
        self.assertTrue(any("• Категорія: Продукти" in t for t in texts))

    # #4: an error anywhere in the batch (here: the third op, an add_expense
    # with an unparsable amount) blocks the WHOLE preview — no partial
    # pending state, no DB write.
    def test_error_in_third_operation_blocks_entire_batch(self):
        chat_id = 990003
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "8", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": _todays_warsaw_date_iso()},
                {"type": "add_expense", "amount": "не число", "currency": "PLN", "category": "Продукти",
                 "description": "Хліб", "expense_date": _todays_warsaw_date_iso()},
            ],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(800000003, chat_id, "Купив молоко за 8 zł. Купив хліб за х zł."))

        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Не зміг безпечно обробити" in t for t in texts))


class TestMultiExpenseConfirm(_BaseWebhookTestCase):
    # #3: confirm passes BOTH expenses into one apply_global_household_operations call.
    def test_confirm_passes_both_expenses_in_one_operation(self):
        chat_id = 990004
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [],
            "consume_changes": [], "inventory_targets": [],
            "new_expenses": [
                {"amount": Decimal("8.00"), "currency": "PLN", "category": "Продукти",
                 "category_was_defaulted": False, "description": "Молоко", "expense_date": date(2026, 7, 5)},
                {"amount": Decimal("5.00"), "currency": "PLN", "category": "Продукти",
                 "category_was_defaulted": False, "description": "Хліб", "expense_date": date(2026, 7, 5)},
            ],
            "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        self.mock_apply.return_value = {
            "shopping_added": 0, "inventory_added": 0, "inventory_updated": 0, "inventory_removed": 0,
            "expense_added_id": 1, "expense_added_ids": [1, 2], "expense_deleted": False,
        }
        _call_webhook(_make_update(800000004, chat_id, "✅ Так, застосувати"))

        self.mock_apply.assert_called_once()
        _, kwargs = self.mock_apply.call_args
        self.assertEqual(len(kwargs["new_expenses"]), 2)
        self.assertEqual(kwargs["new_expenses"][0]["description"], "Молоко")
        self.assertEqual(kwargs["new_expenses"][1]["description"], "Хліб")
        # category_was_defaulted must be stripped before it reaches the DB layer.
        self.assertNotIn("category_was_defaulted", kwargs["new_expenses"][0])
        self.assertNotIn(chat_id, pending_global_household)

    # #5: a legacy single "new_expense"-shaped pending entry (no
    # "new_expenses" key at all — the pre-batch shape) still confirms fine,
    # normalized into a one-element list.
    def test_legacy_single_new_expense_payload_still_confirms(self):
        chat_id = 990005
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [],
            "consume_changes": [], "inventory_targets": [],
            "new_expense": {"amount": Decimal("10.00"), "currency": "PLN", "category": "Продукти",
                             "category_was_defaulted": False, "description": "Масло", "expense_date": date(2026, 7, 5)},
            "delete_expense": None, "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        self.mock_apply.return_value = {
            "shopping_added": 0, "inventory_added": 0, "inventory_updated": 0, "inventory_removed": 0,
            "expense_added_id": 1, "expense_added_ids": [1], "expense_deleted": False,
        }
        _call_webhook(_make_update(800000005, chat_id, "✅ Так, застосувати"))

        self.mock_apply.assert_called_once()
        _, kwargs = self.mock_apply.call_args
        self.assertEqual(len(kwargs["new_expenses"]), 1)
        self.assertEqual(kwargs["new_expenses"][0]["description"], "Масло")


# =========================
# DB layer: apply_global_household_operations — one transaction, one journal record
# =========================
class FakeCursor:
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


def _journal_inserts(cursor):
    return [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]


def _expense(amount, description):
    return {
        "amount": Decimal(amount), "currency": "PLN", "category": "Продукти",
        "description": description, "expense_date": date(2026, 7, 5),
    }


class TestApplyGlobalHouseholdOperationsMultiExpense(unittest.TestCase):
    # #7: two new_expenses inserted in the SAME transaction produce exactly
    # ONE journal record (not one per expense).
    def test_two_new_expenses_create_one_journal_record(self):
        cursor = FakeCursor(fetchone_results=[(501,), (502,)])  # two expense INSERT ... RETURNING id
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.apply_global_household_operations(
                household_id=1, user_db_id=10,
                new_expenses=[_expense("8.00", "Молоко"), _expense("5.00", "Хліб")],
            )
        self.assertTrue(conn.committed)
        self.assertEqual(result["expense_added_ids"], [501, 502])
        self.assertEqual(result["expense_added_id"], 501)
        insert_queries = [q for q in cursor.queries if "INSERT INTO expenses" in q[0]]
        self.assertEqual(len(insert_queries), 2)
        self.assertEqual(len(_journal_inserts(cursor)), 1)

    # #5 (DB layer): the legacy singular `new_expense=` kwarg still works,
    # normalized into a one-element new_expenses list internally.
    def test_legacy_new_expense_kwarg_still_works(self):
        cursor = FakeCursor(fetchone_results=[(555,)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.apply_global_household_operations(
                household_id=1, user_db_id=10, new_expense=_expense("10.00", "Масло"),
            )
        self.assertTrue(conn.committed)
        self.assertEqual(result["expense_added_id"], 555)
        self.assertEqual(result["expense_added_ids"], [555])

    def test_no_expenses_produces_empty_ids_list(self):
        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.apply_global_household_operations(household_id=1, user_db_id=10)
        self.assertEqual(result["expense_added_ids"], [])
        self.assertIsNone(result["expense_added_id"])


# =========================
# Undo: multi-expense atomicity
# =========================
class ScriptedCursor:
    """Same shape as tests/test_safe_undo_global_action.py's ScriptedCursor —
    each execute() is matched against `handlers` (substring, fetchone,
    fetchall), consumed once per match in registration order."""
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


class UndoFakeConnection:
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


def _journal_handler(before_snapshot, post_snapshot, household_id=1, actor_user_id=10, status="active"):
    return (
        "FROM household_action_journal WHERE id=%s FOR UPDATE",
        (household_id, actor_user_id, status, before_snapshot, post_snapshot),
        None,
    )


def _empty_snapshot():
    return {"inventory_buckets": {}, "shopping_buckets": {}, "expense_delete": None}


class _FakeDate:
    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


class TestUndoMultiExpense(unittest.TestCase):
    # #8: undoing a multi-expense action deletes ALL of the expenses it
    # created, in one atomic transaction, and marks the journal row undone.
    def test_undo_deletes_both_created_expenses(self):
        expense_1 = {"id": 501, "household_id": 1, "amount": "8.00", "currency": "PLN",
                     "category": "Продукти", "description": "Молоко", "expense_date": "2026-07-05"}
        expense_2 = {"id": 502, "household_id": 1, "amount": "5.00", "currency": "PLN",
                     "category": "Продукти", "description": "Хліб", "expense_date": "2026-07-05"}
        before_snap = _empty_snapshot()
        post_snap = {**_empty_snapshot(), "expense_adds": [expense_1, expense_2]}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            (
                "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                (Decimal("8.00"), "PLN", "Продукти", "Молоко", _FakeDate("2026-07-05")),
                None,
            ),
            (
                "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                (Decimal("5.00"), "PLN", "Продукти", "Хліб", _FakeDate("2026-07-05")),
                None,
            ),
        ])
        conn = UndoFakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        delete_queries = [q for q in cursor.queries if "DELETE FROM expenses" in q[0]]
        self.assertEqual(len(delete_queries), 2)
        deleted_ids = {q[1][0] for q in delete_queries}
        self.assertEqual(deleted_ids, {501, 502})
        self.assertTrue(any("status='undone'" in sql for sql, _ in cursor.queries))

    # #9: if even ONE of the two created expenses changed since the forward
    # action, the WHOLE undo is blocked — no partial delete.
    def test_one_changed_expense_blocks_the_entire_undo(self):
        expense_1 = {"id": 501, "household_id": 1, "amount": "8.00", "currency": "PLN",
                     "category": "Продукти", "description": "Молоко", "expense_date": "2026-07-05"}
        expense_2 = {"id": 502, "household_id": 1, "amount": "5.00", "currency": "PLN",
                     "category": "Продукти", "description": "Хліб", "expense_date": "2026-07-05"}
        before_snap = _empty_snapshot()
        post_snap = {**_empty_snapshot(), "expense_adds": [expense_1, expense_2]}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            (
                "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                (Decimal("8.00"), "PLN", "Продукти", "Молоко", _FakeDate("2026-07-05")),
                None,
            ),
            (
                "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                (Decimal("99.00"), "PLN", "Продукти", "Хліб", _FakeDate("2026-07-05")),  # amount changed
                None,
            ),
        ])
        conn = UndoFakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertFalse(conn.committed)
        self.assertFalse(any("DELETE FROM expenses" in sql for sql, _ in cursor.queries))

    # #8 (summary/journal side): build_operation_summary reflects all the
    # expenses a multi-expense action added, for the Undo preview text.
    def test_summary_lists_every_added_expense(self):
        expense_1 = {"amount": "8.00", "currency": "PLN", "category": "Продукти", "description": "Молоко"}
        expense_2 = {"amount": "5.00", "currency": "PLN", "category": "Продукти", "description": "Хліб"}
        before_snapshot = {"inventory_buckets": {}, "shopping_buckets": {}, "expense_delete": None}
        post_action_snapshot = {"inventory_buckets": {}, "shopping_buckets": {}, "expense_adds": [expense_1, expense_2]}

        summary = action_history.build_operation_summary(before_snapshot, post_action_snapshot)
        self.assertEqual(len(summary["expenses_added"]), 2)
        self.assertIsNone(summary["expense_added"])  # no single answer for 2 expenses

        preview_text = action_history.format_undo_preview(summary)
        self.assertIn("Видалити витрату: Молоко", preview_text)
        self.assertIn("Видалити витрату: Хліб", preview_text)


if __name__ == "__main__":
    unittest.main()
