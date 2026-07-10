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
    "Формат відповіді:\n"
    '{"is_receipt": true, "merchant": "Biedronka", "total_amount": "86.40", "currency": "PLN", '
    '"date": "2026-07-10", "category": "grocery", "confidence": "high", "warnings": []}'
)


@dataclass
class ReceiptCandidate:
    """Already-parsed (but not yet business-validated — see
    decide_receipt_outcome) Gemini output. `amount` is a positive Decimal
    or None (never a raw string/float)."""
    is_receipt: bool = False
    merchant: str = None
    amount: object = None
    currency: str = "PLN"
    date: str = None  # "YYYY-MM-DD" or None
    category_hint: str = None  # "grocery" | "pharmacy" | "other" | None
    confidence: str = "low"
    warnings: list = field(default_factory=list)


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


def _parse_amount(raw_amount):
    """Parse a Gemini-provided amount into an exact Decimal — never float.
    Accepts comma or dot decimal separators and stray currency text
    (Polish receipts commonly use a comma, e.g. "86,40"). Returns a
    Decimal rounded to 2 places, or None if unparseable/non-positive."""
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
    if amount <= 0:
        return None
    return amount.quantize(Decimal("0.01"))


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
      ("missing_amount", None)
      ("ok", {"amount": Decimal, "merchant": str, "expense_date": date,
               "category_hint": str|None, "confidence": str, "warnings": [...]})
    `merchant` in the "ok" payload always falls back to "Чек" when Gemini
    didn't report one; `expense_date` is always a concrete date (see
    _resolve_expense_date). Never raises — a malformed candidate (Gemini
    JSON that didn't parse at all) is PhotoInputError'd by
    extract_receipt_from_image BEFORE this is ever called.
    """
    if not candidate.is_receipt:
        return "not_a_receipt", None
    if candidate.amount is None:
        return "missing_amount", None
    return "ok", {
        "amount": candidate.amount,
        "merchant": candidate.merchant or "Чек",
        "expense_date": _resolve_expense_date(candidate.date, now=now),
        "category_hint": candidate.category_hint,
        "confidence": candidate.confidence,
        "warnings": candidate.warnings,
    }
