from __future__ import annotations

import json
import os

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import Account, GroceryItem, HouseholdShoppingDefault, RetailProductPreference
from services.retail import ProductSearchResult, RetailProduct
from services.retail.cart import VERIFIED_WALMART_STORE, build_verified_walmart_cart, propose_rebalance_preview
from services.household_context import household_id as current_household_id
from services.selected_store import select_store


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
        raise AssertionError("Unexpected product detail lookup")


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
        account = Account(household_id=current_household_id(), checking_balance=1250.00)
        db.session.add(account)
        db.session.flush()
        select_store(
            current_household_id(), retailer="walmart", store_id="357",
            store_name="Walmart — Versailles", address="1003 W Newton St, Versailles, MO 65084",
            city="Versailles", state="MO", postal_code="65084", account=account,
        )
        db.session.commit()


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
        db.session.add(
            GroceryItem(
                household_id=current_household_id(),
                item_name=item_name or base_item,
                store_name="Walmart",
                shopping_requirement_json=json.dumps(requirement),
            )
        )
        db.session.commit()


def _set_default(preference_key: str, preference_value: str) -> None:
    with app.app_context():
        db.session.add(
            HouseholdShoppingDefault(
                household_id=current_household_id(),
                owner_scope="household:default",
                preference_kind="category_default",
                preference_key=preference_key,
                preference_value=preference_value,
            )
        )
        db.session.commit()


def _set_style(style: str) -> None:
    with app.app_context():
        db.session.add(
            HouseholdShoppingDefault(
                household_id=current_household_id(),
                owner_scope="household:default",
                preference_kind="shopping_style",
                preference_key="shopping_style",
                preference_value=style,
            )
        )
        db.session.commit()


def _by_keyword(cart: dict) -> dict[str, dict]:
    return {str(item.get("keyword") or ""): item for item in cart.get("cart_items", [])}


def _preview_payload(cart: dict, *, budget_limit: float) -> dict:
    return {
        "cart_items": cart["cart_items"],
        "budget_limit": budget_limit,
        "tax_rate": 0.0,
        "cart_context": {
            "retailer": "walmart",
            "store_id": VERIFIED_WALMART_STORE.store_id,
            "store_name": VERIFIED_WALMART_STORE.name,
        },
    }


def test_rebalance_preview_within_budget_not_eligible() -> None:
    _setup()
    _seed_requirement("bread")
    _set_style("prefer_brands_when_possible")
    provider = FakeProvider(
        {
            "bread": [
                _product("bread", "Wonder Bread", "brand", 3.00),
                _product("bread", "Great Value Bread", "cheap", 2.00),
            ]
        }
    )

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=10.00, tax_rate=0.0)
        preview = propose_rebalance_preview(
            cart_items=cart["cart_items"],
            budget_limit=10.00,
            tax_rate=0.0,
            retailer="walmart",
            defaults={"preferences": {}, "shopping_style": "prefer_brands_when_possible"},
            protected_choice_keys=set(),
            context={"retailer": "walmart", "store_id": "357", "store_name": "Walmart"},
        )

    assert preview["status"] == "within_budget"
    assert preview["eligible"] is False


