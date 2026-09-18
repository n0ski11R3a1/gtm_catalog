from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

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
    send_file,
    has_request_context,
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
    REQUIRED_COLUMNS,
    VAPID_PUBLIC_KEY,
    PRODUCT_IMAGES_DIR,
    THUMBNAILS_DIR,
    MAX_IMAGE_SIZE,
    ALLOWED_IMAGE_EXTENSIONS,
    ALLOWED_MIME_TYPES,
    WEBP_QUALITY,
    IMAGE_MAX_WIDTH,
    THUMBNAIL_WIDTH,
    GALLERY_PAGE_SIZE,
)

import db
import push
from services import (
    build_product_image_filename,
    delete_product_image_files,
    generate_thumbnail,
    is_allowed_image_file,
    prepare_uploaded_product_image,
    save_image_upload,
    save_product_image,
    validate_image_size,
    validate_product_form,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["PRODUCT_IMAGES_DIR"] = PRODUCT_IMAGES_DIR
app.config["THUMBNAILS_DIR"] = THUMBNAILS_DIR
app.config["MAX_IMAGE_SIZE"] = MAX_IMAGE_SIZE
app.config["ALLOWED_IMAGE_EXTENSIONS"] = ALLOWED_IMAGE_EXTENSIONS
app.config["ALLOWED_MIME_TYPES"] = ALLOWED_MIME_TYPES
app.config["WEBP_QUALITY"] = WEBP_QUALITY
app.config["IMAGE_MAX_WIDTH"] = IMAGE_MAX_WIDTH
app.config["THUMBNAIL_WIDTH"] = THUMBNAIL_WIDTH
app.config["GALLERY_PAGE_SIZE"] = GALLERY_PAGE_SIZE

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

    # Product IDs must be present and unique once spaces and case are
    # ignored. The official catalog export contains legacy filler rows
    # (like a dozen blank Product IDs at the end) and a few accidental
    # duplicates, which would otherwise fail only at SQLite's unique-index
    # layer after the upload has already started.
    seen = {}
    blank_rows = []
    for index, raw in enumerate(df.get("Product ID", []), start=2):
        if raw is None or pd.isna(raw):
            blank_rows.append(index)
            continue

        value = str(raw).strip()
        if not value:
            blank_rows.append(index)
            continue

        normalized = value.replace(" ", "").upper()
        seen.setdefault(normalized, []).append((index, value))

    if blank_rows:
        return False, (
            "Blank Product IDs found in the sheet (rows "
            + ", ".join(str(r) for r in blank_rows[:10])
            + (" ..." if len(blank_rows) > 10 else "")
            + "). Every row must have a real Product ID before import."
        )

    duplicates = {norm: values for norm, values in seen.items() if len(values) > 1}
    if duplicates:
        details = "; ".join(
            f"{norm} ({len(values)} rows: {', '.join(v for _, v in values[:3])}{'...' if len(values) > 3 else ''})"
            for norm, values in sorted(duplicates.items())
        )
        return False, f"Duplicate Product IDs detected after normalizing spaces/case: {details}"

    return True, None


def login_required():
    """Return True only when an active request has an authenticated admin session."""
    if not has_request_context():
        return False
    return session.get("admin") is True


def require_admin():
    """Redirect unauthenticated users to the login page."""
    if not login_required():
        return redirect(url_for("login"))
    return None


def get_json_payload():
    """Return a JSON body as a dict, or an empty dict when the body is missing or invalid."""
    payload = request.get_json(silent=True) or {}
    return payload if isinstance(payload, dict) else {}


# Product images live in static/product-images/, named to match the
# business Product ID with spaces stripped (e.g. "GTM - 0001" -> looks
# for GTM-0001.jpg / .jpeg / .png / .webp). No upload UI, no database
# column for the path - you just drop a correctly-named file in and it
# appears; if none exists, the detail page shows a placeholder instead.
PRODUCT_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]


def product_photo_slug(business_product_id):
    slug = (business_product_id or "").replace(" ", "").strip()
    if not slug:
        return ""
    return slug.upper()


def build_product_image_filename(business_product_id):
    slug = product_photo_slug(business_product_id)
    if not slug:
        return ""
    return f"{slug}.webp"


def is_allowed_image_file(file):
    """Validate both extension and MIME type."""
    if not file or not getattr(file, "filename", None):
        return False, "No file selected"

    ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "").lower()
    if ext not in app.config["ALLOWED_IMAGE_EXTENSIONS"]:
        return False, f"Invalid format. Allowed: {', '.join(sorted(app.config['ALLOWED_IMAGE_EXTENSIONS']))}"

    if file.mimetype not in app.config["ALLOWED_MIME_TYPES"]:
        return False, f"Invalid MIME type: {file.mimetype or 'unknown'}"

    return True, None


