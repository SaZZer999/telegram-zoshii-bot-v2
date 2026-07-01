import os
import psycopg

HOUSEHOLD_NAME = "Спільний дім"
DEFAULT_CATEGORY = "Інше їстівне"

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

def mark_items_batch(item_ids, completed_by_user_id):
    """Mark multiple items as completed in one transaction. Returns count of updated rows."""
    if not item_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(item_ids))
            cur.execute(
                f"UPDATE shopping_items SET is_completed = TRUE, completed_by_user_id = %s, completed_at = NOW() WHERE id IN ({placeholders}) AND is_completed = FALSE",
                [completed_by_user_id] + list(item_ids)
            )
            count = cur.rowcount
        conn.commit()
    return count

def delete_items_batch(item_ids):
    """Delete multiple shopping items in one transaction. Returns count of deleted rows."""
    if not item_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(item_ids))
            cur.execute(
                f"DELETE FROM shopping_items WHERE id IN ({placeholders}) AND is_completed = FALSE",
                list(item_ids)
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

def delete_inventory_items_batch(item_ids):
    """Delete multiple inventory items in one transaction. Returns count of deleted rows."""
    if not item_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(item_ids))
            cur.execute(
                f"DELETE FROM inventory_items WHERE id IN ({placeholders})",
                list(item_ids)
            )
            count = cur.rowcount
        conn.commit()
    return count

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

    Each update: {item_id, name (or None), quantity_text (or None), category (or None)}.
    Only non-None fields are changed. Returns count of rows updated.
    """
    if not updates:
        return 0
    updated = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for upd in updates:
                sets, params = [], []
                if upd.get("name") is not None:
                    sets.append("name = %s")
                    params.append(upd["name"])
                    sets.append("canonical_name = %s")
                    params.append(canonicalize_name(upd["name"]))
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

    Each update: {item_id, name (or None), quantity_text (or None), category (or None)}.
    Only non-None fields are changed. Returns count of rows updated.
    """
    if not updates:
        return 0
    updated = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for upd in updates:
                sets, params = [], []
                if upd.get("name") is not None:
                    sets.append("name = %s")
                    params.append(upd["name"])
                    sets.append("canonical_name = %s")
                    params.append(canonicalize_name(upd["name"]))
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
