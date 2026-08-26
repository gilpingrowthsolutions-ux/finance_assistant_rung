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
from models import (  # noqa: E402
    Account,
    ExpenseTransaction,
    ShoppingTripCompletion,
    TransactionReconciliation,
)
import services.transaction_deletion as transaction_deletion  # noqa: E402


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


def test_finished_shopping_transaction_is_protected_without_effect(client):
    response = client.post("/api/transactions", json={"description": "Finished grocery", "amount": 40.0, "category": "grocery"})
    txn_id = response.get_json()["id"]
    with app.app_context():
        db.session.add(ShoppingTripCompletion(
            household_id=current_household_id(), operation_id="finished-op", trip_token="finished-trip",
            transaction_id=txn_id, retailer="walmart", store_name="Walmart", store_id="357",
            planned_total_cents=4000, actual_total_cents=4000, cart_signature="finished-cart",
        ))
        db.session.commit()

    listed = client.get("/api/transactions").get_json()
    assert listed[0]["can_delete"] is False
    assert listed[0]["delete_reason"]
    rejected = client.delete(f"/transactions/{txn_id}")
    assert rejected.status_code == 409
    assert _balance() == 460.0
    with app.app_context():
        assert db.session.get(ExpenseTransaction, txn_id) is not None
        assert ShoppingTripCompletion.query.filter_by(transaction_id=txn_id).count() == 1


@pytest.mark.parametrize("status", ["proposed", "matched", "rejected"])
def test_reconciliation_referenced_transaction_is_protected_without_effect(client, status):
    response = client.post("/api/transactions", json={"description": "Bank candidate", "amount": 25.0, "category": "discretionary"})
    txn_id = response.get_json()["id"]
    with app.app_context():
        db.session.add(TransactionReconciliation(
            household_id=current_household_id(), owner_scope="anonymous", manual_transaction_id=txn_id,
            plaid_transaction_id=f"plaid-{status}", status=status,
            user_confirmed=status != "proposed", confirmation_action=status,
        ))
        db.session.commit()

    rejected = client.delete(f"/transactions/{txn_id}")
    assert rejected.status_code == 409
    assert _balance() == 475.0
    with app.app_context():
        assert db.session.get(ExpenseTransaction, txn_id) is not None
        assert TransactionReconciliation.query.filter_by(manual_transaction_id=txn_id).count() == 1


def test_plaid_identified_transaction_is_protected_without_effect(client):
    with app.app_context():
        account = Account.query.first()
        tx = ExpenseTransaction(
            household_id=current_household_id(), description="Plaid purchase", amount=60.0,
            category="discretionary", source="plaid_import", plaid_transaction_id="plaid-identity-1",
            local_account_id=account.id,
        )
        db.session.add(tx)
        account.checking_balance -= 60.0
        db.session.commit()
        txn_id = tx.id

    rejected = client.delete(f"/transactions/{txn_id}")
    assert rejected.status_code == 409
    assert _balance() == 440.0
    with app.app_context():
        assert db.session.get(ExpenseTransaction, txn_id) is not None


def test_reversal_failure_rolls_back_the_delete(client, monkeypatch):
    response = client.post("/api/transactions", json={"description": "Failure safety", "amount": 15.0, "category": "discretionary"})
    txn_id = response.get_json()["id"]

    def fail_balance(*_args, **_kwargs):
        raise RuntimeError("injected balance failure")

    monkeypatch.setattr(transaction_deletion, "apply_balance_delta", fail_balance)
    with pytest.raises(RuntimeError, match="injected balance failure"):
        client.delete(f"/transactions/{txn_id}")
    assert _balance() == 485.0
    with app.app_context():
        assert db.session.get(ExpenseTransaction, txn_id) is not None

if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
