"""Canonical short-term forward-Needs projection.

This is deliberately a pure cent-based calculator.  Persistence and schedule
resolution remain in the application authority; consumers receive provenance
from the one Safe-to-Spend snapshot rather than recreating this arithmetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def forward_horizon(*, as_of: datetime, next_payday: datetime, pay_period_days: int) -> datetime:
    """Later of 31 calendar days or the payday after the next payday."""
    as_of = _utc(as_of)
    next_payday = _utc(next_payday)
    return max(as_of + timedelta(days=31), next_payday + timedelta(days=max(1, pay_period_days)))


def calculate_forward_needs_reserve(
    *,
    as_of: datetime,
    current_boundary: datetime,
    horizon_end: datetime,
    bills: Iterable[dict[str, Any]],
    expected_income: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return the minimum current-money reserve for known forward cash needs.

    Immediate-cycle obligations (due on or before ``current_boundary``) are
    owned by the existing current-Needs calculation.  This projection starts
    just after that boundary at zero discretionary current cash, applies only
    explicit future income and explicit dated required Bills chronologically,
    and protects the deepest projected required-cash deficit.  It therefore
    never reserves every bill up front and never treats forecast income as
    actual money.
    """
    as_of = _utc(as_of)
    current_boundary = _utc(current_boundary)
    horizon_end = _utc(horizon_end)
    events: list[dict[str, Any]] = []
    for row in expected_income:
        when = row.get("date")
        cents = int(row.get("amount_cents") or 0)
        if isinstance(when, datetime) and current_boundary <= _utc(when) <= horizon_end and cents > 0:
            events.append({"at": _utc(when), "kind": "expected_income", "amount_cents": cents,
                           "key": str(row.get("key") or "expected_income"), "label": row.get("label") or "Expected income"})
    for row in bills:
        when = row.get("due_date")
        cents = int(row.get("amount_cents") or 0)
        if isinstance(when, datetime) and current_boundary < _utc(when) <= horizon_end and cents > 0:
            events.append({"at": _utc(when), "kind": "required_need", "amount_cents": cents,
                           "key": str(row.get("key") or "bill"), "label": row.get("label") or "Required bill"})

    # Income is available on its scheduled date before same-day due bills.
    events.sort(key=lambda row: (row["at"], 0 if row["kind"] == "expected_income" else 1, row["key"]))
    projected_cents = 0
    deepest_deficit_cents = 0
    projected_shortfall_cents = 0
    output_events = []
    for event in events:
        delta = event["amount_cents"] if event["kind"] == "expected_income" else -event["amount_cents"]
        projected_cents += delta
        deepest_deficit_cents = min(deepest_deficit_cents, projected_cents)
        output_events.append({**event, "date": event["at"].isoformat(), "projected_cents_after": projected_cents})

    reserve_cents = max(0, -deepest_deficit_cents)
    return {
        "authority": "forward_needs_v1",
        "as_of": as_of.isoformat(),
        "current_boundary": current_boundary.isoformat(),
        "horizon_end": horizon_end.isoformat(),
        "rule": "later_of_31_calendar_days_or_payday_after_next",
        "forward_needs_reserve_cents": reserve_cents,
        "projected_required_cash_shortfall_cents": projected_shortfall_cents,
        "events": output_events,
    }
