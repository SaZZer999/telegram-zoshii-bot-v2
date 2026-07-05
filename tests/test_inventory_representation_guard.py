import sys
import os
import importlib.util
import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock by the time this file executes. This lets us exercise the actual
# _merge_or_insert_inventory_in_tx()/add_inventory_items_batch() SQL/
# transaction shape directly, with a fake connection/cursor standing in for
# Postgres — no real Supabase involved. Same pattern as
# tests/test_expense_delete.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_inventory_guard_test", _database_path)
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

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
import household_router
from bot import (
    pending_inventory_batch,
    pending_global_household,
    inventory_mode,
    active_list_context,
    saved_list_context,
    STALE_PREVIEW_MSG,
    INVENTORY_KEYBOARD,
)

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _milk_row():
    return {"id": 101, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 6.0, "quantity_unit": "л", "quantity_text": "6 л", "quantity_inferred": False}


def _banana_row():
    return {"id": 102, "name": "Банани", "category": "Фрукти та ягоди", "canonical_name": "банани",
             "quantity_value": 3.0, "quantity_unit": "шт.", "quantity_text": "3 шт.", "quantity_inferred": False}


def _sausage_row():
    return {"id": 103, "name": "Сосиски", "category": "М'ясо та риба", "canonical_name": "сосиски",
             "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_text": "2 шт.", "quantity_inferred": False}


def _saffron_row():
    return {"id": 104, "name": "Шафран", "category": "Інше їстівне", "canonical_name": "шафран",
             "quantity_value": Decimal("0.00011"), "quantity_unit": "г", "quantity_text": "0,00011 г",
             "quantity_inferred": False}


def _legacy_banana_row():
    return {"id": 105, "name": "банани", "category": "Фрукти та ягоди", "canonical_name": "банани",
             "quantity_value": 3.0, "quantity_unit": None, "quantity_text": "3", "quantity_inferred": False}


# =========================
# Shared classification helper (pure)
# =========================
class TestResolveInventoryRepresentation(unittest.TestCase):
    # Case 1 — compatible structured quantity merges (unchanged behavior)
    def test_bananas_same_unit_merge(self):
        outcome, existing = bot.resolve_inventory_representation(
            [_banana_row()], "банани", "Фрукти та ягоди", 2.0, "шт.", False,
        )
        self.assertEqual(outcome, "merge")
        self.assertEqual(existing["id"], 102)

    # Case 2 — Decimal-precise merge still works through the guard
    def test_saffron_same_unit_merges_precisely(self):
        outcome, existing = bot.resolve_inventory_representation(
            [_saffron_row()], "шафран", "Інше їстівне", Decimal("0.00001"), "г", False,
        )
        self.assertEqual(outcome, "merge")
        merged_value, merged_unit = bot.merge_quantity_values(
            existing["quantity_value"], existing["quantity_unit"], Decimal("0.00001"), "г",
        )
        self.assertEqual(merged_value, Decimal("0.00012"))
        self.assertEqual(merged_unit, "г")

    # Case 3 — inferred quantity conflicting with a different representation
    def test_inferred_milk_conflict_is_clarify(self):
        outcome, existing = bot.resolve_inventory_representation(
            [_milk_row()], "молоко", "Молочне та яйця", 1.0, "шт.", True,
        )
        self.assertEqual(outcome, "clarify")
        self.assertEqual(existing["id"], 101)

    # Case 5 — explicit incompatible/text quantity is a safe separate record
    def test_sausage_pack_phrase_is_separate(self):
        outcome, existing = bot.resolve_inventory_representation(
            [_sausage_row()], "сосиски", "М'ясо та риба", None, None, False,
        )
        self.assertEqual(outcome, "separate")
        self.assertEqual(existing["id"], 103)

    # Case 7 — legacy unstructured existing row vs new explicit "3 шт."
    def test_legacy_unitless_existing_vs_new_explicit_pieces_is_separate(self):
        outcome, existing = bot.resolve_inventory_representation(
            [_legacy_banana_row()], "банани", "Фрукти та ягоди", 3.0, "шт.", False,
        )
        self.assertEqual(outcome, "separate")

    def test_no_existing_row_is_none(self):
        outcome, existing = bot.resolve_inventory_representation([], "шафран", "Інше їстівне", 1.0, "г", False)
        self.assertEqual(outcome, "none")
        self.assertIsNone(existing)

    # Case 10 — same function reused by household_router.py via the _bot bridge
    def test_household_router_uses_the_same_shared_function(self):
        self.assertIs(household_router._bot.resolve_inventory_representation, bot.resolve_inventory_representation)
        self.assertIs(household_router._bot.merge_quantity_values, bot.merge_quantity_values)


