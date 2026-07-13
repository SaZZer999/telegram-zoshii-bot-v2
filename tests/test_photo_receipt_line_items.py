"""Receipt V2 — line items. Extends Photo Receipt Input V1 (see
tests/test_photo_receipt_routing.py, which stays untouched and fully
green) with product line-item extraction: when a receipt photo yields 1+
usable line items, bot._handle_photo_message builds ONE combined
pending_global_household preview (💸 Витрати for the receipt total, when
present, plus 🧊 Запаси for the line items) instead of the single-expense
pending_expense preview V1 always used — reusing the exact same
confirm/cancel/apply_global_household_operations/Pending Preview Edit
Planner machinery a typed mixed household command already has.

A receipt with NO line items at all (candidate.line_items == [], the
default for every ReceiptCandidate built without passing that kwarg) is
completely unaffected by any of this — see test_photo_receipt_routing.py
and test_photo_receipts_module.py, both still fully green with zero
changes.

Two layers of coverage:
  - Pure unit tests for photo_receipts._parse_line_items/_parse_line_item
    (no webhook) — the REAL proof that a discount/deposit/bag line never
    survives into a ReceiptCandidate's line_items at all.
  - Webhook-level integration tests (no real Gemini/Telegram/Supabase call
    anywhere here — every network-facing function is patched per test),
    same posture as tests/test_photo_receipt_routing.py. These build
    line_items via photo_receipts._parse_line_items(raw_gemini_shaped_
    dicts) rather than hand-rolling an already-filtered list, so the
    webhook tests exercise the SAME parsing/filtering code path a real
    receipt would.
"""
import sys
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.modules['database'] = MagicMock()
sys.modules['groq'] = MagicMock()
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
os.environ.setdefault('ALLOWED_USER_IDS', '')

import bot  # noqa: E402
import photo_receipts  # noqa: E402
from bot import pending_expense, pending_global_household  # noqa: E402


# =========================
# Pure unit tests — photo_receipts._parse_line_items/_parse_line_item, no
# webhook, no bot.py involved.
# =========================
class TestParseLineItems(unittest.TestCase):
    def test_clean_item_parses_with_quantity_and_price(self):
        items = photo_receipts._parse_line_items([
            {"name": "Mleko", "quantity": "2", "unit": "л", "line_price": "8.00"},
        ])
        self.assertEqual(len(items), 1)
        # Receipt V2.1: "mleko" is a known grocery word — normalized to
        # its Ukrainian household name (see _normalize_product_name).
        self.assertEqual(items[0]["name"], "Молоко")
        self.assertEqual(items[0]["quantity_text"], "2 л")
        self.assertEqual(items[0]["line_price"], Decimal("8.00"))

    def test_whole_number_quantity_never_becomes_scientific_notation(self):
        items = photo_receipts._parse_line_items([
            {"name": "Jajka", "quantity": "10", "unit": "шт", "line_price": "12.00"},
        ])
        self.assertEqual(items[0]["quantity_text"], "10 шт")

    def test_discount_line_is_dropped(self):
        items = photo_receipts._parse_line_items([
            {"name": "Mleko", "quantity": "1", "unit": "л", "line_price": "4.00"},
            {"name": "Rabat -10%", "quantity": None, "unit": None, "line_price": "1.00"},
        ])
        names = [i["name"] for i in items]
        self.assertEqual(names, ["Молоко"])

    def test_kaucja_deposit_line_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "Kaucja butelka", "quantity": None, "unit": None, "line_price": "0.50"}])
        self.assertEqual(items, [])

    def test_reklamowka_bag_line_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "Reklamówka", "quantity": "1", "unit": "шт", "line_price": "0.40"}])
        self.assertEqual(items, [])

    def test_vat_summary_line_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "PTU VAT A 23%", "quantity": None, "unit": None, "line_price": None}])
        self.assertEqual(items, [])

    def test_unclear_quantity_or_unit_falls_back_to_blank_quantity_text(self):
        items = photo_receipts._parse_line_items([{"name": "Chleb", "quantity": None, "unit": None, "line_price": "5.00"}])
        self.assertEqual(items[0]["quantity_text"], "")

    def test_unrecognized_unit_falls_back_to_blank_quantity_text(self):
        items = photo_receipts._parse_line_items([{"name": "Chleb", "quantity": "1", "unit": "bochenek", "line_price": "5.00"}])
        self.assertEqual(items[0]["quantity_text"], "")

    def test_blank_name_is_dropped(self):
        items = photo_receipts._parse_line_items([{"name": "  ", "quantity": "1", "unit": "шт", "line_price": "5.00"}])
        self.assertEqual(items, [])

    def test_non_list_input_returns_empty(self):
        self.assertEqual(photo_receipts._parse_line_items(None), [])
        self.assertEqual(photo_receipts._parse_line_items("not a list"), [])

    def test_non_dict_entry_is_skipped(self):
        items = photo_receipts._parse_line_items(["not a dict", {"name": "Mleko", "quantity": "1", "unit": "л", "line_price": "4.00"}])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Молоко")