def test_manual_change_over_budget_offers_rebalance_without_mutating_cart() -> None:
    _setup()
    _seed_requirement("coffee")
    _seed_requirement("paper towels")
    _seed_requirement("dish soap")
    _set_style("prefer_brands_when_possible")
    _set_default("coffee_caffeine", "dont_care")
    provider = FakeProvider(
        {
            "coffee": [
                _product("coffee", "Great Value Coffee", "coffee-cheap", 4.00),
                _product("coffee", "Premium Coffee", "coffee-exp", 11.00),
            ],
            "paper towels": [
                _product("paper towels", "Bounty", "towel-brand", 8.00),
                _product("paper towels", "Great Value Towels", "towel-cheap", 6.00),
            ],
            "dish soap": [
                _product("dish soap", "Dawn Dish Soap", "soap-brand", 5.00),
                _product("dish soap", "Great Value Dish Soap", "soap-cheap", 3.00),
            ],
        }
    )

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=20.00, tax_rate=0.0)

    by_kw = _by_keyword(cart)
    coffee = by_kw["coffee"]
    manual_choice = next(alt for alt in coffee["alternatives"] if alt["us_item_id"] == "coffee-exp")
    original = coffee["selected_product"]
    coffee["alternatives"] = [original] + [alt for alt in coffee["alternatives"] if alt["us_item_id"] != "coffee-exp"]
    coffee["selected_product"] = manual_choice
    coffee["selection_confidence"] = "user_selected"
    coffee["suggested"] = False
    coffee["estimated_price"] = 11.00

    snapshot_title = coffee["selected_product"]["title"]

    with app.app_context():
        preview = propose_rebalance_preview(
            cart_items=cart["cart_items"],
                budget_limit=20.00,
            tax_rate=0.0,
            retailer="walmart",
            defaults={"preferences": {"coffee_caffeine": "dont_care"}, "shopping_style": "prefer_brands_when_possible"},
            protected_choice_keys={"coffee"},
            context={"retailer": "walmart", "store_id": "357", "store_name": "Walmart"},
        )

    assert preview["eligible"] is True
    assert preview["changes"]
    assert all(change["choice_key"] != "coffee" for change in preview["changes"])
    assert coffee["selected_product"]["title"] == snapshot_title


def test_locked_line_excluded_from_rebalance_changes() -> None:
    _setup()
    _seed_requirement("bread")
    _seed_requirement("detergent")
    _set_style("save_most")
    _set_default("bread_type", "dont_care")
    _set_default("laundry_detergent_scent", "dont_care")
    provider = FakeProvider(
        {
            "bread": [
                _product("bread", "Bread Brand", "bread-brand", 8.00),
                _product("bread", "Bread Cheap", "bread-cheap", 4.00),
            ],
            "detergent": [
                _product("detergent", "Soap Brand", "soap-brand", 8.00),
                _product("detergent", "Soap Cheap", "soap-cheap", 4.00),
            ],
        }
    )

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=10.00, tax_rate=0.0)
        preview = propose_rebalance_preview(
            cart_items=cart["cart_items"],
            budget_limit=10.00,
            tax_rate=0.0,
            retailer="walmart",
            defaults={
                "preferences": {"bread_type": "dont_care", "laundry_detergent_scent": "dont_care"},
                "shopping_style": "save_most",
            },
            protected_choice_keys={"bread"},
            context={"retailer": "walmart", "store_id": "357", "store_name": "Walmart"},
        )

    assert all(change["choice_key"] != "bread" for change in preview["changes"])


def test_exact_usual_preference_is_protected_in_rebalance() -> None:
    _setup()
    _seed_requirement("milk")
    _seed_requirement("bread")
    _set_style("save_most")
    provider = FakeProvider(
        {
            "milk": [
                _product("milk", "Brand Whole Milk", "milk-usual", 9.00),
                _product("milk", "Store Milk", "milk-cheap", 4.00),
            ],
            "bread": [
                _product("bread", "Bread Brand", "bread-brand", 7.00),
                _product("bread", "Bread Cheap", "bread-cheap", 3.00),
            ],
        }
    )

    with app.app_context():
        db.session.add(
            RetailProductPreference(
                household_id=current_household_id(),
                base_item="milk",
                normalized_base_item="milk",
                preference_type="usual",
                preferred_product_title="Brand Whole Milk",
                retailer="walmart",
                retailer_us_item_id="milk-usual",
                source="user_explicit",
            )
        )
        db.session.commit()
        cart = build_verified_walmart_cart(provider=provider, budget_limit=9.00, tax_rate=0.0)
        preview = propose_rebalance_preview(
            cart_items=cart["cart_items"],
            budget_limit=9.00,
            tax_rate=0.0,
            retailer="walmart",
            defaults={"preferences": {}, "shopping_style": "save_most"},
            protected_choice_keys=set(),
            context={"retailer": "walmart", "store_id": "357", "store_name": "Walmart"},
        )

    assert all(change["choice_key"] != "milk" for change in preview["changes"])


