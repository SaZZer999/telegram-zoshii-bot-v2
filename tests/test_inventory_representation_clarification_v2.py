"""Inventory Representation Clarification V2 — a conversational resolution
for the ONE conflict shape the Inventory Representation Guard can't safely
resolve on its own: an existing structured count ("шт.") row against an
EXPLICIT incoming mass/volume quantity for the same product, in a Global
Household Operation (add or consume side). No real Gemini, Telegram,
Render, or Supabase call happens anywhere in this file — the Gemini router
call and every DB-facing bot.py helper are patched, and the
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
_spec = importlib.util.spec_from_file_location("real_database_for_representation_v2_test", _database_path)
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
import inventory  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    pending_inventory_representation_clarification,
    active_list_context,
    saved_list_context,
)

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _cheese_1pc_row():
    return {
        "id": 401, "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
        "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False,
    }


def _consume_cheese_200g_router_result():
    return {
        "intent": "household_operations",
        "operations": [{"type": "consume_inventory", "item_number": 1, "quantity_value": 200, "quantity_unit": "г"}],
        "unresolved_fragments": [],
    }


def _add_cheese_250g_router_result():
    return {
        "intent": "household_operations",
        "operations": [{"type": "add_inventory", "name": "Сир", "quantity_text": "250 г", "category": "Молочне та яйця"}],
        "unresolved_fragments": [],
    }


class _BaseGlobalRouterTestCase(unittest.TestCase):
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

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

    def tearDown(self):
        for d in (pending_global_household, pending_inventory_representation_clarification,
                  active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _seed_consume_clarification(self, chat_id, requested_value=200.0, requested_unit="г",
                                     extra_add_inventory_items=None, new_expenses=None, queue=None):
        conflict = household_router._build_consume_representation_conflict(
            _cheese_1pc_row(), requested_value, requested_unit,
        )
        pending_inventory_representation_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "stage": "choice", "conflict": conflict, "queue": queue or [],
            "add_shopping_items": [], "add_inventory_items": extra_add_inventory_items or [],
            "inventory_merge_targets": [],
            "consume_changes": [], "new_expenses": new_expenses or [], "new_expense": None,
            "delete_expense": None, "representation_resolutions": [],
        }

    def _seed_add_clarification(self, chat_id, incoming_value=250.0, incoming_unit="г", incoming_display="250 г"):
        incoming_item = {
            "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
            "quantity_value": incoming_value, "quantity_unit": incoming_unit,
            "quantity_text": incoming_display, "quantity_inferred": False, "was_corrected": False,
        }
        conflict = household_router._build_add_representation_conflict(incoming_item, _cheese_1pc_row())
        pending_inventory_representation_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "stage": "choice", "conflict": conflict, "queue": [],
            "add_shopping_items": [], "add_inventory_items": [], "inventory_merge_targets": [],
            "consume_changes": [], "new_expenses": [], "new_expense": None,
            "delete_expense": None, "representation_resolutions": [],
        }


# =========================
# Flow A — consume side
# =========================
class TestConsumeFlowATrigger(_BaseGlobalRouterTestCase):
    # #1: creates clarification, not a preview, no DB write.
    def test_conflict_creates_clarification_not_preview_or_write(self):
        chat_id = 810001
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            self.mock_hr.return_value = _consume_cheese_200g_router_result()
            _call_webhook(_make_update(810000001, chat_id, "З'їв 200 г сиру"))
        self.assertIn(chat_id, pending_inventory_representation_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(
            "У запасах є «Сир — 1 шт.», а ти хочеш списати 200 г." in t and "Що це означає?" in t
            for t in texts
        ))

    # #2: pending state blocks Global Router, legacy flows, and general AI.
    def test_pending_state_blocks_everything_else(self):
        chat_id = 810002
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000002, chat_id, "Купив банани"))
        self.mock_hr.assert_not_called()
        self.mock_call_gemini.assert_not_called()
        self.mock_apply.assert_not_called()
        self.assertIn(chat_id, pending_inventory_representation_clarification)


class TestConsumeFlowAPartOfExisting(_BaseGlobalRouterTestCase):
    # #3: "⚖️ Це частина наявного запасу" asks for the total quantity.
    def test_part_of_existing_choice_asks_total_quantity(self):
        chat_id = 810003
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000003, chat_id, "⚖️ Це частина наявного запасу"))
        self.assertIn(chat_id, pending_inventory_representation_clarification)
        self.assertEqual(pending_inventory_representation_clarification[chat_id]["stage"], "awaiting_total")
        texts = self._sent_texts()
        self.assertTrue(any("Скільки важив увесь наявний запас «Сир»?" in t for t in texts))

    # #4/#6: a valid total (250 г) is accepted and the final preview shows
    # both the relabel and the resulting consumption.
    def test_valid_total_builds_combined_preview(self):
        chat_id = 810004
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000004, chat_id, "⚖️ Це частина наявного запасу"))
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            _call_webhook(_make_update(810000005, chat_id, "250 г"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["consume_changes"]), 1)
        self.assertEqual(data["consume_changes"][0]["new_unit"], "г")
        texts = self._sent_texts()
        self.assertTrue(any(
            "Сир — 1 шт. → 250 г" in t and "Сир — 250 г − 200 г → буде 50 г" in t
            for t in texts
        ))

    # #5: 200 г, 150 г, 1 шт., 500 мл, and a bare "200" (equal to what's
    # being consumed — not strictly greater) are all rejected as a total
    # quantity for a 200 г consume request. A bare number IS now accepted
    # in this substage (see TestBareNumberTotalQuantity below) — but only
    # when its magnitude is strictly greater than the consumed amount.
    def test_invalid_totals_are_rejected(self):
        for i, bad_total in enumerate(("200 г", "150 г", "1 шт.", "500 мл", "200")):
            chat_id = 810010 + i
            with self.subTest(bad_total=bad_total):
                self._seed_consume_clarification(chat_id)
                _call_webhook(_make_update(810100000 + i * 2, chat_id, "⚖️ Це частина наявного запасу"))
                _call_webhook(_make_update(810100001 + i * 2, chat_id, bad_total))
                self.assertIn(chat_id, pending_inventory_representation_clarification)
                self.assertEqual(pending_inventory_representation_clarification[chat_id]["stage"], "awaiting_total")
                self.assertNotIn(chat_id, pending_global_household)


class TestConsumeFlowASeparateProduct(_BaseGlobalRouterTestCase):
    # #9: "📦 Це інший / не облікований продукт" leaves the "шт." row
    # untouched, doesn't consume anything, and warns explicitly in preview.
    def test_separate_product_choice_leaves_existing_untouched_and_warns(self):
        chat_id = 810020
        self._seed_consume_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            _call_webhook(_make_update(810000020, chat_id, "📦 Це інший / не облікований продукт"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(data["consume_changes"], [])
        resolutions = data["inventory_representation_resolutions"]
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(resolutions[0]["mode"], "skip_consume")
        texts = self._sent_texts()
        self.assertTrue(any(
            "⚠️ Сир — 200 г не списувати: це окремий продукт, якого немає у запасах." in t
            for t in texts
        ))


# =========================
# Flow B — add side
# =========================
class TestAddFlowBTrigger(_BaseGlobalRouterTestCase):
    # #10: an explicit incoming mass/volume quantity against an existing
    # "шт." row starts the add-side clarification.
    def test_add_conflict_creates_clarification(self):
        chat_id = 810030
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            self.mock_hr.return_value = _add_cheese_250g_router_result()
            _call_webhook(_make_update(810000030, chat_id, "Купив 250 г сиру"))
        self.assertIn(chat_id, pending_inventory_representation_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(
            "У запасах уже є «Сир — 1 шт.», а нова кількість — 250 г." in t
            and "Що означають ці 250 г?" in t
            for t in texts
        ))


class TestAddFlowBChoices(_BaseGlobalRouterTestCase):
    # #11: "📦 Це окрема упаковка" leaves "1 шт." alone and adds "250 г" as
    # its own row.
    def test_separate_package_choice_adds_new_row_leaves_existing(self):
        chat_id = 810040
        self._seed_add_clarification(chat_id)
        _call_webhook(_make_update(810000040, chat_id, "📦 Це окрема упаковка — додати окремо"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["_representation_outcome"], "separate")
        self.assertEqual(data["inventory_representation_resolutions"], [])

    # #12: "⚖️ Це вага наявного запису" corrects the existing row, no new row.
    def test_relabel_existing_choice_changes_representation_no_new_row(self):
        chat_id = 810041
        self._seed_add_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            _call_webhook(_make_update(810000041, chat_id, "⚖️ Це вага наявного запису — уточнити його"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(data["add_inventory_items"], [])
        resolutions = data["inventory_representation_resolutions"]
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(resolutions[0]["mode"], "relabel_existing")
        texts = self._sent_texts()
        self.assertTrue(any(
            "Сир — 1 шт. → 250 г (уточнено, без додавання нового товару)" in t for t in texts
        ))


# =========================
# Multi-expense batch preservation + staleness + navigation
# =========================
class TestBatchPreservationAndStaleness(_BaseGlobalRouterTestCase):
    # #13: a full multi-expense batch with a representation conflict keeps
    # every other operation (both expenses, both other inventory adds)
    # until the conflict is resolved, then builds ONE combined preview.
    def test_multi_expense_batch_with_conflict_preserves_everything(self):
        chat_id = 810050
        router_result = {
            "intent": "household_operations",
            "operations": [
                {"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
                {"type": "add_expense", "amount": "8", "currency": "PLN", "category": "Продукти",
                 "description": "Молоко", "expense_date": "2020-01-01"},
                {"type": "add_inventory", "name": "Хліб", "quantity_text": "", "category": "Хліб і випічка"},
                {"type": "add_expense", "amount": "5", "currency": "PLN", "category": "Продукти",
                 "description": "Хліб", "expense_date": "2020-01-01"},
                {"type": "consume_inventory", "item_number": 1, "quantity_value": 200, "quantity_unit": "г"},
                {"type": "add_inventory", "name": "Сосиски", "quantity_text": "пару", "category": "М'ясо та риба"},
            ],
            "unresolved_fragments": [],
        }
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            self.mock_hr.return_value = router_result
            _call_webhook(_make_update(
                810000050, chat_id,
                "Купив 1 л молока за 8 zł\nКупив хліб за 5 zł\nЗ'їв 200 г сиру\nДодай до запасів пару сосисок",
            ))
        self.assertIn(chat_id, pending_inventory_representation_clarification)
        state = pending_inventory_representation_clarification[chat_id]
        self.assertEqual(len(state["new_expenses"]), 2)
        self.assertEqual(len(state["add_inventory_items"]), 3)  # Молоко + Хліб + Сосиски
        self.mock_apply.assert_not_called()

        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            _call_webhook(_make_update(810000051, chat_id, "⚖️ Це частина наявного запасу"))
            _call_webhook(_make_update(810000052, chat_id, "250 г"))
        self.assertIn(chat_id, pending_global_household)
        final = pending_global_household[chat_id]
        self.assertEqual(len(final["new_expenses"]), 2)
        self.assertEqual(len(final["add_inventory_items"]), 3)
        self.assertEqual(len(final["consume_changes"]), 1)
        texts = self._sent_texts()
        self.assertTrue(any("💸 Витрати" in t and "🧊 Запаси" in t for t in texts))

    # #14: the target row changing between the clarification and the final
    # preview blocks the WHOLE plan — nothing is guessed, nothing applied.
    def test_stale_target_between_clarification_and_preview_blocks_plan(self):
        chat_id = 810060
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000060, chat_id, "⚖️ Це частина наявного запасу"))
        changed_row = dict(_cheese_1pc_row())
        changed_row["quantity_value"] = 2.0
        with patch.object(bot, "get_inventory_items", return_value=[changed_row]):
            _call_webhook(_make_update(810000061, chat_id, "250 г"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Запаси змінилися, тому це уточнення вже неактуальне." in t for t in texts))


class TestNavigationAndCancel(_BaseGlobalRouterTestCase):
    # #15: cancel and navigation clear the pending representation clarification.
    def test_cancel_clears_pending_state(self):
        chat_id = 810070
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000070, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)

    def test_start_clears_pending_state(self):
        chat_id = 810071
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000071, chat_id, "/start"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)

    def test_menu_clears_pending_state(self):
        chat_id = 810072
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000072, chat_id, "/menu"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)

    def test_main_menu_button_clears_pending_state(self):
        chat_id = 810073
        self._seed_consume_clarification(chat_id)
        _call_webhook(_make_update(810000073, chat_id, "⬅️ Головне меню"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)


# =========================
# #16/#17/#18 — never fires outside its exact narrow shape
# =========================
class TestNeverTriggersOutsideExactShape(unittest.TestCase):
    # #16a: a plain same-unit add still merges normally, never touching V2.
    def test_same_unit_add_still_merges_without_v2(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        milk_row = {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
                    "quantity_value": 7.0, "quantity_unit": "л", "quantity_text": "7 л", "quantity_inferred": False}
        kind, payload = household_router._validate_operations(router_result, [milk_row], [], NOW)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["add_inventory_items"][0]["_representation_outcome"], "merge")

    # #16b: an inferred incoming quantity ambiguous against several existing
    # rows still uses Inventory Quantity Clarification v1's own "clarify",
    # never the new "clarify_representation".
    def test_inferred_quantity_ambiguity_still_uses_v1_clarify(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        rows = [
            {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False},
            {"id": 2, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 6.0, "quantity_unit": "л", "quantity_text": "6 л", "quantity_inferred": False},
        ]
        kind, payload = household_router._validate_operations(router_result, rows, [], NOW)
        self.assertEqual(kind, "clarify")

    # #17: a text quantity ("дві пачки") never reaches V2 — it's blocked
    # earlier by the existing leaked-quantity-phrase guard.
    def test_container_text_quantity_does_not_trigger_v2(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "дві пачки Сосисок", "quantity_text": "", "category": "М'ясо та риба"}],
            "unresolved_fragments": [],
        }
        kind, reasons = household_router._validate_operations(router_result, [], [], NOW)
        self.assertEqual(kind, "invalid")

    # #18: mass vs volume never gets V2 or automatic conversion — an
    # existing "шт." row is required for V2; a volume-vs-mass mismatch
    # against a NON-count row falls through to the existing "separate" add,
    # exactly as before this feature existed.
    def test_mass_vs_volume_never_gets_v2_or_auto_conversion(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{"type": "add_inventory", "name": "Йогурт", "quantity_text": "200 г", "category": "Молочне та яйця"}],
            "unresolved_fragments": [],
        }
        yogurt_row = {"id": 1, "name": "Йогурт", "category": "Молочне та яйця", "canonical_name": "йогурт",
                      "quantity_value": 500.0, "quantity_unit": "мл", "quantity_text": "500 мл", "quantity_inferred": False}
        kind, payload = household_router._validate_operations(router_result, [yogurt_row], [], NOW)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["add_inventory_items"][0]["_representation_outcome"], "separate")


# =========================
# UX fix #1 — bare number accepted in the "awaiting_total" substage,
# contextual to the unit of the ORIGINAL consume request.
# =========================
class TestBareNumberTotalQuantity(_BaseGlobalRouterTestCase):
    # #1: bare "300" after "З'їв 200 г сиру" means "300 г".
    def test_bare_number_means_same_unit_as_consumed_mass(self):
        chat_id = 810090
        self._seed_consume_clarification(chat_id, requested_value=200.0, requested_unit="г")
        _call_webhook(_make_update(810000090, chat_id, "⚖️ Це частина наявного запасу"))
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            _call_webhook(_make_update(810000091, chat_id, "300"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(data["consume_changes"][0]["new_unit"], "г")
        texts = self._sent_texts()
        self.assertTrue(any("Сир — 1 шт. → 300 г" in t and "буде 100 г" in t for t in texts))

    # #2: bare "1" after "З'їв 0,5 кг сиру" means "1 кг" (checked via the
    # resolution's own resolved_unit — consume_changes' new_unit is always
    # expressed in the group's canonical display unit ("г"/"л"), same
    # pre-existing behavior as every other partial consumption, unrelated
    # to what unit the bare reply was interpreted as).
    def test_bare_number_means_same_unit_as_consumed_mass_kg(self):
        chat_id = 810091
        self._seed_consume_clarification(chat_id, requested_value=0.5, requested_unit="кг")
        _call_webhook(_make_update(810000092, chat_id, "⚖️ Це частина наявного запасу"))
        with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
            _call_webhook(_make_update(810000093, chat_id, "1"))
        self.assertNotIn(chat_id, pending_inventory_representation_clarification)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        resolution = data["inventory_representation_resolutions"][0]
        self.assertEqual(resolution["resolved_value"], Decimal("1"))
        self.assertEqual(resolution["resolved_unit"], "кг")

    # #2b: bare "1000" after "Використав 500 мл" means "1000 мл"; bare "2"
    # after consuming "1 л" means "2 л".
    def test_bare_number_means_same_unit_as_consumed_volume(self):
        for i, (requested_value, requested_unit, bare_reply, expected_unit, chat_id) in enumerate((
            (500.0, "мл", "1000", "мл", 810093),
            (1.0, "л", "2", "л", 810094),
        )):
            with self.subTest(requested_unit=requested_unit, bare_reply=bare_reply):
                self._seed_consume_clarification(chat_id, requested_value=requested_value, requested_unit=requested_unit)
                _call_webhook(_make_update(810000200 + i * 2, chat_id, "⚖️ Це частина наявного запасу"))
                with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
                    _call_webhook(_make_update(810000201 + i * 2, chat_id, bare_reply))
                self.assertIn(chat_id, pending_global_household)
                data = pending_global_household[chat_id]
                resolution = data["inventory_representation_resolutions"][0]
                self.assertEqual(resolution["resolved_value"], Decimal(bare_reply))
                self.assertEqual(resolution["resolved_unit"], expected_unit)

    # Still-supported explicit forms alongside the new bare-number allowance
    # — each paired with a compatible SAME-unit consume request, exactly
    # like every explicit-form total quantity already worked before this fix.
    def test_explicit_forms_still_accepted(self):
        cases = (
            ("300 г", "г", 200.0),
            ("300ГРАМ", "г", 200.0),
            ("0,3 кг", "кг", 0.2),
            ("1 л", "л", 0.5),
            ("500 мл", "мл", 200.0),
        )
        for i, (total_text, requested_unit, requested_value) in enumerate(cases):
            chat_id = 810100 + i
            with self.subTest(total_text=total_text):
                self._seed_consume_clarification(chat_id, requested_value=requested_value, requested_unit=requested_unit)
                _call_webhook(_make_update(810000300 + i * 2, chat_id, "⚖️ Це частина наявного запасу"))
                with patch.object(bot, "get_inventory_items", return_value=[_cheese_1pc_row()]):
                    _call_webhook(_make_update(810000301 + i * 2, chat_id, total_text))
                self.assertIn(chat_id, pending_global_household)

    # #3: a bare number OUTSIDE the representation total substage still
    # never becomes a valid quantity — the general parser is untouched.
    def test_bare_number_rejected_outside_representation_substage(self):
        value, unit = bot._parse_explicit_clarification_quantity("300")
        self.assertIsNone(value)
        self.assertIsNone(unit)

    def test_bare_number_still_rejected_in_v1_quantity_clarification(self):
        chat_id = 810110
        bot.pending_inventory_quantity_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
            "add_shopping_items": [], "add_inventory_items": [{
                "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
                "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.",
                "quantity_inferred": True, "was_corrected": False,
            }],
            "consume_changes": [], "new_expenses": [], "new_expense": None, "delete_expense": None,
        }
        try:
            _call_webhook(_make_update(810000110, chat_id, "300"))
            texts = self._sent_texts()
            self.assertTrue(any("Потрібна точна кількість з одиницею." in t for t in texts))
        finally:
            bot.pending_inventory_quantity_clarification.pop(chat_id, None)

    # #4: a bare number is rejected when its magnitude isn't strictly
    # greater than what's being consumed.
    def test_bare_number_rejected_when_not_strictly_greater(self):
        for bare_reply in ("200", "150"):
            chat_id = 810120 + int(bare_reply)
            with self.subTest(bare_reply=bare_reply):
                self._seed_consume_clarification(chat_id, requested_value=200.0, requested_unit="г")
                _call_webhook(_make_update(810000400 + chat_id, chat_id, "⚖️ Це частина наявного запасу"))
                _call_webhook(_make_update(810000401 + chat_id, chat_id, bare_reply))
                self.assertIn(chat_id, pending_inventory_representation_clarification)
                self.assertNotIn(chat_id, pending_global_household)


# =========================
# UX fix #2 — legacy identity matching in Flow B (add side)
# =========================
def _legacy_ser_1pc_row():
    return {
        "id": 501, "name": "ser", "category": "Молочне та яйця", "canonical_name": "ser",
        "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт.", "quantity_inferred": False,
    }


class TestLegacyNormalizedMatchingPure(unittest.TestCase):
    # #5/#9: exactly one legacy candidate is found via canonical_name
    # normalization; two legacy candidates are never silently chosen.
    def test_legacy_row_found_via_canonical_name_normalization(self):
        existing = inventory.detect_add_representation_v2_conflict(
            [_legacy_ser_1pc_row()], "сир", "Молочне та яйця", 250.0, "г", False,
            name_normalizer=bot.canonicalize_name,
        )
        self.assertIsNotNone(existing)
        self.assertEqual(existing["id"], 501)

    def test_legacy_row_found_via_name_when_canonical_name_missing(self):
        row = {"id": 503, "name": "ser", "category": "Молочне та яйця", "canonical_name": None,
               "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт."}
        existing = inventory.detect_add_representation_v2_conflict(
            [row], "сир", "Молочне та яйця", 250.0, "г", False,
            name_normalizer=bot.canonicalize_name,
        )
        self.assertIsNotNone(existing)
        self.assertEqual(existing["id"], 503)

    def test_two_legacy_candidates_are_not_silently_chosen(self):
        row_a = dict(_legacy_ser_1pc_row())
        row_b = dict(_legacy_ser_1pc_row())
        row_b["id"] = 502
        existing = inventory.detect_add_representation_v2_conflict(
            [row_a, row_b], "сир", "Молочне та яйця", 250.0, "г", False,
            name_normalizer=bot.canonicalize_name,
        )
        self.assertIsNone(existing)

    # #10: an already-canonical row is found exactly as before — the
    # legacy fallback is never even consulted when the exact match succeeds.
    def test_exact_canonical_match_unaffected_by_normalizer(self):
        existing = inventory.detect_add_representation_v2_conflict(
            [_cheese_1pc_row()], "сир", "Молочне та яйця", 250.0, "г", False,
            name_normalizer=bot.canonicalize_name,
        )
        self.assertIsNotNone(existing)
        self.assertEqual(existing["id"], 401)

    # No normalizer given (legacy callers) -> behaves exactly as before this fix.
    def test_no_normalizer_means_no_legacy_fallback(self):
        existing = inventory.detect_add_representation_v2_conflict(
            [_legacy_ser_1pc_row()], "сир", "Молочне та яйця", 250.0, "г", False,
        )
        self.assertIsNone(existing)


class TestLegacyIdentityMatchingAddFlow(_BaseGlobalRouterTestCase):
    # #5: "ser — 1 шт." + "Купив 250 г сиру" triggers the add clarification,
    # never a silent separate-row insert.
    def test_legacy_ser_row_triggers_add_clarification_not_silent_insert(self):
        chat_id = 810130
        with patch.object(bot, "get_inventory_items", return_value=[_legacy_ser_1pc_row()]):
            self.mock_hr.return_value = _add_cheese_250g_router_result()
            _call_webhook(_make_update(810000130, chat_id, "Купив 250 г сиру"))
        self.assertIn(chat_id, pending_inventory_representation_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_apply.assert_not_called()
        # #6: user-facing message shows the resolved readable name "Сир",
        # never the legacy technical label "ser".
        texts = self._sent_texts()
        self.assertTrue(any(
            "У запасах уже є «Сир — 1 шт.», а нова кількість — 250 г." in t
            and "Що означають ці 250 г?" in t
            for t in texts
        ))
        self.assertFalse(any("ser" in t for t in texts))
        # #6 (target snapshot): the conflict's existing target is still the
        # EXACT legacy row — same id, same stored unit, never rewritten.
        conflict = pending_inventory_representation_clarification[chat_id]["conflict"]
        self.assertEqual(conflict["existing"]["item_id"], 501)
        self.assertEqual(conflict["existing"]["quantity_unit"], "шт.")

    def _seed_legacy_add_clarification(self, chat_id):
        incoming_item = {
            "name": "Сир", "category": "Молочне та яйця", "canonical_name": "сир",
            "quantity_value": 250.0, "quantity_unit": "г", "quantity_text": "250 г",
            "quantity_inferred": False, "was_corrected": False,
        }
        conflict = household_router._build_add_representation_conflict(incoming_item, _legacy_ser_1pc_row())
        pending_inventory_representation_clarification[chat_id] = {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "stage": "choice", "conflict": conflict, "queue": [],
            "add_shopping_items": [], "add_inventory_items": [], "inventory_merge_targets": [],
            "consume_changes": [], "new_expenses": [], "new_expense": None,
            "delete_expense": None, "representation_resolutions": [],
        }

    # #7: "вага наявного запису" transforms exactly the legacy target row,
    # no new row created.
    def test_relabel_choice_targets_exact_legacy_row_no_new_row(self):
        chat_id = 810131
        self._seed_legacy_add_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_legacy_ser_1pc_row()]):
            _call_webhook(_make_update(810000131, chat_id, "⚖️ Це вага наявного запису — уточнити його"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(data["add_inventory_items"], [])
        self.assertEqual(len(data["consume_changes"]), 1)
        self.assertEqual(data["consume_changes"][0]["item_id"], 501)
        resolutions = data["inventory_representation_resolutions"]
        self.assertEqual(resolutions[0]["mode"], "relabel_existing")
        self.assertEqual(resolutions[0]["item_id"], 501)
        texts = self._sent_texts()
        self.assertTrue(any("Сир — 1 шт. → 250 г (уточнено, без додавання нового товару)" in t for t in texts))

    # #8: "окрема упаковка" does not touch the legacy target at all.
    def test_separate_package_choice_does_not_touch_legacy_row(self):
        chat_id = 810132
        self._seed_legacy_add_clarification(chat_id)
        with patch.object(bot, "get_inventory_items", return_value=[_legacy_ser_1pc_row()]):
            _call_webhook(_make_update(810000132, chat_id, "📦 Це окрема упаковка — додати окремо"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(data["consume_changes"], [])
        self.assertEqual(data["inventory_representation_resolutions"], [])
        self.assertEqual(len(data["add_inventory_items"]), 1)
        self.assertEqual(data["add_inventory_items"][0]["_representation_outcome"], "separate")


# =========================
# #7 — DB layer: relabel + consume applies atomically, one journal record
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


class TestConversionConsumeDbLayer(unittest.TestCase):
    def test_relabel_and_consume_applies_atomically_with_one_journal_record(self):
        cheese_row = {"id": 401, "name": "Сир", "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт."}
        conflict = household_router._build_consume_representation_conflict(cheese_row, 200.0, "г")
        kind, remaining, remaining_unit = household_router.validate_representation_v2_total_quantity(
            conflict, Decimal("250"), "г",
        )
        self.assertEqual(kind, "ok")
        resolution, consume_change = household_router.resolve_representation_v2_consume_relabel(
            conflict, Decimal("250"), "г", remaining, remaining_unit,
        )
        self.assertEqual(resolution["remaining_display"], "50 г")

        inventory_targets = [{"item_id": 401, "quantity_value": 1.0, "quantity_unit": "шт."}]
        consume_updates = [{
            "item_id": 401, "quantity_value": consume_change["new_value"],
            "quantity_unit": consume_change["new_unit"], "quantity_text": consume_change["new_display"],
        }]

        cursor = FakeCursor(
            fetchall_results=[
                [(401, 1.0, "шт.")],  # _verify_targets_in_tx FOR UPDATE
                [(401, "сир")],  # consume_ids canonical name lookup
                [(401, "Сир", "сир", "1 шт.", Decimal("1"), "шт.", False, "Молочне та яйця")],  # before bucket
                [(401, "Сир", "сир", "50 г", Decimal("50"), "г", False, "Молочне та яйця")],  # after bucket
            ],
            fetchone_results=[(401,)],  # UPDATE ... RETURNING id
        )
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_global_household_operations(
                household_id=1, user_db_id=10,
                consume_updates=consume_updates, inventory_targets=inventory_targets,
            )
        self.assertTrue(conn.committed)
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn("г", update_queries[0][1])
        self.assertIn(50.0, update_queries[0][1])
        journal_inserts = [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]
        self.assertEqual(len(journal_inserts), 1)


# =========================
# #8 — Undo restores the exact before-state ("50 г" -> "1 шт.")
# =========================
class ScriptedCursor:
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


class TestUndoRestoresRepresentation(unittest.TestCase):
    def test_undo_restores_exact_before_state(self):
        before_row = {"id": 401, "household_id": 1, "name": "Сир", "canonical_name": "сир",
                      "quantity_text": "1 шт.", "quantity_value": "1", "quantity_unit": "шт.",
                      "quantity_inferred": False, "category": "Молочне та яйця"}
        post_row = {"id": 401, "household_id": 1, "name": "Сир", "canonical_name": "сир",
                    "quantity_text": "50 г", "quantity_value": "50", "quantity_unit": "г",
                    "quantity_inferred": False, "category": "Молочне та яйця"}
        before_snap = {"inventory_buckets": {"сир": [before_row]}, "shopping_buckets": {}, "expense_delete": None}
        post_snap = {"inventory_buckets": {"сир": [post_row]}, "shopping_buckets": {}, "expense_adds": []}

        cursor = ScriptedCursor(handlers=[
            _journal_handler(before_snap, post_snap),
            ("FROM inventory_items WHERE household_id=%s AND canonical_name=%s", None, [_row_tuple(post_row)]),
        ])
        conn = UndoFakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            real_database.apply_undo_action(action_id=1, household_id=1, actor_user_id=10)

        self.assertTrue(conn.committed)
        update_queries = [q for q in cursor.queries if "UPDATE inventory_items" in q[0]]
        self.assertEqual(len(update_queries), 1)
        self.assertIn("шт.", update_queries[0][1])
        self.assertIn(Decimal("1"), update_queries[0][1])
        self.assertFalse(any("DELETE FROM inventory_items" in sql for sql, _ in cursor.queries))
        self.assertFalse(any("INSERT INTO inventory_items" in sql for sql, _ in cursor.queries))
        self.assertTrue(any("status='undone'" in sql for sql, _ in cursor.queries))


if __name__ == "__main__":
    unittest.main()
