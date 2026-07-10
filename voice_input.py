"""Voice Input V1 — Telegram voice message transcription via Groq Whisper.

Groq is used ONLY for speech-to-text here — the transcribed text is handed
back to bot.py, which passes it through the EXACT SAME message_dispatcher.
dispatch(...) path a typed text message already goes through. This module
never touches Telegram, the database, Gemini, or any pending-state dict;
it only turns an audio file on disk into a transcript string (or raises a
controlled, already-Ukrainian VoiceInputError bot.py can send as-is).

Provider abstraction: only "groq" is implemented in V1. VOICE_TRANSCRIBER
set to anything else (including the literal "disabled") is treated as
"voice input unavailable" via the same VOICE_DISABLED_MSG a future
provider could reuse without bot.py needing to change.

No import of bot.py, Flask, Telegram, psycopg or any Gemini SDK — every
env var is read once at import time (same convention as bot.py's own
TOKEN/GROQ_API_KEY/GEMINI_API_KEY module-level reads).
"""
import logging
import os

from groq import Groq

logger = logging.getLogger(__name__)


def _env_flag(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off")


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


VOICE_INPUT_ENABLED = _env_flag("VOICE_INPUT_ENABLED", True)
VOICE_TRANSCRIBER = (os.getenv("VOICE_TRANSCRIBER") or "groq").strip().lower()
VOICE_TRANSCRIBER_MODEL = (os.getenv("VOICE_TRANSCRIBER_MODEL") or "whisper-large-v3-turbo").strip()
VOICE_SHOW_TRANSCRIPT = _env_flag("VOICE_SHOW_TRANSCRIPT", True)
VOICE_MAX_SECONDS = _env_int("VOICE_MAX_SECONDS", 60)
VOICE_LANGUAGE = (os.getenv("VOICE_LANGUAGE") or "").strip() or None

# Whisper's `prompt` param only biases transcription vocabulary/style — it
# is never interpreted as an instruction, so this stays short on purpose.
TRANSCRIBE_PROMPT = (
    "Transcribe this Telegram voice message. The user may speak Ukrainian "
    "mixed with Polish, Russian, or English."
)

VOICE_DISABLED_MSG = "Голосові команди зараз вимкнені."
MISSING_API_KEY_MSG = "Голосові команди ще не налаштовані: бракує GROQ_API_KEY."
TRANSCRIBE_FAILED_MSG = "Не вдалося розпізнати голосове. Спробуй ще раз або напиши текстом."


class VoiceInputError(Exception):
    """Controlled voice-input failure — str(e) is already a safe,
    user-facing Ukrainian message bot.py can send to Telegram as-is, never
    a raw provider/network error."""


def _resolve_api_key(api_key):
    return api_key if api_key is not None else os.getenv("GROQ_API_KEY")


def ensure_ready(api_key=None):
    """Raise VoiceInputError immediately if voice input can't run at all —
    disabled, an unconfigured provider, or (for "groq") a missing API key.
    Called by bot.py BEFORE downloading the Telegram voice file, so a
    misconfiguration never wastes a download; transcribe_audio_file also
    calls this itself first, so any other caller is protected the same
    way."""
    if not VOICE_INPUT_ENABLED or VOICE_TRANSCRIBER != "groq":
        raise VoiceInputError(VOICE_DISABLED_MSG)
    if not _resolve_api_key(api_key):
        raise VoiceInputError(MISSING_API_KEY_MSG)


def _extract_transcript_text(response):
    """Groq's SDK returns a Transcription object (`.text`) by default; also
    tolerate a plain string (response_format="text") or a dict, so tests
    can use whichever fake is simplest. Returns None (not "") when NONE of
    the recognized shapes matched at all — the caller logs that as an
    unsupported-response-shape diagnostic, distinct from a shape that was
    recognized but genuinely carried an empty transcript."""
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    if text is not None:
        return text
    if isinstance(response, dict):
        if "text" in response:
            return response.get("text") or ""
        return None
    return None


def _sanitize_error_message(exc, api_key=None):
    """Best-effort scrub of an exception's message before it is ever
    logged — strips the resolved GROQ_API_KEY actually used for this call
    (falling back to the process-wide env var if none was passed) if it
    happens to be echoed back verbatim (e.g. an HTTP client's own debug
    repr) and bounds the length. Server-side logs only; never sent to a
    Telegram user (see TRANSCRIBE_FAILED_MSG, the only string that ever
    reaches the user for any transcription failure)."""
    message = str(exc)
    resolved = api_key or os.getenv("GROQ_API_KEY")
    if resolved:
        message = message.replace(resolved, "***")
    return message[:300]


def transcribe_audio_file(file_path, *, filename=None, api_key=None):
    """Transcribe the audio file at `file_path` (already downloaded to
    local disk by the caller) and return the normalized (stripped)
    transcript string — "" if the provider returned nothing usable, never
    raised for that case (caller decides how to react to an empty
    transcript; see bot.py's own "Не вдалося розпізнати..." reply).

    Raises VoiceInputError (already a safe Ukrainian message) if voice
    input is disabled/unconfigured or the provider call itself fails —
    never lets a raw provider/network exception or API key propagate to
    the caller (see _sanitize_error_message — server-side logs only).

    `filename` defaults to file_path's own basename, so its extension
    (already normalized by bot.py's _download_telegram_voice_to_temp to a
    suffix Groq reliably recognizes, e.g. ".ogg" instead of Telegram's own
    ".oga") is what Groq actually sees in the multipart upload.
    """
    ensure_ready(api_key)
    resolved_key = _resolve_api_key(api_key)
    client = Groq(api_key=resolved_key)

    kwargs = {}
    if VOICE_LANGUAGE:
        kwargs["language"] = VOICE_LANGUAGE

    resolved_filename = filename or os.path.basename(file_path)
    suffix = os.path.splitext(resolved_filename)[1] or "(none)"
    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        file_size = -1

    logger.info(
        "voice_transcription_start: provider=groq model=%s file_suffix=%s file_size_bytes=%d",
        VOICE_TRANSCRIBER_MODEL, suffix, file_size,
    )

    try:
        with open(file_path, "rb") as f:
            response = client.audio.transcriptions.create(
                file=(resolved_filename, f.read()),
                model=VOICE_TRANSCRIBER_MODEL,
                prompt=TRANSCRIBE_PROMPT,
                response_format="json",
                temperature=0,
                **kwargs,
            )
    except Exception as e:
        # Covers a missing/unreadable file (OSError) and every Groq SDK/
        # network/provider error alike — the user only ever sees the same
        # generic TRANSCRIBE_FAILED_MSG either way; the exception class and
        # a sanitized message go server-side only.
        logger.error("voice_transcription_error: %s: %s", type(e).__name__, _sanitize_error_message(e, resolved_key))
        raise VoiceInputError(TRANSCRIBE_FAILED_MSG)

    text = _extract_transcript_text(response)
    if text is None:
        logger.warning("voice_transcription_error: unsupported response shape %s", type(response).__name__)
        return ""

    cleaned = text.strip()
    if cleaned:
        logger.info("voice_transcription_success: transcript_length=%d", len(cleaned))
    else:
        logger.info("voice_transcription_empty")
    return cleaned
