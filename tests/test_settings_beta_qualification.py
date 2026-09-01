"""Focused Settings beta-qualification authority regressions."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest
from werkzeug.security import generate_password_hash

from app import (LOCATION_SHARING_SETTING_KEY, NEXT_PAYDAY_SETTING_KEY,
                 PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app)
from extensions import db
from models import (Account, ExpenseTransaction, Household, HouseholdMembership,
                    IncomePlanVersion, ShoppingCart, User, UserSetting)
from services.income_plan import resolve_income_plan


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    monkeypatch.setenv("RUNG_ENV", "beta")
    app.testing = True
    with app.app_context():
        db.drop_all(); db.create_all()
    yield


def seed_households():
    now = datetime.now(timezone.utc)
    with app.app_context():
        rows = []
        for suffix, balance, income in (("a", 2000.0, 100000), ("b", 900.0, 70000)):
            household = Household(legacy_scope_key=f"settings-beta-{suffix}")
            db.session.add(household); db.session.flush()
            db.session.add(Account(household_id=household.id, checking_balance=balance, pay_period_days=14))
            db.session.add_all([
                IncomePlanVersion(household_id=household.id, operation_id=f"historic-{suffix}", expected_income_cents=90000, effective_at=now-timedelta(days=30), source="fixture"),
                IncomePlanVersion(household_id=household.id, operation_id=f"current-{suffix}", expected_income_cents=income, effective_at=now-timedelta(days=7), source="fixture"),
                UserSetting(household_id=household.id, key=NEXT_PAYDAY_SETTING_KEY, value=(now.date()+timedelta(days=7)).isoformat()),
                UserSetting(household_id=household.id, key=PYF_TARGET_SETTING_KEY, value="10"),
                UserSetting(household_id=household.id, key=SAFE_BUFFER_SETTING_KEY, value="100.00"),
                UserSetting(household_id=household.id, key="onboarding_required_expense_review", value="no_expenses_reviewed"),
            ])
            user = User(email=f"settings-{suffix}@example.com", password_hash=generate_password_hash(f"pass-{suffix}-123"), active=True, auth_version=1)
            db.session.add(user); db.session.flush()
            db.session.add(HouseholdMembership(user_id=user.id, household_id=household.id, role="owner", active=True))
            rows.append((household, user))
        db.session.commit()
        return [(household.id, user.id) for household, user in rows]


def login(client, suffix):
    response = client.post("/api/auth/login", json={"email": f"settings-{suffix}@example.com", "password": f"pass-{suffix}-123"})
    assert response.status_code == 200


def test_settings_financial_defaults_preserve_history_and_current_cycle():
    (a_id, _), _ = seed_households(); client = app.test_client(); login(client, "a")
    before = client.get("/api/budget/summary").get_json()["safe_to_spend"]
    changed = client.post("/api/account/update", json={"expected_paycheck": 1250, "expected_paycheck_operation_id": "settings-income-a"})
    assert changed.status_code == 200 and changed.get_json()["income_plan_created"] is True
    replay = client.post("/api/account/update", json={"expected_paycheck": 1250, "expected_paycheck_operation_id": "settings-income-a"})
    assert replay.status_code == 200 and replay.get_json()["income_plan_created"] is False
    after = client.get("/api/budget/summary").get_json()["safe_to_spend"]
    assert before["period_income_cents"] == after["period_income_cents"] == 100000
    assert before["safe_to_spend_cents"] == after["safe_to_spend_cents"]
    with app.app_context():
        plans = IncomePlanVersion.query.filter_by(household_id=a_id).order_by(IncomePlanVersion.id).all()
        assert [p.expected_income_cents for p in plans] == [90000, 100000, 125000]
        assert resolve_income_plan(a_id, at=datetime.now(timezone.utc)).expected_income_cents == 100000
        assert resolve_income_plan(a_id, at=datetime.now(timezone.utc)+timedelta(days=8)).expected_income_cents == 125000


def test_settings_validation_and_simple_setters_have_no_economic_effect():
    seed_households(); client = app.test_client(); login(client, "a")
    assert client.post("/api/account/update", json={"pay_period_days": 0}).status_code == 400
    assert client.post("/api/account/update", json={"pay_period_days": "14.5"}).status_code == 400
    assert client.post("/api/settings/pay-yourself-first", json={"long_term_savings_target_percent": -1}).status_code == 400
    assert client.post("/api/settings/safe-to-spend", json={"protected_buffer": "bad"}).status_code == 400
    before = client.get("/api/budget/summary").get_json()["safe_to_spend"]
    assert client.post("/api/settings/pay-yourself-first", json={"long_term_savings_target_percent": 25}).status_code == 200
    assert client.post("/api/settings/safe-to-spend", json={"protected_buffer": 225.25}).status_code == 200
    after = client.get("/api/budget/summary").get_json()["safe_to_spend"]
    assert float(after["long_term_savings_target_percent"]) == 25.0 and after["protected_buffer_cents"] == 22525
    assert after["safe_to_spend_cents"] < before["safe_to_spend_cents"]
    with app.app_context():
        assert ExpenseTransaction.query.count() == 0 and ShoppingCart.query.count() == 0


def test_settings_defaults_location_and_reads_are_household_scoped_and_store_safe():
    seed_households(); client = app.test_client(); login(client, "a")
    assert client.post("/api/settings/household-shopping-defaults", json={"shopping_style": "save_most", "preferences": {"milk_type": "whole"}}).status_code == 200
    assert client.post("/api/settings/location-sharing", json={"location_sharing_enabled": True}).status_code == 200
    a_location = client.get("/api/settings/current-location").get_json()
    assert a_location["selected_store"]["store_id"] == "" and a_location["location_sharing_enabled"] is True
    client.post("/api/auth/logout"); login(client, "b")
    assert client.get("/api/settings/household-shopping-defaults").get_json()["preferences"] == {}
    assert client.get("/api/settings/location-sharing").get_json()["location_sharing_enabled"] is False
    assert client.post("/api/settings/location-sharing", json={"location_sharing_enabled": "yes"}).status_code == 400
    client.post("/api/auth/logout"); login(client, "a")
    assert client.get("/api/settings/household-shopping-defaults").get_json()["preferences"]["milk_type"] == "whole"
    assert client.get("/api/settings/location-sharing").get_json()["location_sharing_enabled"] is True
