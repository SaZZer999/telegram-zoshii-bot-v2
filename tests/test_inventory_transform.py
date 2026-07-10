"""Inventory Transform V1 — the Unified Household AI Action Planner V1's one
genuinely new capability: a deterministic, lossy combine of TWO OR MORE
existing inventory rows into ONE new named record ("об'єднай сосиски і
мисливські ковбаски в м'ясні вироби").

Every OTHER action type the planner spec describes (inventory_add,
shopping_add, inventory_rename, inventory_remove) already has a safe,
working, deterministic or Gemini-structured route in this codebase
(household_router.py's Global Household Router + Inventory Cleanup Admin
v1) — see this feature's own module docstring in inventory.py for why this
file only adds the ONE missing piece instead of a parallel generic system.

Covers: the pure text-classification/formatting helpers in inventory.py
(parse_inventory_transform_request, format_inventory_transform_preview), the
webhook-level route in bot.py (_route_inventory_transform /
_start_inventory_transform / _apply_inventory_transform_confirm), and
database.execute_inventory_transform's Action History journal integration
(same operation_type/restore path apply_global_household_operations and
execute_inventory_cleanup_merge already use, verified here against the REAL
database.py). No real Gemini, Telegram, Render, or Supabase call happens
anywhere in this file — Inventory Transform V1 uses no Gemini call at all
(pure regex trigger + live-inventory candidate matching, same posture as
Inventory Cleanup Admin v1's rename/delete).
"""
import sys
import os
import importlib.util
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import inventory
import preview_editing

_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_inventory_transform_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
from bot import (  # noqa: E402
    pending_inventory_transform,
    pending_cleanup_admin,
    pending_cleanup_admin_disambiguation,
    pending_global_household,
    pending_destructive_guard,
    GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD,
    GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG,
    STALE_PREVIEW_MSG,
    INVENTORY_KEYBOARD,
)


def _effective_quantity_stub(item):
    value = item.get("quantity_value")
    unit = item.get("quantity_unit")
    text = item.get("quantity_text") or ""
    return value, unit, text


def _sausage_and_kovbaski_rows():
    return [
        {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
         "quantity_value": Decimal("6"), "quantity_unit": "шт.", "quantity_text": "6 шт."},
        {"id": 60, "name": "Мисливські ковбаски", "canonical_name": "мисливські ковбаски",
         "category": "М'ясо та риба",
         "quantity_value": Decimal("2"), "quantity_unit": "шт.", "quantity_text": "2 шт."},
    ]


# =========================
# 1. Pure parser (inventory.py) — no DB, no Telegram.
# =========================
class TestParseInventoryTransformRequest(unittest.TestCase):
    def test_ob_yednay_two_sources(self):
        self.assertEqual(
            inventory.parse_inventory_transform_request(
                "об'єднай сосиски і мисливські ковбаски в м'ясні вироби"
            ),
            (["сосиски", "мисливські ковбаски"], "м'ясні вироби"),
        )

    def test_ob_yednaty_infinitive_with_comma_and_ta(self):
        self.assertEqual(
            inventory.parse_inventory_transform_request(
                "об'єднати молоко, вершки та сметану у молочну суміш"
            ),
            (["молоко", "вершки", "сметану"], "молочну суміш"),
        )

    def test_peretvory_na_target(self):
        self.assertEqual(
            inventory.parse_inventory_transform_request("перетвори сосиски й ковбаски на м'ясні вироби"),
            (["сосиски", "ковбаски"], "м'ясні вироби"),
        )

    def test_single_source_returns_none(self):
        # No lossy-combine meaning with only one source — left for other
        # routes (e.g. rename) to consider instead.
        self.assertEqual(inventory.parse_inventory_transform_request("об'єднай сосиски в запасах"), (None, None))

    def test_not_a_transform_phrase_returns_none(self):
        self.assertEqual(inventory.parse_inventory_transform_request("Купив молоко"), (None, None))

    def test_blank_text_returns_none(self):
        self.assertEqual(inventory.parse_inventory_transform_request(""), (None, None))

    def test_trailing_punctuation_stripped_from_target(self):
        self.assertEqual(
            inventory.parse_inventory_transform_request("об'єднай сосиски і ковбаски в м'ясні вироби."),
            (["сосиски", "ковбаски"], "м'ясні вироби"),
        )


