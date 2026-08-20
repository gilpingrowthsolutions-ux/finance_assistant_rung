from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import tempfile
import uuid
from datetime import datetime, timezone

os.environ["RUNG_DB_PATH"] = f"/tmp/rung_household_context_security_{uuid.uuid4().hex}.db"
os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "gate2a-test-secret"

from app import app
from extensions import db
from models import Account, Bill, ExpenseTransaction, GroceryItem, Household, ShoppingTripCompletion


def _sign(public_id: str) -> str:
    secret = os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"]
    return hmac.new(secret.encode("utf-8"), public_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(household_public_id: str) -> dict[str, str]:
    return {
        "X-Household-Id": household_public_id,
        "X-Household-Signature": _sign(household_public_id),
    }


def _setup() -> tuple[str, str]:
    os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "gate2a-test-secret"
    with app.app_context():
        db.drop_all()
        db.create_all()

        house_a = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="house-a")
        house_b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="house-b")
        db.session.add_all([house_a, house_b])
        db.session.flush()

        acc_a = Account(household_id=house_a.id, checking_balance=1000.0)
        acc_b = Account(household_id=house_b.id, checking_balance=2000.0)
        db.session.add_all([acc_a, acc_b])
        db.session.flush()

        bill_a = Bill(household_id=house_a.id, name="Bill A", amount=11.0, due_date=datetime.now(timezone.utc))
        bill_b = Bill(household_id=house_b.id, name="Bill B", amount=22.0, due_date=datetime.now(timezone.utc))
        tx_a = ExpenseTransaction(household_id=house_a.id, description="Tx A", amount=3.0, category="discretionary", source="manual", local_account_id=acc_a.id)
        tx_b = ExpenseTransaction(household_id=house_b.id, description="Tx B", amount=4.0, category="discretionary", source="manual", local_account_id=acc_b.id)
        item_a = GroceryItem(household_id=house_a.id, item_name="A item", estimated_price=1.0, store_name="Store A")
        item_b = GroceryItem(household_id=house_b.id, item_name="B item", estimated_price=1.0, store_name="Store B")
        db.session.add_all([bill_a, bill_b, tx_a, tx_b, item_a, item_b])
        db.session.commit()

        return house_a.public_id, house_b.public_id


def test_unsigned_or_invalid_signature_cannot_override_household() -> None:
    _, house_b_public_id = _setup()
    client = app.test_client()

    unsigned = client.get("/bills", headers={"X-Household-Id": house_b_public_id})
    assert unsigned.status_code == 403

    bad_sig = client.get(
        "/bills",
        headers={"X-Household-Id": house_b_public_id, "X-Household-Signature": "bad"},
    )
    assert bad_sig.status_code == 403


def test_signed_household_context_isolation_and_direct_id_blocking() -> None:
    house_a_public_id, house_b_public_id = _setup()
    client = app.test_client()

    headers_a = _headers(house_a_public_id)
    headers_b = _headers(house_b_public_id)

    bills_a = client.get("/bills", headers=headers_a).get_json() or []
    bills_b = client.get("/bills", headers=headers_b).get_json() or []
    assert [b["name"] for b in bills_a] == ["Bill A"]
    assert [b["name"] for b in bills_b] == ["Bill B"]

    tx_a = client.get("/api/transactions", headers=headers_a).get_json() or []
    tx_b = client.get("/api/transactions", headers=headers_b).get_json() or []
    assert [t["description"] for t in tx_a] == ["Tx A"]
    assert [t["description"] for t in tx_b] == ["Tx B"]

    with app.app_context():
        b_bill_id = Bill.query.filter(Bill.name == "Bill B").first().id
        b_tx_id = ExpenseTransaction.query.filter(ExpenseTransaction.description == "Tx B").first().id
        b_item_id = GroceryItem.query.filter(GroceryItem.item_name == "B item").first().id

    assert client.post(f"/bills/{b_bill_id}/pay", headers=headers_a).status_code == 404
    assert client.delete(f"/transactions/{b_tx_id}", headers=headers_a).status_code == 404
    assert client.delete(f"/api/grocery/{b_item_id}", headers=headers_a).status_code == 404


