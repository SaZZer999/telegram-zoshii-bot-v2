"""Structured quantity/unit parsing, merging, and display formatting.

Single source of truth for the quantity logic that used to live as two
independently-maintained copies in bot.py and database.py (see their git
history — both had their own STRUCTURED_UNITS/_UNIT_ALIASES/
_UNIT_CONVERSION_GROUPS/parse_structured_quantity/merge_quantity_values/
format_quantity_display, "duplicated on purpose" because database.py must
not import bot.py). Extracting them here removes the reason for that
duplication without introducing any new coupling: this module imports
nothing from bot.py, database.py, or household_router.py.

Deliberately excluded (stays local to bot.py/database.py, which both keep
their own canonicalize_name/_NAME_SYNONYMS/resolve_item_name):
- household alias lookup;
- product-name canonicalization/synonym rules;
- Gemini parsing, preview layout, database merge SQL, inventory
  representation guard.

normalize_quantity_fields (database.py) and normalize_item_quantity (bot.py)
also stay local, as thin wrappers, because they combine a canonical NAME
(out of scope here) with quantity fields (in scope) — they call
parse_quantity_fields()/format_quantity_display() below instead of
duplicating the math.

Every quantity value here is an exact Decimal from parse through merge —
never float — so PostgreSQL's NUMERIC column and every display string are
computed from the same exact number the user (or Gemini) provided.
"""
import re
from decimal import Decimal, InvalidOperation

STRUCTURED_UNITS = {"шт.", "л", "мл", "г", "кг"}

_UNIT_ALIASES = {
    "шт": "шт.", "шт.": "шт.", "штук": "шт.", "штуки": "шт.", "штука": "шт.",
    "л": "л", "літр": "л", "літри": "л", "літра": "л", "l": "л",
    "мл": "мл", "мілілітр": "мл", "мілілітри": "мл", "мілілітрів": "мл", "ml": "мл",
    "г": "г", "грам": "г", "грами": "г", "грама": "г", "грамів": "г",
    "кг": "кг", "кілограм": "кг", "кілограми": "кг", "кілограмів": "кг",
}

# Cross-unit merge groups: units within the same group ("mass"/"volume") are
# safely interconvertible (both are exact powers of 10 apart, so Decimal
# conversion is always exact — see merge_quantity_values). "шт." deliberately
# has no group — it never merges with mass or volume, only with itself.
_UNIT_CONVERSION_GROUPS = {
    "г": ("mass", Decimal("1")),
    "кг": ("mass", Decimal("1000")),
    "мл": ("volume", Decimal("1")),
    "л": ("volume", Decimal("1000")),
}

# Word-numbers that resolve to an exact count — deliberately a tiny, exact
# whitelist (never fuzzy/NLP), and always flagged quantity_inferred=True by
# parse_quantity_fields's callers since the whole quantity (not just the
# unit) is an assumption here, unlike an explicit digit.
_WORD_NUMBER_QUANTITIES = {
    "пара": Decimal("2"),
    "пару": Decimal("2"),
}


