from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Household, IncomePlanVersion, UserPreference, UserSetting
from services.household_context import household_id
from services.income_plan import IncomePlanError, record_income_plan, resolve_income_plan


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
NEXT = datetime(2026, 8, 29, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_db():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all(); db.create_all()
    yield


def add_plan(hid, op, cents, *, now=NOW, next_payday=None, source="test_confirmation"):
    row, created = record_income_plan(hid, operation_id=op, expected_income_cents=cents,
                                      now=now, next_payday=next_payday, source=source)
    db.session.commit()
    return row, created


def test_initial_establishment_is_current_cents_exact_and_household_isolated():
    with app.app_context():
        h1=Household(public_id="plan-h1");h2=Household(public_id="plan-h2");db.session.add_all([h1,h2]);db.session.flush()
        add_plan(h1.id,"one",100001);add_plan(h2.id,"two",250099)
        assert resolve_income_plan(h1.id,at=NOW).expected_income_cents==100001
        assert resolve_income_plan(h2.id,at=NOW).expected_income_cents==250099


def test_retry_is_one_version_and_conflicting_operation_reuse_fails_closed():
    with app.app_context():
        hid=household_id();first,created=add_plan(hid,"retry",100000)
        second,created_again=add_plan(hid,"retry",100000)
        assert created is True and created_again is False and second.id==first.id
        assert IncomePlanVersion.query.filter_by(household_id=hid).count()==1
        with pytest.raises(IncomePlanError): add_plan(hid,"retry",145000)


def test_existing_change_waits_for_next_payday_and_multiple_edits_are_append_only_deterministic():
    with app.app_context():
        hid=household_id();add_plan(hid,"initial",100000)
        add_plan(hid,"edit-1",130000,now=NOW+timedelta(hours=1),next_payday=NEXT)
        add_plan(hid,"edit-2",145000,now=NOW+timedelta(hours=2),next_payday=NEXT)
        assert resolve_income_plan(hid,at=NEXT-timedelta(microseconds=1)).expected_income_cents==100000
        assert resolve_income_plan(hid,at=NEXT).expected_income_cents==145000
        assert [r.expected_income_cents for r in IncomePlanVersion.query.order_by(IncomePlanVersion.id).all()]==[100000,130000,145000]


def test_legacy_account_float_never_becomes_canonical_or_historical():
    with app.app_context():
        hid=household_id();db.session.add(Account(household_id=hid,expected_paycheck=9999,pay_period_days=14));db.session.commit()
        assert resolve_income_plan(hid,at=NOW) is None
        assert IncomePlanVersion.query.count()==0


def test_onboarding_establishes_once_and_settings_edit_is_future_effective():
    client=app.test_client()
    # This is a live API integration test, so its current cycle must be
    # relative to the runtime clock rather than the retired 2026 fixture date.
    # The initial onboarding write establishes the plan immediately; a later
    # Settings write must remain pending until this real future boundary.
    now = datetime.now(timezone.utc)
    next_boundary = (now + timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    next_date=next_boundary.date().isoformat()
    initial=client.post("/api/onboarding/complete",json={
        "checking_balance":2000,"pay_period_days":14,"expected_paycheck":1000,
        "expected_paycheck_operation_id":"onboard-once","next_payday":next_date,
        "long_term_savings_target_percent":10,"protected_buffer":100,
        "baseline_grocery_cost":100,"baseline_fuel_cost":50,
    })
    assert initial.status_code==200
    reviewed = client.post("/api/onboarding/required-expenses-review", json={"answer": "yes", "review_complete": True})
    assert reviewed.status_code == 200
    retry=client.post("/api/onboarding/complete",json={"expected_paycheck":1000,"expected_paycheck_operation_id":"onboard-once","next_payday":next_date})
    assert retry.status_code==200
    before=client.get("/api/budget/summary").get_json()
    changed=client.post("/api/account/update",json={"expected_paycheck":1450,"expected_paycheck_operation_id":"settings-once"})
    assert changed.status_code==200
    replay=client.post("/api/account/update",json={"expected_paycheck":1450,"expected_paycheck_operation_id":"settings-once"})
    assert replay.status_code==200 and replay.get_json()["income_plan_created"] is False
    with app.app_context():
        hid=household_id();assert IncomePlanVersion.query.filter_by(household_id=hid).count()==2
        assert resolve_income_plan(hid,at=now+timedelta(minutes=1)).expected_income_cents==100000
        assert int((changed.get_json()["income_plan"]["pending"])["expected_income_cents"])==145000
        assert resolve_income_plan(hid,at=next_boundary).expected_income_cents==145000
    after=client.get("/api/budget/summary").get_json()
    assert before["safe_to_spend"]["period_income_cents"]==after["safe_to_spend"]["period_income_cents"]==100000
    assert before["safe_to_spend"]["safe_to_spend_cents"]==after["safe_to_spend"]["safe_to_spend_cents"]
    assert client.get("/api/paycheck-timeline").get_json()["trajectory"]["components"]["confirmed_income_variance_cents"]==-100000


def test_missing_plan_keeps_pyf_setup_needed_despite_legacy_default():
    with app.app_context():
        hid=household_id();db.session.add(Account(household_id=hid,checking_balance=2000,expected_paycheck=2000,pay_period_days=14));
        db.session.add_all([UserSetting(household_id=hid,key=NEXT_PAYDAY_SETTING_KEY,value=NEXT.date().isoformat()),UserSetting(household_id=hid,key=PYF_TARGET_SETTING_KEY,value="10"),UserSetting(household_id=hid,key=SAFE_BUFFER_SETTING_KEY,value="100"),UserPreference(household_id=hid,key="baseline_grocery_cost",value="100")]);db.session.commit()
    safe=client=app.test_client().get("/api/budget/summary").get_json()["safe_to_spend"]
    assert safe["state"]=="needs_setup" and "current_period_income" in safe["missing_setup"]
