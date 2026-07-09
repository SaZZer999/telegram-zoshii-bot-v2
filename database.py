import os
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal
import psycopg
from psycopg.types.json import Jsonb

import action_history
from quantities import (
    STRUCTURED_UNITS,
    parse_structured_quantity,
    parse_quantity_fields,
    format_quantity_display,
    merge_quantity_values,
)

HOUSEHOLD_NAME = "Спільний дім"
DEFAULT_CATEGORY = "Інше їстівне"
VALID_LIST_CONTEXTS = {"shopping_saved", "inventory_saved"}

# =========================
# STRUCTURED QUANTITY HELPERS (DB-local)
#
# Only product-name canonicalization/synonym rules live here now — pure
# quantity parsing/merging/formatting (STRUCTURED_UNITS, parse_structured_
# quantity, merge_quantity_values, format_quantity_display) moved to
# quantities.py, the single source of truth bot.py also imports from.
# _NAME_SYNONYMS stays a self-contained mirror of bot.py's identical copy on
# purpose: database.py must not import bot.py (bot.py imports database.py).
# =========================

_NAME_SYNONYMS = {
    "сливки": "вершки",
    "mleko": "молоко",
    "ser": "сир",
    "maslo": "масло",
    "masło": "масло",
    "smietanka": "вершки",
    "śmietanka": "вершки",
    "smietana": "сметана",
    "śmietana": "сметана",
}

# Narrow, deterministic Latin/Cyrillic homoglyph whitelist — only the classic
# ASCII-lookalike Cyrillic letters (visually identical to a Latin letter in
# most fonts). Never used to transliterate real Ukrainian/Polish words (see
# _repair_mixed_script_token: it only fires for a token that is otherwise
# pure Latin, i.e. contains zero genuine Cyrillic-only letters).
_CYRILLIC_HOMOGLYPH_TO_LATIN = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "і": "i",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X", "І": "I",
}


def _clean_unicode_whitespace(text):
    """Step 2 of name normalization: Unicode NFKC normalization (folds
    compatibility characters, e.g. full-width forms) + whitespace collapse.
    Pure cleanup — never translates or transliterates anything."""
    normalized = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", normalized.strip())


def _char_script(c):
    """Classify one character for _repair_mixed_script_token: "homoglyph"
    (a Cyrillic letter that is visually identical to a Latin one),
    "cyrillic_only" (any other Cyrillic letter — no Latin lookalike),
    "latin" (a-z/A-Z), or "other" (digits, punctuation, etc.)."""
    if c in _CYRILLIC_HOMOGLYPH_TO_LATIN:
        return "homoglyph"
    if "CYRILLIC" in unicodedata.name(c, ""):
        return "cyrillic_only"
    if c.isascii() and c.isalpha():
        return "latin"
    return "other"


def _repair_mixed_script_token(token):
    """Step 3 of name normalization: repair ONE whitespace-delimited word
    that is otherwise pure Latin but has one or more Cyrillic look-alike
    letters mixed in (e.g. "mlekо" with a Cyrillic "о" -> "mleko"). Never
    touches a token containing any genuine Cyrillic-only letter — so real
    Ukrainian/Polish words like "молоко" or "сир" are always left untouched,
    and "сосиски"/"сосисок" are never rewritten into each other (no
    stemming/lemmatization here, only a homoglyph fix)."""
    scripts = [_char_script(c) for c in token]
    if "latin" in scripts and "homoglyph" in scripts and "cyrillic_only" not in scripts:
        return "".join(_CYRILLIC_HOMOGLYPH_TO_LATIN.get(c, c) for c in token)
    return token


def _repair_mixed_script(text):
    """Apply _repair_mixed_script_token to each word of `text` independently."""
    if not text:
        return text
    return " ".join(_repair_mixed_script_token(tok) for tok in text.split(" "))


def canonicalize_name(name):
    """Lowercase/trim a name, repair narrow Latin/Cyrillic mixed-script
    homoglyphs (steps 2-3 of the pipeline; see _clean_unicode_whitespace/
    _repair_mixed_script), and map known synonyms to one canonical form
    (steps 5-6). Household alias resolution (steps 1/4) lives in
    resolve_item_name, which is checked before this function is reached."""
    cleaned = _repair_mixed_script(_clean_unicode_whitespace(name or ""))
    base = cleaned.strip().lower()
    return _NAME_SYNONYMS.get(base, base)


def normalize_quantity_fields(name, quantity_text, allow_default_unit=False):
    """Compute canonical_name/quantity_value/quantity_unit/quantity_inferred/quantity_text.

    Thin wrapper: canonical_name comes from this module's own canonicalize_
    name (product-name synonym rules stay local, out of quantities.py's
    scope); the quantity fields themselves are computed by quantities.
    parse_quantity_fields, the single shared implementation bot.py's
    normalize_item_quantity also calls.

    allow_default_unit=True applies the "1 шт." default only when quantity_text
    is genuinely blank (new items) — never for backfilling old data.
    """
    canonical_name = canonicalize_name(name)
    fields = parse_quantity_fields(quantity_text, allow_default_unit=allow_default_unit)
    return {"canonical_name": canonical_name, **fields}


# =========================
# HOUSEHOLD ALIAS RESOLUTION (pure, no DB connection)
# =========================

ALIAS_TEXT_MAX_LEN = 60


def normalize_alias_text(text):
    """Pure normalization for alias TEXT: both the stored alias_normalized key
    and the lookup key used to match incoming product names against it.
    Collapses whitespace, lowercases, strips stray punctuation while keeping
    meaningful digits/% (e.g. "30%"), enforces ALIAS_TEXT_MAX_LEN, rejects
    empty input. Returns the normalized string, or None if unusable. Never raises.
    """
    if not isinstance(text, str):
        return None
    collapsed = re.sub(r"\s+", " ", text.strip())
    if not collapsed:
        return None
    cleaned = re.sub(r"[^\w%\-\s]", "", collapsed.lower(), flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) > ALIAS_TEXT_MAX_LEN:
        return None
    return cleaned


def resolve_item_name(name, alias_map):
    """THE single shared resolver for product-name resolution, used by both
    database.py's own chokepoints and bot.py (via import). Resolution order:

    1. household alias lookup using the name as-is (compatible with aliases
       created before Unicode/mixed-script cleanup existed);
    2. Unicode/whitespace cleanup + narrow mixed-script token repair;
    3. household alias lookup again, against the cleaned name (covers an
       alias created/typed with the same cleanup already applied);
    4. built-in generic synonym, else plain lowercasing (canonicalize_name(),
       which applies the same cleanup+repair internally).

    Household aliases always win over the built-in synonym dictionary — a
    match at step 1 or 3 returns immediately, before canonicalize_name() (and
    therefore the built-in dictionary) is ever consulted. No alias-to-alias
    chaining: a match returns the alias row's stored target directly, one
    hop only.

    alias_map: {alias_normalized: {"target_display_name":.., "target_canonical_name":..}},
    e.g. from get_household_alias_map(). Pass {} or None when there is no
    household context. Returns (display_name, canonical_name). Never raises.
    """
    old_key = normalize_alias_text(name)
    if alias_map and old_key is not None and old_key in alias_map:
        entry = alias_map[old_key]
        return entry["target_display_name"], entry["target_canonical_name"]

    cleaned = _repair_mixed_script(_clean_unicode_whitespace(name or ""))
    new_key = normalize_alias_text(cleaned)
    if alias_map and new_key is not None and new_key != old_key and new_key in alias_map:
        entry = alias_map[new_key]
        return entry["target_display_name"], entry["target_canonical_name"]

    return name, canonicalize_name(name)


# =========================
# PERSISTENT LIST CONTEXT HELPERS (pure, no DB connection)
# =========================

def list_context_is_valid(context):
    """True if context is one of the two allowed persisted list contexts."""
    return context in VALID_LIST_CONTEXTS


def list_context_is_expired(expires_at, now=None):
    """True if expires_at is missing or not strictly in the future.

    `now` is injectable for tests; defaults to the real UTC time.
    """
    if expires_at is None:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    return expires_at <= now


def list_context_is_usable(context, stored_household_id, requested_household_id, expires_at, now=None):
    """Combined pure decision used by get_list_context: valid context value,
    matching household, and not expired. False if any check fails."""
    if not list_context_is_valid(context):
        return False
    if stored_household_id != requested_household_id:
        return False
    if list_context_is_expired(expires_at, now=now):
        return False
    return True


def get_connection():
    url = os.getenv("DATABASE_URL")
    return psycopg.connect(url, connect_timeout=10)

