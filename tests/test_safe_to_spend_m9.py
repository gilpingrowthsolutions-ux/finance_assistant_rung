from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ.setdefault("PLAID_CLIENT_ID", "plaid_test_client")
os.environ.setdefault("PLAID_SECRET", "plaid_test_secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("PLAID_TOKEN_ENCRYPTION_KEY", "x7cUQ1K8v1SCh4skQ53QqE5s8z3v8c2n6cihVQMcWDo=")

from app import PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app, db  # noqa: E402
from extensions import assert_safe_destructive_db_target  # noqa: E402
from models import (  # noqa: E402
    Account,
    Bill,
    ExpenseTransaction,
    PlaidAccount,
    PlaidItem,
    PlaidTransaction,
    ShoppingTripCompletion,
    UserPreference,
    UserSetting,
    UsageEvent,
)
from services.transaction_reconciliation import project_plaid_transactions  # noqa: E402
from services.household_context import household_id as current_household_id  # noqa: E402


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        account = Account(
                household_id=hid,
                checking_balance=1000.0,
                food_allocation_pct=40.0,
                pay_period_days=14,
                meals_per_day=3,
                expected_paycheck=1426.0,
            )
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="0"),
            UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="0.00"),
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="546.40"),
            Bill(household_id=hid, name="Gas Allocation", amount=60.0, due_date=datetime.now(timezone.utc) + timedelta(days=5), is_gas_estimate=True, is_paid=False),
            ExpenseTransaction(household_id=hid, description="Established payday", amount=1426.0, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc) - timedelta(days=5)),
        ])
        db.session.commit()
    return app.test_client()


def _summary(client):
    resp = client.get("/api/budget/summary")
    assert resp.status_code == 200
    return resp.get_json() or {}


def _safe(client):
    return (_summary(client).get("safe_to_spend") or {})


def _safe_amount(client) -> float:
    return float(_safe(client).get("safe_to_spend") or 0.0)


def _components(client) -> dict:
    return (_safe(client).get("components") or {})


def _next_income_date(client) -> str | None:
    nxt = (_safe(client).get("next_expected_income") or {})
    return nxt.get("date")


def _set_buffer(client, amount: float):
    resp = client.post("/api/settings/safe-to-spend", json={"protected_buffer": amount})
    assert resp.status_code == 200


def _add_income(days_ago: int, amount: float = 1426.0, desc: str = "Paycheck"):
    with app.app_context():
        hid = current_household_id()
        account = Account.query.first()
        account.checking_balance = float(account.checking_balance or 0.0) + amount
        db.session.add(
            ExpenseTransaction(
                household_id=hid,
                description=desc,
                amount=amount,
                category="income",
                source="manual",
                local_account_id=account.id,
                date=datetime.now(timezone.utc) - timedelta(days=days_ago),
            )
        )
        db.session.add(account)
        db.session.commit()


def _seed_plaid_base(owner_scope: str = "anonymous"):
    with app.app_context():
        hid = current_household_id()
        item = PlaidItem(
            household_id=hid,
            owner_scope=owner_scope,
            plaid_item_id="item_1",
            access_token_encrypted="enc",
            connection_status="connected",
            institution_name="First Platypus Bank",
        )
        db.session.add(item)
        db.session.flush()
        acct = PlaidAccount(
            household_id=hid,
            owner_scope=owner_scope,
            plaid_item_id=item.id,
            plaid_account_id="acc_1",
            rung_account_id=1,
            name="Checking",
            is_active=True,
        )
        db.session.add(acct)
        db.session.commit()
        return item.id


def _seed_plaid_tx(
    *,
    item_id: int,
    tx_id: str,
    amount_cents: int,
    direction: str,
    merchant: str,
    pending: bool = False,
    replaces_pending_id: str | None = None,
    active: bool = True,
):
    with app.app_context():
        hid = current_household_id()
        row = PlaidTransaction(
            household_id=hid,
            owner_scope="anonymous",
            plaid_item_id=item_id,
            plaid_transaction_id=tx_id,
            plaid_account_id="acc_1",
            pending_transaction_id=None,
            replaces_pending_transaction_id=replaces_pending_id,
            is_pending=pending,
            is_removed=False,
            is_active_event=active,
            pending_lifecycle_status="pending" if pending else "posted",
            amount_cents=amount_cents,
            signed_amount_cents=(-amount_cents if direction == "outflow" else amount_cents),
            direction=direction,
            name=merchant,
            merchant_name=merchant,
            description=merchant,
            transaction_date=date.today(),
            authorized_date=date.today(),
        )
        db.session.add(row)
        db.session.commit()


