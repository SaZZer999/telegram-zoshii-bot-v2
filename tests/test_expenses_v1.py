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
# add_expense() SQL/parameterization directly, with a fake connection/cursor
# standing in for Postgres — no real Supabase involved.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_expenses_test", _database_path)
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
    _parse_expense_amount,
    _validate_expense_category,
    _validate_expense_date,
    _validate_expense_router_result,
    _expense_command_gate,
    DEFAULT_EXPENSE_CATEGORY,
)


WARSAW_NOW_FIXED = datetime(2026, 7, 3, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _todays_warsaw_date_iso():
    """Real current Europe/Warsaw date — used as the default expense_date for
    webhook-level tests, which validate against the real clock (no `now`
    override reaches _validate_expense_date through the full webhook path).
    Keeps those tests correct regardless of which calendar day they run on.
    """
    return datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()


def _ok_router_result(**overrides):
    base = {
        "intent": "create_expense",
        "amount": "86,40",
        "currency": "PLN",
        "category": "Продукти",
        "description": "Biedronka",
        "expense_date": _todays_warsaw_date_iso(),
        "unresolved_fragments": [],
    }
    base.update(overrides)
    return base


# =========================
# 1/2 — amount parsing (exact Decimal, never float)
# =========================
class TestExpenseAmountParsing(unittest.TestCase):
    def test_normal_amount_parses_exact_decimal(self):
        self.assertEqual(_parse_expense_amount("86,40 zł"), Decimal("86.40"))
        self.assertEqual(_parse_expense_amount("120 zł"), Decimal("120.00"))

    def test_zero_amount_blocked(self):
        self.assertIsNone(_parse_expense_amount("0"))
        self.assertIsNone(_parse_expense_amount("0 zł"))

    def test_negative_amount_blocked(self):
        self.assertIsNone(_parse_expense_amount("-5"))
        self.assertIsNone(_parse_expense_amount("-86,40"))

    def test_too_large_amount_blocked(self):
        self.assertIsNone(_parse_expense_amount("1000001"))
        self.assertIsNone(_parse_expense_amount("5000000"))

    def test_max_amount_boundary_allowed(self):
        self.assertEqual(_parse_expense_amount("1000000"), Decimal("1000000.00"))

    def test_invalid_amount_text_blocked(self):
        self.assertIsNone(_parse_expense_amount("сто грн"))
        self.assertIsNone(_parse_expense_amount("abc"))
        self.assertIsNone(_parse_expense_amount(None))
        self.assertIsNone(_parse_expense_amount(""))


# =========================
# 3 — category validation / safe fallback to "Інше"
# =========================
class TestExpenseCategoryValidation(unittest.TestCase):
    def test_valid_category_passes_through_unchanged(self):
        category, was_defaulted = _validate_expense_category("Продукти")
        self.assertEqual(category, "Продукти")
        self.assertFalse(was_defaulted)

    def test_unknown_category_falls_back_to_inshe(self):
        category, was_defaulted = _validate_expense_category("Спорт")
        self.assertEqual(category, DEFAULT_EXPENSE_CATEGORY)
        self.assertTrue(was_defaulted)

    def test_missing_category_falls_back_to_inshe(self):
        category, was_defaulted = _validate_expense_category(None)
        self.assertEqual(category, DEFAULT_EXPENSE_CATEGORY)
        self.assertTrue(was_defaulted)


# =========================
# 4 — future date blocked
# =========================
class TestExpenseDateValidation(unittest.TestCase):
    def test_past_date_ok(self):
        self.assertEqual(
            _validate_expense_date("2026-07-01", now=WARSAW_NOW_FIXED), date(2026, 7, 1)
        )

    def test_today_ok(self):
        self.assertEqual(
            _validate_expense_date("2026-07-03", now=WARSAW_NOW_FIXED), date(2026, 7, 3)
        )

    def test_future_date_blocked(self):
        self.assertIsNone(_validate_expense_date("2026-07-04", now=WARSAW_NOW_FIXED))
        self.assertIsNone(_validate_expense_date("2099-01-01", now=WARSAW_NOW_FIXED))

    def test_invalid_date_text_blocked(self):
        self.assertIsNone(_validate_expense_date("не сьогодні", now=WARSAW_NOW_FIXED))
        self.assertIsNone(_validate_expense_date(None, now=WARSAW_NOW_FIXED))


# =========================
# 5 — unresolved_fragments always blocks the preview
# =========================
class TestExpenseRouterResultValidation(unittest.TestCase):
    def test_unresolved_fragments_block_regardless_of_intent(self):
        kind, payload = _validate_expense_router_result(
            _ok_router_result(unresolved_fragments=["невідома сума"]), now=WARSAW_NOW_FIXED
        )
        self.assertEqual(kind, "unresolved")
        self.assertEqual(payload, ["невідома сума"])

    def test_none_intent_returns_none(self):
        kind, payload = _validate_expense_router_result(
            _ok_router_result(intent="none"), now=WARSAW_NOW_FIXED
        )
        self.assertEqual(kind, "none")

    def test_ok_result_has_validated_fields(self):
        kind, payload = _validate_expense_router_result(
            _ok_router_result(expense_date="2026-07-03"), now=WARSAW_NOW_FIXED
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["amount"], Decimal("86.40"))
        self.assertEqual(payload["currency"], "PLN")
        self.assertEqual(payload["category"], "Продукти")
        self.assertFalse(payload["category_was_defaulted"])
        self.assertEqual(payload["expense_date"], date(2026, 7, 3))

    def test_missing_or_invalid_amount_blocks_preview(self):
        kind, _ = _validate_expense_router_result(
            _ok_router_result(amount=None), now=WARSAW_NOW_FIXED
        )
        self.assertEqual(kind, "invalid")
        kind, _ = _validate_expense_router_result(
            _ok_router_result(amount="0"), now=WARSAW_NOW_FIXED
        )
        self.assertEqual(kind, "invalid")

    def test_non_pln_currency_blocks_preview(self):
        kind, _ = _validate_expense_router_result(
            _ok_router_result(currency="EUR"), now=WARSAW_NOW_FIXED
        )
        self.assertEqual(kind, "invalid")

    def test_future_date_in_router_result_blocks_preview(self):
        kind, _ = _validate_expense_router_result(
            _ok_router_result(expense_date="2099-01-01"), now=WARSAW_NOW_FIXED
        )
        self.assertEqual(kind, "invalid")


# =========================
# 11 — explicit gate: "86 zł" from main menu vs. an ordinary question
# =========================
class TestExpenseCommandGate(unittest.TestCase):
    def test_amount_with_zl_suffix_passes_gate(self):
        self.assertTrue(_expense_command_gate("86 zł"))
        self.assertTrue(_expense_command_gate("Biedronka 86,40 zł"))
        self.assertTrue(_expense_command_gate("120 PLN за інтернет"))
        self.assertTrue(_expense_command_gate("14 zl"))

    def test_zapysy_vytraty_prefix_passes_gate(self):
        self.assertTrue(_expense_command_gate("Запиши витрату 50 zł на каву"))

    def test_ordinary_question_does_not_pass_gate(self):
        self.assertFalse(_expense_command_gate("Яка сьогодні погода?"))
        self.assertFalse(_expense_command_gate("Що приготувати з курки?"))

    def test_empty_text_does_not_pass_gate(self):
        self.assertFalse(_expense_command_gate(""))
        self.assertFalse(_expense_command_gate("   "))


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
    """Invoke the real webhook() dispatch (routing priority and all) inside a
    Flask test request context — no actual HTTP server involved."""
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class TestExpenseWebhookFlow(unittest.TestCase):
    """Cases 2, 6, 7, 8, 9, 10, 12, plus the routing half of 11 —
    full webhook() dispatch, everything network-facing patched."""

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
        # Defensive cleanup in case a test fails before reaching its own cleanup.
        for d in (bot.pending_expense, bot.pending_delete_batch,
                  bot.pending_alias_action, bot.active_list_context, bot.saved_list_context):
            d.clear()

    # Case 2 / 11 (routing half)
    def test_expense_command_from_main_menu_builds_preview_not_ai_chat(self):
        chat_id = 930001
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_ok_router_result()):
            _call_webhook(_make_update(930000001, chat_id, "Biedronka 86,40 zł — продукти"))

        self.mock_call_gemini.assert_not_called()
        self.mock_saved_router.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["amount"], Decimal("86.40"))
        self.assertEqual(bot.pending_expense[chat_id]["origin"], "global")
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Додати витрату?" in t for t in sent_texts))

    # Case 11 — ordinary question never reaches the expense router, falls through to AI chat
    def test_ordinary_question_falls_through_to_ai_chat(self):
        chat_id = 930002
        with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
            _call_webhook(_make_update(930000002, chat_id, "Яка сьогодні погода?"))
            mock_router.assert_not_called()
        # Unified Mini Action Planner V1 classifies first (call 1, falls
        # back to "unknown" for this mock), then general AI-chat runs
        # normally (call 2) — two calls is the expected, correct shape now.
        self.assertEqual(self.mock_call_gemini.call_count, 2)
        self.assertNotIn(chat_id, bot.pending_expense)

    # Case 12
    def test_expense_command_does_not_interrupt_active_confirm_preview(self):
        chat_id = 930003
        bot.pending_delete_batch[chat_id] = {
            "items": [{"id": 1, "name": "Хліб"}], "household_id": 1, "user_db_id": 10,
        }
        try:
            with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
                _call_webhook(_make_update(930000003, chat_id, "86 zł"))
            mock_router.assert_not_called()
            self.assertNotIn(chat_id, bot.pending_expense)
        finally:
            bot.pending_delete_batch.pop(chat_id, None)

    # Case 12 — aliases has explicit priority over a new expense command
    def test_expense_command_does_not_interrupt_active_alias_preview(self):
        chat_id = 930004
        bot.pending_alias_action[chat_id] = {
            "kind": "create", "household_id": 1, "user_db_id": 10,
            "alias_text": "сливки", "target_display_name": "Вершки", "origin": "global",
        }
        try:
            with patch.object(bot, "_ask_gemini_expense_router") as mock_router:
                _call_webhook(_make_update(930000004, chat_id, "86 zł"))
            mock_router.assert_not_called()
            self.assertNotIn(chat_id, bot.pending_expense)
        finally:
            bot.pending_alias_action.pop(chat_id, None)

    # Case 3 — invalid category shown explicitly in preview, defaults to "Інше"
    def test_invalid_category_defaults_to_inshe_and_is_shown_in_preview(self):
        chat_id = 930005
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_ok_router_result(category="Спорт")):
            _call_webhook(_make_update(930000005, chat_id, "86 zł на спортзал"))
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["category"], DEFAULT_EXPENSE_CATEGORY)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Інше" in t for t in sent_texts))

    # Case 4 (webhook level)
    def test_future_date_blocks_preview(self):
        chat_id = 930006
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_ok_router_result(expense_date="2099-01-01")):
            _call_webhook(_make_update(930000006, chat_id, "86 zł завтра"))
        self.assertNotIn(chat_id, bot.pending_expense)

    # Case 5 (webhook level)
    def test_unresolved_fragments_blocks_preview_and_asks_for_clarification(self):
        chat_id = 930007
        with patch.object(bot, "_ask_gemini_expense_router",
                           return_value=_ok_router_result(unresolved_fragments=["незрозуміла сума"])):
            _call_webhook(_make_update(930000007, chat_id, "86 zł на щось незрозуміле"))
        self.assertNotIn(chat_id, bot.pending_expense)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("незрозуміла сума" in t for t in sent_texts))

    # Case 6
    def test_expense_not_created_before_confirm(self):
        chat_id = 930008
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_ok_router_result()):
            with patch.object(bot, "add_expense") as mock_add:
                _call_webhook(_make_update(930000008, chat_id, "86,40 zł"))
                mock_add.assert_not_called()
        self.assertIn(chat_id, bot.pending_expense)

    # Case 7 / 8
    def test_confirm_creates_expense_exactly_once(self):
        chat_id = 930009
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("86.40"), "currency": "PLN",
            "category": "Продукти", "description": "Biedronka", "expense_date": date(2026, 7, 3),
            "origin": "global",
        }
        with patch.object(bot, "add_expense") as mock_add:
            _call_webhook(_make_update(930000009, chat_id, "✅ Так, додати"))
            # Repeated confirm (e.g. duplicate Telegram delivery of a
            # different update_id for the same button press) must not re-apply.
            _call_webhook(_make_update(930000010, chat_id, "✅ Так, додати"))
            mock_add.assert_called_once_with(1, 10, Decimal("86.40"), "PLN", "Продукти", "Biedronka", date(2026, 7, 3))
        self.assertNotIn(chat_id, bot.pending_expense)

    # Case 9
    def test_cancel_writes_nothing(self):
        chat_id = 930011
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("14.00"), "currency": "PLN",
            "category": "Кафе / ресторани", "description": "Кава", "expense_date": date(2026, 7, 3),
            "origin": "global",
        }
        with patch.object(bot, "add_expense") as mock_add:
            _call_webhook(_make_update(930000011, chat_id, "❌ Скасувати"))
            mock_add.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Додавання витрати скасовано" in t for t in sent_texts))

    # Case 10
    def test_stale_confirm_without_pending_state_writes_nothing(self):
        chat_id = 930012
        self.assertNotIn(chat_id, bot.pending_expense)
        with patch.object(bot, "add_expense") as mock_add:
            _call_webhook(_make_update(930000012, chat_id, "✅ Так, додати"))
            mock_add.assert_not_called()
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Немає активної дії для підтвердження" in t for t in sent_texts))

    # Origin: returning to the dedicated expenses submenu after confirm
    def test_confirm_from_expenses_menu_returns_expenses_keyboard(self):
        chat_id = 930013
        bot.active_list_context[chat_id] = "expenses"
        bot.pending_expense[chat_id] = {
            "household_id": 1, "user_db_id": 10, "amount": Decimal("14.00"), "currency": "PLN",
            "category": "Кафе / ресторани", "description": "Кава", "expense_date": date(2026, 7, 3),
            "origin": "expenses_menu",
        }
        try:
            with patch.object(bot, "add_expense"):
                _call_webhook(_make_update(930000013, chat_id, "✅ Так, додати"))
            last_call = self.mock_send.call_args_list[-1]
            self.assertEqual(last_call.kwargs.get("reply_markup"), bot.EXPENSES_KEYBOARD)
        finally:
            bot.active_list_context.pop(chat_id, None)


