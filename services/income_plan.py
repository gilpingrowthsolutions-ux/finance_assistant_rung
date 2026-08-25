"""Canonical append-only, effective-dated expected cycle-income authority."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import IncomePlanVersion


class IncomePlanError(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def resolve_income_plan(household_id: int, *, at: datetime) -> IncomePlanVersion | None:
    """Newest version effective at ``at``; later same-boundary edits win by ID."""
    return (IncomePlanVersion.query
            .filter(IncomePlanVersion.household_id == household_id,
                    IncomePlanVersion.effective_at <= _utc(at))
            .order_by(IncomePlanVersion.effective_at.desc(), IncomePlanVersion.id.desc())
            .first())


def pending_income_plan(household_id: int, *, at: datetime) -> IncomePlanVersion | None:
    return (IncomePlanVersion.query
            .filter(IncomePlanVersion.household_id == household_id,
                    IncomePlanVersion.effective_at > _utc(at))
            .order_by(IncomePlanVersion.effective_at.asc(), IncomePlanVersion.id.desc())
            .first())


def income_plan_payload(household_id: int, *, at: datetime) -> dict[str, Any]:
    current = resolve_income_plan(household_id, at=at)
    pending = pending_income_plan(household_id, at=at)
    def item(row: IncomePlanVersion | None) -> dict[str, Any] | None:
        return None if row is None else {
            "id": row.id, "expected_income_cents": int(row.expected_income_cents),
            "expected_income": round(int(row.expected_income_cents) / 100, 2),
            "effective_at": _utc(row.effective_at).isoformat(), "source": row.source,
        }
    return {"authority": "income_plan_v1", "current": item(current), "pending": item(pending)}


def record_income_plan(
    household_id: int, *, operation_id: str, expected_income_cents: int,
    now: datetime, next_payday: datetime | None, source: str,
) -> tuple[IncomePlanVersion, bool]:
    """Establish now when no history exists; otherwise schedule next payday."""
    operation_id = str(operation_id or "").strip()
    source = str(source or "").strip()[:40]
    cents = int(expected_income_cents)
    if not operation_id or len(operation_id) > 120 or cents <= 0 or not source:
        raise IncomePlanError("A valid operation ID, positive expected paycheck, and source are required.")
    now = _utc(now)
    existing = IncomePlanVersion.query.filter_by(household_id=household_id, operation_id=operation_id).first()
    if existing is not None:
        if (int(existing.expected_income_cents), existing.source) != (cents, source):
            raise IncomePlanError("Operation ID was already used for a different expected-paycheck plan.")
        return existing, False
    has_history = IncomePlanVersion.query.filter_by(household_id=household_id).first() is not None
    effective_at = _utc(next_payday) if has_history and next_payday is not None else now
    if has_history and next_payday is None:
        raise IncomePlanError("Set an authoritative next payday before changing the expected paycheck.")
    row = IncomePlanVersion(household_id=household_id, operation_id=operation_id,
                            expected_income_cents=cents, effective_at=effective_at, source=source)
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
        return row, True
    except IntegrityError:
        existing = IncomePlanVersion.query.filter_by(household_id=household_id, operation_id=operation_id).first()
        if existing is None:
            raise
        if (int(existing.expected_income_cents), _utc(existing.effective_at), existing.source) != (cents, effective_at, source):
            raise IncomePlanError("Operation ID was already used for a different expected-paycheck plan.")
        return existing, False
