"""Package 11 Phase A — onboarding writes to canonical authorities.

Proves that first-launch onboarding persists every onboarding input to the
same canonical financial/store/preference authorities used elsewhere in the
product, never invents readiness, preserves existing data, and keeps
households isolated. Runs against an in-memory disposable SQLite DB only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Disposable, isolated database + a household-context secret for isolation tests.
os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "package11-test-secret"

from app import (  # noqa: E402
    Account,
    Bill,
    ExpenseTransaction,
    Household,
    HouseholdShoppingDefault,
    IncomePlanVersion,
    PYF_TARGET_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY,
    LOCATION_SHARING_SETTING_KEY,
    UserPreference,
    UserSetting,
    app,
    db,
)
from services.financial_state import get_household_account  # noqa: E402
from services.household_context import household_id as current_household_id  # noqa: E402
from services.plaid_foundation import get_plaid_connection_status  # noqa: E402
from services.selected_store import get_selected_store, select_store  # noqa: E402

client = app.test_client()
app.testing = True

HOUSEHOLD_SECRET = b"package11-test-secret"

FULL_FINANCIAL_PAYLOAD = {
    "expected_paycheck_operation_id": "package11-onboarding-plan",
    "household_size": 3,
    "favorite_proteins": ["chicken", "salmon"],
    "dietary_restrictions": ["low carb"],
    "allergies": ["peanuts"],
    "checking_balance": 1750.50,
    "pay_period_days": 14,
    "expected_paycheck": 2000.0,
    "next_payday": (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat(),
    "long_term_savings_target_percent": 12.5,
    "protected_buffer": 150.0,
    "baseline_grocery_cost": 240.0,
    "baseline_fuel_cost": 75.0,
    "recurring_bills": [
        {"name": "Phone", "amount": 95.0},
        {"name": "Internet", "amount": 70.0},
        {"name": "Utilities", "amount": 140.0},
    ],
}


def _reset_db() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _seed_income(amount: float = 2000.0) -> None:
    with app.app_context():
        hid = current_household_id()
        account = Account.query.filter_by(household_id=hid).first()
        if account is None:
            account = Account(household_id=hid, checking_balance=1250.0)
            db.session.add(account)
            db.session.flush()
        db.session.add(ExpenseTransaction(
            household_id=hid,
            description="Established payday",
            amount=amount,
            category="income",
            source="manual",
            local_account_id=account.id,
            date=datetime.now(timezone.utc) - timedelta(days=5),
        ))
        db.session.commit()


def _signature(public_id: str) -> str:
    return hmac.new(HOUSEHOLD_SECRET, public_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers_for(public_id: str) -> dict[str, str]:
    return {
        "X-Household-Id": public_id,
        "X-Household-Signature": _signature(public_id),
    }


def _review_required_expenses() -> None:
    """Establish the explicit review fact required by Slice 8A readiness."""
    response = client.post(
        "/api/onboarding/required-expenses-review",
        json={"answer": "yes", "review_complete": True},
    )
    assert response.status_code == 200


# 1 + 2 + 3 + 4 + 5 + 6 + 7 — manual-first onboarding produces a calculable
# household and every financial input persists to its canonical authority.
def test_manual_first_onboarding_is_financially_calculable() -> None:
    _reset_db()
    _review_required_expenses()

    resp = client.post("/api/onboarding/complete", json=FULL_FINANCIAL_PAYLOAD)
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("is_onboarded") is True

    readiness = body.get("readiness") or {}
    assert readiness.get("complete") is True, readiness
    assert readiness.get("safe_to_spend_available") is True
    assert readiness.get("missing_setup") == []

    with app.app_context():
        hid = current_household_id()
        account = get_household_account(hid)
        # 2 — balance persisted to canonical Account.checking_balance authority
        # via financial_state (balance_version bumped proves the concurrency path).
        assert round(float(account.checking_balance), 2) == 1750.50
        assert int(account.balance_version or 0) >= 1

        # 3 — PYF target persisted to Package 9 canonical setting.
        pyf = UserSetting.query.filter_by(household_id=hid, key=PYF_TARGET_SETTING_KEY).first()
        assert pyf is not None
        assert float(pyf.value) == 12.5

        # 4 — protected buffer persisted to canonical setting.
        buf = UserSetting.query.filter_by(household_id=hid, key=SAFE_BUFFER_SETTING_KEY).first()
        assert buf is not None
        assert float(buf.value) == 150.0

        # 5 — pay schedule and expected income use separate canonical authorities.
        assert int(account.pay_period_days) == 14
        assert account.expected_paycheck is None
        plan = IncomePlanVersion.query.filter_by(household_id=hid).one()
        assert plan.expected_income_cents == 200000

        # 6 — grocery Need baseline persisted to Package 10 authority.
        grocery = UserPreference.query.filter_by(household_id=hid, key="baseline_grocery_cost").first()
        assert grocery is not None
        assert float(grocery.value) == 240.0

        # 7 — fuel/transport Need persisted as the canonical gas-estimate bill.
        gas = Bill.query.filter_by(household_id=hid, is_gas_estimate=True).first()
        assert gas is not None
        assert round(float(gas.amount), 2) == 75.0


# 8 — bills persist without duplication across revisits.
def test_bills_do_not_duplicate_on_revisit() -> None:
    _reset_db()
    payload = {
        "household_size": 2,
        "recurring_bills": [{"name": "Phone", "amount": 95.0}],
        "baseline_fuel_cost": 60.0,
    }
    assert client.post("/api/onboarding/complete", json=payload).status_code == 200
    assert client.post("/api/onboarding/complete", json=payload).status_code == 200

    with app.app_context():
        hid = current_household_id()
        phone = Bill.query.filter_by(household_id=hid).filter(Bill.name.ilike("%phone%")).all()
        assert len(phone) == 1
        assert round(float(phone[0].amount), 2) == 95.0
        gas = Bill.query.filter_by(household_id=hid, is_gas_estimate=True).all()
        assert len(gas) == 1


# 9 + 10 — Shopping Style and Household Shopping Defaults persist to the
# canonical HouseholdShoppingDefault authority.
def test_shopping_style_and_defaults_persist() -> None:
    _reset_db()
    payload = {
        "household_size": 2,
        "shopping_style": "save_most",
        "household_shopping_defaults": {"milk_type": "whole", "bread_type": "wheat"},
    }
    resp = client.post("/api/onboarding/complete", json=payload)
    assert resp.status_code == 200

    with app.app_context():
        hid = current_household_id()
        rows = {r.preference_key: r for r in HouseholdShoppingDefault.query.filter_by(household_id=hid).all()}
        style = [r for r in rows.values() if r.preference_kind == "shopping_style"]
        assert len(style) == 1
        assert style[0].preference_value == "save_most"
        assert rows["milk_type"].preference_value == "whole"
        assert rows["bread_type"].preference_value == "wheat"


# 11 — onboarding (which has no location/store authority) cannot silently
# create, select, or replace the canonical selected shopping store.
def test_onboarding_never_selects_or_replaces_store() -> None:
    _reset_db()
    with app.app_context():
        hid = current_household_id()
        select_store(hid, retailer="kroger", store_id="kroger-61500116", store_name="Test Kroger")
        db.session.commit()

    resp = client.post("/api/onboarding/complete", json=FULL_FINANCIAL_PAYLOAD)
    assert resp.status_code == 200

    with app.app_context():
        hid = current_household_id()
        selected = get_selected_store(hid)
        assert selected.get("store_id") == "kroger-61500116"
        assert selected.get("retailer") == "kroger"
        # No onboarding-only location/sharing key was written.
        location_keys = UserSetting.query.filter_by(household_id=hid).filter(
            UserSetting.key.ilike("%location%")
        ).all()
        assert location_keys == []


# 12 — Plaid remains optional; a manual-first household needs no bank link.
def test_plaid_remains_optional() -> None:
    _reset_db()
    _seed_income()
    _review_required_expenses()
    resp = client.post("/api/onboarding/complete", json=FULL_FINANCIAL_PAYLOAD)
    assert resp.status_code == 200
    assert (resp.get_json() or {}).get("readiness", {}).get("complete") is True

    with app.app_context():
        status = get_plaid_connection_status("anonymous")
        assert status.get("connected") is False
        assert status.get("items") == []


# 13 — revisiting onboarding does not erase already-persisted canonical data.
def test_revisit_does_not_erase_existing_data() -> None:
    _reset_db()
    _seed_income()
    assert client.post("/api/onboarding/complete", json=FULL_FINANCIAL_PAYLOAD).status_code == 200

    # Revisit with only a household-size change (no financial/shopping fields).
    resp = client.post("/api/onboarding/complete", json={"household_size": 5})
    assert resp.status_code == 200

    with app.app_context():
        hid = current_household_id()
        account = get_household_account(hid)
        assert round(float(account.checking_balance), 2) == 1750.50
        assert int(account.household_size) == 5  # the only field we changed
        assert int(account.pay_period_days) == 14
        assert round(float(account.expected_paycheck), 2) == 2000.0  # legacy compatibility value only
        assert IncomePlanVersion.query.filter_by(household_id=hid).one().expected_income_cents == 200000

        pyf = UserSetting.query.filter_by(household_id=hid, key=PYF_TARGET_SETTING_KEY).first()
        assert pyf is not None and float(pyf.value) == 12.5
        buf = UserSetting.query.filter_by(household_id=hid, key=SAFE_BUFFER_SETTING_KEY).first()
        assert buf is not None and float(buf.value) == 150.0
        grocery = UserPreference.query.filter_by(household_id=hid, key="baseline_grocery_cost").first()
        assert grocery is not None and float(grocery.value) == 240.0
        style = HouseholdShoppingDefault.query.filter_by(
            household_id=hid, preference_kind="shopping_style"
        ).first()
        assert style is None  # no style was ever set; still none after revisit


# 14 — reload/resume reflects persisted progress via /api/onboarding/state.
def test_reload_resume_reflects_persisted_state() -> None:
    _reset_db()
    _seed_income()
    _review_required_expenses()
    assert client.post("/api/onboarding/complete", json=FULL_FINANCIAL_PAYLOAD).status_code == 200

    state = client.get("/api/onboarding/state").get_json() or {}
    assert state.get("is_onboarded") is True
    assert state.get("show_onboarding") is False
    defaults = state.get("defaults") or {}
    assert defaults.get("household_size") == 3
    assert defaults.get("checking_balance") == 1750.50
    assert defaults.get("pay_period_days") == 14
    assert defaults.get("expected_paycheck") == 2000.0
    assert defaults.get("next_payday") == FULL_FINANCIAL_PAYLOAD["next_payday"]
    assert defaults.get("long_term_savings_target_percent") == 12.5
    assert defaults.get("protected_buffer") == 150.0
    assert defaults.get("baseline_grocery_cost") == 240.0
    assert defaults.get("baseline_fuel_cost") == 75.0
    assert (state.get("readiness") or {}).get("complete") is True


# 15 — completion/readiness is truthful when critical financial setup is missing.
def test_readiness_is_truthful_when_setup_missing() -> None:
    _reset_db()
    resp = client.post("/api/onboarding/complete", json={"household_size": 2})
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("is_onboarded") is True  # wizard finished...

    readiness = body.get("readiness") or {}
    assert readiness.get("complete") is False  # ...but not financially ready
    missing = set(readiness.get("missing_setup") or [])
    # The account carries legacy defaults for checking/pay-period, but the
    # canonical critical setup (PYF target, buffer, grocery, fuel, payday)
    # must still be reported as missing rather than being invented.
    assert {"long_term_savings_target_percent", "protected_checking_buffer",
            "grocery_need", "fuel_or_transport_need", "payday"} <= missing


# 16 — Household A cannot read or change Household B onboarding state.
def test_household_isolation() -> None:
    # Re-assert the secret at request time: other suites mutate this env var
    # (module-level and at runtime) and can leak a different value into ours.
    os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "package11-test-secret"
    _reset_db()
    with app.app_context():
        db.session.add(Household(public_id="hh-a", legacy_scope_key="scope-a"))
        db.session.add(Household(public_id="hh-b", legacy_scope_key="scope-b"))
        db.session.commit()

    hdr_a = _headers_for("hh-a")
    hdr_b = _headers_for("hh-b")

    resp = client.post("/api/onboarding/complete", json={
        "household_size": 2,
        "checking_balance": 999.0,
        "long_term_savings_target_percent": 33,
        "protected_buffer": 400,
        "baseline_grocery_cost": 500,
        "baseline_fuel_cost": 90,
    }, headers=hdr_a)
    assert resp.status_code == 200

    state_b = client.get("/api/onboarding/state", headers=hdr_b).get_json() or {}
    defaults_b = state_b.get("defaults") or {}
    assert defaults_b.get("household_size") == 4
    assert defaults_b.get("checking_balance") is None
    assert defaults_b.get("pay_period_days") == 0
    assert defaults_b.get("expected_paycheck") is None
    assert defaults_b.get("long_term_savings_target_percent") is None
    assert defaults_b.get("protected_buffer") is None
    assert defaults_b.get("baseline_grocery_cost") is None

    with app.app_context():
        a = Household.query.filter_by(public_id="hh-a").first()
        b = Household.query.filter_by(public_id="hh-b").first()
        acct_a = Account.query.filter_by(household_id=a.id).first()
        acct_b = Account.query.filter_by(household_id=b.id).first()
        assert acct_a is not None and round(float(acct_a.checking_balance), 2) == 999.0
        assert acct_b is not None and acct_b.checking_balance is None
        pyf_a = UserSetting.query.filter_by(household_id=a.id, key=PYF_TARGET_SETTING_KEY).first()
        pyf_b = UserSetting.query.filter_by(household_id=b.id, key=PYF_TARGET_SETTING_KEY).first()
        assert pyf_a is not None and float(pyf_a.value) == 33
        assert pyf_b is None


# 17 — legacy food_allocation_pct cannot substitute for canonical PYF/grocery.
def test_legacy_food_allocation_cannot_substitute_canonical_setup() -> None:
    _reset_db()
    _seed_income()
    with app.app_context():
        hid = current_household_id()
        account = get_household_account(hid)
        account.food_allocation_pct = 40.0  # legacy ratio only
        account.checking_balance = 1000.0
        account.pay_period_days = 14
        account.expected_paycheck = 2000.0
        db.session.commit()

    state = client.get("/api/onboarding/state").get_json() or {}
    readiness = state.get("readiness") or {}
    assert readiness.get("complete") is False
    missing = set(readiness.get("missing_setup") or [])
    # Legacy food_allocation_pct does not satisfy the canonical PYF or grocery need.
    assert "long_term_savings_target_percent" in missing
    assert "grocery_need" in missing


def test_fresh_browser_state_does_not_expose_model_financial_defaults() -> None:
    _reset_db()
    state = client.get("/api/onboarding/state").get_json() or {}
    defaults = state.get("defaults") or {}
    assert defaults.get("checking_balance") is None
    assert defaults.get("pay_period_days") == 0
    assert defaults.get("expected_paycheck") is None
    assert (state.get("readiness") or {}).get("complete") is False
    assert {"checking_balance", "pay_period_days", "current_period_income"} <= set(
        (state.get("readiness") or {}).get("missing_setup") or []
    )


def test_invalid_late_field_rolls_back_all_onboarding_writes() -> None:
    _reset_db()
    payload = dict(FULL_FINANCIAL_PAYLOAD)
    payload["shopping_style"] = "not-a-canonical-style"
    response = client.post("/api/onboarding/complete", json=payload)
    assert response.status_code == 400

    with app.app_context():
        hid = current_household_id()
        account = Account.query.filter_by(household_id=hid).first()
        assert account is not None
        assert account.checking_balance is None
        assert int(account.pay_period_days or 0) == 0
        assert account.expected_paycheck is None
        assert account.is_onboarded is False
        assert UserSetting.query.filter_by(household_id=hid).count() == 0
        assert UserPreference.query.filter_by(household_id=hid).count() == 0
        assert Bill.query.filter_by(household_id=hid).count() == 0
        assert HouseholdShoppingDefault.query.filter_by(household_id=hid).count() == 0


def test_location_sharing_persists_without_changing_selected_store() -> None:
    _reset_db()
    with app.app_context():
        hid = current_household_id()
        select_store(hid, retailer="kroger", store_id="61500116", store_name="Test Kroger")
        db.session.commit()

    response = client.post("/api/onboarding/complete", json={"location_sharing_enabled": True})
    assert response.status_code == 200
    state = client.get("/api/onboarding/state").get_json() or {}
    assert (state.get("defaults") or {}).get("location_sharing_enabled") is True

    with app.app_context():
        hid = current_household_id()
        setting = UserSetting.query.filter_by(household_id=hid, key=LOCATION_SHARING_SETTING_KEY).first()
        assert setting is not None and setting.value == "true"
        selected = get_selected_store(hid)
        assert selected.get("store_id") == "61500116"


def test_omitted_optional_fields_preserve_existing_shopping_defaults() -> None:
    _reset_db()
    first = client.post("/api/onboarding/complete", json={
        "shopping_style": "save_most",
        "household_shopping_defaults": {"milk_type": "whole"},
    })
    assert first.status_code == 200
    assert client.post("/api/onboarding/complete", json={"household_size": 3}).status_code == 200
    state = client.get("/api/onboarding/state").get_json() or {}
    defaults = state.get("defaults") or {}
    assert defaults.get("shopping_style") == "save_most"
    assert (defaults.get("household_shopping_defaults") or {}).get("milk_type") == "whole"


def test_explicit_no_expense_review_ignores_stale_bill_and_baseline_fields() -> None:
    """A durable 'no required expenses' answer is authoritative.

    A browser can flip YES -> NO without necessarily clearing already-typed
    grocery/fuel/bill inputs before the final /api/onboarding/complete call.
    The explicit reviewed-none answer must still win: it must not manufacture
    Bills or baseline preferences from those leftover values.
    """
    _reset_db()
    review = client.post("/api/onboarding/required-expenses-review", json={"answer": "no"})
    assert review.status_code == 200
    assert review.get_json()["required_expense_review"] == "no_expenses_reviewed"

    payload = dict(FULL_FINANCIAL_PAYLOAD)
    completed = client.post("/api/onboarding/complete", json=payload)
    assert completed.status_code == 200

    with app.app_context():
        hid = current_household_id()
        assert Bill.query.filter_by(household_id=hid).count() == 0
        assert UserPreference.query.filter(
            UserPreference.household_id == hid,
            UserPreference.key.in_(["baseline_grocery_cost", "baseline_fuel_cost"]),
        ).count() == 0

    state = client.get("/api/onboarding/state").get_json() or {}
    assert state["required_expense_review"] == "no_expenses_reviewed"
    assert state["readiness"]["complete"] is True
