import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch

# Mock database and groq before importing bot to avoid real connections
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    _check_unresolved_fragments,
    _format_unresolved_fragments_message,
    _validate_start_action,
    _validate_consumptions,
    _validate_compound_operations,
    _validate_reconcile_snapshot,
    _validate_alias_router_result,
    _ask_gemini_saved_list_router,
    _ask_gemini_for_selection,
    pending_mark_batch,
    pending_delete_batch,
    pending_remove_batch,
    pending_inventory_consumption,
)


def _shopping_items():
    return [
        {"id": 501, "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця"},
        {"id": 502, "name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка"},
    ]


def _inventory_items():
    return [
        {"id": 601, "name": "Молоко", "quantity_text": "1 л", "category": "Молочне та яйця",
         "quantity_value": 1.0, "quantity_unit": "л"},
        {"id": 602, "name": "Хліб", "quantity_text": "1 шт.", "category": "Хліб і випічка",
         "quantity_value": 1.0, "quantity_unit": "шт."},
    ]


class TestCheckUnresolvedFragments(unittest.TestCase):
    """Direct tests of the shared gate used by start_action and
    consume_inventory_quantity in the saved-list router dispatch."""

    def test_present_with_fragments_blocks(self):
        router_result = {"unresolved_fragments_present": True, "unresolved_fragments": ["те довге м'ясо"]}
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertTrue(blocked)
        self.assertEqual(fragments, ["те довге м'ясо"])

    def test_present_and_empty_does_not_block(self):
        router_result = {"unresolved_fragments_present": True, "unresolved_fragments": []}
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertFalse(blocked)
        self.assertIsNone(fragments)

    def test_field_explicitly_absent_blocks(self):
        router_result = {"unresolved_fragments_present": False, "unresolved_fragments": []}
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertTrue(blocked)
        self.assertEqual(fragments, [])

    def test_key_missing_entirely_blocks(self):
        # Simulates a malformed/legacy router_result dict that never went
        # through _ask_gemini_saved_list_router's normalization.
        blocked, fragments = _check_unresolved_fragments({})
        self.assertTrue(blocked)
        self.assertEqual(fragments, [])

    def test_whitespace_only_fragments_treated_as_none(self):
        router_result = {"unresolved_fragments_present": True, "unresolved_fragments": ["   ", ""]}
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertFalse(blocked)
        self.assertIsNone(fragments)


class TestFormatUnresolvedFragmentsMessage(unittest.TestCase):

    def test_single_fragment_message(self):
        msg = _format_unresolved_fragments_message(["те довге м'ясо"])
        self.assertIn("Не зміг зрозуміти частину команди: «те довге м'ясо»", msg)
        self.assertIn("Уточни назву товару або напиши його номер зі списку.", msg)

    def test_multiple_fragments_message_lists_all(self):
        msg = _format_unresolved_fragments_message(["те м'ясо", "той сир"])
        self.assertIn("• «те м'ясо»", msg)
        self.assertIn("• «той сир»", msg)


class TestSavedListRouterExposesFragmentPresence(unittest.TestCase):
    """_ask_gemini_saved_list_router must distinguish "Gemini omitted the
    field" from "Gemini returned an empty list", since only the former is a
    hard block for start_action/consume_inventory_quantity."""

    def test_gemini_omits_field_marks_not_present(self):
        raw = json.dumps({"intent": "start_action", "action": "delete_shopping", "selected_numbers": [1]})
        with patch.object(bot, "call_gemini", return_value=raw):
            result = _ask_gemini_saved_list_router(
                "Видали молоко і те довге м'ясо", _shopping_items(), "shopping_saved"
            )
        self.assertFalse(result["unresolved_fragments_present"])
        self.assertEqual(result["unresolved_fragments"], [])

    def test_gemini_returns_fragment(self):
        raw = json.dumps({
            "intent": "start_action", "action": "delete_shopping", "selected_numbers": [1],
            "unresolved_fragments": ["те довге м'ясо"],
        })
        with patch.object(bot, "call_gemini", return_value=raw):
            result = _ask_gemini_saved_list_router(
                "Видали молоко і те довге м'ясо", _shopping_items(), "shopping_saved"
            )
        self.assertTrue(result["unresolved_fragments_present"])
        self.assertEqual(result["unresolved_fragments"], ["те довге м'ясо"])

    def test_gemini_returns_empty_list_explicitly(self):
        raw = json.dumps({
            "intent": "start_action", "action": "delete_shopping", "selected_numbers": [1],
            "unresolved_fragments": [],
        })
        with patch.object(bot, "call_gemini", return_value=raw):
            result = _ask_gemini_saved_list_router("Видали молоко", _shopping_items(), "shopping_saved")
        self.assertTrue(result["unresolved_fragments_present"])
        self.assertEqual(result["unresolved_fragments"], [])

    def test_call_failure_fallback_is_not_present(self):
        with patch.object(bot, "call_gemini", return_value=None):
            result = _ask_gemini_saved_list_router("щось", _shopping_items(), "shopping_saved")
        self.assertEqual(result["intent"], "none")
        self.assertFalse(result["unresolved_fragments_present"])


