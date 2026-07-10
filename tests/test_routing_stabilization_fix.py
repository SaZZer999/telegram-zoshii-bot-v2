"""Routing Stabilization v1 — two live-Voice-Input-V1 text-routing bugs,
neither of them a transcription problem:

Bug 1: "Додай молоко і сир до покупок." (destination phrase LAST, not
first) used to fall through to Global Bare Add v1's "Куди додати ці
позиції?" destination clarification instead of being recognized as an
explicit shopping-destination command — see household_router.py's
_TRAILING_SHOPPING_DESTINATION_RE/_TRAILING_INVENTORY_DESTINATION_RE.

Bug 2: a meal-ideas-shaped question mentioning "вдома"/"є" ("Що можна
приготувати на вечерю з того, що є вдома?") used to be misclassified by
household_read's own Gemini classifier (which has no "meal_ideas" intent)
as inventory_overview and answered with the plain inventory list — see
message_dispatcher.dispatch's own docstring for the reordering fix
(meal_ideas is now tried before household_read in Phase D).

No real Gemini/Groq call happens anywhere in this file.
"""
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
import household_router  # noqa: E402
import household_read_context  # noqa: E402
import meal_ideas  # noqa: E402
import voice_input  # noqa: E402
from bot import (  # noqa: E402
    pending_global_household,
    pending_add_destination_clarification,
    active_list_context,
    saved_list_context,
    ADD_DESTINATION_CLARIFICATION_QUESTION,
)


def _make_update(update_id, chat_id, text, user_id=555):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": user_id, "first_name": "Тест"}},
    }


def _make_voice_update(update_id, chat_id, file_id="voice_1", duration=5, user_id=555):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id}, "voice": {"file_id": file_id, "duration": duration},
            "from": {"id": user_id, "first_name": "Тест"},
        },
    }


def _call_webhook(update):
    with bot.app.test_request_context(json=update):
        return bot.webhook()


def _milk_and_cheese_items():
    return {
        "items": [
            {"name": "Молоко", "quantity_text": "", "category": "Молочне та яйця"},
            {"name": "Сир", "quantity_text": "", "category": "Молочне та яйця"},
        ],
        "unresolved_fragments": [],
    }


# =========================
# Bug 1 — explicit "до покупок"/"до запасів" said LAST, not first.
# =========================
class _BaseRoutingFixTestCase(unittest.TestCase):
    def setUp(self):
        patcher_get_user = patch.object(bot, "get_household_and_user", return_value=(1, 10))
        patcher_get_user.start()
        self.addCleanup(patcher_get_user.stop)

        patcher_alias_map = patch.object(bot, "get_household_alias_map", return_value={})
        patcher_alias_map.start()
        self.addCleanup(patcher_alias_map.stop)

        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

        patcher_items = patch.object(household_router, "_ask_gemini_explicit_add_items")
        self.mock_items = patcher_items.start()
        self.addCleanup(patcher_items.stop)

        patcher_inv = patch.object(bot, "get_inventory_items", return_value=[])
        self.mock_inventory = patcher_inv.start()
        self.addCleanup(patcher_inv.stop)

    def tearDown(self):
        for d in (pending_global_household, pending_add_destination_clarification, active_list_context, saved_list_context):
            d.clear()

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestExplicitShoppingDestinationSaidLast(_BaseRoutingFixTestCase):
    # 1. Typed text — shopping preview directly, no destination
    # clarification.
    def test_typed_text_creates_shopping_preview_directly(self):
        chat_id = 991501
        self.mock_items.return_value = _milk_and_cheese_items()
        _call_webhook(_make_update(991501001, chat_id, "Додай молоко і сир до покупок."))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 2)
        self.assertEqual(payload["add_inventory_items"], [])
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertFalse(any(ADD_DESTINATION_CLARIFICATION_QUESTION in t for t in self._sent_texts()))

    # 2. Voice transcript path equivalent — same outcome as typed text.
    def test_voice_transcript_creates_shopping_preview_directly(self):
        chat_id = 991502
        self.mock_items.return_value = _milk_and_cheese_items()
        with patch.object(bot, "_download_telegram_voice_to_temp", return_value="/tmp/v.ogg"):
            with patch.object(voice_input, "transcribe_audio_file", return_value="Додай молоко і сир до покупок."):
                with patch("os.remove"):
                    _call_webhook(_make_voice_update(991502001, chat_id))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(len(payload["add_shopping_items"]), 2)
        self.assertNotIn(chat_id, pending_add_destination_clarification)
        self.assertTrue(any("🎙️ Розпізнав:" in t for t in self._sent_texts()))

    # 3. Variants.
    def test_v_pokupky_variant(self):
        chat_id = 991503
        self.mock_items.return_value = _milk_and_cheese_items()
        _call_webhook(_make_update(991503001, chat_id, "додай молоко і сир в покупки"))
        self.assertIn(chat_id, pending_global_household)
        self.assertEqual(len(pending_global_household[chat_id]["add_shopping_items"]), 2)
        self.assertNotIn(chat_id, pending_add_destination_clarification)

    def test_u_spysok_pokupok_variant(self):
        chat_id = 991504
        self.mock_items.return_value = _milk_and_cheese_items()
        _call_webhook(_make_update(991504001, chat_id, "додай молоко і сир у список покупок"))
        self.assertIn(chat_id, pending_global_household)
        self.assertEqual(len(pending_global_household[chat_id]["add_shopping_items"]), 2)
        self.assertNotIn(chat_id, pending_add_destination_clarification)

    def test_do_spysku_pokupok_variant(self):
        chat_id = 991505
        self.mock_items.return_value = _milk_and_cheese_items()
        _call_webhook(_make_update(991505001, chat_id, "додай молоко і сир до списку покупок"))
        self.assertIn(chat_id, pending_global_household)
        self.assertEqual(len(pending_global_household[chat_id]["add_shopping_items"]), 2)
        self.assertNotIn(chat_id, pending_add_destination_clarification)

    # 4. Inventory destination said last.
    def test_do_zapasiv_creates_inventory_preview_directly(self):
        chat_id = 991506
        self.mock_items.return_value = _milk_and_cheese_items()
        _call_webhook(_make_update(991506001, chat_id, "додай молоко і сир до запасів"))
        self.assertIn(chat_id, pending_global_household)
        payload = pending_global_household[chat_id]
        self.assertEqual(payload["add_shopping_items"], [])
        self.assertEqual(len(payload["add_inventory_items"]), 2)
        self.assertNotIn(chat_id, pending_add_destination_clarification)

    # 5. No explicit destination — clarification still appears (safety
    # preserved).
    def test_no_destination_still_asks_clarification(self):
        chat_id = 991507
        self.mock_items.return_value = _milk_and_cheese_items()
        _call_webhook(_make_update(991507001, chat_id, "додай молоко і сир"))
        self.assertIn(chat_id, pending_add_destination_clarification)
        self.assertNotIn(chat_id, pending_global_household)
        self.assertTrue(any(ADD_DESTINATION_CLARIFICATION_QUESTION in t for t in self._sent_texts()))


