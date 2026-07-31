from datetime import datetime
from io import BytesIO

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    jsonify,
    send_from_directory,
    send_file
)

import os
import shutil
import tempfile
import pandas as pd

from werkzeug.security import check_password_hash

from config import (
    SECRET_KEY,
    EXCEL_FILE,
    UPLOAD_FOLDER,
    ADMIN_USERNAME,
    ADMIN_PASSWORD_HASH,
    REQUIRED_COLUMNS
)

import db

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Create the products table (if needed) and, on a brand new database,
# auto-import whatever Excel file is already sitting in uploads/.
db.init_db()


# ------------------------
# Helpers
# ------------------------

def validate_excel(path):

    try:
        df = pd.read_excel(
            path,
            sheet_name="Product Catalog"
        )
    except Exception as e:
        return False, str(e)

    missing = []

    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            missing.append(col)

    if missing:
        return False, f"Missing columns: {', '.join(missing)}"

    return True, None


def login_required():

    return session.get("admin") is True


# Product images live in static/product-images/, named to match the
# business Product ID with spaces stripped (e.g. "GTM - 0001" -> looks
# for GTM-0001.jpg / .jpeg / .png / .webp). No upload UI, no database
# column for the path - you just drop a correctly-named file in and it
# appears; if none exists, the detail page shows a placeholder instead.
PRODUCT_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]


def find_product_image(business_product_id):
    """Returns the static-relative path to a product's image if one
    exists on disk, e.g. 'product-images/GTM-0001.jpg' - or None."""

    slug = (business_product_id or "").replace(" ", "").strip()
    if not slug:
        return None

    images_dir = os.path.join(app.static_folder, "product-images")

    for ext in PRODUCT_IMAGE_EXTENSIONS:
        candidate = os.path.join(images_dir, slug + ext)
        if os.path.isfile(candidate):
            return f"product-images/{slug}{ext}"

    return None


# Gallery mode (browsing-only, Photos-app-style second view alongside
# List) - Level 1's category cards need, per category, the first 4
# products (id ASC, same ordering db.get_all_products() already uses)
# that actually have a resolvable image - skipping any without one so a
# category's cover never shows broken/placeholder tiles mixed with real
# photos.
def get_category_covers(all_products, category, limit=4):
    """Returns (cover_products, total_count) for one category. cover_products
    is a list of up to `limit` product dicts (with an added 'image_path'
    key) - the first ones in id order that have a real image on disk.
    total_count is every product in the category, regardless of image."""

    in_category = [p for p in all_products if p["Category"] == category]

    covers = []
    for p in in_category:
        image_path = find_product_image(p["Product ID"])
        if image_path:
            covers.append({**p, "image_path": image_path})
            if len(covers) >= limit:
                break

    return covers, len(in_category)


def product_form_to_dict(form):

    def to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def to_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    return {
        "product_id": form.get("product_id", "").strip(),
        "product_name": form.get("product_name", "").strip(),
        "upc": to_int(form.get("upc", 0)),
        "unit": form.get("unit", "-").strip() or "-",
        "retail": to_float(form.get("retail", 0)),
        "wholesale": to_float(form.get("wholesale", 0)),
        "category": form.get("category", "General").strip() or "General",
        "status": form.get("status", "In Stock").strip() or "In Stock",
        "description": form.get("description", "").strip(),
        "supplier": form.get("supplier", "").strip(),
    }


# ------------------------
# Public Routes
# ------------------------

@app.route("/")
def home():

    products = db.get_all_products()
    categories = db.get_categories()
    reps = db.get_active_reps()

    return render_template(
        "index.html",
        items=products,
        categories=categories,
        reps=reps
    )


