import uuid
from datetime import datetime, timezone

import db
import pytest
from app import app
from werkzeug.security import generate_password_hash


@pytest.fixture
def mobile_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_FILE", str(tmp_path / "mobile-test.db"))
    monkeypatch.setattr(db, "EXCEL_FILE", str(tmp_path / "missing-catalog.xlsx"))
    db.init_db()
    db.add_product({
        "product_id": "GTM - 9001",
        "product_name": "Offline Test Product",
        "upc": 1,
        "unit": "Box",
        "retail": 100,
        "wholesale": 80,
        "category": "Test",
        "status": "In Stock",
        "description": "",
        "supplier": "Test Supplier",
        "has_image": False,
    })
    db.add_rep(
        "T-TEST",
        "Offline Rep",
        username="offline.rep",
        password_hash=generate_password_hash("test-password-123"),
    )
    yield app.test_client()


def _login(client):
    response = client.post(
        "/api/mobile/login",
        json={"username": "offline.rep", "password": "test-password-123"},
    )
    assert response.status_code == 200
    data = response.get_json()
    return data["access_token"]


def test_mobile_catalog_requires_auth_and_excludes_supplier(mobile_client):
    assert mobile_client.get("/api/mobile/catalog").status_code == 401

    invalid_login = mobile_client.post(
        "/api/mobile/login",
        json={"username": 123, "password": ["not", "a", "password"]},
    )
    assert invalid_login.status_code == 400

    token = _login(mobile_client)
    response = mobile_client.get(
        "/api/mobile/catalog",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["snapshot_id"]) == 64
    assert payload["products"][0]["product_name"] == "Offline Test Product"
    assert "supplier" not in payload["products"][0]


def test_mobile_order_sync_preserves_snapshot_price_and_is_idempotent(mobile_client):
    token = _login(mobile_client)
    auth = {"Authorization": f"Bearer {token}"}
    catalog = mobile_client.get("/api/mobile/catalog", headers=auth).get_json()
    product = catalog["products"][0]

    db.update_product(product["id"], {
        "product_id": product["product_id"],
        "product_name": product["product_name"],
        "upc": product["upc"],
        "unit": product["unit"],
        "retail": 125,
        "wholesale": 95,
        "category": product["category"],
        "status": product["status"],
        "description": product["description"],
        "supplier": "Test Supplier",
        "has_image": False,
    })

    client_order_id = str(uuid.uuid4())
    order = {
        "client_order_id": client_order_id,
        "catalog_snapshot_id": catalog["snapshot_id"],
        "outlet_name": "Offline Outlet",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "items": [{"product_pk": product["id"], "quantity": 2}],
    }

    first = mobile_client.post(
        "/api/mobile/sync",
        headers=auth,
        json={"orders": [order]},
    )
    assert first.status_code == 200
    result = first.get_json()["results"][0]
    assert result["ok"] is True
    assert result["price_mismatch"] is True
    assert result["total_retail"] == 200

    retry = mobile_client.post(
        "/api/mobile/sync",
        headers=auth,
        json={"orders": [order]},
    )
    retry_result = retry.get_json()["results"][0]
    assert retry_result["ok"] is True
    assert retry_result["already_synced"] is True
    assert retry_result["order_id"] == result["order_id"]

    conn = db.get_db_connection()
    try:
        saved = conn.execute(
            "SELECT total_retail, price_mismatch FROM orders WHERE id = ?",
            (result["order_id"],),
        ).fetchone()
        line = conn.execute(
            "SELECT unit_retail, quantity FROM order_items WHERE order_id = ?",
            (result["order_id"],),
        ).fetchone()
        order_count = conn.execute("SELECT COUNT(*) AS count FROM orders").fetchone()["count"]
    finally:
        conn.close()
    assert saved["total_retail"] == 200
    assert saved["price_mismatch"] == 1
    assert line["unit_retail"] == 100
    assert line["quantity"] == 2
    assert order_count == 1


def test_disabling_rep_revokes_mobile_token(mobile_client):
    token = _login(mobile_client)
    rep = db.get_mobile_rep_by_username("offline.rep")
    db.update_rep(rep["id"], "T-TEST", "Offline Rep", False)

    response = mobile_client.get(
        "/api/mobile/catalog",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_changing_mobile_password_revokes_existing_token(mobile_client):
    token = _login(mobile_client)
    rep = db.get_mobile_rep_by_username("offline.rep")
    db.update_rep(
        rep["id"],
        "T-TEST",
        "Offline Rep",
        True,
        username="offline.rep",
        password_hash=generate_password_hash("new-password-123"),
    )

    revoked = mobile_client.get(
        "/api/mobile/catalog",
        headers={"Authorization": f"******"},
    )
    assert revoked.status_code == 401

    replacement_login = mobile_client.post(
        "/api/mobile/login",
        json={"username": "offline.rep", "password": "new-password-123"},
    )
    assert replacement_login.status_code == 200
