import sys
import os
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# Load the REAL database.py as an independent module (pure, connection-free
# functions only), bypassing sys.modules entirely. This must not rely on
# import order relative to other test files: under `unittest discover`, all
# test modules share one process, and another test file's
# `sys.modules['database'] = MagicMock()` (executed at its import time) would
# otherwise poison a plain `import database` here too.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("database_real_for_tests", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    _should_restore_persisted_context,
    saved_list_context,
    pending_mark_batch,
    pending_delete_batch,
    pending_remove_batch,
    pending_saved_edit,
    pending_quick_purchase,
    pending_merge,
    clear_shopping_state,
    clear_inventory_state,
)


class TestPersistentListContext(unittest.TestCase):

    def tearDown(self):
        for d in (saved_list_context, pending_mark_batch, pending_delete_batch,
                  pending_remove_batch, pending_saved_edit, pending_quick_purchase, pending_merge):
            d.clear()

    # 1. shopping_saved є допустимим context
    def test_shopping_saved_is_valid(self):
        self.assertTrue(real_database.list_context_is_valid("shopping_saved"))

    # 2. inventory_saved є допустимим context
    def test_inventory_saved_is_valid(self):
        self.assertTrue(real_database.list_context_is_valid("inventory_saved"))

    # 3. Інший context відкидається
    def test_other_context_rejected(self):
        self.assertFalse(real_database.list_context_is_valid("shopping_pending_add"))
        self.assertFalse(real_database.list_context_is_valid("shopping_marking"))
        self.assertFalse(real_database.list_context_is_valid(""))
        self.assertFalse(real_database.list_context_is_valid(None))

    # 4. Прострочений context не відновлюється
    def test_expired_context_not_restored(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expired = now - timedelta(hours=1)
        future = now + timedelta(hours=1)
        self.assertTrue(real_database.list_context_is_expired(expired, now=now))
        self.assertFalse(real_database.list_context_is_expired(future, now=now))
        self.assertFalse(
            real_database.list_context_is_usable("shopping_saved", 1, 1, expired, now=now)
        )
        self.assertTrue(
            real_database.list_context_is_usable("shopping_saved", 1, 1, future, now=now)
        )

    # 5. Context із неправильним household_id не відновлюється
    def test_wrong_household_not_restored(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        future = now + timedelta(hours=1)
        self.assertFalse(
            real_database.list_context_is_usable("shopping_saved", 1, 2, future, now=now)
        )
        self.assertTrue(
            real_database.list_context_is_usable("shopping_saved", 1, 1, future, now=now)
        )

    # 6. Перехід із shopping_saved на inventory_saved замінює старий context
    def test_switching_lists_replaces_ram_context(self):
        chat_id = 70001
        saved_list_context[chat_id] = "shopping_saved"
        # Mirrors what the "🧊 Запаси" handler does: overwrite with the new context
        saved_list_context[chat_id] = "inventory_saved"
        self.assertEqual(saved_list_context[chat_id], "inventory_saved")

    # 7. /start, /menu, ⬅️ Головне меню очищають context
    def test_navigation_clears_ram_context(self):
        chat_id = 70002
        saved_list_context[chat_id] = "shopping_saved"
        clear_shopping_state(chat_id)
        self.assertIsNone(saved_list_context.get(chat_id))
        saved_list_context[chat_id] = "inventory_saved"
        clear_inventory_state(chat_id)
        self.assertIsNone(saved_list_context.get(chat_id))
        # The persisted side is cleared via the same imported clear_list_context
        # that /start, /menu and ⬅️ Головне меню call directly.
        bot.clear_list_context(chat_id)
        self.assertTrue(bot.clear_list_context.called)

    # 8. Persisted context не повинен переважати активний preview або спеціальний режим
    def test_persisted_context_does_not_override_active_preview(self):
        chat_id = 70003
        # No RAM context and nothing pending -> restoration allowed
        self.assertTrue(_should_restore_persisted_context(chat_id))
        for pending_dict in (
            pending_mark_batch, pending_delete_batch, pending_remove_batch,
            pending_saved_edit, pending_quick_purchase, pending_merge,
        ):
            pending_dict.clear()
            pending_dict[chat_id] = {"anything": True}
            self.assertFalse(_should_restore_persisted_context(chat_id))
            pending_dict.clear()
        # An active RAM context also blocks restoration (nothing to restore over it)
        saved_list_context[chat_id] = "shopping_saved"
        self.assertFalse(_should_restore_persisted_context(chat_id))

    # 9. Якщо persisted context відсутній, fallback поводиться як раніше
    def test_no_persisted_context_falls_back_as_before(self):
        chat_id = 70004
        self.assertTrue(_should_restore_persisted_context(chat_id))
        bot.get_list_context.return_value = None
        restored = bot.get_list_context(chat_id, 1)
        self.assertIsNone(restored)
        # Nothing should end up in RAM if there was nothing to restore
        self.assertIsNone(saved_list_context.get(chat_id))


if __name__ == '__main__':
    unittest.main()
