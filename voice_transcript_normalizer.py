"""Voice Transcript Normalizer V1 (+ number-preservation fix).

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
by-the-user.

LIVE REGRESSION FIX: a raw Whisper stutter ("for 60 for 60" — the same
amount spoken/recognized twice in a row) made _numbers_preserved reject an
otherwise CORRECT normalization that naturally collapsed it to one "60"
(exactly what a natural Ukrainian retelling would do), silently falling
back to the raw, still-mixed-language transcript — which looked from the
outside like normalization "did nothing". _numbers_preserved now tolerates
ONLY an immediately-adjacent repeat of the SAME number (a handful of words
apart, nothing else numeric between them) as a stutter, never a genuine
value change — see _collapse_adjacent_number_stutters. As a second,
independent safety net (not just tolerance), a first attempt that still
fails the number check gets exactly ONE retry with a stricter prompt
before falling back to the raw transcript — see normalize()'s own
docstring for the full status/retry contract.

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

# Diagnostic statuses normalize() distinguishes internally and logs (never
# the transcript content itself — see normalize()'s own logging calls).
STATUS_SKIPPED_NOT_UK = "skipped_not_uk"
STATUS_SKIPPED_NO_LATIN = "skipped_no_latin"
STATUS_CHANGED = "changed"
STATUS_UNCHANGED = "unchanged"
STATUS_GEMINI_ERROR = "gemini_error"
STATUS_INVALID_JSON = "invalid_json"
STATUS_NUMBERS_MISMATCH = "numbers_mismatch"
STATUS_FALLBACK_RAW = "fallback_raw"

_STUTTER_RULE_TEXT = (
    "Якщо якесь число випадково повторюється двічі поспіль через затинання розпізнавання мовлення (напр. "
    "«60 for 60», «60 60») — це ОДНЕ число, залиш його один раз природною українською; це НЕ вважається "
    "втратою чи зміною числа."
)

VOICE_NORMALIZER_PROMPT = (
    "Ти — редактор транскриптів голосових повідомлень для приватного домашнього Telegram-бота. "
    "Транскрипт нижче розпізнано Whisper з голосового повідомлення користувача, який говорив УКРАЇНСЬКОЮ, "
    "але Whisper міг помилково вставити фрагменти англійською, польською чи російською мовою всередину "
    "українського тексту. Твоя ЄДИНА задача — переписати транскрипт природною українською мовою, зберігши "
    "ТОЧНО той самий зміст.\n\n"
    "СУВОРІ ПРАВИЛА:\n"
    "1. НІКОЛИ не змінюй жодне число, суму, кількість чи відсоток — кожне число має залишитись ТОЧНО таким "
    "самим, як у оригіналі. " + _STUTTER_RULE_TEXT + "\n"
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

# Requirement 4's retry prompt — used ONLY for the one retry attempt after a
# first response failed _numbers_preserved (see normalize()). Repeats the
# same base rules (Gemini has no memory of the failed attempt — each call is
# stateless) plus an explicit, blunt callout of the exact failure mode.
VOICE_NORMALIZER_RETRY_PROMPT = (
    VOICE_NORMALIZER_PROMPT
    + "\n\nПОПЕРЕДНЯ СПРОБА змінила або загубила якесь число з оригіналу. Спробуй ще раз — переконайся, що "
    "КОЖНЕ число з оригінального тексту зустрічається в результаті РІВНО стільки ж разів, скільки в "
    "оригіналі (рахуй уважно, включно з будь-якими повтореннями). " + _STUTTER_RULE_TEXT + " Не вигадуй, "
    "не округлюй і не видаляй жодне число."
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


def _collapse_adjacent_number_stutters(text, max_gap_words=2):
    """Return every number token in `text`, IN ORDER, with an immediately-
    repeated stutter of the SAME number collapsed to one occurrence — e.g.
    "for 60 for 60" -> ["60"] instead of ["60", "60"]. Only collapses when
    the repeat is the very next number token seen (nothing else numeric
    between the two) AND at most `max_gap_words` words separate them (a raw
    Whisper stutter is adjacent — "60 for 60" has one word, "for", between
    them; two genuinely separate mentions of the same amount elsewhere in a
    longer message are never this close). A DIFFERENT number appearing
    between two occurrences of a repeated one (e.g. "3300 ... 150 ...
    3300") is never treated as a stutter — each occurrence is kept.
    """
    tokens = [(m.start(), m.end(), m.group()) for m in _NUMBER_RE.finditer(text or "")]
    collapsed = []
    prev_end = None
    prev_token = None
    for start, end, token in tokens:
        if prev_token == token and prev_end is not None:
            gap_words = len(text[prev_end:start].split())
            if gap_words <= max_gap_words:
                prev_end = end
                continue
        collapsed.append(token)
        prev_end = end
        prev_token = token
    return collapsed


def _numbers_preserved(raw_transcript, normalized_transcript):
    """True if every number token in `raw_transcript` appears, unchanged,
    in `normalized_transcript` — order-independent, same multiset, EXCEPT
    that an immediately-adjacent stutter of the same number (see
    _collapse_adjacent_number_stutters — a raw Whisper artifact, not a real
    repeated amount) is collapsed to one occurrence on BOTH sides before
    comparing. Symmetric on purpose: Gemini may legitimately either
    collapse a stutter into one natural mention OR preserve it verbatim
    (both are "correct" — see the retry prompt's own wording) — collapsing
    only the raw side would wrongly reject the verbatim-preserved case.
    This is the one hard safety invariant this module enforces: a genuine
    amount/quantity/discount a downstream purchase-preview calculation
    would trust as literally-typed-by-the-user must never be silently
    altered — but a recognizer's own stutter must never force a fallback to
    a still-mixed-language transcript either.
    """
    raw_numbers = sorted(_collapse_adjacent_number_stutters(raw_transcript or ""))
    normalized_numbers = sorted(_collapse_adjacent_number_stutters(normalized_transcript or ""))
    return raw_numbers == normalized_numbers


def _attempt_gemini_normalize(transcript, prompt):
    """One Gemini normalization attempt. Returns (normalized_text, status)
    where status is "ok" (normalized_text is a valid, number-preserving
    result — possibly identical to `transcript`, see rule 6) or one of
    STATUS_GEMINI_ERROR / STATUS_INVALID_JSON / STATUS_NUMBERS_MISMATCH
    (normalized_text is None in every non-"ok" case). Never raises."""
    try:
        raw_response = _bot.call_gemini([{"role": "user", "content": transcript}], prompt, temperature=0.0)
    except Exception as e:
        logger.warning("voice_normalize_attempt: status=%s error=%s", STATUS_GEMINI_ERROR, type(e).__name__)
        return None, STATUS_GEMINI_ERROR
    if not raw_response:
        return None, STATUS_GEMINI_ERROR
    try:
        data = _extract_json(raw_response)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, STATUS_INVALID_JSON
    if not isinstance(data, dict):
        return None, STATUS_INVALID_JSON
    normalized = data.get("normalized")
    if not isinstance(normalized, str) or not normalized.strip():
        return None, STATUS_INVALID_JSON
    normalized = normalized.strip()
    if not _numbers_preserved(transcript, normalized):
        return None, STATUS_NUMBERS_MISMATCH
    return normalized, "ok"


def normalize(transcript, language):
    """Best-effort mixed-language voice transcript normalization. Returns
    (text, changed, status):
      `text` — the normalized transcript on success, or `transcript`
          UNCHANGED on any skip/failure.
      `changed` — True only when a Gemini-normalized result (different
          from the raw transcript) was actually used.
      `status` — one of the STATUS_* constants above, for logging/
          diagnostics only (language + status + changed are logged here —
          never the transcript content, matching voice_input.py's own
          privacy posture).

    Never raises. Failure/skip handling:
      - language != "uk", or a blank transcript -> STATUS_SKIPPED_NOT_UK.
      - "uk" but no Latin-script content at all -> STATUS_SKIPPED_NO_LATIN
        (needs_normalization() says no — no Gemini call).
      - First Gemini attempt fails ONLY the number-preservation check
        (STATUS_NUMBERS_MISMATCH) -> ONE retry with a stricter prompt
        (VOICE_NORMALIZER_RETRY_PROMPT). If the retry succeeds, its result
        is used; if it also fails, falls back to the raw transcript
        (STATUS_FALLBACK_RAW).
      - Any other first-attempt failure (no Gemini response, malformed/
        empty JSON) falls back to the raw transcript directly, no retry —
        a retry with a stricter NUMBER-preservation prompt would not help
        a response that never validly arrived at all.
    """
    if language != "uk":
        return transcript, False, STATUS_SKIPPED_NOT_UK
    if not isinstance(transcript, str) or not transcript.strip():
        return transcript, False, STATUS_SKIPPED_NOT_UK
    if not _LATIN_WORD_RE.search(transcript):
        return transcript, False, STATUS_SKIPPED_NO_LATIN

    normalized, status = _attempt_gemini_normalize(transcript, VOICE_NORMALIZER_PROMPT)

    if status == STATUS_NUMBERS_MISMATCH:
        normalized, retry_status = _attempt_gemini_normalize(transcript, VOICE_NORMALIZER_RETRY_PROMPT)
        status = "ok" if retry_status == "ok" else STATUS_FALLBACK_RAW

    if status != "ok":
        logger.warning("voice_normalize: language=%s status=%s changed=False", language, status)
        return transcript, False, status

    changed = normalized != transcript
    final_status = STATUS_CHANGED if changed else STATUS_UNCHANGED
    logger.info("voice_normalize: language=%s status=%s changed=%s", language, final_status, changed)
    return normalized, changed, final_status
