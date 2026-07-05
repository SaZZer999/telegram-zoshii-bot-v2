"""Shared pure helpers for shopping and inventory list merging,
preview edits, saved-list merges, and stale target preparation.

Single source of truth extracted from bot.py — bot.py re-exports every
name here as the SAME object (direct alias where there's no bot-specific
dependency, or a thin wrapper that injects one where there is), so
household_router.py's existing `_bot._auto_merge_in_place` indirection
(bot.py IS the `_bot` it was configured with), inventory.py's merge
callback parameter, and any existing `bot.<name>` test import all keep
working unchanged.

Deliberately named list_editing.py, not shopping.py: every function here
is used by BOTH the shopping and inventory domains (merge-duplicates in a
pending add batch, saved-list merge via Gemini, preview-edit validation,
stale snapshot targets for the manual-merge confirm flow) — there is no
shopping-only or inventory-only logic in this module.

Where a function needs something bot.py owns (product-name
canonicalization, the household-alias-aware quantity normalizer, the
shared quantity accessor, or the fixed category set/default), that
dependency is an explicit argument — this module never imports bot.py,
database.py, inventory.py, household_router.py, Flask, Telegram, Gemini,
or psycopg. Only the standard library and quantities.py (the shared
quantity/unit math) are imported.

_parse_qty/_MERGEABLE_UNITS_BOT are moved here verbatim, unchanged — a
separate, older, float-based local quantity parser used only by the
merge-quantity computations below. Deliberately NOT unified with
quantities.py in this refactor (that unification, if ever done, needs its
own behavior-contract tests first).
"""
from quantities import merge_quantity_values, format_quantity_display, parse_structured_quantity

_MERGEABLE_UNITS_BOT = {"л", "мл", "г", "кг", "шт."}


def _parse_qty(qty_text):
    if not qty_text:
        return None, None
    normalized = qty_text.strip().replace(",", ".")
    parts = normalized.split()
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0]), parts[1]
    except ValueError:
        return None, None


def names_can_merge(item_a, item_b, canonicalize_name, default_category):
    """Same product (canonical_name) and compatible category (equal, or
    either default). canonicalize_name/default_category are bot.py-owned
    (product-name synonym rules / the fixed default category), injected so
    this module never duplicates or imports them."""
    canon_a = item_a.get("canonical_name") or canonicalize_name(item_a["name"])
    canon_b = item_b.get("canonical_name") or canonicalize_name(item_b["name"])
    if canon_a != canon_b:
        return False
    cat_a = item_a.get("category") or default_category
    cat_b = item_b.get("category") or default_category
    return cat_a == cat_b or cat_a == default_category or cat_b == default_category


def _compute_merged_quantity(merge_items):
    """Compute safe merged quantity_text for a group.

    both empty → ""; one empty → use non-empty; same mergeable unit → sum;
    different units or unparseable → None (group blocked).
    """
    qtys = [item.get("quantity_text") or "" for item in merge_items]
    non_empty = [q.strip() for q in qtys if q.strip()]

    if not non_empty:
        return ""
    if len(non_empty) == 1:
        return non_empty[0]

    parsed = [_parse_qty(q) for q in non_empty]

    if any(val is None for val, unit in parsed):
        unique = set(non_empty)
        return non_empty[0] if len(unique) == 1 else None

    units = set(unit for val, unit in parsed)
    if len(units) != 1:
        return None

    unit = next(iter(units))
    if unit not in _MERGEABLE_UNITS_BOT:
        unique = set(non_empty)
        return non_empty[0] if len(unique) == 1 else None

    total = round(sum(val for val, unit in parsed), 1)
    if total == int(total):
        return f"{int(total)} {unit}"
    return str(total).replace(".", ",") + f" {unit}"


# =========================
# MERGE HELPERS
# =========================

def _auto_merge_in_place(items, effective_quantity, canonicalize_name, default_category):
    """Merge duplicate items within a pending list (pure Python, no Gemini).

    Same canonical_name (+ compatible category) with mergeable structured
    units (merge_quantity_values decides — same unit, or same mass/volume
    conversion group) are summed. Incompatible items are left separate — no
    guessed math. effective_quantity/canonicalize_name/default_category are
    bot.py-owned, injected (see module docstring).
    """
    result = []
    for item in items:
        target = None
        merged_qty = None
        for existing in result:
            if not names_can_merge(existing, item, canonicalize_name, default_category):
                continue
            val_a, unit_a, _ = effective_quantity(existing)
            val_b, unit_b, _ = effective_quantity(item)
            candidate = merge_quantity_values(val_a, unit_a, val_b, unit_b)
            if candidate is not None:
                target = existing
                merged_qty = candidate
                break
        if target is not None:
            value, unit = merged_qty
            target["quantity_value"] = value
            target["quantity_unit"] = unit
            target["quantity_text"] = format_quantity_display(value, unit)
            target["quantity_inferred"] = bool(target.get("quantity_inferred")) and bool(item.get("quantity_inferred"))
            if (target.get("category") or default_category) == default_category and item.get("category"):
                target["category"] = item["category"]
        else:
            result.append(dict(item))
    return result


