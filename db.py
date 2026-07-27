"""
Database layer for the GTM Product & Price Catalog.

Products now live in a small SQLite database (DATABASE_FILE) instead of
being re-read from Excel on every request. The Excel upload in the admin
panel still works exactly as before, from the user's point of view -
it's just treated as a "bulk import" that refreshes the database, rather
than being the live data source itself.

Every function here returns/accepts dicts using the SAME key names the
templates and /api/prices already expect ("Product ID", "Product Name",
"UPC", "Unit", "Retail", "Wholesale", "Category", "Status"), so nothing
downstream (index.html, admin.html, Excel's "Get Data from Web") needs
to change.
"""

import os
import re
import sqlite3

import pandas as pd

from config import DATABASE_FILE, EXCEL_FILE


# ------------------------
# Connection / schema
# ------------------------

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the products table if it doesn't exist yet, and, on a totally
    fresh database, auto-import the existing uploads/sale_price_catalog.xlsx
    if one is already sitting there (one-time migration, no manual steps)."""

    conn = get_db_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT DEFAULT '',
            product_name TEXT DEFAULT '',
            upc INTEGER DEFAULT 0,
            unit TEXT DEFAULT '-',
            retail REAL DEFAULT 0,
            wholesale REAL DEFAULT 0,
            category TEXT DEFAULT 'General',
            status TEXT DEFAULT 'In Stock'
        )
    """)

    # Append-only log of every retail/wholesale change. product_id here is
    # the business key ("GTM - 0001"), NOT the products.id autoincrement pk -
    # that way history survives even if a product is deleted and re-added
    # under the same catalog ID.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            product_name TEXT DEFAULT '',
            old_retail REAL,
            new_retail REAL,
            old_wholesale REAL,
            new_wholesale REAL,
            changed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source TEXT DEFAULT 'manual'
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_history_product_id
        ON price_history(product_id)
    """)

    # Sales-rep order list ("cart"). rep_name is required at submit time.
    # No customer name / login for now - kept intentionally simple.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_name TEXT NOT NULL,
            status TEXT DEFAULT 'New',
            total_retail REAL DEFAULT 0,
            total_wholesale REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Managed list of sales reps (e.g. "T-1", "Myu Latt Aung"). Reps pick
    # from this list at order time - they can't type a free-text name.
    # Deactivating a rep (active=0) removes them from the picker for NEW
    # orders without touching past orders, which store the rep label as
    # a plain snapshot string on orders.rep_name (same pattern as product
    # name/price snapshots elsewhere in this app).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_reps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)

    # MIGRATION: orders.outlet_name didn't exist in earlier deploys.
    # CREATE TABLE IF NOT EXISTS above does nothing for an orders table
    # that already exists on a live database - so check for the column
    # explicitly and add it if missing, without touching existing rows.
    existing_columns = [row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "outlet_name" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN outlet_name TEXT DEFAULT ''")

    # MIGRATION: duplicate-submission guard. If a rep's connection drops
    # right as an order finishes submitting, or a retry/double-tap slips
    # past the client-side button-disable, this lets the server recognize
    # "I've already processed this exact submission" instead of creating
    # a second order.
    if "idempotency_key" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT DEFAULT NULL")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key)")

    # Each line item snapshots the product name + price AT ORDER TIME, so
    # editing a product's price later never rewrites history of what was
    # actually ordered/sold.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id TEXT,
            product_name TEXT,
            unit_retail REAL,
            unit_wholesale REAL,
            quantity INTEGER,
            line_retail REAL,
            line_wholesale REAL,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_order_items_order_id
        ON order_items(order_id)
    """)

    conn.commit()
    conn.close()

    if product_count() == 0 and os.path.exists(EXCEL_FILE):
        import_excel_into_db(EXCEL_FILE, replace=True)


# ------------------------
# Row <-> dict mapping
# ------------------------

def _row_to_dict(row):
    return {
        "id": row["id"],
        "Product ID": row["product_id"],
        "Product Name": row["product_name"],
        "UPC": row["upc"],
        "Unit": row["unit"],
        "Retail": row["retail"],
        "Wholesale": row["wholesale"],
        "Category": row["category"],
        "Status": row["status"],
    }


