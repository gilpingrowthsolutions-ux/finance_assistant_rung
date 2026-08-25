"""Seed only the explicit disposable Tax Coverage browser database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

from app import PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, HouseholdMembership, IncomePlanVersion, User, UserPreference, UserSetting
from services.household_context import household_id
from services.selected_store import select_store
from services.tax_adapters import MissouriDorQ3Adapter
from services.tax_engine import import_dataset_atomic

with app.app_context():
    db.create_all()
    hid = household_id()
    now = datetime.now(timezone.utc)
    account = Account(household_id=hid, checking_balance=2000.0, pay_period_days=14, is_onboarded=True, zip_code="65084", city_state="Versailles, MO")
    db.session.add(account); db.session.flush()
    user = User(email="tax-browser@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True)
    db.session.add(user); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    db.session.add_all([
        IncomePlanVersion(household_id=hid, operation_id="tax-browser-plan", expected_income_cents=100000, effective_at=now - timedelta(days=30), source="test_confirmation"),
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="0"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100"),
        UserPreference(household_id=hid, key="baseline_grocery_cost", value="200"),
        Bill(household_id=hid, name="Required utility", amount=50, due_date=now + timedelta(days=4), is_paid=False),
        Bill(household_id=hid, name="Fuel plan", amount=40, due_date=now + timedelta(days=4), is_gas_estimate=True, is_paid=False),
        ExpenseTransaction(household_id=hid, description="Paycheck", amount=1000, category="income", source="manual", local_account_id=account.id, date=now - timedelta(days=3)),
    ])
    select_store(hid, retailer="walmart", store_id="357", store_name="Walmart — Versailles", address="1003 W Newton St, Versailles, MO 65084", city="Versailles", state="MO", postal_code="65084", account=account)
    imported = import_dataset_atomic(adapter=MissouriDorQ3Adapter(), source_path=str(Path.cwd() / "data/tax/official/missouri"), activate=True)
    if not imported.get("ok"):
        raise RuntimeError(imported)
    db.session.commit()
    print("seeded", hid)
