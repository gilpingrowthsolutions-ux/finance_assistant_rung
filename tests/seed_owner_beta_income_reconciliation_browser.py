"""Fresh ambiguous manual/Plaid income staged through provider projection."""
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from app import PYF_TARGET_SETTING_KEY, app
from extensions import db
from models import (Account, ExpenseTransaction, HouseholdMembership, PlaidItem,
                    PlaidTransaction, User, UserSetting)
from services.household_context import household_id
from services.income_pyf import establish_for_income
from services.transaction_reconciliation import project_plaid_transactions


with app.app_context():
    db.create_all()
    hid = household_id()
    account = Account(household_id=hid, checking_balance=1000, pay_period_days=14,
                      is_onboarded=True)
    user = User(email='income-review-browser@example.com',
                password_hash=generate_password_hash('browser-pass-123'), active=True)
    db.session.add_all([account, user])
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid,
                                       role='owner', active=True))
    db.session.add(UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value='10'))
    item = PlaidItem(household_id=hid, owner_scope=f'user:{user.id}',
                     plaid_item_id='income-review-fixture', access_token_encrypted='fixture')
    db.session.add(item)
    db.session.flush()

    now = datetime.now(timezone.utc)
    for suffix, amount in (('match', 200), ('separate', 150)):
        manual = ExpenseTransaction(household_id=hid, local_account_id=account.id,
                                    description='Acme payroll ' + suffix, amount=amount,
                                    category='income', source='manual', date=now)
        db.session.add(manual)
        db.session.flush()
        account.checking_balance += amount
        establish_for_income(manual)
        db.session.add(PlaidTransaction(
            household_id=hid, owner_scope=f'user:{user.id}', plaid_item_id=item.id,
            plaid_transaction_id='income-review-' + suffix,
            plaid_account_id='income-review-checking', amount_cents=amount * 100,
            signed_amount_cents=amount * 100, direction='inflow', name='ACME PAYROLL',
            merchant_name='ACME PAYROLL', description='ACME PAYROLL',
            transaction_date=now.date(), is_pending=False, is_removed=False,
            is_active_event=True,
        ))
    db.session.commit()
    # This is the production provider projection/reconciliation authority. It
    # stages both pairs; no final decision or provider financial effect is seeded.
    project_plaid_transactions(f'user:{user.id}', item.id)
