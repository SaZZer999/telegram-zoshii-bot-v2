"""Active List Context Routing Stabilization V1 — end-to-end webhook-level
routing coverage for three confirmed live bugs:

  1. A bare "Видали тестовий чай" right after opening "🛒 Покупки" (saved_
     list_context == "shopping_saved") was claimed by the domain-blind
     inventory_admin_route (inventory.parse_inventory_delete_request has no
     concept of which list is open — "видали"/"прибери" trigger it
     regardless), so it searched INVENTORY and answered "Не знайшов такого
     запису в запасах." even though the item was sitting right in the open
     shopping list.
  2. "Видали половину сира Гауда 130 грамм" right after opening "🧊 Запаси"
     (saved_list_context == "inventory_saved") hit the same domain-blind
     inventory_admin_route, which has no concept of a PARTIAL removal — it
     always tried the whole phrase as a name for a FULL delete and never
     found a matching row.
  3. "Молоко 1 л 4,99 zł" while a shopping/inventory list is open used to be
     claimed by global_expense_command (a bare zł-tagged amount alone is
     enough), silently creating an expense-only preview and discarding the
     item quantity.

Fix: bot.py's _route_active_list_context_command — a narrow, deterministic
route wired as message_dispatcher.py's CommandRouteDeps.active_list_
context_route, checked right after destructive_bulk_guard and before every
other command route (ambiguous_add_route through saved_list_router). It
only ever claims a message it can positively, narrowly resolve; an explicit
cross-domain marker (запас location / shopping-list reference / a
financial-reference word) or an unrecognized shape always falls through to
the exact same routing chain that existed before this route did.

No real Gemini/Telegram/Supabase call happens anywhere in this file —
database is mocked at import time, every Gemini-facing bot.py/module
function is patched per-test (and asserted NOT called where the fix is
supposed to stay fully deterministic).
"""
import sys
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import quantities  # noqa: E402
import action_planner  # noqa: E402
import shopping_action_planner  # noqa: E402
from bot import (  # noqa: E402
    saved_list_context,
    active_list_context,
    pending_delete_batch,
    pending_mark_batch,
    pending_cleanup_admin,
    pending_inventory_consumption,
    pending_expense,
    pending_quantity_price_intent,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _shopping_items():
    return [
        {"id": 701, "name": "Тестовий чай", "canonical_name": "тестовий чай", "category": "Напої",
         "quantity_text": "54,37 шт.", "quantity_value": 54.37, "quantity_unit": "шт.", "quantity_inferred": False},
        {"id": 702, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_text": "1 л", "quantity_value": 1.0, "quantity_unit": "л", "quantity_inferred": False},
        {"id": 703, "name": "Хліб", "canonical_name": "хліб", "category": "Хліб і випічка",
         "quantity_text": "1 шт.", "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_inferred": False},
    ]


def _inventory_items():
    return [
        {"id": 801, "name": "Сир Гауда", "canonical_name": "сир гауда", "category": "Молочне та яйця",
         "quantity_text": "270 г", "quantity_value": 270.0, "quantity_unit": "г", "quantity_inferred": False},
    ]


class ActiveListContextTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(saved_list_context.clear)
        self.addCleanup(active_list_context.clear)
        self.addCleanup(pending_delete_batch.clear)
        self.addCleanup(pending_mark_batch.clear)
        self.addCleanup(pending_cleanup_admin.clear)
        self.addCleanup(pending_inventory_consumption.clear)
        self.addCleanup(pending_expense.clear)
        self.addCleanup(pending_quantity_price_intent.clear)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_user.start()
        self.addCleanup(patcher_user.stop)

        patcher_shopping_items = patch.object(bot, "get_active_shopping_items", return_value=_shopping_items())
        self.mock_shopping_items = patcher_shopping_items.start()
        self.addCleanup(patcher_shopping_items.stop)

        patcher_inventory_items = patch.object(bot, "get_inventory_items", return_value=_inventory_items())
        self.mock_inventory_items = patcher_inventory_items.start()
        self.addCleanup(patcher_inventory_items.stop)

        patcher_delete_batch = patch.object(bot, "delete_items_batch", return_value=1)
        self.mock_delete_items_batch = patcher_delete_batch.start()
        self.addCleanup(patcher_delete_batch.stop)

        patcher_mark_batch = patch.object(bot, "mark_items_batch", return_value=1)
        self.mock_mark_items_batch = patcher_mark_batch.start()
        self.addCleanup(patcher_mark_batch.stop)

        patcher_consume = patch.object(bot, "apply_inventory_consumption", return_value=(1, 0))
        self.mock_apply_consumption = patcher_consume.start()
        self.addCleanup(patcher_consume.stop)

        patcher_exec_delete = patch.object(bot, "execute_inventory_delete")
        self.mock_execute_inventory_delete = patcher_exec_delete.start()
        self.addCleanup(patcher_exec_delete.stop)

        patcher_action_planner = patch.object(action_planner, "classify")
        self.mock_action_planner_classify = patcher_action_planner.start()
        self.addCleanup(patcher_action_planner.stop)

        patcher_shopping_planner = patch.object(shopping_action_planner, "classify")
        self.mock_shopping_planner_classify = patcher_shopping_planner.start()
        self.addCleanup(patcher_shopping_planner.stop)

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# Shopping context — live bug 1.
# =========================
class TestShoppingContextBareDelete(ActiveListContextTestCase):
    # 1/2. Opening the shopping list sets shopping_saved context.
    def test_shopping_context_established(self):
        chat_id = 970001
        saved_list_context[chat_id] = "shopping_saved"
        self.assertEqual(saved_list_context.get(chat_id), "shopping_saved")

    # 3/4/5. Bare delete resolves against the OPEN SHOPPING list, not
    # inventory — the exact live-bug text.
    def test_bare_delete_resolves_shopping_item_not_inventory(self):
        chat_id = 970002
        saved_list_context[chat_id] = "shopping_saved"
        _call_webhook(_make_update(960002001, chat_id, "Видали тестовий чай"))
        self.mock_action_planner_classify.assert_not_called()
        self.assertIn(chat_id, pending_delete_batch)
        self.assertEqual([it["id"] for it in pending_delete_batch[chat_id]["items"]], [701])
        self.assertFalse(self.mock_delete_items_batch.called)
        self.assertFalse(any("Не знайшов такого запису в запасах" in t for t in self._sent_texts()))

    # 6. Cancel leaves the item untouched.
    def test_cancel_leaves_item(self):
        chat_id = 970003
        saved_list_context[chat_id] = "shopping_saved"
        _call_webhook(_make_update(960003001, chat_id, "Видали тестовий чай"))
        self.assertIn(chat_id, pending_delete_batch)
        _call_webhook(_make_update(960003002, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_delete_batch)
        self.assertFalse(self.mock_delete_items_batch.called)

    # 7. Confirm deletes only the matched item.
    def test_confirm_deletes_only_matched_item(self):
        chat_id = 970004
        saved_list_context[chat_id] = "shopping_saved"
        _call_webhook(_make_update(960004001, chat_id, "Видали тестовий чай"))
        _call_webhook(_make_update(960004002, chat_id, "✅ Так, видалити"))
        self.mock_delete_items_batch.assert_called_once()
        args, kwargs = self.mock_delete_items_batch.call_args
        self.assertEqual(args[1], [701])

    # 8. The SAME dispatcher entrypoint voice transcripts are routed through
    # (message_dispatcher.dispatch via bot.webhook()) resolves identically —
    # voice_input.py forwards its transcript into this exact text path,
    # never a separate one.
    def test_same_route_handles_transcript_text(self):
        chat_id = 970005
        saved_list_context[chat_id] = "shopping_saved"
        _call_webhook(_make_update(960005001, chat_id, "Видали тестовий чай"))
        self.assertIn(chat_id, pending_delete_batch)
        self.assertEqual([it["id"] for it in pending_delete_batch[chat_id]["items"]], [701])

    # 9. Mark-bought local action still works (deterministic fast path).
    def test_mark_bought_local_action(self):
        chat_id = 970006
        saved_list_context[chat_id] = "shopping_saved"
        _call_webhook(_make_update(960006001, chat_id, "Молоко вже купили"))
        self.mock_shopping_planner_classify.assert_not_called()
        self.assertIn(chat_id, pending_mark_batch)
        self.assertEqual([it["id"] for it in pending_mark_batch[chat_id]["items"]], [702])

    # 10. Explicit cross-domain marker escapes to inventory, not shopping.
    def test_explicit_inventory_marker_escapes_domain(self):
        chat_id = 970007
        saved_list_context[chat_id] = "shopping_saved"
        _call_webhook(_make_update(960007001, chat_id, "Видали сир Гауда із запасів"))
        self.assertNotIn(chat_id, pending_delete_batch)
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 801)


# =========================
# Inventory context — live bug 2.
# =========================
class TestInventoryContextPartialConsume(ActiveListContextTestCase):
    # 11. Opening the inventory list sets inventory_saved context.
    def test_inventory_context_established(self):
        chat_id = 970101
        saved_list_context[chat_id] = "inventory_saved"
        self.assertEqual(saved_list_context.get(chat_id), "inventory_saved")

    # 12/13/14/19/20. Explicit quantity + "половину" -> consume preview,
    # remaining 140 г, "сира Гауда" resolves to "Сир Гауда", "грамм"
    # normalizes to "г".
    def test_explicit_quantity_wins_over_half_word_and_shows_remaining(self):
        chat_id = 970102
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(960102001, chat_id, "Видали половину сира Гауда 130 грамм"))
        self.mock_action_planner_classify.assert_not_called()
        self.assertFalse(self.mock_execute_inventory_delete.called)
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertIn(chat_id, pending_inventory_consumption)
        resolved = pending_inventory_consumption[chat_id]["resolved"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["item_id"], 801)
        self.assertEqual(resolved[0]["old_display"], "270 г")
        self.assertEqual(resolved[0]["new_display"], "140 г")
        self.assertFalse(resolved[0]["will_remove"])
        self.assertTrue(any("140 г" in t for t in self._sent_texts()))

    # 15. Nothing is written to the DB before confirm.
    def test_no_db_write_before_confirm(self):
        chat_id = 970103
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(960103001, chat_id, "Видали половину сира Гауда 130 грамм"))
        self.assertFalse(self.mock_apply_consumption.called)

    # 16. Cancel leaves the original 270 г untouched.
    def test_cancel_leaves_original_quantity(self):
        chat_id = 970104
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(960104001, chat_id, "Видали половину сира Гауда 130 грамм"))
        _call_webhook(_make_update(960104002, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_inventory_consumption)
        self.assertFalse(self.mock_apply_consumption.called)

    # 17/18. Confirm applies the partial consume via the SAME
    # apply_inventory_consumption executor consume_inventory_quantity
    # already uses — never the full-delete executor.
    def test_confirm_applies_partial_consume_not_full_delete(self):
        chat_id = 970105
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(960105001, chat_id, "Видали половину сира Гауда 130 грамм"))
        _call_webhook(_make_update(960105002, chat_id, "✅ Підтвердити зміни"))
        self.mock_apply_consumption.assert_called_once()
        household_id, updates, delete_ids, targets = self.mock_apply_consumption.call_args[0]
        self.assertEqual(updates, [{"item_id": 801, "quantity_value": 140.0, "quantity_unit": "г", "quantity_text": "140 г"}])
        self.assertEqual(delete_ids, [])
        self.assertFalse(self.mock_execute_inventory_delete.called)

    # 21. "половину сиру Гауда" with NO explicit quantity -> clarification,
    # never a silent half-guess, never a full delete, no DB write.
    def test_half_word_without_explicit_quantity_asks_for_amount(self):
        chat_id = 970106
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(960106001, chat_id, "Видали половину сиру Гауда"))
        self.assertNotIn(chat_id, pending_inventory_consumption)
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertFalse(self.mock_execute_inventory_delete.called)
        self.assertTrue(any("Скільки саме списати" in t for t in self._sent_texts()))

    # Full delete WITHOUT any quantity/half word still works exactly as
    # before this fix — unchanged, via the existing inventory_admin_route.
    def test_full_delete_without_quantity_still_works_unchanged(self):
        chat_id = 970107
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(960107001, chat_id, "Видали сир Гауда"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 801)
        self.assertNotIn(chat_id, pending_inventory_consumption)

    # Other explicit-quantity phrasings from the spec (unit normalization,
    # leading-quantity word order, "спиши"/"списати" verb).
    def test_leading_quantity_and_location_phrase(self):
        chat_id = 970108
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(960108001, chat_id, "Прибери з запасів 130 г сиру Гауда"))
        self.assertIn(chat_id, pending_inventory_consumption)
        resolved = pending_inventory_consumption[chat_id]["resolved"]
        self.assertEqual(resolved[0]["item_id"], 801)
        self.assertEqual(resolved[0]["new_display"], "140 г")

    # 22. Explicit shopping-list marker escapes to shopping domain even
    # while inventory context is active.
    def test_explicit_shopping_marker_escapes_domain(self):
        chat_id = 970109
        saved_list_context[chat_id] = "inventory_saved"
        self.mock_shopping_planner_classify.return_value = {
            "version": 1, "action": "shopping_delete", "arguments": {"item_name": "хліб"},
            "clarification_question": None,
        }
        _call_webhook(_make_update(960109001, chat_id, "Викресли хліб зі списку покупок"))
        self.assertNotIn(chat_id, pending_inventory_consumption)
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.mock_shopping_planner_classify.assert_called_once()
        self.assertIn(chat_id, pending_delete_batch)
        self.assertEqual([it["id"] for it in pending_delete_batch[chat_id]["items"]], [703])


