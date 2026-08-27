"""Package 11 database authority for cross-process first-run creation."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
PRIOR_REVISION = "9f7a3d4c1e2b"
HEAD_REVISION = "c81d4e5f7a92"


def _run(args: list[str], *, db_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["RUNG_DB_PATH"] = str(db_path)
    env["PYTHONPATH"] = str(ROOT)
    env.pop("DATABASE_URL", None)
    return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)


def _upgrade(db_path: Path, revision: str = "head") -> subprocess.CompletedProcess[str]:
    return _run([str(PYTHON), "-m", "flask", "--app", "app", "db", "upgrade", revision], db_path=db_path)


def test_migration_blocks_existing_duplicate_account_authority(tmp_path: Path) -> None:
    db_path = tmp_path / "duplicate_preflight.sqlite"
    first = _upgrade(db_path, PRIOR_REVISION)
    assert first.returncode == 0, first.stderr

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO household (public_id, legacy_scope_key, created_at) VALUES (?, ?, ?)",
            ("11111111-1111-1111-1111-111111111111", "duplicate-scope", "2026-08-19 00:00:00"),
        )
        household_id = connection.execute(
            "SELECT id FROM household WHERE legacy_scope_key = 'duplicate-scope'"
        ).fetchone()[0]
        account_columns = (
            "household_id, checking_balance, food_allocation_pct, pay_period_days, meals_per_day, "
            "vault_balance, expected_paycheck, is_onboarded, household_size, latitude, longitude, "
            "zip_code, city_state, sales_tax_rate, grocery_tax_rate, balance_version, "
            "kroger_location_id, kroger_store_name"
        )
        values = (household_id, None, 40, 0, 3, 150, None, 0, 4, None, None, "65084", "", .0825, .0125, 0, None, "Kroger")
        placeholders = ",".join("?" for _ in values)
        connection.execute(f"INSERT INTO account ({account_columns}) VALUES ({placeholders})", values)
        connection.execute(f"INSERT INTO account ({account_columns}) VALUES ({placeholders})", values)
        connection.commit()

    blocked = _upgrade(db_path)
    assert blocked.returncode != 0
    assert "duplicate canonical authority exists" in (blocked.stderr + blocked.stdout)
    assert "account household_ids" in (blocked.stderr + blocked.stdout)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM account WHERE household_id = ?", (household_id,)).fetchone()[0] == 2
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PRIOR_REVISION


def test_database_constraint_rejects_second_account(tmp_path: Path) -> None:
    db_path = tmp_path / "constraint.sqlite"
    upgraded = _upgrade(db_path)
    assert upgraded.returncode == 0, upgraded.stderr

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO household (public_id, legacy_scope_key, created_at) VALUES (?, ?, ?)",
            ("22222222-2222-2222-2222-222222222222", "constraint-scope", "2026-08-19 00:00:00"),
        )
        household_id = connection.execute("SELECT id FROM household WHERE legacy_scope_key='constraint-scope'").fetchone()[0]
        connection.execute("INSERT INTO account (household_id, balance_version) VALUES (?, 0)", (household_id,))
        connection.commit()
        try:
            connection.execute("INSERT INTO account (household_id, balance_version) VALUES (?, 0)", (household_id,))
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
        else:
            raise AssertionError("database accepted a second Account for one household")


def test_migration_preserves_existing_single_account_values(tmp_path: Path) -> None:
    db_path = tmp_path / "preserve.sqlite"
    first = _upgrade(db_path, PRIOR_REVISION)
    assert first.returncode == 0, first.stderr
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO household (public_id, legacy_scope_key, created_at) VALUES (?, ?, ?)",
            ("33333333-3333-3333-3333-333333333333", "preserve-scope", "2026-08-19 00:00:00"),
        )
        household_id = connection.execute("SELECT id FROM household WHERE legacy_scope_key='preserve-scope'").fetchone()[0]
        connection.execute(
            "INSERT INTO account (household_id, checking_balance, pay_period_days, expected_paycheck, balance_version) "
            "VALUES (?, ?, ?, ?, ?)",
            (household_id, 987.65, 14, 2345.67, 7),
        )
        connection.commit()

    upgraded = _upgrade(db_path)
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT checking_balance, pay_period_days, expected_paycheck, balance_version "
            "FROM account WHERE household_id=?",
            (household_id,),
        ).fetchone() == (987.65, 14, 2345.67, 7)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


def test_separate_processes_create_one_truthful_household_account(tmp_path: Path) -> None:
    db_path = tmp_path / "cross_process.sqlite"
    upgraded = _upgrade(db_path)
    assert upgraded.returncode == 0, upgraded.stderr

    start_at = time.time() + 2.0
    worker = r"""