# =========================
# household_router.py — preview honesty + compound blocking
# =========================
class TestHouseholdRouterRepresentationGuard(unittest.TestCase):
    # Case 1 — preview shows the honest combined result
    def test_banana_merge_preview_shows_combined_total(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Банани", "quantity_text": "2 шт.", "category": "Фрукти та ягоди"}],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [_banana_row()], [], NOW)
        self.assertEqual(kind, "ok")
        text = household_router.format_preview(payload)
        self.assertIn("Банани — 3 шт. + 2 шт. → буде 5 шт.", text)
        self.assertEqual(payload["inventory_merge_targets"], [{"item_id": 102, "quantity_value": 3.0, "quantity_unit": "шт."}])

    # Case 2
    def test_saffron_merge_preview_shows_precise_sum(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Шафран", "quantity_text": "0,00001 г", "category": "Інше їстівне"}],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [_saffron_row()], [], NOW)
        self.assertEqual(kind, "ok")
        text = household_router.format_preview(payload)
        self.assertIn("0,00011 г + 0,00001 г → буде 0,00012 г", text)

    # Case 3 — no preview, no write, clarification instead
    def test_milk_conflict_blocks_with_clarify_message(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        kind, message = household_router._validate_operations(router_result, [_milk_row()], [], NOW)
        self.assertEqual(kind, "clarify")
        self.assertIn("У запасах уже є «Молоко — 6 л»", message)
        self.assertIn("Скільки", message)

    # Case 4 — the milk clarify blocks the WHOLE compound command, including
    # an unrelated add_expense bundled in the same message.
    def test_milk_conflict_blocks_entire_compound_command(self):
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": NOW.date().isoformat()},
            ],
            "unresolved_fragments": [],
        }
        kind, message = household_router._validate_operations(router_result, [_milk_row()], [], NOW)
        self.assertEqual(kind, "clarify")
        self.assertIsInstance(message, str)

    # Case 5 — separate-record warning shown, item still added
    def test_sausage_pack_preview_warns_about_separate_record(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Сосиски", "quantity_text": "дві пачки", "category": "М'ясо та риба"}],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [_sausage_row()], [], NOW)
        self.assertEqual(kind, "ok")
        text = household_router.format_preview(payload)
        self.assertIn("⚠️ Сосиски вже є у запасах: 2 шт.", text)
        self.assertIn("буде збережено окремою позицією", text)
        self.assertIn("Додати Сосиски — дві пачки", text)
        self.assertEqual(payload["inventory_merge_targets"], [])

    # Case 7 — legacy unitless existing row doesn't silently merge with a new explicit "3 шт."
    def test_legacy_bananas_do_not_silently_merge(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Банани", "quantity_text": "3 шт.", "category": "Фрукти та ягоди"}],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [_legacy_banana_row()], [], NOW)
        self.assertEqual(kind, "ok")
        text = household_router.format_preview(payload)
        self.assertIn("⚠️", text)
        self.assertIn("буде збережено окремою позицією", text)


# =========================
# database.py — DB-layer write path
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


class TestMergeOrInsertInventoryInTxWritePath(unittest.TestCase):
    # Case 1 (DB side) — same-unit merge still UPDATEs, never inserts a duplicate
    def test_bananas_same_unit_merge_updates_existing_row(self):
        cursor = FakeCursor(fetchall_results=[[(102, "Фрукти та ягоди", 3.0, "шт.", False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_inventory_in_tx(
                        cur, household_id=1, user_db_id=10, name="Банани", qty_text="2 шт.",
                        category="Фрукти та ягоди", canonical_name="банани",
                        quantity_value=2.0, quantity_unit="шт.", quantity_inferred=False,
                    )
        select_sql = cursor.queries[0][0]
        self.assertIn("FOR UPDATE", select_sql)
        update_sql, update_params = cursor.queries[-1]
        self.assertIn("UPDATE inventory_items", update_sql)
        self.assertEqual(update_params[0], "5 шт.")
        self.assertEqual(len(cursor.queries), 2)  # verify SELECT + one UPDATE, no INSERT

    # Case 6 — separate-record confirm: old row untouched, new row inserted
    def test_sausage_pack_confirm_does_not_touch_old_row_and_inserts_new(self):
        cursor = FakeCursor(fetchall_results=[[(103, "М'ясо та риба", 2.0, "шт.", False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_inventory_in_tx(
                        cur, household_id=1, user_db_id=10, name="Сосиски", qty_text="дві пачки",
                        category="М'ясо та риба", canonical_name="сосиски",
                        quantity_value=None, quantity_unit=None, quantity_inferred=False,
                    )
        # No UPDATE at all — the old "2 шт." row must be left exactly as it was.
        self.assertFalse(any("UPDATE inventory_items SET" in q[0] for q in cursor.queries))
        insert_sql, insert_params = cursor.queries[-1]
        self.assertIn("INSERT INTO inventory_items", insert_sql)
        self.assertEqual(insert_params[1], "Сосиски")
        self.assertEqual(insert_params[2], "дві пачки")

    # Case 7 (DB side) — legacy unitless row + new explicit "3 шт." does not merge
    def test_legacy_unitless_row_does_not_merge_with_new_explicit_pieces(self):
        cursor = FakeCursor(fetchall_results=[[(105, "Фрукти та ягоди", 3.0, None, False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_inventory_in_tx(
                        cur, household_id=1, user_db_id=10, name="Банани", qty_text="3 шт.",
                        category="Фрукти та ягоди", canonical_name="банани",
                        quantity_value=3.0, quantity_unit="шт.", quantity_inferred=False,
                    )
        self.assertFalse(any("UPDATE inventory_items SET" in q[0] for q in cursor.queries))
        self.assertTrue(any("INSERT INTO inventory_items" in q[0] for q in cursor.queries))

    # Case 8 — old records are never auto-corrected: no UPDATE fires against
    # a legacy row just because a new item shares its canonical_name.
    def test_old_record_never_auto_corrected(self):
        cursor = FakeCursor(fetchall_results=[[(105, "Фрукти та ягоди", 3.0, None, False)]])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with conn:
                with conn.cursor() as cur:
                    real_database._merge_or_insert_inventory_in_tx(
                        cur, household_id=1, user_db_id=10, name="Банани", qty_text="пару",
                        category="Фрукти та ягоди", canonical_name="банани",
                        quantity_value=Decimal("2"), quantity_unit="шт.", quantity_inferred=True,
                    )
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items SET" in q[0]]
        self.assertEqual(update_queries, [])


class TestAddInventoryItemsBatchStaleGuard(unittest.TestCase):
    # Case 9 — representation changed between preview and confirm aborts
    # everything, no partial write.
    def test_stale_merge_target_aborts_before_any_write(self):
        cursor = FakeCursor(fetchall_results=[[(102, 999.0, "шт.")]])  # live value != snapshot
        conn = FakeConnection(cursor)
        targets = [{"item_id": 102, "quantity_value": 3.0, "quantity_unit": "шт."}]
        item = {"name": "Банани", "category": "Фрукти та ягоди", "canonical_name": "банани",
                "quantity_text": "2 шт.", "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_inferred": False}
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.add_inventory_items_batch(1, 10, [item], targets=targets)
        self.assertFalse(conn.committed)
        # Only the verify SELECT ran — the merge/insert path never got a chance to fire.
        self.assertEqual(len(cursor.queries), 1)
        self.assertIn("FOR UPDATE", cursor.queries[0][0])

    def test_matching_target_lets_batch_proceed(self):
        # First fetchall answers _verify_targets_in_tx, second answers
        # _merge_or_insert_inventory_in_tx's own candidate lookup.
        cursor = FakeCursor(fetchall_results=[
            [(102, 3.0, "шт.")],
            [(102, "Фрукти та ягоди", 3.0, "шт.", False)],
        ])
        conn = FakeConnection(cursor)
        targets = [{"item_id": 102, "quantity_value": 3.0, "quantity_unit": "шт."}]
        item = {"name": "Банани", "category": "Фрукти та ягоди", "canonical_name": "банани",
                "quantity_text": "2 шт.", "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_inferred": False}
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.add_inventory_items_batch(1, 10, [item], targets=targets)
        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        self.assertTrue(any("UPDATE inventory_items" in q[0] for q in cursor.queries))


# =========================
# Webhook-level: normal inventory add flow (inv_mode == "adding")
# =========================
def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _parse_result(name, quantity_text, category):
    # Mirrors what parse_shopping_list_with_gemini really builds per item —
    # runs the raw name/quantity_text through the real normalization
    # pipeline so quantity_value/unit/inferred/text are all correctly set
    # (critical here: a blank quantity_text must produce the real "1 шт."
    # quantity_inferred=True default, not a missing/None field).
    normalized = bot.normalize_item_quantity(name, quantity_text, allow_default_unit=True)
    item = {"name": name, "category": category, "was_corrected": False}
    item.update(normalized)
    return {"items": [item], "ignored_items": []}


class TestNormalInventoryAddFlowWebhook(unittest.TestCase):
    """bot.StaleSnapshotError is reassigned to the REAL exception class for
    this test class only — bot.py's own import binds the name to whatever
    `database` was mocked to at import time (a bare MagicMock attribute
    here, not a real Exception subclass), so `except StaleSnapshotError:`
    inside bot.py couldn't otherwise match a raised instance. Same caveat/
    fix as tests/test_merge_stale_snapshot_protection.py."""

    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        for d in (pending_inventory_batch, pending_global_household, inventory_mode,
                  active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 3 — milk conflict: no preview, no write, clarification instead
    def test_milk_conflict_blocks_preview_and_write(self):
        chat_id = 990001
        inventory_mode[chat_id] = "adding"
        with patch.object(bot, "get_inventory_items", return_value=[_milk_row()]):
            with patch.object(bot, "parse_shopping_list_with_gemini",
                               return_value=_parse_result("Молоко", "", "Молочне та яйця")):
                with patch.object(bot, "add_inventory_items_batch") as mock_add:
                    _call_webhook(_make_update(990000001, chat_id, "Купив молоко"))
                    mock_add.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_batch)
        self.assertTrue(any("У запасах уже є «Молоко — 6 л»" in t for t in self._sent_texts()))

    # Case 5 — sausage pack: preview created with a warning, item still added
    def test_sausage_pack_preview_shows_warning_and_stays_pending(self):
        chat_id = 990002
        inventory_mode[chat_id] = "adding"
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row()]):
            with patch.object(bot, "parse_shopping_list_with_gemini",
                               return_value=_parse_result("Сосиски", "дві пачки", "М'ясо та риба")):
                _call_webhook(_make_update(990000002, chat_id, "Купив дві пачки сосисок"))
        self.assertIn(chat_id, pending_inventory_batch)
        texts = self._sent_texts()
        self.assertTrue(any("⚠️" in t and "буде збережено окремою позицією" in t for t in texts))

    # Case 6 — confirming that preview does not touch the old row
    def test_confirm_after_separate_warning_inserts_new_row_only(self):
        chat_id = 990003
        pending_inventory_batch[chat_id] = {
            "items": [{
                "name": "Сосиски", "category": "М'ясо та риба", "canonical_name": "сосиски",
                "quantity_text": "дві пачки", "quantity_value": None, "quantity_unit": None,
                "quantity_inferred": False, "was_corrected": False,
            }],
            "ignored_items": [], "household_id": 1, "user_db_id": 10, "inventory_targets": [],
        }
        with patch.object(bot, "add_inventory_items_batch", return_value=1) as mock_add:
            _call_webhook(_make_update(990000003, chat_id, "✅ Додати все"))
            mock_add.assert_called_once()
            _, kwargs_or_args = mock_add.call_args, mock_add.call_args
            self.assertEqual(mock_add.call_args.kwargs.get("targets"), [])
        self.assertNotIn(chat_id, pending_inventory_batch)

    # Case 9 (webhook level) — stale target aborts confirm cleanly
    def test_confirm_stale_target_shows_stale_message(self):
        chat_id = 990004
        pending_inventory_batch[chat_id] = {
            "items": [{
                "name": "Банани", "category": "Фрукти та ягоди", "canonical_name": "банани",
                "quantity_text": "2 шт.", "quantity_value": 2.0, "quantity_unit": "шт.",
                "quantity_inferred": False, "was_corrected": False,
            }],
            "ignored_items": [], "household_id": 1, "user_db_id": 10,
            "inventory_targets": [{"item_id": 102, "quantity_value": 3.0, "quantity_unit": "шт."}],
        }
        with patch.object(bot, "add_inventory_items_batch", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(990000004, chat_id, "✅ Додати все"))
        self.assertTrue(any(STALE_PREVIEW_MSG in t for t in self._sent_texts()))


class TestGlobalHouseholdRouterRepresentationGuardWebhook(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_shopping = patch.object(bot, "get_active_shopping_items", return_value=[])
        patcher_shopping.start()
        self.addCleanup(patcher_shopping.stop)

        patcher_expenses = patch.object(bot, "get_recent_expenses_for_deletion", return_value=[])
        patcher_expenses.start()
        self.addCleanup(patcher_expenses.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_hr = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_hr = patcher_hr.start()
        self.addCleanup(patcher_hr.stop)

    def tearDown(self):
        for d in (pending_global_household, active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 4 — Global Router: milk conflict blocks the whole compound command
    def test_global_router_milk_conflict_blocks_entire_compound(self):
        chat_id = 990010
        with patch.object(bot, "get_inventory_items", return_value=[_milk_row()]):
            self.mock_hr.return_value = {
                "intent": "household_operations",
                "operations": [
                    {"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
                    {"type": "add_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
                     "description": "Молоко", "expense_date": NOW.date().isoformat()},
                ],
                "unresolved_fragments": [],
            }
            with patch.object(bot, "apply_global_household_operations") as mock_apply:
                _call_webhook(_make_update(990000010, chat_id, "Купив молоко"))
                mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("У запасах уже є «Молоко — 6 л»" in t for t in self._sent_texts()))


if __name__ == '__main__':
    unittest.main()
