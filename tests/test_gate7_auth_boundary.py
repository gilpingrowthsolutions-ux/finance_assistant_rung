from __future__ import annotations

import os
import uuid

import pytest

# Isolate this suite to a disposable DB before importing app.
os.environ.setdefault("RUNG_DB_PATH", f"/tmp/rung_gate7_auth_{uuid.uuid4().hex}.db")

from app import app, db  # noqa: E402
from models import Account, Bill, Household, HouseholdMembership, User  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


@pytest.fixture(autouse=True)
def _gate7_auth_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNG_ENV", "beta")
    monkeypatch.delenv("RUNG_ALLOW_HOUSEHOLD_HEADER_OVERRIDE", raising=False)
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield


def _seed_households_and_users() -> dict[str, int]:
    with app.app_context():
        house_a = Household(public_id="00000000-0000-0000-0000-000000000001", legacy_scope_key="gate7-a")
        house_b = Household(public_id="00000000-0000-0000-0000-000000000002", legacy_scope_key="gate7-b")
        db.session.add(house_a)
        db.session.add(house_b)
        db.session.flush()

        db.session.add(Account(household_id=house_a.id, checking_balance=1000.0, food_allocation_pct=40.0, pay_period_days=14, meals_per_day=3, expected_paycheck=1200.0))
        db.session.add(Account(household_id=house_b.id, checking_balance=800.0, food_allocation_pct=35.0, pay_period_days=14, meals_per_day=3, expected_paycheck=900.0))

        user_a = User(email="alpha@example.com", password_hash=generate_password_hash("pass-alpha-123"), active=True, auth_version=1)
        user_b = User(email="bravo@example.com", password_hash=generate_password_hash("pass-bravo-123"), active=True, auth_version=1)
        user_inactive = User(email="inactive@example.com", password_hash=generate_password_hash("pass-inactive-123"), active=False, auth_version=1)
        db.session.add(user_a)
        db.session.add(user_b)
        db.session.add(user_inactive)
        db.session.flush()

        db.session.add(HouseholdMembership(user_id=user_a.id, household_id=house_a.id, role="owner", active=True))
        db.session.add(HouseholdMembership(user_id=user_b.id, household_id=house_b.id, role="owner", active=True))
        db.session.commit()

        return {
            "house_a_id": int(house_a.id),
            "house_b_id": int(house_b.id),
        }


def _login(client, email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_login_logout_and_session_boundary():
    _seed_households_and_users()
    client = app.test_client()

    blocked = client.get("/api/budget/summary")
    assert blocked.status_code == 401

    bad = _login(client, "alpha@example.com", "wrong")
    assert bad.status_code == 401
    assert (bad.get_json() or {}).get("error") == "Invalid credentials."

    inactive = _login(client, "inactive@example.com", "pass-inactive-123")
    assert inactive.status_code == 401
    assert (inactive.get_json() or {}).get("error") == "Invalid credentials."

    ok = _login(client, "alpha@example.com", "pass-alpha-123")
    assert ok.status_code == 200
    assert (ok.get_json() or {}).get("authenticated") is True

    current = client.get("/api/auth/session")
    assert current.status_code == 200
    payload = current.get_json() or {}
    assert payload.get("authenticated") is True
    assert (payload.get("user") or {}).get("email") == "alpha@example.com"

    summary = client.get("/api/budget/summary")
    assert summary.status_code == 200

    out = client.post("/api/auth/logout")
    assert out.status_code == 200
    assert (out.get_json() or {}).get("authenticated") is False

    blocked_after = client.get("/api/budget/summary")
    assert blocked_after.status_code == 401


def test_two_household_isolation_and_direct_idor_block():
    _seed_households_and_users()
    client = app.test_client()

    assert _login(client, "alpha@example.com", "pass-alpha-123").status_code == 200
    created = client.post("/bills", json={"name": "Rent", "amount": 700, "due_date": "2026-09-01"})
    assert created.status_code == 200
    bill_id = int((created.get_json() or {}).get("id"))
    client.post("/api/auth/logout")

    assert _login(client, "bravo@example.com", "pass-bravo-123").status_code == 200
    b_bills = client.get("/bills")
    assert b_bills.status_code == 200
    assert (b_bills.get_json() or []) == []

    foreign_delete = client.delete(f"/bills/{bill_id}")
    assert foreign_delete.status_code == 404


def test_forged_household_header_rejected_in_beta_mode():
    _seed_households_and_users()
    client = app.test_client()
    assert _login(client, "alpha@example.com", "pass-alpha-123").status_code == 200
    created = client.post("/bills", json={"name": "Power", "amount": 120, "due_date": "2026-09-05"})
    assert created.status_code == 200

    forged = client.get(
        "/bills",
        headers={
            "X-Household-Id": "00000000-0000-0000-0000-000000000002",
            "X-Household-Signature": "bad-signature",
        },
    )
    assert forged.status_code == 200
    rows = forged.get_json() or []
    assert len(rows) == 1
    assert rows[0]["name"] == "Power"


def test_login_throttle_blocks_repeated_failures():
    _seed_households_and_users()
    client = app.test_client()

    for _ in range(5):
        resp = _login(client, "alpha@example.com", "wrong")
        assert resp.status_code == 401

    blocked = _login(client, "alpha@example.com", "wrong")
    assert blocked.status_code == 429
    payload = blocked.get_json() or {}
    assert payload.get("error") == "Invalid credentials."
    assert int(payload.get("retry_after_seconds") or 0) > 0
