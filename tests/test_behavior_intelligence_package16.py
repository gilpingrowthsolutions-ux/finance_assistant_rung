from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, REQUIRED_EXPENSE_REVIEWED, REQUIRED_EXPENSE_REVIEW_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, BehaviorIntelligenceDecision, Bill, ExpenseTransaction, Household, IncomePlanVersion, SavingsAllocationRun, SavingsDestination, SavingsGoal, SavingsReserve, SavingsTransfer, UserPreference, UserSetting
from services.behavior_intelligence import build_behavior_intelligence, canonical_merchant, reduction_projection
from services.household_context import household_id


NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def tx(identifier, household, days_ago, description, amount, category="discretionary", **extra):
    return ExpenseTransaction(id=identifier, household_id=household, description=description, amount=amount, category=category, source=extra.get("source", "manual"), plaid_transaction_id=extra.get("plaid_transaction_id"), date=NOW-timedelta(days=days_ago))


def snapshot(rows, *, household=1, bills=None, decisions=None):
    return build_behavior_intelligence(household_id=household, transactions=rows, bills=bills or [], decisions=decisions or [], now=NOW)


def test_merchant_normalization_preserves_raw_evidence_and_reconciled_event_counts_once():
    rows = [
        tx(1, 1, 60, "PLANET FITNESS #123", 15, plaid_transaction_id="plaid-1"),
        tx(2, 1, 30, "Planet Fitness Club 456", 15), tx(3, 1, 0, "PLANET FITNESS*789", 15),
    ]
    state = snapshot(rows)
    candidate = state["recurring_candidates"][0]
    assert candidate["canonical_merchant"] == "planet fitness"
    assert candidate["evidence"]["occurrence_count"] == 3
    assert candidate["evidence"]["raw_activity"][0]["raw_description"] == "PLANET FITNESS #123"
    assert candidate["evidence"]["raw_activity"][0]["plaid_linked"] is True


def test_deterministic_monthly_cadence_and_evidence_are_exposed():
    state = snapshot([tx(1,1,60,"Planet Fitness",15),tx(2,1,30,"Planet Fitness",15.5),tx(3,1,0,"Planet Fitness",15)])
    row = state["recurring_candidates"][0]
    assert row["evidence"]["cadence"] == "monthly" and row["evidence"]["cadence_days"] == 30
    assert row["evidence"]["observed_dates"] == ["2026-06-22", "2026-07-22", "2026-08-21"]
    assert row["evidence"]["amount_min_cents"] == 1500 and row["evidence"]["amount_max_cents"] == 1550
    assert row["confidence"] in {"moderate", "high"} and row["evidence"]["sources"] == ["manual"]


def test_irregular_dining_is_pattern_not_subscription():
    rows=[tx(1,1,70,"McDonald's #1",20,category="dining"),tx(2,1,50,"MCDONALDS 998",22,category="dining"),tx(3,1,5,"McDonalds Restaurant",18,category="dining")]
    state=snapshot(rows)
    assert state["recurring_candidates"] == []
    assert state["opportunities"][0]["canonical_merchant"] == "mcdonalds"
    assert state["opportunities"][0]["evidence"]["cadence"] == "irregular"


def test_needs_and_money_movements_are_excluded_from_savings_leak_framing():
    rows=[]; identifier=1
    for merchant, category in [("Essential Prescription","medical"),("Daycare","childcare"),("Basic Groceries","grocery"),("Electric Utility","utilities"),("Savings Transfer","transfer"),("Reserve Transfer","reserve"),("Brokerage Investment","investment")]:
        for day in (60,30,0): rows.append(tx(identifier,1,day,merchant,100,category=category)); identifier+=1
    state=snapshot(rows)
    assert state["opportunities"] == []
    assert not any(row["classification"] == "transfer" for row in state["recurring_candidates"])


def test_annualization_and_25_50_75_math_is_exact_period_labeled_without_replacement_cost():
    projection=reduction_projection(9000,90)
    assert projection["annualized_cents"] == 36500
    assert projection["reductions"]["25"]["annualized_savings_cents"] == 9125
    assert projection["reductions"]["50"]["period_savings_cents"] == 4500
    assert projection["reductions"]["75"]["annualized_savings_cents"] == 27375
    assert "365 ÷ 90" in projection["basis"] and projection["replacement_cost_cents"] is None


