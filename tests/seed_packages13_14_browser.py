"""Seed only an explicitly selected disposable Packages 13-14 browser DB."""
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash
from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, HouseholdMembership, User, UserPreference, UserSetting
from services.household_context import household_id

with app.app_context():
    db.create_all(); hid = household_id()
    account = Account(household_id=hid, checking_balance=1750, pay_period_days=14, expected_paycheck=2000, is_onboarded=True)
    db.session.add(account); db.session.flush()
    user = User(email="savings-browser@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True)
    db.session.add(user); db.session.flush(); db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    db.session.add_all([
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="12.5"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="150"),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY, value=(datetime.now(timezone.utc).date()+timedelta(days=7)).isoformat()),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="240"), UserPreference(household_id=hid, key="baseline_fuel_cost", value="75"),
        Bill(household_id=hid, name="Rent", amount=300, due_date=datetime.now(timezone.utc)+timedelta(days=3), is_paid=False),
        Bill(household_id=hid, name="Required fuel", amount=75, due_date=datetime.now(timezone.utc)+timedelta(days=3), is_paid=False, is_gas_estimate=True),
        ExpenseTransaction(household_id=hid, description="Payday", amount=2000, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc)-timedelta(days=7)),
    ]); db.session.commit(); print("seeded", hid)