class TestDecideReceiptOutcomeWithLineItems(unittest.TestCase):
    def test_total_and_items_gives_ok_with_items(self):
        candidate = photo_receipts.ReceiptCandidate(
            is_receipt=True, amount=Decimal("30.00"),
            line_items=[{"name": "Mleko", "quantity_text": "2 л", "line_price": Decimal("8.00")}],
        )
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "ok_with_items")
        self.assertEqual(payload["amount"], Decimal("30.00"))
        self.assertEqual(len(payload["line_items"]), 1)

    def test_items_without_total_gives_items_only(self):
        candidate = photo_receipts.ReceiptCandidate(
            is_receipt=True, amount=None, confidence="low",
            line_items=[{"name": "Mleko", "quantity_text": "2 л", "line_price": Decimal("8.00")}],
        )
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "items_only")
        self.assertNotIn("amount", payload)
        self.assertEqual(len(payload["line_items"]), 1)

    def test_total_without_items_gives_old_ok_shape_unchanged(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=Decimal("30.00"), line_items=[])
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "ok")
        self.assertEqual(set(payload.keys()), {"amount", "merchant", "expense_date", "category_hint", "confidence", "warnings"})

    def test_neither_total_nor_items_gives_missing_amount(self):
        candidate = photo_receipts.ReceiptCandidate(is_receipt=True, amount=None, line_items=[])
        kind, payload = photo_receipts.decide_receipt_outcome(candidate)
        self.assertEqual(kind, "missing_amount")
        self.assertIsNone(payload)


# =========================
# Webhook-level integration tests.
# =========================
def _make_photo_update(update_id, chat_id, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "photo": [{"file_id": "large_1", "width": 1280, "height": 1280, "file_size": 90000}],
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _receipt_candidate(**overrides):
    fields = {
        "is_receipt": True, "merchant": "Biedronka", "amount": Decimal("30.00"),
        "currency": "PLN", "date": "2026-07-10", "category_hint": "grocery",
        "confidence": "high", "warnings": [], "line_items": [],
    }
    fields.update(overrides)
    return photo_receipts.ReceiptCandidate(**fields)


def _milk_and_eggs_raw_items():
    """Raw Gemini-shaped line items — routed through the REAL
    photo_receipts._parse_line_items in every fixture below, so these
    webhook tests exercise the actual parsing/filtering code, not a
    hand-rolled shortcut."""
    return [
        {"name": "Mleko", "quantity": "2", "unit": "л", "line_price": "8.00"},
        {"name": "Jajka", "quantity": "10", "unit": "шт", "line_price": "12.00"},
    ]


class PhotoLineItemsWebhookTestCase(unittest.TestCase):
    def setUp(self):
        pending_expense.clear()
        pending_global_household.clear()
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)
        patcher_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_user.start()
        self.addCleanup(patcher_user.stop)
        patcher_alias = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias.start()
        self.addCleanup(patcher_alias.stop)
        patcher_inventory = patch.object(bot, "get_inventory_items", return_value=[])
        patcher_inventory.start()
        self.addCleanup(patcher_inventory.stop)
        self._download_patcher = patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/receipt.jpg")
        self._download_patcher.start()
        self.addCleanup(self._download_patcher.stop)
        self._remove_patcher = patch("os.remove")
        self._remove_patcher.start()
        self.addCleanup(self._remove_patcher.stop)

    def tearDown(self):
        pending_expense.clear()
        pending_global_household.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]

    def _send_photo(self, update_id, chat_id, candidate):
        with patch.object(photo_receipts, "extract_receipt_from_image", return_value=candidate):
            _call_webhook(_make_photo_update(update_id, chat_id))