def test_database_connection():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS households (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id                 SERIAL PRIMARY KEY,
                    telegram_user_id   BIGINT NOT NULL UNIQUE,
                    household_id       INTEGER REFERENCES households(id),
                    display_name       TEXT,
                    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shopping_items (
                    id                    SERIAL PRIMARY KEY,
                    household_id          INTEGER NOT NULL REFERENCES households(id),
                    name                  TEXT NOT NULL,
                    quantity_text         TEXT,
                    is_completed          BOOLEAN NOT NULL DEFAULT FALSE,
                    created_by_user_id    INTEGER REFERENCES users(id),
                    completed_by_user_id  INTEGER REFERENCES users(id),
                    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at          TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_shopping_items_active
                ON shopping_items (household_id)
                WHERE is_completed = FALSE
            """)
            cur.execute("""
                ALTER TABLE shopping_items
                ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'Інше їстівне'
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inventory_items (
                    id                    SERIAL PRIMARY KEY,
                    household_id          INTEGER NOT NULL REFERENCES households(id),
                    name                  TEXT NOT NULL,
                    quantity_text         TEXT,
                    category              TEXT NOT NULL DEFAULT 'Інше їстівне',
                    created_by_user_id    INTEGER REFERENCES users(id),
                    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_inventory_items_household
                ON inventory_items (household_id, category, name)
            """)
            cur.execute("""
                ALTER TABLE shopping_items ADD COLUMN IF NOT EXISTS canonical_name TEXT
            """)
            cur.execute("""
                ALTER TABLE shopping_items ADD COLUMN IF NOT EXISTS quantity_value NUMERIC
            """)
            cur.execute("""
                ALTER TABLE shopping_items ADD COLUMN IF NOT EXISTS quantity_unit TEXT
            """)
            cur.execute("""
                ALTER TABLE shopping_items ADD COLUMN IF NOT EXISTS quantity_inferred BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cur.execute("""
                ALTER TABLE shopping_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
            """)
            cur.execute("""
                ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS canonical_name TEXT
            """)
            cur.execute("""
                ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS quantity_value NUMERIC
            """)
            cur.execute("""
                ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS quantity_unit TEXT
            """)
            cur.execute("""
                ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS quantity_inferred BOOLEAN NOT NULL DEFAULT FALSE
            """)
            # chat_id is already PK-indexed; expired rows are deleted by chat_id
            # opportunistically in get_list_context, so no separate expires_at
            # index is needed (no bulk-cleanup job exists to justify one).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_list_contexts (
                    chat_id       BIGINT PRIMARY KEY,
                    household_id  BIGINT NOT NULL,
                    context       TEXT NOT NULL,
                    expires_at    TIMESTAMPTZ NOT NULL,
                    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS household_aliases (
                    id                     SERIAL PRIMARY KEY,
                    household_id           INTEGER NOT NULL REFERENCES households(id),
                    alias_text             TEXT NOT NULL,
                    alias_normalized       TEXT NOT NULL,
                    target_display_name    TEXT NOT NULL,
                    target_canonical_name  TEXT NOT NULL,
                    created_by_user_id     INTEGER REFERENCES users(id),
                    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_household_aliases_unique
                ON household_aliases (household_id, alias_normalized)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id                  SERIAL PRIMARY KEY,
                    household_id        INTEGER NOT NULL REFERENCES households(id),
                    amount              NUMERIC NOT NULL,
                    currency            TEXT NOT NULL DEFAULT 'PLN',
                    category            TEXT NOT NULL,
                    description         TEXT,
                    expense_date        DATE NOT NULL,
                    created_by_user_id  INTEGER REFERENCES users(id),
                    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_expenses_household
                ON expenses (household_id, expense_date)
            """)
            # Action History + Safe Undo v1 — one row per confirmed Global
            # Household Operation. Additive only: never touches shopping_items/
            # inventory_items/expenses schema. See action_history.py for the
            # JSONB payload shapes (forward_payload/inverse_payload/
            # before_snapshot/post_action_snapshot/summary).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS household_action_journal (
                    id                    SERIAL PRIMARY KEY,
                    household_id          INTEGER NOT NULL REFERENCES households(id),
                    actor_user_id         INTEGER REFERENCES users(id),
                    operation_type        TEXT NOT NULL,
                    forward_payload       JSONB NOT NULL,
                    inverse_payload       JSONB,
                    before_snapshot       JSONB NOT NULL,
                    post_action_snapshot  JSONB NOT NULL,
                    summary               JSONB NOT NULL,
                    status                TEXT NOT NULL DEFAULT 'active',
                    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    undone_by_user_id     INTEGER REFERENCES users(id),
                    undone_at             TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_household_action_journal_latest
                ON household_action_journal (household_id, actor_user_id, status, created_at DESC)
            """)
        conn.commit()
    _backfill_structured_quantities()


def _backfill_structured_quantities():
    """One-time, idempotent backfill of canonical_name/quantity_value/quantity_unit
    for rows that predate this migration. Never calls Gemini, never invents a
    "1 шт." default for old blank quantities — those stay unstructured."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in ("shopping_items", "inventory_items"):
                cur.execute(f"SELECT id, name, quantity_text FROM {table} WHERE canonical_name IS NULL")
                rows = cur.fetchall()
                for row_id, name, quantity_text in rows:
                    normalized = normalize_quantity_fields(name, quantity_text or "", allow_default_unit=False)
                    cur.execute(
                        f"UPDATE {table} SET canonical_name=%s, quantity_value=%s, quantity_unit=%s WHERE id=%s",
                        (normalized["canonical_name"], normalized["quantity_value"], normalized["quantity_unit"], row_id),
                    )
        conn.commit()


# =========================
# PERSISTENT LIST CONTEXT (survives restart/deploy, TTL 24h)
# =========================

def save_list_context(chat_id, household_id, context):
    """Persist the last opened saved list (shopping_saved/inventory_saved) for
    a chat, replacing any previous value, with a 24h TTL from now.

    No-ops silently for an invalid context or a DB error — this is a UX
    convenience, never allowed to break the bot.
    """
    if not list_context_is_valid(context):
        return
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_list_contexts (chat_id, household_id, context, expires_at, updated_at)
                    VALUES (%s, %s, %s, NOW() + INTERVAL '24 hours', NOW())
                    ON CONFLICT (chat_id) DO UPDATE
                        SET household_id = EXCLUDED.household_id,
                            context = EXCLUDED.context,
                            expires_at = EXCLUDED.expires_at,
                            updated_at = EXCLUDED.updated_at
                    """,
                    (chat_id, household_id, context)
                )
            conn.commit()
    except Exception:
        pass


def get_list_context(chat_id, household_id):
    """Return the persisted context for chat_id if valid, unexpired, and the
    household matches. Returns None on any error, mismatch, or expiry —
    never raises. Opportunistically deletes an expired row on read.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT household_id, context, expires_at FROM bot_list_contexts WHERE chat_id = %s",
                    (chat_id,)
                )
                row = cur.fetchone()
                if row is None:
                    return None
                stored_household_id, context, expires_at = row
                usable = list_context_is_usable(context, stored_household_id, household_id, expires_at)
                if not usable and list_context_is_expired(expires_at):
                    cur.execute("DELETE FROM bot_list_contexts WHERE chat_id = %s", (chat_id,))
                    conn.commit()
        return context if usable else None
    except Exception:
        return None


def clear_list_context(chat_id):
    """Delete the persisted list context for a chat, if any. Never raises."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_list_contexts WHERE chat_id = %s", (chat_id,))
            conn.commit()
    except Exception:
        pass


def get_or_create_household():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO households (name) VALUES (%s)
                ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (HOUSEHOLD_NAME,)
            )
            row = cur.fetchone()
        conn.commit()
    return row[0]

def get_or_create_user(telegram_user_id, household_id, display_name=None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_user_id, household_id, display_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_user_id) DO UPDATE
                    SET household_id = EXCLUDED.household_id,
                        display_name = COALESCE(EXCLUDED.display_name, users.display_name)
                RETURNING id
                """,
                (telegram_user_id, household_id, display_name)
            )
            row = cur.fetchone()
        conn.commit()
    return row[0]

# =========================
# HOUSEHOLD ALIASES
# =========================

def list_household_aliases(household_id):
    """All aliases for one household, sorted by alias_normalized. Never
    visible across households (WHERE household_id=%s)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, alias_text, alias_normalized, target_display_name, target_canonical_name
                FROM household_aliases
                WHERE household_id = %s
                ORDER BY alias_normalized ASC
                """,
                (household_id,)
            )
            rows = cur.fetchall()
    return [
        {"id": r[0], "alias_text": r[1], "alias_normalized": r[2],
         "target_display_name": r[3], "target_canonical_name": r[4]}
        for r in rows
    ]

def get_household_alias(household_id, alias_normalized):
    """Single read-only lookup by normalized key, scoped to household_id.
    Returns a dict or None. Used to build the create/update preview text
    ("було X / стане Y") — never called at confirm time for writing."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, alias_text, alias_normalized, target_display_name, target_canonical_name
                FROM household_aliases
                WHERE household_id = %s AND alias_normalized = %s
                """,
                (household_id, alias_normalized)
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "alias_text": row[1], "alias_normalized": row[2],
            "target_display_name": row[3], "target_canonical_name": row[4]}

def get_household_alias_map(household_id):
    """All aliases for one household as a lookup dict — fetch ONCE per
    request/batch and reuse across every item, never one query per item:
    {alias_normalized: {"target_display_name":.., "target_canonical_name":..}}."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT alias_normalized, target_display_name, target_canonical_name "
                "FROM household_aliases WHERE household_id = %s",
                (household_id,)
            )
            rows = cur.fetchall()
    return {r[0]: {"target_display_name": r[1], "target_canonical_name": r[2]} for r in rows}

def create_or_update_household_alias(household_id, alias_text, target_display_name, created_by_user_id):
    """Create or update (upsert on household_id+alias_normalized). Re-derives
    alias_normalized/target_canonical_name itself and never trusts a caller's
    pre-check alone (defense in depth) — returns None without ever opening a
    DB connection if the input is invalid (empty/too-long/no-op alias≈target).
    On update, created_by_user_id of the ORIGINAL row is preserved (not
    overwritten). Never touches shopping_items/inventory_items.
    """
    alias_normalized = normalize_alias_text(alias_text)
    target_clean = (target_display_name or "").strip()
    if alias_normalized is None or not target_clean:
        return None
    if alias_normalized == normalize_alias_text(target_display_name):
        return None
    target_canonical_name = canonicalize_name(target_display_name)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO household_aliases
                    (household_id, alias_text, alias_normalized, target_display_name,
                     target_canonical_name, created_by_user_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (household_id, alias_normalized) DO UPDATE
                    SET target_display_name = EXCLUDED.target_display_name,
                        target_canonical_name = EXCLUDED.target_canonical_name,
                        updated_at = NOW()
                RETURNING id, alias_text, alias_normalized, target_display_name, target_canonical_name
                """,
                (household_id, alias_text.strip(), alias_normalized, target_clean,
                 target_canonical_name, created_by_user_id)
            )
            row = cur.fetchone()
        conn.commit()
    return {"id": row[0], "alias_text": row[1], "alias_normalized": row[2],
            "target_display_name": row[3], "target_canonical_name": row[4]}

def delete_household_alias(household_id, alias_normalized):
    """Delete one household alias by normalized key. Only ever deletes the
    alias row — never shopping_items/inventory_items. Returns True if a row
    was deleted, False if none matched (already gone / never existed)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM household_aliases WHERE household_id = %s AND alias_normalized = %s RETURNING id",
                (household_id, alias_normalized)
            )
            row = cur.fetchone()
        conn.commit()
    return row is not None

def delete_household_aliases_batch(household_id, targets):
    """Delete multiple household aliases in one transaction (bulk delete
    preview confirm). `targets`: list of dicts with id, target_display_name,
    target_canonical_name — the snapshot captured when the preview was built.
    Re-verified for staleness inside this same transaction before anything is
    deleted (StaleSnapshotError + full rollback, nothing partially applied, if
    any target alias changed or vanished on another device since the preview
    was shown). Returns count of deleted rows. Only ever deletes rows from
    household_aliases — never shopping_items/inventory_items.
    """
    if not targets:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_alias_targets_in_tx(cur, household_id, targets)
            ids = [t["id"] for t in targets]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(
                f"DELETE FROM household_aliases WHERE id IN ({placeholders}) AND household_id = %s",
                list(ids) + [household_id]
            )
            count = cur.rowcount
        conn.commit()
    return count

# =========================
# EXPENSES
# =========================

