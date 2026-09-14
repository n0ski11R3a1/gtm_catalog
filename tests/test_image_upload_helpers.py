import io
import os
import zipfile
from io import BytesIO

from PIL import Image

from app import build_product_image_filename, prepare_uploaded_product_image, product_photo_slug


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
