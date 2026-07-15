"""Legacy Shopping Flow V1.

Owns the "menu-driven" shopping interaction (open shopping menu, add/mark/
delete via the dedicated shopping submenu) that predates the Global
Household Router. This module owns its own pending state (shopping_mode,
pending_batch, pending_mark_batch, pending_delete_batch) and the shopping
Gemini parser, but has NO dependency on bot.py, Flask, Telegram, psycopg or
any Gemini SDK — everything it needs from the outside world (sending
messages, DB reads/writes, alias lookups, shared list-editing helpers,
keyboards, prompts) is passed in via a `ShoppingFlowDeps` container built
and owned by bot.py.

Deliberately NOT here (still bot.py-owned, shared across shopping/inventory/
expenses): pending_merge, pending_saved_edit, saved_list_context,
pending_quick_purchase, the shared saved-list router, and every confirm/
cancel button handler that spans more than one flow.
"""
import json
import re
from dataclasses import dataclass
from typing import Callable

import quantities


# =========================
# STATE (module-owned)
# =========================
shopping_mode = {}            # chat_id -> "adding" | "marking" | "deleting" | "editing_number" | "editing_text"
pending_batch = {}            # chat_id -> {items, ignored_items, household_id, user_db_id}
pending_mark_batch = {}       # chat_id -> {items, household_id, user_db_id}
pending_delete_batch = {}     # chat_id -> {items, household_id, user_db_id}


@dataclass
class ShoppingFlowDeps:
    """Injected callbacks/values — no import of bot.py, ever."""
    send_message: Callable
    get_household_and_user: Callable
    get_household_alias_map: Callable
    get_active_shopping_items: Callable
    save_list_context: Callable
    normalize_item_quantity: Callable
    parse_item_text: Callable
    call_gemini: Callable
    ask_gemini_for_selection: Callable
    ask_gemini_preview_edit_router: Callable
    validate_preview_updates: Callable
    apply_preview_updates: Callable
    auto_merge_in_place: Callable
    format_shopping_list: Callable
    format_batch_preview: Callable
    format_grouped_list: Callable
    format_unresolved_fragments_message: Callable
    clear_shopping_state: Callable
    clear_inventory_state: Callable
    active_list_context: dict
    saved_list_context: dict
    waiting_for_ingredients: dict
    shopping_keyboard: dict
    add_preview_keyboard: dict
    mark_preview_keyboard: dict
    delete_preview_keyboard: dict
    shopping_parse_prompt: str
    default_category: str
    valid_categories: set
    db_error_msg: str
    selection_error_msg: str


# =========================
# PARSING
# =========================
def parse_shopping_list_with_gemini(deps, text, alias_map=None):
    history = [{"role": "user", "content": text}]
    raw = deps.call_gemini(history, deps.shopping_parse_prompt, temperature=0.1)
    if not raw:
        return None
    cleaned = raw.strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if match:
            cleaned = match.group(1).strip()
    try:
        data = json.loads(cleaned)
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return None
        ignored = list(data.get("ignored_items") or [])
        consumable = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "").strip()
            if not name:
                continue
            if not item.get("is_consumable", True):
                ignored.append(name)
                continue
            cat = item.get("category", "").strip()
            if cat not in deps.valid_categories:
                cat = deps.default_category
            normalized = deps.normalize_item_quantity(
                name, item.get("quantity_text", "").strip(), allow_default_unit=True, alias_map=alias_map
            )
            entry = {
                "name": name,
                "category": cat,
                "was_corrected": bool(item.get("was_corrected", False)),
            }
            entry.update(normalized)
            consumable.append(entry)
        if not consumable and not ignored:
            return None
        return {"items": consumable, "ignored_items": ignored}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


# =========================
# PREVIEWS
# =========================
def _show_mark_preview(deps, chat_id, items, household_id, user_db_id):
    pending_mark_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    preview = deps.format_grouped_list(items, f"🛒 Буде позначено купленими: {len(items)}")
    deps.send_message(chat_id, preview + "\n\nЩо зробити з цими товарами?", reply_markup=deps.mark_preview_keyboard)