@app.route("/product/<product_id_slug>")
def product_detail(product_id_slug):

    # Public, no login required - same audience as the catalog itself.
    product = db.get_product_by_business_id(product_id_slug)

    if product is None:
        # Not a flash+redirect like the admin 404s - a rep tapping a
        # stale/offline-cached link should get a clear "not found" page,
        # not silently bounced back to the catalog.
        return render_template("product_not_found.html", slug=product_id_slug), 404

    image_path = find_product_image(product["Product ID"])
    reps = db.get_active_reps()

    return render_template(
        "product_detail.html",
        p=product,
        image_path=image_path,
        reps=reps
    )


@app.route("/gallery")
def gallery_home():

    # Level 1: category cards with a 4-photo collage cover each. Public,
    # no login required - same audience as the catalog itself. Add-to-order
    # is out of scope for Gallery entirely (browsing-only), so this route
    # doesn't need reps/cart data the way home()/product_detail() do.
    all_products = db.get_all_products()
    categories = db.get_categories()

    category_cards = []
    for cat in categories:
        covers, total_count = get_category_covers(all_products, cat)
        category_cards.append({
            "name": cat,
            "covers": covers,
            "total_count": total_count,
        })

    return render_template("gallery.html", category_cards=category_cards)


@app.route("/gallery/<category>")
def gallery_category(category):

    categories = db.get_categories()

    if category not in categories:
        flash(f'"{category}" is not a known category.', "warning")
        return redirect(url_for("gallery_home"))

    all_products = db.get_all_products()
    products = [p for p in all_products if p["Category"] == category]

    for p in products:
        p["image_path"] = find_product_image(p["Product ID"])

    return render_template(
        "gallery_category.html",
        category=category,
        products=products,
    )


@app.route("/api/prices")
def api_prices():

    # Same field names as before ("Product ID", "Product Name", ...),
    # so Excel's "Get Data from Web" / Power Query keeps working unchanged.
    # The only addition is an "id" field used internally by the admin panel.
    return jsonify(db.get_all_products())


@app.route("/api/product-images")
def api_product_images():

    # Dedicated endpoint (not folded into /api/prices) so that contract
    # stays exactly as-is for Excel's Power Query integration. Used by
    # precache.js to know which images actually exist on disk without
    # guessing extensions client-side - only products with a real,
    # resolvable file are listed.
    products = db.get_all_products()

    images = []
    for p in products:
        image_path = find_product_image(p["Product ID"])
        if image_path:
            images.append({
                "product_id": p["Product ID"],
                "path": image_path,
            })

    return jsonify(images)


@app.route("/api/price-history")
def api_price_history():

    # Same idea as /api/prices, but for the price_history log, so it can
    # be pulled into Excel with Power Query too.
    # Optional ?product_id=GTM - 0001 filters to a single product;
    # otherwise returns the most recent changes across all products.
    product_id = request.args.get("product_id")

    if product_id:
        return jsonify(db.get_price_history(product_id, limit=1000))

    return jsonify(db.get_recent_price_changes(limit=1000))


@app.route("/api/activity")
def api_activity():

    # Backs the notification bell: new products + price changes, most
    # recent first. "latest_id" is always the true highest id regardless
    # of ?limit, so the client can cheaply check "is there anything new
    # since I last looked" with a small ?limit=1 request instead of
    # pulling the whole feed just to compare one number.
    limit = request.args.get("limit", default=30, type=int)
    limit = max(1, min(limit, 100))

    events = db.get_recent_activity(limit=limit)
    latest_id = events[0]["id"] if events else 0

    return jsonify({"latest_id": latest_id, "events": events})


