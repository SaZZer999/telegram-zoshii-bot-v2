"""Inventory Multi-Target Actions V1 — safe batch consume/delete of SEVERAL
named inventory positions from one text/voice command ("Видали одне
автокрісло, печиво і один хліб", "Спиши 200 г сиру, 1 л молока та 2
сосиски").

Live bug this fixes: with active/global inventory context and stock
`Автокрісло — 2 шт.` / `Печиво — 1 кг` / `Хліб — 2 шт.`, the voice command
"Видали одне автокрісло, печиво і один хліб" got "Не знайшов такого запису в
запасах." — inventory_admin_route ran first, its single-target parser
(inventory.parse_inventory_delete_request) saw the leading "одне" and folded
the ENTIRE remainder ("автокрісло, печиво і один хліб") into one fake
product name, found no match, and claimed the message before the
saved_list_router's own multi-item Gemini router ever got a chance.

This module owns ONLY parsing/schema — no Telegram, no database, no pending
state (same "no bot.py import" posture as action_planner.py/shopping_action_
planner.py; configure(bot_module) injects bot.py's own call_gemini at
runtime for the same patch.object(bot, "call_gemini", ...) reasoning those
modules already document). bot.py's `_try_inventory_multi_target` (see its
own docstring) does the live-inventory candidate resolution, all-or-nothing
validation, pending_global_household preview and confirm/cancel/undo reuse.

Parsing strategy (never more than ONE Gemini call per update):

    deterministic splitter (_split_target_segments/_parse_segment)
    -> strict structured validation (_validate_targets/_validate_plan)
    -> Gemini fallback (classify()) ONLY when the deterministic splitter
       itself can't confidently produce 2-10 targets (see looks_like_
       inventory_multi_target's own docstring — the pre-gate IS the
       deterministic splitter, so once it agrees this is worth a route
       hit, parse_multi_target_command almost always succeeds too; the
       Gemini call exists only for a segment shape neither this module's
       splitter nor a future revision of it understands yet).

Strict structured schema (allowlist: inventory_batch_change/clarify/
unsupported; per-target operation allowlist: consume/delete/unspecified):
Gemini only ever segments the message into item_name/operation/
quantity_hint triples — it NEVER returns a database id, SQL, Python/executor
names, or decides which inventory row anything resolves to; bot.py resolves
every target against a FRESH live inventory snapshot using the exact same
inventory.resolve_inventory_admin_candidates/phrase_declension_matches/
quantity-parsing machinery inventory_admin_route/action_planner.py already
use, and never writes to the database before an explicit confirm.

Bare vs explicit-quantity batch semantics (see bot.py's own resolution
docstring for the exact all-or-nothing rules) are NOT decided here — this
module only reports, per target, whether an explicit quantity was found
(operation="consume", quantity_hint=<raw text>) or not
(operation="unspecified", quantity_hint=None). Duplicate-textual-target
detection also happens in bot.py (which can additionally report the
duplicate's CURRENT live quantity), not here.
"""
import json
import re
from decimal import Decimal

import quantities
from inventory import _EXPLANATORY_TAIL_RE

_bot = None


def configure(bot_module):
    global _bot
    _bot = bot_module


_ALLOWED_ACTIONS = {"inventory_batch_change", "clarify", "unsupported"}
_ALLOWED_OPERATIONS = {"consume", "delete", "unspecified"}
_ALLOWED_TARGET_KEYS = {"item_name", "operation", "quantity_hint"}

_MIN_TARGETS = 2
_MAX_TARGETS = 10
_MAX_NAME_LENGTH = 200
_MAX_QUANTITY_HINT_LENGTH = 100
_MAX_CLARIFICATION_LENGTH = 500

_FALLBACK = {"version": 1, "action": "unsupported", "targets": [], "clarification_question": None}

UNSUPPORTED_MSG = (
    "Не зміг безпечно розібрати всі позиції для одночасної зміни запасів.\n\n"
    "Спробуй написати конкретніше, наприклад:\n"
    "«Видали одне автокрісло і один хліб»\n"
    "«Спиши 200 г сиру, 1 л молока та 2 сосиски»\n"
    "«Видали печиво, хліб і автокрісло»"
)

