"""Preview Edit V1 — safe text edits to an ACTIVE pending write preview.

Scope (see the Preview Edit V1 work order for the full spec): only
pending_inventory_transform previews support text edits in this version.
Editing never touches the database — it only mutates the SAME pending
dict already awaiting "✅ Так, застосувати"/"❌ Скасувати" and returns a
freshly rendered preview string for the caller (bot.py) to send.

Deterministic only, no Gemini call: the four required edit shapes ("зроби
Х — N шт", "замість N шт зроби M шт", "назви це Х", "замість Х зроби Y")
are all covered by plain regex + quantities.parse_structured_quantity, so
there's no need for an LLM-assisted patch parser in V1 (see the work
order's "If deterministic parsing can cover the required examples safely,
use deterministic parsing first").

Patch shape (the same closed catalog a future Gemini-assisted parser would
also have to emit — kept here so validate/apply never needs to change if
one is added later):

    {"action": "set_target_quantity", "quantity": "2 шт"}
    {"action": "set_target_name", "name": "М'ясо"}
    {"action": "set_target", "name": "М'ясо", "quantity": "2 шт"}
    {"action": "unsupported", "reason": "..."}

`parse_inventory_transform_edit` only ever produces the first two shapes
(the required examples never need a combined name+quantity edit or an
explicit "unsupported" signal — a text that doesn't match anything simply
returns None) — `validate_inventory_transform_patch`/
`apply_inventory_transform_patch` still handle all four so nothing else
needs to change if a Gemini-assisted fallback is added later.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — canonicalize_name/capitalize_first are injected callables (bot.py's
own product-name synonym table lives in bot.py and must stay there, same
reasoning as quantities.py's own module docstring).
"""
import re

from quantities import parse_structured_quantity, format_quantity_display

UNSUPPORTED_PREVIEW_TYPE_MSG = (
    "Редагування цього плану текстом ще не підтримується. "
    "Підтвердь, скасуй або створи план заново."
)

UNPARSEABLE_EDIT_MSG = (
    "У тебе є незавершений план змін. Підтвердь його, скасуй або напиши зміну точніше."
)

INVALID_QUANTITY_EDIT_MSG = (
    "Не розпізнав нову кількість. Напиши точну кількість з одиницею, наприклад «2 шт.» або «500 мл»."
)

_ALLOWED_ACTIONS = {"set_target_quantity", "set_target_name", "set_target", "unsupported"}

# "замість <old> зроби <new>" / "зроби <new> замість <old>" — the NEW side
# (whichever one follows "зроби") always wins; whether it becomes a
# quantity or a name patch depends only on whether it parses as a
# quantity (see parse_inventory_transform_edit) — the OLD side is only
# used to recognize the sentence shape, never applied.
_INSTEAD_OLD_NEW_RE = re.compile(r"^замість\s+(?P<old>.+?)\s+зроби\s+(?P<new>.+)$", re.IGNORECASE)
_NEW_INSTEAD_OLD_RE = re.compile(r"^зроби\s+(?P<new>.+?)\s+замість\s+(?P<old>.+)$", re.IGNORECASE)

# "назви (це) <name>" — always a rename, never a quantity edit.
_RENAME_RE = re.compile(r"^назви\s+(?:це\s+)?(?P<name>.+)$", re.IGNORECASE)

# "(так[.,] )?(тільки )?зроби <rest>" — the required examples ("так.тільки
# зроби М'ясних виробів — 2 шт", "зроби М'ясні вироби 2 шт") only ever
# change the target QUANTITY, even though `rest` also names the target in
# some (possibly declined) form — the name fragment is deliberately
# ignored here rather than guessed at, since a declined-case fragment
# ("М'ясних виробів") is not a safe display name.
_MAKE_RE = re.compile(r"^(?:так[.,]?\s*)?(?:тільки\s+)?зроби\s+(?P<rest>.+)$", re.IGNORECASE)

_TRAILING_PUNCT = ".!?"


def _strip_trailing_punct(text):
    return text.strip().rstrip(_TRAILING_PUNCT).strip()


def _looks_like_quantity(text):
    value, unit = parse_structured_quantity(text)
    return value is not None


def _extract_trailing_quantity(text):
    """Find the longest trailing word-run (1..3 words) in `text` that
    quantities.parse_structured_quantity accepts as a whole quantity — so
    "М'ясних виробів — 2 шт" finds "2 шт" (the leading name/dash fragment
    is simply ignored, never guessed at as a new name). Returns the raw
    matched substring, or None if no trailing run parses."""
    words = text.strip().split()
    if not words:
        return None
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        candidate = " ".join(words[-n:])
        if _looks_like_quantity(candidate):
            return candidate
    return None


