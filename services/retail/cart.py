from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from extensions import db
from models import GroceryItem, HouseholdShoppingDefault, RetailProductCache
from services.household_context import household_id as current_household_id
from services.recipe_requirements import active_recipe_requirements
from services.retail import RetailProvider, RetailStore, ShoppingRequirement, get_retail_provider
from services.retail.preferences import (
    get_product_preference,
    get_product_substitutions,
    match_approved_substitution,
    match_preference,
    preference_to_dict,
    requirement_allows_saved_preference,
    substitution_to_dict,
)
from services.retail.resolution import retail_resolution_service
from services.retail.shared_foundation import normalize_query, shared_retail_foundation
from services.retail.walmart_serpapi import assess_selection
from services.retail.walmart_serpapi import WalmartSerpApiProvider
from services.usage_meter import record_usage_event

VERIFIED_WALMART_STORE = RetailStore("357", "Walmart — Versailles", "1003 W Newton St, Versailles, MO 65084", "65084", True)
VERIFIED_KROGER_STORE = RetailStore("61500116", "Gerbes Eldon", "410 E North St, Eldon, MO 65026", "65084", True)
VERIFIED_CACHE_TTL_MINUTES = 15
ALTERNATIVE_LIMIT = 4
SELECTION_POLICY_VERSION = 5
HOUSEHOLD_DEFAULT_OWNER_SCOPE = "household:default"
HOUSEHOLD_DEFAULT_KIND_CATEGORY = "category_default"
HOUSEHOLD_DEFAULT_KIND_STYLE = "shopping_style"
HOUSEHOLD_STYLE_KEY = "shopping_style"
HOUSEHOLD_DONT_CARE = "dont_care"
BUDGET_OPTIMIZER_MAX_CANDIDATES = 6
_MONEY_CENT = Decimal("0.01")
_ROUNDING = ROUND_HALF_UP


