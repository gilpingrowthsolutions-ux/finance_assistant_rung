from __future__ import annotations

import json
import os

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import GroceryItem, HouseholdShoppingDefault, RetailProductPreference
from services.retail import ProductSearchResult, RetailProduct
from services.retail.cart import VERIFIED_WALMART_STORE, build_verified_walmart_cart
from services.household_context import household_id as current_household_id


class FakeProvider:
    def __init__(self, products_by_item: dict[str, list[RetailProduct]]) -> None:
        self.products_by_item = products_by_item
        self.search_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        self.search_calls += 1
        key = str(requirement.base_item or "").strip().lower()
        products = self.products_by_item.get(key, [])
        return ProductSearchResult(store, store, products, len(products))

    def get_product(self, product_id, *, store, requested_query):
        # Test products include package metadata, so detail calls should not be needed.
        raise AssertionError("Unexpected product-detail lookup in budget optimizer tests")


def _product(base_item: str, title: str, identity: str, price: float) -> RetailProduct:
    return RetailProduct.now(
        requested_query=base_item,
        retailer="walmart",
        store=VERIFIED_WALMART_STORE,
        product_id=f"p-{identity}",
        us_item_id=identity,
        upc=f"u-{identity}",
        title=title,
        brand=None,
        variant=None,
        package_size="1 ct",
        price=price,
        availability="in_stock",
        price_type="unknown",
        product_url=f"https://www.walmart.com/ip/{identity}",
        source="serpapi_walmart",
        verified_location=True,
    )


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _seed_requirement(base_item: str, *, item_name: str | None = None, brand: str | None = None, variant: str | None = None) -> None:
    requirement = {
        "item_name": item_name or base_item,
        "base_item": base_item,
        "brand": brand,
        "variant": variant,
        "quantity": 1.0,
        "unit": None,
        "requested_package_size": None,
        "category": "General",
    }
    with app.app_context():
        db.session.add(GroceryItem(
            household_id=current_household_id(),
            item_name=item_name or base_item,
            store_name="Walmart",
            shopping_requirement_json=json.dumps(requirement),
        ))
        db.session.commit()


def _set_default(preference_key: str, preference_value: str) -> None:
    with app.app_context():
        db.session.add(HouseholdShoppingDefault(
            household_id=current_household_id(),
            owner_scope="household:default",
            preference_kind="category_default",
            preference_key=preference_key,
            preference_value=preference_value,
        ))
        db.session.commit()


def _set_style(style: str) -> None:
    with app.app_context():
        db.session.add(HouseholdShoppingDefault(
            household_id=current_household_id(),
            owner_scope="household:default",
            preference_kind="shopping_style",
            preference_key="shopping_style",
            preference_value=style,
        ))
        db.session.commit()


def _by_keyword(cart: dict) -> dict[str, dict]:
    return {str(item.get("keyword") or ""): item for item in cart.get("cart_items", [])}


def test_under_budget_cart_stays_unchanged() -> None:
    _setup()
    _seed_requirement("bread")
    _set_style("prefer_brands_when_possible")
    provider = FakeProvider({
        "bread": [
            _product("bread", "Wonder Bread White", "brand", 4.00),
            _product("bread", "Great Value White Bread", "store", 3.00),
        ],
    })

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=10.00, tax_rate=0.0)

    item = cart["cart_items"][0]
    assert item["selected_product"]["us_item_id"] == "brand"
    assert cart["budget_optimization"]["status"] == "within_budget"
    assert cart["budget_optimization"]["applied"] is False


def test_over_budget_uses_whole_cart_and_prefers_lower_penalty_combo() -> None:
    _setup()
    _seed_requirement("milk")
    _seed_requirement("bread")
    _seed_requirement("detergent")
    _set_style("prefer_brands_when_possible")
    _set_default("milk_type", "whole")
    _set_default("bread_type", "dont_care")
    _set_default("laundry_detergent_scent", "dont_care")

    provider = FakeProvider({
        "milk": [
            _product("milk", "Brand Whole Milk", "milk-brand", 10.00),
            _product("milk", "Great Value Skim Milk", "milk-cheap", 5.00),
        ],
        "bread": [
            _product("bread", "Wonder Bread White", "bread-brand", 8.00),
            _product("bread", "Great Value White Bread", "bread-cheap", 5.00),
        ],
        "detergent": [
            _product("detergent", "Tide Original Detergent", "soap-brand", 7.00),
            _product("detergent", "Great Value Original Detergent", "soap-cheap", 5.00),
        ],
    })

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=20.00, tax_rate=0.0)

    by_keyword = _by_keyword(cart)
    assert by_keyword["milk"]["selected_product"]["us_item_id"] == "milk-brand"
    assert by_keyword["bread"]["selected_product"]["us_item_id"] == "bread-cheap"
    assert by_keyword["detergent"]["selected_product"]["us_item_id"] == "soap-cheap"
    assert cart["budget_optimization"]["status"] == "optimized_within_budget"
    assert cart["budget_optimization"]["lines_changed"] == 2


