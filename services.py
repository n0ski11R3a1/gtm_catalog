import os
from io import BytesIO

from PIL import Image

from config import (
    PRODUCT_IMAGES_DIR,
    THUMBNAILS_DIR,
    MAX_IMAGE_SIZE,
    ALLOWED_IMAGE_EXTENSIONS,
    ALLOWED_MIME_TYPES,
    WEBP_QUALITY,
    IMAGE_MAX_WIDTH,
    THUMBNAIL_WIDTH,
)


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


def validate_product_form(data):
    """Return a list of user-facing validation errors for product submissions."""
    errors = []

    if not data.get("product_name", "").strip():
        errors.append("Product Name is required.")

    if data.get("status", "In Stock") == "In Stock" and not data.get("supplier", "").strip():
        errors.append("Supplier is required for In Stock products.")

    return errors


def is_allowed_image_file(file_obj):
    """Validate both extension and MIME type."""
    if not file_obj or not getattr(file_obj, "filename", None):
        return False, "No file selected"

    ext = (file_obj.filename.rsplit(".", 1)[-1] if "." in file_obj.filename else "").lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return False, f"Invalid format. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}"

    if file_obj.mimetype not in ALLOWED_MIME_TYPES:
        return False, f"Invalid MIME type: {file_obj.mimetype or 'unknown'}"

    return True, None


def validate_image_size(file_obj):
    try:
        file_obj.seek(0, os.SEEK_END)
        size = file_obj.tell()
        if hasattr(file_obj, "seek"):
            try:
                file_obj.seek(0)
            except Exception:
                pass
    except (AttributeError, OSError):
        if hasattr(file_obj, "read"):
            pos = None
            try:
                pos = file_obj.tell()
            except Exception:
                pass
            data = file_obj.read()
            size = len(data)
            if pos is not None:
                try:
                    file_obj.seek(pos)
                except Exception:
                    pass
            if hasattr(file_obj, "seek"):
                try:
                    file_obj.seek(0)
                except Exception:
                    pass
        else:
            return False, "Unsupported file object"

    if size > MAX_IMAGE_SIZE:
        return False, f"File too large: {size / 1024 / 1024:.1f} MB (max {MAX_IMAGE_SIZE / 1024 / 1024:.0f} MB)"

    return True, None


def compress_to_webp(file_obj, max_width=None, quality=None):
    max_width = max_width or IMAGE_MAX_WIDTH
    quality = quality or WEBP_QUALITY

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


def prepare_uploaded_product_image(file_obj, product_id):
    """Return (compressed_bytesio, target_filename) for a product image."""
    if not file_obj or not getattr(file_obj, "filename", None):
        raise ValueError("No file selected")

    is_valid, msg = is_allowed_image_file(file_obj)
    if not is_valid:
        raise ValueError(msg)

    is_ok, msg = validate_image_size(file_obj)
    if not is_ok:
        raise ValueError(msg)

    target_filename = build_product_image_filename(product_id)
    if not target_filename:
        raise ValueError("Product ID is required to save an image")

    compressed = compress_to_webp(file_obj)
    return compressed, target_filename


def save_product_image(file_obj, product_id, image_dir=None):
    image_dir = image_dir or PRODUCT_IMAGES_DIR
    os.makedirs(image_dir, exist_ok=True)
    compressed, filename = prepare_uploaded_product_image(file_obj, product_id)
    target_path = os.path.join(image_dir, filename)

    with open(target_path, "wb") as fh:
        fh.write(compressed.getvalue())

    return filename


def delete_product_image_files(product_id, image_dir=None, thumbnails_dir=None):
    """Delete all generated image files for a product ID without affecting the DB row."""
    if not product_id:
        return False

    image_dir = image_dir or PRODUCT_IMAGES_DIR
    thumbnails_dir = thumbnails_dir or THUMBNAILS_DIR

    removed = False
    image_name = build_product_image_filename(product_id)
    if image_name:
        path = os.path.join(image_dir, image_name)
        if os.path.exists(path):
            os.remove(path)
            removed = True

    thumb_path = os.path.join(thumbnails_dir, f"{product_id}.webp")
    if os.path.exists(thumb_path):
        os.remove(thumb_path)
        removed = True

    return removed


def save_image_upload(file_obj, product_id, image_dir=None, thumbnails_dir=None):
    """Validate and persist a product image upload to the canonical product-image path."""
    if file_obj is None or not getattr(file_obj, "filename", None):
        raise ValueError("No file selected")

    valid, msg = is_allowed_image_file(file_obj)
    if not valid:
        raise ValueError(msg)

    valid, msg = validate_image_size(file_obj)
    if not valid:
        raise ValueError(msg)

    _, saved_name = prepare_uploaded_product_image(file_obj, product_id)
    save_product_image(file_obj, product_id, image_dir=image_dir or PRODUCT_IMAGES_DIR)
    return saved_name


def generate_thumbnail(product_id, image_dir=None, thumbnails_dir=None, thumbnail_width=None, quality=None):
    """Generate and cache a 400px-wide thumbnail for a product image."""
    product_id = (product_id or "").strip()
    if not product_id:
        return None

    image_dir = image_dir or PRODUCT_IMAGES_DIR
    thumbnails_dir = thumbnails_dir or THUMBNAILS_DIR
    thumbnail_width = thumbnail_width or THUMBNAIL_WIDTH
    quality = quality or WEBP_QUALITY

    full_name = None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = os.path.join(image_dir, f"{product_id}{ext}")
        if os.path.isfile(candidate):
            full_name = candidate
            break

    if not full_name:
        return None

    os.makedirs(thumbnails_dir, exist_ok=True)
    thumb_path = os.path.join(thumbnails_dir, f"{product_id}.webp")

    with Image.open(full_name) as img:
        width = thumbnail_width
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
        img.save(thumb_path, format="WEBP", quality=quality, method=6)

    return thumb_path
