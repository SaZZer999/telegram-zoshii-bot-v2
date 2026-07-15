"""Context Intent Safety V1 — regression coverage for the live bug: sending
"Тест чай batch 52,37 zł" right after "🛒 Покупки" -> "➕ Додати товар" (active
`shopping_mode == "adding"`) produced a shopping item with quantity
"52,37 шт." — the Gemini shopping-item parser stripped "zł" off the message
and handed back a bare "52,37" quantity_text, which
quantities.parse_structured_quantity then read as a plain 52,37-count item
(its only contract for a bare number with no unit word).

Root cause: active shopping_mode/inventory_mode text dispatch
(legacy_shopping_flow.handle_shopping_mode_text /
legacy_inventory_flow.handle_inventory_mode_text) had unconditional priority
over every other route in message_dispatcher.py, including the global
expense command — so an expense-shaped message never got a chance to reach
expenses._handle_expense_command at all while a mode was active.

Fix: a deterministic pre-Gemini gate (quantities.looks_like_money_amount /
quantities.looks_like_explicit_item_quantity — the raw text, before the
Gemini shopping/inventory parser ever sees it and can drop the currency
marker) added at the top of both "adding" mode branches:
  * money marker present, no explicit item-quantity unit -> mode already
    popped, handler returns False -> message_dispatcher.py falls through to
    the existing global expense route unchanged (no new expense parser).
  * money marker AND an explicit item-quantity unit both present -> a
    controlled clarification, no shopping/inventory item, no expense, no DB
    write.
  * no money marker -> unaffected, existing item parser runs exactly as
    before.
A "Купив X за Y zł"-style compound purchase verb is explicitly excluded from
both branches (_PURCHASE_VERB_RE) — that phrasing is the Global Household
Router's own domain, and active mode already has documented priority over it
(see test_global_household_operations.TestGlobalHouseholdRouterWebhookFlow.
test_active_selection_mode_has_priority) — this fix must not change that.

No real Gemini/Telegram/Supabase call happens anywhere in this file —
database is mocked at import time, every Gemini-facing bot.py function is
patched per-test.
"""
import sys
import os
import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import expenses  # noqa: E402
import quantities  # noqa: E402
import legacy_shopping_flow  # noqa: E402
import legacy_inventory_flow  # noqa: E402
import voice_input  # noqa: E402
from bot import (  # noqa: E402
    shopping_mode,
    inventory_mode,
    pending_batch,
    pending_inventory_batch,
    pending_expense,
    active_list_context,
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


def _todays_warsaw_date_iso():
    return datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()


def _add_router_result(amount, description="Тест чай batch", category="Продукти"):
    return {
        "intent": "create_expense", "amount": amount, "currency": "PLN",
        "category": category, "description": description, "expense_date": _todays_warsaw_date_iso(),
        "selected_numbers": [], "unresolved_fragments": [],
    }


class ContextIntentSafetyBaseCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(shopping_mode.clear)
        self.addCleanup(inventory_mode.clear)
        self.addCleanup(pending_batch.clear)
        self.addCleanup(pending_inventory_batch.clear)
        self.addCleanup(pending_expense.clear)
        self.addCleanup(active_list_context.clear)

        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_user.start()
        self.addCleanup(patcher_user.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_alias = patch.object(bot, "get_household_alias_map", return_value={})
        self.mock_alias_map = patcher_alias.start()
        self.addCleanup(patcher_alias.stop)

        patcher_add_shopping = patch.object(bot, "add_shopping_items_batch")
        self.mock_add_shopping = patcher_add_shopping.start()
        self.addCleanup(patcher_add_shopping.stop)

        patcher_add_inventory = patch.object(bot, "add_inventory_items_batch")
        self.mock_add_inventory = patcher_add_inventory.start()
        self.addCleanup(patcher_add_inventory.stop)

        patcher_add_expense = patch.object(bot, "add_expense", return_value=None)
        self.mock_add_expense = patcher_add_expense.start()
        self.addCleanup(patcher_add_expense.stop)

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# 1/2/3/4/5 — exact live-bug sequence in shopping mode.
# =========================
class TestLiveBugSequenceShoppingMode(ContextIntentSafetyBaseCase):
    def test_expense_preview_shown_not_shopping_preview(self):
        chat_id = 981001
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "call_gemini") as mock_shopping_gemini:
            with patch.object(bot, "_ask_gemini_expense_router",
                               return_value=_add_router_result("52.37")) as mock_expense_router:
                _call_webhook(_make_update(981000001, chat_id, "Тест чай batch 52,37 zł"))
        mock_shopping_gemini.assert_not_called()
        mock_expense_router.assert_called_once()
        self.assertIn(chat_id, pending_expense)
        self.assertEqual(pending_expense[chat_id]["amount"], Decimal("52.37"))
        self.assertNotIn(chat_id, pending_batch)
        self.assertNotIn(chat_id, shopping_mode)

    def test_nothing_written_before_confirm(self):
        chat_id = 981002
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_add_router_result("52.37")):
            _call_webhook(_make_update(981000002, chat_id, "Тест чай batch 52,37 zł"))
        self.mock_add_expense.assert_not_called()
        self.mock_add_shopping.assert_not_called()
        self.assertNotIn(chat_id, pending_batch)

    def test_cancel_creates_neither_item_nor_expense(self):
        chat_id = 981003
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_add_router_result("52.37")):
            _call_webhook(_make_update(981000003, chat_id, "Тест чай batch 52,37 zł"))
        _call_webhook(_make_update(981000004, chat_id, "❌ Скасувати"))
        self.mock_add_expense.assert_not_called()
        self.mock_add_shopping.assert_not_called()
        self.assertNotIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_batch)

    def test_confirm_creates_only_the_expense(self):
        chat_id = 981004
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_add_router_result("52.37")):
            _call_webhook(_make_update(981000005, chat_id, "Тест чай batch 52,37 zł"))
        _call_webhook(_make_update(981000006, chat_id, "✅ Так, додати"))
        self.mock_add_expense.assert_called_once()
        self.assertEqual(self.mock_add_expense.call_args.args[2], Decimal("52.37"))
        self.mock_add_shopping.assert_not_called()
        self.assertNotIn(chat_id, pending_expense)

    def test_quantity_52_37_sht_never_appears_anywhere(self):
        chat_id = 981005
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_add_router_result("52.37")):
            _call_webhook(_make_update(981000007, chat_id, "Тест чай batch 52,37 zł"))
        _call_webhook(_make_update(981000008, chat_id, "✅ Так, додати"))
        self.mock_add_shopping.assert_not_called()
        for text in self._sent_texts():
            self.assertNotIn("52,37 шт", text)


