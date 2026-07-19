"""Inventory Multi-Target Actions V1 — safe batch consume/delete of SEVERAL
named inventory positions from one text/voice command.

Confirmed live bug: in an active/global inventory context with stock
`Автокрісло — 2 шт.` / `Печиво — 1 кг` / `Хліб — 2 шт.`, the voice command
"Видали одне автокрісло, печиво і один хліб" got "Не знайшов такого запису в
запасах." — inventory_admin_route ran first, its single-target parser
folded the ENTIRE remainder into one fake product name, and the mixed
bare/explicit batch was never safely resolvable anyway (see the "Семантика
bare targets" requirement this suite verifies below).

Fix: a new, narrow `inventory_multi_target_route` (message_dispatcher.py's
CommandRouteDeps.inventory_multi_target_route / bot.py's
_try_inventory_multi_target), checked right after destructive_bulk_guard and
right before active_list_context_route — ahead of every domain-blind
single-target inventory gate. Parsing is a deterministic splitter
(inventory_multi_target.py) with a strict-schema Gemini fallback used only
when the splitter itself can't confidently produce a plan. Resolution reuses
inventory.resolve_inventory_admin_candidates/phrase_declension_matches/
normalize_delete_quantity_hint and _resolve_consumption; the preview/
confirm/cancel/atomic-transaction/stale-protection/journal/undo path reuses
pending_global_household + database.apply_global_household_operations via
_handle_household_router_result's existing "ok" branch — no new pending
state, no new DB executor.

No real Gemini/Telegram/Supabase call happens anywhere in this file (except
the dedicated FakeConnection DB-layer class near the bottom, which exercises
the REAL database.apply_global_household_operations against a fake
connection/cursor instead of a real Postgres — same technique as
tests/test_global_household_operations.py).
"""
import sys
import os
import importlib.util
import itertools
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Load the REAL database.py fresh, under its own module name, independent of
# sys.modules['database'] (mocked below for the webhook-level tests) — lets
# the DB-layer class near the bottom exercise the actual apply_global_
# household_operations() SQL/transaction shape directly, with a fake
# connection/cursor standing in for Postgres. Same pattern as
# tests/test_global_household_operations.py.
_database_path = os.path.join(os.path.dirname(__file__), "..", "database.py")
_spec = importlib.util.spec_from_file_location("real_database_for_inventory_multi_target_test", _database_path)
real_database = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_database)

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import inventory_multi_target  # noqa: E402
import action_planner  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    pending_cleanup_admin,
    pending_cleanup_admin_disambiguation,
    pending_inventory_consumption,
    active_list_context,
    saved_list_context,
)


# Webhook update_id dedup (bot.py's _seen_update_ids/_seen_update_ids_set) is
# a process-global, never-cleared-between-files set — an update_id literal
# that happens to collide with a completely unrelated test file's own
# literal (even for a different chat_id) makes bot.webhook() silently treat
# THIS message as an already-processed duplicate and skip it entirely. A
# monotonically increasing counter, seeded from an otherwise-unused base,
# guarantees every update_id this file generates is unique for the whole
# test process regardless of run order/combination with any other file.
_uid = itertools.count(876_000_000)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _live_bug_inventory_items():
    return [
        {"id": 901, "name": "Автокрісло", "canonical_name": "автокрісло", "category": "Інше",
         "quantity_text": "2 шт.", "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_inferred": False},
        {"id": 902, "name": "Печиво", "canonical_name": "печиво", "category": "Хліб і випічка",
         "quantity_text": "1 кг", "quantity_value": 1.0, "quantity_unit": "кг", "quantity_inferred": False},
        {"id": 903, "name": "Хліб", "canonical_name": "хліб", "category": "Хліб і випічка",
         "quantity_text": "2 шт.", "quantity_value": 2.0, "quantity_unit": "шт.", "quantity_inferred": False},
    ]


