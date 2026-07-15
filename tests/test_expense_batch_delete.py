"""Expense Batch Delete V1 — focused coverage for structured multi-target
expense deletion.

Live bug this fixes: expense list had "Тест кава batch — 51,23 zł" and
"Тест еспресо batch — 14,00 zł" ("Тест чай batch" never existed as an
expense — a separate context bug had added it to shopping as "52,37 шт"
instead). "Видали тест чай batch, тест кава batch і тест еспресо batch" and
"Видали тест кава batch і тест еспресо batch" both failed to build a batch
preview — instead the bot showed "Не зміг однозначно визначити витрату" with
the FULL unfiltered recent-expenses list (comod, dytiache lizhechko, a gift
for the daughter — none of them relevant to the command at all).

Root cause: the existing expense-delete schema/pending-state is single-
target only, and the ambiguous-selection fallback showed the whole recent
list with no relevance filtering.

Fix (see expenses.py's "EXPENSE BATCH DELETE V1" section):
  * EXPENSE_ROUTER_PROMPT's delete_expense response gained a `targets` array
    (1-10 {description, amount, date_hint} objects extracted straight from
    the user's own message, no DB ids) — still ONE Gemini call.
  * _resolve_expense_targets resolves each target against LIVE expenses,
    claiming a row for at most one target (never double-used).
  * All-or-nothing: any missing target blocks the WHOLE batch (no partial
    delete); any ambiguous target shows ONLY its own relevance-filtered
    candidates, never unrelated recent expenses.
  * 2+ resolved targets -> a new pending_expense_batch_delete preview,
    confirmed/cancelled through the SAME "✅ Так, видалити"/"❌ Скасувати"
    buttons, deleted atomically via database.delete_expenses_batch (one
    transaction, every row re-verified/locked before anything is deleted).
  * A single resolved target still folds into the EXISTING pending_expense_
    delete/_build_delete_preview_from_match path, unchanged.
  * The existing single-delete ambiguous-fallback (_resolve_expense_delete_
    selection) is now ALSO relevance-filtered instead of showing the full
    recent list (see the updated test_no_matching_candidate_returns_
    controlled_message in tests/test_expense_delete.py).

No DB schema change. No expense-delete undo in this fix — database.
delete_expense/delete_expenses_batch still don't write a household_action_
journal row, exactly like before (see docs/PROJECT_STATE.md).

No real Gemini/Telegram/Supabase call happens anywhere in this file —
database is mocked at import time (webhook-level classes) or loaded fresh
under its own module name (DB-layer class, same trick as
tests/test_expense_delete.py), every Gemini-facing bot.py function is
patched per-test.
"""
import sys
import os
import importlib.util
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_expense_batch_delete_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import expenses  # noqa: E402
import voice_input  # noqa: E402


# =========================
# FakeCursor/FakeConnection — same shape as tests/test_expense_delete.py.
# =========================
class FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.queries = []
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])
        self.rowcount = 0

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


def _target(description="", amount=None, date_hint=None):
    """Builds a RAW (pre-Python-validation) target dict — the same shape
    _ask_gemini_expense_router hands to _validate_expense_delete_targets.
    `amount` is accepted as a Decimal/int/float/str for test convenience but
    always stored as a str, matching the real router's own JSON contract
    (EXPENSE_ROUTER_PROMPT: "amount — сума як рядок") — _parse_expense_amount
    only accepts int/float/str, never Decimal, directly."""
    return {
        "description": description,
        "amount": str(amount) if amount is not None else None,
        "date_hint": date_hint,
    }


