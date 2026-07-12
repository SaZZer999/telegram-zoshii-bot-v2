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
from decimal import Decimal

import expenses
from quantities import (
    parse_structured_quantity, format_quantity_display, STRUCTURED_UNITS, _UNIT_CONVERSION_GROUPS,
)

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


def _split_trailing_quantity_words(text):
    """Like _extract_trailing_quantity, but also returns the leading words
    that remain after removing the matched trailing quantity — used to pull
    a name fragment out of "<name> <quantity>" text (e.g. "молока 1 л" ->
    ("молока", "1 л")). Returns (None, None) if no trailing quantity is
    found; returns ("", quantity) when the quantity consumed every word."""
    words = text.strip().split()
    if not words:
        return None, None
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        candidate = " ".join(words[-n:])
        if _looks_like_quantity(candidate):
            leading = " ".join(words[:-n]).strip()
            return leading, candidate
    return None, None


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


# =========================
# PREVIEW EDIT V2 — safe text edits to an ACTIVE pending_global_household
# "add" preview (add_shopping_items / add_inventory_items only — see the
# Preview Edit V2 work order). Same ground rules as V1 above: deterministic
# only, no Gemini call, never touches the database, only mutates the SAME
# item dicts already sitting in the pending preview's add_shopping_items/
# add_inventory_items lists. consume_changes/new_expenses/delete_expense on
# the same pending preview are never touched by anything in this section —
# out of scope for V2.
#
# `items` (as taken by every function below) is the CALLER's own
# concatenation of add_shopping_items + add_inventory_items, in the same
# order household_router.format_preview renders them (shopping section
# first, then inventory) — that render order is what "1 л, 500 г"-style
# positional shorthand maps against. Because list concatenation only copies
# the outer list, not the item dicts themselves, mutating an entry of that
# concatenated list in place (apply_household_add_preview_edits below)
# mutates the exact same dict already referenced by the pending preview's
# own add_shopping_items/add_inventory_items — no extra write-back step
# needed.
# =========================

HOUSEHOLD_EDIT_AMBIGUOUS_MSG = (
    "У плані кілька товарів підходять під цю назву. Напиши точнішу назву "
    "разом із кількістю, наприклад «Молоко 1 л»."
)

HOUSEHOLD_EDIT_NOT_FOUND_MSG = (
    "Не знайшов такий товар у поточному плані. Напиши точну назву товару з "
    "плану разом із кількістю, наприклад «Молоко 1 л»."
)

HOUSEHOLD_EDIT_POSITIONAL_MISMATCH_MSG = (
    "Кількість значень не збігається з кількістю товарів у плані. Напиши "
    "кількість для кожного товару окремо, наприклад «Молоко 1 л, Сир 500 г»."
)

# "тільки"/"лише"/"а"/"та"/"і" at the start of a comma-split segment are pure
# connective filler ("тільки молока 1 л, а сиру 500 г") — stripped (possibly
# repeatedly) before shape-matching, never treated as part of a name.
_LEADING_FILLER_RE = re.compile(r"^(?:тільки|лише|а|та|і)\s+", re.IGNORECASE)

_RENAME_NA_RE = re.compile(r"^перейменуй\s+(?P<old>.+?)\s+на\s+(?P<new>.+)$", re.IGNORECASE)
_INSTEAD_ITEM_RE = re.compile(r"^замість\s+(?P<old>.+?)\s+зроби\s+(?P<new>.+)$", re.IGNORECASE)
_MAKE_ITEM_RE = re.compile(r"^(?:так[.,]?\s*)?(?:тільки\s+)?зроби\s+(?P<rest>.+)$", re.IGNORECASE)


def _strip_leading_filler(segment):
    s = segment.strip()
    while True:
        m = _LEADING_FILLER_RE.match(s)
        if not m:
            return s
        s = s[m.end():].strip()


