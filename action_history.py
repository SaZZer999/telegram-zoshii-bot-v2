"""Action History + Safe Undo v1 — pure logic only.

No Telegram, no PostgreSQL, no Gemini. database.py calls these helpers to
build/interpret the JSONB `summary` it stores on household_action_journal;
bot.py calls the text/message helpers to recognize the undo command and
render the fixed reply strings. Row dicts everywhere here have the shape
{"id", "household_id", "name", "canonical_name", "quantity_text",
"quantity_value" (string or None), "quantity_unit", "quantity_inferred",
"category"} — quantity_value is always a string (exact Decimal text), never
a float/Decimal object, so every comparison here is a plain value/string
comparison, never a numeric one.
"""

from datetime import date, datetime
from decimal import Decimal

UNDO_BUTTON_TEXT = "↩️ Скасувати останню дію"

_UNDO_COMMAND_PHRASES = {
    "скасувати останню дію",
    "повернути останню дію",
    "верни зміни назад",
}

NO_UNDOABLE_ACTION_MSG = (
    "Немає твоєї підтвердженої дії, яку зараз можна безпечно скасувати.\n\n"
    "Підтримуються лише global-дії, виконані після появи цієї функції."
)

PENDING_UNDO_MSG = (
    "У тебе є незавершене скасування дії.\n\n"
    "Підтвердь його або скасуй."
)

UNDO_STALE_MSG = (
    "Не можу безпечно скасувати цю дію, бо пов'язані дані змінилися після неї.\n\n"
    "Нічого не змінено."
)

UNDO_APPLIED_MSG = "✅ Останню дію скасовано."

UNDO_CANCELLED_MSG = "Скасування останньої дії скасовано."


def is_undo_command(text):
    """True for the undo button label or one of the three recognized
    natural-language phrasings (case/whitespace-insensitive). Never matches
    anything else — deliberately narrow, no Gemini involved."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if normalized == UNDO_BUTTON_TEXT.strip().lower():
        return True
    return normalized in _UNDO_COMMAND_PHRASES


def json_safe(value):
    """Recursively convert Decimal -> exact string and date/datetime -> ISO
    string so the result can be stored as JSONB as-is. Leaves every other
    JSON-native type (str/int/float/bool/None/dict/list) unchanged."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def row_signature(row):
    """Comparable tuple of every user-visible field of a shopping/inventory
    snapshot row — two rows are "the same" for undo purposes iff their
    signatures are equal."""
    return (
        row.get("id"),
        row.get("name"),
        row.get("canonical_name"),
        row.get("quantity_text"),
        row.get("quantity_value"),
        row.get("quantity_unit"),
        row.get("quantity_inferred"),
        row.get("category"),
    )


def buckets_match(current_rows, snapshot_rows):
    """True iff the two row lists represent the exact same set of rows
    (order-independent) — used to verify a canonical-name bucket hasn't
    changed since a snapshot was captured, before undo is allowed to touch it."""
    current = {row_signature(r) for r in (current_rows or [])}
    snapshot = {row_signature(r) for r in (snapshot_rows or [])}
    return current == snapshot


def diff_bucket(before_rows, after_rows):
    """Pure diff between a canonical-name bucket's before/after row lists,
    keyed by id (never by position — a merge target's id is stable, an
    inserted/removed row's id is the only reliable way to tell them apart).

    Returns a list of entries describing what undo will do to THIS bucket:
    - kind "remove": a row present after but not before -> undo deletes it
      (this bucket's forward action inserted it).
    - kind "restore": a row present before but not after -> undo reinserts it
      (this bucket's forward action deleted it, e.g. consume-to-zero).
    - kind "update": a row present in both with different values -> undo
      restores the before values (merge-add or partial consume).
    Unchanged rows produce no entry.
    """
    before_by_id = {r["id"]: r for r in (before_rows or [])}
    after_by_id = {r["id"]: r for r in (after_rows or [])}
    entries = []
    for row_id, arow in after_by_id.items():
        if row_id not in before_by_id:
            entries.append({
                "kind": "remove", "name": arow.get("name"),
                "quantity_text": arow.get("quantity_text"),
            })
        else:
            brow = before_by_id[row_id]
            if row_signature(arow) != row_signature(brow):
                entries.append({
                    "kind": "update", "name": arow.get("name"),
                    "current_text": arow.get("quantity_text"),
                    "target_text": brow.get("quantity_text"),
                })
    for row_id, brow in before_by_id.items():
        if row_id not in after_by_id:
            entries.append({
                "kind": "restore", "name": brow.get("name"),
                "quantity_text": brow.get("quantity_text"),
            })
    return entries


