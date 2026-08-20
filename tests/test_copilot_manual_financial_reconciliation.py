from __future__ import annotations

import os

TEST_DB_PATH = "/tmp/rung_m6_manual_reconciliation.sqlite"
os.environ["RUNG_DB_PATH"] = TEST_DB_PATH

from app import app
from extensions import db
from models import Account, ExpenseTransaction, ShoppingTripCompletion, ActionAudit
from services.household_context import household_id as current_household_id


app.testing = True
client = app.test_client()


def _setup(balance: float = 1000.0) -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(
            Account(
                household_id=current_household_id(),
                checking_balance=balance,
                food_allocation_pct=40.0,
                pay_period_days=14,
                meals_per_day=3,
                kroger_store_name="Walmart",
            )
        )
        db.session.commit()


def _stage(text: str) -> dict:
    resp = client.post("/api/copilot/stage", json={"text": text, "user_id": "m6-user"})
    assert resp.status_code == 200
    body = resp.get_json() or {}
    return body.get("actions_taken") or {}


def _apply(staged: dict, text: str):
    return client.post(
        "/api/copilot/apply",
        json={"staged_actions": staged, "text": text, "user_id": "m6-user"},
    )


def _seed_completed_trip(*, planned: float, actual: float, operation_id: str = "op_seed") -> None:
    with app.app_context():
        txn = ExpenseTransaction(household_id=current_household_id(), description="Grocery trip Walmart", amount=actual, category="grocery")
        db.session.add(txn)
        db.session.flush()
        db.session.add(
            ShoppingTripCompletion(
                household_id=current_household_id(),
                operation_id=operation_id,
                trip_token="trip_seed_token",
                transaction_id=txn.id,
                retailer="walmart",
                store_name="Walmart",
                store_id="357",
                planned_total_cents=int(round(planned * 100)),
                actual_total_cents=int(round(actual * 100)),
                amount_source="actual",
                cart_signature="sig-seed",
                manual_provisional=True,
            )
        )
        db.session.commit()


def test_stage_balance_reconciliation_is_preview_only() -> None:
    _setup(balance=912.34)
    staged = _stage("Set my checking balance to $1000.00")

    rows = staged.get("balance_reconciliations") or []
    assert len(rows) == 1
    assert rows[0]["current_balance"] == 912.34
    assert rows[0]["new_balance"] == 1000.0
    assert rows[0]["difference"] == 87.66

    with app.app_context():
        account = Account.query.first()
        assert round(float(account.checking_balance), 2) == 912.34
        assert ExpenseTransaction.query.count() == 0


def test_apply_balance_reconciliation_updates_balance_exactly() -> None:
    _setup(balance=912.34)
    staged = _stage("Reconcile my balance to $1000")
    resp = _apply(staged, "reconcile balance")

    assert resp.status_code == 200
    with app.app_context():
        account = Account.query.first()
        assert round(float(account.checking_balance), 2) == 1000.00


def test_stage_income_event_and_apply_increases_balance() -> None:
    _setup(balance=500.00)
    staged = _stage("I got paid $250.50")
    income_rows = staged.get("income_logged") or []
    assert len(income_rows) == 1
    assert income_rows[0]["amount"] == 250.50

    resp = _apply(staged, "apply income")
    assert resp.status_code == 200

    with app.app_context():
        account = Account.query.first()
        assert round(float(account.checking_balance), 2) == 750.50
        txns = ExpenseTransaction.query.all()
        assert len(txns) == 1
        assert txns[0].category == "income"
        assert round(float(txns[0].amount), 2) == 250.50


def test_stage_spending_with_merchant_context_and_apply() -> None:
    _setup(balance=1000.00)
    staged = _stage("I spent $23.45 at Costco")
    spending = staged.get("expenses_logged") or []
    assert len(spending) >= 1
    assert any((row.get("merchant") or "").lower() == "costco" for row in spending)

    resp = _apply(staged, "apply spending")
    assert resp.status_code == 200

    with app.app_context():
        account = Account.query.first()
        assert round(float(account.checking_balance), 2) == 976.55


