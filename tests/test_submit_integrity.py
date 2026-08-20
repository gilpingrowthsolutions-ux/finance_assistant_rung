from __future__ import annotations

import os

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import Account, Bill, ExpenseTransaction
from services.household_context import ensure_legacy_household


def _setup():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all()
        db.create_all()
        household = ensure_legacy_household()
        db.session.add(Account(household_id=household.id, checking_balance=1000.0))
        db.session.commit()
    return app.test_client()


def test_one_transaction_request_has_one_row_and_one_balance_effect() -> None:
    client = _setup()

    response = client.post(
        "/api/transactions",
        json={"description": "Integrity test", "amount": 42.25, "category": "discretionary"},
    )

    assert response.status_code == 200
    with app.app_context():
        assert ExpenseTransaction.query.filter_by(description="Integrity test").count() == 1
        assert Account.query.one().checking_balance == 957.75


def test_transaction_validation_still_rejects_missing_description() -> None:
    client = _setup()

    response = client.post(
        "/api/transactions",
        json={"description": "", "amount": 42.25, "category": "discretionary"},
    )

    assert response.status_code == 400
    with app.app_context():
        assert ExpenseTransaction.query.count() == 0
        assert Account.query.one().checking_balance == 1000.0


def test_one_bill_request_has_one_persisted_effect() -> None:
    client = _setup()

    response = client.post(
        "/bills",
        json={"name": "Integrity bill", "amount": 88.50, "due_date": "2026-09-01"},
    )

    assert response.status_code == 200
    with app.app_context():
        assert Bill.query.filter_by(name="Integrity bill").count() == 1
        assert Bill.query.one().amount == 88.50


def test_bill_validation_still_rejects_missing_name() -> None:
    client = _setup()

    response = client.post(
        "/bills",
        json={"name": "", "amount": 88.50, "due_date": "2026-09-01"},
    )

    assert response.status_code == 400
    with app.app_context():
        assert Bill.query.count() == 0
