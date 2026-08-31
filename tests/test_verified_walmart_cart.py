from __future__ import annotations

import json
import os
from unittest.mock import patch

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import Account, GroceryItem, RetailProductCache, StorePriceCache
from services.retail import ProductSearchResult, RetailProduct, ShoppingRequirement
from services.retail.cart import VERIFIED_WALMART_STORE, _active_manual_requirements, build_verified_walmart_cart
from services.household_context import household_id as current_household_id


class FakeProvider:
    def __init__(self, products: list[RetailProduct]) -> None:
        self.products = products
        self.search_calls = 0
        self.detail_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        self.search_calls += 1
        return ProductSearchResult(store, store, self.products, len(self.products))

    def get_product(self, product_id, *, store, requested_query):
        self.detail_calls += 1
        return self.products[0]


def _product(title: str, product_id: str, price: float, package: str | None = "12 oz") -> RetailProduct:
    return RetailProduct.now(
        requested_query="shampoo",
        retailer="walmart",
        store=VERIFIED_WALMART_STORE,
        product_id=product_id,
        us_item_id=product_id,
        upc=None,
        title=title,
        brand=None,
        variant=None,
        package_size=package,
        price=price,
        availability="in_stock",
        price_type="unknown",
        product_url=f"https://www.walmart.com/ip/{product_id}",
        source="serpapi_walmart",
        verified_location=True,
    )


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def test_verified_cart_uses_live_provider_then_verified_cache() -> None:
    _setup()
    provider = FakeProvider([
        _product("Selected Shampoo 12 oz", "1", 4.5),
        _product("Alternative Shampoo 16 oz", "2", 5.0, "16 oz"),
    ])
    with app.app_context():
        requirement = ShoppingRequirement("shampoo", "shampoo")
        db.session.add(GroceryItem(
            household_id=current_household_id(),
            item_name="Shampoo",
            store_name="Walmart",
            shopping_requirement_json=json.dumps(requirement.__dict__),
        ))
        db.session.commit()

        first = build_verified_walmart_cart(provider=provider)
        second = build_verified_walmart_cart(provider=provider)

        assert first["resolution_stats"] == {
            "total_terms": 1,
            "search_calls": 1,
            "product_detail_calls": 0,
            "verified_cache_hits": 0,
            "unresolved": 0,
        }
        item = first["cart_items"][0]
        assert item["selected_product"] is not None
        assert item["selected_product"]["product_id"] == "1"
        assert [row["product_id"] for row in item["alternatives"]] == ["2"]
        assert item["selection_confidence"] == "suggested"
        assert item["needs_user_choice"] is False
        assert item["suggested"] is True
        assert item["confirmed_local_store"] is True
        assert item["store_id"] == "357"
        assert item["estimated_price"] == 4.5
        assert second["resolution_stats"]["verified_cache_hits"] == 1
        assert provider.search_calls == 1
        assert RetailProductCache.query.count() == 1


def test_verified_cart_ignores_legacy_store_price_cache() -> None:
    _setup()
    provider = FakeProvider([])
    with app.app_context():
        db.session.add(StorePriceCache(
            store_name="Walmart",
            item_keyword="milk",
            product_title="Fake Milk",
            price=1.0,
            retailer="walmart",
        ))
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="Milk", store_name="Walmart"))
        db.session.commit()

        cart = build_verified_walmart_cart(provider=provider)

        item = cart["cart_items"][0]
        assert item["resolved"] is False
        assert item["estimated_price"] is None
        assert item["confirmed_local_store"] is False
        assert cart["subtotal"] == 0.0
        assert StorePriceCache.query.count() == 1


def test_manual_quantity_is_package_count_and_duplicates_are_deduplicated() -> None:
    _setup()
    provider = FakeProvider([_product("Shampoo Bottle 12 oz", "1", 4.0)])
    with app.app_context():
        requirement = ShoppingRequirement("shampoo", "shampoo", quantity=2, unit="bottle")
        for _ in range(2):
            db.session.add(GroceryItem(
                household_id=current_household_id(),
                item_name="Shampoo",
                store_name="Walmart",
                shopping_requirement_json=json.dumps(requirement.__dict__),
            ))
        db.session.commit()

        cart = build_verified_walmart_cart(provider=provider)

        assert len(cart["cart_items"]) == 1
        assert cart["cart_items"][0]["packages_to_buy"] == 2
        assert cart["cart_items"][0]["estimated_price"] == 8.0
        assert "net_quantity_needed_oz" not in cart["cart_items"][0]


