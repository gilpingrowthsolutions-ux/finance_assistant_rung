"""Authoritative, provenance-aware deletion of financial transactions.

The Money UI may display this authority, but it must never decide eligibility
itself.  The conditional DELETE is deliberately the single-winner claim: a
request may calculate a reversal only after it has atomically removed an
eligible row in the same database transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, exists, select

from extensions import db
from models import ExpenseTransaction, ShoppingTripCompletion, TransactionReconciliation
from services.financial_state import apply_balance_delta


PROTECTED_MESSAGE = (
    "This transaction is linked to another Rung record and can't be deleted "
    "here. Use its correction or reconciliation flow instead."
)


@dataclass(frozen=True)
class DeleteEligibility:
    can_delete: bool
    reason: str | None = None

    def to_api(self) -> dict[str, object]:
        return {"can_delete": self.can_delete, "delete_reason": self.reason}


def _protection_conditions(transaction_id: int, household_id: int):
    """SQL predicates defining every currently durable deletion protection."""
    shopping_reference = exists(
        select(ShoppingTripCompletion.id).where(
            ShoppingTripCompletion.household_id == household_id,
            ShoppingTripCompletion.transaction_id == transaction_id,
        )
    )
    reconciliation_reference = exists(
        select(TransactionReconciliation.id).where(
            TransactionReconciliation.household_id == household_id,
            TransactionReconciliation.manual_transaction_id == transaction_id,
        )
    )
    return (
        ExpenseTransaction.plaid_transaction_id.is_(None),
        ~shopping_reference,
        ~reconciliation_reference,
    )


def transaction_delete_eligibility(
    transaction: ExpenseTransaction, household_id: int
) -> DeleteEligibility:
    """Return the canonical direct-delete policy for a served transaction."""
    if transaction.household_id != household_id:
        return DeleteEligibility(False, "Transaction not found")
    if transaction.plaid_transaction_id:
        return DeleteEligibility(False, PROTECTED_MESSAGE)
    protected = db.session.execute(
        select(ShoppingTripCompletion.id)
        .where(
            ShoppingTripCompletion.household_id == household_id,
            ShoppingTripCompletion.transaction_id == transaction.id,
        )
        .union_all(
            select(TransactionReconciliation.id).where(
                TransactionReconciliation.household_id == household_id,
                TransactionReconciliation.manual_transaction_id == transaction.id,
            )
        )
        .limit(1)
    ).first()
    return DeleteEligibility(not bool(protected), None if not protected else PROTECTED_MESSAGE)


def delete_transaction_once(transaction_id: int, household_id: int) -> tuple[str, float | None]:
    """Delete one eligible row and reverse its original balance effect once.

    Returns ``(deleted|missing|protected, new_balance)``.  The conditional
    DELETE and optimistic account update share one uncommitted session; any
    failure rolls both back.
    """
    conditions = _protection_conditions(transaction_id, household_id)
    statement = (
        delete(ExpenseTransaction)
        .where(
            ExpenseTransaction.id == transaction_id,
            ExpenseTransaction.household_id == household_id,
            *conditions,
        )
        .returning(ExpenseTransaction.amount, ExpenseTransaction.category)
    )
    row = db.session.execute(statement).one_or_none()
    if row is None:
        remaining = db.session.get(ExpenseTransaction, transaction_id)
        if remaining is None or remaining.household_id != household_id:
            return "missing", None
        return "protected", None

    amount = float(row.amount or 0)
    reversal_delta = -amount if str(row.category or "").strip().lower() == "income" else amount
    try:
        new_balance = apply_balance_delta(household_id, reversal_delta)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return "deleted", new_balance
