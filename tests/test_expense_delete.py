import sys
import os
import importlib.util
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes. This lets us exercise the actual
# delete_expense()/get_recent_expenses_for_deletion() SQL/parameterization
# directly, with a fake connection/cursor standing in for Postgres — no real
# Supabase involved.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_expense_delete_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No real Gemini/Telegram/Supabase
# call happens anywhere in this file — every network-facing bot.py function
# is patched per-test.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
import expenses
from bot import _expense_delete_command_gate


# =========================
# FakeCursor/FakeConnection — same shape as tests/test_expenses_v1.py and
# tests/test_expenses_reports.py, used to verify SQL shape/scoping/params
# without a real Postgres.
# =========================
class FakeCursor:
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


def _expense_dict(expense_id, amount, category="Продукти", description="Булочка",
                   expense_date=date(2026, 7, 3)):
    return {
        "id": expense_id, "amount": amount, "currency": "PLN", "category": category,
        "description": description, "expense_date": expense_date,
        "created_at": datetime(2026, 7, 3, 12, 0),
    }


def _delete_router_result(selected_numbers, unresolved_fragments=None):
    return {
        "intent": "delete_expense", "amount": None, "currency": None, "category": None,
        "description": None, "expense_date": None,
        "selected_numbers": selected_numbers,
        "unresolved_fragments": unresolved_fragments or [],
    }


# =========================
# DB-layer: delete_expense — stale-snapshot guard, household scoping
# =========================
class TestDeleteExpenseDbLayer(unittest.TestCase):
    def test_delete_succeeds_when_snapshot_matches(self):
        cursor = FakeCursor(fetchone_results=[(Decimal("4.00"), "Продукти", date(2026, 7, 3), "Булочка")])
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.delete_expense(household_id=1, expense_id=42, snapshot=snapshot)
        select_sql, select_params = cursor.queries[0]
        self.assertIn("FOR UPDATE", select_sql)
        self.assertEqual(select_params, (42, 1))
        delete_sql, delete_params = cursor.queries[1]
        self.assertIn("DELETE FROM expenses", delete_sql)
        self.assertEqual(delete_params, (42, 1))
        self.assertTrue(conn.committed)

    # Case 7 — household isolation
    def test_delete_is_scoped_to_household_id(self):
        cursor = FakeCursor(fetchone_results=[None])  # no row for this household -> stale
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_expense(household_id=999, expense_id=42, snapshot=snapshot)
        select_sql, select_params = cursor.queries[0]
        self.assertIn("household_id = %s", select_sql)
        self.assertEqual(select_params, (42, 999))
        # Never reaches a DELETE statement once the row lookup comes back empty.
        self.assertEqual(len(cursor.queries), 1)

    # Case 8 — stale: row already deleted
    def test_stale_when_row_already_deleted(self):
        cursor = FakeCursor(fetchone_results=[None])
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_expense(household_id=1, expense_id=42, snapshot=snapshot)
        self.assertFalse(conn.committed)

    # Case 8 — stale: amount changed since the preview was built
    def test_stale_when_amount_changed(self):
        cursor = FakeCursor(fetchone_results=[(Decimal("9.99"), "Продукти", date(2026, 7, 3), "Булочка")])
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_expense(household_id=1, expense_id=42, snapshot=snapshot)
        # Only the SELECT ran — no DELETE was ever issued for a stale row.
        self.assertEqual(len(cursor.queries), 1)

    def test_stale_when_category_changed(self):
        cursor = FakeCursor(fetchone_results=[(Decimal("4.00"), "Транспорт", date(2026, 7, 3), "Булочка")])
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_expense(household_id=1, expense_id=42, snapshot=snapshot)


