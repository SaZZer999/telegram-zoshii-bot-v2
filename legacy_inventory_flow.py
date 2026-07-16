"""Legacy Inventory Flow V1.

Owns the "menu-driven" inventory interaction (open inventory menu, add/
remove via the dedicated inventory submenu) that predates the Global
Household Router. This module owns its own pending state (inventory_mode,
pending_inventory_batch, pending_remove_batch) and the inventory Gemini
parser, but has NO dependency on bot.py, Flask, Telegram, psycopg or any
Gemini SDK — everything it needs from the outside world (sending messages,
DB reads, alias lookups, shared list-editing helpers, keyboards, prompts) is
passed in via an `InventoryFlowDeps` container built and owned by bot.py.

Deliberately NOT here (still bot.py-owned, shared across shopping/inventory/
expenses, or specific to the Global Household Router / compound flows):
pending_merge, pending_saved_edit, saved_list_context, pending_quick_purchase,
the shared saved-list router, every confirm/cancel button handler that spans
more than one flow, the compound inventory flow, pending_inventory_
consumption, pending_compound_inventory, pending_inventory_reconciliation(_
clarify), pending_inventory_quantity_clarification, pending_inventory_
representation_clarification.
"""
import json
import re
from dataclasses import dataclass
from typing import Callable

import quantities


# =========================
# STATE (module-owned)
# =========================
inventory_mode = {}           # chat_id -> "adding" | "removing"
pending_inventory_batch = {}  # chat_id -> {items, ignored_items, household_id, user_db_id, inventory_targets}
pending_remove_batch = {}     # chat_id -> {items, household_id, user_db_id}


@dataclass
class InventoryFlowDeps:
    """Injected callbacks/values — no import of bot.py, ever."""
    send_message: Callable
    call_gemini: Callable
    get_household_and_user: Callable
    get_inventory_items: Callable
    get_household_alias_map: Callable
    save_list_context: Callable
    normalize_item_quantity: Callable
    canonicalize_name: Callable
    parse_inventory_list_with_gemini: Callable
    resolve_inventory_representation: Callable
    format_representation_clarify_message: Callable
    format_representation_separate_warning: Callable
    format_representation_merge_quantity_fragment: Callable
    merge_quantity_values: Callable
    format_quantity_display: Callable
    ask_gemini_for_selection: Callable
    ask_gemini_preview_edit_router: Callable
    validate_preview_updates: Callable
    apply_preview_updates: Callable
    auto_merge_in_place: Callable
    format_grouped_list: Callable
    format_inventory_list: Callable
    format_inventory_preview: Callable
    format_unresolved_fragments_message: Callable
    resolve_numbered_inventory_delete_selection: Callable
    format_numbered_delete_mismatch_message: Callable
    clear_shopping_state: Callable
    clear_inventory_state: Callable
    active_list_context: dict
    saved_list_context: dict
    waiting_for_ingredients: dict
    inventory_keyboard: dict
    add_inventory_preview_keyboard: dict
    remove_preview_keyboard: dict
    inventory_parse_prompt: str
    default_category: str
    valid_categories: set
    inventory_error_msg: str
    selection_error_msg: str
    # Inventory Delete By Visible Number v1 (optional, defaults to a no-op
    # so every pre-existing InventoryFlowDeps(...) construction across the
    # test suite that predates this field keeps working unchanged) — lets
    # bot.py record "this chat just saw the numbered inventory list" so a
    # later bare number reference ("9") can be resolved as a delete-by-
    # number request instead of falling through to general AI-chat.
    mark_inventory_list_shown: Callable = None


# =========================
# PARSING
# =========================
# Narrow whitelist mirror of household_router.py's identical
# _LEAKED_QUANTITY_PREFIX_RE/_looks_like_leaked_quantity_phrase — duplicated
# on purpose (same reasoning as every other small pure helper already
# duplicated across this codebase) rather than reaching into a private name
# in another module. Detects a quantity/container word that leaked into the
# front of `name` instead of being separated into quantity_text — a sign
# Gemini failed to split the phrase safely, e.g. name="дві пачки сосисок".
_LEAKED_QUANTITY_PREFIX_RE = re.compile(
    r"^(пара|пару|два|дві|три|чотири|п['’]?ять|пачка|пачки|пачок|упаковка|упаковки|упаковок)\b",
    re.IGNORECASE,
)


def _looks_like_leaked_quantity_phrase(name):
    """True if `name` still starts with a quantity/container word that
    should have been separated into quantity_text instead. Never guessed at
    beyond this exact whitelist."""
    return bool(_LEAKED_QUANTITY_PREFIX_RE.match((name or "").strip()))


