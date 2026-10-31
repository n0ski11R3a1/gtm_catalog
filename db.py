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
import hashlib
import json
import math
from datetime import date
import math

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
            status TEXT DEFAULT 'In Stock',
            generation INTEGER DEFAULT 1
        )
    """)

    # Append-only log of every retail/wholesale change. product_id here is
    # the business key ("GTM - 0001"), NOT the products.id autoincrement pk -
    # that way history survives even if a product is deleted and re-added
    # under the same catalog ID.
    # generation distinguishes different products that reused the same Product ID.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            product_name TEXT DEFAULT '',
            old_retail REAL,
            new_retail REAL,
            old_wholesale REAL,
            new_wholesale REAL,
            changed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source TEXT DEFAULT 'manual'
        )
    """)

    # Activity feed for the notification bell: new products added + price
    # changes, in one unified stream so the UI only has to query one
    # table. product_id + generation is the composite key.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            product_id TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            product_name TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # MIGRATION: generation column for products - distinguishes different
    # products that reused the same Product ID. Default 1 for existing rows.
    product_columns = [row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()]
    if "generation" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN generation INTEGER DEFAULT 1")

    # MIGRATION: generation column for price_history - matches products.generation
    ph_columns = [row["name"] for row in conn.execute("PRAGMA table_info(price_history)").fetchall()]
    if "generation" not in ph_columns:
        conn.execute("ALTER TABLE price_history ADD COLUMN generation INTEGER DEFAULT 1")

    # MIGRATION: generation column for activity_log - matches products.generation
    al_columns = [row["name"] for row in conn.execute("PRAGMA table_info(activity_log)").fetchall()]
    if "generation" not in al_columns:
        conn.execute("ALTER TABLE activity_log ADD COLUMN generation INTEGER DEFAULT 1")

    # MIGRATION: source column for activity_log - tracks where the event originated
    if "source" not in al_columns:
        conn.execute("ALTER TABLE activity_log ADD COLUMN source TEXT DEFAULT 'manual'")

    # Now create indexes after migrations ensure columns exist
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_history_product_id_gen
        ON price_history(product_id, generation)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_activity_log_product_id_gen
        ON activity_log(product_id, generation)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_activity_log_created_at
        ON activity_log(created_at DESC)
    """)

    # Single-row counter backing get_next_product_id(). Unlike scanning
    # products.product_id for the current max (the old approach), this
    # value only ever goes up - once a number is handed out it's gone for
    # good, even if that product is later deleted. Without this, deleting
    # the highest-numbered product would make its old ID look "available"
    # again to the next scan, and a brand new, unrelated product could end
    # up reusing an ID that already appears in price_history/activity_log
    # for something else entirely.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS id_counter (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_number INTEGER NOT NULL
        )
    """)

    # MIGRATION / SELF-REPAIR: always keep the counter ahead of every GTM
    # number ever used. The original migration only seeded a missing row;
    # on a fresh database that row was created before the initial Excel
    # import, leaving it at 1 even though the import loaded hundreds of IDs.
    counter_row = conn.execute(
        "SELECT next_number FROM id_counter WHERE id = 1"
    ).fetchone()
    max_num = _highest_historical_gtm_number(conn)
    if counter_row is None:
        conn.execute(
            "INSERT INTO id_counter (id, next_number) VALUES (1, ?)",
            (max_num + 1,),
        )
    elif counter_row["next_number"] <= max_num:
        conn.execute(
            "UPDATE id_counter SET next_number = ? WHERE id = 1",
            (max_num + 1,),
        )

    # MIGRATION: prevent two products from ever sharing the same business
    # Product ID going forward. Nothing in the original schema enforced
    # this - it's what let a reused/duplicate ID slip in silently (see the
    # id_counter repair note above). A blank product_id ('') is allowed to
    # repeat (multiple legacy rows may have never had one assigned), so
    # the unique index only applies to non-empty values via a partial
    # index - SQLite supports WHERE clauses on indexes for exactly this.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_products_product_id_unique
        ON products(product_id)
        WHERE product_id != ''
    """)

    # Product IDs are looked up with spaces ignored, so the database must
    # enforce that same identity rule. Without this second index, both
    # "GTM - 0003" and "GTM-0003" can exist as separate rows even though
    # the public catalog and image paths treat them as the same product.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_products_product_id_normalized
        ON products(REPLACE(LOWER(product_id), ' ', ''))
        WHERE TRIM(product_id) != ''
    """)

    # Sales-rep order list ("cart"). rep_name is required at submit time.
    # No customer name / login for now - kept intentionally simple.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_name TEXT NOT NULL,
            status TEXT DEFAULT 'New',
            outlet_id INTEGER,
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
            active INTEGER DEFAULT 1,
            mobile_username TEXT,
            mobile_password_hash TEXT,
            mobile_token_hash TEXT
        )
    """)

    rep_columns = [row["name"] for row in conn.execute("PRAGMA table_info(sales_reps)").fetchall()]
    for column, definition in (
        ("mobile_username", "TEXT"),
        ("mobile_password_hash", "TEXT"),
        ("mobile_token_hash", "TEXT"),
    ):
        if column not in rep_columns:
            conn.execute(f"ALTER TABLE sales_reps ADD COLUMN {column} {definition}")

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_reps_mobile_username
        ON sales_reps(LOWER(mobile_username))
        WHERE mobile_username IS NOT NULL AND mobile_username != ''
    """)

    outlets_table_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outlets'"
    ).fetchone() is not None
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outlets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            shop_type TEXT,
            credit_limit REAL,
            lat REAL,
            lng REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outlet_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outlet_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (outlet_id) REFERENCES outlets(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outlet_aliases_outlet_id
        ON outlet_aliases(outlet_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outlet_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outlet_id INTEGER NOT NULL,
            rep_id INTEGER,
            rep_name TEXT NOT NULL DEFAULT '',
            visit_type TEXT NOT NULL DEFAULT 'order',
            visited_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            order_id INTEGER UNIQUE,
            client_visit_id TEXT,
            note TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (outlet_id) REFERENCES outlets(id),
            FOREIGN KEY (rep_id) REFERENCES sales_reps(id),
            FOREIGN KEY (order_id) REFERENCES orders(id)
        )
    """)
    visit_columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(outlet_visits)"
        ).fetchall()
    }
    for column in ("latitude", "longitude", "accuracy"):
        if column not in visit_columns:
            conn.execute(f"ALTER TABLE outlet_visits ADD COLUMN {column} REAL")
    for column, definition in (
        ("client_visit_id", "TEXT"),
        ("note", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in visit_columns:
            conn.execute(f"ALTER TABLE outlet_visits ADD COLUMN {column} {definition}")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_outlet_visits_mobile_id
        ON outlet_visits(rep_id, client_visit_id)
        WHERE client_visit_id IS NOT NULL
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outlet_visits_outlet_date ON outlet_visits(outlet_id, visited_at DESC)")

    # MIGRATION: orders.outlet_name didn't exist in earlier deploys.
    # CREATE TABLE IF NOT EXISTS above does nothing for an orders table
    # that already exists on a live database - so check for the column
    # explicitly and add it if missing, without touching existing rows.
    existing_columns = [row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "outlet_name" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN outlet_name TEXT DEFAULT ''")
    if "outlet_id" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN outlet_id INTEGER")

    # MIGRATION: duplicate-submission guard. If a rep's connection drops
    # right as an order finishes submitting, or a retry/double-tap slips
    # past the client-side button-disable, this lets the server recognize
    # "I've already processed this exact submission" instead of creating
    # a second order.
    if "idempotency_key" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT DEFAULT NULL")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_outlet_id ON orders(outlet_id)")

    order_columns = [row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    for column, definition in (
        ("rep_id", "INTEGER"),
        ("client_order_id", "TEXT"),
        ("catalog_snapshot_id", "TEXT"),
        ("price_mismatch", "INTEGER NOT NULL DEFAULT 0"),
        ("sync_received_at", "TEXT"),
        ("delivery_rep_id", "INTEGER"),
    ):
        if column not in order_columns:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")

    # Backfill structured outlet and order-visit records from historical
    # free-text order names. Ambiguous names remain unlinked rather than
    # being assigned to an arbitrary outlet.
    historical_orders = conn.execute("""
        SELECT id, rep_id, rep_name, outlet_name, created_at
        FROM orders
        WHERE outlet_id IS NULL AND TRIM(COALESCE(outlet_name, '')) != ''
    """).fetchall() if not outlets_table_existed else []
    for order in historical_orders:
        outlet_id = _resolve_outlet_id(conn, order["outlet_name"])
        if outlet_id is None:
            continue
        conn.execute("UPDATE orders SET outlet_id = ? WHERE id = ?", (outlet_id, order["id"]))
        conn.execute("""
            INSERT OR IGNORE INTO outlet_visits
                (outlet_id, rep_id, rep_name, visit_type, visited_at, order_id)
            VALUES (?, ?, ?, 'order', ?, ?)
        """, (outlet_id, order["rep_id"], order["rep_name"], order["created_at"], order["id"]))

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_rep_client_order
        ON orders(rep_id, client_order_id)
        WHERE rep_id IS NOT NULL AND client_order_id IS NOT NULL AND client_order_id != ''
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS catalog_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Each line item snapshots the product name + price AT ORDER TIME, so
    # editing a product's price later never rewrites history of what was
    # actually ordered/sold.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_pk INTEGER,
            product_generation INTEGER,
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
    order_item_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(order_items)").fetchall()
    }
    for column, definition in (
        ("product_pk", "INTEGER"),
        ("product_generation", "INTEGER"),
    ):
        if column not in order_item_columns:
            conn.execute(f"ALTER TABLE order_items ADD COLUMN {column} {definition}")
    conn.execute("""
        UPDATE order_items
        SET product_pk = (
                SELECT p.id FROM products p
                WHERE p.product_id = order_items.product_id
                  AND p.generation = 1
            ),
            product_generation = 1
        WHERE product_pk IS NULL
          AND EXISTS (
              SELECT 1 FROM products p
              WHERE p.product_id = order_items.product_id
                AND p.generation = 1
          )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            rep_id INTEGER NOT NULL,
            client_attempt_id TEXT NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('delivered', 'failed')),
            failure_reason TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            cash_collected REAL NOT NULL DEFAULT 0 CHECK (cash_collected >= 0),
            latitude REAL,
            longitude REAL,
            accuracy REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (rep_id) REFERENCES sales_reps(id),
            UNIQUE (rep_id, client_attempt_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            rep_id INTEGER NOT NULL,
            assigned_by TEXT NOT NULL DEFAULT '',
            assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (rep_id) REFERENCES sales_reps(id)
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_assignment_active_order
        ON delivery_assignments(order_id) WHERE active = 1
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_attempt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            order_item_id INTEGER NOT NULL,
            product_pk INTEGER NOT NULL,
            product_generation INTEGER NOT NULL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            FOREIGN KEY (attempt_id) REFERENCES delivery_attempts(id),
            FOREIGN KEY (order_item_id) REFERENCES order_items(id),
            FOREIGN KEY (product_pk) REFERENCES products(id),
            UNIQUE (attempt_id, order_item_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_handovers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id INTEGER NOT NULL,
            handover_date TEXT NOT NULL,
            client_handover_id TEXT NOT NULL,
            declared_cash REAL NOT NULL CHECK (declared_cash >= 0),
            received_cash REAL,
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'verified')),
            verified_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            verified_at TEXT,
            FOREIGN KEY (rep_id) REFERENCES sales_reps(id),
            UNIQUE (rep_id, client_handover_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_handover_items (
            handover_id INTEGER NOT NULL,
            attempt_id INTEGER NOT NULL UNIQUE,
            amount REAL NOT NULL CHECK (amount > 0),
            PRIMARY KEY (handover_id, attempt_id),
            FOREIGN KEY (handover_id) REFERENCES delivery_handovers(id),
            FOREIGN KEY (attempt_id) REFERENCES delivery_attempts(id)
        )
    """)

    # MIGRATION: description field for the product detail page. Free text,
    # optional - blank by default. Burmese (or any Unicode text) needs no
    # special handling in SQLite/Python, so this is just a plain TEXT column.
    product_columns = [row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()]
    if "description" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN description TEXT DEFAULT ''")

    # MIGRATION: supplier field. Internal-only (never shown on the public
    # catalog or product detail page) - used to pull supplier alongside
    # price data via /api/prices for inventory automation. Same
    # "ALTER TABLE if missing" pattern as description above, so existing
    # rows on the live DB get a safe '' default instead of breaking.
    if "supplier" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN supplier TEXT DEFAULT ''")

    # MIGRATION: has_image flag for the admin form and gallery image state.
    # This is a boolean-like field on the database row; the actual image
    # file still lives on disk in static/product-images/ and is resolved by
    # Product ID at runtime instead of stored in the DB.
    if "has_image" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN has_image BOOLEAN DEFAULT 0")

    # Web Push subscriptions - one row per device that's opted in to
    # phone/desktop notifications-tray alerts. No login/per-user concept
    # here either (same as LAST_SEEN_ACTIVITY_KEY), so a "subscriber" is
    # just an endpoint URL + the two keys the browser handed us; the
    # endpoint itself is what's unique, not any person. ON CONFLICT
    # upsert (used by add_push_subscription) covers the case where a
    # browser's push subscription rotates its keys but keeps the same
    # endpoint - not supposed to happen per spec, but cheap to be safe.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_balances (
            product_pk INTEGER PRIMARY KEY,
            quantity REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_pk INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 1,
            delta REAL NOT NULL CHECK (delta != 0),
            balance_after REAL NOT NULL CHECK (balance_after >= 0),
            movement_type TEXT NOT NULL,
            reference TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_stock_movements_product_time
        ON stock_movements(product_pk, id DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            count_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
            created_by TEXT NOT NULL DEFAULT '',
            approved_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            approved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_count_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            count_id INTEGER NOT NULL,
            product_pk INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            unit TEXT NOT NULL DEFAULT '',
            book_quantity REAL NOT NULL DEFAULT 0,
            counted_quantity REAL,
            UNIQUE (count_id, product_pk)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_stock_count_lines_count
        ON stock_count_lines(count_id, product_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT NOT NULL UNIQUE,
            supplier TEXT NOT NULL,
            invoice_number TEXT NOT NULL DEFAULT '',
            order_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'partial', 'received', 'cancelled')),
            note TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            received_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_order_id INTEGER NOT NULL,
            product_pk INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 1,
            ordered_quantity REAL NOT NULL CHECK (ordered_quantity > 0),
            unit_cost REAL NOT NULL DEFAULT 0 CHECK (unit_cost >= 0),
            received_quantity REAL NOT NULL DEFAULT 0
                CHECK (received_quantity >= 0 AND received_quantity <= ordered_quantity),
            UNIQUE (purchase_order_id, product_pk)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_purchase_order_items_order
        ON purchase_order_items(purchase_order_id, product_id)
    """)

    conn.commit()
    conn.close()

    if product_count() == 0 and os.path.exists(EXCEL_FILE):
        # log_activity=False: this is the one-time bootstrap import on a
        # brand new database, not a real "someone just uploaded this"
        # event. Without this, a fresh install would flood the
        # notification bell with 200+ "product added" entries on day one
        # instead of starting with a clean feed.
        import_excel_into_db(EXCEL_FILE, replace=True, log_activity=False)


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
        "Description": row["description"] if "description" in row.keys() else "",
        "Supplier": row["supplier"] if "supplier" in row.keys() else "",
        "has_image": bool(row["has_image"]) if "has_image" in row.keys() else False,
        "last_price_change": row["last_price_change"] if "last_price_change" in row.keys() else None,
        "last_price_direction": row["last_price_direction"] if "last_price_direction" in row.keys() else None,
    }


