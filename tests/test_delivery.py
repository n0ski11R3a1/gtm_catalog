import uuid

import db
import pytest
from app import app
from werkzeug.security import generate_password_hash


@pytest.fixture
def delivery_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_FILE", str(tmp_path / "delivery-test.db"))
    monkeypatch.setattr(db, "EXCEL_FILE", str(tmp_path / "missing-catalog.xlsx"))
    db.init_db()
    product_pk = db.add_product({
        "product_id": "GTM - 9201",
        "product_name": "Delivery Test Product",
        "upc": 1,
        "unit": "Box",
        "retail": 100,
        "wholesale": 70,
        "category": "Test",
        "status": "In Stock",
        "description": "",
        "supplier": "Test Supplier",
        "has_image": False,
    })
    db.add_rep(
        "T-DELIVERY",
        "Delivery Rep",
        username="delivery.rep",
        password_hash=generate_password_hash("test-password-123"),
    )
    yield app.test_client(), product_pk


def _login(client, username="delivery.rep"):
    response = client.post(
        "/api/mobile/login",
        json={"username": username, "password": "test-password-123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.get_json()['access_token']}"}


def _create_order_and_assign(product_pk):
    rep = db.get_mobile_rep_by_username("delivery.rep")
    order_id, _retail, _wholesale = db.create_order(
        "T-DELIVERY (Delivery Rep)",
        [{"product_pk": product_pk, "quantity": 2}],
        outlet_name="Delivery Outlet",
    )
    db.assign_delivery_order(order_id, rep["id"], actor="test-admin")
    return rep["id"], order_id


def test_admin_delivery_assignment_requires_mobile_rep_and_admin(delivery_client):
    client, product_pk = delivery_client
    _rep_id, order_id = _create_order_and_assign(product_pk)

    assert client.get("/admin/deliveries").status_code == 302
    with client.session_transaction() as session:
        session["admin"] = True
    page = client.get("/admin/deliveries")
    assert page.status_code == 200
    assert b"Delivery Outlet" in page.data
    assert b"Delivery Rep" in page.data
    assert client.get(f"/admin/orders/{order_id}").status_code == 200

    db.add_rep("T-NOMOBILE", "No Mobile Rep")
    conn = db.get_db_connection()
    try:
        unconfigured = conn.execute(
            "SELECT id FROM sales_reps WHERE code = 'T-NOMOBILE'"
        ).fetchone()["id"]
    finally:
        conn.close()
    with pytest.raises(ValueError, match="mobile sign-in"):
        db.assign_delivery_order(order_id, unconfigured, actor="test-admin")


def test_mobile_delivery_is_private_and_completion_posts_stock_once(delivery_client):
    client, product_pk = delivery_client
    rep_id, order_id = _create_order_and_assign(product_pk)
    auth = _login(client)
    db.record_stock_movement(product_pk, 5, "receipt", reference="Opening stock")

    response = client.get("/api/mobile/deliveries", headers=auth)
    assert response.status_code == 200
    assigned = response.get_json()["orders"]
    assert len(assigned) == 1
    assert assigned[0]["id"] == order_id
    assert "unit_wholesale" not in str(assigned)
    assert "supplier" not in str(assigned)

    attempt = {
        "client_attempt_id": str(uuid.uuid4()),
        "order_id": order_id,
        "result": "delivered",
        "cash_collected": 150,
        "note": "Received by shopkeeper",
    }
    first = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [attempt]},
    ).get_json()["results"][0]
    retry = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [attempt]},
    ).get_json()["results"][0]

    assert first["ok"] is True
    assert retry["ok"] is True
    assert retry["already_synced"] is True
    order, _items = db.get_order(order_id)
    assert order["status"] == "Fulfilled"
    assert order["delivery_rep_label"] == "T-DELIVERY (Delivery Rep)"
    assert order["latest_delivery"]["cash_collected"] == 150
    inventory = {row["id"]: row for row in db.get_inventory_products()}
    assert inventory[product_pk]["quantity"] == 3
    movements = db.get_stock_movements(product_pk)
    assert len(movements) == 2
    assert movements[0]["movement_type"] == "sale_delivery"
    assert movements[0]["delta"] == -2

    another_attempt = {**attempt, "client_attempt_id": str(uuid.uuid4())}
    rejected = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [another_attempt]},
    ).get_json()["results"][0]
    assert rejected["ok"] is False
    assert "no longer assigned" in rejected["error"]


