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


def _parse_line_item(raw):
    """One raw Gemini line_items entry -> {"name", "quantity_text",
    "line_price"} or None if malformed/blank/obviously not an inventory
    item (see _looks_like_non_inventory_name) OR a discount/refund row for
    a real product — a receipt commonly repeats the EXACT SAME product
    name on its own discount line (e.g. two "SER GOUDA" rows: one the real
    purchase, one "-1,00" knocked off it), with no distinguishing keyword
    at all; only the row's own NEGATIVE price marks it as a discount, not
    a second unit purchased — a name-only keyword filter would auto-merge
    that second row into the real one (1+1 -> "2 шт.", the live bug this
    fixes). `quantity_text` is either a clean "<number> <unit>" string
    (only when BOTH parsed cleanly) or "" (an unclear quantity — safely
    defaults downstream, never guessed here). Never raises."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if _looks_like_non_inventory_name(name):
        return None

    signed_price = _parse_amount(raw.get("line_price"), require_positive=False)
    if signed_price is not None and signed_price < 0:
        return None

    quantity_text = ""
    quantity_value = _parse_amount(raw.get("quantity"))
    raw_unit = raw.get("unit")
    if quantity_value is not None and isinstance(raw_unit, str) and raw_unit.strip().lower() in _VALID_ITEM_UNITS:
        quantity_text = f"{_format_plain_number(quantity_value)} {raw_unit.strip().lower()}"

    return {
        "name": name,
        "quantity_text": quantity_text,
        "line_price": _parse_amount(raw.get("line_price")),
    }


def _parse_line_items(raw_items):
    """Every well-formed, inventory-looking entry in `raw_items` — silently
    drops (never rejects the whole receipt for) an individual malformed or
    denylisted entry, since a receipt commonly has many items and one bad
    OCR line/discount row must never block the others. Returns [] if
    `raw_items` itself isn't a list at all."""
    if not isinstance(raw_items, list):
        return []
    items = []
    for raw in raw_items:
        item = _parse_line_item(raw)
        if item is not None:
            items.append(item)
    return items


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

    return ReceiptCandidate(
        is_receipt=bool(data.get("is_receipt")),
        merchant=merchant,
        amount=_parse_amount(data.get("total_amount")),
        currency="PLN",  # V1 only ever shows PLN/zł, regardless of what Gemini reports (see module docstring)
        date=date_value,
        category_hint=_normalize_category_hint(data.get("category")),
        confidence=confidence,
        warnings=warnings,
        line_items=_parse_line_items(data.get("line_items")),
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
      ("ok_with_items", {**same keys as "ok", "line_items": [...]})
          -- Receipt V2: a usable total AND 1+ usable line items.
      ("items_only", {"merchant": str, "expense_date": date,
                       "category_hint": str|None, "confidence": str,
                       "warnings": [...], "line_items": [...]})
          -- Receipt V2: 1+ usable line items but NO usable total — an
          inventory-only preview, never an invented expense amount.
    `merchant` always falls back to "Чек" when Gemini didn't report one;
    `expense_date` is always a concrete date (see _resolve_expense_date).
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
        return "ok_with_items", {**base_payload, "amount": candidate.amount, "line_items": line_items}
    return "items_only", {**base_payload, "line_items": line_items}
