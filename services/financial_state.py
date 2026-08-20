from __future__ import annotations

from decimal import Decimal
from threading import RLock
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Account

MAX_RETRIES = 8
_ACCOUNT_CREATE_LOCK = RLock()


class FinancialStateError(RuntimeError):
    pass


def _as_money(value: float | int | Decimal) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def get_household_account(household_id: int, *, create_if_missing: bool = True) -> Account:
    account = Account.query.filter_by(household_id=household_id).first()
    if account is not None:
        return account
    if not create_if_missing:
        raise FinancialStateError("Household account not found.")
    with _ACCOUNT_CREATE_LOCK:
        # Browser startup performs several reads concurrently. Recheck inside
        # the creator lock so those reads cannot create parallel authorities.
        account = Account.query.filter_by(household_id=household_id).first()
        if account is not None:
            return account
        # A newly created household has no confirmed financial facts. Legacy
        # model defaults are demo conveniences, not user-entered authority.
        account = Account(household_id=household_id)
        try:
            db.session.add(account)
            db.session.flush()
            account.checking_balance = None
            account.pay_period_days = 0
            account.expected_paycheck = None
            db.session.commit()
            return account
        except IntegrityError:
            # A different worker inserted the one-per-household authority.
            # Roll back only our failed create and load the winning row.
            db.session.rollback()
            existing = Account.query.filter_by(household_id=household_id).first()
            if existing is None:
                raise
            return existing


def apply_balance_delta(household_id: int, delta: float | int | Decimal) -> float:
    delta_money = _as_money(delta)
    for _ in range(MAX_RETRIES):
        account = get_household_account(household_id)
        current = _as_money(account.checking_balance)
        version = int(account.balance_version or 0)
        next_balance = current + delta_money
        updated = (
            Account.query
            .filter_by(id=account.id, household_id=household_id, balance_version=version)
            .update(
                {
                    Account.checking_balance: float(next_balance),
                    Account.balance_version: version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            return float(next_balance)
        db.session.expire(account)
    raise FinancialStateError("Could not apply balance delta after retries.")


def set_balance_absolute(household_id: int, target_balance: float | int | Decimal) -> float:
    target = _as_money(target_balance)
    for _ in range(MAX_RETRIES):
        account = get_household_account(household_id)
        version = int(account.balance_version or 0)
        updated = (
            Account.query
            .filter_by(id=account.id, household_id=household_id, balance_version=version)
            .update(
                {
                    Account.checking_balance: float(target),
                    Account.balance_version: version + 1,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            return float(target)
        db.session.expire(account)
    raise FinancialStateError("Could not set balance after retries.")
