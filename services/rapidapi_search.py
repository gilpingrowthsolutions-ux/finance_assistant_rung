"""
RapidAPI Real-Time Product Search — live price lookup with local SQLite caching.
================================================================================

Three-stage resolution for every ingredient keyword:

  Stage 1 — RapidPriceCache Hit
      Query the local ``rapid_price_cache`` table for a fresh entry
      (younger than CACHE_TTL_HOURS). If found, return it directly.

  Stage 2 — Live RapidAPI Call
      If the local cache is stale or missing, call the Real-Time Product
      Search API on RapidAPI, persist the top match into
      ``rapid_price_cache``, and return the parsed result.

  Stage 3 — StorePriceCache Fallback
      If the RapidAPI call fails (timeout, rate-limit, network error),
      fall back to the existing ``store_price_cache`` table (populated
      by the Kroger ingest or manual uploads).

Usage
-----
    from services.rapidapi_search import search_local_product

    result = search_local_product("cilantro")
    if result:
        print(result["title"], result["price"], result["store_name"])
    else:
        print("No product found — cart will use estimated pricing.")

The module is designed to be a drop-in complement to
``services/store_api.py``; call it first for the freshest online prices,
then fall through to the Kroger-based cache if it returns ``None``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from services.usage_meter import check_optional_operation, estimate_usage_cost, record_usage_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RAPIDAPI_HOST = "real-time-product-search.p.rapidapi.com"
RAPIDAPI_SEARCH_URL = f"https://{RAPIDAPI_HOST}/search"
RAPIDAPI_TIMEOUT_S = 12  # seconds before the API call is aborted
CACHE_TTL_HOURS = 24  # re-fetch cached terms older than this
MAX_CACHED_RESULTS = 5  # keep this many results per keyword in the cache

LOGGER = logging.getLogger("rapidapi_search")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_rapidapi_key() -> Optional[str]:
    """Read the RapidAPI key from the environment.

    Returns ``None`` with a logged warning if the key is missing so
    callers can degrade gracefully.
    """
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        LOGGER.warning(
            "RAPIDAPI_KEY not set in environment. "
            "Create a .env file with RAPIDAPI_KEY or export it manually."
        )
        return None
    return key


def _parse_price(raw_price: Any) -> float:
    """Convert a price value (string or number) to a float.

    Handles formats like ``"$1.82"``, ``"$0.03/fl oz"``, ``3.06``, etc.
    Returns ``0.0`` if no numeric price can be extracted.
    """
    if raw_price is None:
        return 0.0
    if isinstance(raw_price, (int, float)):
        return float(raw_price)
    # String: strip leading currency symbols and trailing unit text
    text = str(raw_price).strip()
    # Extract the first decimal number (possibly with leading $ or other currency)
    match = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)", text)
    if match:
        return float(match.group(1))
    # Fallback: try float conversion directly
    cleaned = re.sub(r"[^0-9.]", "", text)
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        LOGGER.debug("Could not parse price from %r — returning 0.0", raw_price)
        return 0.0


def _parse_products(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract and normalise product entries from a RapidAPI response.

    Handles the known response shape:
    ``{"status": "OK", "data": {"products": [...]}}``
    """
    data_wrapper = raw.get("data", {})
    # 'data' can be either a dict with a 'products' key or a direct list
    if isinstance(data_wrapper, dict):
        products = data_wrapper.get("products", [])
    elif isinstance(data_wrapper, list):
        products = data_wrapper
    else:
        products = []

    result: List[Dict[str, Any]] = []
    for prod in products:
        if not isinstance(prod, dict):
            continue
        title = (prod.get("product_title") or "").strip()
        if not title:
            continue

        price_str = prod.get("price") or prod.get("product_price") or "0"
        # Also check for nested offer.price
        offer = prod.get("offer") or {}
        if isinstance(offer, dict) and (not price_str or price_str == "0"):
            price_str = offer.get("price", price_str)

        price = _parse_price(price_str)
        # Skip entries with no real price
        if price <= 0:
            continue

        store = (prod.get("store_name") or prod.get("store") or "").strip()
        url = (prod.get("product_page_url") or prod.get("product_url") or "").strip()
        pkg = (prod.get("package_size") or prod.get("size") or prod.get("product_size") or "").strip()
        img = (prod.get("image_url") or prod.get("image") or prod.get("product_image") or "").strip()

        result.append({
            "title": title,
            "price": round(price, 2),
            "store_name": store or "Online Retailer",
            "product_url": url,
            "package_size": pkg,
            "image_url": img,
        })

    return result