def test_basic_safe_to_spend_calculation(client):
    safe = _safe(client)
    assert safe.get("state") in {"positive", "tight", "overcommitted"}
    assert round(float(safe.get("safe_to_spend") or 0.0), 2) == 393.6


def test_protected_bills_reduce_safe_to_spend(client):
    base = _safe_amount(client)
    client.post("/bills", json={"name": "Rent", "amount": 200, "due_date": (date.today() + timedelta(days=5)).isoformat()})
    assert _safe_amount(client) < base


def test_paid_bill_not_protected_again(client):
    r = client.post("/bills", json={"name": "Internet", "amount": 80, "due_date": (date.today() + timedelta(days=3)).isoformat()})
    bid = (r.get_json() or {}).get("id")
    before = _safe_amount(client)
    client.post(f"/bills/{bid}/pay")
    after = _safe_amount(client)
    assert after > before


def test_committed_expense_reduces_safe_to_spend(client):
    base = _safe_amount(client)
    with app.app_context():
        row = Bill.query.filter_by(name="Gas Allocation").first()
        row.amount = 85.0
        row.is_gas_estimate = True
        db.session.add(row)
        db.session.commit()
    assert _safe_amount(client) < base


def test_protected_buffer_reduces_safe_to_spend(client):
    base = _safe_amount(client)
    _set_buffer(client, 100.0)
    assert round(base - _safe_amount(client), 2) == 100.0


def test_next_payday_is_derived_from_income_data(client):
    _add_income(days_ago=5, amount=1426.0)
    safe = _safe(client)
    nxt = safe.get("next_expected_income") or {}
    assert nxt.get("known") is True
    assert isinstance(safe.get("until_payday_days"), int)
    assert safe.get("until_payday_days") == 9


def test_missing_payday_is_truthful(client):
    with app.app_context():
        ExpenseTransaction.query.filter_by(category="income").delete()
        db.session.commit()
    safe = _safe(client)
    assert safe.get("state") == "needs_setup"
    assert safe.get("safe_to_spend") is None
    assert "payday" in (safe.get("missing_setup") or [])


def test_completed_grocery_spend_not_double_counted(client):
    before = _safe_amount(client)
    stage = client.post(
        "/api/grocery/finished-shopping/stage",
        json={"planned_total": 80.0, "actual_total": 100.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "sig-a"},
    )
    op_id = (stage.get_json() or {}).get("operation_id")
    done = client.post(
        "/api/grocery/finished-shopping/complete",
        json={"planned_total": 80.0, "actual_total": 100.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "sig-a", "operation_id": op_id, "confirm": True},
    )
    assert done.status_code == 200
    with app.app_context():
        assert ShoppingTripCompletion.query.count() == 1
        assert ExpenseTransaction.query.filter_by(category="grocery").count() >= 1
    # Grocery completion should not be double-counted against both balance and protection.
    assert round(_safe_amount(client) - before, 2) == 0.0


def test_remaining_grocery_commitment_protected_correctly(client):
    safe = _safe(client)
    comp = safe.get("components") or {}
    assert round(float(comp.get("groceries_remaining") or 0.0), 2) == 546.4


def test_finished_shopping_actual_amount_drives_financial_truth(client):
    stage = client.post(
        "/api/grocery/finished-shopping/stage",
        json={"planned_total": 70.0, "actual_total": 83.25, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "sig-b"},
    )
    op_id = (stage.get_json() or {}).get("operation_id")
    done = client.post(
        "/api/grocery/finished-shopping/complete",
        json={"planned_total": 70.0, "actual_total": 83.25, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "sig-b", "operation_id": op_id, "confirm": True},
    )
    assert done.status_code == 200
    with app.app_context():
        trip = ShoppingTripCompletion.query.filter_by(operation_id=op_id).first()
        assert trip is not None
        assert trip.planned_total_cents == 7000
        assert trip.actual_total_cents == 8325


