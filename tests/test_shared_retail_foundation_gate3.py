from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import Process, Queue

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

_DB_PATH = os.path.join(tempfile.gettempdir(), f"rung_gate3_{os.getpid()}.db")
os.environ["RUNG_DB_PATH"] = _DB_PATH

from app import app
from extensions import db
from models import (
    GroceryItem,
    Household,
    RetailProduct,
    RetailProductPreference,
    RetailRefreshLease,
    RetailSearchCache,
    RetailStoreIdentity,
    StorePriceCache,
    StoreProductObservation,
)
from services.retail import RetailProduct as ProviderRetailProduct
from services.retail import RetailStore
from services.retail.cart import VERIFIED_KROGER_STORE, VERIFIED_WALMART_STORE, build_verified_retail_cart
from services.retail.shared_foundation import (
    SharedRetailFreshnessPolicy,
    classify_availability_freshness,
    classify_price_freshness,
    normalize_query,
    shared_retail_foundation,
)
from services.household_context import household_id as current_household_id


class _FakeProvider:
    def __init__(self, *, retailer: str, price: float = 3.0, availability: str = "in_stock", fail: bool = False) -> None:
        self.retailer = retailer
        self.price = price
        self.availability = availability
        self.fail = fail
        self.search_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        self.search_calls += 1
        if self.fail:
            raise RuntimeError("provider failure")
        product = ProviderRetailProduct.now(
            requested_query=requirement.search_query(),
            retailer=self.retailer,
            store=store,
            product_id=f"{self.retailer}-sku-{requirement.base_item}",
            us_item_id=f"{self.retailer}-us-{requirement.base_item}",
            upc="000123",
            title=f"{requirement.base_item.title()} 1 ct",
            brand=self.retailer.title(),
            variant=None,
            package_size="1 ct",
            price=self.price,
            availability=self.availability,
            price_type="regular",
            product_url=None,
            source="kroger_api" if self.retailer == "kroger" else "serpapi_walmart",
            verified_location=True,
            fulfillment={"inStore": True, "curbside": True} if self.retailer == "kroger" else {"pickup": True},
            regular_price=self.price,
            promo_price=None,
        )
        from services.retail.base import ProductSearchResult

        return ProductSearchResult(store, store, [product], 1)

    def get_product(self, product_id, *, store, requested_query):
        raise AssertionError("detail not needed in Gate 3 tests")


def _reset_db() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _seed_manual_item(name: str = "milk") -> None:
    with app.app_context():
        db.session.add(GroceryItem(household_id=current_household_id(), item_name=name, store_name="Walmart"))
        db.session.commit()


def test_exact_store_isolation_for_same_sku_price() -> None:
    _reset_db()
    with app.app_context():
        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=RetailStore("44", "Walmart 44", "A", "11111", True),
            retailer_product_id="X",
            title="Milk",
            price=3.0,
            price_type="regular",
            price_source="serpapi_walmart",
            price_confidence="provider_confirmed",
            availability="in_stock",
            availability_source="serpapi_walmart",
            availability_confidence="provider_confirmed",
        )
        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=RetailStore("357", "Walmart 357", "B", "11111", True),
            retailer_product_id="X",
            title="Milk",
            price=4.0,
            price_type="regular",
            price_source="serpapi_walmart",
            price_confidence="provider_confirmed",
            availability="in_stock",
            availability_source="serpapi_walmart",
            availability_confidence="provider_confirmed",
        )
        a = shared_retail_foundation.observation_snapshot(retailer="walmart", retailer_store_id="44", retailer_product_id="X")
        b = shared_retail_foundation.observation_snapshot(retailer="walmart", retailer_store_id="357", retailer_product_id="X")
        assert a["price_cents"] == 300
        assert b["price_cents"] == 400


def test_retailer_isolation_for_same_product_id_string() -> None:
    _reset_db()
    with app.app_context():
        shared_retail_foundation.upsert_product(retailer="walmart", retailer_product_id="same-id", title="Walmart Milk")
        shared_retail_foundation.upsert_product(retailer="kroger", retailer_product_id="same-id", title="Kroger Milk")
        assert RetailProduct.query.filter_by(retailer_product_id="same-id").count() == 2
        assert RetailProduct.query.filter_by(retailer="walmart", retailer_product_id="same-id").count() == 1
        assert RetailProduct.query.filter_by(retailer="kroger", retailer_product_id="same-id").count() == 1