class TestDetectExplicitAddDestinationTrailingForm(unittest.TestCase):
    """Pure household_router.py unit coverage for the trailing-phrase
    regex itself, independent of the webhook/Gemini-mocking machinery
    above."""
    def test_trailing_shopping_phrase_variants(self):
        variants = [
            "Додай молоко і сир до покупок.",
            "додай молоко і сир в покупки",
            "додай молоко і сир у покупки",
            "додай молоко і сир до списку покупок",
            "додай молоко і сир в список покупок",
            "додай молоко і сир у список покупок",
        ]
        for text in variants:
            with self.subTest(text=text):
                self.assertEqual(
                    household_router.detect_explicit_add_destination(text),
                    ("add_shopping", "молоко і сир"),
                )

    def test_trailing_inventory_phrase_variants(self):
        variants = ["додай молоко і сир до запасів", "додай молоко і сир в запаси", "додай молоко і сир у запаси"]
        for text in variants:
            with self.subTest(text=text):
                self.assertEqual(
                    household_router.detect_explicit_add_destination(text),
                    ("add_inventory", "молоко і сир"),
                )

    def test_leading_form_still_works_unchanged(self):
        self.assertEqual(
            household_router.detect_explicit_add_destination("Додай до покупок молоко і хліб"),
            ("add_shopping", "молоко і хліб"),
        )
        self.assertEqual(
            household_router.detect_explicit_add_destination("Додай в запаси 2 банани"),
            ("add_inventory", "2 банани"),
        )

    def test_no_destination_returns_none(self):
        self.assertEqual(household_router.detect_explicit_add_destination("Додай молоко і сир"), (None, None))

    def test_voice_transcript_punctuation_and_case_handled(self):
        self.assertEqual(
            household_router.detect_explicit_add_destination("ДОДАЙ молоко і сир ДО ПОКУПОК!"),
            ("add_shopping", "молоко і сир"),
        )
        self.assertEqual(
            household_router.detect_explicit_add_destination("додай молоко, сир до покупок,"),
            ("add_shopping", "молоко, сир"),
        )


