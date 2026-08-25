from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, BehaviorIntelligenceDecision, ExpenseTransaction, IncomePlanVersion, SavingsAllocationRun, SavingsDestination, SavingsGoal, SavingsReserve, SavingsTransfer, ShoppingTripCompletion, UserPreference, UserSetting
from services.household_context import household_id
from services.payday_recap import build_payday_recap


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
CURRENT_START = datetime(2026, 8, 15, tzinfo=timezone.utc)
PREVIOUS_START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def account(**changes):
    values = {"pay_period_days": 14, "expected_paycheck": 1000.0}
    values.update(changes)
    return SimpleNamespace(**values)


def current_safe(**changes):
    values = {"complete": True, "authority": "canonical_pyf_v1", "safe_to_spend_cents": 43210,
              "feasible_savings_cents": 20000, "long_term_savings_target_percent": 20,
              "components": {"protected_buffer": 100.0}, "missing_setup": []}
    values.update(changes)
    return values


def tx(identifier, household, when, description, amount, category="discretionary", plaid=None, source="manual"):
    return ExpenseTransaction(id=identifier, household_id=household, date=when, description=description,
                              amount=amount, category=category, source=source, plaid_transaction_id=plaid)


def build(*, transactions=None, bills=None, transfers=None, destinations=None, runs=None,
          household=1, safe=None, acct=None, income_expectation="default"):
    datasets = {"transactions": transactions or [], "bills": bills or [], "transfers": transfers or []}
    def bounded(name, stamp):
        def query(hid, start, end):
            return [row for row in datasets[name] if row.household_id == hid and start <= getattr(row, stamp) < end]
        return query
    expectation = ({"amount_cents": 100000, "cycle_key": CURRENT_START.date().isoformat(),
                    "authority": "persisted_completed_cycle_plan_test_authority"}
                   if income_expectation == "default" else income_expectation)
    return build_payday_recap(
        household_id=household, account=acct or account(), now=NOW,
        next_income={"known": True, "date": datetime(2026, 8, 29, tzinfo=timezone.utc), "source": "user_pay_schedule"},
        current_safe_snapshot=safe or current_safe(),
        bill_query=bounded("bills", "due_date"), transaction_query=bounded("transactions", "date"),
        transfer_query=bounded("transfers", "created_at"),
        allocation_query=lambda hid, key: [row for row in (runs or []) if row.household_id == hid and row.cycle_key == key],
        destination_query=lambda hid: [row for row in (destinations or []) if row.household_id == hid],
        completed_cycle_income_expectation=expectation,
    )


def base_rows(*, actual_income=1000, need_actual=100, expected_need=100, pyf_actual=20000):
    transactions = [
        tx(1, 1, PREVIOUS_START, "Paycheck", actual_income, "income"),
        tx(2, 1, PREVIOUS_START + timedelta(days=5), "Electric Utility payment", need_actual, "utilities", plaid="linked-electric"),
    ]
    bills = [Bill(id=1, household_id=1, name="Electric Utility", amount=expected_need,
                  due_date=PREVIOUS_START + timedelta(days=5), is_paid=True)]
    destination = SavingsDestination(id=1, household_id=1, kind="reserve", name="Emergency Reserve", priority=1)
    transfers = [] if pyf_actual is None else [SavingsTransfer(id=1, household_id=1, operation_id="pyf", destination_id=1,
        amount_cents=pyf_actual, transfer_type="pyf_allocation", created_at=PREVIOUS_START + timedelta(days=1))]
    runs = [SavingsAllocationRun(id=1, household_id=1, operation_id="run", cycle_key=CURRENT_START.date().isoformat(), feasible_cents=20000, allocated_cents=20000)]
    return transactions, bills, transfers, [destination], runs


def test_completed_cycle_boundaries_are_payday_inclusive_and_next_payday_exclusive():
    rows = base_rows()
    end_income = tx(3, 1, CURRENT_START, "New-cycle paycheck", 1000, "income")
    result = build(transactions=[*rows[0], end_income], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=rows[4])
    assert result["completed_cycle"]["start_date"] == "2026-08-01"
    assert result["completed_cycle"]["end_date"] == "2026-08-15" and result["completed_cycle"]["end_exclusive"] is True
    assert result["completed_cycle_detail"]["confirmed_income_cents"] == 100000
    assert not any(event["label"] == "New-cycle paycheck" for event in result["completed_cycle_detail"]["events"])


def test_no_confirmed_completed_cycle_and_incomplete_evidence_are_unavailable():
    rows = base_rows()
    no_income = build(transactions=rows[0][1:], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=rows[4])
    assert no_income["status"] == "not_ready" and no_income["finish_status"] == "unavailable"
    no_historical_plan = build(transactions=rows[0], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=[])
    assert no_historical_plan["finish_status"] == "unavailable" and "Historical PYF" in no_historical_plan["finish_reasons"][0]
    no_target = build(transactions=rows[0], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=rows[4], safe=current_safe(long_term_savings_target_percent=None))
    assert no_target["finish_status"] == "unavailable" and "PYF target" in no_target["finish_reasons"][0]


