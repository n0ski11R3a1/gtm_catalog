from datetime import date

import db
import pytest
from app import app


@pytest.fixture
def sales_report_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_FILE", str(tmp_path / "report-test.db"))
    monkeypatch.setattr(db, "EXCEL_FILE", str(tmp_path / "missing-catalog.xlsx"))
    db.init_db()
    db.add_product({
        "product_id": "GTM - 9201",
        "product_name": "Report Product",
        "upc": 1,
        "unit": "Box",
        "retail": 100,
        "wholesale": 80,
        "category": "Test",
        "status": "In Stock",
        "description": "",
        "supplier": "",
        "has_image": False,
    })
    db.add_rep("T-RPT", "Report Rep")
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    return client


def test_sales_report_summarizes_statuses_and_excludes_cancelled_product_sales(sales_report_client):
    rep_name = "T-RPT (Report Rep)"
    product = db.get_all_products()[0]
    orders = []
    for outlet, quantity, status in (
        ("Shop A", 2, "Fulfilled"),
        ("Shop B", 1, "New"),
        ("Shop C", 9, "Cancelled"),
    ):
        order_id, _retail, _wholesale = db.create_order(
            rep_name,
            [{"product_pk": product["id"], "quantity": quantity}],
            outlet_name=outlet,
        )
        db.update_order_status(order_id, status)
        orders.append(order_id)

    report = db.get_sales_report(date.today().isoformat(), date.today().isoformat())
    assert report["totals"]["order_count"] == 3
    assert report["totals"]["outlet_count"] == 3
    assert report["totals"]["fulfilled_count"] == 1
    assert report["totals"]["pending_count"] == 1
    assert report["totals"]["cancelled_count"] == 1
    assert report["totals"]["submitted_sales"] == 1200
    assert report["totals"]["fulfilled_sales"] == 200
    assert report["products"][0]["quantity"] == 3
    assert report["products"][0]["order_count"] == 2
    assert report["products"][0]["sales"] == 300


def test_sales_report_page_validates_range_and_renders(sales_report_client):
    response = sales_report_client.get("/admin/sales-report")
    assert response.status_code == 200
    assert b"Sales performance" in response.data

    invalid_range = sales_report_client.get(
        "/admin/sales-report?from=2026-11-02&to=2026-11-01"
    )
    assert invalid_range.status_code == 302