@app.route("/order/submit", methods=["POST"])
def order_submit():

    # Deliberately public - reps use this straight from the catalog page,
    # no login required (matches the "no rep accounts" decision).
    payload = request.get_json(silent=True) or {}

    rep_name = (payload.get("rep_name") or "").strip()
    outlet_name = (payload.get("outlet_name") or "").strip()
    cart_items = payload.get("items") or []
    idempotency_key = (payload.get("idempotency_key") or "").strip() or None

    if not rep_name:
        return jsonify({"error": "Please select a sales rep."}), 400

    if not outlet_name:
        return jsonify({"error": "Outlet name is required."}), 400

    if not isinstance(cart_items, list) or not cart_items:
        return jsonify({"error": "Order is empty."}), 400

    # SECURITY: rep_name must exactly match one of the currently active,
    # admin-managed reps - never trust a rep string typed/tampered by the
    # client, since the whole point of this feature is "reps pick from a
    # list, they can't type anything."
    valid_labels = {db.rep_label(r) for r in db.get_active_reps()}
    if rep_name not in valid_labels:
        return jsonify({"error": "Invalid sales rep selected."}), 400

    order_id, total_retail, total_wholesale = db.create_order(
        rep_name, cart_items, outlet_name=outlet_name, idempotency_key=idempotency_key
    )

    if order_id is None:
        return jsonify({"error": "No valid items in this order."}), 400

    return jsonify({
        "order_id": order_id,
        "total_retail": total_retail,
        "total_wholesale": total_wholesale,
    })


@app.route("/sw.js")
def sw():

    response = send_from_directory(
        "static",
        "sw.js",
        mimetype="application/javascript"
    )

    response.headers["Service-Worker-Allowed"] = "/"

    return response


# ------------------------
# Login
# ------------------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if session.get("admin"):
        return redirect(url_for("admin"))

    if request.method == "POST":

        username = request.form.get("username", "").strip()

        password = request.form.get("password", "")

        if username != ADMIN_USERNAME:

            flash("Invalid username.", "danger")

            return redirect(url_for("login"))

        if not check_password_hash(
            ADMIN_PASSWORD_HASH,
            password
        ):

            flash("Invalid password.", "danger")

            return redirect(url_for("login"))

        session["admin"] = True

        flash("Welcome back!", "success")

        return redirect(url_for("admin"))

    return render_template("login.html")


@app.route("/logout")
def logout():

    session.clear()

    flash("Logged out.", "success")

    return redirect(url_for("login"))


# ------------------------
# Admin Dashboard
# ------------------------

@app.route("/admin")
def admin():

    if not login_required():
        return redirect(url_for("login"))

    stats = db.get_stats()

    last_updated = None

    if os.path.exists(EXCEL_FILE):
        last_updated = os.path.getmtime(EXCEL_FILE)

    return render_template(
        "admin.html",
        stats=stats,
        last_updated=last_updated,
        filename=os.path.basename(EXCEL_FILE)
    )


# ------------------------
# Manage Products (Edit / Add / Delete)
# ------------------------

@app.route("/admin/products")
def admin_products():

    if not login_required():
        return redirect(url_for("login"))

    products = db.get_all_products()
    categories = db.get_categories()

    return render_template(
        "admin_products.html",
        products=products,
        categories=categories
    )


@app.route("/admin/products/add", methods=["GET", "POST"])
def admin_product_add():

    if not login_required():
        return redirect(url_for("login"))

    categories = db.get_categories()

    if request.method == "POST":

        # The Product ID is always server-generated in Add mode, computed
        # fresh at submit time - never trust whatever the client sent for
        # this field, even though it's displayed read-only in the form.
        next_id = db.get_next_product_id()

        data = product_form_to_dict(request.form)
        data["product_id"] = next_id

        if not data["product_name"]:
            flash("Product Name is required.", "danger")
            return render_template(
                "product_form.html",
                product=None,
                mode="add",
                form_data=data,
                next_product_id=next_id,
                categories=categories
            )

        db.add_product(data)

        flash(f"Added \"{data['product_name']}\" as {next_id}.", "success")

        return redirect(url_for("admin_products"))

    next_id = db.get_next_product_id()

    return render_template(
        "product_form.html",
        product=None,
        mode="add",
        form_data=None,
        next_product_id=next_id,
        categories=categories
    )


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
def admin_product_edit(product_id):

    if not login_required():
        return redirect(url_for("login"))

    product = db.get_product(product_id)

    if product is None:
        flash("Product not found.", "danger")
        return redirect(url_for("admin_products"))

    categories = db.get_categories()

    if request.method == "POST":

        data = product_form_to_dict(request.form)

        if not data["product_name"]:
            flash("Product Name is required.", "danger")
            return render_template(
                "product_form.html",
                product=product,
                mode="edit",
                form_data=data,
                categories=categories,
                price_history=db.get_price_history(product["Product ID"])
            )

        db.update_product(product_id, data)

        flash(f"Updated \"{data['product_name']}\".", "success")

        return redirect(url_for("admin_products"))

    price_history = db.get_price_history(product["Product ID"])

    return render_template(
        "product_form.html",
        product=product,
        mode="edit",
        form_data=None,
        categories=categories,
        price_history=price_history
    )


