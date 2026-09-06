"""Fresh deterministic provider observations for served transfer-review acceptance."""
from datetime import date
from werkzeug.security import generate_password_hash
from app import app
from extensions import db
from models import (Account, HouseholdMembership, PlaidItem, PlaidTransaction,
                    SavingsDestination, SavingsTransfer, SavingsTransferReconciliation, User)
from services.household_context import household_id

with app.app_context():
    db.create_all(); hid = household_id()
    account = Account(household_id=hid, checking_balance=640, pay_period_days=14, is_onboarded=True)
    user = User(email='transfer-browser@example.com', password_hash=generate_password_hash('browser-pass-123'), active=True)
    db.session.add_all([account, user]); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role='owner', active=True))
    dest = SavingsDestination(household_id=hid, kind='reserve', name='Browser savings', priority=1)
    item = PlaidItem(household_id=hid, owner_scope='anonymous', plaid_item_id='fixture-transfer-item', access_token_encrypted='fixture')
    db.session.add_all([dest, item]); db.session.flush()
    owner_scope = f'user:{user.id}'
    for suffix, status in [('match', 'proposed'), ('separate', 'proposed')]:
        transfer = SavingsTransfer(household_id=hid, operation_id='manual-' + suffix, destination_id=dest.id,
            amount_cents=18000, transfer_type='deposit', purpose='Browser ' + suffix,
            economic_date=date.today(), external_direction='outflow')
        plaid_id = 'fixture-transfer-' + suffix
        bank = PlaidTransaction(household_id=hid, owner_scope=owner_scope, plaid_item_id=item.id,
            plaid_transaction_id=plaid_id, plaid_account_id='fixture-checking', amount_cents=18000,
            signed_amount_cents=-18000, direction='outflow', name='TRANSFER TO SAVINGS',
            merchant_name='TRANSFER TO SAVINGS', description='TRANSFER TO SAVINGS', transaction_date=date.today())
        db.session.add_all([transfer, bank]); db.session.flush()
        db.session.add(SavingsTransferReconciliation(household_id=hid, owner_scope=owner_scope,
            savings_transfer_id=transfer.id, plaid_transaction_id=plaid_id, status=status))
    db.session.commit()