# =========================
# V1.4.2 — expense description cleaning, full webhook path
# =========================
class TestExpenseDescriptionCleaningWebhookFlow(unittest.TestCase):
    """Live bug: Gemini's own `description` field can come back as the WHOLE
    raw command instead of a clean name — these drive the full webhook()
    dispatch with a Gemini router mock that returns exactly that dirty
    value, proving _validate_expense_router_result's _clean_expense_
    description call is what actually protects the stored/previewed name.

    Deliberately does NOT subclass TestExpenseWebhookFlow — unittest would
    then also re-run every INHERITED test method under this class, reusing
    the exact same chat_id/update_id pairs already consumed by that class's
    own run and tripping bot.py's process-wide update_id dedup cache (same
    hardcoded ids "already seen" -> webhook() silently no-ops). Same mocks,
    copied setUp/tearDown instead."""

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
        for d in (bot.pending_expense, bot.pending_delete_batch,
                  bot.pending_alias_action, bot.active_list_context, bot.saved_list_context):
            d.clear()

    def _assert_clean_description(self, chat_id, update_id, user_text, dirty_description, expected_clean):
        with patch.object(
            bot, "_ask_gemini_expense_router",
            return_value=_ok_router_result(description=dirty_description),
        ):
            _call_webhook(_make_update(update_id, chat_id, user_text))
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["description"], expected_clean)
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any(f"Опис: {expected_clean}" in t for t in sent_texts))
        self.assertFalse(any(dirty_description in t for t in sent_texts if dirty_description != expected_clean))

    # 8. "Запиши 120 zł за інтернет" — Gemini returns the whole command back
    # as description; the stored/previewed name must still be "інтернет".
    def test_zapysy_za_internet_stores_clean_description(self):
        self._assert_clean_description(
            941101, 941100001, "Запиши 120 zł за інтернет", "Запиши 120 zł за інтернет", "інтернет",
        )

    # 9.
    def test_zapysy_na_internet_stores_clean_description(self):
        self._assert_clean_description(
            941102, 941100002, "Запиши 120 zł на інтернет", "Запиши 120 zł на інтернет", "інтернет",
        )

    # 10.
    def test_bare_amount_and_preposition_stores_clean_description(self):
        self._assert_clean_description(
            941103, 941100003, "120 zł за інтернет", "120 zł за інтернет", "інтернет",
        )

    # 11.
    def test_trailing_amount_stores_clean_description(self):
        self._assert_clean_description(
            941104, 941100004, "Інтернет 120 zł", "Інтернет 120 zł", "Інтернет",
        )

    # 12. Regression — Gemini ALREADY returning a clean description (the
    # normal/expected case) is unaffected by the new cleanup.
    def test_biedronka_with_category_dash_stores_clean_merchant_title(self):
        chat_id = 941105
        with patch.object(
            bot, "_ask_gemini_expense_router",
            return_value=_ok_router_result(description="Biedronka", category="Продукти"),
        ):
            _call_webhook(_make_update(941100005, chat_id, "Biedronka 86,40 zł — продукти"))
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["description"], "Biedronka")
        self.assertEqual(bot.pending_expense[chat_id]["category"], "Продукти")

    # 13.
    def test_kava_stores_clean_description(self):
        self._assert_clean_description(941106, 941100006, "Кава 14 zł", "Кава 14 zł", "Кава")

    # 14. Regression: confirming after a cleaned description still writes
    # exactly the clean text, and preview/confirm behavior is unaffected.
    def test_confirm_after_cleanup_writes_clean_description(self):
        chat_id = 941107
        with patch.object(
            bot, "_ask_gemini_expense_router",
            return_value=_ok_router_result(description="Запиши 120 zł за інтернет", category="Дім і рахунки"),
        ):
            _call_webhook(_make_update(941100007, chat_id, "Запиши 120 zł за інтернет"))
        self.assertEqual(bot.pending_expense[chat_id]["description"], "інтернет")
        with patch.object(bot, "add_expense") as mock_add:
            _call_webhook(_make_update(941100008, chat_id, "✅ Так, додати"))
            mock_add.assert_called_once_with(
                1, 10, Decimal("86.40"), "PLN", "Дім і рахунки", "інтернет", date.fromisoformat(_todays_warsaw_date_iso()),
            )
        self.assertNotIn(chat_id, bot.pending_expense)