@app.route("/admin/products/<int:product_id>/delete", methods=["POST"])
def admin_product_delete(product_id):

    if not login_required():
        return redirect(url_for("login"))

    product = db.get_product(product_id)

    db.delete_product(product_id)

    name = product["Product Name"] if product else "Product"

    flash(f"Deleted \"{name}\".", "success")

    return redirect(url_for("admin_products"))


@app.route("/admin/reps")
def admin_reps():

    if not login_required():
        return redirect(url_for("login"))

    reps = db.get_all_reps()

    return render_template("admin_reps.html", reps=reps)


@app.route("/admin/reps/add", methods=["GET", "POST"])
def admin_rep_add():

    if not login_required():
        return redirect(url_for("login"))

    if request.method == "POST":

        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()

        if not code or not name:
            flash("Both Code and Name are required.", "danger")
            return render_template("rep_form.html", mode="add", rep=None)

        db.add_rep(code, name)

        flash(f"Added sales rep \"{code} ({name})\".", "success")

        return redirect(url_for("admin_reps"))

    return render_template("rep_form.html", mode="add", rep=None)


@app.route("/admin/reps/<int:rep_id>/edit", methods=["GET", "POST"])
def admin_rep_edit(rep_id):

    if not login_required():
        return redirect(url_for("login"))

    rep = db.get_rep(rep_id)

    if rep is None:
        flash("Sales rep not found.", "danger")
        return redirect(url_for("admin_reps"))

    if request.method == "POST":

        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        active = request.form.get("active") == "on"

        if not code or not name:
            flash("Both Code and Name are required.", "danger")
            return render_template("rep_form.html", mode="edit", rep=rep)

        db.update_rep(rep_id, code, name, active)

        flash(f"Updated sales rep \"{code} ({name})\".", "success")

        return redirect(url_for("admin_reps"))

    return render_template("rep_form.html", mode="edit", rep=rep)


@app.route("/admin/reps/<int:rep_id>/delete", methods=["POST"])
def admin_rep_delete(rep_id):

    if not login_required():
        return redirect(url_for("login"))

    db.delete_rep(rep_id)

    flash("Sales rep deleted.", "success")

    return redirect(url_for("admin_reps"))


@app.route("/admin/orders")
def admin_orders():

    if not login_required():
        return redirect(url_for("login"))

    date_filter = request.args.get("date", "").strip() or None

    orders = db.get_all_orders(date_filter=date_filter)
    summary = db.get_order_summary(date_filter=date_filter)
    rep_breakdown = db.get_orders_by_rep(date_filter=date_filter)
    available_dates = db.get_order_dates()
    today = datetime.now().strftime("%Y-%m-%d")

    return render_template(
        "admin_orders.html",
        orders=orders,
        summary=summary,
        rep_breakdown=rep_breakdown,
        available_dates=available_dates,
        selected_date=date_filter,
        today=today
    )


@app.route("/admin/orders/<int:order_id>")
def admin_order_detail(order_id):

    if not login_required():
        return redirect(url_for("login"))

    order, items = db.get_order(order_id)

    if order is None:
        flash("Order not found.", "danger")
        return redirect(url_for("admin_orders"))

    return render_template("admin_order_detail.html", order=order, items=items)


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
def admin_order_status(order_id):

    if not login_required():
        return redirect(url_for("login"))

    new_status = request.form.get("status", "New").strip() or "New"

    db.update_order_status(order_id, new_status)

    flash(f"Order #{order_id} marked as {new_status}.", "success")

    return redirect(url_for("admin_order_detail", order_id=order_id))