class TestFormatInventoryTransformPreview(unittest.TestCase):
    def test_preview_includes_removed_sources_new_target_and_warning(self):
        text = inventory.format_inventory_transform_preview(
            _sausage_and_kovbaski_rows(), _effective_quantity_stub, "М'ясні вироби", "8 шт.",
        )
        self.assertIn("• Прибрати Сосиски — 6 шт.", text)
        self.assertIn("• Прибрати Мисливські ковбаски — 2 шт.", text)
        self.assertIn("• Додати М'ясні вироби — 8 шт.", text)
        self.assertIn("⚠️", text)
        self.assertIn("Сосиски", text)
        self.assertIn("Мисливські ковбаски", text)


# =========================
# 2. Webhook-level routing (bot.py) — network/DB calls patched.
# =========================
def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class InventoryTransformWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_inventory_transform.clear()
        pending_cleanup_admin.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_inventory_transform.clear()
        pending_cleanup_admin.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _reply_markups(self):
        return [call.kwargs.get("reply_markup") for call in self.mock_send.call_args_list]


class TestTransformPreview(InventoryTransformWebhookTestCase):
    # 7/8. Two existing rows with compatible ("шт.") quantities combine into
    # one lossy-warned preview.
    def test_transform_creates_lossy_preview_with_summed_quantity(self):
        chat_id = 772001
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            _call_webhook(_make_update(
                772000001, chat_id, "об'єднай сосиски і мисливські ковбаски в м'ясні вироби",
            ))
        self.assertIn(chat_id, pending_inventory_transform)
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(set(entry["source_item_ids"]), {50, 60})
        self.assertEqual(entry["target_name"], "М'ясні вироби")
        self.assertEqual(entry["target_quantity_value"], Decimal("8"))
        self.assertEqual(entry["target_quantity_unit"], "шт.")
        texts = self._sent_texts()
        self.assertTrue(any("• Прибрати Сосиски — 6 шт." in t for t in texts))
        self.assertTrue(any("• Прибрати Мисливські ковбаски — 2 шт." in t for t in texts))
        self.assertTrue(any("• Додати М'ясні вироби — 8 шт." in t for t in texts))
        self.assertTrue(any("⚠️" in t for t in texts))
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())

    # 9. Nothing is written to the database before confirmation.
    def test_transform_preview_never_writes_before_confirm(self):
        chat_id = 772002
        with patch.object(bot, "get_inventory_items", return_value=_sausage_and_kovbaski_rows()):
            with patch.object(bot, "execute_inventory_transform") as mock_transform:
                _call_webhook(_make_update(
                    772000002, chat_id, "об'єднай сосиски і мисливські ковбаски в м'ясні вироби",
                ))
        mock_transform.assert_not_called()

    # 11. Incompatible units (mass vs a bare "шт." row with no parseable
    # quantity) must be rejected, never guessed at.
    def test_transform_rejects_unresolvable_quantity(self):
        chat_id = 772003
        rows = [
            {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": None, "quantity_unit": None, "quantity_text": "трохи"},
            {"id": 60, "name": "Ковбаски", "canonical_name": "ковбаски", "category": "М'ясо та риба",
             "quantity_value": Decimal("2"), "quantity_unit": "шт.", "quantity_text": "2 шт."},
        ]
        with patch.object(bot, "get_inventory_items", return_value=rows):
            _call_webhook(_make_update(772000003, chat_id, "об'єднай сосиски і ковбаски в м'ясні вироби"))
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("кількість" in t for t in self._sent_texts()))

    def test_transform_rejects_incompatible_units(self):
        chat_id = 772004
        rows = [
            {"id": 50, "name": "Сосиски", "canonical_name": "сосиски", "category": "М'ясо та риба",
             "quantity_value": Decimal("500"), "quantity_unit": "г", "quantity_text": "500 г"},
            {"id": 60, "name": "Ковбаски", "canonical_name": "ковбаски", "category": "М'ясо та риба",
             "quantity_value": Decimal("2"), "quantity_unit": "шт.", "quantity_text": "2 шт."},
        ]
        with patch.object(bot, "get_inventory_items", return_value=rows):
            _call_webhook(_make_update(772000004, chat_id, "об'єднай сосиски і ковбаски в м'ясні вироби"))
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("несумісні" in t for t in self._sent_texts()))

    # 12. A source phrase matching 2+ rows asks for clarification, never
    # guesses.
    def test_transform_ambiguous_source_asks_for_clarification(self):
        chat_id = 772005
        rows = [
            {"id": 10, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": Decimal("1"), "quantity_unit": "л", "quantity_text": "1 л"},
            {"id": 11, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_value": Decimal("2"), "quantity_unit": "л", "quantity_text": "2 л"},
            {"id": 60, "name": "Вершки", "canonical_name": "вершки", "category": "Молочне та яйця",
             "quantity_value": Decimal("200"), "quantity_unit": "мл", "quantity_text": "200 мл"},
        ]
        with patch.object(bot, "get_inventory_items", return_value=rows):
            _call_webhook(_make_update(772000005, chat_id, "об'єднай молоко і вершки в молочну суміш"))
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("не хочу вгадувати" in t for t in self._sent_texts()))

    def test_transform_source_not_found(self):
        chat_id = 772006
        with patch.object(bot, "get_inventory_items", return_value=[]):
            _call_webhook(_make_update(772000006, chat_id, "об'єднай сосиски і ковбаски в м'ясні вироби"))
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("Не знайшов" in t for t in self._sent_texts()))

    # 17. An already-active pending preview blocks a new transform request.
    def test_transform_blocked_by_active_pending_state(self):
        chat_id = 772007
        pending_global_household[chat_id] = {
            "add_shopping_items": [], "add_inventory_items": [], "consume_changes": [],
            "inventory_targets": [], "new_expense": None, "delete_expense": None,
            "household_id": 1, "user_db_id": 10, "origin": "global",
        }
        try:
            _call_webhook(_make_update(
                772000007, chat_id, "об'єднай сосиски і мисливські ковбаски в м'ясні вироби",
            ))
            self.assertNotIn(chat_id, pending_inventory_transform)
            self.assertTrue(any(GLOBAL_HOUSEHOLD_PREVIEW_GUARD_MSG == t for t in self._sent_texts()))
        finally:
            pending_global_household.pop(chat_id, None)