def _show_delete_preview(deps, chat_id, items, household_id, user_db_id):
    pending_delete_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    preview = deps.format_grouped_list(items, f"🗑️ Буде видалено зі списку покупок: {len(items)}")
    deps.send_message(chat_id, preview, reply_markup=deps.delete_preview_keyboard)


# =========================
# MENU HANDLERS
# =========================
def handle_open_shopping_menu(deps, chat_id, user_id, display_name):
    deps.waiting_for_ingredients.pop(chat_id, None)
    deps.active_list_context[chat_id] = "shopping"
    deps.clear_shopping_state(chat_id)
    deps.clear_inventory_state(chat_id)
    deps.saved_list_context[chat_id] = "shopping_saved"
    try:
        household_id, _ = deps.get_household_and_user(user_id, display_name)
        deps.save_list_context(chat_id, household_id, "shopping_saved")
        items = deps.get_active_shopping_items(household_id)
        deps.send_message(chat_id, deps.format_shopping_list(items), reply_markup=deps.shopping_keyboard)
    except Exception:
        deps.send_message(chat_id, deps.db_error_msg, reply_markup=deps.shopping_keyboard)


def handle_start_shopping_add(deps, chat_id):
    deps.active_list_context[chat_id] = "shopping"
    deps.clear_shopping_state(chat_id)
    shopping_mode[chat_id] = "adding"
    deps.send_message(chat_id, "Надішли один товар або список товарів. Можна кожен товар з нового рядка.")


def handle_show_shopping_list(deps, chat_id, user_id, display_name):
    deps.active_list_context[chat_id] = "shopping"
    deps.clear_shopping_state(chat_id)
    deps.saved_list_context[chat_id] = "shopping_saved"
    try:
        household_id, _ = deps.get_household_and_user(user_id, display_name)
        deps.save_list_context(chat_id, household_id, "shopping_saved")
        items = deps.get_active_shopping_items(household_id)
        deps.send_message(chat_id, deps.format_shopping_list(items))
    except Exception:
        deps.send_message(chat_id, deps.db_error_msg)


def handle_start_mark_bought(deps, chat_id, user_id, display_name):
    deps.active_list_context[chat_id] = "shopping"
    deps.clear_shopping_state(chat_id)
    try:
        household_id, _ = deps.get_household_and_user(user_id, display_name)
        items = deps.get_active_shopping_items(household_id)
        if not items:
            deps.send_message(chat_id, "Список покупок поки порожній.")
        else:
            deps.send_message(chat_id, deps.format_shopping_list(items) + "\n\nНапиши, що купив:")
            shopping_mode[chat_id] = "marking"
    except Exception:
        deps.send_message(chat_id, deps.db_error_msg)


def handle_start_delete(deps, chat_id, user_id, display_name):
    deps.active_list_context[chat_id] = "shopping"
    deps.clear_shopping_state(chat_id)
    try:
        household_id, _ = deps.get_household_and_user(user_id, display_name)
        items = deps.get_active_shopping_items(household_id)
        if not items:
            deps.send_message(chat_id, "Список покупок поки порожній.")
        else:
            deps.send_message(chat_id, deps.format_shopping_list(items) + "\n\nНапиши, що видалити:")
            shopping_mode[chat_id] = "deleting"
    except Exception:
        deps.send_message(chat_id, deps.db_error_msg)


# Context Intent Safety V1 — a "Купив X за Y zł"-style compound purchase
# phrasing is deliberately EXCLUDED from the money-vs-quantity gate below.
# Same verb list as household_router._BOUGHT_RE, duplicated on purpose (same
# reasoning as every other small pure helper already duplicated across this
# codebase — see this module's own docstring) rather than importing
# household_router.py here. This is the Global Household Router's own
# combined buy+expense domain; active shopping/inventory mode already has
# documented priority over it (see test_global_household_operations.py's
# "active_selection_mode_has_priority") and this fix must not change that —
# it only targets a plain "item name + price" statement with no purchase
# verb, never this compound shape.
_PURCHASE_VERB_RE = re.compile(r"купив|купила|купили|придбав|придбала", re.IGNORECASE)

