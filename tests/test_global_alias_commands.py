import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No real Gemini/Telegram/Supabase
# call happens anywhere in this file — every network-facing bot.py function
# (get_household_and_user, _ask_gemini_alias_router, send_message,
# call_gemini, the alias CRUD wrappers) is patched per-test.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import _alias_command_gate


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


class TestAliasCommandGate(unittest.TestCase):
    """Cases 1, 3, 4, 5 — the narrow local gate itself, no routing involved."""

    # Case 1
    def test_remember_with_comma_and_equals_passes_gate(self):
        self.assertTrue(_alias_command_gate("Запам'ятай, що сливки = Вершки"))

    def test_remember_without_comma_passes_gate(self):
        self.assertTrue(_alias_command_gate("Запам'ятай сливки = Вершки 30%"))

    def test_change_command_passes_gate(self):
        self.assertTrue(_alias_command_gate("Зміни: сливки = Вершки 30%"))

    # Case 3
    def test_show_my_names_passes_gate(self):
        self.assertTrue(_alias_command_gate("Покажи мої назви"))

    def test_show_product_names_passes_gate(self):
        self.assertTrue(_alias_command_gate("Покажи назви товарів"))

    # Case 4
    def test_forget_command_passes_gate(self):
        self.assertTrue(_alias_command_gate("Забудь, що сливки"))

    # Case 5
    def test_ordinary_question_does_not_pass_gate(self):
        self.assertFalse(_alias_command_gate("Що приготувати з курки?"))

    def test_ordinary_shopping_text_does_not_pass_gate(self):
        self.assertFalse(_alias_command_gate("Молоко 2 л, хліб"))

    def test_bare_forget_without_a_name_does_not_pass_gate(self):
        self.assertFalse(_alias_command_gate("Забудь"))

    def test_remember_without_equals_does_not_pass_gate(self):
        self.assertFalse(_alias_command_gate("Запам'ятай купити молоко"))

    def test_empty_text_does_not_pass_gate(self):
        self.assertFalse(_alias_command_gate(""))
        self.assertFalse(_alias_command_gate("   "))


class TestGlobalAliasCommandRouting(unittest.TestCase):
    """Cases 2, 6, 7, 8, 9, 10 — full webhook() dispatch, everything network-
    facing patched. Each test uses its own chat_id/update_id to stay isolated."""

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
        for d in (bot.pending_alias_action, bot.pending_delete_batch,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    def _create_or_update_router_result(self, alias_text="сливки", target="Вершки"):
        return {
            "intent": "create_or_update", "alias_text": alias_text,
            "target_display_name": target, "unresolved_fragments": [],
        }

    # Case 2
    def test_remember_from_main_menu_builds_preview_not_ai_chat(self):
        chat_id = 910001
        with patch.object(bot, "_ask_gemini_alias_router", return_value=self._create_or_update_router_result()):
            with patch.object(bot, "get_household_alias", return_value=None):
                _call_webhook(_make_update(910000001, chat_id, "Запам'ятай, що сливки = Вершки"))

        self.mock_call_gemini.assert_not_called()
        self.mock_saved_router.assert_not_called()
        self.assertIn(chat_id, bot.pending_alias_action)
        self.assertEqual(bot.pending_alias_action[chat_id]["kind"], "create")
        self.assertEqual(bot.pending_alias_action[chat_id]["origin"], "global")
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Запам'ятати домашню назву" in t for t in sent_texts))
        bot.pending_alias_action.pop(chat_id, None)

    # Case 6
    def test_alias_command_does_not_interrupt_active_confirm_preview(self):
        chat_id = 910002
        bot.pending_delete_batch[chat_id] = {
            "items": [{"id": 1, "name": "Хліб"}], "household_id": 1, "user_db_id": 10,
        }
        try:
            with patch.object(bot, "_ask_gemini_alias_router") as mock_alias_router:
                _call_webhook(_make_update(910000002, chat_id, "Запам'ятай, що сливки = Вершки"))
            # The alias router must never even be consulted — the active
            # pending_delete_batch confirm takes priority over the gate.
            mock_alias_router.assert_not_called()
            self.assertNotIn(chat_id, bot.pending_alias_action)
        finally:
            bot.pending_delete_batch.pop(chat_id, None)
            bot.pending_alias_action.pop(chat_id, None)

    # Case 7
    def test_alias_not_created_before_confirm(self):
        chat_id = 910003
        with patch.object(bot, "_ask_gemini_alias_router", return_value=self._create_or_update_router_result()):
            with patch.object(bot, "get_household_alias", return_value=None):
                with patch.object(bot, "create_or_update_household_alias") as mock_create:
                    _call_webhook(_make_update(910000003, chat_id, "Запам'ятай, що сливки = Вершки"))
                    mock_create.assert_not_called()
        self.assertIn(chat_id, bot.pending_alias_action)
        bot.pending_alias_action.pop(chat_id, None)

    # Case 8
    def test_confirm_creates_alias_exactly_once(self):
        chat_id = 910004
        bot.pending_alias_action[chat_id] = {
            "kind": "create", "household_id": 1, "user_db_id": 10,
            "alias_text": "сливки", "target_display_name": "Вершки", "origin": "global",
        }
        try:
            with patch.object(bot, "create_or_update_household_alias") as mock_create:
                with patch.object(bot, "list_household_aliases", return_value=[]):
                    _call_webhook(_make_update(910000004, chat_id, "✅ Так, запам'ятати"))
                    # Repeated confirm (e.g. duplicate Telegram delivery of a
                    # different update_id for the same button press) must not re-apply.
                    _call_webhook(_make_update(910000005, chat_id, "✅ Так, запам'ятати"))
                    mock_create.assert_called_once()
            self.assertNotIn(chat_id, bot.pending_alias_action)
        finally:
            bot.pending_alias_action.pop(chat_id, None)

    # Case 9
    def test_global_alias_command_does_not_clear_list_context(self):
        chat_id = 910005
        bot.active_list_context[chat_id] = "shopping"
        bot.saved_list_context[chat_id] = "shopping_saved"
        try:
            with patch.object(bot, "_ask_gemini_alias_router", return_value=self._create_or_update_router_result()):
                with patch.object(bot, "get_household_alias", return_value=None):
                    _call_webhook(_make_update(910000006, chat_id, "Запам'ятай, що сливки = Вершки"))
            self.assertEqual(bot.active_list_context.get(chat_id), "shopping")
            self.assertEqual(bot.saved_list_context.get(chat_id), "shopping_saved")
            self.mock_saved_router.assert_not_called()
        finally:
            bot.active_list_context.pop(chat_id, None)
            bot.saved_list_context.pop(chat_id, None)
            bot.pending_alias_action.pop(chat_id, None)

    # Case 10
    def test_invalid_router_result_does_not_fall_back_to_ai_chat(self):
        chat_id = 910006
        none_result = {"intent": "none", "alias_text": None, "target_display_name": None, "unresolved_fragments": []}
        with patch.object(bot, "_ask_gemini_alias_router", return_value=none_result):
            _call_webhook(_make_update(910000007, chat_id, "Забудь, що сливки"))

        self.mock_call_gemini.assert_not_called()
        self.mock_saved_router.assert_not_called()
        sent_texts = [call.args[1] for call in self.mock_send.call_args_list]
        self.assertTrue(any("Не зміг зрозуміти домашню назву" in t for t in sent_texts))


if __name__ == "__main__":
    unittest.main()
