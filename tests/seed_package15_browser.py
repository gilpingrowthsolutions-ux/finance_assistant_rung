"""Seed only the explicit disposable Package 15 browser database."""
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, HouseholdMembership, SavingsDestination, SavingsTransfer, User, UserPreference, UserSetting
from services.household_context import household_id

with app.app_context():
    db.create_all()
    hid = household_id()
    now = datetime.now(timezone.utc)
    account = Account(household_id=hid, checking_balance=1650, pay_period_days=14, expected_paycheck=1000, is_onboarded=True)
    db.session.add(account); db.session.flush()
    user = User(email="timeline-browser@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True)
    db.session.add(user); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    reserve = SavingsDestination(household_id=hid, kind="reserve", name="Emergency Reserve", priority=1, active=True)
    db.session.add(reserve); db.session.flush()
    db.session.add_all([
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100"),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY, value=(now.date()+timedelta(days=7)).isoformat()),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="150"),
        Bill(household_id=hid, name="Rent", amount=950, due_date=now-timedelta(days=2), is_paid=True),
        Bill(household_id=hid, name="Electric", amount=80, due_date=now+timedelta(days=3), is_paid=False),
        Bill(household_id=hid, name="Required fuel", amount=50, due_date=now+timedelta(days=4), is_paid=False, is_gas_estimate=True),
        ExpenseTransaction(household_id=hid, description="Paycheck", amount=1000, category="income", source="manual", local_account_id=account.id, date=now-timedelta(days=7)),
        ExpenseTransaction(household_id=hid, description="Rent payment", amount=900, category="housing", source="manual", plaid_transaction_id="linked-rent", local_account_id=account.id, date=now-timedelta(days=2)),
        ExpenseTransaction(household_id=hid, description="Confirmed bonus", amount=25, category="income", source="manual", local_account_id=account.id, date=now+timedelta(days=2)),
        ExpenseTransaction(household_id=hid, description="Coffee", amount=5, category="discretionary", source="manual", local_account_id=account.id, date=now-timedelta(days=1)),
        SavingsTransfer(household_id=hid, operation_id="browser-pyf", destination_id=reserve.id, amount_cents=20000, transfer_type="pyf_allocation", created_at=now-timedelta(days=6)),
    ])
    db.session.commit()
    print("seeded", hid)
