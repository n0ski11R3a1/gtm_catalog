from app import build_product_image_filename, product_photo_slug


def test_product_photo_slug_removes_spaces_and_normalizes_case():
    assert product_photo_slug("GTM - 0001") == "GTM-0001"
    assert product_photo_slug("gtm - 0001") == "GTM-0001"


def test_build_product_image_filename_uses_webp_extension():
    assert build_product_image_filename("GTM - 0001") == "GTM-0001.webp"
