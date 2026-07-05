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
    _saved_update_is_noop,
    _split_noop_saved_updates,
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
             "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_text": "2 шт.", "quantity_inferred": False}


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
# Pure no-op detection
# =========================
class TestSavedUpdateIsNoop(unittest.TestCase):

    # Case 1 (pure) — Gemini's understanding of "дві пачки" as "2 шт." leaves
    # the row identical to what's already stored.
    def test_identical_quantity_text_is_noop(self):
        upd = {"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}
        self.assertTrue(_saved_update_is_noop(_sausage_row(), upd))

    def test_identical_name_is_noop(self):
        upd = {"item_number": 1, "name": "Сосиски", "quantity_text": None, "category": None}
        self.assertTrue(_saved_update_is_noop(_sausage_row(), upd))

    def test_identical_category_is_noop(self):
        upd = {"item_number": 1, "name": None, "quantity_text": None, "category": "М'ясо та риба"}
        self.assertTrue(_saved_update_is_noop(_sausage_row(), upd))

    # Case 4 — a real quantity change is never treated as no-op
    def test_real_quantity_change_is_not_noop(self):
        upd = {"item_number": 1, "name": None, "quantity_text": "5 шт.", "category": None}
        self.assertFalse(_saved_update_is_noop(_banana_row(), upd))

    def test_real_name_change_is_not_noop(self):
        upd = {"item_number": 1, "name": "Ковбаски", "quantity_text": None, "category": None}
        self.assertFalse(_saved_update_is_noop(_sausage_row(), upd))

    def test_inferred_row_getting_explicit_same_number_is_a_real_change(self):
        # DB always clears quantity_inferred when quantity_text is provided —
        # so even the same displayed number is a real change if the existing
        # row was only an inferred guess.
        row = dict(_sausage_row())
        row["quantity_inferred"] = True
        upd = {"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}
        self.assertFalse(_saved_update_is_noop(row, upd))

    def test_decimal_vs_float_noise_does_not_look_like_a_change(self):
        row = dict(_banana_row())
        row["quantity_value"] = 3.0
        upd = {"item_number": 1, "name": None, "quantity_text": "3 шт.", "category": None}
        self.assertTrue(_saved_update_is_noop(row, upd))


class TestSplitNoopSavedUpdates(unittest.TestCase):
    # Case 6/7 — a mixed request splits into real vs no-op groups
    def test_mixed_request_splits_correctly(self):
        items = [_sausage_row(), _banana_row()]
        updates = [
            {"item_number": 1, "item_id": 103, "name": None, "quantity_text": "2 шт.", "category": None},
            {"item_number": 2, "item_id": 102, "name": None, "quantity_text": "5 шт.", "category": None},
        ]
        real, noop = _split_noop_saved_updates(updates, items)
        self.assertEqual(len(real), 1)
        self.assertEqual(real[0]["item_number"], 2)
        self.assertEqual(len(noop), 1)
        self.assertEqual(noop[0]["item_number"], 1)


# =========================
# Webhook-level: natural saved-list edit flow (both inventory and shopping)
# =========================
class TestNoopSavedEditWebhookFlow(unittest.TestCase):

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

    # Case 1/2/3 — a fully no-op inventory candidate creates no preview, no
    # pending_saved_edit, and never calls the DB update helper.
    def test_inventory_noop_candidate_creates_no_preview_or_pending_state(self):
        chat_id = 994001
        saved_list_context[chat_id] = "inventory_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None}]
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                with patch.object(bot, "update_inventory_items_batch") as mock_update:
                    _call_webhook(_make_update(994000001, chat_id, "Сосиски — дві пачки"))
                    mock_update.assert_not_called()
        self.assertNotIn(chat_id, pending_saved_edit)
        texts = self._sent_texts()
        self.assertTrue(any(
            "Не бачу змін, які можна безпечно застосувати." in t
            and "Поточний запис: Сосиски — 2 шт." in t
            for t in texts
        ))

    # Case 4 — a real inventory quantity change still creates a preview as before
    def test_inventory_real_change_still_creates_preview(self):
        chat_id = 994002
        saved_list_context[chat_id] = "inventory_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "5 шт.", "category": None}]
        with patch.object(bot, "get_inventory_items", return_value=[_banana_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                _call_webhook(_make_update(994000002, chat_id, "Банани — 5 шт."))
        self.assertIn(chat_id, pending_saved_edit)
        texts = self._sent_texts()
        self.assertTrue(any("Банани — 3 шт." in t and "→" in t and "Банани — 5 шт." in t for t in texts))

    # Case 5 — the same no-op guard applies identically to the saved shopping list
    def test_shopping_noop_candidate_creates_no_preview(self):
        chat_id = 994003
        saved_list_context[chat_id] = "shopping_saved"
        updates = [{"item_number": 1, "name": None, "quantity_text": "1 л", "category": None}]
        with patch.object(bot, "get_active_shopping_items", return_value=[_milk_shopping_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                with patch.object(bot, "update_shopping_items_batch") as mock_update:
                    _call_webhook(_make_update(994000003, chat_id, "Молоко — 1 л"))
                    mock_update.assert_not_called()
        self.assertNotIn(chat_id, pending_saved_edit)
        texts = self._sent_texts()
        self.assertTrue(any("Не бачу змін, які можна безпечно застосувати." in t for t in texts))

    # Case 6/7 — mixed request: preview shows only the real change, no-op
    # item is excluded from the pending payload
    def test_mixed_request_previews_only_real_change_and_excludes_noop_from_pending(self):
        chat_id = 994004
        saved_list_context[chat_id] = "inventory_saved"
        updates = [
            {"item_number": 1, "name": None, "quantity_text": "2 шт.", "category": None},
            {"item_number": 2, "name": None, "quantity_text": "5 шт.", "category": None},
        ]
        with patch.object(bot, "get_inventory_items", return_value=[_sausage_row(), _banana_row()]):
            with patch.object(bot, "_ask_gemini_saved_list_router", return_value=_router_result(updates)):
                _call_webhook(_make_update(994000004, chat_id, "Сосиски — дві пачки, банани — 5 шт."))
        self.assertIn(chat_id, pending_saved_edit)
        pending_updates = pending_saved_edit[chat_id]["validated_updates"]
        self.assertEqual(len(pending_updates), 1)
        self.assertEqual(pending_updates[0]["item_number"], 2)
        texts = self._sent_texts()
        self.assertTrue(any("Без змін: 1 позиція." in t and "Банани — 3 шт." in t and "Банани — 5 шт." in t for t in texts))
        self.assertFalse(any("Сосиски" in t and "→" in t for t in texts))

    # Case 8 — confirming a mixed pending edit applies only the real change
    def test_confirm_after_mixed_request_applies_only_real_change(self):
        chat_id = 994005
        pending_saved_edit[chat_id] = {
            "items_snapshot": [_sausage_row(), _banana_row()],
            "validated_updates": [
                {"item_number": 2, "item_id": 102, "name": None, "quantity_text": "5 шт.", "category": None,
                 "old_value": 3.0, "old_unit": "шт."},
            ],
            "household_id": 1, "user_db_id": 10, "context_type": "inventory_saved",
        }
        with patch.object(bot, "update_inventory_items_batch") as mock_update:
            _call_webhook(_make_update(994000005, chat_id, "✅ Підтвердити зміни"))
            mock_update.assert_called_once()
            applied = mock_update.call_args.args[1]
            self.assertEqual(len(applied), 1)
            self.assertEqual(applied[0]["item_id"], 102)
        self.assertNotIn(chat_id, pending_saved_edit)


if __name__ == '__main__':
    unittest.main()
