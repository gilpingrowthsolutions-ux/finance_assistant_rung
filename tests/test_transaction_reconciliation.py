from __future__ import annotations

import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from sqlalchemy.exc import IntegrityError

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ["PLAID_TOKEN_ENCRYPTION_KEY"] = "oR4fJCv6Em2vQ2n80AFM9x5QdSXQ66j3qnP8Qf1EBz8="
os.environ.setdefault("PLAID_CLIENT_ID", "plaid-client-id-test")
os.environ.setdefault("PLAID_SECRET", "plaid-secret-test")
os.environ.setdefault("PLAID_ENV", "sandbox")

from app import app
from extensions import db
from models import Account, ExpenseTransaction, Household, PlaidItem, PlaidTransaction, ShoppingTripCompletion, TransactionReconciliation
import services.plaid_foundation as pf
from services.household_context import household_id as current_household_id


app.testing = True
client = app.test_client()
TODAY = datetime.now(timezone.utc).date()


def _date_text(days_from_today: int = 0) -> str:
    return (TODAY + timedelta(days=days_from_today)).isoformat()


class FakePlaidClient:
    def __init__(self) -> None:
        self.sync_pages: list[dict[str, Any]] = []
        self.accounts_payload: list[dict[str, Any]] = []
        self.item_payload: dict[str, Any] = {"item": {"institution_id": "ins_109508"}}

    def create_link_token(self, user_scope: str) -> dict[str, Any]:
        return {"link_token": "link-token", "expiration": "2030-01-01T00:00:00Z", "request_id": "r"}

    def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        return {"access_token": "access-token", "item_id": "item_1", "request_id": "rx"}

    def get_item(self, access_token: str) -> dict[str, Any]:
        return self.item_payload

    def get_accounts(self, access_token: str) -> dict[str, Any]:
        return {"accounts": list(self.accounts_payload)}

    def transactions_sync(self, access_token: str, cursor: Optional[str]) -> dict[str, Any]:
        if self.sync_pages:
            return self.sync_pages.pop(0)
        return {"added": [], "modified": [], "removed": [], "has_more": False, "next_cursor": cursor}


def _setup(*, balance: float = 1000.0, extra_accounts: int = 0) -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=balance))
        for idx in range(extra_accounts):
            db.session.add(Account(household_id=current_household_id(), checking_balance=0.0 + idx))
        db.session.commit()


def _connect(monkeypatch: pytest.MonkeyPatch, fake: FakePlaidClient, *, account_map: Optional[int] = 1) -> None:
    monkeypatch.setattr(pf, "get_plaid_http_client", lambda: fake)
    fake.accounts_payload = [
        {
            "account_id": "acc_1",
            "name": "Plaid Checking",
            "official_name": "Plaid Checking",
            "mask": "0000",
            "type": "depository",
            "subtype": "checking",
        }
    ]
    payload = {"public_token": "public-token", "user_id": "anonymous"}
    if account_map is not None:
        payload["rung_account_id"] = account_map
    resp = client.post("/api/plaid/exchange-public-token", json=payload)
    assert resp.status_code == 200


def _plaid_tx(*, tx_id: str, amount: float, merchant: str, date_text: str, pending: bool = False, pending_id: Optional[str] = None) -> dict[str, Any]:
    return {
        "transaction_id": tx_id,
        "account_id": "acc_1",
        "pending": pending,
        "pending_transaction_id": pending_id,
        "amount": amount,
        "name": merchant,
        "merchant_name": merchant,
        "date": date_text,
        "authorized_date": date_text,
        "iso_currency_code": "USD",
        "category": ["Shops", "Discount Store"],
    }


def _sync(monkeypatch: pytest.MonkeyPatch, fake: FakePlaidClient, pages: list[dict[str, Any]]) -> dict[str, Any]:
    monkeypatch.setattr(pf, "get_plaid_http_client", lambda: fake)
    fake.sync_pages = pages
    resp = client.post("/api/plaid/sync-transactions", json={"user_id": "anonymous"})
    assert resp.status_code == 200
    return resp.get_json() or {}


