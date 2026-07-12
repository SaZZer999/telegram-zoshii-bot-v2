"""Voice Transcript Normalizer V1.

Groq Whisper occasionally mixes English/Polish/Russian fragments into an
otherwise-Ukrainian voice transcript even when the user has selected 🇺🇦
Українська as their voice language (see voice_input.py's own `language`
hint — a hint, not a hard guarantee). Left as-is, those raw fragments
("We bought a komod...") get handed straight to household_router/
mini_action_planner as if the user had typed them, and can produce wrong
preview items. This module rewrites the WHOLE transcript into natural
Ukrainian, with numbers/amounts/quantities verified unchanged, before it
ever reaches the dispatcher.

Flow (see bot.py's _handle_voice_message, the only caller): raw Whisper
transcript -> normalize(transcript, language) -> the SAME
message_dispatcher.dispatch(...) call a typed message already goes through,
and the SAME "🎙️ Розпізнав:" echo — both now see the NORMALIZED text, never
the raw one (per this module's own work order: "Planner should receive
normalized transcript, not raw transcript").

Scope V1: only runs when the user's selected voice language is exactly
"uk" AND the transcript actually contains Latin-script text (needs_
normalization's cheap pre-gate) — clean Ukrainian speech, or any other
selected language, never costs an extra Gemini call. Gemini is asked for
STRICT JSON only, exactly like mini_action_planner.py's own classifier;
Python then verifies EVERY number in the raw transcript still appears,
unchanged, in the normalized one (_numbers_preserved) — a fabricated or
altered amount is the one failure mode this module can never risk, since
purchase-preview math downstream trusts these numbers as literally-typed-
by-the-user. Any failure at all (no Gemini, malformed JSON, empty result,
a number mismatch) safely falls back to the RAW transcript, never raises,
never blocks the voice pipeline.

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — `configure(bot_module)` injects bot.py's own `call_gemini` at
runtime, the same DI pattern mini_action_planner.py/household_router.py
already use for the exact same reason (patch.object(bot, "call_gemini", ...)
in tests must keep affecting this module's own Gemini call).
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

_bot = None


def configure(bot_module):
    global _bot
    _bot = bot_module


# A run of 3+ Latin letters is a cheap, high-recall signal that Whisper
# mixed in a non-Ukrainian word/phrase ("komod", "We bought") — 1-2 letter
# runs are left alone (unit abbreviations like "l"/"kg" already read fine
# in a Ukrainian sentence and aren't worth a Gemini call by themselves).
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")

# Matches a number token exactly the same way household_router.py's own
# _amount_literally_in_text/_NUMBER_TOKEN_RE do (integer or decimal with a
# dot/comma separator) — kept as an independent copy on purpose: this
# module must never import household_router (voice_input.py's own "never
# import bot.py" reasoning applies the same way here), and the two checks
# serve different call sites that should never need to change together.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")

VOICE_NORMALIZER_PROMPT = (
    "Ти — редактор транскриптів голосових повідомлень для приватного домашнього Telegram-бота. "
    "Транскрипт нижче розпізнано Whisper з голосового повідомлення користувача, який говорив УКРАЇНСЬКОЮ, "
    "але Whisper міг помилково вставити фрагменти англійською, польською чи російською мовою всередину "
    "українського тексту. Твоя ЄДИНА задача — переписати транскрипт природною українською мовою, зберігши "
    "ТОЧНО той самий зміст.\n\n"
    "СУВОРІ ПРАВИЛА:\n"
    "1. НІКОЛИ не змінюй жодне число, суму, кількість чи відсоток — кожне число має залишитись ТОЧНО таким "
    "самим, як у оригіналі.\n"
    "2. НІКОЛИ не вигадуй нових покупок, товарів, людей чи фактів, яких немає в оригіналі.\n"
    "3. НІКОЛИ не прибирай невизначеність — якщо оригінал незрозумілий чи неоднозначний, перепиши його так "
    "само незрозуміло чи неоднозначно українською, не додавай ясності, якої там не було.\n"
    "4. Перекладай ЛИШЕ фрагменти іншими мовами, які явно є частиною ТОГО САМОГО українського повідомлення "
    "(напр. «We bought a komod» всередині української розповіді). Якщо ВЕСЬ транскрипт написаний іншою "
    "мовою (не українською взагалі, жодного українського слова) — поверни його БЕЗ ЗМІН.\n"
    "5. Нормалізуй ці типові побутові слова в українську форму, хоч би якою мовою чи транслітерацією вони "
    "траплялись: komod/komoda → «комод»; auto-carsel/auto carsel/auto car seat/car seat → «автокрісло»; "
    "baby bed/łóżeczko/ліжечку → «дитяче ліжечко»; stroller/wózek → «візочок».\n"
    "6. Якщо транскрипт УЖЕ повністю українською і зрозумілий — поверни його ТОЧНО без жодних змін.\n\n"
    "Відповідай ТІЛЬКИ валідним JSON, без Markdown і без тексту поза JSON: "
    "{\"normalized\": \"...\"}"
)


def needs_normalization(transcript, language):
    """True if `transcript` is worth sending to Gemini for normalization —
    `language` must be exactly "uk" (the user's saved voice-language
    preference — see bot.py's _resolve_voice_language, the only source of
    this value) AND the transcript contains at least one run of 3+ Latin
    letters. Clean Ukrainian speech, an unset/other language preference, or
    a blank transcript never need a Gemini call at all."""
    if language != "uk":
        return False
    if not isinstance(transcript, str) or not transcript.strip():
        return False
    return bool(_LATIN_WORD_RE.search(transcript))


def _extract_json(raw):
    cleaned = (raw or "").strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    return json.loads(cleaned)


def _numbers_preserved(raw_transcript, normalized_transcript):
    """True if every number token in `raw_transcript` appears, unchanged,
    in `normalized_transcript` (order-independent, but the exact same
    multiset — e.g. two separate "60"s in the raw text must both still be
    present). The one hard safety invariant this module enforces: amounts/
    quantities/discounts a downstream purchase-preview calculation would
    trust as literally-typed-by-the-user must never be silently altered by
    a translation pass."""
    return sorted(_NUMBER_RE.findall(raw_transcript or "")) == sorted(_NUMBER_RE.findall(normalized_transcript or ""))


def normalize(transcript, language):
    """Best-effort mixed-language voice transcript normalization. Returns
    (text, changed): `text` is the normalized transcript on success, or
    `transcript` UNCHANGED on any failure/skip; `changed` is True only when
    a Gemini-normalized result was actually used. Never raises — every
    failure (needs_normalization() says no, no Gemini response, malformed
    JSON, empty/blank result, a number that didn't survive) falls back to
    the raw transcript, exactly as if this module didn't run at all.
    """
    if not needs_normalization(transcript, language):
        return transcript, False
    try:
        raw_response = _bot.call_gemini(
            [{"role": "user", "content": transcript}], VOICE_NORMALIZER_PROMPT, temperature=0.0,
        )
        if not raw_response:
            return transcript, False
        data = _extract_json(raw_response)
        if not isinstance(data, dict):
            return transcript, False
        normalized = data.get("normalized")
        if not isinstance(normalized, str) or not normalized.strip():
            return transcript, False
        normalized = normalized.strip()
        if not _numbers_preserved(transcript, normalized):
            logger.warning("voice_normalize_number_mismatch: falling back to raw transcript")
            return transcript, False
        return normalized, True
    except Exception as e:
        # Covers a Gemini/network failure and any malformed-response shape
        # alike — the caller only ever sees a safe (transcript, False)
        # fallback either way; the exception class goes server-side only,
        # never the transcript content (same privacy posture as voice_input.
        # py's own logging, which never logs transcript text).
        logger.warning("voice_normalize_error: %s", type(e).__name__)
        return transcript, False
