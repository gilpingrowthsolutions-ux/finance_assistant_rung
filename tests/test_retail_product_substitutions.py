from __future__ import annotations

import json
import os
from datetime import datetime, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import (
    GroceryItem,
    RetailProductCache,
    RetailProductPreference,
    RetailProductSubstitution,
)
from services.retail import ShoppingRequirement
from services.retail.cart import SELECTION_POLICY_VERSION, build_verified_walmart_cart
from services.retail.preferences import (
    forget_product_preference,
    remove_product_substitution,
    save_product_substitution,
)
from services.household_context import household_id as current_household_id

STORE = {
    "store_id": "357",
    "name": "Walmart — Versailles",
    "address": "1003 W Newton St, Versailles, MO 65084",
    "postal_code": "65084",
    "verified": True,
}


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _candidate(title: str, identity: str, price: float, availability: str = "in_stock") -> dict:
    return {
        "requested_query": "peanut butter",
        "retailer": "walmart",
        "store": STORE,
        "product_id": f"product-{identity}",
        "us_item_id": identity,
        "upc": f"upc-{identity}",
        "title": title,
        "brand": None,
        "variant": None,
        "package_size": "16 oz",
        "price": price,
        "availability": availability,
        "price_type": "unknown",
        "product_url": f"https://www.walmart.com/ip/{identity}",
        "source": "serpapi_walmart",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "verified_location": True,
    }


def _seed(
    *,
    base_item: str,
    candidates: list[dict],
    usual_identity: str,
    item_name: str | None = None,
    brand: str | None = None,
    variant: str | None = None,
) -> RetailProductPreference:
    requirement = ShoppingRequirement(
        item_name or base_item,
        base_item,
        brand=brand,
        variant=variant,
    )
    db.session.add(GroceryItem(
        household_id=current_household_id(),
        item_name=requirement.item_name,
        store_name="Walmart",
        shopping_requirement_json=json.dumps(requirement.__dict__),
    ))
    payload = {
        "requirement": requirement.__dict__,
        "selected_product": None,
        "alternatives": candidates[:4],
        "candidates": candidates,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "selection_confidence": "low",
        "needs_user_choice": True,
        "selection_policy_version": SELECTION_POLICY_VERSION,
    }
    db.session.add(RetailProductCache(
        retailer="walmart",
        store_id="357",
        store_name="Walmart — Versailles",
        store_address=STORE["address"],
        requested_query=requirement.search_query(),
        base_item=base_item,
        title=f"Unresolved: {requirement.search_query()}",
        provider_source="serpapi_walmart",
        verified_location=True,
        response_json=json.dumps(payload),
        retrieved_at=datetime.now(timezone.utc),
    ))
    usual_product = next(product for product in candidates if product["us_item_id"] == usual_identity)
    preference = RetailProductPreference(
        household_id=current_household_id(),
        base_item=base_item,
        normalized_base_item=base_item,
        preference_type="usual",
        preferred_product_title=usual_product["title"],
        upc=usual_product["upc"],
        retailer="walmart",
        retailer_product_id=usual_product["product_id"],
        retailer_us_item_id=usual_identity,
        source="user_explicit",
    )
    db.session.add(preference)
    db.session.commit()
    return preference


