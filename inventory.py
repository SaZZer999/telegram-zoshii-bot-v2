"""Pure inventory-domain helpers: the Inventory Representation Guard v1
(does an incoming quantity merge into an existing row, need clarification,
or become a separate row?), inventory consumption math/validation, and the
numbered inventory-delete selection cluster.

Single source of truth extracted from bot.py — bot.py re-exports every name
here as the SAME object (`from inventory import ...`), so
`bot.resolve_inventory_representation is inventory.resolve_inventory_representation`
holds, and household_router.py's existing `_bot.resolve_inventory_representation`
indirection (bot.py IS the `_bot` it was configured with) keeps working
unchanged.

The numbered-delete cluster needs a couple of pieces bot.py owns
(_effective_quantity, CATEGORY_ORDER, DEFAULT_CATEGORY) — rather than
duplicating or importing them, bot.py's thin wrappers pass them in as
explicit arguments (effective_quantity/category_order/default_category),
keeping this module fully self-contained.

No Telegram, Flask, psycopg, database connection, or Gemini — this module
imports only the standard library and quantities.py (the shared quantity/
unit math). Deliberately excluded (stays in bot.py, out of this module's
scope): webhook routes, keyboards, send_message, pending_* dicts,
_continue_inventory_quantity_clarification, parse_inventory_list_with_gemini,
_validate_compound_operations/_format_compound_preview and the whole
reconciliation cluster (both need bot.py's alias/name normalization),
_effective_quantity itself, format_grouped_list, and every database call.
"""
import re
from decimal import Decimal

from quantities import (
    STRUCTURED_UNITS, format_quantity_display, merge_quantity_values,
    parse_structured_quantity, _WORD_NUMBER_QUANTITIES,
)

# Mirrors bot.py's/database.py's own DEFAULT_CATEGORY literal — a tiny,
# self-contained constant (not business logic), duplicated on purpose so
# this module never needs to import bot.py just for one category string.
DEFAULT_CATEGORY = "Інше їстівне"


# =========================
# INVENTORY REPRESENTATION GUARD v1
# =========================

def find_inventory_representation_matches(inventory_items, canonical_name, category):
    """All existing inventory_items rows (already-fetched snapshot for this
    household) a new item with this canonical_name/category would compete
    with for merge/insert — mirrors _merge_or_insert_inventory_in_tx's exact
    category-compatibility rule and ORDER BY id ASC candidate order. Returns
    a list (possibly empty), sorted by id."""
    matches = [
        item for item in inventory_items
        if item.get("canonical_name") == canonical_name
        and (item.get("category") == category or item.get("category") == DEFAULT_CATEGORY or category == DEFAULT_CATEGORY)
    ]
    matches.sort(key=lambda it: it["id"])
    return matches


def classify_inventory_representation(existing_value, existing_unit, incoming_value, incoming_unit, incoming_inferred):
    """Decide how an incoming quantity relates to ONE existing row's
    quantity/unit. Returns:
      "merge"    — merge_quantity_values actually succeeds against this row.
      "clarify"  — incoming is an inferred guess (quantity_inferred=True)
                   that can't merge here — the whole preview/operation must
                   be blocked and the user asked for an explicit quantity,
                   never silently guessed or written.
      "separate" — incoming is an explicit (non-inferred) quantity that
                   still can't merge — safe to add as its own row, but the
                   preview must say so explicitly, never silently.
    """
    if merge_quantity_values(existing_value, existing_unit, incoming_value, incoming_unit) is not None:
        return "merge"
    return "clarify" if incoming_inferred else "separate"


def resolve_inventory_representation(inventory_items, canonical_name, category,
                                      quantity_value, quantity_unit, quantity_inferred):
    """Full v1 guard decision for one incoming inventory item against ALL
    existing rows sharing canonical_name (not just the first, and not just
    the first one that happens to be mergeable). Returns (outcome, existing):
      ("none", None)             — no existing row at all; plain add.
      ("merge", existing_item)   — a single existing row (dict) to merge
          into (first candidate, in id order, merge_quantity_values actually
          succeeds against — same order the write path uses).
      ("clarify", existing_items) — a LIST of every existing row sharing
          canonical_name (not just the conflicting one), for the "which
          record did you mean" message. Blocks the whole preview/operation.
      ("separate", existing_item) — a single existing row (dict, first
          candidate by id); safe to add as its own row, existing_item is
          only used to build the warning message.

    Critical rule: an INFERRED incoming quantity (quantity_inferred=True)
    must never merge into one compatible row while silently ignoring an
    INCOMPATIBLE sibling row of the same canonical_name — e.g. existing
    "Молоко — 1 шт." and "Молоко — 6 л" both present, incoming inferred
    "1 шт.": even though it merges cleanly with the "1 шт." row, the "6 л"
    row makes it genuinely ambiguous which record the guess belongs to, so
    this is "clarify", not "merge". This check only applies when
    quantity_inferred is True — an EXPLICIT incoming quantity (e.g. "1 л")
    is unambiguous about the user's intent and may still merge with
    whichever single candidate is compatible, ignoring incompatible
    siblings exactly as before.
    """
    candidates = find_inventory_representation_matches(inventory_items, canonical_name, category)
    if not candidates:
        return "none", None

    outcomes = [
        classify_inventory_representation(
            existing.get("quantity_value"), existing.get("quantity_unit"),
            quantity_value, quantity_unit, quantity_inferred,
        )
        for existing in candidates
    ]

    if quantity_inferred:
        if any(outcome != "merge" for outcome in outcomes):
            return "clarify", candidates
        return "merge", candidates[0]

    for existing, outcome in zip(candidates, outcomes):
        if outcome == "merge":
            return "merge", existing
    return "separate", candidates[0]


# =========================
# INVENTORY REPRESENTATION CLARIFICATION V2 — count ("шт.") vs an explicit
# mass/volume quantity for the SAME product. Deliberately narrower than the
# guard above: only this exact shape ever triggers it (never mass<->volume,
# never a text quantity — those never carry a quantity_unit at all — and
# never an inferred incoming guess, which stays Inventory Quantity
# Clarification v1's job), and only when there is exactly one existing
# count row to reason about — several candidate rows is a genuinely
# ambiguous "complex case" this version deliberately leaves to the existing
# guard's own "separate"/"clarify" handling instead.
# =========================
_MASS_UNITS = {"г", "кг"}
_VOLUME_UNITS = {"л", "мл"}
_MASS_OR_VOLUME_UNITS = _MASS_UNITS | _VOLUME_UNITS


def detect_count_vs_mass_volume_conflict(existing_value, existing_unit, incoming_value, incoming_unit, incoming_inferred):
    """True iff `existing` (a structured count row) and `incoming` (an
    explicit mass/volume quantity) are exactly the Inventory Representation
    Clarification V2 shape."""
    if incoming_inferred:
        return False
    if existing_unit != "шт." or existing_value is None:
        return False
    if incoming_value is None or incoming_unit not in _MASS_OR_VOLUME_UNITS:
        return False
    return True


def find_legacy_normalized_matches(inventory_items, canonical_name, category, name_normalizer):
    """Fallback for a legacy row whose STORED canonical_name/name predates
    today's built-in synonym normalization (e.g. canonical_name="ser" for
    what the same trusted synonym rules would now canonicalize to "сир") —
    only ever consulted by the caller when the plain exact-canonical_name
    match (find_inventory_representation_matches) already found nothing, so
    this never changes behavior for any row whose canonical_name is already
    up to date. Re-normalizes each candidate row's OWN canonical_name
    (falling back to its raw name) through `name_normalizer` — the SAME
    trusted built-in synonym rules the incoming item's canonical_name
    already went through — and matches against that. Never fuzzy matching,
    never stemming, never AI, never mutates the row: purely a smarter READ.
    Same category-compatibility rule as find_inventory_representation_matches.
    """
    matches = []
    for item in inventory_items:
        item_category = item.get("category")
        if not (item_category == category or item_category == DEFAULT_CATEGORY or category == DEFAULT_CATEGORY):
            continue
        legacy_identity = name_normalizer(item.get("canonical_name") or "")
        if legacy_identity != canonical_name:
            legacy_identity = name_normalizer(item.get("name") or "")
        if legacy_identity == canonical_name:
            matches.append(item)
    matches.sort(key=lambda it: it["id"])
    return matches


def detect_add_representation_v2_conflict(inventory_items, canonical_name, category,
                                           incoming_value, incoming_unit, incoming_inferred,
                                           name_normalizer=None):
    """Add-side (Flow B) conflict detection: returns the single existing
    "шт." row this incoming item conflicts with, or None. Only ever returns
    a row when there is EXACTLY ONE existing candidate sharing this
    canonical_name/category — several candidates falls through to the
    existing guard's own handling, untouched, per the "complex incompatible
    rows" carve-out above. If the plain exact match finds nothing and
    `name_normalizer` is given, also tries find_legacy_normalized_matches —
    still only ever acting on exactly one match; two or more legacy
    candidates are left alone (never guessed at) exactly like two or more
    exact candidates already are."""
    candidates = find_inventory_representation_matches(inventory_items, canonical_name, category)
    if not candidates and name_normalizer is not None:
        candidates = find_legacy_normalized_matches(inventory_items, canonical_name, category, name_normalizer)
    if len(candidates) != 1:
        return None
    existing = candidates[0]
    if not detect_count_vs_mass_volume_conflict(
        existing.get("quantity_value"), existing.get("quantity_unit"),
        incoming_value, incoming_unit, incoming_inferred,
    ):
        return None
    return existing