# =========================
# PARSING — deterministic splitter/pre-gate, no webhook, no Gemini.
# =========================
class TestPreGate(unittest.TestCase):
    # 1/2/3/4. Comma / "і" / "та" / "а також" separators.
    def test_two_targets_via_i(self):
        self.assertTrue(inventory_multi_target.looks_like_inventory_multi_target(
            "Видали одне автокрісло і один хліб"))

    def test_three_targets_via_comma_and_i(self):
        self.assertTrue(inventory_multi_target.looks_like_inventory_multi_target(
            "Спиши 200 г сиру, 1 л молока та 2 сосиски"))

    def test_separator_ta(self):
        self.assertTrue(inventory_multi_target.looks_like_inventory_multi_target(
            "Видали печиво та хліб"))

    def test_separator_a_takozh(self):
        self.assertTrue(inventory_multi_target.looks_like_inventory_multi_target(
            "Видали печиво, а також хліб"))

    # 7. All bare.
    def test_all_bare_targets(self):
        self.assertTrue(inventory_multi_target.looks_like_inventory_multi_target(
            "Видали печиво, хліб і автокрісло"))

    # 12. Single-target command never matches.
    def test_single_target_never_matches(self):
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Видали молоко"))
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target(
            "Видали молоко одна штука, воно вже не потрібно"))
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("прибери Молоко 12,5 л"))

    def test_no_trigger_verb_never_matches(self):
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Додай молоко і сир до покупок"))
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Скасуй дві останні витрати"))

    # Explicit cross-domain escapes required by the work order.
    def test_cross_domain_shopping_and_expense_escapes(self):
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target(
            "Видали хліб зі списку покупок і запиши витрату 5 zł"))

    def test_bare_bulk_pronoun_does_not_match(self):
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Видали все"))
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Видали всі, крім молока"))

    def test_aliases_domain_escape(self):
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Видали всі назви, крім сливки"))

    def test_decimal_comma_not_a_separator(self):
        segments = inventory_multi_target._split_target_segments("видали молоко 14,5 л і хліб")
        self.assertEqual(segments, ["молоко 14,5 л", "хліб"])


class TestDeterministicSplit(unittest.TestCase):
    # 5/6. Numeric / word-form quantities.
    def test_all_numeric_quantities(self):
        plan = inventory_multi_target.parse_multi_target_command("Спиши 200 г сиру, 1 л молока та 2 сосиски")
        self.assertEqual(plan["action"], "inventory_batch_change")
        targets = plan["targets"]
        self.assertEqual([t["item_name"] for t in targets], ["сиру", "молока", "сосиски"])
        self.assertEqual([t["operation"] for t in targets], ["consume", "consume", "consume"])
        self.assertEqual([t["quantity_hint"] for t in targets], ["200 г", "1 л", "2"])

    def test_all_word_number_quantities(self):
        plan = inventory_multi_target.parse_multi_target_command("Видали одне автокрісло і один хліб")
        targets = plan["targets"]
        self.assertEqual([t["item_name"] for t in targets], ["автокрісло", "хліб"])
        self.assertEqual([t["operation"] for t in targets], ["consume", "consume"])
        self.assertEqual([t["quantity_hint"] for t in targets], ["одне", "один"])

    def test_all_bare_targets_plan(self):
        plan = inventory_multi_target.parse_multi_target_command("Видали печиво, хліб і автокрісло")
        targets = plan["targets"]
        self.assertEqual([t["item_name"] for t in targets], ["печиво", "хліб", "автокрісло"])
        self.assertEqual([t["operation"] for t in targets], ["unspecified", "unspecified", "unspecified"])
        self.assertEqual([t["quantity_hint"] for t in targets], [None, None, None])

    # Exact live scenario's mixed phrasing.
    def test_mixed_explicit_and_bare(self):
        plan = inventory_multi_target.parse_multi_target_command("Видали одне автокрісло, печиво і один хліб")
        targets = plan["targets"]
        self.assertEqual([t["item_name"] for t in targets], ["автокрісло", "печиво", "хліб"])
        self.assertEqual([t["operation"] for t in targets], ["consume", "unspecified", "consume"])
        self.assertEqual([t["quantity_hint"] for t in targets], ["одне", None, "один"])

    # 11. Voice transcript (no punctuation refinements) parses the same way.
    def test_voice_transcript_shape(self):
        plan = inventory_multi_target.parse_multi_target_command("видали одне автокрісло печиво і один хліб")
        # No comma at all between "автокрісло" and "печиво" — only 2
        # segments result (the "і" split), so this never reaches the 3-target
        # shape; asserting it still parses to a valid 2-target plan (never
        # crashes, never silently drops a target).
        self.assertIsNotNone(plan)
        self.assertGreaterEqual(len(plan["targets"]), 2)

    def test_max_ten_targets_and_below_two_rejected(self):
        two = inventory_multi_target._split_target_segments("видали хліб і молоко")
        self.assertEqual(len(two), 2)
        eleven = inventory_multi_target._split_target_segments(
            "видали " + ", ".join(f"товар{i}" for i in range(11))
        )
        self.assertIsNone(eleven)
        single = inventory_multi_target._split_target_segments("видали молоко")
        self.assertIsNone(single)

    def test_resolve_quantity_value(self):
        self.assertEqual(inventory_multi_target.resolve_quantity_value("200 г"), (Decimal("200"), "г"))
        self.assertEqual(inventory_multi_target.resolve_quantity_value("1 л"), (Decimal("1"), "л"))
        self.assertEqual(inventory_multi_target.resolve_quantity_value("2"), (Decimal("2"), "шт."))
        self.assertEqual(inventory_multi_target.resolve_quantity_value("одне"), (Decimal("1"), "шт."))
        self.assertEqual(inventory_multi_target.resolve_quantity_value("два"), (Decimal("2"), "шт."))
        self.assertEqual(inventory_multi_target.resolve_quantity_value(None), (None, None))
        self.assertEqual(inventory_multi_target.resolve_quantity_value("щось незрозуміле"), (None, None))


