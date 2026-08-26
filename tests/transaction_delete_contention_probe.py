"""Disposable SQLite contention probe for provenance-safe transaction deletion.

Run only with ``RUNG_DB_PATH`` set before process start.  Separate threads get
separate Flask/SQLAlchemy request contexts and race the actual DELETE route.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

if not str(os.environ.get("RUNG_DB_PATH") or "").strip():
    raise RuntimeError("Set RUNG_DB_PATH to a disposable SQLite database before running this probe.")

from app import app, db
from models import Account, ExpenseTransaction
from services.household_context import household_id


def main() -> None:
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = household_id()
        account = Account(household_id=hid, checking_balance=500.0)
        db.session.add(account)
        db.session.commit()
    with app.test_client() as client:
        created = client.post("/api/transactions", json={
            "description": "Concurrent eligible expense", "amount": 40.0, "category": "discretionary",
        })
        assert created.status_code == 200
        tx_id = created.get_json()["id"]

    gate = Barrier(2)

    def attempt() -> int:
        with app.test_client() as client:
            gate.wait()
            return client.delete(f"/transactions/{tx_id}").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _unused: attempt(), range(2)))

    with app.app_context():
        account = Account.query.filter_by(household_id=household_id()).one()
        remaining = ExpenseTransaction.query.filter_by(id=tx_id).count()
        result = {
            "statuses": sorted(statuses),
            "remaining_transactions": remaining,
            "checking_balance": round(float(account.checking_balance), 2),
        }
    assert result == {
        "statuses": [200, 404],
        "remaining_transactions": 0,
        "checking_balance": 500.0,
    }, result
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