def _apply_pending_merge(items, validated_groups):
    """Apply merge groups to a pending RAM list. Returns new filtered list."""
    items = list(items)
    for group in validated_groups:
        indices = group["item_indices"]
        main_idx = indices[0]
        if main_idx >= len(items) or items[main_idx] is None:
            continue
        items[main_idx] = dict(items[main_idx])
        items[main_idx]["name"] = group["merged_name"]
        items[main_idx]["quantity_text"] = group["merged_quantity_text"]
        items[main_idx]["category"] = group["merged_category"]
        for idx in indices[1:]:
            if idx < len(items):
                items[idx] = None
    return [it for it in items if it is not None]


def _validate_merge_groups(raw_groups, items_list, valid_categories, default_category, is_pending=False):
    """Validate Gemini merge suggestions against an ordered item list.

    raw_groups use sequential item_refs (#1, #2, ...).
    is_pending=True  → store item_indices (0-based list indices).
    is_pending=False → store item_ids (actual DB ids).
    valid_categories/default_category are bot.py-owned, injected.
    """
    validated = []
    used_refs = set()
    items_by_ref = {i + 1: items_list[i] for i in range(len(items_list))}
    for group in raw_groups:
        refs = group.get("item_refs")
        if not isinstance(refs, list) or len(refs) < 2:
            continue
        try:
            refs = [int(r) for r in refs]
        except (TypeError, ValueError):
            continue
        if any(r in used_refs for r in refs):
            continue
        merge_items = [items_by_ref.get(r) for r in refs]
        if any(m is None for m in merge_items):
            continue

        categories = set(item.get("category") or default_category for item in merge_items)
        non_default = categories - {default_category}
        if len(non_default) > 1:
            continue

        merged_category = (group.get("merged_category") or "").strip()
        if merged_category not in valid_categories:
            merged_category = next(iter(non_default), default_category)

        merged_name = (group.get("merged_name") or "").strip()
        if not merged_name:
            continue

        merged_qty = _compute_merged_quantity(merge_items)
        if merged_qty is None:
            continue

        used_refs.update(refs)
        entry = {
            "merged_name": merged_name,
            "merged_quantity_text": merged_qty,
            "merged_category": merged_category,
            "items": merge_items,
        }
        if is_pending:
            entry["item_indices"] = [r - 1 for r in refs]
        else:
            entry["item_ids"] = [item["id"] for item in merge_items]
        validated.append(entry)
    return validated


def _validate_preview_updates(updates, items, valid_categories):
    """Validate Gemini preview edit updates. Returns list of valid updates or
    None. valid_categories is bot.py-owned, injected."""
    if not isinstance(updates, list) or not updates:
        return None
    total = len(items)
    used_numbers = set()
    valid = []
    for upd in updates:
        if not isinstance(upd, dict):
            return None
        num = upd.get("item_number")
        if not isinstance(num, int) or num < 1 or num > total:
            return None
        if num in used_numbers:
            return None
        used_numbers.add(num)
        name = upd.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return None
        qty = upd.get("quantity_text")
        if qty is not None and (not isinstance(qty, str) or not qty.strip()):
            return None
        cat = upd.get("category")
        if cat is not None and cat not in valid_categories:
            return None
        valid.append({
            "item_number": num,
            "name": name,
            "quantity_text": qty,
            "category": cat,
        })
    return valid


def _apply_preview_updates(items, valid_updates, normalize_item_quantity, alias_map=None):
    """Apply validated updates to items list. Returns new list without
    mutating originals. normalize_item_quantity is bot.py-owned (household
    alias lookup + product-name synonym rules), injected."""
    result = [dict(item) for item in items]
    for upd in valid_updates:
        idx = upd["item_number"] - 1
        name_changed = upd.get("name") is not None
        qty_changed = upd.get("quantity_text") is not None
        if name_changed:
            result[idx]["name"] = str(upd["name"]).strip()
        if qty_changed:
            result[idx]["quantity_text"] = upd["quantity_text"].strip()
        if upd.get("category") is not None:
            result[idx]["category"] = upd["category"]
        if name_changed or qty_changed:
            normalized = normalize_item_quantity(result[idx]["name"], result[idx].get("quantity_text") or "", alias_map=alias_map)
            result[idx].update(normalized)
    return result


# =========================
# SAVED LIST EDIT HELPERS
# =========================