def _approve(preference: RetailProductPreference, product: dict) -> RetailProductSubstitution:
    row = RetailProductSubstitution(
        household_id=current_household_id(),
        base_item=preference.base_item,
        normalized_base_item=preference.normalized_base_item,
        preferred_preference_id=preference.id,
        substitute_product_title=product["title"],
        substitute_upc=product["upc"],
        retailer="walmart",
        retailer_product_id=product["product_id"],
        retailer_us_item_id=product["us_item_id"],
        approval_type="explicit",
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_usual_available_still_outranks_approved_substitute() -> None:
    _setup()
    with app.app_context():
        jif = _candidate("Jif Creamy Peanut Butter 16 oz", "jif", 3.5)
        skippy = _candidate("Skippy Creamy Peanut Butter 16 oz", "skippy", 3.25)
        preference = _seed(base_item="peanut butter", candidates=[jif, skippy], usual_identity="jif")
        _approve(preference, skippy)

        item = build_verified_walmart_cart()["cart_items"][0]

        assert item["selected_product"]["us_item_id"] == "jif"
        assert item["preferred_product"] is True
        assert item["substituted"] is False
        assert item["selection_confidence"] == "high"


def test_unavailable_usual_without_approval_requires_choice() -> None:
    _setup()
    with app.app_context():
        jif = _candidate("Jif Creamy Peanut Butter 16 oz", "jif", 3.5, "out_of_stock")
        skippy = _candidate("Skippy Creamy Peanut Butter 16 oz", "skippy", 3.25)
        _seed(base_item="peanut butter", candidates=[jif, skippy], usual_identity="jif")

        item = build_verified_walmart_cart()["cart_items"][0]

        assert item["selected_product"] is None
        assert item["needs_user_choice"] is True
        assert item["usual_unavailable"] is True
        assert item["substitution"] is None


def test_temporary_substitute_choice_does_not_persist() -> None:
    _setup()
    with app.app_context():
        jif = _candidate("Jif Creamy Peanut Butter 16 oz", "jif", 3.5, "out_of_stock")
        skippy = _candidate("Skippy Creamy Peanut Butter 16 oz", "skippy", 3.25)
        _seed(base_item="peanut butter", candidates=[jif, skippy], usual_identity="jif")
        item = build_verified_walmart_cart()["cart_items"][0]
        assert any(row["us_item_id"] == "skippy" for row in item["alternatives"])
        assert RetailProductSubstitution.query.count() == 0


def test_approved_substitute_persists_and_auto_selects_without_search() -> None:
    _setup()
    with app.app_context():
        jif = _candidate("Jif Creamy Peanut Butter 16 oz", "jif", 3.5, "out_of_stock")
        skippy = _candidate("Skippy Creamy Peanut Butter 16 oz", "skippy", 3.25)
        _seed(base_item="peanut butter", candidates=[jif, skippy], usual_identity="jif")

        substitution, detail_calls = save_product_substitution(
            base_item="peanut butter",
            product_identity="skippy",
            retailer="walmart",
            store_id="357",
        )
        cart = build_verified_walmart_cart()
        item = cart["cart_items"][0]

        assert detail_calls == 0
        assert substitution.approval_type == "explicit"
        assert not hasattr(substitution, "price")
        assert not hasattr(substitution, "availability")
        assert cart["resolution_stats"]["search_calls"] == 0
        assert cart["resolution_stats"]["verified_cache_hits"] == 1
        assert item["selected_product"]["us_item_id"] == "skippy"
        assert item["substituted"] is True
        assert item["usual_unavailable"] is True
        assert item["preferred_product"] is False


def test_approved_substitute_unavailable_returns_to_choice() -> None:
    _setup()
    with app.app_context():
        jif = _candidate("Jif Creamy Peanut Butter 16 oz", "jif", 3.5, "out_of_stock")
        skippy = _candidate("Skippy Creamy Peanut Butter 16 oz", "skippy", 3.25, "out_of_stock")
        other = _candidate("Great Value Creamy Peanut Butter 16 oz", "other", 2.0)
        preference = _seed(base_item="peanut butter", candidates=[jif, skippy, other], usual_identity="jif")
        _approve(preference, skippy)

        item = build_verified_walmart_cart()["cart_items"][0]

        assert item["selected_product"] is None
        assert item["needs_user_choice"] is True
        assert item["usual_unavailable"] is True


def test_explicit_current_request_bypasses_usual_and_substitute() -> None:
    _setup()
    with app.app_context():
        tide = _candidate("Tide Original Laundry Detergent 92 oz", "tide", 14.0)
        gain = _candidate("Gain Original Laundry Detergent 88 oz", "gain", 12.0)
        all_free = _candidate("all Free Clear Laundry Detergent 73 oz", "all", 11.0)
        preference = _seed(
            base_item="laundry detergent",
            candidates=[tide, gain, all_free],
            usual_identity="tide",
            item_name="All Free Clear laundry detergent",
            brand="all",
            variant="free clear",
        )
        _approve(preference, gain)
        cache = RetailProductCache.query.one()
        payload = json.loads(cache.response_json)
        payload["selected_product"] = all_free
        payload["alternatives"] = [tide, gain]
        payload["selection_confidence"] = "high"
        payload["needs_user_choice"] = False
        cache.response_json = json.dumps(payload)
        db.session.commit()

        item = build_verified_walmart_cart()["cart_items"][0]

        assert item["selected_product"]["us_item_id"] == "all"
        assert item["preference"] is None
        assert item["substitution"] is None


def test_remove_substitute_restores_choice_required() -> None:
    _setup()
    with app.app_context():
        tide = _candidate("Tide Original Laundry Detergent 92 oz", "tide", 14.0, "out_of_stock")
        gain = _candidate("Gain Original Laundry Detergent 88 oz", "gain", 12.0)
        preference = _seed(base_item="laundry detergent", candidates=[tide, gain], usual_identity="tide")
        substitution = _approve(preference, gain)
        assert build_verified_walmart_cart()["cart_items"][0]["substituted"] is True

        deleted = remove_product_substitution(substitution.id, base_item="laundry detergent")
        item = build_verified_walmart_cart()["cart_items"][0]

        assert deleted == 1
        assert item["selected_product"] is None
        assert item["needs_user_choice"] is True


def test_forgetting_preference_also_removes_substitutions() -> None:
    _setup()
    with app.app_context():
        usual = _candidate("Usual Shampoo 12 oz", "usual", 8.0)
        substitute = _candidate("Substitute Shampoo 12 oz", "sub", 7.0)
        preference = _seed(base_item="shampoo", candidates=[usual, substitute], usual_identity="usual")
        _approve(preference, substitute)
        assert RetailProductSubstitution.query.count() == 1

        forget_product_preference("shampoo", "usual")

        assert RetailProductSubstitution.query.count() == 0


def test_substitution_api_saves_and_removes_verified_relationship() -> None:
    _setup()
    with app.app_context():
        tide = _candidate("Tide Original Laundry Detergent 92 oz", "tide", 14.0, "out_of_stock")
        gain = _candidate("Gain Original Laundry Detergent 88 oz", "gain", 12.0)
        _seed(base_item="laundry detergent", candidates=[tide, gain], usual_identity="tide")

    saved = app.test_client().post("/api/retail/product-substitution", json={
        "base_item": "laundry detergent",
        "retailer": "walmart",
        "store_id": "357",
        "product_identity": "gain",
    })
    assert saved.status_code == 200
    body = saved.get_json() or {}
    assert body["product_detail_calls"] == 0
    assert body["substitution"]["approval_type"] == "explicit"

    removed = app.test_client().delete("/api/retail/product-substitution", json={
        "base_item": "laundry detergent",
        "substitution_id": body["substitution"]["id"],
    })
    assert removed.status_code == 200
    assert (removed.get_json() or {})["deleted"] == 1