def add_expense(household_id, user_db_id, amount, currency, category, description, expense_date):
    """Insert one expense row for a household — a single DB operation, called
    exactly once per confirmed preview. `amount` must already be a validated
    Decimal > 0 and `category` already validated against the fixed category
    list; this function trusts its caller (bot.py) for those business rules
    and only performs the parameterized SQL insert. Never touches
    shopping_items/inventory_items. Returns the new row id.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO expenses
                    (household_id, amount, currency, category, description, expense_date, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (household_id, amount, currency, category, description or None, expense_date, user_db_id)
            )
            row = cur.fetchone()
        conn.commit()
    return row[0]


def get_recent_expenses(household_id, limit=10):
    """Up to `limit` most recent expenses for one household — newest
    expense_date first, then newest created_at, then newest id (stable
    tie-break for same-instant inserts). Never crosses household_id.
    Read-only, no Gemini involved.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT amount, currency, category, description, expense_date, created_at
                FROM expenses
                WHERE household_id = %s
                ORDER BY expense_date DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (household_id, limit)
            )
            rows = cur.fetchall()
    return [
        {
            "amount": r[0], "currency": r[1], "category": r[2],
            "description": r[3], "expense_date": r[4], "created_at": r[5],
        }
        for r in rows
    ]


def get_recent_expenses_for_deletion(household_id, limit=10):
    """Same recency ordering as get_recent_expenses, but each row also
    carries its id — needed only by the expense-deletion flow to know
    exactly which row a chosen list number refers to. A separate function
    (rather than adding id to get_recent_expenses) so the existing v1.2
    reports helper/shape/tests are left untouched. Never crosses household_id.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, amount, currency, category, description, expense_date, created_at
                FROM expenses
                WHERE household_id = %s
                ORDER BY expense_date DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (household_id, limit)
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0], "amount": r[1], "currency": r[2], "category": r[3],
            "description": r[4], "expense_date": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def get_expense_month_summary(household_id, year, month):
    """Per-category subtotals (Decimal) and grand total for one household's
    expenses within one calendar month (expense_date in [first day of month,
    first day of next month)). Never crosses household_id. Categories are
    only present here if at least one expense row exists for them — there is
    no such thing as a stored zero-amount expense, so a zero subtotal can
    never occur (SUM over a non-empty group of positive amounts is always
    positive).
    """
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT category, SUM(amount)
                FROM expenses
                WHERE household_id = %s AND expense_date >= %s AND expense_date < %s
                GROUP BY category
                """,
                (household_id, start, end)
            )
            rows = cur.fetchall()
    by_category = {category: amount for category, amount in rows}
    total = sum(by_category.values(), Decimal("0"))
    return {"total": total, "by_category": by_category}


def delete_expense(household_id, expense_id, snapshot):
    """Delete exactly one expense row, but only after re-verifying inside
    this same transaction (row locked FOR UPDATE, so no concurrent write can
    slip in first) that it still exists, belongs to household_id, and its
    amount/category/expense_date/description still match `snapshot` — the
    values captured when the delete preview was built. Raises
    StaleSnapshotError (full rollback, nothing deleted) if the row is gone
    or has changed since. A second call with the same arguments after a
    successful delete also raises StaleSnapshotError (the row is simply
    gone), so a repeated confirm can never delete a second row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT amount, category, expense_date, description FROM expenses "
                "WHERE id = %s AND household_id = %s FOR UPDATE",
                (expense_id, household_id)
            )
            row = cur.fetchone()
            if row is None:
                raise StaleSnapshotError()
            amount, category, expense_date, description = row
            if (
                amount != snapshot["amount"]
                or category != snapshot["category"]
                or expense_date != snapshot["expense_date"]
                or (description or None) != (snapshot.get("description") or None)
            ):
                raise StaleSnapshotError()
            cur.execute(
                "DELETE FROM expenses WHERE id = %s AND household_id = %s",
                (expense_id, household_id)
            )
        conn.commit()


def add_shopping_item(household_id, name, quantity_text, created_by_user_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shopping_items (household_id, name, quantity_text, created_by_user_id)
                VALUES (%s, %s, %s, %s)
                """,
                (household_id, name, quantity_text or None, created_by_user_id)
            )
        conn.commit()

def get_active_shopping_items(household_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, quantity_text, category, canonical_name, quantity_value, quantity_unit, quantity_inferred
                FROM shopping_items
                WHERE household_id = %s AND is_completed = FALSE
                ORDER BY created_at ASC
                """,
                (household_id,)
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0], "name": r[1], "quantity_text": r[2], "category": r[3],
            "canonical_name": r[4],
            "quantity_value": float(r[5]) if r[5] is not None else None,
            "quantity_unit": r[6], "quantity_inferred": r[7],
        }
        for r in rows
    ]

def mark_item_completed(household_id, item_number, completed_by_user_id):
    items = get_active_shopping_items(household_id)
    if item_number < 1 or item_number > len(items):
        return None
    item = items[item_number - 1]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopping_items
                SET is_completed = TRUE,
                    completed_by_user_id = %s,
                    completed_at = NOW()
                WHERE id = %s AND is_completed = FALSE
                """,
                (completed_by_user_id, item["id"])
            )
        conn.commit()
    return item["name"]

def delete_active_item(household_id, item_number):
    items = get_active_shopping_items(household_id)
    if item_number < 1 or item_number > len(items):
        return None
    item = items[item_number - 1]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shopping_items WHERE id = %s AND is_completed = FALSE",
                (item["id"],)
            )
        conn.commit()
    return item["name"]

def mark_item_by_id(item_id, completed_by_user_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shopping_items
                SET is_completed = TRUE,
                    completed_by_user_id = %s,
                    completed_at = NOW()
                WHERE id = %s AND is_completed = FALSE
                RETURNING name
                """,
                (completed_by_user_id, item_id)
            )
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None

def delete_item_by_id(item_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shopping_items WHERE id = %s AND is_completed = FALSE RETURNING name",
                (item_id,)
            )
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None

def get_inventory_items(household_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, quantity_text, category, canonical_name, quantity_value, quantity_unit, quantity_inferred
                FROM inventory_items
                WHERE household_id = %s
                ORDER BY category, name ASC
                """,
                (household_id,)
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0], "name": r[1], "quantity_text": r[2], "category": r[3],
            "canonical_name": r[4],
            "quantity_value": float(r[5]) if r[5] is not None else None,
            "quantity_unit": r[6], "quantity_inferred": r[7],
        }
        for r in rows
    ]

def _merge_or_insert_shopping_in_tx(cur, household_id, user_db_id, name, qty_text, category,
                                     canonical_name=None, quantity_value=None, quantity_unit=None,
                                     quantity_inferred=False):
    """Merge into existing active shopping item or insert new one (within open cursor).

    Matches on canonical_name; category must match exactly or either side be
    DEFAULT_CATEGORY. Quantities are summed only via structured value/unit.
    """
    if canonical_name is None:
        normalized = normalize_quantity_fields(name, qty_text, allow_default_unit=True)
        canonical_name = normalized["canonical_name"]
        quantity_value = normalized["quantity_value"]
        quantity_unit = normalized["quantity_unit"]
        quantity_inferred = normalized["quantity_inferred"]
        qty_text = normalized["quantity_text"]

    cur.execute(
        "SELECT id, category, quantity_value, quantity_unit, quantity_inferred FROM shopping_items "
        "WHERE household_id=%s AND canonical_name=%s AND is_completed=FALSE ORDER BY id ASC",
        (household_id, canonical_name)
    )
    existing = None
    for cand_id, cand_category, cand_value, cand_unit, cand_inferred in cur.fetchall():
        if cand_category == category or cand_category == DEFAULT_CATEGORY or category == DEFAULT_CATEGORY:
            existing = (cand_id, float(cand_value) if cand_value is not None else None, cand_unit, cand_inferred)
            break

    if existing is not None and quantity_value is not None:
        ex_id, ex_value, ex_unit, ex_inferred = existing
        merged = merge_quantity_values(ex_value, ex_unit, quantity_value, quantity_unit)
        if merged is not None:
            merged_value, merged_unit = merged
            cur.execute(
                "UPDATE shopping_items SET quantity_text=%s, quantity_value=%s, quantity_unit=%s, "
                "quantity_inferred=%s, updated_at=NOW() WHERE id=%s",
                (format_quantity_display(merged_value, merged_unit), merged_value, merged_unit,
                 bool(ex_inferred) and bool(quantity_inferred), ex_id)
            )
            return

    cur.execute(
        "INSERT INTO shopping_items (household_id, name, quantity_text, category, created_by_user_id, "
        "canonical_name, quantity_value, quantity_unit, quantity_inferred) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (household_id, name, qty_text or None, category, user_db_id,
         canonical_name, quantity_value, quantity_unit, quantity_inferred)
    )

def _merge_or_insert_inventory_in_tx(cur, household_id, user_db_id, name, qty_text, category,
                                      canonical_name=None, quantity_value=None, quantity_unit=None,
                                      quantity_inferred=False):
    """Merge into existing inventory item or insert new one (within open cursor).

    Matches on canonical_name; category must match exactly or either side be
    DEFAULT_CATEGORY. Quantities are summed only via structured value/unit
    (merge_quantity_values decides — including its cross-unit conversion
    within the same mass/volume group, e.g. л merging with мл).

    Inventory Representation Guard v1: candidate rows are locked FOR UPDATE
    and tried in id order, merging into the FIRST one merge_quantity_values
    actually accepts — not just the first category-compatible row overall.
    If a caller's preview predicted a merge into a specific existing row
    (see bot.py's resolve_inventory_representation), that row's snapshot
    should already have been passed as an inventory_targets entry and
    verified via _verify_targets_in_tx before this function runs, so by the
    time we get here it is guaranteed unchanged and this loop reaches the
    exact same row. If none of the candidates can merge, a new row is
    inserted — this is the deliberate "separate record" outcome the guard's
    preview already warned the user about, never a silent duplicate.

    Critical re-check for an INFERRED incoming quantity (quantity_inferred=
    True): even though the preview already refused to build a pending
    "merge" op for an inferred quantity that conflicts with ANY sibling row
    (see bot.py's resolve_inventory_representation), a NEW conflicting row
    could have appeared for the SAME canonical_name between preview and
    confirm. Re-verified here, fresh, against every FOR-UPDATE-locked
    candidate: if the incoming quantity is inferred and at least one
    candidate can merge but at least one other candidate cannot, this is
    exactly the ambiguous "which record do you mean" situation the guard
    exists to prevent — raise StaleSnapshotError (whole transaction rolled
    back, nothing written) instead of guessing which row to merge into.
    """
    if canonical_name is None:
        normalized = normalize_quantity_fields(name, qty_text, allow_default_unit=True)
        canonical_name = normalized["canonical_name"]
        quantity_value = normalized["quantity_value"]
        quantity_unit = normalized["quantity_unit"]
        quantity_inferred = normalized["quantity_inferred"]
        qty_text = normalized["quantity_text"]

    cur.execute(
        "SELECT id, category, quantity_value, quantity_unit, quantity_inferred FROM inventory_items "
        "WHERE household_id=%s AND canonical_name=%s ORDER BY id ASC FOR UPDATE",
        (household_id, canonical_name)
    )
    candidates = [
        (cand_id, float(cand_value) if cand_value is not None else None, cand_unit, cand_inferred)
        for cand_id, cand_category, cand_value, cand_unit, cand_inferred in cur.fetchall()
        if cand_category == category or cand_category == DEFAULT_CATEGORY or category == DEFAULT_CATEGORY
    ]

    if quantity_value is not None:
        mergeable = [
            (ex_id, ex_value, ex_unit, ex_inferred) for ex_id, ex_value, ex_unit, ex_inferred in candidates
            if merge_quantity_values(ex_value, ex_unit, quantity_value, quantity_unit) is not None
        ]
        if quantity_inferred and candidates and len(mergeable) != len(candidates):
            raise StaleSnapshotError()
        if mergeable:
            ex_id, ex_value, ex_unit, ex_inferred = mergeable[0]
            merged_value, merged_unit = merge_quantity_values(ex_value, ex_unit, quantity_value, quantity_unit)
            cur.execute(
                "UPDATE inventory_items SET quantity_text=%s, quantity_value=%s, quantity_unit=%s, "
                "quantity_inferred=%s, updated_at=NOW() WHERE id=%s",
                (format_quantity_display(merged_value, merged_unit), merged_value, merged_unit,
                 bool(ex_inferred) and bool(quantity_inferred), ex_id)
            )
            return

    cur.execute(
        "INSERT INTO inventory_items (household_id, name, quantity_text, category, created_by_user_id, "
        "canonical_name, quantity_value, quantity_unit, quantity_inferred) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (household_id, name, qty_text or None, category, user_db_id,
         canonical_name, quantity_value, quantity_unit, quantity_inferred)
    )

