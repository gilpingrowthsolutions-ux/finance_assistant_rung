from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile

import pytest
from sqlalchemy import create_engine, text

PRODUCTION_SQLITE = Path("/home/ky/finance_assistant/rung_finance.db")
EXPECTED_PRODUCTION_SHA = "6d54ce3c29edd19f9ad0f9404fdf0d98f3896649d9b5dd0609691dd728bd5fd7"
POSTGRES_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL") or "postgresql://rung_gate2b:rung_gate2b_test_only@localhost:5432/rung_gate2b"
POSTGRES_RESTORE_URL = os.environ.get("POSTGRES_RESTORE_TEST_DATABASE_URL") or "postgresql://rung_gate2b:rung_gate2b_test_only@localhost:5432/rung_gate6_restore"

# Use disposable sqlite for in-process app tests.
os.environ["RUNG_DB_PATH"] = f"/tmp/rung_gate6_harness_{uuid.uuid4().hex}.db"
os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "gate6-test-secret"
os.environ.setdefault("PLAID_CLIENT_ID", "plaid_test_client")
os.environ.setdefault("PLAID_SECRET", "plaid_test_secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("PLAID_TOKEN_ENCRYPTION_KEY", "x7cUQ1K8v1SCh4skQ53QqE5s8z3v8c2n6cihVQMcWDo=")

from app import app  # noqa: E402
from extensions import db  # noqa: E402
from models import Account, ExpenseTransaction, Household, ShoppingTripCompletion, StoreTaxProfile, TaxSourceDataset  # noqa: E402
from services.household_context import household_id as current_household_id  # noqa: E402
from services.retail.shared_foundation import shared_retail_foundation  # noqa: E402
from services.tax_engine import ensure_bootstrap_tax_dataset, import_dataset_atomic, resolve_store_tax_profile  # noqa: E402
from services.tax_adapters import MissouriDorQ3Adapter  # noqa: E402


@dataclass
class Gate6Summary:
    production_sha_before: str = ""
    production_sha_after: str = ""
    idempotency_flood_trip_count: int = 0
    idempotency_flood_tx_count: int = 0
    idempotency_flood_balance: Decimal = Decimal("0.00")
    lease_reacquire_seconds: float = 0.0
    national_states_checked: int = 0
    sqlite_backup_integrity_ok: bool = False
    postgres_backup_restore_ok: bool = False


SUMMARY = Gate6Summary()


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: str = "/home/ky/finance_assistant") -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
        if "DATABASE_URL" in env:
            merged.pop("RUNG_DB_PATH", None)
    return subprocess.run(cmd, cwd=cwd, env=merged, capture_output=True, text=True, check=False)


def _assert_disposable_postgres(url: str) -> None:
    lowered = url.lower()
    if "localhost" not in lowered and "127.0.0.1" not in lowered:
        raise AssertionError(f"Refusing non-local postgres URL: {url}")
    if "test" not in lowered and "gate" not in lowered:
        raise AssertionError(f"Refusing non-disposable postgres URL: {url}")


def _sign(public_id: str) -> str:
    secret = os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"]
    return hmac.new(secret.encode("utf-8"), public_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(public_id: str) -> dict[str, str]:
    return {
        "X-Household-Id": public_id,
        "X-Household-Signature": _sign(public_id),
    }


def _reset_sqlite_runtime_db() -> tuple[str, int]:
    with app.app_context():
        db.drop_all()
        db.create_all()
        house = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="gate6-house-a")
        db.session.add(house)
        db.session.flush()
        db.session.add(Account(household_id=house.id, checking_balance=Decimal("1000.00")))
        db.session.commit()
        return house.public_id, house.id


