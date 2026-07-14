"""Navigation "Назад" alias — a bare, natural-language "Назад"/"назад"
(stripped, case-insensitive) must perform the exact same action as the
"⬅️ Головне меню" button: deterministic, no Gemini call, no new pending
state. Covers both message_dispatcher.dispatch() directly (fake deps, no
bot.py involved) and the full webhook path (real bot.py, call_gemini
patched to prove it is never invoked)."""
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

import bot  # noqa: E402
import message_dispatcher  # noqa: E402
import legacy_shopping_flow  # noqa: E402
import legacy_inventory_flow  # noqa: E402
import action_planner  # noqa: E402
import mini_action_planner  # noqa: E402


def _make_fake_shopping_deps(**overrides):
    defaults = dict(
        send_message=MagicMock(),
        get_household_and_user=MagicMock(return_value=(1, 10)),
        get_household_alias_map=MagicMock(return_value={}),
        get_active_shopping_items=MagicMock(return_value=[]),
        save_list_context=MagicMock(),
        normalize_item_quantity=MagicMock(return_value={
            "quantity_text": "", "quantity_value": None, "quantity_unit": None,
            "quantity_inferred": True, "canonical_name": "молоко",
        }),
        parse_item_text=MagicMock(return_value=("Молоко", "")),
        call_gemini=MagicMock(return_value=None),
        ask_gemini_for_selection=MagicMock(return_value=("invalid", None)),
        ask_gemini_preview_edit_router=MagicMock(return_value={"intent": "none", "updates": []}),
        validate_preview_updates=MagicMock(return_value=[]),
        apply_preview_updates=MagicMock(side_effect=lambda items, updates, alias_map=None: items),
        auto_merge_in_place=MagicMock(side_effect=lambda items: items),
        format_shopping_list=MagicMock(side_effect=lambda items: f"list:{len(items)}"),
        format_batch_preview=MagicMock(side_effect=lambda items, ignored=None: f"preview:{len(items)}"),
        format_grouped_list=MagicMock(side_effect=lambda items, header: f"{header}:{len(items)}"),
        format_unresolved_fragments_message=MagicMock(return_value="unresolved"),
        clear_shopping_state=MagicMock(),
        clear_inventory_state=MagicMock(),
        active_list_context={},
        saved_list_context={},
        waiting_for_ingredients={},
        shopping_keyboard={"keyboard": "shopping"},
        add_preview_keyboard={"keyboard": "add_preview"},
        mark_preview_keyboard={"keyboard": "mark_preview"},
        delete_preview_keyboard={"keyboard": "delete_preview"},
        shopping_parse_prompt="SHOPPING_PROMPT",
        default_category="Інше їстівне",
        valid_categories={"Інше їстівне", "Молочне та яйця"},
        db_error_msg="DB_ERROR",
        selection_error_msg="SELECTION_ERROR",
    )
    defaults.update(overrides)
    return legacy_shopping_flow.ShoppingFlowDeps(**defaults)


def _make_fake_inventory_deps(**overrides):
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


def _make_fake_dispatcher_deps(**overrides):
    defaults = dict(
        send_message=MagicMock(),
        clear_interaction_state=MagicMock(),
        main_keyboard={"keyboard": "main"},
        help_text="HELP_TEXT",
        shopping_deps=_make_fake_shopping_deps(),
        inventory_deps=_make_fake_inventory_deps(),
    )
    defaults.update(overrides)
    return message_dispatcher.DispatcherDeps(**defaults)


class TestNazadDispatcherLevel(unittest.TestCase):
    """Pure message_dispatcher-level test, no bot.py routing involved."""

    def test_nazad_capitalized_matches_same_branch_as_button(self):
        deps = _make_fake_dispatcher_deps()
        handled = message_dispatcher.dispatch(deps, 1, 555, "Тест", "Назад")
        self.assertEqual(handled, message_dispatcher.RouteOutcome.HANDLED)
        deps.clear_interaction_state.assert_called_once_with(1)
        deps.send_message.assert_called_once_with(1, "Ось головне меню:", reply_markup=deps.main_keyboard)

    def test_nazad_lowercase_matches_same_branch_as_button(self):
        deps = _make_fake_dispatcher_deps()
        handled = message_dispatcher.dispatch(deps, 2, 555, "Тест", "назад")
        self.assertEqual(handled, message_dispatcher.RouteOutcome.HANDLED)
        deps.send_message.assert_called_once_with(2, "Ось головне меню:", reply_markup=deps.main_keyboard)

    def test_nazad_with_surrounding_whitespace_matches(self):
        deps = _make_fake_dispatcher_deps()
        handled = message_dispatcher.dispatch(deps, 3, 555, "Тест", "  назад  ")
        self.assertEqual(handled, message_dispatcher.RouteOutcome.HANDLED)
        deps.send_message.assert_called_once_with(3, "Ось головне меню:", reply_markup=deps.main_keyboard)

    def test_nazad_outranks_active_shopping_mode(self):
        legacy_shopping_flow.shopping_mode[4] = "adding"
        try:
            deps = _make_fake_dispatcher_deps()
            handled = message_dispatcher.dispatch(deps, 4, 555, "Тест", "Назад")
            self.assertEqual(handled, message_dispatcher.RouteOutcome.HANDLED)
            deps.clear_interaction_state.assert_called_once_with(4)
        finally:
            legacy_shopping_flow.shopping_mode.clear()

    def test_unrelated_word_containing_nazad_as_substring_does_not_match(self):
        # Exact-text match only — "назад" embedded in a longer sentence must
        # NOT trigger navigation (never a substring/fuzzy match).
        deps = _make_fake_dispatcher_deps()
        handled = message_dispatcher.dispatch(deps, 5, 555, "Тест", "піди назад у список")
        self.assertEqual(handled, message_dispatcher.RouteOutcome.CONTINUE)
        deps.clear_interaction_state.assert_not_called()


class TestNazadWebhookLevel(unittest.TestCase):
    """Full webhook path (real bot.py) — proves Gemini/the Inventory Action
    Planner V1/the existing mini_action_planner.py are never reached for
    "Назад"/"назад"."""

    def setUp(self):
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _call_webhook(self, update):
        with bot.app.test_request_context(json=update):
            return bot.webhook()

    def test_nazad_navigates_without_any_gemini_call(self):
        chat_id = 976101
        update = {
            "update_id": 976101001,
            "message": {"chat": {"id": chat_id}, "text": "Назад", "from": {"id": 555, "first_name": "Тест"}},
        }
        with patch.object(bot, "call_gemini") as mock_gemini:
            with patch.object(action_planner, "classify") as mock_action_classify:
                with patch.object(mini_action_planner, "classify") as mock_mini_classify:
                    self._call_webhook(update)
        mock_gemini.assert_not_called()
        mock_action_classify.assert_not_called()
        mock_mini_classify.assert_not_called()
        self.assertTrue(any("головне меню" in t.lower() for t in self._sent_texts()))

    def test_lowercase_nazad_navigates_without_any_gemini_call(self):
        chat_id = 976102
        update = {
            "update_id": 976102001,
            "message": {"chat": {"id": chat_id}, "text": "назад", "from": {"id": 555, "first_name": "Тест"}},
        }
        with patch.object(bot, "call_gemini") as mock_gemini:
            self._call_webhook(update)
        mock_gemini.assert_not_called()
        self.assertTrue(any("головне меню" in t.lower() for t in self._sent_texts()))


if __name__ == "__main__":
    unittest.main()
