from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import PYF_TARGET_SETTING_KEY, REQUIRED_EXPENSE_REVIEWED, REQUIRED_EXPENSE_REVIEW_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app  # noqa: E402
from extensions import db  # noqa: E402
from models import Account, Bill, ExpenseTransaction, IncomePlanVersion, UserPreference, UserSetting  # noqa: E402
from services.household_context import household_id as current_household_id  # noqa: E402
from services.selected_store import get_selected_store, select_store  # noqa: E402
from services.tax_adapters import MissouriDorQ3Adapter  # noqa: E402
from services.tax_engine import canonical_tax_decision, import_dataset_atomic, resolve_store_tax_profile  # noqa: E402


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        account = Account(household_id=hid, checking_balance=2000.0, pay_period_days=14)
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            IncomePlanVersion(household_id=hid, operation_id="tax-gate-plan", expected_income_cents=100000, effective_at=datetime.now(timezone.utc) - timedelta(days=30), source="test_confirmation"),
            UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="0"),
            UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="0.00"),
            UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="200.00"),
            Bill(household_id=hid, name="Required utility", amount=50.0, due_date=datetime.now(timezone.utc) + timedelta(days=4), is_paid=False),
            Bill(household_id=hid, name="Fuel plan", amount=40.0, due_date=datetime.now(timezone.utc) + timedelta(days=4), is_gas_estimate=True, is_paid=False),
            ExpenseTransaction(household_id=hid, description="Paycheck", amount=1000.0, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc) - timedelta(days=3)),
        ])
        select_store(hid, retailer="walmart", store_id="357", store_name="Walmart — Versailles", address="1003 W Newton St, Versailles, MO 65084", city="Versailles", state="MO", postal_code="65084", account=account)
        result = import_dataset_atomic(adapter=MissouriDorQ3Adapter(), source_path=str(Path.cwd() / "data/tax/official/missouri"), activate=True)
        assert result["ok"] is True
        db.session.commit()
    return app.test_client()


def _decision(*, city: str, state: str, postal: str, item: str = "laundry detergent", actual_tax: int | None = None, actual_total: int | None = None, context: str = "manual_local"):
    profile = resolve_store_tax_profile(
        retailer="manual", retailer_store_id=f"{state}-{postal or 'none'}", store_name="Purchase location",
        store_address="", zip_code=postal, city_state=f"{city}, {state}" if state else city,
        latitude=None, longitude=None, calculation_date=date(2026, 8, 15), owner_scope="tax-test",
    )
    return canonical_tax_decision(
        store_tax_profile=profile, cart_items=[{"item_name": item, "estimated_price": 10.00}],
        calculation_date=date(2026, 8, 15), owner_scope="tax-test", purchase_context=context,
        city=city, postal_code=postal, actual_tax_cents=actual_tax, actual_total_cents=actual_total,
    )


def test_exact_eldon_and_versailles_are_rung_calculated(client):
    with app.app_context():
        for city, postal in (("Versailles", "65084"), ("Eldon", "65026")):
            result = _decision(city=city, state="MO", postal=postal)
            assert result["status"] == "rung_calculated"
            assert result["label"] == "Rung-calculated"
            assert result["jurisdiction"]["precision"] in {"ZIP5", "CITY_COUNTY"}
            assert result["source"]["type"] == "official_government"
            assert result["tax_cents"] and result["total_cents"] == 1000 + result["tax_cents"]


def test_unsupported_missouri_is_state_only_estimated(client):
    with app.app_context():
        result = _decision(city="Columbia", state="MO", postal="65201")
        assert result["status"] == "estimated"
        assert result["label"] == "Estimated"
        assert result["jurisdiction"]["precision"] == "STATE_ONLY"
        assert "local_tax_components" in result["jurisdiction"]["missing_components"]


def test_unsupported_national_and_ambiguous_taxability_fail_truthfully(client):
    with app.app_context():
        unsupported = _decision(city="Juneau", state="AK", postal="99801")
        ambiguous = _decision(city="Versailles", state="MO", postal="65084", item="mystery object")
        assert unsupported["status"] == "tax_not_included_yet"
        assert unsupported["tax_cents"] is None and unsupported["total_cents"] is None
        assert unsupported["rate_components_bps"] == {"general_merchandise": 0, "grocery_food": 0, "prepared_food": 0}
        assert ambiguous["status"] == "tax_not_included_yet"
        assert "supported_taxability" in ambiguous["jurisdiction"]["missing_components"]


def test_actual_checkout_precedes_unsupported_jurisdiction_and_rounding(client):
    with app.app_context():
        by_tax = _decision(city="Juneau", state="AK", postal="99801", actual_tax=83)
        by_total = _decision(city="", state="", postal="", item="mystery object", actual_total=1083)
        assert by_tax["status"] == by_total["status"] == "confirmed"
        assert by_tax["tax_cents"] == by_total["tax_cents"] == 83
        assert by_tax["total_cents"] == by_total["total_cents"] == 1083


def test_manual_and_online_context_do_not_change_selected_store(client):
    with app.app_context():
        hid = current_household_id()
        before = get_selected_store(hid)
    local = client.post("/api/decision/can-i-buy", json={"item_name": "Soap", "cost": 10, "purchase_context": "manual_local", "tax_category": "general_merchandise", "city": "Columbia", "state": "MO", "postal_code": "65201"})
    online = client.post("/api/decision/can-i-buy", json={"item_name": "Soap", "cost": 10, "purchase_context": "online_delivery", "tax_category": "general_merchandise", "city": "Versailles", "state": "MO", "postal_code": "65084"})
    unsupported = client.post("/api/decision/can-i-buy", json={"item_name": "Soap", "cost": 10, "purchase_context": "manual_local", "tax_category": "general_merchandise", "city": "Juneau", "state": "AK", "postal_code": "99801"})
    assert local.status_code == online.status_code == unsupported.status_code == 200, (local.get_json(), online.get_json(), unsupported.get_json())
    assert (local.get_json() or {})["tax"]["status"] == "estimated"
    assert (online.get_json() or {})["tax"]["status"] == "estimated"
    assert "seller_marketplace_tax_handling" in (online.get_json() or {})["tax"]["jurisdiction"]["missing_components"]
    assert (unsupported.get_json() or {})["tax"]["status"] == "tax_not_included_yet"
    with app.app_context():
        assert get_selected_store(current_household_id())["store_id"] == before["store_id"] == "357"


def test_affordability_uses_tax_inclusive_total_and_is_read_only(client):
    with app.app_context():
        before = (ExpenseTransaction.query.count(), float(Account.query.first().checking_balance))
    response = client.post("/api/decision/can-i-buy", json={"item_name": "Soap", "cost": 10, "purchase_context": "selected_physical_store", "tax_category": "general_merchandise"})
    body = response.get_json() or {}
    assert response.status_code == 200, body
    assert body["tax"]["status"] == "rung_calculated"
    assert body["purchase_total"] > body["purchase"]
    assert round(body["safe_to_spend_now"] - body["safe_to_spend_after"], 2) == body["purchase_total"]
    with app.app_context():
        assert (ExpenseTransaction.query.count(), float(Account.query.first().checking_balance)) == before


def test_manual_transaction_amount_is_confirmed_total_not_retaxed(client):
    response = client.post("/api/transactions", json={"description": "Local purchase", "amount": 10.83, "category": "discretionary", "purchase_context": {"state": "MO", "postal_code": "65201"}})
    body = response.get_json() or {}
    assert response.status_code == 200
    assert body["amount_authority"] == "confirmed_transaction_total"
    assert body["tax_estimate_applied"] is False
