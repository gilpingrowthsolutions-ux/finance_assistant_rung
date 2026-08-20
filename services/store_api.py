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
import math
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from services.usage_meter import check_optional_operation, estimate_usage_cost, record_usage_event

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

_COUNT_UNITS = {"ct", "count", "ea", "each", "unit", "item", "items", "stick", "sticks", "pk", "pkg", "pack", "packs"}
_MASS_UNITS = {"lb", "lbs", "pound", "pounds", "oz", "ounce", "ounces", "g", "gram", "grams", "kg", "kilogram", "kilograms"}
_VOLUME_UNITS = {"fl oz", "floz", "fluid ounce", "fluid ounces", "ml", "milliliter", "milliliters", "l", "liter", "liters", "cup", "cups", "qt", "quart", "quarts", "pt", "pint", "pints", "gal", "gallon", "gallons"}

_OPTIONAL_MODIFIER_TOKENS = {
    "unsalted", "salted", "boneless", "skinless", "extra", "virgin",
    "ground", "fresh", "organic", "raw",
}

_FINISHED_FOOD_TOKENS = {
    "pizza", "cracker", "crackers", "popcorn", "dinner", "meal", "sandwich",
    "chips", "cookie", "cookies", "biscuit", "wrap", "bowl", "frozen",
    "entree", "snack", "flavored", "flavour", "flavor", "flavouring",
}

_LIKELY_PREPARED_QUERY_TOKENS = {
    "pizza", "dinner", "meal", "snack", "cracker", "cookies", "chips",
    "entree", "frozen",
}

_RELEVANCE_MIN_SCORE = 65
_RELEVANCE_TIE_WINDOW = 15


def _keyword_tokens(keyword: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (keyword or "").lower()) if len(t) >= 3]


def _tokenize_text(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def _keyword_phrase(keyword: str) -> str:
    return re.sub(r"\s+", " ", (keyword or "").replace("_", " ").strip().lower())


def _required_keyword_tokens(keyword: str) -> List[str]:
    toks = _keyword_tokens(keyword)
    required = [t for t in toks if t not in _OPTIONAL_MODIFIER_TOKENS]
    return required if required else toks


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, tuple):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [value]
    return [str(value)]


def _product_text_fields(product: Dict[str, Any]) -> Dict[str, str]:
    title = str(product.get("product_title", "") or "")
    brand = str(product.get("brand", "") or "")
    department = str(product.get("department", "") or "")
    product_type = str(product.get("product_type", "") or "")
    subcategory = str(product.get("subcategory", "") or "")
    categories = " ".join(_as_list(product.get("categories")))
    full = " ".join([title, brand, department, product_type, subcategory, categories]).strip().lower()
    return {
        "title": title.lower(),
        "brand": brand.lower(),
        "department": department.lower(),
        "product_type": product_type.lower(),
        "subcategory": subcategory.lower(),
        "categories": categories.lower(),
        "full": full,
    }


def _looks_like_prepared_food(text_tokens: List[str]) -> bool:
    return any(tok in _FINISHED_FOOD_TOKENS for tok in text_tokens)