def parse_inventory_transform_edit(text):
    """Deterministically parse a free-text edit to an active
    pending_inventory_transform preview into ONE strict patch dict.

    Returns None if `text` doesn't match any recognized edit shape at all
    — the caller (bot.py) must treat that as "unparseable": send
    UNPARSEABLE_EDIT_MSG and leave the pending preview completely
    unchanged, never fall through to any other route.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    m = _INSTEAD_OLD_NEW_RE.match(stripped)
    if m:
        new = _strip_trailing_punct(m.group("new"))
        if not new:
            return None
        if _looks_like_quantity(new):
            return {"action": "set_target_quantity", "quantity": new}
        return {"action": "set_target_name", "name": new}

    m = _NEW_INSTEAD_OLD_RE.match(stripped)
    if m:
        new = _strip_trailing_punct(m.group("new"))
        if not new:
            return None
        if _looks_like_quantity(new):
            return {"action": "set_target_quantity", "quantity": new}
        return {"action": "set_target_name", "name": new}

    m = _RENAME_RE.match(stripped)
    if m:
        name = _strip_trailing_punct(m.group("name"))
        if not name:
            return None
        return {"action": "set_target_name", "name": name}

    m = _MAKE_RE.match(stripped)
    if m:
        rest = _strip_trailing_punct(m.group("rest"))
        qty = _extract_trailing_quantity(rest)
        if qty:
            return {"action": "set_target_quantity", "quantity": qty}
        return None

    return None


def validate_inventory_transform_patch(patch):
    """Reject unknown actions / missing fields. Returns (True, None) for a
    structurally valid patch, or (False, reason) otherwise. Never inspects
    pending-preview state — purely a shape check on `patch` itself."""
    if not isinstance(patch, dict):
        return False, "Патч має бути об'єктом."
    action = patch.get("action")
    if action not in _ALLOWED_ACTIONS:
        return False, "Невідома дія редагування."
    if action == "set_target_quantity":
        if not isinstance(patch.get("quantity"), str) or not patch["quantity"].strip():
            return False, "Відсутня кількість у патчі."
        return True, None
    if action == "set_target_name":
        if not isinstance(patch.get("name"), str) or not patch["name"].strip():
            return False, "Відсутня назва у патчі."
        return True, None
    if action == "set_target":
        name_ok = isinstance(patch.get("name"), str) and patch["name"].strip()
        quantity_ok = isinstance(patch.get("quantity"), str) and patch["quantity"].strip()
        if not (name_ok and quantity_ok):
            return False, "Відсутня назва або кількість у патчі."
        return True, None
    # action == "unsupported" — always structurally valid; apply_* below
    # always turns it into a (False, reason) result.
    return True, None


def apply_inventory_transform_patch(pending_data, patch, canonicalize_name, capitalize_first):
    """Apply a patch to `pending_data` (the SAME
    pending_inventory_transform[chat_id] dict — mutated in place on
    success) — one of the four documented actions.

    Returns (True, None) on success, or (False, error_message) if the
    patch is structurally invalid, the action is "unsupported", or a
    quantity string doesn't parse — in every failure case `pending_data`
    is left COMPLETELY unchanged (checked before anything is mutated).

    Never touches source_item_ids/targets' item identity, never writes to
    the database — only the display/write fields (target_name/
    target_canonical_name/target_quantity_value/target_quantity_unit/
    target_quantity_text) that both the next re-rendered preview and, at
    confirm time, execute_inventory_transform's own write already read
    from this exact dict.
    """
    ok, reason = validate_inventory_transform_patch(patch)
    if not ok:
        return False, reason

    action = patch["action"]
    if action == "unsupported":
        return False, patch.get("reason") or UNPARSEABLE_EDIT_MSG

    new_quantity_value = pending_data["target_quantity_value"]
    new_quantity_unit = pending_data["target_quantity_unit"]
    new_quantity_text = pending_data["target_quantity_text"]
    if action in ("set_target_quantity", "set_target"):
        value, unit = parse_structured_quantity(patch["quantity"].strip())
        if value is None:
            return False, INVALID_QUANTITY_EDIT_MSG
        new_quantity_value, new_quantity_unit = value, unit
        new_quantity_text = format_quantity_display(value, unit)

    new_name = pending_data["target_name"]
    new_canonical_name = pending_data["target_canonical_name"]
    if action in ("set_target_name", "set_target"):
        new_name = capitalize_first(patch["name"].strip())
        new_canonical_name = canonicalize_name(new_name)

    pending_data["target_quantity_value"] = new_quantity_value
    pending_data["target_quantity_unit"] = new_quantity_unit
    pending_data["target_quantity_text"] = new_quantity_text
    pending_data["target_name"] = new_name
    pending_data["target_canonical_name"] = new_canonical_name
    return True, None
