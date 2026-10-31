import uuid
from datetime import datetime, timezone

import db
import pytest
from app import app
from werkzeug.security import generate_password_hash


@pytest.fixture
def outlet_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_FILE", str(tmp_path / "outlet-test.db"))
    monkeypatch.setattr(db, "EXCEL_FILE", str(tmp_path / "missing-catalog.xlsx"))
    db.init_db()
    db.add_product({
        "product_id": "GTM - 9101",
        "product_name": "Outlet Test Product",
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
        "T-OUTLET",
        "Outlet Rep",
        username="outlet.rep",
        password_hash=generate_password_hash("test-password-123"),
    )
    return app.test_client()


def _mobile_context(client):
    response = client.post(
        "/api/mobile/login",
        json={"username": "outlet.rep", "password": "test-password-123"},
    )
    assert response.status_code == 200
    token = response.get_json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    catalog = client.get("/api/mobile/catalog", headers=headers).get_json()
    return headers, catalog


def _offline_order(catalog, outlet_name, *, outlet_id=None, visit_location=None):
    order = {
        "client_order_id": str(uuid.uuid4()),
        "catalog_snapshot_id": catalog["snapshot_id"],
        "outlet_name": outlet_name,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "items": [{"product_pk": catalog["products"][0]["id"], "quantity": 2}],
    }
    if outlet_id is not None:
        order["outlet_id"] = outlet_id
    if visit_location is not None:
        order["visit_location"] = visit_location
    return order


def _offline_visit(outlet_name, *, client_visit_id=None, outlet_id=None, visit_location=None):
    visit = {
        "client_visit_id": client_visit_id or str(uuid.uuid4()),
        "outlet_name": outlet_name,
        "visit_type": "follow_up",
        "note": "Discussed next order",
        "visited_at": datetime.now(timezone.utc).isoformat(),
    }
    if outlet_id is not None:
        visit["outlet_id"] = outlet_id
    if visit_location is not None:
        visit["visit_location"] = visit_location
    return visit


def test_offline_order_creates_outlet_visit_and_resolves_alias(outlet_client):
    outlet_id = db.create_outlet("Golden Shop", area="Township A")
    db.add_outlet_alias(outlet_id, "Golden Store")
    headers, catalog = _mobile_context(outlet_client)

    response = outlet_client.post(
        "/api/mobile/sync",
        headers=headers,
        json={"orders": [_offline_order(catalog, "Golden Store")]},
    )
    result = response.get_json()["results"][0]
    assert result["ok"] is True

    order, _items = db.get_order(result["order_id"])
    outlet = db.get_outlet(outlet_id)
    assert order["outlet_id"] == outlet_id
    assert order["outlet_name"] == "Golden Shop"
    assert [alias["alias"] for alias in outlet["aliases"]] == ["Golden Store"]
    assert len(outlet["visits"]) == 1
    assert outlet["visits"][0]["order_id"] == result["order_id"]


def test_standalone_offline_visit_resolves_alias_and_is_retry_safe(outlet_client):
    outlet_id = db.create_outlet("Follow-up Shop")
    db.add_outlet_alias(outlet_id, "Follow Up Store")
    headers, _catalog = _mobile_context(outlet_client)
    visit = _offline_visit(
        "Follow Up Store",
        visit_location={"latitude": 16.8, "longitude": 96.2, "accuracy": 12},
    )

    first = outlet_client.post(
        "/api/mobile/visits/sync", headers=headers, json={"visits": [visit]}
    ).get_json()["results"][0]
    retry = outlet_client.post(
        "/api/mobile/visits/sync", headers=headers, json={"visits": [visit]}
    ).get_json()["results"][0]
    outlet = db.get_outlet(outlet_id)

    assert first["ok"] is True
    assert retry["ok"] is True
    assert retry["already_synced"] is True
    assert first["visit_id"] == retry["visit_id"]
    assert len(outlet["visits"]) == 1
    assert outlet["visits"][0]["visit_type"] == "follow_up"
    assert outlet["visits"][0]["note"] == "Discussed next order"
    assert outlet["visits"][0]["latitude"] == 16.8
    assert outlet["orders"] == []


def test_standalone_visit_rejects_ambiguous_outlet_and_invalid_gps(outlet_client):
    db.create_outlet("Shared Visit Shop", area="North")
    db.create_outlet("Shared Visit Shop", area="South")
    headers, _catalog = _mobile_context(outlet_client)
    ambiguous = _offline_visit("Shared Visit Shop")
    invalid_gps = _offline_visit(
        "New Visit Shop",
        visit_location={"latitude": 0, "longitude": 181, "accuracy": 2},
    )

    response = outlet_client.post(
        "/api/mobile/visits/sync",
        headers=headers,
        json={"visits": [ambiguous, invalid_gps]},
    )
    results = response.get_json()["results"]

    assert all(result["ok"] is False for result in results)
    assert "multiple outlets" in results[0]["error"]
    assert "coordinate ranges" in results[1]["error"]
    outlets = db.get_all_outlets()
    assert len(outlets) == 2
    assert all(not db.get_outlet(outlet["id"])["visits"] for outlet in outlets)


def test_standalone_visit_sync_requires_rep_authentication(outlet_client):
    response = outlet_client.post(
        "/api/mobile/visits/sync",
        json={"visits": [_offline_visit("Unauthenticated Shop")]},
    )

    assert response.status_code == 401
    assert db.get_all_outlets() == []


def test_offline_order_can_select_between_same_named_outlets(outlet_client):
    first_id = db.create_outlet("Corner Shop", area="North")
    second_id = db.create_outlet("Corner Shop", area="South")
    headers, catalog = _mobile_context(outlet_client)
    order = _offline_order(catalog, "Corner Shop", outlet_id=second_id)

    response = outlet_client.post(
        "/api/mobile/sync",
        headers=headers,
        json={"orders": [order]},
    )
    result = response.get_json()["results"][0]

    assert result["ok"] is True
    saved, _items = db.get_order(result["order_id"])
    assert saved["outlet_id"] == second_id
    assert db.get_outlet(first_id)["orders"] == []


def test_ambiguous_unselected_outlet_is_not_guessed(outlet_client):
    first_id = db.create_outlet("Market Shop", area="North")
    second_id = db.create_outlet("Market Shop", area="South")
    headers, catalog = _mobile_context(outlet_client)

    response = outlet_client.post(
        "/api/mobile/sync",
        headers=headers,
        json={"orders": [_offline_order(catalog, "Market Shop")]},
    )
    result = response.get_json()["results"][0]
    saved, _items = db.get_order(result["order_id"])

    assert result["ok"] is True
    assert saved["outlet_id"] is None
    assert db.get_outlet(first_id)["orders"] == []
    assert db.get_outlet(second_id)["orders"] == []


def test_offline_order_saves_opt_in_gps_to_visit_and_outlet(outlet_client):
    outlet_id = db.create_outlet("GPS Shop")
    headers, catalog = _mobile_context(outlet_client)
    location = {"latitude": 16.8661, "longitude": 96.1951, "accuracy": 8.5}

    response = outlet_client.post(
        "/api/mobile/sync",
        headers=headers,
        json={"orders": [_offline_order(
            catalog, "GPS Shop", outlet_id=outlet_id, visit_location=location
        )]},
    )
    result = response.get_json()["results"][0]
    outlet = db.get_outlet(outlet_id)

    assert result["ok"] is True
    assert outlet["lat"] == location["latitude"]
    assert outlet["lng"] == location["longitude"]
    assert outlet["visits"][0]["latitude"] == location["latitude"]
    assert outlet["visits"][0]["longitude"] == location["longitude"]
    assert outlet["visits"][0]["accuracy"] == location["accuracy"]


def test_mobile_sync_rejects_out_of_range_gps_without_creating_order(outlet_client):
    headers, catalog = _mobile_context(outlet_client)
    order = _offline_order(
        catalog,
        "Invalid GPS Shop",
        visit_location={"latitude": 91, "longitude": 0, "accuracy": 1},
    )

    response = outlet_client.post(
        "/api/mobile/sync", headers=headers, json={"orders": [order]}
    )

    result = response.get_json()["results"][0]
    assert result["ok"] is False
    assert "coordinate ranges" in result["error"]
    assert db.get_all_orders() == []
    assert db.get_all_outlets() == []


def test_outlet_admin_requires_login_and_renders_directory(outlet_client):
    assert outlet_client.get("/admin/outlets").status_code == 302
    with outlet_client.session_transaction() as session:
        session["admin"] = True

    response = outlet_client.post(
        "/admin/outlets",
        data={"name": "Admin Shop", "area": "Downtown", "shop_type": "retail"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Admin Shop" in response.data
    outlet = db.get_all_outlets()[0]
    detail = outlet_client.get(f"/admin/outlets/{outlet['id']}")
    assert detail.status_code == 200
    assert b"Visit history" in detail.data
