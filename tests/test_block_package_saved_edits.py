import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. No test in this file calls real
# Gemini, Telegram, Render, or Supabase — every network-facing function
# (_ask_gemini_saved_list_router, send_message, the DB update helpers) is
# patched per-test below.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
from bot import (
    pending_saved_edit,
    saved_list_context,
    active_list_context,
    _saved_edit_text_has_unsafe_package_conversion,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _sausage_row():
    return {"id": 103, "name": "Сосиски", "category": "М'ясо та риба", "canonical_name": "сосиски",
             "quantity_value": 4.0, "quantity_unit": "шт.", "quantity_text": "4 шт.", "quantity_inferred": False}


def _banana_row():
    return {"id": 102, "name": "Банани", "category": "Фрукти та ягоди", "canonical_name": "банани",
             "quantity_value": 3.0, "quantity_unit": "шт.", "quantity_text": "3 шт.", "quantity_inferred": False}


def _milk_shopping_row():
    return {"id": 201, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
             "quantity_value": 1.0, "quantity_unit": "л", "quantity_text": "1 л", "quantity_inferred": False}


def _router_result(updates):
    return {
        "intent": "edit_saved_items", "action": None, "selected_numbers": [],
        "updates": updates, "merge_groups": [], "items": [], "consumptions": [],
        "operations": [], "unresolved_fragments": [], "unresolved_fragments_present": False,
    }


# =========================
# Pure gate function
# =========================
class TestSavedEditTextHasUnsafePackageConversion(unittest.TestCase):

    def test_package_word_plus_pieces_candidate_is_unsafe(self):
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        self.assertTrue(_saved_edit_text_has_unsafe_package_conversion("Сосиски — дві пачки", updates))

    def test_polish_package_word_plus_pieces_candidate_is_unsafe(self):
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        self.assertTrue(_saved_edit_text_has_unsafe_package_conversion("Sosyski — 2 paczki", updates))

    def test_no_package_word_is_safe(self):
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        self.assertFalse(_saved_edit_text_has_unsafe_package_conversion("Сосиски — 2 шт.", updates))

    def test_package_word_without_a_pieces_candidate_is_safe(self):
        # Package word present, but no candidate actually sets "шт." — e.g.
        # only the category changed — nothing dangerous to block.
        updates = [{"item_number": 1, "name": None, "quantity_text": None, "category": "Інше їстівне"}]
        self.assertFalse(_saved_edit_text_has_unsafe_package_conversion("Сосиски — це пачка", updates))


# =========================
# Webhook-level: natural saved-list edit flow (both inventory and shopping)
# =========================
class TestBlockPackageSavedEditsWebhookFlow(unittest.TestCase):

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
        for d in (pending_saved_edit, saved_list_context, active_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 1/2/3 — "Сосиски — дві пачки" against "Сосиски — 4 шт." must not
    # create a preview, pending_saved_edit, or call the DB update helper.
    def test_package_phrase_does_not_create_preview_or_pending_state(self):
        chat_id = 995001
        saved_list_context[chat_id] = "inventory_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                with patch.object(bot, "update_inventory_items_batch") as mock_update:
                    _call_webhook(_make_update(995000001, chat_id, "Сосиски — дві пачки"))
                    mock_update.assert_not_called()
        self.assertNotIn(chat_id, pending_saved_edit)

    # Case 4 — the message explains that packages don't convert to pieces
    def test_message_explains_packages_do_not_convert_to_pieces(self):
        chat_id = 995002
        saved_list_context[chat_id] = "inventory_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                _call_webhook(_make_update(995000002, chat_id, "Сосиски — дві пачки"))
        texts = self._sent_texts()
        self.assertTrue(any(
            "Не можу безпечно перетворити «дві пачки» на штуки." in t
            and "Пачка не дорівнює певній кількості товару." in t
            and "«Сосиски — 2 шт.»" in t
            and "«Купив дві пачки сосисок»" in t
            for t in texts
        ))

    # Case 5 — an ordinary "N шт." edit still works exactly as before
    def test_ordinary_pieces_edit_still_creates_preview(self):
        chat_id = 995003
        saved_list_context[chat_id] = "inventory_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        with patch.object(bot, "get_inventory_items", return_value=[_banana_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                _call_webhook(_make_update(995000003, chat_id, "Банани — 2 шт."))
        self.assertIn(chat_id, pending_saved_edit)
        texts = self._sent_texts()
        self.assertTrue(any("Банани — 3 шт." in t and "→" in t and "Банани — 2 шт." in t for t in texts))

    # Case 6 — Polish package phrasing is blocked the same way
    def test_polish_package_phrase_is_blocked(self):
        chat_id = 995004
        saved_list_context[chat_id] = "inventory_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                with patch.object(bot, "update_inventory_items_batch") as mock_update:
                    _call_webhook(_make_update(995000004, chat_id, "Sosyski — 2 paczki"))
                    mock_update.assert_not_called()
        self.assertNotIn(chat_id, pending_saved_edit)
        texts = self._sent_texts()
        self.assertTrue(any("Не можу безпечно перетворити" in t and "на штуки." in t for t in texts))

    # Case 7 — the same guard protects the saved shopping list
    def test_shopping_package_phrase_is_blocked(self):
        chat_id = 995005
        saved_list_context[chat_id] = "shopping_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        with patch.object(bot, "get_active_shopping_items", return_value=[_sausage_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                with patch.object(bot, "update_shopping_items_batch") as mock_update:
                    _call_webhook(_make_update(995000005, chat_id, "Сосиски — дві пачки"))
                    mock_update.assert_not_called()
        self.assertNotIn(chat_id, pending_saved_edit)
        texts = self._sent_texts()
        self.assertTrue(any("Не можу безпечно перетворити" in t for t in texts))

    # Case 8 — mixed request: the package-edit blocks the WHOLE request, no
    # partial application of the other, unrelated candidate update.
    def test_mixed_request_blocks_entirely_no_partial_apply(self):
        chat_id = 995006
        saved_list_context[chat_id] = "inventory_saved"
        updates = [
            {"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None},
            {"item_number": 2, "name": None, "quantity_text": "5 шт.", "category": None},
        ]
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row(), _banana_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                with patch.object(bot, "update_inventory_items_batch") as mock_update:
                    _call_webhook(_make_update(995000006, chat_id, "Сосиски — дві пачки, банани — 5 шт."))
                    mock_update.assert_not_called()
        self.assertNotIn(chat_id, pending_saved_edit)
        texts = self._sent_texts()
        self.assertTrue(any("Не можу безпечно перетворити" in t for t in texts))
        self.assertFalse(any("Банани" in t and "→" in t for t in texts))


if __name__ == '__main__':
    unittest.main()