class TestDestructiveFlowsBlockOnUnresolvedFragments(unittest.TestCase):
    """1-4: delete_shopping / remove_inventory / mark_bought / partial
    consumption with an unresolved fragment must not create a preview and
    must not touch the database."""

    def test_delete_shopping_unresolved_fragment_blocks(self):
        chat_id = 910001
        pending_delete_batch.pop(chat_id, None)
        router_result = {
            "unresolved_fragments_present": True, "unresolved_fragments": ["те довге м'ясо"],
        }
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertTrue(blocked)
        self.assertEqual(fragments, ["те довге м'ясо"])
        self.assertNotIn(chat_id, pending_delete_batch)
        self.assertFalse(bot.delete_items_batch.called)

    def test_remove_inventory_unresolved_fragment_blocks(self):
        chat_id = 910002
        pending_remove_batch.pop(chat_id, None)
        router_result = {
            "unresolved_fragments_present": True, "unresolved_fragments": ["те довге м'ясо"],
        }
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertTrue(blocked)
        self.assertNotIn(chat_id, pending_remove_batch)
        self.assertFalse(bot.delete_inventory_items_batch.called)

    def test_mark_bought_unresolved_fragment_blocks(self):
        chat_id = 910003
        pending_mark_batch.pop(chat_id, None)
        router_result = {
            "unresolved_fragments_present": True, "unresolved_fragments": ["те довге м'ясо"],
        }
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertTrue(blocked)
        self.assertNotIn(chat_id, pending_mark_batch)
        self.assertFalse(bot.mark_items_batch.called)

    def test_partial_consumption_unresolved_fragment_blocks(self):
        chat_id = 910004
        pending_inventory_consumption.pop(chat_id, None)
        router_result = {
            "unresolved_fragments_present": True, "unresolved_fragments": ["те незрозуміле"],
        }
        blocked, fragments = _check_unresolved_fragments(router_result)
        self.assertTrue(blocked)
        self.assertNotIn(chat_id, pending_inventory_consumption)
        self.assertFalse(bot.apply_inventory_consumption.called)

    # 6. Відсутнє поле unresolved_fragments блокує кожен destructive/selection intent
    def test_missing_field_blocks_every_destructive_intent(self):
        for action in ("mark_bought", "delete_shopping", "remove_inventory"):
            router_result = {"action": action, "selected_numbers": [1]}
            blocked, fragments = _check_unresolved_fragments(router_result)
            self.assertTrue(blocked, f"missing unresolved_fragments must block {action}")
            self.assertEqual(fragments, [])
        consume_router_result = {
            "consumptions": [{"item_number": 1, "quantity_value": 0.5, "quantity_unit": "л"}],
        }
        blocked, fragments = _check_unresolved_fragments(consume_router_result)
        self.assertTrue(blocked)
        self.assertEqual(fragments, [])


class TestFullyResolvedCommandsStillWork(unittest.TestCase):
    """5. A fully-understood command (no unresolved fragments) must keep
    working exactly as before this safety fix."""

    def test_start_action_with_empty_fragments_still_selects(self):
        items = _shopping_items()
        router_result = {
            "unresolved_fragments_present": True, "unresolved_fragments": [],
        }
        blocked, _ = _check_unresolved_fragments(router_result)
        self.assertFalse(blocked)
        selected = _validate_start_action("delete_shopping", [1], "shopping_saved", items)
        self.assertEqual(selected, [items[0]])

    def test_consumption_with_empty_fragments_still_resolves(self):
        items = _inventory_items()
        router_result = {
            "unresolved_fragments_present": True, "unresolved_fragments": [],
        }
        blocked, _ = _check_unresolved_fragments(router_result)
        self.assertFalse(blocked)
        kind, payload = _validate_consumptions(
            [{"item_number": 1, "quantity_value": 0.5, "quantity_unit": "л"}], items
        )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload[0]["new_value"], 0.5)