def validate_image_size(file):
    try:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        if hasattr(file, "seek"):
            try:
                file.seek(0)
            except Exception:
                pass
    except (AttributeError, OSError):
        if hasattr(file, "read"):
            pos = None
            try:
                pos = file.tell()
            except Exception:
                pass
            data = file.read()
            size = len(data)
            if pos is not None:
                try:
                    file.seek(pos)
                except Exception:
                    pass
            if hasattr(file, "seek"):
                try:
                    file.seek(0)
                except Exception:
                    pass
        else:
            return False, "Unsupported file object"

    if size > app.config["MAX_IMAGE_SIZE"]:
        return False, f"File too large: {size / 1024 / 1024:.1f} MB (max {app.config['MAX_IMAGE_SIZE'] / 1024 / 1024:.0f} MB)"

    return True, None


def compress_to_webp(file_obj, max_width=None, quality=None):
    max_width = max_width or app.config.get("IMAGE_MAX_WIDTH", 1200)
    quality = quality or app.config.get("WEBP_QUALITY", 80)

    file_obj.seek(0)
    img = Image.open(file_obj)

    if img.width > max_width:
        ratio = max_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

    if img.mode in ("RGBA", "LA", "P"):
        if img.mode == "P":
            img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "RGBA":
            alpha = img.split()[-1]
            background.paste(img, mask=alpha)
        else:
            background.paste(img)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    output = BytesIO()
    img.save(output, format="WEBP", quality=quality, method=6)
    output.seek(0)
    return output


def generate_thumbnail(product_id):
    """Generate and cache a 400px-wide thumbnail for a product image."""
    product_id = (product_id or "").strip()
    if not product_id:
        return None

    full_name = None
    for ext in PRODUCT_IMAGE_EXTENSIONS:
        candidate = os.path.join(app.config["PRODUCT_IMAGES_DIR"], f"{product_id}{ext}")
        if os.path.isfile(candidate):
            full_name = candidate
            break

    if not full_name:
        return None

    thumb_dir = app.config["THUMBNAILS_DIR"]
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, f"{product_id}.webp")

    with Image.open(full_name) as img:
        width = app.config.get("THUMBNAIL_WIDTH", 400)
        if img.width > width:
            ratio = width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((width, new_height), Image.Resampling.LANCZOS)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        if img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        img.save(thumb_path, format="WEBP", quality=app.config.get("WEBP_QUALITY", 80), method=6)

    return thumb_path


def prepare_uploaded_product_image(file, product_id):
    """Return (compressed_bytesio, target_filename) for a product image.

    The uploaded file's original client-side name is intentionally ignored.
    The server always renames it to the canonical GTM-####.webp target.
    """
    if not file or not getattr(file, "filename", None):
        raise ValueError("No file selected")

    is_valid, msg = is_allowed_image_file(file)
    if not is_valid:
        raise ValueError(msg)

    is_ok, msg = validate_image_size(file)
    if not is_ok:
        raise ValueError(msg)

    target_filename = build_product_image_filename(product_id)
    if not target_filename:
        raise ValueError("Product ID is required to save an image")

    compressed = compress_to_webp(file)
    return compressed, target_filename


def save_product_image(file, product_id):
    compressed, filename = prepare_uploaded_product_image(file, product_id)
    target_path = os.path.join(app.config["PRODUCT_IMAGES_DIR"], filename)

    with open(target_path, "wb") as fh:
        fh.write(compressed.getvalue())

    return filename


def find_product_image(business_product_id):
    """Returns the static-relative path to a product's image if one
    exists on disk, e.g. 'product-images/GTM-0001.jpg' - or None."""

    slug = product_photo_slug(business_product_id)
    if not slug:
        return None

    images_dir = app.config["PRODUCT_IMAGES_DIR"]

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
    """Returns (cover_products, total_count, oos_count) for one category.
    cover_products is a list of up to `limit` product dicts (with an added
    'image_path' key) - the first ones in id order that have a real image
    on disk. total_count is every product in the category, regardless of
    image. oos_count is how many of those are Out Of Stock, so the gallery
    category card can surface stock status without opening the category."""

    in_category = [p for p in all_products if p["Category"] == category]
    oos_count = sum(1 for p in in_category if p["Status"].lower() == "out of stock")

    covers = []
    for p in in_category:
        image_path = find_product_image(p["Product ID"])
        if image_path:
            covers.append({**p, "image_path": image_path})
            if len(covers) >= limit:
                break

    return covers, len(in_category), oos_count