# 1 — total + line items -> combined expense + inventory preview.
class TestCombinedExpenseAndInventoryPreview(PhotoLineItemsWebhookTestCase):
    def test_total_and_line_items_build_combined_preview(self):
        chat_id = 998001
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998001001, chat_id, candidate)

        self.assertNotIn(chat_id, pending_expense)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(len(data["new_expenses"]), 1)
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("30.00"))
        self.assertEqual(data["new_expenses"][0]["description"], "Biedronka")
        names = {item["canonical_name"] for item in data["add_inventory_items"]}
        self.assertIn("молоко", names)
        # Receipt V2.1: "jajka" is a known grocery word too — normalized
        # to "яйця" before this canonical name is ever computed.
        self.assertIn("яйця", names)
        texts = self._sent_texts()
        self.assertTrue(any("30,00 zł" in t and "🧊" in t and "💸" in t for t in texts))


# 2 — discounts/rabat lines never become inventory (end-to-end, real filter).
class TestDiscountsNeverBecomeInventory(PhotoLineItemsWebhookTestCase):
    def test_discount_and_deposit_lines_are_dropped(self):
        chat_id = 998002
        raw_items = [
            {"name": "Mleko", "quantity": "1", "unit": "л", "line_price": "4.00"},
            {"name": "Rabat -10%", "quantity": None, "unit": None, "line_price": "1.00"},
            {"name": "Kaucja butelka", "quantity": None, "unit": None, "line_price": "0.50"},
        ]
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(raw_items))
        self._send_photo(998002001, chat_id, candidate)

        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        names = [item["name"] for item in data["add_inventory_items"]]
        self.assertEqual(len(names), 1)
        self.assertNotIn("Rabat -10%", names)
        self.assertNotIn("Kaucja butelka", names)


# 3 — unclear quantity gets a safe default (1 шт., flagged as an
# assumption in the preview) rather than being invented or rejected.
class TestUnclearQuantityGetsSafeDefault(PhotoLineItemsWebhookTestCase):
    def test_missing_quantity_defaults_to_one_piece_with_note(self):
        chat_id = 998003
        raw_items = [{"name": "Chleb", "quantity": None, "unit": None, "line_price": "5.00"}]
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(raw_items))
        self._send_photo(998003001, chat_id, candidate)

        data = pending_global_household[chat_id]
        item = data["add_inventory_items"][0]
        self.assertEqual(item["quantity_value"], Decimal("1"))
        self.assertEqual(item["quantity_unit"], "шт.")
        self.assertTrue(item["quantity_inferred"])
        self.assertTrue(any("(припущення)" in t for t in self._sent_texts()))


# 4 — total present but zero usable line items -> old V1 behavior unchanged.
class TestNoLineItemsKeepsOldBehavior(PhotoLineItemsWebhookTestCase):
    def test_total_without_line_items_uses_old_single_expense_flow(self):
        chat_id = 998004
        candidate = _receipt_candidate(line_items=[])
        self._send_photo(998004001, chat_id, candidate)

        self.assertIn(chat_id, pending_expense)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertEqual(pending_expense[chat_id]["amount"], Decimal("30.00"))

    def test_only_denylisted_line_items_also_uses_old_single_expense_flow(self):
        # Every raw line item is filtered out (discount/deposit only) —
        # candidate.line_items ends up empty after the real photo_
        # receipts._parse_line_items filtering, same as if Gemini had
        # reported none at all.
        chat_id = 998005
        raw_items = [
            {"name": "Rabat -10%", "quantity": None, "unit": None, "line_price": "1.00"},
            {"name": "Kaucja butelka", "quantity": None, "unit": None, "line_price": "0.50"},
        ]
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(raw_items))
        self._send_photo(998005001, chat_id, candidate)
        self.assertIn(chat_id, pending_expense)