def test_impossible_rebalance_reports_still_over_budget() -> None:
    _setup()
    _seed_requirement("bread")
    _set_style("save_most")
    provider = FakeProvider({"bread": [_product("bread", "Only Bread", "only", 8.00)]})

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=3.00, tax_rate=0.0)
        preview = propose_rebalance_preview(
            cart_items=cart["cart_items"],
            budget_limit=3.00,
            tax_rate=0.0,
            retailer="walmart",
            defaults={"preferences": {}, "shopping_style": "save_most"},
            protected_choice_keys=set(),
            context={"retailer": "walmart", "store_id": "357", "store_name": "Walmart"},
        )

    assert preview["status"] in {"over_budget_no_acceptable_savings", "rebalance_partial"}
    assert preview["still_over_budget_cents"] > 0


def test_unknown_package_quantity_requirement_is_not_rebalanced() -> None:
    _setup()
    cart_item = {
        "keyword": "rice",
        "resolved": True,
        "quantity_uncertain": True,
        "package_resolution_uncertain": True,
        "packages_to_buy": None,
        "estimated_price": None,
        "selection_confidence": "auto_selected",
        "selected_product": {"title": "Rice 5 lb", "us_item_id": "rice-current", "price": 9.00},
        "alternatives": [{"title": "Rice 2 lb", "us_item_id": "rice-cheap", "price": 3.00}],
        "requirement": {
            "item_name": "rice",
            "base_item": "rice",
            "source_kind": "recipe",
            "source_recipe_title": "Rice Bowl",
            "quantity": None,
            "unit": None,
        },
    }

    with app.app_context():
        preview = propose_rebalance_preview(
            cart_items=[cart_item],
            budget_limit=-1.00,
            tax_rate=0.0,
            retailer="walmart",
            defaults={"preferences": {}, "shopping_style": "save_most"},
            protected_choice_keys=set(),
            context={"retailer": "walmart", "store_id": "357", "store_name": "Walmart"},
        )

    assert preview["changes"] == []
    assert preview["status"] == "over_budget_no_acceptable_savings"


def test_preview_endpoint_does_not_trigger_provider_search_calls() -> None:
    _setup()
    _seed_requirement("milk")
    _set_style("save_most")
    provider = FakeProvider(
        {
            "milk": [
                _product("milk", "Brand Milk", "brand", 8.00),
                _product("milk", "Cheap Milk", "cheap", 3.00),
            ]
        }
    )

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=4.00, tax_rate=0.0)
        calls_before = provider.search_calls

    client = app.test_client()
    response = client.post("/api/grocery/rebalance/preview", json=_preview_payload(cart, budget_limit=4.00))

    assert response.status_code == 200
    assert provider.search_calls == calls_before


def test_apply_rejects_stale_preview_when_budget_changes() -> None:
    _setup()
    _seed_requirement("bread")
    _seed_requirement("detergent")
    _set_style("save_most")
    _set_default("bread_type", "dont_care")
    _set_default("laundry_detergent_scent", "dont_care")
    provider = FakeProvider(
        {
            "bread": [
                _product("bread", "Bread Brand", "bread-brand", 8.00),
                _product("bread", "Bread Cheap", "bread-cheap", 4.00),
            ],
            "detergent": [
                _product("detergent", "Soap Brand", "soap-brand", 8.00),
                _product("detergent", "Soap Cheap", "soap-cheap", 4.00),
            ],
        }
    )

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=10.00, tax_rate=0.0)

    client = app.test_client()
    preview_resp = client.post("/api/grocery/rebalance/preview", json=_preview_payload(cart, budget_limit=10.00))
    assert preview_resp.status_code == 200
    preview = preview_resp.get_json() or {}

    apply_resp = client.post(
        "/api/grocery/rebalance/apply",
        json={
            **_preview_payload(cart, budget_limit=9.00),
            "preview": {
                "context_fingerprint": preview.get("context_fingerprint"),
                "proposal_fingerprint": preview.get("proposal_fingerprint"),
                "protected_choice_keys": preview.get("protected_choice_keys") or [],
            },
        },
    )
    assert apply_resp.status_code == 409
    assert (apply_resp.get_json() or {}).get("code") == "stale_rebalance_preview"