def test_explicit_current_request_is_protected_from_budget_downgrade() -> None:
    _setup()
    _seed_requirement("peanut butter", item_name="Skippy crunchy peanut butter", brand="Skippy", variant="crunchy")
    provider = FakeProvider({
        "peanut butter": [
            _product("peanut butter", "Skippy Crunchy Peanut Butter 16 oz", "skippy", 8.00),
            _product("peanut butter", "Great Value Crunchy Peanut Butter 16 oz", "store", 3.00),
        ],
    })

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=4.00, tax_rate=0.0)

    item = cart["cart_items"][0]
    assert item["selected_product"]["us_item_id"] == "skippy"
    assert cart["budget_optimization"]["status"] == "over_budget_no_flexible_lines"


def test_exact_usual_preference_is_not_replaced_for_budget() -> None:
    _setup()
    _seed_requirement("milk")
    provider = FakeProvider({
        "milk": [
            _product("milk", "Brand Whole Milk", "usual", 8.00),
            _product("milk", "Great Value Whole Milk", "cheap", 3.00),
        ],
    })

    with app.app_context():
        db.session.add(RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk",
            normalized_base_item="milk",
            preference_type="usual",
            preferred_product_title="Brand Whole Milk",
            retailer="walmart",
            retailer_us_item_id="usual",
            source="user_explicit",
        ))
        db.session.commit()
        cart = build_verified_walmart_cart(provider=provider, budget_limit=4.00, tax_rate=0.0)

    item = cart["cart_items"][0]
    assert item["preferred_product"] is True
    assert item["selected_product"]["us_item_id"] == "usual"
    assert cart["budget_optimization"]["status"] == "over_budget_no_flexible_lines"


def test_dont_care_is_more_flexible_than_unanswered_default() -> None:
    _setup()
    _seed_requirement("bread")
    _seed_requirement("milk")
    _set_style("prefer_brands_when_possible")
    _set_default("bread_type", "dont_care")

    provider = FakeProvider({
        "bread": [
            _product("bread", "Wonder Bread White", "bread-brand", 6.00),
            _product("bread", "Great Value White Bread", "bread-cheap", 4.00),
        ],
        "milk": [
            _product("milk", "Brand Whole Milk", "milk-brand", 6.00),
            _product("milk", "Great Value Whole Milk", "milk-cheap", 4.00),
        ],
    })

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=10.00, tax_rate=0.0)

    by_keyword = _by_keyword(cart)
    assert by_keyword["bread"]["selected_product"]["us_item_id"] == "bread-cheap"
    assert by_keyword["milk"]["selected_product"]["us_item_id"] == "milk-brand"


def test_impossible_budget_reports_truthfully_without_quantity_reduction() -> None:
    _setup()
    _seed_requirement("bread")
    _set_style("save_most")
    provider = FakeProvider({
        "bread": [
            _product("bread", "Great Value White Bread", "only", 5.00),
        ],
    })

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=2.00, tax_rate=0.0)

    item = cart["cart_items"][0]
    assert item["packages_to_buy"] == 1
    assert cart["budget_optimization"]["status"] in {
        "over_budget_no_flexible_lines",
        "over_budget_no_feasible_combination",
    }
    assert cart["budget_optimization"]["optimized_total_cents"] > cart["budget_optimization"]["budget_cents"]


def test_optimizer_is_deterministic_for_tie_case() -> None:
    _setup()
    _seed_requirement("bread")
    _seed_requirement("detergent")
    _set_style("prefer_brands_when_possible")
    _set_default("bread_type", "dont_care")
    _set_default("laundry_detergent_scent", "dont_care")

    provider = FakeProvider({
        "bread": [
            _product("bread", "Wonder Bread White", "bread-brand", 6.00),
            _product("bread", "Great Value White Bread", "bread-cheap", 4.00),
        ],
        "detergent": [
            _product("detergent", "Tide Original Detergent", "soap-brand", 6.00),
            _product("detergent", "Great Value Original Detergent", "soap-cheap", 4.00),
        ],
    })

    with app.app_context():
        first = build_verified_walmart_cart(provider=provider, budget_limit=10.00, tax_rate=0.0)
        second = build_verified_walmart_cart(provider=provider, budget_limit=10.00, tax_rate=0.0)

    first_ids = [item["selected_product"]["us_item_id"] for item in first["cart_items"]]
    second_ids = [item["selected_product"]["us_item_id"] for item in second["cart_items"]]
    assert first_ids == second_ids


def test_optimizer_uses_cent_accurate_math() -> None:
    _setup()
    _seed_requirement("bread")
    _set_style("prefer_brands_when_possible")
    provider = FakeProvider({
        "bread": [
            _product("bread", "Brand Bread", "brand", 5.03),
            _product("bread", "Great Value Bread", "cheap", 4.99),
        ],
    })

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=5.00, tax_rate=0.0)

    item = cart["cart_items"][0]
    assert item["selected_product"]["us_item_id"] == "cheap"
    assert abs(float(cart["subtotal"]) - 4.99) < 1e-9