# 5 — line items but unclear total -> inventory-only preview, no invented expense.
class TestUnclearTotalGivesInventoryOnlyPreview(PhotoLineItemsWebhookTestCase):
    def test_missing_total_with_line_items_never_invents_an_expense(self):
        chat_id = 998006
        candidate = _receipt_candidate(
            amount=None, confidence="low",
            line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()),
        )
        self._send_photo(998006001, chat_id, candidate)

        self.assertNotIn(chat_id, pending_expense)
        self.assertIn(chat_id, pending_global_household)
        data = pending_global_household[chat_id]
        self.assertEqual(data["new_expenses"], [])
        self.assertTrue(len(data["add_inventory_items"]) >= 1)


# 6/7 — confirm writes only after ✅, cancel writes nothing.
class TestConfirmCancelAfterLineItemsPreview(PhotoLineItemsWebhookTestCase):
    def test_confirm_writes_expense_and_inventory(self):
        chat_id = 998007
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998007001, chat_id, candidate)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            mock_apply.return_value = {
                "shopping_added": 0, "inventory_added": 2, "inventory_updated": 0,
                "inventory_removed": 0, "expense_added_id": 1, "expense_deleted": False,
            }
            _call_webhook({
                "update_id": 998007002,
                "message": {"chat": {"id": chat_id}, "text": "✅ Так, застосувати", "from": {"id": 555, "first_name": "Тест"}},
            })
        mock_apply.assert_called_once()
        _, kwargs = mock_apply.call_args
        self.assertEqual(len(kwargs["add_inventory_items"]), 2)
        self.assertEqual(kwargs["new_expenses"][0]["amount"], Decimal("30.00"))
        self.assertNotIn(chat_id, pending_global_household)

    def test_cancel_writes_nothing(self):
        chat_id = 998008
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998008001, chat_id, candidate)
        with patch.object(bot, "apply_global_household_operations") as mock_apply:
            _call_webhook({
                "update_id": 998008002,
                "message": {"chat": {"id": chat_id}, "text": "❌ Скасувати", "from": {"id": 555, "first_name": "Тест"}},
            })
        mock_apply.assert_not_called()
        self.assertNotIn(chat_id, pending_global_household)


# Pending Preview Edit Planner should still be able to edit the resulting
# combined preview where possible (existing deterministic quantity edit).
class TestPreviewEditPlannerStillEditsResult(PhotoLineItemsWebhookTestCase):
    def test_quantity_edit_still_works_on_receipt_preview(self):
        chat_id = 998009
        candidate = _receipt_candidate(line_items=photo_receipts._parse_line_items(_milk_and_eggs_raw_items()))
        self._send_photo(998009001, chat_id, candidate)
        _call_webhook({
            "update_id": 998009002,
            "message": {"chat": {"id": chat_id}, "text": "молока 1 л", "from": {"id": 555, "first_name": "Тест"}},
        })
        data = pending_global_household[chat_id]
        quantities = {item["canonical_name"]: item["quantity_text"] for item in data["add_inventory_items"]}
        self.assertEqual(quantities.get("молоко"), "1 л")