# =========================
# STALE SNAPSHOT PROTECTION
#
# Shared guard reused by every confirm-flow that mutates or removes existing
# rows based on a snapshot captured earlier (when a preview was built). The
# check and the mutation always happen inside the same transaction/cursor —
# never a separate SELECT beforehand — so there is no gap between "verify"
# and "write" for a concurrent change from another device to slip into.
# =========================

class StaleSnapshotError(Exception):
    """Raised inside an open transaction when a confirmed household action's
    target rows no longer match the snapshot captured when its preview was
    built (edited or removed from another device/session in the meantime).

    Raising this aborts the whole transaction (the caller's `with get_connection()`
    block rolls back automatically since the exception propagates out of it) —
    nothing from the action is applied, not even partially.
    """
    pass


def _verify_targets_in_tx(cur, table, household_id, targets, extra_fields=None):
    """Re-read `targets` for `table` ("shopping_items" or "inventory_items")
    inside the caller's open transaction and lock the rows (FOR UPDATE) so no
    concurrent write can slip in before this transaction commits. Raises
    StaleSnapshotError if any target row is missing or its quantity_value/
    quantity_unit no longer match the snapshot. No-ops for empty/None targets.

    targets: list of dicts with item_id, quantity_value, quantity_unit — the
    values captured when the preview/snapshot was built.

    extra_fields (optional): tuple of additional column names (e.g.
    ("canonical_name", "category")) to also verify for exact match against
    the same-named keys in each target dict — used by the manual merge guard,
    which must also detect a concurrent rename/re-categorization that leaves
    quantity_value/quantity_unit untouched. None (default) preserves the
    original quantity-only check and SQL shape unchanged for every other caller.
    """
    if not targets:
        return
    ids = [t["item_id"] for t in targets]
    placeholders = ",".join(["%s"] * len(ids))
    extra_cols = list(extra_fields or ())
    columns = ["id", "quantity_value", "quantity_unit"] + extra_cols
    cur.execute(
        f"SELECT {', '.join(columns)} FROM {table} "
        f"WHERE id IN ({placeholders}) AND household_id=%s FOR UPDATE",
        list(ids) + [household_id],
    )
    current = {}
    for row in cur.fetchall():
        value = float(row[1]) if row[1] is not None else None
        current[row[0]] = ((value, row[2]), tuple(row[3:]))
    for t in targets:
        seen = current.get(t["item_id"])
        if seen is None:
            raise StaleSnapshotError()
        seen_qty, seen_extra = seen
        if seen_qty != (t.get("quantity_value"), t.get("quantity_unit")):
            raise StaleSnapshotError()
        if extra_cols and seen_extra != tuple(t.get(f) for f in extra_cols):
            raise StaleSnapshotError()


def _verify_alias_targets_in_tx(cur, household_id, targets):
    """Same guard as _verify_targets_in_tx, but for household_aliases rows
    (which have no quantity fields — the "did this alias change on another
    device" check compares target_display_name/target_canonical_name instead).
    Raises StaleSnapshotError if any target alias is missing or its target
    no longer matches the snapshot captured when the bulk-delete preview was
    built. No-ops for empty/None targets.

    targets: list of dicts with id, target_display_name, target_canonical_name.
    """
    if not targets:
        return
    ids = [t["id"] for t in targets]
    placeholders = ",".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT id, target_display_name, target_canonical_name FROM household_aliases "
        f"WHERE id IN ({placeholders}) AND household_id=%s FOR UPDATE",
        list(ids) + [household_id],
    )
    current = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    for t in targets:
        seen = current.get(t["id"])
        if seen is None or seen != (t.get("target_display_name"), t.get("target_canonical_name")):
            raise StaleSnapshotError()

# =========================
# BATCH ADD
# =========================

def add_shopping_items_batch(household_id, created_by_user_id, items):
    """Add multiple shopping items, merging duplicates with compatible quantities."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for item in items:
                _merge_or_insert_shopping_in_tx(
                    cur, household_id, created_by_user_id,
                    item["name"],
                    item.get("quantity_text") or "",
                    item.get("category") or DEFAULT_CATEGORY,
                    canonical_name=item.get("canonical_name"),
                    quantity_value=item.get("quantity_value"),
                    quantity_unit=item.get("quantity_unit"),
                    quantity_inferred=item.get("quantity_inferred", False),
                )
        conn.commit()
    return len(items)

def add_inventory_items_batch(household_id, created_by_user_id, items, targets=None):
    """Add multiple inventory items, merging duplicates with compatible quantities.

    targets (optional): Inventory Representation Guard v1 merge-target
    snapshots ({item_id, quantity_value, quantity_unit}) for every item the
    caller's preview predicted would merge into a specific existing row —
    verified for staleness inside this same transaction (same guard every
    other confirm-flow uses) before anything is written. Raises
    StaleSnapshotError (nothing applied) if a target row changed or vanished
    since the preview was built.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets)
            for item in items:
                _merge_or_insert_inventory_in_tx(
                    cur, household_id, created_by_user_id,
                    item["name"],
                    item.get("quantity_text") or "",
                    item.get("category") or DEFAULT_CATEGORY,
                    canonical_name=item.get("canonical_name"),
                    quantity_value=item.get("quantity_value"),
                    quantity_unit=item.get("quantity_unit"),
                    quantity_inferred=item.get("quantity_inferred", False),
                )
        conn.commit()
    return len(items)

def add_or_merge_inventory_item(household_id, created_by_user_id, name, quantity_text, category,
                                 canonical_name=None, quantity_value=None, quantity_unit=None,
                                 quantity_inferred=False):
    """Add item to inventory, merging quantity with an existing entry when safe."""
    category = category or DEFAULT_CATEGORY
    with get_connection() as conn:
        with conn.cursor() as cur:
            _merge_or_insert_inventory_in_tx(
                cur, household_id, created_by_user_id,
                name, quantity_text or "", category,
                canonical_name=canonical_name,
                quantity_value=quantity_value,
                quantity_unit=quantity_unit,
                quantity_inferred=quantity_inferred,
            )
        conn.commit()

# =========================
# MARK / DELETE BATCH
# =========================

def mark_items_batch(household_id, item_ids, completed_by_user_id, targets=None):
    """Mark multiple items as completed in one transaction. Returns count of updated rows.

    targets (optional): snapshot of {item_id, quantity_value, quantity_unit} for
    each item_id, captured when the preview was built. Verified for staleness
    inside this same transaction before anything is written — raises
    StaleSnapshotError (transaction rolled back, nothing applied) if any
    target no longer matches the live row.
    """
    if not item_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "shopping_items", household_id, targets)
            placeholders = ",".join(["%s"] * len(item_ids))
            cur.execute(
                f"UPDATE shopping_items SET is_completed = TRUE, completed_by_user_id = %s, completed_at = NOW() "
                f"WHERE id IN ({placeholders}) AND household_id = %s AND is_completed = FALSE",
                [completed_by_user_id] + list(item_ids) + [household_id]
            )
            count = cur.rowcount
        conn.commit()
    return count

def delete_items_batch(household_id, item_ids, targets=None):
    """Delete multiple shopping items in one transaction. Returns count of deleted rows.

    targets (optional): see mark_items_batch — verified for staleness inside
    this same transaction before anything is deleted.
    """
    if not item_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "shopping_items", household_id, targets)
            placeholders = ",".join(["%s"] * len(item_ids))
            cur.execute(
                f"DELETE FROM shopping_items WHERE id IN ({placeholders}) AND household_id = %s AND is_completed = FALSE",
                list(item_ids) + [household_id]
            )
            count = cur.rowcount
        conn.commit()
    return count

def delete_inventory_item_by_id(item_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM inventory_items WHERE id = %s RETURNING name",
                (item_id,)
            )
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None

def delete_inventory_items_batch(household_id, item_ids, targets=None):
    """Delete multiple inventory items in one transaction. Returns count of deleted rows.

    targets (optional): see mark_items_batch — verified for staleness inside
    this same transaction before anything is deleted. This is what prevents a
    stale "remove everything" preview from deleting a row whose quantity was
    changed by another device after the preview was built.
    """
    if not item_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets)
            placeholders = ",".join(["%s"] * len(item_ids))
            cur.execute(
                f"DELETE FROM inventory_items WHERE id IN ({placeholders}) AND household_id = %s",
                list(item_ids) + [household_id]
            )
            count = cur.rowcount
        conn.commit()
    return count

def apply_inventory_consumption(household_id, updates, delete_item_ids, targets=None):
    """Apply partial consumption updates and full removals in a single transaction.

    updates: list of {item_id, quantity_value, quantity_unit, quantity_text} — sets
    structured quantity fields directly (already computed by the caller), unlike
    update_inventory_items_batch which re-derives them from free text.
    delete_item_ids: item ids consumed down to zero, deleted entirely.
    targets (optional): snapshot of {item_id, quantity_value, quantity_unit} for
    every item this consumption touches (both partially reduced and deleted
    outright) — verified for staleness inside this same transaction before
    anything is written.
    Returns (updated_count, deleted_count).
    """
    if not updates and not delete_item_ids:
        return 0, 0
    updated = 0
    deleted = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets)
            for upd in updates:
                cur.execute(
                    "UPDATE inventory_items SET quantity_text=%s, quantity_value=%s, quantity_unit=%s, "
                    "quantity_inferred=FALSE, updated_at=NOW() WHERE id=%s AND household_id=%s RETURNING id",
                    (upd["quantity_text"], upd["quantity_value"], upd["quantity_unit"], upd["item_id"], household_id)
                )
                if cur.fetchone():
                    updated += 1
            if delete_item_ids:
                placeholders = ",".join(["%s"] * len(delete_item_ids))
                cur.execute(
                    f"DELETE FROM inventory_items WHERE id IN ({placeholders}) AND household_id=%s",
                    list(delete_item_ids) + [household_id]
                )
                deleted = cur.rowcount
        conn.commit()
    return updated, deleted