# =========================
# Bug 2 — meal-idea intent must win over generic inventory-read intent.
# =========================
class _BaseMealVsReadTestCase(unittest.TestCase):
    def setUp(self):
        patcher_send = patch.object(bot, "send_message")
        self.mock_send = patcher_send.start()
        self.addCleanup(patcher_send.stop)

    def _sent_texts(self):
        return [call.args[1] for call in self.mock_send.call_args_list]


class TestMealIdeaIntentWinsOverInventoryRead(_BaseMealVsReadTestCase):
    def _run_with_meal_ideas_mocked(self, chat_id, text, update_id):
        with patch.object(meal_ideas, "try_handle_meal_ideas", return_value=True) as mock_meal_ideas:
            with patch.object(household_read_context, "try_handle_household_read") as mock_household_read:
                _call_webhook(_make_update(update_id, chat_id, text))
        mock_meal_ideas.assert_called_once()
        mock_household_read.assert_not_called()

    # 6.
    def test_meal_ideas_wins_for_vecheryu_z_togo_shcho_ye_vdoma(self):
        self._run_with_meal_ideas_mocked(991510, "Що можна приготувати на вечерю з того, що є вдома?", 991510001)

    # 7.
    def test_meal_ideas_wins_for_vecheri_z_togo_shcho_u_nas_ye_doma(self):
        self._run_with_meal_ideas_mocked(991511, "Що можна приготувати на вечері з того, що у нас є дома?", 991511001)

    # 8.
    def test_meal_ideas_wins_for_zaproponuy_vecheryu(self):
        self._run_with_meal_ideas_mocked(991512, "запропонуй вечерю з того що є", 991512001)

    # 9. "Що є вдома?" still answers the inventory list (unmocked real
    # deterministic parser/meal-ideas-gate — neither one wrongly claims it
    # for the wrong reason).
    def test_shcho_ye_vdoma_still_answers_inventory_list(self):
        chat_id = 991513
        with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
            with patch.object(bot, "get_inventory_items", return_value=[]) as mock_inv:
                with patch.object(meal_ideas, "try_handle_meal_ideas") as mock_meal_ideas:
                    _call_webhook(_make_update(991513001, chat_id, "Що є вдома?"))
        mock_meal_ideas.assert_not_called()
        mock_inv.assert_called_once()

    # 10. "Що у нас є дома?" is NOT one of the exact deterministic overview
    # phrases (word order differs from "що є в нас вдома"), so — unlike
    # "Що є вдома?" above — it only resolves via household_read's Phase-D
    # Gemini-classifier fallback. meal_ideas' own real gate correctly
    # declines it (mocked here to return False, standing in for that real
    # decline, so this test never needs a real Gemini call to prove
    # meal_ideas' own text-matching logic — that's already covered by
    # test_meal_ideas_module.py); the point here is only that the REORDER
    # doesn't break household_read's classifier fallback for a legitimate
    # non-meal phrase.
    def test_shcho_u_nas_ye_doma_still_answers_inventory_list(self):
        chat_id = 991514
        with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
            with patch.object(bot, "get_inventory_items", return_value=[]) as mock_inv:
                with patch.object(bot, "call_gemini", return_value='{"intent": "inventory_overview"}'):
                    with patch.object(meal_ideas, "try_handle_meal_ideas", return_value=False) as mock_meal_ideas:
                        _call_webhook(_make_update(991514001, chat_id, "Що у нас є дома?"))
        mock_meal_ideas.assert_called_once()
        mock_inv.assert_called_once()

    # 11. "Чи є молоко?" — availability answer still works.
    def test_chy_ye_moloko_still_answers_availability(self):
        chat_id = 991515
        with patch.object(bot, "get_household_and_user", return_value=(1, 10)):
            with patch.object(bot, "get_inventory_items", return_value=[
                {"id": 1, "name": "Молоко", "category": "Молочне та яйця", "canonical_name": "молоко",
                 "quantity_value": 1.0, "quantity_unit": "л", "quantity_text": "1 л", "quantity_inferred": False},
            ]):
                with patch.object(meal_ideas, "try_handle_meal_ideas") as mock_meal_ideas:
                    _call_webhook(_make_update(991515001, chat_id, "Чи є молоко?"))
        mock_meal_ideas.assert_not_called()
        self.assertTrue(any("Так, є:" in t for t in self._sent_texts()))

    # 12. "Що треба купити?" — shopping read answer still works.
    def test_shcho_trebo_kupyty_still_answers_shopping_list(self):
        chat_id = 991516
        with patch.object(bot, "get_active_shopping_items", return_value=[]) as mock_shop:
            with patch.object(meal_ideas, "try_handle_meal_ideas") as mock_meal_ideas:
                _call_webhook(_make_update(991516001, chat_id, "Що треба купити?"))
        mock_meal_ideas.assert_not_called()
        mock_shop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