def _log_price_change(conn, product_id, product_name, old_retail, new_retail,
                       old_wholesale, new_wholesale, source):
    """Insert a price_history row, but only if retail or wholesale actually
    changed. Caller is responsible for commit/close (runs on an open conn
    so it shares a transaction with the products write)."""

    if old_retail == new_retail and old_wholesale == new_wholesale:
        return

    conn.execute(
        """
        INSERT INTO price_history
            (product_id, product_name, old_retail, new_retail,
             old_wholesale, new_wholesale, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            product_name,
            old_retail,
            new_retail,
            old_wholesale,
            new_wholesale,
            source,
        ),
    )


# ------------------------
# Reads
# ------------------------

def product_count():
    conn = get_db_connection()
    count = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    conn.close()
    return count


def get_all_products():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM products ORDER BY id ASC").fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_categories():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category != '' ORDER BY category ASC"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


def get_product(product_pk):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_pk,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_price_history(product_id, limit=100):
    """All logged price changes for a given catalog Product ID (business
    key, e.g. 'GTM - 0001'), most recent first."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT * FROM price_history
        WHERE product_id = ?
        ORDER BY changed_at DESC, id DESC
        LIMIT ?
        """,
        (product_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_price_changes(limit=50):
    """Most recent price changes across all products - handy for an admin
    'recent activity' feed."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT * FROM price_history
        ORDER BY changed_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_db_connection()

    total = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
    categories = conn.execute(
        "SELECT COUNT(DISTINCT category) AS c FROM products"
    ).fetchone()["c"]
    instock = conn.execute(
        "SELECT COUNT(*) AS c FROM products WHERE status = 'In Stock'"
    ).fetchone()["c"]
    outofstock = conn.execute(
        "SELECT COUNT(*) AS c FROM products WHERE status = 'Out Of Stock'"
    ).fetchone()["c"]

    conn.close()

    return {
        "products": total,
        "categories": categories,
        "instock": instock,
        "outofstock": outofstock,
    }


_GTM_ID_PATTERN = re.compile(r"^gtm\s*-\s*(\d+)$", re.IGNORECASE)


def get_next_product_id():
    """Compute the next auto-generated Product ID, e.g. 'GTM - 0226'.

    Scans existing product_id values for a 'GTM - ####' style pattern -
    tolerant of spacing/case variants (gtm-0001, GTM-0001, GTM - 0001, all
    match) - ignores anything that doesn't match (like legacy IDs that
    don't follow this scheme), and returns one past the highest number
    found. New IDs are always generated in the canonical 'GTM - ####'
    form. Padding starts at 4 digits and grows naturally past 9999
    (GTM - 10000, GTM - 10001, ...) since zfill only pads, never truncates.
    """

    conn = get_db_connection()
    rows = conn.execute("SELECT product_id FROM products").fetchall()
    conn.close()

    max_num = 0

    for row in rows:
        value = (row["product_id"] or "").strip()
        match = _GTM_ID_PATTERN.match(value)
        if match:
            num = int(match.group(1))
            if num > max_num:
                max_num = num

    next_num = max_num + 1

    return f"GTM - {str(next_num).zfill(4)}"


# ------------------------
# Sales reps (managed list, no free typing at order time)
# ------------------------

def get_all_reps():
    """All reps, active and inactive - for the admin management page."""
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM sales_reps ORDER BY code ASC, name ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_reps():
    """Only active reps - this is what populates the order-form picker."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM sales_reps WHERE active = 1 ORDER BY code ASC, name ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rep(rep_id):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM sales_reps WHERE id = ?", (rep_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def rep_label(rep):
    """The exact string stored on an order / shown in the picker,
    e.g. 'T-1 (Myu Latt Aung)'."""
    code = (rep.get("code") or "").strip()
    name = (rep.get("name") or "").strip()
    if code and name:
        return f"{code} ({name})"
    return code or name


def add_rep(code, name):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO sales_reps (code, name, active) VALUES (?, ?, 1)",
        (code, name),
    )
    conn.commit()
    conn.close()


def update_rep(rep_id, code, name, active):
    conn = get_db_connection()
    conn.execute(
        "UPDATE sales_reps SET code = ?, name = ?, active = ? WHERE id = ?",
        (code, name, 1 if active else 0, rep_id),
    )
    conn.commit()
    conn.close()


def delete_rep(rep_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM sales_reps WHERE id = ?", (rep_id,))
    conn.commit()
    conn.close()


# ------------------------
# Sales rep orders ("cart")
# ------------------------

def create_order(rep_name, cart_items, outlet_name="", idempotency_key=None):
    """Create an order from a list of {"product_pk": int, "quantity": int}.

    SECURITY: unit prices are NEVER taken from the client. For every line,
    we look up the product's CURRENT price by its internal id and use
    that - so a tampered request claiming a lower price is simply ignored.

    rep_name is expected to already be validated against the active
    sales_reps list by the caller (app.py) before this is called - this
    function just stores whatever string it's given.

    idempotency_key: if provided and an order already exists with this
    exact key, that existing order is returned unchanged instead of
    creating a duplicate - protects against retries after a dropped
    response, or a double-tap that slips past the client-side button
    disable.

    Returns (order_id, total_retail, total_wholesale) on success, or
    (None, None, None) if there were no valid items to order.
    """

    conn = get_db_connection()

    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            conn.close()
            return existing["id"], existing["total_retail"], existing["total_wholesale"]

    line_items = []
    total_retail = 0.0
    total_wholesale = 0.0

    for entry in cart_items:
        try:
            pk = int(entry.get("product_pk"))
            qty = int(entry.get("quantity"))
        except (TypeError, ValueError):
            continue

        if qty <= 0:
            continue

        product = conn.execute(
            "SELECT * FROM products WHERE id = ?", (pk,)
        ).fetchone()

        if product is None:
            continue

        unit_retail = product["retail"]
        unit_wholesale = product["wholesale"]
        line_retail = unit_retail * qty
        line_wholesale = unit_wholesale * qty

        line_items.append({
            "product_id": product["product_id"],
            "product_name": product["product_name"],
            "unit_retail": unit_retail,
            "unit_wholesale": unit_wholesale,
            "quantity": qty,
            "line_retail": line_retail,
            "line_wholesale": line_wholesale,
        })

        total_retail += line_retail
        total_wholesale += line_wholesale

    if not line_items:
        conn.close()
        return None, None, None

    conn.execute(
        """
        INSERT INTO orders (rep_name, outlet_name, total_retail, total_wholesale, idempotency_key)
        VALUES (?, ?, ?, ?, ?)
        """,
        (rep_name, outlet_name, total_retail, total_wholesale, idempotency_key),
    )

    order_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    for item in line_items:
        conn.execute(
            """
            INSERT INTO order_items
                (order_id, product_id, product_name, unit_retail, unit_wholesale,
                 quantity, line_retail, line_wholesale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                item["product_id"],
                item["product_name"],
                item["unit_retail"],
                item["unit_wholesale"],
                item["quantity"],
                item["line_retail"],
                item["line_wholesale"],
            ),
        )

    conn.commit()
    conn.close()

    return order_id, total_retail, total_wholesale


