from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, REQUIRED_EXPENSE_REVIEWED, REQUIRED_EXPENSE_REVIEW_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, IncomePlanVersion, SavingsAllocationRun, SavingsDestination, SavingsTransfer, UserPreference, UserSetting
from services.household_context import household_id
from services.paycheck_timeline import build_paycheck_timeline, resolve_cycle


NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def _account(**changes):
    values = {"pay_period_days": 14, "expected_paycheck": 1000.0}
    values.update(changes)
    return SimpleNamespace(**values)


def _snapshot(feasible=20000):
    return {"complete": True, "period_income_cents": 100000, "feasible_savings_cents": feasible, "authority": "canonical_pyf_v1"}


def _build(*, txns=None, bills=None, transfers=None, destinations=None, household=1, now=NOW, pyf=None):
    seen = []
    def scoped(rows):
        def query(hid, *bounds):
            seen.append(hid)
            selected = [row for row in rows if row.household_id == hid]
            if len(bounds) == 2 and isinstance(bounds[0], datetime):
                start, end = bounds
                def stamp(row):
                    return getattr(row, "date", None) or getattr(row, "due_date", None) or getattr(row, "created_at", None)
                selected = [row for row in selected if stamp(row) is not None and start <= stamp(row) < end]
            return selected
        return query
    result = build_paycheck_timeline(
        household_id=household, account=_account(), now=now,
        next_income={"known": True, "date": datetime(2026, 8, 28, tzinfo=timezone.utc), "source": "user_pay_schedule"},
        pyf_snapshot=pyf or _snapshot(), bill_query=scoped(bills or []),
        transaction_query=scoped(txns or []), transfer_query=scoped(transfers or []),
        allocation_query=lambda hid, key: [], destination_query=scoped(destinations or []),
    )
    return result, seen


def test_cycle_start_is_inclusive_and_next_payday_exclusive():
    on_payday = resolve_cycle(account=_account(), now=NOW, next_income={
        "known": True, "date": datetime(2026, 8, 21, tzinfo=timezone.utc), "source": "user_pay_schedule"})
    assert on_payday["start_date"] == "2026-08-21"
    assert on_payday["end_date"] == "2026-09-04" and on_payday["end_exclusive"] is True
    start_tx = ExpenseTransaction(id=1, household_id=1, description="Paycheck", amount=1000, category="income", source="manual", date=datetime(2026, 8, 14, tzinfo=timezone.utc))
    next_tx = ExpenseTransaction(id=2, household_id=1, description="Next paycheck", amount=1000, category="income", source="manual", date=datetime(2026, 8, 28, tzinfo=timezone.utc))
    result, _ = _build(txns=[start_tx, next_tx])
    assert {row["key"] for row in result["events"]} == {"transaction:1", "forecast:pyf"}
    assert not any(row["key"] == "transaction:2" for row in result["events"])


def test_chronology_states_and_confirmed_income_supersedes_forecast():
    rows = [
        ExpenseTransaction(id=3, household_id=1, description="Upcoming bonus", amount=50, category="income", source="manual", date=NOW + timedelta(days=2)),
        ExpenseTransaction(id=2, household_id=1, description="Confirmed paycheck", amount=1000, category="income", source="manual", date=NOW - timedelta(days=7)),
        ExpenseTransaction(id=4, household_id=1, description="Groceries", amount=40, category="grocery", source="manual", date=NOW - timedelta(days=1)),
    ]
    result, _ = _build(txns=rows)
    assert [row["occurred_at"] for row in result["events"]] == sorted(row["occurred_at"] for row in result["events"])
    assert {row["state"] for row in result["events"]} == {"completed", "upcoming_confirmed", "forecast"}
    assert not any(row["key"] == "forecast:cycle_income" for row in result["events"])


def test_reconciled_manual_plaid_is_one_economic_event():
    linked = ExpenseTransaction(id=8, household_id=1, description="Rent", amount=900, category="housing", source="manual", plaid_transaction_id="plaid-8", date=NOW - timedelta(days=1))
    bill = Bill(id=9, household_id=1, name="Rent", amount=950, due_date=NOW - timedelta(days=1), is_paid=True)
    result, _ = _build(txns=[linked], bills=[bill])
    matching = [row for row in result["events"] if row.get("supersedes") == "bill:9"]
    assert len(matching) == 1 and matching[0]["provenance"] == "reconciled_manual_plaid"
    assert result["trajectory"]["components"]["settled_needs_variance_cents"] == 5000