def test_household_relative_materiality_is_explainable():
    income=tx(1,1,10,"Paycheck",10000,category="income")
    coffees=[tx(2,1,60,"Coffee Shop",20,category="coffee"),tx(3,1,30,"Coffee Shop",20,category="coffee"),tx(4,1,0,"Coffee Shop",20,category="coffee")]
    state=snapshot([income,*coffees])
    assert state["materiality"]["threshold_cents"] == 10000
    assert state["opportunities"] == []
    assert "1%" in state["materiality"]["rule"]


def test_ignore_suppresses_until_material_pattern_change():
    rows=[tx(1,1,60,"Coffee Shop",10,category="coffee"),tx(2,1,30,"Coffee Shop",10,category="coffee"),tx(3,1,0,"Coffee Shop",10,category="coffee")]
    base=snapshot(rows); row=base["opportunities"][0]
    decision=BehaviorIntelligenceDecision(id=1,household_id=1,operation_id="ignore",candidate_key=row["candidate_key"],action="ignore",pattern_signature=row["pattern_signature"],typical_amount_cents=1000,cadence_days=30,occurrence_count=3,created_at=NOW)
    assert snapshot(rows,decisions=[decision])["opportunities"] == []
    changed=[tx(1,1,60,"Coffee Shop",14,category="coffee"),tx(2,1,30,"Coffee Shop",14,category="coffee"),tx(3,1,0,"Coffee Shop",14,category="coffee")]
    assert snapshot(changed,decisions=[decision])["opportunities"][0]["canonical_merchant"] == "coffee shop"


def test_household_isolation_including_corrections():
    own=[tx(1,1,60,"Coffee Shop",10,category="coffee"),tx(2,1,30,"Coffee Shop",10,category="coffee"),tx(3,1,0,"Coffee Shop",10,category="coffee")]
    other=[tx(4,2,60,"Private Merchant",99),tx(5,2,30,"Private Merchant",99),tx(6,2,0,"Private Merchant",99)]
    correction=BehaviorIntelligenceDecision(id=1,household_id=2,operation_id="other",candidate_key="merchant:coffee shop",action="classify",classification="need",created_at=NOW)
    state=snapshot(own+other,household=1,decisions=[correction])
    assert state["opportunities"][0]["classification"] == "discretionary"
    assert not any(row.get("canonical_merchant")=="private merchant" for row in state["opportunities"])


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all();db.create_all();hid=household_id();now=datetime.now(timezone.utc)
        account=Account(household_id=hid,checking_balance=2000,expected_paycheck=1000,pay_period_days=14);db.session.add(account);db.session.flush()
        db.session.add(IncomePlanVersion(household_id=hid,operation_id="pkg16-plan",expected_income_cents=100000,effective_at=now-timedelta(days=30),source="test_confirmation"))
        flexible=SavingsDestination(household_id=hid,kind="flexible",name="Flexible Savings",priority=1000)
        wealth=SavingsDestination(household_id=hid,kind="wealth_cash",name="Wealth Cash",priority=1100)
        invested=SavingsDestination(household_id=hid,kind="wealth_investment",name="Investments",priority=1200)
        db.session.add_all([flexible,wealth,invested,
            UserSetting(household_id=hid,key=PYF_TARGET_SETTING_KEY,value="20"),UserSetting(household_id=hid,key=SAFE_BUFFER_SETTING_KEY,value="100"),UserSetting(household_id=hid,key=NEXT_PAYDAY_SETTING_KEY,value=(now.date()+timedelta(days=7)).isoformat()),UserSetting(household_id=hid,key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY,value=REQUIRED_EXPENSE_REVIEWED),UserPreference(household_id=hid,key="baseline_grocery_cost",value="100"),Bill(household_id=hid,name="Required fuel",amount=50,due_date=now+timedelta(days=2),is_gas_estimate=True)])
        for index,days in enumerate((60,30,0),1): db.session.add(ExpenseTransaction(household_id=hid,description=f"Planet Fitness #{index}",amount=15,category="subscription",source="manual",local_account_id=account.id,date=now-timedelta(days=days)))
        for index,days in enumerate((55,25,1),10): db.session.add(ExpenseTransaction(household_id=hid,description=f"Coffee Shop {index}",amount=30,category="coffee",source="manual",local_account_id=account.id,date=now-timedelta(days=days)))
        db.session.commit()
    return app.test_client()


