"""Photo Receipt Input V1 — Telegram receipt-photo transcription via Gemini
Vision.

Gemini is used ONLY to read a receipt photo into strict JSON here — the
extracted fields are handed back to bot.py, which (after re-validating
every field in Python) builds the SAME pending_expense preview a typed
"Biedronka 86,40 zł" command would (see expenses.build_receipt_expense_
preview). This module never touches Telegram, the database, or any
pending-state dict; it only turns an image file on disk into a structured
ReceiptCandidate (or raises a controlled, already-Ukrainian PhotoInputError
bot.py can send as-is).

Provider abstraction: only "gemini" is implemented in V1. PHOTO_PROVIDER
set to anything else is treated as "photo input unavailable" via the same
PHOTO_DISABLED_MSG a future provider could reuse without bot.py changing.

No import of bot.py, expenses.py, Flask, Telegram, psycopg or any Groq
SDK — every env var is read once at import time (same convention as
voice_input.py's own TOKEN/GROQ_API_KEY-independent module-level reads).
Category is returned as an abstract `category_hint` ("grocery"/"pharmacy"/
"other"/None) rather than one of expenses.py's own fixed category
strings — bot.py (which already imports both modules) owns the final
mapping, so this module never needs to know expenses.py's category list.
"""
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)


def _env_flag(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off")


def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


PHOTO_INPUT_ENABLED = _env_flag("PHOTO_INPUT_ENABLED", True)
PHOTO_PROVIDER = (os.getenv("PHOTO_PROVIDER") or "gemini").strip().lower()
# gemini-2.5-flash — the same model bot.py's own GEMINI_COOKING_URL already
# uses (the project's existing most-capable, vision-ready Gemini model);
# there is no pre-existing "photo" model default to reuse verbatim, so this
# mirrors that one instead of introducing a third, untested model choice.
PHOTO_RECEIPT_MODEL = (os.getenv("PHOTO_RECEIPT_MODEL") or "gemini-2.5-flash").strip()
PHOTO_MAX_SIZE_MB = _env_float("PHOTO_MAX_SIZE_MB", 8)
PHOTO_SHOW_EXTRACTED = _env_flag("PHOTO_SHOW_EXTRACTED", True)

GEMINI_VISION_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = 30  # seconds; a vision call is slower than a plain text chat call

PHOTO_DISABLED_MSG = "Фото чеків ще не налаштоване."
MISSING_API_KEY_MSG = "Фото чеків ще не налаштоване."
NOT_A_RECEIPT_MSG = "Не схоже на чек. Зараз я вмію обробляти тільки фото чеків."
MALFORMED_MSG = "Не вдалося надійно прочитати чек. Спробуй ще раз або запиши витрату текстом."
MISSING_AMOUNT_MSG = "Я бачу чек, але не зміг надійно знайти суму. Напиши суму, наприклад: 86,40 zł."
LOW_CONFIDENCE_WARNING = "⚠️ Перевір дані — я міг помилитися при розпізнаванні чека."


class PhotoInputError(Exception):
    """Controlled photo-receipt failure — str(e) is already a safe,
    user-facing Ukrainian message bot.py can send to Telegram as-is, never
    a raw provider/network error."""


# Whisper-style narrow, explicit instruction — Gemini's own multimodal
# "read this image" call, not a command the image content could hijack
# (the model only ever returns JSON describing what it saw, never takes
# any action itself).
RECEIPT_PROMPT = (
    "Ти розпізнаєш фото чека з магазину (Польща: Biedronka, Lidl, Żabka, Auchan, Carrefour, Rossmann тощо). "
    "Поверни ТІЛЬКИ валідний JSON, без Markdown і без тексту поза JSON. Ніколи не вигадуй дані, яких не бачиш "
    "на фото.\n"
    "Якщо на фото не чек — is_receipt=false, решту полів залиш null/типовими.\n"
    "Шукай ОСТАТОЧНУ суму до сплати за словами: SUMA, RAZEM, DO ZAPŁATY, TOTAL, KWOTA. Ігноруй знижки, "
    "проміжні суми ПДВ, готівку/решту/авторизацію картки, якщо це явно не підсумкова сума до сплати.\n"
    "Якщо підсумкову суму не видно чітко — total_amount=null, confidence=\"low\" (is_receipt лишається true, "
    "якщо це явно чек).\n"
    "merchant — коротка читабельна назва магазину (напр. «Biedronka»), не повна юридична адреса/ІПН.\n"
    "date — формат YYYY-MM-DD лише якщо чітко видно на фото, інакше null.\n"
    "category — одне з рівно трьох значень: \"grocery\" (продуктовий магазин), \"pharmacy\" (аптека/косметика), "
    "\"other\" (усе інше); якщо не впевнений — \"other\".\n"
    "currency — валюта чека, зазвичай \"PLN\".\n"
    "confidence — \"high\", \"medium\" або \"low\", наскільки ти впевнений у розпізнаних даних.\n"
    "warnings — короткий масив рядків з застереженнями (може бути порожній).\n"
    "line_items — масив окремих ТОВАРІВ, куплених за цим чеком (може бути порожній, якщо позиції нерозбірливі "
    "чи чек їх не показує). Для кожного товару:\n"
    "  - name: коротка назва товару, як на чеку (без кількості/одиниці всередині назви).\n"
    "  - quantity: ЛИШЕ число (напр. \"2\", \"0.5\"), або null якщо кількість нечітка чи не вказана.\n"
    "  - unit: ОДНЕ з рівно п'яти значень — \"шт\", \"кг\", \"г\", \"л\", \"мл\" (переклади польську/іншу "
    "одиницю на ці — напр. \"szt\"→\"шт\", \"kg\"→\"кг\"), або null якщо неясно.\n"
    "  - line_price: сума за ЦЮ позицію (число, як на чеку), або null якщо не видно.\n"
    "НІКОЛИ не додавай у line_items: знижки/rabat/promocja/kupon/voucher, заставу/kaucja, пакет/торбу/"
    "reklamówkę, бонусні бали/картку лояльності, ПДВ/готівку/решту/суму карткою — це не товари, це рядки "
    "оплати чи знижок.\n"
    "ОСОБЛИВО ВАЖЛИВО: якщо той самий товар з'являється на чеку ДРУГИЙ раз як рядок знижки/rabat/promocja "
    "НА ЦЕЙ САМЕ товар (напр. «SER GOUDA» — товар, потім «SER GOUDA» чи «RABAT» з від'ємною сумою нижче) — "
    "це ОДНА позиція, а не дві: не додавай другий рядок у line_items взагалі (навіть з тим самим ім'ям), "
    "і якщо все ж сумніваєшся — постав line_price ВІД'ЄМНИМ числом (напр. \"-1.00\") для рядка знижки, "
    "ніколи не показуй його як окрему кількість товару.\n"
    "Формат відповіді:\n"
    '{"is_receipt": true, "merchant": "Biedronka", "total_amount": "86.40", "currency": "PLN", '
    '"date": "2026-07-10", "category": "grocery", "confidence": "high", "warnings": [], "line_items": '
    '[{"name": "Mleko 2%", "quantity": "2", "unit": "л", "line_price": "8.00"}, '
    '{"name": "Jajka", "quantity": "10", "unit": "шт", "line_price": "12.00"}]}'
)


@dataclass
class ReceiptCandidate:
    """Already-parsed (but not yet business-validated — see
    decide_receipt_outcome) Gemini output. `amount` is a positive Decimal
    or None (never a raw string/float). `line_items` (Receipt V2) is
    always a list — empty for a plain Photo Receipt V1 candidate (every
    existing caller that builds a ReceiptCandidate without passing
    line_items gets this default, so decide_receipt_outcome's original
    "ok"/"missing_amount"/"not_a_receipt" behavior is completely
    unaffected when there are none)."""
    is_receipt: bool = False
    merchant: str = None
    amount: object = None
    currency: str = "PLN"
    date: str = None  # "YYYY-MM-DD" or None
    category_hint: str = None  # "grocery" | "pharmacy" | "other" | None
    confidence: str = "low"
    warnings: list = field(default_factory=list)
    line_items: list = field(default_factory=list)  # [{"name", "quantity_text", "line_price"}, ...]
    # Receipt Debug/Explain V1 — one entry per RAW Gemini line_items row (in
    # original order), regardless of whether it survived parsing/dedup; see
    # _parse_line_items_with_debug's own docstring for the exact shape.
    # Always [] when line_items is [] (nothing to explain), never affects
    # any existing is_receipt/amount/line_items behavior.
    line_item_debug: list = field(default_factory=list)


def _resolve_api_key(api_key):
    return api_key if api_key is not None else os.getenv("GEMINI_API_KEY")


def ensure_ready(api_key=None):
    """Raise PhotoInputError immediately if photo input can't run at all —
    disabled, an unconfigured provider, or (for "gemini") a missing API
    key. Called by bot.py BEFORE downloading the Telegram photo, so a
    misconfiguration never wastes a download; extract_receipt_from_image
    also calls this itself first, so any other caller is protected the
    same way."""
    if not PHOTO_INPUT_ENABLED or PHOTO_PROVIDER != "gemini":
        raise PhotoInputError(PHOTO_DISABLED_MSG)
    if not _resolve_api_key(api_key):
        raise PhotoInputError(MISSING_API_KEY_MSG)


def _sanitize_error_message(exc, api_key=None):
    """Server-side-log-only scrub of an exception's message — strips the
    resolved GEMINI_API_KEY actually used for this call (falling back to
    the process-wide env var if none was passed) if it happens to be
    echoed back verbatim, and bounds the length. Never sent to a Telegram
    user (see MALFORMED_MSG, the only string that ever reaches the user
    for any extraction failure)."""
    message = str(exc)
    resolved = api_key or os.getenv("GEMINI_API_KEY")
    if resolved:
        message = message.replace(resolved, "***")
    return message[:300]


def _call_gemini_vision(image_bytes, mime_type, api_key):
    url = GEMINI_VISION_URL_TEMPLATE.format(model=PHOTO_RECEIPT_MODEL)
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": RECEIPT_PROMPT},
                {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
            ],
        }],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json=payload,
        timeout=GEMINI_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_VALID_CATEGORY_HINTS = {"grocery", "pharmacy", "other"}
