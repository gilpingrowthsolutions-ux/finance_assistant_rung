from __future__ import annotations

import pytest
import os
import subprocess
import sys
import uuid
from sqlalchemy import create_engine, text

import app as appmod
from app import app
from extensions import db
from models import RetailProduct, RetailStoreIdentity, StoreProductObservation
from services.retail import RetailStore
from services.retail.shared_foundation import shared_retail_foundation
from scripts.ingest_store_prices import validate_database_contract


def test_sqlite_keeps_dialect_pool_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNG_DB_POOL_SIZE", raising=False)
    options = appmod._resolve_engine_options("sqlite:///:memory:")
    assert options == {"pool_pre_ping": True, "pool_recycle": 1800}


def test_postgres_pool_and_statement_timeout_are_bounded_and_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNG_DB_POOL_SIZE", "4")
    monkeypatch.setenv("RUNG_DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("RUNG_DB_POOL_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("RUNG_DB_POOL_RECYCLE_SECONDS", "900")
    monkeypatch.setenv("RUNG_DB_STATEMENT_TIMEOUT_MS", "20000")

    assert appmod._resolve_engine_options("postgresql://u:p@localhost/rung") == {
        "pool_pre_ping": True,
        "pool_recycle": 900,
        "pool_size": 4,
        "max_overflow": 2,
        "pool_timeout": 7,
        "connect_args": {"options": "-c statement_timeout=20000"},
    }


@pytest.mark.parametrize(
    ("name", "value"),
    (("RUNG_DB_POOL_SIZE", "0"), ("RUNG_DB_MAX_OVERFLOW", "-1"), ("RUNG_DB_STATEMENT_TIMEOUT_MS", "999")),
)
def test_invalid_postgres_pool_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        appmod._resolve_engine_options("postgresql://u:p@localhost/rung")


def test_hosted_ingest_rejects_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNG_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/not-hosted.db")
    with pytest.raises(RuntimeError, match="PostgreSQL is required"):
        validate_database_contract()


def test_local_ingest_keeps_disposable_sqlite_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNG_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/rung-disposable.db")
    validate_database_contract()


def test_store_product_observation_rolls_back_as_one_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        original_commit = db.session.commit

        def fail_final_commit() -> None:
            raise RuntimeError("simulated final write failure")

        monkeypatch.setattr(db.session, "commit", fail_final_commit)
        with pytest.raises(RuntimeError, match="simulated final write failure"):
            shared_retail_foundation.upsert_observation(
                retailer="walmart",
                store=RetailStore("atomic-44", "Walmart 44", "A", "11111", True),
                retailer_product_id="atomic-sku",
                title="Atomic Milk",
                price=3.25,
                price_source="test",
            )
        db.session.rollback()
        monkeypatch.setattr(db.session, "commit", original_commit)

        assert RetailStoreIdentity.query.filter_by(retailer_store_id="atomic-44").count() == 0
        assert RetailProduct.query.filter_by(retailer_product_id="atomic-sku").count() == 0
        assert StoreProductObservation.query.count() == 0


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_DATABASE_URL"), reason="POSTGRES_TEST_DATABASE_URL not set")
def test_postgres_concurrent_store_product_upsert_converges() -> None:
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    suffix = uuid.uuid4().hex
    worker_code = r'''
import os
from app import app
from services.retail import RetailStore
from services.retail.shared_foundation import shared_retail_foundation
with app.app_context():
    shared_retail_foundation.upsert_observation(
        retailer="walmart",
        store=RetailStore(os.environ["STORE_ID"], "Concurrent Store", "A", "11111", True),
        retailer_product_id=os.environ["PRODUCT_ID"],
        title="Concurrent Product",
        price=3.25,
        price_source="test",
    )
'''
    processes = []
    for _ in range(8):
        env = dict(os.environ)
        env.pop("RUNG_DB_PATH", None)
        env["DATABASE_URL"] = database_url
        env["STORE_ID"] = f"infra-store-{suffix}"
        env["PRODUCT_ID"] = f"infra-product-{suffix}"
        processes.append(subprocess.Popen(
            [sys.executable, "-c", worker_code], env=env, cwd="/home/ky/finance_assistant",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    for process in processes:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM retail_store_identity WHERE retailer_store_id=:value"
            ), {"value": f"infra-store-{suffix}"}).scalar_one() == 1
            assert connection.execute(text(
                "SELECT COUNT(*) FROM retail_product WHERE retailer_product_id=:value"
            ), {"value": f"infra-product-{suffix}"}).scalar_one() == 1
            assert connection.execute(text(
                "SELECT COUNT(*) FROM store_product_observation o "
                "JOIN retail_store_identity s ON s.id=o.retail_store_id "
                "JOIN retail_product p ON p.id=o.retail_product_id "
                "WHERE s.retailer_store_id=:store AND p.retailer_product_id=:product"
            ), {"store": f"infra-store-{suffix}", "product": f"infra-product-{suffix}"}).scalar_one() == 1
    finally:
        engine.dispose()