class TestTransformConfirmAndCancel(InventoryTransformWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    def _pending_entry(self):
        targets = [
            {"item_id": 50, "quantity_value": Decimal("6"), "quantity_unit": "шт.",
             "canonical_name": "сосиски", "category": "М'ясо та риба"},
            {"item_id": 60, "quantity_value": Decimal("2"), "quantity_unit": "шт.",
             "canonical_name": "мисливські ковбаски", "category": "М'ясо та риба"},
        ]
        return {
            "household_id": 1, "user_db_id": 10, "origin": "global",
            "source_item_ids": [50, 60], "targets": targets,
            "target_name": "М'ясні вироби", "target_canonical_name": "м'ясні вироби",
            "target_category": "М'ясо та риба",
            "target_quantity_value": Decimal("8"), "target_quantity_unit": "шт.",
            "target_quantity_text": "8 шт.",
        }

    # 10. Confirming a valid transform calls the stale-protected write.
    def test_confirm_applies_transform_via_journal_recording_write(self):
        chat_id = 772010
        entry = self._pending_entry()
        pending_inventory_transform[chat_id] = entry
        with patch.object(bot, "execute_inventory_transform", return_value=True) as mock_transform:
            _call_webhook(_make_update(772000010, chat_id, "✅ Так, застосувати"))
        mock_transform.assert_called_once_with(
            1, 10, [50, 60], "М'ясні вироби", "м'ясні вироби", "М'ясо та риба",
            Decimal("8"), "шт.", "8 шт.", entry["targets"],
        )
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("✅ Зміни застосовано." == t for t in self._sent_texts()))

    def test_confirm_aborts_on_stale_snapshot(self):
        chat_id = 772011
        pending_inventory_transform[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_inventory_transform", side_effect=bot.StaleSnapshotError()):
            _call_webhook(_make_update(772000011, chat_id, "✅ Так, застосувати"))
        self.assertIn(STALE_PREVIEW_MSG, self._sent_texts())
        self.assertNotIn(chat_id, pending_inventory_transform)

    def test_cancel_clears_pending_transform_no_write(self):
        chat_id = 772012
        pending_inventory_transform[chat_id] = self._pending_entry()
        with patch.object(bot, "execute_inventory_transform") as mock_transform:
            _call_webhook(_make_update(772000012, chat_id, "❌ Скасувати"))
        mock_transform.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))