# =========================
# Expenses Hub V1 — "💸 Витрати" read-only dashboard
# =========================
class TestExpensesHubWebhookFlow(unittest.TestCase):
    """Full webhook() dispatch for the "💸 Витрати" button — everything
    network/DB-facing patched. Does NOT subclass TestExpenseWebhookFlow (see
    that class's own V1.4.2 sibling's docstring for why): unittest would
    re-run its inherited tests here too, reusing the same hardcoded chat_id/
    update_id pairs and tripping bot.py's process-wide update_id dedup
    cache."""

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

        patcher_add = patch.object(bot, "add_expense")
        self.mock_add_expense = patcher_add.start()
        self.addCleanup(patcher_add.stop)

    def tearDown(self):
        for d in (bot.pending_expense, bot.active_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]

    def _recent(self):
        return [
            {"description": "Інтернет", "category": "Дім і рахунки", "amount": Decimal("120.00")},
            {"description": "Кава", "category": "Кафе / ресторани", "amount": Decimal("14.00")},
            {"description": "Biedronka", "category": "Продукти", "amount": Decimal("86.40")},
        ]

    # 1. Shows the hub, not only the plain instructions.
    def test_expenses_button_shows_hub_not_only_instructions(self):
        chat_id = 942101
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("134.00")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("1240.00"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=self._recent()):
            _call_webhook(_make_update(942100001, chat_id, "💸 Витрати"))
        texts = self._sent_texts()
        self.assertTrue(any("💸 Витрати" in t and "Останні витрати:" in t for t in texts))
        self.assertFalse(any(t == bot.EXPENSES_INTRO_TEXT for t in texts))

    # 2/3. Today total and month total both present.
    def test_hub_includes_today_and_month_totals(self):
        chat_id = 942102
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("134.00")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("1240.00"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=[]):
            _call_webhook(_make_update(942100002, chat_id, "💸 Витрати"))
        texts = self._sent_texts()
        self.assertTrue(any("Сьогодні: 134,00 zł" in t for t in texts))
        self.assertTrue(any("Цього місяця: 1240,00 zł" in t for t in texts))

    # 4. Last 5 expenses requested, newest first (get_recent_expenses's own
    # ordering — the hub only asks for limit=5, never more).
    def test_hub_requests_last_five_expenses(self):
        chat_id = 942103
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("0")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("0"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=self._recent()) as mock_recent:
            _call_webhook(_make_update(942100003, chat_id, "💸 Витрати"))
        mock_recent.assert_called_once_with(1, limit=5)
        texts = self._sent_texts()
        self.assertTrue(any("1. Інтернет — 120,00 zł" in t and "2. Кава — 14,00 zł" in t for t in texts))

    # 5. No expenses yet — graceful message, no crash.
    def test_hub_handles_no_expenses_gracefully(self):
        chat_id = 942104
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("0")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("0"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=[]):
            _call_webhook(_make_update(942100004, chat_id, "💸 Витрати"))
        texts = self._sent_texts()
        self.assertTrue(any("Останніх витрат ще немає." in t for t in texts))

    # 6. Keeps the expenses submenu keyboard (navigation never lost).
    def test_hub_keeps_expenses_keyboard(self):
        chat_id = 942105
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("0")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("0"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=[]):
            _call_webhook(_make_update(942100005, chat_id, "💸 Витрати"))
        self.assertIn(bot.EXPENSES_KEYBOARD, self._reply_markups())

    # 7. Never calls Gemini just to render the hub.
    def test_hub_never_calls_gemini(self):
        chat_id = 942106
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("0")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("0"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=[]):
            _call_webhook(_make_update(942100006, chat_id, "💸 Витрати"))
        self.mock_call_gemini.assert_not_called()

    # 8. Never writes to the DB.
    def test_hub_never_writes_to_db(self):
        chat_id = 942107
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("0")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("0"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=[]):
            _call_webhook(_make_update(942100007, chat_id, "💸 Витрати"))
        self.mock_add_expense.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_expense)

    # 12. DB read failure -> controlled Ukrainian message, not a raw
    # exception/crash.
    def test_hub_db_failure_shows_controlled_message(self):
        chat_id = 942108
        with patch.object(bot, "get_expense_day_total", side_effect=Exception("boom")):
            _call_webhook(_make_update(942100008, chat_id, "💸 Витрати"))
        texts = self._sent_texts()
        self.assertTrue(any("Не вдалося показати витрати" in t for t in texts))
        self.assertIn(bot.EXPENSES_KEYBOARD, self._reply_markups())

    # Regression: expense add flow still works from inside the expenses
    # submenu after viewing the hub.
    def test_expense_add_still_works_after_viewing_hub(self):
        chat_id = 942109
        with patch.object(bot, "get_expense_day_total", return_value=Decimal("0")), \
             patch.object(bot, "get_expense_month_summary", return_value={"total": Decimal("0"), "by_category": {}}), \
             patch.object(bot, "get_recent_expenses", return_value=[]):
            _call_webhook(_make_update(942100009, chat_id, "💸 Витрати"))
        self.mock_send.reset_mock()
        with patch.object(bot, "_ask_gemini_expense_router", return_value=_ok_router_result(description="Кава", amount="14")):
            _call_webhook(_make_update(942100010, chat_id, "Кава 14 zł"))
        self.assertIn(chat_id, bot.pending_expense)
        self.assertEqual(bot.pending_expense[chat_id]["description"], "Кава")


