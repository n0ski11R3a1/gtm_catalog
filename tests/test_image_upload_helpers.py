import io
import os
import zipfile
from io import BytesIO
from datetime import datetime, timedelta

import pandas as pd
import pytest
from PIL import Image

import db
from app import build_product_image_filename, prepare_uploaded_product_image, product_photo_slug, validate_excel, timeago


def test_send_to_all_skips_malformed_subscription(monkeypatch):
    import push

    calls = []

    def fake_get_all_push_subscriptions():
        return [
            {"endpoint": "https://example.com/bad", "p256dh": "", "auth": ""},
            {"endpoint": "https://example.com/good", "p256dh": "good-p256dh", "auth": "good-auth"},
        ]

    def fake_remove_push_subscription(endpoint):
        assert endpoint == "https://example.com/bad"

    def fake_webpush(*args, **kwargs):
        calls.append(kwargs["subscription_info"]["endpoint"])

    monkeypatch.setattr(push.db, "get_all_push_subscriptions", fake_get_all_push_subscriptions)
    monkeypatch.setattr(push.db, "remove_push_subscription", fake_remove_push_subscription)
    monkeypatch.setattr(push, "webpush", fake_webpush)

    push.send_to_all("Title", "Body")

    assert calls == ["https://example.com/good"]


def test_admin_images_page_uses_image_panel_controls():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True

    response = client.get("/admin/images")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Manage Images" in html
    assert "imageSearch" in html
    assert "image-action-btn" in html
    assert "image-thumb" in html


def test_admin_pages_include_quick_filters():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True

    products_page = client.get("/admin/products")
    images_page = client.get("/admin/images")

    assert products_page.status_code == 200
    assert images_page.status_code == 200
    assert "productStatusFilters" in products_page.get_data(as_text=True)
    assert "imageStatusFilters" in images_page.get_data(as_text=True)


def test_login_required_only_allows_admin_session():
    from app import app, login_required

    with app.test_request_context("/admin") as ctx:
        from flask import session

        session["admin"] = True
        assert login_required() is True

    with app.test_request_context("/"):
        assert login_required() is False


def test_config_reads_credentials_from_environment(monkeypatch):
    import importlib
    import config

    monkeypatch.setenv("GTM_SECRET_KEY", "env-secret")
    monkeypatch.setenv("GTM_ADMIN_USERNAME", "superadmin")
    monkeypatch.setenv("GTM_ADMIN_PASSWORD_HASH", "scrypt:testhash")
    monkeypatch.setenv("GTM_VAPID_PRIVATE_KEY", "env-vapid-private")
    monkeypatch.setenv("GTM_VAPID_PUBLIC_KEY", "env-vapid-public")
    monkeypatch.setenv("GTM_VAPID_CLAIM_EMAIL", "ops@example.com")

    reloaded = importlib.reload(config)

    assert reloaded.SECRET_KEY == "env-secret"
    assert reloaded.ADMIN_USERNAME == "superadmin"
    assert reloaded.ADMIN_PASSWORD_HASH == "scrypt:testhash"
    assert reloaded.VAPID_PRIVATE_KEY == "env-vapid-private"
    assert reloaded.VAPID_PUBLIC_KEY == "env-vapid-public"
    assert reloaded.VAPID_CLAIM_EMAIL == "ops@example.com"


def test_product_photo_slug_removes_spaces_and_normalizes_case():
    assert product_photo_slug("GTM - 0001") == "GTM-0001"
    assert product_photo_slug("gtm - 0001") == "GTM-0001"


def test_build_product_image_filename_uses_webp_extension():
    assert build_product_image_filename("GTM - 0001") == "GTM-0001.webp"


