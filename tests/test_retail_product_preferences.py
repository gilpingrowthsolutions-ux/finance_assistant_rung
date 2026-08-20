from __future__ import annotations

import json
import os
from unittest.mock import patch

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import GroceryItem, RetailProductPreference
from services.retail import ProductSearchResult, RetailProduct, ShoppingRequirement
from services.retail.cart import VERIFIED_WALMART_STORE, build_verified_walmart_cart
from services.retail.preferences import (
    forget_product_preference,
    get_product_preference,
    save_product_preference,
)
from services.household_context import household_id as current_household_id


class FakeProvider:
    def __init__(self, products: list[RetailProduct], detail: RetailProduct | None = None) -> None:
        self.products = products
        self.detail = detail or products[0]
        self.search_calls = 0
        self.detail_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        self.search_calls += 1
        return ProductSearchResult(store, store, self.products, len(self.products))

    def get_product(self, product_id, *, store, requested_query):
        self.detail_calls += 1
        return self.detail


def _product(
    title: str,
    identity: str,
    price: float,
    *,
    package: str | None = "1 gallon",
    availability: str = "in_stock",
    upc: str | None = None,
) -> RetailProduct:
    return RetailProduct.now(
        requested_query="milk",
        retailer="walmart",
        store=VERIFIED_WALMART_STORE,
        product_id=f"product-{identity}",
        us_item_id=identity,
        upc=upc,
        title=title,
        brand=None,
        variant=None,
        package_size=package,
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


def _seed_ambiguous_cache(provider: FakeProvider, requirement: ShoppingRequirement) -> None:
    db.session.add(GroceryItem(
        household_id=current_household_id(),
        item_name=requirement.item_name,
        store_name="Walmart",
        shopping_requirement_json=json.dumps(requirement.__dict__),
    ))
    db.session.commit()
    cart = build_verified_walmart_cart(provider=provider)
    item = cart["cart_items"][0]
    assert item["selected_product"] is not None or item["needs_user_choice"] is True


def test_save_usual_uses_verified_candidate_and_selective_detail() -> None:
    _setup()
    search_product = _product("Great Value Whole Milk, Gallon", "123", 4.22, upc=None)
    detail_product = _product("Great Value Whole Milk, Gallon", "123", 4.22, upc="000123")
    provider = FakeProvider([search_product], detail=detail_product)
    with app.app_context():
        _seed_ambiguous_cache(
            FakeProvider([search_product, _product("Lactaid Whole Milk", "456", 6.38)]),
            ShoppingRequirement("milk", "milk"),
        )
        preference, detail_calls = save_product_preference(
            base_item="milk",
            preference_type="usual",
            retailer="walmart",
            store_id="357",
            product_identity="123",
            provider=provider,
        )

        assert detail_calls == 1
        assert provider.detail_calls == 1
        assert preference.upc == "000123"
        assert preference.retailer_us_item_id == "123"
        assert preference.preferred_product_title == "Great Value Whole Milk, Gallon"
        assert preference.source == "user_explicit"
        assert not hasattr(preference, "price")


def test_saved_usual_auto_selects_current_cached_product_without_new_search() -> None:
    _setup()
    usual = _product("Great Value Whole Milk, Gallon", "123", 4.22, upc="000123")
    other = _product("Lactaid Whole Milk, 96 oz", "456", 6.38)
    provider = FakeProvider([usual, other])
    with app.app_context():
        _seed_ambiguous_cache(provider, ShoppingRequirement("milk", "milk"))
        db.session.add(RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk",
            normalized_base_item="milk",
            preference_type="usual",
            preferred_product_title=usual.title,
            upc="000123",
            retailer="walmart",
            retailer_product_id=usual.product_id,
            retailer_us_item_id=usual.us_item_id,
            source="user_explicit",
        ))
        db.session.commit()

        cart = build_verified_walmart_cart(provider=provider)
        item = cart["cart_items"][0]

        assert cart["resolution_stats"]["verified_cache_hits"] == 1
        assert provider.search_calls == 1
        assert item["selected_product"]["us_item_id"] == "123"
        assert item["selection_confidence"] == "high"
        assert item["needs_user_choice"] is False
        assert item["preferred_product"] is True
        assert item["preference"]["preference_type"] == "usual"
        assert item["estimated_price"] == 4.22


def test_favorite_has_stronger_authority_than_usual() -> None:
    _setup()
    with app.app_context():
        db.session.add_all([
            RetailProductPreference(
                household_id=current_household_id(),
                base_item="milk", normalized_base_item="milk", preference_type="usual",
                preferred_product_title="Usual Milk", retailer="walmart", retailer_us_item_id="1", source="user_explicit",
            ),
            RetailProductPreference(
                household_id=current_household_id(),
                base_item="milk", normalized_base_item="milk", preference_type="favorite",
                preferred_product_title="Favorite Milk", retailer="walmart", retailer_us_item_id="2", source="user_explicit",
            ),
        ])
        db.session.commit()
        assert get_product_preference("milk").preference_type == "favorite"


def test_explicit_current_request_bypasses_saved_usual() -> None:
    _setup()
    skippy = _product("Skippy Crunchy Peanut Butter 16 oz", "skippy", 3.5, package="16 oz")
    jif = _product("Jif Creamy Peanut Butter 16 oz", "jif", 3.5, package="16 oz")
    provider = FakeProvider([skippy, jif])
    requirement = ShoppingRequirement(
        "Skippy crunchy peanut butter",
        "peanut butter",
        brand="Skippy",
        variant="crunchy",
    )
    with app.app_context():
        db.session.add(GroceryItem(
            household_id=current_household_id(),
            item_name=requirement.item_name,
            store_name="Walmart",
            shopping_requirement_json=json.dumps(requirement.__dict__),
        ))
        db.session.add(RetailProductPreference(
            household_id=current_household_id(),
            base_item="peanut butter", normalized_base_item="peanut butter", preference_type="usual",
            preferred_product_title=jif.title, retailer="walmart", retailer_us_item_id="jif", source="user_explicit",
        ))
        db.session.commit()

        cart = build_verified_walmart_cart(provider=provider)
        item = cart["cart_items"][0]

        assert item["selected_product"]["us_item_id"] == "skippy"
        assert item["preference"] is None
        assert item["preferred_product"] is False