def test_shared_household_reuse_with_private_preference_separation() -> None:
    _reset_db()
    with app.app_context():
        h1 = Household(public_id="h1")
        h2 = Household(public_id="h2")
        db.session.add_all([h1, h2])
        db.session.commit()

        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=RetailStore("44", "Walmart 44", "A", "11111", True),
            retailer_product_id="X",
            title="Milk",
            price=3.0,
            price_source="serpapi_walmart",
            availability="in_stock",
            availability_source="serpapi_walmart",
        )
        db.session.add_all([
            RetailProductPreference(
                household_id=h1.id,
                base_item="milk",
                normalized_base_item="milk",
                preference_type="usual",
                preferred_product_title="Brand A",
                retailer="walmart",
                retailer_product_id="X",
                source="user_explicit",
            ),
            RetailProductPreference(
                household_id=h2.id,
                base_item="milk",
                normalized_base_item="milk",
                preference_type="usual",
                preferred_product_title="Brand B",
                retailer="walmart",
                retailer_product_id="X",
                source="user_explicit",
            ),
        ])
        db.session.commit()

        assert StoreProductObservation.query.count() == 1
        titles = sorted([row.preferred_product_title for row in RetailProductPreference.query.order_by(RetailProductPreference.id.asc()).all()])
        assert titles == ["Brand A", "Brand B"]


def test_price_and_availability_freshness_boundaries_are_independent() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert classify_price_freshness(now - timedelta(hours=24), now=now) == "FRESH"
    assert classify_price_freshness(now - timedelta(hours=25), now=now) == "RECENT"
    assert classify_price_freshness(now - timedelta(hours=73), now=now) == "STALE"
    assert classify_price_freshness(now - timedelta(days=8), now=now) == "OLD"

    assert classify_availability_freshness(now - timedelta(hours=2), now=now) == "FRESH"
    assert classify_availability_freshness(now - timedelta(hours=3), now=now) == "RECENT"
    assert classify_availability_freshness(now - timedelta(hours=13), now=now) == "STALE"
    assert classify_availability_freshness(now - timedelta(hours=25), now=now) == "OLD"


def test_partial_update_preserves_unrelated_fields() -> None:
    _reset_db()
    with app.app_context():
        store = RetailStore("44", "Walmart 44", "A", "11111", True)
        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=store,
            retailer_product_id="X",
            title="Milk",
            price=3.0,
            price_source="serpapi_walmart",
            price_confidence="provider_confirmed",
            availability="in_stock",
            availability_source="serpapi_walmart",
            availability_confidence="provider_confirmed",
        )
        first = shared_retail_foundation.observation_snapshot(retailer="walmart", retailer_store_id="44", retailer_product_id="X")

        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=store,
            retailer_product_id="X",
            title="Milk",
            price=3.5,
            price_source="serpapi_walmart",
            price_confidence="provider_confirmed",
        )
        second = shared_retail_foundation.observation_snapshot(retailer="walmart", retailer_store_id="44", retailer_product_id="X")
        assert second["price_cents"] == 350
        assert second["availability_status"] == first["availability_status"]

        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=store,
            retailer_product_id="X",
            title="Milk",
            availability="unavailable",
            availability_source="serpapi_walmart",
            availability_confidence="provider_confirmed",
        )
        third = shared_retail_foundation.observation_snapshot(retailer="walmart", retailer_store_id="44", retailer_product_id="X")
        assert third["availability_status"] == "unavailable"
        assert third["price_cents"] == 350


def test_search_normalization_and_store_scoping() -> None:
    _reset_db()
    with app.app_context():
        store_44 = RetailStore("44", "Walmart 44", "A", "11111", True)
        store_357 = RetailStore("357", "Walmart 357", "B", "11111", True)

        shared_retail_foundation.upsert_search_cache(
            retailer="walmart",
            store=store_44,
            query=" Milk ",
            retailer_product_ids=["X"],
            source="serpapi_walmart",
        )
        shared_retail_foundation.upsert_search_cache(
            retailer="walmart",
            store=store_44,
            query="MILK",
            retailer_product_ids=["X"],
            source="serpapi_walmart",
        )
        shared_retail_foundation.upsert_search_cache(
            retailer="walmart",
            store=store_357,
            query="milk",
            retailer_product_ids=["Y"],
            source="serpapi_walmart",
        )

        assert normalize_query(" Milk ") == "milk"
        assert RetailSearchCache.query.count() == 2


