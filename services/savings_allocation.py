"""Canonical Goals, Reserves, Flexible Savings, and Wealth authority.

Balances are always derived from the append-only transfer ledger.  PYF remains
the sole authority for how much savings is feasible in a pay cycle.
"""
from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import db
from models import (
    SavingsAllocationRun,
    SavingsDestination,
    SavingsGoal,
    SavingsReserve,
    SavingsTransfer,
)

DEFAULT_RESERVES = (
    ("Emergency Reserve", "emergency", 0),
    ("Vehicle Repair Reserve", "vehicle", 10),
    ("Home & Appliance Reserve", "home_appliance", 20),
    ("Medical Reserve", "medical", 30),
)
VALID_RESERVE_CATEGORIES = {"emergency", "vehicle", "home_appliance", "medical", "custom"}


class SavingsError(ValueError):
    pass


def _destination(household_id: int, destination_id: int) -> SavingsDestination:
    row = SavingsDestination.query.filter_by(id=destination_id, household_id=household_id).first()
    if row is None:
        raise SavingsError("Savings destination not found.")
    return row


def balance_cents(household_id: int, destination_id: int) -> int:
    incoming = sum(int(r.amount_cents) for r in SavingsTransfer.query.filter_by(household_id=household_id, destination_id=destination_id).all())
    outgoing = sum(int(r.amount_cents) for r in SavingsTransfer.query.filter_by(household_id=household_id, source_destination_id=destination_id).all())
    return incoming - outgoing


def _money(cents: int) -> float:
    return round(int(cents) / 100, 2)


def _goal_payload(goal: SavingsGoal, destination: SavingsDestination, *, pay_period_days: int = 14, today: date | None = None) -> dict[str, Any]:
    funded = balance_cents(goal.household_id, goal.destination_id)
    remaining = max(0, int(goal.target_cents) - funded)
    pct = min(100.0, round(funded * 100 / int(goal.target_cents), 1))
    cycles = None
    recommended = 0
    if goal.target_date:
        days = max(0, (goal.target_date - (today or date.today())).days)
        cycles = max(1, math.ceil(days / max(1, pay_period_days)))
        recommended = math.ceil(remaining / cycles) if remaining else 0
    return {
        "id": goal.id, "destination_id": destination.id, "name": destination.name,
        "target_cents": int(goal.target_cents), "target_amount": _money(goal.target_cents),
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "funded_cents": funded, "funded_amount": _money(funded),
        "remaining_cents": remaining, "remaining_amount": _money(remaining),
        "percentage_funded": pct, "priority": destination.priority, "status": goal.status,
        "recommended_contribution_cents": recommended,
        "recommended_contribution": _money(recommended),
        "created_at": goal.created_at.isoformat(),
        "completed_at": goal.completed_at.isoformat() if goal.completed_at else None,
    }


def _reserve_payload(reserve: SavingsReserve, destination: SavingsDestination) -> dict[str, Any]:
    funded = balance_cents(reserve.household_id, reserve.destination_id)
    remaining = max(0, int(reserve.target_cents) - funded)
    return {
        "id": reserve.id, "destination_id": destination.id, "name": destination.name,
        "category": reserve.category, "target_cents": int(reserve.target_cents),
        "target_amount": _money(reserve.target_cents), "funded_cents": funded,
        "funded_amount": _money(funded), "remaining_cents": remaining,
        "remaining_amount": _money(remaining),
        "percentage_funded": min(100.0, round(funded * 100 / int(reserve.target_cents), 1)),
        "priority": destination.priority, "status": reserve.status,
        "allocation_eligible": reserve.status == "active" and funded < int(reserve.target_cents),
    }


def ensure_system_destinations(household_id: int) -> None:
    created = False
    for kind, name, priority in (("flexible", "Flexible Savings", 1000), ("wealth_cash", "Wealth Cash", 1100), ("wealth_investment", "Investments", 1200)):
        if not SavingsDestination.query.filter_by(household_id=household_id, kind=kind).first():
            db.session.add(SavingsDestination(household_id=household_id, kind=kind, name=name, priority=priority)); created = True
    if created: db.session.commit()


