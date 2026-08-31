#!/usr/bin/env python3
"""Idempotency tests for staged Copilot apply flow (/api/copilot/apply)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Use a file-backed SQLite DB for the concurrency test so multiple
# connections/threads can observe the same database state.
os.environ["RUNG_DB_PATH"] = "/tmp/rung_copilot_idempotency.sqlite"

from app import (  # noqa: E402
    app,
    db,
    Account,
    Bill,
    ExpenseTransaction,
    GroceryItem,
    ActionAudit,
    _copilot_stage_binding,
)
from services.household_context import household_id as current_household_id

app.testing = True
client = app.test_client()

_pass = 0
_fail = 0


def _ok(msg: str) -> None:
    global _pass
    _pass += 1
    print("  PASS:", msg)


def _bad(msg: str, expected=None, actual=None) -> None:
    global _fail
    _fail += 1
    print("  FAIL:", msg)
    if expected is not None:
        print("    expected:", repr(expected))
    if actual is not None:
        print("    actual:  ", repr(actual))


def _check(cond: bool, msg: str, expected=None, actual=None) -> None:
    if cond:
        _ok(msg)
    else:
        _bad(msg, expected, actual)


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1000.0, pay_period_days=14, meals_per_day=3, kroger_store_name="Walmart"))
        db.session.commit()


def _make_operation_id() -> str:
    return "op_test_" + uuid.uuid4().hex


def _staged_payload(operation_id: str, expense_amount: float = 12.5) -> dict:
    return {
        "operation_id": operation_id,
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_suggested": [],
        "grocery_list": [],
        "grocery_items_added": [{"item_name": "dish soap", "estimated_price": 4.25}],
        "expenses_logged": [{"description": "gas", "category": "gas", "amount": expense_amount}],
        "bills_added": [{"name": "Streaming Plus", "amount": 19.99}],
        "bills_updated": [],
        "bills_removed": [],
        "clarification_flags": {"need_clarification": False, "clarification_reasons": []},
        "requires_confirmation": True,
        "staged": True,
        "summary": "staged test",
    }


def _post_apply(staged_actions: dict):
    staged_actions = dict(staged_actions)
    with app.app_context():
        staged_actions.setdefault("operation_binding", _copilot_stage_binding(staged_actions["operation_id"]))
    return client.post(
        "/api/copilot/apply",
        json={"text": "apply staged op", "staged_actions": staged_actions, "user_id": "idem-user"},
    )


def _counts() -> tuple[int, int, int]:
    with app.app_context():
        return Bill.query.count(), ExpenseTransaction.query.count(), GroceryItem.query.count()


def test_a_apply_once_then_again_no_duplicates() -> None:
    print("A. apply once then apply exact same operation again")
    _setup()
    operation_id = _make_operation_id()
    payload = _staged_payload(operation_id)

    r1 = _post_apply(payload)
    d1 = r1.get_json() or {}
    c1 = _counts()
    _check(r1.status_code == 200, "first apply returns 200", 200, r1.status_code)
    _check(c1 == (1, 1, 1), "first apply creates one bill/expense/grocery", (1, 1, 1), c1)

    r2 = _post_apply(payload)
    d2 = r2.get_json() or {}
    c2 = _counts()
    _check(r2.status_code == 200, "second apply returns 200", 200, r2.status_code)
    _check(c2 == (1, 1, 1), "second apply does not duplicate rows", (1, 1, 1), c2)
    _check((d2.get("actions_taken") or {}).get("already_applied") is True, "second apply explicitly reports already_applied")
    _check((d1.get("undo_token") or "") == (d2.get("undo_token") or ""), "replay returns original undo token")


def test_b_frontend_double_click_simulation() -> None:
    print("\nB. double-click simulation with rapid repeated apply")
    _setup()
    operation_id = _make_operation_id()
    payload = _staged_payload(operation_id)

    r1 = _post_apply(payload)
    r2 = _post_apply(payload)
    c = _counts()
    _check(r1.status_code == 200 and r2.status_code == 200, "both apply calls return 200")
    _check(c == (1, 1, 1), "double-click simulation still results in one set of writes", (1, 1, 1), c)


def test_c_retry_after_successful_commit() -> None:
    print("\nC. retry after successful backend commit")
    _setup()
    operation_id = _make_operation_id()
    payload = _staged_payload(operation_id)

    first = _post_apply(payload)
    # Simulate lost response by ignoring `first` body and retrying same operation.
    retry = _post_apply(payload)
    c = _counts()
    _check(first.status_code == 200 and retry.status_code == 200, "initial apply and retry both return 200")
    _check(c == (1, 1, 1), "retry after commit does not duplicate side effects", (1, 1, 1), c)


def test_d_new_operation_allows_intentionally_similar_expense() -> None:
    print("\nD. new operation ID allows intentionally similar action")
    _setup()

    op1 = _make_operation_id()
    op2 = _make_operation_id()
    p1 = _staged_payload(op1, expense_amount=20.0)
    p2 = _staged_payload(op2, expense_amount=20.0)

    _post_apply(p1)
    _post_apply(p2)

    with app.app_context():
        expenses = ExpenseTransaction.query.count()
        audits = ActionAudit.query.filter(ActionAudit.operation_id.in_([op1, op2])).count()
    _check(expenses == 2, "similar expense in new operation is allowed", 2, expenses)
    _check(audits == 2, "two different operation IDs create two audit records", 2, audits)


def test_e_apply_once_then_undo_unambiguous() -> None:
    print("\nE. apply once then undo is unambiguous")
    _setup()
    operation_id = _make_operation_id()
    payload = _staged_payload(operation_id)

    resp = _post_apply(payload)
    undo_token = (resp.get_json() or {}).get("undo_token")
    _check(bool(undo_token), "undo token returned on first apply")

    undo_resp = client.post("/api/copilot/undo", json={"undo_token": undo_token, "user_id": "idem-user"})
    c = _counts()
    _check(undo_resp.status_code == 200, "undo returns 200", 200, undo_resp.status_code)
    _check(c == (0, 0, 0), "undo removes applied side effects", (0, 0, 0), c)


def test_f_apply_twice_then_undo_single_relationship() -> None:
    print("\nF. apply same operation twice then undo once")
    _setup()
    operation_id = _make_operation_id()
    payload = _staged_payload(operation_id)

    r1 = _post_apply(payload)
    r2 = _post_apply(payload)
    d1 = r1.get_json() or {}
    d2 = r2.get_json() or {}
    token = d1.get("undo_token")

    with app.app_context():
        audits = ActionAudit.query.filter_by(operation_id=operation_id).all()
    _check(len(audits) == 1, "single audit row exists for operation despite two apply calls", 1, len(audits))
    _check(token == d2.get("undo_token"), "replayed apply returns same undo token")

    undo_resp = client.post("/api/copilot/undo", json={"undo_token": token, "user_id": "idem-user"})
    _check(undo_resp.status_code == 200, "undo succeeds after duplicate apply attempts", 200, undo_resp.status_code)

    undo_again = client.post("/api/copilot/undo", json={"undo_token": token, "user_id": "idem-user"})
    _check(undo_again.status_code == 400, "second undo is rejected as already undone", 400, undo_again.status_code)


def test_g_invalid_operation_not_marked_applied() -> None:
    print("\nG. invalid operation fails before commit and is not marked applied")
    _setup()
    operation_id = _make_operation_id()
    bad_payload = _staged_payload(operation_id)
    bad_payload["grocery_items_added"][0]["estimated_price"] = "not-a-number"

    bad = _post_apply(bad_payload)
    _check(bad.status_code == 400, "invalid payload returns 400", 400, bad.status_code)

    with app.app_context():
        audits = ActionAudit.query.filter_by(operation_id=operation_id).count()
        c = (Bill.query.count(), ExpenseTransaction.query.count(), GroceryItem.query.count())
    _check(audits == 0, "failed apply leaves no persisted operation audit row", 0, audits)
    _check(c == (0, 0, 0), "failed apply leaves no side effects", (0, 0, 0), c)


def test_h_concurrent_apply_same_operation() -> None:
    """Use a fresh process-bound DB; never share pytest's singleton in-memory connection."""
    fd, db_path = tempfile.mkstemp(prefix="rung_copilot_pytest_contention_", suffix=".sqlite")
    os.close(fd)
    env = os.environ.copy()
    env["RUNG_DB_PATH"] = db_path
    env["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "copilot-contention-disposable"
    env["FLASK_SECRET_KEY"] = "copilot-contention-disposable-stage-binding"
    env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    probe = os.path.join(os.path.dirname(__file__), "copilot_apply_contention_probe.py")
    result = subprocess.run([sys.executable, probe], env=env, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "'audit_rows': 1" in result.stdout
    assert "'household_isolation': True" in result.stdout


def test_i_conflicting_payload_reuse_fails_closed() -> None:
    _setup()
    operation_id = _make_operation_id()
    original = _staged_payload(operation_id, expense_amount=12.50)
    conflict = _staged_payload(operation_id, expense_amount=99.00)
    assert _post_apply(original).status_code == 200
    rejected = _post_apply(conflict)
    assert rejected.status_code == 400
    assert "different staged content" in (rejected.get_json() or {}).get("error", "")
    with app.app_context():
        assert [row.amount for row in ExpenseTransaction.query.all()] == [12.50]
        assert ActionAudit.query.filter_by(operation_id=operation_id).count() == 1


def _main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()

    print("\n" + "=" * 60)
    print(f"{_pass} passed, {_fail} failed")
    raise SystemExit(1 if _fail else 0)


if __name__ == "__main__":
    _main()
