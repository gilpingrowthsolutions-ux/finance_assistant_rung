from __future__ import annotations

from typing import Any, Mapping, Optional

import requests

from services.kroger_api import get_kroger_token
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

KROGER_API_URL = "https://api.kroger.com/v1"


class KrogerProvider(RetailProvider):
    retailer = "kroger"

    def __init__(self, *, session: Optional[requests.Session] = None, timeout: int = 20) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout

    def find_stores(self, *, postal_code: str) -> list[RetailStore]:
        target = str(postal_code or "").strip()
        if not target:
            return []
        payload = self._request(
            "/locations",
            {"filter.zipCode.near": target, "filter.radiusInMiles": 100, "filter.limit": 20},
        )
        rows = payload.get("data") or []
        return [self._store_from_location(row, target) for row in rows if isinstance(row, dict)]

    def search_products(self, requirement: ShoppingRequirement, *, store: RetailStore, limit: int = 20) -> ProductSearchResult:
        response_store = self._verified_store(store)
        query = requirement.search_query()
        payload = self._request(
            "/products",
            {"filter.term": query, "filter.locationId": store.store_id, "filter.limit": max(1, limit)},
        )
        rows = payload.get("data") or []
        products = [
            self._normalize_product(row, query, response_store)
            for row in rows
            if isinstance(row, dict) and str(row.get("description") or "").strip()
        ]
        return ProductSearchResult(store, response_store, products, len(rows))

    def get_product(self, product_id: str, *, store: RetailStore, requested_query: str) -> RetailProduct:
        response_store = self._verified_store(store)
        payload = self._request(f"/products/{str(product_id).strip()}", {"filter.locationId": store.store_id})
        raw = payload.get("data") or {}
        if not isinstance(raw, dict) or not str(raw.get("description") or "").strip():
            raise RetailProviderError("Kroger product detail returned no product data.")
        return self._normalize_product(raw, requested_query, response_store)

    def _verified_store(self, store: RetailStore) -> RetailStore:
        if not store.verified or not store.store_id.strip():
            raise RetailConfigurationError("A verified Kroger location_id is required.")
        payload = self._request(f"/locations/{store.store_id.strip()}", {})
        raw = payload.get("data") or {}
        if not isinstance(raw, dict) or str(raw.get("locationId") or "").strip() != store.store_id.strip():
            raise RetailLocationMismatchError("Kroger returned a different location than requested.")
        return self._store_from_location(raw, store.postal_code)

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = get_kroger_token()
        if not token:
            raise RetailConfigurationError("KROGER_CREDENTIALS_REQUIRED")
        response = self.session.get(
            KROGER_API_URL + path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            timeout=self.timeout,
        )
        if response.status_code == 404 and path.startswith("/locations/"):
            raise RetailLocationMismatchError(f"Kroger location {path.rsplit('/', 1)[-1]} was not found.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RetailProviderError("Kroger returned a non-object response.")
        return payload

    @staticmethod
    def _store_from_location(raw: Mapping[str, Any], fallback_postal: Optional[str]) -> RetailStore:
        address = raw.get("address") or {}
        address_text = ", ".join(
            part for part in (
                address.get("addressLine1"),
                address.get("city"),
                " ".join(part for part in (address.get("state"), address.get("zipCode")) if part),
            ) if str(part or "").strip()
        )
        store_id = str(raw.get("locationId") or "").strip()
        if not store_id:
            raise RetailProviderError("Kroger location response missing locationId.")
        chain = str(raw.get("chain") or "Kroger").strip()
        return RetailStore(
            store_id=store_id,
            name=str(raw.get("name") or chain).strip() or chain,
            address=address_text or None,
            postal_code=str(address.get("zipCode") or fallback_postal or "").strip() or None,
            verified=True,
        )

    def _normalize_product(self, raw: Mapping[str, Any], query: str, store: RetailStore) -> RetailProduct:
        items = raw.get("items") or []
        item = items[0] if items and isinstance(items[0], dict) else {}
        price = item.get("price") or {}
        promo = _optional_float(price.get("promo"))
        regular = _optional_float(price.get("regular"))
        fulfillment = item.get("fulfillment") or {}
        availability = "unknown"
        fulfillment_flags = {
            key: value for key, value in fulfillment.items()
            if key in {"inStore", "curbside", "delivery", "shipToHome"} and isinstance(value, bool)
        } if isinstance(fulfillment, dict) else {}
        product_id = _optional_text(raw.get("productId"))
        return RetailProduct.now(
            requested_query=query,
            retailer=self.retailer,
            store=store,
            product_id=product_id,
            us_item_id=_optional_text(item.get("itemId")),
            upc=_optional_text(raw.get("upc") or item.get("upc")),
            title=str(raw.get("description") or "").strip(),
            brand=_optional_text(raw.get("brand")),
            variant=None,
            package_size=_optional_text(item.get("size")),
            price=promo if promo and promo > 0 else regular,
            availability=availability,
            price_type="in_store" if fulfillment_flags.get("inStore") is True else "unknown",
            product_url=_optional_text(raw.get("productPageURI")),
            source="kroger_api",
            verified_location=True,
            regular_price=regular,
            promo_price=promo if promo and promo > 0 else None,
            fulfillment=fulfillment_flags or None,
        )


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None