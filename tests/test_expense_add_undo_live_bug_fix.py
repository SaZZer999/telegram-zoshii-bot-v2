"""Regression coverage for the live bug: confirming a standalone expense add
("✅ Так, додати" from the expenses submenu / global "Запиши ... zł" command)
left an OLDER journal row (e.g. a previous inventory action) as still
"latest", so pressing "↩️ Скасувати останню дію" right after adding an
expense showed a preview for that older action instead of offering to
remove the expense just added.

Root cause: database.add_expense() was a bare INSERT with no journal write
at all, unlike apply_global_household_operations() (used by the Global
Household Router and photo-receipt flows), which already journals every
confirmed action including expense-only ones. The fix makes add_expense()
write one household_action_journal row in the SAME transaction, using the
exact snapshot shape apply_global_household_operations already uses for its
own add_expense op — so get_latest_undoable_action/apply_undo_action/
format_undo_preview (all fully generic) handle it identically with no
changes of their own.

This file covers two layers:
  * DB-layer (real_database, FakeCursor/FakeConnection): the exact SQL/
    transaction/journal-shape guarantees — see also tests/test_action_journal.py.
  * bot.py webhook-layer: the end-to-end user-visible sequence (confirm
    expense -> press undo -> preview references the expense, not an older
    action), using a small in-memory fake journal shared between mocked
    add_expense/get_latest_undoable_action/apply_undo_action, driven through
    the REAL action_history module so the preview text assertions are
    meaningful, and through the REAL bot.py/expenses.py dispatch code.
"""
import os
import sys
import importlib.util
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import action_history

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_live_bug_fix_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)


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