def test_out_of_stock_usual_requires_choice_without_substitution() -> None:
    _setup()
    usual = _product("Great Value Whole Milk, Gallon", "123", 4.22, availability="out_of_stock")
    substitute = _product("Lactaid Whole Milk, 96 oz", "456", 6.38)
    provider = FakeProvider([usual, substitute])
    with app.app_context():
        _seed_ambiguous_cache(provider, ShoppingRequirement("milk", "milk"))
        db.session.add(RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title=usual.title, retailer="walmart", retailer_us_item_id="123", source="user_explicit",
        ))
        db.session.commit()

        item = build_verified_walmart_cart(provider=provider)["cart_items"][0]

        assert item["selected_product"] is None
        assert item["needs_user_choice"] is True
        assert item["usual_unavailable"] is True
        assert item["estimated_price"] is None


def test_forget_usual_returns_generic_request_to_ambiguity() -> None:
    _setup()
    products = [_product("Great Value Whole Milk, Gallon", "123", 4.22), _product("Lactaid Whole Milk", "456", 6.38)]
    provider = FakeProvider(products)
    with app.app_context():
        _seed_ambiguous_cache(provider, ShoppingRequirement("milk", "milk"))
        db.session.add(RetailProductPreference(
            household_id=current_household_id(),
            base_item="milk", normalized_base_item="milk", preference_type="usual",
            preferred_product_title=products[0].title, retailer="walmart", retailer_us_item_id="123", source="user_explicit",
        ))
        db.session.commit()
        assert build_verified_walmart_cart(provider=provider)["cart_items"][0]["preferred_product"] is True
        deleted = forget_product_preference("milk", "usual")
        item = build_verified_walmart_cart(provider=provider)["cart_items"][0]

        assert deleted == 1
        assert item["selected_product"] is not None
        assert item["suggested"] is True
        assert item["needs_user_choice"] is False


def test_api_rejects_product_not_in_verified_cache() -> None:
    _setup()
    response = app.test_client().post("/api/retail/product-preference", json={
        "base_item": "milk",
        "preference_type": "usual",
        "retailer": "walmart",
        "store_id": "357",
        "product_identity": "invented",
    })
    assert response.status_code == 400
    with app.app_context():
        assert RetailProductPreference.query.count() == 0


def test_api_saves_and_forgets_verified_usual() -> None:
    _setup()
    search_product = _product("Great Value Whole Milk, Gallon", "123", 4.22)
    detail_product = _product("Great Value Whole Milk, Gallon", "123", 4.22, upc="000123")
    with app.app_context():
        _seed_ambiguous_cache(
            FakeProvider([search_product, _product("Lactaid Whole Milk", "456", 6.38)]),
            ShoppingRequirement("milk", "milk"),
        )

    with patch("services.retail.preferences.WalmartSerpApiProvider", return_value=FakeProvider([search_product], detail_product)):
        saved = app.test_client().post("/api/retail/product-preference", json={
            "base_item": "milk",
            "preference_type": "usual",
            "retailer": "walmart",
            "store_id": "357",
            "product_identity": "123",
        })

    assert saved.status_code == 200
    assert (saved.get_json() or {})["product_detail_calls"] == 1
    assert (saved.get_json() or {})["preference"]["upc"] == "000123"

    forgotten = app.test_client().delete("/api/retail/product-preference", json={
        "base_item": "milk",
        "preference_type": "usual",
    })
    assert forgotten.status_code == 200
    assert (forgotten.get_json() or {})["deleted"] == 1


def test_non_food_usual_and_explicit_override_use_same_precedence() -> None:
    _setup()
    usual = _product("Pantene Daily Moisture Shampoo 27.7 oz", "pantene", 9.97, package="27.7 oz")
    explicit = _product("Head & Shoulders Dandruff Shampoo 12 oz", "head", 8.97, package="12 oz")
    provider = FakeProvider([usual, explicit])
    with app.app_context():
        _seed_ambiguous_cache(provider, ShoppingRequirement("shampoo", "shampoo"))
        db.session.add(RetailProductPreference(
            household_id=current_household_id(),
            base_item="shampoo", normalized_base_item="shampoo", preference_type="usual",
            preferred_product_title=usual.title, retailer="walmart", retailer_us_item_id="pantene", source="user_explicit",
        ))
        db.session.commit()
        generic_item = build_verified_walmart_cart(provider=provider)["cart_items"][0]
        assert generic_item["selected_product"]["us_item_id"] == "pantene"

        GroceryItem.query.delete()
        explicit_requirement = ShoppingRequirement(
            "Head & Shoulders dandruff shampoo",
            "shampoo",
            brand="Head & Shoulders",
            variant="dandruff",
        )
        db.session.add(GroceryItem(
            household_id=current_household_id(),
            item_name=explicit_requirement.item_name,
            store_name="Walmart",
            shopping_requirement_json=json.dumps(explicit_requirement.__dict__),
        ))
        db.session.commit()
        explicit_item = build_verified_walmart_cart(provider=provider, force_refresh=True)["cart_items"][0]
        assert explicit_item["selected_product"]["us_item_id"] == "head"
        assert explicit_item["preference"] is None