def test_finished_shopping_duplicate_concurrency_is_household_idempotent() -> None:
    fd, db_path = tempfile.mkstemp(prefix="rung_gate2a_ctx_", suffix=".db")
    os.close(fd)
    script = """
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone

os.environ["RUNG_DB_PATH"] = os.environ["GATE2A_TMP_DB_PATH"]
os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "gate2a-test-secret"

from app import app
from extensions import db
from models import Account, Bill, ExpenseTransaction, GroceryItem, Household, ShoppingTripCompletion


def _sign(public_id: str) -> str:
    secret = os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"]
    return hmac.new(secret.encode("utf-8"), public_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(household_public_id: str) -> dict[str, str]:
    return {
        "X-Household-Id": household_public_id,
        "X-Household-Signature": _sign(household_public_id),
    }


with app.app_context():
    db.drop_all()
    db.create_all()
    house_a = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="house-a")
    house_b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="house-b")
    db.session.add_all([house_a, house_b])
    db.session.flush()

    acc_a = Account(household_id=house_a.id, checking_balance=1000.0)
    acc_b = Account(household_id=house_b.id, checking_balance=2000.0)
    db.session.add_all([acc_a, acc_b])
    db.session.flush()

    bill_a = Bill(household_id=house_a.id, name="Bill A", amount=11.0, due_date=datetime.now(timezone.utc))
    bill_b = Bill(household_id=house_b.id, name="Bill B", amount=22.0, due_date=datetime.now(timezone.utc))
    tx_a = ExpenseTransaction(household_id=house_a.id, description="Tx A", amount=3.0, category="discretionary", source="manual", local_account_id=acc_a.id)
    tx_b = ExpenseTransaction(household_id=house_b.id, description="Tx B", amount=4.0, category="discretionary", source="manual", local_account_id=acc_b.id)
    item_a = GroceryItem(household_id=house_a.id, item_name="A item", estimated_price=1.0, store_name="Store A")
    item_b = GroceryItem(household_id=house_b.id, item_name="B item", estimated_price=1.0, store_name="Store B")
    db.session.add_all([bill_a, bill_b, tx_a, tx_b, item_a, item_b])
    db.session.commit()

house_a_public_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
house_b_public_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
headers_a = _headers(house_a_public_id)
headers_b = _headers(house_b_public_id)

payload = {
    "confirm": True,
    "operation_id": "same-op-id",
    "planned_total": 25.0,
    "actual_total": 20.0,
    "retailer": "walmart",
    "store_name": "Walmart",
    "store_id": "357",
    "cart_signature": "same-cart",
}

a_first = app.test_client().post("/api/grocery/finished-shopping/complete", json=payload, headers=headers_a)
b_first = app.test_client().post("/api/grocery/finished-shopping/complete", json=payload, headers=headers_b)

barrier = threading.Barrier(2)
statuses = []
worker_errors = []
result_lock = threading.Lock()


def worker() -> None:
    try:
        with app.test_client() as c:
            local_headers = _headers(house_a_public_id)
            barrier.wait()
            resp = c.post("/api/grocery/finished-shopping/complete", json=payload, headers=local_headers)
        with result_lock:
            statuses.append(resp.status_code)
    except Exception as exc:
        with result_lock:
            worker_errors.append(repr(exc))


t1 = threading.Thread(target=worker)
t2 = threading.Thread(target=worker)
t1.start()
t2.start()
t1.join()
t2.join()

with app.app_context():
    a = Household.query.filter_by(public_id=house_a_public_id).first()
    b = Household.query.filter_by(public_id=house_b_public_id).first()
    a_acct = Account.query.filter_by(household_id=a.id).first()
    b_acct = Account.query.filter_by(household_id=b.id).first()

    payload_out = {
        "a_first": a_first.status_code,
        "b_first": b_first.status_code,
        "worker_errors": worker_errors,
        "statuses": sorted(statuses),
        "a_trip_count": ShoppingTripCompletion.query.filter_by(household_id=a.id, operation_id="same-op-id").count(),
        "b_trip_count": ShoppingTripCompletion.query.filter_by(household_id=b.id, operation_id="same-op-id").count(),
        "a_grocery_count": ExpenseTransaction.query.filter_by(household_id=a.id, category="grocery").count(),
        "b_grocery_count": ExpenseTransaction.query.filter_by(household_id=b.id, category="grocery").count(),
        "a_balance": round(float(a_acct.checking_balance), 2),
        "b_balance": round(float(b_acct.checking_balance), 2),
    }

print(json.dumps(payload_out, sort_keys=True))
"""

    env = dict(os.environ)
    env["GATE2A_TMP_DB_PATH"] = db_path
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    data = json.loads((completed.stdout or "").strip().splitlines()[-1])

    assert data["a_first"] == 200
    assert data["b_first"] == 200
    assert data["worker_errors"] == []
    assert data["statuses"] == [200, 200]
    assert data["a_trip_count"] == 1
    assert data["b_trip_count"] == 1
    assert data["a_grocery_count"] == 1
    assert data["b_grocery_count"] == 1
    assert data["a_balance"] == 980.00
    assert data["b_balance"] == 1980.00