def test_stage_shopping_correction_latest_finds_existing_trip_without_writes() -> None:
    _setup(balance=1000.00)
    _seed_completed_trip(planned=70.00, actual=75.00, operation_id="op_trip_a")

    staged = _stage("Correct my latest shopping trip to $80")
    rows = staged.get("shopping_trip_corrections") or []
    assert len(rows) == 1
    assert rows[0]["previous_actual_total"] == 75.0
    assert rows[0]["new_actual_total"] == 80.0
    assert rows[0]["difference"] == 5.0

    with app.app_context():
        trip = ShoppingTripCompletion.query.one()
        txn = ExpenseTransaction.query.get(trip.transaction_id)
        assert trip.actual_total_cents == 7500
        assert round(float(txn.amount), 2) == 75.00


def test_apply_shopping_correction_adjusts_by_delta_only() -> None:
    _setup(balance=1000.00)
    _seed_completed_trip(planned=70.00, actual=75.00, operation_id="op_trip_a")

    staged = _stage("Correct finished shopping trip op_trip_a actual to $80")
    resp = _apply(staged, "apply correction")
    assert resp.status_code == 200

    with app.app_context():
        account = Account.query.first()
        trip = ShoppingTripCompletion.query.one()
        txn = ExpenseTransaction.query.get(trip.transaction_id)
        assert round(float(account.checking_balance), 2) == 995.00
        assert trip.planned_total_cents == 7000
        assert trip.actual_total_cents == 8000
        assert round(float(txn.amount), 2) == 80.00


def test_apply_shopping_correction_is_idempotent_by_operation_id() -> None:
    _setup(balance=1000.00)
    _seed_completed_trip(planned=70.00, actual=75.00, operation_id="op_trip_a")

    staged = _stage("Correct finished shopping trip op_trip_a actual to $80")
    first = _apply(staged, "apply correction")
    second = _apply(staged, "apply correction")

    assert first.status_code == 200
    assert second.status_code == 200
    body2 = second.get_json() or {}
    assert ((body2.get("actions_taken") or {}).get("already_applied")) is True

    with app.app_context():
        account = Account.query.first()
        assert round(float(account.checking_balance), 2) == 995.00
        assert ActionAudit.query.filter_by(operation_id=staged["operation_id"]).count() == 1


def test_invalid_income_amount_returns_400_without_partial_writes() -> None:
    _setup(balance=500.00)
    staged = _stage("I got paid $250")
    staged["income_logged"][0]["amount"] = "not-a-number"

    resp = _apply(staged, "apply invalid income")
    assert resp.status_code == 400

    with app.app_context():
        account = Account.query.first()
        assert round(float(account.checking_balance), 2) == 500.00
        assert ExpenseTransaction.query.count() == 0
        assert ActionAudit.query.filter_by(operation_id=staged["operation_id"]).count() == 0


def test_invalid_shopping_correction_amount_returns_400_without_partial_writes() -> None:
    _setup(balance=1000.00)
    _seed_completed_trip(planned=70.00, actual=75.00, operation_id="op_trip_a")

    staged = _stage("Correct finished shopping trip op_trip_a actual to $80")
    staged["shopping_trip_corrections"][0]["new_actual_total"] = "oops"

    resp = _apply(staged, "apply invalid correction")
    assert resp.status_code == 400

    with app.app_context():
        account = Account.query.first()
        trip = ShoppingTripCompletion.query.one()
        txn = ExpenseTransaction.query.get(trip.transaction_id)
        assert round(float(account.checking_balance), 2) == 1000.00
        assert trip.actual_total_cents == 7500
        assert round(float(txn.amount), 2) == 75.00
        assert ActionAudit.query.filter_by(operation_id=staged["operation_id"]).count() == 0