def parse_inventory_list_with_gemini(deps, text, alias_map=None):
    """Inventory-only mirror of legacy_shopping_flow.parse_shopping_list_with_
    gemini — same contract/return shape, but uses deps.inventory_parse_prompt
    (explicit rules for word-numbers/container quantities like "дві пачки"/
    "пачка"/"пару" staying verbatim in quantity_text, never converted to a
    digit or left inside name) and additionally refuses to turn a leaked
    quantity/container phrase in `name` into a broken canonical item — such
    an item is treated exactly like a non-consumable one (excluded from the
    returned items, listed under ignored_items) instead of creating a
    preview with a garbage name.
    """
    history = [{"role": "user", "content": text}]
    raw = deps.call_gemini(history, deps.inventory_parse_prompt, temperature=0.1)
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
            if _looks_like_leaked_quantity_phrase(name):
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


def _mark_inventory_list_shown(deps, chat_id, household_id):
    """Thin None-guard so every InventoryFlowDeps construction that
    predates this optional field (the whole existing test suite) keeps
    working unchanged — see InventoryFlowDeps.mark_inventory_list_shown's
    own docstring."""
    if deps.mark_inventory_list_shown is not None:
        deps.mark_inventory_list_shown(chat_id, household_id)


# =========================
# PREVIEWS
# =========================
def _show_remove_preview(deps, chat_id, items, household_id, user_db_id):
    pending_remove_batch[chat_id] = {
        "items": items,
        "household_id": household_id,
        "user_db_id": user_db_id,
    }
    preview = deps.format_grouped_list(items, f"🧊 Буде прибрано із запасів: {len(items)}")
    deps.send_message(chat_id, preview, reply_markup=deps.remove_preview_keyboard)


# =========================
# MENU HANDLERS
# =========================
def handle_open_inventory_menu(deps, chat_id, user_id, display_name):
    deps.waiting_for_ingredients.pop(chat_id, None)
    deps.active_list_context[chat_id] = "inventory"
    deps.clear_shopping_state(chat_id)
    deps.clear_inventory_state(chat_id)
    deps.saved_list_context[chat_id] = "inventory_saved"
    try:
        household_id, _ = deps.get_household_and_user(user_id, display_name)
        deps.save_list_context(chat_id, household_id, "inventory_saved")
        items = deps.get_inventory_items(household_id)
        _mark_inventory_list_shown(deps, chat_id, household_id)
        deps.send_message(chat_id, deps.format_inventory_list(items), reply_markup=deps.inventory_keyboard)
    except Exception:
        deps.send_message(chat_id, deps.inventory_error_msg, reply_markup=deps.inventory_keyboard)


def handle_start_inventory_add(deps, chat_id):
    deps.active_list_context[chat_id] = "inventory"
    deps.clear_shopping_state(chat_id)
    deps.clear_inventory_state(chat_id)
    inventory_mode[chat_id] = "adding"
    deps.send_message(chat_id, "Надішли один продукт або список продуктів. Можна кожен продукт з нового рядка.")


def handle_show_inventory_list(deps, chat_id, user_id, display_name):
    deps.active_list_context[chat_id] = "inventory"
    deps.clear_shopping_state(chat_id)
    deps.clear_inventory_state(chat_id)
    deps.saved_list_context[chat_id] = "inventory_saved"
    try:
        household_id, _ = deps.get_household_and_user(user_id, display_name)
        deps.save_list_context(chat_id, household_id, "inventory_saved")
        items = deps.get_inventory_items(household_id)
        _mark_inventory_list_shown(deps, chat_id, household_id)
        deps.send_message(chat_id, deps.format_inventory_list(items))
    except Exception:
        deps.send_message(chat_id, deps.inventory_error_msg)


def handle_start_inventory_remove(deps, chat_id, user_id, display_name):
    deps.active_list_context[chat_id] = "inventory"
    deps.clear_shopping_state(chat_id)
    deps.clear_inventory_state(chat_id)
    try:
        household_id, user_db_id = deps.get_household_and_user(user_id, display_name)
        items = deps.get_inventory_items(household_id)
        _mark_inventory_list_shown(deps, chat_id, household_id)
        if not items:
            deps.send_message(chat_id, "Запаси поки порожні.")
        else:
            deps.send_message(chat_id, deps.format_inventory_list(items) + "\n\nНапиши, що прибрати із запасів:")
            inventory_mode[chat_id] = "removing"
    except Exception:
        deps.send_message(chat_id, deps.inventory_error_msg)