# Context Intent Safety V1 — controlled refusal for an ambiguous "item
# quantity + price in one message" shape (e.g. "Молоко 1 л 4,99 zł"): V1
# deliberately doesn't add a new 3-way pending-state/keyboard for this (see
# docs/PROJECT_STATE.md) — just asks for the two separate, already-supported
# phrasings instead. No item, no expense, no DB write either way.
_MONEY_AND_QUANTITY_CLARIFY_MSG = (
    "Бачу в повідомленні і кількість товару, і суму в злотих — щоб не "
    "помилитися, напиши окремо:\n"
    "• товар з кількістю (напр. «Молоко 1 л»)\n"
    "• або витрату (напр. «Молоко 4,99 zł»)"
)


# =========================
# SHOPPING MODE TEXT DISPATCH
# =========================
def handle_shopping_mode_text(deps, chat_id, user_id, display_name, text):
    """Returns True if handled (webhook should stop and return "ok"), False
    if there was no active shopping_mode (webhook should fall through to the
    next router, e.g. inventory mode)."""
    mode = shopping_mode.pop(chat_id, None)

    if mode == "adding":
        # Context Intent Safety V1 — a money amount ("52,37 zł") can never
        # become an item quantity ("52,37 шт."), checked on the RAW text
        # before the Gemini shopping-item parser ever sees it (Gemini itself
        # is what produced the live bug: it stripped "zł" off "Тест чай
        # batch 52,37 zł" and handed back a bare "52,37" quantity_text,
        # which parse_structured_quantity then read as a 52,37-count item).
        # "adding" mode is already popped above either way, so a stronger-
        # intent hit here safely clears it without a second pending state.
        if quantities.looks_like_money_amount(text) and not _PURCHASE_VERB_RE.search(text):
            if quantities.looks_like_explicit_item_quantity(text):
                deps.send_message(chat_id, _MONEY_AND_QUANTITY_CLARIFY_MSG)
                return True
            # Pure expense, no item quantity — let it fall through to the
            # existing global expense route (message_dispatcher.py checks
            # that right after shopping/inventory mode); no shopping item,
            # no DB write happens here.
            return False
        try:
            household_id, user_db_id = deps.get_household_and_user(user_id, display_name)
            alias_map = deps.get_household_alias_map(household_id)
        except Exception:
            shopping_mode[chat_id] = "adding"
            deps.send_message(chat_id, deps.db_error_msg)
            return True
        result = parse_shopping_list_with_gemini(deps, text, alias_map=alias_map)
        if result is None:
            shopping_mode[chat_id] = "adding"
            deps.send_message(
                chat_id,
                "Не зміг точно розібрати список. Надішли товари ще раз, бажано кожен з нового рядка."
            )
            return True
        items = result["items"]
        if not items:
            shopping_mode[chat_id] = "adding"
            ignored = result["ignored_items"]
            msg = "Не знайшов їстівних товарів у списку. Надішли ще раз."
            if ignored:
                msg += "\n\nНе додано: " + ", ".join(ignored)
            deps.send_message(chat_id, msg)
            return True
        items = deps.auto_merge_in_place(items)
        try:
            pending_batch[chat_id] = {
                "items": items,
                "ignored_items": result["ignored_items"],
                "household_id": household_id,
                "user_db_id": user_db_id,
            }
            preview = deps.format_batch_preview(items, result["ignored_items"])
            deps.send_message(chat_id, preview, reply_markup=deps.add_preview_keyboard)
        except Exception:
            deps.send_message(chat_id, deps.db_error_msg)
        return True

    if mode == "editing_number":
        batch = pending_batch.get(chat_id)
        if not batch:
            return True
        try:
            num = int(text.strip())
            if num < 1 or num > len(batch["items"]):
                shopping_mode[chat_id] = "editing_number"
                deps.send_message(chat_id, f"Такого номера немає. Напиши число від 1 до {len(batch['items'])}:")
                return True
            batch["edit_index"] = num - 1
            shopping_mode[chat_id] = "editing_text"
            deps.send_message(chat_id, "Надішли нову назву або «назва — кількість»:")
        except ValueError:
            shopping_mode[chat_id] = "editing_number"
            deps.send_message(chat_id, "Напиши номер позиції (числом):")
        return True

    if mode == "editing_text":
        batch = pending_batch.get(chat_id)
        if not batch:
            return True
        idx = batch.pop("edit_index", None)
        if idx is None or idx >= len(batch["items"]):
            return True
        name, quantity_text = deps.parse_item_text(text)
        batch["items"][idx]["name"] = name
        batch["items"][idx]["was_corrected"] = False
        try:
            alias_map = deps.get_household_alias_map(batch["household_id"])
        except Exception:
            alias_map = {}
        normalized = deps.normalize_item_quantity(name, quantity_text or "", allow_default_unit=True, alias_map=alias_map)
        batch["items"][idx].update(normalized)
        preview = deps.format_batch_preview(batch["items"], batch.get("ignored_items"))
        deps.send_message(chat_id, preview, reply_markup=deps.add_preview_keyboard)
        return True

    if mode == "marking":
        try:
            household_id, user_db_id = deps.get_household_and_user(user_id, display_name)
            items = deps.get_active_shopping_items(household_id)
            if not items:
                deps.send_message(chat_id, "Список покупок поки порожній.")
                return True
            kind, payload = deps.ask_gemini_for_selection(text, items, "Список покупок", "позначити купленими")
            if kind == "ok":
                _show_mark_preview(deps, chat_id, payload, household_id, user_db_id)
            elif kind == "unresolved":
                deps.send_message(chat_id, deps.format_unresolved_fragments_message(payload))
                shopping_mode[chat_id] = "marking"
            else:
                deps.send_message(chat_id, deps.selection_error_msg)
                shopping_mode[chat_id] = "marking"
        except Exception:
            deps.send_message(chat_id, deps.db_error_msg)
        return True

    if mode == "deleting":
        try:
            household_id, user_db_id = deps.get_household_and_user(user_id, display_name)
            items = deps.get_active_shopping_items(household_id)
            if not items:
                deps.send_message(chat_id, "Список покупок поки порожній.")
                return True
            kind, payload = deps.ask_gemini_for_selection(text, items, "Список покупок", "видалити зі списку")
            if kind == "ok":
                _show_delete_preview(deps, chat_id, payload, household_id, user_db_id)
            elif kind == "unresolved":
                deps.send_message(chat_id, deps.format_unresolved_fragments_message(payload))
                shopping_mode[chat_id] = "deleting"
            else:
                deps.send_message(chat_id, deps.selection_error_msg)
                shopping_mode[chat_id] = "deleting"
        except Exception:
            deps.send_message(chat_id, deps.db_error_msg)
        return True

    return False


