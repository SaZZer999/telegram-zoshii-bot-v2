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
    "шт": "шт.", "шт.": "шт.", "штук": "шт.", "штуки": "шт.", "штука": "шт.", "штуку": "шт.",
    "л": "л", "літр": "л", "літри": "л", "літра": "л", "l": "л",
    "мл": "мл", "мілілітр": "мл", "мілілітри": "мл", "мілілітрів": "мл", "ml": "мл",
    "г": "г", "грам": "г", "грами": "г", "грама": "г", "грамів": "г", "g": "г", "gram": "г", "grams": "г",
    # Active List Context Routing Stabilization V1 — Russian/mixed-language
    # spellings ("грамм"/"граммов" with a doubled "м", "килограмм"/
    # "килограмов" with "и" instead of "і") that real household voice
    # transcripts also produce, same tolerance this codebase already applies
    # elsewhere for mixed Ukrainian/Russian speech (see inventory.py's own
    # "з"/"із"/"из" preposition handling).
    "грамм": "г", "граммов": "г",
    "кг": "кг", "кілограм": "кг", "кілограми": "кг", "кілограмів": "кг", "kg": "кг",
    "килограмм": "кг", "килограмов": "кг",
    # Word-number Quantity + Price V1 — "литр" (Russian spelling, "и"
    # instead of "і") is one of the explicitly required Whisper-transcript
    # forms for this feature.
    "литр": "л", "литра": "л",
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


# Context Intent Safety V1 — a money amount ("52,37 zł") must never be
# silently read as an item quantity ("52,37 шт."). These two detectors work
# on the RAW user text (before any Gemini call, while the original money
# marker is still attached to the number) — see legacy_shopping_flow.py's/
# legacy_inventory_flow.py's "adding" mode handlers, the only callers: a
# quantity_text field a parser hands back later (e.g. Gemini's own "52,37",
# with the currency marker already stripped) can no longer be told apart
# from a genuine bare count, which is exactly why this check has to happen
# before that split ever occurs.
_MONEY_MARKER_RE = re.compile(
    r"\d[\d\s.,]*\s*(?:zł|zl\b|pln\b|злот\w*|зл\b)", re.IGNORECASE,
)

# Every known structured-unit word (шт/л/мл/г/кг and their aliases, dot
# stripped) reused from _UNIT_ALIASES above — never a second hand-maintained
# list — so a number tagged with an explicit quantity unit ("1 л", "500 г")
# is recognized the same way parse_structured_quantity itself would.
_QUANTITY_UNIT_STEMS = sorted({alias.rstrip(".") for alias in _UNIT_ALIASES}, key=len, reverse=True)
_QUANTITY_UNIT_RE = re.compile(
    r"\d[\d\s.,]*\s*(?:" + "|".join(re.escape(stem) for stem in _QUANTITY_UNIT_STEMS) + r")\b",
    re.IGNORECASE,
)


def looks_like_money_amount(text):
    """True if `text` contains a number tagged with a money marker (zł/zl/
    PLN/злотий/злотих/зл, case/punctuation-insensitive) anywhere. Pure/
    local, never calls Gemini."""
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_MONEY_MARKER_RE.search(text))


def looks_like_explicit_item_quantity(text):
    """True if `text` contains a number tagged with a known structured
    quantity unit (шт/л/мл/г/кг or an alias) anywhere — used together with
    looks_like_money_amount to tell a pure expense ("Кава 14 zł", no
    quantity) apart from an ambiguous item+price message ("Молоко 1 л
    4,99 zł"). Pure/local, never calls Gemini."""
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_QUANTITY_UNIT_RE.search(text))


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