# =========================
# 6 — the same live-bug sequence in active inventory add mode.
# =========================
class TestLiveBugSequenceInventoryMode(ContextIntentSafetyBaseCase):
    def test_expense_preview_shown_not_inventory_preview(self):
        chat_id = 981101
        inventory_mode[chat_id] = "adding"
        with patch.object(bot, "call_gemini") as mock_inventory_gemini:
            with patch.object(bot, "_ask_gemini_expense_router",
                               return_value=_add_router_result("52.37")) as mock_expense_router:
                _call_webhook(_make_update(981100001, chat_id, "Тест чай batch 52,37 zł"))
        mock_inventory_gemini.assert_not_called()
        mock_expense_router.assert_called_once()
        self.assertIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_inventory_batch)
        self.assertNotIn(chat_id, inventory_mode)

    def test_confirm_creates_only_the_expense(self):
        chat_id = 981102
        inventory_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_add_router_result("52.37")):
            _call_webhook(_make_update(981100002, chat_id, "Тест чай batch 52,37 zł"))
        _call_webhook(_make_update(981100003, chat_id, "✅ Так, додати"))
        self.mock_add_expense.assert_called_once()
        self.mock_add_inventory.assert_not_called()


# =========================
# 7/8 — other pure-money phrasings also reach the expense flow (zł and the
# Cyrillic "злотих" spelling both trigger it).
# =========================
class TestPureMoneyPhrasings(ContextIntentSafetyBaseCase):
    def test_kava_14_zl_expense_preview(self):
        chat_id = 981201
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router",
                           return_value=_add_router_result("14.00", description="Кава")) as mock_router:
            _call_webhook(_make_update(981200001, chat_id, "Кава 14 zł"))
        mock_router.assert_called_once()
        self.assertIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_batch)

    def test_kava_14_zlotykh_expense_preview(self):
        chat_id = 981202
        inventory_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router",
                           return_value=_add_router_result("14.00", description="Кава")) as mock_router:
            _call_webhook(_make_update(981200002, chat_id, "Кава 14 злотих"))
        mock_router.assert_called_once()
        self.assertIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_inventory_batch)