def apply_compound_inventory_operations(household_id, user_db_id, consume_updates, delete_item_ids, shopping_items, targets=None):
    """Apply partial consumption, full removal, and shopping-list additions in one transaction.

    consume_updates: list of {item_id, quantity_value, quantity_unit, quantity_text} — sets
    structured quantity fields directly (already computed by the caller).
    delete_item_ids: inventory item ids to delete entirely (remove_inventory operations
    plus consume operations that hit zero).
    shopping_items: item dicts (name, category, canonical_name, quantity_value,
    quantity_unit, quantity_inferred) merged/inserted into shopping_items using the same
    safe merge rules as add_shopping_items_batch.
    targets (optional): snapshot of {item_id, quantity_value, quantity_unit} for every
    inventory item this batch touches (consume + remove operations) — verified for
    staleness inside this same transaction before anything is written. New shopping_items
    are plain inserts/merges and need no staleness check.
    Returns (inventory_updated_count, inventory_deleted_count, shopping_added_count).
    """
    if not consume_updates and not delete_item_ids and not shopping_items:
        return 0, 0, 0
    updated = 0
    deleted = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets)
            for upd in consume_updates:
                cur.execute(
                    "UPDATE inventory_items SET quantity_text=%s, quantity_value=%s, quantity_unit=%s, "
                    "quantity_inferred=FALSE, updated_at=NOW() WHERE id=%s AND household_id=%s RETURNING id",
                    (upd["quantity_text"], upd["quantity_value"], upd["quantity_unit"], upd["item_id"], household_id)
                )
                if cur.fetchone():
                    updated += 1
            if delete_item_ids:
                placeholders = ",".join(["%s"] * len(delete_item_ids))
                cur.execute(
                    f"DELETE FROM inventory_items WHERE id IN ({placeholders}) AND household_id=%s",
                    list(delete_item_ids) + [household_id]
                )
                deleted = cur.rowcount
            for item in shopping_items:
                _merge_or_insert_shopping_in_tx(
                    cur, household_id, user_db_id,
                    item["name"],
                    item.get("quantity_text") or "",
                    item.get("category") or DEFAULT_CATEGORY,
                    canonical_name=item.get("canonical_name"),
                    quantity_value=item.get("quantity_value"),
                    quantity_unit=item.get("quantity_unit"),
                    quantity_inferred=item.get("quantity_inferred", False),
                )
        conn.commit()
    return updated, deleted, len(shopping_items)

def apply_inventory_reconciliation(household_id, user_db_id, updates, insert_items, delete_item_ids, targets=None):
    """Apply a full inventory snapshot reconciliation (updates + deletes + inserts)
    in one transaction. Mirrors apply_compound_inventory_operations's shape.

    updates: list of {item_id, quantity_value, quantity_unit, quantity_text} — sets
    structured quantity fields directly (already computed by the caller).
    insert_items: item dicts (name, category, canonical_name, quantity_value,
    quantity_unit, quantity_inferred) merged/inserted via _merge_or_insert_inventory_in_tx.
    delete_item_ids: inventory item ids to delete entirely.
    targets (optional): snapshot of {item_id, quantity_value, quantity_unit} for every
    item this reconciliation touches (updates + deletes) — verified for staleness
    inside this same transaction before anything is written. New insert_items are
    plain inserts/merges and need no staleness check.
    Returns (updated_count, deleted_count, inserted_count).
    """
    if not updates and not delete_item_ids and not insert_items:
        return 0, 0, 0
    updated = 0
    deleted = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets)
            for upd in updates:
                cur.execute(
                    "UPDATE inventory_items SET quantity_text=%s, quantity_value=%s, quantity_unit=%s, "
                    "quantity_inferred=FALSE, updated_at=NOW() WHERE id=%s AND household_id=%s RETURNING id",
                    (upd["quantity_text"], upd["quantity_value"], upd["quantity_unit"], upd["item_id"], household_id)
                )
                if cur.fetchone():
                    updated += 1
            if delete_item_ids:
                placeholders = ",".join(["%s"] * len(delete_item_ids))
                cur.execute(
                    f"DELETE FROM inventory_items WHERE id IN ({placeholders}) AND household_id=%s",
                    list(delete_item_ids) + [household_id]
                )
                deleted = cur.rowcount
            for item in insert_items:
                _merge_or_insert_inventory_in_tx(
                    cur, household_id, user_db_id,
                    item["name"],
                    item.get("quantity_text") or "",
                    item.get("category") or DEFAULT_CATEGORY,
                    canonical_name=item.get("canonical_name"),
                    quantity_value=item.get("quantity_value"),
                    quantity_unit=item.get("quantity_unit"),
                    quantity_inferred=item.get("quantity_inferred", False),
                )
        conn.commit()
    return updated, deleted, len(insert_items)

def _resolve_canonical_name(name, quantity_text, canonical_name=None):
    """Same resolution _merge_or_insert_shopping_in_tx/_merge_or_insert_
    inventory_in_tx fall back to when an item dict has no canonical_name of
    its own — used here (pure, no DB) so the journal snapshot can know which
    canonical-name bucket an add_shopping/add_inventory item belongs to
    BEFORE the merge helper runs."""
    if canonical_name is not None:
        return canonical_name
    normalized = normalize_quantity_fields(name, quantity_text or "", allow_default_unit=True)
    return normalized["canonical_name"]


def _fetch_bucket_rows_in_tx(cur, table, household_id, canonical_name, active_only=False, lock=True):
    """Full snapshot of every row in `table` for this household sharing
    `canonical_name` — never just the first/merge-target row (see
    action_history.py module docstring for why: duplicate representations,
    consume-to-zero, and multiple operations touching the same product in one
    compound action all need the WHOLE bucket, not one row). Returns a list
    of JSON-safe row dicts (quantity_value already a string)."""
    query = (
        f"SELECT id, name, canonical_name, quantity_text, quantity_value, quantity_unit, "
        f"quantity_inferred, category FROM {table} WHERE household_id=%s AND canonical_name=%s"
    )
    params = [household_id, canonical_name]
    if active_only:
        query += " AND is_completed=FALSE"
    query += " ORDER BY id ASC"
    if lock:
        query += " FOR UPDATE"
    cur.execute(query, params)
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": r[0], "household_id": household_id, "name": r[1], "canonical_name": r[2],
            "quantity_text": r[3], "quantity_value": str(r[4]) if r[4] is not None else None,
            "quantity_unit": r[5], "quantity_inferred": r[6], "category": r[7],
        })
    return rows


def _restore_bucket_in_tx(cur, table, household_id, actor_user_id, current_rows, before_rows, is_shopping=False):
    """Apply one canonical-name bucket's restore: delete rows the forward
    action inserted (present now, absent from `before_rows`), update rows
    the forward action changed back to their before values, and reinsert
    rows the forward action deleted (present in `before_rows`, absent now —
    gets a new id, per spec, since ids are SERIAL). Caller has already
    verified `current_rows` matches the post-action snapshot."""
    current_by_id = {r["id"]: r for r in current_rows}
    before_by_id = {r["id"]: r for r in before_rows}

    for row_id in current_by_id:
        if row_id not in before_by_id:
            cur.execute(f"DELETE FROM {table} WHERE id=%s AND household_id=%s", (row_id, household_id))

    for row_id, brow in before_by_id.items():
        value = Decimal(brow["quantity_value"]) if brow["quantity_value"] is not None else None
        if row_id in current_by_id:
            crow = current_by_id[row_id]
            if action_history.row_signature(crow) != action_history.row_signature(brow):
                cur.execute(
                    f"UPDATE {table} SET name=%s, canonical_name=%s, quantity_text=%s, quantity_value=%s, "
                    f"quantity_unit=%s, quantity_inferred=%s, category=%s, updated_at=NOW() "
                    f"WHERE id=%s AND household_id=%s",
                    (brow["name"], brow["canonical_name"], brow["quantity_text"], value,
                     brow["quantity_unit"], brow["quantity_inferred"], brow["category"], row_id, household_id)
                )
        else:
            if is_shopping:
                cur.execute(
                    "INSERT INTO shopping_items (household_id, name, quantity_text, category, created_by_user_id, "
                    "canonical_name, quantity_value, quantity_unit, quantity_inferred, is_completed) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)",
                    (household_id, brow["name"], brow["quantity_text"], brow["category"], actor_user_id,
                     brow["canonical_name"], value, brow["quantity_unit"], brow["quantity_inferred"])
                )
            else:
                cur.execute(
                    "INSERT INTO inventory_items (household_id, name, quantity_text, category, created_by_user_id, "
                    "canonical_name, quantity_value, quantity_unit, quantity_inferred) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (household_id, brow["name"], brow["quantity_text"], brow["category"], actor_user_id,
                     brow["canonical_name"], value, brow["quantity_unit"], brow["quantity_inferred"])
                )