# =========================
# PRE-GATE + DETERMINISTIC SPLITTER — see module docstring. Deliberately the
# SAME function family decides both "is this worth a route hit" (looks_like_
# inventory_multi_target) and "how do we split it" (parse_multi_target_
# command) so the two can never disagree about what counts as a multi-target
# shape.
# =========================
_TRIGGER_VERBS = ("видали", "видалити", "прибери", "прибрати", "забери", "забрати", "спиши", "списати")
_TRIGGER_RE = re.compile(r"^(?:%s)\s+(?P<rest>.+)$" % "|".join(_TRIGGER_VERBS), re.IGNORECASE)
_LOCATION_SUFFIX_RE = re.compile(r"\s*(?:із|из|з|в|у)\s+запас\w*\.?\s*$", re.IGNORECASE)

_A_TAKOZH_RE = re.compile(r"\bа\s+також\b", re.IGNORECASE)
_TA_RE = re.compile(r"\bта\b", re.IGNORECASE)
_I_RE = re.compile(r"\bі\b", re.IGNORECASE)

# Explicit cross-domain markers — a shopping-list location phrase, an
# expense/financial stem (reuses the same short root list expenses.py's own
# _EXPENSE_FINANCIAL_REFERENCE_STEMS documents, plus the "запиши"/"оплат"
# verb roots that specifically signal a NEW expense/purchase rather than an
# inventory write-off), or a bare money amount (quantities.looks_like_money_
# amount) — any ONE of these means this is NOT a pure inventory batch
# command, so the pre-gate must reject it and let the message fall through
# to the Global Household Router/expense routes below, exactly as it did
# before this module existed. See this module's own test coverage for the
# exact escaped phrases required by the work order ("Видали хліб зі списку
# покупок і запиши витрату 5 zł", "Скасуй дві останні витрати", "Додай
# молоко і сир до покупок" — the last one never even reaches this check
# since it has no _TRIGGER_RE verb at all).
_SHOPPING_LOCATION_RE = re.compile(r"(?:зі?\s+списку\s+покупок|з\s+покупок|із\s+покупок)", re.IGNORECASE)
_EXPENSE_MARKER_RE = re.compile(
    r"витрат\w*|запиш\w*|оплат\w*|платіж\w*|плат\w*|транзакц\w*|чек\w*|списанн\w*", re.IGNORECASE,
)
# The household-aliases feature ("домашні назви") is a completely different
# domain (see bot.py's active_aliases_context route) that also recognizes a
# "Видали всі назви, крім X" bulk-exception phrasing — "назв..." never
# appears in real household grocery vocabulary, so excluding it here is safe
# and keeps that domain's own bulk-delete flow (further down the dispatch
# chain) untouched.
_ALIAS_DOMAIN_MARKER_RE = re.compile(r"назв\w*", re.IGNORECASE)
# A bulk "all/all-except" pronoun ("Видали все", "Видали всі, крім X") names
# no specific product at all — this route's job is 2-10 NAMED targets, never
# a bulk selection (that's Destructive Bulk Household Request Guard v1's own
# job, checked earlier in message_dispatcher.py, or the aliases-specific bulk
# flow above).
_BULK_PRONOUN_RE = re.compile(r"^(?:все|всі|усе|усі)(?:\s*,?\s*крім\s+.+)?$", re.IGNORECASE)

_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
# A comma is a target separator EXCEPT when it's a Ukrainian decimal point
# ("14,5 л", "0,5 кг" — quantities.format_quantity_display's own convention)
# — never split a comma that has a digit immediately on both sides.
_TARGET_COMMA_SPLIT_RE = re.compile(r"(?<!\d),(?!\d)")


def _has_cross_domain_marker(text):
    if _SHOPPING_LOCATION_RE.search(text):
        return True
    if _EXPENSE_MARKER_RE.search(text):
        return True
    if _ALIAS_DOMAIN_MARKER_RE.search(text):
        return True
    if quantities.looks_like_money_amount(text):
        return True
    return False


