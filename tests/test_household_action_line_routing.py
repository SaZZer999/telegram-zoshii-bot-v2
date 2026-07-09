"""V1.2 bugfix: bot-preview-style/infinitive "Додати X — N шт." action lines
(and standalone "🛒 Покупки"/"🧊 Запаси" destination headers) must never fall
through to the general AI-chat fallback — see household_router.py's
_ADD_VERB_RE/detect_header_add_destination and bot.py's
_looks_like_unrouted_household_action. Mirrors tests/test_global_bare_add.py
and tests/test_global_explicit_add.py's setup/conventions exactly, since
these are the same routes, just reached by a wider verb/header shape.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: F401 — import side effect wires household_router.configure(...)
import household_router
from bot import (
    pending_global_household,
    pending_inventory_quantity_clarification,
    pending_add_destination_clarification,
    active_list_context,
    saved_list_context,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _tea_item():
    return {
        "items": [{"name": "Тестовий чай", "quantity_text": "1 шт.", "category": "Напої"}],
        "unresolved_fragments": [],
    }


def _three_tea_items():
    return {
        "items": [
            {"name": "Тестовий чай", "quantity_text": "1 шт.", "category": "Напої"},
            {"name": "Зелений чай", "quantity_text": "1 шт.", "category": "Напої"},
            {"name": "Мисливські ковбаски", "quantity_text": "дві пачки", "category": "М'ясо та риба"},
        ],
        "unresolved_fragments": [],
    }


def _coconut_milk_item():
    return {
        "items": [{"name": "Кокосове молоко", "quantity_text": "2 л", "category": "Молочне та яйця"}],
        "unresolved_fragments": [],
    }


class _BaseHouseholdActionLineTestCase(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_items = patch.object(household_router, "_ask_gemini_explicit_add_items")
        self.mock_items = patcher_items.start()
        self.addCleanup(patcher_items.stop)

        patcher_hr = patch.object(household_router, "_ask_gemini_household_router")
        self.mock_hr = patcher_hr.start()
        self.addCleanup(patcher_hr.stop)

        patcher_gemini_chat = patch.object(bot, "call_gemini")
        self.mock_call_gemini = patcher_gemini_chat.start()
        self.addCleanup(patcher_gemini_chat.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations")
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

        patcher_inv = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory = patcher_inv.start()
        self.addCleanup(patcher_inv.stop)

    def tearDown(self):
        for d in (
            pending_global_household, pending_inventory_quantity_clarification,
            pending_add_destination_clarification, active_list_context, saved_list_context,
        ):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestInfinitiveAddNeverReachesGeneralAI(_BaseHouseholdActionLineTestCase):
    # 1. A single preview-style "Додати X — N шт." line must never answer
    # with the general AI-chat's "I have no DB access" reply — no active
    # menu means the destination is unknown, so it asks instead.
    def test_single_line_asks_destination_instead_of_general_ai(self):
        chat_id = 999001
        self.mock_items.return_value = _tea_item()
        _call_webhook(_make_update(1999000001, chat_id, "Додати Тестовий чай — 1 шт."))
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("Куди додати ці позиції?" in t for t in texts))
        self.assertFalse(any("не маю доступу" in t for t in texts))

    # 2. Multi-line preview-style block (three "Додати ..." lines) must also
    # never fall into general AI-chat.
    def test_multi_line_block_asks_destination_instead_of_general_ai(self):
        chat_id = 999002
        self.mock_items.return_value = _three_tea_items()
        text = (
            "Додати Тестовий чай — 1 шт.\n"
            "Додати Зелений чай — 1 шт.\n"
            "Додати Мисливські ковбаски — дві пачки"
        )
        _call_webhook(_make_update(1999000002, chat_id, text))
        self.mock_call_gemini.assert_not_called()
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertEqual(len(pending_add_destination_clarification[chat_id]["validated_items"]), 3)
        texts = self._sent_texts()
        self.assertTrue(any("Куди додати ці позиції?" in t for t in texts))

    # 3. Once a menu already implies a destination, the same infinitive line
    # routes straight to a preview instead of asking.
    def test_active_shopping_menu_routes_directly(self):
        chat_id = 999003
        active_list_context[chat_id] = "shopping"
        self.mock_items.return_value = _tea_item()
        _call_webhook(_make_update(1999000003, chat_id, "Додати Тестовий чай — 1 шт."))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 1)
        self.assertNotIn(chat_id, pending_add_destination_clarification)


class TestHeaderDestinationLine(_BaseHouseholdActionLineTestCase):
    # 4. A "🛒 Покупки" header line makes the destination shopping.
    def test_shopping_header_creates_shopping_preview(self):
        chat_id = 999010
        self.mock_items.return_value = _tea_item()
        text = "🛒 Покупки\nДодати Тестовий чай — 1 шт."
        _call_webhook(_make_update(1999000010, chat_id, text))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 1)
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        # The item text handed to Gemini has the header line and the
        # per-line "Додати" verb noise stripped — never raw "🛒 Покупки".
        called_item_text = self.mock_items.call_args[0][0]
        self.assertNotIn("Покупки", called_item_text)
        self.assertNotIn("Додати", called_item_text)

    # 5. A "🧊 Запаси" header line makes the destination inventory.
    def test_inventory_header_creates_inventory_preview(self):
        chat_id = 999011
        self.mock_items.return_value = _coconut_milk_item()
        text = "🧊 Запаси\nДодати Кокосове молоко — 2 л"
        _call_webhook(_make_update(1999000011, chat_id, text))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_shopping_items"], [])
        self.assertEqual(len(payload["add_inventory_items"]), 1)


class TestUnroutedHouseholdActionVerbsNeverReachGeneralAI(_BaseHouseholdActionLineTestCase):
    # Safety net (household_router.py has no dedicated deterministic route
    # for "Прибрати"/"Використати" yet): still must never produce the "I
    # have no DB access" general AI-chat reply.
    def test_pribraty_line_gets_controlled_clarification_not_general_ai(self):
        chat_id = 999020
        _call_webhook(_make_update(1999000020, chat_id, "Прибрати Чай — 1 шт."))
        self.mock_call_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(bot.UNROUTED_HOUSEHOLD_ACTION_MSG == t for t in texts))
        self.assertFalse(any("не маю доступу" in t for t in texts))

    def test_vykorystaty_line_gets_controlled_clarification_not_general_ai(self):
        chat_id = 999021
        _call_webhook(_make_update(1999000021, chat_id, "Використати Молоко — 500 мл"))
        self.mock_call_gemini.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any(bot.UNROUTED_HOUSEHOLD_ACTION_MSG == t for t in texts))


if __name__ == "__main__":
    unittest.main()