def apply_global_household_operations(household_id, user_db_id, add_shopping_items=None,
                                       add_inventory_items=None, consume_updates=None,
                                       consume_delete_ids=None, inventory_targets=None,
                                       new_expense=None, new_expenses=None, delete_expense_id=None,
                                       delete_expense_snapshot=None):
    """Apply the Global Household Router's combined preview (up to five kinds
    of operations: add_shopping, add_inventory, consume_inventory,
    add_expense, delete_expense) in ONE transaction, and record an Action
    History journal row for it in the SAME transaction (Action History +
    Safe Undo v1) — user_db_id is the actor for that journal row.

    add_shopping_items / add_inventory_items: item dicts merged/inserted via
    the same _merge_or_insert_*_in_tx helpers add_shopping_items_batch/
    add_inventory_items_batch already use.
    consume_updates: list of {item_id, quantity_value, quantity_unit,
    quantity_text} — partial consumption, structured fields set directly.
    consume_delete_ids: inventory item ids consumed down to zero, deleted
    entirely.
    inventory_targets (optional): snapshot of {item_id, quantity_value,
    quantity_unit} for every inventory item this batch touches (both
    consume_updates and consume_delete_ids) — verified for staleness inside
    this same transaction (same guard as apply_compound_inventory_operations)
    before anything is written.
    new_expenses (optional): list of {amount, currency, category,
    description, expense_date} — plain inserts, in order, needs no
    staleness check. new_expense (optional, singular, deprecated): kept for
    backward compatibility with callers/tests predating Multi-Expense Batch
    v1 — normalized into a one-element new_expenses list at the top of this
    function; everything below only ever works off new_expenses.
    delete_expense_id / delete_expense_snapshot (optional): re-verified
    inside this same transaction (row locked FOR UPDATE, same rule as
    delete_expense()) before being deleted — raises StaleSnapshotError if the
    row is gone or its amount/category/expense_date/description no longer
    match the snapshot.

    Both staleness checks run BEFORE any write, so a stale inventory target
    or a stale expense-delete target aborts the whole transaction (rolled
    back automatically since the exception propagates out of the `with
    get_connection()` block) — never a partial apply. The journal INSERT
    happens after every write but still before commit, so a rollback from
    either staleness check or any later error removes the journal row too —
    never data without a journal entry, never a journal entry without data.

    Returns a dict: {"shopping_added": n, "inventory_added": n,
    "inventory_updated": n, "inventory_removed": n, "expense_added_id":
    id_or_None (the first inserted expense, back-compat), "expense_added_ids":
    [id, ...] (every inserted expense, in order), "expense_deleted": bool}.
    """
    add_shopping_items = add_shopping_items or []
    add_inventory_items = add_inventory_items or []
    consume_updates = consume_updates or []
    consume_delete_ids = consume_delete_ids or []
    if new_expenses is None:
        new_expenses = [new_expense] if new_expense is not None else []
    else:
        new_expenses = list(new_expenses)

    inventory_updated = 0
    inventory_removed = 0
    expense_added_id = None
    expense_deleted = False

    shopping_canonical_names = {
        _resolve_canonical_name(item["name"], item.get("quantity_text"), item.get("canonical_name"))
        for item in add_shopping_items
    }
    inventory_canonical_names = {
        _resolve_canonical_name(item["name"], item.get("quantity_text"), item.get("canonical_name"))
        for item in add_inventory_items
    }
    consume_ids = [upd["item_id"] for upd in consume_updates] + list(consume_delete_ids)

    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, inventory_targets)

            if consume_ids:
                placeholders = ",".join(["%s"] * len(consume_ids))
                cur.execute(
                    f"SELECT id, canonical_name FROM inventory_items WHERE id IN ({placeholders}) AND household_id=%s",
                    list(consume_ids) + [household_id]
                )
                for _row_id, cname in cur.fetchall():
                    if cname:
                        inventory_canonical_names.add(cname)

            delete_expense_before = None
            if delete_expense_id is not None:
                cur.execute(
                    "SELECT amount, currency, category, expense_date, description, created_by_user_id "
                    "FROM expenses WHERE id = %s AND household_id = %s FOR UPDATE",
                    (delete_expense_id, household_id)
                )
                row = cur.fetchone()
                if row is None:
                    raise StaleSnapshotError()
                amount, currency, category, expense_date, description, expense_creator_id = row
                snapshot = delete_expense_snapshot or {}
                if (
                    amount != snapshot.get("amount")
                    or category != snapshot.get("category")
                    or expense_date != snapshot.get("expense_date")
                    or (description or None) != (snapshot.get("description") or None)
                ):
                    raise StaleSnapshotError()
                delete_expense_before = {
                    "id": delete_expense_id, "household_id": household_id, "amount": str(amount),
                    "currency": currency, "category": category, "description": description,
                    "expense_date": expense_date.isoformat(), "created_by_user_id": expense_creator_id,
                }

            # Capture the BEFORE snapshot of every touched canonical-name
            # bucket now, locked, before any write below can change them.
            before_inventory_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, cname, lock=True)
                for cname in inventory_canonical_names
            }
            before_shopping_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "shopping_items", household_id, cname, active_only=True, lock=True)
                for cname in shopping_canonical_names
            }

            for upd in consume_updates:
                cur.execute(
                    "UPDATE inventory_items SET quantity_text=%s, quantity_value=%s, quantity_unit=%s, "
                    "quantity_inferred=FALSE, updated_at=NOW() WHERE id=%s AND household_id=%s RETURNING id",
                    (upd["quantity_text"], upd["quantity_value"], upd["quantity_unit"], upd["item_id"], household_id)
                )
                if cur.fetchone():
                    inventory_updated += 1

            if consume_delete_ids:
                placeholders = ",".join(["%s"] * len(consume_delete_ids))
                cur.execute(
                    f"DELETE FROM inventory_items WHERE id IN ({placeholders}) AND household_id=%s",
                    list(consume_delete_ids) + [household_id]
                )
                inventory_removed = cur.rowcount

            for item in add_shopping_items:
                _merge_or_insert_shopping_in_tx(
                    cur, household_id, user_db_id,
                    item["name"],
                    item.get("quantity_text") or "",
                    item.get("category") or DEFAULT_CATEGORY,
                    canonical_name=item.get("canonical_name"),
                    quantity_value=item.get("quantity_value"),
                    quantity_unit=item.get("quantity_unit"),
                    quantity_inferred=item.get("quantity_inferred", False),
                )

            for item in add_inventory_items:
                _merge_or_insert_inventory_in_tx(
                    cur, household_id, user_db_id,
                    item["name"],
                    item.get("quantity_text") or "",
                    item.get("category") or DEFAULT_CATEGORY,
                    canonical_name=item.get("canonical_name"),
                    quantity_value=item.get("quantity_value"),
                    quantity_unit=item.get("quantity_unit"),
                    quantity_inferred=item.get("quantity_inferred", False),
                )

            new_expenses_after = []
            for expense in new_expenses:
                cur.execute(
                    """
                    INSERT INTO expenses
                        (household_id, amount, currency, category, description, expense_date, created_by_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (household_id, expense["amount"], expense["currency"], expense["category"],
                     expense.get("description") or None, expense["expense_date"], user_db_id)
                )
                new_expense_id = cur.fetchone()[0]
                new_expenses_after.append({
                    "id": new_expense_id, "household_id": household_id,
                    "amount": str(expense["amount"]), "currency": expense["currency"],
                    "category": expense["category"], "description": expense.get("description") or None,
                    "expense_date": expense["expense_date"].isoformat(),
                    "created_by_user_id": user_db_id,
                })
            expense_added_ids = [e["id"] for e in new_expenses_after]
            expense_added_id = expense_added_ids[0] if expense_added_ids else None

            if delete_expense_id is not None:
                cur.execute(
                    "DELETE FROM expenses WHERE id = %s AND household_id = %s",
                    (delete_expense_id, household_id)
                )
                expense_deleted = True

            # Capture the AFTER snapshot of the SAME buckets — still inside
            # this transaction, still holding the locks taken above, so this
            # reflects exactly what was just written, nothing more.
            after_inventory_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, cname, lock=False)
                for cname in inventory_canonical_names
            }
            after_shopping_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "shopping_items", household_id, cname, active_only=True, lock=False)
                for cname in shopping_canonical_names
            }

            before_snapshot = {
                "inventory_buckets": before_inventory_buckets,
                "shopping_buckets": before_shopping_buckets,
                "expense_delete": delete_expense_before,
            }
            post_action_snapshot = {
                "inventory_buckets": after_inventory_buckets,
                "shopping_buckets": after_shopping_buckets,
                "expense_adds": new_expenses_after,
            }
            forward_payload = action_history.json_safe({
                "add_shopping_items": add_shopping_items,
                "add_inventory_items": add_inventory_items,
                "consume_updates": consume_updates,
                "consume_delete_ids": list(consume_delete_ids),
                "new_expenses": new_expenses,
                "delete_expense_id": delete_expense_id,
            })
            inverse_payload = {
                "restore_inventory_canonical_names": sorted(inventory_canonical_names),
                "restore_shopping_canonical_names": sorted(shopping_canonical_names),
                "delete_expense_ids": expense_added_ids,
                "restore_expense": delete_expense_before,
            }
            summary = action_history.build_operation_summary(before_snapshot, post_action_snapshot)

            cur.execute(
                """
                INSERT INTO household_action_journal
                    (household_id, actor_user_id, operation_type, forward_payload, inverse_payload,
                     before_snapshot, post_action_snapshot, summary, status, created_at)
                VALUES (%s, %s, 'global_household', %s, %s, %s, %s, %s, 'active', NOW())
                """,
                (household_id, user_db_id, Jsonb(forward_payload), Jsonb(inverse_payload),
                 Jsonb(before_snapshot), Jsonb(post_action_snapshot), Jsonb(summary))
            )

        conn.commit()

    return {
        "shopping_added": len(add_shopping_items),
        "inventory_added": len(add_inventory_items),
        "inventory_updated": inventory_updated,
        "inventory_removed": inventory_removed,
        "expense_added_id": expense_added_id,
        "expense_added_ids": expense_added_ids,
        "expense_deleted": expense_deleted,
    }


# =========================
# ACTION HISTORY + SAFE UNDO v1
# =========================

def get_latest_undoable_action(household_id, actor_user_id):
    """The most recent still-active global_household journal row for THIS
    actor in THIS household, or None. Never returns another user's action —
    undo defaults to "my last action", never "the household's last action"
    (see docs discussion: undoing a partner's action silently is a real risk
    for a two-person household). Read-only, no locking (locking happens
    inside apply_undo_action's own transaction)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, summary FROM household_action_journal
                WHERE household_id=%s AND actor_user_id=%s AND status='active'
                    AND operation_type='global_household'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (household_id, actor_user_id)
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "summary": row[1]}


def apply_undo_action(action_id, household_id, actor_user_id):
    """Undo exactly one journal row, atomically. Re-verifies inside this
    same transaction (every relevant row locked FOR UPDATE) that:
    - the journal row still belongs to this household/actor and is still
      'active' (raises StaleSnapshotError otherwise — also what a duplicate
      confirm on an already-undone action hits, so a repeat confirm can
      never apply the inverse twice);
    - every canonical-name bucket the forward action touched still matches
      its post_action_snapshot exactly (raises StaleSnapshotError, whole
      transaction rolled back, if anything changed since);
    - every added expense (if any — one or several, see Multi-Expense Batch
      v1) still exists and is unchanged, and the deleted expense (if any) is
      still absent.

    Only if every check passes does it restore shopping_items/inventory_items
    rows to their before_snapshot values, delete every expense the forward
    action added, and/or reinsert the expense the forward action deleted —
    then mark the journal row 'undone'. Never calls Gemini. Never partial:
    any failed check aborts the whole transaction before any write.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT household_id, actor_user_id, status, before_snapshot, post_action_snapshot "
                "FROM household_action_journal WHERE id=%s FOR UPDATE",
                (action_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise StaleSnapshotError()
            journal_household_id, journal_actor_id, status, before_snapshot, post_action_snapshot = row
            if journal_household_id != household_id or journal_actor_id != actor_user_id or status != "active":
                raise StaleSnapshotError()

            inventory_before = before_snapshot.get("inventory_buckets") or {}
            inventory_post = post_action_snapshot.get("inventory_buckets") or {}
            shopping_before = before_snapshot.get("shopping_buckets") or {}
            shopping_post = post_action_snapshot.get("shopping_buckets") or {}

            inventory_current = {}
            for cname in set(inventory_before) | set(inventory_post):
                rows = _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, cname, lock=True)
                if not action_history.buckets_match(rows, inventory_post.get(cname, [])):
                    raise StaleSnapshotError()
                inventory_current[cname] = rows

            shopping_current = {}
            for cname in set(shopping_before) | set(shopping_post):
                rows = _fetch_bucket_rows_in_tx(cur, "shopping_items", household_id, cname, active_only=True, lock=True)
                if not action_history.buckets_match(rows, shopping_post.get(cname, [])):
                    raise StaleSnapshotError()
                shopping_current[cname] = rows

            # "expense_adds" (list) is the current shape written by
            # apply_global_household_operations for every forward action —
            # "expense_add" (singular) is only ever read here, never written,
            # kept so a journal row from before Multi-Expense Batch v1 is
            # still undoable.
            expense_add_snapshots = post_action_snapshot.get("expense_adds")
            if expense_add_snapshots is None:
                legacy_expense_add = post_action_snapshot.get("expense_add")
                expense_add_snapshots = [legacy_expense_add] if legacy_expense_add is not None else []
            else:
                expense_add_snapshots = list(expense_add_snapshots)
            for expense_add_snapshot in expense_add_snapshots:
                cur.execute(
                    "SELECT amount, currency, category, description, expense_date "
                    "FROM expenses WHERE id=%s AND household_id=%s FOR UPDATE",
                    (expense_add_snapshot["id"], household_id)
                )
                exp_row = cur.fetchone()
                if exp_row is None:
                    raise StaleSnapshotError()
                amount, currency, category, description, expense_date = exp_row
                if (
                    str(amount) != expense_add_snapshot["amount"]
                    or currency != expense_add_snapshot["currency"]
                    or category != expense_add_snapshot["category"]
                    or (description or None) != (expense_add_snapshot.get("description") or None)
                    or expense_date.isoformat() != expense_add_snapshot["expense_date"]
                ):
                    raise StaleSnapshotError()

            expense_delete_snapshot = before_snapshot.get("expense_delete")
            if expense_delete_snapshot is not None:
                cur.execute(
                    "SELECT id FROM expenses WHERE id=%s AND household_id=%s",
                    (expense_delete_snapshot["id"], household_id)
                )
                if cur.fetchone() is not None:
                    raise StaleSnapshotError()

            # ---- every check passed: apply the restore ----
            for cname, current_rows in inventory_current.items():
                _restore_bucket_in_tx(
                    cur, "inventory_items", household_id, actor_user_id,
                    current_rows, inventory_before.get(cname, []),
                )
            for cname, current_rows in shopping_current.items():
                _restore_bucket_in_tx(
                    cur, "shopping_items", household_id, actor_user_id,
                    current_rows, shopping_before.get(cname, []), is_shopping=True,
                )

            for expense_add_snapshot in expense_add_snapshots:
                cur.execute(
                    "DELETE FROM expenses WHERE id=%s AND household_id=%s",
                    (expense_add_snapshot["id"], household_id)
                )

            if expense_delete_snapshot is not None:
                cur.execute(
                    "INSERT INTO expenses (household_id, amount, currency, category, description, "
                    "expense_date, created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (household_id, Decimal(expense_delete_snapshot["amount"]), expense_delete_snapshot["currency"],
                     expense_delete_snapshot["category"], expense_delete_snapshot.get("description"),
                     expense_delete_snapshot["expense_date"], expense_delete_snapshot.get("created_by_user_id"))
                )

            cur.execute(
                "UPDATE household_action_journal SET status='undone', undone_by_user_id=%s, undone_at=NOW() "
                "WHERE id=%s AND status='active'",
                (actor_user_id, action_id)
            )
        conn.commit()


# =========================
# MANUAL MERGE
# =========================

def execute_merge_shopping(household_id, validated_groups, targets=None):
    """Merge validated groups in shopping_items in one transaction.

    Each group: {item_ids, merged_name, merged_quantity_text, merged_category}.
    First id gets updated; remaining ids get deleted.

    targets (optional): snapshot of {item_id, quantity_value, quantity_unit,
    canonical_name, category} for every source item across validated_groups,
    captured when the merge preview was built. Verified for staleness inside
    this same transaction before anything is written — raises
    StaleSnapshotError (transaction rolled back, nothing applied, not even
    partially) if any target's quantity, unit, canonical_name, or category
    changed, or if any target row vanished, since the preview was shown.
    Returns count of groups merged.
    """
    if not validated_groups:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "shopping_items", household_id, targets,
                                   extra_fields=("canonical_name", "category"))
            for group in validated_groups:
                main_id = group["item_ids"][0]
                rest_ids = group["item_ids"][1:]
                canonical_name = group.get("canonical_name")
                quantity_value = group.get("merged_quantity_value")
                quantity_unit = group.get("merged_quantity_unit")
                if canonical_name is None:
                    normalized = normalize_quantity_fields(group["merged_name"], group["merged_quantity_text"] or "")
                    canonical_name = normalized["canonical_name"]
                    quantity_value = normalized["quantity_value"]
                    quantity_unit = normalized["quantity_unit"]
                cur.execute(
                    "UPDATE shopping_items SET name=%s, quantity_text=%s, category=%s, "
                    "canonical_name=%s, quantity_value=%s, quantity_unit=%s, quantity_inferred=FALSE, updated_at=NOW() "
                    "WHERE id=%s AND household_id=%s AND is_completed=FALSE",
                    (group["merged_name"], group["merged_quantity_text"] or None, group["merged_category"],
                     canonical_name, quantity_value, quantity_unit, main_id, household_id)
                )
                if rest_ids:
                    placeholders = ",".join(["%s"] * len(rest_ids))
                    cur.execute(
                        f"DELETE FROM shopping_items WHERE id IN ({placeholders}) AND household_id=%s AND is_completed=FALSE",
                        rest_ids + [household_id]
                    )
        conn.commit()
    return len(validated_groups)

# =========================
# BATCH UPDATE
# =========================

def update_shopping_items_batch(household_id, updates):
    """Update name/quantity_text/category for multiple active shopping items in one transaction.

    Each update: {item_id, name (or None), quantity_text (or None), category (or None),
    old_value (or None), old_unit (or None)}. Only non-None name/quantity_text/category
    fields are changed. old_value/old_unit are the snapshot quantity captured when the
    preview was built — every update entry is re-verified against the live row inside
    this same transaction before anything is written (StaleSnapshotError + rollback if
    any target changed or vanished). Returns count of rows updated.
    """
    if not updates:
        return 0
    updated = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            targets = [
                {"item_id": upd["item_id"], "quantity_value": upd.get("old_value"), "quantity_unit": upd.get("old_unit")}
                for upd in updates
            ]
            _verify_targets_in_tx(cur, "shopping_items", household_id, targets)
            alias_map = get_household_alias_map(household_id)
            for upd in updates:
                sets, params = [], []
                if upd.get("name") is not None:
                    resolved_name, canonical_name = resolve_item_name(upd["name"], alias_map)
                    sets.append("name = %s")
                    params.append(resolved_name)
                    sets.append("canonical_name = %s")
                    params.append(canonical_name)
                qty = upd.get("quantity_text")
                if qty is not None:
                    value, unit = parse_structured_quantity(qty)
                    sets.append("quantity_text = %s")
                    params.append(format_quantity_display(value, unit) if value is not None else (qty or None))
                    sets.append("quantity_value = %s")
                    params.append(value)
                    sets.append("quantity_unit = %s")
                    params.append(unit)
                    sets.append("quantity_inferred = FALSE")
                if upd.get("category") is not None:
                    sets.append("category = %s")
                    params.append(upd["category"])
                if not sets:
                    continue
                sets.append("updated_at = NOW()")
                params.extend([upd["item_id"], household_id])
                cur.execute(
                    f"UPDATE shopping_items SET {', '.join(sets)} WHERE id = %s AND household_id = %s AND is_completed = FALSE RETURNING id",
                    params,
                )
                if cur.fetchone():
                    updated += 1
        conn.commit()
    return updated


def update_inventory_items_batch(household_id, updates):
    """Update name/quantity_text/category for multiple inventory items in one transaction.

    Each update: {item_id, name (or None), quantity_text (or None), category (or None),
    old_value (or None), old_unit (or None)}. Only non-None name/quantity_text/category
    fields are changed. old_value/old_unit are the snapshot quantity captured when the
    preview was built — every update entry is re-verified against the live row inside
    this same transaction before anything is written (StaleSnapshotError + rollback if
    any target changed or vanished). Returns count of rows updated.
    """
    if not updates:
        return 0
    updated = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            targets = [
                {"item_id": upd["item_id"], "quantity_value": upd.get("old_value"), "quantity_unit": upd.get("old_unit")}
                for upd in updates
            ]
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets)
            alias_map = get_household_alias_map(household_id)
            for upd in updates:
                sets, params = [], []
                if upd.get("name") is not None:
                    resolved_name, canonical_name = resolve_item_name(upd["name"], alias_map)
                    sets.append("name = %s")
                    params.append(resolved_name)
                    sets.append("canonical_name = %s")
                    params.append(canonical_name)
                qty = upd.get("quantity_text")
                if qty is not None:
                    value, unit = parse_structured_quantity(qty)
                    sets.append("quantity_text = %s")
                    params.append(format_quantity_display(value, unit) if value is not None else (qty or None))
                    sets.append("quantity_value = %s")
                    params.append(value)
                    sets.append("quantity_unit = %s")
                    params.append(unit)
                    sets.append("quantity_inferred = FALSE")
                if upd.get("category") is not None:
                    sets.append("category = %s")
                    params.append(upd["category"])
                if not sets:
                    continue
                sets.append("updated_at = NOW()")
                params.extend([upd["item_id"], household_id])
                cur.execute(
                    f"UPDATE inventory_items SET {', '.join(sets)} WHERE id = %s AND household_id = %s RETURNING id",
                    params,
                )
                if cur.fetchone():
                    updated += 1
        conn.commit()
    return updated


def execute_merge_inventory(household_id, validated_groups, targets=None):
    """Merge validated groups in inventory_items in one transaction.

    Each group: {item_ids, merged_name, merged_quantity_text, merged_category}.
    First id gets updated; remaining ids get deleted.

    targets (optional): snapshot of {item_id, quantity_value, quantity_unit,
    canonical_name, category} for every source item across validated_groups,
    captured when the merge preview was built. Verified for staleness inside
    this same transaction before anything is written — raises
    StaleSnapshotError (transaction rolled back, nothing applied, not even
    partially) if any target's quantity, unit, canonical_name, or category
    changed, or if any target row vanished, since the preview was shown.
    Returns count of groups merged.
    """
    if not validated_groups:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets,
                                   extra_fields=("canonical_name", "category"))
            for group in validated_groups:
                main_id = group["item_ids"][0]
                rest_ids = group["item_ids"][1:]
                canonical_name = group.get("canonical_name")
                quantity_value = group.get("merged_quantity_value")
                quantity_unit = group.get("merged_quantity_unit")
                if canonical_name is None:
                    normalized = normalize_quantity_fields(group["merged_name"], group["merged_quantity_text"] or "")
                    canonical_name = normalized["canonical_name"]
                    quantity_value = normalized["quantity_value"]
                    quantity_unit = normalized["quantity_unit"]
                cur.execute(
                    "UPDATE inventory_items SET name=%s, quantity_text=%s, category=%s, "
                    "canonical_name=%s, quantity_value=%s, quantity_unit=%s, quantity_inferred=FALSE, updated_at=NOW() "
                    "WHERE id=%s AND household_id=%s",
                    (group["merged_name"], group["merged_quantity_text"] or None, group["merged_category"],
                     canonical_name, quantity_value, quantity_unit, main_id, household_id)
                )
                if rest_ids:
                    placeholders = ",".join(["%s"] * len(rest_ids))
                    cur.execute(
                        f"DELETE FROM inventory_items WHERE id IN ({placeholders}) AND household_id=%s",
                        rest_ids + [household_id]
                    )
        conn.commit()
    return len(validated_groups)


def execute_inventory_cleanup_merge(household_id, actor_user_id, validated_groups, targets=None):
    """Inventory Cleanup / Merge v1.1 — same write as execute_merge_inventory
    (first item_id per group updated, the rest deleted), PLUS a
    household_action_journal row recorded in the SAME transaction so the
    merge becomes the latest undo-able action (Action History + Safe Undo
    v1). Deliberately reuses that EXISTING journal/undo path end to end,
    not a new one: operation_type is 'global_household' (the only value
    get_latest_undoable_action's query looks for), before/post snapshots
    use the same {"inventory_buckets": {canonical_name: [row, ...]}} shape
    apply_global_household_operations already writes, and
    action_history.build_operation_summary/format_undo_preview render the
    undo preview with zero new formatting code — apply_undo_action restores
    the affected canonical-name bucket(s) exactly as it would for any other
    global_household action, no inventory-merge-specific undo code needed.

    validated_groups/targets: identical shape/contract to
    execute_merge_inventory. Returns count of groups merged.
    """
    if not validated_groups:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, targets,
                                   extra_fields=("canonical_name", "category"))

            canonical_names = set()
            for group in validated_groups:
                cname = group.get("canonical_name")
                if cname is None:
                    cname = normalize_quantity_fields(group["merged_name"], group["merged_quantity_text"] or "")["canonical_name"]
                canonical_names.add(cname)

            before_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, cname, lock=True)
                for cname in canonical_names
            }

            for group in validated_groups:
                main_id = group["item_ids"][0]
                rest_ids = group["item_ids"][1:]
                canonical_name = group.get("canonical_name")
                quantity_value = group.get("merged_quantity_value")
                quantity_unit = group.get("merged_quantity_unit")
                if canonical_name is None:
                    normalized = normalize_quantity_fields(group["merged_name"], group["merged_quantity_text"] or "")
                    canonical_name = normalized["canonical_name"]
                    quantity_value = normalized["quantity_value"]
                    quantity_unit = normalized["quantity_unit"]
                cur.execute(
                    "UPDATE inventory_items SET name=%s, quantity_text=%s, category=%s, "
                    "canonical_name=%s, quantity_value=%s, quantity_unit=%s, quantity_inferred=FALSE, updated_at=NOW() "
                    "WHERE id=%s AND household_id=%s",
                    (group["merged_name"], group["merged_quantity_text"] or None, group["merged_category"],
                     canonical_name, quantity_value, quantity_unit, main_id, household_id)
                )
                if rest_ids:
                    placeholders = ",".join(["%s"] * len(rest_ids))
                    cur.execute(
                        f"DELETE FROM inventory_items WHERE id IN ({placeholders}) AND household_id=%s",
                        rest_ids + [household_id]
                    )

            after_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, cname, lock=False)
                for cname in canonical_names
            }

            before_snapshot = {"inventory_buckets": before_buckets, "shopping_buckets": {}, "expense_delete": None}
            post_action_snapshot = {"inventory_buckets": after_buckets, "shopping_buckets": {}, "expense_adds": []}
            forward_payload = action_history.json_safe({
                "inventory_cleanup_merge_groups": [
                    {"item_ids": g["item_ids"], "merged_name": g["merged_name"],
                     "merged_quantity_text": g["merged_quantity_text"]}
                    for g in validated_groups
                ],
            })
            summary = action_history.build_operation_summary(before_snapshot, post_action_snapshot)

            cur.execute(
                """
                INSERT INTO household_action_journal
                    (household_id, actor_user_id, operation_type, forward_payload, inverse_payload,
                     before_snapshot, post_action_snapshot, summary, status, created_at)
                VALUES (%s, %s, 'global_household', %s, NULL, %s, %s, %s, 'active', NOW())
                """,
                (household_id, actor_user_id, Jsonb(forward_payload),
                 Jsonb(before_snapshot), Jsonb(post_action_snapshot), Jsonb(summary))
            )
        conn.commit()
    return len(validated_groups)


def execute_inventory_rename(household_id, actor_user_id, item_id, new_name, new_canonical_name, target):
    """Inventory Cleanup Admin v1 — rename ONE inventory row's display name
    (and re-derived canonical_name), stale-protected and journal-recorded
    exactly like execute_inventory_cleanup_merge (same operation_type
    'global_household', same {"inventory_buckets": {...}} snapshot shape,
    so apply_undo_action restores it with zero new undo code).

    target: {item_id, quantity_value, quantity_unit, name, canonical_name}
    — the exact row snapshot the preview was built from; re-verified (locked
    FOR UPDATE) inside this transaction via _verify_targets_in_tx before
    anything is written — raises StaleSnapshotError if the row vanished or
    its name/canonical_name/quantity changed since the preview was shown.

    new_canonical_name may equal target["canonical_name"] (the common case:
    a legacy row's stored canonical_name is often already correct, only the
    display name is dirty) or differ from it (a genuinely stale
    canonical_name gets corrected too) — either way both the OLD and NEW
    canonical-name buckets are captured before/after, so undo restores
    correctly regardless of which case this is.
    """
    old_canonical_name = target["canonical_name"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, [target],
                                   extra_fields=("name", "canonical_name"))

            # sorted(), not a raw set iteration — deterministic query order
            # (matters when old/new canonical_name genuinely differ, so
            # two buckets are fetched) rather than relying on Python's
            # randomized string-hash set ordering.
            canonical_names = sorted({old_canonical_name, new_canonical_name})
            before_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, cname, lock=True)
                for cname in canonical_names
            }

            cur.execute(
                "UPDATE inventory_items SET name=%s, canonical_name=%s, updated_at=NOW() "
                "WHERE id=%s AND household_id=%s",
                (new_name, new_canonical_name, item_id, household_id)
            )

            after_buckets = {
                cname: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, cname, lock=False)
                for cname in canonical_names
            }

            before_snapshot = {"inventory_buckets": before_buckets, "shopping_buckets": {}, "expense_delete": None}
            post_action_snapshot = {"inventory_buckets": after_buckets, "shopping_buckets": {}, "expense_adds": []}
            forward_payload = action_history.json_safe({
                "inventory_rename": {"item_id": item_id, "new_name": new_name, "new_canonical_name": new_canonical_name},
            })
            summary = action_history.build_operation_summary(before_snapshot, post_action_snapshot)

            cur.execute(
                """
                INSERT INTO household_action_journal
                    (household_id, actor_user_id, operation_type, forward_payload, inverse_payload,
                     before_snapshot, post_action_snapshot, summary, status, created_at)
                VALUES (%s, %s, 'global_household', %s, NULL, %s, %s, %s, 'active', NOW())
                """,
                (household_id, actor_user_id, Jsonb(forward_payload),
                 Jsonb(before_snapshot), Jsonb(post_action_snapshot), Jsonb(summary))
            )
        conn.commit()
    return True


def execute_inventory_delete(household_id, actor_user_id, item_id, target):
    """Inventory Cleanup Admin v1 — delete ONE inventory row, stale-protected
    and journal-recorded exactly like execute_inventory_cleanup_merge (see
    its docstring) — the row disappearing from its canonical-name bucket's
    before/after snapshot is exactly what apply_undo_action's existing
    generic bucket-restore already knows how to reinsert (new id, per spec),
    so this needs no new undo code either.

    target: {item_id, quantity_value, quantity_unit, name, canonical_name}
    — re-verified (locked FOR UPDATE) before anything is written; raises
    StaleSnapshotError if the row vanished or its name/canonical_name/
    quantity changed since the preview was shown.
    """
    canonical_name = target["canonical_name"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            _verify_targets_in_tx(cur, "inventory_items", household_id, [target],
                                   extra_fields=("name", "canonical_name"))

            before_buckets = {
                canonical_name: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, canonical_name, lock=True),
            }

            cur.execute(
                "DELETE FROM inventory_items WHERE id=%s AND household_id=%s",
                (item_id, household_id)
            )

            after_buckets = {
                canonical_name: _fetch_bucket_rows_in_tx(cur, "inventory_items", household_id, canonical_name, lock=False),
            }

            before_snapshot = {"inventory_buckets": before_buckets, "shopping_buckets": {}, "expense_delete": None}
            post_action_snapshot = {"inventory_buckets": after_buckets, "shopping_buckets": {}, "expense_adds": []}
            forward_payload = action_history.json_safe({
                "inventory_delete": {"item_id": item_id, "name": target.get("name")},
            })
            summary = action_history.build_operation_summary(before_snapshot, post_action_snapshot)

            cur.execute(
                """
                INSERT INTO household_action_journal
                    (household_id, actor_user_id, operation_type, forward_payload, inverse_payload,
                     before_snapshot, post_action_snapshot, summary, status, created_at)
                VALUES (%s, %s, 'global_household', %s, NULL, %s, %s, %s, 'active', NOW())
                """,
                (household_id, actor_user_id, Jsonb(forward_payload),
                 Jsonb(before_snapshot), Jsonb(post_action_snapshot), Jsonb(summary))
            )
        conn.commit()
    return True