import json, os, time
from app import app
from services.household_context import ensure_legacy_household
from services.financial_state import get_household_account
with app.app_context():
    while time.time() < float(os.environ['RUNG_CREATE_START_AT']):
        time.sleep(0.002)
    household = ensure_legacy_household()
    account = get_household_account(household.id)
    print(json.dumps({
        'household_id': household.id,
        'account_id': account.id,
        'checking_balance': account.checking_balance,
        'pay_period_days': account.pay_period_days,
        'expected_paycheck': account.expected_paycheck,
    }))
"""
    processes: list[subprocess.Popen[str]] = []
    for _ in range(8):
        env = dict(os.environ)
        env["RUNG_DB_PATH"] = str(db_path)
        env["PYTHONPATH"] = str(ROOT)
        env["RUNG_CREATE_START_AT"] = str(start_at)
        env.pop("DATABASE_URL", None)
        processes.append(subprocess.Popen(
            [str(PYTHON), "-c", worker], cwd=ROOT, env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))

    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))

    assert len({row["household_id"] for row in results}) == 1
    assert len({row["account_id"] for row in results}) == 1
    assert all(row["checking_balance"] is None for row in results)
    assert all(row["pay_period_days"] == 0 for row in results)
    assert all(row["expected_paycheck"] is None for row in results)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM household WHERE legacy_scope_key='anonymous'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 1
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_DATABASE_URL"), reason="POSTGRES_TEST_DATABASE_URL not set")
def test_postgres_separate_process_authority() -> None:
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    database_name = str(make_url(database_url).database or "").lower()
    assert database_name != "rung_prod"
    assert any(token in database_name for token in ("test", "gate", "disposable", "pkg11"))

    reset = subprocess.run(
        ["psql", database_url, "-v", "ON_ERROR_STOP=1", "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert reset.returncode == 0, reset.stderr

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(ROOT)
    env.pop("RUNG_DB_PATH", None)
    upgrade = subprocess.run(
        [str(PYTHON), "-m", "flask", "--app", "app", "db", "upgrade"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    assert upgrade.returncode == 0, upgrade.stderr

    start_at = time.time() + 2.0
    worker = r"""
import json, os, time
from app import app
from services.household_context import ensure_legacy_household
from services.financial_state import get_household_account
with app.app_context():
    while time.time() < float(os.environ['RUNG_CREATE_START_AT']):
        time.sleep(0.002)
    household = ensure_legacy_household()
    account = get_household_account(household.id)
    print(json.dumps({'household_id': household.id, 'account_id': account.id,
        'checking_balance': account.checking_balance, 'pay_period_days': account.pay_period_days,
        'expected_paycheck': account.expected_paycheck}))
"""
    processes = []
    for _ in range(8):
        worker_env = dict(env)
        worker_env["RUNG_CREATE_START_AT"] = str(start_at)
        processes.append(subprocess.Popen(
            [str(PYTHON), "-c", worker], cwd=ROOT, env=worker_env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))
    rows = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        rows.append(json.loads(stdout.strip().splitlines()[-1]))

    assert len({row["household_id"] for row in rows}) == 1
    assert len({row["account_id"] for row in rows}) == 1
    assert all(row["checking_balance"] is None for row in rows)
    assert all(row["pay_period_days"] == 0 for row in rows)
    assert all(row["expected_paycheck"] is None for row in rows)

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM household WHERE legacy_scope_key='anonymous'")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM account")).scalar_one() == 1
            constraints = connection.execute(text(
                "SELECT COUNT(*) FROM pg_constraint WHERE conname='uq_account_household_id'"
            )).scalar_one()
            assert constraints == 1
    finally:
        engine.dispose()
