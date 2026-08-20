from __future__ import annotations

import os
from typing import Any

import pytest

from services.retail import (
    RetailLocationMismatchError,
    RetailStore,
    SerpApiKeyRequired,
    ShoppingRequirement,
    WalmartSerpApiProvider,
    get_retail_provider,
)
from services.retail.walmart_serpapi import assess_selection, score_product
from services.retail.walmart_serpapi import _extract_package_size


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


STORE = RetailStore(
    store_id="357",
    name="Walmart Versailles",
    address="1003 W Newton St, Versailles, MO 65084",
    postal_code="65084",
    verified=True,
)


def test_find_stores_returns_candidates_without_auto_selecting() -> None:
    session = FakeSession([
        FakeResponse([
            {"store_id": "357", "postal_code": "65084", "address": "1003 W Newton St, Versailles, MO 65084"},
            {"store_id": "51", "postal_code": "65251", "address": "1701 N Bluff St, Fulton, MO 65251"},
        ])
    ])
    provider = WalmartSerpApiProvider(api_key="", session=session)

    stores = provider.find_stores(postal_code="65084")

    assert stores == [STORE]
    assert session.calls[0]["params"] if "params" in session.calls[0] else True


def test_missing_key_stops_before_product_request() -> None:
    session = FakeSession([])
    provider = WalmartSerpApiProvider(api_key="", session=session)

    with pytest.raises(SerpApiKeyRequired, match="SERPAPI_API_KEY_REQUIRED"):
        provider.search_products(ShoppingRequirement("milk", "milk"), store=STORE)

    assert session.calls == []


def test_search_uses_verified_store_and_normalizes_supported_fields() -> None:
    session = FakeSession([
        FakeResponse({
            "search_information": {"location": {"store_id": "357", "postal_code": "65084", "city": "Versailles"}},
            "organic_results": [{
                "us_item_id": "123",
                "product_id": "ABC",
                "upc": "000111",
                "title": "Jif Creamy Peanut Butter, 40 oz Jar",
                "out_of_stock": False,
                "primary_offer": {"offer_price": 6.97},
                "product_page_url": "https://www.walmart.com/ip/123",
            }],
        })
    ])
    provider = WalmartSerpApiProvider(api_key="secret", session=session)
    requirement = ShoppingRequirement(
        item_name="Jif creamy peanut butter",
        base_item="peanut butter",
        brand="Jif",
        variant="creamy",
        requested_package_size="40 oz",
    )

    result = provider.search_products(requirement, store=STORE)

    params = session.calls[0]["params"]
    assert params["engine"] == "walmart"
    assert params["query"] == "Jif creamy peanut butter"
    assert params["store_id"] == "357"
    assert len(result.products) == 1
    product = result.products[0]
    assert product.verified_location is True
    assert product.store == STORE
    assert product.product_id == "ABC"
    assert product.us_item_id == "123"
    assert product.upc == "000111"
    assert product.package_size == "40 oz"
    assert product.price == 6.97
    assert product.availability == "in_stock"
    assert product.price_type == "unknown"
    assert product.source == "serpapi_walmart"


def test_search_rejects_provider_location_mismatch() -> None:
    session = FakeSession([
        FakeResponse({
            "search_information": {"location": {"store_id": "51", "postal_code": "65251"}},
            "organic_results": [],
        })
    ])
    provider = WalmartSerpApiProvider(api_key="secret", session=session)

    with pytest.raises(RetailLocationMismatchError, match="Requested Walmart store 357"):
        provider.search_products(ShoppingRequirement("milk", "milk"), store=STORE)


def test_product_detail_uses_same_store_and_preserves_unknowns() -> None:
    session = FakeSession([
        FakeResponse({
            "search_information": {"location": {"store_id": "357", "postal_code": "65084"}},
            "product_result": {
                "us_item_id": "123",
                "product_id": "ABC",
                "title": "Example Shampoo",
                "specification_highlights": [{"key": "Brand", "value": "Example"}],
                "price_map": {"price": 4.25},
                "in_stock": True,
                "pickup_option": {"available": True, "location": "Versailles Supercenter"},
                "product_page_url": "https://www.walmart.com/ip/123",
            },
        })
    ])
    provider = WalmartSerpApiProvider(api_key="secret", session=session)

    product = provider.get_product("123", store=STORE, requested_query="shampoo")

    params = session.calls[0]["params"]
    assert params == {
        "engine": "walmart_product",
        "product_id": "123",
        "store_id": "357",
        "api_key": "secret",
    }
    assert product.brand == "Example"
    assert product.upc is None
    assert product.package_size is None
    assert product.availability == "in_stock"
    assert product.price_type == "pickup"


