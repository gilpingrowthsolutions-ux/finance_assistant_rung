"""Disposable browser fixture for the Overview setup-resume qualification."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import (NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
                 REQUIRED_EXPENSE_REVIEWED, SAFE_BUFFER_SETTING_KEY, app)
from extensions import db
from models import Account, Bill, Household, HouseholdMembership, IncomePlanVersion, User, UserPreference, UserSetting


def household(email: str, *, payday: bool, review: str | None, grocery: bool, transport: bool) -> None:
    now = datetime.now(timezone.utc)
    row = Household(); db.session.add(row); db.session.flush()
    db.session.add(Account(household_id=row.id, checking_balance=1200, pay_period_days=14, is_onboarded=True))
    user = User(email=email, password_hash=generate_password_hash("sts-pass-123"), active=True)
    db.session.add(user); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=row.id, role="owner", active=True))
    settings = [
        UserSetting(household_id=row.id, key=PYF_TARGET_SETTING_KEY, value="10"),
        UserSetting(household_id=row.id, key=SAFE_BUFFER_SETTING_KEY, value="80"),
    ]
    if payday:
        settings.append(UserSetting(household_id=row.id, key=NEXT_PAYDAY_SETTING_KEY, value=(now + timedelta(days=14)).date().isoformat()))
    if review:
        settings.append(UserSetting(household_id=row.id, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=review))
    db.session.add_all(settings)
    db.session.add(IncomePlanVersion(household_id=row.id, operation_id="setup-matrix-" + email,
                                     expected_income_cents=120000, effective_at=now - timedelta(days=1), source="test_setup"))
    if grocery:
        db.session.add(UserPreference(household_id=row.id, key="baseline_grocery_cost", value="100.00"))
    if transport:
        db.session.add(Bill(household_id=row.id, name="Required transport", amount=50, due_date=now + timedelta(days=3), is_gas_estimate=True, is_paid=False))


with app.app_context():
    db.create_all()
    household("matrix-payday@example.com", payday=False, review="no_expenses_reviewed", grocery=False, transport=False)
    household("matrix-expenses@example.com", payday=True, review=None, grocery=False, transport=False)
    household("matrix-grocery@example.com", payday=True, review=REQUIRED_EXPENSE_REVIEWED, grocery=False, transport=True)
    household("matrix-transport@example.com", payday=True, review=REQUIRED_EXPENSE_REVIEWED, grocery=True, transport=False)
    household("matrix-multi@example.com", payday=True, review=REQUIRED_EXPENSE_REVIEWED, grocery=False, transport=False)
    db.session.commit()