def test_apply_rejects_stale_preview_when_retailer_changes() -> None:
    _setup()
    _seed_requirement("bread")
    _set_style("save_most")
    provider = FakeProvider(
        {
            "bread": [
                _product("bread", "Bread Brand", "bread-brand", 8.00),
                _product("bread", "Bread Cheap", "bread-cheap", 4.00),
            ]
        }
    )

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=6.00, tax_rate=0.0)

    client = app.test_client()
    payload = _preview_payload(cart, budget_limit=6.00)
    preview_resp = client.post("/api/grocery/rebalance/preview", json=payload)
    preview = preview_resp.get_json() or {}

    payload["cart_context"]["retailer"] = "kroger"
    apply_resp = client.post(
        "/api/grocery/rebalance/apply",
        json={
            **payload,
            "preview": {
                "context_fingerprint": preview.get("context_fingerprint"),
                "proposal_fingerprint": preview.get("proposal_fingerprint"),
                "protected_choice_keys": preview.get("protected_choice_keys") or [],
            },
        },
    )

    assert apply_resp.status_code == 409


def test_apply_returns_only_previewed_lines_and_no_preference_persistence() -> None:
    _setup()
    _seed_requirement("coffee")
    _seed_requirement("paper towels")
    _seed_requirement("dish soap")
    _set_style("prefer_brands_when_possible")
    _set_default("coffee_caffeine", "dont_care")
    provider = FakeProvider(
        {
            "coffee": [
                _product("coffee", "Great Value Coffee", "coffee-cheap", 4.00),
                _product("coffee", "Premium Coffee", "coffee-exp", 11.00),
            ],
            "paper towels": [
                _product("paper towels", "Bounty", "towel-brand", 8.00),
                _product("paper towels", "Great Value Towels", "towel-cheap", 6.00),
            ],
            "dish soap": [
                _product("dish soap", "Dawn Dish Soap", "soap-brand", 5.00),
                _product("dish soap", "Great Value Dish Soap", "soap-cheap", 3.00),
            ],
        }
    )

    with app.app_context():
        cart = build_verified_walmart_cart(provider=provider, budget_limit=20.00, tax_rate=0.0)

    by_kw = _by_keyword(cart)
    coffee = by_kw["coffee"]
    manual_choice = next(alt for alt in coffee["alternatives"] if alt["us_item_id"] == "coffee-exp")
    original = coffee["selected_product"]
    coffee["alternatives"] = [original] + [alt for alt in coffee["alternatives"] if alt["us_item_id"] != "coffee-exp"]
    coffee["selected_product"] = manual_choice
    coffee["selection_confidence"] = "user_selected"
    coffee["suggested"] = False
    coffee["estimated_price"] = 11.00

    client = app.test_client()
    preview_resp = client.post(
        "/api/grocery/rebalance/preview",
        json={
            **_preview_payload(cart, budget_limit=20.00),
            "last_changed_choice_key": "coffee",
            "protected_choice_keys": ["coffee"],
        },
    )
    preview = preview_resp.get_json() or {}
    assert preview_resp.status_code == 200
    assert preview.get("changes")

    apply_resp = client.post(
        "/api/grocery/rebalance/apply",
        json={
            **_preview_payload(cart, budget_limit=20.00),
            "protected_choice_keys": ["coffee"],
            "preview": {
                "context_fingerprint": preview.get("context_fingerprint"),
                "proposal_fingerprint": preview.get("proposal_fingerprint"),
                "protected_choice_keys": preview.get("protected_choice_keys") or [],
            },
        },
    )
    body = apply_resp.get_json() or {}
    assert apply_resp.status_code == 200
    assert body.get("applied") is True

    preview_keys = sorted(change.get("choice_key") for change in (preview.get("changes") or []))
    applied_keys = sorted(change.get("choice_key") for change in (body.get("applied_choices") or []))
    assert applied_keys == preview_keys

    with app.app_context():
        assert RetailProductPreference.query.count() == 0
