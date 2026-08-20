from __future__ import annotations

import os

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import (
    Account,
    ExpenseTransaction,
    HouseholdShoppingDefault,
    RetailProductPreference,
    RetailProductSubstitution,
    ShoppingTripCompletion,
)
from services.household_context import household_id as current_household_id


client = app.test_client()


def _setup(balance: float = 1000.00) -> None:
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


def _stage(*, planned: float, actual: float | None = None, use_planned: bool = False, retailer: str = "walmart", store_name: str = "Walmart", store_id: str = "357", cart_signature: str = "sig-a"):
    return client.post(
        "/api/grocery/finished-shopping/stage",
        json={
            "planned_total": planned,
            "actual_total": actual,
            "use_planned_total": use_planned,
            "retailer": retailer,
            "store_name": store_name,
            "store_id": store_id,
            "cart_signature": cart_signature,
        },
    )


def _complete(*, planned: float, actual: float | None = None, use_planned: bool = False, retailer: str = "walmart", store_name: str = "Walmart", store_id: str = "357", cart_signature: str = "sig-a", operation_id: str | None = None, trip_token: str | None = None):
    payload = {
        "planned_total": planned,
        "actual_total": actual,
        "use_planned_total": use_planned,
        "retailer": retailer,
        "store_name": store_name,
        "store_id": store_id,
        "cart_signature": cart_signature,
        "confirm": True,
    }
    if operation_id:
        payload["operation_id"] = operation_id
    if trip_token:
        payload["trip_token"] = trip_token
    return client.post("/api/grocery/finished-shopping/complete", json=payload)


def test_stage_does_not_write_before_confirmation() -> None:
    _setup()
    resp = _stage(planned=163.48, use_planned=True)
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("staged") is True
    assert body.get("requires_confirmation") is True
    with app.app_context():
        assert ExpenseTransaction.query.count() == 0
        assert ShoppingTripCompletion.query.count() == 0


def test_use_planned_total_records_grocery_spend_and_updates_financials() -> None:
    _setup(balance=1000.00)
    stage = _stage(planned=163.48, use_planned=True)
    op_id = (stage.get_json() or {}).get("operation_id")
    resp = _complete(planned=163.48, use_planned=True, operation_id=op_id)
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("completed") is True
    assert body.get("already_completed") is False
    assert body.get("actual_total") == 163.48
    assert body.get("planned_total") == 163.48
    assert body.get("amount_source") == "planned"

    with app.app_context():
        tx = ExpenseTransaction.query.one()
        trip = ShoppingTripCompletion.query.one()
        account = Account.query.first()
        assert tx.category == "grocery"
        assert round(float(tx.amount), 2) == 163.48
        assert round(float(account.checking_balance), 2) == 836.52
        assert trip.planned_total_cents == 16348
        assert trip.actual_total_cents == 16348


def test_actual_total_overrides_planned_and_preserves_distinction() -> None:
    _setup(balance=1000.00)
    stage = _stage(planned=163.48, actual=171.00, use_planned=False)
    op_id = (stage.get_json() or {}).get("operation_id")
    resp = _complete(planned=163.48, actual=171.00, use_planned=False, operation_id=op_id)
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("planned_total") == 163.48
    assert body.get("actual_total") == 171.00
    assert body.get("amount_source") == "actual"

    with app.app_context():
        trip = ShoppingTripCompletion.query.one()
        tx = ExpenseTransaction.query.one()
        assert trip.planned_total_cents == 16348
        assert trip.actual_total_cents == 17100
        assert round(float(tx.amount), 2) == 171.00


def test_financial_metrics_refresh_after_completion() -> None:
    _setup(balance=1000.00)
    stage = _stage(planned=100.00, use_planned=True)
    op_id = (stage.get_json() or {}).get("operation_id")
    resp = _complete(planned=100.00, use_planned=True, operation_id=op_id)
    assert resp.status_code == 200
    metrics = (resp.get_json() or {}).get("metrics") or {}
    assert metrics.get("checking_balance") == 900.0
    safe = metrics.get("safe_to_spend") or {}
    components = safe.get("components") or {}
    assert components.get("grocery_spend_to_date") == 100.0
    assert components.get("groceries_remaining") is None
    assert safe.get("state") == "needs_setup"


def test_idempotent_duplicate_completion_same_operation_id() -> None:
    _setup(balance=1000.00)
    stage = _stage(planned=50.00, use_planned=True)
    op_id = (stage.get_json() or {}).get("operation_id")
    first = _complete(planned=50.00, use_planned=True, operation_id=op_id)
    second = _complete(planned=50.00, use_planned=True, operation_id=op_id)
    assert first.status_code == 200
    assert second.status_code == 200
    b2 = second.get_json() or {}
    assert b2.get("already_completed") is True

    with app.app_context():
        assert ExpenseTransaction.query.count() == 1
        assert ShoppingTripCompletion.query.count() == 1


