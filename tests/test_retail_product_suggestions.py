from __future__ import annotations

import json
import os

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import GroceryItem, HouseholdShoppingDefault, RetailProductPreference, RetailProductSubstitution
from services.retail import ProductSearchResult, RetailProduct, ShoppingRequirement
from services.retail.cart import VERIFIED_WALMART_STORE, build_verified_walmart_cart
from services.household_context import household_id as current_household_id


class FakeProvider:
    def __init__(self, products: list[RetailProduct]) -> None:
        self.products = products
        self.search_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        self.search_calls += 1
        return ProductSearchResult(store, store, self.products, len(self.products))

    def get_product(self, product_id, *, store, requested_query):
        return self.products[0]


def _product(title: str, identity: str, price: float, *, availability: str = "in_stock") -> RetailProduct:
    return RetailProduct.now(
        requested_query="milk",
        retailer="walmart",
        store=VERIFIED_WALMART_STORE,
        product_id=f"product-{identity}",
        us_item_id=identity,
        upc=f"upc-{identity}",
        title=title,
        brand=None,
        variant=None,
        package_size="1 ct",
        price=price,
        availability=availability,
        price_type="unknown",
        product_url=f"https://www.walmart.com/ip/{identity}",
        source="serpapi_walmart",
        verified_location=True,
    )


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _seed_requirement(requirement: ShoppingRequirement) -> None:
    db.session.add(GroceryItem(
        household_id=current_household_id(),
        item_name=requirement.item_name,
        store_name="Walmart",
        shopping_requirement_json=json.dumps(requirement.__dict__),
    ))
    db.session.commit()


def _set_household_default(key: str, value: str) -> None:
    db.session.add(HouseholdShoppingDefault(
        household_id=current_household_id(),
        owner_scope="household:default",
        preference_kind="category_default",
        preference_key=key,
        preference_value=value,
    ))
    db.session.commit()


def _set_shopping_style(style: str) -> None:
    db.session.add(HouseholdShoppingDefault(
        household_id=current_household_id(),
        owner_scope="household:default",
        preference_kind="shopping_style",
        preference_key="shopping_style",
        preference_value=style,
    ))
    db.session.commit()


def test_suggested_selection_uses_shopping_style_save_most() -> None:
    _setup()
    provider = FakeProvider([
        _product("Brand Milk Gallon", "brand", 5.50),
        _product("Great Value Milk Gallon", "store", 3.99),
    ])
    with app.app_context():
        _seed_requirement(ShoppingRequirement("milk", "milk"))
        _set_shopping_style("save_most")

        item = build_verified_walmart_cart(provider=provider)["cart_items"][0]

        assert item["selected_product"]["us_item_id"] == "store"
        assert item["suggested"] is True
        assert item["needs_user_choice"] is False
        assert item["selection_confidence"] == "suggested"


def test_suggested_selection_honors_household_trait_before_style() -> None:
    _setup()
    provider = FakeProvider([
        _product("Great Value Whole Milk Gallon", "whole", 3.99),
        _product("Lactaid Lactose Free Milk Gallon", "lf", 6.49),
    ])
    with app.app_context():
        _seed_requirement(ShoppingRequirement("milk", "milk"))
        _set_household_default("milk_type", "lactose_free")
        _set_shopping_style("save_most")

        item = build_verified_walmart_cart(provider=provider)["cart_items"][0]

        assert item["selected_product"]["us_item_id"] == "lf"
        assert item["suggested"] is True
        assert item["suggestion_reason"] == "matched_household_default"


def test_soda_do_not_buy_default_blocks_auto_suggestion() -> None:
    _setup()
    provider = FakeProvider([
        _product("Coke Regular Soda 12 ct", "coke", 7.99),
        _product("Pepsi Soda 12 ct", "pepsi", 7.99),
    ])
    with app.app_context():
        _seed_requirement(ShoppingRequirement("soda", "soda"))
        _set_household_default("soda_preference", "dont_buy_soda")

        item = build_verified_walmart_cart(provider=provider)["cart_items"][0]

        assert item["selected_product"] is None
        assert item["needs_user_choice"] is True
        assert item["suggested"] is False


def test_explicit_request_with_no_match_stays_unresolved_not_suggested() -> None:
    _setup()
    provider = FakeProvider([
        _product("Jif Creamy Peanut Butter 16 oz", "jif", 3.99),
    ])
    with app.app_context():
        _seed_requirement(ShoppingRequirement(
            "Skippy crunchy peanut butter",
            "peanut butter",
            brand="Skippy",
            variant="crunchy",
        ))

        item = build_verified_walmart_cart(provider=provider)["cart_items"][0]

        assert item["selected_product"] is None
        assert item["needs_user_choice"] is False
        assert item["suggested"] is False


def test_usual_unavailable_without_approved_substitute_requires_choice_even_with_defaults() -> None:
    _setup()
    provider = FakeProvider([
        _product("Great Value Whole Milk Gallon", "usual", 3.99, availability="out_of_stock"),
        _product("Lactaid Lactose Free Milk Gallon", "lf", 6.49),
    ])
    with app.app_context():
        _seed_requirement(ShoppingRequirement("milk", "milk"))
        _set_household_default("milk_type", "lactose_free")
        db.session.add(RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk",
            normalized_base_item="milk",
            preference_type="usual",
            preferred_product_title="Great Value Whole Milk Gallon",
            retailer="walmart",
            retailer_us_item_id="usual",
            source="user_explicit",
        ))
        db.session.commit()

        item = build_verified_walmart_cart(provider=provider)["cart_items"][0]

        assert item["selected_product"] is None
        assert item["needs_user_choice"] is True
        assert item["usual_unavailable"] is True
        assert item["suggested"] is False


def test_suggested_selection_does_not_persist_preference_or_substitution_and_uses_cache() -> None:
    _setup()
    provider = FakeProvider([
        _product("Great Value Shampoo 12 oz", "store", 4.99),
        _product("Brand Shampoo 12 oz", "brand", 5.99),
    ])
    with app.app_context():
        _seed_requirement(ShoppingRequirement("shampoo", "shampoo"))
        first = build_verified_walmart_cart(provider=provider)
        second = build_verified_walmart_cart(provider=provider)

        assert first["cart_items"][0]["suggested"] is True
        assert second["resolution_stats"]["verified_cache_hits"] == 1
        assert provider.search_calls == 1
        assert RetailProductPreference.query.count() == 0
        assert RetailProductSubstitution.query.count() == 0
