"""Fresh two-cycle PYF state for served Money/Savings acceptance."""
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import (app, NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY,
                 REQUIRED_EXPENSE_REVIEW_SETTING_KEY, REQUIRED_EXPENSE_NONE,
                 SAFE_BUFFER_SETTING_KEY)
from extensions import db
from models import (Account, ExpenseTransaction, HouseholdMembership,
                    IncomePlanVersion,
                    IncomePyfProtection, SavingsDestination, User, UserPreference,
                    UserSetting)
from services.household_context import household_id

with app.app_context():
    db.create_all()
    hid = household_id()
    now = datetime.now(timezone.utc)
    account = Account(household_id=hid, checking_balance=1800, pay_period_days=14,
                      expected_paycheck=1800, is_onboarded=True)
    user = User(email='two-cycle-browser@example.com',
                password_hash=generate_password_hash('browser-pass-123'), active=True)
    db.session.add_all([account, user]); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid,
                                       role='owner', active=True))
    old_income = ExpenseTransaction(
        household_id=hid, description='Prior-cycle payroll', amount=1800,
        category='income', source='manual', local_account_id=account.id,
        date=now - timedelta(days=20))
    destination = SavingsDestination(household_id=hid, kind='reserve',
                                     name='Emergency savings', priority=1)
    db.session.add_all([old_income, destination]); db.session.flush()
    db.session.add(IncomePlanVersion(
        household_id=hid, operation_id='fixture-current-income-plan',
        expected_income_cents=180000, effective_at=now - timedelta(days=1),
        source='fixture'))
    db.session.add(IncomePyfProtection(
        household_id=hid, income_transaction_id=old_income.id,
        operation_id='fixture-prior-cycle-pyf', target_percent=10,
        target_cents=18000, protected_cents=18000, status='active'))
    db.session.add_all([
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value='10'),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value='0'),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY,
                    value=(now.date() + timedelta(days=13)).isoformat()),
        UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
                    value=REQUIRED_EXPENSE_NONE),
        UserPreference(household_id=hid, key='baseline_grocery_cost', value='0'),
    ])
    db.session.commit()
