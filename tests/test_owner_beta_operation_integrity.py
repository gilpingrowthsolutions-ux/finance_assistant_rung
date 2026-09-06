"""Served API integrity checks for owner-beta money mutations."""
from datetime import datetime, timezone

import pytest

from app import app
from extensions import db
from models import Account, Bill, ExpenseTransaction, IncomePyfProtection, RecurringRequiredObligation, SavingsTransfer, UserSetting
from services.household_context import household_id
from services.recurring_needs import project_occurrences
from services.savings_allocation import create_reserve


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all(); db.create_all()
        hid = household_id()
        db.session.add_all([
            Account(household_id=hid, checking_balance=1800, pay_period_days=14),
            UserSetting(household_id=hid, key='pyf_long_term_target_percent', value='10'),
        ])
        db.session.commit()
    return app.test_client()


def test_manual_income_operation_is_exact_replay_with_one_income_balance_and_pyf(client):
    request = {'description': 'September payroll', 'amount': 1800, 'category': 'income', 'operation_id': 'income-op-1'}
    first = client.post('/api/transactions', json=request)
    replay = client.post('/api/transactions', json=request)
    changed = client.post('/api/transactions', json={**request, 'description': 'different payroll memo'})
    assert first.status_code == replay.status_code == 200
    assert replay.get_json()['already_logged'] is True
    assert changed.status_code == 409
    with app.app_context():
        hid = household_id()
        assert ExpenseTransaction.query.filter_by(household_id=hid, operation_id='income-op-1').count() == 1
        assert IncomePyfProtection.query.filter_by(household_id=hid).count() == 1
        assert Account.query.filter_by(household_id=hid).one().checking_balance == 3600


def test_physical_transfer_options_are_oldest_first_and_factually_distinguishable(client):
    with app.app_context():
        hid = household_id()
        older = ExpenseTransaction(household_id=hid, description='August payroll', amount=1800,
            category='income', source='manual', date=datetime(2026, 8, 21, tzinfo=timezone.utc))
        newer = ExpenseTransaction(household_id=hid, description='September payroll', amount=1800,
            category='income', source='manual', date=datetime(2026, 9, 4, tzinfo=timezone.utc))
        db.session.add_all([older, newer]); db.session.flush()
        old_protection = IncomePyfProtection(household_id=hid, income_transaction_id=older.id,
            operation_id='old-protection', target_percent=10, target_cents=18000, protected_cents=18000)
        new_protection = IncomePyfProtection(household_id=hid, income_transaction_id=newer.id,
            operation_id='new-protection', target_percent=10, target_cents=18000, protected_cents=18000)
        db.session.add_all([old_protection, new_protection]); db.session.commit()
        expected = [(old_protection.id, '2026-08-21', 'August payroll'),
                    (new_protection.id, '2026-09-04', 'September payroll')]
    options = client.get('/api/savings/state').get_json()['pyf_transfer_options']['protections']
    assert [(row['id'], row['income_date'], row['income_description']) for row in options] == expected
    assert [row['remaining_cents'] for row in options] == [18000, 18000]


def test_recurring_patch_rejects_ambiguous_boolean_and_changes_only_future_authority(client):
    with app.app_context():
        hid = household_id()
        row = RecurringRequiredObligation(household_id=hid, name='Rent', expected_amount_cents=120000,
            next_due_date=datetime(2026, 10, 1, tzinfo=timezone.utc), recurrence='monthly')
        db.session.add(row); db.session.commit(); row_id = row.id
    assert client.patch(f'/api/recurring-needs/{row_id}', json={'is_active': 'false'}).status_code == 400
    changed = client.patch(f'/api/recurring-needs/{row_id}', json={'expected_amount': '1250', 'next_due_date': '2026-11-01', 'recurrence': 'weekly', 'is_active': False})
    assert changed.status_code == 200
    assert client.patch(f'/api/recurring-needs/{row_id}', json={'recurrence': 'nonsense'}).status_code == 400
    with app.app_context():
        row = db.session.get(RecurringRequiredObligation, row_id)
        assert (row.expected_amount_cents, row.next_due_date.date().isoformat(), row.recurrence, row.is_active) == (125000, '2026-11-01', 'weekly', False)


