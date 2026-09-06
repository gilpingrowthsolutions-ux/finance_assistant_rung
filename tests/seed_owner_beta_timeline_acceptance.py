"""Seed the explicit disposable owner-beta timeline acceptance database.

Canonical Safe-to-Spend arithmetic:
checking $1,800.57 - Needs $280.00 - buffer $80.00 - feasible PYF $180.00
= $1,260.57.  The expected $1,800 paycheck is two calendar days ahead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import (
    NEXT_PAYDAY_SETTING_KEY,
    PYF_TARGET_SETTING_KEY,
    REQUIRED_EXPENSE_REVIEWED,
    REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY,
    app,
)
from extensions import db
from models import Account, Bill, HouseholdMembership, IncomePlanVersion, User, UserPreference, UserSetting
from services.household_context import household_id


with app.app_context():
    db.create_all()
    now = datetime.now(timezone.utc)
    hid = household_id()
    account = Account(household_id=hid, checking_balance=1800.57, pay_period_days=14, is_onboarded=True)
    db.session.add(account)
    db.session.flush()
    user = User(email="owner-beta-timeline@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True)
    db.session.add(user)
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    db.session.add_all([
        IncomePlanVersion(household_id=hid, operation_id="owner-beta-income", expected_income_cents=180000,
                          effective_at=now - timedelta(days=1), source="test_setup"),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY, value=(now.date() + timedelta(days=2)).isoformat()),
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="10"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="80"),
        UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="0"),
        Bill(household_id=hid, name="Required transportation", amount=280,
             due_date=now + timedelta(days=1), is_gas_estimate=True),
    ])
    db.session.commit()
    print("seeded", hid)