# Context Intent Safety V1 — same purchase-verb exclusion as legacy_shopping_
# flow._PURCHASE_VERB_RE (see its own docstring for the full rationale: a
# "Купив X за Y zł" compound phrasing stays inside active mode, unchanged).
# "взял\w*"/"взяв" added by Quantity + Price Intent Clarification V1 — kept
# in sync with household_router._BOUGHT_RE's own identical addition.
_PURCHASE_VERB_RE = re.compile(r"купив|купила|купили|придбав|придбала|взял\w*|взяв", re.IGNORECASE)

# Context Intent Safety V1 — same controlled refusal as legacy_shopping_
# flow.py's own _MONEY_AND_QUANTITY_CLARIFY_MSG, duplicated on purpose (same
# reasoning as every other small pure text/helper already duplicated across
# this codebase — see this module's own docstring) rather than importing
# from a sibling flow module.
_MONEY_AND_QUANTITY_CLARIFY_MSG = (
    "Бачу в повідомленні і кількість товару, і суму в злотих — щоб не "
    "помилитися, напиши окремо:\n"
    "• товар з кількістю (напр. «Молоко 1 л»)\n"
    "• або витрату (напр. «Молоко 4,99 zł»)"
)


# =========================
# INVENTORY MODE TEXT DISPATCH
# =========================
def handle_inventory_mode_text(deps, chat_id, user_id, display_name, text):
    """Returns True if handled (webhook should stop and return "ok"), False
    if there was no active inventory_mode (webhook should fall through to
    the pending preview router)."""
    inv_mode = inventory_mode.pop(chat_id, None)

    if inv_mode == "adding":
        # Context Intent Safety V1 — see legacy_shopping_flow.handle_
        # shopping_mode_text's identical guard for the full rationale (same
        # live bug, same fix shape, mirrored here for inventory add mode).
        if quantities.looks_like_money_amount(text) and not _PURCHASE_VERB_RE.search(text):
            if quantities.looks_like_explicit_item_quantity(text):
                deps.send_message(chat_id, _MONEY_AND_QUANTITY_CLARIFY_MSG)
                return True
            return False
        try:
            household_id, user_db_id = deps.get_household_and_user(user_id, display_name)
            alias_map = deps.get_household_alias_map(household_id)
        except Exception:
            inventory_mode[chat_id] = "adding"
            deps.send_message(chat_id, deps.inventory_error_msg)
            return True
        result = deps.parse_inventory_list_with_gemini(text, alias_map=alias_map)
        if result is None:
            inventory_mode[chat_id] = "adding"
            deps.send_message(
                chat_id,
                "Не зміг точно розібрати список. Надішли продукти ще раз, бажано кожен з нового рядка."
            )
            return True
        items = result["items"]
        if not items:
            inventory_mode[chat_id] = "adding"
            ignored = result["ignored_items"]
            msg = "Не знайшов їстівних продуктів у списку. Надішли ще раз."
            if ignored:
                msg += "\n\nНе додано: " + ", ".join(ignored)
            deps.send_message(chat_id, msg)
            return True
        items = deps.auto_merge_in_place(items)
        try:
            existing_inventory = deps.get_inventory_items(household_id)
            inventory_targets = []
            representation_notes = []
            display_items = []
            for item in items:
                outcome, existing = deps.resolve_inventory_representation(
                    existing_inventory, item.get("canonical_name"), item.get("category"),
                    item.get("quantity_value"), item.get("quantity_unit"), item.get("quantity_inferred", False),
                )
                if outcome == "clarify":
                    inventory_mode[chat_id] = "adding"
                    deps.send_message(chat_id, deps.format_representation_clarify_message(item["name"], existing))
                    return True
                if outcome == "merge":
                    # `items` (the write path, stored in pending_inventory_batch below)
                    # keeps the raw incoming quantity untouched — the actual merge
                    # arithmetic happens fresh at confirm time in
                    # _merge_or_insert_inventory_in_tx. `display_items` is a
                    # preview-only copy so the honest "X + Y -> буде Z" line can
                    # replace the plain quantity without touching what gets written.
                    merged_value, merged_unit = deps.merge_quantity_values(
                        existing["quantity_value"], existing["quantity_unit"],
                        item["quantity_value"], item["quantity_unit"],
                    )
                    display_item = dict(item)
                    display_item["quantity_value"] = None
                    display_item["quantity_text"] = deps.format_representation_merge_quantity_fragment(
                        existing["quantity_text"], item["quantity_text"],
                        deps.format_quantity_display(merged_value, merged_unit),
                    )
                    display_items.append(display_item)
                    inventory_targets.append({
                        "item_id": existing["id"], "quantity_value": existing["quantity_value"],
                        "quantity_unit": existing["quantity_unit"],
                    })
                    continue
                if outcome == "separate":
                    representation_notes.append(deps.format_representation_separate_warning(
                        item["name"], existing["quantity_text"], item["quantity_text"],
                    ))
                display_items.append(item)

            pending_inventory_batch[chat_id] = {
                "items": items,
                "ignored_items": result["ignored_items"],
                "household_id": household_id,
                "user_db_id": user_db_id,
                "inventory_targets": inventory_targets,
            }
            preview = deps.format_inventory_preview(display_items, result["ignored_items"])
            if representation_notes:
                preview = "\n\n".join(representation_notes) + "\n\n" + preview
            deps.send_message(chat_id, preview, reply_markup=deps.add_inventory_preview_keyboard)
        except Exception:
            deps.send_message(chat_id, deps.inventory_error_msg)
        return True

    if inv_mode == "removing":
        try:
            household_id, user_db_id = deps.get_household_and_user(user_id, display_name)
            items = deps.get_inventory_items(household_id)
            if not items:
                deps.send_message(chat_id, "Запаси поки порожні.")
                return True
            # Explicit numbered references ("N. назва — кількість" / "N)
            # ...") never go through Gemini — resolved deterministically
            # against the SAME numbering the user was just shown, so a
            # semantically-similar item (e.g. "Сосиски — 2 шт." vs the
            # user's "сосисок — пару") can never be picked instead of the
            # exact position requested. Falls back to the existing
            # natural-language Gemini flow untouched when the text isn't
            # entirely composed of such numbered lines.
            numbered_kind, numbered_payload = deps.resolve_numbered_inventory_delete_selection(text, items)
            if numbered_kind == "ok":
                _show_remove_preview(deps, chat_id, numbered_payload, household_id, user_db_id)
                return True
            if numbered_kind == "mismatch":
                number, exists = numbered_payload
                deps.send_message(chat_id, deps.format_numbered_delete_mismatch_message(number, exists))
                inventory_mode[chat_id] = "removing"
                return True
            kind, payload = deps.ask_gemini_for_selection(text, items, "Список запасів", "прибрати із запасів")
            if kind == "ok":
                _show_remove_preview(deps, chat_id, payload, household_id, user_db_id)
            elif kind == "unresolved":
                deps.send_message(chat_id, deps.format_unresolved_fragments_message(payload))
                inventory_mode[chat_id] = "removing"
            else:
                deps.send_message(chat_id, deps.selection_error_msg)
                inventory_mode[chat_id] = "removing"
        except Exception:
            deps.send_message(chat_id, deps.inventory_error_msg)
        return True

    return False


