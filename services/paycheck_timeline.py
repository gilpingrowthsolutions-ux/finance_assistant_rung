"""Read-only Package 15 paycheck timeline and trajectory projection.

This module derives a household view from existing authorities.  It owns no
money, persists nothing, and deliberately does not feed Safe-to-Spend.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from models import Bill, ExpenseTransaction, SavingsAllocationRun, SavingsDestination, SavingsTransfer


def _cents(value: Any) -> int:
    return int((Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _words(value: str) -> set[str]:
    return {word for word in re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower()).split() if len(word) > 2}


def _bill_transaction_match(bill: Bill, transactions: list[ExpenseTransaction]) -> ExpenseTransaction | None:
    """Use conservative description evidence; ambiguity means no match."""
    wanted = _words(bill.name)
    if not wanted:
        return None
    matches = [row for row in transactions if row.category != "income" and wanted <= _words(row.description)]
    return matches[0] if len(matches) == 1 else None


def _event(*, key: str, occurred_at: datetime, label: str, amount_cents: int,
           kind: str, state: str, provenance: str, important: bool = True,
           uncertainty: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "occurred_at": _utc(occurred_at).isoformat(),
        "date": _utc(occurred_at).date().isoformat(),
        "label": label,
        "amount_cents": int(amount_cents),
        "amount": float(Decimal(int(amount_cents)) / 100),
        "kind": kind,
        "state": state,
        "provenance": provenance,
        "important": bool(important),
        "uncertainty": uncertainty,
    }


def resolve_cycle(*, account: Any, now: datetime, next_income: dict[str, Any]) -> dict[str, Any]:
    """Interpret the canonical next-income result as [payday, next payday)."""
    days = int(getattr(account, "pay_period_days", 0) or 0)
    resolved = next_income.get("date")
    if days <= 0 or not next_income.get("known") or not isinstance(resolved, datetime):
        return {"available": False, "setup_needed": True, "missing": ["authoritative_pay_schedule"]}
    resolved = _utc(resolved)
    today_start = datetime.combine(_utc(now).date(), time.min, tzinfo=timezone.utc)
    resolved_start = datetime.combine(resolved.date(), time.min, tzinfo=timezone.utc)
    if resolved_start <= today_start:
        start, end = resolved_start, resolved_start + timedelta(days=days)
    else:
        end, start = resolved_start, resolved_start - timedelta(days=days)
    return {
        "available": True,
        "setup_needed": False,
        "start": start,
        "end": end,
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "end_exclusive": True,
        "schedule_source": next_income.get("source"),
    }


def build_paycheck_timeline(
    *, household_id: int, account: Any, now: datetime,
    next_income: dict[str, Any], pyf_snapshot: dict[str, Any],
    bill_query: Callable[..., Any], transaction_query: Callable[..., Any],
    transfer_query: Callable[..., Any], allocation_query: Callable[..., Any],
    destination_query: Callable[..., Any],
    income_comparable_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the deterministic household-scoped Package 15 read model."""
    now = _utc(now)
    cycle = resolve_cycle(account=account, now=now, next_income=next_income)
    unavailable = {
        "authority": "paycheck_timeline_v1", "read_only": True,
        "status": "unavailable", "setup_needed": True,
        "cycle": cycle, "events": [], "important_events": [],
        "income": {"forecast_only": True, "is_due": False},
        "factual_components": {},
    }
    if not cycle.get("available") or not pyf_snapshot.get("complete"):
        if pyf_snapshot.get("missing_setup"):
            unavailable["cycle"]["missing"] = list(pyf_snapshot["missing_setup"])
        return unavailable

    start, end = cycle["start"], cycle["end"]
    transactions = transaction_query(household_id, start, end)
    bills = bill_query(household_id, start, end)
    transfers = transfer_query(household_id, start, min(now, end))
    runs = allocation_query(household_id, cycle["end_date"])
    destinations = {row.id: row for row in destination_query(household_id)}
    events: list[dict[str, Any]] = []

    income_rows = [row for row in transactions if str(row.category or "").lower() == "income"]
    for row in transactions:
        row_at = _utc(row.date)
        is_income = str(row.category or "").lower() == "income"
        events.append(_event(
            key=f"transaction:{row.id}", occurred_at=row_at, label=row.description,
            amount_cents=_cents(row.amount), kind="income" if is_income else "need_actual",
            state="completed" if row_at <= now else "upcoming_confirmed",
            provenance="reconciled_manual_plaid" if row.plaid_transaction_id else str(row.source or "manual"),
            important=is_income or str(row.category or "").lower() in {"grocery", "fuel", "transport", "housing", "utilities", "medical", "childcare"},
        ))

    # Use the same current-period income authority already resolved by the PYF
    # engine (configured paycheck first, established history fallback).
    expected_income = int(pyf_snapshot.get("period_income_cents") or 0)
    scheduled_income_at = next_income.get("date")
    if isinstance(scheduled_income_at, datetime):
        scheduled_income_at = _utc(scheduled_income_at)
    else:
        # A supported cycle always has this authority.  Retaining the cycle
        # start fallback keeps the read model conservative if an older caller
        # provides a cycle without a usable scheduled point.
        scheduled_income_at = start
    actual_income = sum(_cents(row.amount) for row in income_rows if _utc(row.date) <= now)
    if not income_rows and expected_income > 0:
        events.append(_event(
            key="forecast:cycle_income", occurred_at=scheduled_income_at, label="Expected paycheck",
            amount_cents=expected_income, kind="income", state="forecast",
            provenance=str(cycle.get("schedule_source") or "canonical_pay_schedule"),
            uncertainty="Expected amount; no confirmed cycle income yet.",
        ))

    need_variance = 0
    for bill in bills:
        matched = _bill_transaction_match(bill, transactions)
        bill_at = _utc(bill.due_date)
        if matched is not None:
            # The transaction is the completed reality; suppress the matching forecast.
            expected = _cents(bill.amount)
            actual = _cents(matched.amount)
            need_variance += expected - actual
            for event in events:
                if event["key"] == f"transaction:{matched.id}":
                    event.update({"kind": "need_actual", "important": True,
                                  "supersedes": f"bill:{bill.id}", "expected_amount_cents": expected})
            continue
        events.append(_event(
            key=f"bill:{bill.id}", occurred_at=bill_at, label=bill.name,
            amount_cents=_cents(bill.amount), kind="obligation",
            state="completed" if bill.is_paid else ("upcoming_confirmed" if bill_at >= now else "forecast"),
            provenance="bill", uncertainty=None if bill.is_paid else "Outstanding required obligation.",
        ))

    # Canonical forecast Needs that do not have their own Bill row remain
    # visible as forecasts near the cycle boundary. They never create favorable
    # variance merely because they have not happened yet.
    for need in pyf_snapshot.get("needs") or []:
        if need.get("key") != "groceries_remaining" or int(need.get("amount_cents") or 0) <= 0:
            continue
        events.append(_event(
            key="forecast:groceries_remaining", occurred_at=end - timedelta(microseconds=1),
            label=str(need.get("label") or "Required groceries remaining"),
            amount_cents=int(need["amount_cents"]), kind="forecast_need", state="forecast",
            provenance="canonical_pyf_v1", uncertainty="Expected required spending still outstanding.",
        ))

    # Package 13–14 ledger is the sole actual savings/allocation evidence.
    actual_pyf = 0
    for row in transfers:
        if getattr(row, 'superseded_by_transfer_id', None) is not None:
            continue
        if row.transfer_type != "pyf_allocation":
            continue
        actual_pyf += int(row.amount_cents)
        destination = destinations.get(row.destination_id)
        events.append(_event(
            key=f"savings_transfer:{row.id}", occurred_at=(
                datetime.combine(row.economic_date, time.min, tzinfo=timezone.utc)
                if row.economic_date is not None else _utc(row.created_at)
            ),
            label=(destination.name if destination else "Savings allocation"),
            amount_cents=int(row.amount_cents), kind="pyf_allocation", state="completed",
            provenance="packages_13_14_savings_ledger",
        ))
    expected_pyf = int(pyf_snapshot.get("feasible_savings_cents") or 0)
    if actual_pyf == 0 and expected_pyf > 0:
        events.append(_event(
            key="forecast:pyf", occurred_at=start, label="PYF savings protection",
            amount_cents=expected_pyf, kind="pyf_allocation", state="forecast",
            provenance="canonical_pyf_v1", uncertainty="Expected protection not yet recorded in the savings ledger.",
        ))

    # Expected income remains a forecast until its canonical scheduled payday.
    # Do not describe an unarrived paycheck as missing.  Confirmed income is
    # still reality, and becomes comparable to the expected amount once due.
    comparison_as_of = _utc(income_comparable_at) if income_comparable_at is not None else now
    comparable_expected_income = expected_income if scheduled_income_at <= comparison_as_of else 0
    # Future Needs remain neutral until settled, preventing false favorable
    # variance.  PYF timing deliberately remains governed by its own current
    # behavior until product authority establishes its comparable point.
    # Global aggregate plan scoring is intentionally retired.  These are
    # factual component values, not a household financial judgment and never
    # affect Safe-to-Spend.
    income_variance = actual_income - comparable_expected_income
    pyf_variance = actual_pyf - expected_pyf

    events.sort(key=lambda row: (row["occurred_at"], row["key"]))
    return {
        "authority": "paycheck_timeline_v1", "read_only": True,
        "status": "available", "setup_needed": False, "cycle": cycle,
        "events": events, "important_events": [row for row in events if row["important"]],
        "income": {
            "expected_cents": expected_income, "confirmed_cents": actual_income,
            "scheduled_at": scheduled_income_at.isoformat(),
            "is_due": bool(scheduled_income_at <= comparison_as_of),
            "unconfirmed_due_cents": max(0, comparable_expected_income - actual_income),
            "forecast_only": True,
        },
        "factual_components": {
            "confirmed_income_variance_cents": income_variance,
            "settled_needs_variance_cents": need_variance,
            "pyf_progress_cents": actual_pyf,
            "expected_pyf_cents": expected_pyf,
        },
        "safe_to_spend_effect_cents": 0,
        "evidence": {"allocation_run_count": len(runs), "transaction_count": len(transactions)},
    }