def _parse_household_edit_segment(raw_segment):
    """Parse ONE comma-separated segment of a household add-preview edit
    into a shape tuple, or None if it matches nothing:
      ("rename", old_name_token_or_None, new_name_text)
      ("quantity", name_token_or_None, quantity_text)
      ("positional_quantity", quantity_text)
    `name_token` is None only for a bare "зроби <quantity>" with no name at
    all (safe only when the active preview has exactly one item — resolved
    by the caller, never guessed here)."""
    seg = _strip_trailing_punct(_strip_leading_filler(raw_segment))
    if not seg:
        return None

    m = _RENAME_NA_RE.match(seg)
    if m:
        old = _strip_trailing_punct(m.group("old"))
        new = _strip_trailing_punct(m.group("new"))
        if not old or not new:
            return None
        return ("rename", old, new)

    m = _INSTEAD_ITEM_RE.match(seg)
    if m:
        old_part = _strip_trailing_punct(m.group("old"))
        new = _strip_trailing_punct(m.group("new"))
        if not old_part or not new:
            return None
        # old_part may itself carry a trailing (old) quantity fragment, e.g.
        # "молока 1 шт" — only the leading name matters for item matching.
        old_leading, _old_qty = _split_trailing_quantity_words(old_part)
        old_name = old_leading if old_leading else old_part
        if _looks_like_quantity(new):
            return ("quantity", old_name or None, new)
        return ("rename", old_name or None, new)

    m = _MAKE_ITEM_RE.match(seg)
    if m:
        rest = _strip_trailing_punct(m.group("rest"))
        leading, qty = _split_trailing_quantity_words(rest)
        if qty is None:
            return None
        return ("quantity", leading or None, qty)

    if _looks_like_quantity(seg):
        return ("positional_quantity", seg)

    leading, qty = _split_trailing_quantity_words(seg)
    if qty is not None and leading:
        return ("quantity", leading, qty)

    return None


def _name_token_matches(token, item):
    """True if free-text `token` plausibly refers to `item` — matches the
    item's display name or canonical_name exactly, or via a narrow
    Ukrainian-declension-tolerant stem check (e.g. "молока"/"молоко",
    "сиру"/"сир"): the shorter of the two normalized strings must be a
    prefix of the longer one, with at most 2 trailing characters differing.
    Deliberately simple/deterministic — never fuzzy-NLP, never guesses
    across genuinely different product names."""
    token_norm = (token or "").strip().lower()
    if not token_norm:
        return False
    for candidate in (item.get("name"), item.get("canonical_name")):
        cand_norm = (candidate or "").strip().lower()
        if not cand_norm:
            continue
        if token_norm == cand_norm:
            return True
        shorter_len = min(len(token_norm), len(cand_norm))
        longer_len = max(len(token_norm), len(cand_norm))
        if shorter_len < 3:
            continue
        common = 0
        for a, b in zip(token_norm, cand_norm):
            if a != b:
                break
            common += 1
        if len(token_norm) == len(cand_norm):
            # Same length, only trailing declension chars may differ (e.g.
            # "молока"/"молоко").
            if common >= shorter_len - 2:
                return True
        elif common == shorter_len and (longer_len - shorter_len) <= 2:
            # Different length: the shorter one must be a genuine prefix of
            # the longer (e.g. "сир"/"сиру"), with only a short declension
            # suffix appended.
            return True
    return False