def list_state(household_id: int, *, pay_period_days: int = 14) -> dict[str, Any]:
    ensure_system_destinations(household_id)
    destinations = {d.id: d for d in SavingsDestination.query.filter_by(household_id=household_id).all()}
    goals = [_goal_payload(g, destinations[g.destination_id], pay_period_days=pay_period_days) for g in SavingsGoal.query.filter_by(household_id=household_id).all()]
    reserves = [_reserve_payload(r, destinations[r.destination_id]) for r in SavingsReserve.query.filter_by(household_id=household_id).all()]
    goals.sort(key=lambda x: (x["priority"], x["id"]))
    reserves.sort(key=lambda x: (0 if x["category"] == "emergency" else 1, x["priority"], x["id"]))
    simple = {}
    for kind in ("flexible", "wealth_cash", "wealth_investment"):
        d = next(row for row in destinations.values() if row.kind == kind)
        simple[kind] = {"destination_id": d.id, "balance_cents": balance_cents(household_id, d.id), "balance": _money(balance_cents(household_id, d.id))}
    return {"authority": "savings_ledger_v1", "goals": goals, "reserves": reserves, **simple}


def _create_goal_once(household_id: int, *, operation_id: str, name: str, target_cents: int, target_date: date | None, priority: int) -> SavingsGoal:
    if not operation_id or not name.strip() or target_cents <= 0:
        raise SavingsError("Goal name and a positive target are required.")
    def matching_or_error(existing: SavingsGoal) -> SavingsGoal:
        destination = _destination(household_id, existing.destination_id)
        expected = (name.strip(), int(target_cents), target_date, int(priority))
        actual = (destination.name, int(existing.target_cents), existing.target_date, int(destination.priority))
        if actual != expected:
            raise SavingsError("Operation ID was already used for a different Goal.")
        return existing

    existing = SavingsGoal.query.filter_by(household_id=household_id, create_operation_id=operation_id).first()
    if existing: return matching_or_error(existing)
    d = SavingsDestination(household_id=household_id, kind="goal", name=name.strip(), priority=priority)
    db.session.add(d); db.session.flush()
    goal = SavingsGoal(household_id=household_id, destination_id=d.id, create_operation_id=operation_id, target_cents=target_cents, target_date=target_date)
    db.session.add(goal)
    try: db.session.commit(); return goal
    except IntegrityError:
        db.session.rollback(); existing = SavingsGoal.query.filter_by(household_id=household_id, create_operation_id=operation_id).first()
        if existing: return matching_or_error(existing)
        raise


def create_goal(household_id: int, *, operation_id: str, name: str, target_cents: int, target_date: date | None, priority: int) -> SavingsGoal:
    """Create one idempotent Goal, retrying only rolled-back SQLite lock loss."""
    for attempt in range(5):
        try:
            return _create_goal_once(
                household_id, operation_id=operation_id, name=name,
                target_cents=target_cents, target_date=target_date, priority=priority,
            )
        except OperationalError as exc:
            db.session.rollback()
            locked = "database is locked" in str(exc).lower()
            if str(getattr(db.engine.dialect, "name", "")) != "sqlite" or not locked or attempt == 4:
                raise
            time.sleep(0.025 * (2 ** attempt))
    raise RuntimeError("Goal creation retry loop exhausted.")


def create_reserve(household_id: int, *, operation_id: str, name: str, category: str, target_cents: int, priority: int) -> SavingsReserve:
    if not operation_id or category not in VALID_RESERVE_CATEGORIES or not name.strip() or target_cents <= 0:
        raise SavingsError("Reserve name, category, and a positive target are required.")
    existing = SavingsReserve.query.filter_by(household_id=household_id, create_operation_id=operation_id).first()
    if existing: return existing
    d = SavingsDestination(household_id=household_id, kind="reserve", name=name.strip(), priority=priority)
    db.session.add(d); db.session.flush()
    reserve = SavingsReserve(household_id=household_id, destination_id=d.id, create_operation_id=operation_id, category=category, target_cents=target_cents)
    db.session.add(reserve)
    try: db.session.commit(); return reserve
    except IntegrityError:
        db.session.rollback(); existing = SavingsReserve.query.filter_by(household_id=household_id, create_operation_id=operation_id).first()
        if existing: return existing
        raise