# =========================
# PREVIEW EDIT V1 — safe text edits to an active pending_inventory_transform
# preview (message_dispatcher._dispatch_pending_routes + bot._handle_
# inventory_transform_edit_text + preview_editing.py). Every entry below is
# shaped exactly like _start_inventory_transform now builds it (targets
# carry "name", see bot.py), so these tests exercise the real production
# data shape, not a hand-trimmed one.
# =========================
def _pending_transform_entry():
    targets = [
        {"item_id": 50, "name": "Сосиски", "quantity_value": Decimal("6"), "quantity_unit": "шт.",
         "canonical_name": "сосиски", "category": "М'ясо та риба"},
        {"item_id": 60, "name": "Мисливські ковбаски", "quantity_value": Decimal("2"), "quantity_unit": "шт.",
         "canonical_name": "мисливські ковбаски", "category": "М'ясо та риба"},
    ]
    return {
        "household_id": 1, "user_db_id": 10, "origin": "global",
        "source_item_ids": [50, 60], "targets": targets,
        "target_name": "М'ясні вироби", "target_canonical_name": "м'ясні вироби",
        "target_category": "М'ясо та риба",
        "target_quantity_value": Decimal("8"), "target_quantity_unit": "шт.",
        "target_quantity_text": "8 шт.",
    }