def test_delivery_stock_failure_rolls_back_order_and_attempt(delivery_client):
    client, product_pk = delivery_client
    _rep_id, order_id = _create_order_and_assign(product_pk)
    auth = _login(client)
    attempt = {
        "client_attempt_id": str(uuid.uuid4()),
        "order_id": order_id,
        "result": "delivered",
        "cash_collected": 0,
    }

    response = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [attempt]},
    )
    result = response.get_json()["results"][0]

    assert result["ok"] is False
    assert "Insufficient stock" in result["error"]
    order, _items = db.get_order(order_id)
    assert order["status"] == "New"
    assert db.get_stock_movements(product_pk) == []
    conn = db.get_db_connection()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM delivery_attempts"
        ).fetchone()["count"] == 0
    finally:
        conn.close()


def test_partial_delivery_keeps_only_remaining_quantities_assigned(delivery_client):
    client, product_pk = delivery_client
    _rep_id, order_id = _create_order_and_assign(product_pk)
    auth = _login(client)
    db.record_stock_movement(product_pk, 5, "receipt", reference="Opening stock")
    assignment = client.get("/api/mobile/deliveries", headers=auth).get_json()["orders"][0]
    item_id = assignment["items"][0]["id"]

    invalid = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [{
            "client_attempt_id": str(uuid.uuid4()),
            "order_id": order_id,
            "result": "delivered",
            "delivered_items": [{"order_item_id": item_id, "quantity": 3}],
            "cash_collected": 100,
        }]},
    ).get_json()["results"][0]
    assert invalid["ok"] is False
    assert db.get_inventory_products()[0]["quantity"] == 5

    partial_attempt = {
        "client_attempt_id": str(uuid.uuid4()),
        "order_id": order_id,
        "result": "delivered",
        "delivered_items": [{"order_item_id": item_id, "quantity": 1}],
        "cash_collected": 100,
    }
    response = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [partial_attempt]},
    ).get_json()["results"][0]
    assert response["ok"] is True
    partial_order, _items = db.get_order(order_id)
    assert partial_order["status"] == "New"
    assert partial_order["delivery_partial"] is True
    with pytest.raises(ValueError, match="undelivered quantities"):
        db.update_order_status(order_id, "Fulfilled")
    saved_assignment = client.get("/api/mobile/deliveries", headers=auth).get_json()["orders"][0]
    assert saved_assignment["items"][0]["quantity"] == 1
    assert saved_assignment["cash_collectable"] == 100
    assert db.get_inventory_products()[0]["quantity"] == 4

    over_delivery = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [{
            "client_attempt_id": str(uuid.uuid4()),
            "order_id": order_id,
            "result": "delivered",
            "delivered_items": [{"order_item_id": item_id, "quantity": 2}],
            "cash_collected": 100,
        }]},
    ).get_json()["results"][0]
    assert over_delivery["ok"] is False
    assert db.get_inventory_products()[0]["quantity"] == 4

    retry = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [partial_attempt]},
    ).get_json()["results"][0]
    assert retry["ok"] is True and retry["already_synced"] is True
    assert db.get_inventory_products()[0]["quantity"] == 4

    final_attempt = {
        "client_attempt_id": str(uuid.uuid4()),
        "order_id": order_id,
        "result": "delivered",
        "delivered_items": [{"order_item_id": item_id, "quantity": 1}],
        "cash_collected": 100,
    }
    completed = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [final_attempt]},
    ).get_json()["results"][0]
    assert completed["ok"] is True
    order, _items = db.get_order(order_id)
    assert order["status"] == "Fulfilled"
    assert order["delivery_partial"] is False
    assert db.get_inventory_products()[0]["quantity"] == 3
    assert len(db.get_stock_movements(product_pk)) == 3


def test_delivery_endpoint_rejects_unassigned_rep_bad_result_and_overcollection(delivery_client):
    client, product_pk = delivery_client
    _rep_id, order_id = _create_order_and_assign(product_pk)
    db.add_rep(
        "T-OTHER",
        "Other Rep",
        username="other.rep",
        password_hash=generate_password_hash("test-password-123"),
    )
    auth = _login(client, "other.rep")
    assert client.get("/api/mobile/deliveries", headers=auth).get_json()["orders"] == []

    responses = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [
            {
                "client_attempt_id": str(uuid.uuid4()),
                "order_id": order_id,
                "result": "delivered",
                "cash_collected": 0,
            },
            {
                "client_attempt_id": str(uuid.uuid4()),
                "order_id": order_id,
                "result": [],
                "cash_collected": 0,
            },
        ]},
    ).get_json()["results"]
    assert all(result["ok"] is False for result in responses)

    assigned_auth = _login(client)
    db.record_stock_movement(product_pk, 2, "receipt", reference="Opening stock")
    overcollect = client.post(
        "/api/mobile/deliveries/sync",
        headers=assigned_auth,
        json={"attempts": [{
            "client_attempt_id": str(uuid.uuid4()),
            "order_id": order_id,
            "result": "delivered",
            "cash_collected": 201,
        }]},
    ).get_json()["results"][0]
    assert overcollect["ok"] is False
    assert "cannot exceed" in overcollect["error"]
    order, _items = db.get_order(order_id)
    assert order["status"] == "New"
    assert db.get_inventory_products()[0]["quantity"] == 2