def test_corrected_shopping_actual_recalculates_safe_to_spend(client):
    with app.app_context():
        hid = current_household_id()
        tx = ExpenseTransaction(household_id=hid, description="Grocery trip Walmart", amount=75.0, category="grocery")
        db.session.add(tx)
        db.session.flush()
        db.session.add(
            ShoppingTripCompletion(
                household_id=hid,
                operation_id="op_trip_a",
                trip_token="trip_seed",
                transaction_id=tx.id,
                retailer="walmart",
                store_name="Walmart",
                store_id="357",
                planned_total_cents=7000,
                actual_total_cents=7500,
                amount_source="actual",
                cart_signature="sig-seed",
                manual_provisional=True,
            )
        )
        db.session.commit()

    before = _safe_amount(client)
    staged = client.post("/api/copilot/stage", json={"text": "Correct finished shopping trip op_trip_a actual to $80", "user_id": "anonymous"})
    payload = staged.get_json() or {}
    applied = client.post("/api/copilot/apply", json={"staged_actions": payload.get("actions_taken") or {}, "text": "apply correction", "user_id": "anonymous"})
    assert applied.status_code == 200
    after = _safe_amount(client)
    assert round(after - before, 2) == 0.0


def test_reconciled_manual_and_plaid_expense_counts_once(client):
    post = client.post("/api/transactions", json={"description": "Dollar General", "amount": 38.0, "category": "discretionary"})
    manual_id = (post.get_json() or {}).get("id")

    item_id = _seed_plaid_base()
    _seed_plaid_tx(item_id=item_id, tx_id="tx_exp_1", amount_cents=3800, direction="outflow", merchant="DOLLAR GENERAL #123")

    with app.app_context():
        project_plaid_transactions(owner_scope="anonymous")
    mid_safe = _safe_amount(client)

    resp = client.post(
        "/api/reconciliation/decision",
        json={
            "user_id": "anonymous",
            "action": "match",
            "manual_transaction_id": manual_id,
            "plaid_transaction_id": "tx_exp_1",
        },
    )
    assert resp.status_code == 200
    with app.app_context():
        assert ExpenseTransaction.query.filter(ExpenseTransaction.category != "income").count() == 1
    assert round(_safe_amount(client), 2) == round(mid_safe, 2)


def test_reconciled_income_counts_once(client):
    with app.app_context():
        hid = current_household_id()
        account = Account.query.first()
        account.checking_balance += 500.0
        tx = ExpenseTransaction(
            household_id=hid,
            description="Employer payroll",
            amount=500.0,
            category="income",
            source="manual",
            local_account_id=account.id,
            date=datetime.now(timezone.utc),
        )
        db.session.add(tx)
        db.session.add(account)
        db.session.commit()
        manual_id = tx.id

    item_id = _seed_plaid_base()
    _seed_plaid_tx(item_id=item_id, tx_id="tx_inc_1", amount_cents=50000, direction="inflow", merchant="EMPLOYER PAYROLL")

    with app.app_context():
        project_plaid_transactions(owner_scope="anonymous")
    mid = _safe_amount(client)

    resp = client.post(
        "/api/reconciliation/decision",
        json={
            "user_id": "anonymous",
            "action": "match",
            "manual_transaction_id": manual_id,
            "plaid_transaction_id": "tx_inc_1",
        },
    )
    assert resp.status_code == 200
    assert round(_safe_amount(client), 2) == round(mid, 2)


