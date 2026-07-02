import sys
import os
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] — other test files in this suite (run in the same
# process by `unittest discover`) may already have replaced that entry with a
# MagicMock. This lets us exercise the real bulk-delete stale-snapshot guard
# directly, with a fake connection/cursor standing in for Postgres.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_tests", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

# Mock database and groq before importing bot, same as every other test file
# in this suite. No real Gemini/Telegram/Supabase call happens anywhere in
# this file — every network-facing bot.py function is patched per-test.
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import _alias_command_gate, _validate_alias_bulk_delete, MAIN_KEYBOARD, SHOPPING_KEYBOARD, INVENTORY_KEYBOARD, ALIASES_KEYBOARD


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
    """Invoke the real webhook() dispatch inside a Flask test request context
    — no actual HTTP server involved."""
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _aliases_fixture():
    return [
        {"id": 1, "alias_text": "сливки", "alias_normalized": "сливки",
         "target_display_name": "Вершки", "target_canonical_name": "вершки"},
        {"id": 2, "alias_text": "приправа курка", "alias_normalized": "приправа курка",
         "target_display_name": "Приправа до курки", "target_canonical_name": "приправа до курки"},
        {"id": 3, "alias_text": "вершки для пасти", "alias_normalized": "вершки для пасти",
         "target_display_name": "Вершки 30%", "target_canonical_name": "вершки 30%"},
    ]


class FakeCursor:
    """Stands in for a psycopg cursor. Records every executed statement (in
    order) and returns `select_rows` for the snapshot-verification SELECT ...
    FOR UPDATE, mirroring tests/test_stale_preview_protection.py's pattern."""

    def __init__(self, select_rows):
        self.select_rows = select_rows
        self.queries = []
        self.rowcount = 0
        self._last_result = None

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "SELECT" in sql and "FOR UPDATE" in sql:
            self._last_result = list(self.select_rows)
        else:
            self.rowcount = 1
            self._last_result = None

    def fetchall(self):
        return self._last_result or []

    def fetchone(self):
        return None

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

    def __exit__(self, exc_type, exc, tb):
        return False


def _bulk_delete_router_result(selected_numbers):
    return {
        "intent": "delete_aliases", "alias_text": None, "target_display_name": None,
        "selected_numbers": selected_numbers, "unresolved_fragments": [],
    }


class TestBulkDeleteValidation(unittest.TestCase):
    """Pure _validate_alias_bulk_delete — the number-selection logic itself."""

    def test_all_numbers_selected_in_list_order(self):
        aliases = _aliases_fixture()
        kind, selected = _validate_alias_bulk_delete([1, 2, 3], aliases)
        self.assertEqual(kind, "ok")
        self.assertEqual([a["id"] for a in selected], [1, 2, 3])

    def test_all_except_one(self):
        aliases = _aliases_fixture()
        kind, selected = _validate_alias_bulk_delete([2, 3], aliases)
        self.assertEqual(kind, "ok")
        self.assertEqual([a["id"] for a in selected], [2, 3])

    def test_duplicates_and_out_of_order_numbers_deduped_and_reordered(self):
        aliases = _aliases_fixture()
        kind, selected = _validate_alias_bulk_delete([3, 1, 3, 1], aliases)
        self.assertEqual(kind, "ok")
        self.assertEqual([a["id"] for a in selected], [1, 3])

    def test_empty_selection_invalid(self):
        kind, _ = _validate_alias_bulk_delete([], _aliases_fixture())
        self.assertEqual(kind, "invalid")

    def test_out_of_range_number_invalid(self):
        kind, _ = _validate_alias_bulk_delete([1, 99], _aliases_fixture())
        self.assertEqual(kind, "invalid")


class TestAliasGateBulkPhrasing(unittest.TestCase):
    # Case 6
    def test_global_bulk_delete_with_domashni_nazvy_passes_gate(self):
        self.assertTrue(_alias_command_gate("Видали всі домашні назви, крім сливки"))

    def test_global_forget_all_home_names_passes_gate(self):
        self.assertTrue(_alias_command_gate("Забудь усі домашні назви"))

    # Case 7
    def test_bare_delete_all_does_not_pass_gate(self):
        self.assertFalse(_alias_command_gate("Видали всі"))

    def test_bare_leave_only_does_not_pass_gate(self):
        self.assertFalse(_alias_command_gate("Залиш тільки сливки, решту видали"))


class TestBulkDeleteAndReturnContextRouting(unittest.TestCase):
    """Webhook-level dispatch, everything network-facing patched. Each test
    uses its own chat_id/update_id to stay isolated."""

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

        patcher_list_aliases = patch.object(bot, "list_household_aliases", return_value=_aliases_fixture())
        self.mock_list_aliases = patcher_list_aliases.start()
        self.addCleanup(patcher_list_aliases.stop)

    def tearDown(self):
        for d in (bot.pending_alias_action, bot.pending_delete_batch, bot.pending_remove_batch,
                  bot.active_list_context, bot.saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]

    # Case 1
    def test_delete_all_in_aliases_menu_selects_everything(self):
        chat_id = 920001
        bot.active_list_context[chat_id] = "aliases"
        with patch.object(bot, "_ask_gemini_alias_router", return_value=_bulk_delete_router_result([1, 2, 3])):
            _call_webhook(_make_update(920000001, chat_id, "Видали всі назви"))
        self.assertEqual(bot.pending_alias_action[chat_id]["kind"], "bulk_delete")
        self.assertEqual({t["id"] for t in bot.pending_alias_action[chat_id]["targets"]}, {1, 2, 3})
        self.assertTrue(any("Буде видалено домашніх назв: 3" in t for t in self._sent_texts()))
        self.assertTrue(any("Не залишиться жодної домашньої назви." in t for t in self._sent_texts()))

    # Case 2
    def test_delete_all_except_one_in_aliases_menu(self):
        chat_id = 920002
        bot.active_list_context[chat_id] = "aliases"
        with patch.object(bot, "_ask_gemini_alias_router", return_value=_bulk_delete_router_result([2, 3])):
            _call_webhook(_make_update(920000002, chat_id, "Видали всі назви, крім сливки"))
        self.assertEqual({t["id"] for t in bot.pending_alias_action[chat_id]["targets"]}, {2, 3})
        texts = self._sent_texts()
        self.assertTrue(any("Буде видалено домашніх назв: 2" in t for t in texts))
        self.assertTrue(any("Залишиться:" in t and "сливки → Вершки" in t for t in texts))

    # Case 3
    def test_bulk_delete_preview_does_not_touch_db_before_confirm(self):
        chat_id = 920003
        bot.active_list_context[chat_id] = "aliases"
        with patch.object(bot, "_ask_gemini_alias_router", return_value=_bulk_delete_router_result([1, 2, 3])):
            with patch.object(bot, "delete_household_aliases_batch") as mock_delete:
                _call_webhook(_make_update(920000003, chat_id, "Видали всі назви"))
                mock_delete.assert_not_called()

    # Case 4
    def test_confirm_deletes_bulk_aliases_exactly_once(self):
        chat_id = 920004
        bot.pending_alias_action[chat_id] = {
            "kind": "bulk_delete", "household_id": 1, "user_db_id": 10,
            "targets": [{"id": 2, "target_display_name": "Приправа до курки", "target_canonical_name": "приправа до курки"},
                        {"id": 3, "target_display_name": "Вершки 30%", "target_canonical_name": "вершки 30%"}],
            "origin": "aliases_menu",
        }
        with patch.object(bot, "delete_household_aliases_batch", return_value=2) as mock_delete:
            _call_webhook(_make_update(920000004, chat_id, "✅ Так, видалити"))
            _call_webhook(_make_update(920000005, chat_id, "✅ Так, видалити"))
            mock_delete.assert_called_once()
        self.assertNotIn(chat_id, bot.pending_alias_action)

    # Case 5
    def test_cancel_bulk_delete_does_not_touch_db(self):
        chat_id = 920005
        bot.pending_alias_action[chat_id] = {
            "kind": "bulk_delete", "household_id": 1, "user_db_id": 10,
            "targets": [{"id": 1, "target_display_name": "Вершки", "target_canonical_name": "вершки"}],
            "origin": "global",
        }
        with patch.object(bot, "delete_household_aliases_batch") as mock_delete:
            _call_webhook(_make_update(920000006, chat_id, "❌ Скасувати"))
            mock_delete.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_alias_action)

    # Case 7 (routing level, complements the pure-gate test above)
    def test_bare_delete_all_outside_aliases_menu_does_not_touch_aliases(self):
        chat_id = 920007
        with patch.object(bot, "_ask_gemini_alias_router") as mock_router:
            _call_webhook(_make_update(920000007, chat_id, "Видали всі"))
        mock_router.assert_not_called()
        self.assertNotIn(chat_id, bot.pending_alias_action)

    # Case 8
    def test_create_from_main_menu_confirm_returns_main_keyboard(self):
        chat_id = 920008
        create_result = {"intent": "create_or_update", "alias_text": "сливки",
                          "target_display_name": "Вершки", "unresolved_fragments": []}
        with patch.object(bot, "_ask_gemini_alias_router", return_value=create_result):
            with patch.object(bot, "get_household_alias", return_value=None):
                _call_webhook(_make_update(920000008, chat_id, "Запам'ятай, що сливки = Вершки"))
        with patch.object(bot, "create_or_update_household_alias"):
            _call_webhook(_make_update(920000009, chat_id, "✅ Так, запам'ятати"))
        self.assertIn(MAIN_KEYBOARD, self._reply_markups())

    # Case 9
    def test_create_from_inventory_saved_confirm_returns_inventory_keyboard_and_keeps_context(self):
        chat_id = 920009
        bot.saved_list_context[chat_id] = "inventory_saved"
        bot.active_list_context[chat_id] = "inventory"
        create_result = {"intent": "create_or_update", "alias_text": "сливки",
                          "target_display_name": "Вершки", "unresolved_fragments": []}
        with patch.object(bot, "_ask_gemini_alias_router", return_value=create_result):
            with patch.object(bot, "get_household_alias", return_value=None):
                _call_webhook(_make_update(920000010, chat_id, "Запам'ятай, що сливки = Вершки"))
        with patch.object(bot, "create_or_update_household_alias"):
            _call_webhook(_make_update(920000011, chat_id, "✅ Так, запам'ятати"))
        self.assertIn(INVENTORY_KEYBOARD, self._reply_markups())
        self.assertEqual(bot.saved_list_context.get(chat_id), "inventory_saved")
        self.assertEqual(bot.active_list_context.get(chat_id), "inventory")

    # Case 10
    def test_create_from_aliases_menu_confirm_stays_in_aliases_menu(self):
        chat_id = 920010
        bot.active_list_context[chat_id] = "aliases"
        create_result = {"intent": "create_or_update", "alias_text": "сливки",
                          "target_display_name": "Вершки", "unresolved_fragments": []}
        with patch.object(bot, "_ask_gemini_alias_router", return_value=create_result):
            with patch.object(bot, "get_household_alias", return_value=None):
                _call_webhook(_make_update(920000012, chat_id, "Запам'ятай, що сливки = Вершки"))
        with patch.object(bot, "create_or_update_household_alias"):
            _call_webhook(_make_update(920000013, chat_id, "✅ Так, запам'ятати"))
        self.assertIn(ALIASES_KEYBOARD, self._reply_markups())
        self.assertEqual(bot.active_list_context.get(chat_id), "aliases")

    # Case 11
    def test_cancel_never_shows_shopping_add_cancelled_message(self):
        chat_id = 920011
        bot.pending_alias_action[chat_id] = {
            "kind": "create", "household_id": 1, "user_db_id": 10,
            "alias_text": "сливки", "target_display_name": "Вершки", "origin": "global",
        }
        _call_webhook(_make_update(920000014, chat_id, "❌ Скасувати"))
        texts = self._sent_texts()
        self.assertFalse(any("Додавання товарів скасовано" in t for t in texts))
        self.assertTrue(any("Дію з домашніми назвами скасовано." == t for t in texts))

    # Case 12
    def test_repeated_confirm_after_success_shows_nothing_pending_not_stale_mode(self):
        chat_id = 920012
        create_result = {"intent": "create_or_update", "alias_text": "сливки",
                          "target_display_name": "Вершки", "unresolved_fragments": []}
        with patch.object(bot, "_ask_gemini_alias_router", return_value=create_result):
            with patch.object(bot, "get_household_alias", return_value=None):
                _call_webhook(_make_update(920000015, chat_id, "Запам'ятай, що сливки = Вершки"))
        with patch.object(bot, "create_or_update_household_alias") as mock_create:
            _call_webhook(_make_update(920000016, chat_id, "✅ Так, запам'ятати"))
            self.mock_send.reset_mock()
            _call_webhook(_make_update(920000017, chat_id, "✅ Так, запам'ятати"))
            mock_create.assert_called_once()
        texts = self._sent_texts()
        self.assertTrue(any("Немає активної дії для підтвердження." == t for t in texts))
        self.assertFalse(any("Додавання товарів скасовано" in t for t in texts))
        self.assertEqual(bot.pending_batch, {})

    # Case 13
    def test_alias_command_does_not_interrupt_active_inventory_remove_preview(self):
        chat_id = 920013
        bot.pending_remove_batch[chat_id] = {
            "items": [{"id": 5, "name": "Йогурт"}], "household_id": 1, "user_db_id": 10,
        }
        try:
            with patch.object(bot, "_ask_gemini_alias_router") as mock_router:
                _call_webhook(_make_update(920000018, chat_id, "Забудь усі домашні назви"))
            mock_router.assert_not_called()
            self.assertNotIn(chat_id, bot.pending_alias_action)
        finally:
            bot.pending_remove_batch.pop(chat_id, None)