def test_failed_delivery_cancels_without_stock_or_cash(delivery_client):
    client, product_pk = delivery_client
    _rep_id, order_id = _create_order_and_assign(product_pk)
    auth = _login(client)
    response = client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [{
            "client_attempt_id": str(uuid.uuid4()),
            "order_id": order_id,
            "result": "failed",
            "failure_reason": "Shop closed",
            "cash_collected": 999,
        }]},
    )

    result = response.get_json()["results"][0]
    order, _items = db.get_order(order_id)
    assert result["ok"] is True
    assert order["status"] == "Cancelled"
    assert order["latest_delivery"]["failure_reason"] == "Shop closed"
    assert order["latest_delivery"]["cash_collected"] == 0
    assert db.get_stock_movements(product_pk) == []


def test_handover_is_idempotent_and_requires_admin_verification(delivery_client):
    client, product_pk = delivery_client
    rep_id, order_id = _create_order_and_assign(product_pk)
    db.record_stock_movement(product_pk, 2, "receipt", reference="Opening stock")
    auth = _login(client)
    client.post(
        "/api/mobile/deliveries/sync",
        headers=auth,
        json={"attempts": [{
            "client_attempt_id": str(uuid.uuid4()),
            "order_id": order_id,
            "result": "delivered",
            "cash_collected": 200,
        }]},
    )
    pending = client.get(
        "/api/mobile/deliveries", headers=auth
    ).get_json()["pending_cash"]
    assert len(pending) == 1
    handover = {
        "client_handover_id": str(uuid.uuid4()),
        "handover_date": pending[0]["created_at"][:10],
        "attempt_ids": [pending[0]["id"]],
        "declared_cash": 200,
        "note": "End of route",
    }
    first = client.post(
        "/api/mobile/handovers/sync",
        headers=auth,
        json={"handovers": [handover]},
    ).get_json()["results"][0]
    retry = client.post(
        "/api/mobile/handovers/sync",
        headers=auth,
        json={"handovers": [handover]},
    ).get_json()["results"][0]
    assert first["ok"] is True
    assert retry["ok"] is True and retry["already_synced"] is True

    with client.session_transaction() as session:
        session["admin"] = True
    dashboard = client.get(
        f"/admin/deliveries?date={handover['handover_date']}"
    )
    assert dashboard.status_code == 200
    assert b"200.00" in dashboard.data
    handover_row = db.get_delivery_handovers(handover["handover_date"])[0]
    verified = client.post(
        f"/admin/deliveries/handovers/{handover_row['id']}/verify",
        data={"date": handover["handover_date"], "received_cash": "190"},
        follow_redirects=True,
    )
    assert verified.status_code == 200
    saved = db.get_delivery_handovers(handover["handover_date"])[0]
    assert saved["status"] == "verified"
    assert saved["received_cash"] == 190
    assert saved["verified_by"]

    with pytest.raises(ValueError, match="already been verified"):
        db.verify_delivery_handover(
            handover_row["id"], 190, actor="admin"
        )
    assert rep_id > 0


def test_rep_with_delivery_history_cannot_be_deleted(delivery_client):
    client, product_pk = delivery_client
    rep_id, order_id = _create_order_and_assign(product_pk)

    with pytest.raises(ValueError, match="deactivate the rep instead"):
        db.delete_rep(rep_id)
    assert db.get_mobile_rep_by_username("delivery.rep")["id"] == rep_id

    with client.session_transaction() as session:
        session["admin"] = True
    response = client.post(f"/admin/reps/{rep_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert b"deactivate the rep instead" in response.data
    assert db.get_mobile_rep_by_username("delivery.rep")["id"] == rep_id

    assert db.get_order(order_id)[0]["delivery_rep_label"]