def test_explicit_specificity_is_authoritative_for_relevance() -> None:
    provider = WalmartSerpApiProvider(api_key="secret", session=FakeSession([]))
    requirement = ShoppingRequirement(
        item_name="Jif creamy peanut butter",
        base_item="peanut butter",
        brand="Jif",
        variant="creamy",
    )
    matching = provider._normalize_search_product(
        {"title": "Jif Creamy Peanut Butter 40 oz", "primary_offer": {"offer_price": 7.0}},
        requirement.search_query(),
        STORE,
    )
    generic = provider._normalize_search_product(
        {"title": "Great Value Creamy Peanut Butter 40 oz", "primary_offer": {"offer_price": 3.0}},
        requirement.search_query(),
        STORE,
    )

    assert score_product(requirement, matching)[0] is True
    assert score_product(requirement, generic)[0] is False


def test_exact_tokens_do_not_match_embedded_words() -> None:
    provider = WalmartSerpApiProvider(api_key="secret", session=FakeSession([]))
    requirement = ShoppingRequirement("milk", "milk")
    regular = provider._normalize_search_product(
        {"title": "Great Value Whole Vitamin D Milk, Gallon", "primary_offer": {"offer_price": 4.22}},
        "milk",
        STORE,
    )
    oatmilk = provider._normalize_search_product(
        {"title": "Oatly Original Oatmilk, Dairy-Free Milk, 64 fl oz", "primary_offer": {"offer_price": 5.27}},
        "milk",
        STORE,
    )

    assert score_product(requirement, regular)[0] is True
    assert score_product(requirement, oatmilk)[0] is True
    assert score_product(requirement, regular)[1] > score_product(requirement, oatmilk)[1]
    assert any("embedded_base_compound:oatmilk" in reason for reason in score_product(requirement, oatmilk)[2])

    ham = ShoppingRequirement("ham", "ham")
    shampoo = provider._normalize_search_product(
        {"title": "Daily Moisture Shampoo 12 oz", "primary_offer": {"offer_price": 4.0}},
        "ham",
        STORE,
    )
    assert score_product(ham, shampoo)[0] is False


def test_tied_generic_candidates_require_user_choice() -> None:
    provider = WalmartSerpApiProvider(api_key="secret", session=FakeSession([]))
    requirement = ShoppingRequirement("milk", "milk")
    candidates = [
        provider._normalize_search_product(
            {"title": "Lactaid Whole Milk 96 oz", "primary_offer": {"offer_price": 6.38}}, "milk", STORE,
        ),
        provider._normalize_search_product(
            {"title": "Great Value Whole Vitamin D Milk Gallon", "primary_offer": {"offer_price": 4.22}}, "milk", STORE,
        ),
    ]

    selected, alternatives, confidence, needs_choice = assess_selection(requirement, candidates)

    assert selected is None
    assert [product.title for product in alternatives] == [product.title for product in candidates]
    assert confidence == "low"
    assert needs_choice is True


def test_explicit_brand_variant_and_package_select_automatically() -> None:
    provider = WalmartSerpApiProvider(api_key="secret", session=FakeSession([]))
    requirement = ShoppingRequirement(
        "Jif creamy peanut butter",
        "peanut butter",
        brand="Jif",
        variant="creamy",
        requested_package_size="40 oz",
    )
    candidates = [
        provider._normalize_search_product(
            {"title": "Skippy Creamy Peanut Butter 40 oz", "primary_offer": {"offer_price": 5.0}}, requirement.search_query(), STORE,
        ),
        provider._normalize_search_product(
            {"title": "Jif Crunchy Peanut Butter 40 oz", "primary_offer": {"offer_price": 6.0}}, requirement.search_query(), STORE,
        ),
        provider._normalize_search_product(
            {"title": "Jif Creamy Peanut Butter 40 oz", "primary_offer": {"offer_price": 7.0}}, requirement.search_query(), STORE,
        ),
    ]

    selected, _, confidence, needs_choice = assess_selection(requirement, candidates)

    assert selected and selected.title == "Jif Creamy Peanut Butter 40 oz"
    assert confidence == "high"
    assert needs_choice is False


def test_router_returns_walmart_provider() -> None:
    provider = get_retail_provider("Walmart", api_key="test", session=FakeSession([]))
    assert isinstance(provider, WalmartSerpApiProvider)


def test_package_container_is_preserved_when_title_supports_it() -> None:
    assert _extract_package_size("Great Value Whole Vitamin D Milk, Gallon") == "Gallon"
    assert _extract_package_size("Marketside Fresh Organic Bananas, Bunch") == "Bunch"


def test_pytest_defaults_to_non_production_database() -> None:
    assert os.environ.get("RUNG_DB_PATH")
    assert os.environ["RUNG_DB_PATH"] != os.path.join(os.getcwd(), "rung_finance.db")
