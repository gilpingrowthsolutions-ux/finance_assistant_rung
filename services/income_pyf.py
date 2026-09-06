"""Canonical actual-income to automatic PYF protection authority."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from extensions import db
from models import ExpenseTransaction, IncomePyfProtection, UserSetting
from services.pyf_financial_state import percentage_amount_cents

# This is the same durable household setting read by the served PYF settings
# and Safe-to-Spend authority.  Actual income must not consult a parallel
# legacy key or a customer can configure a target that never protects income.
PYF_TARGET_SETTING_KEY = 'pyf_long_term_target_percent'


def _cents(value: Any) -> int:
    return int((Decimal(str(value or 0)).copy_abs() * 100).quantize(Decimal('1')))


def establish_for_income(tx: ExpenseTransaction) -> IncomePyfProtection | None:
    """Create/update the sole PYF consequence in the caller's transaction.

    Call only after the canonical income transaction has been flushed.  The
    database unique constraint is the race-safe final authority.
    """
    if str(tx.category or '').lower() != 'income' or float(tx.amount or 0) <= 0:
        return reverse_for_transaction(tx)
    target = UserSetting.query.filter_by(household_id=tx.household_id, key=PYF_TARGET_SETTING_KEY).first()
    if target is None:
        return None
    try:
        pct = Decimal(str(target.value))
    except Exception:
        return None
    if pct < 0:
        return None
    amount = _cents(tx.amount)
    planned = percentage_amount_cents(amount, pct)
    row = IncomePyfProtection.query.filter_by(household_id=tx.household_id, income_transaction_id=tx.id).first()
    if row is None:
        row = IncomePyfProtection(household_id=tx.household_id, income_transaction_id=tx.id,
                                  operation_id=f'income-pyf:{tx.id}', target_percent=pct,
                                  target_cents=planned, protected_cents=planned)
        try:
            # A savepoint isolates a unique-key race.  Rolling back the outer
            # request here would also erase the income/checking write.
            with db.session.begin_nested():
                db.session.add(row)
                db.session.flush()
        except IntegrityError:
            row = IncomePyfProtection.query.filter_by(household_id=tx.household_id, income_transaction_id=tx.id).one()
    else:
        row.target_cents = planned
        row.protected_cents = max(int(row.fulfilled_cents or 0), planned)
        row.status = 'fulfilled' if row.fulfilled_cents >= row.protected_cents else 'active'
        db.session.add(row)
    return row


def reverse_for_transaction(tx: ExpenseTransaction) -> IncomePyfProtection | None:
    row = IncomePyfProtection.query.filter_by(household_id=tx.household_id, income_transaction_id=tx.id).first()
    if row is not None:
        row.status = 'reversed'
        row.protected_cents = 0
        row.fulfilled_cents = 0
        db.session.add(row)
    return row


def fulfill(protection: IncomePyfProtection, amount_cents: int) -> IncomePyfProtection:
    # Lock the authoritative row before computing its remaining amount.  A
    # concurrent loser raises before its encompassing transfer request can
    # commit either a second ledger row or a second checking debit.
    protection = db.session.execute(
        select(IncomePyfProtection)
        .where(IncomePyfProtection.id == protection.id, IncomePyfProtection.household_id == protection.household_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if protection.status != 'active':
        raise ValueError('Only an active PYF protection can be fulfilled.')
    requested = int(amount_cents)
    remaining = int(protection.protected_cents) - int(protection.fulfilled_cents)
    if requested <= 0 or requested > remaining:
        raise ValueError('PYF fulfillment must be a positive amount no greater than its remaining protection.')
    protection.fulfilled_cents = int(protection.fulfilled_cents) + requested
    protection.status = 'fulfilled' if protection.fulfilled_cents >= protection.protected_cents else 'active'
    db.session.add(protection)
    return protection
