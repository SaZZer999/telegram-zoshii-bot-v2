import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot to avoid real connections —
# no real Gemini/Telegram/Supabase call happens anywhere in this file.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    inventory_mode,
    pending_remove_batch,
    active_list_context,
    STALE_PREVIEW_MSG,
)


def _item(item_id, name, category, quantity_text, quantity_value=None, quantity_unit=None):
    return {
        "id": item_id, "name": name, "category": category, "quantity_text": quantity_text,
        "quantity_value": quantity_value, "quantity_unit": quantity_unit, "was_corrected": False,
    }


def _bug_report_inventory():
    """Reproduces the exact reported scenario: a 14-item snapshot across
    multiple categories where the CATEGORY_ORDER-grouped display numbering
    the user actually sees puts:
      1. дві пачки сосисок — дві пачки
      4. Сосиски — 2 шт.
      6. сосисок — пару
      12. Молоко — 1 шт.
      14. банани — 3
    """
    return [
        # М'ясо та риба -> positions 1-6
        _item(1, "дві пачки сосисок", "М'ясо та риба", "дві пачки"),
        _item(2, "Курка", "М'ясо та риба", "1 шт.", 1.0, "шт."),
        _item(3, "Риба", "М'ясо та риба", "1 шт.", 1.0, "шт."),
        _item(4, "Сосиски", "М'ясо та риба", "2 шт.", 2.0, "шт."),
        _item(5, "Фарш", "М'ясо та риба", "1 шт.", 1.0, "шт."),
        _item(6, "сосисок", "М'ясо та риба", "пару"),
        # Молочне та яйця -> positions 7-12
        _item(7, "Кефір", "Молочне та яйця", "1 л", 1.0, "л"),
        _item(8, "Сир", "Молочне та яйця", "1 шт.", 1.0, "шт."),
        _item(9, "Сметана", "Молочне та яйця", "1 шт.", 1.0, "шт."),
        _item(10, "Йогурт", "Молочне та яйця", "1 шт.", 1.0, "шт."),
        _item(11, "Масло", "Молочне та яйця", "1 шт.", 1.0, "шт."),
        _item(12, "Молоко", "Молочне та яйця", "1 шт.", 1.0, "шт."),
        # Фрукти та ягоди -> positions 13-14
        _item(13, "Яблука", "Фрукти та ягоди", "1 шт.", 1.0, "шт."),
        _item(14, "банани", "Фрукти та ягоди", "3"),
    ]


# =========================
# Pure helpers
# =========================
class TestNumberedInventoryDisplayOrder(unittest.TestCase):
    def test_numbering_matches_format_grouped_list_exactly(self):
        items = _bug_report_inventory()
        rendered = bot.format_grouped_list(items, "Header")
        numbered = bot._numbered_inventory_display_items(items)
        for number, item in numbered:
            self.assertIn(f"{number}. {bot._render_inventory_item_label(item)}", rendered)


class TestResolveNumberedInventoryDeleteSelection(unittest.TestCase):
    # Case 1 — five valid numbered references select exactly those five ids
    def test_five_valid_numbered_references_select_exact_items(self):
        items = _bug_report_inventory()
        text = (
            "6. сосисок — пару\n"
            "1. дві пачки сосисок — дві пачки\n"
            "4. Сосиски — 2 шт.\n"
            "12. Молоко — 1 шт.\n"
            "14. банани — 3"
        )
        kind, selected = bot._resolve_numbered_inventory_delete_selection(text, items)
        self.assertEqual(kind, "ok")
        self.assertEqual([it["id"] for it in selected], [6, 1, 4, 12, 14])

    # Case 2 — number 6 must resolve to "сосисок — пару", never the
    # similarly-named "Сосиски — 2 шт." at a different position
    def test_number_six_never_picks_the_similar_looking_item(self):
        items = _bug_report_inventory()
        kind, selected = bot._resolve_numbered_inventory_delete_selection("6. сосисок — пару", items)
        self.assertEqual(kind, "ok")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], 6)
        self.assertNotEqual(selected[0]["id"], 4)

    # Case 3 — number + description mismatch blocks the WHOLE preview
    def test_description_mismatch_blocks_entire_batch(self):
        items = _bug_report_inventory()
        text = (
            "6. Сосиски — 2 шт.\n"  # wrong description for position 6
            "1. дві пачки сосисок — дві пачки"
        )
        kind, payload = bot._resolve_numbered_inventory_delete_selection(text, items)
        self.assertEqual(kind, "mismatch")
        number, exists = payload
        self.assertEqual(number, 6)
        self.assertTrue(exists)

    # Case 4 — an invalid/out-of-range number blocks the whole batch
    def test_invalid_number_blocks_entire_batch(self):
        items = _bug_report_inventory()
        text = "99. щось незрозуміле\n1. дві пачки сосисок — дві пачки"
        kind, payload = bot._resolve_numbered_inventory_delete_selection(text, items)
        self.assertEqual(kind, "mismatch")
        number, exists = payload
        self.assertEqual(number, 99)
        self.assertFalse(exists)

    # Case 5 — a duplicate number never creates a duplicate selection
    def test_duplicate_number_is_not_selected_twice(self):
        items = _bug_report_inventory()
        text = "4. Сосиски — 2 шт.\n4. Сосиски — 2 шт."
        kind, selected = bot._resolve_numbered_inventory_delete_selection(text, items)
        self.assertEqual(kind, "ok")
        self.assertEqual([it["id"] for it in selected], [4])

    # Natural language (no numbered lines at all) defers to the caller's
    # existing Gemini-based selection path.
    def test_natural_language_text_is_not_a_numbered_request(self):
        items = _bug_report_inventory()
        kind, payload = bot._resolve_numbered_inventory_delete_selection("Прибери старі сосиски", items)
        self.assertIsNone(kind)
        self.assertIsNone(payload)

    def test_mixed_prose_and_numbers_defers_to_gemini_path(self):
        items = _bug_report_inventory()
        text = "Прибери, будь ласка:\n4. Сосиски — 2 шт."
        kind, payload = bot._resolve_numbered_inventory_delete_selection(text, items)
        self.assertIsNone(kind)
        self.assertIsNone(payload)

    def test_bare_number_without_description_defers_to_gemini_path(self):
        items = _bug_report_inventory()
        kind, payload = bot._resolve_numbered_inventory_delete_selection("6", items)
        self.assertIsNone(kind)
        self.assertIsNone(payload)

    def test_parenthesis_style_numbering_also_recognized(self):
        items = _bug_report_inventory()
        kind, selected = bot._resolve_numbered_inventory_delete_selection("4) Сосиски — 2 шт.", items)
        self.assertEqual(kind, "ok")
        self.assertEqual(selected[0]["id"], 4)