def notify_activity_since(before_id):
    """Call right after a db write that might have logged activity_log
    rows (add_product / update_product / import_excel_into_db). Diffs
    against the id snapshot taken right before that write and pushes a
    notification for whatever actually got logged - reuses
    _log_price_change/_log_status_change's own "did it actually change"
    checks (already applied inside db.py) rather than re-deciding that
    here, and never fires a push if nothing really changed."""
    events = db.get_activity_after(before_id)
    push.notify_new_activity(events)


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


def product_record_to_db_payload(product, overrides=None):
    payload = {
        "product_id": product.get("Product ID") or product.get("product_id", ""),
        "product_name": product.get("Product Name") or product.get("product_name", ""),
        "upc": product.get("UPC", product.get("upc", 0)),
        "unit": product.get("Unit") or product.get("unit", "-"),
        "retail": product.get("Retail", product.get("retail", 0)),
        "wholesale": product.get("Wholesale", product.get("wholesale", 0)),
        "category": product.get("Category") or product.get("category", "General"),
        "status": product.get("Status") or product.get("status", "In Stock"),
        "description": product.get("Description") or product.get("description", ""),
        "supplier": product.get("Supplier") or product.get("supplier", ""),
        "has_image": bool(product.get("has_image", False)),
    }
    if overrides:
        payload.update(overrides)
    return payload


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
        covers, total_count, oos_count = get_category_covers(all_products, cat)
        category_cards.append({
            "name": cat,
            "covers": covers,
            "total_count": total_count,
            "oos_count": oos_count,
        })

    return render_template("gallery.html", category_cards=category_cards)


@app.route("/thumbnails/<product_id>.webp")
def product_thumbnail(product_id):
    thumb_path = os.path.join(app.config["THUMBNAILS_DIR"], f"{product_id}.webp")
    if os.path.exists(thumb_path):
        response = send_file(thumb_path, mimetype="image/webp")
        response.headers["Cache-Control"] = "public, max-age=604800"
        return response

    try:
        generated = generate_thumbnail(product_id)
    except Exception as exc:
        app.logger.error("Thumbnail generation failed for %s: %s", product_id, exc)
        return "", 500

    if generated is None:
        return "", 404

    response = send_file(generated, mimetype="image/webp")
    response.headers["Cache-Control"] = "public, max-age=604800"
    return response


@app.route("/gallery/<category>")
def gallery_category(category):

    categories = db.get_categories()

    if category not in categories:
        flash(f'"{category}" is not a known category.', "warning")
        return redirect(url_for("gallery_home"))

    page = request.args.get("page", 1, type=int)
    per_page = app.config.get("GALLERY_PAGE_SIZE", 24)

    all_products = db.get_all_products()
    products = [p for p in all_products if p["Category"] == category]

    total = len(products)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    paged_products = products[start:end]

    for p in paged_products:
        p["image_path"] = find_product_image(p["Product ID"])

    return render_template(
        "gallery_category.html",
        category=category,
        products=paged_products,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
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


# Fields safe to expose on the PUBLIC, unauthenticated /api/price-history
# endpoint (no login_required() - see api_price_history below). Excludes
# old_base_price/new_base_price/old_b2c/new_b2c: those are internal
# supplier-cost columns on price_history, and this endpoint has no auth
# check, so leaving them in would hand cost data to anyone who hits the
# URL. The admin-only Price History tab (behind login_required(), in
# admin_product_edit) reads db.get_price_history() directly instead of
# through this endpoint, so it still sees the full row - this filtering
# only affects what's serialized here.
_PUBLIC_PRICE_HISTORY_FIELDS = (
    "id", "product_id", "product_name",
    "old_retail", "new_retail", "old_wholesale", "new_wholesale",
    "source", "changed_at",
)


def _sanitize_price_history_for_public_api(rows):
    return [
        {field: row[field] for field in _PUBLIC_PRICE_HISTORY_FIELDS if field in row}
        for row in rows
    ]


def _format_activity_event(event):
    """Format an activity event with human-readable title and message.
    
    Transforms raw activity_log entries into user-friendly notifications
    with clear titles and descriptive messages.
    """
    if not event:
        return event
    
    event_type = event.get("event_type")
    product_name = event.get("product_name") or "Unknown Product"
    product_id = event.get("product_id", "")
    details = event.get("details", "")
    created_at = event.get("created_at", "")
    generation = event.get("generation", 1)
    
    # Generate human-readable title and message based on event type
    if event_type == "product_added":
        # Check if it was from excel upload
        if "excel" in details.lower() or "catalog upload" in details.lower():
            title = f"New product added: {product_name}"
            message = "Added via catalog upload"
        else:
            title = f"New product added: {product_name}"
            message = "Added manually"
    
    elif event_type == "product_replaced":
        # Extract old product name from details if available
        old_name = "previous product"
        if "Replaced" in details and "with" in details:
            try:
                old_name = details.split('Replaced "')[1].split('" with')[0]
            except:
                pass
        title = f"Product replaced: {product_name}"
        message = f"Replaced \"{old_name}\""
    
    elif event_type == "price_changed":
        # Use the existing details which already has formatted price changes
        title = f"Price updated: {product_name}"
        message = details or "Price changed"
    
    elif event_type == "out_of_stock":
        title = f"Out of stock: {product_name}"
        message = "Marked as Out of Stock"
    
    elif event_type == "back_in_stock":
        title = f"Back in stock: {product_name}"
        message = "Now available (In Stock)"
    
    else:
        title = f"Catalog update: {product_name}"
        message = details or "Updated"
    
    # Return enhanced event with formatted fields
    return {
        **event,
        "formatted_title": title,
        "formatted_message": message,
        "display_time": _format_relative_time(created_at),
        "product_link": f"/product/{product_id.replace(' ', '')}" if product_id else None,
    }


def _format_relative_time(sqlite_string):
    """Format SQLite timestamp as relative time (e.g., '2h ago', '3d ago')."""
    if not sqlite_string:
        return "Unknown time"
    
    try:
        # SQLite timestamps are in UTC but without timezone marker
        iso = sqlite_string.replace(" ", "T") + "Z"
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt
        
        if diff.total_seconds() < 60:
            return "just now"
        elif diff.total_seconds() < 3600:
            mins = int(diff.total_seconds() / 60)
            return f"{mins}m ago"
        elif diff.total_seconds() < 86400:
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}h ago"
        elif diff.total_seconds() < 604800:
            days = int(diff.total_seconds() / 86400)
            return f"{days}d ago"
        else:
            return dt.strftime("%b %d, %Y")
    except Exception:
        return sqlite_string