def get_all_orders(date_filter=None):
    """Order list for the admin Orders page, newest first, with item count.
    date_filter: 'YYYY-MM-DD' string to restrict to one day, or None for all."""

    conn = get_db_connection()

    if date_filter:
        rows = conn.execute(
            """
            SELECT o.*, COUNT(oi.id) AS item_count
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.id
            WHERE date(o.created_at) = ?
            GROUP BY o.id
            ORDER BY o.created_at DESC, o.id DESC
            """,
            (date_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT o.*, COUNT(oi.id) AS item_count
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.id
            GROUP BY o.id
            ORDER BY o.created_at DESC, o.id DESC
            """
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_order_summary(date_filter=None):
    """Aggregate counts for the orders list - total orders, distinct
    outlets, distinct reps, total sales - optionally filtered to one day."""

    conn = get_db_connection()
    where_clause = "WHERE date(created_at) = ?" if date_filter else ""
    params = (date_filter,) if date_filter else ()

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS order_count,
            COUNT(DISTINCT outlet_name) AS outlet_count,
            COUNT(DISTINCT rep_name) AS rep_count,
            COALESCE(SUM(total_retail), 0) AS total_retail,
            COALESCE(SUM(total_wholesale), 0) AS total_wholesale
        FROM orders
        {where_clause}
        """,
        params,
    ).fetchone()

    conn.close()
    return dict(row)


def get_orders_by_rep(date_filter=None):
    """Per-rep breakdown: order count, distinct outlets, total sales -
    answers 'how many orders / outlets from which rep', optionally
    filtered to one day."""

    conn = get_db_connection()
    where_clause = "WHERE date(created_at) = ?" if date_filter else ""
    params = (date_filter,) if date_filter else ()

    rows = conn.execute(
        f"""
        SELECT
            rep_name,
            COUNT(*) AS order_count,
            COUNT(DISTINCT outlet_name) AS outlet_count,
            COALESCE(SUM(total_retail), 0) AS total_retail
        FROM orders
        {where_clause}
        GROUP BY rep_name
        ORDER BY total_retail DESC
        """,
        params,
    ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_order_dates():
    """Distinct dates that have at least one order, newest first - used
    to populate the date filter dropdown."""

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT DISTINCT date(created_at) AS d FROM orders ORDER BY d DESC"
    ).fetchall()
    conn.close()
    return [r["d"] for r in rows]


def get_order(order_id):
    """A single order plus its line items, or (None, None) if not found."""

    conn = get_db_connection()

    order = conn.execute(
        "SELECT * FROM orders WHERE id = ?", (order_id,)
    ).fetchone()

    if order is None:
        conn.close()
        return None, None

    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC",
        (order_id,),
    ).fetchall()

    conn.close()

    return dict(order), [dict(i) for i in items]


def update_order_status(order_id, status):
    conn = get_db_connection()
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()