def test_pending_to_posted_plaid_lifecycle_counts_once(client):
    item_id = _seed_plaid_base()
    _seed_plaid_tx(item_id=item_id, tx_id="tx_pending_1", amount_cents=10000, direction="outflow", merchant="Store A", pending=True)

    with app.app_context():
        project_plaid_transactions(owner_scope="anonymous")
        pending_row = PlaidTransaction.query.filter_by(plaid_transaction_id="tx_pending_1").first()
        pending_row.is_active_event = False
        pending_row.replaced_by_transaction_id = "tx_posted_1"
        db.session.add(pending_row)
        db.session.commit()

    _seed_plaid_tx(
        item_id=item_id,
        tx_id="tx_posted_1",
        amount_cents=10000,
        direction="outflow",
        merchant="Store A",
        replaces_pending_id="tx_pending_1",
    )

    with app.app_context():
        project_plaid_transactions(owner_scope="anonymous")
        assert ExpenseTransaction.query.filter(ExpenseTransaction.category != "income").count() == 1
        row = ExpenseTransaction.query.filter(ExpenseTransaction.category != "income").first()
        assert row.plaid_transaction_id == "tx_posted_1"


def test_balance_update_resets_safe_to_spend_truth(client):
    client.post("/api/account/update", json={"checking_balance": 1400.0})
    safe = _safe(client)
    comp = safe.get("components") or {}
    assert round(float(comp.get("usable_money") or 0.0), 2) == 1400.0


def test_new_manual_expense_recalculates_hero(client):
    before = _safe_amount(client)
    client.post("/api/transactions", json={"description": "Coffee", "amount": 9.5, "category": "discretionary"})
    assert round(_safe_amount(client) - before, 2) == -9.5


def test_new_income_recalculates_hero(client):
    before = _safe_amount(client)
    _add_income(days_ago=0, amount=300.0, desc="Side gig")
    assert round(_safe_amount(client) - before, 2) == 300.0


def test_discretionary_expense_changes_safe_to_spend_by_exact_amount_when_commitments_unchanged(client):
    before_safe = _safe_amount(client)
    before_components = _components(client)

    resp = client.post("/api/transactions", json={"description": "Coffee", "amount": 20.0, "category": "discretionary"})
    assert resp.status_code == 200

    after_safe = _safe_amount(client)
    after_components = _components(client)
    assert round(after_safe - before_safe, 2) == -20.0
    assert round(float(after_components.get("groceries_remaining") or 0.0), 2) == round(float(before_components.get("groceries_remaining") or 0.0), 2)
    assert round(float(after_components.get("other_committed_spending") or 0.0), 2) == round(float(before_components.get("other_committed_spending") or 0.0), 2)


def test_income_changes_safe_to_spend_by_exact_amount_when_obligations_unchanged(client):
    before_safe = _safe_amount(client)
    before_components = _components(client)

    _add_income(days_ago=0, amount=500.0, desc="Payroll")

    after_safe = _safe_amount(client)
    after_components = _components(client)
    assert round(after_safe - before_safe, 2) == 500.0
    assert round(float(after_components.get("groceries_remaining") or 0.0), 2) == round(float(before_components.get("groceries_remaining") or 0.0), 2)
    assert round(float(after_components.get("other_committed_spending") or 0.0), 2) == round(float(before_components.get("other_committed_spending") or 0.0), 2)


def test_income_does_not_resize_grocery_or_gas_commitments_by_default(client):
    before = _components(client)
    _add_income(days_ago=0, amount=500.0, desc="Payroll")
    after = _components(client)

    assert round(float(after.get("grocery_commitment_total") or 0.0), 2) == round(float(before.get("grocery_commitment_total") or 0.0), 2)
    assert round(float(after.get("groceries_remaining") or 0.0), 2) == round(float(before.get("groceries_remaining") or 0.0), 2)
    assert round(float(after.get("other_committed_spending") or 0.0), 2) == round(float(before.get("other_committed_spending") or 0.0), 2)


def test_finished_shopping_updates_cash_and_grocery_remaining_without_creating_money(client):
    before_safe = _safe_amount(client)
    before_components = _components(client)
    before_checking = round(float(before_components.get("usable_money") or 0.0), 2)
    before_remaining = round(float(before_components.get("groceries_remaining") or 0.0), 2)

    stage = client.post(
        "/api/grocery/finished-shopping/stage",
        json={"planned_total": 90.0, "actual_total": 130.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "sig-m9-fs"},
    )
    op_id = (stage.get_json() or {}).get("operation_id")
    done = client.post(
        "/api/grocery/finished-shopping/complete",
        json={"planned_total": 90.0, "actual_total": 130.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "sig-m9-fs", "operation_id": op_id, "confirm": True},
    )
    assert done.status_code == 200

    after_safe = _safe_amount(client)
    after_components = _components(client)
    after_checking = round(float(after_components.get("usable_money") or 0.0), 2)
    after_remaining = round(float(after_components.get("groceries_remaining") or 0.0), 2)

    assert round(after_checking - before_checking, 2) == -130.0
    assert round(before_remaining - after_remaining, 2) == 130.0
    assert round(after_safe - before_safe, 2) == 0.0
    assert after_safe <= before_safe