def test_force_refresh_bypasses_verified_cache() -> None:
    _setup()
    provider = FakeProvider([_product("Shampoo Bottle 12 oz", "1", 4.0)])
    with app.app_context():
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="Shampoo", store_name="Walmart"))
        db.session.commit()
        build_verified_walmart_cart(provider=provider)
        refreshed = build_verified_walmart_cart(provider=provider, force_refresh=True)
        assert refreshed["resolution_stats"]["search_calls"] == 1
        assert refreshed["resolution_stats"]["verified_cache_hits"] == 0
        assert provider.search_calls == 2


def test_walmart_cart_endpoint_uses_verified_provider_contract() -> None:
    _setup()
    provider = FakeProvider([
        _product("Selected Shampoo 12 oz", "1", 4.5),
        _product("Alternative Shampoo 16 oz", "2", 5.0, "16 oz"),
    ])
    with app.app_context():
        db.session.add(Account(household_id=current_household_id(), checking_balance=1000.0, kroger_store_name="Walmart", kroger_location_id="loc1"))
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="Shampoo", store_name="Walmart"))
        db.session.add(StorePriceCache(
            store_name="Walmart",
            item_keyword="shampoo",
            product_title="Fake Shampoo",
            price=0.5,
            retailer="walmart",
        ))
        db.session.commit()

    with patch("services.retail.cart.WalmartSerpApiProvider", return_value=provider):
        response = app.test_client().post(
            "/api/grocery/generate-pay-period-plan",
                json={"recipe_ids": [], "store_name": "Walmart", "budget_limit": 100.0},
        )

    body = response.get_json() or {}
    assert response.status_code == 200
    assert body["store"]["store_id"] == "357"
    assert body["cart_items"][0]["product_label"] == "Selected Shampoo 12 oz"
    assert [row["product_id"] for row in body["cart_items"][0]["alternatives"]] == ["2"]
    assert body["cart_items"][0]["confirmed_local_store"] is True
    assert body["cart_items"][0]["needs_user_choice"] is False
    assert body["cart_items"][0]["suggested"] is True
    assert body["resolution_stats"]["search_calls"] == 1
    assert body["resolution_stats"]["verified_cache_hits"] == 0
    assert "Fake Shampoo" not in str(body)


def test_structured_requirement_overrides_legacy_generic_row() -> None:
    _setup()
    with app.app_context():
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="Peanut Butter", store_name="Walmart"))
        db.session.flush()
        structured = ShoppingRequirement(
            "Jif creamy peanut butter",
            "peanut butter",
            brand="Jif",
            variant="creamy",
        )
        row = GroceryItem(
            household_id=current_household_id(),
            item_name="Jif Creamy Peanut Butter",
            store_name="Walmart",
            shopping_requirement_json=json.dumps(structured.__dict__),
        )
        db.session.add(row)
        db.session.commit()

        requirements = _active_manual_requirements()

        assert len(requirements) == 1
        assert requirements[0].base_item == structured.base_item
        assert requirements[0].brand == structured.brand
        assert requirements[0].variant == structured.variant
        assert requirements[0].source_requirement_id == row.id


def test_more_informative_recent_structure_preserves_quantity_and_specificity() -> None:
    _setup()
    with app.app_context():
        db.session.add(GroceryItem(household_id=current_household_id(), item_name="Shampoo", store_name="Walmart"))
        db.session.flush()
        structured = ShoppingRequirement(
            "Head & Shoulders dandruff shampoo",
            "shampoo",
            brand="Head & Shoulders",
            variant="dandruff",
            quantity=2,
            unit="bottle",
        )
        row = GroceryItem(
            household_id=current_household_id(),
            item_name=structured.item_name,
            store_name="Walmart",
            shopping_requirement_json=json.dumps(structured.__dict__),
        )
        db.session.add(row)
        db.session.commit()

        requirements = _active_manual_requirements()

        assert len(requirements) == 1
        assert (requirements[0].quantity, requirements[0].unit) == (2.0, "bottle")
        assert requirements[0].source_requirement_id == row.id


def test_exact_generic_duplicates_collapse_to_one_package() -> None:
    _setup()
    with app.app_context():
        db.session.add_all([
            GroceryItem(household_id=current_household_id(), item_name="Bread", store_name="Walmart"),
            GroceryItem(household_id=current_household_id(), item_name="Bread", store_name="Walmart"),
        ])
        db.session.commit()

        requirements = _active_manual_requirements()

        assert len(requirements) == 1
        assert requirements[0].base_item == "Bread"
        assert requirements[0].quantity == 1.0
