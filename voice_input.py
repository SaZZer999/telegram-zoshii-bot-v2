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
import os

from groq import Groq


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

# Never forces command execution/explanation out of the model — Whisper
# transcription prompts are a hint, not an instruction the audio content
# could override, but kept narrow and explicit anyway.
TRANSCRIBE_PROMPT = (
    "Transcribe this Telegram voice message exactly. The user may speak "
    "Ukrainian mixed with Polish, Russian, or English. Return only the "
    "transcript text. Do not execute commands. Do not explain."
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
    can use whichever fake is simplest."""
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    if text is not None:
        return text
    if isinstance(response, dict):
        return response.get("text") or ""
    return ""


def transcribe_audio_file(file_path, *, filename=None, api_key=None):
    """Transcribe the audio file at `file_path` (already downloaded to
    local disk by the caller) and return the normalized (stripped)
    transcript string — "" if the provider returned nothing usable, never
    raised for that case (caller decides how to react to an empty
    transcript; see bot.py's own "Не вдалося розпізнати..." reply).

    Raises VoiceInputError (already a safe Ukrainian message) if voice
    input is disabled/unconfigured or the provider call itself fails —
    never lets a raw provider/network exception or API key propagate to
    the caller.
    """
    ensure_ready(api_key)
    resolved_key = _resolve_api_key(api_key)
    client = Groq(api_key=resolved_key)

    kwargs = {}
    if VOICE_LANGUAGE:
        kwargs["language"] = VOICE_LANGUAGE

    try:
        with open(file_path, "rb") as f:
            response = client.audio.transcriptions.create(
                file=(filename or os.path.basename(file_path), f.read()),
                model=VOICE_TRANSCRIBER_MODEL,
                prompt=TRANSCRIBE_PROMPT,
                **kwargs,
            )
    except OSError:
        # The file itself is missing/unreadable — same controlled message
        # as a provider failure, never a raw traceback to the user.
        raise VoiceInputError(TRANSCRIBE_FAILED_MSG)
    except Exception:
        raise VoiceInputError(TRANSCRIBE_FAILED_MSG)

    return (_extract_transcript_text(response) or "").strip()
