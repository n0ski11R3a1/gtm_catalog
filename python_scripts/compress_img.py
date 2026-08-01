import os
from pathlib import Path
from PIL import Image

# Path to your GTM folder
FOLDER_PATH = r"C:\Users\trust\Desktop\gtm_products_image"

# Output folder for optimized web images
OUTPUT_FOLDER = Path(FOLDER_PATH) / "compressed_web"

# Maximum pixel size (width or height) for product catalog display
MAX_DIMENSION = 1200 

# Compression Quality (1-100). 80-85 is the sweet spot for web catalogs
QUALITY = 82 

# Convert everything to WebP for modern web performance? 
# Set to True for .webp format, or False to keep original formats (.jpg, .png)
CONVERT_TO_WEBP = True 

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}

def compress_images():
    source_path = Path(FOLDER_PATH)
    
    if not source_path.exists():
        print(f"Error: Folder '{FOLDER_PATH}' not found.")
        return

    # Create output directory
    OUTPUT_FOLDER.mkdir(exist_ok=True)

    images = [f for f in source_path.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS]
    
    if not images:
        print("No image files found.")
        return

    print(f"Found {len(images)} images. Processing for web...\n")

    for img_path in images:
        try:
            with Image.open(img_path) as img:
                # Convert color modes for saving (handling PNG alpha transparency if present)
                if img.mode in ("RGBA", "P"):
                    if not CONVERT_TO_WEBP:
                        img = img.convert("RGB") # JPG does not support alpha transparency
                else:
                    img = img.convert("RGB")

                # Step 1: Resize if larger than MAX_DIMENSION (preserving aspect ratio)
                img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)

                # Step 2: Determine output file name and format
                if CONVERT_TO_WEBP:
                    out_name = img_path.stem + ".webp"
                    out_path = OUTPUT_FOLDER / out_name
                    img.save(out_path, "WEBP", quality=QUALITY, optimize=True)
                else:
                    out_path = OUTPUT_FOLDER / img_path.name
                    fmt = "PNG" if img_path.suffix.lower() == ".png" else "JPEG"
                    img.save(out_path, fmt, quality=QUALITY, optimize=True)

                # Output file size check
                orig_size_kb = img_path.stat().st_size / 1024
                new_size_kb = out_path.stat().st_size / 1024
                savings = 100 - (new_size_kb / orig_size_kb * 100)

                print(f"Compressed {img_path.name} -> {out_path.name} | "
                      f"{orig_size_kb:.1f}KB -> {new_size_kb:.1f}KB ({savings:.1f}% reduction)")

        except Exception as e:
            print(f"Failed to process {img_path.name}: {e}")

    print(f"\nDone! Web-ready images are saved in:\n{OUTPUT_FOLDER}")

if __name__ == "__main__":
    compress_images()