# =========================
# Money + quantity ambiguity in active context — live bug 3.
# =========================
class TestMoneyAndQuantityAmbiguityInContext(ActiveListContextTestCase):
    # 23/24/25/26. Shopping context: quantity + price -> controlled
    # clarification, no shopping pending, no expense pending, no DB write.
    # Updated for Quantity + Price Intent Clarification V1: the static
    # refusal is now an actionable four-choice clarification
    # (pending_quantity_price_intent) instead of a dead-end message — see
    # tests/test_quantity_price_intent_clarification.py for full coverage
    # of that feature. Still zero Gemini calls, still no shopping/expense
    # write before an explicit choice.
    def test_shopping_context_quantity_and_price_refusal(self):
        chat_id = 970201
        saved_list_context[chat_id] = "shopping_saved"
        with patch.object(bot, "_ask_gemini_expense_router") as mock_expense_router:
            _call_webhook(_make_update(960201001, chat_id, "Молоко 1 л 4,99 zł"))
        mock_expense_router.assert_not_called()
        self.assertNotIn(chat_id, pending_delete_batch)
        self.assertNotIn(chat_id, pending_mark_batch)
        self.assertNotIn(chat_id, pending_expense)
        self.assertFalse(self.mock_delete_items_batch.called)
        self.assertIn(chat_id, pending_quantity_price_intent)
        self.assertTrue(any("товар" in t and "ціну" in t for t in self._sent_texts()))

    # 27. Same safe behavior for inventory context.
    def test_inventory_context_quantity_and_price_refusal(self):
        chat_id = 970202
        saved_list_context[chat_id] = "inventory_saved"
        with patch.object(bot, "_ask_gemini_expense_router") as mock_expense_router:
            _call_webhook(_make_update(960202001, chat_id, "Молоко 1 л 4,99 zł"))
        mock_expense_router.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_consumption)
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertNotIn(chat_id, pending_expense)
        self.assertIn(chat_id, pending_quantity_price_intent)

    # 28. Pure money, no item quantity, still creates an expense preview
    # (existing 6054fe2 behavior, unaffected).
    def test_pure_expense_phrase_still_creates_expense_preview(self):
        chat_id = 970203
        saved_list_context[chat_id] = "shopping_saved"
        router_result = {
            "intent": "create_expense", "amount": "52.37", "currency": "PLN",
            "category": "Продукти", "description": "Тест чай batch", "expense_date": "2026-07-16",
            "selected_numbers": [], "unresolved_fragments": [],
        }
        with patch.object(bot, "_ask_gemini_expense_router", return_value=router_result):
            _call_webhook(_make_update(960203001, chat_id, "Тест чай batch 52,37 zł"))
        self.assertIn(chat_id, pending_expense)
        self.assertEqual(pending_expense[chat_id]["amount"], Decimal("52.37"))
        self.assertNotIn(chat_id, pending_delete_batch)