def _proposal_rows() -> list[dict[str, Any]]:
    resp = client.get("/api/reconciliation/proposals", query_string={"user_id": "anonymous"})
    assert resp.status_code == 200
    return (resp.get_json() or {}).get("proposals") or []


def _manual_expense(desc: str, amount: float, category: str = "discretionary") -> int:
    resp = client.post("/api/transactions", json={"description": desc, "amount": amount, "category": category})
    assert resp.status_code == 200
    return int((resp.get_json() or {}).get("id"))


def test_manual_expense_plus_exact_plaid_candidate_creates_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _manual_expense("Dollar General", 38.00)

    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_1", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    props = _proposal_rows()
    assert len(props) == 1


def test_no_merge_before_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _manual_expense("Dollar General", 38.00)
    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_1", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])

    with app.app_context():
        assert ExpenseTransaction.query.count() == 1
        acc = Account.query.first()
        assert round(float(acc.checking_balance), 2) == 962.00


def test_confirmed_match_counts_one_expense_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    manual_id = _manual_expense("Dollar General", 38.00)
    page = {"added": [_plaid_tx(tx_id="tx_1", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}
    _sync(monkeypatch, fake, [page])

    for _ in range(2):
        resp = client.post("/api/reconciliation/decision", json={
            "user_id": "anonymous",
            "action": "match",
            "manual_transaction_id": manual_id,
            "plaid_transaction_id": "tx_1",
        })
        assert resp.status_code == 200

    _sync(monkeypatch, fake, [{"added": [], "modified": [page["added"][0]], "removed": [], "has_more": False, "next_cursor": "c2"}])

    with app.app_context():
        assert ExpenseTransaction.query.count() == 1
        tx = ExpenseTransaction.query.get(manual_id)
        assert tx.plaid_transaction_id == "tx_1"
        assert round(float(Account.query.first().checking_balance), 2) == 962.00
        assert TransactionReconciliation.query.filter_by(status="matched").count() == 1


def test_keep_separate_preserves_both_and_rejected_pair_not_resurfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    manual_id = _manual_expense("Dollar General", 38.00)
    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_1", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])

    keep = client.post("/api/reconciliation/decision", json={
        "user_id": "anonymous",
        "action": "keep_separate",
        "manual_transaction_id": manual_id,
        "plaid_transaction_id": "tx_1",
    })
    assert keep.status_code == 200

    _sync(monkeypatch, fake, [{"added": [], "modified": [_plaid_tx(tx_id="tx_1", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "removed": [], "has_more": False, "next_cursor": "c2"}])
    props = _proposal_rows()
    assert props == []

    with app.app_context():
        assert ExpenseTransaction.query.count() == 2
        rej = TransactionReconciliation.query.one()
        assert rej.status == "rejected"

    reversed_decision = client.post("/api/reconciliation/decision", json={
        "user_id": "anonymous",
        "action": "match",
        "manual_transaction_id": manual_id,
        "plaid_transaction_id": "tx_1",
    })
    assert reversed_decision.status_code == 400

    with app.app_context():
        assert TransactionReconciliation.query.one().status == "rejected"
        assert ExpenseTransaction.query.count() == 2
        assert round(float(Account.query.first().checking_balance), 2) == 924.00


def test_multiple_candidates_require_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _manual_expense("Walmart", 147.82, "grocery")
    _manual_expense("Walmart Supercenter", 147.82, "grocery")

    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_w", amount=147.82, merchant="WALMART SUPERCENTER 357", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    props = _proposal_rows()
    assert len(props) == 2


def test_different_amount_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    manual_id = _manual_expense("Dollar General", 38.00)
    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_2", amount=39.00, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    assert _proposal_rows() == []
    crafted_match = client.post("/api/reconciliation/decision", json={
        "user_id": "anonymous",
        "action": "match",
        "manual_transaction_id": manual_id,
        "plaid_transaction_id": "tx_2",
    })
    assert crafted_match.status_code == 400
    with app.app_context():
        assert ExpenseTransaction.query.count() == 2
        assert TransactionReconciliation.query.count() == 0
        assert round(float(Account.query.first().checking_balance), 2) == 923.00


def test_incompatible_direction_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _manual_expense("Payroll", 1426.00, "income")

    inflow_tx = _plaid_tx(tx_id="tx_dep", amount=1426.0, merchant="EMPLOYER PAYROLL", date_text=_date_text())
    outflow_same = dict(inflow_tx)
    outflow_same["amount"] = 1426.0  # outflow in Plaid model because non-negative
    _sync(monkeypatch, fake, [{"added": [outflow_same], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    assert _proposal_rows() == []


def test_second_same_household_account_is_rejected() -> None:
    _setup(balance=1000.0)
    with app.app_context():
        db.session.add(Account(household_id=current_household_id(), checking_balance=0.0))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


@pytest.mark.parametrize(("manual_days_ago", "expected_proposals"), [(3, 1), (4, 0)])
def test_date_window_boundary_is_durable(monkeypatch: pytest.MonkeyPatch, manual_days_ago: int, expected_proposals: int) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)

    with app.app_context():
        account = Account.query.first()
        manual_tx = ExpenseTransaction(
            household_id=current_household_id(),
            description="Dollar General",
            amount=38.0,
            category="discretionary",
            source="manual",
            local_account_id=account.id,
            date=datetime.combine(TODAY - timedelta(days=manual_days_ago), datetime.min.time(), tzinfo=timezone.utc),
        )
        db.session.add(manual_tx)
        db.session.commit()

    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_date", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    props = _proposal_rows()
    assert len(props) == expected_proposals


def test_normalized_merchant_variation_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _manual_expense("Walmart", 147.82, "grocery")
    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_wm", amount=147.82, merchant="WALMART SUPERCENTER 357", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    assert len(_proposal_rows()) == 1


def test_manual_income_and_plaid_income_reconcile_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=500.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)

    with app.app_context():
        account = Account.query.first()
        tx = ExpenseTransaction(
            household_id=current_household_id(),
            description="Employer payroll",
            amount=1426.0,
            category="income",
            source="manual",
            local_account_id=account.id,
        )
        db.session.add(tx)
        account.checking_balance += 1426.0
        db.session.commit()
        manual_id = tx.id

    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_income", amount=-1426.0, merchant="EMPLOYER PAYROLL", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    props = _proposal_rows()
    assert len(props) == 1

    match = client.post("/api/reconciliation/decision", json={
        "user_id": "anonymous",
        "action": "match",
        "manual_transaction_id": manual_id,
        "plaid_transaction_id": "tx_income",
    })
    assert match.status_code == 200

    with app.app_context():
        assert ExpenseTransaction.query.count() == 1
        linked = ExpenseTransaction.query.get(manual_id)
        assert linked.plaid_transaction_id == "tx_income"


def test_finished_shopping_match_preserves_trip_and_single_grocery_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)

    with app.app_context():
        account = Account.query.first()
        tx = ExpenseTransaction(
            household_id=current_household_id(),
            description="Grocery trip Walmart",
            amount=147.82,
            category="grocery",
            source="manual",
            local_account_id=account.id,
        )
        db.session.add(tx)
        db.session.flush()
        trip = ShoppingTripCompletion(
            household_id=current_household_id(),
            operation_id="op_trip_1",
            trip_token="trip_1",
            transaction_id=tx.id,
            retailer="walmart",
            store_name="Walmart",
            store_id="357",
            planned_total_cents=15000,
            actual_total_cents=14782,
            amount_source="actual",
            cart_signature="sig",
            manual_provisional=True,
        )
        db.session.add(trip)
        account.checking_balance -= 147.82
        db.session.commit()
        manual_id = tx.id

    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_groc", amount=147.82, merchant="WALMART SUPERCENTER 357", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    props = _proposal_rows()
    assert len(props) == 1

    match = client.post("/api/reconciliation/decision", json={
        "user_id": "anonymous",
        "action": "match",
        "manual_transaction_id": manual_id,
        "plaid_transaction_id": "tx_groc",
    })
    assert match.status_code == 200

    with app.app_context():
        tx = ExpenseTransaction.query.get(manual_id)
        trip = ShoppingTripCompletion.query.filter_by(operation_id="op_trip_1").one()
        assert tx.plaid_transaction_id == "tx_groc"
        assert trip.transaction_id == manual_id
        assert trip.planned_total_cents == 15000
        assert trip.actual_total_cents == 14782
        grocery_rows = ExpenseTransaction.query.filter_by(category="grocery").count()
        assert grocery_rows == 1


def test_balance_reconciliation_is_excluded_from_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=900.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)

    # Milestone 6 style balance update does not create manual transactions;
    # ensure no fake match proposals are generated from non-existent rows.
    staged = {
        "operation_id": "op_balance_1",
        "balance_reconciliations": [{"new_balance": 1000.0, "reason": "manual_reconciliation"}],
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_suggested": [],
        "grocery_list": [],
        "grocery_items_added": [],
        "expenses_logged": [],
        "income_logged": [],
        "shopping_trip_corrections": [],
        "bills_added": [],
        "bills_updated": [],
        "bills_removed": [],
    }
    from app import _copilot_stage_binding
    with app.app_context():
        staged["operation_binding"] = _copilot_stage_binding(staged["operation_id"])
    apply_resp = client.post("/api/copilot/apply", json={"staged_actions": staged, "text": "reconcile", "user_id": "anonymous"})
    assert apply_resp.status_code == 200

    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_b", amount=100.0, merchant="SOMETHING", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    assert _proposal_rows() == []


def test_pending_to_posted_lifecycle_does_not_duplicate_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _manual_expense("Dollar General", 38.00)

    posted_date = datetime.now(timezone.utc).date()
    pending_date = posted_date - timedelta(days=1)

    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_pending", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=pending_date.isoformat(), pending=True)], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])
    props1 = _proposal_rows()
    assert len(props1) == 1

    posted = _plaid_tx(tx_id="tx_posted", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=posted_date.isoformat(), pending=False, pending_id="tx_pending")
    _sync(monkeypatch, fake, [{"added": [posted], "modified": [], "removed": [], "has_more": False, "next_cursor": "c2"}])
    props2 = _proposal_rows()
    assert len(props2) == 1
    assert props2[0]["bank"]["plaid_transaction_id"] == "tx_posted"


def test_plaid_first_manual_later_stage_offers_use_existing_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_pf", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])

    def _fake_parse(_text: str, staging_only: bool = True):
        return {
            "selected_recipes": [],
            "discretionary_events": [],
            "spending_events": [{"amount": 38.0, "merchant": "Dollar General", "description": "Dollar General", "category": "discretionary", "date": _date_text()}],
            "income_events": [],
            "bill_updates": [],
            "shopping_requirements": [],
            "grocery_additions": [],
            "shopping_corrections": [],
        }

    monkeypatch.setattr("app.parse_copilot_prompt", _fake_parse)
    stage = client.post("/api/copilot/stage", json={"text": "I forgot I spent $38 at Dollar General", "user_id": "anonymous"})
    assert stage.status_code == 200
    actions = (stage.get_json() or {}).get("actions_taken") or {}
    expense = (actions.get("expenses_logged") or [])[0]
    assert len(expense.get("candidate_plaid_transactions") or []) >= 1

    staged = actions
    staged["expenses_logged"][0]["reconciliation_action"] = "use_existing"
    staged["expenses_logged"][0]["selected_plaid_transaction_id"] = "tx_pf"
    apply_resp = client.post("/api/copilot/apply", json={"staged_actions": staged, "text": "apply", "user_id": "anonymous"})
    assert apply_resp.status_code == 200

    with app.app_context():
        assert ExpenseTransaction.query.count() == 1


def test_plaid_first_record_another_creates_separate_and_persists_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(balance=1000.0)
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_pf2", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])

    def _fake_parse(_text: str, staging_only: bool = True):
        return {
            "selected_recipes": [],
            "discretionary_events": [],
            "spending_events": [{"amount": 38.0, "merchant": "Dollar General", "description": "Dollar General", "category": "discretionary", "date": _date_text()}],
            "income_events": [],
            "bill_updates": [],
            "shopping_requirements": [],
            "grocery_additions": [],
            "shopping_corrections": [],
        }

    monkeypatch.setattr("app.parse_copilot_prompt", _fake_parse)
    stage = client.post("/api/copilot/stage", json={"text": "I forgot I spent $38 at Dollar General", "user_id": "anonymous"})
    actions = (stage.get_json() or {}).get("actions_taken") or {}
    actions["expenses_logged"][0]["reconciliation_action"] = "record_another"
    actions["expenses_logged"][0]["selected_plaid_transaction_id"] = "tx_pf2"

    apply_resp = client.post("/api/copilot/apply", json={"staged_actions": actions, "text": "apply", "user_id": "anonymous"})
    assert apply_resp.status_code == 200

    with app.app_context():
        assert ExpenseTransaction.query.count() == 2
        row = TransactionReconciliation.query.order_by(TransactionReconciliation.id.desc()).first()
        assert row.status == "rejected"


def test_failed_reconciliation_request_does_not_mutate_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect(monkeypatch, fake)
    manual_id = _manual_expense("Dollar General", 38.0)
    _sync(monkeypatch, fake, [{"added": [_plaid_tx(tx_id="tx_fail", amount=38.0, merchant="DOLLAR GENERAL #1234", date_text=_date_text())], "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}])

    bad = client.post("/api/reconciliation/decision", json={
        "user_id": "anonymous",
        "action": "unknown",
        "manual_transaction_id": manual_id,
        "plaid_transaction_id": "tx_fail",
    })
    assert bad.status_code == 400

    with app.app_context():
        tx = ExpenseTransaction.query.get(manual_id)
        assert tx.plaid_transaction_id is None


def test_reconciliation_candidates_and_direct_ids_are_household_scoped() -> None:
    _setup(balance=1000.0)
    manual_id = _manual_expense("Dollar General", 38.0)
    secret = "reconciliation-household-test-secret"
    os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = secret

    with app.app_context():
        house_b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="house-b")
        db.session.add(house_b)
        db.session.flush()
        db.session.add(Account(household_id=house_b.id, checking_balance=2000.0))
        item_b = PlaidItem(
            household_id=house_b.id,
            owner_scope="anonymous",
            plaid_item_id="item_house_b",
            access_token_encrypted="disposable-test-token",
        )
        db.session.add(item_b)
        db.session.flush()
        db.session.add(PlaidTransaction(
            household_id=house_b.id,
            owner_scope="anonymous",
            plaid_item_id=item_b.id,
            plaid_transaction_id="tx_house_b",
            plaid_account_id="acc_house_b",
            amount_cents=3800,
            signed_amount_cents=-3800,
            direction="outflow",
            name="Dollar General",
            merchant_name="Dollar General",
            description="Dollar General",
            transaction_date=TODAY,
            authorized_date=TODAY,
        ))
        db.session.commit()
        house_b_public_id = house_b.public_id

    signature = hmac.new(secret.encode(), house_b_public_id.encode(), hashlib.sha256).hexdigest()
    headers_b = {"X-Household-Id": house_b_public_id, "X-Household-Signature": signature}

    assert _proposal_rows() == []
    crafted = client.post("/api/reconciliation/decision", json={
        "user_id": "anonymous",
        "action": "match",
        "manual_transaction_id": manual_id,
        "plaid_transaction_id": "tx_house_b",
    })
    assert crafted.status_code == 400
    assert (client.get("/api/reconciliation/proposals", headers=headers_b).get_json() or {}).get("proposals") == []

    with app.app_context():
        assert ExpenseTransaction.query.filter_by(id=manual_id, plaid_transaction_id=None).count() == 1
        assert TransactionReconciliation.query.count() == 0