class TestGetRecentExpensesForDeletionIsolation(unittest.TestCase):
    def test_scoped_to_household_id_and_includes_id(self):
        cursor = FakeCursor(fetchall_results=[[(7, Decimal("4.00"), "PLN", "Продукти", "Булочка",
                                                 date(2026, 7, 3), datetime(2026, 7, 3, 12, 0))]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.get_recent_expenses_for_deletion(household_id=3, limit=10)
        sql, params = cursor.queries[-1]
        self.assertIn("WHERE household_id = %s", sql)
        self.assertEqual(params, (3, 10))
        self.assertEqual(result[0]["id"], 7)


# =========================
# Gate — Case 9, 10
# =========================
class TestExpenseDeleteCommandGate(unittest.TestCase):
    def test_button_text_matches(self):
        self.assertTrue(_expense_delete_command_gate("🗑️ Видалити витрату"))

    def test_explicit_delete_phrases_match(self):
        self.assertTrue(_expense_delete_command_gate("Видали витрату за булочку 4 zł"))
        self.assertTrue(_expense_delete_command_gate("Скасуй витрату Biedronka 86,40 zł"))

    # Case 10
    def test_plain_delete_phrase_without_expense_word_does_not_match(self):
        self.assertFalse(_expense_delete_command_gate("Видали булочку"))
        self.assertFalse(_expense_delete_command_gate("Видали булочку 4 zł"))

    def test_empty_text_does_not_match(self):
        self.assertFalse(_expense_delete_command_gate(""))
        self.assertFalse(_expense_delete_command_gate("   "))


class TestNaturalExpenseDeleteGateWidening(unittest.TestCase):
    """Focused tests for the widened _expense_delete_command_gate: a
    delete/cancel verb ("видали"/"видалити"/"скасуй"/"скасувати"/"прибери"/
    "прибрати") combined with a financial-reference stem (покупк/плат/
    транзакц/чек/списанн) now also reaches the existing expense-delete
    router, without requiring the literal word "витрата". The gate itself
    is the only thing that changed — candidate resolution, preview,
    confirm/cancel, stale protection and undo all stay in the existing
    expense-delete flow untouched (see TestNaturalExpenseDeletionRouting
    below for webhook-level proof of that)."""

    # 1.
    def test_skasuy_tu_pokupku_matches(self):
        self.assertTrue(_expense_delete_command_gate("Скасуй ту покупку на 50 zł"))

    # 2.
    def test_prybery_ostanniy_platizh_matches(self):
        self.assertTrue(_expense_delete_command_gate("Прибери останній платіж"))

    # 3.
    def test_vydaly_ostannyu_oplatu_matches(self):
        self.assertTrue(_expense_delete_command_gate("Видали останню оплату за інтернет"))

    # 4. Existing "витрата" form must not regress.
    def test_vydaly_ostannyu_vytratu_still_matches(self):
        self.assertTrue(_expense_delete_command_gate("Видали останню витрату"))

    # Existing forms explicitly called out as must-not-break.
    def test_skasuy_vytratu_na_summu_still_matches(self):
        self.assertTrue(_expense_delete_command_gate("Скасуй витрату на 50 zł"))

    def test_prybery_vytratu_za_internet_matches(self):
        # "прибери" was not previously a recognized delete verb at all — now
        # covered by the widened verb set, alongside "видали"/"скасуй".
        self.assertTrue(_expense_delete_command_gate("Прибери витрату за інтернет"))

    # 5.
    def test_skasuy_zustrich_does_not_match(self):
        self.assertFalse(_expense_delete_command_gate("Скасуй зустріч"))

    def test_skasuy_zamovlennya_does_not_match(self):
        self.assertFalse(_expense_delete_command_gate("Скасуй замовлення в магазині"))

    # 6.
    def test_prybery_moloko_zi_spysku_pokupok_does_not_match(self):
        self.assertFalse(_expense_delete_command_gate("Прибери молоко зі списку покупок"))

    # 7.
    def test_zapyshy_pokupku_stays_add_expense_shape(self):
        self.assertFalse(_expense_delete_command_gate("Запиши покупку на 50 zł"))

    # 8.
    def test_ya_oplatyv_internet_does_not_become_delete(self):
        self.assertFalse(_expense_delete_command_gate("Я оплатив інтернет 120 zł"))

    # A bare zł amount alone (no financial-reference word) must stay just as
    # ambiguous as before the widening — same existing guarantee as
    # "Видали булочку 4 zł" above, now re-verified against the new verb set.
    def test_bare_amount_with_no_financial_word_still_does_not_match(self):
        self.assertFalse(_expense_delete_command_gate("Прибери булочку 4 zł"))


class TestActiveContextDeleteGateAndShoppingBoundary(unittest.TestCase):
    """Pure-function coverage for the two new gates added by this fix (see
    expenses.py): the looser, context-scoped delete gate used ONLY while the
    active expenses submenu is open, and the shopping-list domain-boundary
    check that keeps it from ever swallowing an explicit shopping command."""

    def test_exact_live_phrase_matches_active_context_gate(self):
        self.assertTrue(expenses._expense_delete_active_context_gate("Викресли тестова кава зі списку"))
        self.assertTrue(expenses._expense_delete_active_context_gate("Викресли тестову каву зі списку"))

    def test_button_text_matches_active_context_gate(self):
        self.assertTrue(expenses._expense_delete_active_context_gate("🗑️ Видалити витрату"))

    def test_bare_verb_with_no_financial_word_matches_in_context(self):
        # Unlike the GLOBAL _expense_delete_command_gate, no "витрат"/
        # financial-reference word is required here — the active submenu
        # context itself already establishes this is about an expense.
        self.assertTrue(expenses._expense_delete_active_context_gate("Видали булочку"))
        self.assertTrue(expenses._expense_delete_active_context_gate("Прибери тестова кава"))

    def test_no_delete_verb_does_not_match(self):
        self.assertFalse(expenses._expense_delete_active_context_gate("Кава 14 zł"))
        self.assertFalse(expenses._expense_delete_active_context_gate(""))
        self.assertFalse(expenses._expense_delete_active_context_gate("   "))

    def test_explicit_shopping_list_reference_detected(self):
        self.assertTrue(expenses._looks_like_shopping_list_reference("Викресли хліб зі списку покупок"))
        self.assertTrue(expenses._looks_like_shopping_list_reference("Прибери молоко зі списку покупок"))

    def test_bare_expenses_list_reference_is_not_a_shopping_reference(self):
        # "зі списку" alone (no "покуп..." word) — inside the active
        # expenses context this means THIS (expenses) list, not shopping.
        self.assertFalse(expenses._looks_like_shopping_list_reference("Викресли тестова кава зі списку"))
        self.assertFalse(expenses._looks_like_shopping_list_reference(""))

    def test_strip_delete_command_wrapper_isolates_bare_description(self):
        self.assertEqual(
            expenses._strip_delete_command_wrapper("Викресли тестова кава зі списку"), "тестова кава",
        )
        self.assertEqual(
            expenses._strip_delete_command_wrapper("Видали витрату Biedronka зі списку"), "Biedronka",
        )
        # No recognized wrapper at all — text passes through unchanged.
        self.assertEqual(expenses._strip_delete_command_wrapper("тестова кава"), "тестова кава")


# =========================
# Webhook-level flow
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


class TestExpenseDeleteWebhookFlow(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_saved_router = patch.object(bot, "_ask_gemini_saved_list_router")
        self.mock_saved_router = patcher_saved_router.start()
        self.addCleanup(patcher_saved_router.stop)

    def tearDown(self):
        for d in (bot.pending_expense_delete, bot.expense_delete_selection,
                  bot.pending_expense, bot.pending_delete_batch, bot.pending_alias_action,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    # Case 1 — preview does not delete before confirm
    def test_preview_does_not_delete_before_confirm(self):
        chat_id = 950001
        expenses = [_expense_dict(101, Decimal("4.00"))]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(950000001, chat_id, "Видали витрату за булочку 4 zł"))
                    mock_delete.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 101)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Видалити витрату?" in t for t in sent_texts))

    # Case 2 — confirm deletes exactly the one selected expense
    def test_confirm_deletes_exactly_one_expense(self):
        chat_id = 950002
        bot.pending_expense_delete[chat_id] = {
            "expense_id": 101, "household_id": 1,
            "snapshot": {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"},
            "origin": "global",
        }
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(950000002, chat_id, "✅ Так, видалити"))
            mock_delete.assert_called_once_with(
                1, 101, {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"}
            )
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("✅ Витрату видалено." in t for t in sent_texts))

    # Case 3 — cancel deletes nothing
    def test_cancel_deletes_nothing(self):
        chat_id = 950003
        bot.pending_expense_delete[chat_id] = {
            "expense_id": 101, "household_id": 1,
            "snapshot": {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"},
            "origin": "global",
        }
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(950000003, chat_id, "❌ Скасувати"))
            mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Видалення витрати скасовано." in t for t in sent_texts))

    # Case 4 — repeated confirm never deletes twice
    def test_repeated_confirm_does_not_delete_twice(self):
        chat_id = 950004
        bot.pending_expense_delete[chat_id] = {
            "expense_id": 101, "household_id": 1,
            "snapshot": {"amount": Decimal("4.00"), "category": "Продукти",
                         "expense_date": date(2026, 7, 3), "description": "Булочка"},
            "origin": "global",
        }
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(950000004, chat_id, "✅ Так, видалити"))
            _call_webhook(_make_update(950000005, chat_id, "✅ Так, видалити"))
            mock_delete.assert_called_once()
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Немає активної дії для підтвердження." in t for t in sent_texts))

    # Case 5 — ambiguous selection never creates a preview
    def test_ambiguous_selection_does_not_create_preview(self):
        chat_id = 950005
        expenses = [_expense_dict(101, Decimal("4.00"), description="Булочка"),
                    _expense_dict(102, Decimal("4.00"), description="Пряник")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1, 2])):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(950000006, chat_id, "Видали витрату за 4 zł"))
                    mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertIn(chat_id, bot.expense_delete_selection)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Яку витрату видалити?" in t for t in sent_texts))

    # Case 6 — unresolved_fragments blocks deletion
    def test_unresolved_fragments_blocks_deletion(self):
        chat_id = 950006
        expenses = [_expense_dict(101, Decimal("4.00"))]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(
                bot, "_ask_gemini_expense_router",
                return_value=_delete_router_result([], unresolved_fragments=["незрозуміло яку"]),
            ):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(950000007, chat_id, "Видали ту дивну витрату"))
                    mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # Case 9 — explicit global delete command never reaches general AI-chat
    def test_explicit_delete_command_never_reaches_ai_chat(self):
        chat_id = 950009
        expenses = [_expense_dict(101, Decimal("86.40"), description="Biedronka")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                _call_webhook(_make_update(950000009, chat_id, "Скасуй витрату Biedronka 86,40 zł"))
        self.mock_call_gemini.assert_not_called()
        self.mock_saved_router.assert_not_called()

    # Case 10 — ordinary "Видали булочку" outside the expenses menu is not treated as expense deletion
    def test_plain_phrase_outside_expenses_menu_is_not_treated_as_deletion(self):
        chat_id = 950010
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            with patch.object(bot, "get_recent_expenses_for_deletion") as mock_get_recent:
                _call_webhook(_make_update(950000010, chat_id, "Видали булочку"))
            mock_router.assert_not_called()
            mock_get_recent.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertNotIn(chat_id, bot.expense_delete_selection)

    # Selection mode: pressing the button shows a numbered list, no Gemini call
    def test_button_press_shows_list_without_gemini_call(self):
        chat_id = 950011
        expenses = [_expense_dict(101, Decimal("4.00")), _expense_dict(102, Decimal("86.40"), description="Biedronka")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
                _call_webhook(_make_update(950000011, chat_id, "🗑️ Видалити витрату"))
                mock_router.assert_not_called()
        self.assertIn(chat_id, bot.expense_delete_selection)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Яку витрату видалити?" in t for t in sent_texts))

    # Selection mode: a bare number typed afterwards resolves against the stored list
    def test_number_typed_in_selection_mode_resolves_to_preview(self):
        chat_id = 950012
        expenses = [_expense_dict(101, Decimal("4.00"), description="Булочка"),
                    _expense_dict(102, Decimal("86.40"), description="Biedronka")]
        bot.expense_delete_selection[chat_id] = {
            "household_id": 1, "user_db_id": 10, "expenses": expenses, "origin": "expenses_menu",
        }
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([2])) as mock_router:
            _call_webhook(_make_update(950000012, chat_id, "2"))
            mock_router.assert_called_once()
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 102)
        self.assertNotIn(chat_id, bot.expense_delete_selection)

    # Priority: an active pending preview of another flow is never interrupted
    # by the expense-delete gate.
    def test_delete_gate_does_not_interrupt_other_pending_preview(self):
        chat_id = 950013
        bot.pending_delete_batch[chat_id] = {
            "items": [{"id": 1, "name": "Хліб"}], "household_id": 1, "user_db_id": 10,
        }
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            _call_webhook(_make_update(950000013, chat_id, "Видали витрату за булочку 4 zł"))
            mock_router.assert_not_called()
        self.assertIn(chat_id, bot.pending_delete_batch)
        self.assertNotIn(chat_id, bot.pending_expense_delete)