def _log_activity(conn, event_type, product_id, product_name, details, generation=1, source="manual"):
    """Insert an activity_log row - the single source the notification
    bell reads from. Caller is responsible for commit/close (shares a
    transaction with whatever write triggered it)."""

    conn.execute(
        """
        INSERT INTO activity_log (event_type, product_id, generation, product_name, details, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_type, product_id, generation, product_name, details, source),
    )


def _format_price_change_details(old_retail, new_retail, old_wholesale, new_wholesale):
    """Build a short human-readable summary for the public activity feed.

    The notification bell only reports retail/wholesale changes because
    those are the customer-facing values and the public /api/activity
    endpoint is unauthenticated.
    """

    parts = []
    if old_retail != new_retail:
        parts.append(f"Retail: {old_retail:,.0f} \u2192 {new_retail:,.0f} Ks")
    if old_wholesale != new_wholesale:
        parts.append(f"Wholesale: {old_wholesale:,.0f} \u2192 {new_wholesale:,.0f} Ks")
    return " \u2022 ".join(parts)


def _format_activity_message(event_type, product_name, **kwargs):
    """Format human-readable activity messages for the notification bell.
    
    Returns a dict with 'title' and 'message' for consistent formatting.
    """
    name = product_name or "a product"
    
    if event_type == "product_added":
        source = kwargs.get("source", "manual")
        if source == "excel_upload":
            return {
                "title": f"Added: {name}",
                "message": "Added via catalog upload"
            }
        return {
            "title": f"Added: {name}",
            "message": "New product added"
        }
    
    elif event_type == "product_replaced":
        old_name = kwargs.get("old_name", "previous product")
        return {
            "title": f"Replaced: {name}",
            "message": f"Replaced \"{old_name}\""
        }
    
    elif event_type == "price_changed":
        retail_change = kwargs.get("retail_change")
        wholesale_change = kwargs.get("wholesale_change")
        parts = []
        if retail_change:
            direction = "increased" if retail_change > 0 else "decreased"
            parts.append(f"Retail {direction} to {kwargs.get('new_retail', 0):,.0f} Ks")
        if wholesale_change:
            direction = "increased" if wholesale_change > 0 else "decreased"
            parts.append(f"Wholesale {direction} to {kwargs.get('new_wholesale', 0):,.0f} Ks")
        return {
            "title": f"Price updated: {name}",
            "message": " \u2022 ".join(parts) if parts else "Price updated"
        }
    
    elif event_type == "out_of_stock":
        return {
            "title": f"Out of stock: {name}",
            "message": "Marked as Out of Stock"
        }
    
    elif event_type == "back_in_stock":
        return {
            "title": f"Back in stock: {name}",
            "message": "Now available (In Stock)"
        }
    
    return {
        "title": f"Update: {name}",
        "message": "Catalog updated"
    }


def _log_price_change(conn, product_id, product_name, old_retail, new_retail,
                       old_wholesale, new_wholesale, source, generation=1,
                       old_base_price=None, new_base_price=None,
                       old_b2c=None, new_b2c=None):
    """Insert a price_history row only when a real price change occurred.

    This app's price_history table does not currently store base-price or
    B2C rows at all, so those extra parameters are accepted for future
    compatibility but ignored by the INSERT itself. The notification bell
    still only logs retail/wholesale changes, which is what the public
    API is allowed to expose.
    """

    if old_retail == new_retail and old_wholesale == new_wholesale:
        return

    conn.execute(
        """
        INSERT INTO price_history
            (product_id, generation, product_name, old_retail, new_retail,
             old_wholesale, new_wholesale, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            generation,
            product_name,
            old_retail,
            new_retail,
            old_wholesale,
            new_wholesale,
            source,
        ),
    )

    if old_retail != new_retail or old_wholesale != new_wholesale:
        _log_activity(
            conn,
            event_type="price_changed",
            product_id=product_id,
            generation=generation,
            product_name=product_name,
            details=_format_price_change_details(old_retail, new_retail, old_wholesale, new_wholesale),
        )


