"""Seed a disposable owner-like financial state for Overview layout acceptance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import (
    NEXT_PAYDAY_SETTING_KEY,
    PYF_TARGET_SETTING_KEY,
    REQUIRED_EXPENSE_NONE,
    REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY,
    app,
)
from extensions import db
from models import Account, Household, HouseholdMembership, IncomePlanVersion, User, UserSetting


with app.app_context():
    db.create_all()
    now = datetime.now(timezone.utc)
    household = Household()
    db.session.add(household)
    db.session.flush()
    account = Account(
        household_id=household.id,
        checking_balance=1250.00,
        pay_period_days=14,
        is_onboarded=True,
    )
    user = User(email="owner-beta-overview@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True)
    db.session.add_all([account, user])
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=household.id, role="owner", active=True))
    db.session.add_all([
        IncomePlanVersion(
            household_id=household.id,
            operation_id="owner-beta-overview-income",
            expected_income_cents=120000,
            effective_at=now - timedelta(days=1),
            source="test_setup",
        ),
        UserSetting(household_id=household.id, key=NEXT_PAYDAY_SETTING_KEY, value=(now + timedelta(days=11)).date().isoformat()),
        UserSetting(household_id=household.id, key=PYF_TARGET_SETTING_KEY, value="10"),
        UserSetting(household_id=household.id, key=SAFE_BUFFER_SETTING_KEY, value="80.00"),
        UserSetting(household_id=household.id, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_NONE),
    ])
    db.session.commit()
