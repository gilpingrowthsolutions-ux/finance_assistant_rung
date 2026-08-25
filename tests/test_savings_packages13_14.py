from __future__ import annotations

import os
from datetime import date, timedelta

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import app
from extensions import db
from models import Account, Household, SavingsAllocationRun, SavingsDestination, SavingsGoal, SavingsReserve, SavingsTransfer, UserSetting
from services.household_context import household_id
from services.savings_allocation import allocation_plan, apply_allocation, balance_cents, create_goal, create_reserve, list_state, match_reserve_purpose, transfer, update_goal


@pytest.fixture()
def household():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all(); db.create_all()
        hid = household_id()
        db.session.add(Account(household_id=hid, checking_balance=2000, expected_paycheck=1000, pay_period_days=14))
        db.session.add(UserSetting(household_id=hid, key="pyf_long_term_target_percent", value="20")); db.session.commit()
        yield hid


def test_goal_crud_lifecycle_progress_and_unrelated_savings_separation(household):
    with app.app_context():
        goal = create_goal(household, operation_id="goal-create", name="Vacation", target_cents=100_00, target_date=None, priority=2)
        same = create_goal(household, operation_id="goal-create", name="Vacation", target_cents=100_00, target_date=None, priority=2)
        assert same.id == goal.id and SavingsGoal.query.count() == 1
        state = list_state(household); flexible = state["flexible"]["destination_id"]
        transfer(household, operation_id="flex-deposit", amount_cents=50_00, source_id=None, destination_id=flexible, transfer_type="deposit")
        transfer(household, operation_id="goal-deposit", amount_cents=40_00, source_id=None, destination_id=goal.destination_id, transfer_type="deposit")
        row = list_state(household)["goals"][0]
        assert row["funded_cents"] == 40_00 and row["percentage_funded"] == 40.0 and row["remaining_cents"] == 60_00
        update_goal(household, goal.id, {"status": "paused", "priority": 1, "target_cents": 120_00})
        assert list_state(household)["goals"][0]["status"] == "paused"
        update_goal(household, goal.id, {"status": "active"})
        transfer(household, operation_id="goal-complete", amount_cents=80_00, source_id=None, destination_id=goal.destination_id, transfer_type="deposit")
        assert list_state(household)["goals"][0]["status"] == "completed"


def test_deadline_protection_truthfulness_priority_and_completion_rollover(household):
    with app.app_context():
        first = create_goal(household, operation_id="g1", name="Dated", target_cents=300_00, target_date=date.today()+timedelta(days=28), priority=5)
        second = create_goal(household, operation_id="g2", name="Next", target_cents=500_00, target_date=None, priority=1)
        plan = allocation_plan(household, 200_00, pay_period_days=14)
        assert plan["allocations"][0]["destination_id"] == first.destination_id
        assert plan["allocations"][0]["amount_cents"] == 150_00
        assert plan["allocations"][1]["destination_id"] == second.destination_id
        impossible = allocation_plan(household, 100_00, pay_period_days=14)
        assert impossible["impossible_schedules"][0]["required_cents"] == 150_00
        transfer(household, operation_id="complete-first", amount_cents=300_00, source_id=None, destination_id=first.destination_id, transfer_type="deposit")
        rollover = allocation_plan(household, 50_00)
        assert rollover["allocations"][0]["destination_id"] == second.destination_id


