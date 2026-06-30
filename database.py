import os
import psycopg

HOUSEHOLD_NAME = "Спільний дім"

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
                SELECT id, name, quantity_text, category
                FROM shopping_items
                WHERE household_id = %s AND is_completed = FALSE
                ORDER BY created_at ASC
                """,
                (household_id,)
            )
            rows = cur.fetchall()
    return [{"id": r[0], "name": r[1], "quantity_text": r[2], "category": r[3]} for r in rows]

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
                SELECT id, name, quantity_text, category
                FROM inventory_items
                WHERE household_id = %s
                ORDER BY category, name ASC
                """,
                (household_id,)
            )
            rows = cur.fetchall()
    return [{"id": r[0], "name": r[1], "quantity_text": r[2], "category": r[3]} for r in rows]

def add_inventory_items_batch(household_id, created_by_user_id, items):
    """Insert multiple inventory items in one transaction. Returns count of added rows."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for item in items:
                cur.execute(
                    """
                    INSERT INTO inventory_items (household_id, name, quantity_text, category, created_by_user_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        household_id,
                        item["name"],
                        item["quantity_text"] or None,
                        item.get("category") or "Інше їстівне",
                        created_by_user_id,
                    )
                )
        conn.commit()
    return len(items)

_MERGEABLE_UNITS = {"л", "мл", "г", "кг", "шт."}

def _parse_quantity(qty_text):
    """Parse 'number unit' string. Returns (float, str) or (None, None)."""
    if not qty_text:
        return None, None
    normalized = qty_text.strip().replace(",", ".")
    parts = normalized.split()
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0]), parts[1]
    except ValueError:
        return None, None

def add_or_merge_inventory_item(household_id, created_by_user_id, name, quantity_text, category):
    """Add item to inventory, merging quantity with an existing entry when safe.

    Merges only when: same normalized name, same category, both quantities have the
    same unit from _MERGEABLE_UNITS and numeric values that can be summed.
    Returns 'merged' or 'added'.
    """
    category = category or "Інше їстівне"
    norm_name = name.strip().lower()
    new_val, new_unit = _parse_quantity(quantity_text)

    with get_connection() as conn:
        with conn.cursor() as cur:
            if new_val is not None and new_unit in _MERGEABLE_UNITS:
                cur.execute(
                    """
                    SELECT id, quantity_text
                    FROM inventory_items
                    WHERE household_id = %s
                      AND LOWER(TRIM(name)) = %s
                      AND category = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (household_id, norm_name, category)
                )
                existing = cur.fetchone()
                if existing:
                    ex_id, ex_qty_text = existing
                    ex_val, ex_unit = _parse_quantity(ex_qty_text)
                    if ex_val is not None and ex_unit == new_unit:
                        merged = round(ex_val + new_val, 1)
                        if merged == int(merged):
                            merged_qty = f"{int(merged)} {new_unit}"
                        else:
                            merged_qty = str(merged).replace(".", ",") + f" {new_unit}"
                        cur.execute(
                            "UPDATE inventory_items SET quantity_text = %s, updated_at = NOW() WHERE id = %s",
                            (merged_qty, ex_id)
                        )
                        conn.commit()
                        return "merged"
            cur.execute(
                """
                INSERT INTO inventory_items (household_id, name, quantity_text, category, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (household_id, name, quantity_text or None, category, created_by_user_id)
            )
        conn.commit()
    return "added"

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

def add_shopping_items_batch(household_id, created_by_user_id, items):
    """Insert multiple items in one transaction. Returns count of added rows."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for item in items:
                cur.execute(
                    """
                    INSERT INTO shopping_items (household_id, name, quantity_text, category, created_by_user_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        household_id,
                        item["name"],
                        item["quantity_text"] or None,
                        item.get("category") or "Інше їстівне",
                        created_by_user_id,
                    )
                )
        conn.commit()
    return len(items)

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