def score_product_relevance(keyword: str, product: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic relevance score for ingredient -> product matching.

    Returns:
      {
        "valid": bool,
        "score": int,
        "reasons": [str, ...],
      }
    """
    phrase = _keyword_phrase(keyword)
    if not phrase:
        return {"valid": True, "score": 0, "reasons": ["empty_keyword"]}

    required = _required_keyword_tokens(keyword)
    optional = [t for t in _keyword_tokens(keyword) if t in _OPTIONAL_MODIFIER_TOKENS]
    fields = _product_text_fields(product)
    title_tokens = set(_tokenize_text(fields["title"]))
    full_tokens = set(_tokenize_text(fields["full"]))
    reasons: List[str] = []
    score = 0

    missing_required = [t for t in required if t not in full_tokens]
    if missing_required:
        return {
            "valid": False,
            "score": 0,
            "reasons": ["missing_required_tokens:" + ",".join(missing_required)],
        }

    if fields["title"].strip() == phrase:
        score += 140
        reasons.append("exact_title_match")
    elif phrase in fields["title"]:
        score += 105
        reasons.append("phrase_in_title")
    elif phrase in fields["full"]:
        score += 70
        reasons.append("phrase_in_metadata")

    if all(t in title_tokens for t in required):
        score += 65
        reasons.append("all_required_tokens_in_title")
    else:
        score += 35
        reasons.append("all_required_tokens_in_full_text")

    optional_hits = [t for t in optional if t in full_tokens]
    if optional_hits:
        score += min(15, len(optional_hits) * 4)
        reasons.append("optional_modifiers:" + ",".join(optional_hits))

    keyword_tokens = set(_keyword_tokens(keyword))
    prepared_query = any(t in _LIKELY_PREPARED_QUERY_TOKENS for t in keyword_tokens)
    prepared_candidate = _looks_like_prepared_food(list(full_tokens))
    if prepared_candidate and not prepared_query:
        score -= 130
        reasons.append("prepared_food_penalty")

    if "dr" in full_tokens and "pepper" in full_tokens and "black" in keyword_tokens:
        score -= 160
        reasons.append("brand_soda_conflict")

    if score < _RELEVANCE_MIN_SCORE:
        reasons.append("below_relevance_threshold")
        return {"valid": False, "score": score, "reasons": reasons}

    return {"valid": True, "score": score, "reasons": reasons}


def rank_product_candidates(keyword: str, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for p in products or []:
        res = score_product_relevance(keyword, p)
        ranked.append({
            "product_title": p.get("product_title", ""),
            "score": int(res.get("score", 0) or 0),
            "valid": bool(res.get("valid", False)),
            "reasons": list(res.get("reasons", [])),
        })
    ranked.sort(key=lambda x: (x["score"], x["valid"]), reverse=True)
    return ranked


def _normalize_package_unit(raw_unit: str) -> str:
    u = re.sub(r"\s+", " ", (raw_unit or "").strip().lower())
    if u in {"count", "cnt", "ct", "ea", "each", "unit", "item", "items"}:
        return "count"
    if u in {"stick", "sticks"}:
        return "count"
    if u in {"pk", "pkg", "pack", "packs"}:
        return "count"
    if u in {"lb", "lbs", "pound", "pounds"}:
        return "mass_oz"
    if u in {"oz", "ounce", "ounces"}:
        # Ambiguous between weight and fluid ounce. Keep it parseable and
        # allow compatibility with either mass or volume requirements.
        return "oz_ambiguous"
    if u in {"g", "gram", "grams"}:
        return "mass_oz"
    if u in {"kg", "kilogram", "kilograms"}:
        return "mass_oz"
    if u in {"fl oz", "floz", "fluid ounce", "fluid ounces", "ml", "milliliter", "milliliters", "l", "liter", "liters", "cup", "cups", "qt", "quart", "quarts", "pt", "pint", "pints", "gal", "gallon", "gallons"}:
        return "volume_floz"
    return "unknown"


def _unit_to_standard_amount(value: float, raw_unit: str) -> float:
    u = re.sub(r"\s+", " ", (raw_unit or "").strip().lower())
    if u in {"count", "cnt", "ct", "ea", "each", "unit", "item", "items", "stick", "sticks", "pk", "pkg", "pack", "packs"}:
        return value
    if u in {"lb", "lbs", "pound", "pounds"}:
        return value * 16.0
    if u in {"oz", "ounce", "ounces"}:
        return value
    if u in {"g", "gram", "grams"}:
        return value * 0.03527396
    if u in {"kg", "kilogram", "kilograms"}:
        return value * 35.27396
    if u in {"fl oz", "floz", "fluid ounce", "fluid ounces"}:
        return value
    if u in {"ml", "milliliter", "milliliters"}:
        return value * 0.033814
    if u in {"l", "liter", "liters"}:
        return value * 33.814
    if u in {"cup", "cups"}:
        return value * 8.0
    if u in {"qt", "quart", "quarts"}:
        return value * 32.0
    if u in {"pt", "pint", "pints"}:
        return value * 16.0
    if u in {"gal", "gallon", "gallons"}:
        return value * 128.0
    return 0.0


def _parse_package_size(package_size: str) -> Dict[str, Any]:
    text = re.sub(r"\s+", " ", (package_size or "").strip().lower())
    if not text:
        return {"ok": False, "uncertain": True, "reason": "missing_package_size"}

    # Keep longer tokens first to avoid partial matches like "gal" -> "g".
    unit_pattern = (
        r"fl\s*oz|floz|fluid\s+ounces?|"
        r"milliliters?|milliliter|ml|"
        r"gallons?|gallon|gal|"
        r"quarts?|quart|qt|"
        r"pints?|pint|pt|"
        r"liters?|liter|"
        r"kilograms?|kilogram|kg|"
        r"pounds?|pound|lbs?|lb|"
        r"ounces?|ounce|oz|"
        r"grams?|gram|"
        r"cups?|cup|"
        r"count|ct|each|ea|items?|item|units?|unit|sticks?|pk|pkg|packs?|pack|"
        r"l|g"
    )

    m = re.search(rf"(\d+(?:\.\d+)?)\s*(?:x|×)\s*(\d+(?:\.\d+)?)\s*({unit_pattern})\b", text)
    if m:
        pack_count = float(m.group(1))
        per_pack = float(m.group(2))
        raw_unit = m.group(3)
    else:
        m = re.search(rf"(\d+(?:\.\d+)?)\s*({unit_pattern})\b\s*(?:x|×)\s*(\d+(?:\.\d+)?)", text)
        if m:
            per_pack = float(m.group(1))
            raw_unit = m.group(2)
            pack_count = float(m.group(3))
        else:
            m = re.search(rf"(\d+(?:\.\d+)?)\s*({unit_pattern})\b", text)
            if not m:
                return {"ok": False, "uncertain": True, "reason": "unparseable_package_size", "raw": package_size}
            per_pack = float(m.group(1))
            raw_unit = m.group(2)
            pack_count = 1.0

    unit_kind = _normalize_package_unit(raw_unit)
    standard_per_pack = _unit_to_standard_amount(per_pack, raw_unit)
    if standard_per_pack <= 0:
        return {"ok": False, "uncertain": True, "reason": "unknown_unit", "raw": package_size}

    total_standard = standard_per_pack * max(pack_count, 1.0)
    return {
        "ok": True,
        "uncertain": False,
        "raw": package_size,
        "pack_count": pack_count,
        "per_pack_amount": per_pack,
        "raw_unit": raw_unit,
        "unit_kind": unit_kind,
        "total_standard_qty": total_standard,
    }


def _dimension_compatible(required_dimension: Optional[str], unit_kind: str) -> bool:
    if not required_dimension:
        return True
    if required_dimension == "count":
        return unit_kind == "count"
    if required_dimension == "mass":
        return unit_kind in {"mass_oz", "oz_ambiguous"}
    if required_dimension == "volume":
        return unit_kind in {"volume_floz", "oz_ambiguous"}
    return False


def _rank_with_policy(candidates: List[Dict[str, Any]], net_needed: Optional[float]) -> Dict[str, Any]:
    if not candidates:
        return None
    need = float(net_needed or 0.0)

    def _key(c):
        total_cost = c["price"] * c["packages_to_buy"]
        overbuy = max(0.0, (c.get("total_supplied", 0.0) - need))
        return (round(total_cost, 6), round(overbuy, 6), c["packages_to_buy"], c["price"])

    return min(candidates, key=_key)

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

        # Storage keys use underscores; the API expects space-separated words.
        search_term = term.replace("_", " ")
        return requests.get(
            KROGER_PRODUCTS_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={
                "filter.term": search_term,
                "filter.locationId": location_id,
                "filter.limit": limit,
            },
            timeout=10,
        )

    @staticmethod
    def _normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
        description = (raw.get("description") or "").strip()
        brand = (raw.get("brand") or "").strip()
        categories = raw.get("categories") or []
        aisle_locations = raw.get("aisleLocations") or []
        department = ""
        if aisle_locations and isinstance(aisle_locations, list):
            first_aisle = aisle_locations[0] or {}
            department = (first_aisle.get("description") or "").strip()
        product_type = (raw.get("productType") or "").strip()
        subcategory = (raw.get("subcategory") or "").strip()
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
            "categories": categories,
            "department": department,
            "product_type": product_type,
            "subcategory": subcategory,
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
    from models import StorePriceCache

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
    from extensions import db
    from models import StorePriceCache

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
    from models import StorePriceCache

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
            record_usage_event(
                category="retail_cache",
                provider="kroger_api",
                operation="product_lookup",
                success=True,
                external_call=False,
                request_count=1,
                cache_status="hit" if bool(cached) else "miss",
                force_refresh=force_refresh,
            )
            for row in cached:
                products.append(
                    {
                        "product_title": row.product_title,
                        "price": row.price,
                        "package_size": row.package_size or "",
                        "image_url": row.image_url or "",
                        "store_item_id": "",
                        "is_store_brand": row.is_store_brand,
                        "source": "kroger_cache",
                        "source_store_name": row.store_name or store_name,
                            "categories": [],
                            "department": "",
                            "product_type": "",
                            "subcategory": "",
                    }
                )

        # --- Stage 2: Live API ---
        if needs_api and (force_refresh or not products):
            gate = check_optional_operation(None, "retail_external_call")
            if not gate.get("allowed", True):
                record_usage_event(
                    category="retail_provider",
                    provider="kroger_api",
                    operation="product_search_blocked",
                    success=False,
                    external_call=False,
                    request_count=1,
                    cost_status="unknown",
                    metadata={"code": gate.get("code")},
                )
                result[term] = products
                continue

            api_products = client.search_products(kw, location_id, limit=limit)
            search_cost = estimate_usage_cost(
                category="retail_provider",
                provider="kroger_api",
                operation="product_search",
                request_count=1,
            )
            record_usage_event(
                category="retail_provider",
                provider="kroger_api",
                operation="product_search",
                success=True,
                external_call=True,
                request_count=1,
                force_refresh=force_refresh,
                estimated_cost_micros=search_cost.get("estimated_cost_micros"),
                cost_status=search_cost.get("cost_status"),
                cost_rate_key=search_cost.get("cost_rate_key"),
                metadata={"result_count": len(api_products or [])},
            )
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
                            "source": "kroger_api",
                            "source_store_name": store_name,
                            "categories": p.get("categories") or [],
                            "department": p.get("department", ""),
                            "product_type": p.get("product_type", ""),
                            "subcategory": p.get("subcategory", ""),
                        }
                    )

        result[term] = products

    return result


def pick_best(
    products: List[Dict[str, Any]],
    prefer_store_brand: bool = True,
    keyword: Optional[str] = None,
    net_needed: Optional[float] = None,
    required_dimension: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Select the best matching product from a list.

    If *prefer_store_brand* is ``True``, store-brand products are
    preferred over name-brand ones. Falls back to any product if
    no store-brand match exists.

    If *prefer_store_brand* is ``False``, name-brand products are
    preferred (store-brand excluded). Falls back to any product if
    no name-brand match exists.

    Package adequacy is evaluated when *net_needed* is provided. Only
    parseable, dimension-compatible package sizes are considered
    "satisfying". Unknown package sizes remain selectable only as
    uncertain fallbacks and are never treated as satisfying.

    Returns ``None`` when *products* is empty or no product matches the
    requested *keyword*.
    """
    if not products:
        return None

    if keyword:
        relevance_rank = rank_product_candidates(keyword, products)
        score_map: Dict[str, Dict[str, Any]] = {}
        for p in products:
            k = f"{p.get('product_title','')}|{p.get('brand','')}|{p.get('package_size','')}|{p.get('price','')}"
            if k not in score_map:
                score_map[k] = score_product_relevance(keyword, p)

        matching = []
        for p in products:
            k = f"{p.get('product_title','')}|{p.get('brand','')}|{p.get('package_size','')}|{p.get('price','')}"
            r = score_map.get(k, {"valid": False, "score": 0, "reasons": ["no_score"]})
            if r.get("valid"):
                q = dict(p)
                q["relevance_score"] = int(r.get("score", 0) or 0)
                q["relevance_reasons"] = list(r.get("reasons", []))
                matching.append(q)

        if not matching:
            LOGGER.info("pick_best unresolved keyword=%s no relevant candidates; ranked=%s", keyword, relevance_rank[:5])
            return None
        max_rel = max((int(p.get("relevance_score", 0) or 0) for p in matching), default=0)
        scoped = [p for p in matching if int(p.get("relevance_score", 0) or 0) >= max(_RELEVANCE_MIN_SCORE, max_rel - _RELEVANCE_TIE_WINDOW)]
        if not scoped:
            LOGGER.info("pick_best unresolved keyword=%s relevance below threshold; ranked=%s", keyword, relevance_rank[:5])
            return None
    else:
        scoped = list(products)

    if prefer_store_brand:
        preferred = [p for p in scoped if p.get("is_store_brand")]
        pool = preferred if preferred else scoped
    else:
        preferred = [p for p in scoped if not p.get("is_store_brand")]
        pool = preferred if preferred else scoped

    valid: List[Dict[str, Any]] = []
    uncertain: List[Dict[str, Any]] = []

    for p in pool:
        parsed = _parse_package_size(p.get("package_size", ""))
        candidate = dict(p)
        candidate["package_parse_uncertain"] = not parsed.get("ok", False)
        if not parsed.get("ok", False):
            candidate["packages_to_buy"] = 1
            candidate["total_supplied"] = None
            candidate["package_unit_kind"] = "unknown"
            uncertain.append(candidate)
            continue

        unit_kind = parsed.get("unit_kind")
        if not _dimension_compatible(required_dimension, unit_kind):
            continue

        unit_qty = float(parsed.get("total_standard_qty") or 0.0)
        if unit_qty <= 0:
            candidate["packages_to_buy"] = 1
            candidate["total_supplied"] = None
            candidate["package_unit_kind"] = unit_kind
            candidate["package_parse_uncertain"] = True
            uncertain.append(candidate)
            continue

        need = float(net_needed or 0.0)
        packages = 1
        if need > 0:
            packages = max(1, int(math.ceil(need / unit_qty)))

        candidate["packages_to_buy"] = packages
        candidate["total_supplied"] = unit_qty * packages
        candidate["package_unit_kind"] = unit_kind
        candidate["package_parse_uncertain"] = False
        valid.append(candidate)

    chosen = _rank_with_policy(valid, net_needed)
    if chosen:
        return chosen

    if uncertain:
        return min(uncertain, key=lambda p: p["price"])

    return None