def update_goal(household_id: int, goal_id: int, changes: dict[str, Any]) -> None:
    goal = SavingsGoal.query.filter_by(id=goal_id, household_id=household_id).first()
    if not goal: raise SavingsError("Goal not found.")
    d = _destination(household_id, goal.destination_id)
    if "name" in changes and str(changes["name"]).strip(): d.name = str(changes["name"]).strip()
    if "target_cents" in changes:
        if int(changes["target_cents"]) <= 0: raise SavingsError("Target must be positive.")
        goal.target_cents = int(changes["target_cents"])
    if "target_date" in changes: goal.target_date = changes["target_date"]
    if "priority" in changes: d.priority = int(changes["priority"])
    if "status" in changes:
        status = str(changes["status"])
        if status not in {"active", "paused", "completed"}: raise SavingsError("Invalid goal status.")
        goal.status = status
        goal.completed_at = datetime.now(timezone.utc) if status == "completed" else None
    db.session.commit()


def update_reserve(household_id: int, reserve_id: int, changes: dict[str, Any]) -> None:
    reserve = SavingsReserve.query.filter_by(id=reserve_id, household_id=household_id).first()
    if not reserve: raise SavingsError("Reserve not found.")
    d = _destination(household_id, reserve.destination_id)
    if "name" in changes and str(changes["name"]).strip(): d.name = str(changes["name"]).strip()
    if "target_cents" in changes:
        if int(changes["target_cents"]) <= 0: raise SavingsError("Target must be positive.")
        reserve.target_cents = int(changes["target_cents"])
    if "priority" in changes: d.priority = int(changes["priority"])
    if "status" in changes:
        if changes["status"] not in {"active", "paused"}: raise SavingsError("Invalid reserve status.")
        reserve.status = changes["status"]
    db.session.commit()


def transfer(household_id: int, *, operation_id: str, amount_cents: int, source_id: int | None, destination_id: int | None, transfer_type: str, purpose: str = "") -> SavingsTransfer:
    if not operation_id or amount_cents <= 0 or (source_id is None and destination_id is None): raise SavingsError("A valid operation, amount, and source or destination are required.")
    if source_id is not None:
        _destination(household_id, source_id)
        if balance_cents(household_id, source_id) < amount_cents: raise SavingsError("That destination does not have enough saved money.")
    if destination_id is not None: _destination(household_id, destination_id)
    existing = SavingsTransfer.query.filter_by(household_id=household_id, operation_id=operation_id).first()
    if existing:
        if (existing.amount_cents, existing.source_destination_id, existing.destination_id, existing.transfer_type) != (amount_cents, source_id, destination_id, transfer_type):
            raise SavingsError("Operation ID was already used for a different transfer.")
        return existing
    row = SavingsTransfer(household_id=household_id, operation_id=operation_id, amount_cents=amount_cents, source_destination_id=source_id, destination_id=destination_id, transfer_type=transfer_type, purpose=purpose[:200])
    db.session.add(row)
    try: db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = SavingsTransfer.query.filter_by(household_id=household_id, operation_id=operation_id).first()
        if existing: return existing
        raise
    _sync_goal_completion(household_id)
    db.session.commit()
    return row


def allocation_plan(household_id: int, feasible_cents: int, *, pay_period_days: int = 14, today: date | None = None) -> dict[str, Any]:
    today = today or date.today(); remaining_pool = max(0, int(feasible_cents)); rows: list[dict[str, Any]] = []
    state = list_state(household_id, pay_period_days=pay_period_days)
    for reserve in state["reserves"]:
        if remaining_pool <= 0: break
        if not reserve["allocation_eligible"]: continue
        amount = min(remaining_pool, reserve["remaining_cents"])
        rows.append({"destination_id": reserve["destination_id"], "kind": "reserve", "name": reserve["name"], "amount_cents": amount, "reason": "protection"})
        remaining_pool -= amount
    dated = [g for g in state["goals"] if g["status"] == "active" and g["remaining_cents"] > 0 and g["target_date"]]
    impossible = []
    for goal in dated:
        required = goal["recommended_contribution_cents"]
        amount = min(remaining_pool, required)
        if amount: rows.append({"destination_id": goal["destination_id"], "kind": "goal", "name": goal["name"], "amount_cents": amount, "reason": "deadline_protection"})
        remaining_pool -= amount
        if amount < required: impossible.append({"goal_id": goal["id"], "name": goal["name"], "required_cents": required, "available_cents": amount, "message": "Current feasible savings cannot keep this goal on schedule."})
    goals = [g for g in state["goals"] if g["status"] == "active" and g["remaining_cents"] > 0]
    for goal in goals:
        if remaining_pool <= 0: break
        already = sum(r["amount_cents"] for r in rows if r["destination_id"] == goal["destination_id"])
        amount = min(remaining_pool, max(0, goal["remaining_cents"] - already))
        if amount: rows.append({"destination_id": goal["destination_id"], "kind": "goal", "name": goal["name"], "amount_cents": amount, "reason": "priority_waterfall"}); remaining_pool -= amount
    if remaining_pool:
        flex = state["flexible"]
        rows.append({"destination_id": flex["destination_id"], "kind": "flexible", "name": "Flexible Savings", "amount_cents": remaining_pool, "reason": "waterfall_remainder"}); remaining_pool = 0
    return {"authority": "canonical_pyf_v1", "feasible_cents": feasible_cents, "allocated_cents": sum(r["amount_cents"] for r in rows), "allocations": rows, "impossible_schedules": impossible, "truthful": True}