def test_deleting_linked_bill_preserves_manageable_recurring_authority_and_future_only_forecast(client):
    """A Bill is one explicit occurrence, never an implicit command to end its repeat."""
    with app.app_context():
        hid = household_id()
        recurring = RecurringRequiredObligation(
            household_id=hid, name='Rent', expected_amount_cents=120000,
            next_due_date=datetime(2026, 10, 1, tzinfo=timezone.utc), recurrence='monthly', is_active=True,
        )
        db.session.add(recurring); db.session.flush()
        explicit = Bill(household_id=hid, name='Rent', amount=1200, due_date=datetime(2026, 10, 1, tzinfo=timezone.utc), recurring_obligation_id=recurring.id)
        settled = ExpenseTransaction(household_id=hid, description='Settled rent', amount=1200, category='need', date=datetime(2026, 9, 1, tzinfo=timezone.utc))
        db.session.add_all([explicit, settled]); db.session.commit()
        recurring_id, bill_id, settled_id = recurring.id, explicit.id, settled.id

    assert client.delete(f'/bills/{bill_id}').status_code == 200
    rows = client.get('/api/recurring-needs').get_json()
    assert [(row['id'], row['is_active']) for row in rows] == [(recurring_id, True)]
    with app.app_context():
        assert db.session.get(Bill, bill_id) is None
        assert db.session.get(ExpenseTransaction, settled_id) is not None
        recurring = db.session.get(RecurringRequiredObligation, recurring_id)
        projected = project_occurrences(obligations=[recurring], as_of=datetime(2026, 10, 2, tzinfo=timezone.utc), horizon_end=datetime(2026, 11, 30, tzinfo=timezone.utc), explicit_bill_links=set())
        assert [row['key'] for row in projected['occurrences']] == ['recurring:%s:2026-11-01' % recurring_id]

    assert client.patch(f'/api/recurring-needs/{recurring_id}', json={'is_active': False}).status_code == 200
    with app.app_context():
        recurring = db.session.get(RecurringRequiredObligation, recurring_id)
        assert project_occurrences(obligations=[recurring], as_of=datetime(2026, 10, 2, tzinfo=timezone.utc), horizon_end=datetime(2026, 11, 30, tzinfo=timezone.utc), explicit_bill_links=set())['occurrences'] == []
    assert client.patch(f'/api/recurring-needs/{recurring_id}', json={'is_active': True}).status_code == 200
    with app.app_context():
        recurring = db.session.get(RecurringRequiredObligation, recurring_id)
        assert len(project_occurrences(obligations=[recurring], as_of=datetime(2026, 10, 2, tzinfo=timezone.utc), horizon_end=datetime(2026, 11, 30, tzinfo=timezone.utc), explicit_bill_links=set())['occurrences']) == 1
        assert Bill.query.filter_by(household_id=hid, recurring_obligation_id=recurring_id).count() == 0


def test_reviewed_physical_pyf_transfer_debits_checking_once_and_replays_once(client):
    income = client.post('/api/transactions', json={'description': 'Paycheck', 'amount': 1800, 'category': 'income', 'operation_id': 'income-op-transfer'})
    assert income.status_code == 200
    with app.app_context():
        hid = household_id()
        reserve = create_reserve(hid, operation_id='reserve-for-pyf', name='Emergency', category='emergency', target_cents=100000, priority=1)
        protection = IncomePyfProtection.query.filter_by(household_id=hid, income_transaction_id=income.get_json()['id']).one()
        destination_id, protection_id = reserve.destination_id, protection.id
    body = {'confirm': True, 'operation_id': 'physical-pyf-1', 'amount_cents': 10000,
            'destination_id': destination_id, 'transfer_type': 'deposit', 'income_pyf_protection_id': protection_id}
    assert client.post('/api/savings/transfer', json=body).status_code == 200
    assert client.post('/api/savings/transfer', json=body).status_code == 200
    with app.app_context():
        hid = household_id()
        protection = db.session.get(IncomePyfProtection, protection_id)
        assert protection.fulfilled_cents == 10000 and protection.status == 'active'
        assert Account.query.filter_by(household_id=hid).one().checking_balance == 3500
        assert SavingsTransfer.query.filter_by(household_id=hid, operation_id='physical-pyf-1', income_pyf_protection_id=protection_id).count() == 1