# =========================
# PENDING INVENTORY BATCH EDIT ROUTER
# =========================
def handle_pending_inventory_batch_edit_text(deps, chat_id, text):
    """Returns True if the message was consumed as a preview-edit/merge
    intent (webhook sets _preview_intercepted = True), False if intent was
    "none" (webhook falls through to general AI chat)."""
    batch = pending_inventory_batch[chat_id]
    try:
        router_result = deps.ask_gemini_preview_edit_router(text, batch["items"], "inventory_pending_add")
        intent = router_result["intent"]
        if intent == "edit_preview":
            valid_updates = deps.validate_preview_updates(router_result["updates"], batch["items"])
            if valid_updates:
                alias_map = deps.get_household_alias_map(batch["household_id"])
                batch["items"] = deps.apply_preview_updates(batch["items"], valid_updates, alias_map=alias_map)
                preview = deps.format_inventory_preview(batch["items"], batch.get("ignored_items"))
                deps.send_message(chat_id, preview, reply_markup=deps.add_inventory_preview_keyboard)
            else:
                deps.send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
            return True
        elif intent == "merge_duplicates":
            merged = deps.auto_merge_in_place(batch["items"])
            if len(merged) < len(batch["items"]):
                batch["items"] = merged
                preview = deps.format_inventory_preview(merged, batch.get("ignored_items"))
                deps.send_message(chat_id, preview, reply_markup=deps.add_inventory_preview_keyboard)
            else:
                deps.send_message(chat_id, "Не знайшов безпечних дублікатів для об'єднання.")
            return True
        # intent == "none": fall through to AI chat
        return False
    except Exception:
        deps.send_message(chat_id, "Не зміг безпечно зрозуміти зміну. Спробуй написати інакше.")
        return True
