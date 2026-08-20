from __future__ import annotations

import json
import os
import subprocess

import pytest


POSTGRES_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
        if "DATABASE_URL" in env:
            merged.pop("RUNG_DB_PATH", None)
    return subprocess.run(
        cmd,
        cwd="/home/ky/finance_assistant",
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(not POSTGRES_URL, reason="POSTGRES_TEST_DATABASE_URL not set")
def test_login_throttle_accumulates_and_recovers_on_postgres() -> None:
    reset = _run([
        "psql",
        POSTGRES_URL,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;",
    ])
    assert reset.returncode == 0, reset.stderr

    env = {
        "DATABASE_URL": POSTGRES_URL,
        "FLASK_APP": "app.py",
        "RUNG_ENV": "beta",
        "SECRET_KEY": "gate7-postgres-throttle-test-secret-0123456789012345",
        "PLAID_ENABLED": "0",
        "PLAID_CLIENT_ID": "",
        "PLAID_SECRET": "",
    }

    upgrade = _run([
        "/home/ky/finance_assistant/venv/bin/python",
        "-m",
        "flask",
        "db",
        "upgrade",
    ], env=env)
    assert upgrade.returncode == 0, upgrade.stderr

    seed_code = """
from werkzeug.security import generate_password_hash
from app import app
from extensions import db
from models import Account, Household, HouseholdMembership, User

with app.app_context():
    h = Household(public_id='11111111-1111-1111-1111-111111111111', legacy_scope_key='gate7-throttle-pg')
    db.session.add(h)
    db.session.flush()
    db.session.add(Account(household_id=h.id, checking_balance=1000.0, food_allocation_pct=40.0, pay_period_days=14, meals_per_day=3, expected_paycheck=1200.0))
    u = User(email='throttle@example.com', password_hash=generate_password_hash('good-pass-123'), active=True, auth_version=1)
    db.session.add(u)
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=u.id, household_id=h.id, role='owner', active=True))
    db.session.commit()
"""
    seed = _run([
        "/home/ky/finance_assistant/venv/bin/python",
        "-c",
        seed_code,
    ], env=env)
    assert seed.returncode == 0, seed.stderr

    probe_code = """
import json
from datetime import timedelta

from app import app
from extensions import db
from models import LoginThrottle
from services.auth_session import _LOGIN_FAIL_LIMIT

client = app.test_client()
responses = []
for _ in range(_LOGIN_FAIL_LIMIT):
    responses.append(client.post('/api/auth/login', json={'email': 'throttle@example.com', 'password': 'bad-pass'}).status_code)

blocked = client.post('/api/auth/login', json={'email': 'throttle@example.com', 'password': 'bad-pass'})
blocked_payload = blocked.get_json() or {}

with app.app_context():
    row = LoginThrottle.query.filter(LoginThrottle.subject_key.like('throttle@example.com|%')).first()
    assert row is not None
    row.blocked_until = row.window_started_at - timedelta(seconds=1)
    db.session.add(row)
    db.session.commit()

after_expiry_valid = client.post('/api/auth/login', json={'email': 'throttle@example.com', 'password': 'good-pass-123'})

with app.app_context():
    remaining_rows = LoginThrottle.query.filter(LoginThrottle.subject_key.like('throttle@example.com|%')).count()

after_reset_fail = client.post('/api/auth/login', json={'email': 'throttle@example.com', 'password': 'bad-pass'})

print(json.dumps({
    'responses_before_block': responses,
    'blocked_status': blocked.status_code,
    'blocked_error': blocked_payload.get('error'),
    'blocked_retry_after': int(blocked_payload.get('retry_after_seconds') or 0),
    'after_expiry_valid_status': after_expiry_valid.status_code,
    'remaining_rows_after_valid_login': remaining_rows,
    'after_reset_fail_status': after_reset_fail.status_code,
}))
"""
    probe = _run([
        "/home/ky/finance_assistant/venv/bin/python",
        "-c",
        probe_code,
    ], env=env)
    assert probe.returncode == 0, probe.stderr

    payload = json.loads((probe.stdout or "").strip())
    assert payload["responses_before_block"] == [401] * payload["responses_before_block"].__len__()
    assert payload["blocked_status"] == 429
    assert payload["blocked_error"] == "Invalid credentials."
    assert payload["blocked_retry_after"] > 0
    assert payload["after_expiry_valid_status"] == 200
    assert payload["remaining_rows_after_valid_login"] == 0
    assert payload["after_reset_fail_status"] == 401