@app.route("/api/price-history")
def api_price_history():

    # Same idea as /api/prices, so it can be pulled into Excel with Power
    # Query too. PUBLIC, unauthenticated - which is exactly why the
    # result is run through _sanitize_price_history_for_public_api()
    # before being returned: Base Price/B2C history must never appear
    # here (see that function's docstring). The full, unsanitized rows
    # are still available in-app on the login-gated Price History tab.
    # Optional ?product_id=GTM - 0001 filters to a single product;
    # otherwise returns the most recent changes across all products.
    product_id = request.args.get("product_id")

    if product_id:
        rows = db.get_price_history(product_id, limit=1000)
    else:
        rows = db.get_recent_price_changes(limit=1000)

    return jsonify(_sanitize_price_history_for_public_api(rows))


@app.route("/api/activity")
def api_activity():

    # Backs the notification bell: new products + price changes, most
    # recent first. "latest_id" is always the true highest id regardless
    # of ?limit, so the client can cheaply check "is there anything new
    # since I last looked" with a small ?limit=1 request instead of
    # pulling the whole feed just to compare one number.
    #
    # ?before_id powers the panel's "See more" button - a cursor, not a
    # page number, so rows already shown can't shift or duplicate if
    # something new gets logged between clicks (see get_recent_activity).
    limit = request.args.get("limit", default=30, type=int)
    limit = max(1, min(limit, 100))
    before_id = request.args.get("before_id", type=int)

    events = db.get_recent_activity(limit=limit, before_id=before_id)

    # Format events with human-readable titles and messages
    formatted_events = [_format_activity_event(e) for e in events]

    # latest_id reflects the true current max regardless of pagination -
    # a "See more" request (before_id set) still needs the real latest_id
    # so the client's unread check stays correct, not the id of the
    # oldest row in this particular page.
    latest_id = db.get_latest_activity_id()

    return jsonify({
        "latest_id": latest_id,
        "events": formatted_events,
        "has_more": len(events) == limit,
    })