def test_reserve_completion_redirect_depletion_and_replenishment(household):
    with app.app_context():
        reserve = create_reserve(household, operation_id="r1", name="Vehicle Repair Reserve", category="vehicle", target_cents=100_00, priority=1)
        goal = create_goal(household, operation_id="g1", name="Computer", target_cents=500_00, target_date=None, priority=1)
        run = apply_allocation(household, operation_id="cycle-1", cycle_key="2026-09-01", plan=allocation_plan(household, 150_00))
        assert run.allocated_cents == 150_00 and balance_cents(household, reserve.destination_id) == 100_00 and balance_cents(household, goal.destination_id) == 50_00
        same_cycle = apply_allocation(household, operation_id="different-click", cycle_key="2026-09-01", plan=allocation_plan(household, 150_00))
        assert same_cycle.id == run.id and SavingsAllocationRun.query.count() == 1
        transfer(household, operation_id="repair", amount_cents=60_00, source_id=reserve.destination_id, destination_id=None, transfer_type="reserve_use", purpose="truck transmission repair")
        assert list_state(household)["reserves"][0]["allocation_eligible"] is True
        replenishment = allocation_plan(household, 50_00)
        assert replenishment["allocations"][0]["destination_id"] == reserve.destination_id


def test_ledger_idempotency_wealth_transfer_is_not_expense_and_cents_exact(household):
    with app.app_context():
        state = list_state(household); cash = state["wealth_cash"]["destination_id"]; invested = state["wealth_investment"]["destination_id"]
        first = transfer(household, operation_id="wealth-deposit", amount_cents=10_01, source_id=None, destination_id=cash, transfer_type="deposit")
        retry = transfer(household, operation_id="wealth-deposit", amount_cents=10_01, source_id=None, destination_id=cash, transfer_type="deposit")
        assert retry.id == first.id and SavingsTransfer.query.count() == 1
        transfer(household, operation_id="invest", amount_cents=3_33, source_id=cash, destination_id=invested, transfer_type="transfer")
        assert balance_cents(household, cash) == 6_68 and balance_cents(household, invested) == 3_33
        assert SavingsTransfer.query.filter_by(operation_id="invest").one().transfer_type == "transfer"


def test_household_isolation_and_no_account_or_setting_balance_authority(household):
    with app.app_context():
        other = Household(public_id="other", legacy_scope_key="other"); db.session.add(other); db.session.commit()
        goal = create_goal(household, operation_id="private", name="Private Goal", target_cents=100_00, target_date=None, priority=1)
        assert SavingsGoal.query.filter_by(household_id=other.id).count() == 0
        assert not any(column.name in {"goal_balance", "reserve_balance", "flexible_savings_balance", "wealth_balance"} for column in Account.__table__.columns)
        assert SavingsDestination.query.filter_by(household_id=other.id, id=goal.destination_id).first() is None


@pytest.mark.parametrize("text,category", [("truck transmission repair","vehicle"),("HVAC stopped working","home_appliance"),("urgent care prescription","medical")])
def test_deterministic_purpose_matching(text, category):
    assert match_reserve_purpose(text)["category"] == category


def test_discretionary_vehicle_language_does_not_authorize_reserve():
    match = match_reserve_purpose("new wheels because they look better")
    assert match["category"] is None and match["confidence"] == "unresolved"


def test_copilot_goal_is_staged_then_confirmed_once(household):
    client = app.test_client()
    staged = client.post("/api/copilot/stage", json={"text": "Add a family vacation goal for $1,200 by 2027-06-01"})
    assert staged.status_code == 200
    actions = staged.get_json()["actions_taken"]
    with app.app_context(): assert SavingsGoal.query.count() == 0
    applied = client.post("/api/copilot/apply", json={"staged_actions": actions, "text": "confirm goal"})
    assert applied.status_code == 200
    retry = client.post("/api/copilot/apply", json={"staged_actions": actions, "text": "confirm goal"})
    conflict_actions = dict(actions)
    conflict_actions["goals_added"] = [dict(actions["goals_added"][0], target_cents=999999, target_amount=9999.99)]
    conflict = client.post("/api/copilot/apply", json={"staged_actions": conflict_actions, "text": "conflicting retry"})
    assert retry.status_code == 200
    assert conflict.status_code == 400
    assert "different Goal" in (conflict.get_json() or {}).get("error", "")
    with app.app_context():
        assert SavingsGoal.query.count() == 1
        assert SavingsGoal.query.one().target_cents == 120_000