class TestLiveBugReproductionAtDbLayer(unittest.TestCase):
    """Reproduces the exact live sequence at the DB layer: an older
    inventory-restore action already sits in the journal (mirroring "🧊
    Запаси\\n• Повернути Молоко — 1 шт." from the bug report); confirming a
    standalone expense add must now itself produce a journal row whose
    formatted preview references the expense, not the older inventory
    action — because format_undo_preview only ever renders whatever ONE
    summary it's given, get_latest_undoable_action (SQL-ordered by
    created_at DESC, id DESC, already covered by test_action_journal.py) is
    what ensures that summary is now the expense-add's own, not the stale
    older row."""

    def test_expense_add_journal_summary_renders_expense_not_prior_inventory_action(self):
        # The older action's own summary is irrelevant here — the point is
        # that the NEW add_expense call produces its OWN independent journal
        # row/summary, never reusing or merging with any prior one.
        cursor = FakeCursor(fetchone_results=[(555,)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.add_expense(
                1, 10, Decimal("51.23"), "PLN", "Кафе / ресторани", "тестова кава", date(2026, 7, 14)
            )
        _, params = _journal_inserts(cursor)[0]
        summary = params[6].obj
        preview = action_history.format_undo_preview(summary)
        self.assertIn("тестова кава", preview)
        self.assertIn("51,23 zł", preview)
        self.assertNotIn("Молоко", preview)
        self.assertNotIn("🧊 Запаси", preview)
        self.assertIn("💸 Витрати", preview)


class _FakeExpenseDate:
    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


# =========================
# bot.py webhook-layer reproduction
# =========================
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import expenses  # noqa: E402
from bot import pending_undo_action  # noqa: E402


import itertools  # noqa: E402

# A 13-digit base kept far outside the range of any chat_id used as a
# literal update_id elsewhere in the suite (bot.webhook()'s
# _is_duplicate_update guard is a single process-global cache keyed only by
# update_id, shared across every test file in the same run).
_update_ids = itertools.count(9_000_000_000_000)


def _make_update(chat_id, text, user_id=555):
    # update_id must be unique per call — bot.webhook()'s _is_duplicate_update
    # guard is a process-global, update_id-only cache, so reusing an id (e.g.
    # equal to chat_id) across the multiple webhook calls a single test makes
    # would silently drop every call after the first.
    return {
        "update_id": next(_update_ids),
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class _FakeJournal:
    """In-memory stand-in for household_action_journal, driven through the
    REAL action_history module so summaries/preview text are exactly what
    production code would produce — shared by the three mocked DB
    functions bot.py calls (add_expense, get_latest_undoable_action,
    apply_undo_action), so the sequence exercised here is the real
    bot.py/expenses.py dispatch code, not a re-implementation of it."""

    def __init__(self):
        self.rows = []
        self._next_id = 1
        self._next_expense_id = 1

    def seed_inventory_restore(self, household_id, actor_user_id, name="Молоко", qty_text="1 шт."):
        row = {"id": 900, "name": name, "canonical_name": name.lower(), "quantity_text": qty_text,
               "category": "Молочне та яйця"}
        before_snap = {"inventory_buckets": {name.lower(): [row]}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {name.lower(): []}, "shopping_buckets": {}, "expense_adds": []}
        self._append(household_id, actor_user_id, before_snap, post_snap)

    def add_expense(self, household_id, user_db_id, amount, currency, category, description, expense_date):
        expense_id = self._next_expense_id
        self._next_expense_id += 1
        expense_added = {
            "id": expense_id, "household_id": household_id, "amount": str(amount), "currency": currency,
            "category": category, "description": description or None, "expense_date": expense_date.isoformat(),
            "created_by_user_id": user_db_id,
        }
        before_snap = {"inventory_buckets": {}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {}, "shopping_buckets": {}, "expense_adds": [expense_added]}
        self._append(household_id, user_db_id, before_snap, post_snap)
        return expense_id

    def _append(self, household_id, actor_user_id, before_snap, post_snap):
        summary = action_history.build_operation_summary(before_snap, post_snap)
        self.rows.append({
            "id": self._next_id, "household_id": household_id, "actor_user_id": actor_user_id,
            "status": "active", "summary": summary,
        })
        self._next_id += 1

    def get_latest_undoable_action(self, household_id, actor_user_id):
        for row in reversed(self.rows):
            if row["status"] == "active" and row["household_id"] == household_id and row["actor_user_id"] == actor_user_id:
                return {"id": row["id"], "summary": row["summary"]}
        return None

    def apply_undo_action(self, action_id, household_id, actor_user_id):
        for row in self.rows:
            if row["id"] == action_id and row["household_id"] == household_id and row["actor_user_id"] == actor_user_id:
                if row["status"] != "active":
                    raise real_database.StaleSnapshotError("already undone")
                row["status"] = "undone"
                return
        raise real_database.StaleSnapshotError("not found")


class TestLiveBugReproductionThroughBotWebhook(unittest.TestCase):
    """End-to-end reproduction of the user's exact reported sequence through
    the real bot.py dispatcher and expenses.py confirm handler: confirm a
    standalone expense add, immediately press "↩️ Скасувати останню дію" —
    the preview must reference the just-added expense, never the older
    inventory action."""

    def setUp(self):
        pending_undo_action.clear()
        expenses.pending_expense.clear()
        self.journal = _FakeJournal()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_add = patch.object(bot, "add_expense", side_effect=self.journal.add_expense)
        patcher_add.start()
        self.addCleanup(patcher_add.stop)
        patcher_latest = patch.object(bot, "get_latest_undoable_action", side_effect=self.journal.get_latest_undoable_action)
        patcher_latest.start()
        self.addCleanup(patcher_latest.stop)
        patcher_apply = patch.object(bot, "apply_undo_action", side_effect=self.journal.apply_undo_action)
        patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

    def tearDown(self):
        pending_undo_action.clear()
        expenses.pending_expense.clear()
        bot.active_list_context.clear()

    def _confirm_pending_expense(self, chat_id, amount=Decimal("51.23"), description="тестова кава",
                                  category="Кафе / ресторани"):
        expenses.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": amount, "currency": "PLN",
            "category": category, "description": description, "expense_date": date(2026, 7, 14),
            "origin": "global",
        }
        _call_webhook(_make_update(chat_id,"✅ Так, додати"))

    def test_undo_preview_after_expense_confirm_references_the_expense(self):
        chat_id = 9101
        self.journal.seed_inventory_restore(household_id=1, actor_user_id=10)  # the older action from the bug report
        self._confirm_pending_expense(chat_id)

        _call_webhook(_make_update(chat_id,"↩️ Скасувати останню дію"))

        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        preview_texts = [t for t in sent_texts if t.startswith(action_history.UNDO_BUTTON_TEXT)]
        self.assertEqual(len(preview_texts), 1)
        preview = preview_texts[0]
        self.assertIn("тестова кава", preview)
        self.assertIn("51,23 zł", preview)
        self.assertNotIn("Молоко", preview)
        self.assertIn(chat_id, pending_undo_action)

    def test_cancel_undo_leaves_expense_and_journal_untouched(self):
        chat_id = 9102
        self._confirm_pending_expense(chat_id)
        _call_webhook(_make_update(chat_id,"↩️ Скасувати останню дію"))
        self.assertIn(chat_id, pending_undo_action)

        _call_webhook(_make_update(chat_id,"❌ Скасувати"))

        self.assertNotIn(chat_id, pending_undo_action)
        self.assertEqual(self.journal.rows[-1]["status"], "active")

    def test_confirm_undo_deletes_exactly_the_added_expense_action(self):
        chat_id = 9103
        self._confirm_pending_expense(chat_id)
        _call_webhook(_make_update(chat_id,"↩️ Скасувати останню дію"))
        action_id = pending_undo_action[chat_id]["action_id"]

        _call_webhook(_make_update(chat_id,"✅ Так, скасувати"))

        self.assertNotIn(chat_id, pending_undo_action)
        undone = [r for r in self.journal.rows if r["id"] == action_id]
        self.assertEqual(len(undone), 1)
        self.assertEqual(undone[0]["status"], "undone")

    def test_repeated_undo_confirm_is_blocked(self):
        chat_id = 9104
        self._confirm_pending_expense(chat_id)
        _call_webhook(_make_update(chat_id,"↩️ Скасувати останню дію"))
        _call_webhook(_make_update(chat_id,"✅ Так, скасувати"))
        self.mock_send.reset_mock()

        _call_webhook(_make_update(chat_id,"✅ Так, скасувати"))

        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertIn("Немає активної дії для підтвердження.", sent_texts)

    def test_undo_never_crosses_into_another_households_or_users_scope(self):
        chat_id_a = 9105
        self._confirm_pending_expense(chat_id_a)

        with patch.object(bot, "get_household_and_user", return_value=(2, 20)):
            chat_id_b = 9106
            with patch.object(bot, "get_latest_undoable_action", side_effect=self.journal.get_latest_undoable_action):
                _call_webhook(_make_update(chat_id_b,"↩️ Скасувати останню дію"))

        self.assertNotIn(chat_id_b, pending_undo_action)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertIn(action_history.NO_UNDOABLE_ACTION_MSG, sent_texts)

    def test_later_inventory_action_becomes_latest_after_expense_add(self):
        """#15: an action journaled AFTER the expense add correctly becomes
        "latest" in turn — the expense-add fix doesn't freeze "latest" on
        itself forever."""
        chat_id = 9107
        self._confirm_pending_expense(chat_id)
        self.journal.seed_inventory_restore(household_id=1, actor_user_id=10, name="Хліб", qty_text="2 шт.")

        _call_webhook(_make_update(chat_id,"↩️ Скасувати останню дію"))

        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        preview_texts = [t for t in sent_texts if t.startswith(action_history.UNDO_BUTTON_TEXT)]
        self.assertEqual(len(preview_texts), 1)
        self.assertIn("Хліб", preview_texts[0])
        self.assertNotIn("тестова кава", preview_texts[0])


if __name__ == "__main__":
    unittest.main()
