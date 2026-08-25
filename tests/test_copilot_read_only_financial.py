from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import PYF_TARGET_SETTING_KEY, REQUIRED_EXPENSE_REVIEWED, REQUIRED_EXPENSE_REVIEW_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app  # noqa: E402
from extensions import db  # noqa: E402
from models import (  # noqa: E402
    Account,
    Bill,
    ExpenseTransaction,
    GroceryItem,
    IncomePlanVersion,
    MealPlanItem,
    RetailProductPreference,
    SavingsGoal,
    ShoppingTripCompletion,
    UserPreference,
    UserSetting,
)
from services.household_context import household_id as current_household_id  # noqa: E402


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        now = datetime.now(timezone.utc)
        account = Account(household_id=hid, checking_balance=1000.0, pay_period_days=14)
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            IncomePlanVersion(
                household_id=hid,
                operation_id="copilot-read-only-income",
                expected_income_cents=120000,
                effective_at=now - timedelta(days=30),
                source="test",
            ),
            UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="0.10"),
            UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100.00"),
            UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="100.00"),
            Bill(household_id=hid, name="Rent", amount=200.0, due_date=now + timedelta(days=4), is_paid=False),
            Bill(household_id=hid, name="Required fuel", amount=50.0, due_date=now + timedelta(days=4), is_paid=False, is_gas_estimate=True),
            ExpenseTransaction(
                household_id=hid,
                description="Established payday",
                amount=1200.0,
                category="income",
                source="manual",
                local_account_id=account.id,
                date=now - timedelta(days=7),
            ),
        ])
        db.session.commit()
    return app.test_client()


def _safe_cents(client) -> int:
    response = client.get("/api/budget/summary")
    assert response.status_code == 200
    safe = (response.get_json() or {}).get("safe_to_spend") or {}
    assert safe.get("complete") is True
    return int(safe["safe_to_spend_cents"])


def _business_counts() -> tuple[int, ...]:
    return (
        ExpenseTransaction.query.count(),
        SavingsGoal.query.count(),
        GroceryItem.query.count(),
        MealPlanItem.query.count(),
        RetailProductPreference.query.count(),
        ShoppingTripCompletion.query.count(),
    )


@pytest.mark.parametrize("offset_cents, expected_fits", [(-5000, True), (0, True), (5000, False)])
def test_purchase_affordability_uses_canonical_safe_to_spend_without_writes(client, offset_cents, expected_fits):
    safe_cents = _safe_cents(client)
    requested_cents = safe_cents + offset_cents
    assert requested_cents > 0
    with app.app_context():
        before = _business_counts()
        balance_before = Account.query.one().checking_balance

    response = client.post("/api/copilot/stage", json={"text": f"Can I afford ${requested_cents / 100:.2f} tonight?"})
    assert response.status_code == 200
    body = response.get_json() or {}
    actions = body.get("actions_taken") or {}
    assert (body.get("parsed") or {}).get("path") == "deterministic_financial_read_only_v1"
    assert actions.get("read_only") is True
    assert actions.get("financial_mutations") is False
    assert actions.get("safe_to_spend_cents") == safe_cents
    assert actions.get("requested_amount_cents") == requested_cents
    assert actions.get("remaining_after_purchase_cents") == safe_cents - requested_cents
    assert actions.get("fits") is expected_fits
    assert "current balance" not in str(actions.get("summary") or "").lower()
    assert body.get("clarification_question") is None

    with app.app_context():
        assert _business_counts() == before
        assert Account.query.one().checking_balance == balance_before


def test_purchase_affordability_setup_needed_is_truthful_and_does_not_ask_known_balance(client):
    with app.app_context():
        UserSetting.query.filter_by(key=PYF_TARGET_SETTING_KEY).delete()
        db.session.commit()
        before = _business_counts()
        balance_before = Account.query.one().checking_balance

    response = client.post("/api/copilot/stage", json={"text": "Can I spend $75 on dinner?"})
    assert response.status_code == 200
    body = response.get_json() or {}
    actions = body.get("actions_taken") or {}
    assert actions.get("setup_needed") is True
    assert "pay yourself first target" in str(actions.get("summary") or "").lower()
    assert "current checking balance" not in str(actions.get("summary") or "").lower()
    assert body.get("clarification_question") is None
    with app.app_context():
        assert _business_counts() == before
        assert Account.query.one().checking_balance == balance_before


@pytest.mark.parametrize(("prompt", "requested_cents"), [
    ("Can I spend $75 on dinner?", 7500),
    ("Is $100 safe for me to spend?", 10000),
    ("Can I buy something for $40?", 4000),
])
def test_purchase_affordability_language_variants_use_same_canonical_path(client, prompt, requested_cents):
    response = client.post("/api/copilot/stage", json={"text": prompt})
    assert response.status_code == 200
    body = response.get_json() or {}
    actions = body.get("actions_taken") or {}
    assert (body.get("parsed") or {}).get("path") == "deterministic_financial_read_only_v1"
    assert actions.get("requested_amount_cents") == requested_cents
    assert actions.get("read_only") is True
    assert actions.get("fits") is True


def test_safe_to_spend_explanation_uses_current_components_and_truthful_causal_limit(client):
    safe_cents = _safe_cents(client)
    with app.app_context():
        before = _business_counts()
        balance_before = Account.query.one().checking_balance

    response = client.post("/api/copilot/stage", json={"text": "Why did my Safe-to-Spend change?"})
    assert response.status_code == 200
    body = response.get_json() or {}
    actions = body.get("actions_taken") or {}
    summary = str(actions.get("summary") or "")
    assert actions.get("intent") == "safe_to_spend_explanation"
    assert actions.get("safe_to_spend_cents") == safe_cents
    assert actions.get("causal_provenance") == "not_available"
    assert actions.get("read_only") is True
    assert "does not yet have complete verified before-and-after provenance" in summary
    assert "current needs" in summary.lower()
    assert "pay yourself first" in summary.lower()
    assert "checking buffer" in summary.lower()
    assert "recent transaction caused" in summary.lower()
    assert body.get("clarification_question") is None

    with app.app_context():
        assert _business_counts() == before
        assert Account.query.one().checking_balance == balance_before


@pytest.mark.parametrize("prompt", [
    "Explain my Safe-to-Spend.",
    "What is affecting my Safe-to-Spend?",
    "Why is my Safe-to-Spend lower?",
])
def test_safe_to_spend_explanation_variants_use_deterministic_read_only_path(client, prompt):
    response = client.post("/api/copilot/stage", json={"text": prompt})
    assert response.status_code == 200
    body = response.get_json() or {}
    assert (body.get("parsed") or {}).get("path") == "deterministic_financial_read_only_v1"
    assert (body.get("actions_taken") or {}).get("intent") == "safe_to_spend_explanation"