def format_representation_clarify_message(name, existing_items):
    """The blocking clarification message for the "clarify" outcome — no
    preview is built and nothing is written; the user must restate an
    explicit quantity/unit or container count. existing_items is the full
    list of every existing row sharing this canonical_name the guard found
    (never just the one that conflicts) — with 2+ rows, ALL of them are
    listed so the user understands there's genuine ambiguity, not just a
    single mismatch."""
    if len(existing_items) == 1:
        existing_display = existing_items[0]["quantity_text"]
        return (
            f"У запасах уже є «{name} — {existing_display}».\n\n"
            f"Скільки {name} ти купив?\n"
            f"Напиши явну кількість і одиницю (наприклад «1 л») або кількість тари (наприклад «дві пачки»)."
        )
    lines = [f"У запасах уже є кілька записів «{name}»:", ""]
    for item in existing_items:
        lines.append(f"• {item['quantity_text']}")
    lines.append("")
    lines.append("Не хочу вгадувати, до якого запису додати нову покупку.")
    lines.append(f"Напиши точну кількість, наприклад: «Купив 1 л {name.lower()}».")
    return "\n".join(lines)


def format_global_quantity_clarification_message(name, existing_items):
    """Clarification message for the Global Household Router's Inventory
    Quantity Clarification v1 continuation flow (pending_inventory_
    quantity_clarification) — deliberately separate wording from
    format_representation_clarify_message (which stays used, unchanged, by
    the normal/legacy "➕ Додати продукти" add flow): since the next reply
    here continues the ORIGINAL command instead of restating it in full,
    the example shows a bare quantity, not "Купив 1 л X"."""
    if len(existing_items) == 1:
        # Exactly one existing row — there's no ambiguity about WHICH record
        # to add to, only about how much, so this must never say "до якого
        # запису" (that phrasing is reserved for the 2+ rows branch below).
        lines = [f"У запасах уже є «{name} — {existing_items[0]['quantity_text']}».", ""]
        lines.append("Скільки додати?")
        lines.append("Напиши, наприклад: «1 л» або «500 мл».")
        return "\n".join(lines)
    lines = [f"У запасах уже є кілька записів «{name}»:", ""]
    for item in existing_items:
        lines.append(f"• {item['quantity_text']}")
    lines.append("")
    lines.append("Не хочу вгадувати, до якого запису додати нову покупку.")
    lines.append("Напиши точну кількість, наприклад: «1 л» або «500 мл».")
    return "\n".join(lines)


def format_representation_separate_warning(name, existing_display, incoming_display):
    """The non-blocking warning paragraph for the "separate" outcome —
    incoming item is still added, just never silently merged."""
    existing_text = (existing_display or "").rstrip(".")
    incoming_text = (incoming_display or "").rstrip(".")
    return (
        f"⚠️ {name} вже є у запасах: {existing_text}.\n"
        f"Нове надходження: {incoming_text}.\n"
        f"Його буде збережено окремою позицією, без об'єднання."
    )


def format_representation_merge_line(name, existing_display, incoming_display, merged_display):
    """The honest "X + Y -> буде Z" line for the "merge" outcome, replacing
    the plain "Додати ..." line for that one item — bullet-list style, used
    by household_router.py's own preview formatter."""
    return f"• {name} — {existing_display} + {incoming_display} → буде {merged_display}"


def format_representation_merge_quantity_fragment(existing_display, incoming_display, merged_display):
    """Same "X + Y -> буде Z" honesty as format_representation_merge_line,
    but just the quantity fragment (no name/bullet) — composes with
    format_grouped_list's own "{n}. {name} — {qty}" numbered-list template,
    used by the normal/legacy inventory add flow's preview."""
    return f"{existing_display} + {incoming_display} → буде {merged_display}"


# =========================
# INVENTORY CONSUMPTION
# =========================

_UNIT_GROUP = {"л": "volume", "мл": "volume", "кг": "mass", "г": "mass", "шт.": "count"}
_UNIT_TO_CANONICAL_FACTOR = {
    "л": Decimal("1"), "мл": Decimal("0.001"),
    "кг": Decimal("1000"), "г": Decimal("1"),
    "шт.": Decimal("1"),
}
_CANONICAL_UNIT_FOR_GROUP = {"volume": "л", "mass": "г", "count": "шт."}


def _resolve_consumption(current_value, current_unit, consume_value, consume_unit):
    """Compute the remaining quantity after consuming part of an inventory item.

    Uses Decimal throughout (never float) for the subtraction/conversion. The
    remainder is always expressed in the group's canonical display unit (л for
    volume, г for mass, шт. for count), not necessarily current_unit — e.g.
    1 кг - 200 г is shown as 800 г, not 0,8 кг.

    Returns ("ok", remaining_decimal, remaining_unit), ("incompatible_units", None, None)
    if the two units aren't from the same group, or ("insufficient", None, None) if
    consume_value exceeds what's available.
    """
    current_group = _UNIT_GROUP.get(current_unit)
    consume_group = _UNIT_GROUP.get(consume_unit)
    if current_group is None or consume_group is None or current_group != consume_group:
        return "incompatible_units", None, None
    current_canonical = Decimal(str(current_value)) * _UNIT_TO_CANONICAL_FACTOR[current_unit]
    consume_canonical = Decimal(str(consume_value)) * _UNIT_TO_CANONICAL_FACTOR[consume_unit]
    if consume_canonical > current_canonical:
        return "insufficient", None, None
    remaining = current_canonical - consume_canonical
    return "ok", remaining, _CANONICAL_UNIT_FOR_GROUP[current_group]


