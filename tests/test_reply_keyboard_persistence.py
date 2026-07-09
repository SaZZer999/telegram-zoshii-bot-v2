"""Telegram reply-keyboard persistence fix (V1.4.2).

Live symptom: in Telegram Desktop, the bottom reply keyboard disappeared
after some flows and never came back — "⬅️ Головне меню" answered with
"Ось головне меню:" but no keyboard was attached to some ordinary/fallback
replies (general AI-chat, the destructive-guard clarification), so once any
earlier one-time keyboard collapsed, the user was left with no visible
keyboard at all until some OTHER handler happened to resend one.

Covers: /start and "⬅️ Головне меню" already attaching MAIN_KEYBOARD (no
regression), _run_general_ai_fallback and _route_destructive_bulk_guard NOW
attaching MAIN_KEYBOARD when no pending preview is active, and that an
ACTIVE preview (cleanup-admin rename/delete, historical undo) keeps its OWN
confirm/cancel keyboard untouched — never silently replaced by MAIN_KEYBOARD.

No real Gemini, Telegram, Render, or Supabase call happens anywhere in this
file.
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
from bot import (  # noqa: E402
    pending_cleanup_admin,
    pending_undo_action,
    MAIN_KEYBOARD,
    EXPENSES_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
    UNDO_PREVIEW_KEYBOARD,
    DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class ReplyKeyboardWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_cleanup_admin.clear()
        pending_undo_action.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_cleanup_admin.clear()
        pending_undo_action.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


class TestNavigationKeepsMainKeyboard(ReplyKeyboardWebhookTestCase):
    # 1. /start response includes main menu reply_markup.
    def test_start_includes_main_keyboard(self):
        chat_id = 780001
        _call_webhook(_make_update(780000001, chat_id, "/start"))
        self.assertIn(MAIN_KEYBOARD, self._reply_markups())

    # 2. "⬅️ Головне меню" response includes main menu reply_markup.
    def test_main_menu_button_includes_main_keyboard(self):
        chat_id = 780002
        _call_webhook(_make_update(780000002, chat_id, "⬅️ Головне меню"))
        texts = self._sent_texts()
        self.assertTrue(any("Ось головне меню:" == t for t in texts))
        self.assertIn(MAIN_KEYBOARD, self._reply_markups())

    # 3. "💸 Витрати" response includes the appropriate (expenses submenu)
    # reply_markup — never no keyboard at all — and that submenu keyboard
    # itself always carries "⬅️ Головне меню", so navigation is never lost.
    def test_expenses_button_includes_expenses_keyboard(self):
        chat_id = 780010
        _call_webhook(_make_update(780000010, chat_id, "💸 Витрати"))
        markups = self._reply_markups()
        self.assertIn(EXPENSES_KEYBOARD, markups)
        self.assertTrue(any(rm is not None for rm in markups))
        self.assertIn(["⬅️ Головне меню"], EXPENSES_KEYBOARD["keyboard"])


class TestGeneralAiFallbackKeepsMainKeyboard(ReplyKeyboardWebhookTestCase):
    # 3. General AI fallback response includes main menu reply_markup.
    def test_general_ai_answer_includes_main_keyboard(self):
        chat_id = 780003
        with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн реагує на кислоту."):
            _call_webhook(_make_update(780000003, chat_id, "Поясни коротко, чому молоко згортається в каві?"))
        self.assertIn(MAIN_KEYBOARD, self._reply_markups())

    def test_general_ai_answer_omits_keyboard_when_a_preview_is_active(self):
        # An active pending_batch/pending_inventory_batch-style preview
        # reaching this fallback via RouteOutcome.DIRECT_GENERAL_AI_FALLBACK
        # must never have its OWN keyboard silently replaced.
        chat_id = 780004
        bot.pending_batch[chat_id] = {
            "items": [], "household_id": 1, "user_db_id": 10, "ignored_items": [],
        }
        try:
            with patch.object(bot, "call_gemini", return_value="stub"):
                bot._run_general_ai_fallback(chat_id, "щось геть не по темі")
            self.assertIsNone(self._reply_markups()[-1])
        finally:
            bot.pending_batch.pop(chat_id, None)


class TestDestructiveGuardKeepsMainKeyboard(ReplyKeyboardWebhookTestCase):
    # 4. Controlled destructive guard response "Видали все" includes main
    # menu reply_markup when no pending preview exists.
    def test_destructive_guard_includes_main_keyboard_without_active_preview(self):
        chat_id = 780005
        with patch.object(bot, "call_gemini") as mock_gemini:
            _call_webhook(_make_update(780000005, chat_id, "Видали все"))
        mock_gemini.assert_not_called()
        self.assertEqual(self._sent_texts(), [DESTRUCTIVE_BULK_HOUSEHOLD_GUARD_MSG])
        self.assertIn(MAIN_KEYBOARD, self._reply_markups())

    def test_destructive_guard_does_not_override_active_preview_keyboard(self):
        chat_id = 780006
        pending_cleanup_admin[chat_id] = {
            "action": "delete", "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_id": 21, "target": {
                "item_id": 21, "quantity_value": Decimal("1"), "quantity_unit": "шт.",
                "name": "Молоко", "canonical_name": "молоко",
            },
        }
        _call_webhook(_make_update(780000006, chat_id, "Видали все"))
        self.assertNotIn(MAIN_KEYBOARD, self._reply_markups())
        self.assertIn(chat_id, pending_cleanup_admin)


class TestActivePreviewKeyboardsUnchanged(ReplyKeyboardWebhookTestCase):
    # 5. Active cleanup-admin preview still uses confirm/cancel keyboard,
    # not main menu.
    def test_cleanup_admin_rename_preview_uses_its_own_keyboard(self):
        chat_id = 780007
        cheese_row = {
            "id": 5, "name": "ser", "canonical_name": "сир", "category": "Молочне та яйця",
            "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт.",
        }
        with patch.object(bot, "get_inventory_items", return_value=[cheese_row]):
            _call_webhook(_make_update(780000007, chat_id, "перейменуй ser на сир"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())
        self.assertNotIn(MAIN_KEYBOARD, self._reply_markups())

    # 6. Active undo preview still uses undo confirm/cancel keyboard, not
    # main menu.
    def test_undo_preview_uses_its_own_keyboard(self):
        chat_id = 780008
        action = {"id": 99, "summary": {"inventory_changes": [], "shopping_changes": [], "expense_change": None}}
        with patch.object(bot, "get_latest_undoable_action", return_value=action):
            _call_webhook(_make_update(780000008, chat_id, "↩️ Скасувати останню дію"))
        self.assertIn(chat_id, pending_undo_action)
        self.assertIn(UNDO_PREVIEW_KEYBOARD, self._reply_markups())
        self.assertNotIn(MAIN_KEYBOARD, self._reply_markups())

    # 7. Cancel from a cleanup-admin preview returns the main menu keyboard
    # (existing project style — household_router.origin_keyboard("global")
    # is MAIN_KEYBOARD).
    def test_cancel_from_preview_returns_main_keyboard(self):
        chat_id = 780009
        pending_cleanup_admin[chat_id] = {
            "action": "delete", "household_id": 1, "user_db_id": 10, "origin": "global",
            "item_id": 21, "target": {
                "item_id": 21, "quantity_value": Decimal("1"), "quantity_unit": "шт.",
                "name": "Молоко", "canonical_name": "молоко",
            },
        }
        _call_webhook(_make_update(780000009, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertIn(MAIN_KEYBOARD, self._reply_markups())


if __name__ == "__main__":
    unittest.main()