@app.route("/api/activity/summary")
def api_activity_summary():
    """Grouped activity summary for the Changes Summary page.
    
    Returns activities grouped by event_type with counts, for a quick
    overview of what changed without scrolling through individual items.
    """
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit, 500))
    before_id = request.args.get("before_id", type=int)

    events = db.get_recent_activity(limit=limit, before_id=before_id)
    
    # Group by event_type
    from collections import defaultdict
    groups = defaultdict(list)
    for event in events:
        event_type = event.get("event_type", "unknown")
        formatted = _format_activity_event(event)
        groups[event_type].append(formatted)
    
    # Build summary with counts and sample items per group
    summary = []
    for event_type, items in groups.items():
        # Sort by time descending within each group
        items.sort(key=lambda x: x.get("id", 0), reverse=True)
        
        # Get human-readable group name
        group_names = {
            "product_added": "New Products",
            "product_replaced": "Products Replaced",
            "price_changed": "Price Changes",
            "out_of_stock": "Out of Stock",
            "back_in_stock": "Back in Stock",
        }
        group_name = group_names.get(event_type, event_type.replace("_", " ").title())
        
        summary.append({
            "event_type": event_type,
            "group_name": group_name,
            "count": len(items),
            "items": items[:5],  # Show first 5 items in each group
        })
    
    # Sort groups by most recent activity
    summary.sort(key=lambda g: g["items"][0].get("id", 0) if g["items"] else 0, reverse=True)
    
    latest_id = db.get_latest_activity_id()
    
    return jsonify({
        "latest_id": latest_id,
        "groups": summary,
        "total_count": len(events),
        "has_more": len(events) == limit,
    })


@app.route("/api/push/public-key")
def api_push_public_key():
    # Public, no login - the frontend needs this to call
    # PushManager.subscribe() with the right applicationServerKey.
    # It's a public key by definition; there's nothing to protect here.
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    # Public, no login - same audience as the catalog itself. Any device
    # that opts in (via the browser's own permission prompt) can
    # register, no rep account or admin gate involved.
    payload = get_json_payload()
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "Invalid subscription."}), 400

    db.add_push_subscription(endpoint, p256dh, auth)
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    payload = get_json_payload()
    endpoint = payload.get("endpoint")

    if endpoint:
        db.remove_push_subscription(endpoint)

    return jsonify({"ok": True})


@app.route("/order/submit", methods=["POST"])
def order_submit():

    # Deliberately public - reps use this straight from the catalog page,
    # no login required (matches the "no rep accounts" decision).
    payload = get_json_payload()

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

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    stats = db.get_stats()
    today = datetime.now().date().isoformat()
    today_orders = db.get_order_summary(today)
    recent_orders = db.get_all_orders()[:5]

    last_updated = None

    if os.path.exists(EXCEL_FILE):
        last_updated = os.path.getmtime(EXCEL_FILE)

    return render_template(
        "admin.html",
        stats=stats,
        today=today,
        today_orders=today_orders,
        recent_orders=recent_orders,
        last_updated=last_updated,
        filename=os.path.basename(EXCEL_FILE)
    )


@app.route("/admin/upload-images", methods=["GET", "POST"])
def admin_upload_images():
    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    if request.method == "GET":
        return render_template("admin_upload_images.html")

    zip_file = request.files.get("zip_file")
    if not zip_file or not zip_file.filename:
        flash("No ZIP file selected.", "danger")
        return render_template("admin_upload_images.html")

    if not zip_file.filename.lower().endswith(".zip"):
        flash("Only .zip files are accepted.", "danger")
        return render_template("admin_upload_images.html")

    results = []
    success = 0
    skipped = 0
    errors = 0

    try:
        import zipfile as zipfile_module
        with zipfile_module.ZipFile(zip_file) as archive:
            for entry in archive.namelist():
                base = os.path.basename(entry)
                if not base or base.startswith("."):
                    continue

                ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
                if ext not in app.config["ALLOWED_IMAGE_EXTENSIONS"]:
                    results.append((base, "skip", "Not an image file — skipped."))
                    skipped += 1
                    continue

                name_without_ext = base.rsplit(".", 1)[0].upper()
                if not name_without_ext.startswith("GTM-"):
                    results.append((base, "error", "Filename must start with 'GTM-' (example: GTM-0001.jpg)."))
                    errors += 1
                    continue

                product_id = name_without_ext
                product = db.get_product_by_business_id(product_id)
                if product is None:
                    results.append((base, "error", f"No product found with ID '{product_id}'."))
                    errors += 1
                    continue

                try:
                    raw_bytes = archive.read(entry)
                except Exception as exc:
                    results.append((base, "error", f"Could not read ZIP entry: {exc}"))
                    errors += 1
                    continue

                if len(raw_bytes) > app.config["MAX_IMAGE_SIZE"]:
                    mb = len(raw_bytes) / 1024 / 1024
                    max_mb = app.config["MAX_IMAGE_SIZE"] / 1024 / 1024
                    results.append((base, "error", f"Too large ({mb:.1f} MB, max {max_mb:.0f} MB)."))
                    errors += 1
                    continue

                try:
                    compressed = compress_to_webp(BytesIO(raw_bytes))
                except Exception as exc:
                    results.append((base, "error", f"Image processing failed: {exc}"))
                    errors += 1
                    continue

                target_name = f"{product_id}.webp"
                target_path = os.path.join(app.config["PRODUCT_IMAGES_DIR"], target_name)
                try:
                    with open(target_path, "wb") as fh:
                        fh.write(compressed.getvalue())
                    thumb_path = os.path.join(app.config["THUMBNAILS_DIR"], f"{product_id}.webp")
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
                    db.update_product(product["id"], {**product, "has_image": True})
                    results.append((base, "ok", f"Saved as {target_name}."))
                    success += 1
                except Exception as exc:
                    results.append((base, "error", f"Could not save file: {exc}"))
                    errors += 1
    except Exception as exc:
        flash(f"Invalid or corrupted ZIP file: {exc}", "danger")
        return render_template("admin_upload_images.html")

    return render_template(
        "admin_upload_images.html",
        results=results,
        success=success,
        skipped=skipped,
        errors=errors,
    )