def delete_order(order_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()


# ------------------------
# Writes: single-product CRUD (new admin features)
# ------------------------

def add_product(data):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO products
            (product_id, product_name, upc, unit, retail, wholesale, category, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("product_id", ""),
            data.get("product_name", ""),
            data.get("upc", 0),
            data.get("unit", "-"),
            data.get("retail", 0),
            data.get("wholesale", 0),
            data.get("category", "General"),
            data.get("status", "In Stock"),
        ),
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    return new_id


def update_product(product_pk, data):
    conn = get_db_connection()

    # Fetch the pre-edit row so we can detect and log a price change.
    existing = conn.execute(
        "SELECT * FROM products WHERE id = ?", (product_pk,)
    ).fetchone()

    conn.execute(
        """
        UPDATE products SET
            product_id = ?,
            product_name = ?,
            upc = ?,
            unit = ?,
            retail = ?,
            wholesale = ?,
            category = ?,
            status = ?
        WHERE id = ?
        """,
        (
            data.get("product_id", ""),
            data.get("product_name", ""),
            data.get("upc", 0),
            data.get("unit", "-"),
            data.get("retail", 0),
            data.get("wholesale", 0),
            data.get("category", "General"),
            data.get("status", "In Stock"),
            product_pk,
        ),
    )

    if existing is not None:
        _log_price_change(
            conn,
            product_id=data.get("product_id", "") or existing["product_id"],
            product_name=data.get("product_name", "") or existing["product_name"],
            old_retail=existing["retail"],
            new_retail=data.get("retail", 0),
            old_wholesale=existing["wholesale"],
            new_wholesale=data.get("wholesale", 0),
            source="manual",
        )

    conn.commit()
    conn.close()


def delete_product(product_pk):
    conn = get_db_connection()
    conn.execute("DELETE FROM products WHERE id = ?", (product_pk,))
    conn.commit()
    conn.close()


# ------------------------
# Bulk import (the existing "upload Excel" admin feature)
# ------------------------

def import_excel_into_db(path, replace=True, source="excel_upload"):
    """Read a validated Excel file and load it into the database.

    This is an UPSERT keyed on "Product ID" (the business key, e.g.
    "GTM - 0001") rather than a wipe-and-reinsert:

    - Existing products keep their internal `id` (products.id) across
      uploads, so admin edit links and price_history stay valid.
    - Any row whose Retail or Wholesale differs from what's currently in
      the database gets a price_history entry logged automatically,
      before being overwritten - this is the "diff detection" step.
    - New Product IDs are inserted as new rows.
    - replace=True (matches old behavior): any product currently in the
      DB whose Product ID is NOT present in this upload gets marked
      'Out Of Stock' rather than deleted, so it doesn't vanish from
      price history. Pass replace=False to skip this step entirely
      (pure "merge in whatever's in the file" import).
    """

    df = pd.read_excel(path, sheet_name="Product Catalog")

    df["Product ID"] = df["Product ID"].fillna("")
    df["Product Name"] = df["Product Name"].fillna("")
    df["UPC"] = df["UPC"].fillna(0).astype(int)
    df["Unit"] = df["Unit"].fillna("-")
    df["Retail"] = df["Retail"].fillna(0)
    df["Wholesale"] = df["Wholesale"].fillna(0)
    df["Category"] = df["Category"].fillna("General")
    df["Status"] = (
        df["Status"].fillna("In Stock").astype(str).str.strip().str.title()
    )

    conn = get_db_connection()

    seen_product_ids = []

    for _, row in df.iterrows():
        product_id = str(row["Product ID"])
        product_name = str(row["Product Name"])
        upc = int(row["UPC"])
        unit = str(row["Unit"])
        retail = float(row["Retail"])
        wholesale = float(row["Wholesale"])
        category = str(row["Category"])
        status = str(row["Status"])

        seen_product_ids.append(product_id)

        existing = conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()

        if existing is not None:
            _log_price_change(
                conn,
                product_id=product_id,
                product_name=product_name,
                old_retail=existing["retail"],
                new_retail=retail,
                old_wholesale=existing["wholesale"],
                new_wholesale=wholesale,
                source=source,
            )

            conn.execute(
                """
                UPDATE products SET
                    product_name = ?,
                    upc = ?,
                    unit = ?,
                    retail = ?,
                    wholesale = ?,
                    category = ?,
                    status = ?
                WHERE product_id = ?
                """,
                (product_name, upc, unit, retail, wholesale, category, status, product_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO products
                    (product_id, product_name, upc, unit, retail, wholesale, category, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (product_id, product_name, upc, unit, retail, wholesale, category, status),
            )

    if replace and seen_product_ids:
        placeholders = ",".join("?" for _ in seen_product_ids)
        conn.execute(
            f"""
            UPDATE products
            SET status = 'Out Of Stock'
            WHERE product_id NOT IN ({placeholders})
              AND status != 'Out Of Stock'
            """,
            seen_product_ids,
        )

    conn.commit()
    conn.close()

    return len(df)