def test_unsettled_need_is_not_favorable_and_above_forecast_is_unfavorable():
    future = Bill(id=1, household_id=1, name="Electric", amount=100, due_date=NOW + timedelta(days=2), is_paid=False)
    result, _ = _build(bills=[future], pyf=_snapshot(0))
    assert result["trajectory"]["components"]["settled_needs_variance_cents"] == 0
    assert result["trajectory"]["status"] == "behind"  # missing expected income, never false-ahead
    paid = Bill(id=2, household_id=1, name="Water Utility", amount=100, due_date=NOW - timedelta(days=1), is_paid=True)
    actual = ExpenseTransaction(id=2, household_id=1, description="Water Utility payment", amount=125, category="utilities", source="manual", date=NOW - timedelta(days=1))
    above, _ = _build(txns=[actual], bills=[paid], pyf=_snapshot(0))
    assert above["trajectory"]["components"]["settled_needs_variance_cents"] == -2500


def test_pyf_shortfall_is_unfavorable_and_ledger_progress_is_canonical():
    dest = SavingsDestination(id=1, household_id=1, name="Emergency Reserve", kind="reserve", priority=1, active=True)
    transfer = SavingsTransfer(id=1, household_id=1, operation_id="alloc", destination_id=1, amount_cents=5000, transfer_type="pyf_allocation", created_at=NOW - timedelta(days=1))
    income = ExpenseTransaction(id=1, household_id=1, description="Paycheck", amount=1000, category="income", source="manual", date=NOW - timedelta(days=7))
    result, _ = _build(txns=[income], transfers=[transfer], destinations=[dest])
    assert result["trajectory"]["components"]["pyf_progress_variance_cents"] == -15000
    assert result["trajectory"]["status"] == "behind"
    assert any(row["provenance"] == "packages_13_14_savings_ledger" for row in result["events"])


def test_household_scope_is_applied_to_every_authoritative_query():
    own = ExpenseTransaction(id=1, household_id=1, description="Own", amount=1000, category="income", source="manual", date=NOW - timedelta(days=7))
    other = ExpenseTransaction(id=2, household_id=2, description="Other", amount=9999, category="income", source="manual", date=NOW - timedelta(days=7))
    result, seen = _build(txns=[own, other], household=1)
    assert all(value == 1 for value in seen)
    assert not any(row["label"] == "Other" for row in result["events"])


def test_missing_authority_is_unavailable_without_fabricated_values():
    result = build_paycheck_timeline(
        household_id=1, account=_account(pay_period_days=0), now=NOW,
        next_income={"known": False}, pyf_snapshot={"complete": False, "missing_setup": ["payday"]},
        bill_query=lambda *_: [], transaction_query=lambda *_: [], transfer_query=lambda *_: [],
        allocation_query=lambda *_: [], destination_query=lambda *_: [],
    )
    assert result["trajectory"]["status"] == "unavailable"
    assert result["trajectory"]["amount_cents"] is None and result["events"] == []


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all(); db.create_all()
        hid = household_id()
        today = datetime.now(timezone.utc).date()
        db.session.add(Account(household_id=hid, checking_balance=1500, expected_paycheck=1000, pay_period_days=14))
        db.session.add(IncomePlanVersion(household_id=hid, operation_id="timeline-plan", expected_income_cents=100000, effective_at=datetime.now(timezone.utc)-timedelta(days=30), source="test_confirmation"))
        db.session.add_all([
            UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY, value=(today + timedelta(days=7)).isoformat()),
            UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
            UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100"),
            UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="100"),
            Bill(household_id=hid, name="Fuel", amount=50, due_date=datetime.now(timezone.utc)+timedelta(days=2), is_gas_estimate=True),
        ])
        db.session.commit()
    return app.test_client()


def test_endpoint_reuses_schedule_is_read_only_and_cannot_change_safe_to_spend(client):
    before = client.get("/api/budget/summary").get_json()["safe_to_spend"]["safe_to_spend_cents"]
    with app.app_context():
        counts_before = tuple(model.query.count() for model in (Account, Bill, ExpenseTransaction, SavingsTransfer, SavingsAllocationRun, UserSetting))
    first = client.get("/api/paycheck-timeline")
    second = client.get("/api/paycheck-timeline")
    assert first.status_code == second.status_code == 200
    payload = first.get_json()
    assert payload["cycle"]["schedule_source"] == "user_pay_schedule"
    assert payload["trajectory"]["informational_only"] is True
    assert payload["safe_to_spend_proof"]["trajectory_affects_safe_to_spend"] is False
    assert client.get("/api/budget/summary").get_json()["safe_to_spend"]["safe_to_spend_cents"] == before
    with app.app_context():
        counts_after = tuple(model.query.count() for model in (Account, Bill, ExpenseTransaction, SavingsTransfer, SavingsAllocationRun, UserSetting))
    assert counts_after == counts_before
