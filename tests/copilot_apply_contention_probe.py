"""Separate-process Copilot apply probe for explicitly disposable databases."""
from __future__ import annotations

import hashlib
import hmac
import multiprocessing as mp
import os
from uuid import uuid4


OPERATION_ID = "copilot-contention-same-operation"
PAYLOAD = {
    "operation_id": OPERATION_ID,
    "recipes_added": [], "recipes_auto_filled": [], "recipes_suggested": [],
    "grocery_list": [],
    "grocery_items_added": [{"item_name": "dish soap", "estimated_price": 4.25}],
    "expenses_logged": [{"description": "gas", "category": "gas", "amount": 12.50}],
    "income_logged": [], "balance_reconciliations": [], "shopping_trip_corrections": [],
    "bills_added": [{"name": "Streaming Plus", "amount": 19.99}],
    "bills_updated": [], "bills_removed": [],
    "clarification_flags": {"need_clarification": False, "clarification_reasons": []},
    "requires_confirmation": True, "staged": True, "summary": "contention probe",
}


def worker(_index: int) -> tuple[int, str | None, bool]:
    from app import app, _copilot_stage_binding
    with app.app_context():
        payload = dict(PAYLOAD)
        payload["operation_binding"] = _copilot_stage_binding(payload["operation_id"])
        response = app.test_client().post(
            "/api/copilot/apply", json={"text": "approved", "staged_actions": payload}
        )
    body = response.get_json() or {}
    actions = body.get("actions_taken") or {}
    return response.status_code, actions.get("operation_id"), bool(actions.get("already_applied"))


def signed_headers(public_id: str) -> dict[str, str]:
    signature = hmac.new(
        os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"].encode(), public_id.encode(), hashlib.sha256
    ).hexdigest()
    return {"X-Household-Id": public_id, "X-Household-Signature": signature}


if __name__ == "__main__":
    os.environ.setdefault("RUNG_HOUSEHOLD_CONTEXT_SECRET", "copilot-contention-disposable")
    from app import app
    from extensions import db
    from models import Account, ActionAudit, Bill, ExpenseTransaction, GroceryItem, Household
    from services.household_context import ensure_legacy_household

    with app.app_context():
        # Fresh SQLite pytest probes begin empty; PostgreSQL acceptance is
        # migrated to head before invoking this script.
        db.create_all()
        household = ensure_legacy_household()
        hid = household.id
        ActionAudit.query.filter_by(household_id=hid).delete(synchronize_session=False)
        Bill.query.filter_by(household_id=hid).delete(synchronize_session=False)
        ExpenseTransaction.query.filter_by(household_id=hid).delete(synchronize_session=False)
        GroceryItem.query.filter_by(household_id=hid).delete(synchronize_session=False)
        account = Account.query.filter_by(household_id=hid).first()
        if account is None:
            db.session.add(Account(household_id=hid, checking_balance=1000, pay_period_days=14))
        else:
            account.checking_balance = 1000
        db.session.commit()

    ctx = mp.get_context("spawn")
    with ctx.Pool(8) as pool:
        results = pool.map(worker, range(8))

    with app.app_context():
        assert {status for status, _, _ in results} == {200}, results
        assert {operation for _, operation, _ in results} == {OPERATION_ID}, results
        assert ActionAudit.query.filter_by(household_id=hid, operation_id=OPERATION_ID).count() == 1
        assert Bill.query.filter_by(household_id=hid).count() == 1
        assert ExpenseTransaction.query.filter_by(household_id=hid).count() == 1
        assert GroceryItem.query.filter_by(household_id=hid).count() == 1
        assert round(Account.query.filter_by(household_id=hid).one().checking_balance, 2) == 987.50

        from app import _copilot_stage_binding
        bound_payload = dict(PAYLOAD)
        bound_payload["operation_binding"] = _copilot_stage_binding(OPERATION_ID)

        conflict = dict(bound_payload)
        conflict["expenses_logged"] = [{"description": "gas", "category": "gas", "amount": 99.00}]
        rejected = app.test_client().post(
            "/api/copilot/apply", json={"text": "conflict", "staged_actions": conflict}
        )
        assert rejected.status_code == 400
        assert ExpenseTransaction.query.filter_by(household_id=hid).one().amount == 12.50

        invalid = dict(bound_payload)
        invalid["operation_id"] = "copilot-invalid-multi-action"
        invalid["operation_binding"] = _copilot_stage_binding(invalid["operation_id"])
        invalid["recipes_added"] = [{"id": 999999, "title": "Missing recipe"}]
        invalid_response = app.test_client().post(
            "/api/copilot/apply", json={"text": "invalid multi", "staged_actions": invalid}
        )
        assert invalid_response.status_code == 400
        assert ActionAudit.query.filter_by(household_id=hid, operation_id=invalid["operation_id"]).count() == 0
        assert Bill.query.filter_by(household_id=hid).count() == 1
        assert ExpenseTransaction.query.filter_by(household_id=hid).count() == 1
        assert GroceryItem.query.filter_by(household_id=hid).count() == 1

        other = Household(public_id=str(uuid4()), legacy_scope_key="copilot-other-" + uuid4().hex)
        db.session.add(other); db.session.flush()
        db.session.add(Account(household_id=other.id, checking_balance=500, pay_period_days=14))
        db.session.commit()
        # The probe deliberately keeps one outer app context for assertions;
        # clear its cached default household before exercising a signed request.
        from flask import g
        g.pop("_household_context", None)
        other_response = app.test_client().post(
            "/api/copilot/apply",
            headers=signed_headers(other.public_id),
            json={"text": "approved", "staged_actions": bound_payload},
        )
        assert other_response.status_code == 409, other_response.get_json()
        assert ActionAudit.query.filter_by(household_id=other.id, operation_id=OPERATION_ID).count() == 0
        assert ExpenseTransaction.query.filter_by(household_id=other.id).count() == 0
        assert round(Account.query.filter_by(household_id=other.id).one().checking_balance, 2) == 500.00

        print({"responses": results, "audit_rows": 1, "bills": 1, "expenses": 1,
               "grocery_items": 1, "balance": 987.50, "household_isolation": True,
               "conflict_failed_closed": True, "invalid_multi_rolled_back": True})