# =========================
# 9/10/14 — normal item add (no price) does not regress.
# =========================
class TestNormalItemAddDoesNotRegress(ContextIntentSafetyBaseCase):
    def test_two_units_still_builds_shopping_preview(self):
        chat_id = 981301
        shopping_mode[chat_id] = "adding"
        raw = ('{"items": [{"name": "Тестовий чай", "category": "Напої", '
               '"is_consumable": true, "quantity_text": "2 шт"}], "ignored_items": []}')
        with patch.object(bot, "call_gemini", return_value=raw) as mock_gemini:
            with patch.object(bot, "_ask_gemini_expense_router") as mock_expense_router:
                _call_webhook(_make_update(981300001, chat_id, "Тестовий чай 2 шт"))
        mock_gemini.assert_called_once()
        mock_expense_router.assert_not_called()
        self.assertIn(chat_id, pending_batch)
        self.assertNotIn(chat_id, pending_expense)

    def test_bare_volume_still_builds_item_preview(self):
        chat_id = 981302
        inventory_mode[chat_id] = "adding"
        raw = ('{"items": [{"name": "Молоко", "category": "Молочне та яйця", '
               '"is_consumable": true, "quantity_text": "1 л"}], "ignored_items": []}')
        with patch.object(bot, "call_gemini", return_value=raw):
            with patch.object(bot, "_ask_gemini_expense_router") as mock_expense_router:
                with patch.object(bot, "get_inventory_items", return_value=[]):
                    _call_webhook(_make_update(981300002, chat_id, "Молоко 1 л"))
        mock_expense_router.assert_not_called()
        self.assertIn(chat_id, pending_inventory_batch)
        self.assertNotIn(chat_id, pending_expense)

    def test_plain_name_no_price_still_builds_shopping_preview(self):
        chat_id = 981303
        shopping_mode[chat_id] = "adding"
        raw = '{"items": [{"name": "Хліб", "category": "Інше їстівне", "is_consumable": true, "quantity_text": ""}], "ignored_items": []}'
        with patch.object(bot, "call_gemini", return_value=raw):
            with patch.object(bot, "_ask_gemini_expense_router") as mock_expense_router:
                _call_webhook(_make_update(981300003, chat_id, "Хліб"))
        mock_expense_router.assert_not_called()
        self.assertIn(chat_id, pending_batch)


# =========================
# 11/12 — quantity + price together is a controlled refusal, never a silent
# guess (no item, no expense, no DB write).
# =========================
class TestAmbiguousQuantityAndPriceRefusal(ContextIntentSafetyBaseCase):
    def test_moloko_1l_499zl_shopping_mode_refuses(self):
        chat_id = 981401
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "call_gemini") as mock_shopping_gemini:
            with patch.object(bot, "_ask_gemini_expense_router") as mock_expense_router:
                _call_webhook(_make_update(981400001, chat_id, "Молоко 1 л 4,99 zł"))
        mock_shopping_gemini.assert_not_called()
        mock_expense_router.assert_not_called()
        self.assertNotIn(chat_id, pending_batch)
        self.assertNotIn(chat_id, pending_expense)
        self.mock_add_shopping.assert_not_called()
        self.mock_add_expense.assert_not_called()
        self.assertTrue(any("кількість" in t and "суму" in t for t in self._sent_texts()))

    def test_syr_500g_12zl_inventory_mode_refuses(self):
        chat_id = 981402
        inventory_mode[chat_id] = "adding"
        with patch.object(bot, "call_gemini") as mock_inventory_gemini:
            with patch.object(bot, "_ask_gemini_expense_router") as mock_expense_router:
                _call_webhook(_make_update(981400002, chat_id, "Сир 500 г, 12 zł"))
        mock_inventory_gemini.assert_not_called()
        mock_expense_router.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_batch)
        self.assertNotIn(chat_id, pending_expense)
        self.mock_add_inventory.assert_not_called()
        self.mock_add_expense.assert_not_called()

    def test_refusal_never_leaves_mode_dangling_next_message_is_fresh(self):
        chat_id = 981403
        shopping_mode[chat_id] = "adding"
        _call_webhook(_make_update(981400003, chat_id, "Молоко 1 л 4,99 zł"))
        self.assertNotIn(chat_id, shopping_mode)
        raw = '{"items": [{"name": "Хліб", "category": "Інше їстівне", "is_consumable": true, "quantity_text": ""}], "ignored_items": []}'
        with patch.object(bot, "call_gemini", return_value=raw):
            shopping_mode[chat_id] = "adding"
            _call_webhook(_make_update(981400004, chat_id, "Хліб"))
        self.assertIn(chat_id, pending_batch)


