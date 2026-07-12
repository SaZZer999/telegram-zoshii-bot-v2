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
# apply_global_household_operations() SQL/transaction shape directly, with a
# fake connection/cursor standing in for Postgres — no real Supabase
# involved. Same pattern as tests/test_expense_delete.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_global_household_ops_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No real Gemini/Telegram/Supabase
# call happens anywhere in this file — every network-facing function is
# patched per-test.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
import household_router
import expenses
from bot import (
    pending_global_household,
    pending_delete_batch,
    shopping_mode,
    expense_delete_selection,
    pending_expense,
    pending_expense_delete,
    active_list_context,
    saved_list_context,
    STALE_PREVIEW_MSG,
    SHOPPING_KEYBOARD,
    INVENTORY_KEYBOARD,
    EXPENSES_KEYBOARD,
    MAIN_KEYBOARD,
)


def _todays_warsaw_date_iso():
    """Real current Europe/Warsaw date — used as expense_date for
    webhook-level tests, which validate against the real clock (no `now`
    override reaches the router's validation through the full webhook
    path). Keeps those tests correct regardless of which day they run on."""
    return datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()


# =========================
# FakeCursor/FakeConnection — same shape as tests/test_expense_delete.py,
# used to verify SQL shape/scoping/params without a real Postgres.
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
            # One id per placeholder group in every test here — good enough
            # for asserting "N rows affected" without a real Postgres.
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


# =========================
# DB-layer: apply_global_household_operations
# =========================
class TestApplyGlobalHouseholdOperationsDbLayer(unittest.TestCase):
    def test_new_items_and_new_expense_inserted_in_one_transaction(self):
        cursor = FakeCursor(
            fetchall_results=[[], []],  # merge-check SELECTs for the shopping item and the inventory item
            fetchone_results=[(555,)],  # expense INSERT ... RETURNING id
        )
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.apply_global_household_operations(
                household_id=1, user_db_id=10,
                add_shopping_items=[_new_item("Булочка", "Хліб і випічка")],
                add_inventory_items=[_new_item("Масло")],
                new_expense={
                    "amount": Decimal("10.00"), "currency": "PLN", "category": "Продукти",
                    "description": "Масло", "expense_date": date(2026, 7, 5),
                },
            )
        self.assertTrue(conn.committed)
        self.assertEqual(result["shopping_added"], 1)
        self.assertEqual(result["inventory_added"], 1)
        self.assertEqual(result["expense_added_id"], 555)
        self.assertFalse(result["expense_deleted"])
        # Verify + inserts happened, no expense-delete SELECT at all.
        self.assertTrue(any("INSERT INTO expenses" in sql for sql, _ in cursor.queries))
        self.assertTrue(any("INSERT INTO shopping_items" in sql for sql, _ in cursor.queries))
        self.assertTrue(any("INSERT INTO inventory_items" in sql for sql, _ in cursor.queries))

    def test_stale_inventory_target_aborts_before_any_write(self):
        cursor = FakeCursor(fetchall_results=[[(501, 10.0, "шт.")]])  # live value (10) != snapshot (14)
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
        # Only the verify SELECT ran — the shopping insert never got a chance to fire.
        self.assertEqual(len(cursor.queries), 1)
        self.assertIn("FOR UPDATE", cursor.queries[0][0])

    def test_stale_expense_delete_target_aborts_before_any_write(self):
        cursor = FakeCursor(fetchone_results=[None])  # row already gone
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_global_household_operations(
                    household_id=1, user_db_id=10,
                    add_shopping_items=[_new_item("Булочка", "Хліб і випічка")],
                    delete_expense_id=999, delete_expense_snapshot=snapshot,
                )
        self.assertFalse(conn.committed)
        self.assertEqual(len(cursor.queries), 1)
        self.assertIn("FOR UPDATE", cursor.queries[0][0])

    def test_stale_expense_delete_amount_mismatch_aborts(self):
        cursor = FakeCursor(fetchone_results=[(Decimal("99.00"), "PLN", "Продукти", date(2026, 7, 3), "Булочка", 10)])
        conn = FakeConnection(cursor)
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_global_household_operations(
                    household_id=1, user_db_id=10, delete_expense_id=999, delete_expense_snapshot=snapshot,
                )
        self.assertFalse(conn.committed)

    def test_consume_updates_and_deletes_and_expense_delete_together(self):
        cursor = FakeCursor(
            fetchall_results=[[(501, 14.0, "шт."), (502, 1.0, "шт.")]],  # inventory targets verify
            fetchone_results=[
                (Decimal("4.00"), "PLN", "Продукти", date(2026, 7, 3), "Булочка", 10),  # expense verify
                (501,),  # consume UPDATE ... RETURNING id
            ],
        )
        conn = FakeConnection(cursor)
        targets = [
            {"item_id": 501, "quantity_value": 14.0, "quantity_unit": "шт."},
            {"item_id": 502, "quantity_value": 1.0, "quantity_unit": "шт."},
        ]
        snapshot = {"amount": Decimal("4.00"), "category": "Продукти",
                    "expense_date": date(2026, 7, 3), "description": "Булочка"}
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.apply_global_household_operations(
                household_id=1, user_db_id=10,
                consume_updates=[{"item_id": 501, "quantity_value": 12.0, "quantity_unit": "шт.", "quantity_text": "12 шт."}],
                consume_delete_ids=[502],
                inventory_targets=targets,
                delete_expense_id=101, delete_expense_snapshot=snapshot,
            )
        self.assertTrue(conn.committed)
        self.assertEqual(result["inventory_updated"], 1)
        self.assertEqual(result["inventory_removed"], 1)
        self.assertTrue(result["expense_deleted"])
        self.assertTrue(any("DELETE FROM inventory_items" in sql for sql, _ in cursor.queries))
        self.assertTrue(any("DELETE FROM expenses" in sql for sql, _ in cursor.queries))


