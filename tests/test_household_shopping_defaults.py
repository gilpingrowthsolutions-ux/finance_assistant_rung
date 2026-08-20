from __future__ import annotations

import json
import os
from datetime import datetime, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import (
    GroceryItem,
    HouseholdShoppingDefault,
    RetailProductCache,
    RetailProductPreference,
    RetailProductSubstitution,
)
from services.retail import ShoppingRequirement
from services.retail.cart import SELECTION_POLICY_VERSION, build_verified_walmart_cart
from services.retail.preferences import get_product_preference
from services.household_context import household_id as current_household_id


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()


def _save_defaults(payload: dict):
    return app.test_client().post("/api/settings/household-shopping-defaults", json=payload)


def _get_defaults():
    return app.test_client().get("/api/settings/household-shopping-defaults")


def _candidate(title: str, identity: str, price: float) -> dict:
    return {
        "requested_query": "peanut butter",
        "retailer": "walmart",
        "store": {
            "store_id": "357",
            "name": "Walmart - Versailles",
            "address": "1003 W Newton St, Versailles, MO 65084",
            "postal_code": "65084",
            "verified": True,
        },
        "product_id": f"product-{identity}",
        "us_item_id": identity,
        "upc": f"upc-{identity}",
        "title": title,
        "brand": None,
        "variant": None,
        "package_size": "16 oz",
        "price": price,
        "availability": "in_stock",
        "price_type": "unknown",
        "product_url": f"https://www.walmart.com/ip/{identity}",
        "source": "serpapi_walmart",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "verified_location": True,
    }


def _seed_cached_cart_with_exact_preference() -> None:
    requirement = ShoppingRequirement("peanut butter", "peanut butter")
    jif = _candidate("Jif Creamy Peanut Butter 16 oz", "jif", 3.50)
    skippy = _candidate("Skippy Crunchy Peanut Butter 16 oz", "skippy", 3.25)

    with app.app_context():
        db.session.add(GroceryItem(
            household_id=current_household_id(),
            item_name=requirement.item_name,
            store_name="Walmart",
            shopping_requirement_json=json.dumps(requirement.__dict__),
        ))
        payload = {
            "requirement": requirement.__dict__,
            "selected_product": None,
            "alternatives": [jif, skippy],
            "candidates": [jif, skippy],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "selection_confidence": "low",
            "needs_user_choice": True,
            "selection_policy_version": SELECTION_POLICY_VERSION,
        }
        db.session.add(RetailProductCache(
            retailer="walmart",
            store_id="357",
            store_name="Walmart - Versailles",
            store_address="1003 W Newton St, Versailles, MO 65084",
            requested_query=requirement.search_query(),
            base_item="peanut butter",
            title="Unresolved: peanut butter",
            provider_source="serpapi_walmart",
            verified_location=True,
            response_json=json.dumps(payload),
            retrieved_at=datetime.now(timezone.utc),
        ))
        preference = RetailProductPreference(
            household_id=current_household_id(),
            base_item="peanut butter",
            normalized_base_item="peanut butter",
            preference_type="usual",
            preferred_product_title=jif["title"],
            upc=jif["upc"],
            retailer="walmart",
            retailer_product_id=jif["product_id"],
            retailer_us_item_id="jif",
            source="user_explicit",
        )
        db.session.add(preference)
        db.session.flush()
        db.session.add(RetailProductSubstitution(
            household_id=current_household_id(),
            base_item="peanut butter",
            normalized_base_item="peanut butter",
            preferred_preference_id=preference.id,
            substitute_product_title=skippy["title"],
            substitute_upc=skippy["upc"],
            retailer="walmart",
            retailer_product_id=skippy["product_id"],
            retailer_us_item_id=skippy["us_item_id"],
            approval_type="explicit",
        ))
        db.session.commit()


def test_household_defaults_empty_state() -> None:
    _setup()
    resp = _get_defaults()
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("preferences") == {}
    assert body.get("shopping_style") is None
    definitions = body.get("definitions") or {}
    assert len(definitions.get("questions") or []) >= 8


def test_household_defaults_save_retrieve_edit_and_dont_care() -> None:
    _setup()
    initial = _save_defaults({
        "preferences": {
            "milk_type": "two_percent",
            "peanut_butter_texture": "dont_care",
            "bread_type": None,
        },
        "shopping_style": "store_brands_ok",
    })
    assert initial.status_code == 200

    fetched = _get_defaults().get_json() or {}
    assert fetched["preferences"]["milk_type"] == "two_percent"
    assert fetched["preferences"]["peanut_butter_texture"] == "dont_care"
    assert "bread_type" not in fetched["preferences"]
    assert fetched["shopping_style"] == "store_brands_ok"

    edited = _save_defaults({
        "preferences": {
            "milk_type": "skim",
            "peanut_butter_texture": None,
            "bread_type": "wheat",
        },
        "shopping_style": "prefer_brands_when_possible",
    })
    assert edited.status_code == 200
    updated = _get_defaults().get_json() or {}
    assert updated["preferences"]["milk_type"] == "skim"
    assert updated["preferences"]["bread_type"] == "wheat"
    assert "peanut_butter_texture" not in updated["preferences"]
    assert updated["shopping_style"] == "prefer_brands_when_possible"


