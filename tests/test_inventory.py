import pytest

import db
from app import app


@pytest.fixture
def inventory_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_FILE", str(tmp_path / "inventory-test.db"))
    monkeypatch.setattr(db, "EXCEL_FILE", str(tmp_path / "missing-catalog.xlsx"))
    db.init_db()
    product_pk = db.add_product({
        "product_id": "GTM - 9301",
        "product_name": "Inventory Test Product",
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
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
        session["admin_username"] = "inventory.admin"
    return client, product_pk


def test_stock_receipts_and_issues_are_atomic_and_cannot_go_negative(inventory_client):
    _client, product_pk = inventory_client
    assert db.get_inventory_products()[0]["quantity"] == 0

    receipt_id = db.record_stock_movement(
        product_pk, "12.5", "receipt", reference="DEL-100", actor="admin"
    )
    issue_id = db.record_stock_movement(
        product_pk, 2.5, "issue", reference="ISS-100", actor="admin"
    )

    assert receipt_id != issue_id
    assert db.get_inventory_products()[0]["quantity"] == 10
    ledger = db.get_stock_movements(product_pk)
    assert [movement["movement_type"] for movement in ledger] == ["issue", "receipt"]
    assert ledger[0]["delta"] == -2.5
    assert ledger[0]["balance_after"] == 10

    with pytest.raises(ValueError, match="Insufficient stock"):
        db.record_stock_movement(product_pk, 11, "issue")
    assert db.get_inventory_products()[0]["quantity"] == 10
    assert len(db.get_stock_movements(product_pk)) == 2


def test_stock_count_requires_complete_entries_and_approval_reconciles_once(inventory_client):
    _client, product_pk = inventory_client
    db.record_stock_movement(product_pk, 10, "receipt")
    count_id = db.create_stock_count(actor="counter")
    count, lines = db.get_stock_count(count_id)
    assert count["status"] == "draft"
    assert len(lines) == 1

    with pytest.raises(ValueError, match="every product"):
        db.approve_stock_count(count_id, actor="approver")
    assert db.get_inventory_products()[0]["quantity"] == 10

    db.save_stock_count(count_id, {lines[0]["id"]: "7"})
    db.approve_stock_count(count_id, actor="approver")

    count, lines = db.get_stock_count(count_id)
    assert count["status"] == "approved"
    assert count["approved_by"] == "approver"
    assert lines[0]["counted_quantity"] == 7
    assert db.get_inventory_products()[0]["quantity"] == 7
    adjustment = db.get_stock_movements(product_pk)[0]
    assert adjustment["movement_type"] == "count_adjustment"
    assert adjustment["delta"] == -3

    with pytest.raises(ValueError, match="already approved"):
        db.approve_stock_count(count_id, actor="approver")
    with pytest.raises(ValueError, match="cannot be changed"):
        db.save_stock_count(count_id, {lines[0]["id"]: 8})
    with pytest.raises(ValueError, match="cannot be deleted"):
        db.delete_stock_count(count_id)


def test_inventory_pages_and_manual_movement_require_admin(inventory_client):
    client, product_pk = inventory_client
    anonymous = app.test_client()
    assert anonymous.get("/admin/inventory").status_code == 302
    assert anonymous.post(
        "/admin/inventory/movement",
        data={"product_pk": product_pk, "movement_type": "receipt", "quantity": 3},
    ).status_code == 302

    inventory = client.get("/admin/inventory")
    assert inventory.status_code == 200
    assert b"Current balances" in inventory.data

    response = client.post(
        "/admin/inventory/movement",
        data={
            "product_pk": product_pk,
            "movement_type": "receipt",
            "quantity": "3",
            "reference": "PO-123",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Stock movement recorded." in response.data
    assert db.get_inventory_products()[0]["quantity"] == 3

    counts = client.get("/admin/stock-counts")
    assert counts.status_code == 200
    create = client.post("/admin/stock-counts/create")
    assert create.status_code == 302
    assert "/admin/stock-counts/1" in create.headers["Location"]
    detail = client.get("/admin/stock-counts/1")
    assert detail.status_code == 200

    _count, lines = db.get_stock_count(1)
    saved = client.post(
        "/admin/stock-counts/1/save",
        data={f"count_{lines[0]['id']}": "3"},
        follow_redirects=True,
    )
    assert saved.status_code == 200
    approved = client.post(
        "/admin/stock-counts/1/approve",
        follow_redirects=True,
    )
    assert approved.status_code == 200
    assert db.get_inventory_products()[0]["quantity"] == 3


def test_products_with_stock_history_cannot_be_deleted_or_reused(inventory_client):
    _client, product_pk = inventory_client
    db.record_stock_movement(product_pk, 2, "receipt")
    with pytest.raises(ValueError, match="cannot be deleted"):
        db.delete_product(product_pk)

    db.update_product(product_pk, {
        "product_id": "GTM - 9301",
        "product_name": "Inventory Test Product",
        "upc": 1,
        "unit": "Box",
        "retail": 100,
        "wholesale": 80,
        "category": "Test",
        "status": "Out Of Stock",
        "description": "",
        "supplier": "Test Supplier",
        "has_image": False,
    })
    with pytest.raises(ValueError, match="still has stock on hand"):
        db.reuse_product_id("GTM - 9301", {
            "product_name": "Replacement",
            "status": "In Stock",
        })


def test_purchase_order_partial_receipt_posts_stock_once(inventory_client):
    _client, product_pk = inventory_client
    purchase_order_id = db.create_purchase_order(
        "Test Supplier",
        [{"product_pk": product_pk, "quantity": 10, "unit_cost": 12.5}],
        invoice_number="INV-10",
        actor="buyer",
    )
    order, lines = db.get_purchase_order(purchase_order_id)
    assert order["status"] == "open"
    assert order["order_code"].startswith("PO-")
    assert db.get_inventory_products()[0]["quantity"] == 0

    status = db.receive_purchase_order(
        purchase_order_id, {lines[0]["id"]: "4"}, actor="receiver"
    )
    assert status == "partial"
    assert db.get_inventory_products()[0]["quantity"] == 4
    with pytest.raises(ValueError, match="Cannot receive more"):
        db.receive_purchase_order(
            purchase_order_id, {lines[0]["id"]: "7"}, actor="receiver"
        )
    assert db.get_inventory_products()[0]["quantity"] == 4

    status = db.receive_purchase_order(
        purchase_order_id, {lines[0]["id"]: "6"}, actor="receiver"
    )
    order, lines = db.get_purchase_order(purchase_order_id)
    assert status == "received"
    assert order["status"] == "received"
    assert lines[0]["received_quantity"] == 10
    assert db.get_inventory_products()[0]["quantity"] == 10
    movements = db.get_stock_movements(product_pk)
    assert len(movements) == 2
    assert all(row["movement_type"] == "purchase_receipt" for row in movements)
    with pytest.raises(ValueError, match="cannot be cancelled"):
        db.cancel_purchase_order(purchase_order_id)


def test_open_purchase_order_can_be_cancelled_without_stock_changes(inventory_client):
    _client, product_pk = inventory_client
    purchase_order_id = db.create_purchase_order(
        "Test Supplier",
        [{"product_pk": product_pk, "quantity": 2, "unit_cost": 5}],
    )
    order, _lines = db.get_purchase_order(purchase_order_id)
    db.cancel_purchase_order(purchase_order_id)
    order, _lines = db.get_purchase_order(purchase_order_id)
    assert order["status"] == "cancelled"
    assert db.get_inventory_products()[0]["quantity"] == 0


def test_purchase_order_routes_create_and_receive(inventory_client):
    client, product_pk = inventory_client
    assert app.test_client().get("/admin/purchases").status_code == 302
    assert client.get("/admin/purchases").status_code == 200
    assert client.get("/admin/purchases/new").status_code == 200
    response = client.post(
        "/admin/purchases/new",
        data={
            "supplier": "Test Supplier",
            "invoice_number": "INV-UI",
            "order_date": "2026-10-31",
            "product_pk": [str(product_pk)],
            "quantity": ["5"],
            "unit_cost": ["2.5"],
        },
    )
    assert response.status_code == 302
    assert "/admin/purchases/1" in response.headers["Location"]
    detail = client.get("/admin/purchases/1")
    assert detail.status_code == 200
    assert b"INV-UI" in detail.data

    _order, lines = db.get_purchase_order(1)
    receipt = client.post(
        "/admin/purchases/1/receive",
        data={f"received_{lines[0]['id']}": "5"},
        follow_redirects=True,
    )
    assert receipt.status_code == 200
    assert b"Received quantities added to inventory" in receipt.data
    assert db.get_inventory_products()[0]["quantity"] == 5


def test_open_purchase_order_prevents_product_reuse_or_deletion(inventory_client):
    _client, product_pk = inventory_client
    db.create_purchase_order(
        "Test Supplier",
        [{"product_pk": product_pk, "quantity": 1, "unit_cost": 5}],
    )
    db.update_product(product_pk, {
        "product_id": "GTM - 9301",
        "product_name": "Inventory Test Product",
        "upc": 1,
        "unit": "Box",
        "retail": 100,
        "wholesale": 80,
        "category": "Test",
        "status": "Out Of Stock",
        "description": "",
        "supplier": "Test Supplier",
        "has_image": False,
    })
    with pytest.raises(ValueError, match="open purchase order"):
        db.reuse_product_id("GTM - 9301", {
            "product_name": "Replacement",
            "status": "In Stock",
        })
    with pytest.raises(ValueError, match="open purchase order"):
        db.delete_product(product_pk)
