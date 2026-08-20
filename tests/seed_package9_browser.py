from datetime import datetime, timedelta, timezone

from app import PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, UserPreference, UserSetting
from services.household_context import household_id

with app.app_context():
    db.create_all()
    hid = household_id()
    account = Account(household_id=hid, checking_balance=1200.0, expected_paycheck=1000.0, pay_period_days=14, food_allocation_pct=99.0)
    db.session.add(account)
    db.session.flush()
    db.session.add_all([
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100.00"),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="200.00"),
        Bill(household_id=hid, name="Rent", amount=300.0, due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=False),
        Bill(household_id=hid, name="Required fuel", amount=100.0, due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=True),
        ExpenseTransaction(household_id=hid, description="Established payday", amount=1000.0, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc) - timedelta(days=5)),
    ])
    db.session.commit()
    print(f"seeded household={hid} account={account.id} balance={account.checking_balance}")
