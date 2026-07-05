import sys
import os
import json
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot, same as every other test file
# in this suite — avoids bot.py's module-level init_db()/Groq() calls trying
# to reach a real service at import time. Every network-facing function
# (call_gemini, send_message, DB writes) is patched per-test below — no test
# in this file calls real Gemini, Telegram, Render, or Supabase.
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
)

NOW = None  # not needed — no datetime-dependent assertions in this file


def _gemini_json(items, ignored_items=None):
    return json.dumps({"items": items, "ignored_items": ignored_items or []})


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
             "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_text": "2 шт.", "quantity_inferred": False}


# =========================
# Pure parse_inventory_list_with_gemini tests — call_gemini itself is mocked,
# nothing reaches a real Gemini/HTTP endpoint.
# =========================
class TestParseInventoryListWithGemini(unittest.TestCase):

    # Case 1/2/3 — "дві пачки сосисок" must never become "2 шт."
    def test_package_phrase_kept_as_literal_quantity_text(self):
        raw = _gemini_json([
            {"name": "Сосиски", "quantity_text": "дві пачки", "category": "М'ясо та риба",
             "was_corrected": False, "is_consumable": True},
        ])
        with patch.object(bot, "call_gemini", return_value=raw):
            result = bot.parse_inventory_list_with_gemini("дві пачки сосисок")
        self.assertIsNotNone(result)
        item = result["items"][0]
        self.assertEqual(item["name"], "Сосиски")
        self.assertEqual(item["quantity_text"], "дві пачки")
        self.assertIsNone(item["quantity_value"])
        self.assertIsNone(item["quantity_unit"])
        self.assertFalse(item["quantity_inferred"])
        self.assertNotIn("дві пачки", item["name"])

    def test_single_pack_phrase_also_kept_literal(self):
        raw = _gemini_json([
            {"name": "Макарони", "quantity_text": "пачка", "category": "Крупи макарони та борошно",
             "was_corrected": False, "is_consumable": True},
        ])
        with patch.object(bot, "call_gemini", return_value=raw):
            result = bot.parse_inventory_list_with_gemini("пачка макаронів")
        item = result["items"][0]
        self.assertEqual(item["name"], "Макарони")
        self.assertEqual(item["quantity_text"], "пачка")
        self.assertIsNone(item["quantity_value"])
        self.assertIsNone(item["quantity_unit"])

    # Case 6 — explicit bare number preserved
    def test_explicit_bare_number_stays_pieces_not_inferred(self):
        raw = _gemini_json([
            {"name": "Банани", "quantity_text": "3", "category": "Фрукти та ягоди",
             "was_corrected": False, "is_consumable": True},
        ])
        with patch.object(bot, "call_gemini", return_value=raw):
            result = bot.parse_inventory_list_with_gemini("3 банани")
        item = result["items"][0]
        self.assertEqual(item["name"], "Банани")
        self.assertEqual(item["quantity_value"], Decimal("3"))
        self.assertEqual(item["quantity_unit"], "шт.")
        self.assertFalse(item["quantity_inferred"])

    # Case 7 — approximate word-number preserved
    def test_word_number_pair_resolves_to_two_pieces_inferred(self):
        raw = _gemini_json([
            {"name": "Сосиски", "quantity_text": "пару", "category": "М'ясо та риба",
             "was_corrected": False, "is_consumable": True},
        ])
        with patch.object(bot, "call_gemini", return_value=raw):
            result = bot.parse_inventory_list_with_gemini("пару сосисок")
        item = result["items"][0]
        self.assertEqual(item["name"], "Сосиски")
        self.assertEqual(item["quantity_value"], Decimal("2"))
        self.assertEqual(item["quantity_unit"], "шт.")
        self.assertTrue(item["quantity_inferred"])

    # Case 8 — precise weight quantity does not regress
    def test_precise_weight_quantity_not_regressed(self):
        raw = _gemini_json([
            {"name": "Шафран", "quantity_text": "0,00011 г", "category": "Інше їстівне",
             "was_corrected": False, "is_consumable": True},
        ])
        with patch.object(bot, "call_gemini", return_value=raw):
            result = bot.parse_inventory_list_with_gemini("0,00011 г шафрану")
        item = result["items"][0]
        self.assertEqual(item["name"], "Шафран")
        self.assertAlmostEqual(item["quantity_value"], 0.00011)
        self.assertEqual(item["quantity_unit"], "г")
        self.assertFalse(item["quantity_inferred"])

    # Safety net — if Gemini still leaks the quantity phrase into `name`
    # (name="дві пачки сосисок", quantity_text=""), no broken preview must
    # ever be created: the item is dropped into ignored_items instead.
    def test_leaked_quantity_in_name_is_blocked_not_shown_as_broken_item(self):
        raw = _gemini_json([
            {"name": "дві пачки сосисок", "quantity_text": "", "category": "М'ясо та риба",
             "was_corrected": False, "is_consumable": True},
        ])
        with patch.object(bot, "call_gemini", return_value=raw):
            result = bot.parse_inventory_list_with_gemini("дві пачки сосисок")
        self.assertEqual(result["items"], [])
        self.assertIn("дві пачки сосисок", result["ignored_items"])


