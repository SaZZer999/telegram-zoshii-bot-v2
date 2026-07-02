import os
import re
from datetime import datetime, timezone
import psycopg

HOUSEHOLD_NAME = "Спільний дім"
DEFAULT_CATEGORY = "Інше їстівне"
VALID_LIST_CONTEXTS = {"shopping_saved", "inventory_saved"}

# =========================
# STRUCTURED QUANTITY HELPERS (DB-local)
#
# Self-contained mirror of bot.py's normalization logic. Duplicated on
# purpose: database.py must not import bot.py (bot.py imports database.py,
# and bot.py's own copy is what pending-preview/RAM code uses — these two
# copies only need to agree on output format, not share code).
# =========================

_NAME_SYNONYMS = {
    "сливки": "вершки",
}

_UNIT_ALIASES = {
    "шт": "шт.", "шт.": "шт.", "штук": "шт.", "штуки": "шт.", "штука": "шт.",
    "л": "л", "літр": "л", "літри": "л", "літра": "л",
    "мл": "мл", "мілілітр": "мл", "мілілітри": "мл", "мілілітрів": "мл",
    "г": "г", "грам": "г", "грами": "г", "грама": "г", "грамів": "г",
    "кг": "кг", "кілограм": "кг", "кілограми": "кг", "кілограмів": "кг",
}

STRUCTURED_UNITS = {"шт.", "л", "мл", "г", "кг"}


def canonicalize_name(name):
    """Lowercase/trim a name and map known synonyms to one canonical form."""
    base = (name or "").strip().lower()
    return _NAME_SYNONYMS.get(base, base)


def parse_structured_quantity(quantity_text):
    """Parse an unambiguous 'value unit' or bare-number quantity_text.

    Returns (value: float|None, unit: str|None). Never raises.
    """
    if not quantity_text or not quantity_text.strip():
        return None, None
    normalized = quantity_text.strip().replace(",", ".")
    parts = normalized.split()
    if len(parts) == 1:
        try:
            return float(parts[0]), None
        except ValueError:
            return None, None
    if len(parts) == 2:
        try:
            value = float(parts[0])
        except ValueError:
            return None, None
        unit = _UNIT_ALIASES.get(parts[1].lower().rstrip("."))
        if unit is None:
            return None, None
        return value, unit
    return None, None


def format_quantity_display(value, unit):
    """Format a numeric value+unit for display: comma decimal, no trailing .0."""
    if value is None:
        return ""
    if value == int(value):
        value_str = str(int(value))
    else:
        value_str = ("%g" % value).replace(".", ",")
    return f"{value_str} {unit}" if unit else value_str


def normalize_quantity_fields(name, quantity_text, allow_default_unit=False):
    """Compute canonical_name/quantity_value/quantity_unit/quantity_inferred/quantity_text.

    allow_default_unit=True applies the "1 шт." default only when quantity_text
    is genuinely blank (new items) — never for backfilling old data.
    """
    canonical_name = canonicalize_name(name)
    value, unit = parse_structured_quantity(quantity_text)
    inferred = False
    if value is None and not (quantity_text or "").strip() and allow_default_unit:
        value, unit, inferred = 1.0, "шт.", True
    display = format_quantity_display(value, unit) if value is not None else (quantity_text or "").strip()
    return {
        "canonical_name": canonical_name,
        "quantity_value": value,
        "quantity_unit": unit,
        "quantity_inferred": inferred,
        "quantity_text": display,
    }


def merge_quantity_values(value_a, unit_a, value_b, unit_b):
    """Return merged (value, unit) if two structured quantities can be safely
    summed, else None. Units must match and be one of the known structured units."""
    if value_a is None or value_b is None:
        return None
    if unit_a != unit_b:
        return None
    if unit_a not in STRUCTURED_UNITS:
        return None
    return round(value_a + value_b, 2), unit_a


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
    household alias (highest priority) -> built-in generic synonym via
    canonicalize_name() -> plain lowercasing. No alias-to-alias chaining: a
    match returns the alias row's stored target directly, one hop only.

    alias_map: {alias_normalized: {"target_display_name":.., "target_canonical_name":..}},
    e.g. from get_household_alias_map(). Pass {} or None when there is no
    household context. Returns (display_name, canonical_name). Never raises.
    """
    key = normalize_alias_text(name)
    if alias_map and key is not None and key in alias_map:
        entry = alias_map[key]
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
        "SELECT id, category, quantity_value, quantity_unit, quantity_inferred FROM inventory_items "
        "WHERE household_id=%s AND canonical_name=%s ORDER BY id ASC",
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


def _verify_targets_in_tx(cur, table, household_id, targets):
    """Re-read `targets` for `table` ("shopping_items" or "inventory_items")
    inside the caller's open transaction and lock the rows (FOR UPDATE) so no
    concurrent write can slip in before this transaction commits. Raises
    StaleSnapshotError if any target row is missing or its quantity_value/
    quantity_unit no longer match the snapshot. No-ops for empty/None targets.

    targets: list of dicts with item_id, quantity_value, quantity_unit — the
    values captured when the preview/snapshot was built.
    """
    if not targets:
        return
    ids = [t["item_id"] for t in targets]
    placeholders = ",".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT id, quantity_value, quantity_unit FROM {table} "
        f"WHERE id IN ({placeholders}) AND household_id=%s FOR UPDATE",
        list(ids) + [household_id],
    )
    current = {
        row[0]: (float(row[1]) if row[1] is not None else None, row[2])
        for row in cur.fetchall()
    }
    for t in targets:
        seen = current.get(t["item_id"])
        if seen is None or seen != (t.get("quantity_value"), t.get("quantity_unit")):
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

def add_inventory_items_batch(household_id, created_by_user_id, items):
    """Add multiple inventory items, merging duplicates with compatible quantities."""
    with get_connection() as conn:
        with conn.cursor() as cur:
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

# =========================
# MANUAL MERGE
# =========================

def execute_merge_shopping(household_id, validated_groups):
    """Merge validated groups in shopping_items in one transaction.

    Each group: {item_ids, merged_name, merged_quantity_text, merged_category}.
    First id gets updated; remaining ids get deleted.
    Returns count of groups merged.
    """
    if not validated_groups:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
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


def execute_merge_inventory(household_id, validated_groups):
    """Merge validated groups in inventory_items in one transaction.

    Each group: {item_ids, merged_name, merged_quantity_text, merged_category}.
    First id gets updated; remaining ids get deleted.
    Returns count of groups merged.
    """
    if not validated_groups:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
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