# =========================
# Live bug: a discount/rabat row for a product ALREADY on the receipt
# (SER GOUDA appearing once as the real purchase and once as a negative-
# priced discount on that same cheese) must never be merged into the real
# item's quantity — see photo_receipts._parse_line_item's own docstring
# for the exact mechanism (a negative line_price drops the row entirely,
# BEFORE household_router's own auto-merge-by-name ever sees a duplicate
# to combine).
# =========================
class TestReceiptDiscountRowNeverDuplicatesProduct(unittest.TestCase):
    def test_negative_priced_duplicate_is_dropped_by_the_parser(self):
        # Pure unit level: the exact live shape — same name twice, second
        # occurrence carries the receipt's own negative discount price.
        raw_items = [
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "9.98"},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "-2.00"},
            {"name": "OLEJ BARTEK", "quantity": "1", "unit": "л", "line_price": "6.50"},
            {"name": "CZOSNEK", "quantity": "1", "unit": "шт", "line_price": "1.80"},
        ]
        items = photo_receipts._parse_line_items(raw_items)
        names = [i["name"] for i in items]
        # Receipt V2.1: "SER GOUDA" is normalized to its Ukrainian display
        # name before this count is even taken.
        self.assertEqual(names.count("Сир Гауда"), 1)
        self.assertEqual(len(items), 3)

    def test_zabka_receipt_gives_one_cheese_item_not_two(self):
        # Webhook level: the exact live receipt from the bug report —
        # 3 real products + a discount duplicate for the cheese — must
        # produce exactly one cheese row in the preview, never "2 шт.".
        chat_id = 998010
        raw_items = [
            {"name": "OLEJ BARTEK", "quantity": "1", "unit": "л", "line_price": "6.50"},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "9.98"},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "-2.00"},
            {"name": "CZOSNEK", "quantity": "1", "unit": "шт", "line_price": "1.80"},
        ]
        candidate = _receipt_candidate(
            merchant="Żabka", amount=Decimal("27.28"),
            line_items=photo_receipts._parse_line_items(raw_items),
        )

        pending_expense.clear()
        pending_global_household.clear()
        with patch.object(bot, "send_message") as mock_send, \
             patch.object(bot, "get_household_and_user", return_value=(1, 10)), \
             patch.object(bot, "get_household_alias_map", return_value={}), \
             patch.object(bot, "get_inventory_items", return_value=[]), \
             patch.object(bot, "_download_telegram_photo_to_temp", return_value="/tmp/zabka.jpg"), \
             patch("os.remove"), \
             patch.object(photo_receipts, "extract_receipt_from_image", return_value=candidate):
            _call_webhook(_make_photo_update(998010001, chat_id))

        data = pending_global_household[chat_id]
        cheese_items = [i for i in data["add_inventory_items"] if i["canonical_name"] == "сир гауда"]
        self.assertEqual(len(cheese_items), 1)
        self.assertEqual(cheese_items[0]["quantity_value"], Decimal("1"))
        self.assertEqual(len(data["add_inventory_items"]), 3)
        # Total expense stays exactly what the receipt said, unaffected by
        # excluding the discount row from inventory.
        self.assertEqual(data["new_expenses"][0]["amount"], Decimal("27.28"))
        self.assertEqual(data["new_expenses"][0]["description"], "Żabka")
        pending_expense.clear()
        pending_global_household.clear()


# =========================
# Receipt V2.1 — product name normalization (package-size stripping +
# Polish/English -> Ukrainian grocery-word translation). See
# photo_receipts._normalize_product_name/_strip_package_size.
# =========================
class TestProductNameNormalization(unittest.TestCase):
    def test_package_size_suffix_stripped_and_used_as_fallback_quantity(self):
        # 1 — "SER GOUDA 135g" -> display name "Сир Гауда", not the raw
        # "SER GOUDA 135g" the live bug reported; the embedded "135g" is
        # used as the quantity ONLY because Gemini's own quantity/unit
        # fields were blank here.
        item = photo_receipts._parse_line_item({
            "name": "SER GOUDA 135g", "quantity": None, "unit": None, "line_price": "9.98",
        })
        self.assertEqual(item["name"], "Сир Гауда")
        self.assertEqual(item["quantity_text"], "135 г")

    def test_separate_gemini_quantity_wins_over_embedded_package_size(self):
        item = photo_receipts._parse_line_item({
            "name": "SER GOUDA 135g", "quantity": "2", "unit": "шт", "line_price": "19.96",
        })
        self.assertEqual(item["name"], "Сир Гауда")
        self.assertEqual(item["quantity_text"], "2 шт")

    def test_olej_bartek_preserves_brand_name(self):
        # 2 — "OLEJ BARTEK" -> "Олія Bartek": translated grocery word +
        # preserved brand, not just "Олія" alone.
        item = photo_receipts._parse_line_item({
            "name": "OLEJ BARTEK", "quantity": "1", "unit": "л", "line_price": "6.50",
        })
        self.assertEqual(item["name"], "Олія Bartek")

    def test_czosnek_translates_to_chasnyk(self):
        # 3 — "CZOSNEK" -> "Часник".
        item = photo_receipts._parse_line_item({
            "name": "CZOSNEK", "quantity": "1", "unit": "шт", "line_price": "1.80",
        })
        self.assertEqual(item["name"], "Часник")

    def test_mleko_1l_strips_size_and_translates(self):
        item = photo_receipts._parse_line_item({
            "name": "MLEKO 1L", "quantity": None, "unit": None, "line_price": "4.50",
        })
        self.assertEqual(item["name"], "Молоко")
        self.assertEqual(item["quantity_text"], "1 л")

    def test_unrecognized_brand_only_name_gets_title_cased(self):
        item = photo_receipts._parse_line_item({
            "name": "COCA COLA", "quantity": "1", "unit": "л", "line_price": "7.00",
        })
        self.assertEqual(item["name"], "Coca Cola")


