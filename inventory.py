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

from quantities import STRUCTURED_UNITS, format_quantity_display, merge_quantity_values

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
        lines = [f"У запасах уже є «{name} — {existing_items[0]['quantity_text']}».", ""]
    else:
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
    return False, remainder


def find_inventory_cleanup_candidates(inventory_items, canonical_name):
    """Every inventory row matching this canonical_name, regardless of
    category (a mis-categorized duplicate is still a duplicate) — sorted by
    id, same ordering convention as find_inventory_representation_matches."""
    matches = [item for item in inventory_items if item.get("canonical_name") == canonical_name]
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