def _compute_saved_merged_quantity(group_items, effective_quantity):
    """Compute merged quantity for saved list merging.

    All empty → count as 1 шт. each (e.g. Хліб + Хліб → 2 шт.).
    empty + "N шт." → (N+1) шт. (empty treated as 1 шт.).
    Same parseable non-шт. unit → sum (no empty items allowed for liquid/weight units).
    Returns quantity string or None if incompatible. effective_quantity is
    bot.py-owned, injected.
    """
    qtys = [effective_quantity(item)[2] for item in group_items]
    non_empty = [q.strip() for q in qtys if q.strip()]
    empty_count = len(qtys) - len(non_empty)

    if not non_empty:
        return f"{len(group_items)} шт."

    parsed = []
    for q in non_empty:
        val, unit = _parse_qty(q)
        if val is None:
            return None
        parsed.append((val, unit))

    units = {u for _, u in parsed}
    if len(units) != 1:
        return None
    unit = next(iter(units))
    if unit not in _MERGEABLE_UNITS_BOT:
        return None

    if unit == "шт.":
        total = round(sum(v for v, _ in parsed) + empty_count, 1)
    else:
        if empty_count > 0:
            return None
        total = round(sum(v for v, _ in parsed), 1)

    if total == int(total):
        return f"{int(total)} {unit}"
    return str(total).replace(".", ",") + f" {unit}"


def _compute_saved_merge_groups(merge_groups_raw, items, canonicalize_name, effective_quantity, default_category):
    """Convert Gemini [[2, 4], [1, 3]] merge_groups into validated groups for DB merge.

    Validates: same normalized name, compatible categories, safe quantity
    merge. Returns list of groups ready for execute_merge_shopping/inventory
    and _format_merge_preview. canonicalize_name/effective_quantity/
    default_category are bot.py-owned, injected.
    """
    if not isinstance(merge_groups_raw, list) or not merge_groups_raw:
        return []
    total = len(items)
    items_by_num = {i + 1: items[i] for i in range(total)}
    used_numbers = set()
    validated = []
    for group_raw in merge_groups_raw:
        if not isinstance(group_raw, list) or len(group_raw) < 2:
            continue
        try:
            nums = [int(n) for n in group_raw]
        except (TypeError, ValueError):
            continue
        if any(n < 1 or n > total for n in nums):
            continue
        if any(n in used_numbers for n in nums):
            continue
        group_items = [items_by_num[n] for n in nums]
        canonical_names = {it.get("canonical_name") or canonicalize_name(it["name"]) for it in group_items}
        if len(canonical_names) > 1:
            continue
        categories = {it.get("category") or default_category for it in group_items}
        non_default = categories - {default_category}
        if len(non_default) > 1:
            continue
        merged_category = next(iter(non_default), default_category)
        merged_qty = _compute_saved_merged_quantity(group_items, effective_quantity)
        if merged_qty is None:
            continue
        merged_value, merged_unit = parse_structured_quantity(merged_qty)
        used_numbers.update(nums)
        validated.append({
            "item_ids": [it["id"] for it in group_items],
            "merged_name": group_items[0]["name"],
            "merged_quantity_text": merged_qty,
            "merged_category": merged_category,
            "canonical_name": next(iter(canonical_names)),
            "merged_quantity_value": merged_value,
            "merged_quantity_unit": merged_unit,
            "items": group_items,
        })
    return validated


def _format_merge_preview(validated_groups, effective_quantity):
    """effective_quantity is bot.py-owned, injected."""
    lines = [f"🧹 Буде об'єднано груп: {len(validated_groups)}", ""]
    for i, group in enumerate(validated_groups):
        parts = []
        for item in group["items"]:
            label = item["name"]
            item_qty = effective_quantity(item)[2]
            if item_qty:
                label += f" — {item_qty}"
            parts.append(label)
        result = group["merged_name"]
        if group["merged_quantity_text"]:
            result += f" — {group['merged_quantity_text']}"
        lines.append(f"{i + 1}. {' + '.join(parts)}")
        lines.append(f"   → {result}")
    return "\n".join(lines)


def _merge_snapshot_targets(validated_groups, canonicalize_name, default_category):
    """Build {item_id, quantity_value, quantity_unit, canonical_name, category}
    snapshot targets for every SOURCE item across a set of validated saved-list
    merge groups (each group's "items" list, as built by _compute_saved_merge_groups)
    — fed into database._verify_targets_in_tx's extra_fields check, the exact same
    guard every other confirm-flow already uses, just extended to also cover the
    two extra fields a merge's UPDATE actually changes. Blocks the manual merge
    (StaleSnapshotError) if any target item's quantity, unit, canonical_name, or
    category changed — or the item vanished — since the merge preview was built.
    canonicalize_name/default_category are bot.py-owned, injected.
    """
    targets = []
    for group in validated_groups:
        for it in group["items"]:
            targets.append({
                "item_id": it["id"],
                "quantity_value": it.get("quantity_value"),
                "quantity_unit": it.get("quantity_unit"),
                "canonical_name": it.get("canonical_name") or canonicalize_name(it["name"]),
                "category": it.get("category") or default_category,
            })
    return targets