def _targets_router_result(raw_targets, unresolved_fragments=None):
    return {
        "intent": "delete_expense", "amount": None, "currency": None, "category": None,
        "description": None, "expense_date": None,
        "targets": raw_targets, "selected_numbers": [],
        "unresolved_fragments": unresolved_fragments or [],
    }


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _make_voice_update(update_id, chat_id, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "voice": {"file_id": "voice_1", "duration": 4, "mime_type": "audio/ogg"},
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


# =========================
# DB layer — delete_expenses_batch / _verify_expense_targets_in_tx.
# Tests 20/21 (stale row / error rolls back the WHOLE batch).
# =========================
class TestDeleteExpensesBatchDbLayer(unittest.TestCase):
    def _snapshots(self):
        return [
            {"id": 401, "amount": Decimal("51.23"), "category": "Кафе / ресторани",
             "expense_date": date(2026, 7, 3), "description": "Тест кава batch"},
            {"id": 402, "amount": Decimal("14.00"), "category": "Кафе / ресторани",
             "expense_date": date(2026, 7, 3), "description": "Тест еспресо batch"},
        ]

    def test_deletes_all_rows_in_one_transaction_when_snapshots_match(self):
        cursor = FakeCursor(fetchall_results=[[
            (401, Decimal("51.23"), "Кафе / ресторани", date(2026, 7, 3), "Тест кава batch"),
            (402, Decimal("14.00"), "Кафе / ресторани", date(2026, 7, 3), "Тест еспресо batch"),
        ]])
        cursor.rowcount = 2
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.delete_expenses_batch(1, [401, 402], self._snapshots())
        self.assertEqual(count, 2)
        self.assertTrue(conn.committed)
        delete_queries = [q for q in cursor.queries if q[0].strip().startswith("DELETE")]
        self.assertEqual(len(delete_queries), 1)

    # 20 — one stale row rolls back the WHOLE batch, nothing deleted.
    def test_one_stale_row_rolls_back_whole_batch(self):
        cursor = FakeCursor(fetchall_results=[[
            (401, Decimal("51.23"), "Кафе / ресторани", date(2026, 7, 3), "Тест кава batch"),
            # 402's amount changed since the preview was built (14.00 -> 20.00)
            (402, Decimal("20.00"), "Кафе / ресторани", date(2026, 7, 3), "Тест еспресо batch"),
        ]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_expenses_batch(1, [401, 402], self._snapshots())
        self.assertFalse(conn.committed)
        delete_queries = [q for q in cursor.queries if q[0].strip().startswith("DELETE")]
        self.assertEqual(len(delete_queries), 0)

    def test_missing_row_raises_stale_and_deletes_nothing(self):
        cursor = FakeCursor(fetchall_results=[[
            (401, Decimal("51.23"), "Кафе / ресторани", date(2026, 7, 3), "Тест кава batch"),
            # 402 no longer exists at all
        ]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_expenses_batch(1, [401, 402], self._snapshots())
        self.assertFalse(conn.committed)

    def test_scoped_to_household_id(self):
        cursor = FakeCursor(fetchall_results=[[
            (401, Decimal("51.23"), "Кафе / ресторани", date(2026, 7, 3), "Тест кава batch"),
            (402, Decimal("14.00"), "Кафе / ресторани", date(2026, 7, 3), "Тест еспресо batch"),
        ]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.delete_expenses_batch(1, [401, 402], self._snapshots())
        select_query = cursor.queries[0]
        self.assertIn(1, select_query[1])
        delete_query = [q for q in cursor.queries if q[0].strip().startswith("DELETE")][0]
        self.assertIn(1, delete_query[1])


# =========================
# Pure resolution logic — no Gemini, no DB, no webhook.
# =========================
class TestResolveExpenseTargets(unittest.TestCase):
    def setUp(self):
        self.kava_batch = _expense_dict(201, Decimal("51.23"), category="Кафе / ресторани", description="Тест кава batch")
        self.espresso_batch = _expense_dict(202, Decimal("14.00"), category="Кафе / ресторани", description="Тест еспресо batch")
        self.gift = _expense_dict(203, Decimal("60.00"), description="Подарунок доньці")
        self.comod = _expense_dict(204, Decimal("527.00"), description="Комод")
        self.crib = _expense_dict(205, Decimal("300.00"), description="Дитяче ліжечко")
        self.candidates = [self.kava_batch, self.espresso_batch, self.gift, self.comod, self.crib]

    # 8/9/10 — one missing target blocks the whole batch; the message names
    # exactly the missing target and lists the found-but-undeleted ones.
    def test_missing_target_blocks_whole_batch_and_is_named(self):
        targets = [_target("тест чай batch"), _target("тест кава batch"), _target("тест еспресо batch")]
        result = expenses._resolve_expense_targets(targets, self.candidates)
        self.assertEqual(result["missing"], ["тест чай batch"])
        self.assertEqual([e["id"] for e in result["resolved"]], [201, 202])
        self.assertEqual(result["ambiguous"], [])

    # 2/7 — two targets both resolve -> batch of two, unrelated expenses
    # excluded from the resolved set entirely.
    def test_two_targets_both_resolve(self):
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        result = expenses._resolve_expense_targets(targets, self.candidates)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual([e["id"] for e in result["resolved"]], [201, 202])

    # 11 — unrelated expenses (gift/comod/crib) never appear as candidates
    # for a target that doesn't mention them at all.
    def test_unrelated_expenses_never_become_candidates(self):
        status, result = expenses._resolve_single_target("тест кава batch", None, None, self.candidates)
        self.assertEqual(status, "found")
        self.assertEqual(result["id"], 201)

    # 12 — an ambiguous target only surfaces its OWN relevant candidates.
    def test_ambiguous_target_filtered_to_relevant_candidates_only(self):
        kava1 = _expense_dict(301, Decimal("14.00"), description="Кава")
        kava2 = _expense_dict(302, Decimal("18.00"), description="Кава")
        pool = [kava1, kava2, self.gift, self.comod]
        status, result = expenses._resolve_single_target("кава", None, None, pool)
        self.assertEqual(status, "ambiguous")
        self.assertEqual({e["id"] for e in result}, {301, 302})

    # 13 — an explicit amount alongside the description picks the right row.
    def test_description_plus_amount_picks_correct_row(self):
        kava1 = _expense_dict(301, Decimal("14.00"), description="Кава")
        kava2 = _expense_dict(302, Decimal("18.00"), description="Кава")
        status, result = expenses._resolve_single_target("кава", Decimal("14.00"), None, [kava1, kava2])
        self.assertEqual(status, "found")
        self.assertEqual(result["id"], 301)

    # 14 — identical description+amount on different dates still needs
    # clarification (no date_hint given to disambiguate).
    def test_same_description_and_amount_different_dates_still_ambiguous(self):
        kava_mon = _expense_dict(301, Decimal("14.00"), description="Кава", expense_date=date(2026, 7, 1))
        kava_tue = _expense_dict(302, Decimal("14.00"), description="Кава", expense_date=date(2026, 7, 2))
        status, result = expenses._resolve_single_target("кава", Decimal("14.00"), None, [kava_mon, kava_tue])
        self.assertEqual(status, "ambiguous")
        self.assertEqual({e["id"] for e in result}, {301, 302})

    # 14b — a date_hint that matches only one row resolves it.
    def test_date_hint_disambiguates_identical_description_and_amount(self):
        kava_mon = _expense_dict(301, Decimal("14.00"), description="Кава", expense_date=date(2026, 7, 1))
        kava_tue = _expense_dict(302, Decimal("14.00"), description="Кава", expense_date=date(2026, 7, 2))
        status, result = expenses._resolve_single_target("кава", Decimal("14.00"), date(2026, 7, 2), [kava_mon, kava_tue])
        self.assertEqual(status, "found")
        self.assertEqual(result["id"], 302)

    # 15 — a row already claimed by an earlier target is never reused by a
    # later target in the same batch, even if it would otherwise match.
    def test_row_never_claimed_by_two_targets(self):
        kava = _expense_dict(301, Decimal("14.00"), description="Кава")
        targets = [_target("кава"), _target("кава")]
        result = expenses._resolve_expense_targets(targets, [kava])
        self.assertEqual([e["id"] for e in result["resolved"]], [301])
        self.assertEqual(result["missing"], ["кава"])


# =========================
# Prompt content — comma/і/та/а також segmentation guidance (test 4) and the
# Python-side allowlist rejecting unknown target fields.
# =========================
class TestBatchDeleteTargetValidation(unittest.TestCase):
    def test_prompt_documents_conjunction_splitting(self):
        for marker in ("Кома", "«і»", "«та»", "«а також»"):
            self.assertIn(marker, expenses.EXPENSE_ROUTER_PROMPT)

    def test_prompt_never_asks_gemini_for_a_db_id(self):
        self.assertNotIn("\"id\"", expenses.EXPENSE_ROUTER_PROMPT)

    def test_unknown_target_field_rejects_whole_targets_array(self):
        raw = [{"description": "кава", "amount": None, "date_hint": None, "id": 999}]
        self.assertIsNone(expenses._validate_expense_delete_targets(raw))

    def test_too_many_targets_rejected(self):
        raw = [{"description": f"item{i}"} for i in range(11)]
        self.assertIsNone(expenses._validate_expense_delete_targets(raw))

    def test_empty_targets_list_rejected(self):
        self.assertIsNone(expenses._validate_expense_delete_targets([]))

    def test_target_with_neither_description_nor_amount_rejected(self):
        raw = [{"description": "", "amount": None, "date_hint": None}]
        self.assertIsNone(expenses._validate_expense_delete_targets(raw))

    def test_amount_only_target_accepted(self):
        raw = [{"description": "", "amount": "14.00", "date_hint": None}]
        validated = expenses._validate_expense_delete_targets(raw)
        self.assertEqual(validated, [{"description": "", "amount": Decimal("14.00"), "date_hint": None}])


# =========================
# Webhook-level: the exact live sequence, all-or-nothing preview,
# confirm/cancel, atomicity, priority, and non-regression of adjacent flows.
# =========================
class TestExpenseBatchDeleteWebhookFlow(unittest.TestCase):
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

        self.kava_batch = _expense_dict(201, Decimal("51.23"), category="Кафе / ресторани", description="Тест кава batch")
        self.espresso_batch = _expense_dict(202, Decimal("14.00"), category="Кафе / ресторани", description="Тест еспресо batch")
        self.gift = _expense_dict(203, Decimal("60.00"), description="Подарунок доньці")
        self.comod = _expense_dict(204, Decimal("527.00"), description="Комод")
        self.crib = _expense_dict(205, Decimal("300.00"), description="Дитяче ліжечко")
        self.candidates = [self.kava_batch, self.espresso_batch, self.gift, self.comod, self.crib]

    def tearDown(self):
        for d in (bot.pending_expense_delete, bot.expense_delete_selection, bot.pending_expense_batch_delete,
                  bot.pending_expense, bot.pending_delete_batch, bot.pending_alias_action,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # 7 — exact live case with two EXISTING expenses: full batch preview.
    def test_two_existing_targets_build_batch_preview(self):
        chat_id = 973101
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)) as mock_router:
                _call_webhook(_make_update(973100001, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        mock_router.assert_called_once()
        self.assertIn(chat_id, bot.pending_expense_batch_delete)
        self.assertEqual(set(bot.pending_expense_batch_delete[chat_id]["expense_ids"]), {201, 202})
        self.assertEqual(bot.pending_expense_batch_delete[chat_id]["total"], Decimal("65.23"))
        sent = self._sent_texts()
        self.assertTrue(any("Видалити витрати?" in t for t in sent))
        self.assertTrue(any("Разом: 65,23 zł" in t for t in sent))
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 3 — three targets build a batch preview when all three exist.
    def test_three_targets_all_resolve_build_batch_preview(self):
        chat_id = 973102
        tea_batch = _expense_dict(206, Decimal("30.00"), category="Кафе / ресторани", description="Тест чай batch")
        candidates = self.candidates + [tea_batch]
        targets = [_target("тест чай batch"), _target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100002, chat_id, "Видали витрати тест чай batch, тест кава batch і тест еспресо batch"))
        self.assertIn(chat_id, bot.pending_expense_batch_delete)
        self.assertEqual(set(bot.pending_expense_batch_delete[chat_id]["expense_ids"]), {201, 202, 206})

    # 8/9/10 — exact live case: one missing, two existing -> no preview at
    # all, no partial write, and the response names the missing target.
    def test_one_missing_two_existing_no_preview_no_write(self):
        chat_id = 973103
        targets = [_target("тест чай batch"), _target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                with patch.object(bot, "delete_expense") as mock_delete:
                    with patch.object(bot, "delete_expenses_batch") as mock_batch_delete:
                        _call_webhook(_make_update(973100003, chat_id, "Видали витрати тест чай batch, тест кава batch і тест еспресо batch"))
                    mock_delete.assert_not_called()
                    mock_batch_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        sent = self._sent_texts()
        self.assertTrue(any("Не вдалося підготувати видалення всіх витрат" in t for t in sent))
        self.assertTrue(any("тест чай batch" in t for t in sent))
        self.assertTrue(any("Тест кава batch" in t and "51,23 zł" in t for t in sent))
        self.assertTrue(any("Тест еспресо batch" in t and "14,00 zł" in t for t in sent))
        # 11 — unrelated expenses never appear in the missing-target message.
        self.assertFalse(any("Комод" in t for t in sent))
        self.assertFalse(any("Подарунок доньці" in t for t in sent))
        self.assertFalse(any("ліжечко" in t for t in sent))

    # 6 — an explicit "кава 14 zł"-style description+amount target resolves
    # the correct amount even when a same-named row with a different
    # amount also exists.
    def test_description_plus_amount_target_selects_correct_row(self):
        chat_id = 973104
        kava_14 = _expense_dict(301, Decimal("14.00"), description="Кава")
        kava_18 = _expense_dict(302, Decimal("18.00"), description="Кава")
        targets = [_target("кава", amount=Decimal("14.00"))]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=[kava_14, kava_18]):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100004, chat_id, "Видали витрату за каву 14 zł"))
        self.assertIn(chat_id, bot.pending_expense_delete)
        self.assertEqual(bot.pending_expense_delete[chat_id]["expense_id"], 301)
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)

    # 12 — an ambiguous target shows ONLY its own relevant candidates.
    def test_ambiguous_target_shows_only_relevant_candidates(self):
        chat_id = 973105
        kava_14 = _expense_dict(301, Decimal("14.00"), description="Кава")
        kava_18 = _expense_dict(302, Decimal("18.00"), description="Кава")
        candidates = [kava_14, kava_18, self.comod, self.crib]
        targets = [_target("кава")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                with patch.object(bot, "delete_expense") as mock_delete:
                    _call_webhook(_make_update(973100005, chat_id, "Видали витрату каву"))
                    mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_delete)
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)
        sent = self._sent_texts()
        self.assertTrue(any("Потрібно уточнити" in t and "14,00 zł" in t and "18,00 zł" in t for t in sent))
        self.assertFalse(any("Комод" in t for t in sent))
        self.assertFalse(any("ліжечко" in t for t in sent))

    # 16 — no DB write of any kind before confirm.
    def test_no_db_write_before_confirm(self):
        chat_id = 973106
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                with patch.object(bot, "delete_expense") as mock_delete:
                    with patch.object(bot, "delete_expenses_batch") as mock_batch_delete:
                        _call_webhook(_make_update(973100006, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
                    mock_delete.assert_not_called()
                    mock_batch_delete.assert_not_called()

    # 17 — cancel leaves every expense untouched.
    def test_cancel_leaves_all_expenses_untouched(self):
        chat_id = 973107
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100007, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        with patch.object(bot, "delete_expenses_batch") as mock_batch_delete:
            _call_webhook(_make_update(973100008, chat_id, "❌ Скасувати"))
            mock_batch_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)
        self.assertTrue(any("Видалення витрат скасовано." in t for t in self._sent_texts()))

    # 18/19 — confirm deletes exactly the selected expenses, atomically, and
    # nothing else.
    def test_confirm_deletes_exactly_the_batch(self):
        chat_id = 973108
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100009, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        with patch.object(bot, "delete_expenses_batch", return_value=2) as mock_batch_delete:
            _call_webhook(_make_update(973100010, chat_id, "✅ Так, видалити"))
        mock_batch_delete.assert_called_once()
        args, _ = mock_batch_delete.call_args
        self.assertEqual(args[0], 1)
        self.assertEqual(set(args[1]), {201, 202})
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)
        self.assertTrue(any("Видалено витрат: 2" in t for t in self._sent_texts()))

    # 20 — a stale row at confirm time rolls back the whole batch (webhook
    # surfaces the same STALE_PREVIEW_MSG the single-delete flow already uses).
    def test_stale_row_at_confirm_rolls_back_whole_batch(self):
        chat_id = 973109
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100011, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        original_stale_error = expenses.StaleSnapshotError
        expenses.StaleSnapshotError = real_database.StaleSnapshotError
        try:
            with patch.object(bot, "delete_expenses_batch", side_effect=expenses.StaleSnapshotError()):
                _call_webhook(_make_update(973100012, chat_id, "✅ Так, видалити"))
        finally:
            expenses.StaleSnapshotError = original_stale_error
        self.assertTrue(any("Список змінився з іншого пристрою" in t for t in self._sent_texts()))
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)

    # 21 — a generic DB error also aborts the whole batch (controlled
    # message, pending state cleared, no partial state left behind).
    def test_db_error_at_confirm_aborts_whole_batch(self):
        chat_id = 973110
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100013, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        # expenses.py does `from database import StaleSnapshotError` itself
        # (never through the injected _bot) — its except clause checks
        # against expenses.StaleSnapshotError, which is a plain MagicMock
        # attribute (not a real exception class) while `database` is
        # globally mocked in this file; swap in the real class so `except
        # StaleSnapshotError:` is valid Python and a generic Exception
        # correctly falls through to the `except Exception:` branch below it
        # — same pattern test_stale_row_at_confirm_rolls_back_whole_batch uses.
        original_stale_error = expenses.StaleSnapshotError
        expenses.StaleSnapshotError = real_database.StaleSnapshotError
        try:
            with patch.object(bot, "delete_expenses_batch", side_effect=Exception("boom")):
                _call_webhook(_make_update(973100014, chat_id, "✅ Так, видалити"))
        finally:
            expenses.StaleSnapshotError = original_stale_error
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)
        self.assertTrue(any("Не вдалося видалити витрати" in t for t in self._sent_texts()))

    # 22 — a repeated confirm never deletes the batch twice.
    def test_repeated_confirm_does_not_delete_twice(self):
        chat_id = 973111
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100015, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        with patch.object(bot, "delete_expenses_batch", return_value=2) as mock_batch_delete:
            _call_webhook(_make_update(973100016, chat_id, "✅ Так, видалити"))
            _call_webhook(_make_update(973100017, chat_id, "✅ Так, видалити"))
            mock_batch_delete.assert_called_once()
        self.assertTrue(any("Немає активної дії для підтвердження." in t for t in self._sent_texts()))

    # 23 — the active "💸 Витрати" context also supports batch delete
    # (routes through the SAME _handle_expense_delete_global_command).
    def test_active_expenses_context_supports_batch(self):
        chat_id = 973112
        bot.active_list_context[chat_id] = "expenses"
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100018, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        self.assertIn(chat_id, bot.pending_expense_batch_delete)

    # 24 — the global expense-delete command (outside any context) supports
    # batch delete too.
    def test_global_command_outside_context_supports_batch(self):
        chat_id = 973113
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100019, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        self.assertIn(chat_id, bot.pending_expense_batch_delete)

    # 25 — a genuine shopping-list phrase is never captured by the widened
    # delete flow (domain-boundary guard unchanged).
    def test_shopping_intent_not_intercepted(self):
        chat_id = 973114
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            _call_webhook(_make_update(973100020, chat_id, "Викресли хліб зі списку покупок"))
        mock_router.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 26 — an add-expense phrase never becomes a delete (no delete verb).
    def test_add_expense_not_treated_as_delete(self):
        chat_id = 973115
        with patch.object(bot, "_ask_gemini_expense_router",
                           return_value={
                               "intent": "create_expense", "amount": "51.23", "currency": "PLN",
                               "category": "Кафе / ресторани", "description": "Тест кава batch",
                               "expense_date": "2026-07-03", "targets": [], "selected_numbers": [],
                               "unresolved_fragments": [],
                           }):
            _call_webhook(_make_update(973100021, chat_id, "Тест кава batch 51,23 zł"))
        self.assertIn(chat_id, bot.pending_expense)
        self.assertNotIn(chat_id, bot.pending_expense_batch_delete)
        self.assertNotIn(chat_id, bot.pending_expense_delete)

    # 19 (continued) — confirming a batch never touches an unrelated
    # expense that wasn't part of the targets.
    def test_confirm_batch_never_touches_unrelated_expense(self):
        chat_id = 973116
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=self.candidates):
            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                _call_webhook(_make_update(973100022, chat_id, "Видали витрати тест кава batch і тест еспресо batch"))
        with patch.object(bot, "delete_expenses_batch", return_value=2) as mock_batch_delete:
            _call_webhook(_make_update(973100023, chat_id, "✅ Так, видалити"))
        ids_deleted = set(mock_batch_delete.call_args.args[1])
        self.assertNotIn(203, ids_deleted)  # gift
        self.assertNotIn(204, ids_deleted)  # comod
        self.assertNotIn(205, ids_deleted)  # crib