def add_push_subscription(endpoint, p256dh, auth):
    """Stores (or refreshes) one device's push subscription. Upserted on
    endpoint since that's the actual unique identity of a subscription -
    a browser re-subscribing with the same endpoint but rotated keys
    should overwrite, not duplicate."""
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO push_subscriptions (endpoint, p256dh, auth)
        VALUES (?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            p256dh = excluded.p256dh,
            auth = excluded.auth
        """,
        (endpoint, p256dh, auth),
    )
    conn.commit()
    conn.close()


def remove_push_subscription(endpoint):
    """Called both when a device explicitly unsubscribes and when
    push.py discovers a dead endpoint (browser returned 404/410)."""
    conn = get_db_connection()
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()
    conn.close()


def get_all_push_subscriptions():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _log_status_change(conn, product_id, product_name, old_status, new_status, generation=1):
    """Logs an activity_log entry when a product's Status actually changes
    (e.g. 'In Stock' -> 'Out Of Stock'). Same "only if it actually
    changed" guard as _log_price_change, and shares its transaction with
    the caller for the same reason (commit/close is the caller's job).

    Two distinct event_types instead of one generic "status_changed" -
    same reasoning as product_added vs price_changed being separate: it
    lets the bell/activity feed show a different icon/color for
    "went out of stock" vs "back in stock" without the client having to
    parse the details string to figure out which happened."""

    old_status = (old_status or "").strip()
    new_status = (new_status or "").strip()

    if not new_status or old_status == new_status:
        return

    event_type = "back_in_stock" if new_status == "In Stock" else "out_of_stock"

    _log_activity(
        conn,
        event_type=event_type,
        product_id=product_id,
        generation=generation,
        product_name=product_name,
        details=f"{old_status or 'Unknown'} \u2192 {new_status}",
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
    rows = conn.execute("""
        SELECT p.*,
               (SELECT changed_at FROM price_history
                WHERE product_id = p.product_id
                ORDER BY changed_at DESC, id DESC LIMIT 1) as last_price_change,
               (SELECT CASE
                    WHEN COALESCE(new_retail, 0) > COALESCE(old_retail, 0) THEN 'up'
                    WHEN COALESCE(new_retail, 0) < COALESCE(old_retail, 0) THEN 'down'
                    WHEN COALESCE(new_wholesale, 0) > COALESCE(old_wholesale, 0) THEN 'up'
                    WHEN COALESCE(new_wholesale, 0) < COALESCE(old_wholesale, 0) THEN 'down'
                    ELSE 'none'
                END
                FROM price_history
                WHERE product_id = p.product_id
                ORDER BY changed_at DESC, id DESC LIMIT 1) as last_price_direction
        FROM products p
        ORDER BY p.id ASC
    """).fetchall()
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
    row = conn.execute("""
        SELECT p.*,
               (SELECT changed_at FROM price_history
                WHERE product_id = p.product_id
                ORDER BY changed_at DESC, id DESC LIMIT 1) as last_price_change,
               (SELECT CASE
                    WHEN COALESCE(new_retail, 0) > COALESCE(old_retail, 0) THEN 'up'
                    WHEN COALESCE(new_retail, 0) < COALESCE(old_retail, 0) THEN 'down'
                    WHEN COALESCE(new_wholesale, 0) > COALESCE(old_wholesale, 0) THEN 'up'
                    WHEN COALESCE(new_wholesale, 0) < COALESCE(old_wholesale, 0) THEN 'down'
                    ELSE 'none'
                END
                FROM price_history
                WHERE product_id = p.product_id
                ORDER BY changed_at DESC, id DESC LIMIT 1) as last_price_direction
        FROM products p
        WHERE p.id = ?
    """, (product_pk,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_product_by_business_id(product_id_slug):
    """Look up a product by its business Product ID, ignoring spaces on
    both sides - so URL slugs like 'GTM-0001' correctly match DB values
    stored in the canonical 'GTM - 0001' form. Used by the /product/<id>
    detail page route."""
    normalized = (product_id_slug or "").replace(" ", "").strip()
    if not normalized:
        return None

    conn = get_db_connection()
    row = conn.execute("""
        SELECT p.*,
               (SELECT changed_at FROM price_history
                WHERE product_id = p.product_id
                ORDER BY changed_at DESC, id DESC LIMIT 1) as last_price_change,
               (SELECT CASE
                    WHEN COALESCE(new_retail, 0) > COALESCE(old_retail, 0) THEN 'up'
                    WHEN COALESCE(new_retail, 0) < COALESCE(old_retail, 0) THEN 'down'
                    WHEN COALESCE(new_wholesale, 0) > COALESCE(old_wholesale, 0) THEN 'up'
                    WHEN COALESCE(new_wholesale, 0) < COALESCE(old_wholesale, 0) THEN 'down'
                    ELSE 'none'
                END
                FROM price_history
                WHERE product_id = p.product_id
                ORDER BY changed_at DESC, id DESC LIMIT 1) as last_price_direction
        FROM products p
        WHERE REPLACE(p.product_id, ' ', '') = ? COLLATE NOCASE
    """, (normalized,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_price_history(product_id, limit=100):
    """All logged price changes for a given catalog Product ID (business
    key, e.g. 'GTM - 0001'), most recent first. Filters by current generation
    to avoid showing history from previous products that reused the same ID."""
    conn = get_db_connection()

    # Get current generation for this product_id
    gen_row = conn.execute(
        "SELECT generation FROM products WHERE product_id = ?",
        (product_id,)
    ).fetchone()
    generation = gen_row["generation"] if gen_row and "generation" in gen_row.keys() else 1

    rows = conn.execute(
        """
        SELECT * FROM price_history
        WHERE product_id = ? AND generation = ?
        ORDER BY changed_at DESC, id DESC
        LIMIT ?
        """,
        (product_id, generation, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_price_changes(limit=50):
    """Most recent price changes across all products - handy for an admin
    'recent activity' feed. Shows only current generation history."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT ph.* FROM price_history ph
        JOIN products p ON ph.product_id = p.product_id AND ph.generation = p.generation
        ORDER BY ph.changed_at DESC, ph.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_activity(limit=30, before_id=None):
    """Recent activity for the notification bell: new products + price
    changes, most recent first. Backs GET /api/activity.

    before_id powers the panel's "See more" button: a cursor (not a
    page-number OFFSET) so that a new activity_log row inserted between
    two "See more" clicks can't shift already-seen rows into view again
    or skip one - each page just asks for "everything older than the
    last id I already have".

    Only shows activity for current generation of each product (filters out
    history from previous products that reused the same Product ID)."""
    conn = get_db_connection()

    if before_id:
        rows = conn.execute(
            """
            SELECT al.* FROM activity_log al
            JOIN products p ON al.product_id = p.product_id AND al.generation = p.generation
            WHERE al.id < ?
            ORDER BY al.id DESC
            LIMIT ?
            """,
            (before_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT al.* FROM activity_log al
            JOIN products p ON al.product_id = p.product_id AND al.generation = p.generation
            ORDER BY al.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_latest_activity_id():
    """Cheap 'what's the highest activity_log id right now' check - used
    by app.py to snapshot the id right before a write, so it can tell
    afterwards whether that write actually logged anything (and if so,
    exactly which rows) without re-deriving the same diff logic
    _log_price_change/_log_status_change already apply internally."""
    conn = get_db_connection()
    row = conn.execute("SELECT MAX(id) AS m FROM activity_log").fetchone()
    conn.close()
    return row["m"] or 0


def get_activity_after(after_id, limit=500):
    """Everything logged after a given id, oldest first - pairs with
    get_latest_activity_id() to tell app.py exactly what a write just
    produced, so it can decide what to push.

    Only shows activity for current generation of each product."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT al.* FROM activity_log al
        JOIN products p ON al.product_id = p.product_id AND al.generation = p.generation
        WHERE al.id > ?
        ORDER BY al.id ASC
        LIMIT ?
        """,
        (after_id, limit),
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


def create_mobile_catalog_snapshot():
    """Persist and return an immutable, public-safe catalog price snapshot."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT id, product_id, product_name, upc, unit, retail, wholesale,
               category, status, description, generation
        FROM products
        ORDER BY id ASC
    """).fetchall()
    products = [dict(row) for row in rows]
    payload_json = json.dumps(products, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_id = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO catalog_snapshots (snapshot_id, payload_json) VALUES (?, ?)",
        (snapshot_id, payload_json),
    )
    conn.commit()
    conn.close()

    public_products = [
        {key: value for key, value in product.items() if key != "generation"}
        for product in products
    ]
    return {
        "snapshot_id": snapshot_id,
        "products": public_products,
        "outlets": get_public_outlets(),
    }


def get_mobile_rep_by_username(username):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM sales_reps WHERE LOWER(mobile_username) = LOWER(?)",
        (username,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_mobile_rep_by_token_hash(token_hash):
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT id, code, name, active, mobile_username
        FROM sales_reps
        WHERE mobile_token_hash = ? AND active = 1
        """,
        (token_hash,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_mobile_token_hash(rep_id, token_hash):
    conn = get_db_connection()
    conn.execute(
        "UPDATE sales_reps SET mobile_token_hash = ? WHERE id = ? AND active = 1",
        (token_hash, rep_id),
    )
    conn.commit()
    conn.close()


def _normalise_outlet_text(value):
    return " ".join(str(value or "").split()).casefold()


def _resolve_outlet_id(conn, outlet_name, *, create_if_missing=True):
    normalized = _normalise_outlet_text(outlet_name)
    if not normalized:
        return None

    matches = set()
    for row in conn.execute("SELECT id, name FROM outlets").fetchall():
        if _normalise_outlet_text(row["name"]) == normalized:
            matches.add(row["id"])
    for row in conn.execute("SELECT outlet_id, alias FROM outlet_aliases").fetchall():
        if _normalise_outlet_text(row["alias"]) == normalized:
            matches.add(row["outlet_id"])

    if len(matches) == 1:
        return matches.pop()
    if matches or not create_if_missing:
        return None

    cursor = conn.execute("INSERT INTO outlets (name) VALUES (?)", (str(outlet_name).strip(),))
    return cursor.lastrowid


def get_all_outlets():
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT o.*,
               COUNT(DISTINCT ord.id) AS order_count,
               COALESCE(SUM(ord.total_retail), 0) AS total_sales,
               MAX(ord.created_at) AS last_order_at
        FROM outlets o
        LEFT JOIN orders ord ON ord.outlet_id = o.id
        GROUP BY o.id
        ORDER BY o.name COLLATE NOCASE, o.area COLLATE NOCASE
    """).fetchall()
    outlets = [dict(row) for row in rows]
    aliases = conn.execute(
        "SELECT outlet_id, alias FROM outlet_aliases ORDER BY alias COLLATE NOCASE"
    ).fetchall()
    conn.close()

    aliases_by_outlet = {}
    for alias in aliases:
        aliases_by_outlet.setdefault(alias["outlet_id"], []).append(alias["alias"])
    for outlet in outlets:
        outlet["aliases"] = aliases_by_outlet.get(outlet["id"], [])
    return outlets


def get_outlet(outlet_id):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM outlets WHERE id = ?", (outlet_id,)).fetchone()
    if row is None:
        conn.close()
        return None

    outlet = dict(row)
    outlet["aliases"] = [
        dict(row) for row in conn.execute(
            "SELECT id, alias FROM outlet_aliases WHERE outlet_id = ? ORDER BY alias COLLATE NOCASE",
            (outlet_id,),
        ).fetchall()
    ]
    outlet["orders"] = [
        dict(order) for order in conn.execute("""
            SELECT id, rep_name, outlet_name, status, total_retail, created_at
            FROM orders WHERE outlet_id = ?
            ORDER BY created_at DESC, id DESC LIMIT 50
        """, (outlet_id,)).fetchall()
    ]
    outlet["visits"] = [
        dict(visit) for visit in conn.execute("""
            SELECT rep_name, visit_type, visited_at, order_id, note,
                   latitude, longitude, accuracy
            FROM outlet_visits WHERE outlet_id = ?
            ORDER BY visited_at DESC, id DESC LIMIT 50
        """, (outlet_id,)).fetchall()
    ]
    conn.close()
    return outlet


def create_outlet(name, area="", note="", shop_type=None, credit_limit=None):
    name = str(name or "").strip()
    area = str(area or "").strip()
    note = str(note or "").strip()
    if not name or len(name) > 200:
        raise ValueError("Outlet name is required and must be 200 characters or fewer.")
    if len(area) > 200 or len(note) > 500:
        raise ValueError("Outlet area or note is too long.")

    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT id, name, area FROM outlets").fetchall()
        if any(
            _normalise_outlet_text(row["name"]) == _normalise_outlet_text(name)
            and _normalise_outlet_text(row["area"]) == _normalise_outlet_text(area)
            for row in existing
        ):
            raise ValueError("An outlet with that name and area already exists.")
        cursor = conn.execute(
            "INSERT INTO outlets (name, area, note, shop_type, credit_limit) VALUES (?, ?, ?, ?, ?)",
            (name, area, note, shop_type, credit_limit),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_outlet(outlet_id, name, area="", note="", shop_type=None, credit_limit=None):
    name = str(name or "").strip()
    area = str(area or "").strip()
    note = str(note or "").strip()
    if not name or len(name) > 200:
        raise ValueError("Outlet name is required and must be 200 characters or fewer.")
    if len(area) > 200 or len(note) > 500:
        raise ValueError("Outlet area or note is too long.")

    conn = get_db_connection()
    try:
        existing = conn.execute(
            "SELECT id, name, area FROM outlets WHERE id != ?", (outlet_id,)
        ).fetchall()
        if any(
            _normalise_outlet_text(row["name"]) == _normalise_outlet_text(name)
            and _normalise_outlet_text(row["area"]) == _normalise_outlet_text(area)
            for row in existing
        ):
            raise ValueError("An outlet with that name and area already exists.")
        conn.execute("""
            UPDATE outlets
            SET name = ?, area = ?, note = ?, shop_type = ?, credit_limit = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (name, area, note, shop_type, credit_limit, outlet_id))
        if conn.total_changes == 0:
            raise ValueError("Outlet not found.")
        conn.execute(
            "UPDATE orders SET outlet_name = ? WHERE outlet_id = ?",
            (name, outlet_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_outlet_alias(outlet_id, alias):
    alias = " ".join(str(alias or "").split())
    if not alias or len(alias) > 200:
        raise ValueError("Alias is required and must be 200 characters or fewer.")
    conn = get_db_connection()
    try:
        if conn.execute("SELECT 1 FROM outlets WHERE id = ?", (outlet_id,)).fetchone() is None:
            raise ValueError("Outlet not found.")
        existing = conn.execute(
            "SELECT alias FROM outlet_aliases WHERE outlet_id = ?", (outlet_id,)
        ).fetchall()
        if any(_normalise_outlet_text(row["alias"]) == _normalise_outlet_text(alias) for row in existing):
            raise ValueError("That alias is already registered for this outlet.")
        conn.execute("INSERT INTO outlet_aliases (outlet_id, alias) VALUES (?, ?)", (outlet_id, alias))
        conn.commit()
    finally:
        conn.close()


def delete_outlet_alias(outlet_id, alias_id):
    conn = get_db_connection()
    conn.execute(
        "DELETE FROM outlet_aliases WHERE id = ? AND outlet_id = ?",
        (alias_id, outlet_id),
    )
    conn.commit()
    conn.close()


def get_public_outlets():
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT id, name, area, note, shop_type
        FROM outlets ORDER BY name COLLATE NOCASE, area COLLATE NOCASE
    """).fetchall()
    aliases = conn.execute(
        "SELECT outlet_id, alias FROM outlet_aliases ORDER BY alias COLLATE NOCASE"
    ).fetchall()
    conn.close()
    aliases_by_outlet = {}
    for alias in aliases:
        aliases_by_outlet.setdefault(alias["outlet_id"], []).append(alias["alias"])
    result = [dict(row) for row in rows]
    for outlet in result:
        outlet["aliases"] = aliases_by_outlet.get(outlet["id"], [])
    return result


def create_mobile_order(
    rep_id, rep_name, client_order_id, snapshot_id, outlet_name, collected_at, items,
    outlet_id=None, visit_location=None,
):
    """Create one authenticated mobile order using prices from a stored snapshot.

    Client order IDs are unique per rep so retries after an interrupted
    upload return the original order rather than creating a duplicate.
    """
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT id, total_retail, total_wholesale, price_mismatch
            FROM orders
            WHERE rep_id = ? AND client_order_id = ?
            """,
            (rep_id, client_order_id),
        ).fetchone()
        if existing:
            conn.commit()
            return {
                "order_id": existing["id"],
                "total_retail": existing["total_retail"],
                "total_wholesale": existing["total_wholesale"],
                "price_mismatch": bool(existing["price_mismatch"]),
                "already_synced": True,
            }

        snapshot_row = conn.execute(
            "SELECT payload_json FROM catalog_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if snapshot_row is None:
            raise ValueError("Catalog snapshot is no longer available. Sync the catalog and retry.")

        snapshot_products = json.loads(snapshot_row["payload_json"])
        snapshot_by_pk = {int(product["id"]): product for product in snapshot_products}
        if not isinstance(items, list) or not items:
            raise ValueError("Order must contain at least one item.")

        line_items = []
        total_retail = 0.0
        total_wholesale = 0.0
        price_mismatch = False
        quantities = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid order item.")
            pk = item.get("product_pk")
            quantity = item.get("quantity")
            if isinstance(pk, bool) or isinstance(quantity, bool):
                raise ValueError("Invalid product or quantity.")
            try:
                pk = int(pk)
                quantity = int(quantity)
            except (TypeError, ValueError):
                raise ValueError("Invalid product or quantity.") from None
            if quantity < 1 or quantity > 999:
                raise ValueError("Order quantities must be between 1 and 999.")
            if pk not in snapshot_by_pk:
                raise ValueError("An ordered product is not in the selected catalog snapshot.")
            quantities[pk] = quantities.get(pk, 0) + quantity

        for pk, quantity in quantities.items():
            saved_product = snapshot_by_pk[pk]
            current = conn.execute(
                "SELECT product_id, product_name, retail, wholesale, status, generation FROM products WHERE id = ?",
                (pk,),
            ).fetchone()
            if (
                current is None
                or current["product_id"] != saved_product["product_id"]
                or current["generation"] != saved_product["generation"]
                or current["retail"] != saved_product["retail"]
                or current["wholesale"] != saved_product["wholesale"]
                or current["status"] != saved_product["status"]
            ):
                price_mismatch = True

            unit_retail = float(saved_product["retail"] or 0)
            unit_wholesale = float(saved_product["wholesale"] or 0)
            line_retail = unit_retail * quantity
            line_wholesale = unit_wholesale * quantity
            line_items.append({
                "product_pk": pk,
                "product_generation": saved_product["generation"],
                "product_id": saved_product["product_id"],
                "product_name": saved_product["product_name"],
                "unit_retail": unit_retail,
                "unit_wholesale": unit_wholesale,
                "quantity": quantity,
                "line_retail": line_retail,
                "line_wholesale": line_wholesale,
            })
            total_retail += line_retail
            total_wholesale += line_wholesale

        if outlet_id is not None:
            selected_outlet = conn.execute(
                "SELECT id, name FROM outlets WHERE id = ?", (outlet_id,)
            ).fetchone()
            if selected_outlet is None:
                raise ValueError("Selected outlet is no longer available. Refresh the outlet list and retry.")
        else:
            outlet_id = _resolve_outlet_id(conn, outlet_name)
            selected_outlet = (
                conn.execute("SELECT id, name FROM outlets WHERE id = ?", (outlet_id,)).fetchone()
                if outlet_id is not None else None
            )
        if selected_outlet is not None:
            outlet_name = selected_outlet["name"]
        conn.execute(
            """
            INSERT INTO orders
                (rep_id, rep_name, outlet_id, outlet_name, total_retail, total_wholesale,
                 client_order_id, catalog_snapshot_id, price_mismatch, created_at,
                 sync_received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                rep_id, rep_name, outlet_id, outlet_name, total_retail, total_wholesale,
                client_order_id, snapshot_id, int(price_mismatch), collected_at,
            ),
        )
        order_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        if outlet_id is not None:
            location = visit_location or {}
            conn.execute(
                """
                INSERT INTO outlet_visits
                    (outlet_id, rep_id, rep_name, visit_type, visited_at, order_id,
                     latitude, longitude, accuracy)
                VALUES (?, ?, ?, 'order', ?, ?, ?, ?, ?)
                """,
                (
                    outlet_id,
                    rep_id,
                    rep_name,
                    collected_at,
                    order_id,
                    location.get("latitude"),
                    location.get("longitude"),
                    location.get("accuracy"),
                ),
            )
            if location:
                conn.execute(
                    """
                    UPDATE outlets SET lat = ?, lng = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND (lat IS NULL OR lng IS NULL)
                    """,
                    (location["latitude"], location["longitude"], outlet_id),
                )
        for item in line_items:
            conn.execute(
                """
                INSERT INTO order_items
                    (order_id, product_pk, product_generation, product_id, product_name,
                     unit_retail, unit_wholesale, quantity, line_retail, line_wholesale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id, item["product_pk"], item["product_generation"],
                    item["product_id"], item["product_name"],
                    item["unit_retail"], item["unit_wholesale"], item["quantity"],
                    item["line_retail"], item["line_wholesale"],
                ),
            )
        conn.commit()
        return {
            "order_id": order_id,
            "total_retail": total_retail,
            "total_wholesale": total_wholesale,
            "price_mismatch": price_mismatch,
            "already_synced": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_mobile_visit(
    rep_id,
    rep_name,
    client_visit_id,
    outlet_name,
    visited_at,
    *,
    visit_type="general",
    note="",
    outlet_id=None,
    visit_location=None,
):
    """Create one retry-safe standalone visit from an authenticated field rep."""
    outlet_name = " ".join(str(outlet_name or "").split())
    visit_type = " ".join(str(visit_type or "").split())
    note = str(note or "").strip()
    if not outlet_name or len(outlet_name) > 200:
        raise ValueError("Outlet name is required and must be 200 characters or fewer.")
    if not visit_type or len(visit_type) > 40:
        raise ValueError("Visit type is required and must be 40 characters or fewer.")
    if len(note) > 500:
        raise ValueError("Visit note must be 500 characters or fewer.")

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT id FROM outlet_visits
            WHERE rep_id = ? AND client_visit_id = ?
            """,
            (rep_id, client_visit_id),
        ).fetchone()
        if existing:
            conn.commit()
            return {"visit_id": existing["id"], "already_synced": True}

        normalized_name = _normalise_outlet_text(outlet_name)
        if outlet_id is not None:
            selected = conn.execute(
                "SELECT id, name FROM outlets WHERE id = ?", (outlet_id,)
            ).fetchone()
            if selected is None:
                raise ValueError("Selected outlet is no longer available. Refresh the outlet list and retry.")
            aliases = conn.execute(
                "SELECT alias FROM outlet_aliases WHERE outlet_id = ?",
                (outlet_id,),
            ).fetchall()
            if normalized_name not in {
                _normalise_outlet_text(selected["name"]),
                *(_normalise_outlet_text(alias["alias"]) for alias in aliases),
            }:
                raise ValueError("Selected outlet does not match the recorded outlet name.")
            outlet_id = selected["id"]
            outlet_name = selected["name"]
        else:
            matching_ids = {
                row["id"]
                for row in conn.execute("SELECT id, name FROM outlets").fetchall()
                if _normalise_outlet_text(row["name"]) == normalized_name
            }
            matching_ids.update(
                row["outlet_id"]
                for row in conn.execute("SELECT outlet_id, alias FROM outlet_aliases").fetchall()
                if _normalise_outlet_text(row["alias"]) == normalized_name
            )
            if len(matching_ids) > 1:
                raise ValueError("This outlet name matches multiple outlets. Choose the correct outlet and retry.")
            if matching_ids:
                outlet_id = matching_ids.pop()
                outlet_name = conn.execute(
                    "SELECT name FROM outlets WHERE id = ?", (outlet_id,)
                ).fetchone()["name"]
            else:
                outlet_id = conn.execute(
                    "INSERT INTO outlets (name) VALUES (?)", (outlet_name,)
                ).lastrowid

        location = visit_location or {}
        cursor = conn.execute(
            """
            INSERT INTO outlet_visits
                (outlet_id, rep_id, rep_name, visit_type, visited_at,
                 client_visit_id, note, latitude, longitude, accuracy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outlet_id,
                rep_id,
                rep_name,
                visit_type,
                visited_at,
                client_visit_id,
                note,
                location.get("latitude"),
                location.get("longitude"),
                location.get("accuracy"),
            ),
        )
        if location:
            conn.execute(
                """
                UPDATE outlets SET lat = ?, lng = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND (lat IS NULL OR lng IS NULL)
                """,
                (location["latitude"], location["longitude"], outlet_id),
            )
        conn.commit()
        return {"visit_id": cursor.lastrowid, "already_synced": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_delivery_board():
    conn = get_db_connection()
    try:
        orders = conn.execute("""
            SELECT o.id, o.rep_name, o.outlet_name, o.total_retail, o.created_at,
                   o.delivery_rep_id, outlet.area,
                   assigned.code AS delivery_code,
                   assigned.name AS delivery_name
            FROM orders o
            LEFT JOIN outlets outlet ON outlet.id = o.outlet_id
            LEFT JOIN sales_reps assigned ON assigned.id = o.delivery_rep_id
            WHERE o.status = 'New'
            ORDER BY o.created_at, o.id
        """).fetchall()
        reps = conn.execute("""
            SELECT id, code, name FROM sales_reps
            WHERE active = 1 AND mobile_username IS NOT NULL
              AND mobile_username != '' AND mobile_password_hash IS NOT NULL
            ORDER BY name COLLATE NOCASE
        """).fetchall()
        return [dict(row) for row in orders], [dict(row) for row in reps]
    finally:
        conn.close()


def assign_delivery_order(order_id, rep_id, *, actor=""):
    actor = str(actor or "").strip()
    if len(actor) > 120:
        raise ValueError("Assignment actor is too long.")
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT id, status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise ValueError("Order not found.")
        if order["status"] != "New":
            raise ValueError("Only new orders can be assigned for delivery.")
        rep = conn.execute("""
            SELECT id FROM sales_reps
            WHERE id = ? AND active = 1 AND mobile_username IS NOT NULL
              AND mobile_username != '' AND mobile_password_hash IS NOT NULL
        """, (rep_id,)).fetchone()
        if rep is None:
            raise ValueError("Choose an active rep with mobile sign-in configured.")
        conn.execute(
            "UPDATE delivery_assignments SET active = 0 WHERE order_id = ? AND active = 1",
            (order_id,),
        )
        conn.execute("""
            INSERT INTO delivery_assignments (order_id, rep_id, assigned_by)
            VALUES (?, ?, ?)
        """, (order_id, rep_id, actor))
        conn.execute(
            "UPDATE orders SET delivery_rep_id = ? WHERE id = ?",
            (rep_id, order_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_mobile_deliveries(rep_id, delivery_date=None):
    conn = get_db_connection()
    try:
        orders = conn.execute("""
            SELECT o.id, o.outlet_name, o.total_retail, o.created_at,
                   outlet.area
            FROM orders o
            LEFT JOIN outlets outlet ON outlet.id = o.outlet_id
            JOIN delivery_assignments da
              ON da.order_id = o.id AND da.rep_id = o.delivery_rep_id AND da.active = 1
            WHERE o.delivery_rep_id = ? AND o.status = 'New'
            ORDER BY o.created_at, o.id
        """, (rep_id,)).fetchall()
        result = []
        for order in orders:
            items = conn.execute("""
                SELECT oi.id, oi.product_id, oi.product_name,
                       oi.unit_retail,
                       oi.quantity - COALESCE(SUM(dai.quantity), 0) AS quantity
                FROM order_items oi
                LEFT JOIN delivery_attempt_items dai ON dai.order_item_id = oi.id
                WHERE oi.order_id = ?
                GROUP BY oi.id
                HAVING oi.quantity - COALESCE(SUM(dai.quantity), 0) > 0
                ORDER BY oi.id
            """, (order["id"],)).fetchall()
            result.append({
                "id": order["id"],
                "outlet_name": order["outlet_name"],
                "area": order["area"] or "",
                "created_at": order["created_at"],
                "total_retail": float(order["total_retail"] or 0),
                "items": [
                    {
                        "id": item["id"],
                        "product_id": item["product_id"],
                        "product_name": item["product_name"],
                        "quantity": item["quantity"],
                        "unit_retail": float(item["unit_retail"] or 0),
                    }
                    for item in items
                ],
            })
            delivered_value = conn.execute("""
                SELECT COALESCE(SUM(dai.quantity * oi.unit_retail), 0) AS total
                FROM delivery_attempt_items dai
                JOIN order_items oi ON oi.id = dai.order_item_id
                JOIN delivery_attempts a ON a.id = dai.attempt_id
                WHERE a.order_id = ?
            """, (order["id"],)).fetchone()["total"]
            collected_cash = conn.execute("""
                SELECT COALESCE(SUM(cash_collected), 0) AS total
                FROM delivery_attempts WHERE order_id = ?
            """, (order["id"],)).fetchone()["total"]
            remaining_item_value = sum(
                float(item["quantity"] or 0) * float(item["unit_retail"] or 0)
                for item in items
            )
            result[-1]["cash_collectable"] = max(
                0.0,
                min(
                    float(order["total_retail"] or 0) - float(collected_cash or 0),
                    float(delivered_value or 0) + remaining_item_value - float(collected_cash or 0),
                ),
            )

        pending_cash_query = """
            SELECT a.id, a.order_id, a.cash_collected, a.created_at,
                   o.outlet_name
            FROM delivery_attempts a
            JOIN orders o ON o.id = a.order_id
            LEFT JOIN delivery_handover_items hi ON hi.attempt_id = a.id
            WHERE a.rep_id = ? AND a.result = 'delivered'
              AND a.cash_collected > 0 AND hi.attempt_id IS NULL
        """
        args = [rep_id]
        if delivery_date:
            pending_cash_query += " AND substr(a.created_at, 1, 10) = ?"
            args.append(delivery_date)
        pending_cash_query += " ORDER BY a.created_at, a.id"
        pending_cash = conn.execute(pending_cash_query, args).fetchall()
        return {
            "orders": result,
            "pending_cash": [dict(row) for row in pending_cash],
            "pending_cash_total": sum(float(row["cash_collected"]) for row in pending_cash),
        }
    finally:
        conn.close()


def record_mobile_delivery_attempt(
    rep_id,
    client_attempt_id,
    order_id,
    result,
    *,
    failure_reason="",
    note="",
    cash_collected=0,
    visit_location=None,
    delivered_items=None,
):
    if not isinstance(result, str) or result not in {"delivered", "failed"}:
        raise ValueError("Choose a valid delivery outcome.")
    if not isinstance(failure_reason, str) or not isinstance(note, str):
        raise ValueError("Delivery notes must be text.")
    failure_reason = failure_reason.strip()
    note = note.strip()
    if len(failure_reason) > 200 or len(note) > 500:
        raise ValueError("Delivery notes are too long.")
    if result == "failed" and not failure_reason:
        raise ValueError("Enter a reason for the failed delivery.")
    if result == "failed":
        cash_collected = 0
    if isinstance(cash_collected, bool):
        raise ValueError("Collected cash must be a valid non-negative amount.")
    try:
        cash_collected = float(cash_collected)
    except (TypeError, ValueError):
        raise ValueError("Collected cash must be a valid non-negative amount.") from None
    if not math.isfinite(cash_collected) or cash_collected < 0:
        raise ValueError("Collected cash must be a valid non-negative amount.")

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("""
            SELECT id FROM delivery_attempts
            WHERE rep_id = ? AND client_attempt_id = ?
        """, (rep_id, client_attempt_id)).fetchone()
        if prior:
            conn.commit()
            return {"attempt_id": prior["id"], "already_synced": True}

        order = conn.execute("""
            SELECT id, status, delivery_rep_id, total_retail
            FROM orders WHERE id = ?
        """, (order_id,)).fetchone()
        if (
            order is None
            or order["status"] != "New"
            or order["delivery_rep_id"] != rep_id
        ):
            raise ValueError("This order is no longer assigned to you for delivery.")
        if not conn.execute("""
            SELECT 1 FROM delivery_assignments
            WHERE order_id = ? AND rep_id = ? AND active = 1
            LIMIT 1
        """, (order_id, rep_id)).fetchone():
            raise ValueError("This order is no longer assigned to you for delivery.")

        location = visit_location or {}
        if result == "delivered":
            items = conn.execute("""
                SELECT oi.id AS order_item_id, oi.product_pk, oi.product_generation,
                       oi.quantity, oi.unit_retail,
                       COALESCE(SUM(dai.quantity), 0) AS delivered_quantity,
                       p.id, p.product_id, p.product_name, p.unit, p.generation
                FROM order_items oi
                LEFT JOIN delivery_attempt_items dai ON dai.order_item_id = oi.id
                LEFT JOIN products p ON p.id = oi.product_pk
                WHERE oi.order_id = ?
                GROUP BY oi.id
                ORDER BY oi.id
            """, (order_id,)).fetchall()
            if not items or any(
                item["product_pk"] is None
                or item["id"] is None
                or item["generation"] != item["product_generation"]
                for item in items
            ):
                raise ValueError(
                    "This order has no safe inventory identity snapshot. Ask an admin to review it."
                )
            remaining_by_id = {
                item["order_item_id"]: max(
                    0.0, float(item["quantity"] or 0) - float(item["delivered_quantity"] or 0)
                )
                for item in items
            }
            if delivered_items is None:
                quantities = dict(remaining_by_id)
            else:
                if not isinstance(delivered_items, list) or not delivered_items:
                    raise ValueError("Choose at least one delivered item quantity.")
                quantities = {}
                for line in delivered_items:
                    if not isinstance(line, dict):
                        raise ValueError("Invalid delivered item quantity.")
                    raw_item_id = line.get("order_item_id")
                    raw_quantity = line.get("quantity")
                    if (
                        isinstance(raw_item_id, bool)
                        or isinstance(raw_item_id, float) and not raw_item_id.is_integer()
                        or isinstance(raw_quantity, bool)
                    ):
                        raise ValueError("Delivered item quantities must be valid numbers.")
                    try:
                        item_id = int(raw_item_id)
                        quantity = float(raw_quantity)
                    except (TypeError, ValueError):
                        raise ValueError("Delivered item quantities must be valid numbers.") from None
                    if (
                        item_id in quantities
                        or item_id not in remaining_by_id
                        or not math.isfinite(quantity)
                        or quantity <= 0
                        or quantity > remaining_by_id[item_id] + 1e-9
                    ):
                        raise ValueError("Delivered quantities must be positive and no greater than the remaining order quantities.")
                    quantities[item_id] = quantity
            if not quantities:
                raise ValueError("Choose at least one delivered item quantity.")
            subtotal = sum(
                float(item["unit_retail"] or 0) * quantities.get(item["order_item_id"], 0)
                for item in items
            )
            previously_delivered_value = sum(
                float(item["unit_retail"] or 0) * float(item["delivered_quantity"] or 0)
                for item in items
            )
            previously_collected = conn.execute("""
                SELECT COALESCE(SUM(cash_collected), 0) AS total
                FROM delivery_attempts
                WHERE order_id = ? AND result = 'delivered'
            """, (order_id,)).fetchone()["total"]
            collectable = max(
                0.0,
                min(
                    float(order["total_retail"] or 0) - float(previously_collected or 0),
                    previously_delivered_value + subtotal - float(previously_collected or 0),
                ),
            )
            if cash_collected > collectable + 1e-9:
                raise ValueError("Collected cash cannot exceed the unpaid value of delivered items.")
            if cash_collected + float(previously_collected or 0) > float(order["total_retail"] or 0) + 1e-9:
                raise ValueError("Collected cash cannot exceed the order total.")
            attempt_id = conn.execute("""
                INSERT INTO delivery_attempts
                    (order_id, rep_id, client_attempt_id, result, note,
                     cash_collected, latitude, longitude, accuracy)
                VALUES (?, ?, ?, 'delivered', ?, ?, ?, ?, ?)
            """, (
                order_id, rep_id, client_attempt_id, note, cash_collected,
                location.get("latitude"), location.get("longitude"),
                location.get("accuracy"),
            )).lastrowid
            for item in items:
                quantity = quantities.get(item["order_item_id"], 0)
                if quantity <= 0:
                    continue
                _record_stock_movement(
                    conn,
                    item,
                    -quantity,
                    "sale_delivery",
                    f"Order #{order_id}",
                    f"Delivery attempt #{attempt_id}",
                    f"rep:{rep_id}",
                )
                conn.execute("""
                    INSERT INTO delivery_attempt_items
                        (attempt_id, order_item_id, product_pk,
                         product_generation, quantity)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    attempt_id,
                    item["order_item_id"],
                    item["product_pk"],
                    item["product_generation"],
                    quantity,
                ))
            completed = all(
                float(item["delivered_quantity"] or 0) + quantities.get(item["order_item_id"], 0)
                >= float(item["quantity"] or 0) - 1e-9
                for item in items
            )
            if completed:
                conn.execute(
                    "UPDATE orders SET status = 'Fulfilled' WHERE id = ?",
                    (order_id,),
                )
        else:
            attempt_id = conn.execute("""
                INSERT INTO delivery_attempts
                    (order_id, rep_id, client_attempt_id, result,
                     failure_reason, note, latitude, longitude, accuracy)
                VALUES (?, ?, ?, 'failed', ?, ?, ?, ?, ?)
            """, (
                order_id, rep_id, client_attempt_id, failure_reason, note,
                location.get("latitude"), location.get("longitude"),
                location.get("accuracy"),
            )).lastrowid
            conn.execute(
                "UPDATE orders SET status = 'Cancelled' WHERE id = ?",
                (order_id,),
            )

        if result == "failed" or (result == "delivered" and completed):
            conn.execute(
                "UPDATE delivery_assignments SET active = 0 WHERE order_id = ? AND active = 1",
                (order_id,),
            )
        conn.commit()
        return {"attempt_id": attempt_id, "already_synced": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_mobile_delivery_handover(
    rep_id,
    client_handover_id,
    attempt_ids,
    declared_cash,
    handover_date,
    *,
    note="",
):
    if isinstance(declared_cash, bool):
        raise ValueError("Declared cash must be a valid non-negative amount.")
    try:
        declared_cash = float(declared_cash)
    except (TypeError, ValueError):
        raise ValueError("Declared cash must be a valid non-negative amount.") from None
    if not math.isfinite(declared_cash) or declared_cash < 0:
        raise ValueError("Declared cash must be a valid non-negative amount.")
    if not isinstance(note, str):
        raise ValueError("Handover note must be text.")
    note = note.strip()
    if len(note) > 500:
        raise ValueError("Handover note must be 500 characters or fewer.")
    if not isinstance(attempt_ids, list) or not attempt_ids or len(attempt_ids) > 100:
        raise ValueError("Select between 1 and 100 delivery collections.")
    if any(
        isinstance(value, bool)
        or (isinstance(value, float) and not value.is_integer())
        for value in attempt_ids
    ):
        raise ValueError("Delivery collection selection is invalid.")
    try:
        attempt_ids = [int(value) for value in attempt_ids]
    except (TypeError, ValueError):
        raise ValueError("Delivery collection selection is invalid.") from None
    if any(value < 1 for value in attempt_ids) or len(set(attempt_ids)) != len(attempt_ids):
        raise ValueError("Delivery collection selection is invalid.")

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("""
            SELECT id, status FROM delivery_handovers
            WHERE rep_id = ? AND client_handover_id = ?
        """, (rep_id, client_handover_id)).fetchone()
        if prior:
            conn.commit()
            return {
                "handover_id": prior["id"],
                "already_synced": True,
            }

        records = conn.execute(f"""
            SELECT a.id, a.cash_collected, a.created_at, hi.attempt_id AS assigned_handover
            FROM delivery_attempts a
            LEFT JOIN delivery_handover_items hi ON hi.attempt_id = a.id
            WHERE a.rep_id = ? AND a.result = 'delivered'
              AND a.id IN ({','.join('?' for _ in attempt_ids)})
            ORDER BY a.id
        """, (rep_id, *attempt_ids)).fetchall()
        if len(records) != len(attempt_ids):
            raise ValueError("One or more selected cash collections are unavailable.")
        if any(record["assigned_handover"] is not None for record in records):
            raise ValueError("A selected cash collection is already included in a handover.")
        if any(str(record["created_at"])[:10] != handover_date for record in records):
            raise ValueError("Selected cash collections must be from the handover date.")
        expected = sum(float(record["cash_collected"]) for record in records)
        if abs(expected - declared_cash) > 0.01:
            raise ValueError("Declared cash must match the selected delivery collections.")
        handover_id = conn.execute("""
            INSERT INTO delivery_handovers
                (rep_id, handover_date, client_handover_id, declared_cash, note)
            VALUES (?, ?, ?, ?, ?)
        """, (rep_id, handover_date, client_handover_id, declared_cash, note)).lastrowid
        conn.executemany("""
            INSERT INTO delivery_handover_items (handover_id, attempt_id, amount)
            VALUES (?, ?, ?)
        """, [
            (handover_id, record["id"], record["cash_collected"])
            for record in records
        ])
        conn.commit()
        return {"handover_id": handover_id, "already_synced": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_delivery_handovers(handover_date=None):
    conn = get_db_connection()
    try:
        query = """
            SELECT h.*, r.code, r.name AS rep_name,
                   COALESCE(SUM(hi.amount), 0) AS expected_cash
            FROM delivery_handovers h
            JOIN sales_reps r ON r.id = h.rep_id
            LEFT JOIN delivery_handover_items hi ON hi.handover_id = h.id
        """
        args = ()
        if handover_date:
            query += " WHERE h.handover_date = ?"
            args = (handover_date,)
        query += " GROUP BY h.id ORDER BY h.created_at DESC, h.id DESC"
        return [dict(row) for row in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


def verify_delivery_handover(handover_id, received_cash, *, actor=""):
    if isinstance(received_cash, bool):
        raise ValueError("Received cash must be a valid non-negative amount.")
    try:
        received_cash = float(received_cash)
    except (TypeError, ValueError):
        raise ValueError("Received cash must be a valid non-negative amount.") from None
    if not math.isfinite(received_cash) or received_cash < 0:
        raise ValueError("Received cash must be a valid non-negative amount.")
    actor = str(actor or "").strip()
    if not actor or len(actor) > 120:
        raise ValueError("A valid verifier is required.")
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        handover = conn.execute(
            "SELECT status FROM delivery_handovers WHERE id = ?", (handover_id,)
        ).fetchone()
        if handover is None:
            raise ValueError("Handover not found.")
        if handover["status"] != "pending":
            raise ValueError("This handover has already been verified.")
        conn.execute("""
            UPDATE delivery_handovers
            SET received_cash = ?, status = 'verified', verified_by = ?,
                verified_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (received_cash, actor, handover_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def detect_duplicate_product_ids(rows):
    """Return normalized duplicate IDs seen in a list of rows/dicts.

    Example return value:
    {
        "GTM-0001": {
            "count": 2,
            "values": ["GTM - 0001", "GTM-0001"],
        }
    }
    """
    seen = {}
    for row in rows:
        if isinstance(row, dict):
            raw = row.get("Product ID")
        else:
            raw = row

        if raw is None or pd.isna(raw):
            continue

        value = str(raw).strip()
        if not value:
            continue

        normalized = value.replace(" ", "").upper()
        bucket = seen.setdefault(normalized, {"count": 0, "values": []})
        bucket["count"] += 1
        bucket["values"].append(value)

    return {
        norm: info for norm, info in sorted(seen.items()) if info["count"] > 1
    }


def build_cleaned_catalog_frame(df):
    """Drop blank rows and keep only the first occurrence of each Product ID
    after normalizing spaces/case."""
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()

    cleaned = df.copy()
    if "Product ID" not in cleaned.columns:
        return cleaned

    cleaned["Product ID"] = cleaned["Product ID"].map(
        lambda v: "" if v is None or pd.isna(v) else str(v).strip()
    )

    keep_mask = []
    seen = set()
    for _, row in cleaned.iterrows():
        raw = row.get("Product ID")
        if raw is None or str(raw).strip() == "":
            keep_mask.append(False)
            continue

        normalized = str(raw).replace(" ", "").upper()
        if normalized in seen:
            keep_mask.append(False)
            continue

        seen.add(normalized)
        keep_mask.append(True)

    return cleaned.loc[keep_mask].reset_index(drop=True)


def get_duplicate_product_ids_in_db():
    """List duplicates currently stored in the live SQLite catalog."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT product_id, product_name FROM products WHERE TRIM(COALESCE(product_id, '')) != '' ORDER BY product_id ASC"
    ).fetchall()
    conn.close()
    return detect_duplicate_product_ids([dict(r) for r in rows])


_GTM_ID_PATTERN = re.compile(r"^gtm\s*-\s*(\d+)$", re.IGNORECASE)


def _highest_historical_gtm_number(conn):
    max_num = 0
    for table, column in (
        ("products", "product_id"),
        ("price_history", "product_id"),
        ("activity_log", "product_id"),
    ):
        for row in conn.execute(f"SELECT {column} AS pid FROM {table}").fetchall():
            match = _GTM_ID_PATTERN.match((row["pid"] or "").strip())
            if match:
                max_num = max(max_num, int(match.group(1)))
    return max_num


def _seed_id_counter_if_missing(conn):
    """Shared by peek/reserve: if id_counter somehow still has no row at
    call time (shouldn't happen after init_db()'s migration, but a
    fallback here is cheap insurance), seed it from the highest
    'GTM - ####' number that has EVER appeared in products, price_history,
    or activity_log - never just current products, since that's exactly
    what let deleted products' old IDs get reused (see init_db)."""

    row = conn.execute("SELECT next_number FROM id_counter WHERE id = 1").fetchone()
    max_num = _highest_historical_gtm_number(conn)
    minimum_next_num = max_num + 1

    if row is None:
        conn.execute(
            "INSERT INTO id_counter (id, next_number) VALUES (1, ?)",
            (minimum_next_num,),
        )
        return minimum_next_num

    if row["next_number"] < minimum_next_num:
        conn.execute(
            "UPDATE id_counter SET next_number = ? WHERE id = 1",
            (minimum_next_num,),
        )
        return minimum_next_num

    return row["next_number"]


def peek_next_product_id():
    """Read-only preview of what the next auto-generated Product ID WOULD
    be, e.g. 'GTM - 0226' - does NOT reserve/consume it. Use this anywhere
    you're just displaying the suggested id (like the Add Product page's
    GET render) - never for an id that will actually be written to a
    product, or two different rows could both display/use the same
    preview number before either commits.

    (Previously the Add Product page's GET route called the
    reserving get_next_product_id() just to show a preview, which meant
    every page load/refresh silently burned a real number AND the number
    shown on the form didn't even match what submit actually assigned,
    since submit reserved a fresh one on top of it.)"""

    conn = get_db_connection()
    next_num = _seed_id_counter_if_missing(conn)
    conn.commit()
    conn.close()
    return f"GTM - {str(next_num).zfill(4)}"


def get_next_product_id(conn=None):
    """Reserves and returns the next auto-generated Product ID, e.g.
    'GTM - 0226'. Backed by the id_counter table, which only ever
    increases - once a number is returned here it's permanently reserved,
    even if that product is later deleted. This replaces the old
    "scan products for the current max GTM - #### and add one" approach,
    which would silently hand a deleted product's old number to a
    completely different new product (e.g. two unrelated products both
    ending up as "GTM - 0244" at different points in time - confusing in
    price_history/activity_log, which key on this business id).

    Only call this at the point an id is ACTUALLY about to be written to
    a new product (form submit / import insert) - use peek_next_product_id()
    for any read-only preview/display, or every page view burns a real
    number whether or not a product ever gets created with it.

    Pass an existing `conn` to reserve the id inside a caller-owned
    connection/transaction instead of opening a new one (the caller is
    then responsible for commit/close) - import_excel_into_db does this
    so ID reservation shares its transaction rather than opening a second,
    competing connection while the first is still mid-transaction, which
    could otherwise trip SQLite's single-writer lock.
    """

    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()

    next_num = _seed_id_counter_if_missing(conn)
    conn.execute(
        "UPDATE id_counter SET next_number = ? WHERE id = 1",
        (next_num + 1,),
    )

    if owns_conn:
        conn.commit()
        conn.close()

    return f"GTM - {str(next_num).zfill(4)}"


def get_available_product_ids():
    """Return Product IDs of Out Of Stock products that can be reused.

    These are products with status 'Out Of Stock' that still exist in the
    products table. Reusing their Product ID for a new product avoids
    incrementing the ID counter and keeps history attached to the same
    business key.
    """
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT product_id, product_name, id
        FROM products
        WHERE status = 'Out Of Stock' AND TRIM(product_id) != ''
        ORDER BY product_id
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reuse_product_id(product_id, data):
    """Update an existing Out Of Stock product with new data, reusing its Product ID.

    This is used when adding a "new" product that replaces a discontinued one.
    Instead of creating a new Product ID (which increments the counter),
    we update the existing Out Of Stock product's fields, increment the generation,
    and log a 'product_replaced' activity event with the NEW generation.

    Args:
        product_id: The business Product ID to reuse (e.g., 'GTM - 0001')
        data: Dict with new product data (product_name, retail, wholesale, etc.)

    Returns:
        The internal products.id (pk) of the updated product
    """
    conn = get_db_connection()

    existing = conn.execute(
        "SELECT * FROM products WHERE product_id = ?", (product_id,)
    ).fetchone()

    if existing is None:
        conn.close()
        raise ValueError(f"Product ID {product_id} not found")

    if existing["status"] != "Out Of Stock":
        conn.close()
        raise ValueError(f"Product ID {product_id} is not Out Of Stock (cannot reuse)")

    stock_row = conn.execute(
        "SELECT quantity FROM stock_balances WHERE product_pk = ?",
        (existing["id"],),
    ).fetchone()
    if stock_row and stock_row["quantity"] > 1e-9:
        conn.close()
        raise ValueError(
            f"Product ID {product_id} still has stock on hand and cannot be reused"
        )
    pending_purchase = conn.execute(
        """
        SELECT 1
        FROM purchase_order_items i
        JOIN purchase_orders p ON p.id = i.purchase_order_id
        WHERE i.product_pk = ? AND i.generation = ?
          AND p.status IN ('open', 'partial')
        LIMIT 1
        """,
        (existing["id"], existing["generation"]),
    ).fetchone()
    if pending_purchase:
        conn.close()
        raise ValueError(
            f"Product ID {product_id} is on an open purchase order and cannot be reused"
        )

    old_name = existing["product_name"]
    old_gen = existing["generation"] if "generation" in existing.keys() else 1
    new_gen = old_gen + 1

    # Delete old image files since this is a new product reusing the ID
    # Import here to avoid circular imports
    try:
        from services import delete_product_image_files
        delete_product_image_files(product_id)
    except Exception:
        pass  # Ignore if services not available or no files to delete

    conn.execute(
        """
        UPDATE products SET
            product_name = ?,
            upc = ?,
            unit = ?,
            retail = ?,
            wholesale = ?,
            category = ?,
            status = ?,
            description = ?,
            supplier = ?,
            has_image = ?,
            generation = ?
        WHERE product_id = ?
        """,
        (
            data.get("product_name", ""),
            data.get("upc", 0),
            data.get("unit", "-"),
            data.get("retail", 0),
            data.get("wholesale", 0),
            data.get("category", "General"),
            data.get("status", "In Stock"),
            data.get("description", ""),
            data.get("supplier", ""),
            1 if data.get("has_image") else 0,
            new_gen,
            product_id,
        ),
    )

    _log_activity(
        conn,
        event_type="product_replaced",
        product_id=product_id,
        generation=new_gen,
        product_name=data.get("product_name", ""),
        details=f"Replaced \"{old_name}\" with \"{data.get('product_name', '')}\"",
    )

    conn.commit()

    product_pk = existing["id"]
    conn.close()
    return product_pk


# ------------------------
# Sales reps (managed list, no free typing at order time)
# ------------------------

def get_all_reps():
    """All reps, active and inactive - for the admin management page."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT id, code, name, active, mobile_username
        FROM sales_reps
        ORDER BY code ASC, name ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_reps():
    """Only active reps - this is what populates the order-form picker."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT id, code, name, active
        FROM sales_reps
        WHERE active = 1
        ORDER BY code ASC, name ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rep(rep_id):
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT id, code, name, active, mobile_username
        FROM sales_reps
        WHERE id = ?
        """,
        (rep_id,),
    ).fetchone()
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


def add_rep(code, name, username=None, password_hash=None):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO sales_reps
            (code, name, active, mobile_username, mobile_password_hash)
        VALUES (?, ?, 1, ?, ?)
        """,
        (code, name, username, password_hash),
    )
    conn.commit()
    conn.close()


_UNSET = object()


def update_rep(rep_id, code, name, active, username=_UNSET, password_hash=None):
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT mobile_username, mobile_password_hash, mobile_token_hash FROM sales_reps WHERE id = ?",
        (rep_id,),
    ).fetchone()
    if existing is None:
        conn.close()
        return

    if username is _UNSET:
        username = existing["mobile_username"]
        password_hash = existing["mobile_password_hash"]
        token_hash = existing["mobile_token_hash"] if active else None
    else:
        if username:
            password_hash = password_hash or existing["mobile_password_hash"]
        else:
            password_hash = None
        credentials_changed = (
            username != existing["mobile_username"]
            or (password_hash is not None and password_hash != existing["mobile_password_hash"])
        )
        token_hash = None if credentials_changed or not active else existing["mobile_token_hash"]

    conn.execute(
        """
        UPDATE sales_reps
        SET code = ?, name = ?, active = ?, mobile_username = ?,
            mobile_password_hash = ?, mobile_token_hash = ?
        WHERE id = ?
        """,
        (code, name, 1 if active else 0, username, password_hash, token_hash, rep_id),
    )
    conn.commit()
    conn.close()


def delete_rep(rep_id):
    conn = get_db_connection()
    try:
        if conn.execute("""
            SELECT 1 FROM delivery_assignments WHERE rep_id = ? LIMIT 1
        """, (rep_id,)).fetchone() or conn.execute("""
            SELECT 1 FROM delivery_attempts WHERE rep_id = ? LIMIT 1
        """, (rep_id,)).fetchone() or conn.execute("""
            SELECT 1 FROM delivery_handovers WHERE rep_id = ? LIMIT 1
        """, (rep_id,)).fetchone():
            raise ValueError(
                "A rep with delivery history cannot be deleted; deactivate the rep instead."
            )
        conn.execute("DELETE FROM sales_reps WHERE id = ?", (rep_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
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
    conn.execute("BEGIN IMMEDIATE")

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
            "product_pk": pk,
            "product_generation": product["generation"],
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
        conn.rollback()
        conn.close()
        return None, None, None

    outlet_id = _resolve_outlet_id(conn, outlet_name)
    if outlet_id is not None:
        outlet_name = conn.execute(
            "SELECT name FROM outlets WHERE id = ?", (outlet_id,)
        ).fetchone()["name"]
    rep = conn.execute(
        "SELECT id FROM sales_reps WHERE code || ' (' || name || ')' = ? AND active = 1",
        (rep_name,),
    ).fetchone()
    rep_id = rep["id"] if rep else None
    conn.execute(
        """
        INSERT INTO orders (rep_name, outlet_id, outlet_name, total_retail, total_wholesale, idempotency_key)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (rep_name, outlet_id, outlet_name, total_retail, total_wholesale, idempotency_key),
    )

    order_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    if outlet_id is not None:
        conn.execute(
            """
            INSERT INTO outlet_visits (outlet_id, rep_id, rep_name, visit_type, order_id)
            VALUES (?, ?, ?, 'order', ?)
            """,
            (outlet_id, rep_id, rep_name, order_id),
        )

    for item in line_items:
        conn.execute(
            """
            INSERT INTO order_items
                (order_id, product_pk, product_generation, product_id, product_name,
                 unit_retail, unit_wholesale, quantity, line_retail, line_wholesale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                item["product_pk"],
                item["product_generation"],
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


def get_sales_report(start_date, end_date, rep_name=None):
    """Return date-bounded order performance and non-cancelled product sales."""
    where = ["date(o.created_at) BETWEEN ? AND ?"]
    params = [start_date, end_date]
    if rep_name:
        where.append("o.rep_name = ?")
        params.append(rep_name)
    where_sql = " AND ".join(where)

    conn = get_db_connection()
    reps = conn.execute(f"""
        SELECT o.rep_name,
               COUNT(*) AS order_count,
               COUNT(DISTINCT CASE
                   WHEN o.outlet_id IS NOT NULL THEN 'outlet:' || o.outlet_id
                   ELSE 'name:' || LOWER(TRIM(COALESCE(o.outlet_name, '')))
               END) AS outlet_count,
               COALESCE(SUM(o.total_retail), 0) AS submitted_sales,
               COALESCE(SUM(CASE WHEN o.status = 'Fulfilled' THEN o.total_retail ELSE 0 END), 0) AS fulfilled_sales,
               COALESCE(SUM(CASE WHEN o.status = 'Fulfilled' THEN 1 ELSE 0 END), 0) AS fulfilled_count,
               COALESCE(SUM(CASE WHEN o.status = 'New' THEN 1 ELSE 0 END), 0) AS pending_count,
               COALESCE(SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END), 0) AS cancelled_count
        FROM orders o
        WHERE {where_sql}
        GROUP BY o.rep_name
        ORDER BY submitted_sales DESC, o.rep_name COLLATE NOCASE
    """, params).fetchall()
    totals = conn.execute(f"""
        SELECT COUNT(*) AS order_count,
               COUNT(DISTINCT CASE
                   WHEN o.outlet_id IS NOT NULL THEN 'outlet:' || o.outlet_id
                   ELSE 'name:' || LOWER(TRIM(COALESCE(o.outlet_name, '')))
               END) AS outlet_count,
               COALESCE(SUM(o.total_retail), 0) AS submitted_sales,
               COALESCE(SUM(CASE WHEN o.status = 'Fulfilled' THEN o.total_retail ELSE 0 END), 0) AS fulfilled_sales,
               COALESCE(SUM(CASE WHEN o.status = 'Fulfilled' THEN 1 ELSE 0 END), 0) AS fulfilled_count,
               COALESCE(SUM(CASE WHEN o.status = 'New' THEN 1 ELSE 0 END), 0) AS pending_count,
               COALESCE(SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END), 0) AS cancelled_count
        FROM orders o
        WHERE {where_sql}
    """, params).fetchone()
    products = conn.execute(f"""
        SELECT oi.product_id, oi.product_name,
               COALESCE(SUM(oi.quantity), 0) AS quantity,
               COALESCE(SUM(oi.line_retail), 0) AS sales,
               COUNT(DISTINCT o.id) AS order_count
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE {where_sql} AND o.status != 'Cancelled'
        GROUP BY oi.product_id, oi.product_name
        ORDER BY sales DESC, oi.product_name COLLATE NOCASE
        LIMIT 100
    """, params).fetchall()
    daily = conn.execute(f"""
        SELECT date(o.created_at) AS day,
               COUNT(*) AS order_count,
               COALESCE(SUM(o.total_retail), 0) AS submitted_sales,
               COALESCE(SUM(CASE WHEN o.status = 'Fulfilled' THEN o.total_retail ELSE 0 END), 0) AS fulfilled_sales
        FROM orders o
        WHERE {where_sql}
        GROUP BY date(o.created_at)
        ORDER BY day
    """, params).fetchall()
    conn.close()
    return {
        "totals": dict(totals),
        "reps": [dict(row) for row in reps],
        "products": [dict(row) for row in products],
        "daily": [dict(row) for row in daily],
    }


def get_order_rep_names():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT DISTINCT rep_name FROM orders WHERE TRIM(COALESCE(rep_name, '')) != '' ORDER BY rep_name COLLATE NOCASE"
    ).fetchall()
    conn.close()
    return [row["rep_name"] for row in rows]


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
    delivery_rep = conn.execute("""
        SELECT code, name FROM sales_reps WHERE id = ?
    """, (order["delivery_rep_id"],)).fetchone() if order["delivery_rep_id"] else None
    latest_delivery = conn.execute("""
        SELECT id, result, failure_reason, note, cash_collected, created_at,
               latitude, longitude, accuracy
        FROM delivery_attempts WHERE order_id = ? ORDER BY id DESC LIMIT 1
    """, (order_id,)).fetchone()
    delivery_items = conn.execute("""
        SELECT oi.product_name, oi.quantity AS ordered_quantity,
               COALESCE(SUM(dai.quantity), 0) AS delivered_quantity
        FROM order_items oi
        LEFT JOIN delivery_attempt_items dai ON dai.order_item_id = oi.id
        WHERE oi.order_id = ?
        GROUP BY oi.id ORDER BY oi.id
    """, (order_id,)).fetchall()
    delivery_cash = conn.execute("""
        SELECT COALESCE(SUM(cash_collected), 0) AS total
        FROM delivery_attempts WHERE order_id = ?
    """, (order_id,)).fetchone()["total"]
    latest_delivery_items = conn.execute("""
        SELECT oi.product_name, dai.quantity
        FROM delivery_attempt_items dai
        JOIN order_items oi ON oi.id = dai.order_item_id
        WHERE dai.attempt_id = ? ORDER BY dai.id
    """, (latest_delivery["id"],)).fetchall() if latest_delivery else []

    conn.close()

    order_data = dict(order)
    order_data["delivery_rep_label"] = (
        f"{delivery_rep['code']} ({delivery_rep['name']})" if delivery_rep else ""
    )
    order_data["latest_delivery"] = dict(latest_delivery) if latest_delivery else None
    if order_data["latest_delivery"]:
        order_data["latest_delivery"]["items"] = [dict(row) for row in latest_delivery_items]
    order_data["delivery_items"] = [dict(row) for row in delivery_items]
    order_data["delivery_cash_collected"] = float(delivery_cash or 0)
    total_ordered = sum(float(item["ordered_quantity"] or 0) for item in delivery_items)
    total_delivered = sum(float(item["delivered_quantity"] or 0) for item in delivery_items)
    order_data["delivery_partial"] = (
        total_delivered > 0 and total_delivered < total_ordered - 1e-9
    )
    return order_data, [dict(i) for i in items]


def update_order_status(order_id, status):
    if status not in {"New", "Fulfilled", "Cancelled"}:
        raise ValueError("Choose a valid order status.")
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if status == "Fulfilled" and conn.execute("""
            SELECT 1
            FROM order_items oi
            LEFT JOIN delivery_attempt_items dai ON dai.order_item_id = oi.id
            WHERE oi.order_id = ?
            GROUP BY oi.id
            HAVING COALESCE(SUM(dai.quantity), 0) < oi.quantity - 1e-9
            LIMIT 1
        """, (order_id,)).fetchone():
            if conn.execute(
                "SELECT 1 FROM delivery_attempts WHERE order_id = ? LIMIT 1",
                (order_id,),
            ).fetchone():
                raise ValueError(
                    "This order still has undelivered quantities; record or cancel the remaining delivery."
                )
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        if status != "New":
            conn.execute(
                "UPDATE delivery_assignments SET active = 0 WHERE order_id = ? AND active = 1",
                (order_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_order(order_id):
    conn = get_db_connection()
    try:
        if conn.execute(
            "SELECT 1 FROM delivery_attempts WHERE order_id = ? LIMIT 1",
            (order_id,),
        ).fetchone():
            raise ValueError("An order with a delivery record cannot be deleted.")
        conn.execute(
            "DELETE FROM delivery_assignments WHERE order_id = ?", (order_id,)
        )
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------
# Writes: single-product CRUD (new admin features)
# ------------------------

def add_product(data):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO products
            (product_id, product_name, upc, unit, retail, wholesale, category, status, description, supplier, has_image, generation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            data.get("description", ""),
            data.get("supplier", ""),
            1 if data.get("has_image") else 0,
            1,  # generation = 1 for new products
        ),
    )

    # Capture the new product's id RIGHT NOW, before any other INSERT on
    # this connection (like the activity_log write below) can happen -
    # last_insert_rowid() always reflects the MOST RECENT insert on the
    # connection, so if it were queried after _log_activity() runs its own
    # INSERT, it would silently return the activity_log row's id instead
    # of the product's id.
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    _log_activity(
        conn,
        event_type="product_added",
        product_id=data.get("product_id", ""),
        generation=1,
        product_name=data.get("product_name", ""),
        details="New product added",
        source="manual",
    )

    conn.commit()
    conn.close()
    return new_id


def update_product(product_pk, data):
    conn = get_db_connection()

    existing = conn.execute(
        "SELECT * FROM products WHERE id = ?", (product_pk,)
    ).fetchone()

    has_image = data.get("has_image")
    if has_image is None:
        has_image = bool(existing["has_image"]) if existing else False

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
            status = ?,
            description = ?,
            supplier = ?,
            has_image = ?
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
            data.get("description", ""),
            data.get("supplier", ""),
            1 if has_image else 0,
            product_pk,
        ),
    )

    if existing is not None:
        gen = existing["generation"] if "generation" in existing.keys() else 1
        _log_price_change(
            conn,
            product_id=data.get("product_id", "") or existing["product_id"],
            product_name=data.get("product_name", "") or existing["product_name"],
            old_retail=existing["retail"],
            new_retail=data.get("retail", 0),
            old_wholesale=existing["wholesale"],
            new_wholesale=data.get("wholesale", 0),
            source="manual",
            generation=gen,
        )

        _log_status_change(
            conn,
            product_id=data.get("product_id", "") or existing["product_id"],
            product_name=data.get("product_name", "") or existing["product_name"],
            old_status=existing["status"],
            new_status=data.get("status", "In Stock"),
            generation=gen,
        )

    conn.commit()
    conn.close()


def delete_product(product_pk):
    conn = get_db_connection()
    try:
        if conn.execute(
            "SELECT 1 FROM stock_movements WHERE product_pk = ? LIMIT 1",
            (product_pk,),
        ).fetchone():
            raise ValueError(
                "This product has stock history and cannot be deleted. "
                "Mark it Out Of Stock instead."
            )
        if conn.execute(
            """
            SELECT 1
            FROM purchase_order_items i
            JOIN purchase_orders p ON p.id = i.purchase_order_id
            WHERE i.product_pk = ? AND p.status IN ('open', 'partial')
            LIMIT 1
            """,
            (product_pk,),
        ).fetchone():
            raise ValueError(
                "This product is on an open purchase order and cannot be deleted."
            )
        conn.execute("DELETE FROM stock_balances WHERE product_pk = ?", (product_pk,))
        conn.execute("DELETE FROM products WHERE id = ?", (product_pk,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------
# Bulk import (the existing "upload Excel" admin feature)
# ------------------------


def validate_pricing_rows(path):
    """Row-level validation, run BEFORE any DB write.

    Confirmed with the business owner: Out Of Stock products are exempt
    from all requirements below. Only In Stock rows must have valid data.
    """

    df = pd.read_excel(path, sheet_name="Product Catalog")

    errors = []

    for _, row in df.iterrows():

        product_id = str(row.get("Product ID", "") or "").strip() or "(blank ID)"
        product_name = str(row.get("Product Name", "") or "").strip() or "unnamed"
        status = str(row.get("Status", "In Stock") or "In Stock").strip().title()
        label = f"{product_id} ({product_name})"

        if status != "In Stock":
            continue

        supplier_raw = row.get("Supplier")
        if pd.isna(supplier_raw) or str(supplier_raw).strip() == "":
            errors.append(f"{label}: Supplier is required for In Stock products.")

        base_price_raw = row.get("Base Price")

        if pd.isna(base_price_raw):
            errors.append(f"{label}: Base Price is required for In Stock products.")
            continue

        try:
            base_price = float(base_price_raw)
        except (TypeError, ValueError):
            errors.append(f"{label}: Base Price ({base_price_raw!r}) is not a valid number.")
            continue

        if base_price <= 0:
            errors.append(f"{label}: Base Price must be greater than 0 (got {base_price:,.2f}).")
            continue

        b2c_raw = row.get("B2C")
        if pd.isna(b2c_raw):
            errors.append(f"{label}: B2C price is required for In Stock products.")
        else:
            try:
                b2c = float(b2c_raw)
                if b2c < base_price:
                    errors.append(
                        f"{label}: B2C ({b2c:,.2f}) is below Base Price ({base_price:,.2f})."
                    )
            except (TypeError, ValueError):
                errors.append(f"{label}: B2C ({b2c_raw!r}) is not a valid number.")

        for field_name in ("Retail", "Wholesale"):
            raw_value = row.get(field_name)
            if pd.isna(raw_value):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value < base_price:
                errors.append(
                    f"{label}: {field_name} ({value:,.2f}) is below Base Price ({base_price:,.2f})."
                )

    return errors


def backup_before_import():
    """Snapshot the live DB using SQLite's built-in .backup() API."""

    backups_dir = os.path.join(os.path.dirname(DATABASE_FILE), "backups")
    os.makedirs(backups_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backups_dir, f"pre_import_{timestamp}.db")

    source_conn = sqlite3.connect(DATABASE_FILE)
    try:
        dest_conn = sqlite3.connect(backup_path)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    return backup_path


def import_excel_into_db(path, replace=True, source="excel_upload", log_activity=True):
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
    - "Description" is an OPTIONAL column. If the uploaded sheet doesn't
      have it at all, existing descriptions are left completely untouched
      (not wiped to blank) - only a sheet that actually includes the
      column can update descriptions in bulk.
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

    has_description_col = "Description" in df.columns
    if has_description_col:
        df["Description"] = df["Description"].fillna("")

    # Supplier: same "optional column" treatment as Description - a sheet
    # that doesn't include this column must never wipe existing supplier
    # values in bulk, only a sheet that actually has the column updates it.
    has_supplier_col = "Supplier" in df.columns
    if has_supplier_col:
        df["Supplier"] = df["Supplier"].fillna("")

    conn = get_db_connection()

    seen_product_ids = []

    # Guard against a sheet's explicitly-typed Product ID silently
    # colliding with an ID that belonged to a NOW-DELETED product. That
    # product no longer exists in `products` (so the "existing" lookup
    # below won't find it), but its price_history/activity_log rows are
    # still keyed on that same business id - so a new, unrelated product
    # would silently inherit a stranger's history/notifications (this is
    # the exact bug already confirmed in this database for
    # GTM - 0245/0255/0256). This doesn't block the upload - it's a real
    # if rare legitimate case (re-adding a genuinely discontinued-then-
    # relisted product) - it just collects a warning so an admin can
    # verify it was intentional instead of it happening silently.
    historical_ids = set()
    for table in ("price_history", "activity_log"):
        for r in conn.execute(f"SELECT DISTINCT product_id FROM {table}").fetchall():
            pid = (r["product_id"] or "").strip()
            if pid:
                historical_ids.add(pid)
    reused_id_warnings = []

    for _, row in df.iterrows():
        product_id = str(row["Product ID"]).strip()
        product_name = str(row["Product Name"])
        upc = int(row["UPC"])
        unit = str(row["Unit"])
        retail = float(row["Retail"])
        wholesale = float(row["Wholesale"])
        category = str(row["Category"])
        status = str(row["Status"])

        # A blank Product ID must NEVER be looked up against the table -
        # it always means "this is a new product," never "match whatever
        # other row also happens to have a blank id." Without this guard,
        # two different blank-ID rows uploaded at different times would
        # silently collapse into the SAME database row (the second
        # "new" product would just overwrite the first's fields instead
        # of being inserted) - and since an overwrite doesn't necessarily
        # change price or status, that could mean NO activity gets logged
        # at all for what the uploader intended as a brand new product.
        existing = None
        if product_id:
            existing = conn.execute(
                "SELECT * FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()

        if existing is not None:
            gen = existing["generation"] if "generation" in existing.keys() else 1
            _log_price_change(
                conn,
                product_id=product_id,
                product_name=product_name,
                old_retail=existing["retail"],
                new_retail=retail,
                old_wholesale=existing["wholesale"],
                new_wholesale=wholesale,
                source=source,
                generation=gen,
            )

            if log_activity:
                _log_status_change(
                    conn,
                    product_id=product_id,
                    product_name=product_name,
                    old_status=existing["status"],
                    new_status=status,
                    generation=gen,
                )

            # Optional columns (Description, Supplier): if this sheet
            # doesn't include the column, keep whatever's already in the
            # DB for that field rather than wiping it - only a sheet that
            # actually has the column can bulk-update it.
            description_val = str(row["Description"]) if has_description_col else existing["description"]
            supplier_val = str(row["Supplier"]) if has_supplier_col else (
                existing["supplier"] if "supplier" in existing.keys() else ""
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
                    status = ?,
                    description = ?,
                    supplier = ?
                WHERE product_id = ?
                """,
                (product_name, upc, unit, retail, wholesale, category, status,
                 description_val, supplier_val, product_id),
            )

            seen_product_ids.append(product_id)
        else:
            if not product_id:
                # Sheet left the Product ID blank for this row (the normal
                # way to signal "auto-assign one," same as the web Add
                # Product form) - reserve a real, never-reused id instead
                # of inserting a literal blank that could collide with a
                # future upload's blank row.
                product_id = get_next_product_id(conn)

            if product_id in historical_ids:
                reused_id_warnings.append(
                    f"{product_id} ({product_name or 'unnamed'}) was previously used by a "
                    f"different, now-deleted product - its notification/price history will "
                    f"now be shown as belonging to this new product. Double-check this ID "
                    f"was intentional."
                )

            description = str(row["Description"]) if has_description_col else ""
            supplier = str(row["Supplier"]) if has_supplier_col else ""
            conn.execute(
                """
                INSERT INTO products
                    (product_id, product_name, upc, unit, retail, wholesale, category, status, description, supplier, generation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (product_id, product_name, upc, unit, retail, wholesale, category, status, description, supplier, 1),
            )

            if log_activity:
                _log_activity(
                    conn,
                    event_type="product_added",
                    product_id=product_id,
                    generation=1,
                    product_name=product_name,
                    details="New product added via catalog upload",
                )

            seen_product_ids.append(product_id)

    if replace and seen_product_ids:
        placeholders = ",".join("?" for _ in seen_product_ids)

        # Fetch exactly which rows are about to flip to Out Of Stock
        # BEFORE running the bulk UPDATE below, so each one can get its
        # own activity_log entry - the UPDATE itself is a single bulk SQL
        # statement with no per-row hook to log from otherwise.
        about_to_go_oos = conn.execute(
            f"""
            SELECT product_id, product_name, status, generation FROM products
            WHERE product_id NOT IN ({placeholders})
              AND status != 'Out Of Stock'
            """,
            seen_product_ids,
        ).fetchall()

        conn.execute(
            f"""
            UPDATE products
            SET status = 'Out Of Stock'
            WHERE product_id NOT IN ({placeholders})
              AND status != 'Out Of Stock'
            """,
            seen_product_ids,
        )

        if log_activity:
            for row in about_to_go_oos:
                gen = row["generation"] if "generation" in row.keys() else 1
                _log_status_change(
                    conn,
                    product_id=row["product_id"],
                    product_name=row["product_name"],
                    old_status=row["status"],
                    new_status="Out Of Stock",
                    generation=gen,
                )

    conn.commit()
    conn.close()

    return len(df), reused_id_warnings


def _validated_stock_number(value, *, field_name, allow_zero=True):
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a valid number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid number.") from None
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        condition = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{field_name} must be {condition}.")
    return number


def _stock_balance(conn, product_pk):
    row = conn.execute(
        "SELECT quantity FROM stock_balances WHERE product_pk = ?",
        (product_pk,),
    ).fetchone()
    return float(row["quantity"]) if row else 0.0


def _record_stock_movement(
    conn,
    product,
    delta,
    movement_type,
    reference,
    note,
    actor,
):
    current_quantity = _stock_balance(conn, product["id"])
    new_quantity = current_quantity + delta
    if new_quantity < -1e-9:
        raise ValueError(
            f"Insufficient stock for {product['product_name']}: "
            f"{current_quantity:g} {product['unit'] or ''} available."
        )
    if abs(new_quantity) < 1e-9:
        new_quantity = 0.0

    conn.execute(
        """
        INSERT INTO stock_movements
            (product_pk, product_id, product_name, unit, generation, delta,
             balance_after, movement_type, reference, note, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product["id"],
            product["product_id"],
            product["product_name"],
            product["unit"] or "",
            product["generation"] or 1,
            delta,
            new_quantity,
            movement_type,
            reference,
            note,
            actor,
        ),
    )
    movement_id = conn.execute(
        "SELECT last_insert_rowid() AS id"
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO stock_balances (product_pk, quantity, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(product_pk) DO UPDATE SET
            quantity = excluded.quantity,
            updated_at = CURRENT_TIMESTAMP
        """,
        (product["id"], new_quantity),
    )
    return movement_id, new_quantity


def record_stock_movement(
    product_pk,
    quantity,
    movement_type,
    *,
    reference="",
    note="",
    actor="",
):
    quantity = _validated_stock_number(
        quantity, field_name="Quantity", allow_zero=False
    )
    if movement_type not in {"receipt", "issue"}:
        raise ValueError("Choose a valid stock movement type.")
    delta = quantity if movement_type == "receipt" else -quantity
    reference = str(reference or "").strip()
    note = str(note or "").strip()
    actor = str(actor or "").strip()
    if len(reference) > 120 or len(note) > 500 or len(actor) > 120:
        raise ValueError("Stock movement details are too long.")

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        product = conn.execute(
            "SELECT id, product_id, product_name, unit, generation FROM products WHERE id = ?",
            (product_pk,),
        ).fetchone()
        if product is None:
            raise ValueError("Product not found.")
        movement_id, _new_quantity = _record_stock_movement(
            conn, product, delta, movement_type, reference, note, actor
        )
        conn.commit()
        return movement_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_inventory_products():
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT p.id, p.product_id, p.product_name, p.unit, p.category, p.supplier,
               p.status, COALESCE(b.quantity, 0) AS quantity, b.updated_at
        FROM products p
        LEFT JOIN stock_balances b ON b.product_pk = p.id
        ORDER BY p.product_name COLLATE NOCASE, p.product_id
    """).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_stock_movements(product_pk=None, limit=200):
    conn = get_db_connection()
    query = "SELECT * FROM stock_movements"
    args = []
    if product_pk is not None:
        query += " WHERE product_pk = ?"
        args.append(product_pk)
    query += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(query, args).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def create_stock_count(actor="", count_date=None):
    count_date = count_date or date.today().isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO stock_counts (count_date, created_by) VALUES (?, ?)",
            (count_date, str(actor or "").strip()[:120]),
        )
        count_id = conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]
        products = conn.execute("""
            SELECT p.id, p.product_id, p.product_name, p.unit, p.generation,
                   COALESCE(b.quantity, 0) AS quantity
            FROM products p
            LEFT JOIN stock_balances b ON b.product_pk = p.id
            ORDER BY p.product_id
        """).fetchall()
        conn.executemany(
            """
            INSERT INTO stock_count_lines
                (count_id, product_pk, product_id, product_name, generation,
                 unit, book_quantity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    count_id,
                    product["id"],
                    product["product_id"],
                    product["product_name"],
                    product["generation"] or 1,
                    product["unit"] or "",
                    product["quantity"],
                )
                for product in products
            ],
        )
        conn.commit()
        return count_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_stock_counts():
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT c.*,
               COUNT(l.id) AS line_count,
               SUM(CASE WHEN l.counted_quantity IS NOT NULL THEN 1 ELSE 0 END) AS counted_count
        FROM stock_counts c
        LEFT JOIN stock_count_lines l ON l.count_id = c.id
        GROUP BY c.id
        ORDER BY c.id DESC
    """).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_stock_count(count_id):
    conn = get_db_connection()
    count = conn.execute(
        "SELECT * FROM stock_counts WHERE id = ?", (count_id,)
    ).fetchone()
    if count is None:
        conn.close()
        return None, []
    lines = conn.execute("""
        SELECT l.*, COALESCE(b.quantity, 0) AS current_quantity
        FROM stock_count_lines l
        LEFT JOIN stock_balances b ON b.product_pk = l.product_pk
        WHERE l.count_id = ?
        ORDER BY l.product_id
    """, (count_id,)).fetchall()
    conn.close()
    return dict(count), [dict(line) for line in lines]


def save_stock_count(count_id, counts):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute(
            "SELECT status FROM stock_counts WHERE id = ?", (count_id,)
        ).fetchone()
        if count is None:
            raise ValueError("Stock count not found.")
        if count["status"] != "draft":
            raise ValueError("Approved stock counts cannot be changed.")
        lines = conn.execute(
            "SELECT id FROM stock_count_lines WHERE count_id = ?", (count_id,)
        ).fetchall()
        valid_line_ids = {line["id"] for line in lines}
        if set(counts) != valid_line_ids:
            raise ValueError("The submitted count does not match this stock count.")
        for line_id, raw_quantity in counts.items():
            quantity = (
                None
                if raw_quantity is None or str(raw_quantity).strip() == ""
                else _validated_stock_number(
                    raw_quantity, field_name="Counted quantity", allow_zero=True
                )
            )
            conn.execute(
                "UPDATE stock_count_lines SET counted_quantity = ? WHERE id = ?",
                (quantity, line_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def approve_stock_count(count_id, actor=""):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute(
            "SELECT status FROM stock_counts WHERE id = ?", (count_id,)
        ).fetchone()
        if count is None:
            raise ValueError("Stock count not found.")
        if count["status"] != "draft":
            raise ValueError("This stock count is already approved.")
        lines = conn.execute(
            "SELECT * FROM stock_count_lines WHERE count_id = ? ORDER BY product_id",
            (count_id,),
        ).fetchall()
        if not lines:
            raise ValueError("This stock count has no products to approve.")
        if any(line["counted_quantity"] is None for line in lines):
            raise ValueError(
                "Enter a counted quantity for every product before approval."
            )

        for line in lines:
            product = conn.execute(
                "SELECT id, product_id, product_name, unit, generation FROM products WHERE id = ?",
                (line["product_pk"],),
            ).fetchone()
            if product is None or product["generation"] != line["generation"]:
                raise ValueError(
                    f"{line['product_id']} changed or was removed after this count began."
                )
            delta = line["counted_quantity"] - _stock_balance(
                conn, line["product_pk"]
            )
            if abs(delta) > 1e-9:
                _record_stock_movement(
                    conn,
                    product,
                    delta,
                    "count_adjustment",
                    f"COUNT-{count_id}",
                    "Approved physical stock count",
                    str(actor or "").strip()[:120],
                )
        conn.execute(
            """
            UPDATE stock_counts
            SET status = 'approved', approved_by = ?, approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(actor or "").strip()[:120], count_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_stock_count(count_id):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute(
            "SELECT status FROM stock_counts WHERE id = ?", (count_id,)
        ).fetchone()
        if count is None:
            raise ValueError("Stock count not found.")
        if count["status"] != "draft":
            raise ValueError("Approved stock counts cannot be deleted.")
        conn.execute("DELETE FROM stock_count_lines WHERE count_id = ?", (count_id,))
        conn.execute("DELETE FROM stock_counts WHERE id = ?", (count_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_purchase_order(
    supplier,
    items,
    *,
    order_date=None,
    invoice_number="",
    note="",
    actor="",
):
    supplier = str(supplier or "").strip()
    invoice_number = str(invoice_number or "").strip()
    note = str(note or "").strip()
    actor = str(actor or "").strip()
    order_date = str(order_date or date.today().isoformat()).strip()
    if not supplier or len(supplier) > 200:
        raise ValueError("Supplier is required and must be 200 characters or fewer.")
    if len(invoice_number) > 120 or len(note) > 500 or len(actor) > 120:
        raise ValueError("Purchase order details are too long.")
    try:
        if date.fromisoformat(order_date).isoformat() != order_date:
            raise ValueError
    except ValueError:
        raise ValueError("Choose a valid purchase date.") from None
    if not isinstance(items, list) or not items:
        raise ValueError("Add at least one product to the purchase order.")

    normalized_items = []
    seen_products = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid purchase order line.")
        raw_pk = item.get("product_pk")
        if isinstance(raw_pk, bool):
            raise ValueError("Choose a valid product.")
        try:
            product_pk = int(raw_pk)
        except (TypeError, ValueError):
            raise ValueError("Choose a valid product.") from None
        if product_pk in seen_products:
            raise ValueError("Each product may appear only once on a purchase order.")
        seen_products.add(product_pk)
        quantity = _validated_stock_number(
            item.get("quantity"), field_name="Ordered quantity", allow_zero=False
        )
        unit_cost = _validated_stock_number(
            item.get("unit_cost", 0), field_name="Unit cost", allow_zero=True
        )
        normalized_items.append((product_pk, quantity, unit_cost))

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        product_rows = {}
        for product_pk, _quantity, _unit_cost in normalized_items:
            product = conn.execute(
                """
                SELECT id, product_id, product_name, unit, generation
                FROM products WHERE id = ?
                """,
                (product_pk,),
            ).fetchone()
            if product is None:
                raise ValueError(f"Product {product_pk} was not found.")
            product_rows[product_pk] = product

        conn.execute(
            """
            INSERT INTO purchase_orders
                (order_code, supplier, invoice_number, order_date, note, created_by)
            VALUES ('PENDING', ?, ?, ?, ?, ?)
            """,
            (supplier, invoice_number, order_date, note, actor),
        )
        purchase_order_id = conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]
        order_code = f"PO-{date.today():%Y%m%d}-{purchase_order_id:05d}"
        conn.execute(
            "UPDATE purchase_orders SET order_code = ? WHERE id = ?",
            (order_code, purchase_order_id),
        )
        conn.executemany(
            """
            INSERT INTO purchase_order_items
                (purchase_order_id, product_pk, product_id, product_name, unit,
                 generation, ordered_quantity, unit_cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    purchase_order_id,
                    product_pk,
                    product_rows[product_pk]["product_id"],
                    product_rows[product_pk]["product_name"],
                    product_rows[product_pk]["unit"] or "",
                    product_rows[product_pk]["generation"] or 1,
                    quantity,
                    unit_cost,
                )
                for product_pk, quantity, unit_cost in normalized_items
            ],
        )
        conn.commit()
        return purchase_order_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_purchase_orders():
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT p.*,
               COUNT(i.id) AS line_count,
               COALESCE(SUM(i.ordered_quantity * i.unit_cost), 0) AS total_cost,
               COALESCE(SUM(i.received_quantity), 0) AS received_quantity,
               COALESCE(SUM(i.ordered_quantity), 0) AS ordered_quantity
        FROM purchase_orders p
        LEFT JOIN purchase_order_items i ON i.purchase_order_id = p.id
        GROUP BY p.id
        ORDER BY p.id DESC
    """).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_purchase_order(purchase_order_id):
    conn = get_db_connection()
    order = conn.execute(
        "SELECT * FROM purchase_orders WHERE id = ?", (purchase_order_id,)
    ).fetchone()
    if order is None:
        conn.close()
        return None, []
    items = conn.execute("""
        SELECT * FROM purchase_order_items
        WHERE purchase_order_id = ?
        ORDER BY product_id
    """, (purchase_order_id,)).fetchall()
    conn.close()
    return dict(order), [dict(item) for item in items]


def receive_purchase_order(purchase_order_id, quantities, *, actor=""):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT * FROM purchase_orders WHERE id = ?",
            (purchase_order_id,),
        ).fetchone()
        if order is None:
            raise ValueError("Purchase order not found.")
        if order["status"] not in {"open", "partial"}:
            raise ValueError("This purchase order cannot receive more stock.")
        lines = conn.execute(
            "SELECT * FROM purchase_order_items WHERE purchase_order_id = ?",
            (purchase_order_id,),
        ).fetchall()
        valid_line_ids = {line["id"] for line in lines}
        if set(quantities) != valid_line_ids:
            raise ValueError("The submitted receipt does not match this purchase order.")

        receipt_values = {}
        received_any = False
        for line in lines:
            raw_value = quantities[line["id"]]
            quantity = (
                0.0
                if raw_value is None or str(raw_value).strip() == ""
                else _validated_stock_number(
                    raw_value, field_name="Received quantity", allow_zero=True
                )
            )
            remaining = line["ordered_quantity"] - line["received_quantity"]
            if quantity > remaining + 1e-9:
                raise ValueError(
                    f"Cannot receive more than the remaining {remaining:g} "
                    f"{line['unit']} for {line['product_id']}."
                )
            receipt_values[line["id"]] = quantity
            received_any = received_any or quantity > 0
        if not received_any:
            raise ValueError("Enter a received quantity for at least one product.")

        for line in lines:
            quantity = receipt_values[line["id"]]
            if quantity <= 0:
                continue
            product = conn.execute(
                """
                SELECT id, product_id, product_name, unit, generation
                FROM products WHERE id = ?
                """,
                (line["product_pk"],),
            ).fetchone()
            if product is None or product["generation"] != line["generation"]:
                raise ValueError(
                    f"{line['product_id']} changed or was removed before receiving."
                )
            _record_stock_movement(
                conn,
                product,
                quantity,
                "purchase_receipt",
                order["order_code"],
                order["invoice_number"],
                str(actor or "").strip()[:120],
            )
            conn.execute(
                """
                UPDATE purchase_order_items
                SET received_quantity = received_quantity + ?
                WHERE id = ?
                """,
                (quantity, line["id"]),
            )

        remaining_count = conn.execute(
            """
            SELECT COUNT(*) AS remaining
            FROM purchase_order_items
            WHERE purchase_order_id = ? AND received_quantity < ordered_quantity
            """,
            (purchase_order_id,),
        ).fetchone()["remaining"]
        any_received = conn.execute(
            """
            SELECT 1 FROM purchase_order_items
            WHERE purchase_order_id = ? AND received_quantity > 0 LIMIT 1
            """,
            (purchase_order_id,),
        ).fetchone()
        next_status = "received" if remaining_count == 0 else (
            "partial" if any_received else "open"
        )
        conn.execute(
            """
            UPDATE purchase_orders
            SET status = ?,
                received_at = CASE WHEN ? = 'received'
                    THEN CURRENT_TIMESTAMP ELSE received_at END
            WHERE id = ?
            """,
            (next_status, next_status, purchase_order_id),
        )
        conn.commit()
        return next_status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_purchase_order(purchase_order_id):
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT status FROM purchase_orders WHERE id = ?",
            (purchase_order_id,),
        ).fetchone()
        if order is None:
            raise ValueError("Purchase order not found.")
        if order["status"] not in {"open", "partial"}:
            raise ValueError("This purchase order cannot be cancelled.")
        received = conn.execute(
            """
            SELECT 1 FROM purchase_order_items
            WHERE purchase_order_id = ? AND received_quantity > 0 LIMIT 1
            """,
            (purchase_order_id,),
        ).fetchone()
        if received:
            raise ValueError(
                "A purchase order with received stock cannot be cancelled; "
                "record a supplier return instead."
            )
        conn.execute(
            "UPDATE purchase_orders SET status = 'cancelled' WHERE id = ?",
            (purchase_order_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
