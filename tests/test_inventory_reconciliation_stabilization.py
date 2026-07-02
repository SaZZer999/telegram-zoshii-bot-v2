import sys
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

# Mock database and groq before importing bot to avoid real connections
sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot
from bot import (
    _is_duplicate_update,
    _resolve_consumption,
    _validate_consumptions,
    _validate_reconcile_snapshot,
    _resolve_reconciliation_unit_clarification,
    _format_reconciliation_preview,
    _compound_snapshot_is_stale,
    SYSTEM_PROMPT,
    pending_inventory_reconciliation,
)


def make_current_inventory():
    return [
        {"id": 601, "name": "Мисливські ковбаски", "canonical_name": "мисливські ковбаски",
         "category": "М'ясо та риба", "quantity_value": 8.0, "quantity_unit": "шт.", "quantity_text": "8 шт."},
        {"id": 602, "name": "Сосиски", "canonical_name": "сосиски",
         "category": "М'ясо та риба", "quantity_value": 14.0, "quantity_unit": "шт.", "quantity_text": "14 шт."},
        {"id": 603, "name": "Вершки", "canonical_name": "вершки",
         "category": "Молочне та яйця", "quantity_value": None, "quantity_unit": None, "quantity_text": ""},
        {"id": 604, "name": "Приправа до курки", "canonical_name": "приправа до курки",
         "category": "Соуси, спеції та бакалія", "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_text": "2 шт."},
    ]


def make_valid_snapshot_items():
    """Мисливські ковбаски незмінні, Сосиски 14->18, Приправа до курки відсутня (delete
    candidate), Вершки лишається без кількості (inferred, не вигадуємо), Хліб — новий товар."""
    return [
        {"name": "Мисливські ковбаски", "canonical_name": "мисливські ковбаски", "quantity_value": 8,
         "quantity_unit": "шт.", "quantity_inferred": False, "category": "М'ясо та риба", "is_consumable": True},
        {"name": "Сосиски", "canonical_name": "сосиски", "quantity_value": 18,
         "quantity_unit": "шт.", "quantity_inferred": False, "category": "М'ясо та риба", "is_consumable": True},
        {"name": "Вершки", "canonical_name": "вершки", "quantity_value": None,
         "quantity_unit": None, "quantity_inferred": True, "category": "Молочне та яйця", "is_consumable": True},
        {"name": "Хліб", "canonical_name": "хліб", "quantity_value": 3,
         "quantity_unit": "шт.", "quantity_inferred": False, "category": "Хліб і випічка", "is_consumable": True},
    ]


def make_ambiguous_milk_group():
    return [
        {"name": "Молоко", "canonical_name": "молоко", "quantity_value": 5.5, "quantity_unit": "л",
         "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
        {"name": "Молоко", "canonical_name": "молоко", "quantity_value": 1, "quantity_unit": "шт.",
         "quantity_inferred": False, "category": "Молочне та яйця", "is_consumable": True},
    ]


class TestDoubleSendDedup(unittest.TestCase):

    # 1a. Новий update_id не вважається дублікатом
    def test_dedup_allows_new_update_id(self):
        self.assertFalse(_is_duplicate_update(100000001))

    # 1b. Повторний update_id вважається дублікатом (Telegram retry після timeout)
    def test_dedup_rejects_repeated_update_id(self):
        self.assertFalse(_is_duplicate_update(100000002))
        self.assertTrue(_is_duplicate_update(100000002))


class TestHalfQuantityConsumption(unittest.TestCase):

    # 2. 2 шт. - 0,5 шт. = 1,5 шт.
    def test_half_piece_consumed_two_to_one_and_half(self):
        kind, remaining, unit = _resolve_consumption(2, "шт.", 0.5, "шт.")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("1.5"))
        self.assertEqual(unit, "шт.")

    # 3. 8 шт. - 0,5 шт. = 7,5 шт.
    def test_half_piece_consumed_eight_to_seven_and_half(self):
        kind, remaining, unit = _resolve_consumption(8, "шт.", 0.5, "шт.")
        self.assertEqual(kind, "ok")
        self.assertEqual(remaining, Decimal("7.5"))
        self.assertEqual(unit, "шт.")

    # 4. Часткове списання 0,5 не перетворюється на повне видалення
    def test_partial_half_consumption_does_not_delete_item(self):
        items = [
            {"id": 701, "name": "Приправа до курки", "quantity_text": "2 шт.",
             "category": "Соуси, спеції та бакалія", "quantity_value": 2.0, "quantity_unit": "шт."},
        ]
        kind, resolved = _validate_consumptions(
            [{"item_number": 1, "quantity_value": 0.5, "quantity_unit": "шт."}], items
        )
        self.assertEqual(kind, "ok")
        self.assertFalse(resolved[0]["will_remove"])
        self.assertEqual(resolved[0]["new_value"], 1.5)
        self.assertEqual(resolved[0]["new_display"], "1,5 шт.")


