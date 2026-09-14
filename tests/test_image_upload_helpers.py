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
