from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from services.retail.base import RetailProduct, RetailProvider, RetailStore, ShoppingRequirement
from services.retail.shared_foundation import normalize_query, shared_retail_foundation
from services.retail.walmart_serpapi import WalmartSerpApiProvider
from services.usage_meter import (
    check_retail_provider_operation,
    estimate_usage_cost,
    record_usage_event,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quality_from_price_freshness(price_freshness: str) -> str:
    if price_freshness == "FRESH":
        return "LIVE_PROVIDER"
    if price_freshness == "RECENT":
        return "RECENT_CONFIRMED"
    if price_freshness == "STALE":
        return "LAST_KNOWN"
    if price_freshness == "OLD":
        return "ESTIMATE"
    return "UNKNOWN"


def _needs_price_refresh(snapshot: Optional[dict[str, Any]], *, explicit_live_refresh: bool) -> tuple[bool, str]:
    if explicit_live_refresh:
        return True, "explicit_live_refresh"
    if snapshot is None or snapshot.get("price_cents") is None:
        return True, "cold_exact_sku_miss"
    freshness = snapshot.get("price_freshness") or "UNKNOWN"
    if freshness == "STALE":
        return True, "stale_price_refresh"
    if freshness == "OLD":
        return True, "old_price_refresh"
    return False, "cache_reuse"


def _needs_kroger_refresh(snapshot: Optional[dict[str, Any]], *, explicit_live_refresh: bool) -> tuple[bool, str]:
    need, reason = _needs_price_refresh(snapshot, explicit_live_refresh=explicit_live_refresh)
    if need:
        return True, reason
    availability_freshness = (snapshot or {}).get("availability_freshness") or "UNKNOWN"
    if availability_freshness in {"STALE", "OLD"}:
        return True, "stale_availability_refresh"
    return False, "cache_reuse"


def _serialize_provider_product(product: RetailProduct) -> dict[str, Any]:
    payload = product.to_dict()
    payload["data_quality"] = "LIVE_PROVIDER"
    payload["price_freshness"] = "FRESH"
    payload["availability_freshness"] = "FRESH" if product.availability != "unknown" else "UNKNOWN"
    return payload


def _snapshot_candidate(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = shared_retail_foundation.snapshot_to_candidate(snapshot)
    payload["data_quality"] = _quality_from_price_freshness(snapshot.get("price_freshness") or "UNKNOWN")
    return payload


def _record_resolution_request(owner_scope: str, *, mode: str, retailer: str) -> None:
    record_usage_event(
        owner_scope=owner_scope,
        category="retail_resolution",
        provider=retailer,
        operation="retail_resolution_requests",
        success=True,
        external_call=False,
        metadata={"mode": mode},
    )


def _record_serpapi_decision(owner_scope: str, *, reason: str, outcome: str) -> None:
    record_usage_event(
        owner_scope=owner_scope,
        category="retail_resolution",
        provider="serpapi_walmart",
        operation="serpapi_fallback_decision",
        success=outcome in {"allowed", "provider_succeeded"},
        external_call=outcome in {"provider_failed", "provider_succeeded"},
        metadata={"reason": reason, "outcome": outcome},
    )


class RetailResolutionService:
    def resolve_exact(
        self,
        *,
        retailer: str,
        store: RetailStore,
        retailer_product_id: str,
        provider: RetailProvider,
        owner_scope: str,
        explicit_live_refresh: bool,
    ) -> dict[str, Any]:
        _record_resolution_request(owner_scope, mode="exact", retailer=retailer)
        snapshot = shared_retail_foundation.observation_snapshot(
            retailer=retailer,
            retailer_store_id=store.store_id,
            retailer_product_id=retailer_product_id,
        )

        if snapshot is not None and snapshot.get("price_cents") is not None:
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=retailer,
                operation="exact_sku_cache_hits",
                success=True,
                external_call=False,
            )
        else:
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=retailer,
                operation="exact_sku_cache_misses",
                success=True,
                external_call=False,
            )

        refresh_decider = _needs_kroger_refresh if retailer in {"kroger", "gerbes"} else _needs_price_refresh
        needs_refresh, reason = refresh_decider(snapshot, explicit_live_refresh=explicit_live_refresh)

        if not needs_refresh and snapshot is not None and snapshot.get("price_cents") is not None:
            return {
                "mode": "exact",
                "selected_product": _snapshot_candidate(snapshot),
                "candidates": [_snapshot_candidate(snapshot)],
                "external_call": False,
                "provider": None,
                "degraded_reason": None,
            }

        provider_name = "kroger_api" if retailer in {"kroger", "gerbes"} else "serpapi_walmart"
        if provider_name == "serpapi_walmart":
            _record_serpapi_decision(owner_scope, reason=reason, outcome="requested")

        gate = check_retail_provider_operation(
            owner_scope,
            provider=provider_name,
            projected_cost_micros=estimate_usage_cost(
                category="retail_provider",
                provider=provider_name,
                operation="product_detail",
                request_count=1,
            ).get("estimated_cost_micros"),
            require_explicit_allowance=(provider_name == "serpapi_walmart" and isinstance(provider, WalmartSerpApiProvider)),
        )
        if not gate.get("allowed", False):
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=provider_name,
                operation="provider_refresh_blocked",
                success=False,
                external_call=False,
                metadata={"reason": reason, "code": gate.get("code")},
            )
            if provider_name == "serpapi_walmart":
                if gate.get("code") == "blocked_unconfigured":
                    _record_serpapi_decision(owner_scope, reason=reason, outcome="blocked_unconfigured")
                elif gate.get("code") == "retail_live_disabled":
                    _record_serpapi_decision(owner_scope, reason=reason, outcome="blocked_by_kill_switch")
                else:
                    _record_serpapi_decision(owner_scope, reason=reason, outcome="blocked_by_limit")
            if snapshot is None:
                record_usage_event(
                    owner_scope=owner_scope,
                    category="retail_resolution",
                    provider=retailer,
                    operation="unknown_cold_misses",
                    success=True,
                    external_call=False,
                )
                return {
                    "mode": "exact",
                    "selected_product": None,
                    "candidates": [],
                    "external_call": False,
                    "provider": provider_name,
                    "degraded_reason": gate.get("code") or "blocked",
                }
            return {
                "mode": "exact",
                "selected_product": _snapshot_candidate(snapshot),
                "candidates": [_snapshot_candidate(snapshot)],
                "external_call": False,
                "provider": provider_name,
                "degraded_reason": gate.get("code") or "blocked",
            }

        if provider_name == "serpapi_walmart":
            _record_serpapi_decision(owner_scope, reason=reason, outcome="allowed")

        resource_key = f"product:{retailer}:{store.store_id}:{retailer_product_id}"
        acquired, lease_owner = shared_retail_foundation.acquire_refresh_lease(resource_key=resource_key)
        if not acquired:
            if snapshot is not None:
                record_usage_event(
                    owner_scope=owner_scope,
                    category="retail_resolution",
                    provider=retailer,
                    operation="last_known_fallbacks",
                    success=True,
                    external_call=False,
                )
                return {
                    "mode": "exact",
                    "selected_product": _snapshot_candidate(snapshot),
                    "candidates": [_snapshot_candidate(snapshot)],
                    "external_call": False,
                    "provider": provider_name,
                    "degraded_reason": "refresh_inflight",
                }
            waited = shared_retail_foundation.wait_for_refresh(
                resource_key=resource_key,
                load_current=lambda: shared_retail_foundation.observation_snapshot(
                    retailer=retailer,
                    retailer_store_id=store.store_id,
                    retailer_product_id=retailer_product_id,
                ),
            )
            if waited is not None:
                candidate = _snapshot_candidate(waited)
                return {
                    "mode": "exact",
                    "selected_product": candidate,
                    "candidates": [candidate],
                    "external_call": False,
                    "provider": provider_name,
                    "degraded_reason": None,
                }
            return {
                "mode": "exact",
                "selected_product": None,
                "candidates": [],
                "external_call": False,
                "provider": provider_name,
                "degraded_reason": "refresh_inflight_no_data",
            }

        try:
            product = provider.get_product(retailer_product_id, store=store, requested_query=retailer_product_id)
            shared_retail_foundation.upsert_observation(
                retailer=retailer,
                store=store,
                retailer_product_id=str(product.product_id or product.us_item_id or retailer_product_id),
                title=product.title,
                upc=product.upc,
                brand=product.brand,
                package_size=product.package_size,
                variant=product.variant,
                price=product.price,
                price_type=product.price_type,
                price_source=provider_name,
                price_confidence="provider_confirmed" if product.price is not None else None,
                availability=product.availability,
                fulfillment=product.fulfillment,
                availability_source=provider_name,
                availability_confidence="provider_confirmed" if product.availability != "unknown" else None,
                observed_at=product.retrieved_at,
            )
            cost = estimate_usage_cost(
                category="retail_provider",
                provider=provider_name,
                operation="product_detail",
                request_count=1,
            )
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_provider",
                provider=provider_name,
                operation="product_detail",
                success=True,
                external_call=True,
                estimated_cost_micros=cost.get("estimated_cost_micros"),
                cost_status=cost.get("cost_status"),
                cost_rate_key=cost.get("cost_rate_key"),
                metadata={"reason": reason},
            )
            if provider_name == "serpapi_walmart":
                _record_serpapi_decision(owner_scope, reason=reason, outcome="provider_succeeded")
            return {
                "mode": "exact",
                "selected_product": _serialize_provider_product(product),
                "candidates": [_serialize_provider_product(product)],
                "external_call": True,
                "provider": provider_name,
                "degraded_reason": None,
            }
        except Exception:
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=provider_name,
                operation="provider_refresh_failed",
                success=False,
                external_call=True,
                metadata={"reason": reason},
            )
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_provider",
                provider=provider_name,
                operation="product_detail",
                success=False,
                external_call=True,
                metadata={"reason": reason},
            )
            if provider_name == "serpapi_walmart":
                _record_serpapi_decision(owner_scope, reason=reason, outcome="provider_failed")
            if snapshot is not None:
                return {
                    "mode": "exact",
                    "selected_product": _snapshot_candidate(snapshot),
                    "candidates": [_snapshot_candidate(snapshot)],
                    "external_call": False,
                    "provider": provider_name,
                    "degraded_reason": "provider_failed_last_known",
                }
            return {
                "mode": "exact",
                "selected_product": None,
                "candidates": [],
                "external_call": False,
                "provider": provider_name,
                "degraded_reason": "provider_failed_cold_miss",
            }
        finally:
            shared_retail_foundation.release_refresh_lease(resource_key=resource_key, lease_owner=lease_owner)

    def resolve_search(
        self,
        *,
        retailer: str,
        store: RetailStore,
        requirement: ShoppingRequirement,
        provider: RetailProvider,
        owner_scope: str,
        explicit_live_refresh: bool,
    ) -> dict[str, Any]:
        _record_resolution_request(owner_scope, mode="search", retailer=retailer)
        query = requirement.search_query()
        cache, candidates = shared_retail_foundation.search_cache_candidates(
            retailer=retailer,
            retailer_store_id=store.store_id,
            query=query,
        )
        freshness = (cache or {}).get("freshness") if cache else "UNKNOWN"

        if cache is not None:
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=retailer,
                operation="search_cache_hits",
                success=True,
                external_call=False,
                metadata={"freshness": freshness},
            )
        else:
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=retailer,
                operation="search_cache_misses",
                success=True,
                external_call=False,
            )

        usable_candidates = [row for row in candidates if row.get("price") is not None]
        if cache is not None and freshness == "FRESH" and candidates and not explicit_live_refresh:
            return {
                "mode": "search",
                "query": query,
                "candidates": candidates,
                "external_call": False,
                "provider": None,
                "degraded_reason": None,
            }
        if usable_candidates and not explicit_live_refresh:
            return {
                "mode": "search",
                "query": query,
                "candidates": candidates,
                "external_call": False,
                "provider": None,
                "degraded_reason": "stale_search_cache_reuse",
            }

        reason = "explicit_live_refresh" if explicit_live_refresh else ("cold_search_miss" if cache is None else "stale_search_refresh")
        provider_name = "kroger_api" if retailer in {"kroger", "gerbes"} else "serpapi_walmart"
        if provider_name == "serpapi_walmart":
            _record_serpapi_decision(owner_scope, reason=reason, outcome="requested")

        gate = check_retail_provider_operation(
            owner_scope,
            provider=provider_name,
            projected_cost_micros=estimate_usage_cost(
                category="retail_provider",
                provider=provider_name,
                operation="product_search",
                request_count=1,
            ).get("estimated_cost_micros"),
            require_explicit_allowance=(provider_name == "serpapi_walmart" and isinstance(provider, WalmartSerpApiProvider)),
        )
        if not gate.get("allowed", False):
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=provider_name,
                operation="provider_refresh_blocked",
                success=False,
                external_call=False,
                metadata={"reason": reason, "code": gate.get("code")},
            )
            if provider_name == "serpapi_walmart":
                if gate.get("code") == "blocked_unconfigured":
                    _record_serpapi_decision(owner_scope, reason=reason, outcome="blocked_unconfigured")
                elif gate.get("code") == "retail_live_disabled":
                    _record_serpapi_decision(owner_scope, reason=reason, outcome="blocked_by_kill_switch")
                else:
                    _record_serpapi_decision(owner_scope, reason=reason, outcome="blocked_by_limit")
            return {
                "mode": "search",
                "query": query,
                "candidates": candidates,
                "external_call": False,
                "provider": provider_name,
                "degraded_reason": gate.get("code") or "blocked",
            }

        if provider_name == "serpapi_walmart":
            _record_serpapi_decision(owner_scope, reason=reason, outcome="allowed")

        resource_key = f"search:{retailer}:{store.store_id}:{normalize_query(query)}"
        acquired, lease_owner = shared_retail_foundation.acquire_refresh_lease(resource_key=resource_key)
        if not acquired:
            if candidates:
                return {
                    "mode": "search",
                    "query": query,
                    "candidates": candidates,
                    "external_call": False,
                    "provider": provider_name,
                    "degraded_reason": "refresh_inflight",
                }
            waited_cache, waited_candidates = shared_retail_foundation.search_cache_candidates(
                retailer=retailer,
                retailer_store_id=store.store_id,
                query=query,
            )
            if waited_cache is not None and waited_candidates:
                return {
                    "mode": "search",
                    "query": query,
                    "candidates": waited_candidates,
                    "external_call": False,
                    "provider": provider_name,
                    "degraded_reason": None,
                }
            return {
                "mode": "search",
                "query": query,
                "candidates": [],
                "external_call": False,
                "provider": provider_name,
                "degraded_reason": "refresh_inflight_no_data",
            }

        try:
            result = provider.search_products(requirement, store=store, limit=20)
            products = [_serialize_provider_product(product) for product in result.products]
            for product in result.products:
                product_id = str(product.product_id or product.us_item_id or "").strip()
                if not product_id:
                    continue
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
                    price_source=provider_name,
                    price_confidence="provider_confirmed" if product.price is not None else None,
                    availability=product.availability,
                    fulfillment=product.fulfillment,
                    availability_source=provider_name,
                    availability_confidence="provider_confirmed" if product.availability != "unknown" else None,
                    observed_at=product.retrieved_at,
                )
            shared_retail_foundation.upsert_search_cache(
                retailer=retailer,
                store=store,
                query=query,
                retailer_product_ids=[str(p.product_id or p.us_item_id or "").strip() for p in result.products if str(p.product_id or p.us_item_id or "").strip()],
                source=provider_name,
            )
            cost = estimate_usage_cost(
                category="retail_provider",
                provider=provider_name,
                operation="product_search",
                request_count=1,
            )
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_provider",
                provider=provider_name,
                operation="product_search",
                success=True,
                external_call=True,
                estimated_cost_micros=cost.get("estimated_cost_micros"),
                cost_status=cost.get("cost_status"),
                cost_rate_key=cost.get("cost_rate_key"),
                metadata={"reason": reason},
            )
            if provider_name == "serpapi_walmart":
                _record_serpapi_decision(owner_scope, reason=reason, outcome="provider_succeeded")
            return {
                "mode": "search",
                "query": query,
                "candidates": products,
                "external_call": True,
                "provider": provider_name,
                "degraded_reason": None,
            }
        except Exception:
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_resolution",
                provider=provider_name,
                operation="provider_refresh_failed",
                success=False,
                external_call=True,
                metadata={"reason": reason},
            )
            record_usage_event(
                owner_scope=owner_scope,
                category="retail_provider",
                provider=provider_name,
                operation="product_search",
                success=False,
                external_call=True,
                metadata={"reason": reason},
            )
            if provider_name == "serpapi_walmart":
                _record_serpapi_decision(owner_scope, reason=reason, outcome="provider_failed")
            return {
                "mode": "search",
                "query": query,
                "candidates": candidates,
                "external_call": False,
                "provider": provider_name,
                "degraded_reason": "provider_failed" if candidates else "provider_failed_cold_miss",
            }
        finally:
            shared_retail_foundation.release_refresh_lease(resource_key=resource_key, lease_owner=lease_owner)


retail_resolution_service = RetailResolutionService()
