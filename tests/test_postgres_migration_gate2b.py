from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

import app as appmod


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
        text=True,
        capture_output=True,
        check=False,
    )


def test_database_url_normalization_postgres_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNG_DB_PATH", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@localhost:5432/rung")
    assert appmod._resolve_database_uri() == "postgresql://u:p@localhost:5432/rung"


def test_invalid_database_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(appmod.app.config, "SQLALCHEMY_DATABASE_URI", "not-a-db-uri")
    with pytest.raises(RuntimeError):
        appmod._validate_startup_configuration()


def test_sqlite_migration_upgrade_is_idempotent() -> None:
    fd, db_path = tempfile.mkstemp(prefix="rung_gate2b_idempotent_", suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "FLASK_APP": "app.py",
    }
    first = _run([
        "/home/ky/finance_assistant/venv/bin/python",
        "-m",
        "flask",
        "db",
        "upgrade",
    ], env=env)
    assert first.returncode == 0, first.stderr

    second = _run([
        "/home/ky/finance_assistant/venv/bin/python",
        "-m",
        "flask",
        "db",
        "upgrade",
    ], env=env)
    assert second.returncode == 0, second.stderr


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_DATABASE_URL"), reason="POSTGRES_TEST_DATABASE_URL not set")
def test_sqlite_to_postgres_import_parity() -> None:
    postgres_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    src_path = f"/tmp/rung_gate2b_src_{os.getpid()}.db"

    upgrade = _run(
        [
            "/home/ky/finance_assistant/venv/bin/python",
            "-m",
            "flask",
            "db",
            "upgrade",
        ],
        env={"DATABASE_URL": f"sqlite:///{src_path}", "FLASK_APP": "app.py", "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert upgrade.returncode == 0, upgrade.stderr

    seed_code = """
from app import app
from extensions import db
from models import Household, Account, Bill, ExpenseTransaction, UserPreference
from datetime import datetime, timezone

with app.app_context():
    h = Household(public_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', legacy_scope_key='house-a')
    db.session.add(h)
    db.session.flush()
    db.session.add(Account(household_id=h.id, checking_balance=1234.56, balance_version=0))
    db.session.add(Bill(household_id=h.id, name='Seed Bill', amount=99.99, due_date=datetime.now(timezone.utc)))
    db.session.add(ExpenseTransaction(household_id=h.id, description='Seed Tx', amount=12.34, category='discretionary', source='manual'))
    db.session.add(UserPreference(household_id=h.id, key='favorite_proteins', value='[\"chicken\"]'))
    db.session.commit()
"""
    seed = _run(
        ["/home/ky/finance_assistant/venv/bin/python", "-c", seed_code],
        env={"DATABASE_URL": f"sqlite:///{src_path}", "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert seed.returncode == 0, seed.stderr

    apply_pg = _run(
        [
            "/home/ky/finance_assistant/venv/bin/python",
            "-m",
            "flask",
            "db",
            "upgrade",
        ],
        env={"DATABASE_URL": postgres_url, "FLASK_APP": "app.py", "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert apply_pg.returncode == 0, apply_pg.stderr

    reset_pg = _run([
        "psql",
        postgres_url,
        "-c",
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
    ])
    assert reset_pg.returncode == 0, reset_pg.stderr

    reapply_pg = _run(
        [
            "/home/ky/finance_assistant/venv/bin/python",
            "-m",
            "flask",
            "db",
            "upgrade",
        ],
        env={"DATABASE_URL": postgres_url, "FLASK_APP": "app.py", "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert reapply_pg.returncode == 0, reapply_pg.stderr

    importer = _run(
        [
            "/home/ky/finance_assistant/venv/bin/python",
            "scripts/sqlite_to_postgres_import.py",
            "--source-sqlite",
            src_path,
            "--target-url",
            postgres_url,
            "--allow-nonempty-target",
        ]
    )
    assert importer.returncode == 0, importer.stderr

    parity = _run(
        [
            "/home/ky/finance_assistant/venv/bin/python",
            "scripts/validate_sqlite_postgres_parity.py",
            "--source-sqlite",
            src_path,
            "--target-url",
            postgres_url,
        ]
    )
    assert parity.returncode == 0, parity.stderr + "\n" + parity.stdout
