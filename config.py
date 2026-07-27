import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

EXCEL_FILE = os.path.join(UPLOAD_FOLDER, "sale_price_catalog.xlsx")

# SQLite database that now backs the catalog. Products live here going
# forward; the Excel file above is kept only as the "bulk import" source
# (uploaded from the admin panel) and as a backup copy of the last import.
DATABASE_FILE = os.path.join(BASE_DIR, "gtm_catalog.db")

SECRET_KEY = "CHANGE_THIS_TO_A_RANDOM_SECRET_KEY"

ADMIN_USERNAME = "admin"
# password = admin123
ADMIN_PASSWORD_HASH = "scrypt:32768:8:1$xw3HOVyQHCqtEHVj$423b78ba463e04f4bb2f710f0d7224c9970dd9677188b90aa928b4180749e2a3f76c722e1184b046e247ba26df10ffbe82502865a7db7e8d959ca0b6690bd75e"

REQUIRED_COLUMNS = [
    "Product ID",
    "Product Name",
    "UPC",
    "Unit",
    "Retail",
    "Wholesale",
    "Category",
    "Status"
]