# =========================
# Webhook-level dispatch
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


def _compound_router_result():
    return {
        "intent": "household_operations",
        "operations": [
            {"type": "add_inventory", "name": "Масло", "quantity_text": "", "category": "Молочне та яйця"},
            {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
             "description": "Масло", "expense_date": _todays_warsaw_date_iso()},
        ],
        "unresolved_fragments": [],
    }


class TestGlobalHouseholdRouterWebhookFlow(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_shopping_items = patch.object(bot, "get_active_shopping_items", return_value=[])
        self.mock_shopping_items = patcher_shopping_items.start()
        self.addCleanup(patcher_shopping_items.stop)

        patcher_inventory_items = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory_items = patcher_inventory_items.start()
        self.addCleanup(patcher_inventory_items.stop)

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

        patcher_saved_router = patch.object(bot, "_ask_gemini_saved_list_router")
        self.mock_saved_router = patcher_saved_router.start()
        self.addCleanup(patcher_saved_router.stop)

        patcher_household_router = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_household_router = patcher_household_router.start()
        self.addCleanup(patcher_household_router.stop)

    def tearDown(self):
        for d in (pending_global_household, pending_delete_batch, shopping_mode,
                  expense_delete_selection, pending_expense, pending_expense_delete,
                  active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 1
    def test_bought_with_price_from_main_menu_builds_compound_preview(self):
        chat_id = 970001
        self.mock_household_router.return_value = _compound_router_result()
        _call_webhook(_make_update(970000001, chat_id, "Купив масло за 10 zł"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertIsNotNone(data["new_expense"])
        texts = self._sent_texts()
        self.assertTrue(any("🧊 Запаси" in t and "💸 Витрати" in t for t in texts))
        self.assertTrue(any("✅ Так, застосувати" in t for t in texts))

    # Case 2 — same phrase from shopping/inventory/expenses menus builds the same compound preview
    def test_bought_with_price_from_every_menu_builds_same_compound_preview(self):
        for i, ctx in enumerate(("shopping", "inventory", "expenses")):
            with self.subTest(ctx=ctx):
                chat_id = 970100 + i
                active_list_context[chat_id] = ctx
                self.mock_household_router.return_value = _compound_router_result()
                _call_webhook(_make_update(970100000 + i, chat_id, "Купив масло за 10 zł"))
                self.assertIn(chat_id, pending_global_household)
                data = pending_global_household[chat_id]
                self.assertEqual(len(data["add_inventory_items"]), 1)
                self.assertIsNotNone(data["new_expense"])

    # Case 3
    def test_buy_plan_in_expenses_menu_builds_shopping_only_preview(self):
        chat_id = 970002
        active_list_context[chat_id] = "expenses"
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [{"type": "add_shopping", "name": "Булочка", "quantity_text": "", "category": "Хліб і випічка"}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(970000002, chat_id, "Планую купити булочку"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_shopping_items"]), 1)
        self.assertIsNone(data["new_expense"])
        self.assertEqual(data["origin"], "expenses_menu")

    # Case 4
    def test_consume_from_main_menu_builds_inventory_consumption_preview(self):
        chat_id = 970003
        self.mock_inventory_items.return_value = [{
            "id": 501, "name": "Ковбаски", "category": "М'ясо та риба",
            "quantity_value": 14.0, "quantity_unit": "шт.", "quantity_text": "14 шт.",
        }]
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [{"type": "consume_inventory", "item_number": 1, "quantity_value": 2, "quantity_unit": "шт."}],
            "unresolved_fragments": [],
        }
        _call_webhook(_make_update(970000003, chat_id, "З'їв 2 ковбаски"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["consume_changes"]), 1)
        self.assertEqual(data["consume_changes"][0]["new_value"], 12.0)

    # Case 5
    def test_unresolved_fragments_block_entire_compound_preview(self):
        chat_id = 970004
        self.mock_household_router.return_value = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Масло", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": ["щось незрозуміле"],
        }
        _call_webhook(_make_update(970000004, chat_id, "Купив масло і ще щось незрозуміле"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Не зміг зрозуміти" in t for t in self._sent_texts()))

    # Case 6
    def test_confirm_applies_all_operations_exactly_once(self):
        chat_id = 970005
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [_new_item("Масло")],
            "consume_changes": [], "inventory_targets": [],
            "new_expense": {"amount": Decimal("10.00"), "currency": "PLN", "category": "Продукти",
                             "description": "Масло", "expense_date": date(2026, 7, 5)},
            "delete_expense": None, "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {"shopping_added": 0, "inventory_added": 1, "inventory_updated": 0,
                                        "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False}
            _call_webhook(_make_update(970000005, chat_id, "✅ Так, застосувати"))
            _call_webhook(_make_update(970000006, chat_id, "✅ Так, застосувати"))
            mock_apply.assert_called_once()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("✅ Зміни застосовано." in t for t in self._sent_texts()))
        self.assertTrue(any("Немає активної дії для підтвердження." in t for t in self._sent_texts()))

    # Case 7
    def test_cancel_applies_nothing(self):
        chat_id = 970006
        pending_global_household[chat_id] = {
            "add_shopping_items": [_new_item("Булочка", "Хліб і випічка")], "add_inventory_items": [],
            "consume_changes": [], "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook(_make_update(970000007, chat_id, "❌ Скасувати"))
            mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))

    # Case 8
    def test_stale_target_blocks_confirm_with_no_partial_writes(self):
        chat_id = 970007
        original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError
        try:
            pending_global_household[chat_id] = {
                "add_shopping_items": [], "add_inventory_items": [_new_item("Масло")],
                "consume_changes": [], "inventory_targets": [{"item_id": 501, "quantity_value": 14.0, "quantity_unit": "шт."}],
                "new_expense": None, "delete_expense": None,
                "household_id": 1, "user_db_id": 10, "origin": "global",
            }
            with patch.object(bot, "apply_global_household_operations",
                               side_effect=bot.StaleSnapshotError()) as mock_apply:
                _call_webhook(_make_update(970000008, chat_id, "✅ Так, застосувати"))
                mock_apply.assert_called_once()
        finally:
            bot.StaleSnapshotError = original_stale_error
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any(STALE_PREVIEW_MSG in t for t in self._sent_texts()))

    # Case 9 — active preview of another flow has priority over the global router
    def test_active_preview_of_another_flow_has_priority(self):
        chat_id = 970008
        pending_delete_batch[chat_id] = {"items": [{"id": 1, "name": "Хліб"}], "household_id": 1, "user_db_id": 10}
        _call_webhook(_make_update(970000009, chat_id, "Купив масло за 10 zł"))
        self.mock_household_router.assert_not_called()
        self.assertIn(chat_id, pending_delete_batch)
        self.assertNotIn(chat_id, pending_global_household)

    # Case 10 — active selection mode has priority over the global router
    def test_active_selection_mode_has_priority(self):
        chat_id = 970009
        shopping_mode[chat_id] = "adding"
        self.mock_call_gemini.return_value = None  # parse_shopping_list_with_gemini returns None -> early "ok"
        _call_webhook(_make_update(970000010, chat_id, "Купив масло за 10 zł"))
        self.mock_household_router.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)

    # Case 13
    def test_ordinary_question_never_reaches_household_router(self):
        chat_id = 970010
        _call_webhook(_make_update(970000011, chat_id, "Яка сьогодні погода?"))
        self.mock_household_router.assert_not_called()
        # Unified Mini Action Planner V1's pre-gate rejects this text
        # (no household vocabulary/quantity signal) before ever calling
        # Gemini — only general AI-chat's own single call happens.
        self.mock_call_gemini.assert_called_once()
        self.assertNotIn(chat_id, pending_global_household)

    # Case 14 — existing single-expense flow keeps using the legacy dict/preview unchanged
    def test_plain_expense_phrase_still_uses_legacy_expense_flow(self):
        chat_id = 970011
        with patch.object(bot, "_ask_gemini_expense_router",
                           return_value={
                               "intent": "create_expense", "amount": "86,40", "currency": "PLN",
                               "category": "Продукти", "description": "Biedronka",
                               "expense_date": _todays_warsaw_date_iso(), "selected_numbers": [],
                               "unresolved_fragments": [],
                           }):
            _call_webhook(_make_update(970000012, chat_id, "Biedronka 86,40 zł — продукти"))
        self.mock_household_router.assert_not_called()
        self.assertIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("Додати витрату?" in t for t in self._sent_texts()))


class TestPartALocalMatchWebhookFlow(unittest.TestCase):
    def setUp(self):
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        expense_delete_selection.clear()
        pending_expense_delete.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _expense(self, expense_id, description):
        return {
            "id": expense_id, "amount": Decimal("4.00"), "currency": "PLN", "category": "Продукти",
            "description": description, "expense_date": date(2026, 7, 3),
            "created_at": datetime(2026, 7, 3, 12, 0),
        }

    # Case 11
    def test_single_bare_name_match_resolves_locally_without_gemini(self):
        chat_id = 970020
        expense_delete_selection[chat_id] = {
            "household_id": 1, "user_db_id": 10,
            "expenses": [self._expense(101, "Масло")], "origin": "expenses_menu",
        }
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            _call_webhook(_make_update(970000020, chat_id, "Масло"))
            mock_router.assert_not_called()
        self.assertIn(chat_id, pending_expense_delete)
        self.assertEqual(pending_expense_delete[chat_id]["expense_id"], 101)
        self.assertNotIn(chat_id, expense_delete_selection)

    # Case 12
    def test_multiple_bare_name_matches_do_not_auto_select(self):
        chat_id = 970021
        expense_delete_selection[chat_id] = {
            "household_id": 1, "user_db_id": 10,
            "expenses": [self._expense(101, "Масло"), self._expense(102, "Масло")],
            "origin": "expenses_menu",
        }
        with patch.object(bot, "_ask_gemini_expense_router",
                           return_value={"intent": "none", "amount": None, "currency": None, "category": None,
                                          "description": None, "expense_date": None, "selected_numbers": [],
                                          "unresolved_fragments": []}) as mock_router:
            _call_webhook(_make_update(970000021, chat_id, "Масло"))
            mock_router.assert_called_once()
        self.assertNotIn(chat_id, pending_expense_delete)
        self.assertIn(chat_id, expense_delete_selection)


if __name__ == '__main__':
    unittest.main()
