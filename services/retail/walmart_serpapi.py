from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

import requests

from services.retail.base import (
    ProductSearchResult,
    RetailConfigurationError,
    RetailLocationMismatchError,
    RetailProduct,
    RetailProvider,
    RetailProviderError,
    RetailStore,
    ShoppingRequirement,
)

SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
SERPAPI_WALMART_STORES_URL = "https://serpapi.com/walmart-stores.json"


class SerpApiKeyRequired(RetailConfigurationError):
    pass


class WalmartSerpApiProvider(RetailProvider):
    retailer = "walmart"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        timeout: int = 20,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.environ.get("SERPAPI_API_KEY", "")).strip()
        self.session = session or requests.Session()
        self.timeout = timeout

    def find_stores(self, *, postal_code: str) -> list[RetailStore]:
        target = str(postal_code or "").strip()
        if not target:
            return []
        response = self.session.get(SERPAPI_WALMART_STORES_URL, timeout=self.timeout)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RetailProviderError("SerpApi Walmart store directory returned an unexpected response.")

        stores: list[RetailStore] = []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("postal_code") or "").strip() != target:
                continue
            store_id = str(row.get("store_id") or "").strip()
            if not store_id or not store_id.isdigit():
                continue
            address = _optional_text(row.get("address"))
            stores.append(
                RetailStore(
                    store_id=store_id,
                    name=_store_name_from_address(address),
                    address=address,
                    postal_code=target,
                    verified=True,
                )
            )
        return stores

    def search_products(
        self,
        requirement: ShoppingRequirement,
        *,
        store: RetailStore,
        limit: int = 20,
    ) -> ProductSearchResult:
        self._require_key()
        self._validate_requested_store(store)
        query = requirement.search_query()
        payload = self._request(
            {
                "engine": "walmart",
                "query": query,
                "store_id": store.store_id,
                "api_key": self.api_key,
            }
        )
        response_store = self._verified_response_store(payload, store)
        raw_products = payload.get("organic_results") or []
        if not isinstance(raw_products, list):
            raw_products = []
        products = [
            self._normalize_search_product(row, query, response_store)
            for row in raw_products[: max(1, limit)]
            if isinstance(row, dict) and str(row.get("title") or "").strip()
        ]
        ranked = rank_products(requirement, products)
        return ProductSearchResult(
            requested_store=store,
            response_store=response_store,
            products=ranked,
            raw_result_count=len(raw_products),
        )

    def get_product(
        self,
        product_id: str,
        *,
        store: RetailStore,
        requested_query: str,
    ) -> RetailProduct:
        self._require_key()
        self._validate_requested_store(store)
        payload = self._request(
            {
                "engine": "walmart_product",
                "product_id": str(product_id).strip(),
                "store_id": store.store_id,
                "api_key": self.api_key,
            }
        )
        response_store = self._verified_response_store(payload, store)
        raw = payload.get("product_result") or {}
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            raise RetailProviderError("SerpApi Walmart Product returned no product result.")
        return self._normalize_product_detail(raw, requested_query, response_store)

    def _require_key(self) -> None:
        if not self.api_key:
            raise SerpApiKeyRequired("SERPAPI_API_KEY_REQUIRED")

    @staticmethod
    def _validate_requested_store(store: RetailStore) -> None:
        if not store.verified or not store.store_id.isdigit():
            raise RetailConfigurationError("A verified numeric Walmart store_id is required.")

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(SERPAPI_SEARCH_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RetailProviderError("SerpApi returned a non-object response.")
        if payload.get("error"):
            raise RetailProviderError(str(payload["error"]))
        return payload

    @staticmethod
    def _verified_response_store(payload: Mapping[str, Any], requested: RetailStore) -> RetailStore:
        search_information = payload.get("search_information") or {}
        location = search_information.get("location") if isinstance(search_information, dict) else {}
        location = location if isinstance(location, dict) else {}
        returned_id = str(location.get("store_id") or "").strip()
        if returned_id != requested.store_id:
            raise RetailLocationMismatchError(
                f"Requested Walmart store {requested.store_id}, provider returned {returned_id or 'no store_id'}."
            )
        returned_postal = _optional_text(location.get("postal_code"))
        if requested.postal_code and returned_postal and returned_postal != requested.postal_code:
            raise RetailLocationMismatchError(
                f"Requested Walmart postal code {requested.postal_code}, provider returned {returned_postal}."
            )
        return RetailStore(
            store_id=returned_id,
            name=requested.name,
            address=requested.address,
            postal_code=returned_postal or requested.postal_code,
            verified=True,
        )

    def _normalize_search_product(
        self,
        raw: Mapping[str, Any],
        query: str,
        store: RetailStore,
    ) -> RetailProduct:
        offer = raw.get("primary_offer") or {}
        offer = offer if isinstance(offer, dict) else {}
        out_of_stock = raw.get("out_of_stock")
        availability = "unknown"
        if isinstance(out_of_stock, bool):
            availability = "out_of_stock" if out_of_stock else "in_stock"
        return RetailProduct.now(
            requested_query=query,
            retailer=self.retailer,
            store=store,
            product_id=_optional_text(raw.get("product_id")),
            us_item_id=_optional_text(raw.get("us_item_id")),
            upc=_optional_text(raw.get("upc")),
            title=str(raw.get("title") or "").strip(),
            brand=_optional_text(raw.get("brand")),
            variant=None,
            package_size=_extract_package_size(str(raw.get("title") or "")),
            price=_optional_float(offer.get("offer_price")),
            availability=availability,
            price_type="unknown",
            product_url=_optional_text(raw.get("product_page_url")),
            source="serpapi_walmart",
            verified_location=True,
        )

    def _normalize_product_detail(
        self,
        raw: Mapping[str, Any],
        query: str,
        store: RetailStore,
    ) -> RetailProduct:
        specifications = raw.get("specification_highlights") or []
        spec_map = {
            str(item.get("key") or "").strip().lower(): str(item.get("value") or "").strip()
            for item in specifications
            if isinstance(item, dict)
        }
        price_map = raw.get("price_map") or {}
        price_map = price_map if isinstance(price_map, dict) else {}
        in_stock = raw.get("in_stock")
        availability = "unknown"
        if isinstance(in_stock, bool):
            availability = "in_stock" if in_stock else "out_of_stock"
        pickup = raw.get("pickup_option") or {}
        pickup = pickup if isinstance(pickup, dict) else {}
        offer_type = str(raw.get("offer_type") or "").upper()
        price_type = "unknown"
        if pickup.get("available") is True:
            price_type = "pickup"
        elif offer_type == "STORE_ONLY":
            price_type = "in_store"
        elif offer_type == "ONLINE_ONLY":
            price_type = "online"
        return RetailProduct.now(
            requested_query=query,
            retailer=self.retailer,
            store=store,
            product_id=_optional_text(raw.get("product_id")),
            us_item_id=_optional_text(raw.get("us_item_id")),
            upc=_optional_text(raw.get("upc")),
            title=str(raw.get("title") or "").strip(),
            brand=_optional_text(spec_map.get("brand")),
            variant=None,
            package_size=_optional_text(spec_map.get("product net content parent")) or _extract_package_size(str(raw.get("title") or "")),
            price=_optional_float(price_map.get("price")),
            availability=availability,
            price_type=price_type,
            product_url=_optional_text(raw.get("product_page_url")),
            source="serpapi_walmart",
            verified_location=True,
        )


def score_product(requirement: ShoppingRequirement, product: RetailProduct) -> tuple[bool, int, list[str]]:
    title = product.title.lower()
    reasons: list[str] = []
    score = 0

    base_tokens = _tokens(requirement.base_item)
    missing_base = [token for token in base_tokens if token not in _tokens(title)]
    if missing_base:
        return False, 0, ["missing_base:" + ",".join(missing_base)]
    score += 100
    reasons.append("base_item_match")

    title_tokens = _tokens(title)
    embedded_base_compounds = [
        title_token
        for title_token in title_tokens
        for base_token in base_tokens
        if title_token != base_token and len(base_token) >= 3 and base_token in title_token
    ]
    if embedded_base_compounds:
        score -= 35
        reasons.append("embedded_base_compound:" + ",".join(sorted(set(embedded_base_compounds))))

    for label, expected, weight in (("brand", requirement.brand, 80), ("variant", requirement.variant, 50)):
        if not expected:
            continue
        missing = [token for token in _tokens(expected) if token not in _tokens(title)]
        if missing:
            return False, score, [f"missing_{label}:" + ",".join(missing)]
        score += weight
        reasons.append(f"{label}_match")

    if requirement.requested_package_size:
        expected_size = _normalized_size(requirement.requested_package_size)
        actual_size = _normalized_size(product.package_size or product.title)
        if not expected_size or expected_size not in actual_size:
            return False, score, ["requested_package_mismatch"]
        score += 30
        reasons.append("requested_package_match")

    if product.availability == "in_stock":
        score += 10
    return True, score, reasons


def assess_selection(
    requirement: ShoppingRequirement,
    products: list[RetailProduct],
) -> tuple[Optional[RetailProduct], list[RetailProduct], str, bool]:
    scored: list[tuple[int, int, RetailProduct]] = []
    for index, product in enumerate(products):
        valid, score, _ = score_product(requirement, product)
        if valid:
            scored.append((score, index, product))
    scored.sort(key=lambda row: (-row[0], row[1]))
    ranked = [row[2] for row in scored]
    if not scored:
        return None, [], "low", False

    top_score = scored[0][0]
    runner_up_score = scored[1][0] if len(scored) > 1 else None
    has_explicit_constraints = bool(
        requirement.brand
        or requirement.variant
        or requirement.requested_package_size
    )
    if has_explicit_constraints:
        confidence = "high" if runner_up_score is None or top_score > runner_up_score else "medium"
        return ranked[0], ranked[1:], confidence, False

    if runner_up_score is None or top_score >= runner_up_score + 20:
        return ranked[0], ranked[1:], "high", False
    return None, ranked, "low", True


def rank_products(requirement: ShoppingRequirement, products: list[RetailProduct]) -> list[RetailProduct]:
    scored: list[tuple[int, int, RetailProduct]] = []
    for index, product in enumerate(products):
        valid, score, _ = score_product(requirement, product)
        if valid:
            scored.append((-score, index, product))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in scored]


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(value or "").lower()) if token}


def _normalized_size(value: str) -> str:
    return " ".join(re.findall(r"\d+(?:\.\d+)?|fl oz|oz|lb|ct|count|ml|l", str(value or "").lower()))


def _extract_package_size(title: str) -> Optional[str]:
    matches = re.findall(
        r"\b\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|ounces?|lb|lbs|pounds?|ct|count|ml|liters?|l|gallons?|gal)\b",
        title,
        re.IGNORECASE,
    )
    if matches:
        return matches[-1].strip()
    container = re.search(
        r"\b(?:half\s+gallon|gallons?|bunch|jars?|bottles?|loaves?|loaf|packs?)\b",
        title,
        re.IGNORECASE,
    )
    return container.group(0).strip() if container else None


def _store_name_from_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    parts = [part.strip() for part in address.replace("\xa0", " ").split(",")]
    if len(parts) >= 3:
        return f"Walmart {parts[-2]}"
    return "Walmart"


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
