"""Bounded test support for non-financial meal-plan behavior.

Tests of plan authority deliberately exercise the real income-resolution path.
Other tests may install this deterministic application-boundary cycle so their
subject remains recipes, carts, or Copilot rather than schedule construction.
"""
from __future__ import annotations

from datetime import datetime, timezone

from models import MealPlanItem


def deterministic_cycle() -> dict:
    return {
        "available": True,
        "key": "paycheck:2026-08-14:2026-08-28",
        "start": datetime(2026, 8, 14, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 28, tzinfo=timezone.utc),
    }


def install_current_cycle(monkeypatch=None) -> dict:
    import app as app_module
    cycle = deterministic_cycle()
    resolver = lambda: dict(cycle)
    if monkeypatch is None:
        app_module._current_meal_plan_cycle = resolver
    else:
        monkeypatch.setattr(app_module, "_current_meal_plan_cycle", resolver)
    return cycle


def current_plan_item(*, household_id: int, recipe_id: int, source: str = "user") -> MealPlanItem:
    cycle = deterministic_cycle()
    return MealPlanItem(
        household_id=household_id, recipe_id=recipe_id, source=source,
        cycle_key=cycle["key"], cycle_start=cycle["start"], cycle_end=cycle["end"],
    )


def historical_plan_item(*, household_id: int, recipe_id: int, source: str = "user") -> MealPlanItem:
    return MealPlanItem(
        household_id=household_id, recipe_id=recipe_id, source=source,
        cycle_key="paycheck:2026-07-31:2026-08-14",
        cycle_start=datetime(2026, 7, 31, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