def _split_target_segments(text):
    """Deterministically split `text` into 2-10 raw target-phrase segments,
    or return None if the shape doesn't apply at all (no recognized trigger
    verb, no location-suffix/explanatory-tail-stripped remainder, a bulk
    "все"/"всі"-only pronoun, or fewer than 2/more than 10 comma-or-
    conjunction-separated segments). Never calls Gemini."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    match = _TRIGGER_RE.match(stripped)
    if not match:
        return None
    rest = match.group("rest").strip()
    rest = _LOCATION_SUFFIX_RE.sub("", rest).strip()
    # A trailing explanatory clause ("..., воно вже не потрібно", "..., бо
    # зіпсувалося" — the SAME whitelist inventory.parse_inventory_delete_
    # request's own single-target parser already strips) must never be
    # mistaken for a second target — strip it BEFORE splitting on commas.
    rest = _EXPLANATORY_TAIL_RE.sub("", rest).strip()
    rest = rest.rstrip(".!?").strip()
    if not rest or _BULK_PRONOUN_RE.match(rest):
        return None

    normalized = _A_TAKOZH_RE.sub(",", rest)
    normalized = _TA_RE.sub(",", normalized)
    normalized = _I_RE.sub(",", normalized)

    segments = [re.sub(r"\s+", " ", s).strip().rstrip(".!?").strip() for s in _TARGET_COMMA_SPLIT_RE.split(normalized)]
    segments = [s for s in segments if s]
    if not (_MIN_TARGETS <= len(segments) <= _MAX_TARGETS):
        return None
    return segments


def looks_like_inventory_multi_target(text):
    """Cheap, deterministic pre-gate — True only when `text` names an
    inventory change verb AND deterministically splits into 2-10 target
    phrases AND carries no explicit shopping/expense/cross-domain marker.
    A single-target command ("Видали молоко") never matches (the split
    always yields exactly 1 segment for it), so it's never claimed by this
    route at all — see message_dispatcher.py's CommandRouteDeps.inventory_
    multi_target_route for the exact routing position (right after
    destructive_bulk_guard, right before active_list_context_route)."""
    if not isinstance(text, str) or not text.strip():
        return False
    if _has_cross_domain_marker(text):
        return False
    return _split_target_segments(text) is not None


def _parse_segment(seg):
    """Parse one raw target-phrase segment into a raw {item_name, operation,
    quantity_hint} dict, or None if no usable product name can be extracted
    at all (should be rare — an empty segment never reaches here, since
    _split_target_segments already drops those).

    Quantity shapes recognized, in this exact priority order:
      1. leading NUMERIC quantity + a known structured unit word + name
         ("200 г сиру", "1 л молока") — quantity_hint is the raw "num unit"
         text, re-parsed later via quantities.parse_structured_quantity.
      2. leading BARE numeric quantity (no unit word) + name ("2 сосиски")
         — implies "шт.", same contract as quantities.parse_structured_
         quantity's own single-bare-number rule.
      3. leading WORD-NUMBER quantity + name ("одне автокрісло", "один
         хліб") — reuses quantities._consume_word_number/_clean_word_
         number_token (the same number-word vocabulary quantities.py's own
         Word-number Quantity + Price V1 section already established),
         deliberately WITHOUT requiring a following unit word (unlike
         quantities.parse_word_quantity, which is for a measured amount
         like "один літр" — here the word-number directly precedes the
         PRODUCT NAME itself, implying a "шт." count, exactly the shape
         inventory.py's own _LEADING_ONE_QUANTITY_RE already recognizes for
         a single-target delete).
      4. no quantity at all -> the whole segment is the bare item name.
    """
    seg = re.sub(r"\s+", " ", seg or "").strip().rstrip(".!?").strip()
    if not seg:
        return None

    tokens = seg.split(" ")

    if len(tokens) >= 3 and _NUMBER_RE.match(tokens[0]):
        unit = quantities._UNIT_ALIASES.get(tokens[1].lower().rstrip("."))
        if unit is not None:
            name = " ".join(tokens[2:]).strip()
            if name:
                return {"item_name": name, "operation": "consume", "quantity_hint": f"{tokens[0]} {tokens[1]}"}

    if len(tokens) >= 2 and _NUMBER_RE.match(tokens[0]):
        name = " ".join(tokens[1:]).strip()
        if name:
            return {"item_name": name, "operation": "consume", "quantity_hint": tokens[0]}

    cleaned = [quantities._clean_word_number_token(t) for t in tokens]
    value, next_i = quantities._consume_word_number(cleaned, 0)
    if value is not None and 0 < next_i < len(tokens):
        name = " ".join(tokens[next_i:]).strip()
        if name:
            raw_hint = " ".join(tokens[:next_i])
            return {"item_name": name, "operation": "consume", "quantity_hint": raw_hint}

    return {"item_name": seg, "operation": "unspecified", "quantity_hint": None}


def resolve_quantity_value(hint):
    """Convert a raw quantity_hint string (numeric-with-unit, bare numeric,
    or word-number count) into an exact (Decimal value, unit str) pair,
    completely independent of any inventory row's own quantity/unit — used
    by bot.py to compute the actual amount to consume. Returns (None, None)
    for blank/unparseable input. Never guesses beyond quantities.parse_
    structured_quantity's own contract plus the same word-number vocabulary
    _parse_segment already used to find the hint in the first place."""
    if not hint or not hint.strip():
        return None, None
    value, unit = quantities.parse_structured_quantity(hint)
    if value is not None:
        return value, unit
    tokens = hint.strip().split()
    cleaned = [quantities._clean_word_number_token(t) for t in tokens]
    value, next_i = quantities._consume_word_number(cleaned, 0)
    if value is not None and next_i == len(cleaned):
        return Decimal(value), "шт."
    return None, None


def _clean_text(value, max_len):
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned or len(cleaned) > max_len:
        return None
    return cleaned


def _validate_targets(raw_targets):
    """Strict schema validation for a targets list — 2-10 entries, each a
    dict with EXACTLY item_name/operation/quantity_hint (no DB id, no extra
    key of any kind), item_name a non-blank length-capped string, operation
    one of consume/delete/unspecified, quantity_hint a length-capped string
    or None. Returns the normalized list, or None if anything is unsafe/
    malformed. Deliberately does NOT reject duplicate item_name text here —
    bot.py's own resolution does that, where it can also report the
    duplicate's live quantity (see this module's own docstring)."""
    if not isinstance(raw_targets, list):
        return None
    if not (_MIN_TARGETS <= len(raw_targets) <= _MAX_TARGETS):
        return None
    targets = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            return None
        if set(raw.keys()) - _ALLOWED_TARGET_KEYS:
            return None
        item_name = _clean_text(raw.get("item_name"), _MAX_NAME_LENGTH)
        if item_name is None:
            return None
        operation = raw.get("operation")
        if operation not in _ALLOWED_OPERATIONS:
            return None
        raw_hint = raw.get("quantity_hint")
        quantity_hint = None
        if raw_hint is not None:
            quantity_hint = _clean_text(raw_hint, _MAX_QUANTITY_HINT_LENGTH)
        targets.append({"item_name": item_name, "operation": operation, "quantity_hint": quantity_hint})
    return targets


def parse_multi_target_command(text):
    """Deterministic split + strict validation, no Gemini call. Returns a
    validated {"version":1, "action":"inventory_batch_change", "targets":
    [...], "clarification_question": None} plan, or None if the message
    doesn't match this shape confidently enough (caller should try classify()
    next)."""
    segments = _split_target_segments(text)
    if segments is None:
        return None
    raw_targets = [_parse_segment(seg) for seg in segments]
    if any(t is None for t in raw_targets):
        return None
    targets = _validate_targets(raw_targets)
    if targets is None:
        return None
    return {"version": 1, "action": "inventory_batch_change", "targets": targets, "clarification_question": None}


# =========================
# GEMINI FALLBACK — only ever reached when looks_like_inventory_multi_target
# already agreed this is worth ONE classify() call AND parse_multi_target_
# command (the same deterministic splitter) still couldn't produce a safe
# plan on its own (see module docstring). Same "STRICT JSON only, re-
# validate everything in Python" posture as action_planner.py/shopping_
# action_planner.py.
# =========================
MULTI_TARGET_PROMPT = (
    "Ти — розпізнавач наміру для приватного домашнього Telegram-бота одного господарства. Користувач "
    "написав повідомлення про ОДНОЧАСНУ зміну КІЛЬКОХ різних позицій запасів (inventory) в одному "
    "повідомленні, яке звичайний детермінований розбір не зміг однозначно розділити. Твоя ЄДИНА задача — "
    "розбити повідомлення на окремі позиції (targets) і повернути СТРОГО валідний JSON, без Markdown і "
    "без жодного тексту поза JSON.\n\n"
    "Дії (action) — рівно одна з:\n"
    "- \"inventory_batch_change\" — повідомлення називає 2-10 РІЗНИХ позицій запасів для видалення/"
    "часткового списання одним повідомленням (напр. «Видали одне автокрісло, печиво і один хліб», "
    "«Спиши 200 г сиру, 1 л молока та 2 сосиски»). Для КОЖНОЇ позиції поверни: item_name (назва товару, "
    "як написано, без кількості/одиниці/сполучників), operation (\"consume\" — якщо в тексті ЯВНО вказана "
    "кількість для цієї позиції, \"unspecified\" — якщо кількість не вказана), quantity_hint (кількість "
    "рівно так, як написано в тексті — напр. «200 г», «одне», «2» — або null, якщо не вказана; НІКОЛИ сам "
    "не рахуй кількість).\n"
    "- \"clarify\" — видно, що це якась батч-дія із запасами, але бракує конкретних назв товарів. Постав "
    "коротке конкретне уточнювальне запитання українською в clarification_question.\n"
    "- \"unsupported\" — будь-що інше: одна-єдина позиція запасів, звичайна розмова, читання/перегляд "
    "запасів, покупки, витрати, або якщо не впевнений щодо жодної з двох дій вище.\n\n"
    "ВАЖЛИВО:\n"
    "- Ти НІКОЛИ не повертаєш ID записів бази даних, SQL, код чи назви функцій.\n"
    "- Ти НІКОЛИ не вирішуєш, які записи РЕАЛЬНО існують у запасах, і не вигадуєш кількість — Python "
    "окремо звірить кожну названу позицію з актуальним станом запасів.\n"
    "- Якщо не впевнений — обирай \"clarify\" або \"unsupported\", ніколи не вгадуй назву товару.\n\n"
    "Формат відповіді:\n"
    "{\"version\": 1, \"action\": \"inventory_batch_change\", \"targets\": [{\"item_name\": \"...\", "
    "\"operation\": \"consume\", \"quantity_hint\": \"...\" або null}, ...], \"clarification_question\": "
    "null}\n"
    "{\"version\": 1, \"action\": \"clarify\", \"targets\": [], \"clarification_question\": \"...\"}\n"
    "{\"version\": 1, \"action\": \"unsupported\", \"targets\": [], \"clarification_question\": null}\n\n"
    "Приклад:\n"
    "\"Видали одне автокрісло, печиво і один хліб\" -> {\"version\": 1, \"action\": \"inventory_batch_"
    "change\", \"targets\": [{\"item_name\": \"автокрісло\", \"operation\": \"consume\", \"quantity_hint\": "
    "\"одне\"}, {\"item_name\": \"печиво\", \"operation\": \"unspecified\", \"quantity_hint\": null}, "
    "{\"item_name\": \"хліб\", \"operation\": \"consume\", \"quantity_hint\": \"один\"}], "
    "\"clarification_question\": null}"
)


def _extract_json(raw):
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", cleaned)
        if m:
            cleaned = m.group(1).strip()
    return json.loads(cleaned)


def _validate_plan(data):
    if not isinstance(data, dict):
        return None
    if data.get("version") != 1:
        return None
    action = data.get("action")
    if action not in _ALLOWED_ACTIONS:
        return None

    if action == "clarify":
        raw_question = data.get("clarification_question")
        if not isinstance(raw_question, str) or not raw_question.strip():
            return None
        clarification_question = re.sub(r"\s+", " ", raw_question).strip()[:_MAX_CLARIFICATION_LENGTH]
        return {"version": 1, "action": "clarify", "targets": [], "clarification_question": clarification_question}

    if action == "unsupported":
        return {"version": 1, "action": "unsupported", "targets": [], "clarification_question": None}

    targets = _validate_targets(data.get("targets"))
    if targets is None:
        return None
    return {"version": 1, "action": "inventory_batch_change", "targets": targets, "clarification_question": None}


def _ask_gemini(text):
    """ONE Gemini call. Never raises — any failure at any step (no API key,
    network error, timeout, empty response, malformed JSON, wrong top-level
    shape, an invalid/unsafe plan) collapses to the same safe _FALLBACK
    dict."""
    raw = _bot.call_gemini([{"role": "user", "content": text}], MULTI_TARGET_PROMPT, temperature=0.0)
    if not raw:
        return dict(_FALLBACK)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return dict(_FALLBACK)
    plan = _validate_plan(data)
    if plan is None:
        return dict(_FALLBACK)
    return plan


def classify(text):
    """Public Gemini-fallback entrypoint. Returns a validated plan dict —
    see _validate_plan for its exact shape. Never calls Gemini for blank/
    non-string input."""
    if not isinstance(text, str) or not text.strip():
        return dict(_FALLBACK)
    return _ask_gemini(text)