def build_operation_summary(before_snapshot, post_action_snapshot):
    """Build the compact, display-ready `summary` stored on the journal row
    from the raw before/post bucket snapshots — the only thing
    format_undo_preview needs to render the undo preview later, decoupled
    from the raw snapshot shape."""
    inventory_before = before_snapshot.get("inventory_buckets") or {}
    inventory_post = post_action_snapshot.get("inventory_buckets") or {}
    shopping_before = before_snapshot.get("shopping_buckets") or {}
    shopping_post = post_action_snapshot.get("shopping_buckets") or {}

    inventory_entries = []
    for cname in sorted(set(inventory_before) | set(inventory_post)):
        inventory_entries.extend(diff_bucket(inventory_before.get(cname), inventory_post.get(cname)))

    shopping_entries = []
    for cname in sorted(set(shopping_before) | set(shopping_post)):
        shopping_entries.extend(diff_bucket(shopping_before.get(cname), shopping_post.get(cname)))

    expense_add = post_action_snapshot.get("expense_add")
    expense_delete = before_snapshot.get("expense_delete")
    return {
        "inventory": inventory_entries,
        "shopping": shopping_entries,
        "expense_added": (
            {
                "amount": expense_add["amount"], "currency": expense_add["currency"],
                "category": expense_add["category"], "description": expense_add.get("description"),
            }
            if expense_add else None
        ),
        "expense_deleted": (
            {
                "amount": expense_delete["amount"], "currency": expense_delete["currency"],
                "category": expense_delete["category"], "description": expense_delete.get("description"),
            }
            if expense_delete else None
        ),
    }


def _format_amount(amount_str, currency):
    try:
        formatted = f"{Decimal(amount_str):.2f}".replace(".", ",")
    except Exception:
        formatted = amount_str
    unit = "zł" if currency == "PLN" else (currency or "")
    return f"{formatted} {unit}".strip()


def _format_expense_label(expense):
    name = expense.get("description") or expense.get("category")
    return f"{name} — {_format_amount(expense['amount'], expense.get('currency'))}"


def _format_bucket_line(entry):
    name = entry.get("name")
    quantity_text = entry.get("quantity_text")
    if entry["kind"] == "update":
        return f"• {name} — {entry.get('current_text')} → {entry.get('target_text')}"
    if entry["kind"] == "restore":
        suffix = f" — {quantity_text}" if quantity_text else ""
        return f"• Повернути {name}{suffix}"
    suffix = f" — {quantity_text}" if quantity_text else ""
    return f"• Прибрати {name}{suffix}"


def format_undo_preview(summary):
    """Render the undo confirmation preview from a stored `summary` dict.
    Never touches the DB — pure formatting of already-captured data."""
    lines = [f"{UNDO_BUTTON_TEXT}?", "", "Буде повернено:"]

    if summary.get("inventory"):
        lines.append("")
        lines.append("🧊 Запаси")
        lines.extend(_format_bucket_line(e) for e in summary["inventory"])

    if summary.get("shopping"):
        lines.append("")
        lines.append("🛒 Покупки")
        lines.extend(_format_bucket_line(e) for e in summary["shopping"])

    if summary.get("expense_added") or summary.get("expense_deleted"):
        lines.append("")
        lines.append("💸 Витрати")
        if summary.get("expense_added"):
            lines.append(f"• Видалити витрату: {_format_expense_label(summary['expense_added'])}")
        if summary.get("expense_deleted"):
            lines.append(f"• Відновити витрату: {_format_expense_label(summary['expense_deleted'])}")

    return "\n".join(lines)