# =========================
# PENDING BATCH EDIT ROUTER
# =========================
def handle_pending_batch_edit_text(deps, chat_id, text):
    """Returns True if the message was consumed as a preview-edit/merge
    intent (webhook sets _preview_intercepted = True), False if intent was
    "none" (webhook falls through to general AI chat)."""
    batch = pending_batch[chat_id]
    try:
        router_result = deps.ask_gemini_preview_edit_router(text, batch["items"], "shopping_pending_add")
        intent = router_result["intent"]
        if intent == "edit_preview":
            valid_updates = deps.validate_preview_updates(router_result["updates"], batch["items"])
            if valid_updates:
                alias_map = deps.get_household_alias_map(batch["household_id"])
                batch["items"] = deps.apply_preview_updates(batch["items"], valid_updates, alias_map=alias_map)
                preview = deps.format_batch_preview(batch["items"], batch.get("ignored_items"))
                deps.send_message(chat_id, preview, reply_markup=deps.add_preview_keyboard)
            else:
                deps.send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
            return True
        elif intent == "merge_duplicates":
            merged = deps.auto_merge_in_place(batch["items"])
            if len(merged) < len(batch["items"]):
                batch["items"] = merged
                preview = deps.format_batch_preview(merged, batch.get("ignored_items"))
                deps.send_message(chat_id, preview, reply_markup=deps.add_preview_keyboard)
            else:
                deps.send_message(chat_id, "Не знайшов безпечних дублікатів для об'єднання.")
            return True
        # intent == "none": fall through to AI chat
        return False
    except Exception:
        deps.send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
        return True