# =========================
# WORD-NUMBER QUANTITY + PRICE V1 — a small, deterministic, compositional
# Ukrainian/Russian number-word parser (0-999 whole part, 0-99 fractional/
# grosze part) for household quantity ("один літр", "двісті п'ятдесят
# грамів") and PLN money ("чотири дев'яносто дев'ять злотих", "дванадцять
# злотих") phrases spoken/transcribed as words instead of digits — NOT a
# general-purpose number-to-words calculator. Fixes a live bug: "Тестове
# молоко один літр за чотири дев'яносто дев'ять злотих" fell all the way
# through to general AI-chat (which fabricated "I can't write to a
# database") because neither the digit-based quantities.looks_like_money_
# amount/looks_like_explicit_item_quantity detection nor bot.py's Quantity
# + Price Intent Clarification V1 (545113e) recognize spelled-out numbers
# at all — both only ever look for an actual digit.
#
# normalize_word_number_measurements(text) is the single public entrypoint
# most callers should use — it rewrites word-number quantity/money phrases
# into the SAME digit+unit/digit+currency shapes the existing numeric
# pipeline already understands ("Тестове молоко один літр за чотири
# дев'яносто дев'ять злотих" -> "Тестове молоко 1 л за 4,99 zł"), so every
# downstream route (pending_quantity_price_intent, the Global Household
# Router's own "Купив X за Y zł" purchase gate, ...) keeps working
# completely unchanged on a plain digit-normalized string — never a second
# Gemini call, never a new pending-state shape, never a new Gemini prompt.
# =========================
_WORD_NUMBER_ONES = {
    "нуль": 0, "один": 1, "одна": 1, "одне": 1, "одну": 1, "одної": 1,
    "два": 2, "дві": 2,
    "три": 3,
    "чотири": 4,
    "пять": 5,
    "шість": 6,
    "сім": 7,
    "вісім": 8,
    "девять": 9,
}
_WORD_NUMBER_TEENS = {
    "десять": 10, "одинадцять": 11, "дванадцять": 12, "тринадцять": 13,
    "чотирнадцять": 14, "пятнадцять": 15, "шістнадцять": 16, "сімнадцять": 17,
    "вісімнадцять": 18, "девятнадцять": 19,
}
_WORD_NUMBER_TENS = {
    "двадцять": 20, "тридцять": 30, "сорок": 40, "пятдесят": 50,
    "шістдесят": 60, "сімдесят": 70, "вісімдесят": 80, "девяносто": 90,
}
_WORD_NUMBER_HUNDREDS = {
    "сто": 100, "двісті": 200, "триста": 300, "чотириста": 400,
    "пятсот": 500, "шістсот": 600, "сімсот": 700, "вісімсот": 800,
    "девятсот": 900,
}
_HALF_QUANTITY_WORDS = {"пів", "половина", "половину", "половини"}
# "злот..." stem — mirrors expenses._EXPENSE_AMOUNT_RE's own "злот\w*"
# digit-side marker, so a word phrase is recognized as PLN by the exact
# same currency vocabulary the digit pipeline already trusts.
_CURRENCY_WORD_PREFIX = "злот"
_GROSZE_WORDS = {"гроші", "гроша", "грошей", "копійок", "копійки", "копійка", "копійку"}
_WORD_NUMBER_ZA_RE = re.compile(r"\bза\b", re.IGNORECASE)


def _clean_word_number_token(word):
    """Lowercase, strip trailing/leading punctuation and apostrophe
    variants (Ukrainian "п'ять" is spelled with an ASCII "'", a typographic
    "’", a modifier-letter "ʼ", or no apostrophe at all in mixed/Russian-
    influenced speech) — used only for number/unit/currency-word DICT
    LOOKUPS, never for the actual text spliced back into the normalized
    result."""
    cleaned = word.strip(".,!?;:").lower()
    return cleaned.replace("'", "").replace("’", "").replace("ʼ", "")