def parse_household_add_preview_edit(text, items):
    """Deterministically parse a free-text edit to an active
    pending_global_household "add" preview into a list of item-resolved
    edits — `items` is the caller's add_shopping_items + add_inventory_items
    concatenation (see this section's own module-level docstring above).

    Returns (True, edits) on success — edits is a non-empty list of either
      {"index": i, "quantity_value": Decimal, "quantity_unit": str, "quantity_text": str}
    or
      {"index": i, "new_name_raw": str}
    (index into `items`) for the caller to apply via
    apply_household_add_preview_edits.

    Returns (False, message_or_None) otherwise: message_or_None is a ready-
    to-send, specific explanation (ambiguous name, item not found, or a
    positional-shorthand count mismatch) — or None when `text` doesn't match
    ANY recognized edit shape at all, signaling the caller should fall back
    to its own existing "unfinished plan" guard message instead (keeps prior
    behavior for genuinely unrelated text unchanged). Never mutates `items`
    itself — that only happens in apply_household_add_preview_edits, and
    only after this function has already returned a fully successful
    (True, edits) result.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False, None

    segments = []
    for raw in stripped.split(","):
        if not raw.strip():
            continue
        parsed = _parse_household_edit_segment(raw)
        if parsed is None:
            return False, None
        segments.append(parsed)
    if not segments:
        return False, None

    if all(seg[0] == "positional_quantity" for seg in segments):
        if len(segments) != len(items):
            return False, HOUSEHOLD_EDIT_POSITIONAL_MISMATCH_MSG
        edits = []
        for index, (_, quantity_text) in enumerate(segments):
            value, unit = parse_structured_quantity(quantity_text)
            if value is None:
                return False, INVALID_QUANTITY_EDIT_MSG
            edits.append({
                "index": index, "quantity_value": value, "quantity_unit": unit,
                "quantity_text": format_quantity_display(value, unit),
            })
        return True, edits

    edits = []
    for kind, name_token, value_text in segments:
        if kind == "positional_quantity":
            # A bare quantity mixed with a named edit in the same message —
            # positional shorthand only applies when EVERY segment is a bare
            # quantity; never guess which item an unnamed one belongs to
            # once a name is also present elsewhere in the same message.
            return False, HOUSEHOLD_EDIT_POSITIONAL_MISMATCH_MSG
        if name_token is None:
            if len(items) != 1:
                return False, HOUSEHOLD_EDIT_AMBIGUOUS_MSG
            index = 0
        else:
            matches = [i for i, item in enumerate(items) if _name_token_matches(name_token, item)]
            if not matches:
                return False, HOUSEHOLD_EDIT_NOT_FOUND_MSG
            if len(matches) > 1:
                return False, HOUSEHOLD_EDIT_AMBIGUOUS_MSG
            index = matches[0]
        if kind == "quantity":
            value, unit = parse_structured_quantity(value_text)
            if value is None:
                return False, INVALID_QUANTITY_EDIT_MSG
            edits.append({
                "index": index, "quantity_value": value, "quantity_unit": unit,
                "quantity_text": format_quantity_display(value, unit),
            })
        else:  # "rename"
            edits.append({"index": index, "new_name_raw": value_text})

    if not edits:
        return False, None
    return True, edits


def apply_household_add_preview_edits(items, edits, canonicalize_name, capitalize_first):
    """Apply `edits` (as returned by parse_household_add_preview_edit) to
    `items` in place — mutates the SAME item dicts the caller's pending
    preview already references, never reorders/removes anything, never
    touches the database. Only ever call this with edits from a successful
    (True, edits) parse result; every "index" is assumed valid for `items`."""
    for edit in edits:
        item = items[edit["index"]]
        if "new_name_raw" in edit:
            new_name = capitalize_first(edit["new_name_raw"].strip())
            item["name"] = new_name
            item["canonical_name"] = canonicalize_name(new_name)
        else:
            item["quantity_value"] = edit["quantity_value"]
            item["quantity_unit"] = edit["quantity_unit"]
            item["quantity_text"] = edit["quantity_text"]
            item["quantity_inferred"] = False


# =========================
# PRICE CLARIFICATION V1 — a follow-up to Purchase Event Planner V1
# (household_router.py's "ambiguous_expense"/discount-marker handling): once
# a pending_global_household preview has an inventory add but no expense
# (an amount was ambiguous — a discount, an original/per-unit price, or
# omitted entirely), a short clarification reply like "за пів кілограма
# 5 zl", "0,5 кг було 5 злотих", or "заплатив 10 zł" must update the SAME
# preview with a real expense instead of being rejected as an unrelated new
# command — that rejection (the generic pending-plan guard message) was the
# actual live bug this section fixes.
#
# `expenses._parse_expense_amount` is reused (never a second amount parser)
# so every accepted spelling/rounding/max-amount rule stays identical to
# the rest of the expense flow — expenses.py never imports bot.py or this
# module (see its own imports: only database.StaleSnapshotError and
# action_history), so importing it here creates no cycle, same reasoning as
# household_router.py's own direct `import expenses`.
#
# Deterministic only, no Gemini call, no database write: parse_price_
# clarification + compute_quantity_multiplier only ever return numbers that
# are exact, explicit, and safe (see compute_quantity_multiplier's own
# "never guessed" contract) — anything not cleanly resolvable returns None,
# and the caller (bot.py) must fall back to its existing guard/clarification
# message rather than ever inventing an amount.
# =========================

_TOTAL_PAID_VERB_RE = re.compile(
    r"^(?:заплатив|заплатила|заплатили|оплатив|оплатила|оплатили|сплатив|сплатила|сплатили)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

_UNIT_PRICE_ZA_RE = re.compile(r"^за\s+(?P<rest>.+)$", re.IGNORECASE)

_UNIT_PRICE_BULO_RE = re.compile(r"^(?P<qty>.+?)\s+(?:було|була|був)\s+(?P<rest>.+)$", re.IGNORECASE)

# "пів кіло"/"пів кілограма"/"пів кг"/"пів літра"/"пів л" -> (0.5, "кг"/"л").
# Deliberately local to this one clarification flow rather than widening
# quantities.py's own _WORD_NUMBER_QUANTITIES (a general-purpose table used
# far beyond price clarification) — same reasoning as this module's V1
# section keeping its own narrow patch-shape catalog.
_HALF_QUANTITY_RE = re.compile(r"^пів\s*(кіло(?:грам\w*)?|кг|л(?:ітр\w*)?|л)$", re.IGNORECASE)

# expenses._parse_expense_amount already strips "zł"/"zl"/"pln"/a bare "z"
# marker, but NOT the spelled-out Ukrainian word ("злотих"/"злотий"/
# "злоті") a typed clarification is just as likely to use — stripped here
# first so every amount extraction in this section accepts both forms
# identically.
_ZLOTY_WORD_RE = re.compile(r"злот\w*", re.IGNORECASE)


def _parse_clarification_amount(text):
    return expenses._parse_expense_amount(_ZLOTY_WORD_RE.sub("", text or ""))


def _resolve_clarification_quantity(text):
    """Parse a quantity phrase for a price clarification — a normal
    structured quantity ("0,5 кг", "500 г") via parse_structured_quantity,
    or the narrow word-fraction _HALF_QUANTITY_RE above. Returns
    (value, unit) or (None, None) — never guessed beyond these two exact
    shapes."""
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    value, unit = parse_structured_quantity(stripped)
    if value is not None:
        return value, unit
    m = _HALF_QUANTITY_RE.match(stripped)
    if m:
        return Decimal("0.5"), ("л" if m.group(1).lower().startswith("л") else "кг")
    return None, None


def _split_trailing_amount(text):
    """Like _split_trailing_quantity_words above, but for a trailing
    amount+currency span ("5 zł", "5,00 zł", "5 злотих") instead of a
    quantity — returns (leading_text, Decimal) for the LONGEST trailing
    1..3-word run _parse_clarification_amount accepts, or (None, None) if
    none does."""
    words = (text or "").strip().split()
    if not words:
        return None, None
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        candidate = " ".join(words[-n:])
        amount = _parse_clarification_amount(candidate)
        if amount is not None:
            leading = " ".join(words[:-n]).strip()
            return leading, amount
    return None, None


def parse_price_clarification(text):
    """Deterministically parse a free-text reply while a pending_global_
    household preview's expense amount is ambiguous/missing, into ONE of:

      {"kind": "total_paid", "amount": Decimal}
          — the user stated the FINAL paid amount directly ("заплатив
            10 zł") — the caller uses it AS-IS, no math.
      {"kind": "unit_price", "unit_quantity_value": Decimal,
       "unit_quantity_unit": str, "unit_amount": Decimal}
          — a PER-UNIT price ("за пів кілограма 5 zl", "0,5 кг було 5
            злотих") — the caller must still multiply unit_amount by
            compute_quantity_multiplier(pending item's own quantity,
            this unit) before it is safe to use as an expense amount.

    Returns None if `text` doesn't match any recognized shape at all — the
    caller must treat that exactly like an unrecognized preview edit (fall
    back to its own existing guard/clarification message), never guess.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    m = _TOTAL_PAID_VERB_RE.match(stripped)
    if m:
        amount = _parse_clarification_amount(m.group("rest").strip())
        if amount is None:
            return None
        return {"kind": "total_paid", "amount": amount}

    m = _UNIT_PRICE_ZA_RE.match(stripped)
    if m:
        leading, amount = _split_trailing_amount(m.group("rest"))
        if amount is None or not leading:
            return None
        qty_value, qty_unit = _resolve_clarification_quantity(leading)
        if qty_value is None:
            return None
        return {
            "kind": "unit_price", "unit_quantity_value": qty_value,
            "unit_quantity_unit": qty_unit, "unit_amount": amount,
        }

    m = _UNIT_PRICE_BULO_RE.match(stripped)
    if m:
        qty_value, qty_unit = _resolve_clarification_quantity(m.group("qty"))
        if qty_value is None:
            return None
        amount = _parse_clarification_amount(m.group("rest").strip())
        if amount is None:
            return None
        return {
            "kind": "unit_price", "unit_quantity_value": qty_value,
            "unit_quantity_unit": qty_unit, "unit_amount": amount,
        }

    return None


# =========================
# TEXT CORRECTION V1 — a short correction phrase ("не X, а Y", "заміни X на
# Y", "перейменуй X на Y", "там має бути X не A, а X B") that fixes an item
# name or expense description already sitting in an ACTIVE
# pending_global_household preview, instead of being rejected as an
# unrelated new command by the generic pending-plan guard message — that
# rejection was the live bug this section fixes. Deterministic only, no
# Gemini call: parse_text_correction only ever extracts an (old, new)
# fragment pair; the caller (bot.py's _try_apply_text_correction) decides
# WHICH item/expense it applies to (exactly one candidate whose text
# contains `old`, never guessed across multiple matches — see that
# function's own docstring) and calls apply_text_correction to build the
# corrected text. Never touches quantities/amounts/units/dates/categories —
# purely a substring replace within whichever single field matched.
# =========================

# "X не A, а X B" / "не A, а B" — the OPTIONAL leading `prefix` (a single
# word right before "не", e.g. "подарунок", "це", or filler like "має
# бути"'s last word) only controls whether an optional repeat of the SAME
# word is consumed right after "а" — it is never itself part of the
# returned old/new fragments, so a filler word captured here (e.g. "це")
# that doesn't literally reappear after "а" is harmless: the optional
# repeat group just matches zero times and `new` is extracted correctly
# either way. re.search (not match) on purpose — the required "там має
# бути ..." example has "не X, а Y" embedded mid-sentence, not at the start.
_CONTRAST_CORRECTION_RE = re.compile(
    r"(?:(?P<prefix>\S+)\s+)?не\s+(?P<old>.+?),?\s+а\s+(?:(?P=prefix)\s+)?(?P<new>.+?)[.!?]*$",
    re.IGNORECASE,
)

_ZAMINY_CORRECTION_RE = re.compile(r"^заміни\s+(?P<old>.+?)\s+на\s+(?P<new>.+?)[.!?]*$", re.IGNORECASE)
_PEREYMENUY_CORRECTION_RE = re.compile(r"^перейменуй\s+(?P<old>.+?)\s+на\s+(?P<new>.+?)[.!?]*$", re.IGNORECASE)


def parse_text_correction(text):
    """Deterministically parse a short correction phrase into {"old": str,
    "new": str} — `old` is the fragment to find (case-insensitive
    substring) among the active preview's item names/expense descriptions,
    `new` is what to replace it with. Returns None if `text` doesn't match
    any recognized correction shape at all — the caller must treat that
    exactly like an unrecognized preview edit (fall back to its own
    existing guard/clarification message), never guess.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    m = _ZAMINY_CORRECTION_RE.match(stripped)
    if m:
        old, new = m.group("old").strip(), m.group("new").strip()
        return {"old": old, "new": new} if old and new else None

    m = _PEREYMENUY_CORRECTION_RE.match(stripped)
    if m:
        old, new = m.group("old").strip(), m.group("new").strip()
        return {"old": old, "new": new} if old and new else None

    m = _CONTRAST_CORRECTION_RE.search(stripped)
    if m:
        old = _strip_trailing_punct(m.group("old"))
        new = _strip_trailing_punct(m.group("new"))
        if not old or not new:
            return None
        # Pending Preview Edit Planner V2 — a combined correction message
        # ("Х не A, а Х B, і ціна за Y не N, а M") has TWO independent "не
        # ..., а ..." clauses; since `new` above is lazy but still anchored
        # to end-of-string, it greedily swallows the SECOND clause whole
        # instead of stopping after the first one (e.g. new ends up
        # "дочці, і ціна за комод не 527, а 528" instead of just "дочці").
        # Rather than trying to teach this deterministic regex to safely
        # split multiple independent corrections apart, detect that it
        # happened (the swallowed `new` itself contains another full
        # contrast clause) and decline entirely — the caller then falls
        # through to the semantic AI planner, which DOES support multiple
        # patches in one message and applies each to its own correct target.
        if _CONTRAST_CORRECTION_RE.search(new):
            return None
        return {"old": old, "new": new}

    return None


def apply_text_correction(text, old_fragment, new_fragment):
    """Replace the FIRST case-insensitive occurrence of `old_fragment`
    within `text` with `new_fragment`, leaving the rest of `text` untouched
    (e.g. "Подарунок сестрі" with old="сестрі"/new="дочці" ->
    "Подарунок дочці") — never a full-text overwrite, so surrounding words
    (a product name, a shared prefix) survive the correction unchanged."""
    return re.sub(re.escape(old_fragment), new_fragment, text, count=1, flags=re.IGNORECASE)


def find_text_correction_targets(old_fragment, candidates):
    """`candidates` is a list of (kind, index, text) tuples (kind/index are
    caller-defined, opaque here — e.g. ("item", 2, "Автокрісло") or
    ("expense", 0, "Подарунок сестрі")). Returns the SUBSET whose `text`
    contains `old_fragment` (case-insensitive substring) — the caller
    applies the correction only when this list has EXACTLY one entry;
    zero or 2+ matches must never guess (see this module's own "TEXT
    CORRECTION V1" section docstring)."""
    needle = (old_fragment or "").strip().lower()
    if not needle:
        return []
    return [c for c in candidates if needle in (c[2] or "").lower()]


def compute_quantity_multiplier(pending_value, pending_unit, unit_value, unit_unit):
    """How many `unit_value unit_unit`-sized units fit into `pending_value
    pending_unit` — e.g. (1, "кг") against (0.5, "кг") -> Decimal("2"). Same
    compatibility rule as quantities.merge_quantity_values: identical unit,
    or same mass/volume conversion group; "шт." only ever matches itself.

    Returns None (never guessed) if either value is missing, either unit is
    not a known structured unit, the units are incompatible, or unit_value
    is zero — the caller must fall back to a clarification/guard message
    rather than ever inventing a multiplier.
    """
    if pending_value is None or unit_value is None:
        return None
    if pending_unit not in STRUCTURED_UNITS or unit_unit not in STRUCTURED_UNITS:
        return None
    dec_pending = pending_value if isinstance(pending_value, Decimal) else Decimal(str(pending_value))
    dec_unit = unit_value if isinstance(unit_value, Decimal) else Decimal(str(unit_value))
    if dec_unit == 0:
        return None
    if pending_unit == unit_unit:
        return dec_pending / dec_unit
    group_pending = _UNIT_CONVERSION_GROUPS.get(pending_unit)
    group_unit = _UNIT_CONVERSION_GROUPS.get(unit_unit)
    if group_pending is None or group_unit is None or group_pending[0] != group_unit[0]:
        return None
    base_pending = dec_pending * group_pending[1]
    base_unit = dec_unit * group_unit[1]
    return base_pending / base_unit
