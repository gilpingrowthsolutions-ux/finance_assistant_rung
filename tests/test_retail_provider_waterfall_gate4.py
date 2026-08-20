from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import GroceryItem, RetailProductPreference, UsageEvent
from services.household_context import household_id as current_household_id
from services.retail import RetailProduct, RetailStore
from services.retail.cart import VERIFIED_WALMART_STORE, build_verified_retail_cart
from services.retail.shared_foundation import shared_retail_foundation
from services.retail.walmart_serpapi import WalmartSerpApiProvider
from services.usage_meter import set_usage_controls


class _NoCallProvider(WalmartSerpApiProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    def search_products(self, requirement, *, store, limit=20):
        raise AssertionError("search_products should not be called in this scenario")

    def get_product(self, product_id, *, store, requested_query):
        raise AssertionError("get_product should not be called in this scenario")


class _DetailProvider(WalmartSerpApiProvider):
    def __init__(self, *, price: float = 5.25) -> None:
        super().__init__(api_key="test")
        self.price = price
        self.detail_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        raise AssertionError("search_products not used in exact-SKU tests")

    def get_product(self, product_id, *, store, requested_query):
        self.detail_calls += 1
        return RetailProduct.now(
            requested_query=requested_query,
            retailer="walmart",
            store=store,
            product_id=str(product_id),
            us_item_id=str(product_id),
            upc="000111",
            title="Laundry Detergent 1 ct",
            brand="Brand",
            variant=None,
            package_size="1 ct",
            price=self.price,
            availability="in_stock",
            price_type="regular",
            product_url=None,
            source="serpapi_walmart",
            verified_location=True,
        )


def _reset() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _seed_manual_detergent() -> None:
    with app.app_context():
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="Laundry Detergent", store_name="Walmart"))
        db.session.commit()


def _seed_exact_usual(product_id: str = "sku-detergent") -> None:
    with app.app_context():
        db.session.add(
            RetailProductPreference(
                household_id=current_household_id(),
                base_item="Laundry Detergent",
                normalized_base_item="laundry detergent",
                preference_type="usual",
                preferred_product_title="Laundry Detergent 1 ct",
                retailer="walmart",
                retailer_product_id=product_id,
                retailer_us_item_id=product_id,
                source="user_explicit",
            )
        )
        db.session.commit()


def _seed_observation(*, price_hours_ago: float, availability_hours_ago: float, product_id: str = "sku-detergent") -> None:
    now = datetime.now(timezone.utc)
    price_observed = (now - timedelta(hours=price_hours_ago)).isoformat()
    availability_observed = (now - timedelta(hours=availability_hours_ago)).isoformat()
    with app.app_context():
        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            retailer_product_id=product_id,
            title="Laundry Detergent 1 ct",
            price=4.99,
            price_type="regular",
            price_source="serpapi_walmart",
            price_confidence="provider_confirmed",
            observed_at=price_observed,
        )
        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            retailer_product_id=product_id,
            title="Laundry Detergent 1 ct",
            availability="in_stock",
            availability_source="serpapi_walmart",
            availability_confidence="provider_confirmed",
            observed_at=availability_observed,
        )


def _set_serpapi_controls(*, enabled: bool, daily: int | None, monthly: int | None, retail_live_enabled: bool = True) -> None:
    with app.app_context():
        set_usage_controls(
            {
                "kill_switches": {
                    "retail_live_refresh_enabled": retail_live_enabled,
                    "serpapi_fallback_enabled": enabled,
                },
                "provider_limits": {
                    "serpapi_calls_per_day": daily,
                    "serpapi_calls_per_month": monthly,
                    "retail_external_calls_per_day": 10_000,
                },
            }
        )


def _serpapi_external_calls() -> int:
    with app.app_context():
        return UsageEvent.query.filter_by(
            category="retail_provider",
            provider="serpapi_walmart",
            operation="product_detail",
            external_call=True,
            success=True,
        ).count()


def test_walmart_recent_exact_sku_reuses_shared_without_serpapi_call() -> None:
    _reset()
    _seed_manual_detergent()
    _seed_exact_usual()
    _seed_observation(price_hours_ago=48, availability_hours_ago=1)
    _set_serpapi_controls(enabled=False, daily=None, monthly=None)

    with app.app_context():
        cart = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE, provider=_NoCallProvider())

    item = cart["cart_items"][0]
    assert item["resolved"] is True
    assert item["data_quality"] == "RECENT_CONFIRMED"
    assert item["price_freshness"] == "RECENT"
    assert cart["resolution_stats"]["product_detail_calls"] == 0
    assert _serpapi_external_calls() == 0


def test_walmart_stale_exact_sku_refreshes_once_then_reuses_shared_cache() -> None:
    _reset()
    _seed_manual_detergent()
    _seed_exact_usual()
    _seed_observation(price_hours_ago=96, availability_hours_ago=1)
    _set_serpapi_controls(enabled=True, daily=50, monthly=500)
    provider = _DetailProvider(price=6.11)

    with app.app_context():
        first = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE, provider=provider)
        second = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE, provider=provider)

    assert provider.detail_calls == 1
    assert first["resolution_stats"]["product_detail_calls"] == 1
    assert second["resolution_stats"]["product_detail_calls"] == 0
    assert _serpapi_external_calls() == 1


def test_walmart_stale_exact_sku_fallback_disabled_returns_last_known() -> None:
    _reset()
    _seed_manual_detergent()
    _seed_exact_usual()
    _seed_observation(price_hours_ago=96, availability_hours_ago=1)
    _set_serpapi_controls(enabled=False, daily=50, monthly=500)

    with app.app_context():
        cart = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE)

    item = cart["cart_items"][0]
    assert item["resolved"] is True
    assert item["data_quality"] == "LAST_KNOWN"
    assert item["price_freshness"] == "STALE"
    assert _serpapi_external_calls() == 0


def test_walmart_old_exact_sku_hard_cap_exhausted_degrades_without_call() -> None:
    _reset()
    _seed_manual_detergent()
    _seed_exact_usual()
    _seed_observation(price_hours_ago=24 * 9, availability_hours_ago=1)
    _set_serpapi_controls(enabled=True, daily=0, monthly=0)

    with app.app_context():
        cart = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE)

    item = cart["cart_items"][0]
    assert item["resolved"] is True
    assert item["data_quality"] in {"ESTIMATE", "LAST_KNOWN"}
    assert item["confirmed_local_store"] is False
    assert _serpapi_external_calls() == 0


def test_walmart_unconfigured_fallback_is_fail_closed() -> None:
    _reset()
    _seed_manual_detergent()
    _seed_exact_usual()
    _seed_observation(price_hours_ago=96, availability_hours_ago=1)
    _set_serpapi_controls(enabled=True, daily=None, monthly=None)

    with app.app_context():
        cart = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE)

    item = cart["cart_items"][0]
    assert item["resolved"] is True
    assert item["data_quality"] == "LAST_KNOWN"
    assert _serpapi_external_calls() == 0


def test_walmart_availability_only_staleness_does_not_trigger_paid_refresh() -> None:
    _reset()
    _seed_manual_detergent()
    _seed_exact_usual()
    _seed_observation(price_hours_ago=1, availability_hours_ago=30)
    _set_serpapi_controls(enabled=True, daily=50, monthly=500)

    with app.app_context():
        cart = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE, provider=_NoCallProvider())

    item = cart["cart_items"][0]
    assert item["resolved"] is True
    assert item["price_freshness"] == "FRESH"
    assert item["availability_freshness"] == "OLD"
    assert _serpapi_external_calls() == 0