def _consume_word_number(cleaned_words, start):
    """Greedily consume a run of number-words at `cleaned_words[start:]` —
    an optional hundreds word, then EITHER a teens word OR a tens word
    (optionally followed by an ones word) OR a bare ones word — summing
    into one integer 0-999. Returns (value, next_index), or (None, start)
    if `cleaned_words[start]` isn't a recognized number-word at all."""
    idx = start
    total = 0
    matched = False

    def word_at(i):
        return cleaned_words[i] if i < len(cleaned_words) else None

    w = word_at(idx)
    if w in _WORD_NUMBER_HUNDREDS:
        total += _WORD_NUMBER_HUNDREDS[w]
        idx += 1
        matched = True
        w = word_at(idx)

    if w in _WORD_NUMBER_TEENS:
        total += _WORD_NUMBER_TEENS[w]
        idx += 1
        matched = True
    elif w in _WORD_NUMBER_TENS:
        total += _WORD_NUMBER_TENS[w]
        idx += 1
        matched = True
        w = word_at(idx)
        if w in _WORD_NUMBER_ONES:
            total += _WORD_NUMBER_ONES[w]
            idx += 1
    elif w in _WORD_NUMBER_ONES:
        total += _WORD_NUMBER_ONES[w]
        idx += 1
        matched = True

    if not matched:
        return None, start
    return total, idx


_WORD_NUMBER_TRAILING_PUNCT = ".,!?;:"


def _tokenize_with_spans(text):
    """Whitespace-delimited tokens with their character spans, TRAILING
    punctuation (".,!?;:") excluded from both the returned word and its
    span end — so a matched phrase's replacement span never accidentally
    swallows a comma/period that belongs to the SURROUNDING sentence (e.g.
    "одна штука, бо ..." — the comma right after "штука" must survive a
    quantity-phrase replacement, since callers like inventory.py's own
    _EXPLANATORY_TAIL_RE require that exact comma to still be there)."""
    tokens = []
    for m in re.finditer(r"\S+", text):
        start = m.start()
        trimmed = m.group(0).rstrip(_WORD_NUMBER_TRAILING_PUNCT)
        tokens.append((trimmed, start, start + len(trimmed)))
    return tokens


def parse_word_quantity(text):
    """Find the FIRST word-number household quantity phrase in `text`:
    either a "пів"/"половина"/"половину"/"половини" + unit word half-phrase
    (-> 0.5), or a number-word run (0-999) immediately followed by a
    recognized structured-unit word (see _UNIT_ALIASES — шт/г/кг/мл/л and
    every declined/Russian-spelling alias already registered there). Returns
    (value: Decimal, unit: str, start: int, end: int) — the character span
    in `text` this phrase occupies — or None if no such phrase exists.
    Never guesses beyond an exact number-word + exact unit-word match."""
    if not isinstance(text, str) or not text.strip():
        return None
    tokens = _tokenize_with_spans(text)
    cleaned = [_clean_word_number_token(t[0]) for t in tokens]

    for i, w in enumerate(cleaned):
        if w in _HALF_QUANTITY_WORDS and i + 1 < len(cleaned):
            unit = _UNIT_ALIASES.get(cleaned[i + 1])
            if unit:
                return Decimal("0.5"), unit, tokens[i][1], tokens[i + 1][2]

    i = 0
    while i < len(cleaned):
        value, next_i = _consume_word_number(cleaned, i)
        if value is not None and next_i < len(cleaned):
            unit = _UNIT_ALIASES.get(cleaned[next_i])
            if unit:
                return Decimal(value), unit, tokens[i][1], tokens[next_i][2]
        i += 1
    return None