def test_single_flight_same_process_50_contenders() -> None:
    fd, db_path = tempfile.mkstemp(prefix="rung_gate3_sameproc_", suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE retail_refresh_lease (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_key VARCHAR(300) NOT NULL UNIQUE,
                lease_owner VARCHAR(120) NOT NULL,
                lease_until DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        ))

    resource = "search:walmart:44:milk"
    barrier = threading.Barrier(50)
    callback_count = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal callback_count
        owner = f"{threading.get_ident()}-{time.time_ns()}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        lease_until = now + timedelta(seconds=30)
        barrier.wait()
        acquired = False
        with engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE retail_refresh_lease
                    SET lease_owner = :owner, lease_until = :lease_until, updated_at = :now
                    WHERE resource_key = :resource_key AND lease_until <= :now
                    """
                ),
                {
                    "owner": owner,
                    "lease_until": lease_until,
                    "now": now,
                    "resource_key": resource,
                },
            ).rowcount
            acquired = updated > 0
            if not acquired:
                try:
                    conn.execute(
                        text(
                            """
                            INSERT INTO retail_refresh_lease
                                (resource_key, lease_owner, lease_until, created_at, updated_at)
                            VALUES
                                (:resource_key, :owner, :lease_until, :now, :now)
                            """
                        ),
                        {
                            "resource_key": resource,
                            "owner": owner,
                            "lease_until": lease_until,
                            "now": now,
                        },
                    )
                    acquired = True
                except IntegrityError:
                    acquired = False
        if acquired:
            with lock:
                callback_count += 1

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert callback_count == 1


def _postgres_contender_worker(db_url: str, resource_key: str, output: Queue) -> None:
    try:
        engine = create_engine(db_url)
        owner = f"{os.getpid()}-{time.time_ns()}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        lease_until = now + timedelta(seconds=2)

        acquired = False
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE retail_refresh_lease
                    SET lease_owner = :owner, lease_until = :lease_until, updated_at = :now
                    WHERE resource_key = :resource_key AND lease_until <= :now
                    """
                ),
                {
                    "owner": owner,
                    "lease_until": lease_until,
                    "now": now,
                    "resource_key": resource_key,
                },
            )
            acquired = result.rowcount > 0
            if not acquired:
                try:
                    conn.execute(
                        text(
                            """
                            INSERT INTO retail_refresh_lease
                                (resource_key, lease_owner, lease_until, created_at, updated_at)
                            VALUES
                                (:resource_key, :owner, :lease_until, :now, :now)
                            """
                        ),
                        {
                            "resource_key": resource_key,
                            "owner": owner,
                            "lease_until": lease_until,
                            "now": now,
                        },
                    )
                    acquired = True
                except IntegrityError:
                    acquired = False

        output.put(1 if acquired else 0)
        if acquired:
            time.sleep(0.6)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE retail_refresh_lease
                        SET lease_until = :now, updated_at = :now
                        WHERE resource_key = :resource_key AND lease_owner = :owner
                        """
                    ),
                    {"now": datetime.now(timezone.utc).replace(tzinfo=None), "resource_key": resource_key, "owner": owner},
                )
    except Exception as exc:
        output.put(f"ERR:{type(exc).__name__}:{exc}")


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_DATABASE_URL"), reason="POSTGRES_TEST_DATABASE_URL not set")
def test_single_flight_multi_process_postgres() -> None:
    db_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    resource = "search:walmart:44:milk"

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM retail_refresh_lease WHERE resource_key = :k"), {"k": resource})

    queue: Queue = Queue()
    procs = [Process(target=_postgres_contender_worker, args=(db_url, resource, queue)) for _ in range(10)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()

    results = []
    for _ in procs:
        try:
            results.append(queue.get(timeout=5))
        except queue.Empty:
            pytest.fail("Timed out waiting for contender result")

    assert all(value in {0, 1} for value in results), f"Unexpected contender results: {results}"
    assert sum(results) == 1


def test_lease_expiration_allows_reacquire() -> None:
    _reset_db()
    with app.app_context():
        resource = "product:walmart:44:X"
        first, owner = shared_retail_foundation.acquire_refresh_lease(resource_key=resource, lease_seconds=1)
        assert first is True
        time.sleep(1.2)
        second, _ = shared_retail_foundation.acquire_refresh_lease(resource_key=resource, lease_seconds=1)
        assert second is True
        shared_retail_foundation.release_refresh_lease(resource_key=resource, lease_owner=owner)


def test_provider_failure_while_refreshing_reuses_stale_cache() -> None:
    _reset_db()
    with app.app_context():
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="milk", store_name="Walmart"))
        payload = {
            "requirement": {"item_name": "milk", "base_item": "milk", "quantity": 1.0, "category": "General"},
            "selected_product": {
                "product_id": "walmart-sku-milk",
                "us_item_id": "walmart-us-milk",
                "title": "Milk 1 ct",
                "price": 3.0,
                "availability": "in_stock",
                "verified_location": True,
            },
            "alternatives": [],
            "candidates": [],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "selection_confidence": "high",
            "needs_user_choice": False,
            "selection_policy_version": 5,
        }
        from models import RetailProductCache

        db.session.add(RetailProductCache(
            retailer="walmart",
            store_id="357",
            store_name="Walmart",
            store_address="A",
            requested_query="milk",
            base_item="milk",
            product_id="walmart-sku-milk",
            us_item_id="walmart-us-milk",
            title="Milk 1 ct",
            package_size="1 ct",
            price=3.0,
            availability="in_stock",
            provider_source="serpapi_walmart",
            verified_location=True,
            response_json=json.dumps(payload),
            retrieved_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5),
        ))
        db.session.commit()

        cart = build_verified_retail_cart(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            provider=_FakeProvider(retailer="walmart", fail=True),
            force_refresh=True,
        )
        assert cart["cart_items"][0]["estimated_price"] == 3.0
        assert cart["cart_items"][0]["resolved"] is True


def test_cold_miss_plus_refresh_failure_does_not_fabricate_observation() -> None:
    _reset_db()
    _seed_manual_item("milk")
    with app.app_context():
        cart = build_verified_retail_cart(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            provider=_FakeProvider(retailer="walmart", fail=True),
            force_refresh=True,
        )
        assert cart["cart_items"][0]["resolved"] is False
        assert StoreProductObservation.query.count() == 0


def test_provider_dual_write_for_kroger_and_walmart() -> None:
    _reset_db()
    _seed_manual_item("milk")
    with app.app_context():
        build_verified_retail_cart(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            provider=_FakeProvider(retailer="walmart", price=3.0),
        )
        build_verified_retail_cart(
            retailer="kroger",
            store=VERIFIED_KROGER_STORE,
            provider=_FakeProvider(retailer="kroger", price=4.0, availability="unknown"),
        )

        assert RetailStoreIdentity.query.filter_by(retailer="walmart", retailer_store_id="357").count() == 1
        assert RetailStoreIdentity.query.filter_by(retailer="kroger", retailer_store_id="61500116").count() == 1
        assert RetailProduct.query.filter_by(retailer="walmart").count() >= 1
        assert RetailProduct.query.filter_by(retailer="kroger").count() >= 1
        assert StoreProductObservation.query.count() >= 2
        assert RetailSearchCache.query.count() >= 2


def test_legacy_cache_rows_are_not_promoted_to_shared_observation() -> None:
    _reset_db()
    with app.app_context():
        db.session.add(StorePriceCache(
            store_name="Walmart",
            item_keyword="milk",
            product_title="Manual Fake Milk",
            price=1.0,
            retailer="walmart",
        ))
        db.session.commit()
        assert StoreProductObservation.query.count() == 0


def test_store_switching_identity_remains_isolated_in_shared_layer() -> None:
    _reset_db()
    _seed_manual_item("milk")
    with app.app_context():
        build_verified_retail_cart(
            retailer="walmart",
            store=RetailStore("44", "Walmart 44", "A", "11111", True),
            provider=_FakeProvider(retailer="walmart", price=3.0),
            force_refresh=True,
        )
        build_verified_retail_cart(
            retailer="walmart",
            store=RetailStore("357", "Walmart 357", "B", "11111", True),
            provider=_FakeProvider(retailer="walmart", price=4.0),
            force_refresh=True,
        )
        s44 = shared_retail_foundation.observation_snapshot(
            retailer="walmart",
            retailer_store_id="44",
            retailer_product_id="walmart-sku-milk",
        )
        s357 = shared_retail_foundation.observation_snapshot(
            retailer="walmart",
            retailer_store_id="357",
            retailer_product_id="walmart-sku-milk",
        )
        assert s44["price_cents"] == 300
        assert s357["price_cents"] == 400