# =========================
# 13 — household isolation at the SQL layer
# =========================
class FakeCursor:
    """Stands in for a psycopg cursor. Records every executed statement (in
    order, with params) and returns canned fetchone results — enough to
    verify SQL shape/scoping without a real Postgres."""

    def __init__(self, fetchone_results=None):
        self.queries = []
        self._fetchone_results = list(fetchone_results or [])

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

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


class TestAddExpenseHouseholdIsolation(unittest.TestCase):
    def test_add_expense_insert_is_scoped_and_parameterized_by_household_id(self):
        cursor = FakeCursor(fetchone_results=[(101,)])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            new_id = real_database.add_expense(
                household_id=7, user_db_id=3, amount=Decimal("10.00"), currency="PLN",
                category="Продукти", description="Тест", expense_date=date(2026, 7, 1),
            )
        self.assertEqual(new_id, 101)
        sql, params = cursor.queries[-1]
        self.assertIn("INSERT INTO expenses", sql)
        self.assertIn("%s", sql)
        self.assertEqual(params[0], 7)  # household_id is the first bound parameter
        self.assertTrue(conn.committed)

    def test_add_expense_never_leaks_between_households(self):
        cursor1 = FakeCursor(fetchone_results=[(1,)])
        conn1 = FakeConnection(cursor1)
        with patch.object(real_database, "get_connection", return_value=conn1):
            real_database.add_expense(
                household_id=1, user_db_id=1, amount=Decimal("5.00"), currency="PLN",
                category="Інше", description="", expense_date=date(2026, 7, 1),
            )
        cursor2 = FakeCursor(fetchone_results=[(2,)])
        conn2 = FakeConnection(cursor2)
        with patch.object(real_database, "get_connection", return_value=conn2):
            real_database.add_expense(
                household_id=2, user_db_id=1, amount=Decimal("5.00"), currency="PLN",
                category="Інше", description="", expense_date=date(2026, 7, 1),
            )
        # Each household's insert only ever carries that household's own id —
        # no shared/global state connects the two calls.
        self.assertEqual(cursor1.queries[-1][1][0], 1)
        self.assertEqual(cursor2.queries[-1][1][0], 2)


if __name__ == "__main__":
    unittest.main()