# =========================
# 5 — voice transcript goes through the exact same message_dispatcher.
# dispatch() path, so it gets the same batch-delete resolution as typed text.
# =========================
class TestVoiceTranscriptBatchDelete(unittest.TestCase):
    def setUp(self):
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        for d in (bot.pending_expense_delete, bot.expense_delete_selection, bot.pending_expense_batch_delete,
                  bot.pending_expense, bot.active_list_context, bot.saved_list_context):
            d.clear()

    def test_voice_transcript_builds_batch_preview(self):
        chat_id = 973201
        kava_batch = _expense_dict(201, Decimal("51.23"), category="Кафе / ресторани", description="Тест кава batch")
        espresso_batch = _expense_dict(202, Decimal("14.00"), category="Кафе / ресторани", description="Тест еспресо batch")
        targets = [_target("тест кава batch"), _target("тест еспресо batch")]
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/batch1.oga"):
            with patch.object(voice_input, "transcribe_audio_file",
                               return_value="Видали витрати тест кава batch і тест еспресо batch"):
                with patch("os.remove"):
                    with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
                        with patch.object(bot, "get_recent_expenses_for_deletion", return_value=[kava_batch, espresso_batch]):
                            with patch.object(bot, "_ask_gemini_expense_router", return_value=_targets_router_result(targets)):
                                _call_webhook(_make_voice_update(973200001, chat_id))
        self.assertIn(chat_id, bot.pending_expense_batch_delete)
        self.assertEqual(set(bot.pending_expense_batch_delete[chat_id]["expense_ids"]), {201, 202})


if __name__ == "__main__":
    unittest.main()
