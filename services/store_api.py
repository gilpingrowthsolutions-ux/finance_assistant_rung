"""
Store API Resolver — live retail price resolution with local SQLite caching.
=============================================================================

Two-stage resolution for every ingredient keyword:

  Stage 1 — Cache Hit
      Query StorePriceCache for rows matching the keyword. If at least one
      product was cached within CACHE_TTL_HOURS (default 24), return those
      rows directly — no network call.

  Stage 2 — Live API
      If the cache is empty or stale, call the Kroger Product Search API,
      persist fresh results into StorePriceCache, and return them.

After resolution, the caller selects the cheapest product that matches
the user's store-brand preference via ``pick_best()``.

Usage
-----
    from services.store_api import resolve_terms, pick_best
    results = resolve_terms(app, ["chicken breast", "brown rice"])
    best = pick_best(results["chicken breast"], prefer_store_brand=True)
    print(best["product_title"], best["price"])
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Kroger API constants — keep in sync with scripts/ingest_store_prices.py
# ---------------------------------------------------------------------------
KROGER_TOKEN_URL = "https://api.kroger.com/v1/connect/oauth2/token"
KROGER_PRODUCTS_URL = "https://api.kroger.com/v1/products"
DEFAULT_SCOPE = "product.compact"
DEFAULT_LIMIT = 5
CACHE_TTL_HOURS = 24  # re-fetch cached terms older than this

# Known store-brand substrings (lowercased)
STORE_BRAND_TOKENS = (
    "kroger",
    "private selection",
    "simple truth",
    "kroger naturals",
    "kroger brand",
    "ps",
    "st",
)

LOGGER = logging.getLogger("store_api")

# ---------------------------------------------------------------------------
# Kroger OAuth2 Client (lightweight, no pip SDK dependency)
# ---------------------------------------------------------------------------


class KrogerClient:
    """Lightweight Kroger Developer API client with token caching.

    Same interface as the one in ``scripts/ingest_store_prices.py`` but
    self-contained so ``services/store_api.py`` has zero import-time
    coupling to the Flask app or the CLI tool.
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[Dict[str, Any]] = None

    # ---- public API ----

    def search_products(
        self,
        term: str,
        location_id: str,
        limit: int = DEFAULT_LIMIT,
    ) -> Optional[List[Dict[str, Any]]]:
        """Search Kroger products for *term* at *location_id*.

        Returns a list of normalised product dicts on success, ``None``
        on transport / auth failure.
        """
        token = self._ensure_token()
        if not token:
            return None
        try:
            resp = self._get(token, term, location_id, limit)
        except Exception as exc:
            LOGGER.warning('Products search "%s" error: %s', term, exc)
            return None
        if resp.status_code == 401:
            self._token = None  # force token refresh
            token = self._ensure_token()
            if not token:
                return None
            try:
                resp = self._get(token, term, location_id, limit)
            except Exception as exc:
                LOGGER.warning('Products search retry "%s" error: %s', term, exc)
                return None
        if not resp.ok:
            LOGGER.warning(
                'Products search "%s" HTTP %s: %s',
                term,
                resp.status_code,
                resp.text[:200],
            )
            return None
        try:
            body = resp.json()
        except ValueError:
            LOGGER.warning('Products search "%s" returned non-JSON', term)
            return None
        return [self._normalise(p) for p in body.get("data", [])]

    # ---- internal helpers ----

    def _ensure_token(self) -> Optional[str]:
        now = datetime.utcnow()
        if self._token and self._token.get("expires_at", now) > now:
            return self._token["access_token"]
        import requests

        try:
            resp = requests.post(
                KROGER_TOKEN_URL,
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials", "scope": DEFAULT_SCOPE},
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as exc:
            LOGGER.error("Kroger token request failed: %s", exc)
            return None
        data = resp.json()
        if "access_token" not in data:
            LOGGER.error("Token response missing access_token: %s", data)
            return None
        expires_in = int(data.get("expires_in", 1800))
        data["expires_at"] = datetime.utcnow() + timedelta(
            seconds=max(60, expires_in - 60)
        )
        self._token = data
        return data["access_token"]

    def _get(self, token: str, term: str, location_id: str, limit: int):
        import requests

        return requests.get(
            KROGER_PRODUCTS_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={
                "filter.term": term,
                "filter.locationId": location_id,
                "filter.limit": limit,
            },
            timeout=10,
        )

    @staticmethod
    def _normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
        description = (raw.get("description") or "").strip()
        brand = (raw.get("brand") or "").strip()
        items = raw.get("items") or [{}]
        item0 = items[0] if items else {}
        price_info = item0.get("price") or {}
        regular_price = price_info.get("regular")
        if regular_price is None:
            regular_price = price_info.get("promo") or 0
        size = (item0.get("size") or "").strip()
        image_url = ""
        images = raw.get("images") or []
        if images:
            sizes = images[0].get("sizes") or []
            if sizes:
                image_url = sizes[0].get("url") or ""
        # Extract upc / SKU
        upc = raw.get("upc") or item0.get("upc") or ""
        return {
            "product_title": description,
            "brand": brand,
            "price": float(regular_price or 0),
            "package_size": size,
            "image_url": image_url,
            "upc": upc,
            "store_item_id": item0.get("itemId", ""),
            "is_store_brand": _is_store_brand(brand, description),
        }


def _is_store_brand(brand: str, description: str) -> bool:
    """Heuristic: is this a store-brand product?"""
    text = f"{brand} {description}".lower()
    if not text.strip():
        return False
    return any(tok in text for tok in STORE_BRAND_TOKENS)


# ---------------------------------------------------------------------------
# Two-stage resolver
# ---------------------------------------------------------------------------


def _get_kroger_credentials() -> Optional[Dict[str, str]]:
    """Read Kroger API credentials from environment.

    Returns ``None`` with a logged warning if either is missing so
    callers can fall back to cache-only mode without crashing.
    """
    cid = os.environ.get("KROGER_CLIENT_ID", "").strip()
    csec = os.environ.get("KROGER_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        LOGGER.warning(
            "KROGER_CLIENT_ID / KROGER_CLIENT_SECRET not set — "
            "live API resolution disabled, using cache only."
        )
        return None
    return {"client_id": cid, "client_secret": csec}


def _fetch_cache_products(app, keyword: str) -> List[Any]:
    """Query StorePriceCache for *keyword*, newest first."""
    from app import StorePriceCache

    with app.app_context():
        threshold = datetime.utcnow() - timedelta(hours=CACHE_TTL_HOURS)
        rows = (
            StorePriceCache.query.filter(
                StorePriceCache.item_keyword == keyword,
                StorePriceCache.last_updated >= threshold,
                StorePriceCache.price > 0,
            )
            .order_by(StorePriceCache.last_updated.desc())
            .all()
        )
        return rows


def _upsert_product(
    app, store_name: str, keyword: str, product: Dict[str, Any]
) -> None:
    """Insert or update a single StorePriceCache row.

    Uses (store_name, item_keyword, product_title) as the upsert key so
    re-runs don't duplicate the same product.
    """
    from app import db, StorePriceCache

    with app.app_context():
        existing = StorePriceCache.query.filter_by(
            store_name=store_name,
            item_keyword=keyword,
            product_title=product["product_title"][:200],
        ).first()
        now = datetime.utcnow()
        if existing is None:
            row = StorePriceCache(
                store_name=store_name,
                item_keyword=keyword,
                product_title=product["product_title"][:200],
                price=product["price"],
                unit="each",
                package_size=product["package_size"][:100]
                if product.get("package_size")
                else None,
                image_url=product["image_url"][:500]
                if product.get("image_url")
                else None,
                retailer="kroger",
                is_store_brand=product.get("is_store_brand", False),
                last_updated=now,
            )
            db.session.add(row)
        else:
            existing.price = product["price"]
            existing.package_size = product["package_size"][:100] if product.get("package_size") else None
            existing.image_url = product["image_url"][:500] if product.get("image_url") else None
            existing.is_store_brand = product.get("is_store_brand", False)
            existing.last_updated = now
        db.session.commit()


def resolve_terms(
    app,
    terms: List[str],
    store_name: str = "Kroger",
    location_id: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    force_refresh: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Resolve ingredient keywords to real retail products.

    Two-stage resolution per term:
      1. Check local ``StorePriceCache`` for fresh entries (< CACHE_TTL_HOURS old).
      2. If stale/missing and Kroger credentials are configured, call the
         live Kroger API, cache results, and return.

    Parameters
    ----------
    app:
        The Flask application instance (needed for ``app.app_context()``).
    terms:
        List of clean ingredient keywords (e.g. ``["chicken_breast", "rice"]``).
    store_name:
        Store name to tag cached entries with.
    location_id:
        Kroger location ID. Required for live API queries. If omitted and
        the cache is empty, falls back gracefully.
    limit:
        Max results per term from the Kroker API.
    force_refresh:
        If ``True``, skip the cache check and always hit the API.

    Returns
    -------
    Dict[str, List[Dict]]
        Mapping of *term → list of normalised product dicts*. Each product
        dict has keys: ``product_title``, ``price``, ``package_size``,
        ``image_url``, ``store_item_id``, ``is_store_brand``, ``source``
        (``"cache"`` or ``"api"``).

        Terms with zero matches return an empty list — the caller should
        fall back to estimated pricing for those.
    """
    from app import StorePriceCache

    result: Dict[str, List[Dict[str, Any]]] = {}
    creds = _get_kroger_credentials()
    client = KrogerClient(**creds) if creds else None
    needs_api = bool(client and location_id)

    for term in terms:
        kw = term.lower().strip()
        if not kw:
            result[term] = []
            continue

        products: List[Dict[str, Any]] = []

        # --- Stage 1: Local cache ---
        if not force_refresh:
            cached = _fetch_cache_products(app, kw)
            for row in cached:
                products.append(
                    {
                        "product_title": row.product_title,
                        "price": row.price,
                        "package_size": row.package_size or "",
                        "image_url": row.image_url or "",
                        "store_item_id": "",
                        "is_store_brand": row.is_store_brand,
                        "source": "cache",
                    }
                )

        # --- Stage 2: Live API ---
        if needs_api and (force_refresh or not products):
            api_products = client.search_products(kw, location_id, limit=limit)
            if api_products:
                for p in api_products:
                    if p["price"] <= 0:
                        continue
                    _upsert_product(app, store_name, kw, p)
                    products.append(
                        {
                            "product_title": p["product_title"],
                            "price": p["price"],
                            "package_size": p.get("package_size", ""),
                            "image_url": p.get("image_url", ""),
                            "store_item_id": p.get("store_item_id", ""),
                            "is_store_brand": p.get("is_store_brand", False),
                            "source": "api",
                        }
                    )

        result[term] = products

    return result


def pick_best(
    products: List[Dict[str, Any]],
    prefer_store_brand: bool = True,
) -> Optional[Dict[str, Any]]:
    """Select the cheapest matching product from a list.

    If *prefer_store_brand* is ``True``, store-brand products are
    preferred over name-brand ones. Falls back to any product if
    no store-brand match exists.

    If *prefer_store_brand* is ``False``, name-brand products are
    preferred (store-brand excluded). Falls back to any product if
    no name-brand match exists.

    Returns ``None`` when *products* is empty.
    """
    if not products:
        return None
    if prefer_store_brand:
        store_brand = [p for p in products if p.get("is_store_brand")]
        if store_brand:
            return min(store_brand, key=lambda p: p["price"])
    else:
        # User explicitly wants name-brand — exclude store brands.
        name_brand = [p for p in products if not p.get("is_store_brand")]
        if name_brand:
            return min(name_brand, key=lambda p: p["price"])
    return min(products, key=lambda p: p["price"])