def _validate_consumptions(consumptions, items):
    """Validate Gemini consume_inventory_quantity output against current inventory items.

    Returns one of:
      ("ok", [resolved...]) — each resolved dict has item_number, item_id, name,
          old_value, old_unit, old_display, new_value, new_unit, new_display,
          will_remove (True when the remainder is exactly zero).
      ("missing_quantity", item_name) — item has no structured quantity to subtract from.
      ("insufficient", (item_name, available_display, requested_display)) — not enough left.
      ("invalid", None) — malformed input, out-of-range/duplicate item_number, bad unit,
          non-positive quantity, or incompatible units.

    Callers must check for unresolved_fragments (see _check_unresolved_fragments)
    before calling this — this function is unchanged from before that check existed.
    """
    if not isinstance(consumptions, list) or not consumptions:
        return "invalid", None
    total = len(items)
    used_numbers = set()
    resolved = []
    for entry in consumptions:
        if not isinstance(entry, dict):
            return "invalid", None
        num = entry.get("item_number")
        if not isinstance(num, int) or num < 1 or num > total:
            return "invalid", None
        if num in used_numbers:
            return "invalid", None
        used_numbers.add(num)
        value = entry.get("quantity_value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return "invalid", None
        unit = entry.get("quantity_unit")
        if unit not in STRUCTURED_UNITS:
            return "invalid", None
        item = items[num - 1]
        cur_value = item.get("quantity_value")
        cur_unit = item.get("quantity_unit")
        if cur_value is None or cur_unit is None:
            return "missing_quantity", item["name"]
        kind, remaining, remaining_unit = _resolve_consumption(cur_value, cur_unit, value, unit)
        if kind == "incompatible_units":
            return "invalid", None
        if kind == "insufficient":
            available_display = format_quantity_display(cur_value, cur_unit)
            requested_display = format_quantity_display(value, unit)
            return "insufficient", (item["name"], available_display, requested_display)
        will_remove = remaining == 0
        new_value = None if will_remove else float(remaining)
        new_unit = None if will_remove else remaining_unit
        resolved.append({
            "item_number": num,
            "item_id": item["id"],
            "name": item["name"],
            "old_value": cur_value,
            "old_unit": cur_unit,
            "old_display": format_quantity_display(cur_value, cur_unit),
            "new_value": new_value,
            "new_unit": new_unit,
            "new_display": None if will_remove else format_quantity_display(new_value, new_unit),
            "will_remove": will_remove,
        })
    return "ok", resolved


def _format_consumption_preview(resolved):
    lines = [f"🧊 Буде використано: {len(resolved)}", ""]
    for r in resolved:
        lines.append(f"{r['item_number']}. {r['name']} — {r['old_display']}")
        if r["will_remove"]:
            lines.append("   → буде прибрано із запасів")
        else:
            lines.append(f"   → {r['name']} — {r['new_display']}")
        lines.append("")
    return "\n".join(lines).rstrip()


# =========================
# STALE SNAPSHOT CHECK
# =========================

def _compound_snapshot_is_stale(inventory_changes, current_items):
    """True if any inventory_changes item no longer exists, or its quantity_value/unit
    changed since the compound preview was built (detects edits from another device)."""
    current_by_id = {it["id"]: it for it in current_items}
    for c in inventory_changes:
        cur = current_by_id.get(c["item_id"])
        if cur is None or cur.get("quantity_value") != c["old_value"] or cur.get("quantity_unit") != c["old_unit"]:
            return True
    return False


# =========================
# COMPOUND INVENTORY PLANNING
#
# Validates/formats a "remove some items, consume part of others, add the
# rest to shopping" plan in one pass. normalize_item_quantity/
# auto_merge_in_place/effective_quantity are bot.py-owned (name/alias
# resolution and a shopping+inventory-shared quantity accessor) — injected
# as explicit callback arguments so this module never duplicates or
# imports them. valid_categories/default_category are likewise bot.py's
# own category data, passed in the same way.
# =========================

_COMPOUND_OP_TYPES = {"remove_inventory", "consume_inventory_quantity", "add_to_shopping"}


def validate_compound_operations(
    operations, unresolved_fragments, items,
    normalize_item_quantity, auto_merge_in_place,
    valid_categories, default_category,
    alias_map=None,
):
    """Validate a compound_inventory_operations router result against current inventory items.

    Returns one of:
      ("unresolved", [fragment_str, ...]) — the router flagged part of the message as
          unclear; nothing should be applied.
      ("invalid", [reason_str, ...]) — one or more operations are malformed, conflicting,
          or unsafe; nothing should be applied (no partial preview, no partial apply).
      ("ok", {"inventory_changes": [...], "add_to_shopping": [...]}) — inventory_changes
          preserves the order operations were given in (remove_inventory and
          consume_inventory_quantity entries interleaved as given), each with
          item_number, item_id, name, old_value, old_unit, old_display, new_value,
          new_unit, new_display, will_remove, op_type ("remove"|"consume").
          add_to_shopping is a list of normalized+merged item dicts ready for
          add_shopping_items_batch-style insertion.
    """
    if unresolved_fragments:
        if not isinstance(unresolved_fragments, list):
            return "unresolved", ["(не вдалося розібрати частину повідомлення)"]
        fragments = [str(f).strip() for f in unresolved_fragments if str(f).strip()]
        return "unresolved", fragments or ["(не вдалося розібрати частину повідомлення)"]

    if not isinstance(operations, list) or not operations:
        return "invalid", ["Не знайшов жодної дії для виконання."]

    total = len(items)
    reasons = []
    used_item_numbers = set()
    inventory_changes = []
    shopping_raw = []

    for op in operations:
        if not isinstance(op, dict) or op.get("type") not in _COMPOUND_OP_TYPES:
            reasons.append("Незрозуміла дія.")
            continue
        op_type = op["type"]

        if op_type in ("remove_inventory", "consume_inventory_quantity"):
            num = op.get("item_number")
            if not isinstance(num, int) or num < 1 or num > total:
                reasons.append("Невідома позиція запасів.")
                continue
            item = items[num - 1]
            if num in used_item_numbers:
                reasons.append(f"«{item['name']}» — позиція задіяна в кількох операціях одночасно.")
                continue

            if op_type == "remove_inventory":
                used_item_numbers.add(num)
                inventory_changes.append({
                    "item_number": num, "item_id": item["id"], "name": item["name"],
                    "old_value": item.get("quantity_value"), "old_unit": item.get("quantity_unit"),
                    "old_display": format_quantity_display(item.get("quantity_value"), item.get("quantity_unit")),
                    "new_value": None, "new_unit": None, "new_display": None,
                    "will_remove": True, "op_type": "remove",
                })
                continue

            # consume_inventory_quantity
            value = op.get("quantity_value")
            unit = op.get("quantity_unit")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                reasons.append(f"«{item['name']}» — не можу безпечно визначити кількість для списання. Уточни, будь ласка.")
                continue
            if unit not in STRUCTURED_UNITS:
                reasons.append(f"«{item['name']}» — невідома одиниця вимірювання.")
                continue
            cur_value = item.get("quantity_value")
            cur_unit = item.get("quantity_unit")
            if cur_value is None or cur_unit is None:
                reasons.append(f"«{item['name']}» — не вказана точна кількість, не можна безпечно списати частину.")
                continue
            kind, remaining, remaining_unit = _resolve_consumption(cur_value, cur_unit, value, unit)
            if kind == "incompatible_units":
                reasons.append(f"«{item['name']}» — несумісні одиниці для списання.")
                continue
            if kind == "insufficient":
                available_display = format_quantity_display(cur_value, cur_unit)
                requested_display = format_quantity_display(value, unit)
                reasons.append(f"«{item['name']}» — у запасах лише {available_display}, а вказано {requested_display}.")
                continue
            used_item_numbers.add(num)
            will_remove = remaining == 0
            new_value = None if will_remove else float(remaining)
            new_unit = None if will_remove else remaining_unit
            inventory_changes.append({
                "item_number": num, "item_id": item["id"], "name": item["name"],
                "old_value": cur_value, "old_unit": cur_unit,
                "old_display": format_quantity_display(cur_value, cur_unit),
                "new_value": new_value, "new_unit": new_unit,
                "new_display": None if will_remove else format_quantity_display(new_value, new_unit),
                "will_remove": will_remove, "op_type": "consume",
            })
            continue

        # add_to_shopping
        name = op.get("name")
        if not isinstance(name, str) or not name.strip():
            reasons.append("Товар для покупок без назви.")
            continue
        name = name.strip()
        if not op.get("is_consumable", True):
            reasons.append(f"«{name}» — не їстівний товар, не можу додати до покупок.")
            continue
        cat = op.get("category")
        if not isinstance(cat, str) or cat not in valid_categories:
            cat = default_category
        qty_value = op.get("quantity_value")
        qty_unit = op.get("quantity_unit")
        if (
            not isinstance(qty_value, (int, float)) or isinstance(qty_value, bool)
            or qty_value <= 0
            or not isinstance(qty_unit, str) or qty_unit not in STRUCTURED_UNITS
        ):
            qty_value, qty_unit = None, None
        normalized = normalize_item_quantity(
            name, "", quantity_value=qty_value, quantity_unit=qty_unit, allow_default_unit=(qty_value is None),
            alias_map=alias_map,
        )
        shopping_item = {"name": name, "category": cat, "was_corrected": False}
        shopping_item.update(normalized)
        shopping_raw.append(shopping_item)

    if reasons:
        return "invalid", reasons
    if not inventory_changes and not shopping_raw:
        return "invalid", ["Не знайшов жодної безпечної дії."]

    add_to_shopping = auto_merge_in_place(shopping_raw) if shopping_raw else []
    return "ok", {"inventory_changes": inventory_changes, "add_to_shopping": add_to_shopping}


def format_compound_preview(resolved, effective_quantity):
    changes = resolved["inventory_changes"]
    shopping = resolved["add_to_shopping"]
    lines = ["🧊 Буде змінено в запасах:", ""]
    for i, c in enumerate(changes, start=1):
        label = c["name"]
        if c["old_display"]:
            label += f" — {c['old_display']}"
        lines.append(f"{i}. {label}")
        if c["will_remove"]:
            lines.append("   → буде прибрано із запасів")
        else:
            new_label = c["name"]
            if c["new_display"]:
                new_label += f" — {c['new_display']}"
            lines.append(f"   → {new_label}")
        lines.append("")
    if shopping:
        lines.append("🛒 Буде додано до покупок:")
        lines.append("")
        for item in shopping:
            _, _, qty_display = effective_quantity(item)
            label = item["name"]
            if qty_display:
                label += f" — {qty_display}"
            lines.append(f"• {label}")
    return "\n".join(lines).rstrip()


# =========================
# NUMBERED INVENTORY DELETE SELECTION
# =========================

def _numbered_inventory_display_items(items, category_order, default_category):
    """Rebuilds the exact same 1-based numbering format_grouped_list shows
    the user for an inventory snapshot — category_order traversal (grouped
    by category, in category_order's fixed sequence), never the raw
    get_inventory_items() SQL order ("category, name ASC" — plain
    alphabetical, which does NOT match category_order). This is the single
    source of truth for what "number N" means to the user on screen, reused
    by the deterministic numbered-reference delete-selection path so it can
    never number items differently than what's actually displayed.
    Returns an ordered list of (number, item) tuples.
    """
    numbered = []
    counter = 1
    for cat in category_order:
        for item in items:
            if (item.get("category") or default_category) != cat:
                continue
            numbered.append((counter, item))
            counter += 1
    return numbered


def _render_inventory_item_label(item, effective_quantity):
    """The exact per-item label text format_grouped_list renders after the
    number (name + optional "(виправлено)" + optional " — quantity"),
    without the leading "N. " — used to verify a user-typed description
    against the actual current label at that position. `effective_quantity`
    is bot.py's shared (value, unit, display_text) accessor, injected so
    this module never needs to duplicate or import it."""
    label = item["name"]
    if item.get("was_corrected"):
        label += " (виправлено)"
    _, _, qty_display = effective_quantity(item)
    if qty_display:
        label += f" — {qty_display}"
    return label


def _normalize_delete_match_text(s):
    """Lower/trim/collapse whitespace and strip punctuation — used only for
    exact-equality comparison of a numbered delete reference's description
    against the actual rendered label, never substring/fuzzy matching."""
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_NUMBERED_DELETE_LINE_RE = re.compile(r"^\s*(\d+)[.)]\s*(.+?)\s*$")


def _parse_numbered_delete_lines(text):
    """Detect an EXPLICIT numbered-reference delete request — every
    non-blank line of `text` must match "N. description" or "N) description".
    Returns an ordered list of (number, description) tuples, or None if the
    text isn't entirely composed of such lines — the caller then falls back
    to the existing natural-language/Gemini selection path unchanged."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    parsed = []
    for line in lines:
        m = _NUMBERED_DELETE_LINE_RE.match(line)
        if not m:
            return None
        parsed.append((int(m.group(1)), m.group(2)))
    return parsed


def _format_numbered_delete_mismatch_message(number, exists):
    """Blocks the whole batch — never guess which item a stale/mismatched
    numbered reference meant."""
    if not exists:
        reason = f"Номер {number} не існує в поточному списку запасів."
    else:
        reason = f"Номер {number} зараз відповідає іншій позиції у запасах."
    return (
        "Не можу безпечно підтвердити вибір.\n\n"
        f"{reason}\n"
        "Покажи список запасів ще раз і вибери актуальний номер."
    )


def _resolve_numbered_inventory_delete_selection(text, items, effective_quantity, category_order, default_category):
    """Deterministic, non-Gemini resolution for an explicit numbered
    inventory-delete request (every line shaped "N. description" / "N)
    description"). Never fuzzy-matched, never stemmed/lemmatized, never
    passed to Gemini — an exact match (after safe normalization) against the
    CURRENT rendered label at that exact display position, or the whole
    batch is blocked.

    effective_quantity/category_order/default_category are injected from
    bot.py (see module docstring) — this function itself stays pure.

    Returns one of:
      (None, None) — text isn't a purely-numbered request; caller should
          fall back to the existing natural-language/Gemini selection path.
      ("mismatch", (number_or_None, exists_bool)) — blocks the whole batch:
          a referenced number doesn't exist in the current snapshot, or its
          description doesn't match the item currently at that position.
      ("ok", [item, ...]) — every referenced number exists and matches;
          deduplicated (a repeated number is never selected twice), order
          preserved as first referenced.
    """
    parsed = _parse_numbered_delete_lines(text)
    if parsed is None:
        return None, None

    numbered = _numbered_inventory_display_items(items, category_order, default_category)
    by_number = {n: item for n, item in numbered}

    selected = []
    seen_ids = set()
    for number, description in parsed:
        item = by_number.get(number)
        if item is None:
            return "mismatch", (number, False)
        rendered = _render_inventory_item_label(item, effective_quantity)
        if _normalize_delete_match_text(description) != _normalize_delete_match_text(rendered):
            return "mismatch", (number, True)
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            selected.append(item)

    if not selected:
        return "mismatch", (None, False)
    return "ok", selected


# =========================
# INVENTORY CLEANUP / MERGE v1
# =========================
# Pure text-classification + candidate-grouping for "об'єднай молоко в
# запасах" style duplicate cleanup requests. No Gemini, no DB, no bot.py
# dependency — bot.py resolves the product phrase into a canonical_name
# (household alias lookup + canonicalize_name, exactly like every other
# add/merge flow), fetches the live inventory snapshot, and calls the two
# functions below; the actual DB write reuses database.execute_merge_
# inventory (same targets/StaleSnapshotError contract as the existing
# saved-list "merge_duplicates" flow) — no new write path.

# Bare "прибери" alone is already the general inventory-removal verb
# (➖ Використати / прибрати) — this trigger only fires paired with
# "дублікат(и)" so a plain "прибери молоко" (consume/remove) is never
# misrouted into a duplicate search.
_CLEANUP_TRIGGER_RE = re.compile(
    r"об.?[єе]дна\w*|прибери\s+(?:ці\s+|цей\s+)?дублікат\w*",
    re.IGNORECASE,
)
_CLEANUP_LEADING_DUPLICATE_WORD_RE = re.compile(r"^дублікат\w*\s*", re.IGNORECASE)
_CLEANUP_TRAILING_LOCATION_RE = re.compile(r"\s*(?:в|у|із|з)\s+запас\w*\.?$", re.IGNORECASE)

# Referential follow-ups with no explicit product name — "об'єднай их"/
# "об'єднай ці записи" after the bot already showed duplicate candidates.
# Deliberately a tiny fixed set (never fuzzy-matched) covering both the
# correct Ukrainian "їх" and the common "их" typo/Russianism.
_CLEANUP_FOLLOWUP_PHRASES = {
    "их", "їх", "це", "ці", "ці записи", "цей запис",
    "ці дублікати", "цей дублікат", "дублікати",
}

# Inventory Cleanup Merge vs Transform Guard v1 — a message whose remainder
# (the text parse_inventory_cleanup_request is about to treat as ONE
# product-name search) itself carries an Inventory Transform V1 shape —
# multiple DIFFERENT source items collapsed into a NEW named record, not a
# duplicate-merge of ONE product — must never be silently claimed here.
# _CLEANUP_TRIGGER_RE matches the bare word "об'єднай" anywhere in the
# message (unanchored, unlike parse_inventory_transform_request's own
# `^об'єднай ... в/у/на ...` grammar), so a transform-shaped phrase whose
# trigger verb isn't at the very start of the message (e.g. "В запасах
# об'єднай X і Y і запиши як Z") or whose target clause isn't the exact
# "в/у/на" preposition parse_inventory_transform_request requires (e.g.
# "...і запиши як Z") would otherwise still match THIS trigger and swallow
# the whole tail as a single, never-found product name. This guard only
# ever says "this isn't a single-product cleanup search, let it fall
# through" — it never itself parses a transform plan (that stays
# parse_inventory_transform_request's job for the phrasing it already
# understands, and the Inventory Action Planner V1's job for everything
# else, see action_planner.py). Deliberately a small, explicit signal list
# (arrow/plus notation, an explicit "call the result X" target clause, or a
# generic "combine into one position" phrase) — never a broad NLP guess, and
# never triggered by a bare preposition ("в"/"у"/"на") alone, so an ordinary
# product name that happens to contain one (e.g. "Об'єднай дублікати молока
# в запасах", already location-stripped above) is never affected.
_CLEANUP_TRANSFORM_SHAPE_ARROW_RE = re.compile(r"→|->")
_CLEANUP_TRANSFORM_SHAPE_PLUS_RE = re.compile(r"\S\s*\+\s*\S")
_CLEANUP_TRANSFORM_SHAPE_TARGET_CLAUSE_RE = re.compile(
    r"запиши(?:те)?\s+як|назви(?:те)?\s+як|назви(?:те)?\s+це|зроби\s+з\b|"
    r"(?:в|у)\s+одну\s+позиц\w*|перетвор\w*\s+.+\s+(?:в|у|на)\s+\S",
    re.IGNORECASE,
)


def _looks_like_transform_shape(remainder):
    """True if `remainder` actually describes an Inventory Transform V1
    request (see this module's own comment above) instead of a single-
    product duplicate-merge search. Never itself a transform parser — the
    caller's only job when this returns True is to stop claiming the
    message, never to build a transform plan here."""
    if not remainder:
        return False
    return bool(
        _CLEANUP_TRANSFORM_SHAPE_ARROW_RE.search(remainder)
        or _CLEANUP_TRANSFORM_SHAPE_PLUS_RE.search(remainder)
        or _CLEANUP_TRANSFORM_SHAPE_TARGET_CLAUSE_RE.search(remainder)
    )


def parse_inventory_cleanup_request(text):
    """Classify free text as an Inventory Cleanup / Merge v1 request.

    Returns (None, None) if `text` isn't a cleanup phrase at all — caller
    should try other routes. Returns (True, None) for a referential follow-
    up ("об'єднай их", "об'єднай ці записи", "прибери ці дублікати") — the
    caller must resolve it against a previously shown duplicate-search
    context (never guess a product name). Returns (False, product_phrase)
    for a direct request naming a product ("об'єднай молоко в запасах",
    "прибери дублікати молока") — product_phrase is the raw, not yet
    canonicalized/alias-resolved, name fragment the caller should resolve.

    Deliberately narrow/deterministic, V1 scope: rename ("перейменуй ser на
    сир"), an explicit two-name merge ("об'єднай ser і сир"), and quoted/
    explicit-quantity removal ("прибери «сосисок — пару»") don't match this
    gate at all and fall through to whatever route already handles that
    text today — documented V1 limitation, not a silent misfire.

    Also returns (None, None) — same as "not a cleanup phrase at all" — when
    the remainder itself looks like an Inventory Transform V1 request
    instead of a single-product duplicate search (see
    _looks_like_transform_shape's own comment above): the caller must let
    the message fall through to inventory_transform_route / the Inventory
    Action Planner V1, never claim it here as a (doomed to fail) product-name
    search.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    lowered = stripped.lower()

    trigger = _CLEANUP_TRIGGER_RE.search(lowered)
    if not trigger:
        return None, None

    remainder = lowered[trigger.end():].strip()
    remainder = _CLEANUP_LEADING_DUPLICATE_WORD_RE.sub("", remainder).strip()
    remainder = _CLEANUP_TRAILING_LOCATION_RE.sub("", remainder).strip()
    remainder = remainder.rstrip(".!?").strip()

    if not remainder or remainder in _CLEANUP_FOLLOWUP_PHRASES:
        return True, None
    if _looks_like_transform_shape(remainder):
        return None, None
    return False, remainder


# =========================
# INVENTORY CLEANUP / MERGE v1.1 — cleanup-specific alias layer
# =========================
# Deliberately NOT the global canonicalize_name/_NAME_SYNONYMS table
# (bot.py/database.py) — this only widens what the CLEANUP search accepts
# as "the same product" (a small fixed set of Ukrainian case/number
# endings the global canonicalizer has no morphology for), never changes
# what a new shopping/inventory item resolves to. No morphology engine: a
# tiny, explicit lookup table, same spirit as quantities.py's
# _WORD_NUMBER_QUANTITIES whitelist. Mixed-script homoglyphs (e.g. "mlekо"
# with a Cyrillic "о") are already repaired by the global canonicalize_name
# itself (_repair_mixed_script_token) — listed here too anyway, as a second,
# independent line of defense in case a legacy row's stored canonical_name
# predates that fix.
_CLEANUP_NAME_ALIASES = {
    "mleko": "молоко",
    "mlekо": "молоко",  # note: trailing character is Cyrillic "о" (U+043E), not Latin "o"
    "молока": "молоко",
    "молоку": "молоко",
    "ser": "сир",
    "сиру": "сир",
    "сира": "сир",
    "сосисок": "сосиски",
    "сосиски": "сосиски",
    "сосисками": "сосиски",
    "курку": "курка",
    "курки": "курка",
}


def cleanup_canonical_name_candidates(canonicalize_name, product_phrase):
    """Every canonical_name worth treating as "this product" for one cleanup
    request, most-preferred first. If product_phrase (lowercased/trimmed)
    hits this module's small cleanup-only alias table (declined/plural
    Ukrainian forms canonicalize_name has no morphology for — "молока",
    "сосисок", ...), that normalized form comes FIRST (it's what a new
    merged row's canonical_name should actually store). canonicalize_name's
    own result (household-alias-free; bot.py-owned, injected) always
    follows, deduplicated. Callers search inventory with the FULL list via
    find_inventory_cleanup_candidates and use candidates[0] as the primary
    canonical_name for the merge write.
    """
    raw = (product_phrase or "").strip().lower()
    alias_hit = _CLEANUP_NAME_ALIASES.get(raw)
    canonical = canonicalize_name(product_phrase)
    ordered = []
    for candidate in (alias_hit, canonical):
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered


def find_inventory_cleanup_candidates(inventory_items, canonical_name_candidates, canonicalize_name):
    """Every inventory row recognized as one of `canonical_name_candidates`
    (see cleanup_canonical_name_candidates), regardless of category (a mis-
    categorized duplicate is still a duplicate) — sorted by id, same
    ordering convention as find_inventory_representation_matches.

    A row matches if EITHER its stored canonical_name, a fresh
    canonicalize_name(row['name']) re-derivation, or this module's own
    cleanup alias lookup on row['name'] lands in that set — the fresh
    re-derivation/alias fallback exists because a legacy row's STORED
    canonical_name column can predate a later normalization fix (e.g. mixed-
    script homoglyph repair) or use a declined/plural form ("сосисок") the
    global canonicalizer has no morphology for; cleanup search must still
    find it even when the stored column alone would miss it.
    canonicalize_name is bot.py-owned, injected (see module docstring).
    """
    candidate_set = set(canonical_name_candidates)
    matches = []
    for item in inventory_items:
        name = item.get("name") or ""
        row_names = {
            item.get("canonical_name"),
            canonicalize_name(name),
            _CLEANUP_NAME_ALIASES.get(name.strip().lower()),
        }
        row_names.discard(None)
        if row_names & candidate_set:
            matches.append(item)
    matches.sort(key=lambda it: it["id"])
    return matches


# Display-unit preference within a compatible family: prefer the "bigger"
# unit as the merge base (9 л + 500 мл -> 9,5 л, not 9500 мл) — mirrors
# quantities._UNIT_CONVERSION_GROUPS' own conversion factors (not re-
# exported by quantities.py, so duplicated here as a tiny weight table).
_CLEANUP_UNIT_DISPLAY_WEIGHT = {"шт.": 1, "г": 1, "кг": 1000, "мл": 1, "л": 1000}
_CLEANUP_UNIT_FAMILY = {"шт.": "count", "г": "mass", "кг": "mass", "мл": "volume", "л": "volume"}


def group_inventory_cleanup_candidates(candidate_rows):
    """Split same-product duplicate rows into safely-mergeable groups plus a
    leftover "incompatible" list needing an explicit user decision.

    Rows are bucketed by unit family (count/mass/volume; unparseable or
    unknown units get their own bucket) — quantities.merge_quantity_values
    guarantees any two rows within the same family (mass: г/кг; volume:
    мл/л; count: шт. only with itself) can be summed exactly, so a family
    bucket with 2+ rows always becomes ONE merge group. A family bucket with
    only 1 row has no partner to merge with and goes to "incompatible" —
    it's not itself wrong, there's just nothing safe to combine it with
    (e.g. "mlekо — 1 шт." next to litre/ml duplicates).

    Returns {"groups": [{"rows": [...], "merged_value", "merged_unit"}],
    "incompatible": [row, ...]}. "groups" rows are ordered base-first (the
    row whose unit becomes the merged display unit).
    """
    families = {}
    incompatible = []
    for row in candidate_rows:
        unit = row.get("quantity_unit")
        value = row.get("quantity_value")
        family = _CLEANUP_UNIT_FAMILY.get(unit) if value is not None else None
        if family is None:
            incompatible.append(row)
            continue
        families.setdefault(family, []).append(row)

    groups = []
    for rows in families.values():
        if len(rows) < 2:
            incompatible.extend(rows)
            continue
        ordered = sorted(rows, key=lambda r: (-_CLEANUP_UNIT_DISPLAY_WEIGHT.get(r["quantity_unit"], 0), r["id"]))
        base = ordered[0]
        running_value, running_unit = base["quantity_value"], base["quantity_unit"]
        merged_rows = [base]
        for row in ordered[1:]:
            candidate = merge_quantity_values(running_value, running_unit, row["quantity_value"], row["quantity_unit"])
            if candidate is None:
                # Defensive only — same-family rows are always compatible
                # per merge_quantity_values' own contract, so this never
                # actually triggers; kept so a leftover row is still safe
                # (never silently dropped) if that contract ever changes.
                incompatible.append(row)
                continue
            running_value, running_unit = candidate
            merged_rows.append(row)
        if len(merged_rows) < 2:
            incompatible.extend(merged_rows)
            continue
        groups.append({"rows": merged_rows, "merged_value": running_value, "merged_unit": running_unit})

    return {"groups": groups, "incompatible": incompatible}


_CLEANUP_FAMILY_LABELS = {"volume": "л/мл", "mass": "кг/г", "count": "шт."}


def describe_cleanup_incompatibility_reason(candidate_rows):
    """Short Ukrainian reason for why NONE of `candidate_rows` could be
    safely auto-merged — used only when group_inventory_cleanup_candidates
    returned no groups at all (every row is alone in its own family, or a
    mix of numeric/text quantities)."""
    has_numeric = any(r.get("quantity_value") is not None for r in candidate_rows)
    has_text = any(r.get("quantity_value") is None for r in candidate_rows)
    if has_numeric and has_text:
        return "одна кількість числова, інша текстова"
    families = {
        _CLEANUP_UNIT_FAMILY.get(r.get("quantity_unit"))
        for r in candidate_rows if r.get("quantity_value") is not None
    }
    families.discard(None)
    if len(families) > 1:
        return "несумісні одиниці виміру"
    return "недостатньо однакових записів для безпечного об'єднання"


def format_inventory_cleanup_preview(validated_groups, incompatible_rows, effective_quantity):
    """Render the Inventory Cleanup / Merge v1.1 preview. effective_quantity
    is bot.py-owned (prefers structured fields, falls back to raw
    quantity_text), injected same as every other formatter in this module.

    Two shapes:
    - validated_groups non-empty: "🧹 Можна безпечно об'єднати" section (one
      line + result per group) followed by an "⚠️ Не об'єдную автоматично"
      section listing every leftover row with a short per-row reason
      ("несумісна одиниця з <family>").
    - validated_groups empty (nothing safe to merge at all): a single
      read-only "🧹 Знайшов схожі записи" + reason block — caller must NOT
      open a pending-confirm state or show the merge keyboard for this shape
      (see bot.py's _start_inventory_cleanup).
    """
    lines = []
    if validated_groups:
        lines.append("🧹 Можна безпечно об'єднати:")
        merged_families = set()
        for group in validated_groups:
            parts = []
            for item in group["items"]:
                label = item["name"]
                qty = effective_quantity(item)[2]
                if qty:
                    label += f" — {qty}"
                parts.append(label)
            result = group["merged_name"]
            if group["merged_quantity_text"]:
                result += f" — {group['merged_quantity_text']}"
            lines.append(f"• {' + '.join(parts)}")
            lines.append(f"  → {result}")
            family = _CLEANUP_UNIT_FAMILY.get(group.get("merged_unit"))
            if family:
                merged_families.add(_CLEANUP_FAMILY_LABELS.get(family, family))

        if incompatible_rows:
            lines.append("")
            lines.append("⚠️ Не об'єдную автоматично:")
            other_label = "/".join(sorted(merged_families)) if merged_families else None
            for row in incompatible_rows:
                qty = effective_quantity(row)[2]
                label = row["name"] + (f" — {qty}" if qty else "")
                reason = f" — несумісна одиниця з {other_label}" if other_label else ""
                lines.append(f"• {label}{reason}")
        return "\n".join(lines)

    lines.append("🧹 Знайшов схожі записи:")
    lines.append("")
    lines.append("⚠️ Не можу безпечно об'єднати автоматично:")
    for row in incompatible_rows:
        qty = effective_quantity(row)[2]
        label = row["name"] + (f" — {qty}" if qty else "")
        lines.append(f"• {label}")
    lines.append("")
    lines.append(f"Причина: {describe_cleanup_incompatibility_reason(incompatible_rows)}.")
    lines.append("Можеш прибрати або виправити зайвий запис через існуючі дії з запасами.")
    return "\n".join(lines)


# =========================
# INVENTORY CLEANUP ADMIN V1 — deterministic rename/delete of ONE existing
# inventory row ("перейменуй ser на сир", "видали mlekо із запасів", "прибери
# сосисок — пару"). Pure text-classification/candidate-resolution/preview-
# formatting only, no Gemini, no DB — bot.py resolves the household's live
# inventory snapshot and calls the write via database.execute_inventory_
# rename/execute_inventory_delete (same StaleSnapshotError/journal-undo
# contract as execute_inventory_cleanup_merge — see that function's own
# docstring). Deliberately reuses cleanup_canonical_name_candidates/
# find_inventory_cleanup_candidates (Inventory Cleanup / Merge v1.1) for
# name matching — no second alias table.
# =========================
_RENAME_TRIGGER_RE = re.compile(
    r"^(?:перейменуй|виправ|зміни\s+назву|заміни)\s+(?P<old>.+?)\s+на\s+(?P<new>.+)$",
    re.IGNORECASE,
)
# "з"/"із" (Ukrainian) and "из" (Russian, common in mixed-language
# household speech — see parse_inventory_delete_request's own docstring)
# all mean the same "from" preposition here; "запас\w*" already matches
# both the Ukrainian "запасів"/"запасах" and Russian "запасов" endings on
# its own (\w matches any word character), so only the preposition itself
# needed the Russian spelling added.
_ADMIN_LOCATION_SUFFIX_RE = re.compile(r"\s*(?:із|из|з|в|у)\s+запас\w*\.?\s*$", re.IGNORECASE)


def parse_inventory_rename_request(text):
    """Deterministically detect a rename request ("перейменуй X на Y",
    "виправ X на Y", "зміни назву X на Y"), optionally followed by a
    trailing "в запасах"/"у запасах" location phrase on the NEW-name side
    (stripped, never treated as part of the new name).

    Returns (old_phrase, new_name) — both raw, not yet canonicalized/
    resolved — or (None, None) if `text` doesn't match this shape at all.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    match = _RENAME_TRIGGER_RE.match(stripped)
    if not match:
        return None, None
    old_phrase = match.group("old").strip()
    new_phrase = _ADMIN_LOCATION_SUFFIX_RE.sub("", match.group("new").strip()).strip()
    new_phrase = new_phrase.rstrip(".!?").strip()
    if not old_phrase or not new_phrase:
        return None, None
    return old_phrase, new_phrase


_DELETE_TRIGGER_RE = re.compile(
    r"^(?:видали|прибери)\s+(?:запис\s+)?(?P<rest>.+)$",
    re.IGNORECASE,
)
_ADMIN_DASH_RE = re.compile(r"\s*[—–]\s*|\s+-\s+")

# Bare "все"/"всі"/"усе"/"усі" (with optional "крім ..." exception clause)
# is the existing bulk "select/delete everything" pronoun several OTHER
# flows already own — the aliases submenu's own "Видали всі назви" bulk-
# delete (matched via active_list_context == "aliases", checked well before
# this route) and the shopping/inventory numbered-list "mark/remove all"
# selection mode. Neither names an actual product, so this route must never
# claim it — doing so would misfire as "Не знайшов такого запису в
# запасах." for a message some OTHER already-correct flow either already
# handled, or (outside any of those modes) intentionally leaves for general
# AI-chat/the safety-net guard, exactly as before this route existed.
_DELETE_BULK_PRONOUN_RE = re.compile(r"^(?:все|всі|усе|усі)(?:\s*,?\s*крім\s+.+)?$", re.IGNORECASE)
# A location phrase naming the SHOPPING list, not inventory — out of scope
# for this route (see module docstring: rename/delete here is inventory-
# only); never claimed, even though nothing else handles it yet either.
_SHOPPING_LOCATION_RE = re.compile(r"(?:зі?\s+списку\s+покупок|з\s+покупок|із\s+покупок)\s*\.?\s*$", re.IGNORECASE)

# Inventory Delete Quantity-Match v1 — a trailing EXPLANATORY clause a
# household member commonly tacks onto a delete request ("...,  воно вже
# не потрібно") must never survive into the matched product name/quantity
# hint; this tiny, deliberately narrow whitelist (never fuzzy/NLP) is
# stripped BEFORE any name/quantity splitting below, exactly like the
# location-suffix strip above. Matched at the very END of the phrase only
# (optional leading comma/whitespace, optional trailing punctuation), so a
# product that happens to legitimately be named with one of these words
# is never touched (there is no such product in this household's actual
# grocery vocabulary). A second, separate alternative strips an open-ended
# ", бо ..." causal clause ("...,  бо воно зіпсувалося") — the comma is
# REQUIRED for this one (unlike the fixed-phrase whitelist above, which
# tolerates a missing comma) specifically so a hypothetical product name
# containing the bare word "бо" without a preceding comma is never
# mistaken for one; deliberately still not a general NLP guess since it
# only fires on the literal conjunction "бо" right after a comma.
_EXPLANATORY_TAIL_RE = re.compile(
    r"\s*,?\s*(?:воно\s+вже\s+не\s+потрібно|це\s+вже\s+не\s+треба|більше\s+не\s+треба|закінчилось)\s*[.!?]*\s*$"
    r"|\s*,\s*бо\s+.+$",
    re.IGNORECASE,
)

# A leading spelled-out "one" determiner directly in front of the product
# name ("видали одне молоко" = "delete one [unit of] milk") — grammatical
# gender varies with the noun it modifies (одне/одна/один), so all three
# forms are recognized; always resolves to the same "1 шт." hint a numeric
# "1 шт" would. Checked BEFORE the trailing-quantity checks below since it
# sits at the OPPOSITE end of the phrase from every other quantity hint
# this function recognizes.
_LEADING_ONE_QUANTITY_RE = re.compile(r"^(?:одне|одна|один)\s+(?P<name>\S.*)$", re.IGNORECASE)

# A trailing spelled-out "one piece" phrase ("одна штука"/"одну штуку") —
# the WORD-form equivalent of a numeric "1 шт"/"1 штуку", which quantities.
# parse_structured_quantity already handles (see the generic 2-word
# structured-quantity check further down) but can't parse here since
# "одна"/"одну" isn't a digit. Deliberately narrow (exactly these two
# count-words + exactly these two piece-nouns) rather than a general
# word-number parser — never guesses at "дві штуки"/"три штуки" etc.,
# which stay unrecognized (candidate count alone must disambiguate, same
# as any other quantity phrase this function doesn't understand).
_ONE_PIECE_COUNT_WORDS = {"одна", "одну"}
_PIECE_NOUN_WORDS = {"штука", "штуку"}


def parse_inventory_delete_request(text):
    """Deterministically detect a delete request ("видали X із запасів",
    "прибери X", "прибери запис X", "видали X <text-quantity>", "прибери X
    — <text-quantity>"). The trailing location phrase is stripped via
    _ADMIN_LOCATION_SUFFIX_RE regardless of whether it's spelled the
    Ukrainian way ("із запасів"/"з запасів") or the Russian way ("из
    запасов") — real household speech mixes both freely (e.g. "Видали сир
    из запасов"), and a Russian preposition must never survive into the
    matched product name/end up blocking the match entirely. Deliberately
    excludes "прибери ... дублікат..."
    (Inventory Cleanup / Merge v1's own trigger — caller must try that gate
    FIRST, see bot.py's dispatch order) — this function has no "дублікат"
    special-case of its own, so a duplicate-cleanup phrase simply produces a
    (name, None) pair the caller never reaches (already claimed upstream).
    Also excludes a bare bulk "все"/"всі"/"усе"/"усі" pronoun (see
    _DELETE_BULK_PRONOUN_RE) and an explicit shopping-list location phrase
    (see _SHOPPING_LOCATION_RE) — neither is this route's job.

    Returns (name_phrase, quantity_hint) — quantity_hint is None when no
    explicit quantity was given (candidate count alone must disambiguate),
    or the exact text after an explicit "—"/"-" separator, or the LAST word
    when it's one of quantities._WORD_NUMBER_QUANTITIES's tiny whitelist
    ("пара"/"пару") — deliberately narrow so an ordinary multi-word product
    name (e.g. "кокосове молоко") is never mis-split into a fake quantity
    hint. A trailing NUMERIC quantity ("1 шт", "1 штуку", "1,5 л" — with or
    without a "—"/"-" separator) is also detected and re-rendered through
    _normalize_numeric_quantity_hint into the SAME canonical form an
    inventory row's own quantity_text is stored in (e.g. "1 шт."), so
    "прибери Молоко 1 шт"/"прибери Молоко — 1 шт"/"прибери молоко 1 штуку"
    all resolve to the exact stored "1 шт." — never blocked by a trailing-
    dot/unit-spelling mismatch.

    A trailing EXPLANATORY clause ("... воно вже не потрібно"/"це вже не
    треба"/"більше не треба"/"закінчилось", or an open-ended ", бо ..."
    causal clause, see _EXPLANATORY_TAIL_RE) is
    stripped before any of the above, so it never becomes part of the
    matched name. A spelled-out "one" count — leading ("видали одне
    молоко", see _LEADING_ONE_QUANTITY_RE) or trailing ("видали молоко
    одна штука", see _ONE_PIECE_COUNT_WORDS/_PIECE_NOUN_WORDS) — resolves
    to the same "1 шт." hint a numeric "1 шт" would.

    Returns (None, None) if `text` doesn't match this shape at all.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    match = _DELETE_TRIGGER_RE.match(stripped)
    if not match:
        return None, None
    raw_rest = match.group("rest").strip()
    if _SHOPPING_LOCATION_RE.search(raw_rest):
        return None, None
    rest = _ADMIN_LOCATION_SUFFIX_RE.sub("", raw_rest).strip()
    rest = _EXPLANATORY_TAIL_RE.sub("", rest).strip()
    rest = rest.rstrip(".!?").strip()
    if not rest or _DELETE_BULK_PRONOUN_RE.match(rest):
        return None, None

    leading_match = _LEADING_ONE_QUANTITY_RE.match(rest)
    if leading_match:
        name_part = leading_match.group("name").strip()
        if name_part:
            return name_part, "1 шт."

    dash_match = _ADMIN_DASH_RE.search(rest)
    if dash_match:
        name_part = rest[:dash_match.start()].strip()
        qty_part = rest[dash_match.end():].strip()
        if name_part and qty_part:
            return name_part, _normalize_numeric_quantity_hint(qty_part)

    words = rest.split()

    if (
        len(words) >= 3
        and words[-2].lower() in _ONE_PIECE_COUNT_WORDS
        and words[-1].lower() in _PIECE_NOUN_WORDS
    ):
        name_part = " ".join(words[:-2]).strip()
        if name_part:
            return name_part, "1 шт."

    if len(words) >= 2 and words[-1].lower() in _WORD_NUMBER_QUANTITIES:
        return " ".join(words[:-1]).strip(), words[-1]

    if len(words) >= 3:
        trailing_qty = " ".join(words[-2:])
        value, unit = parse_structured_quantity(trailing_qty)
        if value is not None and unit is not None:
            name_part = " ".join(words[:-2]).strip()
            if name_part:
                return name_part, format_quantity_display(value, unit)

    return rest, None


def _normalize_numeric_quantity_hint(text):
    """If `text` is a genuine NUMERIC quantity ("1 шт", "1,5 л", ...) that
    quantities.parse_structured_quantity can parse, re-render it through
    quantities.format_quantity_display — the SAME canonical form an
    inventory row's own quantity_text is stored in (e.g. "1 шт."), so a
    trailing-dot/unit-spelling mismatch never blocks an otherwise-correct
    match. A WORD-based quantity ("пара"/"пару") has no digit and is
    returned UNCHANGED — those rows store the literal word as their
    quantity_text (e.g. "пару"), so re-rendering through format_quantity_
    display (which would turn "пару" into "2 шт.") must never happen here."""
    if not text or not any(ch.isdigit() for ch in text):
        return text
    value, unit = parse_structured_quantity(text)
    if value is None:
        return text
    return format_quantity_display(value, unit)


# Standalone quantity-hint normalizer for a caller that already has a hint
# string in ISOLATION (not embedded in a full "видали X <qty>" phrase) — the
# Inventory Action Planner V1's own inventory_delete action extracts
# item_name/quantity_hint as two separate fields, so its quantity_hint must
# go through the SAME natural-language normalization parse_inventory_delete_
# request's own trailing-quantity parsing already applies to a phrase it
# parsed itself, so "Видали молоко одна штука" (deterministic parser) and a
# planner-routed item_name="молоко"/quantity_hint="одна штука" resolve to
# the exact same "1 шт." candidate-matching hint — see this module's live
# fix for the natural-quantity inventory-delete bug. Handles the spelled-out
# "one" shapes _normalize_numeric_quantity_hint alone can't (no digit to
# trigger on): the exact two-word "одна штука"/"одну штуку" phrase, and a
# bare "одне"/"одна"/"один" — then falls back to _normalize_numeric_
# quantity_hint for everything else (numeric hints re-rendered to canonical
# form, "пара"/"пару" returned unchanged, anything unparseable returned
# unchanged so the caller's own exact-match narrowing simply finds no
# candidate and falls back to the full list, per resolve_inventory_admin_
# candidates' own contract, rather than crashing).
_BARE_ONE_WORD_RE = re.compile(r"^(?:одне|одна|один)$", re.IGNORECASE)


def normalize_delete_quantity_hint(text):
    """Normalize a standalone delete quantity_hint string the same way
    parse_inventory_delete_request already normalizes a trailing quantity
    fragment parsed out of a full phrase. Returns None for missing/blank
    input (no hint at all — the caller's candidate search then relies on
    name alone, same as today)."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    words = stripped.split()
    if (
        len(words) == 2
        and words[0].lower() in _ONE_PIECE_COUNT_WORDS
        and words[1].lower() in _PIECE_NOUN_WORDS
    ):
        return "1 шт."
    if _BARE_ONE_WORD_RE.match(stripped):
        return "1 шт."
    return _normalize_numeric_quantity_hint(stripped)


# =========================
# INVENTORY DELETE BY VISIBLE NUMBER — "видали 9"/"видали номер 9"/"прибери
# №9" (an explicit reference to a row's position in the numbered "🧊
# Запаси" listing, instead of a product name) and the bare-number
# continuation ("9" alone, with no verb at all) that only applies after a
# recent inventory list view or a recent failed name-based delete attempt
# (bot.py owns that recency gate — see pending_inventory_number_context —
# this module stays pure text-parsing/candidate-resolution only, same
# "no Gemini, no DB, no bot.py" posture as the rest of Inventory Cleanup
# Admin v1 above).
# =========================
_DELETE_BY_NUMBER_RE = re.compile(
    r"^(?:видали|прибери)\s+(?:запис\s+)?(?:№\s*|номер\s+|#\s*)?(?P<number>\d+)\s*\.?\s*$",
    re.IGNORECASE,
)
_BARE_NUMBER_REFERENCE_RE = re.compile(r"^(?:№\s*|номер\s+|#\s*)?(?P<number>\d+)\s*\.?\s*$", re.IGNORECASE)


def parse_inventory_delete_by_number(text):
    """Deterministically detect an explicit delete-by-VISIBLE-NUMBER
    request — the same trigger verbs as parse_inventory_delete_request
    ("видали"/"прибери", optionally "запис"), naming a row's 1-based
    position in the last-shown numbered inventory listing ("видали 9",
    "видали номер 9", "прибери №9") instead of a product name. Returns the
    number (int, always >= 1) or None if `text` doesn't match this shape
    at all — in particular, None for anything with a non-numeric remainder
    ("видали молоко"), so the caller's existing name-based parser is never
    shadowed. Caller resolves the number against a FRESH numbered snapshot
    (see resolve_inventory_number_reference) — this function never touches
    inventory data itself."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    match = _DELETE_BY_NUMBER_RE.match(stripped)
    if not match:
        return None
    number = int(match.group("number"))
    return number if number >= 1 else None


def parse_bare_inventory_number_reference(text):
    """Detect a message that is JUST a row number ("9", "№9", "номер 9",
    "#9") with no delete verb at all. Deliberately narrow (the ENTIRE
    message, nothing else) — used ONLY as a continuation after a recent
    inventory list view or a recent failed inventory delete attempt (see
    bot.py's pending_inventory_number_context/_has_recent_inventory_
    number_context), never claimed on its own initiative, since a bare
    number could mean many other things in an unrelated context. Returns
    the number (int, always >= 1) or None."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    match = _BARE_NUMBER_REFERENCE_RE.match(stripped)
    if not match:
        return None
    number = int(match.group("number"))
    return number if number >= 1 else None


def resolve_inventory_number_reference(number, items, category_order, default_category):
    """Resolve a 1-based visible row number (as shown in "🧊 Запаси") against
    a FRESH inventory_items snapshot — reuses _numbered_inventory_display_
    items directly, so "number N" can never mean anything different here
    than what the user actually sees on screen. Returns the matching item
    dict, or None if the number doesn't exist in the CURRENT list (caller
    shows a controlled error via format_inventory_number_not_found_message,
    never guesses)."""
    numbered = _numbered_inventory_display_items(items, category_order, default_category)
    by_number = {n: item for n, item in numbered}
    return by_number.get(number)


def format_inventory_number_not_found_message(number):
    """Controlled error for an inventory delete-by-number reference to a
    row that doesn't exist in the CURRENT list — never silently guesses a
    different row, mirrors _format_numbered_delete_mismatch_message's own
    "show the list again" guidance."""
    return (
        f"Номер {number} не існує в поточному списку запасів.\n\n"
        "Покажи список запасів ще раз і вибери актуальний номер."
    )


# =========================
# INVENTORY TRANSFORM V1 — deterministic, lossy combine of TWO OR MORE
# existing inventory rows into ONE new named record ("об'єднай сосиски і
# мисливські ковбаски в м'ясні вироби", "перетвори молоко та вершки на
# молочну суміш"). Same "no Gemini, no fuzzy NLP" posture as rename/delete
# above — the trigger phrase and the source/target split are both pure regex,
# never guessed. Unlike Inventory Cleanup / Merge v1.1 (which only merges
# multiple rows that already share ONE canonical product), this deliberately
# collapses DIFFERENT products into a new general record — always shown with
# an explicit "lossy" warning before it can be confirmed (see bot.py's
# _start_inventory_transform), since the original per-product rows are gone
# afterwards. Pure text-classification only, no Gemini, no DB — bot.py
# resolves each source phrase against the live inventory snapshot (reusing
# resolve_inventory_admin_candidates, exactly like rename/delete) and calls
# the write via database.execute_inventory_transform (same StaleSnapshotError/
# journal-undo contract as execute_inventory_cleanup_merge).
# =========================
_TRANSFORM_TRIGGER_RE = re.compile(
    r"^(?:об['’]?єднай(?:те)?|об['’]?єднати|перетвори(?:ти)?)\s+(?P<sources>.+?)\s+(?:в|у|на)\s+(?P<target>.+)$",
    re.IGNORECASE,
)
_TRANSFORM_SOURCE_SPLIT_RE = re.compile(r"\s*,\s*|\s+(?:та|і|й)\s+", re.IGNORECASE)


def parse_inventory_transform_request(text):
    """Deterministically detect a transform/combine request ("об'єднай X і Y
    в Z", "об'єднати X, Y та Z у W", "перетвори X на Y"), splitting the
    source phrase list on commas and "та"/"і"/"й".

    Returns (source_phrases, target_phrase) — source_phrases is a list of 2+
    raw name fragments (never yet resolved against live inventory), target_
    phrase is the raw new record name — or (None, None) if `text` doesn't
    match this shape at all, or fewer than two source phrases were found
    (a single-source "transform" has no lossy-combine meaning here and is
    left for the caller's other routes, e.g. rename, to consider instead).
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    match = _TRANSFORM_TRIGGER_RE.match(stripped)
    if not match:
        return None, None
    target_phrase = match.group("target").strip().rstrip(".!?").strip()
    if not target_phrase:
        return None, None
    source_phrases = [
        p.strip() for p in _TRANSFORM_SOURCE_SPLIT_RE.split(match.group("sources").strip()) if p.strip()
    ]
    if len(source_phrases) < 2:
        return None, None
    return source_phrases, target_phrase


def format_inventory_transform_preview(source_rows, effective_quantity, target_name, target_quantity_text, header="План змін:"):
    """Render the Inventory Transform V1 preview — every source row removed,
    the new target record added, plus a fixed warning that different
    products are being collapsed into one general record (always shown here,
    never conditional, since Inventory Transform V1 only ever runs for a
    genuine multi-source combine). `header` defaults to the original preview
    header; Preview Edit V1 passes "Оновив план:" instead when re-rendering
    an edited preview, without changing anything else about the layout."""
    lines = [header, "", "🧊 Запаси"]
    for row in source_rows:
        qty = effective_quantity(row)[2]
        label = row["name"] + (f" — {qty}" if qty else "")
        lines.append(f"• Прибрати {label}")
    target_label = target_name + (f" — {target_quantity_text}" if target_quantity_text else "")
    lines.append(f"• Додати {target_label}")
    lines.append("")
    lines.append(
        f"⚠️ Це об'єднає різні продукти в один загальний запис. Після цього бот не буде знати, "
        f"що окремо були " + ", ".join(f"«{row['name']}»" for row in source_rows) + "."
    )
    return "\n".join(lines)


def find_inventory_admin_exact_name_matches(inventory_items, name_phrase, name_normalizer):
    """Every existing inventory row whose OWN visible name (item['name'] —
    NOT canonical_name) is exactly the same product as `name_phrase`, once
    both go through `name_normalizer` (bot.py-owned: lowercase/trim +
    Latin/Cyrillic homoglyph repair, deliberately WITHOUT the global
    synonym table canonicalize_name applies). This is the HIGHEST-priority
    match for Inventory Cleanup Admin rename/delete: "mlekо" must resolve to
    the row literally named "mlekо"/"Mleko"/"Mlekо" and NEVER to a
    DIFFERENT row (e.g. "Молоко") just because both happen to share a
    canonical_name — that collision is exactly what canonicalize_name's own
    synonym mapping would otherwise cause if used for this comparison.
    Sorted by id, same convention as every other candidate-search helper in
    this module."""
    target = name_normalizer(name_phrase)
    if not target:
        return []
    matches = [item for item in inventory_items if name_normalizer(item.get("name") or "") == target]
    matches.sort(key=lambda it: it["id"])
    return matches


def resolve_inventory_admin_candidates(inventory_items, canonical_name_candidates, canonicalize_name, quantity_hint=None,
                                        name_phrase=None, name_normalizer=None):
    """Every inventory row matching `canonical_name_candidates` (see
    find_inventory_cleanup_candidates), narrowed to an exact quantity_text
    match when `quantity_hint` is given AND that narrowing actually finds at
    least one row — e.g. "сосисок пару" narrows "Сосиски — 6 шт." + "сосисок
    — пару" down to just the second row. If the hint matches nothing (e.g. a
    stale/wrong guess), the FULL candidate list is returned instead of an
    empty one, so the caller's normal not-found/ambiguous handling still
    applies rather than silently losing a real match. Comparison is
    case/whitespace-insensitive against the row's stored quantity_text
    exactly as-is — never re-parsed/re-interpreted (a legacy row's raw text
    quantity, e.g. "пару", is exactly what this must match).

    When `name_phrase`/`name_normalizer` are BOTH given (bot.py's Inventory
    Cleanup Admin caller always provides them), an EXACT visible row-name
    match (find_inventory_admin_exact_name_matches) is tried FIRST and, if
    it finds anything at all, wins outright over the alias/canonical pool
    below — even a single alias/canonical match is never allowed to override
    an existing exact-name row, and quantity_hint still narrows the exact-
    match pool the same way it narrows the fallback pool. Only when NO
    exact visible-name match exists at all does this fall back to the
    alias/canonical cleanup search. Callers that omit name_phrase/
    name_normalizer (both default None) get the exact same behavior as
    before this priority existed."""
    if name_phrase is not None and name_normalizer is not None:
        exact_matches = find_inventory_admin_exact_name_matches(inventory_items, name_phrase, name_normalizer)
        if exact_matches:
            if quantity_hint:
                hint_norm = quantity_hint.strip().lower()
                narrowed = [c for c in exact_matches if (c.get("quantity_text") or "").strip().lower() == hint_norm]
                if narrowed:
                    return narrowed
            return exact_matches

    candidates = find_inventory_cleanup_candidates(inventory_items, canonical_name_candidates, canonicalize_name)
    if quantity_hint:
        hint_norm = quantity_hint.strip().lower()
        narrowed = [c for c in candidates if (c.get("quantity_text") or "").strip().lower() == hint_norm]
        if narrowed:
            return narrowed
    return candidates


def _normalize_quantity_fragment_for_disambiguation(text):
    """Lower/trim + strip a single trailing '.' — used only to compare a
    user-typed quantity fragment ("1 шт") against a row's stored
    quantity_text ("1 шт."), which almost always carries that trailing
    period. Never applied to name matching."""
    return (text or "").strip().rstrip(".").lower()


_ADMIN_DISAMBIGUATION_NUMBER_RE = re.compile(r"^№?\s*(\d+)\s*$")


def resolve_cleanup_admin_disambiguation_reply(text, candidates, name_normalizer):
    """Try to select exactly ONE row from a previously-shown Inventory
    Cleanup Admin ambiguous-candidates list, using a short follow-up reply.
    Deterministic, no Gemini, no fuzzy matching:

      - A bare "№N" or plain "N" selects the row at that 1-based POSITION
        in `candidates` — the exact same order they were numbered in by
        format_inventory_admin_ambiguous_message — only when
        1 <= N <= len(candidates).
      - Otherwise, an optional "—"/"-" dash splits the reply into a name
        fragment and a quantity fragment (e.g. "mlekо — 1 шт"); with no
        dash, this also tries to peel a trailing quantity fragment off the
        end of the text by checking whether it matches (after
        _normalize_quantity_fragment_for_disambiguation) any candidate's
        own quantity_text — e.g. "Mleko 1 шт" against a candidate whose
        quantity_text is "1 шт." peels into name "Mleko" + quantity "1 шт".
        If no candidate quantity_text matches as a suffix at all, the whole
        reply is treated as a bare name fragment.
      - Each detected fragment independently narrows the candidate pool: a
        name fragment (via `name_normalizer`, same Latin/Cyrillic-homoglyph
        tolerance as find_inventory_admin_exact_name_matches) against each
        candidate's own name, a quantity fragment against quantity_text. A
        fragment that matches NOTHING in the current pool is ignored rather
        than emptying the pool (so a wrong guess never wipes out an
        otherwise-correct narrowing from the other fragment).

    Returns the single selected row (dict) if exactly one candidate remains
    after every applicable narrowing step, else None (still ambiguous, or
    nothing matched at all — caller must ask again)."""
    stripped = (text or "").strip()
    if not stripped or not candidates:
        return None

    numbered = _ADMIN_DISAMBIGUATION_NUMBER_RE.match(stripped)
    if numbered:
        idx = int(numbered.group(1))
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]
        return None

    name_part, qty_part = None, None
    dash_match = _ADMIN_DASH_RE.search(stripped)
    if dash_match:
        name_part = stripped[:dash_match.start()].strip() or None
        qty_part = stripped[dash_match.end():].strip() or None
    else:
        lowered = stripped.lower()
        for cand in candidates:
            qty_norm = _normalize_quantity_fragment_for_disambiguation(cand.get("quantity_text"))
            if qty_norm and lowered.endswith(qty_norm):
                remainder = stripped[: len(stripped) - len(qty_norm)].strip()
                name_part = remainder or None
                qty_part = stripped[len(stripped) - len(qty_norm):].strip()
                break
        if qty_part is None:
            name_part = stripped

    pool = candidates
    if name_part:
        name_pool = [c for c in pool if name_normalizer(c.get("name") or "") == name_normalizer(name_part)]
        if name_pool:
            pool = name_pool
    if qty_part:
        qty_norm = _normalize_quantity_fragment_for_disambiguation(qty_part)
        qty_pool = [c for c in pool if _normalize_quantity_fragment_for_disambiguation(c.get("quantity_text")) == qty_norm]
        if qty_pool:
            pool = qty_pool

    if len(pool) == 1:
        return pool[0]
    return None


def capitalize_first(text):
    """Capitalize only the first character of `text`, leaving every other
    character as typed — matches this codebase's existing display-name
    convention (e.g. "Зелений чай", "Кокосове молоко": only the first word's
    first letter is capitalized, never a per-word title-case). Never touches
    an empty/blank string."""
    stripped = (text or "").strip()
    if not stripped:
        return stripped
    return stripped[0].upper() + stripped[1:]


def is_noop_rename(row, new_name, new_canonical_name, name_normalizer):
    """True if renaming `row` to new_name/new_canonical_name would be a
    meaningless no-op — the row's CURRENT visible name already normalizes to
    the same thing as new_name (case/Latin-Cyrillic-homoglyph-insensitive,
    via `name_normalizer`) AND its canonical_name already equals
    new_canonical_name. Blocks re-running "перейменуй ser на сир" after
    "ser" was already renamed to "Сир" — the alias/canonical fallback search
    (see resolve_inventory_admin_candidates) still finds that row (its
    canonical_name is "сир"), but renaming it to itself must never create a
    preview, a DB write, or an undo journal entry."""
    return (
        name_normalizer(row.get("name") or "") == name_normalizer(new_name)
        and (row.get("canonical_name") or "") == (new_canonical_name or "")
    )


def format_noop_rename_message(current_name):
    """Controlled reply for is_noop_rename's blocked case — never implies a
    DB write happened, never shows an empty "X -> X" preview."""
    return f"Цей запис уже називається «{current_name}». Змін не потрібно."


def format_inventory_rename_preview(old_name, quantity_text, new_name):
    """Render the Inventory Cleanup Admin V1 rename preview — one line,
    the row's own quantity shown on both sides unchanged (only the name
    changes)."""
    lines = ["План змін:", "", "🧊 Запаси"]
    suffix = f" — {quantity_text}" if quantity_text else ""
    lines.append(f"• {old_name}{suffix} → {new_name}{suffix}")
    return "\n".join(lines)


def format_inventory_delete_preview(name, quantity_text):
    """Render the Inventory Cleanup Admin V1 delete preview — one line."""
    lines = ["План змін:", "", "🧊 Запаси"]
    suffix = f" — {quantity_text}" if quantity_text else ""
    lines.append(f"• Прибрати {name}{suffix}")
    return "\n".join(lines)


def format_inventory_admin_ambiguous_message(candidates, effective_quantity):
    """Multiple rows matched the same rename/delete request — never guess;
    list every candidate (numbered, 1-based, in the SAME order the caller
    stores them in a pending disambiguation context — see
    resolve_cleanup_admin_disambiguation_reply's "№N"/bare-N selection) and
    ask for a more precise reference (e.g. with an explicit quantity or that
    number)."""
    lines = ["Знайшов кілька записів, не хочу вгадувати:", ""]
    for i, row in enumerate(candidates, start=1):
        qty = effective_quantity(row)[2]
        label = row["name"] + (f" — {qty}" if qty else "")
        lines.append(f"{i}. {label}")
    lines.append("")
    lines.append("Напиши точніше, який саме запис потрібен (наприклад, з кількістю або номером).")
    return "\n".join(lines)