# ---------------------------------------------------------------------------
# Cache helpers (operate on the Flask-SQLAlchemy model from app.py)
# ---------------------------------------------------------------------------


def _fetch_from_cache(app, keyword: str, store_name_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the freshest cached product for *keyword*, or ``None``."""
    from models import RapidPriceCache

    with app.app_context():
        threshold = datetime.utcnow() - timedelta(hours=CACHE_TTL_HOURS)
        query = RapidPriceCache.query.filter(
            RapidPriceCache.ingredient_keyword == keyword.lower().strip(),
            RapidPriceCache.last_updated >= threshold,
            RapidPriceCache.price > 0,
        ).order_by(RapidPriceCache.last_updated.desc())

        # If a store hint is given, prefer results from that store
        if store_name_hint:
            query = query.filter(
                RapidPriceCache.store_name.ilike(f"%{store_name_hint}%")
            )

        row = query.first()
        if row:
            return {
                "title": row.title,
                "price": row.price,
                "store_name": row.store_name,
                "product_url": row.product_url,
                "package_size": row.package_size or "",
                "image_url": row.image_url or "",
                "source": "rapid_cache",
            }
        return None


def _save_to_cache(app, keyword: str, product: Dict[str, Any]) -> None:
    """Upsert the best product match into ``RapidPriceCache``.

    Uses (ingredient_keyword, title) as the dedup key so re-runs
    update the price instead of creating duplicates.
    """
    from extensions import db
    from models import RapidPriceCache

    with app.app_context():
        kw = keyword.lower().strip()
        existing = RapidPriceCache.query.filter_by(
            ingredient_keyword=kw,
            title=product["title"][:300],
        ).first()

        now = datetime.utcnow()
        if existing is None:
            row = RapidPriceCache(
                ingredient_keyword=kw,
                title=product["title"][:300],
                price=product["price"],
                store_name=product.get("store_name", "")[:100],
                package_size=product.get("package_size", "")[:100],
                image_url=product.get("image_url", "")[:500],
                product_url=product.get("product_url", "")[:500],
                location="",
                last_updated=now,
            )
            db.session.add(row)
        else:
            existing.price = product["price"]
            existing.store_name = product.get("store_name", "")[:100]
            existing.package_size = product.get("package_size", "")[:100]
            existing.image_url = product.get("image_url", "")[:500]
            existing.product_url = product.get("product_url", "")[:500]
            existing.last_updated = now
        db.session.commit()


def _fallback_to_local_cache(
    app,
    keyword: str,
    store_name_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fall back to the ``StorePriceCache`` table when RapidAPI is unavailable.

    Returns the cheapest cached product for *keyword*, optionally preferring
    rows whose store name matches *store_name_hint*.
    """
    from models import StorePriceCache

    with app.app_context():
        kw = keyword.lower().strip()
        query = StorePriceCache.query.filter(
            StorePriceCache.item_keyword == kw,
            StorePriceCache.price > 0,
        )
        if store_name_hint:
            query = query.filter(
                StorePriceCache.store_name.ilike(f"%{store_name_hint}%")
            )

        rows = query.order_by(StorePriceCache.price.asc()).all()
        if rows:
            best = rows[0]
            return {
                "title": best.product_title,
                "price": best.price,
                "store_name": best.store_name,
                "package_size": best.package_size or "",
                "product_url": "",
                "source": "store_cache_fallback",
            }

        if store_name_hint:
            rows = (
                StorePriceCache.query.filter(
                    StorePriceCache.item_keyword == kw,
                    StorePriceCache.price > 0,
                )
                .order_by(StorePriceCache.price.asc())
                .all()
            )
            if rows:
                best = rows[0]
                return {
                    "title": best.product_title,
                    "price": best.price,
                    "store_name": best.store_name,
                    "package_size": best.package_size or "",
                    "product_url": "",
                    "source": "store_cache_fallback",
                }

        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration helpers (convert RapidAPI results for resolve_terms/pick_best)
# ---------------------------------------------------------------------------


def rapid_result_to_product_dict(
    rapid_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert a ``search_local_product()`` result to the product dict shape
    expected by ``store_api.pick_best()`` and the cart pipeline.

    The returned dict has keys: ``product_title``, ``price``,
    ``package_size``, ``image_url``, ``store_item_id``, ``is_store_brand``,
    ``source``, and ``store_name`` (extra field for cart display).
    """
    return {
        "product_title": rapid_result.get("title", ""),
        "price": rapid_result.get("price", 0.0),
        "package_size": rapid_result.get("package_size", ""),
        "image_url": rapid_result.get("image_url", ""),
        "store_item_id": "",
        "is_store_brand": False,
        "source": rapid_result.get("source", "rapid_api"),
        "store_name": rapid_result.get("store_name", "Online Retailer"),
    }


def search_local_product(
    ingredient_keyword: str,
    store_name: Optional[str] = None,
    location: str = "Eldon, MO",
    app=None,
) -> Optional[Dict[str, Any]]:
    """Search for a product by keyword and return the best match.

    Resolution order:

    1. **RapidPriceCache** — local SQLite table. If a fresh entry
       (≤ *CACHE_TTL_HOURS* old) exists for *ingredient_keyword*,
       return it immediately — no network call.

    2. **RapidAPI Real-Time Product Search** — live API call. Query
       with parameters ``q``, ``country='us'``, ``limit=5``. The top
       matching product is parsed, cached, and returned.

    3. **StorePriceCache fallback** — if the RapidAPI call fails
       (timeout, HTTP error, missing credentials, etc.), consult
       the existing ``store_price_cache`` table (Kroger ingest data).

    4. If all stages return nothing, return ``None`` — the caller
       should use estimated pricing.

    Parameters
    ----------
    ingredient_keyword:
        Clean ingredient keyword (e.g. ``"cilantro"``, ``"chicken breast"``).
    store_name:
        Optional store name to narrow results (e.g. ``"Walmart"``,
        ``"Gerbes"``). If provided, ``q`` becomes ``"{keyword} {store}"``.
    location:
        Location context string (default ``"Eldon, MO"``). Currently
        advisory; the RapidAPI endpoint does not accept a full location
        param, but this may be used in future iterations.
    app:
        Flask application instance (needed for ``app.app_context()``).
        If omitted, the function will try to import it from ``app.py``.

    Returns
    -------
    Dict or ``None``
        A product dict with keys: ``title``, ``price``, ``store_name``,
        ``product_url``, ``source``. ``source`` is one of
        ``"rapid_cache"``, ``"rapid_api"``, ``"store_cache_fallback"``.
    """
    # --- Lazy-load app if not provided ---
    if app is None:
        try:
            from app import app as _flask_app

            app = _flask_app
        except ImportError:
            LOGGER.error("Could not import Flask app — provide it explicitly as the `app` kwarg.")
            return None

    kw = ingredient_keyword.strip().lower()
    if not kw:
        return None

    # --- Stage 1: RapidPriceCache ---
    cached = _fetch_from_cache(app, kw, store_name_hint=store_name)
    if cached:
        record_usage_event(
            category="retail_cache",
            provider="walmart_serpapi",
            operation="product_lookup",
            success=True,
            external_call=False,
            request_count=1,
            cache_status="hit",
        )
        LOGGER.debug("RapidPriceCache hit for '%s'", kw)
        return cached
    record_usage_event(
        category="retail_cache",
        provider="walmart_serpapi",
        operation="product_lookup",
        success=True,
        external_call=False,
        request_count=1,
        cache_status="miss",
    )

    # --- Stage 2: Live RapidAPI ---
    gate = check_optional_operation(None, "retail_external_call")
    if not gate.get("allowed", True):
        record_usage_event(
            category="retail_provider",
            provider="walmart_serpapi",
            operation="product_search_blocked",
            success=False,
            external_call=False,
            request_count=1,
            cost_status="unknown",
            metadata={"code": gate.get("code")},
        )
        return _fallback_to_local_cache(app, kw, store_name_hint=store_name)

    api_key = _get_rapidapi_key()
    if api_key:
        try:
            # Build the query string; storage keys use underscores, API expects spaces.
            search_kw = kw.replace("_", " ")
            q = f"{search_kw} {store_name}" if store_name else search_kw
            params: Dict[str, Any] = {
                "q": q,
                "country": "us",
                "limit": MAX_CACHED_RESULTS,
            }
            headers = {
                "x-rapidapi-key": api_key,
                "x-rapidapi-host": RAPIDAPI_HOST,
            }

            resp = requests.get(
                RAPIDAPI_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=RAPIDAPI_TIMEOUT_S,
            )
            resp.raise_for_status()
            raw = resp.json()

            cost = estimate_usage_cost(
                category="retail_provider",
                provider="walmart_serpapi",
                operation="product_search",
                request_count=1,
            )
            record_usage_event(
                category="retail_provider",
                provider="walmart_serpapi",
                operation="product_search",
                success=True,
                external_call=True,
                request_count=1,
                estimated_cost_micros=cost.get("estimated_cost_micros"),
                cost_status=cost.get("cost_status"),
                cost_rate_key=cost.get("cost_rate_key"),
            )

            products = _parse_products(raw)
            if products:
                # Pick the cheapest product as the top match
                best = min(products, key=lambda p: p["price"])
                best["source"] = "rapid_api"
                # Cache it for next time
                _save_to_cache(app, kw, best)
                return best

            LOGGER.info("RapidAPI returned 0 products for '%s'", kw)

        except requests.exceptions.Timeout:
            record_usage_event(
                category="retail_provider",
                provider="walmart_serpapi",
                operation="product_search",
                success=False,
                external_call=True,
                request_count=1,
                cost_status="unknown",
                metadata={"error": "timeout"},
            )
            LOGGER.warning("RapidAPI request timed out for '%s'", kw)
        except requests.exceptions.RequestException as exc:
            record_usage_event(
                category="retail_provider",
                provider="walmart_serpapi",
                operation="product_search",
                success=False,
                external_call=True,
                request_count=1,
                cost_status="unknown",
                metadata={"error": type(exc).__name__},
            )
            LOGGER.warning("RapidAPI request failed for '%s': %s", kw, exc)
        except Exception as exc:
            record_usage_event(
                category="retail_provider",
                provider="walmart_serpapi",
                operation="product_search",
                success=False,
                external_call=True,
                request_count=1,
                cost_status="unknown",
                metadata={"error": type(exc).__name__},
            )
            LOGGER.warning("RapidAPI parse error for '%s': %s", kw, exc)

    # --- Stage 3: StorePriceCache fallback ---
    fallback = _fallback_to_local_cache(app, kw, store_name_hint=store_name)
    if fallback:
        LOGGER.debug("StorePriceCache fallback for '%s': %s", kw, fallback["title"])
        return fallback

    # --- All stages exhausted ---
    return None


def search_multiple_products(
    keywords: List[str],
    store_name: Optional[str] = None,
    location: str = "Eldon, MO",
    app=None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Batch version of ``search_local_product``.

    Resolves every keyword in *keywords* and returns a dict mapping
    each keyword to its best product result (or ``None``).

    This is more efficient than calling ``search_local_product`` in a
    loop because it internally lazy-loads the Flask app once.
    """
    if app is None:
        try:
            from app import app as _flask_app

            app = _flask_app
        except ImportError:
            LOGGER.error("Could not import Flask app.")
            return {k: None for k in keywords}

    return {
        kw: search_local_product(kw, store_name=store_name, location=location, app=app)
        for kw in keywords
    }