def build_verified_retail_cart(
    *,
    retailer: str,
    store: RetailStore,
    force_refresh: bool = False,
    provider: Optional[RetailProvider] = None,
    budget_limit: Optional[float] = None,
    tax_rate: float = 0.0,
    owner_scope: str = "anonymous",
) -> dict[str, Any]:
    retailer = str(retailer or "").strip().lower()
    resolver = provider or (WalmartSerpApiProvider() if retailer == "walmart" else get_retail_provider(retailer))
    provider_source = "serpapi_walmart" if retailer == "walmart" else "kroger_api"
    household_id = current_household_id()
    requirements = _active_manual_requirements() + active_recipe_requirements(household_id)
    household_defaults = _load_household_shopping_defaults()
    cart_items = []
    search_calls = detail_calls = cache_hits = 0
    exact_cache_served = 0
    search_cache_served = 0
    last_known_fallbacks = 0
    unknown_cold_misses = 0
    provider_calls_serpapi = 0
    provider_calls_kroger = 0

    for requirement in requirements:
        query = requirement.search_query()
        preference = get_product_preference(requirement.base_item, retailer=retailer) if requirement_allows_saved_preference(requirement) else None
        legacy_cached = _load_cached(
            query,
            retailer=retailer,
            store=store,
            provider_source=provider_source,
            include_stale=force_refresh,
        )
        selected = None
        alternatives: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        confidence = "low"
        needs_choice = True
        retrieved_at = datetime.now(timezone.utc).isoformat()

        exact_identity = None
        if preference is not None and (preference.retailer or "").lower() == retailer:
            exact_identity = preference.retailer_product_id or preference.retailer_us_item_id

        used_exact = False
        used_external_call = False
        counted_cache_hit = False
        exact_result: dict[str, Any] | None = None
        if exact_identity:
            exact_result = retail_resolution_service.resolve_exact(
                retailer=retailer,
                store=store,
                retailer_product_id=str(exact_identity),
                provider=resolver,
                owner_scope=owner_scope,
                explicit_live_refresh=force_refresh,
            )
            if exact_result.get("external_call"):
                detail_calls += 1
                used_external_call = True
                if provider_source == "serpapi_walmart":
                    provider_calls_serpapi += 1
                else:
                    provider_calls_kroger += 1
            exact_selected = exact_result.get("selected_product")
            if exact_selected is not None:
                selected = exact_selected
                candidates = list(exact_result.get("candidates") or [exact_selected])
                alternatives = [row for row in candidates if not _same_product(row, selected)]
                retrieved_at = selected.get("retrieved_at") or retrieved_at
                confidence = "high"
                needs_choice = False
                used_exact = selected.get("availability") != "out_of_stock"
                if not exact_result.get("external_call"):
                    exact_cache_served += 1

        if not used_exact and legacy_cached is not None and not force_refresh:
            selected, alternatives, candidates, retrieved_at, confidence, needs_choice = _cached_payload_to_selection(legacy_cached)
            if preference is None and requirement_allows_saved_preference(requirement) and confidence != "suggested":
                selected = None
                alternatives = _diverse_dicts(candidates, ALTERNATIVE_LIMIT)
                confidence = "low"
                needs_choice = True

        if not used_exact and not candidates:
            search_result = retail_resolution_service.resolve_search(
                retailer=retailer,
                store=store,
                requirement=requirement,
                provider=resolver,
                owner_scope=owner_scope,
                explicit_live_refresh=force_refresh,
            )
            if search_result.get("external_call"):
                search_calls += 1
                used_external_call = True
                if provider_source == "serpapi_walmart":
                    provider_calls_serpapi += 1
                else:
                    provider_calls_kroger += 1
            candidates = list(search_result.get("candidates") or [])
            if candidates:
                selected = None
                alternatives = _diverse_dicts(candidates, ALTERNATIVE_LIMIT)
                retrieved_at = datetime.now(timezone.utc).isoformat()
                confidence = "low"
                needs_choice = True
                if not search_result.get("external_call"):
                    search_cache_served += 1
            else:
                selected = None
                alternatives = []
                confidence = "low"
                needs_choice = True
                retrieved_at = datetime.now(timezone.utc).isoformat()

        if selected is None and legacy_cached is not None and not candidates:
            selected, alternatives, candidates, retrieved_at, confidence, needs_choice = _cached_payload_to_selection(legacy_cached)
            if preference is None and requirement_allows_saved_preference(requirement) and confidence != "suggested":
                selected = None
                alternatives = _diverse_dicts(candidates, ALTERNATIVE_LIMIT)
                confidence = "low"
                needs_choice = True
            if selected is not None:
                cache_hits += 1
                counted_cache_hit = True

        record_usage_event(
            owner_scope=owner_scope,
            category="retail_cache",
            provider=provider_source,
            operation="product_lookup",
            success=True,
            external_call=False,
            request_count=1,
            cache_status="hit" if selected is not None and not used_external_call else "miss",
            force_refresh=force_refresh,
        )

        preferred = substituted = usual_unavailable = False
        suggested = confidence == "suggested"
        applied_substitution = None
        suggestion_reason = None
        if preference is not None:
            matched = match_preference(preference, candidates, retailer=retailer)
            if matched is not None and matched.get("availability") != "out_of_stock":
                selected = _preference_identity_overlay(matched, preference)
                alternatives = _diverse_dicts([row for row in candidates if not _same_product(row, selected)], ALTERNATIVE_LIMIT)
                confidence, needs_choice, preferred = "high", False, True
            else:
                usual_unavailable = True
                substitution, substitute = match_approved_substitution(get_product_substitutions(preference.id, retailer=retailer), candidates, retailer=retailer)
                if substitution is not None and substitute is not None:
                    selected = substitute
                    alternatives = _diverse_dicts([row for row in candidates if not _same_product(row, selected)], ALTERNATIVE_LIMIT)
                    confidence, needs_choice, substituted = "high", False, True
                    applied_substitution = substitution_to_dict(substitution)
                else:
                    selected, alternatives, confidence, needs_choice = None, _diverse_dicts(candidates, ALTERNATIVE_LIMIT), "low", True

        if selected is None and candidates and not requirement_allows_saved_preference(requirement):
            explicit_match = _match_explicit_requirement(requirement, candidates)
            if explicit_match is not None:
                selected = explicit_match
                alternatives = _diverse_dicts([row for row in candidates if not _same_product(row, selected)], ALTERNATIVE_LIMIT)
                confidence = "high"
                needs_choice = False
            else:
                selected = None
                alternatives = _diverse_dicts(candidates, ALTERNATIVE_LIMIT)
                confidence = "low"
                needs_choice = False

        if selected is None and candidates and requirement_allows_saved_preference(requirement):
            has_blocking_unavailable_usual = bool(preference is not None and usual_unavailable and applied_substitution is None)
            if not has_blocking_unavailable_usual:
                suggested_product, suggestion_reason = _suggest_candidate(
                    requirement,
                    candidates,
                    retailer=retailer,
                    defaults=household_defaults,
                )
                if suggested_product is not None:
                    selected = suggested_product
                    alternatives = _diverse_dicts([row for row in candidates if not _same_product(row, selected)], ALTERNATIVE_LIMIT)
                    confidence = "suggested"
                    needs_choice = False
                    suggested = True

        if selected is not None:
            _save_cached(
                requirement,
                query,
                selected,
                alternatives,
                retrieved_at,
                confidence,
                needs_choice,
                candidates,
                retailer=retailer,
                store=store,
                provider_source=provider_source,
            )
            if not used_external_call and not counted_cache_hit:
                cache_hits += 1

        if selected is None:
            unknown_cold_misses += 1
        elif str(selected.get("data_quality") or "").upper() in {"LAST_KNOWN", "ESTIMATE"}:
            last_known_fallbacks += 1

        item = _cart_item(requirement, selected, alternatives, retrieved_at, confidence, needs_choice, preference_to_dict(preference) if preference else None, preferred, usual_unavailable, substituted, applied_substitution, suggested, suggestion_reason, store=store, provider_source=provider_source)
        item["_all_candidates"] = candidates
        cart_items.append(item)

    optimization = _optimize_cart_for_budget(
        cart_items,
        budget_limit=budget_limit,
        tax_rate=tax_rate,
        retailer=retailer,
        defaults=household_defaults,
    )
    for item in cart_items:
        item.pop("_all_candidates", None)

    subtotal = round(sum(float(item.get("estimated_price") or 0.0) for item in cart_items if item.get("resolved")), 2)
    payload = {
        "cart_items": cart_items, "subtotal": subtotal, "total_cart_cost": subtotal,
        "grocery_tax_rate": 0.0, "tax_amount": 0.0, "pantry_items_skipped": 0,
        "recipes_used": [], "store": store.to_dict(),
        "resolution_stats": {
            "total_terms": len(requirements),
            "search_calls": search_calls,
            "product_detail_calls": detail_calls,
            "verified_cache_hits": cache_hits,
            "unresolved": sum(1 for item in cart_items if not item.get("resolved")),
        },
        "cost_diagnostics": {
            "exact_sku_cache_hits": exact_cache_served,
            "search_cache_hits": search_cache_served,
            "serpapi_external_calls": provider_calls_serpapi,
            "kroger_external_calls": provider_calls_kroger,
            "external_calls_per_cart": provider_calls_serpapi + provider_calls_kroger,
            "last_known_fallbacks": last_known_fallbacks,
            "unknown_cold_misses": unknown_cold_misses,
        },
    }
    if optimization is not None:
        payload["budget_optimization"] = optimization
    return payload


def build_verified_walmart_cart(
    *,
    force_refresh: bool = False,
    provider: Optional[RetailProvider] = None,
    budget_limit: Optional[float] = None,
    tax_rate: float = 0.0,
    owner_scope: str = "anonymous",
) -> dict[str, Any]:
    return build_verified_retail_cart(
        retailer="walmart",
        store=VERIFIED_WALMART_STORE,
        force_refresh=force_refresh,
        provider=provider,
        budget_limit=budget_limit,
        tax_rate=tax_rate,
        owner_scope=owner_scope,
    )


def _active_manual_requirements(household_id: Optional[int] = None) -> list[ShoppingRequirement]:
    household_id = current_household_id() if household_id is None else int(household_id)
    rows = (
        GroceryItem.query
        .filter(GroceryItem.household_id == household_id)
        .filter(GroceryItem.is_purchased.is_(False))
        .filter(db.or_(GroceryItem.recipe_ids == "", GroceryItem.recipe_ids.is_(None)))
        .order_by(GroceryItem.id.asc())
        .all()
    )
    candidates: dict[str, tuple[tuple[int, int, int], ShoppingRequirement]] = {}
    for row in rows:
        raw: dict[str, Any] = {}
        if row.shopping_requirement_json:
            try:
                parsed = json.loads(row.shopping_requirement_json)
                raw = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                raw = {}
        if not raw:
            name = str(row.item_name or "").strip()
            raw = {"item_name": name, "base_item": name, "quantity": 1.0, "category": "General"}
        requirement = ShoppingRequirement.from_mapping(raw)
        key = " ".join(requirement.base_item.lower().split())
        score = sum(bool(value) for value in (requirement.brand, requirement.variant, requirement.unit, requirement.requested_package_size)) + int(requirement.quantity != 1.0)
        precedence = (int(bool(row.shopping_requirement_json)), score, int(row.id or 0))
        if key not in candidates or precedence > candidates[key][0]:
            candidates[key] = (precedence, requirement)
    return [value[1] for value in candidates.values()]


