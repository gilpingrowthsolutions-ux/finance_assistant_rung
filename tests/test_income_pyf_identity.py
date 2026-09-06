from datetime import datetime, timezone

import pytest

from app import app

from extensions import db
from models import Account, ExpenseTransaction, IncomePyfProtection, PlaidAccount, PlaidItem, PlaidTransaction, UserSetting
from services.financial_state import apply_balance_delta
from services.income_pyf import establish_for_income, fulfill, reverse_for_transaction
from services.household_context import household_id
from services.transaction_reconciliation import project_plaid_transactions
from services.pyf_financial_state import calculate_pyf_snapshot


@pytest.fixture()
def database():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all(); db.create_all()
        yield


def _setup():
    hid = household_id()
    db.session.add(Account(household_id=hid, checking_balance=0, pay_period_days=14))
    db.session.add(UserSetting(household_id=hid, key='pyf_long_term_target_percent', value='10'))
    db.session.flush()
    return hid


def _income(hid, amount, description='Paycheck'):
    tx = ExpenseTransaction(household_id=hid, description=description, amount=amount, category='income', source='manual', date=datetime.now(timezone.utc))
    db.session.add(tx); db.session.flush(); apply_balance_delta(hid, amount)
    return tx


def test_one_income_has_one_database_linked_percentage_protection(database):
    with app.app_context():
        hid = _setup(); tx = _income(hid, 1800)
        first = establish_for_income(tx); second = establish_for_income(tx); db.session.commit()
        assert first.id == second.id
        assert IncomePyfProtection.query.filter_by(household_id=hid, income_transaction_id=tx.id).count() == 1
        assert first.target_cents == first.protected_cents == 18000


def test_canonical_target_wins_when_a_legacy_setting_also_exists(database):
    """Actual income must use the same target the served Money UI saves."""
    with app.app_context():
        hid = _setup()
        db.session.add(UserSetting(
            household_id=hid, key='pay_yourself_first_target_percent', value='99'
        ))
        tx = _income(hid, 1800)
        protection = establish_for_income(tx)
        db.session.commit()
        assert protection is not None
        assert protection.target_percent == 10
        assert protection.protected_cents == 18000


def test_multiple_partial_income_is_proportional_and_household_scoped(database):
    with app.app_context():
        hid = _setup(); first = establish_for_income(_income(hid, 900)); second = establish_for_income(_income(hid, 900)); db.session.commit()
        assert first.protected_cents == second.protected_cents == 9000
        assert IncomePyfProtection.query.filter_by(household_id=hid).count() == 2


def test_income_correction_and_reclassification_update_the_existing_link(database):
    with app.app_context():
        hid = _setup(); tx = _income(hid, 1800); row = establish_for_income(tx)
        tx.amount = 1600; changed = establish_for_income(tx)
        assert changed.id == row.id and changed.protected_cents == 16000
        tx.category = 'discretionary'; reversed_row = establish_for_income(tx); db.session.commit()
        assert reversed_row.id == row.id and reversed_row.status == 'reversed' and reversed_row.protected_cents == 0


def test_fulfillment_is_linked_and_cannot_create_another_protection(database):
    with app.app_context():
        hid = _setup(); tx = _income(hid, 1800); row = establish_for_income(tx)
        fulfill(row, 10000); fulfill(row, 8000); db.session.commit()
        assert row.status == 'fulfilled' and row.fulfilled_cents == 18000
        assert IncomePyfProtection.query.filter_by(household_id=hid, income_transaction_id=tx.id).count() == 1


def test_nonincome_and_balance_style_rows_never_create_protection(database):
    with app.app_context():
        hid = _setup()
        tx = ExpenseTransaction(household_id=hid, description='Balance correction', amount=1800, category='balance_reconciliation', source='manual')
        db.session.add(tx); db.session.flush()
        assert establish_for_income(tx) is None
        assert IncomePyfProtection.query.filter_by(household_id=hid).count() == 0


def test_prior_cycle_protection_does_not_satisfy_new_cycle_target_but_remains_unavailable():
    snapshot = calculate_pyf_snapshot(checking_cents=360000, period_income_cents=180000,
        savings_target_percent=10, protected_buffer_cents=0, needs=[],
        active_income_pyf_cents=18000, current_cycle_income_pyf_cents=0)
    assert snapshot['active_income_pyf_cents'] == 18000
    assert snapshot['current_cycle_income_pyf_cents'] == 0
    assert snapshot['feasible_savings_cents'] == 36000
    assert snapshot['safe_to_spend_cents'] == 324000


def test_pending_plaid_paycheck_waits_for_posting_and_keeps_one_income_effect(database):
    """A pending row is not actual income; its posted replacement is one event."""
    with app.app_context():
        hid = _setup()
        account = Account.query.filter_by(household_id=hid).one()
        item = PlaidItem(household_id=hid, owner_scope="anonymous", plaid_item_id="item-pending-pyf", access_token_encrypted="test")
        db.session.add(item); db.session.flush()
        db.session.add(PlaidAccount(household_id=hid, owner_scope="anonymous", plaid_item_id=item.id,
                                    plaid_account_id="account-pending-pyf", rung_account_id=account.id, name="Checking"))
        pending = PlaidTransaction(
            household_id=hid, owner_scope="anonymous", plaid_item_id=item.id,
            plaid_transaction_id="pending-paycheck", plaid_account_id="account-pending-pyf",
            is_pending=True, is_removed=False, is_active_event=True, pending_lifecycle_status="pending",
            amount_cents=180000, signed_amount_cents=180000, direction="inflow",
            name="Payroll", description="Payroll", transaction_date=datetime.now(timezone.utc).date(),
        )
        db.session.add(pending); db.session.commit()

        assert project_plaid_transactions("anonymous") == {"applied": 0, "proposed": 0, "skipped": 1}
        assert ExpenseTransaction.query.filter_by(household_id=hid).count() == 0
        assert IncomePyfProtection.query.filter_by(household_id=hid).count() == 0
        assert Account.query.filter_by(household_id=hid).one().checking_balance == 0

        pending.is_active_event = False
        posted = PlaidTransaction(
            household_id=hid, owner_scope="anonymous", plaid_item_id=item.id,
            plaid_transaction_id="posted-paycheck", plaid_account_id="account-pending-pyf",
            replaces_pending_transaction_id="pending-paycheck", is_pending=False, is_removed=False,
            is_active_event=True, pending_lifecycle_status="posted", amount_cents=180000,
            signed_amount_cents=180000, direction="inflow", name="Payroll", description="Payroll",
            transaction_date=datetime.now(timezone.utc).date(),
        )
        db.session.add(posted); db.session.commit()

        assert project_plaid_transactions("anonymous")["applied"] == 1
        assert project_plaid_transactions("anonymous")["applied"] == 0
        assert ExpenseTransaction.query.filter_by(household_id=hid).count() == 1
        assert IncomePyfProtection.query.filter_by(household_id=hid).count() == 1
        assert Account.query.filter_by(household_id=hid).one().checking_balance == 1800
