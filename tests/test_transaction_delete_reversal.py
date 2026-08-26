from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ.setdefault("PLAID_CLIENT_ID", "plaid_test_client")
os.environ.setdefault("PLAID_SECRET", "plaid_test_secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("PLAID_TOKEN_ENCRYPTION_KEY", "x7cUQ1K8v1SCh4skQ53QqE5s8z3v8c2n6cihVQMcWDo=")

from app import app, db  # noqa: E402
from services.household_context import household_id as current_household_id  # noqa: E402
from models import Account, ExpenseTransaction  # noqa: E402


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        account = Account(
            household_id=current_household_id(),
            checking_balance=500.0,
            food_allocation_pct=40.0,
            pay_period_days=14,
            meals_per_day=3,
        )
        db.session.add(account)
        db.session.commit()
    return app.test_client()


def _balance() -> float:
    with app.app_context():
        return float(Account.query.first().checking_balance)


def test_delete_expense_transaction_reverses_balance_exactly_once(client):
    resp = client.post("/api/transactions", json={"description": "Hardware store", "amount": 40.0, "category": "discretionary"})
    assert resp.status_code == 200
    txn_id = resp.get_json()["id"]
    assert _balance() == 460.0

    delete_resp = client.delete(f"/transactions/{txn_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.get_json()["new_balance"] == 500.0
    assert _balance() == 500.0
    with app.app_context():
        assert db.session.get(ExpenseTransaction, txn_id) is None


def test_delete_income_transaction_reverses_balance_exactly_once(client):
    with app.app_context():
        account = Account.query.first()
        tx = ExpenseTransaction(
            household_id=current_household_id(),
            description="Paycheck",
            amount=250.0,
            category="income",
            source="manual",
            local_account_id=account.id,
            date=datetime.now(timezone.utc),
        )
        db.session.add(tx)
        account.checking_balance = float(account.checking_balance) + 250.0
        db.session.commit()
        txn_id = tx.id
    assert _balance() == 750.0

    delete_resp = client.delete(f"/transactions/{txn_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.get_json()["new_balance"] == 500.0
    assert _balance() == 500.0


def test_delete_missing_transaction_does_not_change_balance(client):
    resp = client.delete("/transactions/999999")
    assert resp.status_code == 404
    assert _balance() == 500.0


def test_double_delete_only_reverses_once(client):
    resp = client.post("/api/transactions", json={"description": "Coffee", "amount": 5.0, "category": "discretionary"})
    txn_id = resp.get_json()["id"]
    assert _balance() == 495.0

    first = client.delete(f"/transactions/{txn_id}")
    assert first.status_code == 200
    assert _balance() == 500.0

    second = client.delete(f"/transactions/{txn_id}")
    assert second.status_code == 404
    assert _balance() == 500.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
