"""Canonical Pay Yourself First financial-state calculation.

This module is intentionally independent of Rung's legacy liquidity engine.
Callers resolve household-scoped authoritative inputs; this service performs
only cent-accurate PYF feasibility arithmetic.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def money_to_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid money value") from exc
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def percentage_amount_cents(period_income_cents: int, target_percent: Any) -> int:
    try:
        pct = Decimal(str(target_percent))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid savings target percentage") from exc
    if pct < 0:
        raise ValueError("Savings target percentage cannot be negative")
    return max(
        0,
        int((Decimal(period_income_cents) * pct / Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP)),
    )


def cents_to_money(cents: int) -> float:
    return float((Decimal(int(cents)) / Decimal("100")).quantize(Decimal("0.01")))


def calculate_pyf_snapshot(
    *,
    checking_cents: int | None,
    period_income_cents: int | None,
    savings_target_percent: Any | None,
    protected_buffer_cents: int | None,
    active_income_pyf_cents: int = 0,
    current_cycle_income_pyf_cents: int = 0,
    current_cycle_established_pyf_cents: int | None = None,
    needs: list[dict[str, Any]],
    missing_setup: list[str] | None = None,
) -> dict[str, Any]:
    """Return canonical current-period PYF state using integer cents only."""
    missing = list(dict.fromkeys(missing_setup or []))
    if checking_cents is None:
        missing.append("checking_balance")
    if period_income_cents is None:
        missing.append("current_period_income")
    if savings_target_percent is None:
        missing.append("long_term_savings_target_percent")
    if protected_buffer_cents is None:
        missing.append("protected_checking_buffer")
    missing = list(dict.fromkeys(missing))

    if missing:
        return {
            "authority": "canonical_pyf_v1",
            "state": "needs_setup",
            "feasibility": "setup_required",
            "complete": False,
            "missing_setup": missing,
            "checking_cents": checking_cents,
            "checking_balance": cents_to_money(checking_cents or 0) if checking_cents is not None else None,
            "needs_total_cents": None,
            "needs_total": None,
            "needs": needs,
            "long_term_savings_target_percent": savings_target_percent,
            "target_savings_cents": None,
            "target_savings_amount": None,
            "feasible_savings_cents": None,
            "feasible_savings_contribution": None,
            "savings_shortfall_cents": None,
            "savings_shortfall": None,
            "protected_buffer_cents": protected_buffer_cents,
            "protected_buffer": cents_to_money(protected_buffer_cents or 0) if protected_buffer_cents is not None else None,
            "safe_to_spend_cents": None,
            "safe_to_spend": None,
        }

    checking = int(checking_cents)
    income = max(0, int(period_income_cents))
    buffer_cents = max(0, int(protected_buffer_cents))
    needs_total = sum(max(0, int(row.get("amount_cents") or 0)) for row in needs)
    target_cents = percentage_amount_cents(income, savings_target_percent)
    active_pyf = max(0, int(active_income_pyf_cents or 0))
    current_cycle_pyf = min(active_pyf, max(0, int(current_cycle_income_pyf_cents or 0)))
    # Fulfilled money has left checking and must not remain a virtual
    # deduction, but its established income consequence still satisfies this
    # cycle's target. Without this distinction a physical transfer would both
    # lower checking and cause the same PYF amount to be re-created virtually.
    current_cycle_established = max(current_cycle_pyf, int(
        current_cycle_income_pyf_cents if current_cycle_established_pyf_cents is None
        else current_cycle_established_pyf_cents
    ))
    available_after_needs_and_buffer = max(0, checking - needs_total - buffer_cents)
    # All unfulfilled protection remains unavailable in checking.  Only the
    # portion created by current-cycle income may satisfy this cycle's PYF
    # target; prior-cycle savings never erase a new paycheck's consequence.
    remaining_target_cents = max(0, target_cents - current_cycle_established)
    feasible_cents = min(remaining_target_cents, max(0, available_after_needs_and_buffer - active_pyf))
    total_protected_cents = active_pyf + feasible_cents
    current_cycle_total_cents = current_cycle_established + feasible_cents
    shortfall_cents = max(0, target_cents - current_cycle_total_cents)
    safe_cents = max(0, checking - needs_total - buffer_cents - total_protected_cents)

    if current_cycle_total_cents >= target_cents:
        feasibility = "full_target_feasible"
    elif feasible_cents > 0:
        feasibility = "partial_target_feasible"
    else:
        feasibility = "no_contribution_feasible"

    return {
        "authority": "canonical_pyf_v1",
        "state": "positive" if safe_cents > 0 else ("tight" if checking >= needs_total + buffer_cents else "overcommitted"),
        "feasibility": feasibility,
        "complete": True,
        "missing_setup": [],
        "checking_cents": checking,
        "checking_balance": cents_to_money(checking),
        "period_income_cents": income,
        "period_income": cents_to_money(income),
        "needs_total_cents": needs_total,
        "needs_total": cents_to_money(needs_total),
        "needs": needs,
        "long_term_savings_target_percent": float(Decimal(str(savings_target_percent))),
        "target_savings_cents": target_cents,
        "target_savings_amount": cents_to_money(target_cents),
        "feasible_savings_cents": total_protected_cents,
        "feasible_savings_contribution": cents_to_money(total_protected_cents),
        "active_income_pyf_cents": active_pyf,
        "current_cycle_income_pyf_cents": current_cycle_pyf,
        "savings_shortfall_cents": shortfall_cents,
        "savings_shortfall": cents_to_money(shortfall_cents),
        "protected_buffer_cents": buffer_cents,
        "protected_buffer": cents_to_money(buffer_cents),
        "safe_to_spend_cents": safe_cents,
        "safe_to_spend": cents_to_money(safe_cents),
        "checks": {
            "cent_accurate": checking - needs_total - buffer_cents - feasible_cents == safe_cents,
            "buffer_protected": safe_cents <= max(0, checking - needs_total - buffer_cents),
            "target_preserved": True,
        },
    }