def test_completed_cycle_expectation_is_cycle_bound_and_current_account_change_cannot_rewrite_history():
    rows = base_rows(actual_income=1000, need_actual=100, expected_need=100, pyf_actual=20000)
    historical = {"amount_cents": 100000, "cycle_key": CURRENT_START.date().isoformat(),
                  "authority": "persisted_completed_cycle_plan_test_authority"}
    before = build(transactions=rows[0], bills=rows[1], transfers=rows[2],
                   destinations=rows[3], runs=rows[4], acct=account(expected_paycheck=1000),
                   income_expectation=historical)
    after = build(transactions=rows[0], bills=rows[1], transfers=rows[2],
                  destinations=rows[3], runs=rows[4], acct=account(expected_paycheck=1750),
                  income_expectation=historical)
    assert before["finish_status"] == "on_track"
    assert after["finish_status"] == before["finish_status"]
    assert after["finish_amount_cents"] == before["finish_amount_cents"] == 0
    assert after["completed_cycle_detail"]["expected_income_authority"] == historical["authority"]
    assert after["current_safe_to_spend_cents"] == before["current_safe_to_spend_cents"] == 43210


def test_missing_or_wrong_cycle_historical_income_authority_is_not_ready_instead_of_guessed():
    rows = base_rows()
    missing = build(transactions=rows[0], bills=rows[1], transfers=rows[2],
                    destinations=rows[3], runs=rows[4], acct=account(expected_paycheck=9999),
                    income_expectation=None)
    wrong_cycle = build(transactions=rows[0], bills=rows[1], transfers=rows[2],
                        destinations=rows[3], runs=rows[4],
                        income_expectation={"amount_cents": 100000, "cycle_key": "2026-08-29",
                                            "authority": "wrong_cycle_test_authority"})
    for result in (missing, wrong_cycle):
        assert result["status"] == "not_ready" and result["finish_status"] == "unavailable"
        assert "current paycheck settings cannot be used as history" in result["finish_reasons"][0]
        assert result["current_safe_to_spend_cents"] == 43210


@pytest.mark.parametrize(("income","need","expected","pyf","status","amount"), [
    (1000, 80, 100, 20000, "ahead", 2000),
    (900, 100, 100, 20000, "behind", -10000),
    (1000, 100, 100, 20000, "on_track", 0),
])
def test_finish_status_reuses_package15_trajectory(income, need, expected, pyf, status, amount):
    rows = base_rows(actual_income=income, need_actual=need, expected_need=expected, pyf_actual=pyf)
    result = build(transactions=rows[0], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=rows[4])
    assert result["finish_status"] == status and result["finish_amount_cents"] == amount
    assert result["informational_only"] is True and result["safe_to_spend_effect_cents"] == 0
    assert result["current_safe_to_spend_cents"] == 43210
    assert result["current_protected_buffer_cents"] == 10000


def test_settled_bill_reality_replaces_forecast_and_future_need_is_not_favorable():
    rows = base_rows(need_actual=80, expected_need=100)
    future = Bill(id=2, household_id=1, name="Future Water", amount=500,
                  due_date=CURRENT_START + timedelta(days=2), is_paid=False)
    result = build(transactions=rows[0], bills=[*rows[1], future], transfers=rows[2], destinations=rows[3], runs=rows[4])
    matched = [row for row in result["completed_cycle_detail"]["events"] if row.get("supersedes") == "bill:1"]
    assert len(matched) == 1 and matched[0]["provenance"] == "reconciled_manual_plaid"
    assert result["finish_amount_cents"] == 2000
    assert len(result["biggest_changes"]) <= 3 and result["biggest_changes"][0]["kind"] == "settled_needs"


def test_protection_summary_counts_goal_and_reserve_ledger_rows_once_and_classifies_transfers():
    transactions, bills, _, _, runs = base_rows()
    goal = SavingsDestination(id=1, household_id=1, kind="goal", name="Vacation", priority=1)
    reserve = SavingsDestination(id=2, household_id=1, kind="reserve", name="Emergency", priority=2)
    wealth = SavingsDestination(id=3, household_id=1, kind="wealth_investment", name="Investments", priority=3)
    transfers = [
        SavingsTransfer(id=1, household_id=1, operation_id="goal", destination_id=1, amount_cents=12000, transfer_type="pyf_allocation", created_at=PREVIOUS_START+timedelta(days=1)),
        SavingsTransfer(id=2, household_id=1, operation_id="reserve", destination_id=2, amount_cents=8000, transfer_type="pyf_allocation", created_at=PREVIOUS_START+timedelta(days=1)),
        SavingsTransfer(id=3, household_id=1, operation_id="move", source_destination_id=2, destination_id=3, amount_cents=2000, transfer_type="transfer", created_at=PREVIOUS_START+timedelta(days=3)),
        SavingsTransfer(id=4, household_id=1, operation_id="use", source_destination_id=2, amount_cents=1000, transfer_type="reserve_use", created_at=PREVIOUS_START+timedelta(days=4)),
    ]
    result = build(transactions=transactions, bills=bills, transfers=transfers, destinations=[goal,reserve,wealth], runs=runs)
    protected = result["protected_summary"]
    assert protected["actual_protected_cents"] == 20000
    assert protected["goal_funding_cents"] == 12000 and protected["reserve_funding_cents"] == 8000
    assert protected["internal_transfer_cents"] == 2000 and protected["reserve_use_cents"] == 1000


