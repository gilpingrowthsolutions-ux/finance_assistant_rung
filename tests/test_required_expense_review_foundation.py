"""Canonical required-expense review state foundation (Slice 8A).

These tests use only an in-memory disposable SQLite database.  They prove
that explicit expense-review presence is household-scoped and does not
manufacture financial records.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "slice8a-review-state-secret"

from app import (  # noqa: E402
    PYF_TARGET_SETTING_KEY,
    REQUIRED_EXPENSE_NONE,
    REQUIRED_EXPENSE_PENDING,
    REQUIRED_EXPENSE_REVIEWED,
    REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
    REQUIRED_EXPENSE_UNANSWERED,
    SAFE_BUFFER_SETTING_KEY,
    Account,
    Bill,
    ExpenseTransaction,
    Household,
    IncomePlanVersion,
    UserPreference,
    UserSetting,
    _compute_safe_to_spend_snapshot,
    app,
    db,
)
from services.household_context import household_id as current_household_id  # noqa: E402


HOUSEHOLD_SECRET = b"slice8a-review-state-secret"


def _reset_db() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _headers(public_id: str) -> dict[str, str]:
    signature = hmac.new(HOUSEHOLD_SECRET, public_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Household-Id": public_id, "X-Household-Signature": signature}


def _seed_complete_non_expense_inputs() -> Account:
    """Seed canonical money inputs but intentionally no Needs/Bills."""
    hid = current_household_id()
    account = Account(
        household_id=hid,
        checking_balance=1000.0,
        pay_period_days=14,
    )
    db.session.add(account)
    db.session.flush()
    db.session.add_all([
        IncomePlanVersion(
            household_id=hid,
            operation_id="slice8a-income-plan",
            expected_income_cents=150000,
            effective_at=datetime.now(timezone.utc) - timedelta(days=1),
            source="test",
        ),
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="10"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100"),
        UserSetting(
            household_id=hid,
            key="next_payday_date",
            value=(datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat(),
        ),
    ])
    db.session.commit()
    return account


def test_new_household_defaults_to_unanswered_and_skip_does_not_answer() -> None:
    _reset_db()
    client = app.test_client()

    state = client.get("/api/onboarding/state").get_json() or {}
    assert state["required_expense_review"] == REQUIRED_EXPENSE_UNANSWERED

    skipped = client.post("/api/onboarding/skip", json={})
    assert skipped.status_code == 200
    state = client.get("/api/onboarding/state").get_json() or {}
    assert state["required_expense_review"] == REQUIRED_EXPENSE_UNANSWERED


def test_explicit_review_transitions_are_durable_and_do_not_create_records() -> None:
    _reset_db()
    client = app.test_client()

    no = client.post("/api/onboarding/required-expenses-review", json={"answer": "no"})
    assert no.status_code == 200
    assert no.get_json()["required_expense_review"] == REQUIRED_EXPENSE_NONE

    with app.app_context():
        hid = current_household_id()
        assert UserSetting.query.filter_by(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY).one().value == REQUIRED_EXPENSE_NONE
        assert Bill.query.filter_by(household_id=hid).count() == 0
        assert ExpenseTransaction.query.filter_by(household_id=hid).count() == 0
        assert UserPreference.query.filter(
            UserPreference.household_id == hid,
            UserPreference.key.in_(["baseline_grocery_cost", "baseline_fuel_cost"]),
        ).count() == 0

    # A fresh request proves durable database authority rather than client-only state.
    assert (client.get("/api/onboarding/state").get_json() or {})["required_expense_review"] == REQUIRED_EXPENSE_NONE

    yes = client.post("/api/onboarding/required-expenses-review", json={"answer": "yes"})
    assert yes.status_code == 200
    assert yes.get_json()["required_expense_review"] == REQUIRED_EXPENSE_PENDING

    completed = client.post(
        "/api/onboarding/required-expenses-review",
        json={"answer": "yes", "review_complete": True},
    )
    assert completed.status_code == 200
    assert completed.get_json()["required_expense_review"] == REQUIRED_EXPENSE_REVIEWED

    # A person can correct a YES answer to NO without silently deleting Bills.
    assert client.post("/api/onboarding/required-expenses-review", json={"answer": "no"}).status_code == 200
    assert (client.get("/api/onboarding/state").get_json() or {})["required_expense_review"] == REQUIRED_EXPENSE_NONE


def test_readiness_distinguishes_unknown_none_pending_and_reviewed_expenses() -> None:
    _reset_db()
    with app.app_context():
        account = _seed_complete_non_expense_inputs()

        unanswered = _compute_safe_to_spend_snapshot(account, now_utc=datetime.now(timezone.utc))
        assert unanswered["complete"] is False
        assert "required_expenses_review" in unanswered["missing_setup"]
        assert "grocery_need" in unanswered["missing_setup"]
        assert "fuel_or_transport_need" in unanswered["missing_setup"]

        hid = current_household_id()
        db.session.add(UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_NONE))
        db.session.commit()
        reviewed_none = _compute_safe_to_spend_snapshot(account, now_utc=datetime.now(timezone.utc))
        assert reviewed_none["complete"] is True
        assert reviewed_none["needs_total_cents"] == 0
        assert reviewed_none["components"]["required_expense_review"] == REQUIRED_EXPENSE_NONE
        assert Bill.query.filter_by(household_id=hid).count() == 0

        # Presence semantics do not alter the PYF arithmetic: an explicit
        # reviewed-none snapshot equals otherwise equivalent known-zero Needs.
        db.session.add_all([
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="0"),
            Bill(
                household_id=hid,
                name="Known-zero transport fixture",
                amount=0,
                due_date=datetime.now(timezone.utc) + timedelta(days=2),
                is_gas_estimate=True,
                is_paid=False,
            ),
        ])
        UserSetting.query.filter_by(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY).update(
            {"value": REQUIRED_EXPENSE_REVIEWED}
        )
        db.session.commit()
        known_zero = _compute_safe_to_spend_snapshot(account, now_utc=datetime.now(timezone.utc))
        assert known_zero["complete"] is True
        assert known_zero["needs_total_cents"] == reviewed_none["needs_total_cents"]
        assert known_zero["safe_to_spend_cents"] == reviewed_none["safe_to_spend_cents"]

        UserSetting.query.filter_by(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY).update({"value": REQUIRED_EXPENSE_PENDING})
        db.session.commit()
        pending = _compute_safe_to_spend_snapshot(account, now_utc=datetime.now(timezone.utc))
        assert pending["complete"] is False
        assert "required_expenses_review" in pending["missing_setup"]

        UserSetting.query.filter_by(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY).update({"value": REQUIRED_EXPENSE_REVIEWED})
        db.session.commit()
        reviewed = _compute_safe_to_spend_snapshot(account, now_utc=datetime.now(timezone.utc))
        assert reviewed["complete"] is True
        assert reviewed["components"]["required_expense_review"] == REQUIRED_EXPENSE_REVIEWED


def test_review_state_is_household_isolated() -> None:
    os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "slice8a-review-state-secret"
    _reset_db()
    public_a = "22222222-2222-4222-8222-222222222222"
    public_b = "33333333-3333-4333-8333-333333333333"
    with app.app_context():
        db.session.add_all([Household(public_id=public_a), Household(public_id=public_b)])
        db.session.commit()

    client = app.test_client()
    assert client.post(
        "/api/onboarding/required-expenses-review", json={"answer": "no"}, headers=_headers(public_a)
    ).status_code == 200
    state_b = client.get("/api/onboarding/state", headers=_headers(public_b)).get_json() or {}
    assert state_b["required_expense_review"] == REQUIRED_EXPENSE_UNANSWERED
    state_a = client.get("/api/onboarding/state", headers=_headers(public_a)).get_json() or {}
    assert state_a["required_expense_review"] == REQUIRED_EXPENSE_NONE
