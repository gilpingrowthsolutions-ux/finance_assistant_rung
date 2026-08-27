"""Pay-period meal-plan identity and query authority.

This deliberately reuses ``paycheck_timeline.resolve_cycle``.  It does not
consult Account.expected_paycheck and it never derives an old row's identity
from today's mutable settings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from models import MealPlanItem
from services.paycheck_timeline import resolve_cycle


def cycle_identity(cycle: dict[str, Any]) -> str | None:
    if not cycle.get("available"):
        return None
    start, end = cycle.get("start"), cycle.get("end")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return f"paycheck:{start.date().isoformat()}:{end.date().isoformat()}"


def resolve_current_cycle(*, account: Any, next_income: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    cycle = resolve_cycle(account=account, now=now or datetime.now(timezone.utc), next_income=next_income)
    key = cycle_identity(cycle)
    if key is not None:
        cycle = {**cycle, "key": key}
    return cycle


def current_plan_query(household_id: int, cycle: dict[str, Any]):
    key = cycle.get("key")
    # Missing financial authority must fail closed: it is never permission to
    # show all historical activations as current.
    if not key:
        return MealPlanItem.query.filter(False)
    return MealPlanItem.query.filter_by(household_id=int(household_id), cycle_key=key)


def historical_plan_query(household_id: int):
    """Explicit historical query; callers must not use this for current UI."""
    return MealPlanItem.query.filter_by(household_id=int(household_id))


def new_plan_item(*, household_id: int, recipe_id: int, source: str, cycle: dict[str, Any]) -> MealPlanItem:
    key = cycle.get("key")
    if not key:
        raise ValueError("An authoritative current pay period is required.")
    return MealPlanItem(
        household_id=int(household_id), recipe_id=int(recipe_id), source=source,
        cycle_key=key, cycle_start=cycle["start"], cycle_end=cycle["end"],
    )