def test_prepare_uploaded_product_image_ignores_original_client_filename():
    class FakeUpload:
        filename = "custom_name.png"
        mimetype = "image/png"

        def __init__(self):
            buf = BytesIO()
            Image.new("RGB", (10, 10), "red").save(buf, format="PNG")
            self._data = buf.getvalue()
            self._pos = 0

        def seek(self, pos, whence=0):
            if whence == 0:
                self._pos = pos
            elif whence == 1:
                self._pos += pos
            elif whence == 2:
                self._pos = len(self._data) + pos
            return self._pos

        def tell(self):
            return self._pos

        def read(self, size=-1):
            if size is None or size < 0:
                size = len(self._data) - self._pos
            chunk = self._data[self._pos:self._pos + size]
            self._pos += len(chunk)
            return chunk

    result = prepare_uploaded_product_image(FakeUpload(), "GTM - 0001")
    assert result[0].getvalue().startswith(b"RIFF")
    assert result[1] == "GTM-0001.webp"


def test_validate_excel_rejects_duplicate_normalized_product_ids(tmp_path):
    path = tmp_path / "duplicate_ids.xlsx"
    pd.DataFrame([
        {"Product ID": "GTM - 0001", "Product Name": "One", "UPC": 1, "Unit": "Box", "Retail": 100, "Wholesale": 80, "Category": "General", "Status": "In Stock", "Supplier": "A", "Base Price": 90, "B2C": 110},
        {"Product ID": "GTM-0001", "Product Name": "Two", "UPC": 2, "Unit": "Box", "Retail": 110, "Wholesale": 90, "Category": "General", "Status": "In Stock", "Supplier": "A", "Base Price": 90, "B2C": 120},
        {"Product ID": "GTM - 0002", "Product Name": "Three", "UPC": 3, "Unit": "Box", "Retail": 120, "Wholesale": 100, "Category": "General", "Status": "In Stock", "Supplier": "A", "Base Price": 95, "B2C": 130},
    ]).to_excel(path, index=False, sheet_name="Product Catalog")

    valid, error = validate_excel(str(path))

    assert valid is False
    assert "Duplicate Product IDs" in error
    assert "GTM-0001" in error


def test_validate_excel_rejects_blank_product_ids(tmp_path):
    path = tmp_path / "blank_ids.xlsx"
    pd.DataFrame([
        {"Product ID": "GTM - 0001", "Product Name": "One", "UPC": 1, "Unit": "Box", "Retail": 100, "Wholesale": 80, "Category": "General", "Status": "In Stock", "Supplier": "A", "Base Price": 90, "B2C": 110},
        {"Product ID": "", "Product Name": "Bad blank", "UPC": 0, "Unit": "-", "Retail": 0, "Wholesale": 0, "Category": "General", "Status": "In Stock", "Supplier": "A", "Base Price": 10, "B2C": 12},
    ]).to_excel(path, index=False, sheet_name="Product Catalog")

    valid, error = validate_excel(str(path))

    assert valid is False
    assert "Blank Product IDs" in error


def test_detect_duplicate_product_ids_ignores_spaces_and_case():
    rows = [
        {"Product ID": "GTM - 0001", "Product Name": "One"},
        {"Product ID": "GTM-0001", "Product Name": "Two"},
        {"Product ID": "GTM - 0002", "Product Name": "Three"},
        {"Product ID": "", "Product Name": "Blank"},
        {"Product ID": None, "Product Name": "Also blank"},
    ]

    duplicates = db.detect_duplicate_product_ids(rows)

    assert "GTM-0001" in duplicates
    assert duplicates["GTM-0001"]["count"] == 2
    assert duplicates["GTM-0001"]["values"] == ["GTM - 0001", "GTM-0001"]


def test_build_cleaned_catalog_frame_removes_blank_and_duplicate_rows():
    df = pd.DataFrame([
        {"Product ID": "GTM - 0001", "Product Name": "One", "Retail": 100},
        {"Product ID": "GTM-0001", "Product Name": "Duplicate", "Retail": 110},
        {"Product ID": "", "Product Name": "Blank", "Retail": 0},
        {"Product ID": "GTM - 0002", "Product Name": "Two", "Retail": 200},
    ])

    cleaned = db.build_cleaned_catalog_frame(df)

    assert len(cleaned) == 2
    assert [row["Product ID"] for _, row in cleaned.iterrows()] == ["GTM - 0001", "GTM - 0002"]