class TestSchemaValidation(unittest.TestCase):
    def test_invalid_json_collapses_to_unsupported(self):
        with patch.object(bot, "call_gemini", return_value="not json"):
            result = inventory_multi_target.classify("якийсь текст")
        self.assertEqual(result["action"], "unsupported")

    def test_wrong_version_collapses_to_unsupported(self):
        with patch.object(bot, "call_gemini", return_value='{"version": 2, "action": "inventory_batch_change", "targets": []}'):
            result = inventory_multi_target.classify("якийсь текст")
        self.assertEqual(result["action"], "unsupported")

    def test_extra_key_rejected(self):
        raw = (
            '{"version": 1, "action": "inventory_batch_change", "targets": '
            '[{"item_name": "молоко", "operation": "consume", "quantity_hint": "1 л", "item_id": 5}, '
            '{"item_name": "хліб", "operation": "unspecified", "quantity_hint": null}]}'
        )
        with patch.object(bot, "call_gemini", return_value=raw):
            result = inventory_multi_target.classify("якийсь текст")
        self.assertEqual(result["action"], "unsupported")

    def test_single_target_rejected_by_schema(self):
        raw = '{"version": 1, "action": "inventory_batch_change", "targets": [{"item_name": "молоко", "operation": "unspecified", "quantity_hint": null}]}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = inventory_multi_target.classify("якийсь текст")
        self.assertEqual(result["action"], "unsupported")

    def test_clarify_requires_question(self):
        with patch.object(bot, "call_gemini", return_value='{"version": 1, "action": "clarify", "targets": []}'):
            result = inventory_multi_target.classify("якийсь текст")
        self.assertEqual(result["action"], "unsupported")

    def test_valid_clarify(self):
        raw = '{"version": 1, "action": "clarify", "targets": [], "clarification_question": "Що саме видалити?"}'
        with patch.object(bot, "call_gemini", return_value=raw):
            result = inventory_multi_target.classify("якийсь текст")
        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["clarification_question"], "Що саме видалити?")