def _load_cached(query: str, *, retailer: str, store: RetailStore, provider_source: str, include_stale: bool = False) -> Optional[dict[str, Any]]:
    threshold = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=VERIFIED_CACHE_TTL_MINUTES)
    query_builder = RetailProductCache.query.filter_by(
        retailer=retailer,
        store_id=store.store_id,
        requested_query=query,
        verified_location=True,
        provider_source=provider_source,
    )
    if not include_stale:
        query_builder = query_builder.filter(RetailProductCache.retrieved_at >= threshold)
    row = query_builder.first()
    if row is None:
        return None
    try:
        payload = json.loads(row.response_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("selection_policy_version") != SELECTION_POLICY_VERSION:
        return None
    payload["retrieved_at"] = row.retrieved_at.replace(tzinfo=timezone.utc).isoformat()
    return payload


def _cached_payload_to_selection(cached: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], Optional[str], str, bool]:
    selected = cached.get("selected_product")
    alternatives = cached.get("alternatives") or []
    candidates = cached.get("candidates") or (([selected] if selected else []) + alternatives)
    retrieved_at = cached.get("retrieved_at")
    confidence = cached.get("selection_confidence") or "low"
    needs_choice = bool(cached.get("needs_user_choice"))
    return selected, alternatives, candidates, retrieved_at, confidence, needs_choice


def _dual_write_shared_foundation(
    *,
    retailer: str,
    store: RetailStore,
    products: list[Any],
    query: str,
    provider_source: str,
) -> None:
    shared_ids: list[str] = []
    for product in products or []:
        product_id = str(product.product_id or product.us_item_id or "").strip()
        if not product_id:
            continue
        shared_ids.append(product_id)
        shared_retail_foundation.upsert_observation(
            retailer=retailer,
            store=store,
            retailer_product_id=product_id,
            title=product.title,
            upc=product.upc,
            brand=product.brand,
            package_size=product.package_size,
            variant=product.variant,
            price=product.price,
            price_type=product.price_type,
            price_source=provider_source,
            price_confidence="provider_confirmed" if product.price is not None else None,
            availability=product.availability,
            fulfillment=product.fulfillment,
            availability_source=provider_source,
            availability_confidence="provider_confirmed" if product.availability != "unknown" else None,
            observed_at=product.retrieved_at,
        )
    if shared_ids:
        shared_retail_foundation.upsert_search_cache(
            retailer=retailer,
            store=store,
            query=query,
            retailer_product_ids=shared_ids,
            source=provider_source,
        )