def test_generate_thumbnail_creates_cached_webp(tmp_path):
    from app import app, generate_thumbnail

    original_images_dir = app.config.get("PRODUCT_IMAGES_DIR")
    original_thumbs_dir = app.config.get("THUMBNAILS_DIR")

    try:
        app.config["PRODUCT_IMAGES_DIR"] = str(tmp_path / "images")
        app.config["THUMBNAILS_DIR"] = str(tmp_path / "thumbs")
        os.makedirs(app.config["PRODUCT_IMAGES_DIR"], exist_ok=True)
        os.makedirs(app.config["THUMBNAILS_DIR"], exist_ok=True)

        source_path = os.path.join(app.config["PRODUCT_IMAGES_DIR"], "GTM-0001.webp")
        Image.new("RGB", (1600, 900), "blue").save(source_path, format="WEBP")

        thumb_path = generate_thumbnail("GTM-0001")
        assert thumb_path is not None
        assert os.path.exists(thumb_path)
        assert os.path.getsize(thumb_path) > 0
    finally:
        app.config["PRODUCT_IMAGES_DIR"] = original_images_dir
        app.config["THUMBNAILS_DIR"] = original_thumbs_dir


def test_bulk_zip_upload_processes_images_for_matching_products():
    import db
    from app import app

    product_id = db.get_next_product_id().replace(" ", "")
    db.add_product({
        "product_id": product_id,
        "product_name": "Bulk Upload Test",
        "upc": 0,
        "unit": "-",
        "retail": 100,
        "wholesale": 80,
        "category": "General",
        "status": "In Stock",
        "description": "",
        "supplier": "Test Supplier",
        "has_image": False,
    })

    try:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            image_bytes = io.BytesIO()
            Image.new("RGB", (40, 40), "green").save(image_bytes, format="PNG")
            zf.writestr(f"{product_id}.png", image_bytes.getvalue())

        client = app.test_client()
        with client.session_transaction() as session:
            session["admin"] = True

        zip_buffer.seek(0)
        response = client.post(
            "/admin/upload-images",
            data={"zip_file": (zip_buffer, "images.zip")},
            content_type="multipart/form-data",
        )

        assert response.status_code == 200
        assert b"saved" in response.data.lower()
        assert os.path.exists(os.path.join(app.config["PRODUCT_IMAGES_DIR"], f"{product_id}.webp"))
    finally:
        product = db.get_product_by_business_id(product_id)
        if product:
            db.delete_product(product["id"])
        for path in (
            os.path.join(app.config["PRODUCT_IMAGES_DIR"], f"{product_id}.webp"),
            os.path.join(app.config["THUMBNAILS_DIR"], f"{product_id}.webp"),
        ):
            if os.path.exists(path):
                os.remove(path)


def test_get_all_products_includes_last_price_change():
    products = db.get_all_products()
    assert len(products) > 0
    assert "last_price_change" in products[0]
    assert "last_price_direction" in products[0]
    changed = [p for p in products if p["last_price_change"] is not None]
    assert len(changed) > 0


def test_get_product_includes_last_price_change():
    products = db.get_all_products()
    changed = [p for p in products if p["last_price_change"] is not None]
    if changed:
        pid = changed[0]["Product ID"]
        fetched = db.get_product_by_business_id(pid.replace(" ", ""))
        assert fetched is not None
        assert "last_price_change" in fetched
        assert fetched["last_price_change"] == changed[0]["last_price_change"]


def test_timeago_formats_relative_times():
    now = datetime.now()

    recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert "m ago" in timeago(recent)

    today = now.strftime("%Y-%m-%d %H:%M:%S")
    assert timeago(today) == "just now"

    older = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    assert "d ago" in timeago(older)

    assert timeago("") == ""
    assert timeago(None) == ""
    assert timeago("garbage") == ""


def test_catalog_page_renders_price_change_markers():
    from app import app
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "price-change-badge" in html
    assert "data-timestamp" in html
    assert "data-direction" in html
