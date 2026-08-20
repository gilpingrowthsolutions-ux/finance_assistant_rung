from __future__ import annotations

import json
import os

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import GroceryItem, RetailProductCache, RetailProductPreference, RetailProductSubstitution
from services.retail import ProductSearchResult, RetailProduct, RetailStore, ShoppingRequirement
from services.retail.cart import VERIFIED_KROGER_STORE, VERIFIED_WALMART_STORE, build_verified_retail_cart
from services.retail.preferences import (
    forget_product_preference,
    get_product_preference,
    match_approved_substitution,
    match_preference,
)
from services.household_context import household_id as current_household_id


class FakeProvider:
    def __init__(self) -> None:
        self.search_calls = 0
        self.detail_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        self.search_calls += 1
        title = requirement.base_item.title()
        product = RetailProduct.now(
            requested_query=requirement.search_query(),
            retailer="kroger",
            store=store,
            product_id=f"k-{requirement.base_item}",
            us_item_id=f"u-{requirement.base_item}",
            upc=f"000000000{self.search_calls:03d}",
            title=f"Kroger {title} 1 ct",
            brand="Kroger",
            variant=None,
            package_size="1 ct",
            price=2.50,
            availability="unknown",
            price_type="unknown",
            product_url=None,
            source="kroger_api",
            verified_location=True,
            fulfillment={"inStore": True, "curbside": True},
            regular_price=2.75,
            promo_price=2.50,
        )
        return ProductSearchResult(store, store, [product], 1)

    def get_product(self, product_id, *, store, requested_query):
        self.detail_calls += 1
        raise AssertionError("test products already include package data")


def setup_function():
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="milk", shopping_requirement_json=json.dumps({"item_name": "milk", "base_item": "milk"})))
        db.session.commit()


def test_kroger_cart_reuses_verified_cache_and_preserves_fulfillment():
    provider = FakeProvider()
    with app.app_context():
        first = build_verified_retail_cart(retailer="kroger", store=VERIFIED_KROGER_STORE, provider=provider)
        second = build_verified_retail_cart(retailer="kroger", store=VERIFIED_KROGER_STORE, provider=provider)

        item = first["cart_items"][0]
        assert item["store_id"] == "61500116"
        assert item["confirmed_local_store"] is True
        assert item["availability"] == "unknown"
        assert item["fulfillment"] == {"inStore": True, "curbside": True}
        assert item["regular_price"] == 2.75
        assert item["promo_price"] == 2.50
        assert first["resolution_stats"]["search_calls"] == 1
        assert second["resolution_stats"]["verified_cache_hits"] == 1
        assert provider.search_calls == 1


def test_retailer_and_store_cache_identity_do_not_collide():
    with app.app_context():
        db.session.add(RetailProductCache(
            retailer="walmart", store_id="357", store_name="Walmart", store_address="x",
            requested_query="milk", base_item="milk", title="Great Value Milk",
            provider_source="serpapi_walmart", response_json="{}", verified_location=True,
        ))
        db.session.commit()
        provider = FakeProvider()
        build_verified_retail_cart(retailer="kroger", store=VERIFIED_KROGER_STORE, provider=provider)
        assert provider.search_calls == 1
        assert RetailProductCache.query.filter_by(retailer="kroger", store_id="61500116", requested_query="milk").count() == 1
        assert RetailProductCache.query.filter_by(retailer="walmart", store_id="357", requested_query="milk").count() == 1


def test_walmart_preference_does_not_match_retailer_specific_kroger_product():
    with app.app_context():
        preference = RetailProductPreference(
                household_id=current_household_id(), base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title="Great Value Whole Milk", retailer="walmart",
            retailer_product_id="w-1",
        )
        db.session.add(preference)
        db.session.commit()
        candidate = {"title": "Kroger Whole Milk", "product_id": "k-1", "upc": "different"}
        assert match_preference(preference, [candidate], retailer="kroger") is None


def test_walmart_substitution_does_not_carry_to_kroger_without_shared_upc():
    with app.app_context():
        preference = RetailProductPreference(
                household_id=current_household_id(), base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title="Great Value Whole Milk", retailer="walmart",
        )
        db.session.add(preference)
        db.session.flush()
        substitution = RetailProductSubstitution(
                household_id=current_household_id(), base_item="milk", normalized_base_item="milk", preferred_preference_id=preference.id,
            substitute_product_title="Horizon Whole Milk", retailer="walmart",
            retailer_product_id="w-substitute", approval_type="explicit",
        )
        db.session.add(substitution)
        db.session.commit()
        candidate = {"title": "Horizon Whole Milk", "product_id": "k-product", "upc": "different"}
        assert match_approved_substitution([substitution], [candidate], retailer="kroger") == (None, None)


def _seed_verified_cache(*, retailer: str, store_id: str, store_name: str, requested_query: str, base_item: str, candidates: list[dict]) -> None:
    payload = {
        "requirement": {"item_name": base_item, "base_item": base_item},
        "selected_product": None,
        "alternatives": candidates,
        "candidates": candidates,
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "selection_confidence": "low",
        "needs_user_choice": True,
        "selection_policy_version": 5,
    }
    db.session.add(RetailProductCache(
        retailer=retailer,
        store_id=store_id,
        store_name=store_name,
        store_address="x",
        requested_query=requested_query,
        base_item=base_item,
        title=f"Unresolved: {requested_query}",
        provider_source="serpapi_walmart" if retailer == "walmart" else "kroger_api",
        response_json=json.dumps(payload),
        verified_location=True,
    ))