class TestPreviewEditV1(InventoryTransformWebhookTestCase):
    def _seed(self, chat_id):
        entry = _pending_transform_entry()
        pending_inventory_transform[chat_id] = entry
        return entry

    # 1. "так.тільки зроби М'ясних виробів — 2 шт" updates target quantity
    # only and re-renders the preview.
    def test_yes_only_change_quantity_updates_target_and_rerenders(self):
        chat_id = 772100
        self._seed(chat_id)
        _call_webhook(_make_update(772100001, chat_id, "так.тільки зроби М'ясних виробів — 2 шт"))
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(entry["target_quantity_value"], Decimal("2"))
        self.assertEqual(entry["target_quantity_unit"], "шт.")
        self.assertEqual(entry["target_quantity_text"], "2 шт.")
        self.assertEqual(entry["target_name"], "М'ясні вироби")
        texts = self._sent_texts()
        self.assertTrue(any("Оновив план:" in t for t in texts))
        self.assertTrue(any("• Додати М'ясні вироби — 2 шт." in t for t in texts))
        self.assertIn(GLOBAL_HOUSEHOLD_PREVIEW_KEYBOARD, self._reply_markups())

    # 2. "зроби М'ясні вироби 2 шт" also only changes the quantity.
    def test_bare_zroby_name_and_quantity_only_changes_quantity(self):
        chat_id = 772101
        self._seed(chat_id)
        _call_webhook(_make_update(772101001, chat_id, "зроби М'ясні вироби 2 шт"))
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(entry["target_quantity_value"], Decimal("2"))
        self.assertEqual(entry["target_name"], "М'ясні вироби")

    # 3. "назви це М'ясо" renames the target, quantity untouched.
    def test_nazvy_tse_renames_target(self):
        chat_id = 772102
        self._seed(chat_id)
        _call_webhook(_make_update(772102001, chat_id, "назви це М'ясо"))
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(entry["target_name"], "М'ясо")
        self.assertEqual(entry["target_quantity_value"], Decimal("8"))
        self.assertTrue(any("• Додати М'ясо — 8 шт." in t for t in self._sent_texts()))

    # 4. "замість М'ясні вироби зроби М'ясо" also renames the target.
    def test_zamist_old_name_zroby_new_name_renames_target(self):
        chat_id = 772103
        self._seed(chat_id)
        _call_webhook(_make_update(772103001, chat_id, "замість М'ясні вироби зроби М'ясо"))
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(entry["target_name"], "М'ясо")
        self.assertEqual(entry["target_quantity_value"], Decimal("8"))

    # 5. "замість 8 шт зроби 2 шт" updates the quantity.
    def test_zamist_old_qty_zroby_new_qty_updates_quantity(self):
        chat_id = 772104
        self._seed(chat_id)
        _call_webhook(_make_update(772104001, chat_id, "замість 8 шт зроби 2 шт"))
        entry = pending_inventory_transform[chat_id]
        self.assertEqual(entry["target_quantity_value"], Decimal("2"))
        self.assertEqual(entry["target_name"], "М'ясні вироби")

    # Cross-unit rewrite: "зроби 500 мл замість 0,5 л".
    def test_zroby_new_qty_zamist_old_qty_supports_unit_change(self):
        chat_id = 772105
        entry = self._seed(chat_id)
        entry["target_quantity_value"] = Decimal("0.5")
        entry["target_quantity_unit"] = "л"
        entry["target_quantity_text"] = "0,5 л"
        _call_webhook(_make_update(772105001, chat_id, "зроби 500 мл замість 0,5 л"))
        updated = pending_inventory_transform[chat_id]
        self.assertEqual(updated["target_quantity_value"], Decimal("500"))
        self.assertEqual(updated["target_quantity_unit"], "мл")
        self.assertEqual(updated["target_quantity_text"], "500 мл")

    # 6. Unparseable edit text — controlled message, pending preview
    # unchanged, never falls through to general AI.
    def test_unparseable_edit_text_leaves_preview_unchanged(self):
        chat_id = 772106
        entry = self._seed(chat_id)
        original = dict(entry)
        with patch.object(bot, "call_gemini") as mock_gemini:
            _call_webhook(_make_update(772106001, chat_id, "хм, не знаю, подумай сам"))
        mock_gemini.assert_not_called()
        self.assertEqual(pending_inventory_transform[chat_id], original)
        self.assertTrue(any(preview_editing.UNPARSEABLE_EDIT_MSG == t for t in self._sent_texts()))

    # 7. "Видали все" during an active transform preview is blocked by the
    # preview — never opens the destructive guard.
    def test_destructive_command_blocked_by_active_preview(self):
        chat_id = 772107
        self._seed(chat_id)
        with patch.object(bot, "call_gemini") as mock_gemini:
            _call_webhook(_make_update(772107001, chat_id, "Видали все"))
        mock_gemini.assert_not_called()
        self.assertNotIn(chat_id, pending_destructive_guard)
        self.assertIn(chat_id, pending_inventory_transform)
        self.assertTrue(any(preview_editing.UNPARSEABLE_EDIT_MSG == t for t in self._sent_texts()))

    # 8. Another action command ("перейменуй ser на сир") during an active
    # transform preview is blocked, no new cleanup-admin preview starts.
    def test_other_action_command_blocked_by_active_preview(self):
        chat_id = 772108
        self._seed(chat_id)
        _call_webhook(_make_update(772108001, chat_id, "перейменуй ser на сир"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        self.assertNotIn(chat_id, pending_cleanup_admin_disambiguation)
        self.assertIn(chat_id, pending_inventory_transform)
        self.assertTrue(any(preview_editing.UNPARSEABLE_EDIT_MSG == t for t in self._sent_texts()))


class TestPreviewEditV1ConfirmCancelUndo(InventoryTransformWebhookTestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = real_database.StaleSnapshotError

    @classmethod
    def tearDownClass(cls):
        bot.StaleSnapshotError = cls._original_stale_error

    # 9. Confirming an EDITED preview writes the edited target, not the
    # original one.
    def test_confirm_after_edit_writes_edited_target(self):
        chat_id = 772110
        entry = _pending_transform_entry()
        pending_inventory_transform[chat_id] = entry
        _call_webhook(_make_update(772110001, chat_id, "так.тільки зроби М'ясних виробів — 2 шт"))
        with patch.object(bot, "execute_inventory_transform", return_value=True) as mock_transform:
            _call_webhook(_make_update(772110002, chat_id, "✅ Так, застосувати"))
        mock_transform.assert_called_once_with(
            1, 10, [50, 60], "М'ясні вироби", "м'ясні вироби", "М'ясо та риба",
            Decimal("2"), "шт.", "2 шт.", entry["targets"],
        )
        self.assertNotIn(chat_id, pending_inventory_transform)

    # 10. Cancelling after an edit writes nothing.
    def test_cancel_after_edit_writes_nothing(self):
        chat_id = 772111
        pending_inventory_transform[chat_id] = _pending_transform_entry()
        _call_webhook(_make_update(772111001, chat_id, "назви це М'ясо"))
        with patch.object(bot, "execute_inventory_transform") as mock_transform:
            _call_webhook(_make_update(772111002, chat_id, "❌ Скасувати"))
        mock_transform.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_transform)
        self.assertTrue(any("Зміни скасовано." in t for t in self._sent_texts()))

    # 11. Confirming an edited preview journals the EDITED target as the
    # "after" state (and the original source rows as "before") — the same
    # generic before/after-bucket restore apply_undo_action already uses
    # for every other global_household operation (see
    # test_safe_undo_global_action.TestUndoInventoryMergeAndInsert) then
    # restores the ORIGINAL source rows and removes the edited target;
    # this only re-verifies that the write itself journals the edited
    # values, since the restore mechanism itself is untouched by Preview
    # Edit V1 and already covered elsewhere.
    def test_confirm_after_edit_journals_edited_target_for_undo(self):
        chat_id = 772112
        entry = _pending_transform_entry()
        pending_inventory_transform[chat_id] = entry
        _call_webhook(_make_update(772112001, chat_id, "назви це М'ясо"))

        class FakeCursor:
            def __init__(self):
                self.queries = []
                self._fetchall_results = [
                    [(50, Decimal("6"), "шт.", "сосиски", "М'ясо та риба"),
                     (60, Decimal("2"), "шт.", "мисливські ковбаски", "М'ясо та риба")],
                    [(50, "Сосиски", "сосиски", "6 шт.", Decimal("6"), "шт.", False, "М'ясо та риба")],
                    [(60, "Мисливські ковбаски", "мисливські ковбаски", "2 шт.", Decimal("2"), "шт.", False, "М'ясо та риба")],
                    [],
                    [],
                    [], [],
                    [(70, "М'ясо", "м'ясо", "8 шт.", Decimal("8"), "шт.", False, "М'ясо та риба")],
                ]

            def execute(self, sql, params=None):
                self.queries.append((sql, params))

            def fetchall(self):
                return self._fetchall_results.pop(0) if self._fetchall_results else []

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

            def __exit__(self, *exc):
                return False

        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with patch.object(bot, "execute_inventory_transform", side_effect=real_database.execute_inventory_transform):
                _call_webhook(_make_update(772112002, chat_id, "✅ Так, застосувати"))
        self.assertTrue(conn.committed)
        insert_item_queries = [q for q in cursor.queries if "INSERT INTO inventory_items" in q[0]]
        self.assertEqual(len(insert_item_queries), 1)
        self.assertIn("М'ясо", insert_item_queries[0][1])
        self.assertNotIn("М'ясні вироби", insert_item_queries[0][1])


# =========================
# 13/14/15/16. Existing routes/guards still win; general AI unaffected.
# =========================
class TestExistingRoutesStillWinOverTransform(InventoryTransformWebhookTestCase):
    # 13. A bare destructive bulk-clear imperative is never treated as a
    # transform request (and never reaches Gemini either).
    def test_destructive_bulk_guard_wins_over_transform(self):
        chat_id = 772020
        with patch.object(bot, "call_gemini") as mock_gemini:
            _call_webhook(_make_update(772000020, chat_id, "Видали все"))
        mock_gemini.assert_not_called()
        self.assertNotIn(chat_id, pending_inventory_transform)

    # 14. A plain single-item rename/delete/read/expense route still wins —
    # none of those phrases match the transform trigger at all.
    def test_household_read_question_still_works(self):
        chat_id = 772021
        with patch.object(bot, "get_active_shopping_items", return_value=[]):
            _call_webhook(_make_update(772000021, chat_id, "Що треба купити?"))
        self.assertTrue(self._sent_texts())
        self.assertNotIn(chat_id, pending_inventory_transform)

    def test_bought_milk_still_works_via_household_router(self):
        chat_id = 772022
        with patch.object(bot, "get_household_alias_map", return_value={}):
            with patch.object(bot, "get_active_shopping_items", return_value=[]):
                with patch.object(bot, "get_inventory_items", return_value=[]):
                    with patch.object(bot, "get_recent_expenses_for_deletion", return_value=[]):
                        with patch("household_router._ask_gemini_household_router") as mock_gemini:
                            mock_gemini.return_value = {
                                "intent": "household_operations",
                                "operations": [
                                    {"type": "add_inventory", "name": "Молоко", "quantity_text": "",
                                     "category": "Молочне та яйця"},
                                ],
                                "unresolved_fragments": [],
                            }
                            _call_webhook(_make_update(772000022, chat_id, "Купив молоко"))
        self.assertIn(chat_id, pending_global_household)
        pending_global_household.pop(chat_id, None)
        self.assertNotIn(chat_id, pending_inventory_transform)

    # 16. General AI still answers normal non-household questions.
    def test_general_ai_still_answers_unrelated_questions(self):
        chat_id = 772023
        with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн реагує на кислоту.") as mock_gemini:
            _call_webhook(_make_update(772000023, chat_id, "Поясни коротко, чому молоко згортається в каві?"))
        mock_gemini.assert_called_once()
        self.assertNotIn(chat_id, pending_inventory_transform)

    # 15. After a pending_inventory_transform preview is cancelled, general
    # AI works again for a normal question — the block only applies while
    # the preview is actually active.
    def test_general_ai_works_again_after_transform_preview_cancelled(self):
        chat_id = 772024
        pending_inventory_transform[chat_id] = _pending_transform_entry()
        _call_webhook(_make_update(772000024, chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_inventory_transform)
        with patch.object(bot, "call_gemini", return_value="Бо це білок казеїн реагує на кислоту.") as mock_gemini:
            _call_webhook(_make_update(
                772000025, chat_id, "Поясни коротко, чому молоко згортається в каві?",
            ))
        mock_gemini.assert_called_once()


# =========================
# DB-level: execute_inventory_transform (REAL database.py) journal/stale.
# =========================
class FakeCursor:
    def __init__(self, fetchall_results=None):
        self.queries = []
        self._fetchall_results = list(fetchall_results or [])

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

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

    def __exit__(self, *exc):
        return False


def _transform_targets():
    return [
        {"item_id": 50, "quantity_value": Decimal("6"), "quantity_unit": "шт.",
         "canonical_name": "сосиски", "category": "М'ясо та риба"},
        {"item_id": 60, "quantity_value": Decimal("2"), "quantity_unit": "шт.",
         "canonical_name": "мисливські ковбаски", "category": "М'ясо та риба"},
    ]


class TestExecuteInventoryTransformRecordsJournal(unittest.TestCase):
    def test_inserts_global_household_journal_row_and_deletes_sources(self):
        verify_rows = [
            (50, Decimal("6"), "шт.", "сосиски", "М'ясо та риба"),
            (60, Decimal("2"), "шт.", "мисливські ковбаски", "М'ясо та риба"),
        ]
        before_bucket_rows_sosysky = [(50, "Сосиски", "сосиски", "6 шт.", Decimal("6"), "шт.", False, "М'ясо та риба")]
        before_bucket_rows_kovbasky = [
            (60, "Мисливські ковбаски", "мисливські ковбаски", "2 шт.", Decimal("2"), "шт.", False, "М'ясо та риба"),
        ]
        before_bucket_rows_target = []
        after_bucket_rows_sosysky = []
        after_bucket_rows_kovbasky = []
        after_bucket_rows_target = [
            (70, "М'ясні вироби", "м'ясні вироби", "8 шт.", Decimal("8"), "шт.", False, "М'ясо та риба"),
        ]
        cursor = FakeCursor(fetchall_results=[
            verify_rows,
            before_bucket_rows_kovbasky, before_bucket_rows_sosysky, before_bucket_rows_target,
            [],  # _merge_or_insert_inventory_in_tx's own candidate SELECT (no existing target row)
            after_bucket_rows_kovbasky, after_bucket_rows_sosysky, after_bucket_rows_target,
        ])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.execute_inventory_transform(
                1, 10, [50, 60], "М'ясні вироби", "м'ясні вироби", "М'ясо та риба",
                Decimal("8"), "шт.", "8 шт.", _transform_targets(),
            )

        self.assertTrue(result)
        self.assertTrue(conn.committed)
        delete_queries = [q for q in cursor.queries if "DELETE FROM inventory_items" in q[0]]
        self.assertEqual(len(delete_queries), 1)
        self.assertEqual(set(delete_queries[0][1][:2]), {50, 60})
        insert_item_queries = [q for q in cursor.queries if "INSERT INTO inventory_items" in q[0]]
        self.assertEqual(len(insert_item_queries), 1)
        self.assertIn("М'ясні вироби", insert_item_queries[0][1])
        insert_journal_queries = [q for q in cursor.queries if "INSERT INTO household_action_journal" in q[0]]
        self.assertEqual(len(insert_journal_queries), 1)
        sql, params = insert_journal_queries[0]
        self.assertIn("'global_household'", sql)
        self.assertEqual((params[0], params[1]), (1, 10))

    def test_stale_source_target_raises_before_any_write(self):
        # DB now shows a different quantity than the snapshot the preview
        # was built from — _verify_targets_in_tx must reject this before any
        # DELETE/INSERT happens.
        verify_rows = [
            (50, Decimal("1"), "шт.", "сосиски", "М'ясо та риба"),
            (60, Decimal("2"), "шт.", "мисливські ковбаски", "М'ясо та риба"),
        ]
        cursor = FakeCursor(fetchall_results=[verify_rows])
        conn = FakeConnection(cursor)
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.execute_inventory_transform(
                    1, 10, [50, 60], "М'ясні вироби", "м'ясні вироби", "М'ясо та риба",
                    Decimal("8"), "шт.", "8 шт.", _transform_targets(),
                )
        self.assertFalse(conn.committed)
        self.assertFalse(any("DELETE FROM inventory_items" in sql for sql, _ in cursor.queries))


if __name__ == "__main__":
    unittest.main()