def test_payday_window_changes_only_after_income_semantics(client):
    _add_income(days_ago=5, amount=1426.0, desc="Initial payroll")
    before = _next_income_date(client)

    client.post("/api/transactions", json={"description": "Coffee", "amount": 20.0, "category": "discretionary"})
    after_expense = _next_income_date(client)
    assert after_expense == before

    _add_income(days_ago=0, amount=500.0, desc="Bonus")
    after_income = _next_income_date(client)
    assert after_income is not None
    assert after_income != before


def test_bill_change_recalculates_hero(client):
    r = client.post("/bills", json={"name": "Phone", "amount": 50, "due_date": (date.today() + timedelta(days=2)).isoformat()})
    assert r.status_code == 200
    down = _safe_amount(client)
    with app.app_context():
        b = Bill.query.filter_by(name="Phone").first()
        b.is_paid = True
        db.session.add(b)
        db.session.commit()
    up = _safe_amount(client)
    assert up > down


def test_buffer_change_recalculates_hero(client):
    base = _safe_amount(client)
    _set_buffer(client, 123.45)
    assert round(base - _safe_amount(client), 2) == 123.45


def test_negative_overcommitted_state(client):
    client.post("/api/account/update", json={"checking_balance": 50.0})
    client.post("/bills", json={"name": "Big Bill", "amount": 500, "due_date": (date.today() + timedelta(days=1)).isoformat()})
    safe = _safe(client)
    assert safe.get("state") == "overcommitted"
    assert float(safe.get("safe_to_spend") or 0.0) == 0


def test_breakdown_sums_exactly_to_hero(client):
    safe = _safe(client)
    lines = ((safe.get("breakdown") or {}).get("lines") or [])
    total = sum(int(row.get("amount_cents") or 0) for row in lines[:-1])
    hero = int((lines[-1] or {}).get("amount_cents") or 0)
    assert total == hero


def test_can_i_afford_projection_is_cent_accurate_and_writes_nothing(client):
    with app.app_context():
        before = ExpenseTransaction.query.count()
    resp = client.post("/api/decision/can-i-buy", json={"item_name": "Tool", "cost": 75.33})
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert round(float(body.get("safe_to_spend_now") - body.get("safe_to_spend_after")), 2) == 75.33
    with app.app_context():
        after = ExpenseTransaction.query.count()
    assert before == after


def test_plaid_disabled_stale_state_does_not_break_calculation(client):
    with app.app_context():
        hid = current_household_id()
        item = PlaidItem(
            household_id=hid,
            owner_scope="anonymous",
            plaid_item_id="item_paused",
            access_token_encrypted="enc",
            connection_status="connected",
            institution_name="First Platypus Bank",
            last_sync_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db.session.add(item)
        db.session.commit()

    ctl = client.post("/api/internal/usage/controls", json={"kill_switches": {"plaid_sync_enabled": False}})
    assert ctl.status_code == 200
    safe = _safe(client)
    freshness = safe.get("freshness") or {}
    assert "paused" in str(freshness.get("text") or "").lower()
    assert safe.get("safe_to_spend") is not None


def test_m8_usage_telemetry_unaffected_by_safe_to_spend_math(client):
    with app.app_context():
        before = UsageEvent.query.count()
    _summary(client)
    client.post("/api/decision/can-i-buy", json={"item_name": "Paper towels", "cost": 12.49})
    with app.app_context():
        after = UsageEvent.query.count()
    assert before == after


def test_db_safety_guard_remains_active():
    with pytest.raises(RuntimeError):
        assert_safe_destructive_db_target(
            "sqlite:////home/ky/finance_assistant/rung_finance.db",
            None,
        )