# =========================
# Webhook-level: inv_mode == "removing"
# =========================
def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class TestInventoryRemovingWebhookFlow(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_inv = patch.object(bot, "get_inventory_items", return_value=_bug_report_inventory())
        patcher_inv.start()
        self.addCleanup(patcher_inv.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        for d in (inventory_mode, pending_remove_batch, active_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    # Case 6 — explicit numbered flow never calls the Gemini selection helper
    def test_numbered_request_never_calls_gemini_selection(self):
        chat_id = 992001
        inventory_mode[chat_id] = "removing"
        text = (
            "6. сосисок — пару\n"
            "1. дві пачки сосисок — дві пачки\n"
            "4. Сосиски — 2 шт.\n"
            "12. Молоко — 1 шт.\n"
            "14. банани — 3"
        )
        with patch.object(bot, "_ask_gemini_for_selection") as mock_selection:
            _call_webhook(_make_update(992000001, chat_id, text))
            mock_selection.assert_not_called()
        self.assertIn(chat_id, pending_remove_batch)
        self.assertEqual(
            sorted(it["id"] for it in pending_remove_batch[chat_id]["items"]),
            [1, 4, 6, 12, 14],
        )

    def test_numbered_mismatch_blocks_preview_and_stays_in_mode(self):
        chat_id = 992002
        inventory_mode[chat_id] = "removing"
        with patch.object(bot, "_ask_gemini_for_selection") as mock_selection:
            _call_webhook(_make_update(992000002, chat_id, "6. Сосиски — 2 шт."))
            mock_selection.assert_not_called()
        self.assertNotIn(chat_id, pending_remove_batch)
        self.assertEqual(inventory_mode.get(chat_id), "removing")
        self.assertTrue(any("Не можу безпечно підтвердити вибір" in t for t in self._sent_texts()))

    # Case 7 — ordinary natural-language delete without numbers still uses
    # the existing Gemini selection flow, unchanged.
    def test_natural_language_request_still_uses_gemini_selection(self):
        chat_id = 992003
        inventory_mode[chat_id] = "removing"
        with patch.object(bot, "_ask_gemini_for_selection", return_value=("ok", [_bug_report_inventory()[3]])) as mock_selection:
            _call_webhook(_make_update(992000003, chat_id, "Прибери старі сосиски"))
            mock_selection.assert_called_once()
        self.assertIn(chat_id, pending_remove_batch)


class TestNumberedDeleteConfirmStaleProtection(unittest.TestCase):
    """Confirm-time stale protection (case 8) is the EXISTING mechanism
    (delete_inventory_items_batch + _snapshot_targets/StaleSnapshotError),
    untouched by this fix — reassign bot.StaleSnapshotError to the REAL
    exception class for this test only, same caveat/fix as other test files
    in this suite (bot.py's own import binds the name to a bare MagicMock
    attribute here, not a real Exception subclass, since `database` is
    mocked at import time)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
        spec = importlib.util.spec_from_file_location("real_database_for_numbered_delete_test", database_path)
        cls.real_database = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.real_database)
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = cls.real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def setUp(self):
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def tearDown(self):
        pending_remove_batch.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def test_stale_target_between_preview_and_confirm_blocks_removal(self):
        chat_id = 992010
        items = [_bug_report_inventory()[3], _bug_report_inventory()[5]]  # ids 4 and 6
        pending_remove_batch[chat_id] = {"items": items, "household_id": 1, "user_db_id": 10}
        with patch.object(bot, "delete_inventory_items_batch", side_effect=bot.StaleSnapshotError()) as mock_delete:
            _call_webhook(_make_update(992000010, chat_id, "✅ Так, прибрати"))
            mock_delete.assert_called_once()
        self.assertNotIn(chat_id, pending_remove_batch)
        self.assertTrue(any(STALE_PREVIEW_MSG in t for t in self._sent_texts()))


if __name__ == '__main__':
    unittest.main()