def test_household_defaults_invalid_key_rejected() -> None:
    _setup()
    resp = _save_defaults({"preferences": {"made_up_key": "value"}})
    assert resp.status_code == 400
    with app.app_context():
        assert HouseholdShoppingDefault.query.count() == 0


def test_household_defaults_invalid_value_rejected() -> None:
    _setup()
    resp = _save_defaults({"preferences": {"milk_type": "ultra_magic"}})
    assert resp.status_code == 400
    with app.app_context():
        assert HouseholdShoppingDefault.query.count() == 0


def test_household_defaults_shopping_style_persistence() -> None:
    _setup()
    saved = _save_defaults({"shopping_style": "save_most"})
    assert saved.status_code == 200
    reloaded = _get_defaults().get_json() or {}
    assert reloaded.get("shopping_style") == "save_most"


def test_household_defaults_skip_state_distinct_from_dont_care() -> None:
    _setup()
    resp = _save_defaults({
        "preferences": {
            "soda_preference": "dont_care",
            "coffee_roast": None,
        }
    })
    assert resp.status_code == 200
    payload = _get_defaults().get_json() or {}
    prefs = payload.get("preferences") or {}
    assert prefs.get("soda_preference") == "dont_care"
    assert "coffee_roast" not in prefs


def test_household_defaults_do_not_change_exact_prefs_or_substitutions() -> None:
    _setup()
    _seed_cached_cart_with_exact_preference()

    with app.app_context():
        before_pref = RetailProductPreference.query.one()
        before_sub = RetailProductSubstitution.query.one()

    resp = _save_defaults({
        "preferences": {
            "peanut_butter_texture": "crunchy",
            "milk_type": "lactose_free",
        },
        "shopping_style": "save_most",
    })
    assert resp.status_code == 200

    with app.app_context():
        after_pref = RetailProductPreference.query.one()
        after_sub = RetailProductSubstitution.query.one()
        assert after_pref.id == before_pref.id
        assert after_pref.preferred_product_title == before_pref.preferred_product_title
        assert after_sub.id == before_sub.id
        assert after_sub.substitute_product_title == before_sub.substitute_product_title


def test_household_defaults_preserve_retailer_specific_exact_preference_isolation() -> None:
    _setup()
    with app.app_context():
        db.session.add_all([
            RetailProductPreference(
                household_id=current_household_id(),
                base_item="milk",
                normalized_base_item="milk",
                preference_type="usual",
                preferred_product_title="Walmart Milk",
                retailer="walmart",
                retailer_us_item_id="wmilk",
                source="user_explicit",
            ),
            RetailProductPreference(
                household_id=current_household_id(),
                base_item="milk",
                normalized_base_item="milk",
                preference_type="usual",
                preferred_product_title="Kroger Milk",
                retailer="kroger",
                retailer_us_item_id="kmilk",
                source="user_explicit",
            ),
        ])
        db.session.commit()

    with app.app_context():
        before = get_product_preference("milk", retailer="kroger")
        assert before is not None and before.preferred_product_title == "Kroger Milk"

    resp = _save_defaults({"preferences": {"milk_type": "whole"}})
    assert resp.status_code == 200

    with app.app_context():
        after = get_product_preference("milk", retailer="kroger")
        assert after is not None and after.preferred_product_title == "Kroger Milk"


def test_household_defaults_have_no_ranking_effect_regression() -> None:
    _setup()
    _seed_cached_cart_with_exact_preference()

    with app.app_context():
        before = build_verified_walmart_cart()
        before_item = before["cart_items"][0]
        before_selected = (before_item.get("selected_product") or {}).get("us_item_id")
        before_confidence = before_item.get("selection_confidence")

    saved = _save_defaults({
        "preferences": {
            "peanut_butter_texture": "crunchy",
            "bread_type": "wheat",
            "milk_type": "non_dairy",
        },
        "shopping_style": "do_not_switch_usuals_for_savings",
    })
    assert saved.status_code == 200

    with app.app_context():
        after = build_verified_walmart_cart()
        after_item = after["cart_items"][0]
        after_selected = (after_item.get("selected_product") or {}).get("us_item_id")
        after_confidence = after_item.get("selection_confidence")

    assert before_selected == "jif"
    assert after_selected == before_selected
    assert after_confidence == before_confidence
    assert after_item.get("preferred_product") is True
