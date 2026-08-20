from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, UserPreference, UserSetting
from services.financial_state import apply_balance_delta, set_balance_absolute
from services.household_context import household_id
from services.pyf_financial_state import calculate_pyf_snapshot


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = household_id()
        account = Account(household_id=hid, checking_balance=1200.0, expected_paycheck=1000.0, pay_period_days=14)
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
            UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100.00"),
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="200.00"),
            Bill(household_id=hid, name="Rent", amount=300.0, due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=False),
            Bill(household_id=hid, name="Required fuel", amount=100.0, due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=True),
            ExpenseTransaction(household_id=hid, description="Established payday", amount=1000.0, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc) - timedelta(days=5)),
        ])
        db.session.commit()
    yield app.test_client()


def safe(client) -> dict:
    response = client.get("/api/budget/summary")
    assert response.status_code == 200
    return (response.get_json() or {}).get("safe_to_spend") or {}


def test_full_target_feasible(client):
    state = safe(client)
    assert state["authority"] == "canonical_pyf_v1"
    assert state["feasibility"] == "full_target_feasible"
    assert state["needs_total_cents"] == 60000
    assert state["target_savings_cents"] == 20000
    assert state["feasible_savings_cents"] == 20000
    assert state["savings_shortfall_cents"] == 0
    assert state["safe_to_spend_cents"] == 30000


def test_partial_target_feasible_and_target_preserved(client):
    with app.app_context():
        set_balance_absolute(household_id(), 800.0)
        db.session.commit()
    state = safe(client)
    assert state["feasibility"] == "partial_target_feasible"
    assert state["long_term_savings_target_percent"] == 20.0
    assert state["feasible_savings_cents"] == 10000
    assert state["savings_shortfall_cents"] == 10000
    assert state["safe_to_spend_cents"] == 0
    assert client.get("/api/settings/pay-yourself-first").get_json()["long_term_savings_target_percent"] == 20.0


def test_no_contribution_feasible_without_spending_needs_or_buffer(client):
    with app.app_context():
        set_balance_absolute(household_id(), 700.0)
        db.session.commit()
    state = safe(client)
    assert state["feasibility"] == "no_contribution_feasible"
    assert state["feasible_savings_cents"] == 0
    assert state["safe_to_spend_cents"] == 0
    assert state["protected_buffer_cents"] == 10000


def test_discretionary_expense_recalculates_exactly(client):
    before = safe(client)
    response = client.post("/api/transactions", json={"description": "Coffee", "amount": 25.25, "category": "discretionary"})
    assert response.status_code == 200
    after = safe(client)
    assert after["safe_to_spend_cents"] - before["safe_to_spend_cents"] == -2525


def test_income_increases_feasibility_without_changing_target(client):
    with app.app_context():
        set_balance_absolute(household_id(), 800.0)
        db.session.commit()
    before = safe(client)
    with app.app_context():
        apply_balance_delta(household_id(), 75.0)
        db.session.add(ExpenseTransaction(household_id=household_id(), description="Income", amount=75.0, category="income", source="manual", date=datetime.now(timezone.utc)))
        db.session.commit()
    after = safe(client)
    assert before["feasible_savings_cents"] == 10000
    assert after["feasible_savings_cents"] == 17500
    assert after["target_savings_cents"] == before["target_savings_cents"] == 20000


def test_finished_shopping_realizes_need_once(client):
    before = safe(client)
    stage = client.post("/api/grocery/finished-shopping/stage", json={"planned_total": 100.0, "actual_total": 100.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "pyf-trip"})
    op_id = (stage.get_json() or {})["operation_id"]
    payload = {"planned_total": 100.0, "actual_total": 100.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "pyf-trip", "operation_id": op_id, "confirm": True}
    first = client.post("/api/grocery/finished-shopping/complete", json=payload)
    second = client.post("/api/grocery/finished-shopping/complete", json=payload)
    assert first.status_code == second.status_code == 200
    assert (second.get_json() or {})["already_completed"] is True
    after = safe(client)
    assert after["checking_cents"] == before["checking_cents"] - 10000
    assert after["needs_total_cents"] == before["needs_total_cents"] - 10000
    assert after["safe_to_spend_cents"] == before["safe_to_spend_cents"]


def test_direct_balance_correction_recalculates_without_fake_expense(client):
    with app.app_context():
        count = ExpenseTransaction.query.count()
    response = client.post("/api/account/update", json={"checking_balance": 1400.0})
    assert response.status_code == 200
    assert safe(client)["safe_to_spend_cents"] == 50000
    with app.app_context():
        assert ExpenseTransaction.query.count() == count


def test_missing_setup_is_truthful(client):
    with app.app_context():
        UserSetting.query.filter_by(key=PYF_TARGET_SETTING_KEY).delete()
        db.session.commit()
    state = safe(client)
    assert state["state"] == "needs_setup"
    assert state["safe_to_spend"] is None
    assert "long_term_savings_target_percent" in state["missing_setup"]


def test_legacy_ratios_and_legacy_metrics_cannot_control_pyf(client):
    baseline = safe(client)
    with app.app_context():
        account = Account.query.one()
        account.food_allocation_pct = 99.0
        account.meals_per_day = 9
        db.session.commit()
    changed = safe(client)
    assert changed["safe_to_spend_cents"] == baseline["safe_to_spend_cents"]
    assert changed["needs_total_cents"] == baseline["needs_total_cents"]


def test_no_arbitrary_target_max_and_cent_boundary():
    state = calculate_pyf_snapshot(
        checking_cents=10001,
        period_income_cents=3333,
        savings_target_percent="125.5",
        protected_buffer_cents=1000,
        needs=[{"key": "need", "amount_cents": 5000}],
    )
    assert state["target_savings_cents"] == 4183
    assert state["feasible_savings_cents"] == 4001
    assert state["savings_shortfall_cents"] == 182
    assert state["safe_to_spend_cents"] == 0
    assert state["checks"]["cent_accurate"] is True
