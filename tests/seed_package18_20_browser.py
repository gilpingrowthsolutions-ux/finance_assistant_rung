"""Seed an explicitly disposable Package 18/20 browser-acceptance database."""

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import (
    LOCATION_SHARING_SETTING_KEY,
    NEXT_PAYDAY_SETTING_KEY,
    PYF_TARGET_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY,
    _save_household_shopping_defaults,
    app,
)
from extensions import db
from models import Account, Bill, ExpenseTransaction, HouseholdMembership, User, UserPreference, UserSetting
from services.household_context import household_id


with app.app_context():
    db.create_all()
    hid = household_id()
    account = Account(
        household_id=hid,
        checking_balance=1750.0,
        pay_period_days=14,
        expected_paycheck=2000.0,
        food_allocation_pct=99.0,
        is_onboarded=True,
        zip_code="65084",
        city_state="Versailles, MO",
        latitude=38.4314,
        longitude=-92.8410,
    )
    db.session.add(account)
    db.session.flush()
    user = User(
        email="browser@example.com",
        password_hash=generate_password_hash("browser-pass-123"),
        active=True,
        auth_version=1,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    db.session.add_all([
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="12.5"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="150.00"),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY, value=(datetime.now(timezone.utc).date() + timedelta(days=6)).isoformat()),
        UserSetting(household_id=hid, key=LOCATION_SHARING_SETTING_KEY, value="true"),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="240.00"),
        UserPreference(household_id=hid, key="baseline_fuel_cost", value="75.00"),
        Bill(household_id=hid, name="Rent", amount=300.0, due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=False),
        Bill(household_id=hid, name="Required fuel", amount=75.0, due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=True),
        ExpenseTransaction(household_id=hid, description="Established payday", amount=2000.0, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc) - timedelta(days=8)),
    ])
    ok, errors = _save_household_shopping_defaults({
        "shopping_style": "save_most",
        "preferences": {"milk_type": "whole", "bread_type": "wheat"},
    }, commit=False)
    assert ok, errors
    db.session.commit()
    print(f"seeded Package 18/20 household={hid} account={account.id} user={user.email}")