# =========================
# Webhook-level: legacy "➕ Додати продукти" add flow end-to-end, including
# the Representation Guard preview/confirm behavior.
# =========================
class TestLegacyInventoryAddWebhookFlow(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # bot.py binds StaleSnapshotError to whatever `database` was mocked
        # to at import time; not needed by these tests (no stale-target
        # scenario is exercised here), but keep the real class available in
        # case a future case in this file needs it.
        cls._original_stale_error = bot.StaleSnapshotError

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

    # Case 4/5 — existing "Сосиски — 2 шт." + new "дві пачки сосисок" must
    # show the exact separate-record warning and stay pending (no merge).
    def test_existing_pieces_row_plus_package_phrase_shows_separate_warning(self):
        chat_id = 993001
        inventory_mode[chat_id] = "adding"
        raw = _gemini_json([
            {"name": "Сосиски", "quantity_text": "дві пачки", "category": "М'ясо та риба",
             "was_corrected": False, "is_consumable": True},
        ])
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row()]):
            with patch.object(bot, "call_gemini", return_value=raw):
                _call_webhook(_make_update(993000001, chat_id, "Купив дві пачки сосисок"))
        self.assertIn(chat_id, pending_inventory_batch)
        texts = self._sent_texts()
        self.assertTrue(any(
            "⚠️ Сосиски вже є у запасах: 2 шт." in t
            and "Нове надходження: дві пачки." in t
            and "буде збережено окремою позицією, без об'єднання." in t
            and "Сосиски — дві пачки" in t
            for t in texts
        ))
        item = pending_inventory_batch[chat_id]["items"][0]
        self.assertEqual(item["quantity_text"], "дві пачки")
        self.assertIsNone(item["quantity_value"])
        self.assertIsNone(item["quantity_unit"])

    # Case 5 — confirming does not touch the old "2 шт." row and inserts the
    # package quantity as a separate row (no merge arithmetic applied).
    def test_confirm_keeps_old_row_untouched_and_adds_separate_package_row(self):
        chat_id = 993002
        pending_inventory_batch[chat_id] = {
            "items": [{
                "name": "Сосиски", "category": "М'ясо та риба", "canonical_name": "сосиски",
                "quantity_text": "дві пачки", "quantity_value": None, "quantity_unit": None,
                "quantity_inferred": False, "was_corrected": False,
            }],
            "ignored_items": [], "household_id": 1, "user_db_id": 10, "inventory_targets": [],
        }
        with patch.object(bot, "add_inventory_items_batch", return_value=1) as mock_add:
            _call_webhook(_make_update(993000002, chat_id, "✅ Додати все"))
            mock_add.assert_called_once()
            written_items = mock_add.call_args.args[2]
            self.assertEqual(written_items[0]["quantity_text"], "дві пачки")
            self.assertIsNone(written_items[0]["quantity_value"])
            self.assertEqual(mock_add.call_args.kwargs.get("targets"), [])
        self.assertNotIn(chat_id, pending_inventory_batch)


# =========================
# Case 9 — Global Household Router's handling of the same phrase must be
# unchanged by this fix (household_router.py was not touched).
# =========================
class TestGlobalRouterUnchangedForPackagePhrase(unittest.TestCase):
    def test_global_router_keeps_package_phrase_as_literal_quantity_text(self):
        router_result = {
            "intent": "household_operations",
            "operations": [{
                "type": "add_inventory", "name": "Сосиски",
                "quantity_text": "дві пачки", "category": "М'ясо та риба",
            }],
            "unresolved_fragments": [],
        }
        kind, payload = household_router._validate_operations(router_result, [], [], None)
        self.assertEqual(kind, "ok")
        item = payload["add_inventory_items"][0]
        self.assertEqual(item["name"], "Сосиски")
        self.assertEqual(item["quantity_text"], "дві пачки")
        self.assertIsNone(item["quantity_value"])
        self.assertIsNone(item["quantity_unit"])


if __name__ == '__main__':
    unittest.main()