class TestSelectionPromptFlowUnresolvedFragments(unittest.TestCase):
    """The standalone SELECTION_PROMPT flow (shopping_mode "marking"/"deleting",
    inventory_mode "removing") must apply the same safety rule."""

    def test_unresolved_fragment_blocks_selection(self):
        raw = json.dumps({"selected_numbers": [1], "unresolved_fragments": ["те довге м'ясо"]})
        with patch.object(bot, "call_gemini", return_value=raw):
            kind, payload = _ask_gemini_for_selection(
                "Видали молоко і те довге м'ясо", _shopping_items(), "Список покупок", "видалити зі списку"
            )
        self.assertEqual(kind, "unresolved")
        self.assertEqual(payload, ["те довге м'ясо"])

    def test_missing_field_is_invalid_not_silently_ok(self):
        raw = json.dumps({"selected_numbers": [1]})
        with patch.object(bot, "call_gemini", return_value=raw):
            kind, payload = _ask_gemini_for_selection(
                "Видали молоко", _shopping_items(), "Список покупок", "видалити зі списку"
            )
        self.assertEqual(kind, "invalid")
        self.assertIsNone(payload)

    def test_fully_understood_command_still_works(self):
        raw = json.dumps({"selected_numbers": [1], "unresolved_fragments": []})
        with patch.object(bot, "call_gemini", return_value=raw):
            kind, payload = _ask_gemini_for_selection(
                "Видали молоко", _shopping_items(), "Список покупок", "видалити зі списку"
            )
        self.assertEqual(kind, "ok")
        self.assertEqual(payload, [_shopping_items()[0]])


class TestOtherFlowsDoNotRegress(unittest.TestCase):
    """8. compound_inventory_operations, reconcile_inventory_snapshot, and the
    alias router must keep their exact pre-existing unresolved_fragments
    behavior (missing field == nothing unresolved, non-empty field == block)."""

    def test_compound_operations_missing_field_still_proceeds(self):
        items = [
            {"id": 1, "name": "Вершки", "category": "Молочне та яйця",
             "quantity_value": None, "quantity_unit": None, "quantity_text": ""},
        ]
        kind, _ = _validate_compound_operations([{"type": "remove_inventory", "item_number": 1}], None, items)
        self.assertEqual(kind, "ok")

    def test_compound_operations_fragments_still_block(self):
        items = [
            {"id": 1, "name": "Вершки", "category": "Молочне та яйця",
             "quantity_value": None, "quantity_unit": None, "quantity_text": ""},
        ]
        kind, fragments = _validate_compound_operations(
            [{"type": "remove_inventory", "item_number": 1}], ["щось незрозуміле"], items
        )
        self.assertEqual(kind, "unresolved")
        self.assertEqual(fragments, ["щось незрозуміле"])

    def test_reconciliation_missing_field_still_proceeds(self):
        list_items = _inventory_items()
        raw_items = [
            {"name": "Молоко", "canonical_name": "молоко", "quantity_value": 2, "quantity_unit": "л",
             "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
            {"name": "Хліб", "canonical_name": "хліб", "quantity_value": 1, "quantity_unit": "шт.",
             "quantity_inferred": False, "category": "Хліб і випічка", "is_consumable": True},
        ]
        kind, _ = _validate_reconcile_snapshot(raw_items, None, list_items)
        self.assertEqual(kind, "ok")

    def test_alias_router_unresolved_still_blocks(self):
        router_result = {
            "intent": "create_or_update", "alias_text": None, "target_display_name": None,
            "selected_numbers": [], "unresolved_fragments": ["якась хрінь"],
        }
        kind, payload = _validate_alias_router_result(router_result)
        self.assertEqual(kind, "unresolved")
        self.assertEqual(payload, ["якась хрінь"])


if __name__ == '__main__':
    unittest.main()