def _run_core_gate_suites() -> None:
    env = {
        "POSTGRES_TEST_DATABASE_URL": POSTGRES_URL,
    }
    suites = [
        "tests/test_household_context_security_gate2a.py",
        "tests/test_safe_to_spend_m9.py",
        "tests/test_shared_retail_foundation_gate3.py",
        "tests/test_serpapi_hard_cap_concurrency.py",
        "tests/test_retail_provider_waterfall_gate4.py",
        "tests/test_gate5_tax_engine.py",
    ]
    failures: list[str] = []
    for suite in suites:
        completed = _run(
            [
                "/home/ky/finance_assistant/venv/bin/python",
                "-m",
                "pytest",
                "-q",
                suite,
            ],
            env=env,
        )
        if completed.returncode != 0:
            failures.append(
                f"SUITE: {suite}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            )
    if failures:
        raise AssertionError("Focused Gate suites failed.\n\n" + "\n\n".join(failures))


def test_gate6_production_sqlite_hash_protected_before() -> None:
    assert PRODUCTION_SQLITE.exists(), "Production SQLite file missing"
    before = _sha256(PRODUCTION_SQLITE)
    SUMMARY.production_sha_before = before
    assert before == EXPECTED_PRODUCTION_SHA


def test_gate6_retain_existing_gate_coverage() -> None:
    _assert_disposable_postgres(POSTGRES_URL)
    _run_core_gate_suites()


def test_gate6_idempotency_flood_finished_shopping_50_duplicates() -> None:
    public_id, hid = _reset_sqlite_runtime_db()
    payload = {
        "confirm": True,
        "operation_id": "gate6-flood-op",
        "planned_total": 70.0,
        "actual_total": 70.0,
        "retailer": "walmart",
        "store_name": "Walmart",
        "store_id": "357",
        "cart_signature": "gate6-flood-cart",
    }
    headers = _headers(public_id)

    statuses: list[int] = []
    failures: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(50)

    def worker() -> None:
        try:
            client = app.test_client()
            barrier.wait()
            response = client.post("/api/grocery/finished-shopping/complete", json=payload, headers=headers)
            with lock:
                statuses.append(response.status_code)
        except Exception as exc:  # pragma: no cover - failure path assertion below
            with lock:
                failures.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(statuses) == 50
    assert all(status == 200 for status in statuses)

    with app.app_context():
        trip_count = ShoppingTripCompletion.query.filter_by(household_id=hid, operation_id="gate6-flood-op").count()
        tx_count = ExpenseTransaction.query.filter_by(household_id=hid, category="grocery").count()
        acct = Account.query.filter_by(household_id=hid).first()
        assert acct is not None
        balance = Decimal(str(acct.checking_balance)).quantize(Decimal("0.01"))

    assert trip_count == 1
    assert tx_count == 1
    assert balance == Decimal("930.00")

    SUMMARY.idempotency_flood_trip_count = trip_count
    SUMMARY.idempotency_flood_tx_count = tx_count
    SUMMARY.idempotency_flood_balance = balance


def test_gate6_refresh_owner_crash_lease_recovers() -> None:
    with app.app_context():
        resource_key = "search:walmart:357:milk"
        acquired, owner = shared_retail_foundation.acquire_refresh_lease(
            resource_key=resource_key,
            lease_owner="gate6-owner-a",
            lease_seconds=1,
        )
        assert acquired is True
        start = time.monotonic()
        time.sleep(1.25)
        acquired2, _ = shared_retail_foundation.acquire_refresh_lease(
            resource_key=resource_key,
            lease_owner="gate6-owner-b",
            lease_seconds=1,
        )
        elapsed = time.monotonic() - start
        assert acquired2 is True
        shared_retail_foundation.release_refresh_lease(resource_key=resource_key, lease_owner=owner)

    SUMMARY.lease_reacquire_seconds = elapsed


def test_gate6_national_tax_degraded_honesty() -> None:
    states = ["MO", "CA", "TX", "FL", "OH", "WA", "NY", "CO"]
    with app.app_context():
        ensure_bootstrap_tax_dataset()
        for idx, state in enumerate(states):
            profile = resolve_store_tax_profile(
                retailer="walmart",
                retailer_store_id=f"gate6-{idx}-{state}",
                store_name="Walmart",
                store_address="",
                zip_code="",
                city_state=f"City, {state}",
                latitude=None,
                longitude=None,
                calculation_date=date(2026, 8, 15),
                owner_scope="gate6",
            )
            assert profile.state == state
            assert profile.location_precision in {"STATE_ONLY", "UNRESOLVED", "ZIP5", "CITY_STATE"}

    SUMMARY.national_states_checked = len(states)


def test_gate6_sqlite_backup_restore_disposable_copy() -> None:
    src_copy = Path(f"/tmp/rung_gate6_source_{uuid.uuid4().hex}.db")
    backup_path = Path(f"/tmp/rung_gate6_backup_{uuid.uuid4().hex}.db")
    restored_path = Path(f"/tmp/rung_gate6_restored_{uuid.uuid4().hex}.db")

    shutil.copy2(PRODUCTION_SQLITE, src_copy)

    with sqlite3.connect(str(src_copy)) as source_conn:
        with sqlite3.connect(str(backup_path)) as backup_conn:
            source_conn.backup(backup_conn)

    # Mutate source copy only; production DB is never touched.
    with sqlite3.connect(str(src_copy)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS gate6_mutation_probe(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO gate6_mutation_probe(note) VALUES('mutated')")
        conn.commit()

    shutil.copy2(backup_path, restored_path)

    with sqlite3.connect(str(restored_path)) as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        assert row is not None
        assert row[0] == "ok"
        SUMMARY.sqlite_backup_integrity_ok = True


def test_gate6_postgres_backup_restore_disposable_database() -> None:
    _assert_disposable_postgres(POSTGRES_URL)
    _assert_disposable_postgres(POSTGRES_RESTORE_URL)
    source_url = POSTGRES_URL
    source_engine = create_engine(source_url)
    with source_engine.begin() as conn:
        assert conn.dialect.name == "postgresql"

    # Ensure schema at head on source.
    upgraded = _run(
        ["/home/ky/finance_assistant/venv/bin/python", "-m", "flask", "db", "upgrade"],
        env={"FLASK_APP": "app.py", "DATABASE_URL": source_url, "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert upgraded.returncode == 0, upgraded.stderr or upgraded.stdout

    seed_code = """
from app import app
from extensions import db
from models import Household, Account, ExpenseTransaction, ShoppingTripCompletion
import uuid

with app.app_context():
    suffix = uuid.uuid4().hex[:12]
    h = Household(public_id=str(uuid.uuid4()), legacy_scope_key=f'gate6-pg-house-{suffix}')
    db.session.add(h)
    db.session.flush()
    a = Account(household_id=h.id, checking_balance=1500.00)
    db.session.add(a)
    db.session.flush()
    tx = ExpenseTransaction(household_id=h.id, description='Gate6 Seed Expense', amount=25.00, category='grocery', source='manual', local_account_id=a.id)
    db.session.add(tx)
    db.session.flush()
    db.session.add(ShoppingTripCompletion(household_id=h.id, operation_id=f'gate6-op-{suffix}', trip_token=f'gate6-token-{suffix}', transaction_id=tx.id, planned_total_cents=2500, actual_total_cents=2500, amount_source='actual', retailer='walmart', store_id='357', store_name='Walmart'))
    db.session.commit()
"""
    seeded = _run(
        ["/home/ky/finance_assistant/venv/bin/python", "-c", seed_code],
        env={"DATABASE_URL": source_url, "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert seeded.returncode == 0, seeded.stderr or seeded.stdout

    dump_path = Path(f"/tmp/rung_gate6_restore_{uuid.uuid4().hex[:10]}.dump")
    restore_url = POSTGRES_RESTORE_URL

    # Target DB is explicitly disposable; reset schema before restore.
    reset_restore = _run([
        "psql",
        restore_url,
        "-c",
        "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;",
    ])
    assert reset_restore.returncode == 0, reset_restore.stderr or reset_restore.stdout

    dumped = _run([
        "pg_dump",
        "--format=custom",
        "--file",
        str(dump_path),
        source_url,
    ])
    assert dumped.returncode == 0, dumped.stderr or dumped.stdout

    restored = _run([
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        restore_url,
        str(dump_path),
    ])
    assert restored.returncode == 0, restored.stderr or restored.stdout

    src_engine = create_engine(source_url)
    dst_engine = create_engine(restore_url)
    tables = [
        "household",
        "account",
        "expense_transactions",
        "shopping_trip_completion",
        "store_product_observation",
        "tax_source_dataset",
        "usage_limit_counter",
    ]
    for table_name in tables:
        with src_engine.begin() as src_conn:
            src_count = src_conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        with dst_engine.begin() as dst_conn:
            dst_count = dst_conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        assert int(src_count) == int(dst_count), f"table {table_name} mismatch: {src_count} != {dst_count}"

    rw_check_code = """
from app import app
from extensions import db
from models import Account

with app.app_context():
    account = Account.query.first()
    assert account is not None
    account.food_allocation_pct = float(account.food_allocation_pct or 40.0)
    account.pay_period_days = int(account.pay_period_days or 14)
    db.session.add(account)
    db.session.commit()
    refreshed = Account.query.first()
    print('RESTORE_RW_OK', refreshed.id)
"""
    rw = _run(
        ["/home/ky/finance_assistant/venv/bin/python", "-c", rw_check_code],
        env={"DATABASE_URL": restore_url, "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert rw.returncode == 0, rw.stderr or rw.stdout

    SUMMARY.postgres_backup_restore_ok = True


def test_gate6_database_unreachable_fails_closed() -> None:
    bad_url = "postgresql://invalid:invalid@127.0.0.1:1/rung_unreachable"
    probe = _run(
        [
            "/home/ky/finance_assistant/venv/bin/python",
            "-c",
            "from app import app; from extensions import db;\nwith app.app_context():\n db.session.execute('SELECT 1')",
        ],
        env={"DATABASE_URL": bad_url, "PLAID_ENABLED": "0", "PLAID_CLIENT_ID": "", "PLAID_SECRET": ""},
    )
    assert probe.returncode != 0
    assert "sqlite:///" not in (probe.stdout + probe.stderr)


def test_gate6_tax_failure_preserves_last_valid_dataset() -> None:
    with app.app_context():
        ensure_bootstrap_tax_dataset()
        good = import_dataset_atomic(
            adapter=MissouriDorQ3Adapter(),
            source_path=str(Path("/home/ky/finance_assistant/data/tax/official/missouri")),
            activate=True,
        )
        assert bool(good.get("ok")) is True
        before = TaxSourceDataset.query.filter_by(status="active").first()
        assert before is not None

        # Missing source path simulates import failure.
        failed = import_dataset_atomic(
            adapter=MissouriDorQ3Adapter(),
            source_path="/tmp/does_not_exist_gate6",
            activate=True,
        )
        assert bool(failed.get("ok")) is False
        after = TaxSourceDataset.query.filter_by(status="active").first()
        assert after is not None
        assert after.id == before.id


def test_gate6_reconciliation_match_four_process_race_is_idempotent() -> None:
    _assert_disposable_postgres(POSTGRES_URL)

    script = '''
import json
import os
import hmac
import hashlib
import uuid
from datetime import date
from multiprocessing import get_context

os.environ.pop("RUNG_DB_PATH", None)

from app import app
from extensions import db
from models import Household, Account, ExpenseTransaction, PlaidItem, PlaidTransaction, TransactionReconciliation
from services.financial_state import apply_balance_delta


def _sign(public_id: str) -> str:
    secret = os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"]
    return hmac.new(secret.encode("utf-8"), public_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(public_id: str) -> dict[str, str]:
    return {
        "X-Household-Id": public_id,
        "X-Household-Signature": _sign(public_id),
    }


def _create_scenario(prefix: str) -> tuple[str, int, str, int]:
    with app.app_context():
        suffix = uuid.uuid4().hex[:12]
        public_id = str(uuid.uuid4())
        h = Household(public_id=public_id, legacy_scope_key=f"{prefix}-{suffix}")
        db.session.add(h)
        db.session.flush()

        account = Account(household_id=h.id, checking_balance=1000.0)
        db.session.add(account)
        db.session.flush()

        plaid_tx_id = f"{prefix}-plaid-{suffix}"
        plaid_item_public = f"{prefix}-item-{suffix}"

        manual = ExpenseTransaction(
            household_id=h.id,
            description="manual tx",
            amount=25.0,
            category="discretionary",
            source="manual",
            local_account_id=account.id,
        )
        db.session.add(manual)
        apply_balance_delta(h.id, -25.0)
        db.session.flush()

        dup = ExpenseTransaction(
            household_id=h.id,
            description="plaid imported duplicate",
            amount=25.0,
            category="discretionary",
            source="plaid_import",
            plaid_transaction_id=plaid_tx_id,
            local_account_id=account.id,
        )
        db.session.add(dup)
        apply_balance_delta(h.id, -25.0)

        item = PlaidItem(
            household_id=h.id,
            owner_scope="anonymous",
            plaid_item_id=plaid_item_public,
            access_token_encrypted="enc",
        )
        db.session.add(item)
        db.session.flush()

        ptx = PlaidTransaction(
            household_id=h.id,
            owner_scope="anonymous",
            plaid_item_id=item.id,
            plaid_transaction_id=plaid_tx_id,
            plaid_account_id=f"acct-{suffix}",
            amount_cents=2500,
            signed_amount_cents=-2500,
            direction="outflow",
            name="WALMART",
            description="WALMART",
            transaction_date=date.today(),
        )
        db.session.add(ptx)

        rec = TransactionReconciliation(
            household_id=h.id,
            owner_scope="anonymous",
            manual_transaction_id=manual.id,
            plaid_transaction_id=plaid_tx_id,
            status="proposed",
            match_strength=95,
        )
        db.session.add(rec)
        db.session.commit()
        return public_id, int(manual.id), plaid_tx_id, int(h.id)


def _snapshot(household_id: int, plaid_tx_id: str) -> dict[str, object]:
    with app.app_context():
        account = Account.query.filter_by(household_id=household_id).first()
        recon_rows = TransactionReconciliation.query.filter_by(
            household_id=household_id,
            plaid_transaction_id=plaid_tx_id,
        ).all()
        status_counts: dict[str, int] = {}
        for row in recon_rows:
            key = str(row.status or "unknown")
            status_counts[key] = status_counts.get(key, 0) + 1
        return {
            "balance": round(float(account.checking_balance or 0.0), 2) if account else None,
            "discretionary_count": ExpenseTransaction.query.filter_by(household_id=household_id, category="discretionary").count(),
            "plaid_linked_count": ExpenseTransaction.query.filter_by(household_id=household_id, plaid_transaction_id=plaid_tx_id).count(),
            "reconciliation_count": len(recon_rows),
            "reconciliation_status_counts": status_counts,
        }


def _worker(public_id: str, manual_id: int, plaid_tx_id: str, queue) -> None:
    try:
        with app.test_client() as client:
            response = client.post(
                "/api/reconciliation/decision",
                headers=_headers(public_id),
                json={
                    "user_id": "anonymous",
                    "action": "match",
                    "manual_transaction_id": manual_id,
                    "plaid_transaction_id": plaid_tx_id,
                },
            )
            body = response.get_json() or {}
            queue.put(
                {
                    "status_code": int(response.status_code),
                    "result_status": ((body.get("result") or {}).get("status")),
                    "error": body.get("error"),
                }
            )
    except Exception as exc:
        queue.put({"status_code": 0, "result_status": None, "error": repr(exc)})


def _single_process_expected() -> dict[str, object]:
    public_id, manual_id, plaid_tx_id, hid = _create_scenario("gate6-single")
    with app.test_client() as client:
        response = client.post(
            "/api/reconciliation/decision",
            headers=_headers(public_id),
            json={
                "user_id": "anonymous",
                "action": "match",
                "manual_transaction_id": manual_id,
                "plaid_transaction_id": plaid_tx_id,
            },
        )
        body = response.get_json() or {}
    return {
        "status_code": int(response.status_code),
        "result_status": ((body.get("result") or {}).get("status")),
        "state": _snapshot(hid, plaid_tx_id),
    }


def _four_process_race() -> dict[str, object]:
    public_id, manual_id, plaid_tx_id, hid = _create_scenario("gate6-race")
    ctx = get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(public_id, manual_id, plaid_tx_id, queue)) for _ in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    responses = [queue.get(timeout=20) for _ in procs]
    return {
        "responses": responses,
        "state": _snapshot(hid, plaid_tx_id),
    }


if __name__ == "__main__":
    expected = _single_process_expected()
    raced = _four_process_race()

    if expected["status_code"] != 200:
        raise RuntimeError(f"single-process decision failed: {expected}")
    if expected["result_status"] not in {"matched", "already_matched"}:
        raise RuntimeError(f"unexpected single-process status: {expected}")

    codes = [int(item.get("status_code") or 0) for item in raced["responses"]]
    if not all(code == 200 for code in codes):
        raise RuntimeError(f"unexpected race status codes: {raced}")

    result_statuses = [item.get("result_status") for item in raced["responses"]]
    allowed_statuses = {"matched", "already_matched"}
    if not all(status in allowed_statuses for status in result_statuses):
        raise RuntimeError(f"unexpected race result statuses: {raced}")

    if raced["state"] != expected["state"]:
        raise RuntimeError(f"state mismatch expected={expected['state']} raced={raced['state']}")

    print(json.dumps({"expected": expected, "raced": raced}, sort_keys=True))
'''

    with tempfile.NamedTemporaryFile("w", suffix="_gate6_recon_race.py", delete=False, encoding="utf-8") as tmp:
        tmp.write(script)
        script_path = tmp.name

    try:
        completed = _run(
            ["/home/ky/finance_assistant/venv/bin/python", script_path],
            env={
                "DATABASE_URL": POSTGRES_URL,
                "RUNG_HOUSEHOLD_CONTEXT_SECRET": os.environ.get("RUNG_HOUSEHOLD_CONTEXT_SECRET", "gate6-test-secret"),
                "PLAID_ENABLED": "0",
                "PLAID_CLIENT_ID": "",
                "PLAID_SECRET": "",
            },
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        payload = json.loads((completed.stdout or "").strip().splitlines()[-1])

        expected_state = payload["expected"]["state"]
        raced_state = payload["raced"]["state"]
        assert raced_state == expected_state
        assert raced_state["balance"] == 975.0
        assert raced_state["discretionary_count"] == 1
        assert raced_state["plaid_linked_count"] == 1
        assert raced_state["reconciliation_count"] == 1
        assert raced_state["reconciliation_status_counts"].get("matched", 0) == 1
        assert all(int(item["status_code"]) == 200 for item in payload["raced"]["responses"])
        assert all(item.get("result_status") in {"matched", "already_matched"} for item in payload["raced"]["responses"])
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def test_gate6_production_sqlite_hash_protected_after() -> None:
    after = _sha256(PRODUCTION_SQLITE)
    SUMMARY.production_sha_after = after
    assert after == EXPECTED_PRODUCTION_SHA

    summary_path = Path("/tmp/rung_gate6_harness_summary.json")
    summary_path.write_text(
        json.dumps(
            {
                "production_sha_before": SUMMARY.production_sha_before,
                "production_sha_after": SUMMARY.production_sha_after,
                "idempotency_flood_trip_count": SUMMARY.idempotency_flood_trip_count,
                "idempotency_flood_tx_count": SUMMARY.idempotency_flood_tx_count,
                "idempotency_flood_balance": str(SUMMARY.idempotency_flood_balance),
                "lease_reacquire_seconds": round(SUMMARY.lease_reacquire_seconds, 3),
                "national_states_checked": SUMMARY.national_states_checked,
                "sqlite_backup_integrity_ok": SUMMARY.sqlite_backup_integrity_ok,
                "postgres_backup_restore_ok": SUMMARY.postgres_backup_restore_ok,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