class TestReconciliationSnapshotValidation(unittest.TestCase):

    # 5. Snapshot без явного full-intent (порожній items, як повернув би router для
    # звичайної згадки товару) не створює reconciliation preview
    def test_non_trigger_message_produces_no_reconciliation_preview(self):
        kind, payload = _validate_reconcile_snapshot([], [], make_current_inventory())
        self.assertEqual(kind, "invalid")

    # 6. Valid full snapshot створює preview і не записує нічого в БД до підтвердження
    def test_valid_snapshot_builds_preview_without_db_writes(self):
        kind, payload = _validate_reconcile_snapshot(make_valid_snapshot_items(), [], make_current_inventory())
        self.assertEqual(kind, "ok")
        self.assertFalse(bot.apply_inventory_reconciliation.called)
        preview = _format_reconciliation_preview(payload)
        self.assertIn("🔄 Буде звірено запаси", preview)
        self.assertIn("Це повне звіряння", preview)

    # 7. Відсутня в snapshot стара позиція з'являється як кандидат на видалення
    def test_missing_old_item_is_removal_candidate(self):
        kind, payload = _validate_reconcile_snapshot(make_valid_snapshot_items(), [], make_current_inventory())
        self.assertEqual(kind, "ok")
        deleted_ids = {d["item_id"] for d in payload["deletes"]}
        self.assertIn(604, deleted_ids)  # Приправа до курки

    # 8. Нова позиція з'являється як кандидат на додавання
    def test_new_item_not_in_old_inventory_is_addition_candidate(self):
        kind, payload = _validate_reconcile_snapshot(make_valid_snapshot_items(), [], make_current_inventory())
        self.assertEqual(kind, "ok")
        added_names = {a["name"] for a in payload["additions"]}
        self.assertIn("Хліб", added_names)
        # Змінена кількість (14 -> 18) з'являється як update, а не як add/delete
        updated = {u["item_id"]: u for u in payload["updates"]}
        self.assertEqual(updated[602]["new_value"], 18.0)
        # Existing item without an explicit new quantity (Вершки) is left unchanged, not fabricated
        unchanged_ids = {u["item_id"] for u in payload["unchanged"]}
        self.assertIn(603, unchanged_ids)

    # 9. Молоко — 5,5 л + Молоко — 1 шт. не зливаються без явного уточнення
    def test_milk_liters_and_pieces_not_merged_without_clarification(self):
        kind, payload = _validate_reconcile_snapshot(make_ambiguous_milk_group(), [], [])
        self.assertEqual(kind, "ambiguous_unit_group")
        self.assertEqual(len(payload["ambiguous_group"]), 2)

    # 10. Після уточнення "1 л" результат стає 6,5 л
    def test_clarify_one_liter_resolves_to_six_point_five_liters(self):
        kind, resolved = _resolve_reconciliation_unit_clarification(make_ambiguous_milk_group(), "1 л")
        self.assertEqual(kind, "merged")
        self.assertEqual(resolved[0]["quantity_value"], 6.5)
        self.assertEqual(resolved[0]["quantity_unit"], "л")

    # 11. Після "залиш окремо" позиції лишаються окремими (нічого не мерджиться)
    def test_clarify_keep_separate_leaves_two_items(self):
        kind, resolved = _resolve_reconciliation_unit_clarification(make_ambiguous_milk_group(), "залиш окремо")
        self.assertEqual(kind, "kept_separate")
        self.assertIsNone(resolved)

    # 12. Непорожній unresolved_fragments блокує створення preview
    def test_unresolved_fragments_block_reconciliation_preview(self):
        kind, payload = _validate_reconcile_snapshot([], ["щось незрозуміле"], make_current_inventory())
        self.assertEqual(kind, "unresolved")
        self.assertEqual(payload, ["щось незрозуміле"])

    # 13. Повторне підтвердження не застосовує звіряння двічі
    def test_reconciliation_pending_applied_only_once(self):
        chat_id = 88888
        pending_inventory_reconciliation[chat_id] = {
            "updates": [], "additions": [], "deletes": [{"item_id": 604}],
            "household_id": 1, "user_db_id": 1,
        }
        first = pending_inventory_reconciliation.pop(chat_id, None)
        self.assertIsNotNone(first)
        second = pending_inventory_reconciliation.pop(chat_id, None)
        self.assertIsNone(second)

    # 14. Застарілий snapshot (зміна з іншого пристрою) не застосовується
    def test_stale_reconciliation_snapshot_detected(self):
        current_items = make_current_inventory()
        kind, payload = _validate_reconcile_snapshot(make_valid_snapshot_items(), [], current_items)
        self.assertEqual(kind, "ok")
        touched = payload["updates"] + payload["deletes"]
        self.assertFalse(_compound_snapshot_is_stale(touched, current_items))

        changed_elsewhere = [dict(it) for it in current_items]
        for it in changed_elsewhere:
            if it["id"] == 602:
                it["quantity_value"] = 20.0
        self.assertTrue(_compound_snapshot_is_stale(touched, changed_elsewhere))


class TestGeneralAiHonesty(unittest.TestCase):

    # 15. SYSTEM_PROMPT забороняє вигадані підтвердження запису в базу
    def test_system_prompt_forbids_fabricated_confirmations(self):
        self.assertIn("Я зафіксував", SYSTEM_PROMPT)
        self.assertIn("Не вигадуй зміни в PostgreSQL", SYSTEM_PROMPT)


if __name__ == '__main__':
    unittest.main()