def parse_word_money_amount(text, require_currency_marker=True):
    """Find the FIRST word-number PLN money phrase in `text`: a whole-part
    number-word run (0-999), a "злот..." currency word (either right after
    the whole part, e.g. "дванадцять злотих"/"п'ятдесят один злотий двадцять
    три гроші", or right after the fractional part, e.g. "чотири дев'яносто
    дев'ять злотих" — both orders appear in real speech), an optional
    fractional-part number-word run (0-99, grosze), and an optional explicit
    grosze word ("гроші"/"копійк...") right after that fractional part.

    `require_currency_marker=True` (the default, for standalone use) rejects
    a bare "N N" two-number-group phrase with no currency/grosze word
    anywhere — too ambiguous outside a context that already established
    "this is a price" (see normalize_word_number_measurements's own
    "за"-splitting, which passes False there for exactly that reason, so a
    colloquial "п'ять сорок дев'ять" — "5.49" with no "злотих" word at all —
    is still trusted once "за" already said what follows is a price).

    Returns (amount: Decimal, start: int, end: int) or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    tokens = _tokenize_with_spans(text)
    cleaned = [_clean_word_number_token(t[0]) for t in tokens]

    i = 0
    while i < len(cleaned):
        whole, after_whole = _consume_word_number(cleaned, i)
        if whole is None:
            i += 1
            continue
        end_i = after_whole
        saw_currency = False
        if end_i < len(cleaned) and cleaned[end_i].startswith(_CURRENCY_WORD_PREFIX):
            saw_currency = True
            end_i += 1

        fraction = 0
        saw_fraction = False
        frac_value, after_frac = _consume_word_number(cleaned, end_i)
        if frac_value is not None and frac_value < 100:
            fraction = frac_value
            saw_fraction = True
            end_i = after_frac
            if not saw_currency and end_i < len(cleaned) and cleaned[end_i].startswith(_CURRENCY_WORD_PREFIX):
                saw_currency = True
                end_i += 1
            if end_i < len(cleaned) and cleaned[end_i] in _GROSZE_WORDS:
                end_i += 1

        if not saw_currency and not saw_fraction:
            i += 1
            continue
        if require_currency_marker and not saw_currency:
            i += 1
            continue

        amount = Decimal(whole) + (Decimal(fraction) / Decimal(100) if saw_fraction else Decimal(0))
        return amount, tokens[i][1], tokens[end_i - 1][2]
    return None


def _format_word_money_amount(amount):
    """Comma-decimal "4,99 zł" display, same convention as expenses.py's
    own _format_expense_amount — duplicated here as one tiny pure line
    rather than importing expenses.py (which itself imports database.py;
    quantities.py stays the dependency-free leaf module its own top-of-file
    docstring already commits to)."""
    return f"{amount.quantize(Decimal('0.01')):.2f}".replace(".", ",") + " zł"


def normalize_word_number_measurements(text):
    """Deterministically rewrite Ukrainian/Russian word-number quantity and
    money phrases in `text` into the SAME digit+unit/digit+currency shapes
    the existing numeric pipeline already understands ("Тестове молоко один
    літр за чотири дев'яносто дев'ять злотих" -> "Тестове молоко 1 л за
    4,99 zł") — see this section's own module comment for the full
    reasoning. "за" splits the quantity search zone (everything before it)
    from the money search zone (everything after it); the money search only
    relaxes its currency-word requirement inside that zone — a bare "п'ять
    сорок дев'ять" two-number shape is only trusted as a price once "за"
    already said so. With no "за" at all, both searches run over the whole
    text, and the money search still requires an explicit currency/grosze
    word (never guessed from two bare numbers alone).

    Returns `text` UNCHANGED if neither phrase is found — safe to call
    unconditionally on every incoming message; a normal digit-only,
    non-household, or already-numeric message round-trips untouched."""
    if not isinstance(text, str) or not text.strip():
        return text

    za_match = _WORD_NUMBER_ZA_RE.search(text)
    if za_match:
        quantity_zone_end = za_match.start()
        price_zone_start = za_match.end()
    else:
        quantity_zone_end = len(text)
        price_zone_start = 0

    result = text

    money = parse_word_money_amount(text[price_zone_start:], require_currency_marker=(za_match is None))
    if money is not None:
        amount, rel_start, rel_end = money
        abs_start, abs_end = price_zone_start + rel_start, price_zone_start + rel_end
        result = result[:abs_start] + _format_word_money_amount(amount) + result[abs_end:]

    quantity = parse_word_quantity(text[:quantity_zone_end])
    if quantity is not None:
        value, unit, q_start, q_end = quantity
        result = result[:q_start] + format_quantity_display(value, unit) + result[q_end:]

    return result
