"""Seed only an explicitly selected disposable Packages 13-14 browser DB."""
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash
from app import (NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY,
                 REQUIRED_EXPENSE_REVIEWED, REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
                 SAFE_BUFFER_SETTING_KEY, app)
from extensions import db
from models import (Account, Bill, ExpenseTransaction, Household,
                    HouseholdMembership, IncomePlanVersion, User,
                    UserPreference, UserSetting)
from services.household_context import household_id
from services.savings_allocation import create_goal, create_reserve

with app.app_context():
    db.create_all(); hid = household_id()
    account = Account(household_id=hid, checking_balance=1750, pay_period_days=14, expected_paycheck=2000, is_onboarded=True)
    db.session.add(account); db.session.flush()
    user = User(email="recap-browser@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True)
    db.session.add(user); db.session.flush(); db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    db.session.add_all([
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="150"),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY, value=(datetime.now(timezone.utc).date()+timedelta(days=7)).isoformat()),
        UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
        IncomePlanVersion(household_id=hid, operation_id="browser-current-plan", expected_income_cents=2000_00,
                          effective_at=datetime.now(timezone.utc)-timedelta(days=1), source="test_setup"),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="240"), UserPreference(household_id=hid, key="baseline_fuel_cost", value="75"),
        Bill(household_id=hid, name="Rent", amount=300, due_date=datetime.now(timezone.utc)+timedelta(days=3), is_paid=False),
        Bill(household_id=hid, name="Required fuel", amount=75, due_date=datetime.now(timezone.utc)+timedelta(days=3), is_paid=False, is_gas_estimate=True),
        ExpenseTransaction(household_id=hid, description="Payday", amount=2000, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc)-timedelta(days=7)),
    ]); db.session.commit()
    create_reserve(hid, operation_id="browser-emergency-reserve", name="Emergency Reserve", category="emergency", target_cents=100_00, priority=0)
    create_goal(hid, operation_id="browser-allocation-goal", name="Allocation Completion Goal", target_cents=300_00,
                target_date=None, priority=1)
    second = Household(public_id="browser-household-b", legacy_scope_key="browser-household-b")
    db.session.add(second); db.session.flush()
    second_account = Account(household_id=second.id, checking_balance=321, pay_period_days=14, expected_paycheck=600, is_onboarded=True)
    second_user = User(email="savings-browser-b@example.com", password_hash=generate_password_hash("browser-pass-b"), active=True)
    db.session.add_all([second_account, second_user]); db.session.flush()
    db.session.add_all([
        HouseholdMembership(user_id=second_user.id, household_id=second.id, role="owner", active=True),
        UserSetting(household_id=second.id, key=PYF_TARGET_SETTING_KEY, value="10"),
        UserSetting(household_id=second.id, key=SAFE_BUFFER_SETTING_KEY, value="25"),
        UserSetting(household_id=second.id, key=NEXT_PAYDAY_SETTING_KEY, value=(datetime.now(timezone.utc).date()+timedelta(days=7)).isoformat()),
        UserSetting(household_id=second.id, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
        IncomePlanVersion(household_id=second.id, operation_id="browser-b-current-plan", expected_income_cents=600_00,
                          effective_at=datetime.now(timezone.utc)-timedelta(days=1), source="test_setup"),
    ]); db.session.commit()
    create_reserve(second.id, operation_id="browser-b-reserve", name="B-only Reserve", category="medical", target_cents=90_00, priority=0)
    print("seeded", hid)