def _save_cached(requirement: ShoppingRequirement, query: str, selected: Optional[dict[str, Any]], alternatives: list[dict[str, Any]], retrieved_at: str, confidence: str, needs_choice: bool, candidates: list[dict[str, Any]], *, retailer: str, store: RetailStore, provider_source: str) -> None:
    retrieved = _parse_datetime(retrieved_at)
    payload = {"requirement": requirement.__dict__, "selected_product": selected, "alternatives": alternatives, "retrieved_at": retrieved.replace(tzinfo=timezone.utc).isoformat(), "selection_confidence": confidence, "needs_user_choice": needs_choice, "selection_policy_version": SELECTION_POLICY_VERSION, "candidates": candidates}
    product = selected or {}
    values = {"store_name": store.name or retailer.title(), "store_address": store.address or "", "base_item": requirement.base_item, "product_id": product.get("product_id"), "us_item_id": product.get("us_item_id"), "title": product.get("title") or f"Unresolved: {query}", "package_size": product.get("package_size"), "price": product.get("price"), "availability": product.get("availability") or "unknown", "provider_source": provider_source, "verified_location": bool(product.get("verified_location")) if selected else True, "response_json": json.dumps(payload), "retrieved_at": retrieved}
    row = RetailProductCache.query.filter_by(retailer=retailer, store_id=store.store_id, requested_query=query).first()
    if row is None:
        db.session.add(RetailProductCache(retailer=retailer, store_id=store.store_id, requested_query=query, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.session.commit()


def _cart_item(requirement: ShoppingRequirement, selected: Optional[dict[str, Any]], alternatives: list[dict[str, Any]], retrieved_at: Optional[str], confidence: str, needs_choice: bool, preference: Optional[dict[str, Any]], preferred: bool, usual_unavailable: bool, substituted: bool, substitution: Optional[dict[str, Any]], suggested: bool, suggestion_reason: Optional[str], *, store: RetailStore, provider_source: str) -> dict[str, Any]:
    raw_quantity = requirement.quantity
    quantity_uncertain = raw_quantity is None
    package_resolution_uncertain = requirement.source_kind == "recipe"
    if raw_quantity is None or package_resolution_uncertain:
        packages_to_buy: Optional[int] = None
        quantity = 1
    else:
        quantity = max(1, int(raw_quantity))
        packages_to_buy = quantity
    if selected is None:
        return {"keyword": requirement.base_item.replace(" ", "_"), "requirement": requirement.__dict__, "selected_product": None, "alternatives": alternatives, "resolved": False, "product_label": f"Choose your {requirement.base_item}" if needs_choice else f"{requirement.item_name} — unavailable at selected store", "estimated_price": None, "unit_price": None, "packages_to_buy": packages_to_buy, "quantity_uncertain": quantity_uncertain, "package_resolution_uncertain": package_resolution_uncertain, "price_source": "unresolved", "confirmed_local_store": False, "store_name": store.name, "store_id": store.store_id, "package_size": None, "availability": "unknown", "retrieved_at": retrieved_at, "selection_confidence": confidence, "needs_user_choice": needs_choice, "preference": preference, "preferred_product": preferred, "usual_unavailable": usual_unavailable, "substituted": substituted, "substitution": substitution, "suggested": False, "suggestion_reason": None, "data_quality": "UNKNOWN", "price_freshness": "UNKNOWN", "availability_freshness": "UNKNOWN"}
    unit_price = selected.get("price")
    data_quality = str(selected.get("data_quality") or "UNKNOWN")
    estimated_price = round(float(unit_price) * quantity, 2) if (unit_price is not None and not quantity_uncertain and not package_resolution_uncertain) else None
    return {"keyword": requirement.base_item.replace(" ", "_"), "requirement": requirement.__dict__, "selected_product": selected, "alternatives": alternatives, "resolved": bool(selected.get("product_id") or selected.get("us_item_id")), "product_label": selected.get("title"), "estimated_price": estimated_price, "unit_price": unit_price, "packages_to_buy": packages_to_buy, "quantity_uncertain": quantity_uncertain, "package_resolution_uncertain": package_resolution_uncertain, "price_source": selected.get("source") or provider_source, "confirmed_local_store": bool(selected.get("verified_location")) and data_quality in {"LIVE_PROVIDER", "RECENT_CONFIRMED"}, "store_name": store.name, "store_id": store.store_id, "package_size": selected.get("package_size"), "availability": selected.get("availability") or "unknown", "fulfillment": selected.get("fulfillment"), "regular_price": selected.get("regular_price"), "promo_price": selected.get("promo_price"), "retrieved_at": retrieved_at or selected.get("retrieved_at"), "selection_confidence": confidence, "needs_user_choice": needs_choice, "preference": preference, "preferred_product": preferred, "usual_unavailable": usual_unavailable, "substituted": substituted, "substitution": substitution, "suggested": suggested, "suggestion_reason": suggestion_reason, "data_quality": data_quality, "price_freshness": selected.get("price_freshness") or "UNKNOWN", "availability_freshness": selected.get("availability_freshness") or "UNKNOWN"}


def _load_household_shopping_defaults(owner_scope: str = HOUSEHOLD_DEFAULT_OWNER_SCOPE) -> dict[str, Any]:
    rows = HouseholdShoppingDefault.query.filter_by(owner_scope=owner_scope).all()
    preferences: dict[str, str] = {}
    shopping_style = None
    for row in rows:
        if row.preference_kind == HOUSEHOLD_DEFAULT_KIND_CATEGORY:
            preferences[str(row.preference_key)] = str(row.preference_value)
        elif row.preference_kind == HOUSEHOLD_DEFAULT_KIND_STYLE and row.preference_key == HOUSEHOLD_STYLE_KEY:
            shopping_style = str(row.preference_value)
    return {
        "preferences": preferences,
        "shopping_style": shopping_style or "store_brands_ok",
    }


def _suggest_candidate(requirement: ShoppingRequirement, candidates: list[dict[str, Any]], *, retailer: str, defaults: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    available = [row for row in candidates if str(row.get("availability") or "unknown") != "out_of_stock"]
    if not available:
        return None, None
    if _household_defaults_block_suggestion(requirement, defaults):
        return None, None
    style = str(defaults.get("shopping_style") or "store_brands_ok")
    ranked = sorted(
        available,
        key=lambda row: _suggestion_sort_key(
            requirement,
            row,
            retailer=retailer,
            defaults=defaults,
            style=style,
        ),
    )
    if not ranked:
        return None, None
    reason = "ranked_by_shopping_style"
    if _candidate_matches_household_defaults(requirement, ranked[0], defaults):
        reason = "matched_household_default"
    return ranked[0], reason


def _suggestion_sort_key(requirement: ShoppingRequirement, candidate: dict[str, Any], *, retailer: str, defaults: dict[str, Any], style: str) -> tuple[Any, ...]:
    title = str(candidate.get("title") or "")
    normalized = _normalized_text(title)
    relevance = _relevance_score(requirement, normalized)
    price = _safe_float(candidate.get("price"))
    price_bucket = price if price is not None else 10_000.0
    store_brand = _is_store_brand(normalized, retailer)
    branded = 0 if store_brand else 1

    if style == "save_most":
        style_rank = (price_bucket, -branded, -relevance)
    elif style == "store_brands_ok":
        style_rank = (0 if store_brand else 1, price_bucket, -relevance)
    elif style == "prefer_brands_when_possible":
        style_rank = (0 if not store_brand else 1, price_bucket, -relevance)
    else:
        style_rank = (-relevance, price_bucket, 0 if store_brand else 1)

    pref_rank = 0 if _candidate_matches_household_defaults(requirement, candidate, defaults) else 1
    return (pref_rank, style_rank, normalized)


def _household_defaults_block_suggestion(requirement: ShoppingRequirement, defaults: dict[str, Any]) -> bool:
    values: dict[str, str] = defaults.get("preferences") or {}
    keys = _default_keys_for_requirement(requirement)
    for key in keys:
        value = str(values.get(key) or "").strip().lower()
        if key != "soda_preference" or value != "dont_buy_soda":
            continue
        return True
    return False


def _candidate_matches_household_defaults(requirement: ShoppingRequirement, candidate: dict[str, Any], defaults: dict[str, Any]) -> bool:
    values: dict[str, str] = defaults.get("preferences") or {}
    keys = _default_keys_for_requirement(requirement)
    candidate_title = _normalized_text(str(candidate.get("title") or ""))
    has_specific_default = False
    for key in keys:
        value = str(values.get(key) or "").strip().lower()
        if not value or value == HOUSEHOLD_DONT_CARE:
            continue
        tokens = _preference_tokens(key, value)
        if not tokens:
            continue
        has_specific_default = True
        if not any(token in candidate_title for token in tokens):
            return False
    return has_specific_default


def _default_keys_for_requirement(requirement: ShoppingRequirement) -> list[str]:
    text = _normalized_text(requirement.base_item)
    keys: list[str] = []
    mapping = (
        ("milk", "milk_type"),
        ("peanut butter", "peanut_butter_texture"),
        ("bread", "bread_type"),
        ("soda", "soda_preference"),
        ("coffee", "coffee_caffeine"),
        ("coffee", "coffee_roast"),
        ("yogurt", "yogurt_type"),
        ("butter", "butter_spread_type"),
        ("spread", "butter_spread_type"),
        ("lunch meat", "lunch_meat_type"),
        ("detergent", "laundry_detergent_scent"),
        ("toothpaste", "toothpaste_type"),
        ("shampoo", "shampoo_type"),
    )
    for needle, key in mapping:
        if needle in text and key not in keys:
            keys.append(key)
    return keys


def _preference_tokens(key: str, value: str) -> list[str]:
    token_map: dict[str, dict[str, list[str]]] = {
        "milk_type": {
            "whole": ["whole"],
            "two_percent": ["2%", "2 percent", "reduced fat"],
            "skim": ["skim", "fat free"],
            "lactose_free": ["lactose free"],
            "non_dairy": ["non dairy", "dairy free", "almond", "oat", "soy", "coconut"],
        },
        "peanut_butter_texture": {
            "smooth": ["smooth", "creamy"],
            "crunchy": ["crunchy"],
        },
        "bread_type": {
            "white": ["white"],
            "wheat": ["wheat"],
            "multigrain": ["multigrain", "multi grain"],
        },
        "soda_preference": {
            "regular": ["regular"],
            "diet": ["diet"],
            "zero_sugar": ["zero sugar", "zero"],
        },
        "coffee_caffeine": {
            "regular": ["regular", "caffeinated"],
            "decaf": ["decaf", "decaffeinated"],
            "both": ["regular", "decaf", "caffeinated", "decaffeinated"],
        },
        "coffee_roast": {
            "light": ["light roast"],
            "medium": ["medium roast"],
            "dark": ["dark roast"],
        },
        "yogurt_type": {
            "regular": ["regular"],
            "greek": ["greek"],
        },
        "butter_spread_type": {
            "butter": ["butter"],
            "margarine": ["margarine"],
            "plant_based": ["plant based", "plant-based", "vegan"],
        },
        "lunch_meat_type": {
            "turkey": ["turkey"],
            "ham": ["ham"],
            "chicken": ["chicken"],
            "roast_beef": ["roast beef"],
        },
        "laundry_detergent_scent": {
            "scented": ["scented", "fresh", "original"],
            "fragrance_free": ["fragrance free", "free clear", "free & clear", "unscented"],
        },
        "toothpaste_type": {
            "regular": ["regular"],
            "whitening": ["whitening"],
            "sensitivity": ["sensitivity", "sensitive"],
        },
        "shampoo_type": {
            "regular": ["regular", "daily"],
            "dandruff": ["dandruff"],
            "moisturizing": ["moisturizing", "moisture"],
            "color_safe": ["color safe", "color-safe"],
        },
    }
    return token_map.get(key, {}).get(value, [])


def _relevance_score(requirement: ShoppingRequirement, normalized_title: str) -> int:
    tokens = [token for token in _tokens(requirement.base_item) if token]
    score = 0
    for token in tokens:
        if token in normalized_title:
            score += 25
    return score


def _is_store_brand(normalized_title: str, retailer: str) -> bool:
    store_brands = {
        "walmart": ("great value", "equate", "marketside", "sam's choice"),
        "kroger": ("kroger", "simple truth", "private selection"),
        "gerbes": ("kroger", "simple truth", "private selection"),
    }
    for brand in store_brands.get(retailer, ()):
        if brand in normalized_title:
            return True
    return False


def _tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", str(value or "").lower()) if token]


def _normalized_text(value: str) -> str:
    return " ".join(_tokens(value))


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _diverse_products(products: list[RetailProduct], limit: int) -> list[RetailProduct]:
    selected, seen = [], set()
    for product in products:
        family = (product.title.lower().replace("-", " ").split() or [""])[0]
        if family in seen:
            continue
        seen.add(family)
        selected.append(product)
        if len(selected) >= limit:
            break
    return selected


def _diverse_dicts(products: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected, seen = [], set()
    for product in products:
        family = (str(product.get("title") or "").lower().replace("-", " ").split() or [""])[0]
        if family in seen:
            continue
        seen.add(family)
        selected.append(product)
        if len(selected) >= limit:
            break
    return selected


def _same_product(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for field in ("upc", "us_item_id", "product_id"):
        if left.get(field) and right.get(field) and str(left[field]) == str(right[field]):
            return True
    return str(left.get("title") or "").strip().lower() == str(right.get("title") or "").strip().lower()


def _preference_identity_overlay(product: dict[str, Any], preference: Any) -> dict[str, Any]:
    return {
        **product,
        "upc": product.get("upc") or preference.upc,
        "brand": product.get("brand") or preference.preferred_brand,
        "variant": product.get("variant") or preference.preferred_variant,
        "package_size": product.get("package_size") or preference.preferred_package_size,
        "product_id": product.get("product_id") or preference.retailer_product_id,
        "us_item_id": product.get("us_item_id") or preference.retailer_us_item_id,
    }


def _match_explicit_requirement(requirement: ShoppingRequirement, candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    def _contains_all(value: str, tokens: list[str]) -> bool:
        normalized = _normalized_text(value)
        return all(token in normalized for token in tokens)

    brand_tokens = _tokens(requirement.brand or "")
    variant_tokens = _tokens(requirement.variant or "")
    package_tokens = _tokens(requirement.requested_package_size or "")

    for candidate in candidates:
        title = str(candidate.get("title") or "")
        brand = str(candidate.get("brand") or "")
        package = str(candidate.get("package_size") or "")
        haystack = " ".join([title, brand, package]).strip()
        if brand_tokens and not _contains_all(haystack, brand_tokens):
            continue
        if variant_tokens and not _contains_all(haystack, variant_tokens):
            continue
        if package_tokens and not _contains_all(haystack, package_tokens):
            continue
        return candidate
    return None


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = datetime.now(timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _optimize_cart_for_budget(
    cart_items: list[dict[str, Any]],
    *,
    budget_limit: Optional[float],
    tax_rate: float,
    retailer: str,
    defaults: dict[str, Any],
    mutable_predicate: Optional[Callable[[dict[str, Any]], bool]] = None,
    prefer_fewer_changes: bool = False,
) -> Optional[dict[str, Any]]:
    if budget_limit is None:
        return None

    tax = _safe_decimal(tax_rate)
    budget_cents = _to_cents(budget_limit)
    base_subtotal_cents = sum(_line_cents(item) for item in cart_items)
    base_total_cents = _total_with_tax_cents(base_subtotal_cents, tax)
    if base_total_cents <= budget_cents:
        return {
            "applied": False,
            "status": "within_budget",
            "budget_cents": budget_cents,
            "base_total_cents": base_total_cents,
            "optimized_total_cents": base_total_cents,
            "lines_changed": 0,
            "candidate_bound": BUDGET_OPTIMIZER_MAX_CANDIDATES,
        }

    mutable_groups = _build_mutable_option_groups(
        cart_items,
        retailer=retailer,
        defaults=defaults,
        mutable_predicate=mutable_predicate,
    )
    if not mutable_groups:
        return {
            "applied": False,
            "status": "over_budget_no_flexible_lines",
            "budget_cents": budget_cents,
            "base_total_cents": base_total_cents,
            "optimized_total_cents": base_total_cents,
            "over_budget_cents": max(0, base_total_cents - budget_cents),
            "lines_changed": 0,
            "candidate_bound": BUDGET_OPTIMIZER_MAX_CANDIDATES,
        }

    states: dict[int, tuple[int, tuple[tuple[int, str], ...], tuple[int, ...]]] = {0: (0, tuple(), tuple())}
    for group_index, group in enumerate(mutable_groups):
        next_states: dict[int, tuple[int, tuple[tuple[int, str], ...], tuple[int, ...]]] = {}
        for savings_so_far, (penalty_so_far, unit_metric_so_far, picks_so_far) in states.items():
            for option_index, option in enumerate(group["options"]):
                savings_now = savings_so_far + option["savings_cents"]
                penalty_now = penalty_so_far + option["penalty_points"]
                unit_metric_now = unit_metric_so_far + ((option["unit_metric"], option["identity"]),)
                picks_now = picks_so_far + (option_index,)
                current = next_states.get(savings_now)
                if current is None or _is_better_state(
                    candidate=(penalty_now, unit_metric_now, picks_now),
                    incumbent=current,
                ):
                    next_states[savings_now] = (penalty_now, unit_metric_now, picks_now)
        states = next_states

    best_savings: Optional[int] = None
    best_state: Optional[tuple[int, tuple[tuple[int, str], ...], tuple[int, ...]]] = None
    for savings, state in states.items():
        subtotal_cents = max(0, base_subtotal_cents - savings)
        total_cents = _total_with_tax_cents(subtotal_cents, tax)
        if total_cents > budget_cents:
            continue
        if best_state is None:
            best_savings = savings
            best_state = state
            continue
        if _is_preferred_solution(
            candidate_state=state,
            candidate_savings=savings,
            incumbent_state=best_state,
            incumbent_savings=best_savings or 0,
            prefer_fewer_changes=prefer_fewer_changes,
        ):
            best_savings = savings
            best_state = state

    if best_state is None or best_savings is None:
        return {
            "applied": False,
            "status": "over_budget_no_feasible_combination",
            "budget_cents": budget_cents,
            "base_total_cents": base_total_cents,
            "optimized_total_cents": base_total_cents,
            "over_budget_cents": max(0, base_total_cents - budget_cents),
            "lines_changed": 0,
            "candidate_bound": BUDGET_OPTIMIZER_MAX_CANDIDATES,
        }

    changed = 0
    for group_index, option_index in enumerate(best_state[2]):
        group = mutable_groups[group_index]
        option = group["options"][option_index]
        if option_index == 0:
            continue
        _apply_option_to_item(group["item"], option)
        changed += 1

    optimized_subtotal_cents = max(0, base_subtotal_cents - best_savings)
    optimized_total_cents = _total_with_tax_cents(optimized_subtotal_cents, tax)
    return {
        "applied": changed > 0,
        "status": "optimized_within_budget" if optimized_total_cents <= budget_cents else "optimized_but_over_budget",
        "budget_cents": budget_cents,
        "base_total_cents": base_total_cents,
        "optimized_total_cents": optimized_total_cents,
        "savings_cents": max(0, base_total_cents - optimized_total_cents),
        "over_budget_cents": max(0, optimized_total_cents - budget_cents),
        "lines_changed": changed,
        "candidate_bound": BUDGET_OPTIMIZER_MAX_CANDIDATES,
    }


def _build_mutable_option_groups(
    cart_items: list[dict[str, Any]],
    *,
    retailer: str,
    defaults: dict[str, Any],
    mutable_predicate: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    predicate = mutable_predicate or _is_budget_mutable_item
    for item in cart_items:
        if not predicate(item):
            continue
        selected = item.get("selected_product")
        if not isinstance(selected, dict):
            continue
        requirement = ShoppingRequirement.from_mapping(item.get("requirement") or {})
        all_candidates = item.get("_all_candidates") or []
        if not isinstance(all_candidates, list):
            continue

        ranked = sorted(
            [row for row in all_candidates if isinstance(row, dict) and str(row.get("availability") or "unknown") != "out_of_stock"],
            key=lambda row: _suggestion_sort_key(
                requirement,
                row,
                retailer=retailer,
                defaults=defaults,
                style=str(defaults.get("shopping_style") or "store_brands_ok"),
            ),
        )
        ranked = _ensure_identity_in_ranked(ranked, selected)
        if not ranked:
            continue

        ranked = ranked[:BUDGET_OPTIMIZER_MAX_CANDIDATES]
        selected_identity = _candidate_identity(selected)
        rank_lookup = {_candidate_identity(row): index for index, row in enumerate(ranked)}
        selected_rank = rank_lookup.get(selected_identity, 0)
        selected_cents = _line_cents(item)
        if selected_cents <= 0:
            continue

        flexibility_weight = _flexibility_weight(requirement, defaults)
        options = []
        for row in ranked:
            line_cents = _candidate_line_cents(row, item)
            if line_cents <= 0 or line_cents > selected_cents:
                continue
            identity = _candidate_identity(row)
            rank = rank_lookup.get(identity, selected_rank)
            rank_penalty = max(0, rank - selected_rank)
            default_penalty = _default_mismatch_penalty(requirement, selected, row, defaults)
            options.append(
                {
                    "candidate": row,
                    "identity": identity,
                    "line_cents": line_cents,
                    "savings_cents": max(0, selected_cents - line_cents),
                    "penalty_points": rank_penalty * flexibility_weight + default_penalty,
                    "unit_metric": _unit_metric(row),
                }
            )

        if not options:
            continue

        options = sorted(
            options,
            key=lambda option: (
                option["penalty_points"],
                option["savings_cents"],
                option["unit_metric"],
                option["identity"],
            ),
        )

        base_option = next((opt for opt in options if opt["identity"] == selected_identity), None)
        if base_option is None:
            continue
        base_option = {**base_option, "penalty_points": 0, "savings_cents": 0}
        other_options = [opt for opt in options if opt["identity"] != selected_identity]
        groups.append({"item": item, "options": [base_option] + other_options})

    return groups


def _is_budget_mutable_item(item: dict[str, Any]) -> bool:
    if not item.get("resolved"):
        return False
    if item.get("needs_user_choice"):
        return False
    if item.get("preferred_product") or item.get("substituted"):
        return False
    if not item.get("suggested"):
        return False
    requirement = item.get("requirement") or {}
    if requirement.get("brand") or requirement.get("variant") or requirement.get("requested_package_size"):
        return False
    return True


def _apply_option_to_item(item: dict[str, Any], option: dict[str, Any]) -> None:
    candidate = option["candidate"]
    previous_selected = item.get("selected_product")
    alternatives = [row for row in (item.get("_all_candidates") or []) if isinstance(row, dict) and not _same_product(row, candidate)]
    alternatives = _diverse_dicts(alternatives, ALTERNATIVE_LIMIT)
    if previous_selected and not _same_product(previous_selected, candidate):
        alternatives = _diverse_dicts([previous_selected] + alternatives, ALTERNATIVE_LIMIT)
    item["selected_product"] = candidate
    item["alternatives"] = alternatives
    item["resolved"] = bool(candidate.get("product_id") or candidate.get("us_item_id"))
    item["product_label"] = candidate.get("title")
    item["unit_price"] = candidate.get("price")
    item["estimated_price"] = _cents_to_float(option["line_cents"])
    item["package_size"] = candidate.get("package_size")
    item["availability"] = candidate.get("availability") or "unknown"
    item["fulfillment"] = candidate.get("fulfillment")
    item["regular_price"] = candidate.get("regular_price")
    item["promo_price"] = candidate.get("promo_price")
    item["confirmed_local_store"] = bool(candidate.get("verified_location"))
    item["retrieved_at"] = candidate.get("retrieved_at") or item.get("retrieved_at")
    item["selection_confidence"] = "suggested"
    item["suggested"] = True
    item["suggestion_reason"] = "budget_optimized"


def _is_better_state(
    *,
    candidate: tuple[int, tuple[tuple[int, str], ...], tuple[int, ...]],
    incumbent: tuple[int, tuple[tuple[int, str], ...], tuple[int, ...]],
) -> bool:
    if candidate[0] != incumbent[0]:
        return candidate[0] < incumbent[0]
    if candidate[1] != incumbent[1]:
        return candidate[1] < incumbent[1]
    return candidate[2] < incumbent[2]


def _is_preferred_solution(
    *,
    candidate_state: tuple[int, tuple[tuple[int, str], ...], tuple[int, ...]],
    candidate_savings: int,
    incumbent_state: tuple[int, tuple[tuple[int, str], ...], tuple[int, ...]],
    incumbent_savings: int,
    prefer_fewer_changes: bool = False,
) -> bool:
    if candidate_state[0] != incumbent_state[0]:
        return candidate_state[0] < incumbent_state[0]
    if prefer_fewer_changes:
        candidate_changes = sum(1 for idx in candidate_state[2] if idx != 0)
        incumbent_changes = sum(1 for idx in incumbent_state[2] if idx != 0)
        if candidate_changes != incumbent_changes:
            return candidate_changes < incumbent_changes
    if candidate_savings != incumbent_savings:
        return candidate_savings < incumbent_savings
    if candidate_state[1] != incumbent_state[1]:
        return candidate_state[1] < incumbent_state[1]
    return candidate_state[2] < incumbent_state[2]


def propose_rebalance_preview(
    *,
    cart_items: list[dict[str, Any]],
    budget_limit: float,
    tax_rate: float,
    retailer: str,
    defaults: dict[str, Any],
    protected_choice_keys: set[str],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Build a non-mutating rebalance preview using existing optimizer semantics."""
    budget_cents = _to_cents(budget_limit)
    tax = _safe_decimal(tax_rate) or Decimal("0")
    base_subtotal_cents = sum(_line_cents(item) for item in cart_items)
    base_total_cents = _total_with_tax_cents(base_subtotal_cents, tax)
    over_budget_cents = max(0, base_total_cents - budget_cents)

    if over_budget_cents <= 0:
        return {
            "eligible": False,
            "status": "within_budget",
            "budget_cents": budget_cents,
            "base_total_cents": base_total_cents,
            "optimized_total_cents": base_total_cents,
            "required_savings_cents": 0,
            "max_available_savings_cents": 0,
            "changes": [],
            "context_fingerprint": _rebalance_context_fingerprint(cart_items, context, budget_cents),
            "proposal_fingerprint": "",
        }

    working_items: list[dict[str, Any]] = []
    for item in cart_items:
        clone = dict(item)
        clone["_all_candidates"] = _candidate_pool_for_rebalance(item)
        working_items.append(clone)

    before_by_key = {
        _choice_key_for_item(item): {
            "selected_product": dict(item.get("selected_product") or {}),
            "line_cents": _line_cents(item),
            "item_name": (item.get("requirement") or {}).get("item_name") or item.get("product_label") or _choice_key_for_item(item),
        }
        for item in working_items
    }

    def _rebalance_mutable(item: dict[str, Any]) -> bool:
        key = _choice_key_for_item(item)
        if key in protected_choice_keys:
            return False
        if item.get("selection_confidence") == "user_selected":
            return False
        if not item.get("resolved"):
            return False
        if item.get("quantity_uncertain") or item.get("package_resolution_uncertain") or item.get("packages_to_buy") is None:
            return False
        if item.get("needs_user_choice"):
            return False
        if item.get("preferred_product") or item.get("substituted"):
            return False
        requirement = item.get("requirement") or {}
        if requirement.get("brand") or requirement.get("variant") or requirement.get("requested_package_size"):
            return False
        return True

    optimization = _optimize_cart_for_budget(
        working_items,
        budget_limit=budget_limit,
        tax_rate=tax_rate,
        retailer=retailer,
        defaults=defaults,
        mutable_predicate=_rebalance_mutable,
        prefer_fewer_changes=True,
    )

    optimized_total_cents = int((optimization or {}).get("optimized_total_cents") or base_total_cents)
    actual_savings_cents = max(0, base_total_cents - optimized_total_cents)

    changes: list[dict[str, Any]] = []
    after_by_key = {_choice_key_for_item(item): item for item in working_items}
    for key, before in before_by_key.items():
        after = after_by_key.get(key)
        if not after:
            continue
        before_selected = before.get("selected_product") or {}
        after_selected = after.get("selected_product") or {}
        if not before_selected or not after_selected:
            continue
        if _same_product(before_selected, after_selected):
            continue
        before_cents = int(before.get("line_cents") or 0)
        after_cents = _line_cents(after)
        changes.append({
            "choice_key": key,
            "item_name": before.get("item_name") or key,
            "current_product": {
                "title": before_selected.get("title"),
                "product_id": before_selected.get("product_id"),
                "us_item_id": before_selected.get("us_item_id"),
                "package_size": before_selected.get("package_size"),
                "line_price_cents": before_cents,
            },
            "proposed_product": {
                "title": after_selected.get("title"),
                "product_id": after_selected.get("product_id"),
                "us_item_id": after_selected.get("us_item_id"),
                "package_size": after_selected.get("package_size"),
                "line_price_cents": after_cents,
            },
            "savings_cents": max(0, before_cents - after_cents),
        })

    context_fingerprint = _rebalance_context_fingerprint(cart_items, context, budget_cents)
    proposal_fingerprint = _rebalance_proposal_fingerprint(
        context_fingerprint=context_fingerprint,
        changes=changes,
    )

    max_available_savings_cents = max(0, base_total_cents - optimized_total_cents)
    if not changes:
        status = "over_budget_no_acceptable_savings"
    elif optimized_total_cents <= budget_cents:
        status = "rebalance_available"
    else:
        status = "rebalance_partial"

    return {
        "eligible": bool(changes),
        "status": status,
        "budget_cents": budget_cents,
        "base_total_cents": base_total_cents,
        "optimized_total_cents": optimized_total_cents,
        "required_savings_cents": over_budget_cents,
        "max_available_savings_cents": max_available_savings_cents,
        "remaining_cents": max(0, budget_cents - optimized_total_cents),
        "still_over_budget_cents": max(0, optimized_total_cents - budget_cents),
        "changes": sorted(changes, key=lambda row: (row["savings_cents"] * -1, row["choice_key"])),
        "context_fingerprint": context_fingerprint,
        "proposal_fingerprint": proposal_fingerprint,
    }


def _choice_key_for_item(item: dict[str, Any]) -> str:
    requirement = item.get("requirement") or {}
    base_item = str(requirement.get("base_item") or item.get("keyword") or "").strip().lower()
    return " ".join(base_item.split())


def _candidate_pool_for_rebalance(item: dict[str, Any]) -> list[dict[str, Any]]:
    selected = item.get("selected_product")
    alternatives = item.get("alternatives") or []
    pool: list[dict[str, Any]] = []
    if isinstance(selected, dict):
        pool.append(selected)
    for alt in alternatives:
        if not isinstance(alt, dict):
            continue
        if any(_same_product(existing, alt) for existing in pool):
            continue
        pool.append(alt)
    return pool


def _rebalance_context_fingerprint(
    cart_items: list[dict[str, Any]],
    context: dict[str, Any],
    budget_cents: int,
) -> str:
    rows = []
    for item in cart_items:
        selected = item.get("selected_product") or {}
        rows.append({
            "choice_key": _choice_key_for_item(item),
            "selected_identity": _candidate_identity(selected),
            "packages_to_buy": int(item.get("packages_to_buy") or 1),
            "line_cents": _line_cents(item),
        })
    rows = sorted(rows, key=lambda row: row["choice_key"])
    payload = {
        "retailer": str(context.get("retailer") or "").strip().lower(),
        "store_id": str(context.get("store_id") or "").strip(),
        "store_name": str(context.get("store_name") or "").strip().lower(),
        "budget_cents": int(budget_cents),
        "lines": rows,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rebalance_proposal_fingerprint(*, context_fingerprint: str, changes: list[dict[str, Any]]) -> str:
    compact_changes = [
        {
            "choice_key": row.get("choice_key"),
            "to_identity": _candidate_identity(row.get("proposed_product") or {}),
        }
        for row in changes
    ]
    compact_changes = sorted(compact_changes, key=lambda row: row["choice_key"] or "")
    encoded = json.dumps(
        {
            "context": context_fingerprint,
            "changes": compact_changes,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ensure_identity_in_ranked(ranked: list[dict[str, Any]], selected: dict[str, Any]) -> list[dict[str, Any]]:
    if any(_same_product(row, selected) for row in ranked):
        return ranked
    return [selected] + ranked


def _candidate_identity(candidate: dict[str, Any]) -> str:
    return str(candidate.get("upc") or candidate.get("us_item_id") or candidate.get("product_id") or candidate.get("title") or "")


def _line_cents(item: dict[str, Any]) -> int:
    if not item.get("resolved"):
        return 0
    price = item.get("estimated_price")
    if price is None:
        return 0
    return _to_cents(price)


def _candidate_line_cents(candidate: dict[str, Any], item: dict[str, Any]) -> int:
    unit_price = _safe_decimal(candidate.get("price"))
    if unit_price is None:
        return 0
    packages = max(1, int(item.get("packages_to_buy") or 1))
    line_total = unit_price * Decimal(packages)
    return _decimal_to_cents(line_total)


def _default_mismatch_penalty(
    requirement: ShoppingRequirement,
    selected: dict[str, Any],
    candidate: dict[str, Any],
    defaults: dict[str, Any],
) -> int:
    selected_match = _candidate_matches_household_defaults(requirement, selected, defaults)
    candidate_match = _candidate_matches_household_defaults(requirement, candidate, defaults)
    return 3 if selected_match and not candidate_match else 0


def _flexibility_weight(requirement: ShoppingRequirement, defaults: dict[str, Any]) -> int:
    keys = _default_keys_for_requirement(requirement)
    if not keys:
        return 2
    values: dict[str, str] = defaults.get("preferences") or {}
    filled = [str(values.get(key) or "").strip().lower() for key in keys if str(values.get(key) or "").strip()]
    if not filled:
        return 2
    if all(value == HOUSEHOLD_DONT_CARE for value in filled):
        return 1
    if any(value == HOUSEHOLD_DONT_CARE for value in filled):
        return 2
    return 4


def _unit_metric(candidate: dict[str, Any]) -> int:
    unit_price = _safe_decimal(candidate.get("price"))
    package_size = str(candidate.get("package_size") or "")
    if unit_price is None or not package_size:
        return 10**9
    qty = _parse_package_quantity(package_size)
    if qty is None or qty <= 0:
        return 10**9
    cents = _decimal_to_cents(unit_price)
    per_unit = (Decimal(cents) / qty).quantize(Decimal("1"), rounding=_ROUNDING)
    return int(per_unit)


def _parse_package_quantity(package_size: str) -> Optional[Decimal]:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(oz|ounce|ounces|lb|pound|pounds|ct|count)\b", package_size.lower())
    if not match:
        return None
    qty = Decimal(match.group(1))
    unit = match.group(2)
    if unit in {"lb", "pound", "pounds"}:
        return qty * Decimal("16")
    return qty


def _safe_decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_cents(value: Any) -> int:
    parsed = _safe_decimal(value)
    if parsed is None:
        return 0
    return _decimal_to_cents(parsed)


def _decimal_to_cents(value: Decimal) -> int:
    quantized = value.quantize(_MONEY_CENT, rounding=_ROUNDING)
    return int((quantized * Decimal("100")).to_integral_value(rounding=_ROUNDING))


def _total_with_tax_cents(subtotal_cents: int, tax_rate: Decimal) -> int:
    subtotal = Decimal(subtotal_cents) / Decimal("100")
    tax_amount = (subtotal * tax_rate).quantize(_MONEY_CENT, rounding=_ROUNDING)
    return _decimal_to_cents(subtotal + tax_amount)


def _cents_to_float(value: int) -> float:
    return float((Decimal(value) / Decimal("100")).quantize(_MONEY_CENT, rounding=_ROUNDING))