def _to_decimal(value):
    """Coerce a str/int/float/Decimal into an exact Decimal — always via
    Decimal(str(value)), never Decimal(float) directly, to avoid binary-
    float artifacts when value happens to already be a plain float."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _split_number_and_unit_no_space(text):
    """Insert a space between a leading numeral and an immediately-following
    unit word (e.g. "1Л" -> "1 Л", "500мл" -> "500 мл", "1,5л" -> "1,5 л") so
    an unspaced number+unit parses the same as a spaced one. Only touches a
    single leading numeral+unit token — returns text unchanged if it doesn't
    match that exact shape (so an already-spaced "6 штук" round-trips
    unchanged, and anything with 3+ tokens is untouched)."""
    match = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*([^\d\s]+)\s*$", text or "")
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return text


def parse_structured_quantity(quantity_text):
    """Parse an unambiguous quantity_text into (value, unit). Never raises.

    - "" / blank -> (None, None).
    - a bare number with no unit word (e.g. "3") -> (Decimal, "шт.") — an
      explicit count needs an obvious unit attached, not a guessed quantity.
    - an exact word-number from _WORD_NUMBER_QUANTITIES (e.g. "пара"/"пару")
      -> (Decimal, "шт.") — same Decimal contract; callers detect "no digit
      in the original text" to flag quantity_inferred=True for this case.
    - "value unit", spaced ("6 штук") or unspaced ("1Л", "500ML", "1,5л") ->
      (Decimal, unit).
    Anything else (containers like "пачка"/"упаковка", 3+ tokens, unknown
    unit words) stays unparseable -> (None, None).
    """
    if not quantity_text or not quantity_text.strip():
        return None, None
    normalized = _split_number_and_unit_no_space(quantity_text.strip()).replace(",", ".")
    parts = normalized.split()
    if len(parts) == 1:
        word = parts[0].lower()
        if word in _WORD_NUMBER_QUANTITIES:
            return _WORD_NUMBER_QUANTITIES[word], "шт."
        try:
            return Decimal(parts[0]), "шт."
        except InvalidOperation:
            return None, None
    if len(parts) == 2:
        try:
            value = Decimal(parts[0])
        except InvalidOperation:
            return None, None
        unit = _UNIT_ALIASES.get(parts[1].lower().rstrip("."))
        if unit is None:
            return None, None
        return value, unit
    return None, None


def format_quantity_display(value, unit):
    """Format a numeric value+unit for display: comma decimal, no trailing
    zeros, never scientific notation, never rounds/truncates the value
    itself — a small nonzero value (e.g. 0.00011) must never be shown as
    "0". Converts through Decimal(str(value)) rather than Decimal(value)
    directly to avoid binary-float artifacts when value is a plain float.
    """
    if value is None:
        return ""
    dec_value = _to_decimal(value)
    text = format(dec_value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text.lstrip("-") in ("", "0"):
        text = "0"
    value_str = text.replace(".", ",")
    return f"{value_str} {unit}" if unit else value_str


def parse_quantity_fields(quantity_text, allow_default_unit=False):
    """The pure-quantity half of normalize_quantity_fields/normalize_item_
    quantity (minus canonical_name, which needs product-name synonym rules
    that stay local to bot.py/database.py). allow_default_unit=True applies
    the "1 шт." default only when quantity_text is genuinely blank (new
    items) — never for backfilling old data.

    Returns {"quantity_value", "quantity_unit", "quantity_inferred",
    "quantity_text"} — quantity_value is an exact Decimal or None.
    """
    value, unit = parse_structured_quantity(quantity_text)
    inferred = False
    if value is None and not (quantity_text or "").strip() and allow_default_unit:
        value, unit, inferred = Decimal("1"), "шт.", True
    elif value is not None and not any(ch.isdigit() for ch in (quantity_text or "")):
        # Resolved from a non-digit word (e.g. "пара"/"пару" via
        # _WORD_NUMBER_QUANTITIES) rather than an explicit number — the
        # whole quantity is an assumption here, not just the unit, so it
        # gets the same quantity_inferred=True flag as the blank-text default.
        inferred = True
    display = format_quantity_display(value, unit) if value is not None else (quantity_text or "").strip()
    return {
        "quantity_value": value,
        "quantity_unit": unit,
        "quantity_inferred": inferred,
        "quantity_text": display,
    }


def merge_quantity_values(value_a, unit_a, value_b, unit_b):
    """Return merged (value, unit) if two structured quantities can be safely
    summed, else None. Both units must be known structured units; either the
    same unit, or two units from the same conversion group (_UNIT_CONVERSION_
    GROUPS — mass: г/кг, volume: мл/л). "шт." has no group, so it only merges
    with itself, never with mass/volume — a count is never quantity-
    convertible into a weight/volume. The merged result always keeps unit_a
    (the FIRST/existing quantity's unit) as the display representation, per
    every caller's convention of passing the existing row first.

    Sums via exact Decimal arithmetic (never binary float directly, each
    input safely converted through Decimal(str(value))) and never rounds the
    result — full precision is preserved for NUMERIC storage; only
    format_quantity_display decides how to show it. Cross-unit conversion is
    likewise exact: every group factor is a power of 10, so the Decimal
    division below never loses precision. Returns the merged value as an
    exact Decimal (never float) — callers must pass it straight through to
    PostgreSQL's NUMERIC column without any float() round-trip, which would
    reintroduce binary-float imprecision right before storage.
    """
    if value_a is None or value_b is None:
        return None
    if unit_a not in STRUCTURED_UNITS or unit_b not in STRUCTURED_UNITS:
        return None
    dec_a = _to_decimal(value_a)
    dec_b = _to_decimal(value_b)
    if unit_a == unit_b:
        return dec_a + dec_b, unit_a
    group_a = _UNIT_CONVERSION_GROUPS.get(unit_a)
    group_b = _UNIT_CONVERSION_GROUPS.get(unit_b)
    if group_a is None or group_b is None or group_a[0] != group_b[0]:
        return None
    converted_b = (dec_b * group_b[1]) / group_a[1]
    return dec_a + converted_b, unit_a
