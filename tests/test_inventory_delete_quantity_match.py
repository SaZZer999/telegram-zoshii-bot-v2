"""Inventory Delete Quantity-Match v1 — fixes the live bug where a natural
delete phrase with a spelled-out quantity and/or a trailing explanatory
clause ("Видали молоко одна штука, воно вже не потрібно.") matched NOTHING
in inventory.parse_inventory_delete_request: the whole sentence (quantity
words and explanatory tail included) became the "product name", which no
inventory row's canonical name ever equals, so the bot answered "Не
знайшов такого запису в запасах." even though "Молоко — 1 шт." existed.

Covers: inventory.parse_inventory_delete_request's new explanatory-tail
stripping (_EXPLANATORY_TAIL_RE) and spelled-out "one" quantity handling
(_LEADING_ONE_QUANTITY_RE / _ONE_PIECE_COUNT_WORDS+_PIECE_NOUN_WORDS), plus
the webhook-level delete-preview selection when two "Молоко" rows differ
only by quantity. No real Gemini/Telegram/Supabase call anywhere here.
"""
import sys
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

import inventory

sys.modules.setdefault('database', __import__('unittest.mock', fromlist=['MagicMock']).MagicMock())
sys.modules.setdefault('groq', __import__('unittest.mock', fromlist=['MagicMock']).MagicMock())
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
from bot import pending_cleanup_admin, pending_cleanup_admin_disambiguation, pending_cleanup_notice  # noqa: E402


# =========================
# Pure unit tests — inventory.parse_inventory_delete_request, no webhook.
# =========================
class TestExplanatoryTailStripped(unittest.TestCase):
    def test_voiced_one_piece_with_explanatory_tail(self):
        name, qty = inventory.parse_inventory_delete_request(
            "Видали молоко одна штука, воно вже не потрібно."
        )
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "1 шт.")

    def test_explanatory_tail_alone_never_becomes_part_of_name(self):
        name, qty = inventory.parse_inventory_delete_request("Видали молоко, воно вже не потрібно")
        self.assertEqual(name, "молоко")
        self.assertIsNone(qty)

    def test_tse_vzhe_ne_treba_tail_stripped(self):
        name, qty = inventory.parse_inventory_delete_request("видали сир, це вже не треба")
        self.assertEqual(name, "сир")
        self.assertIsNone(qty)

    def test_bilshe_ne_treba_tail_stripped(self):
        name, qty = inventory.parse_inventory_delete_request("видали хліб більше не треба")
        self.assertEqual(name, "хліб")
        self.assertIsNone(qty)

    def test_zakinchylosia_tail_stripped(self):
        name, qty = inventory.parse_inventory_delete_request("видали масло закінчилось")
        self.assertEqual(name, "масло")
        self.assertIsNone(qty)

    def test_bo_causal_clause_after_quantity_stripped(self):
        name, qty = inventory.parse_inventory_delete_request(
            "Видали молоко одна штука, бо воно зіпсувалося"
        )
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "1 шт.")

    def test_bo_causal_clause_without_quantity_stripped(self):
        name, qty = inventory.parse_inventory_delete_request("видали сир, бо запліснявів")
        self.assertEqual(name, "сир")
        self.assertIsNone(qty)


class TestSpelledOutOneQuantity(unittest.TestCase):
    def test_trailing_odna_shtuka(self):
        name, qty = inventory.parse_inventory_delete_request("Видали молоко одна штука")
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "1 шт.")

    def test_trailing_odnu_shtuku(self):
        name, qty = inventory.parse_inventory_delete_request("прибери молоко одну штуку")
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "1 шт.")

    def test_leading_odne(self):
        name, qty = inventory.parse_inventory_delete_request("Видали одне молоко")
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "1 шт.")

    def test_leading_odna(self):
        name, qty = inventory.parse_inventory_delete_request("видали одна сосиска")
        self.assertEqual(name, "сосиска")
        self.assertEqual(qty, "1 шт.")

    def test_numeric_1_shtuku_already_worked_and_still_does(self):
        name, qty = inventory.parse_inventory_delete_request("Видали молоко 1 штуку")
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "1 шт.")

    def test_numeric_1_sht_already_worked_and_still_does(self):
        name, qty = inventory.parse_inventory_delete_request("Видали молоко 1 шт")
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "1 шт.")


class TestStructuredQuantityHintsUnaffected(unittest.TestCase):
    def test_liters_hint(self):
        name, qty = inventory.parse_inventory_delete_request("Видали молоко 14,5 л")
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "14,5 л")

    def test_dot_decimal_liters_hint(self):
        name, qty = inventory.parse_inventory_delete_request("Видали молоко 14.5 л")
        self.assertEqual(name, "молоко")
        self.assertEqual(qty, "14,5 л")

    def test_grams_hint(self):
        name, qty = inventory.parse_inventory_delete_request("видали сир 500 г")
        self.assertEqual(name, "сир")
        self.assertEqual(qty, "500 г")

    def test_kg_comma_hint(self):
        name, qty = inventory.parse_inventory_delete_request("видали печиво 0,5 кг")
        self.assertEqual(name, "печиво")
        self.assertEqual(qty, "0,5 кг")


class TestExistingBehaviorUnaffected(unittest.TestCase):
    def test_bare_name_with_two_candidates_still_ambiguous(self):
        name, qty = inventory.parse_inventory_delete_request("видали молоко")
        self.assertEqual(name, "молоко")
        self.assertIsNone(qty)

    def test_pair_word_quantity_still_works(self):
        name, qty = inventory.parse_inventory_delete_request("прибери сосисок пару")
        self.assertEqual(name, "сосисок")
        self.assertEqual(qty, "пару")

    def test_multiword_product_name_not_mangled(self):
        name, qty = inventory.parse_inventory_delete_request("видали кокосове молоко")
        self.assertEqual(name, "кокосове молоко")
        self.assertIsNone(qty)