def test_duplicate_trip_token_conflict_prevents_double_count() -> None:
    _setup(balance=1000.00)
    token = "trip-token-1"
    first = _complete(planned=20.00, use_planned=True, operation_id="op-a", trip_token=token)
    second = _complete(planned=20.00, use_planned=True, operation_id="op-b", trip_token=token)
    assert first.status_code == 200
    assert second.status_code == 409

    with app.app_context():
        assert ExpenseTransaction.query.count() == 1
        assert ShoppingTripCompletion.query.count() == 1


def test_failed_request_does_not_partially_write_state() -> None:
    _setup(balance=1000.00)
    bad = client.post(
        "/api/grocery/finished-shopping/complete",
        json={
            "planned_total": -5,
            "actual_total": 10,
            "use_planned_total": False,
            "retailer": "walmart",
            "store_name": "Walmart",
            "store_id": "357",
            "cart_signature": "sig-b",
            "confirm": True,
        },
    )
    assert bad.status_code == 400
    with app.app_context():
        assert ExpenseTransaction.query.count() == 0
        assert ShoppingTripCompletion.query.count() == 0


def test_reload_status_endpoint_preserves_completed_state() -> None:
    _setup(balance=1000.00)
    done = _complete(planned=22.00, use_planned=True, operation_id="op-reload")
    body = done.get_json() or {}
    status = client.get(
        f"/api/grocery/finished-shopping/status?operation_id={body['operation_id']}&trip_token={body['trip_token']}"
    )
    assert status.status_code == 200
    s = status.get_json() or {}
    assert s.get("completed") is True
    assert s.get("actual_total") == 22.0
    assert s.get("planned_total") == 22.0


def test_walmart_and_kroger_contexts_are_recorded() -> None:
    _setup(balance=1000.00)
    a = _complete(planned=30.00, use_planned=True, retailer="walmart", store_name="Walmart", store_id="357", operation_id="op-w")
    b = _complete(planned=31.00, use_planned=True, retailer="kroger", store_name="Gerbes", store_id="01100479", operation_id="op-k")
    assert a.status_code == 200
    assert b.status_code == 200

    with app.app_context():
        rows = ShoppingTripCompletion.query.order_by(ShoppingTripCompletion.id.asc()).all()
        assert len(rows) == 2
        assert rows[0].retailer == "walmart"
        assert rows[1].retailer == "kroger"
        assert rows[1].store_name == "Gerbes"


def test_completion_does_not_modify_preferences_substitutions_or_household_defaults() -> None:
    _setup(balance=1000.00)
    with app.app_context():
        pref = RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk",
            normalized_base_item="milk",
            preference_type="usual",
            preferred_product_title="Brand Milk",
            retailer="walmart",
            retailer_us_item_id="us-1",
            source="user_explicit",
        )
        db.session.add(pref)
        db.session.flush()
        db.session.add(
            RetailProductSubstitution(
                household_id=current_household_id(),
                base_item="milk",
                normalized_base_item="milk",
                preferred_preference_id=pref.id,
                substitute_product_title="Alt Milk",
                retailer="walmart",
                retailer_us_item_id="us-2",
                approval_type="explicit",
            )
        )
        db.session.add(
            HouseholdShoppingDefault(
                household_id=current_household_id(),
                owner_scope="household:default",
                preference_kind="category_default",
                preference_key="milk_type",
                preference_value="whole",
            )
        )
        db.session.commit()

    _complete(planned=40.00, use_planned=True, operation_id="op-safe")

    with app.app_context():
        assert RetailProductPreference.query.count() == 1
        assert RetailProductSubstitution.query.count() == 1
        assert HouseholdShoppingDefault.query.count() == 1


def test_cent_accurate_rounding_for_actual_amount() -> None:
    _setup(balance=1000.00)
    stage = _stage(planned=10.01, actual=10.015, use_planned=False)
    op_id = (stage.get_json() or {}).get("operation_id")
    resp = _complete(planned=10.01, actual=10.015, use_planned=False, operation_id=op_id)
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("actual_total") == 10.02

    with app.app_context():
        trip = ShoppingTripCompletion.query.one()
        tx = ExpenseTransaction.query.one()
        assert trip.actual_total_cents == 1002
        assert round(float(tx.amount), 2) == 10.02