# =========================
# 4/5 — same-normalized-name duplicate where the discount row has NO price
# at all (neither positive nor negative) — the negative-price check alone
# can't catch this; _dedupe_discount_duplicates (grouping by the FINAL
# normalized name) is what's needed.
# =========================
class TestMissingPriceDuplicateIsDropped(unittest.TestCase):
    def test_priceless_duplicate_with_package_size_variant_is_dropped(self):
        # The real row has the package-size suffix; Gemini's discount row
        # for the SAME cheese has no distinguishing keyword AND no price
        # at all (not even negative) — only visible as a same-name repeat
        # once both are normalized to "Сир Гауда".
        raw_items = [
            {"name": "SER GOUDA 135g", "quantity": None, "unit": None, "line_price": "9.98"},
            {"name": "SER GOUDA 135g", "quantity": None, "unit": None, "line_price": None},
        ]
        items = photo_receipts._parse_line_items(raw_items)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Сир Гауда")
        self.assertEqual(items[0]["line_price"], Decimal("9.98"))

    def test_one_real_one_priceless_cheese_duplicate_keeps_one(self):
        # Work order test 1: SER GOUDA duplicated, one row has a real
        # positive price, the other has no price at all (not negative,
        # just missing) — the priceless one is dropped, never "2 шт.".
        raw_items = [
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": "9.98"},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": None},
        ]
        items = photo_receipts._parse_line_items(raw_items)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Сир Гауда")
        self.assertEqual(items[0]["line_price"], Decimal("9.98"))

    def test_both_priceless_cheese_duplicates_collapsed_to_one(self):
        # Work order test 2: SER GOUDA duplicated, BOTH rows missing price
        # — no evidence of two purchased units, so only one cheese row
        # survives, never "2 шт.".
        raw_items = [
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": None},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": None},
        ]
        items = photo_receipts._parse_line_items(raw_items)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Сир Гауда")

    def test_two_genuinely_priced_identical_products_both_kept(self):
        # 5 — two CLEARLY real purchases (both have their own real price)
        # of the identical product must still be allowed to become 2 шт.
        raw_items = [
            {"name": "Jajka", "quantity": None, "unit": None, "line_price": "6.00"},
            {"name": "Jajka", "quantity": None, "unit": None, "line_price": "6.00"},
        ]
        items = photo_receipts._parse_line_items(raw_items)
        self.assertEqual(len(items), 2)

    def test_package_size_plus_suspicious_duplicate_never_becomes_two(self):
        # Work order test 4: "SER GOUDA 135g" plus a suspicious priceless
        # duplicate of the SAME cheese (no package-size token this time) —
        # final result is one cheese row; since the 135g variant is kept
        # (it carries real quantity_text), it wins over the blank one.
        raw_items = [
            {"name": "SER GOUDA 135g", "quantity": None, "unit": None, "line_price": None},
            {"name": "SER GOUDA", "quantity": None, "unit": None, "line_price": None},
        ]
        items = photo_receipts._parse_line_items(raw_items)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Сир Гауда")
        self.assertEqual(items[0]["quantity_text"], "135 г")

    def test_both_priceless_duplicates_are_collapsed_to_one(self):
        # Neither row has any price evidence at all — that's not proof of
        # two purchased units either, so only one survives (conservative
        # receipt duplicate policy: never invent a 2nd unit without real
        # evidence — see _dedupe_discount_duplicates).
        raw_items = [
            {"name": "Chleb", "quantity": None, "unit": None, "line_price": None},
            {"name": "Chleb", "quantity": None, "unit": None, "line_price": None},
        ]
        items = photo_receipts._parse_line_items(raw_items)
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