@app.route("/admin/clear-thumbnail-cache", methods=["POST"])
def admin_clear_thumbnail_cache():
    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    deleted = 0
    for filename in os.listdir(app.config["THUMBNAILS_DIR"]):
        if filename.endswith(".webp"):
            try:
                os.remove(os.path.join(app.config["THUMBNAILS_DIR"], filename))
                deleted += 1
            except OSError:
                pass

    flash(f"Cleared {deleted} cached thumbnails.", "success")
    return redirect(url_for("admin"))


# ------------------------
# Manage Products (Edit / Add / Delete)
# ------------------------

@app.route("/admin/products")
def admin_products():

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    products = db.get_all_products()
    categories = db.get_categories()

    return render_template(
        "admin_products.html",
        products=products,
        categories=categories
    )


@app.route("/admin/images")
def admin_images():

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    products = db.get_all_products()
    for product in products:
        product["image_path"] = find_product_image(product["Product ID"])
        product["has_image"] = bool(product["image_path"])

    return render_template(
        "admin_images.html",
        products=products,
    )


@app.route("/admin/products/add", methods=["GET", "POST"])
def admin_product_add():

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    categories = db.get_categories()
    available_ids = db.get_available_product_ids()

    if request.method == "POST":

        data = product_form_to_dict(request.form)

        form_errors = validate_product_form(data)

        if form_errors:
            for err in form_errors:
                flash(err, "danger")
            return render_template(
                "product_form.html",
                product=None,
                mode="add",
                form_data=data,
                next_product_id=db.peek_next_product_id(),
                available_ids=available_ids,
                categories=categories
            )

        reuse_id = request.form.get("reuse_product_id", "").strip()

        if reuse_id:
            # Reusing an existing Out Of Stock product's ID
            try:
                before_id = db.get_latest_activity_id()
                product_pk = db.reuse_product_id(reuse_id, data)
                notify_activity_since(before_id)

                # Handle image upload for reused product
                uploaded_image = request.files.get("product_image")
                if uploaded_image and uploaded_image.filename:
                    try:
                        expected_filename = save_product_image(uploaded_image, reuse_id)
                        # Update has_image flag
                        conn = db.get_db_connection()
                        conn.execute(
                            "UPDATE products SET has_image = 1 WHERE product_id = ?",
                            (reuse_id,)
                        )
                        conn.commit()
                        conn.close()
                        flash(f"Image saved as {expected_filename}.", "success")
                    except Exception as exc:
                        flash(f"Image upload failed: {exc}", "danger")

                flash(f"Replaced product with \"{data['product_name']}\" using {reuse_id}.", "success")
            except ValueError as e:
                flash(str(e), "danger")
                return render_template(
                    "product_form.html",
                    product=None,
                    mode="add",
                    form_data=data,
                    next_product_id=db.peek_next_product_id(),
                    available_ids=available_ids,
                    categories=categories
                )
        else:
            # Normal new product - generate new ID
            next_id = db.get_next_product_id()
            data["product_id"] = next_id

            uploaded_image = request.files.get("product_image")
            if uploaded_image and uploaded_image.filename:
                try:
                    _, expected_filename = prepare_uploaded_product_image(uploaded_image, next_id)
                    save_product_image(uploaded_image, next_id)
                    data["has_image"] = True
                    flash(f"Image saved as {expected_filename}.", "success")
                except Exception as exc:
                    flash(f"Image upload failed: {exc}", "danger")
                    return render_template(
                        "product_form.html",
                        product=None,
                        mode="add",
                        form_data=data,
                        next_product_id=next_id,
                        available_ids=available_ids,
                        categories=categories
                    )

            before_id = db.get_latest_activity_id()
            db.add_product(data)
            notify_activity_since(before_id)

            flash(f"Added \"{data['product_name']}\" as {next_id}.", "success")

        return redirect(url_for("admin_products"))

    # GET (just showing the form): PEEK only, don't reserve/consume a
    # real id - this is a preview, not a commitment. The actual id is
    # generated fresh at submit time above (line ~536), which is also
    # why it's safe for this preview number to differ from what a
    # concurrent admin's submit ends up using.
    next_id = db.peek_next_product_id()

    return render_template(
        "product_form.html",
        product=None,
        mode="add",
        form_data=None,
        next_product_id=next_id,
        available_ids=available_ids,
        categories=categories
    )


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
def admin_product_edit(product_id):

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    product = db.get_product(product_id)

    if product is None:
        flash("Product not found.", "danger")
        return redirect(url_for("admin_products"))

    categories = db.get_categories()

    if request.method == "POST":

        data = product_form_to_dict(request.form)

        form_errors = validate_product_form(data)

        if form_errors:
            for err in form_errors:
                flash(err, "danger")
            return render_template(
                "product_form.html",
                product=product,
                mode="edit",
                form_data=data,
                categories=categories,
                price_history=db.get_price_history(product["Product ID"])
            )

        uploaded_image = request.files.get("product_image")
        if uploaded_image and uploaded_image.filename:
            if product.get("has_image") and not request.form.get("confirm_replace_image"):
                flash("This product already has an image. Check the replacement box to confirm replacing it.", "warning")
                return render_template(
                    "product_form.html",
                    product=product,
                    mode="edit",
                    form_data=data,
                    categories=categories,
                    price_history=db.get_price_history(product["Product ID"]),
                    show_replace_warning=True,
                )

            try:
                current_product_id = data.get("product_id") or product["Product ID"]
                if product.get("has_image") and current_product_id != product["Product ID"]:
                    delete_product_image_files(product["Product ID"])
                saved_name = save_image_upload(uploaded_image, current_product_id)
                data["has_image"] = True
                flash(f"Image saved as {saved_name}.", "success")
            except Exception as exc:
                flash(f"Image upload failed: {exc}", "danger")
                return render_template(
                    "product_form.html",
                    product=product,
                    mode="edit",
                    form_data=data,
                    categories=categories,
                    price_history=db.get_price_history(product["Product ID"])
                )

        if request.form.get("delete_image"):
            delete_product_image_files(product["Product ID"])
            data["has_image"] = False
            flash("Image deleted.", "success")

        before_id = db.get_latest_activity_id()
        db.update_product(product_id, data)
        notify_activity_since(before_id)

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