# =========================
# 13 — pure quantities.py unit coverage: a money token is never accepted as
# a quantity unit / never silently defaults to "шт.".
# =========================
class TestMoneyNeverBecomesQuantityUnit(unittest.TestCase):
    def test_bare_number_with_currency_word_is_unparseable_as_quantity(self):
        value, unit = quantities.parse_structured_quantity("52,37 zł")
        self.assertIsNone(value)
        self.assertIsNone(unit)

    def test_zlotykh_is_unparseable_as_quantity(self):
        value, unit = quantities.parse_structured_quantity("14 злотих")
        self.assertIsNone(value)
        self.assertIsNone(unit)

    def test_looks_like_money_amount_recognizes_all_required_markers(self):
        for text in ("52,37 zł", "14 zl", "14 PLN", "14 злотих", "14 злотий", "10 зл", "10 ЗЛ", "14 Zł"):
            with self.subTest(text=text):
                self.assertTrue(quantities.looks_like_money_amount(text))

    def test_looks_like_money_amount_false_for_plain_quantity(self):
        for text in ("2 шт", "1 л", "500 г", "Хліб", ""):
            with self.subTest(text=text):
                self.assertFalse(quantities.looks_like_money_amount(text))

    def test_looks_like_explicit_item_quantity_true_only_with_a_unit(self):
        self.assertTrue(quantities.looks_like_explicit_item_quantity("1 л"))
        self.assertTrue(quantities.looks_like_explicit_item_quantity("500 г"))
        self.assertFalse(quantities.looks_like_explicit_item_quantity("52,37 zł"))
        self.assertFalse(quantities.looks_like_explicit_item_quantity("Тест чай batch 52,37 zł"))


# =========================
# 15 — existing global expense add (no mode active at all) does not regress.
# =========================
class TestExistingGlobalExpenseAddDoesNotRegress(ContextIntentSafetyBaseCase):
    def test_plain_global_expense_command_unaffected(self):
        chat_id = 981501
        with patch.object(bot, "_ask_gemini_expense_router",
                           return_value=_add_router_result("14.00", description="Кава")) as mock_router:
            _call_webhook(_make_update(981500001, chat_id, "Кава 14 zł"))
        mock_router.assert_called_once()
        self.assertIn(chat_id, pending_expense)


# =========================
# 16 — the expense created via this new fallthrough path is written through
# the SAME single production call site (expenses.py:handle_add_confirm ->
# bot.add_expense) the Journal-Standalone-Expense-Additions fix (cc50495)
# already instruments — no second/parallel expense-creation path was added.
# =========================
class TestExpenseAddUndoPathNotBypassed(ContextIntentSafetyBaseCase):
    def test_confirm_goes_through_expenses_handle_add_confirm(self):
        chat_id = 981601
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_add_router_result("52.37")):
            _call_webhook(_make_update(981600001, chat_id, "Тест чай batch 52,37 zł"))
        with patch.object(expenses, "handle_add_confirm", wraps=lambda cid: pending_expense.pop(cid, None)) as mock_confirm:
            _call_webhook(_make_update(981600002, chat_id, "✅ Так, додати"))
        mock_confirm.assert_called_once_with(chat_id)


# =========================
# 18 — a voice transcript goes through the exact same
# message_dispatcher.dispatch() path, so it gets the same stronger-intent
# gate as typed text (no voice-specific bypass).
# =========================
class TestVoiceTranscriptGetsSameGate(ContextIntentSafetyBaseCase):
    def test_voice_transcript_with_money_in_shopping_mode_falls_through_to_expense(self):
        chat_id = 981801
        shopping_mode[chat_id] = "adding"
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/civ1.oga"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="Тест чай batch 52,37 zł"):
                with patch("os.remove"):
                    with patch.object(bot, "call_gemini") as mock_shopping_gemini:
                        with patch.object(bot, "_ask_gemini_expense_router",
                                           return_value=_add_router_result("52.37")) as mock_expense_router:
                            _call_webhook(_make_voice_update(981800001, chat_id))
        mock_shopping_gemini.assert_not_called()
        mock_expense_router.assert_called_once()
        self.assertIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_batch)


if __name__ == "__main__":
    unittest.main()