# =========================
# Natural-language expense-deletion routing — webhook-level proof that the
# WIDENED gate only decides whether to hand text to the EXISTING expense-
# delete router; every downstream guarantee (single Gemini call, no DB
# write before confirm, cancel writes nothing, confirm deletes exactly one
# row, multi-candidate clarification, stale protection, pending-preview
# priority, no leak to general AI-chat) is exercised through the new
# phrasing exactly the same way tests/test_expense_delete.py's own
# TestExpenseDeleteWebhookFlow already proves it for "Видали витрату ...".
# =========================
class TestNaturalExpenseDeletionRouting(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_saved_router = patch.object(bot, "_ask_gemini_saved_list_router")
        self.mock_saved_router = patcher_saved_router.start()
        self.addCleanup(patcher_saved_router.stop)

    def tearDown(self):
        for d in (bot.pending_expense_delete, bot.expense_delete_selection,
                  bot.pending_expense, bot.pending_delete_batch, bot.pending_alias_action,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # 9. New phrase calls the existing expense-router at most once.
    def test_new_phrase_calls_existing_router_at_most_once(self):
        chat_id = 965001
        expenses = [_expense_dict(201, Decimal("50.00"), description="Покупка")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])) as mock_router:
                _call_webhook(_make_update(965000001, chat_id, "Скасуй ту покупку на 50 zł"))
        self.assertEqual(mock_router.call_count, 1)

    # 10. No DB write before confirm.
    def test_no_db_write_before_confirm(self):
        chat_id = 965002
        expenses = [_expense_dict(202, Decimal("50.00"))]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(965000002, chat_id, "Прибери останній платіж"))
                    mock_delete.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 202)

    # 11. Cancel deletes nothing.
    def test_cancel_deletes_nothing(self):
        chat_id = 965003
        expenses = [_expense_dict(203, Decimal("40.00"), description="Інтернет")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                _call_webhook(_make_update(965000003, chat_id, "Видали останню оплату за інтернет"))
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(965000004, chat_id, "❌ Скасувати"))
            mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 12. Confirm deletes only the selected expense.
    def test_confirm_deletes_only_selected_expense(self):
        chat_id = 965004
        expenses = [_expense_dict(204, Decimal("50.00"), description="Покупка")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                _call_webhook(_make_update(965000005, chat_id, "Скасуй ту покупку на 50 zł"))
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(965000006, chat_id, "✅ Так, видалити"))
        mock_delete.assert_called_once_with(1, 204, expenses[0] and {
            "amount": Decimal("50.00"), "category": "Продукти",
            "expense_date": date(2026, 7, 3), "description": "Покупка",
        })
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 13. Multiple candidates use the existing clarification/selection flow.
    def test_multiple_candidates_use_existing_clarification(self):
        chat_id = 965005
        expenses = [
            _expense_dict(205, Decimal("50.00"), description="Покупка А"),
            _expense_dict(206, Decimal("50.00"), description="Покупка Б"),
        ]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1, 2])):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(965000007, chat_id, "Прибери останній платіж"))
                    mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertIn(chat_id, bot.expense_delete_selection)
        self.assertTrue(any("Яку витрату видалити?" in t for t in self._sent_texts()))

    # 14. A stale expense preview is never applied.
    def test_stale_preview_is_not_applied(self):
        chat_id = 965006
        # expenses.py does `from database import StaleSnapshotError` itself
        # (never through the injected _bot) — its own except clause checks
        # against expenses.StaleSnapshotError, not bot.StaleSnapshotError.
        original_stale_error = expenses.StaleSnapshotError
        expenses.StaleSnapshotError = real_database.StaleSnapshotError
        try:
            recent = [_expense_dict(207, Decimal("50.00"), description="Покупка")]
            with patch.object(bot, "get_recent_expenses_for_deletion", return_value=recent):
                with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                    _call_webhook(_make_update(965000008, chat_id, "Скасуй ту покупку на 50 zł"))
            with patch.object(bot, "delete_expense", side_effect=expenses.StaleSnapshotError()):
                _call_webhook(_make_update(965000009, chat_id, "✅ Так, видалити"))
        finally:
            expenses.StaleSnapshotError = original_stale_error
        self.assertTrue(any("Список змінився з іншого пристрою" in t for t in self._sent_texts()))
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 15. Confirming via the new phrasing goes through the exact same
    # handle_delete_confirm()/delete_expense() call as the existing
    # "Видали витрату ..." phrasing — so whatever undo support this flow
    # has today is identically inherited, unaffected by the gate widening.
    # NOTE (finding, not a regression from this change): database.
    # delete_expense() does not itself write a household_action_journal
    # row — unlike execute_inventory_delete/execute_inventory_transform,
    # this dedicated expense-delete path has no "↩️ Скасувати останню дію"
    # integration today. Pre-existing, out of this focused fix's scope
    # (only add_expense/delete_expense issued THROUGH the Global Household
    # Router's own compound-op path get journaled) — not changed here.
    def test_confirm_uses_same_executor_as_existing_vytrata_phrasing(self):
        chat_id = 965007
        expenses = [_expense_dict(208, Decimal("50.00"), description="Покупка")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                _call_webhook(_make_update(965000010, chat_id, "Скасуй ту покупку на 50 zł"))
        with patch.object(bot, "delete_expense", return_value=None) as mock_delete:
            _call_webhook(_make_update(965000011, chat_id, "✅ Так, видалити"))
        mock_delete.assert_called_once()
        args, _ = mock_delete.call_args
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1], 208)

    # 16. Active pending preview has priority — the new gate is never even
    # consulted.
    def test_active_pending_preview_wins_new_gate_never_called(self):
        chat_id = 965008
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("10.00"),
            "currency": "PLN", "category": "Продукти", "description": "Хліб",
            "expense_date": date(2026, 7, 3), "origin": "global",
        }
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            _call_webhook(_make_update(965000012, chat_id, "Скасуй ту покупку на 50 zł"))
            mock_router.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense)
        self.assertTrue(any("незавершена дія з витратами" in t for t in self._sent_texts()))

    # 17. general AI fallback never receives an operational expense-delete
    # command the widened gate accepted.
    def test_general_ai_never_receives_accepted_delete_command(self):
        chat_id = 965009
        expenses = [_expense_dict(209, Decimal("40.00"), description="Інтернет")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                _call_webhook(_make_update(965000013, chat_id, "Видали останню оплату за інтернет"))
        self.mock_call_gemini.assert_not_called()
        self.mock_saved_router.assert_not_called()


# =========================
# Live bug fix: natural-language expense deletion INSIDE the active
# expenses submenu context ("💸 Витрати" -> active_list_context[chat_id] ==
# "expenses"). Root cause: bot.py's _route_active_expenses_context handed
# ANY text straight to _handle_expense_command (the ADD-oriented router,
# which never passes the household's recent-expenses list to Gemini) —
# so "Викресли тестова кава зі списку" could never resolve to a delete
# target and surfaced Gemini's own "не надано список" explanation as if it
# were a real parsing failure. Fix reuses the existing free-text delete
# handler (_handle_expense_delete_global_command ->
# _resolve_expense_delete_selection) with the live DB candidate list,
# gated by a context-scoped, verb-only delete check plus an explicit
# shopping-list domain-boundary guard — see bot.py's
# _route_active_expenses_context and expenses.py's
# _expense_delete_active_context_gate/_looks_like_shopping_list_reference/
# _strip_delete_command_wrapper for the full reasoning.
# =========================
class TestActiveExpensesContextDeleteFix(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_saved_router = patch.object(bot, "_ask_gemini_saved_list_router")
        self.mock_saved_router = patcher_saved_router.start()
        self.addCleanup(patcher_saved_router.stop)

    def tearDown(self):
        for d in (bot.pending_expense_delete, bot.expense_delete_selection,
                  bot.pending_expense, bot.pending_delete_batch, bot.pending_alias_action,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _enter_expenses_context(self, chat_id):
        bot.active_list_context[chat_id] = "expenses"

    # 1/2/4/7/9/10 — exact live phrase: local exact-match fast path, no
    # Gemini call, correct preview.
    def test_exact_live_phrase_creates_preview_via_local_fast_path(self):
        chat_id = 9765001
        self._enter_expenses_context(chat_id)
        expenses_list = [
            _expense_dict(301, Decimal("51.23"), category="Кафе / ресторани", description="тестова кава"),
            _expense_dict(302, Decimal("60.00"), description="Подарунок доньці"),
        ]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list) as mock_get_recent:
            with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
                _call_webhook(_make_update(9765000001, chat_id, "Викресли тестова кава зі списку"))
                mock_router.assert_not_called()
        mock_get_recent.assert_called_once()
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 301)
        sent = self._sent_texts()
        self.assertTrue(any("Видалити витрату?" in t for t in sent))
        self.assertTrue(any("тестова кава" in t and "51,23 zł" in t for t in sent))
        # The old bug's internal-error text must never appear.
        self.assertFalse(any("Не надано список останніх витрат" in t for t in sent))

    # 3 — the declined ("тестову каву") form still resolves, via the
    # existing Gemini router now correctly given the live candidate list.
    def test_declined_form_resolves_via_router_with_live_candidates(self):
        chat_id = 9765002
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(303, Decimal("51.23"), category="Кафе / ресторани", description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])) as mock_router:
                _call_webhook(_make_update(9765000002, chat_id, "Викресли тестову каву зі списку"))
                mock_router.assert_called_once()
                # The exact fix: recent_expenses is now passed as candidates.
                _, kwargs = mock_router.call_args
                call_args = mock_router.call_args.args
                passed_recent = kwargs.get("recent_expenses") if "recent_expenses" in kwargs else (
                    call_args[1] if len(call_args) > 1 else None
                )
                self.assertEqual(passed_recent, expenses_list)
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 303)

    # 5 — no DB write before confirm.
    def test_no_db_write_before_confirm(self):
        chat_id = 9765003
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(304, Decimal("51.23"), description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            with patch.object(bot, "delete_expense") as mock_delete:
                _call_webhook(_make_update(9765000003, chat_id, "Викресли тестова кава зі списку"))
                mock_delete.assert_not_called()

    # 6 — cancel leaves the expense untouched.
    def test_cancel_leaves_expense_untouched(self):
        chat_id = 9765004
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(305, Decimal("51.23"), description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            _call_webhook(_make_update(9765000004, chat_id, "Викресли тестова кава зі списку"))
        with patch.object(bot, "delete_expense") as mock_delete:
            _call_webhook(_make_update(9765000005, chat_id, "❌ Скасувати"))
            mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 7 — confirm deletes only the selected expense, others untouched.
    def test_confirm_deletes_only_selected_expense(self):
        chat_id = 9765005
        self._enter_expenses_context(chat_id)
        expenses_list = [
            _expense_dict(306, Decimal("51.23"), description="тестова кава"),
            _expense_dict(307, Decimal("60.00"), description="Подарунок доньці"),
            _expense_dict(308, Decimal("527.00"), description="Комод"),
        ]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            _call_webhook(_make_update(9765000006, chat_id, "Викресли тестова кава зі списку"))
        with patch.object(bot, "delete_expense", return_value=None) as mock_delete:
            _call_webhook(_make_update(9765000007, chat_id, "✅ Так, видалити"))
        mock_delete.assert_called_once()
        args, _ = mock_delete.call_args
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1], 306)
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 8/9 — the active-context resolver fetches LIVE expenses from the DB
    # (never text saved from the earlier menu render) and never surfaces the
    # old internal Gemini explanation to the user.
    def test_resolver_uses_live_db_candidates_not_stale_menu_text(self):
        chat_id = 9765006
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(309, Decimal("51.23"), description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list) as mock_get_recent:
            _call_webhook(_make_update(9765000008, chat_id, "Викресли тестова кава зі списку"))
        mock_get_recent.assert_called_once_with(1, limit=10)
        self.assertFalse(any("Не надано список останніх витрат" in t for t in self._sent_texts()))

    # 11 — multiple expenses with the identical description fall back to
    # the existing disambiguation/selection flow (never guesses).
    def test_multiple_same_description_uses_existing_disambiguation(self):
        chat_id = 9765007
        self._enter_expenses_context(chat_id)
        expenses_list = [
            _expense_dict(310, Decimal("51.23"), description="тестова кава"),
            _expense_dict(311, Decimal("14.00"), description="тестова кава"),
        ]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1, 2])):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(9765000009, chat_id, "Викресли тестова кава зі списку"))
                    mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertIn(chat_id, bot.expense_delete_selection)
        self.assertTrue(any("Яку витрату видалити?" in t for t in self._sent_texts()))

    # 12 — identical description, different amounts: the router (given the
    # live amount-annotated candidate list) picks the right one, never
    # confused by the shared description alone.
    def test_same_description_different_amounts_not_confused(self):
        chat_id = 9765008
        self._enter_expenses_context(chat_id)
        expenses_list = [
            _expense_dict(312, Decimal("51.23"), description="тестова кава"),
            _expense_dict(313, Decimal("14.00"), description="тестова кава"),
        ]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([2])):
                _call_webhook(_make_update(9765000010, chat_id, "Викресли тестова кава за 14 zł зі списку"))
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 313)

    # 13 — no matching candidate at all: controlled message, no guess.
    def test_no_matching_candidate_returns_controlled_message(self):
        chat_id = 9765009
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(314, Decimal("60.00"), description="Подарунок доньці")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([])):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(9765000011, chat_id, "Викресли неіснуючу витрату зі списку"))
                    mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        sent = self._sent_texts()
        self.assertFalse(any("Не надано список останніх витрат" in t for t in sent))
        self.assertTrue(any("Яку витрату видалити?" in t for t in sent))

    # 14 — empty expense list: controlled message, no crash.
    def test_empty_expense_list_returns_controlled_message(self):
        chat_id = 9765010
        self._enter_expenses_context(chat_id)
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=[]):
            with patch.object(bot, "delete_expense") as mock_delete:
                _call_webhook(_make_update(9765000012, chat_id, "Викресли тестова кава зі списку"))
                mock_delete.assert_not_called()
        self.assertTrue(any("Витрат поки немає." in t for t in self._sent_texts()))

    # 15 — a stale candidate (already gone by confirm time) blocks the
    # delete; existing stale-snapshot protection unchanged.
    def test_stale_candidate_after_preview_is_not_deleted(self):
        chat_id = 9765011
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(315, Decimal("51.23"), description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            _call_webhook(_make_update(9765000013, chat_id, "Викресли тестова кава зі списку"))
        original_stale_error = expenses.StaleSnapshotError
        expenses.StaleSnapshotError = real_database.StaleSnapshotError
        try:
            with patch.object(bot, "delete_expense", side_effect=expenses.StaleSnapshotError()):
                _call_webhook(_make_update(9765000014, chat_id, "✅ Так, видалити"))
        finally:
            expenses.StaleSnapshotError = original_stale_error
        self.assertTrue(any("Список змінився з іншого пристрою" in t for t in self._sent_texts()))
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 16 — the active-context delete flow itself has priority over general
    # AI-chat: a genuine delete-in-context message is fully handled by the
    # expense-delete resolver and never falls through to general_ai_
    # fallback's own call_gemini chat call (a plain unrelated "none"-intent
    # message inside this context falling through to general AI-chat is
    # separate, pre-existing, unchanged behavior — see _handle_expense_
    # command's own docstring on why "none" + origin=="expenses_menu" is the
    # one case allowed to fall through; not what this fix touches).
    def test_active_context_delete_flow_never_reaches_general_ai_chat(self):
        chat_id = 9765012
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(318, Decimal("51.23"), description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            _call_webhook(_make_update(9765000015, chat_id, "Викресли тестова кава зі списку"))
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense_delete)

    # 17 — global command outside expenses context still uses fix dbdd0f7,
    # unaffected by this change (no active expenses context set at all).
    def test_global_command_outside_context_still_uses_existing_fix(self):
        chat_id = 9765013
        expenses_list = [_expense_dict(316, Decimal("51.23"), description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])):
                _call_webhook(_make_update(9765000016, chat_id, "Скасуй ту покупку на 51,23 zł"))
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 316)

    # 18 — an add-expense command inside active context is unaffected (still
    # add, never delete).
    def test_add_expense_command_in_active_context_is_unaffected(self):
        chat_id = 9765014
        self._enter_expenses_context(chat_id)
        router_result = {
            "intent": "create_expense", "amount": "50.00", "currency": "PLN", "category": "Продукти",
            "description": "Покупка", "expense_date": "2026-07-14", "selected_numbers": [], "unresolved_fragments": [],
        }
        with patch.object(bot, "_ask_gemini_expense_router", return_value=router_result) as mock_router:
            _call_webhook(_make_update(9765000017, chat_id, "Запиши покупку на 50 zł"))
        mock_router.assert_called_once()
        self.assertIn(chat_id, bot.pending_expense)
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 19 — an explicit shopping-list command in active expenses context
    # never creates an expense-delete preview (domain-boundary guard).
    def test_explicit_shopping_list_command_does_not_create_expense_delete(self):
        chat_id = 9765015
        self._enter_expenses_context(chat_id)
        with patch.object(bot, "get_recent_expenses_for_deletion") as mock_get_recent:
            with patch.object(bot, "_try_shopping_action_planner", return_value=True) as mock_shopping_planner:
                _call_webhook(_make_update(9765000018, chat_id, "Викресли хліб зі списку покупок"))
                mock_shopping_planner.assert_called_once()
            mock_get_recent.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertNotIn(chat_id, bot.expense_delete_selection)

    # 20 — confirm/cancel buttons keep priority over the active expenses
    # context (an open OTHER preview is never reinterpreted as a new
    # delete command).
    def test_confirm_cancel_has_priority_over_active_expenses_context(self):
        chat_id = 9765016
        self._enter_expenses_context(chat_id)
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("10.00"),
            "currency": "PLN", "category": "Продукти", "description": "Хліб",
            "expense_date": date(2026, 7, 3), "origin": "expenses_menu",
        }
        with patch.object(bot, "add_expense", return_value=999) as mock_add:
            _call_webhook(_make_update(9765000019, chat_id, "✅ Так, додати"))
        mock_add.assert_called_once()
        self.assertNotIn(chat_id, bot.pending_expense)

    # 21 — the router/planner is invoked at most once per update for the
    # active-context delete path (declined-form case actually calls Gemini).
    def test_router_called_at_most_once_per_update(self):
        chat_id = 9765017
        self._enter_expenses_context(chat_id)
        expenses_list = [_expense_dict(317, Decimal("51.23"), description="тестова кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=expenses_list):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_delete_router_result([1])) as mock_router:
                _call_webhook(_make_update(9765000020, chat_id, "Викресли тестову каву зі списку"))
        self.assertEqual(mock_router.call_count, 1)


if __name__ == "__main__":
    unittest.main()