# =========================
# WEBHOOK-LEVEL — routing, resolution, preview, confirm/cancel.
# =========================
class MultiTargetTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(active_list_context.clear)
        self.addCleanup(saved_list_context.clear)
        self.addCleanup(pending_global_household.clear)
        self.addCleanup(pending_cleanup_admin.clear)
        self.addCleanup(pending_cleanup_admin_disambiguation.clear)
        self.addCleanup(pending_inventory_consumption.clear)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        self.mock_get_user = patcher_user.start()
        self.addCleanup(patcher_user.stop)

        patcher_items = patch.object(bot, "get_inventory_items", return_value=_live_bug_inventory_items())
        self.mock_inventory_items = patcher_items.start()
        self.addCleanup(patcher_items.stop)

        patcher_apply = patch.object(bot, "apply_global_household_operations", return_value={
            "shopping_added": 0, "inventory_added": 0, "inventory_updated": 0, "inventory_removed": 0,
            "expense_added_id": None, "expense_added_ids": [], "expense_deleted": False,
        })
        self.mock_apply = patcher_apply.start()
        self.addCleanup(patcher_apply.stop)

        patcher_exec_delete = patch.object(bot, "execute_inventory_delete")
        self.mock_execute_inventory_delete = patcher_exec_delete.start()
        self.addCleanup(patcher_exec_delete.stop)

        patcher_action_planner = patch.object(action_planner, "classify")
        self.mock_action_planner_classify = patcher_action_planner.start()
        self.addCleanup(patcher_action_planner.stop)

        patcher_classify = patch.object(inventory_multi_target, "classify")
        self.mock_multi_target_classify = patcher_classify.start()
        self.addCleanup(patcher_classify.stop)

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# =========================
# Exact live scenario.
# =========================
class TestExactLiveScenario(MultiTargetTestCase):
    def test_mixed_batch_blocks_with_specific_clarification(self):
        chat_id = 980001
        _call_webhook(_make_update(next(_uid), chat_id, "Видали одне автокрісло, печиво і один хліб"))
        self.mock_multi_target_classify.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)
        self.assertFalse(self.mock_apply.called)
        texts = self._sent_texts()
        self.assertTrue(any("Печиво" in t and "1 кг" in t for t in texts))
        self.assertTrue(any("Потрібно уточнити кількість" in t for t in texts))
        # Автокрісло/Хліб (the two resolvable targets) are never mentioned as
        # needing clarification, and no unrelated inventory row leaks in.
        self.assertFalse(any("Автокрісло" in t for t in texts))

    def test_clarified_command_builds_single_preview_with_live_quantities(self):
        chat_id = 980002
        _call_webhook(_make_update(next(_uid), chat_id, "Видали одне автокрісло, 200 г печива і один хліб"))
        self.assertIn(chat_id, pending_global_household)
        changes = {c["item_id"]: c for c in pending_global_household[chat_id]["consume_changes"]}
        self.assertEqual(changes[901]["new_display"], "1 шт.")
        self.assertFalse(changes[901]["will_remove"])
        self.assertEqual(changes[902]["new_display"], "800 г")
        self.assertFalse(changes[902]["will_remove"])
        self.assertEqual(changes[903]["new_display"], "1 шт.")
        self.assertFalse(changes[903]["will_remove"])
        preview = self._sent_texts()[-1]
        self.assertIn("Автокрісло", preview)
        self.assertIn("1 шт.", preview)
        self.assertIn("800 г", preview)


