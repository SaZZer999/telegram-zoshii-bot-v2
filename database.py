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