def test_walmart_and_kroger_usuals_coexist_and_each_cart_uses_its_own_preference():
    with app.app_context():
        walmart_pref = RetailProductPreference(
                household_id=current_household_id(), base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title="Great Value Whole Milk", retailer="walmart",
            upc="111", retailer_product_id="w-1", retailer_us_item_id="w-us-1", source="user_explicit",
        )
        kroger_pref = RetailProductPreference(
                household_id=current_household_id(), base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title="Kroger Whole Milk", retailer="kroger",
            upc="222", retailer_product_id="k-1", retailer_us_item_id="k-us-1", source="user_explicit",
        )
        db.session.add_all([walmart_pref, kroger_pref])
        db.session.commit()
        assert RetailProductPreference.query.filter_by(normalized_base_item="milk", preference_type="usual").count() == 2

        _seed_verified_cache(
            retailer="walmart",
            store_id=VERIFIED_WALMART_STORE.store_id,
            store_name=VERIFIED_WALMART_STORE.name,
            requested_query="milk",
            base_item="milk",
            candidates=[
                {"title": "Great Value Whole Milk", "upc": "111", "product_id": "w-1", "us_item_id": "w-us-1", "availability": "in_stock", "verified_location": True, "price": 4.11},
                {"title": "Other Walmart Milk", "upc": "333", "product_id": "w-2", "us_item_id": "w-us-2", "availability": "in_stock", "verified_location": True, "price": 4.55},
            ],
        )
        _seed_verified_cache(
            retailer="kroger",
            store_id=VERIFIED_KROGER_STORE.store_id,
            store_name=VERIFIED_KROGER_STORE.name,
            requested_query="milk",
            base_item="milk",
            candidates=[
                {"title": "Kroger Whole Milk", "upc": "222", "product_id": "k-1", "us_item_id": "k-us-1", "availability": "in_stock", "verified_location": True, "price": 3.49},
                {"title": "Other Kroger Milk", "upc": "444", "product_id": "k-2", "us_item_id": "k-us-2", "availability": "in_stock", "verified_location": True, "price": 3.99},
            ],
        )
        db.session.commit()

        walmart_cart = build_verified_retail_cart(retailer="walmart", store=VERIFIED_WALMART_STORE, provider=FakeProvider())
        kroger_cart = build_verified_retail_cart(retailer="kroger", store=VERIFIED_KROGER_STORE, provider=FakeProvider())

        assert walmart_cart["cart_items"][0]["selected_product"]["us_item_id"] == "w-us-1"
        assert walmart_cart["cart_items"][0]["preference"]["retailer"] == "walmart"
        assert kroger_cart["cart_items"][0]["selected_product"]["us_item_id"] == "k-us-1"
        assert kroger_cart["cart_items"][0]["preference"]["retailer"] == "kroger"


def test_cross_retailer_fallback_requires_shared_upc():
    with app.app_context():
        walmart_preference = RetailProductPreference(
                household_id=current_household_id(), base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title="Great Value Whole Milk", retailer="walmart",
            upc="shared-upc", retailer_product_id="w-only", retailer_us_item_id="w-only-us", source="user_explicit",
        )
        db.session.add(walmart_preference)
        db.session.commit()

        chosen = get_product_preference("milk", retailer="kroger")
        assert chosen is not None
        assert chosen.id == walmart_preference.id
        match = match_preference(chosen, [{
            "title": "Kroger Whole Milk",
            "upc": "shared-upc",
            "product_id": "k-only",
            "us_item_id": "k-only-us",
            "availability": "in_stock",
        }], retailer="kroger")
        assert match is not None
        assert match["upc"] == "shared-upc"


def test_update_and_delete_are_retailer_scoped():
    with app.app_context():
        walmart_preference = RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title="Great Value Whole Milk", retailer="walmart", source="user_explicit",
        )
        kroger_preference = RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title="Kroger Whole Milk", retailer="kroger", source="user_explicit",
        )
        db.session.add_all([walmart_preference, kroger_preference])
        db.session.commit()

        walmart_preference.preferred_product_title = "Great Value Whole Milk 2%"
        db.session.commit()
        reloaded_kroger = RetailProductPreference.query.filter_by(
            normalized_base_item="milk", preference_type="usual", retailer="kroger"
        ).first()
        assert reloaded_kroger is not None
        assert reloaded_kroger.preferred_product_title == "Kroger Whole Milk"

        deleted = forget_product_preference("milk", "usual", retailer="walmart")
        assert deleted == 1
        assert RetailProductPreference.query.filter_by(
            normalized_base_item="milk", preference_type="usual", retailer="walmart"
        ).count() == 0
        assert RetailProductPreference.query.filter_by(
            normalized_base_item="milk", preference_type="usual", retailer="kroger"
        ).count() == 1