class TestStaleBulkDeleteSnapshotAtDbLayer(unittest.TestCase):
    """Case 14: a changed alias from a second phone blocks a stale bulk-delete
    preview. Tested at the database.py layer directly with the REAL module
    (loaded independently of sys.modules['database']) and a fake cursor/
    connection — the same pattern tests/test_stale_preview_protection.py
    uses for the identical scenario on shopping/inventory rows, and for the
    same reason: bot.py's own `except StaleSnapshotError:` can't reliably be
    exercised through a mock once `database` has been replaced with a bare
    MagicMock (StaleSnapshotError stops being a real exception class), so the
    transactional guard itself — where the actual protection lives — is what
    gets tested here.
    """

    def test_changed_target_on_another_device_blocks_delete_no_partial_write(self):
        # Snapshot captured when the preview was built said "Вершки"; the row
        # now says "Вершки 30%" — as if changed from a second phone meanwhile.
        targets = [{"id": 1, "target_display_name": "Вершки", "target_canonical_name": "вершки"}]
        cur = FakeCursor(select_rows=[(1, "Вершки 30%", "вершки 30%")])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.delete_household_aliases_batch(1, targets)
        # Only the verification SELECT ran — no DELETE was ever issued, no commit.
        self.assertEqual(len(cur.queries), 1)
        self.assertIn("FOR UPDATE", cur.queries[0][0])
        self.assertFalse(conn.committed)

    def test_unchanged_snapshot_deletes_successfully(self):
        targets = [{"id": 1, "target_display_name": "Вершки", "target_canonical_name": "вершки"}]
        cur = FakeCursor(select_rows=[(1, "Вершки", "вершки")])
        conn = FakeConnection(cur)
        with patch.object(real_database, "get_connection", return_value=conn):
            count = real_database.delete_household_aliases_batch(1, targets)
        self.assertEqual(count, 1)
        self.assertTrue(conn.committed)
        self.assertIn("DELETE FROM household_aliases", cur.queries[1][0])


if __name__ == "__main__":
    unittest.main()