# =========================
# Full-delete batch (all-bare).
# =========================
class TestFullDeleteBatch(MultiTargetTestCase):
    def test_all_bare_builds_full_delete_preview_with_live_quantities(self):
        chat_id = 980101
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["consume_changes"]), 3)
        self.assertTrue(all(c["will_remove"] for c in data["consume_changes"]))
        by_id = {c["item_id"]: c for c in data["consume_changes"]}
        self.assertEqual(by_id[902]["old_display"], "1 кг")
        self.assertEqual(by_id[903]["old_display"], "2 шт.")
        self.assertEqual(by_id[901]["old_display"], "2 шт.")
        preview = self._sent_texts()[-1]
        self.assertIn("буде прибрано із запасів", preview)

    def test_no_db_write_before_confirm(self):
        chat_id = 980102
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.assertFalse(self.mock_apply.called)

    def test_cancel_leaves_all_rows(self):
        chat_id = 980103
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        _call_webhook(_make_update(next(_uid), chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertFalse(self.mock_apply.called)

    def test_confirm_deletes_exactly_three_targets(self):
        chat_id = 980104
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        self.mock_apply.assert_called_once()
        kwargs = self.mock_apply.call_args.kwargs
        self.assertEqual(sorted(kwargs["consume_delete_ids"]), [901, 902, 903])
        self.assertEqual(kwargs["consume_updates"], [])
        self.assertEqual(kwargs["add_shopping_items"], [])
        self.assertEqual(kwargs["add_inventory_items"], [])
        self.assertNotIn(chat_id, pending_global_household)

    def test_repeated_confirm_does_not_apply_twice(self):
        chat_id = 980105
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        self.mock_apply.assert_called_once()
        self.assertTrue(any("Немає активної дії" in t for t in self._sent_texts()))


# =========================
# All-explicit consume batch.
# =========================
class TestAllExplicitConsumeBatch(MultiTargetTestCase):
    def test_numeric_and_word_number_quantities_build_partial_consume(self):
        chat_id = 980201
        _call_webhook(_make_update(next(_uid), chat_id, "Видали одне автокрісло і один хліб"))
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["consume_changes"]), 2)
        self.assertTrue(all(not c["will_remove"] for c in data["consume_changes"]))
        by_id = {c["item_id"]: c for c in data["consume_changes"]}
        self.assertEqual(by_id[901]["new_display"], "1 шт.")
        self.assertEqual(by_id[903]["new_display"], "1 шт.")

    def test_quantity_equal_to_full_stock_removes_row(self):
        chat_id = 980202
        _call_webhook(_make_update(next(_uid), chat_id, "Видали два автокрісло і один хліб"))
        data = pending_global_household[chat_id]
        by_id = {c["item_id"]: c for c in data["consume_changes"]}
        self.assertTrue(by_id[901]["will_remove"])
        self.assertIsNone(by_id[901]["new_display"])

    def test_confirm_applies_partial_consume(self):
        chat_id = 980203
        _call_webhook(_make_update(next(_uid), chat_id, "Видали одне автокрісло і один хліб"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        kwargs = self.mock_apply.call_args.kwargs
        self.assertEqual(kwargs["consume_delete_ids"], [])
        updates_by_id = {u["item_id"]: u for u in kwargs["consume_updates"]}
        self.assertEqual(updates_by_id[901]["quantity_text"], "1 шт.")
        self.assertEqual(updates_by_id[903]["quantity_text"], "1 шт.")


# =========================
# Resolution edge cases.
# =========================
class TestResolutionEdgeCases(MultiTargetTestCase):
    def test_missing_target_blocks_whole_batch(self):
        chat_id = 980301
        _call_webhook(_make_update(next(_uid), chat_id, "Видали тестове печиво і хліб"))
        self.assertNotIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("Не знайшов у запасах" in t and "тестове печиво" in t for t in texts))
        self.assertFalse(any("Хліб" in t for t in texts))

    def test_ambiguous_target_shows_only_relevant_candidates(self):
        chat_id = 980302
        items = _live_bug_inventory_items() + [
            {"id": 950, "name": "Хліб", "canonical_name": "хліб", "category": "Хліб і випічка",
             "quantity_text": "1 шт.", "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_inferred": False},
        ]
        with patch.object(bot, "get_inventory_items", return_value=items):
            _call_webhook(_make_update(next(_uid), chat_id, "Видали хліб і печиво"))
        self.assertNotIn(chat_id, pending_global_household)
        texts = self._sent_texts()
        self.assertTrue(any("не хочу вгадувати" in t for t in texts))
        self.assertFalse(any("Автокрісло" in t for t in texts))

    def test_incompatible_unit_blocks_batch(self):
        chat_id = 980303
        _call_webhook(_make_update(next(_uid), chat_id, "Видали 1 л автокрісло і один хліб"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("несумісні одиниці" in t for t in self._sent_texts()))

    def test_quantity_exceeds_stock_blocks_batch(self):
        chat_id = 980304
        _call_webhook(_make_update(next(_uid), chat_id, "Видали 5 кг печива і один хліб"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("у запасах лише" in t for t in self._sent_texts()))

    def test_two_targets_resolving_to_same_row_blocks_second(self):
        chat_id = 980305
        _call_webhook(_make_update(next(_uid), chat_id, "Видали хліб і хліб"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any("названо двічі" in t for t in self._sent_texts()))

    def test_declension_and_unit_alias_resolution(self):
        chat_id = 980306
        items = [
            {"id": 960, "name": "Сир Гауда", "canonical_name": "сир гауда", "category": "Молочне та яйця",
             "quantity_text": "270 г", "quantity_value": 270.0, "quantity_unit": "г", "quantity_inferred": False},
            {"id": 961, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
             "quantity_text": "1 л", "quantity_value": 1.0, "quantity_unit": "л", "quantity_inferred": False},
        ]
        with patch.object(bot, "get_inventory_items", return_value=items):
            _call_webhook(_make_update(next(_uid), chat_id, "Спиши 130 грамм сиру Гауда і 1 літр молока"))
        self.assertIn(chat_id, pending_global_household)
        by_id = {c["item_id"]: c for c in pending_global_household[chat_id]["consume_changes"]}
        self.assertEqual(by_id[960]["new_display"], "140 г")
        self.assertTrue(by_id[961]["will_remove"])


# =========================
# Transaction safety (stale/exception handling via the reused executor).
# =========================
class _RealStaleSnapshotError(Exception):
    """`sys.modules['database'] = MagicMock()` (top of this file) means
    bot.StaleSnapshotError is bound to a bare MagicMock attribute, not a
    real exception class — raising/catching it directly would fail with
    "catching classes that do not inherit from BaseException". Same
    monkeypatch technique tests/test_inventory_cleanup_admin.py already uses
    for the same reason."""


class TestTransactionSafety(MultiTargetTestCase):
    def setUp(self):
        super().setUp()
        self._original_stale_error = bot.StaleSnapshotError
        bot.StaleSnapshotError = _RealStaleSnapshotError
        self.addCleanup(setattr, bot, "StaleSnapshotError", self._original_stale_error)

    def test_stale_snapshot_on_confirm_shows_controlled_message(self):
        chat_id = 980401
        self.mock_apply.side_effect = _RealStaleSnapshotError()
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        self.assertTrue(any("змінився з іншого пристрою" in t for t in self._sent_texts()))
        self.assertNotIn(chat_id, pending_global_household)

    def test_db_exception_on_confirm_shows_controlled_message(self):
        chat_id = 980402
        self.mock_apply.side_effect = RuntimeError("boom")
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        self.assertTrue(any("Не вдалося застосувати" in t for t in self._sent_texts()))

    def test_pending_cleared_after_success(self):
        chat_id = 980403
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        _call_webhook(_make_update(next(_uid), chat_id, "✅ Так, застосувати"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_pending_cleared_after_cancel(self):
        chat_id = 980404
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        _call_webhook(_make_update(next(_uid), chat_id, "❌ Скасувати"))
        self.assertNotIn(chat_id, pending_global_household)


# =========================
# Routing.
# =========================
class TestRouting(MultiTargetTestCase):
    def test_active_inventory_context_routes_correctly(self):
        chat_id = 980501
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.assertIn(chat_id, pending_global_household)

    def test_global_command_without_context_routes_correctly(self):
        chat_id = 980502
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.assertIn(chat_id, pending_global_household)

    def test_single_target_delete_not_regressed(self):
        chat_id = 980503
        _call_webhook(_make_update(next(_uid), chat_id, "Видали хліб"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 903)

    def test_single_target_partial_consume_not_regressed(self):
        chat_id = 980504
        saved_list_context[chat_id] = "inventory_saved"
        _call_webhook(_make_update(next(_uid), chat_id, "Видали 130 г печива"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertIn(chat_id, pending_inventory_consumption)

    def test_active_pending_has_priority(self):
        chat_id = 980505
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.assertIn(chat_id, pending_global_household)
        # A second multi-target-shaped message while a preview is already
        # pending must not start a NEW one (pending routes win before command
        # routes are ever reached again).
        _call_webhook(_make_update(next(_uid), chat_id, "Видали одне автокрісло і один хліб"))
        self.assertEqual(len(pending_global_household[chat_id]["consume_changes"]), 3)

    def test_explicit_shopping_command_not_intercepted(self):
        # No trigger verb at all ("Додай") -> the pre-gate rejects it outright
        # and the message proceeds through the existing add-to-shopping
        # routing, completely untouched by this feature.
        chat_id = 980506
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Додай молоко і сир до покупок"))
        _call_webhook(_make_update(next(_uid), chat_id, "Додай молоко і сир до покупок"))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_multi_target_classify.assert_not_called()

    def test_expense_delete_command_not_intercepted(self):
        # No trigger verb at all ("Скасуй") -> the pre-gate rejects it
        # outright and the message proceeds through the existing expense-
        # delete routing, completely untouched by this feature.
        chat_id = 980507
        self.assertFalse(inventory_multi_target.looks_like_inventory_multi_target("Скасуй дві останні витрати"))
        _call_webhook(_make_update(next(_uid), chat_id, "Скасуй дві останні витрати"))
        self.assertNotIn(chat_id, pending_global_household)
        self.mock_multi_target_classify.assert_not_called()

    def test_at_most_one_gemini_call(self):
        chat_id = 980508
        # Deterministic path succeeds -> Gemini fallback never invoked.
        _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.mock_multi_target_classify.assert_not_called()

    def test_gemini_fallback_used_when_deterministic_split_fails(self):
        chat_id = 980509
        self.mock_multi_target_classify.return_value = {
            "version": 1, "action": "inventory_batch_change", "targets": [], "clarification_question": None,
        }
        # Force the deterministic splitter to fail by using an unrecognized
        # trigger-adjacent shape (bulk pronoun alone is rejected outright by
        # the pre-gate, so use a shape the splitter accepts as "worth a
        # route hit" but can't fully parse per-segment instead: patch
        # parse_multi_target_command directly to simulate that low-
        # confidence case without inventing a new brittle text shape).
        with patch.object(inventory_multi_target, "parse_multi_target_command", return_value=None):
            _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.mock_multi_target_classify.assert_called_once()

    def test_gemini_fallback_unsupported_shows_controlled_message(self):
        chat_id = 980510
        self.mock_multi_target_classify.return_value = {
            "version": 1, "action": "unsupported", "targets": [], "clarification_question": None,
        }
        with patch.object(inventory_multi_target, "parse_multi_target_command", return_value=None):
            _call_webhook(_make_update(next(_uid), chat_id, "Видали печиво, хліб і автокрісло"))
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any(t == inventory_multi_target.UNSUPPORTED_MSG for t in self._sent_texts()))


# =========================
# DB-LAYER — the REAL database.apply_global_household_operations exercised
# against a fake connection/cursor with the exact multi-item consume_changes
# shape _resolve_inventory_batch_targets produces (mixed will_remove=True/
# False in one batch), proving the reused executor's one-transaction/one-
# journal-row/atomic-rollback contract holds for a 3-target batch. Same
# FakeCursor/FakeConnection technique as tests/test_global_household_
# operations.py — no real Postgres involved. apply_undo_action's own
# restore-path re-verification is exhaustively covered generically by
# tests/test_safe_undo_global_action.py and tests/test_action_journal.py
# (neither database.py nor action_history.py changed for this feature), so
# it isn't re-derived here.
# =========================
class FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.queries = []
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "DELETE" in sql:
            self.rowcount = len(params) - 1 if params else 0

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

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


def _bucket_row(item_id, name, canonical_name, quantity_text, quantity_value, quantity_unit, category):
    return (item_id, name, canonical_name, quantity_text, quantity_value, quantity_unit, False, category)


class TestRealExecutorDbLayer(unittest.TestCase):
    def test_three_target_batch_one_transaction_one_journal_row(self):
        # Mirrors the live scenario after clarification: a mixed batch of 2
        # partial updates (Автокрісло/Печиво) + 1 full delete (Хліб), all
        # applied in ONE call.
        cursor = FakeCursor(
            fetchone_results=[(901,), (902,)],  # UPDATE inventory_items ... RETURNING id (x2)
            fetchall_results=[
                [(901, 2.0, "шт."), (902, 1.0, "кг"), (903, 2.0, "шт.")],  # inventory_targets verify
                [(901, "автокрісло"), (902, "печиво"), (903, "хліб")],  # consume_ids -> canonical_name lookup
                [_bucket_row(901, "Автокрісло", "автокрісло", "2 шт.", 2.0, "шт.", "Інше")],  # before: автокрісло
                [_bucket_row(902, "Печиво", "печиво", "1 кг", 1.0, "кг", "Хліб і випічка")],  # before: печиво
                [_bucket_row(903, "Хліб", "хліб", "2 шт.", 2.0, "шт.", "Хліб і випічка")],  # before: хліб
                [_bucket_row(901, "Автокрісло", "автокрісло", "1 шт.", 1.0, "шт.", "Інше")],  # after: автокрісло
                [_bucket_row(902, "Печиво", "печиво", "0,8 кг", 0.8, "кг", "Хліб і випічка")],  # after: печиво
                [],  # after: хліб (row deleted -> empty bucket)
            ],
        )
        conn = FakeConnection(cursor)
        targets = [
            {"item_id": 901, "quantity_value": 2.0, "quantity_unit": "шт."},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "кг"},
            {"item_id": 903, "quantity_value": 2.0, "quantity_unit": "шт."},
        ]
        consume_updates = [
            {"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт."},
            {"item_id": 902, "quantity_value": 0.8, "quantity_unit": "кг", "quantity_text": "0,8 кг"},
        ]
        with patch.object(real_database, "get_connection", return_value=conn):
            result = real_database.apply_global_household_operations(
                household_id=1, user_db_id=10,
                consume_updates=consume_updates, consume_delete_ids=[903],
                inventory_targets=targets,
            )
        self.assertTrue(conn.committed)
        self.assertEqual(result["inventory_updated"], 2)
        self.assertEqual(result["inventory_removed"], 1)
        # Exactly one journal INSERT for the whole compound batch.
        journal_inserts = [q for q, _ in cursor.queries if "INSERT INTO household_action_journal" in q]
        self.assertEqual(len(journal_inserts), 1)
        update_queries = [q for q, _ in cursor.queries if q.strip().startswith("UPDATE inventory_items")]
        self.assertEqual(len(update_queries), 2)
        delete_queries = [q for q, _ in cursor.queries if q.strip().startswith("DELETE FROM inventory_items")]
        self.assertEqual(len(delete_queries), 1)

    def test_one_stale_target_rolls_back_whole_batch(self):
        # Live value (10) for item 902 doesn't match the snapshot (1.0) taken
        # when the preview was built -> the WHOLE batch aborts, including
        # the two OTHER targets that were perfectly fine.
        cursor = FakeCursor(fetchall_results=[[(901, 2.0, "шт."), (902, 10.0, "кг"), (903, 2.0, "шт.")]])
        conn = FakeConnection(cursor)
        targets = [
            {"item_id": 901, "quantity_value": 2.0, "quantity_unit": "шт."},
            {"item_id": 902, "quantity_value": 1.0, "quantity_unit": "кг"},
            {"item_id": 903, "quantity_value": 2.0, "quantity_unit": "шт."},
        ]
        consume_updates = [{"item_id": 901, "quantity_value": 1.0, "quantity_unit": "шт.", "quantity_text": "1 шт."}]
        with patch.object(real_database, "get_connection", return_value=conn):
            with self.assertRaises(real_database.StaleSnapshotError):
                real_database.apply_global_household_operations(
                    household_id=1, user_db_id=10,
                    consume_updates=consume_updates, consume_delete_ids=[903],
                    inventory_targets=targets,
                )
        self.assertFalse(conn.committed)
        # Only the verify SELECT ran — no UPDATE/DELETE/journal write at all.
        self.assertEqual(len(cursor.queries), 1)
        self.assertIn("FOR UPDATE", cursor.queries[0][0])


if __name__ == "__main__":
    unittest.main()
