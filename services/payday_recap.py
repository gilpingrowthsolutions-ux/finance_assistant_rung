"""Deterministic, read-only Payday Recap projection (Package 17)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

from services.paycheck_timeline import build_paycheck_timeline, resolve_cycle


TRANSFER_CATEGORIES = {"transfer", "savings", "reserve", "investment", "investments", "balance", "adjustment"}
NEED_CATEGORIES = {"grocery", "groceries", "fuel", "transport", "transportation", "housing", "rent", "utilities", "utility", "medical", "prescription", "childcare", "insurance", "essentials"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _cents(value: Any) -> int:
    return int(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def _money(cents: int | None) -> float | None:
    return None if cents is None else float(Decimal(int(cents)) / 100)


def _unavailable(*, reason: str, current_cycle: dict[str, Any] | None = None,
                 completed_cycle: dict[str, Any] | None = None,
                 current_safe: dict[str, Any] | None = None, status: str = "not_ready") -> dict[str, Any]:
    safe = current_safe or {}
    return {
        "authority": "payday_recap_v1", "read_only": True, "status": status,
        "completed_cycle": completed_cycle, "finish_status": "unavailable",
        "finish_amount_cents": None, "finish_amount": None,
        "finish_reasons": [reason], "protected_summary": None,
        "biggest_changes": [], "completed_cycle_detail": None,
        "current_cycle": current_cycle,
        "next_payday": (current_cycle or {}).get("end_date"),
        "current_safe_to_spend_cents": safe.get("safe_to_spend_cents") if safe.get("complete") else None,
        "current_safe_to_spend": _money(safe.get("safe_to_spend_cents")) if safe.get("complete") else None,
        "safe_to_spend_authority": safe.get("authority") or "canonical_pyf_v1",
        "current_setup_complete": bool(safe.get("complete")),
        "current_setup_missing": list(safe.get("missing_setup") or []),
        "informational_only": True, "financial_mutations": False,
        "safe_to_spend_effect_cents": 0,
    }


def build_payday_recap(
    *, household_id: int, account: Any, now: datetime, next_income: dict[str, Any],
    current_safe_snapshot: dict[str, Any], bill_query: Callable[..., Any],
    transaction_query: Callable[..., Any], transfer_query: Callable[..., Any],
    allocation_query: Callable[..., Any], destination_query: Callable[..., Any],
    completed_cycle_income_expectation: dict[str, Any] | None = None,
    income_plan_resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build the latest truthfully completed cycle without persisting anything."""
    now = _utc(now)
    current = resolve_cycle(account=account, now=now, next_income=next_income)
    if not current.get("available"):
        return _unavailable(reason="Complete authoritative pay-cycle setup before Rung can identify a finished cycle.", current_cycle=current, current_safe=current_safe_snapshot, status="missing_setup")

    period_days = int(getattr(account, "pay_period_days", 0) or 0)
    completed_end = current["start"]
    completed_start = completed_end - timedelta(days=period_days)
    completed_cycle = {
        "available": True, "start": completed_start, "end": completed_end,
        "start_date": completed_start.date().isoformat(),
        "end_date": completed_end.date().isoformat(), "end_exclusive": True,
        "schedule_source": current.get("schedule_source"),
    }
    transactions = list(transaction_query(household_id, completed_start, completed_end))
    confirmed_income = [row for row in transactions if str(row.category or "").lower() == "income" and _utc(row.date) < completed_end]
    if not confirmed_income:
        return _unavailable(reason="No confirmed income establishes that a prior pay cycle has completed yet.", completed_cycle={key: value for key, value in completed_cycle.items() if key not in {"start", "end"}}, current_cycle=current, current_safe=current_safe_snapshot)

    # Account.expected_paycheck is current mutable setup, not historical plan
    # evidence.  Only an explicitly cycle-bound authority may supply the prior
    # cycle comparison amount; otherwise a recap must remain unavailable.
    income_expectation = completed_cycle_income_expectation or {}
    if not income_expectation and income_plan_resolver is not None:
        resolved_plan = income_plan_resolver(household_id, completed_start)
        if resolved_plan is not None:
            income_expectation = {
                "amount_cents": int(resolved_plan.expected_income_cents),
                "cycle_key": completed_cycle["end_date"],
                "authority": "income_plan_v1",
            }
    expected_income_cents = int(income_expectation.get("amount_cents") or 0)
    expectation_cycle_key = str(income_expectation.get("cycle_key") or "")
    expectation_authority = str(income_expectation.get("authority") or "")
    if (expected_income_cents <= 0
            or expectation_cycle_key != completed_cycle["end_date"]
            or not expectation_authority):
        return _unavailable(
            reason="No authoritative expected-income plan is recorded for this completed pay cycle; current paycheck settings cannot be used as history.",
            completed_cycle={key: value for key, value in completed_cycle.items() if key not in {"start", "end"}},
            current_cycle=current, current_safe=current_safe_snapshot,
        )

    runs = list(allocation_query(household_id, completed_cycle["end_date"]))
    target_raw = current_safe_snapshot.get("long_term_savings_target_percent")
    if target_raw is None:
        return _unavailable(reason="The household PYF target is unavailable, so completed-cycle protection cannot be evaluated.", current_cycle=current, current_safe=current_safe_snapshot)
    target_pct = Decimal(str(target_raw))
    if target_pct > 0 and not runs:
        return _unavailable(reason="Historical PYF feasibility was not recorded for the completed cycle, so its finish status cannot be reconstructed safely.", current_cycle=current, current_safe=current_safe_snapshot)
    expected_pyf_cents = int(runs[0].feasible_cents) if runs else 0
    historical_plan = {
        "complete": True, "period_income_cents": expected_income_cents,
        "feasible_savings_cents": expected_pyf_cents, "authority": "canonical_pyf_v1",
    }
    # Passing the completed payday as the resolver's next payday gives the
    # Package 15 service exactly [previous payday, completed payday).
    timeline = build_paycheck_timeline(
        household_id=household_id, account=account, now=completed_end - timedelta(microseconds=1),
        next_income={"known": True, "date": completed_end, "source": current.get("schedule_source")},
        pyf_snapshot=historical_plan, bill_query=bill_query,
        transaction_query=transaction_query, transfer_query=transfer_query,
        allocation_query=allocation_query, destination_query=destination_query,
    )
    if timeline.get("status") != "available":
        return _unavailable(reason="Completed-cycle evidence is insufficient for a truthful recap.", current_cycle=current, current_safe=current_safe_snapshot)

    transfers = list(transfer_query(household_id, completed_start, completed_end))
    destinations = {row.id: row for row in destination_query(household_id)}
    funding = {"goal": 0, "reserve": 0, "flexible": 0, "wealth_cash": 0, "wealth_investment": 0}
    external_funding_total = 0
    internal_transfers = 0
    reserve_use = 0
    for row in transfers:
        amount = int(row.amount_cents)
        destination = destinations.get(row.destination_id)
        source = destinations.get(row.source_destination_id)
        if row.transfer_type in {"pyf_allocation", "deposit"} and row.source_destination_id is None:
            external_funding_total += amount
            if destination and destination.kind in funding:
                funding[destination.kind] += amount
        elif row.transfer_type == "transfer":
            internal_transfers += amount
        elif row.transfer_type == "reserve_use" and source and source.kind == "reserve":
            reserve_use += amount

    matched_need_events = [row for row in timeline["events"] if row.get("kind") == "need_actual" and row.get("supersedes")]
    bills_covered_cents = sum(int(row.get("amount_cents") or 0) for row in matched_need_events)
    trajectory = timeline["trajectory"]
    components = trajectory.get("components") or {}
    component_changes = [
        (abs(int(components.get("confirmed_income_variance_cents") or 0)), "income", int(components.get("confirmed_income_variance_cents") or 0), trajectory["reasons"]),
        (abs(int(components.get("settled_needs_variance_cents") or 0)), "settled_needs", int(components.get("settled_needs_variance_cents") or 0), trajectory["reasons"]),
        (abs(int(components.get("pyf_progress_variance_cents") or 0)), "pyf", int(components.get("pyf_progress_variance_cents") or 0), trajectory["reasons"]),
    ]
    reason_by_kind = {}
    for reason in trajectory["reasons"]:
        lowered = reason.lower()
        if "income" in lowered: reason_by_kind["income"] = reason
        elif "settled needs" in lowered: reason_by_kind["settled_needs"] = reason
        elif "pyf" in lowered: reason_by_kind["pyf"] = reason
    biggest = []
    for magnitude, kind, signed, _reasons in sorted(component_changes, key=lambda row: (-row[0], row[1])):
        if magnitude <= 0: continue
        biggest.append({"kind": kind, "amount_cents": signed, "amount": _money(signed), "direction": "favorable" if signed > 0 else "unfavorable", "summary": reason_by_kind.get(kind, "A supported cycle component changed.")})
    if not biggest:
        biggest = [{"kind": "on_track", "amount_cents": 0, "amount": 0.0, "direction": "neutral", "summary": "Confirmed reality matched the supported completed-cycle expectations."}]

    discretionary_cents = 0
    needs_actual_cents = 0
    excluded_money_movement_cents = 0
    for row in transactions:
        category = str(row.category or "").lower()
        if category == "income": continue
        amount = _cents(row.amount)
        if category in TRANSFER_CATEGORIES:
            excluded_money_movement_cents += amount
        elif category in NEED_CATEGORIES:
            needs_actual_cents += amount
        else:
            discretionary_cents += amount

    finish_cents = int(trajectory["amount_cents"])
    current_safe_cents = int(current_safe_snapshot["safe_to_spend_cents"]) if current_safe_snapshot.get("complete") else None
    return {
        "authority": "payday_recap_v1", "read_only": True, "status": "available",
        "completed_cycle": {key: value for key, value in completed_cycle.items() if key not in {"start", "end"}},
        "finish_status": trajectory["status"], "finish_amount_cents": finish_cents,
        "finish_amount": _money(finish_cents), "finish_reasons": list(trajectory["reasons"][:3]),
        "protected_summary": {
            "actual_protected_cents": external_funding_total, "actual_protected": _money(external_funding_total),
            "pyf_expected_cents": expected_pyf_cents, "pyf_completed_cents": sum(int(row.amount_cents) for row in transfers if row.transfer_type == "pyf_allocation"),
            "pyf_successfully_protected": expected_pyf_cents == 0 or sum(int(row.amount_cents) for row in transfers if row.transfer_type == "pyf_allocation") >= expected_pyf_cents,
            "goal_funding_cents": funding["goal"], "reserve_funding_cents": funding["reserve"],
            "flexible_funding_cents": funding["flexible"],
            "wealth_funding_cents": funding["wealth_cash"] + funding["wealth_investment"],
            "internal_transfer_cents": internal_transfers, "reserve_use_cents": reserve_use,
            "covered_need_count": len(matched_need_events), "covered_need_cents": bills_covered_cents,
            "provenance": "packages_13_14_savings_ledger_and_reconciled_cycle_activity",
        },
        "biggest_changes": biggest[:3],
        "completed_cycle_detail": {
            "confirmed_income_cents": sum(_cents(row.amount) for row in confirmed_income),
            "expected_income_cents": expected_income_cents,
            "expected_income_authority": expectation_authority,
            "settled_need_actual_cents": needs_actual_cents,
            "discretionary_actual_cents": discretionary_cents,
            "excluded_money_movement_cents": excluded_money_movement_cents,
            "trajectory_components": components,
            "events": timeline["events"], "transaction_count": len(transactions),
            "reconciled_projection": True, "shopping_completion_counted_via_transaction_only": True,
            "package16_hypothetical_savings_included_cents": 0,
        },
        "current_cycle": {key: value for key, value in current.items() if key not in {"start", "end"}},
        "next_payday": current.get("end_date"),
        "current_safe_to_spend_cents": current_safe_cents,
        "current_safe_to_spend": _money(current_safe_cents),
        "safe_to_spend_authority": current_safe_snapshot.get("authority") or "canonical_pyf_v1",
        "current_setup_complete": bool(current_safe_snapshot.get("complete")),
        "current_setup_missing": list(current_safe_snapshot.get("missing_setup") or []),
        "current_pyf_feasible_cents": current_safe_snapshot.get("feasible_savings_cents") if current_safe_snapshot.get("complete") else None,
        "current_protected_buffer_cents": _cents((current_safe_snapshot.get("components") or {}).get("protected_buffer")) if current_safe_snapshot.get("complete") else None,
        "informational_only": True, "financial_mutations": False,
        "safe_to_spend_effect_cents": 0, "automatic_allocation": False,
        "rollover_or_spending_grant_cents": 0,
    }