@app.route("/admin/orders/<int:order_id>/delete", methods=["POST"])
def admin_order_delete(order_id):

    if not login_required():
        return redirect(url_for("login"))

    db.delete_order(order_id)

    flash(f"Order #{order_id} deleted.", "success")

    return redirect(url_for("admin_orders"))


# ------------------------
# Excel export (download current prices / price history as real .xlsx
# files - separate from /api/prices and /api/price-history, which serve
# live JSON for Power Query auto-refresh rather than a one-time download)
# ------------------------

@app.route("/admin/export/prices.xlsx")
def export_prices_excel():

    if not login_required():
        return redirect(url_for("login"))

    products = db.get_all_products()
    df = pd.DataFrame(products)

    if not df.empty:
        df = df.drop(columns=["id"], errors="ignore")

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Product Catalog", index=False)
    buffer.seek(0)

    filename = f"gtm_prices_{datetime.now().strftime('%Y-%m-%d')}.xlsx"

    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/admin/export/price-history.xlsx")
def export_price_history_excel():

    if not login_required():
        return redirect(url_for("login"))

    history = db.get_recent_price_changes(limit=1000000)  # effectively "all"
    df = pd.DataFrame(history)

    if not df.empty:
        df = df.drop(columns=["id"], errors="ignore")

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Price History", index=False)
    buffer.seek(0)

    filename = f"gtm_price_history_{datetime.now().strftime('%Y-%m-%d')}.xlsx"

    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ------------------------
# Upload Excel (bulk import into the database)
# ------------------------

@app.route("/upload", methods=["POST"])
def upload():

    if not login_required():
        return redirect(url_for("login"))

    if "excel" not in request.files:

        flash("No file selected.", "danger")

        return redirect(url_for("admin"))

    file = request.files["excel"]

    if file.filename == "":

        flash("No file selected.", "danger")

        return redirect(url_for("admin"))

    fd, temp_path = tempfile.mkstemp(
        suffix=".xlsx"
    )

    os.close(fd)

    try:

        file.save(temp_path)

        valid, error = validate_excel(
            temp_path
        )

        if not valid:

            os.remove(temp_path)

            flash(error, "danger")

            return redirect(url_for("admin"))

        shutil.move(
            temp_path,
            EXCEL_FILE
        )

        row_count = db.import_excel_into_db(EXCEL_FILE, replace=True)

        flash(
            f"Catalog uploaded successfully ({row_count} products).",
            "success"
        )

    except Exception as e:

        if os.path.exists(temp_path):
            os.remove(temp_path)

        flash(str(e), "danger")

    return redirect(url_for("admin"))


@app.template_filter("cleannum")
def cleannum(value):
    """Format a number for display in a text input: '1500.0' -> '1500',
    but '1500.5' stays '1500.5'. Leaves blanks/None/non-numeric untouched."""

    if value is None or value == "":
        return value

    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return value

    if as_float == int(as_float):
        return str(int(as_float))

    return str(as_float)


@app.template_filter("sqlitedatetime")
def sqlitedatetime(value):
    """Format a SQLite CURRENT_TIMESTAMP string ('2026-07-23 09:12:01')
    for display. Distinct from datetimeformat, which expects a Unix
    timestamp (used for the Excel file's mtime) - these are not
    interchangeable."""

    if not value:
        return "-"

    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d, %Y %I:%M %p")
    except (ValueError, TypeError):
        return value


@app.template_filter("datetimeformat")
def datetimeformat(value):

    if value is None:
        return "-"

    return datetime.fromtimestamp(value).strftime("%b %d, %Y %I:%M %p")


# ------------------------
# Run
# ------------------------

if __name__ == "__main__":

    app.run(
        debug=True
    )
