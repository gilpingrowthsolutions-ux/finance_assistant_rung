"""Fresh manual-first state for served physical-PYF transfer recording."""
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash
from app import app, NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY
from extensions import db
from models import Account, ExpenseTransaction, HouseholdMembership, IncomePyfProtection, SavingsDestination, User, UserSetting
from services.household_context import household_id

with app.app_context():
    db.create_all(); hid=household_id(); now=datetime.now(timezone.utc)
    account=Account(household_id=hid, checking_balance=3600, pay_period_days=14, is_onboarded=True)
    user=User(email='physical-transfer-browser@example.com', password_hash=generate_password_hash('browser-pass-123'), active=True)
    db.session.add_all([account,user]); db.session.flush(); db.session.add(HouseholdMembership(user_id=user.id,household_id=hid,role='owner',active=True))
    destination=SavingsDestination(household_id=hid,kind='reserve',name='Emergency savings',priority=1)
    income=ExpenseTransaction(household_id=hid,description='Fixture payroll',amount=1800,category='income',source='manual',local_account_id=account.id,date=now-timedelta(days=1))
    db.session.add_all([destination,income]); db.session.flush()
    db.session.add(IncomePyfProtection(household_id=hid,income_transaction_id=income.id,operation_id='fixture-income-pyf',target_percent=10,target_cents=18000,protected_cents=18000,status='active'))
    db.session.add_all([UserSetting(household_id=hid,key=PYF_TARGET_SETTING_KEY,value='10'),UserSetting(household_id=hid,key=SAFE_BUFFER_SETTING_KEY,value='0'),UserSetting(household_id=hid,key=NEXT_PAYDAY_SETTING_KEY,value=(now.date()+timedelta(days=13)).isoformat())]); db.session.commit()
