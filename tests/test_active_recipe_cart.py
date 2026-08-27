"""Package 7 Phase A — persisted active recipes enter the verified retail cart.

These tests prove that ``MealPlanItem`` (the Package 5 active-recipe
authority) flows through ``Recipe`` / ``RecipeIngredient`` into the *same*
verified Walmart/Kroger cart path used for direct grocery items, while
preserving Package 6 quantity/unit fidelity and recipe provenance.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from unittest.mock import patch

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import app
from extensions import db
from models import (
    Account,
    GroceryItem,
    Household,
    MealPlanItem,
    Recipe,
    RecipeIngredient,
)
from services.household_context import household_id as current_household_id
from services.recipe_requirements import (
    active_recipe_requirements,
    derive_recipe_base_item,
)
from services.retail import ProductSearchResult, RetailProduct, RetailStore
from services.retail.cart import (
    VERIFIED_KROGER_STORE,
    VERIFIED_WALMART_STORE,
    build_verified_retail_cart,
    build_verified_walmart_cart,
)
from services.selected_store import select_store
from tests.meal_plan_support import current_plan_item, install_current_cycle


class FakeProvider:
    def __init__(self) -> None:
        self.search_calls = 0
        self.detail_calls = 0

    def search_products(self, requirement, *, store, limit=20):
        self.search_calls += 1
        title = requirement.base_item.title()
        product = RetailProduct.now(
            requested_query=requirement.search_query(),
            retailer="walmart",
            store=store,
            product_id=f"p-{requirement.base_item}",
            us_item_id=f"u-{requirement.base_item}",
            upc=f"000000{self.search_calls:06d}",
            title=f"Great Value {title} 1 lb",
            brand="Great Value",
            variant=None,
            package_size="1 lb",
            price=2.50,
            availability="in_stock",
            price_type="unknown",
            product_url=f"https://example.com/{requirement.base_item}",
            source="serpapi_walmart",
            verified_location=True,
            regular_price=2.75,
            promo_price=2.50,
        )
        return ProductSearchResult(store, store, [product], 1)

    def get_product(self, product_id, *, store, requested_query):
        self.detail_calls += 1
        raise AssertionError("test products already include package data")


@pytest.fixture()
def setup(monkeypatch):
    install_current_cycle(monkeypatch)
    with app.app_context():
        db.drop_all()
        db.create_all()
    return app.test_client()


def _seed_recipe(title: str, ingredients: list[tuple]) -> Recipe:
    recipe = Recipe(title=title, servings=4, recipe_scope=Recipe.SCOPE_CANONICAL)
    db.session.add(recipe)
    db.session.flush()
    for product_name, clean_keyword, quantity, unit in ingredients:
        db.session.add(RecipeIngredient(
            recipe_id=recipe.id,
            product_name=product_name,
            clean_keyword=clean_keyword,
            quantity=quantity,
            unit=unit,
        ))
    db.session.flush()
    return recipe


def test_single_active_recipe_produces_ingredient_requirements_with_fidelity(setup):
    with app.app_context():
        hid = current_household_id()
        recipe = _seed_recipe("Rice Bowl", [
            ("2 cups rice", "rice", 2.0, "cup"),
            ("1.5 lb chicken", "chicken", 1.5, "lb"),
        ])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()

        requirements = active_recipe_requirements(hid)

        assert len(requirements) == 2
        rice, chicken = requirements
        assert (rice.base_item, rice.quantity, rice.unit) == ("rice", 2.0, "cup")
        assert (chicken.base_item, chicken.quantity, chicken.unit) == ("chicken", 1.5, "lb")


def test_base_retail_query_excludes_quantity_and_unit_text():
    cases = [
        ("2 cups rice", "rice", "rice"),
        ("1.5 lb chicken", "chicken", "chicken"),
        ("3 cans beans", "beans", "beans"),
    ]
    for product_name, clean_keyword, expected in cases:
        assert derive_recipe_base_item(product_name=product_name, clean_keyword=clean_keyword) == expected
        # Even a quantity-bearing clean_keyword must be defensively stripped.
        quantity_bearing = product_name.replace(" ", "_")
        assert derive_recipe_base_item(product_name=product_name, clean_keyword=quantity_bearing) == expected


def test_base_retail_query_preserves_meaningful_product_terms():
    assert derive_recipe_base_item(product_name="2 cups ready-to-eat rice") == "ready-to-eat rice"
    assert derive_recipe_base_item(product_name="1 lb chicken stock") == "chicken stock"
    assert derive_recipe_base_item(product_name="garnish cherries") == "garnish cherries"
    assert derive_recipe_base_item(product_name="taste of the wild seasoning") == "taste of the wild seasoning"
    assert derive_recipe_base_item(product_name="salt to taste", clean_keyword="salt_to_taste") == "salt"


def test_multiple_active_recipes_contribute_requirements(setup):
    with app.app_context():
        hid = current_household_id()
        rice_bowl = _seed_recipe("Rice Bowl", [("2 cups rice", "rice", 2.0, "cup")])
        tacos = _seed_recipe("Tacos", [
            ("3 cans beans", "beans", 3.0, "can"),
            ("1 head lettuce", "lettuce", 1.0, "head"),
        ])
        db.session.add_all([
            current_plan_item(household_id=hid, recipe_id=rice_bowl.id, source="user"),
            current_plan_item(household_id=hid, recipe_id=tacos.id, source="user"),
        ])
        db.session.commit()

        requirements = active_recipe_requirements(hid)

        assert [r.base_item for r in requirements] == ["rice", "beans", "lettuce"]
        assert {r.source_recipe_id for r in requirements} == {rice_bowl.id, tacos.id}


def test_direct_grocery_items_and_recipe_requirements_coexist(setup):
    provider = FakeProvider()
    with app.app_context():
        hid = current_household_id()
        db.session.add(GroceryItem(household_id=hid, item_name="Milk", store_name="Walmart"))
        recipe = _seed_recipe("Rice Bowl", [("2 cups rice", "rice", 2.0, "cup")])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()

        cart = build_verified_walmart_cart(provider=provider)

    keywords = sorted((item["keyword"] or "").lower() for item in cart["cart_items"])
    assert "milk" in keywords
    assert "rice" in keywords


def test_inactive_recipes_contribute_nothing(setup):
    with app.app_context():
        hid = current_household_id()
        active = _seed_recipe("Active", [("2 cups rice", "rice", 2.0, "cup")])
        inactive = _seed_recipe("Inactive", [("1 lb beef", "beef", 1.0, "lb")])
        # Only "Active" is added to the meal plan.
        db.session.add(current_plan_item(household_id=hid, recipe_id=active.id, source="user"))
        db.session.commit()

        requirements = active_recipe_requirements(hid)

        assert [r.base_item for r in requirements] == ["rice"]
        assert all(r.source_recipe_id != inactive.id for r in requirements)


def test_unknown_quantity_and_unit_remain_truthful(setup):
    provider = FakeProvider()
    with app.app_context():
        hid = current_household_id()
        recipe = _seed_recipe("Seasoned", [("salt to taste", "salt", None, None)])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()

        requirements = active_recipe_requirements(hid)
        assert len(requirements) == 1
        assert requirements[0].quantity is None
        assert requirements[0].unit is None
        assert requirements[0].base_item == "salt"

        cart = build_verified_walmart_cart(provider=provider)

    item = cart["cart_items"][0]
    assert item["quantity_uncertain"] is True
    assert item["packages_to_buy"] is None
    assert item["estimated_price"] is None


def test_canonical_selected_store_is_used_for_recipe_cart(setup):
    provider = FakeProvider()
    recipe_id = None
    with app.app_context():
        hid = current_household_id()
        account = Account(household_id=hid, checking_balance=1000.0, zip_code="65026")
        db.session.add(account)
        db.session.flush()
        select_store(
            hid,
            retailer="walmart",
            store_id="999",
            store_name="Walmart Test Store",
            address="1 Main St, Eldon, MO 65026",
            postal_code="65026",
            account=account,
        )
        recipe = _seed_recipe("Rice Bowl", [("2 cups rice", "rice", 2.0, "cup")])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()
        recipe_id = recipe.id

    with patch("services.retail.cart.WalmartSerpApiProvider", return_value=provider):
        response = app.test_client().post(
            "/api/grocery/generate-pay-period-plan",
                json={"recipe_ids": [recipe_id], "budget_limit": 100.0},
        )

    assert response.status_code == 200
    body = response.get_json() or {}
    assert body["store"]["store_id"] == "999"
    assert body["store"]["name"] == "Walmart Test Store"
    assert any(item["keyword"] == "rice" for item in body["cart_items"])


def test_walmart_verified_path_resolves_recipe_requirements(setup):
    provider = FakeProvider()
    with app.app_context():
        hid = current_household_id()
        recipe = _seed_recipe("Rice Bowl", [("2 cups rice", "rice", 2.0, "cup")])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()

        cart = build_verified_walmart_cart(provider=provider)

    item = cart["cart_items"][0]
    assert item["keyword"] == "rice"
    assert item["resolved"] is True
    assert item["store_id"] == VERIFIED_WALMART_STORE.store_id
    assert item["requirement"]["quantity"] == 2.0
    assert item["requirement"]["unit"] == "cup"
    assert item["packages_to_buy"] is None
    assert item["estimated_price"] is None
    assert item["package_resolution_uncertain"] is True


def test_kroger_verified_path_resolves_recipe_requirements(setup):
    provider = FakeProvider()
    with app.app_context():
        hid = current_household_id()
        recipe = _seed_recipe("Rice Bowl", [("2 cups rice", "rice", 2.0, "cup")])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()

        cart = build_verified_retail_cart(
            retailer="kroger",
            store=VERIFIED_KROGER_STORE,
            provider=provider,
        )

    item = cart["cart_items"][0]
    assert item["keyword"] == "rice"
    assert item["resolved"] is True
    assert item["store_id"] == VERIFIED_KROGER_STORE.store_id


def test_recipe_provenance_survives_into_cart_item(setup):
    provider = FakeProvider()
    recipe_id = None
    with app.app_context():
        hid = current_household_id()
        recipe = _seed_recipe("Rice Bowl", [("2 cups rice", "rice", 2.0, "cup")])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()
        recipe_id = recipe.id

        cart = build_verified_walmart_cart(provider=provider)

    requirement = cart["cart_items"][0]["requirement"]
    assert requirement["source_kind"] == "recipe"
    assert requirement["source_recipe_id"] == recipe_id
    assert requirement["source_recipe_title"] == "Rice Bowl"
    assert requirement["source_text"] == "2 cups rice"
    assert requirement["quantity"] == 2.0
    assert requirement["unit"] == "cup"


def test_household_isolation_holds_for_recipe_requirements(setup):
    with app.app_context():
        db.drop_all()
        db.create_all()
        a = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="a")
        b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="b")
        db.session.add_all([a, b])
        db.session.flush()
        rice_recipe = _seed_recipe("A Rice", [("2 cups rice", "rice", 2.0, "cup")])
        bean_recipe = _seed_recipe("B Beans", [("3 cans beans", "beans", 3.0, "can")])
        db.session.add_all([
            current_plan_item(household_id=a.id, recipe_id=rice_recipe.id, source="user"),
            current_plan_item(household_id=b.id, recipe_id=bean_recipe.id, source="user"),
        ])
        db.session.commit()
        a_id, b_id = a.id, b.id

    with app.app_context():
        assert [r.base_item for r in active_recipe_requirements(a_id)] == ["rice"]
        assert [r.base_item for r in active_recipe_requirements(b_id)] == ["beans"]


def test_verified_cart_scopes_direct_and_recipe_requirements_to_signed_household(setup, monkeypatch):
    monkeypatch.setenv("RUNG_HOUSEHOLD_CONTEXT_SECRET", "pkg7-household-secret")
    with app.app_context():
        db.drop_all()
        db.create_all()
        a = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="a")
        b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="b")
        db.session.add_all([a, b])
        db.session.flush()
        db.session.add_all([
            Account(household_id=a.id, checking_balance=500.0),
            Account(household_id=b.id, checking_balance=500.0),
            GroceryItem(household_id=a.id, item_name="A Milk", store_name="Walmart"),
            GroceryItem(household_id=b.id, item_name="B Soap", store_name="Walmart"),
        ])
        recipe_a = _seed_recipe("A Rice", [("2 cups rice", "rice", 2.0, "cup")])
        recipe_b = _seed_recipe("B Beans", [("3 cans beans", "beans", 3.0, "can")])
        db.session.add_all([
            current_plan_item(household_id=a.id, recipe_id=recipe_a.id, source="user"),
            current_plan_item(household_id=b.id, recipe_id=recipe_b.id, source="user"),
        ])
        db.session.commit()
        a_public_id = a.public_id
        b_id = b.id
        recipe_b_id = recipe_b.id

    signature = hmac.new(
        b"pkg7-household-secret", a_public_id.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    headers = {"X-Household-Id": a_public_id, "X-Household-Signature": signature}
    with app.test_request_context(headers=headers), app.app_context():
        cart = build_verified_walmart_cart(provider=FakeProvider())
        keywords = {item["keyword"].lower() for item in cart["cart_items"]}
        assert keywords == {"a_milk", "rice"}
        assert GroceryItem.query.filter_by(household_id=b_id, item_name="B Soap", is_purchased=False).count() == 1
        assert MealPlanItem.query.filter_by(household_id=b_id, recipe_id=recipe_b_id).count() == 1


def test_repeated_cart_generation_does_not_mutate_or_duplicate_recipe_state(setup):
    provider = FakeProvider()
    with app.app_context():
        hid = current_household_id()
        recipe = _seed_recipe("Rice Bowl", [("2 cups rice", "rice", 2.0, "cup")])
        db.session.add(current_plan_item(household_id=hid, recipe_id=recipe.id, source="user"))
        db.session.commit()
        plan_before = MealPlanItem.query.count()
        ingredient_before = RecipeIngredient.query.count()

        first = build_verified_walmart_cart(provider=provider)
        second = build_verified_walmart_cart(provider=provider)

        assert MealPlanItem.query.count() == plan_before
        assert RecipeIngredient.query.count() == ingredient_before
        assert len(first["cart_items"]) == 1
        assert len(second["cart_items"]) == 1