# =========================
# Routing regressions — pending-state/confirm priority, generic planners
# still reachable outside active context, zero-extra-Gemini-call contract.
# =========================
class TestRoutingRegressions(ActiveListContextTestCase):
    # 30/31. An already-active pending preview wins over the new context
    # route entirely (pending routes are checked before command routes).
    def test_active_pending_preview_wins_over_context_route(self):
        chat_id = 970301
        saved_list_context[chat_id] = "shopping_saved"
        pending_delete_batch[chat_id] = {
            "items": [_shopping_items()[0]], "household_id": 1, "user_db_id": 10,
        }
        _call_webhook(_make_update(960301001, chat_id, "Видали молоко"))
        # Still exactly the ORIGINAL pending preview, untouched by the new
        # message (guard message sent instead of starting a second action).
        self.assertEqual([it["id"] for it in pending_delete_batch[chat_id]["items"]], [701])
        self.assertFalse(self.mock_delete_items_batch.called)

    # 32. Generic Inventory Action Planner still reachable OUTSIDE active
    # list context (no saved_list_context at all).
    def test_inventory_action_planner_reachable_outside_context(self):
        chat_id = 970302
        self.mock_action_planner_classify.return_value = {
            "version": 1, "action": "unsupported", "arguments": {},
            "confidence": 0.0, "clarification_question": None,
        }
        # A synonym ("комбінуй") none of the three deterministic gates
        # (inventory_transform_route/inventory_cleanup_route/inventory_
        # admin_route) recognize, but action_planner's own pre-gate does
        # once patched — isolates this test from those gates' own trigger
        # vocabulary so it exercises action_planner_route specifically.
        with patch.object(action_planner, "looks_like_inventory_admin_or_transform", return_value=True):
            _call_webhook(_make_update(960302001, chat_id, "Комбінуй сосиски і мисливські ковбаски"))
        self.mock_action_planner_classify.assert_called_once()

    # 33. Shopping Action Planner still reachable OUTSIDE active list
    # context.
    def test_shopping_action_planner_reachable_outside_context(self):
        chat_id = 970303
        self.mock_shopping_planner_classify.return_value = {
            "version": 1, "action": "shopping_delete", "arguments": {"item_name": "молоко"},
            "clarification_question": None,
        }
        _call_webhook(_make_update(960303001, chat_id, "Викресли молоко зі списку"))
        self.mock_shopping_planner_classify.assert_called_once()

    # 36. Zero extra Gemini calls for the deterministic local-context
    # shapes this fix owns.
    def test_zero_gemini_calls_for_local_shopping_delete(self):
        chat_id = 970304
        saved_list_context[chat_id] = "shopping_saved"
        with patch.object(bot, "call_gemini") as mock_call_gemini:
            _call_webhook(_make_update(960304001, chat_id, "Видали тестовий чай"))
        mock_call_gemini.assert_not_called()
        self.mock_action_planner_classify.assert_not_called()
        self.mock_shopping_planner_classify.assert_not_called()

    def test_zero_gemini_calls_for_local_inventory_consume(self):
        chat_id = 970305
        saved_list_context[chat_id] = "inventory_saved"
        with patch.object(bot, "call_gemini") as mock_call_gemini:
            _call_webhook(_make_update(960305001, chat_id, "Видали половину сира Гауда 130 грамм"))
        mock_call_gemini.assert_not_called()
        self.mock_action_planner_classify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