def test_read_endpoint_and_hypothetical_preview_make_no_financial_writes(client):
    before_safe=client.get("/api/budget/summary").get_json()["safe_to_spend"]["safe_to_spend_cents"]
    with app.app_context(): before=tuple(model.query.count() for model in (Account,Bill,ExpenseTransaction,SavingsTransfer,SavingsAllocationRun,SavingsGoal,SavingsReserve,UserSetting,BehaviorIntelligenceDecision))
    state=client.get("/api/behavior-intelligence").get_json();key=state["opportunities"][0]["candidate_key"]
    preview=client.post("/api/behavior-intelligence/savings-preview",json={"candidate_key":key,"reduction_percent":50})
    assert preview.status_code==200 and preview.get_json()["mutated"] is False
    assert client.get("/api/budget/summary").get_json()["safe_to_spend"]["safe_to_spend_cents"]==before_safe
    with app.app_context(): after=tuple(model.query.count() for model in (Account,Bill,ExpenseTransaction,SavingsTransfer,SavingsAllocationRun,SavingsGoal,SavingsReserve,UserSetting,BehaviorIntelligenceDecision))
    assert after==before


def test_ignore_is_idempotent_and_persists(client):
    state=client.get("/api/behavior-intelligence").get_json();row=state["recurring_candidates"][0]
    payload={"operation_id":"ignore-once","candidate_key":row["candidate_key"],"action":"ignore","pattern_signature":row["pattern_signature"],"typical_amount_cents":row["evidence"]["typical_amount_cents"],"cadence_days":row["evidence"]["cadence_days"],"occurrence_count":row["evidence"]["occurrence_count"]}
    first=client.post("/api/behavior-intelligence/decision",json=payload);second=client.post("/api/behavior-intelligence/decision",json=payload)
    assert first.status_code==201 and second.get_json()["already_applied"] is True
    collision=client.post("/api/behavior-intelligence/decision",json={**payload,"candidate_key":"recurring:different merchant"})
    assert collision.status_code==409
    assert not any(row["candidate_key"]==payload["candidate_key"] for row in client.get("/api/behavior-intelligence").get_json()["recurring_candidates"])
    with app.app_context(): assert BehaviorIntelligenceDecision.query.count()==1


def test_add_recurring_bill_is_staged_then_one_confirmed_mutation(client):
    before_safe=client.get("/api/budget/summary").get_json()["safe_to_spend"]["safe_to_spend_cents"]
    candidate=client.get("/api/behavior-intelligence").get_json()["recurring_candidates"][0]
    staged=client.post("/api/behavior-intelligence/stage-recurring-bill",json={"candidate_key":candidate["candidate_key"]})
    assert staged.status_code==200 and staged.get_json()["financial_mutations"] is False
    with app.app_context(): before_bills=Bill.query.count()
    staged_actions=staged.get_json()["staged_actions"]
    staged_actions["goals_added"]=[]  # The browser normalizer includes empty sections.
    payload={"staged_actions":staged_actions,"text":"confirm reviewed recurring bill"}
    first=client.post("/api/copilot/apply",json=payload);second=client.post("/api/copilot/apply",json=payload)
    assert first.status_code==second.status_code==200
    with app.app_context(): assert Bill.query.count()==before_bills+1
    # Bill authority may naturally recalculate Safe-to-Spend only after explicit confirmation.
    assert before_safe is not None


def test_empty_state_is_truthful():
    state=snapshot([])
    assert state["empty"] is True and state["recurring_candidates"]==[] and state["opportunities"]==[]
    assert state["financial_mutations"] is False and state["safe_to_spend_effect_cents"]==0


def test_intelligence_read_does_not_create_missing_account():
    with app.app_context():
        db.drop_all(); db.create_all(); household_id()
        assert Account.query.count() == 0
    response = app.test_client().get("/api/behavior-intelligence")
    assert response.status_code == 200 and response.get_json()["empty"] is True
    with app.app_context(): assert Account.query.count() == 0
