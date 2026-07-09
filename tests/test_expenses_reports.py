import sys
import os
import importlib.util
import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes. This lets us exercise the actual
# get_recent_expenses()/get_expense_month_summary() SQL/parameterization
# directly, with a fake connection/cursor standing in for Postgres — no real
# Supabase involved.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_expense_reports_test", _database_path)
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
from bot import (
    _expense_report_gate,
    _format_recent_expenses,
    _format_expense_month_summary,
)


# =========================
# FakeCursor/FakeConnection — same shape as tests/test_expenses_v1.py, used
# to verify SQL shape/scoping/params without a real Postgres.
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


def _expense_row(amount, currency="PLN", category="Продукти", description="Тест",
                  expense_date=date(2026, 7, 3), created_at=None):
    return (amount, currency, category, description, expense_date, created_at or datetime(2026, 7, 3, 12, 0))


# =========================
# Case 1 — household isolation at the SQL layer
# =========================
class TestGetRecentExpensesHouseholdIsolation(unittest.TestCase):
    def test_recent_expenses_query_scoped_to_household_id(self):
        cursor = FakeCursor(fetchall_results=[[_expense_row(Decimal("10.00"))]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.get_recent_expenses(household_id=7)
        sql, params = cursor.queries[-1]
        self.assertIn("WHERE household_id = %s", sql)
        self.assertEqual(params[0], 7)

    def test_different_households_never_share_params(self):
        cursor1 = FakeCursor(fetchall_results=[[_expense_row(Decimal("5.00"))]])
        conn1 = FakeConnection(cursor1)
        with patch.object(real_database, "get_connection", return_value=conn1):
            real_database.get_recent_expenses(household_id=1)
        cursor2 = FakeCursor(fetchall_results=[[_expense_row(Decimal("5.00"))]])
        conn2 = FakeConnection(cursor2)
        with patch.object(real_database, "get_connection", return_value=conn2):
            real_database.get_recent_expenses(household_id=2)
        self.assertEqual(cursor1.queries[-1][1][0], 1)
        self.assertEqual(cursor2.queries[-1][1][0], 2)


# =========================
# Case 2 — limit is 10
# =========================
class TestGetRecentExpensesLimit(unittest.TestCase):
    def test_default_limit_is_10(self):
        cursor = FakeCursor(fetchall_results=[[]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.get_recent_expenses(household_id=1)
        sql, params = cursor.queries[-1]
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params[1], 10)

    def test_bot_report_handler_requests_limit_10(self):
        with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
            with patch.object(bot, "get_recent_expenses", return_value=[]) as mock_get:
                with patch.object(bot, "send_message"):
                    bot._handle_expense_report_command(940001, 555, "Тест", "recent")
        mock_get.assert_called_once_with(1, limit=10)


# =========================
# Case 3 — sorting of recent expenses is stable (SQL ORDER BY, never re-sorted by formatting)
# =========================
class TestRecentExpensesSortingStable(unittest.TestCase):
    def test_sql_orders_by_date_then_created_at_then_id_desc(self):
        cursor = FakeCursor(fetchall_results=[[]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.get_recent_expenses(household_id=1)
        sql, _ = cursor.queries[-1]
        self.assertIn("ORDER BY expense_date DESC, created_at DESC, id DESC", sql)

    def test_formatting_preserves_given_order_without_resorting(self):
        # Deliberately NOT in amount order — proves _format_recent_expenses
        # trusts the DB's ordering and never reshuffles by amount/date itself.
        expenses = [
            {"amount": Decimal("5.00"), "currency": "PLN", "category": "Транспорт",
             "description": "Автобус", "expense_date": date(2026, 7, 3), "created_at": None},
            {"amount": Decimal("86.40"), "currency": "PLN", "category": "Продукти",
             "description": "Biedronka", "expense_date": date(2026, 7, 2), "created_at": None},
        ]
        text = _format_recent_expenses(expenses)
        self.assertLess(text.index("Автобус"), text.index("Biedronka"))


# =========================
# Case 4 — sums via Decimal, exact (never float)
# =========================
class TestExactDecimalSums(unittest.TestCase):
    def test_recent_expenses_total_is_exact_decimal(self):
        expenses = [
            {"amount": Decimal("0.10"), "currency": "PLN", "category": "Інше",
             "description": "A", "expense_date": date(2026, 7, 3), "created_at": None},
            {"amount": Decimal("0.20"), "currency": "PLN", "category": "Інше",
             "description": "B", "expense_date": date(2026, 7, 3), "created_at": None},
        ]
        text = _format_recent_expenses(expenses)
        self.assertIn("Разом: 0,30 zł", text)

    def test_month_summary_total_is_exact_decimal(self):
        summary = {
            "total": Decimal("0.10") + Decimal("0.20"),
            "by_category": {"Інше": Decimal("0.30")},
        }
        text = _format_expense_month_summary(summary, 2026, 7)
        self.assertIn("Разом: 0,30 zł", text)

    def test_db_helper_sums_categories_as_decimal(self):
        # GROUP BY already sums at the SQL layer: one row per category.
        cursor = FakeCursor(fetchall_results=[[("Продукти", Decimal("86.40")), ("Транспорт", Decimal("5.00"))]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            summary = real_database.get_expense_month_summary(household_id=1, year=2026, month=7)
        self.assertEqual(summary["total"], Decimal("91.40"))
        self.assertIsInstance(summary["total"], Decimal)
        self.assertEqual(summary["by_category"]["Продукти"], Decimal("86.40"))


# =========================
# Case 5 — month summary never crosses into another month
# =========================
class TestMonthSummaryDateBounds(unittest.TestCase):
    def test_july_bounds_are_first_of_july_to_first_of_august(self):
        cursor = FakeCursor(fetchall_results=[[]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.get_expense_month_summary(household_id=1, year=2026, month=7)
        sql, params = cursor.queries[-1]
        self.assertIn("expense_date >= %s AND expense_date < %s", sql)
        self.assertEqual(params[1], date(2026, 7, 1))
        self.assertEqual(params[2], date(2026, 8, 1))

    def test_december_wraps_into_january_next_year(self):
        cursor = FakeCursor(fetchall_results=[[]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.get_expense_month_summary(household_id=1, year=2026, month=12)
        _, params = cursor.queries[-1]
        self.assertEqual(params[1], date(2026, 12, 1))
        self.assertEqual(params[2], date(2027, 1, 1))


# =========================
# Case 6 — category grouping/ordering
# =========================
class TestMonthSummaryCategoryOrdering(unittest.TestCase):
    def test_categories_sorted_by_amount_desc_then_name_asc(self):
        summary = {
            "total": Decimal("220.40"),
            "by_category": {
                "Дім і рахунки": Decimal("120.00"),
                "Продукти": Decimal("86.40"),
                "Кафе / ресторани": Decimal("14.00"),
            },
        }
        text = _format_expense_month_summary(summary, 2026, 7)
        lines = text.splitlines()
        idx_home = lines.index("Дім і рахунки — 120,00 zł")
        idx_food = lines.index("Продукти — 86,40 zł")
        idx_cafe = lines.index("Кафе / ресторани — 14,00 zł")
        self.assertLess(idx_home, idx_food)
        self.assertLess(idx_food, idx_cafe)

    def test_tie_amounts_sorted_by_name_ascending(self):
        summary = {
            "total": Decimal("20.00"),
            "by_category": {"Транспорт": Decimal("10.00"), "Побут": Decimal("10.00")},
        }
        text = _format_expense_month_summary(summary, 2026, 7)
        lines = text.splitlines()
        self.assertLess(lines.index("Побут — 10,00 zł"), lines.index("Транспорт — 10,00 zł"))

    def test_zero_amount_category_is_skipped(self):
        summary = {"total": Decimal("10.00"), "by_category": {"Продукти": Decimal("10.00"), "Інше": Decimal("0")}}
        text = _format_expense_month_summary(summary, 2026, 7)
        self.assertNotIn("Інше — ", text)


# =========================
# Case 7 — empty states
# =========================
class TestEmptyReportStates(unittest.TestCase):
    def test_no_recent_expenses_message(self):
        self.assertEqual(_format_recent_expenses([]), "Витрат поки немає.")

    def test_no_expenses_this_month_message(self):
        summary = {"total": Decimal("0"), "by_category": {}}
        text = _format_expense_month_summary(summary, 2026, 7)
        self.assertIn("Витрат за цей місяць поки немає.", text)
        self.assertIn("липень 2026", text)


# =========================
# Report gate — exact buttons and free-text equivalents, no Gemini
# =========================
class TestExpenseReportGate(unittest.TestCase):
    def test_recent_button_and_phrase(self):
        self.assertEqual(_expense_report_gate("🧾 Останні витрати"), "recent")
        self.assertEqual(_expense_report_gate("Покажи останні витрати"), "recent")

    def test_monthly_button_and_phrases(self):
        self.assertEqual(_expense_report_gate("📊 Цей місяць"), "monthly")
        self.assertEqual(_expense_report_gate("Підсумок за цей місяць"), "monthly")
        self.assertEqual(_expense_report_gate("Скільки витратили цього місяця"), "monthly")

    def test_ordinary_text_does_not_match(self):
        self.assertIsNone(_expense_report_gate("Що приготувати з курки?"))
        self.assertIsNone(_expense_report_gate(""))


# =========================
# Case 8 — report commands never interrupt an active pending preview
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


class TestReportsDoNotInterruptPendingPreview(unittest.TestCase):
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
        for d in (bot.pending_delete_batch, bot.pending_expense, bot.pending_alias_action,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    def test_recent_report_does_not_interrupt_other_pending_preview(self):
        chat_id = 940101
        bot.pending_delete_batch[chat_id] = {
            "items": [{"id": 1, "name": "Хліб"}], "household_id": 1, "user_db_id": 10,
        }
        with patch.object(bot, "get_recent_expenses") as mock_get_recent:
            _call_webhook(_make_update(940000001, chat_id, "🧾 Останні витрати"))
        mock_get_recent.assert_not_called()
        self.assertIn(chat_id, bot.pending_delete_batch)

    def test_monthly_report_does_not_interrupt_other_pending_preview(self):
        chat_id = 940102
        bot.pending_delete_batch[chat_id] = {
            "items": [{"id": 1, "name": "Хліб"}], "household_id": 1, "user_db_id": 10,
        }
        with patch.object(bot, "get_expense_month_summary") as mock_get_summary:
            _call_webhook(_make_update(940000002, chat_id, "Підсумок за цей місяць"))
        mock_get_summary.assert_not_called()
        self.assertIn(chat_id, bot.pending_delete_batch)

    def test_report_does_not_interrupt_pending_expense_add_preview(self):
        chat_id = 940103
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("14.00"), "currency": "PLN",
            "category": "Кафе / ресторани", "description": "Кава", "expense_date": date(2026, 7, 3),
            "origin": "global",
        }
        with patch.object(bot, "get_recent_expenses") as mock_get_recent:
            _call_webhook(_make_update(940000003, chat_id, "📊 Цей місяць"))
        mock_get_recent.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense)

    def test_report_works_normally_when_nothing_pending(self):
        chat_id = 940104
        with patch.object(bot, "get_recent_expenses", return_value=[]) as mock_get_recent:
            _call_webhook(_make_update(940000004, chat_id, "🧾 Останні витрати"))
        mock_get_recent.assert_called_once()
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Витрат поки немає." in t for t in sent_texts))


# =========================
# Expenses Hub V1 — get_expense_day_total's SQL/parameterization layer
# =========================
class TestGetExpenseDayTotal(unittest.TestCase):
    def test_sums_amount_as_exact_decimal_scoped_to_household_and_day(self):
        cursor = FakeCursor(fetchone_results=[(Decimal("134.00"),)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            total = real_database.get_expense_day_total(household_id=7, day=date(2026, 7, 3))
        self.assertEqual(total, Decimal("134.00"))
        self.assertIsInstance(total, Decimal)
        sql, params = cursor.queries[-1]
        self.assertIn("household_id = %s AND expense_date = %s", sql)
        self.assertEqual(params, (7, date(2026, 7, 3)))

    def test_no_expenses_that_day_returns_zero_not_none(self):
        cursor = FakeCursor(fetchone_results=[(None,)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            total = real_database.get_expense_day_total(household_id=1, day=date(2026, 7, 3))
        self.assertEqual(total, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
