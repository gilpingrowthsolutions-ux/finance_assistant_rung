"""Integration tests for the AI Copilot parser and dispatch endpoint."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app, db, Account, Bill, ExpenseTransaction, GroceryItem

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

client = app.test_client()
app.testing = True
_pass = 0
_fail = 0


def _setup():
    """Fresh DB with a single account."""
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(checking_balance=1250.00))
        db.session.commit()


def _assert_eq(a, b, msg=""):
    global _pass, _fail
    if a == b:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL {msg}: expected {b!r}, got {a!r}")


def _assert_truthy(val, msg=""):
    global _pass, _fail
    if val:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL {msg}: value is falsy")


# ---------------------------------------------------------------------------
# 1 — Regex fallback parser (no LLM credentials)
# ---------------------------------------------------------------------------


def test_copilot_parse_bills_regex():
    """Regex fallback extracts bills with $/mo pattern."""
    _setup()
    with app.app_context():
        Bill.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "Add Netflix $22.99/mo and Spotify $9.99 per month"},
    )
    _assert_eq(resp.status_code, 200, "copilot/parse returns 200")
    d = resp.get_json() or {}

    _assert_truthy(d.get("_fallback"), "regex fallback flag is set")
    actions = d.get("actions_taken", {})
    bills = actions.get("bills_added", [])
    _assert_eq(len(bills), 2, "two bills extracted")
    names = {b["name"] for b in bills}
    _assert_truthy("netflix" in names or "spotify" in names, "bill names found")

    with app.app_context():
        db_bills = Bill.query.all()
        _assert_eq(len(db_bills), 2, "two bills persisted")


def test_copilot_parse_grocery_regex():
    """Regex fallback extracts household items."""
    _setup()
    with app.app_context():
        GroceryItem.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "I need dish soap and paper towels for the kitchen"},
    )
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    items = actions.get("grocery_items_added", [])
    _assert_truthy(len(items) >= 1, "at least one grocery item found")

    with app.app_context():
        db_items = GroceryItem.query.all()
        _assert_truthy(len(db_items) >= 1, "grocery items persisted")


def test_copilot_parse_discretionary_regex():
    """Regex fallback extracts dining-out events."""
    _setup()
    with app.app_context():
        ExpenseTransaction.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "Dinner out at Olive Garden $45"},
    )
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    events = actions.get("expenses_logged", [])
    _assert_truthy(len(events) >= 1, "discretionary event extracted")


def test_copilot_parse_recipes_regex():
    """Regex fallback extracts recipe suggestions."""
    _setup()
    resp = client.post(
        "/api/copilot/parse",
        json={"text": "Cook chicken rice bowl and flank steak fajitas this week"},
    )
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    recipes = actions.get("recipes_suggested", [])
    _assert_truthy(len(recipes) >= 1, "at least one recipe suggested")


# ---------------------------------------------------------------------------
# 2 — Combined multi-intent input
# ---------------------------------------------------------------------------


def test_copilot_multi_intent():
    """A single message with bills + grocery + discretionary is handled."""
    _setup()
    with app.app_context():
        Bill.query.delete()
        ExpenseTransaction.query.delete()
        GroceryItem.query.delete()
        db.session.commit()

    text = (
        "Meal prep chicken rice bowl. "
        "Add Netflix $22.99/mo. "
        "I need dish soap and paper towels. "
        "Dinner out at Chili's $35"
    )
    resp = client.post("/api/copilot/parse", json={"text": text})
    _assert_eq(resp.status_code, 200)
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})

    total = (
        len(actions.get("bills_added", []))
        + len(actions.get("expenses_logged", []))
        + len(actions.get("grocery_items_added", []))
        + len(actions.get("recipes_suggested", []))
    )
    _assert_truthy(total >= 2, f"multi-intent had at least 2 actions (got {total})")


# ---------------------------------------------------------------------------
# 3 — Empty / missing input
# ---------------------------------------------------------------------------


def test_copilot_empty_text():
    _setup()
    resp = client.post("/api/copilot/parse", json={"text": ""})
    _assert_eq(resp.status_code, 400, "empty text returns 400")


def test_copilot_missing_text():
    _setup()
    resp = client.post("/api/copilot/parse", json={})
    _assert_eq(resp.status_code, 400, "missing text returns 400")


# ---------------------------------------------------------------------------
# 4 — No-op input (text with no actionable content)
# ---------------------------------------------------------------------------


def test_copilot_noop():
    """Gibberish that matches no patterns returns empty actions."""
    _setup()
    resp = client.post("/api/copilot/parse", json={"text": "hello world"})
    _assert_eq(resp.status_code, 200)
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    total = sum(len(v) for v in (actions or {}).values() if isinstance(v, list))
    _assert_eq(total, 0, "no actions for no-op input")


# ---------------------------------------------------------------------------
# 5 — Account balance decrement on expenses
# ---------------------------------------------------------------------------


def test_copilot_expense_decrements_balance():
    """Logging an expense via copilot should decrement checking_balance."""
    _setup()
    with app.app_context():
        acc = Account.query.first()
        acc.checking_balance = 1000.00
        ExpenseTransaction.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "buy a new impact driver $120"},
    )
    _assert_eq(resp.status_code, 200)

    with app.app_context():
        acc = Account.query.first()
        _assert_truthy(acc.checking_balance < 1000.00, "balance was decremented")


# ---------------------------------------------------------------------------
# 6 — Bill removal (placeholder — regex fallback won't catch this)
# ---------------------------------------------------------------------------


def test_copilot_bill_removal():
    """Ensure the remove action path doesn't crash."""
    _setup()
    from datetime import datetime, timedelta

    with app.app_context():
        b = Bill(name="Hulu", amount=15.99, due_date=datetime.utcnow() + timedelta(days=10))
        db.session.add(b)
        db.session.commit()

    resp = client.post("/api/copilot/parse", json={"text": "Remove Hulu"})
    _assert_eq(resp.status_code, 200)
    d = resp.get_json() or {}
    _assert_truthy("actions_taken" in d, "response has actions_taken")


# =============================================================================
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"{fn.__name__} ...")
        try:
            fn()
        except Exception as exc:
            _fail += 1
            print(f"  ERROR: {exc}")
    print(f"\n{_pass} passed, {_fail} failed")
    sys.exit(1 if _fail else 0)
