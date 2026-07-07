"""Legacy Inventory Flow V1 — module boundary tests.

Does NOT re-test inventory business logic (package-quantity parsing,
representation-guard merge/clarify/separate rules, numbered delete
selection, stale-snapshot protection, etc. — those already live in
test_legacy_inventory_package_quantities.py, test_inventory_representation_
guard.py, test_inventory_numbered_delete_selection.py, test_stale_preview_
protection.py and friends, and keep passing unchanged against the extracted
module). This file only asserts: state-dict ownership/identity between
bot.py and legacy_inventory_flow.py, that the module's handlers behave
correctly against fake injected deps (no DB write before confirm, same
preview/keyboard semantics), and that webhook() still calls into the module
at the same priority slots as before.

No real Gemini/Telegram/Supabase call happens anywhere in this file —
legacy_inventory_flow.py takes only a plain fake InventoryFlowDeps, and the
webhook-level tests mock bot.send_message/bot.call_gemini/bot.get_household_
and_user exactly like every other routing test in this suite.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import legacy_inventory_flow  # noqa: E402


def _make_fake_deps(**overrides):
    """An InventoryFlowDeps built from plain fakes/MagicMocks — no bot.py
    import, no network, no DB. Individual fields can be overridden per test."""
    defaults = dict(
        send_message=MagicMock(),
        call_gemini=MagicMock(return_value=None),
        get_household_and_user=MagicMock(return_value=(1, 10)),
        get_inventory_items=MagicMock(return_value=[]),
        get_household_alias_map=MagicMock(return_value={}),
        save_list_context=MagicMock(),
        normalize_item_quantity=MagicMock(return_value={
            "quantity_text": "", "quantity_value": None, "quantity_unit": None,
            "quantity_inferred": True, "canonical_name": "молоко",
        }),
        canonicalize_name=MagicMock(side_effect=lambda name: (name or "").strip().lower()),
        parse_inventory_list_with_gemini=MagicMock(return_value=None),
        resolve_inventory_representation=MagicMock(return_value=("new", None)),
        format_representation_clarify_message=MagicMock(return_value="clarify"),
        format_representation_separate_warning=MagicMock(return_value="separate warning"),
        format_representation_merge_quantity_fragment=MagicMock(return_value="merged fragment"),
        merge_quantity_values=MagicMock(return_value=(None, None)),
        format_quantity_display=MagicMock(return_value=""),
        ask_gemini_for_selection=MagicMock(return_value=("invalid", None)),
        ask_gemini_preview_edit_router=MagicMock(return_value={"intent": "none", "updates": []}),
        validate_preview_updates=MagicMock(return_value=[]),
        apply_preview_updates=MagicMock(side_effect=lambda items, updates, alias_map=None: items),
        auto_merge_in_place=MagicMock(side_effect=lambda items: items),
        format_grouped_list=MagicMock(side_effect=lambda items, header: f"{header}:{len(items)}"),
        format_inventory_list=MagicMock(side_effect=lambda items: f"list:{len(items)}"),
        format_inventory_preview=MagicMock(side_effect=lambda items, ignored=None: f"preview:{len(items)}"),
        format_unresolved_fragments_message=MagicMock(return_value="unresolved"),
        resolve_numbered_inventory_delete_selection=MagicMock(return_value=(None, None)),
        format_numbered_delete_mismatch_message=MagicMock(return_value="mismatch"),
        clear_shopping_state=MagicMock(),
        clear_inventory_state=MagicMock(),
        active_list_context={},
        saved_list_context={},
        waiting_for_ingredients={},
        inventory_keyboard={"keyboard": "inventory"},
        add_inventory_preview_keyboard={"keyboard": "add_inventory_preview"},
        remove_preview_keyboard={"keyboard": "remove_preview"},
        inventory_parse_prompt="INVENTORY_PROMPT",
        default_category="Інше їстівне",
        valid_categories={"Інше їстівне", "Молочне та яйця"},
        inventory_error_msg="INVENTORY_ERROR",
        selection_error_msg="SELECTION_ERROR",
    )
    defaults.update(overrides)
    return legacy_inventory_flow.InventoryFlowDeps(**defaults)


class TestStateDictIdentity(unittest.TestCase):
    """1. bot.py re-exports the SAME dict objects legacy_inventory_flow.py owns."""

    def test_inventory_mode_is_same_object(self):
        self.assertIs(bot.inventory_mode, legacy_inventory_flow.inventory_mode)

    def test_pending_inventory_batch_is_same_object(self):
        self.assertIs(bot.pending_inventory_batch, legacy_inventory_flow.pending_inventory_batch)

    def test_pending_remove_batch_is_same_object(self):
        self.assertIs(bot.pending_remove_batch, legacy_inventory_flow.pending_remove_batch)

    def test_mutation_via_bot_visible_via_module(self):
        chat_id = 998001
        bot.inventory_mode[chat_id] = "adding"
        try:
            self.assertEqual(legacy_inventory_flow.inventory_mode[chat_id], "adding")
        finally:
            bot.inventory_mode.pop(chat_id, None)


class TestOpenInventoryMenu(unittest.TestCase):
    """2. handle_open_inventory_menu shows the same inventory list and keyboard."""

    def setUp(self):
        legacy_inventory_flow.inventory_mode.clear()
        legacy_inventory_flow.pending_inventory_batch.clear()

    def test_shows_list_and_inventory_keyboard(self):
        items = [{"id": 1, "name": "Молоко", "category": "Молочне та яйця"}]
        deps = _make_fake_deps(get_inventory_items=MagicMock(return_value=items))
        legacy_inventory_flow.handle_open_inventory_menu(deps, chat_id=1, user_id=555, display_name="Тест")

        deps.send_message.assert_called_once()
        args, kwargs = deps.send_message.call_args
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1], "list:1")
        self.assertEqual(kwargs["reply_markup"], deps.inventory_keyboard)
        self.assertEqual(deps.active_list_context[1], "inventory")
        self.assertEqual(deps.saved_list_context[1], "inventory_saved")
        deps.clear_shopping_state.assert_called_once_with(1)
        deps.clear_inventory_state.assert_called_once_with(1)

    def test_db_error_still_sends_inventory_keyboard(self):
        deps = _make_fake_deps(get_inventory_items=MagicMock(side_effect=Exception("boom")))
        legacy_inventory_flow.handle_open_inventory_menu(deps, chat_id=2, user_id=555, display_name="Тест")
        deps.send_message.assert_called_once_with(2, deps.inventory_error_msg, reply_markup=deps.inventory_keyboard)


class TestStartInventoryAdd(unittest.TestCase):
    """3. Start add sets the same inventory_mode value ("adding")."""

    def tearDown(self):
        legacy_inventory_flow.inventory_mode.clear()

    def test_sets_adding_mode(self):
        deps = _make_fake_deps()
        legacy_inventory_flow.handle_start_inventory_add(deps, chat_id=3)
        self.assertEqual(legacy_inventory_flow.inventory_mode[3], "adding")
        deps.clear_shopping_state.assert_called_once_with(3)
        deps.clear_inventory_state.assert_called_once_with(3)
        deps.send_message.assert_called_once()


class TestInventoryModeTextHandler(unittest.TestCase):
    """4. Inventory add text handler uses the injected Gemini callback and
    creates a pending batch WITHOUT any DB write."""

    def tearDown(self):
        legacy_inventory_flow.inventory_mode.clear()
        legacy_inventory_flow.pending_inventory_batch.clear()

    def test_adding_mode_builds_pending_batch_via_injected_parser(self):
        chat_id = 4
        legacy_inventory_flow.inventory_mode[chat_id] = "adding"
        parsed = {
            "items": [{
                "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
                "quantity_text": "", "quantity_value": None, "quantity_unit": None,
                "quantity_inferred": True, "was_corrected": False,
            }],
            "ignored_items": [],
        }
        deps = _make_fake_deps(parse_inventory_list_with_gemini=MagicMock(return_value=parsed))

        handled = legacy_inventory_flow.handle_inventory_mode_text(
            deps, chat_id, user_id=555, display_name="Тест", text="Молоко"
        )

        self.assertTrue(handled)
        deps.parse_inventory_list_with_gemini.assert_called_once()
        self.assertIn(chat_id, legacy_inventory_flow.pending_inventory_batch)
        batch = legacy_inventory_flow.pending_inventory_batch[chat_id]
        self.assertEqual(batch["items"][0]["name"], "Молоко")
        self.assertEqual(batch["household_id"], 1)
        # No DB-write callable exists anywhere on deps — a preview only ever
        # writes to the in-memory pending_inventory_batch dict, never to the
        # database.
        self.assertFalse(hasattr(deps, "add_inventory_items_batch"))

    def test_no_active_mode_falls_through_for_pending_preview_router(self):
        deps = _make_fake_deps()
        handled = legacy_inventory_flow.handle_inventory_mode_text(
            deps, chat_id=5, user_id=555, display_name="Тест", text="щось"
        )
        self.assertFalse(handled)
        deps.send_message.assert_not_called()

    # 5. Package phrase "дві пачки сосисок" proves the same legacy parse path
    # is used — the parser callback receives the raw text unchanged, and its
    # returned quantity_text ("дві пачки") is never converted to "2 шт." by
    # anything in handle_inventory_mode_text itself.
    def test_package_phrase_kept_as_literal_quantity_text_via_parser(self):
        chat_id = 6
        legacy_inventory_flow.inventory_mode[chat_id] = "adding"
        parsed = {
            "items": [{
                "name": "Сосиски", "category": "М'ясо та риба", "canonical_name": "сосиски",
                "quantity_text": "дві пачки", "quantity_value": None, "quantity_unit": None,
                "quantity_inferred": False, "was_corrected": False,
            }],
            "ignored_items": [],
        }
        deps = _make_fake_deps(parse_inventory_list_with_gemini=MagicMock(return_value=parsed))

        legacy_inventory_flow.handle_inventory_mode_text(
            deps, chat_id, user_id=555, display_name="Тест", text="дві пачки сосисок"
        )

        deps.parse_inventory_list_with_gemini.assert_called_once_with("дві пачки сосисок", alias_map={})
        item = legacy_inventory_flow.pending_inventory_batch[chat_id]["items"][0]
        self.assertEqual(item["quantity_text"], "дві пачки")
        self.assertIsNone(item["quantity_value"])

    # 6. Representation guard callback is called in the add flow with the
    # same payload (existing inventory rows, canonical name, category,
    # incoming quantity) — a "clarify" outcome blocks the preview and keeps
    # inventory_mode "adding" instead of writing a pending batch.
    def test_representation_guard_clarify_blocks_preview(self):
        chat_id = 7
        legacy_inventory_flow.inventory_mode[chat_id] = "adding"
        parsed = {
            "items": [{
                "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
                "quantity_text": "1 л", "quantity_value": 1, "quantity_unit": "л",
                "quantity_inferred": False, "was_corrected": False,
            }],
            "ignored_items": [],
        }
        existing_rows = [{"id": 9, "quantity_text": "6 шт.", "quantity_value": 6, "quantity_unit": "шт."}]
        deps = _make_fake_deps(
            parse_inventory_list_with_gemini=MagicMock(return_value=parsed),
            get_inventory_items=MagicMock(return_value=existing_rows),
            resolve_inventory_representation=MagicMock(return_value=("clarify", existing_rows[0])),
        )

        handled = legacy_inventory_flow.handle_inventory_mode_text(
            deps, chat_id, user_id=555, display_name="Тест", text="Молоко"
        )

        self.assertTrue(handled)
        deps.resolve_inventory_representation.assert_called_once()
        self.assertNotIn(chat_id, legacy_inventory_flow.pending_inventory_batch)
        self.assertEqual(legacy_inventory_flow.inventory_mode.get(chat_id), "adding")
        deps.format_representation_clarify_message.assert_called_once_with("Молоко", existing_rows[0])


class TestStartInventoryRemove(unittest.TestCase):
    """7. Start remove shows the same preview state (inventory_mode="removing")."""

    def tearDown(self):
        legacy_inventory_flow.inventory_mode.clear()

    def test_nonempty_list_enters_removing_mode(self):
        deps = _make_fake_deps(get_inventory_items=MagicMock(return_value=[{"id": 1, "name": "Молоко"}]))
        legacy_inventory_flow.handle_start_inventory_remove(deps, chat_id=8, user_id=555, display_name="Тест")
        self.assertEqual(legacy_inventory_flow.inventory_mode[8], "removing")

    def test_empty_list_does_not_enter_removing_mode(self):
        deps = _make_fake_deps(get_inventory_items=MagicMock(return_value=[]))
        legacy_inventory_flow.handle_start_inventory_remove(deps, chat_id=9, user_id=555, display_name="Тест")
        self.assertNotIn(9, legacy_inventory_flow.inventory_mode)
        deps.send_message.assert_called_once_with(9, "Запаси поки порожні.")


class TestNumberedRemoveSelection(unittest.TestCase):
    """8. Numbered remove selection uses the injected selection helpers and
    never calls the Gemini selection callback."""

    def tearDown(self):
        legacy_inventory_flow.inventory_mode.clear()
        legacy_inventory_flow.pending_remove_batch.clear()

    def test_numbered_ok_shows_remove_preview_without_gemini_selection(self):
        chat_id = 10
        legacy_inventory_flow.inventory_mode[chat_id] = "removing"
        items = [{"id": 1, "name": "Молоко"}, {"id": 2, "name": "Хліб"}]
        selected = [items[0]]
        deps = _make_fake_deps(
            get_inventory_items=MagicMock(return_value=items),
            resolve_numbered_inventory_delete_selection=MagicMock(return_value=("ok", selected)),
        )

        handled = legacy_inventory_flow.handle_inventory_mode_text(
            deps, chat_id, user_id=555, display_name="Тест", text="1. Молоко"
        )

        self.assertTrue(handled)
        deps.ask_gemini_for_selection.assert_not_called()
        self.assertIn(chat_id, legacy_inventory_flow.pending_remove_batch)
        self.assertEqual(legacy_inventory_flow.pending_remove_batch[chat_id]["items"], selected)

    def test_numbered_mismatch_uses_injected_mismatch_formatter(self):
        chat_id = 11
        legacy_inventory_flow.inventory_mode[chat_id] = "removing"
        items = [{"id": 1, "name": "Молоко"}]
        deps = _make_fake_deps(
            get_inventory_items=MagicMock(return_value=items),
            resolve_numbered_inventory_delete_selection=MagicMock(return_value=("mismatch", (5, True))),
        )

        legacy_inventory_flow.handle_inventory_mode_text(
            deps, chat_id, user_id=555, display_name="Тест", text="5. Щось"
        )

        deps.ask_gemini_for_selection.assert_not_called()
        deps.format_numbered_delete_mismatch_message.assert_called_once_with(5, True)
        self.assertNotIn(chat_id, legacy_inventory_flow.pending_remove_batch)
        self.assertEqual(legacy_inventory_flow.inventory_mode.get(chat_id), "removing")


class TestPendingInventoryBatchEditHandler(unittest.TestCase):
    """9. Pending inventory batch edit handler does not touch the DB and
    preserves preview semantics."""

    def tearDown(self):
        legacy_inventory_flow.pending_inventory_batch.clear()

    def test_edit_preview_intent_updates_items_and_reshows_preview(self):
        chat_id = 12
        legacy_inventory_flow.pending_inventory_batch[chat_id] = {
            "items": [{"id": None, "name": "Молоко", "category": "Молочне та яйця"}],
            "ignored_items": [],
            "household_id": 1,
            "user_db_id": 10,
            "inventory_targets": [],
        }
        deps = _make_fake_deps(
            ask_gemini_preview_edit_router=MagicMock(return_value={
                "intent": "edit_preview",
                "updates": [{"item_number": 1, "quantity_text": "2 л"}],
            }),
            validate_preview_updates=MagicMock(return_value=[{"item_number": 1, "quantity_text": "2 л"}]),
        )

        handled = legacy_inventory_flow.handle_pending_inventory_batch_edit_text(deps, chat_id, "2 літри молока")

        self.assertTrue(handled)
        deps.apply_preview_updates.assert_called_once()
        deps.send_message.assert_called_once()
        _, kwargs = deps.send_message.call_args
        self.assertEqual(kwargs["reply_markup"], deps.add_inventory_preview_keyboard)
        self.assertFalse(hasattr(deps, "add_inventory_items_batch"))

    def test_intent_none_falls_through_to_ai_chat(self):
        chat_id = 13
        legacy_inventory_flow.pending_inventory_batch[chat_id] = {
            "items": [{"id": None, "name": "Молоко", "category": "Молочне та яйця"}],
            "ignored_items": [],
            "household_id": 1,
            "user_db_id": 10,
            "inventory_targets": [],
        }
        deps = _make_fake_deps()
        handled = legacy_inventory_flow.handle_pending_inventory_batch_edit_text(deps, chat_id, "яка сьогодні погода?")
        self.assertFalse(handled)
        deps.send_message.assert_not_called()


def _make_update(chat_id, text, user_id=555, update_id=None):
    return {
        "update_id": update_id if update_id is not None else chat_id * 1000,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class TestWebhookCallsModuleAtSamePrioritySlots(unittest.TestCase):
    """10. webhook() still dispatches into legacy_inventory_flow at the exact
    same priority slots as the old inline code (menu buttons, inventory_mode
    text dispatch, pending_inventory_batch edit router).

    11. No real Gemini/Telegram/Render/Supabase call happens anywhere in this
    file — every network-facing bot.py function is patched per-test.
    """

    def setUp(self):
        legacy_inventory_flow.inventory_mode.clear()
        legacy_inventory_flow.pending_inventory_batch.clear()
        legacy_inventory_flow.pending_remove_batch.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_inventory_items = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory_items = patcher_inventory_items.start()
        self.addCleanup(patcher_inventory_items.stop)

        patcher_gemini = patch.object(bot, "call_gemini", return_value=None)
        self.mock_gemini = patcher_gemini.start()
        self.addCleanup(patcher_gemini.stop)

    def tearDown(self):
        legacy_inventory_flow.inventory_mode.clear()
        legacy_inventory_flow.pending_inventory_batch.clear()
        legacy_inventory_flow.pending_remove_batch.clear()
        bot.active_list_context.clear()
        bot.saved_list_context.clear()

    def test_open_inventory_menu_button_calls_module_handler(self):
        with patch.object(legacy_inventory_flow, "handle_open_inventory_menu") as mock_handler:
            _call_webhook(_make_update(201, "🧊 Запаси"))
            mock_handler.assert_called_once_with(bot._inventory_deps, 201, 555, "Тест")

    def test_start_add_button_calls_module_handler(self):
        with patch.object(legacy_inventory_flow, "handle_start_inventory_add") as mock_handler:
            _call_webhook(_make_update(202, "➕ Додати продукти"))
            mock_handler.assert_called_once_with(bot._inventory_deps, 202)

    def test_show_list_button_calls_module_handler(self):
        with patch.object(legacy_inventory_flow, "handle_show_inventory_list") as mock_handler:
            _call_webhook(_make_update(203, "📋 Показати запаси"))
            mock_handler.assert_called_once_with(bot._inventory_deps, 203, 555, "Тест")

    def test_start_remove_button_calls_module_handler(self):
        with patch.object(legacy_inventory_flow, "handle_start_inventory_remove") as mock_handler:
            _call_webhook(_make_update(204, "➖ Використати / прибрати"))
            mock_handler.assert_called_once_with(bot._inventory_deps, 204, 555, "Тест")

    def test_inventory_mode_text_dispatch_reached_after_shopping_mode(self):
        chat_id = 205
        legacy_inventory_flow.inventory_mode[chat_id] = "adding"
        with patch.object(legacy_inventory_flow, "handle_inventory_mode_text", return_value=True) as mock_handler:
            _call_webhook(_make_update(chat_id, "Молоко"))
            mock_handler.assert_called_once_with(bot._inventory_deps, chat_id, 555, "Тест", "Молоко")

    def test_pending_inventory_batch_edit_router_reached_at_same_slot(self):
        chat_id = 206
        legacy_inventory_flow.pending_inventory_batch[chat_id] = {
            "items": [{"id": None, "name": "Молоко", "category": "Молочне та яйця"}],
            "ignored_items": [],
            "household_id": 1,
            "user_db_id": 10,
            "inventory_targets": [],
        }
        with patch.object(legacy_inventory_flow, "handle_pending_inventory_batch_edit_text", return_value=True) as mock_handler:
            _call_webhook(_make_update(chat_id, "додай ще один"))
            mock_handler.assert_called_once_with(bot._inventory_deps, chat_id, "додай ще один")

    def test_no_gemini_telegram_or_supabase_needed_for_unit_test(self):
        """The whole inventory_mode "removing" round-trip runs with
        call_gemini/send_message/get_household_and_user mocked and
        get_inventory_items mocked — no real network or DB call happens."""
        chat_id = 207
        legacy_inventory_flow.inventory_mode[chat_id] = "removing"
        _call_webhook(_make_update(chat_id, "молоко"))
        self.mock_send.assert_called_with(chat_id, "Запаси поки порожні.")


if __name__ == "__main__":
    unittest.main()