_VALID_CONFIDENCE = {"high", "medium", "low"}


def _parse_amount(raw_amount, *, require_positive=True):
    """Parse a Gemini-provided amount into an exact Decimal — never float.
    Accepts comma or dot decimal separators and stray currency text
    (Polish receipts commonly use a comma, e.g. "86,40"), and a leading
    minus sign (a discount/refund row's own negative price). Returns a
    Decimal rounded to 2 places, or None if unparseable — or, when
    `require_positive` (the default, used everywhere except the Receipt
    V2 discount-row sign check below), non-positive too."""
    if raw_amount is None:
        return None
    if isinstance(raw_amount, (int, float)):
        raw_amount = str(raw_amount)
    if not isinstance(raw_amount, str):
        return None
    cleaned = raw_amount.strip().lower()
    cleaned = cleaned.replace("zł", "").replace("zl", "").replace("pln", "")
    cleaned = cleaned.replace(" ", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if require_positive and amount <= 0:
        return None
    return amount.quantize(Decimal("0.01"))


# Receipt V2 — line items. `unit` is restricted to this exact vocabulary
# (the same STRUCTURED_UNITS/aliases bot.py's own item-quantity parsing
# already speaks — see quantities.py) rather than trusting whatever raw
# Polish/other unit word Gemini saw on the receipt; the prompt itself asks
# Gemini to translate into one of these five, so a value outside this set
# is treated as "unit unclear" (quantity_text stays "", which downstream
# normalize_item_quantity(allow_default_unit=True) safely defaults to
# "1 шт." — the exact "safe default with a note" the work order asks for).
_VALID_ITEM_UNITS = {"шт", "кг", "г", "л", "мл"}

# Defense-in-depth against a discount/deposit/bag/loyalty line slipping
# into line_items despite the prompt's own explicit instruction not to
# include them — never trust a single layer of "Gemini was told not to".
# Deliberately a plain case-insensitive substring match (Polish AND
# Ukrainian spellings) rather than an exact-word list, since receipt OCR
# text commonly has no clean word boundaries. Stems (e.g. "promocj",
# "znizk"/"zniżk", "platnos"/"płatnoś") deliberately cover multiple word
# forms (promocja/promocji, zniżka/zniżki, płatność/płatności) without
# listing every inflection.
_NON_INVENTORY_NAME_KEYWORDS = (
    "rabat", "znizk", "zniżk", "знижка", "promocj", "промо", "discount", "kupon", "voucher",
    "kaucja", "застава", "deposit", "depozyt", "zwrot", "korekt",
    "reklamówka", "reklamowka", "torba", "торба", "пакет",
    "punkty", "bonus", "бонус", "lojalnoś", "лояльност",
    "opłata", "oplata", "płatnoś", "platnos",
    "gotówka", "gotowka", "готівка", "reszta", "решта", "karta płat", "картою",
    "vat", "pdv", "пдв", "suma", "razem", "total", "разом", "підсумок",
    "paragon", "rachunek", "чек",
)


def _looks_like_non_inventory_name(name):
    lowered = name.lower()
    return any(keyword in lowered for keyword in _NON_INVENTORY_NAME_KEYWORDS)


def _format_plain_number(value):
    """A Decimal as plain fixed-point text, trailing zeros/dot trimmed —
    never scientific notation (Decimal.normalize() would turn "10.00" into
    "1E+1", which the downstream quantity parser can't read at all)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# Receipt V2.1 — product name normalization. A raw receipt name commonly
# has a package-size token baked in ("SER GOUDA 135g") and/or is plain
# Polish/English in ALL CAPS — neither belongs in a household-friendly
# inventory preview line. `\s*` between the number and unit lets an
# unspaced "135g" match the same as a spaced "135 g"; the lookbehind/
# lookahead require a real boundary on both sides so a number that's part
# of some other word is never touched.
_PACKAGE_SIZE_RE = re.compile(
    r"(?<!\S)(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l)(?=\s|$|[.,;)])", re.IGNORECASE,
)
_PACKAGE_SIZE_UNIT_DISPLAY = {"g": "г", "kg": "кг", "ml": "мл", "l": "л"}


def _strip_package_size(name):
    """Remove the FIRST embedded package-size token (e.g. "135g", "1L",
    "500 ml") from `name`, returning (cleaned_name, package_value,
    package_unit) — package_value/package_unit are both None if no such
    token was found. package_value is an exact Decimal, package_unit is
    already the display unit ("г"/"кг"/"мл"/"л").

    Receipt V2.2: this package size is the MOST RELIABLE quantity signal
    on a receipt line — a receipt commonly prints "SER GOUDA 130g" for a
    variable-weight product but Gemini's own separate quantity/unit fields
    for that same row report a plain PACKAGE COUNT ("1 шт", "2 шт" — how
    many of that package were bought), never the weight itself. See
    _parse_line_item's own docstring for how the two are combined (count
    × package size, e.g. 2 × 130g = 260 г) rather than one silently
    overriding the other."""
    m = _PACKAGE_SIZE_RE.search(name)
    if not m:
        return name, None, None
    cleaned = re.sub(r"\s+", " ", (name[:m.start()] + name[m.end():])).strip()
    package_unit = _PACKAGE_SIZE_UNIT_DISPLAY[m.group(2).lower()]
    package_value = Decimal(m.group(1).replace(",", "."))
    return cleaned, package_value, package_unit


# Deterministic Polish/English -> Ukrainian grocery-word translation,
# applied word-by-word (or as a whole recognized phrase, checked first) —
# never a full machine-translation attempt, just the common grocery
# vocabulary this bot's household actually shops for. A word/phrase NOT in
# either table (a brand name like "BARTEK") is preserved, only its casing
# is normalized (ALL CAPS -> Title Case) — never dropped, never guessed at.
_PHRASE_NAME_MAP = {
    "ser gouda": "Сир Гауда",
}
# Sorted longest-phrase-first so a 2-word phrase is tried before any
# single-word fallback below could shadow part of it.
_PHRASE_NAME_MAP_ITEMS = sorted(
    ((phrase.split(), translation) for phrase, translation in _PHRASE_NAME_MAP.items()),
    key=lambda pair: -len(pair[0]),
)

_WORD_NAME_MAP = {
    "olej": "олія", "czosnek": "часник", "ser": "сир",
    "mleko": "молоко", "jajka": "яйця", "jaja": "яйця",
    "masło": "масло", "maslo": "масло", "chleb": "хліб",
    "bułka": "булка", "bulka": "булка",
    "pomidor": "помідори", "pomidory": "помідори",
    "ogórek": "огірки", "ogorek": "огірки", "ogórki": "огірки", "ogorki": "огірки",
}


def _normalize_product_name(name):
    """Translate known Polish/English grocery words/phrases to Ukrainian,
    word by word, preserving any unrecognized word (a brand name) as-is
    except for ALL-CAPS -> Title Case cleanup — "OLEJ BARTEK" -> "Олія
    Bartek" (translated + preserved brand), "SER GOUDA" -> "Сир Гауда"
    (whole-phrase translation), "CZOSNEK" -> "Часник". Never raises;
    returns `name` unchanged (just whitespace-normalized) if it has no
    recognizable words at all."""
    words = name.split()
    if not words:
        return name
    lowered = [w.lower() for w in words]

    result = []
    i = 0
    n = len(words)
    while i < n:
        matched = False
        for phrase_words, translation in _PHRASE_NAME_MAP_ITEMS:
            plen = len(phrase_words)
            if lowered[i:i + plen] == phrase_words:
                result.append(translation)
                i += plen
                matched = True
                break
        if matched:
            continue
        word, lw = words[i], lowered[i]
        if lw in _WORD_NAME_MAP:
            result.append(_WORD_NAME_MAP[lw])
        elif word.isupper() and len(word) > 1:
            result.append(word.capitalize())
        else:
            result.append(word)
        i += 1

    result[0] = result[0][:1].upper() + result[0][1:]
    return " ".join(result)


# Receipt V2.2 — deterministic category assignment. Checked against the
# already-normalized (translated to Ukrainian) display name, word by word
# (never a substring match — a substring match would misfire on something
# like "десерт" containing "сир"-adjacent letters); this is why it runs
# AFTER _normalize_product_name, not on the raw Polish/English text. Every
# category string here is one of bot.py's own fixed VALID_CATEGORIES
# values verbatim (photo_receipts.py never imports bot.py — see this
# module's own docstring — so these are deliberately duplicated literals,
# not a shared import); an unrecognized product returns None, which
# bot.py's household_router._validate_new_item_op already treats the same
# as "no category given" (falls back to DEFAULT_CATEGORY) — a safe,
# explicitly-allowed default, never a guess dressed up as certainty.
_CATEGORY_BY_WORD = {
    "сир": "Молочне та яйця", "гауда": "Молочне та яйця",
    "молоко": "Молочне та яйця", "масло": "Молочне та яйця",
    "яйця": "Молочне та яйця",
    "часник": "Овочі та зелень",
    "олія": "Інше їстівне",
    "хліб": "Хліб і випічка", "булка": "Хліб і випічка",
    "печиво": "Солодке та снеки",
    "банан": "Фрукти та ягоди", "банани": "Фрукти та ягоди", "фрукти": "Фрукти та ягоди",
    "напої": "Напої", "напій": "Напої", "сік": "Напої", "вода": "Напої",
}


def _categorize_display_name(display_name):
    """Deterministic Receipt V2.2 categorizer — see _CATEGORY_BY_WORD's
    own docstring. Exact whole-word match only (never substring), checked
    against every word of the already-translated Ukrainian display name;
    returns the FIRST matching category in word order, or None if nothing
    in the name is recognized. Never raises."""
    for word in display_name.lower().replace(",", " ").split():
        category = _CATEGORY_BY_WORD.get(word)
        if category:
            return category
    return None


def _debug_row(raw_name=None, normalized_name=None, quantity_text="", line_price=None,
                kept=False, drop_reason=None, category=None, package_size_text=None):
    """One Receipt Debug/Explain V1 row — see ReceiptCandidate.line_item_
    debug's own docstring for the field contract. `dedupe_reason` is never
    set here (always None at this point) — only _dedupe_discount_
    duplicates ever fills it in, once it can see every same-name row
    together. `package_size_text` (Receipt V2.2) is the raw package-size
    token detected in the name (e.g. "130 г"), or None if the row had
    none — purely informational, shown alongside the final quantity_text
    so a "why 260 г?" question can be answered (count × package size)."""
    return {
        "raw_name": raw_name, "normalized_name": normalized_name, "quantity_text": quantity_text,
        "line_price": line_price, "kept": kept, "drop_reason": drop_reason, "dedupe_reason": None,
        "category": category, "package_size_text": package_size_text,
    }


def _parse_line_item(raw, *, debug=False):
    """One raw Gemini line_items entry -> {"name", "quantity_text",
    "line_price", "category"} or None if malformed/blank/obviously not an
    inventory item (see _looks_like_non_inventory_name) OR a discount/
    refund row for a real product — a receipt commonly repeats the EXACT
    SAME product name on its own discount line (e.g. two "SER GOUDA" rows:
    one the real purchase, one "-1,00" knocked off it), with no
    distinguishing keyword at all; only the row's own NEGATIVE price marks
    it as a discount, not a second unit purchased — a name-only keyword
    filter would auto-merge that second row into the real one (1+1 ->
    "2 шт.", the live bug this fixes). A second live bug (a discount row
    that DOESN'T even get a negative price from Gemini — just no price at
    all) is handled one level up, in _parse_line_items' own dedup pass,
    once every row's FINAL normalized name is known.

    `name` is normalized before being returned: any embedded package-size
    token ("135g", "1L", ...) is stripped out (see _strip_package_size),
    and known Polish/English grocery words are translated to Ukrainian
    (see _normalize_product_name); an unrecognized word (a brand name) is
    kept, only its casing is cleaned up.

    `quantity_text` (Receipt V2.2 — see _strip_package_size's own
    docstring for why): when the name carries a package-size token, that
    size is the TRUE unit — Gemini's own separate "quantity" field for
    that row is then a package COUNT, not a weight/volume, so the final
    quantity is count × package size (e.g. "SER GOUDA 130g" with
    quantity=2 -> "260 г", not "2 шт."; quantity blank/missing defaults
    the count to 1 -> "130 г", never "1 шт."). The one exception: if
    Gemini's own unit for the row is ALREADY a weight/volume unit (not a
    plain "шт." count), that's a direct per-row reading (e.g. a scale
    receipt printing the exact weight bought) and is trusted as-is, package
    token only used as a quantity fallback if that reading is itself
    missing. With no package-size token at all, this falls back to
    Gemini's own quantity+unit verbatim, or "" if unclear/missing (a safe
    default handled downstream, never guessed here). Never raises.

    `category` (Receipt V2.2): a deterministic keyword categorization of
    the FINAL display name (see _categorize_display_name) — or None if
    unrecognized, which household_router._validate_new_item_op already
    treats as "no category given" (falls back to the same default every
    other uncategorized item gets), never a fabricated guess.

    debug=True (Receipt Debug/Explain V1) returns (item_or_None, debug_row)
    instead of just item_or_None — every caller outside this module keeps
    using the default (debug=False, plain item_or_None) unchanged; only
    _parse_line_items_with_debug passes debug=True."""
    def _result(item, row):
        return (item, row) if debug else item

    if not isinstance(raw, dict):
        return _result(None, _debug_row(drop_reason="рядок чека має некоректний формат"))
    raw_name = raw.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return _result(None, _debug_row(
            raw_name=raw_name if isinstance(raw_name, str) else None,
            drop_reason="порожня назва товару",
        ))
    raw_name = raw_name.strip()
    if _looks_like_non_inventory_name(raw_name):
        return _result(None, _debug_row(
            raw_name=raw_name,
            line_price=_parse_amount(raw.get("line_price"), require_positive=False),
            drop_reason="схоже на знижку/тару/оплату — не товар",
        ))

    signed_price = _parse_amount(raw.get("line_price"), require_positive=False)
    if signed_price is not None and signed_price < 0:
        return _result(None, _debug_row(
            raw_name=raw_name, line_price=signed_price,
            drop_reason="від'ємна ціна — рядок знижки на цей товар",
        ))

    stripped_name, package_value, package_unit = _strip_package_size(raw_name)
    display_name = _normalize_product_name(stripped_name)
    package_size_text = f"{_format_plain_number(package_value)} {package_unit}" if package_value is not None else None

    quantity_value = _parse_amount(raw.get("quantity"))
    raw_unit_raw = raw.get("unit")
    raw_unit = raw_unit_raw.strip().lower() if isinstance(raw_unit_raw, str) else None
    unit_is_direct_reading = raw_unit in _VALID_ITEM_UNITS and raw_unit != "шт"

    if package_value is not None and not unit_is_direct_reading:
        # Package size is the true unit; Gemini's own quantity here (if
        # any) is a PACKAGE COUNT multiplier, defaulting to 1 (a single
        # package) — never "1 шт."/"2 шт." for a weight/volume product.
        count = quantity_value if quantity_value is not None else Decimal("1")
        total_value = count * package_value
        quantity_text = f"{_format_plain_number(total_value)} {package_unit}"
    elif quantity_value is not None and raw_unit in _VALID_ITEM_UNITS:
        quantity_text = f"{_format_plain_number(quantity_value)} {raw_unit}"
    elif package_value is not None:
        # unit_is_direct_reading was True but quantity_value itself was
        # blank — fall back to the package size alone (count of 1).
        quantity_text = f"{_format_plain_number(package_value)} {package_unit}"
    else:
        quantity_text = ""

    category = _categorize_display_name(display_name)

    item = {
        "name": display_name,
        "quantity_text": quantity_text,
        "line_price": _parse_amount(raw.get("line_price")),
        "category": category,
    }
    return _result(item, _debug_row(
        raw_name=raw_name, normalized_name=display_name, quantity_text=quantity_text,
        line_price=item["line_price"], kept=True, category=category, package_size_text=package_size_text,
    ))


def _pick_best_duplicate(items, indices):
    """Among a group of same-name rows with NO price evidence at all (see
    _dedupe_discount_duplicates), pick the single row to keep: prefer one
    that already carries a real quantity_text (e.g. from an embedded
    package-size token like "135g", see _strip_package_size) over a blank
    one — that's real information about the actual purchased amount, never
    guessed — otherwise just the first row encountered, so the receipt's
    own row order decides ties."""
    for i in indices:
        if items[i]["quantity_text"]:
            return i
    return indices[0]


def _dedupe_discount_duplicates(items, debug_rows=None, debug_index=None):
    """Second layer of duplicate/discount defense (see _parse_line_item's
    own docstring): groups already-parsed items by their FINAL normalized
    name (case-insensitive) — this is what lets "SER GOUDA 135g" (the real
    purchase) and a second "SER GOUDA 135g" discount row that Gemini
    reported with NO price at all (neither positive nor negative, so
    _parse_line_item's own negative-price check never saw anything to
    reject) both resolve to the same "Сир Гауда" key and be recognized as
    duplicates here.

    Within a same-name group, conservative-by-default (never invent a 2nd
    purchased unit without real evidence):
      - at least one row priced AND at least one row priceless: the
        priceless row(s) are dropped — a genuine second unit purchased
        almost always shows its own price too, so a same-name row with NO
        price sitting next to one that DOES have a price is exactly the
        "suspicious metadata" signature of an unlabeled discount/
        correction line, never a real second item.
      - EVERY row in the group priced (two genuinely identical purchases):
        all are kept — quantity 2 is a perfectly normal outcome then, not
        a bug, since a real price on every row IS the strong evidence.
      - EVERY row in the group priceless (zero price evidence either way):
        same-name rows with no price backing either of them are not proof
        of two purchased units either — keep only one (see
        _pick_best_duplicate).

    debug_rows/debug_index (Receipt Debug/Explain V1, both optional): when
    given, debug_rows[debug_index[i]] is the debug row for items[i] — this
    fills in that row's own "dedupe_reason" (and "kept"/"drop_reason" for
    anything dropped here) IN PLACE, purely additive bookkeeping that never
    changes which items are kept or dropped."""
    groups = {}
    for i, item in enumerate(items):
        groups.setdefault(item["name"].strip().lower(), []).append(i)

    def _note(i, dedupe_reason):
        if debug_rows is not None:
            debug_rows[debug_index[i]]["dedupe_reason"] = dedupe_reason

    drop = set()
    for indices in groups.values():
        if len(indices) < 2:
            continue
        priced = [i for i in indices if items[i]["line_price"] is not None]
        unpriced = [i for i in indices if items[i]["line_price"] is None]
        if priced and unpriced:
            drop.update(unpriced)
            for i in unpriced:
                if debug_rows is not None:
                    row = debug_rows[debug_index[i]]
                    row["kept"] = False
                    row["drop_reason"] = "дублікат без ціни поруч із ціновим рядком того ж товару"
                _note(i, "без ціни поруч із ціновим рядком того ж товару — прибрано як ймовірну знижку/корекцію")
            for i in priced:
                _note(i, "рядок з реальною ціною серед дублікатів — залишено")
        elif not priced:
            keep = _pick_best_duplicate(items, indices)
            dropped = [i for i in indices if i != keep]
            drop.update(dropped)
            for i in dropped:
                if debug_rows is not None:
                    row = debug_rows[debug_index[i]]
                    row["kept"] = False
                    row["drop_reason"] = "дублікат без жодної цінової ознаки — залишено інший рядок цього товару"
                _note(i, "без жодної цінової ознаки — залишено лише один рядок цього товару")
            _note(keep, "обрано як основний серед безцінових дублікатів (є кількість/розмір пакування, або перший у чеку)")
        else:
            for i in indices:
                _note(i, "усі дублікати мають реальну ціну — це справжні окремі покупки")

    return [item for i, item in enumerate(items) if i not in drop]


def _parse_line_items_with_debug(raw_items):
    """Receipt Debug/Explain V1: same filtering/dedup as _parse_line_items,
    but also returns a debug_rows list with ONE entry per raw input row (in
    original order), regardless of whether it survived parsing or dedup —
    see _debug_row's own field contract. Returns ([], []) if `raw_items`
    isn't a list at all. `_parse_line_items` (below) is a thin wrapper
    around this that only ever returns the items half, so every existing
    caller/test of `_parse_line_items` is completely unaffected."""
    if not isinstance(raw_items, list):
        return [], []
    items = []
    debug_rows = []
    item_debug_index = []
    for raw in raw_items:
        item, debug_row = _parse_line_item(raw, debug=True)
        debug_rows.append(debug_row)
        if item is not None:
            items.append(item)
            item_debug_index.append(len(debug_rows) - 1)

    final_items = _dedupe_discount_duplicates(items, debug_rows=debug_rows, debug_index=item_debug_index)
    return final_items, debug_rows


def _parse_line_items(raw_items):
    """Every well-formed, inventory-looking entry in `raw_items` — silently
    drops (never rejects the whole receipt for) an individual malformed or
    denylisted entry, since a receipt commonly has many items and one bad
    OCR line/discount row must never block the others. Returns [] if
    `raw_items` itself isn't a list at all. See _dedupe_discount_
    duplicates for the final same-name-collision pass."""
    return _parse_line_items_with_debug(raw_items)[0]


def _normalize_category_hint(raw_category):
    if not isinstance(raw_category, str):
        return None
    normalized = raw_category.strip().lower()
    return normalized if normalized in _VALID_CATEGORY_HINTS else ("other" if normalized else None)


def _parse_receipt_json(raw_text):
    """Parse Gemini's raw text response into a ReceiptCandidate, or None if
    the text isn't valid JSON / isn't a JSON object at all — the caller
    treats None as MALFORMED_MSG, never guessing at partial data."""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    merchant = data.get("merchant")
    merchant = merchant.strip() if isinstance(merchant, str) and merchant.strip() else None

    raw_date = data.get("date")
    date_value = raw_date.strip() if isinstance(raw_date, str) and _DATE_RE.match(raw_date.strip() or "") else None

    confidence = data.get("confidence")
    confidence = confidence.strip().lower() if isinstance(confidence, str) else None
    if confidence not in _VALID_CONFIDENCE:
        confidence = "low"

    raw_warnings = data.get("warnings")
    warnings = [str(w).strip() for w in raw_warnings if str(w).strip()] if isinstance(raw_warnings, list) else []

    line_items, line_item_debug = _parse_line_items_with_debug(data.get("line_items"))

    return ReceiptCandidate(
        is_receipt=bool(data.get("is_receipt")),
        merchant=merchant,
        amount=_parse_amount(data.get("total_amount")),
        currency="PLN",  # V1 only ever shows PLN/zł, regardless of what Gemini reports (see module docstring)
        date=date_value,
        category_hint=_normalize_category_hint(data.get("category")),
        confidence=confidence,
        warnings=warnings,
        line_items=line_items,
        line_item_debug=line_item_debug,
    )


def extract_receipt_from_image(file_path, *, api_key=None):
    """Read the image at `file_path` (already downloaded to local disk by
    the caller) and return a ReceiptCandidate.

    Raises PhotoInputError (already a safe Ukrainian message) if photo
    input is disabled/unconfigured, the file can't be read, the provider
    call itself fails, or the response isn't valid JSON — never lets a raw
    provider/network exception or API key propagate to the caller (see
    _sanitize_error_message — server-side logs only). A well-formed JSON
    response is ALWAYS returned as a ReceiptCandidate, even when is_receipt
    is false or amount is missing — see decide_receipt_outcome for that
    business decision.
    """
    ensure_ready(api_key)
    resolved_key = _resolve_api_key(api_key)
    mime_type = _guess_mime_type(file_path)

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        file_size = -1

    logger.info(
        "photo_receipt_extraction_start: provider=gemini model=%s mime_type=%s file_size_bytes=%d",
        PHOTO_RECEIPT_MODEL, mime_type, file_size,
    )

    try:
        with open(file_path, "rb") as f:
            image_bytes = f.read()
        raw_text = _call_gemini_vision(image_bytes, mime_type, resolved_key)
    except Exception as e:
        # Covers a missing/unreadable file (OSError) and every Gemini
        # HTTP/network/provider error alike — the user only ever sees the
        # same generic MALFORMED_MSG either way; the exception class and a
        # sanitized message go server-side only.
        logger.error("photo_receipt_extraction_error: %s: %s", type(e).__name__, _sanitize_error_message(e, resolved_key))
        raise PhotoInputError(MALFORMED_MSG)

    candidate = _parse_receipt_json(raw_text)
    if candidate is None:
        logger.warning("photo_receipt_extraction_error: malformed JSON response")
        raise PhotoInputError(MALFORMED_MSG)

    if not candidate.is_receipt:
        logger.info("photo_receipt_extraction_empty_or_not_receipt: is_receipt=false")
    elif candidate.amount is None:
        logger.info("photo_receipt_extraction_empty_or_not_receipt: missing amount")
    else:
        logger.info(
            "photo_receipt_extraction_success: confidence=%s has_merchant=%s has_date=%s",
            candidate.confidence, bool(candidate.merchant), bool(candidate.date),
        )
    return candidate


_MIME_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _guess_mime_type(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    return _MIME_BY_SUFFIX.get(ext, "image/jpeg")


def _resolve_expense_date(raw_date, now=None):
    """Normalize an optional "YYYY-MM-DD" string into a real date, never
    in the future (same guard as expenses._validate_expense_date) —
    missing/invalid/future defaults to today, Europe/Warsaw, matching how
    a typed expense command always resolves to a concrete date too."""
    if now is None:
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
    if isinstance(raw_date, str) and _DATE_RE.match(raw_date):
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            return now.date()
        return parsed if parsed <= now.date() else now.date()
    return now.date()


def decide_receipt_outcome(candidate, now=None):
    """Pure business decision over an already-parsed ReceiptCandidate —
    mirrors expenses._validate_expense_router_result's own (kind, payload)
    shape/spirit. Returns one of:
      ("not_a_receipt", None)
      ("missing_amount", None)  -- no usable total AND no usable line items
      ("ok", {"amount": Decimal, "merchant": str, "expense_date": date,
               "category_hint": str|None, "confidence": str, "warnings": [...]})
          -- Photo Receipt V1's original shape, UNCHANGED, used whenever
          there are no line items at all (candidate.line_items empty) —
          every existing caller/test that never sets line_items keeps
          getting exactly this, regardless of any Receipt V2 code below.
      ("ok_with_items", {**same keys as "ok", "line_items": [...],
                          "line_item_debug": [...]})
          -- Receipt V2: a usable total AND 1+ usable line items.
      ("items_only", {"merchant": str, "expense_date": date,
                       "category_hint": str|None, "confidence": str,
                       "warnings": [...], "line_items": [...],
                       "line_item_debug": [...]})
          -- Receipt V2: 1+ usable line items but NO usable total — an
          inventory-only preview, never an invented expense amount.
    `merchant` always falls back to "Чек" when Gemini didn't report one;
    `expense_date` is always a concrete date (see _resolve_expense_date).
    `line_item_debug` (Receipt Debug/Explain V1, only present on the two
    line-items outcomes — the plain "ok" shape stays byte-for-byte
    unchanged for every existing caller) is candidate.line_item_debug
    verbatim, for bot.py to stash on the resulting pending preview so a
    later "чому так?"/"debug чек" request can explain the parse.
    Never raises — a malformed candidate (Gemini JSON that didn't parse at
    all) is PhotoInputError'd by extract_receipt_from_image BEFORE this is
    ever called.
    """
    if not candidate.is_receipt:
        return "not_a_receipt", None

    line_items = candidate.line_items or []
    if candidate.amount is None and not line_items:
        return "missing_amount", None

    base_payload = {
        "merchant": candidate.merchant or "Чек",
        "expense_date": _resolve_expense_date(candidate.date, now=now),
        "category_hint": candidate.category_hint,
        "confidence": candidate.confidence,
        "warnings": candidate.warnings,
    }

    if not line_items:
        return "ok", {**base_payload, "amount": candidate.amount}
    if candidate.amount is not None:
        return "ok_with_items", {
            **base_payload, "amount": candidate.amount, "line_items": line_items,
            "line_item_debug": candidate.line_item_debug,
        }
    return "items_only", {**base_payload, "line_items": line_items, "line_item_debug": candidate.line_item_debug}


# =========================
# RECEIPT DEBUG/EXPLAIN V1 — a short, user-facing Ukrainian explanation of
# what the receipt parser saw for every raw Gemini line_items row and why
# each one ended up kept or dropped (see _parse_line_items_with_debug).
# Shown ONLY on an explicit user request during an active receipt-built
# preview (bot.py owns that gate — see _handle_receipt_debug_request) —
# never sent automatically, never logged in full to Render logs (see
# extract_receipt_from_image's own logging, which stays confidence/counts
# only, no item names/prices). Pure text formatting: no Gemini, no
# Telegram, no state, no image bytes/secrets anywhere in the output.
# =========================
NO_RECEIPT_DEBUG_DATA_MSG = "Немає даних для розбору цього чека."


def format_receipt_debug_summary(debug_rows):
    """`debug_rows` is a ReceiptCandidate.line_item_debug list (or the same
    list carried on a pending preview's own "receipt_debug" key) — one
    entry per RAW Gemini line_items row, in original receipt order. Always
    returns a non-empty string; NO_RECEIPT_DEBUG_DATA_MSG for an empty/
    missing list (the caller decides whether that's even reachable —
    bot.py's own gate already gives a more specific "нічого розбирати"
    reply before ever calling this with an empty list)."""
    if not debug_rows:
        return NO_RECEIPT_DEBUG_DATA_MSG

    lines = [f"🧾 Розбір чека — Gemini повернув {len(debug_rows)} рядків:", ""]
    for i, row in enumerate(debug_rows, start=1):
        raw_name = row.get("raw_name") or "(без назви)"
        entry = f'{i}. «{raw_name}»'
        if row.get("kept"):
            normalized = row.get("normalized_name") or raw_name
            quantity_text = row.get("quantity_text") or "кількість не вказана"
            price = row.get("line_price")
            price_text = f"{price} zł" if price is not None else "ціна не вказана"
            category = row.get("category") or "категорія не визначена"
            entry += f" → {normalized}, {quantity_text}, {price_text}, категорія: {category} ✅ додано"
            package_size_text = row.get("package_size_text")
            if package_size_text:
                entry += f"\n   ↳ розмір пакування на чеку: {package_size_text}"
        else:
            entry += f" ❌ відкинуто: {row.get('drop_reason') or 'причина невідома'}"
        dedupe_reason = row.get("dedupe_reason")
        if dedupe_reason:
            entry += f"\n   ↳ дублікат: {dedupe_reason}"
        lines.append(entry)

    return "\n".join(lines)