@app.route("/admin/products/<int:product_id>/images", methods=["GET", "POST"])
def admin_product_images(product_id):

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    product = db.get_product(product_id)
    if product is None:
        flash("Product not found.", "danger")
        return redirect(url_for("admin_products"))

    if request.method == "POST":
        payload = product_record_to_db_payload(product)

        if request.form.get("delete_image"):
            delete_product_image_files(product["Product ID"])
            payload["has_image"] = False
            db.update_product(product_id, payload)
            flash("Image deleted.", "success")
            return redirect(url_for("admin_product_images", product_id=product_id))

        uploaded_image = request.files.get("product_image")
        if uploaded_image and uploaded_image.filename:
            if product.get("has_image") and not request.form.get("confirm_replace_image"):
                flash("This product already has an image. Check the replacement box to confirm replacing it.", "warning")
                return render_template("product_image_form.html", product=product, replace_warning=True)

            try:
                saved_name = save_image_upload(uploaded_image, product["Product ID"])
                payload["has_image"] = True
                db.update_product(product_id, payload)
                flash(f"Image updated for {product['Product ID']} as {saved_name}.", "success")
                return redirect(url_for("admin_product_images", product_id=product_id))
            except Exception as exc:
                flash(f"Image upload failed: {exc}", "danger")
                return render_template("product_image_form.html", product=product)

        flash("No image file was selected.", "warning")

    return render_template("product_image_form.html", product=product)


@app.route("/admin/products/<int:product_id>/delete", methods=["POST"])
def admin_product_delete(product_id):

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    product = db.get_product(product_id)

    db.delete_product(product_id)

    name = product["Product Name"] if product else "Product"

    flash(f"Deleted \"{name}\".", "success")

    return redirect(url_for("admin_products"))


@app.route("/admin/reps")
def admin_reps():

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    reps = db.get_all_reps()

    return render_template("admin_reps.html", reps=reps)


@app.route("/admin/reps/add", methods=["GET", "POST"])
def admin_rep_add():

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

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

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

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

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    db.delete_rep(rep_id)

    flash("Sales rep deleted.", "success")

    return redirect(url_for("admin_reps"))


