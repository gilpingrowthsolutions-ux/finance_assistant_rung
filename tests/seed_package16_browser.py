"""Seed only the explicit disposable Package 16 browser database."""
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, HouseholdMembership, SavingsDestination, User, UserPreference, UserSetting
from services.household_context import household_id

with app.app_context():
    db.create_all()
    hid = household_id(); now = datetime.now(timezone.utc)
    account = Account(household_id=hid, checking_balance=2400, pay_period_days=14, expected_paycheck=1800, is_onboarded=True)
    db.session.add(account); db.session.flush()
    user = User(email="behavior-browser@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True)
    db.session.add(user); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    db.session.add_all([
        SavingsDestination(household_id=hid, kind="flexible", name="Flexible Savings", priority=1000),
        SavingsDestination(household_id=hid, kind="wealth_cash", name="Wealth Cash", priority=1100),
        SavingsDestination(household_id=hid, kind="wealth_investment", name="Investments", priority=1200),
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100"),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY, value=(now.date()+timedelta(days=7)).isoformat()),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="150"),
        Bill(household_id=hid, name="Rent", amount=900, due_date=now+timedelta(days=3)),
    ])
    identifier = 0
    def add(description, amount, category, days, plaid=None):
        global identifier
        identifier += 1
        db.session.add(ExpenseTransaction(household_id=hid, description=description, amount=amount, category=category, source="manual", plaid_transaction_id=plaid, local_account_id=account.id, date=now-timedelta(days=days)))
    add("Paycheck", 1800, "income", 8)
    for day, name in [(62,"PLANET FITNESS #100"),(31,"Planet Fitness Club 200"),(1,"PLANET FITNESS*300")]: add(name,15,"subscription",day,"linked-planet" if day==31 else None)
    for day, name in [(63,"STREAM BOX #100"),(33,"Stream Box 200"),(3,"STREAM BOX*300")]: add(name,12,"subscription",day)
    for day in (70,48,5): add("Coffee House",30,"coffee",day)
    for day in (60,30,0): add("Essential Prescription",75,"medical",day)
    for day in (59,29,1): add("Emergency Reserve Transfer",200,"reserve",day)
    db.session.commit()
    print("seeded", hid)