def apply_allocation(household_id: int, *, operation_id: str, cycle_key: str, plan: dict[str, Any]) -> SavingsAllocationRun:
    existing = SavingsAllocationRun.query.filter_by(household_id=household_id, operation_id=operation_id).first()
    if existing: return existing
    existing_cycle = SavingsAllocationRun.query.filter_by(household_id=household_id, cycle_key=cycle_key).first()
    if existing_cycle: return existing_cycle
    run = SavingsAllocationRun(household_id=household_id, operation_id=operation_id, cycle_key=cycle_key, feasible_cents=plan["feasible_cents"], allocated_cents=plan["allocated_cents"])
    try:
        db.session.add(run); db.session.flush()
        for index, item in enumerate(plan["allocations"]):
            db.session.add(SavingsTransfer(household_id=household_id, operation_id=f"{operation_id}:{index}", destination_id=item["destination_id"], amount_cents=item["amount_cents"], transfer_type="pyf_allocation", purpose=item["reason"]))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = SavingsAllocationRun.query.filter_by(household_id=household_id, cycle_key=cycle_key).first()
        if existing: return existing
        raise
    _sync_goal_completion(household_id); db.session.commit(); return run


def _sync_goal_completion(household_id: int) -> None:
    for goal in SavingsGoal.query.filter_by(household_id=household_id).all():
        funded = balance_cents(household_id, goal.destination_id)
        if funded >= goal.target_cents and goal.status == "active":
            goal.status = "completed"; goal.completed_at = datetime.now(timezone.utc)
        elif funded < goal.target_cents and goal.status == "completed":
            goal.status = "active"; goal.completed_at = None


ALIASES = {
    "vehicle": {"vehicle","car","truck","pickup","van","suv","rig","big rig","semi","buggy","atv","transmission","engine","brakes","tires","mechanic"},
    "home_appliance": {"home","house","appliance","hvac","furnace","air conditioner","roof","plumbing","washer","dryer","refrigerator","oven","dishwasher"},
    "medical": {"medical","doctor","hospital","dentist","dental","prescription","medicine","urgent care","therapy","surgery","health"},
}
DISCRETIONARY = {"looks better","cosmetic","upgrade for fun","just because","style","rims","luxury"}


def match_reserve_purpose(text: str) -> dict[str, Any]:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower()); normalized = " ".join(normalized.split())
    if any(signal in normalized for signal in DISCRETIONARY):
        return {"category": None, "confidence": "unresolved", "reason": "discretionary_context"}
    scores = {category: sum(1 for alias in aliases if re.search(r"\b" + re.escape(alias) + r"\b", normalized)) for category, aliases in ALIASES.items()}
    best = max(scores, key=scores.get) if scores else None; score = scores.get(best, 0) if best else 0
    if score == 0: return {"category": None, "confidence": "unresolved", "reason": "no_match"}
    tied = [k for k, value in scores.items() if value == score]
    if len(tied) > 1: return {"category": None, "confidence": "ambiguous", "candidates": tied}
    return {"category": best, "confidence": "high" if score >= 2 else "medium", "reason": "deterministic_alias_context"}