@app.route("/admin/orders")
def admin_orders():

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

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

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    order, items = db.get_order(order_id)

    if order is None:
        flash("Order not found.", "danger")
        return redirect(url_for("admin_orders"))

    return render_template("admin_order_detail.html", order=order, items=items)


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
def admin_order_status(order_id):

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    new_status = request.form.get("status", "New").strip() or "New"

    db.update_order_status(order_id, new_status)

    flash(f"Order #{order_id} marked as {new_status}.", "success")

    return redirect(url_for("admin_order_detail", order_id=order_id))


@app.route("/admin/orders/<int:order_id>/delete", methods=["POST"])
def admin_order_delete(order_id):

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

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

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

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

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

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


@app.route("/admin/catalog-audit", methods=["GET", "POST"])
def admin_catalog_audit():
    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

    db_duplicates = db.get_duplicate_product_ids_in_db()
    local_issues = []
    for norm, info in sorted(db_duplicates.items()):
        values = sorted(dict.fromkeys(str(v) for v in info["values"]))
        local_issues.append(
            {
                "id": norm,
                "count": info["count"],
                "values": values,
                "message": f"{norm} appears {info['count']} times in the local DB; keep one row and remove the rest.",
            }
        )

    audit_issues = []
    cleaned_df = pd.DataFrame()

    if request.method == "POST" and "audit_excel" in request.files:
        file = request.files["audit_excel"]
        if file and file.filename:
            fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
            os.close(fd)
            try:
                file.save(temp_path)
                df = pd.read_excel(temp_path, sheet_name="Product Catalog")
                if "Product ID" not in df.columns:
                    audit_issues.append({"id": "file", "message": "The sheet is missing the Product ID column."})
                else:
                    cleaned_df = db.build_cleaned_catalog_frame(df)
                    duplicates = db.detect_duplicate_product_ids(df.to_dict(orient="records"))
                    for norm, info in sorted(duplicates.items()):
                        audit_issues.append(
                            {
                                "id": norm,
                                "count": info["count"],
                                "values": sorted(dict.fromkeys(str(v) for v in info["values"])),
                                "message": f"{norm} appears {info['count']} times in the Excel file. Keep one row only.",
                            }
                        )
                    blank_rows = []
                    for idx, raw in enumerate(df.get("Product ID", []), start=2):
                        if raw is None or pd.isna(raw) or str(raw).strip() == "":
                            blank_rows.append(idx)
                    if blank_rows:
                        audit_issues.append(
                            {
                                "id": "blank",
                                "count": len(blank_rows),
                                "values": [str(v) for v in blank_rows[:10]],
                                "message": "Blank Product ID rows found in Excel. Fill or remove these rows before upload.",
                            }
                        )

                action = request.form.get("action")
                if action == "download_cleaned" and "Product ID" in df.columns and not df.empty:
                    buffer = BytesIO()
                    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                        cleaned_df.to_excel(writer, sheet_name="Product Catalog", index=False)
                    buffer.seek(0)
                    filename = f"cleaned_catalog_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.xlsx"
                    return send_file(
                        buffer,
                        as_attachment=True,
                        download_name=filename,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    return render_template(
        "admin_catalog_audit.html",
        duplicates=local_issues,
        audit_issues=audit_issues,
        cleaned_preview=cleaned_df.head(10) if not cleaned_df.empty else pd.DataFrame(),
        has_clean_preview=not cleaned_df.empty,
    )


# ------------------------
# Upload Excel (bulk import into the database)
# ------------------------

@app.route("/upload", methods=["POST"])
def upload():

    redirect_response = require_admin()
    if redirect_response is not None:
        return redirect_response

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

        before_id = db.get_latest_activity_id()
        row_count, reused_id_warnings = db.import_excel_into_db(EXCEL_FILE, replace=True)
        notify_activity_since(before_id)

        flash(
            f"Catalog uploaded successfully ({row_count} products).",
            "success"
        )

        # Flag (don't block) any Product ID in this upload that previously
        # belonged to a different, now-deleted product - that product's
        # old notifications/price history will now display as this new
        # product's, which is usually a typo rather than intentional.
        for warning in reused_id_warnings:
            flash(warning, "warning")

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


@app.template_filter("timeago")
def timeago(value):
    """Format a SQLite CURRENT_TIMESTAMP string ('2026-07-23 09:12:01')
    as a human-readable relative time like '2h ago', 'yesterday', '3d ago'.
    Empty/None input returns an empty string so templates can use it
    conditionally."""

    if not value:
        return ""

    try:
        changed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""

    now = datetime.now()
    diff = now - changed
    seconds = int(diff.total_seconds())

    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"

    days = hours // 24
    if days < 2:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks}w ago"
    months = days // 30
    return f"{months}mo ago"


# ------------------------
# Run
# ------------------------

if __name__ == "__main__":

    app.run(
        debug=True
    )