# =========================
# Webhook-level integration tests — the exact live-bug shape: two "Молоко"
# rows differing only by quantity.
# =========================
def _milk_one_piece_and_liters_inventory():
    return [
        {"id": 7, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
        {"id": 8, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("14.5"), "quantity_unit": "л", "quantity_text": "14,5 л"},
    ]


def _single_milk_row_inventory():
    return [
        {"id": 7, "name": "Молоко", "canonical_name": "молоко", "category": "Молочне та яйця",
         "quantity_value": Decimal("1"), "quantity_unit": "шт.", "quantity_text": "1 шт."},
    ]


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


class MilkQuantityDeleteWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_cleanup_admin.clear()
        pending_cleanup_admin_disambiguation.clear()
        pending_cleanup_notice.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)

    def tearDown(self):
        pending_cleanup_admin.clear()
        pending_cleanup_admin_disambiguation.clear()
        pending_cleanup_notice.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


# 1 — the exact live bug phrase now selects the "1 шт." row.
class TestVoicedOnePieceWithTailSelectsCorrectRow(MilkQuantityDeleteWebhookTestCase):
    def test_selects_one_piece_milk_row(self):
        chat_id = 773001
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            _call_webhook(_make_update(773001001, chat_id, "Видали молоко одна штука, воно вже не потрібно."))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 7)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 1 шт." in t for t in texts))


# 1b — a ", бо ..." causal tail (instead of "воно вже не потрібно") is
# stripped the same way and never blocks the "1 шт." row selection.
class TestBoCausalTailSelectsCorrectRow(MilkQuantityDeleteWebhookTestCase):
    def test_selects_one_piece_milk_row(self):
        chat_id = 773008
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            _call_webhook(_make_update(773008001, chat_id, "Видали молоко одна штука, бо воно зіпсувалося"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 7)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 1 шт." in t for t in texts))


# 2 — numeric "1 шт" already worked; still selects the right row.
class TestNumericOneShtSelectsCorrectRow(MilkQuantityDeleteWebhookTestCase):
    def test_selects_one_piece_milk_row(self):
        chat_id = 773002
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            _call_webhook(_make_update(773002001, chat_id, "Видали молоко 1 шт"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 7)


# 3 — liter hint selects the OTHER row.
class TestLiterHintSelectsLiterRow(MilkQuantityDeleteWebhookTestCase):
    def test_selects_liter_milk_row(self):
        chat_id = 773003
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            _call_webhook(_make_update(773003001, chat_id, "Видали молоко 14,5 л"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 8)
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 14,5 л" in t for t in texts))


# 4 — no quantity hint at all with two candidates -> clarification, no write.
class TestBareNameWithTwoCandidatesAsksClarification(MilkQuantityDeleteWebhookTestCase):
    def test_asks_which_milk_row(self):
        chat_id = 773004
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()), \
             patch.object(bot, "execute_inventory_delete") as mock_delete:
            _call_webhook(_make_update(773004001, chat_id, "Видали молоко"))
        self.assertNotIn(chat_id, pending_cleanup_admin)
        mock_delete.assert_not_called()
        texts = self._sent_texts()
        self.assertTrue(any("Молоко — 1 шт." in t for t in texts))
        self.assertTrue(any("Молоко — 14,5 л" in t for t in texts))


# 5 — only one milk row -> bare name selects it directly (no ambiguity).
class TestBareNameWithSingleCandidateSelectsIt(MilkQuantityDeleteWebhookTestCase):
    def test_selects_the_only_milk_row(self):
        chat_id = 773005
        with patch.object(bot, "get_inventory_items", return_value=_single_milk_row_inventory()):
            _call_webhook(_make_update(773005001, chat_id, "Видали молоко"))
        self.assertIn(chat_id, pending_cleanup_admin)
        self.assertEqual(pending_cleanup_admin[chat_id]["item_id"], 7)


# 6 — confirm deletes exactly the selected row; cancel deletes nothing.
class TestConfirmCancelAfterQuantityMatchedDelete(MilkQuantityDeleteWebhookTestCase):
    def test_confirm_deletes_selected_row(self):
        chat_id = 773006
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            _call_webhook(_make_update(773006001, chat_id, "Видали молоко одна штука, воно вже не потрібно."))
        with patch.object(bot, "execute_inventory_delete", return_value=True) as mock_delete:
            _call_webhook(_make_update(773006002, chat_id, "✅ Так, застосувати"))
        mock_delete.assert_called_once()
        args, _ = mock_delete.call_args
        self.assertEqual(args[2], 7)  # item_id
        self.assertNotIn(chat_id, pending_cleanup_admin)

    def test_cancel_deletes_nothing(self):
        chat_id = 773007
        with patch.object(bot, "get_inventory_items", return_value=_milk_one_piece_and_liters_inventory()):
            _call_webhook(_make_update(773007001, chat_id, "Видали молоко одна штука, воно вже не потрібно."))
        with patch.object(bot, "execute_inventory_delete") as mock_delete:
            _call_webhook(_make_update(773007002, chat_id, "❌ Скасувати"))
        mock_delete.assert_not_called()
        self.assertNotIn(chat_id, pending_cleanup_admin)


if __name__ == "__main__":
    unittest.main()