def test_unmet_pyf_is_not_described_as_success_and_no_auto_allocation_or_rollover():
    rows = base_rows(pyf_actual=10000)
    result = build(transactions=rows[0], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=rows[4])
    assert result["finish_status"] == "behind"
    assert result["protected_summary"]["pyf_successfully_protected"] is False
    assert result["automatic_allocation"] is False and result["rollover_or_spending_grant_cents"] == 0


def test_transfer_activity_is_not_spending_and_package16_hypotheticals_are_never_realized():
    rows = base_rows()
    movement = tx(5, 1, PREVIOUS_START+timedelta(days=4), "Brokerage transfer", 500, "investment")
    result = build(transactions=[*rows[0], movement], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=rows[4])
    detail = result["completed_cycle_detail"]
    assert detail["excluded_money_movement_cents"] == 50000
    assert detail["discretionary_actual_cents"] == 0
    assert detail["package16_hypothetical_savings_included_cents"] == 0


def test_household_isolation_and_reconciled_shopping_transaction_counts_once():
    rows = base_rows()
    shopping = tx(8, 1, PREVIOUS_START+timedelta(days=7), "Finished Shopping", 75, "grocery", plaid="linked-trip")
    other = tx(9, 2, PREVIOUS_START+timedelta(days=7), "Other household", 999, "income")
    result = build(transactions=[*rows[0], shopping, other], bills=rows[1], transfers=rows[2], destinations=rows[3], runs=rows[4], household=1)
    assert result["completed_cycle_detail"]["transaction_count"] == 3
    assert sum(1 for row in result["completed_cycle_detail"]["events"] if row["key"] == "transaction:8") == 1
    assert not any(row["label"] == "Other household" for row in result["completed_cycle_detail"]["events"])


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all(); db.create_all(); hid=household_id(); now=datetime.now(timezone.utc); current_start=(now.date()-timedelta(days=7)); previous_start=current_start-timedelta(days=14)
        account_row=Account(household_id=hid, checking_balance=1500, expected_paycheck=1000, pay_period_days=14); db.session.add(account_row); db.session.flush()
        reserve=SavingsDestination(household_id=hid,kind="reserve",name="Emergency",priority=1);db.session.add(reserve);db.session.flush()
        db.session.add_all([
            IncomePlanVersion(household_id=hid,operation_id="recap-historical-plan",expected_income_cents=100000,effective_at=datetime.combine(previous_start,datetime.min.time(),tzinfo=timezone.utc),source="test_confirmation"),
            UserSetting(household_id=hid,key=NEXT_PAYDAY_SETTING_KEY,value=(current_start+timedelta(days=14)).isoformat()),
            UserSetting(household_id=hid,key=PYF_TARGET_SETTING_KEY,value="20"),UserSetting(household_id=hid,key=SAFE_BUFFER_SETTING_KEY,value="100"),
            UserPreference(household_id=hid,key="baseline_grocery_cost",value="100"),
            Bill(household_id=hid,name="Fuel",amount=50,due_date=now+timedelta(days=2),is_gas_estimate=True),
            ExpenseTransaction(household_id=hid,description="Paycheck",amount=1000,category="income",source="manual",local_account_id=account_row.id,date=datetime.combine(previous_start,datetime.min.time(),tzinfo=timezone.utc)),
            SavingsAllocationRun(household_id=hid,operation_id="run",cycle_key=current_start.isoformat(),feasible_cents=20000,allocated_cents=20000),
            SavingsTransfer(household_id=hid,operation_id="pyf",destination_id=reserve.id,amount_cents=20000,transfer_type="pyf_allocation",created_at=datetime.combine(previous_start+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)),
        ]);db.session.commit()
    return app.test_client()


def test_get_endpoint_is_stable_and_creates_no_financial_or_intelligence_rows(client):
    models=(Account,IncomePlanVersion,Bill,ExpenseTransaction,SavingsTransfer,SavingsAllocationRun,SavingsGoal,SavingsReserve,UserSetting,BehaviorIntelligenceDecision,ShoppingTripCompletion)
    before_safe=client.get("/api/budget/summary").get_json()["safe_to_spend"]["safe_to_spend_cents"]
    with app.app_context(): before=tuple(model.query.count() for model in models)
    first=client.get("/api/payday-recap");second=client.get("/api/payday-recap")
    assert first.status_code==second.status_code==200 and first.get_json()==second.get_json()
    assert first.get_json()["current_safe_to_spend_cents"]==before_safe
    with app.app_context(): after=tuple(model.query.count() for model in models)
    assert after==before
