from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from extensions import db
from models import (
    RetailProduct,
    RetailRefreshLease,
    RetailSearchCache,
    RetailStoreIdentity,
    StoreProductObservation,
)
from services.retail.base import RetailStore
from services.usage_meter import record_usage_event


PRICE_TYPES = {
    "regular",
    "promo",
    "member",
    "digital_coupon",
    "personalized",
    "unknown",
}

AVAILABILITY_STATUSES = {
    "available_for_pickup",
    "available_for_fulfillment",
    "unavailable",
    "unknown",
}

SOURCE_LIVE_PROVIDER = "LIVE_PROVIDER"
SOURCE_RECENT_CONFIRMED = "RECENT_CONFIRMED"
SOURCE_LAST_KNOWN = "LAST_KNOWN"
SOURCE_ESTIMATE = "ESTIMATE"
SOURCE_UNKNOWN = "UNKNOWN"


class SharedRetailFreshnessPolicy:
    PRICE_FRESH_HOURS = 24
    PRICE_RECENT_HOURS = 72
    PRICE_STALE_HOURS = 24 * 7

    AVAILABILITY_FRESH_HOURS = 2
    AVAILABILITY_RECENT_HOURS = 12
    AVAILABILITY_STALE_HOURS = 24

    SEARCH_FRESH_HOURS = 24

    LEASE_SECONDS = 20
    LEASE_WAIT_SECONDS = 2.5
    LEASE_POLL_SECONDS = 0.1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_query(value: str) -> str:
    return " ".join(_normalize_text(value).lower().split())


def _to_cents(amount: Optional[float]) -> Optional[int]:
    if amount is None:
        return None
    cents = (Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = _normalize_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _coerce_price_type(value: Optional[str]) -> str:
    raw = _normalize_text(value).lower()
    mapping = {
        "pickup": "unknown",
        "in_store": "regular",
        "online": "unknown",
    }
    normalized = mapping.get(raw, raw or "unknown")
    return normalized if normalized in PRICE_TYPES else "unknown"


def _coerce_availability_status(value: Optional[str], fulfillment: Optional[dict[str, bool]] = None) -> str:
    raw = _normalize_text(value).lower()
    if raw in {"out_of_stock", "unavailable"}:
        return "unavailable"
    if raw in {"in_stock", "available_for_pickup", "available_for_fulfillment"}:
        if fulfillment:
            if any(bool(v) for v in fulfillment.values()):
                return "available_for_fulfillment"
        return "available_for_fulfillment"
    if fulfillment and any(bool(v) for v in fulfillment.values()):
        return "available_for_fulfillment"
    return "unknown"


def _freshness_from_age_hours(age_hours: float, *, fresh: float, recent: float, stale: float) -> str:
    if age_hours <= fresh:
        return "FRESH"
    if age_hours <= recent:
        return "RECENT"
    if age_hours <= stale:
        return "STALE"
    return "OLD"


def classify_price_freshness(observed_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    if observed_at is None:
        return "UNKNOWN"
    reference = now or _utcnow()
    age_hours = max(0.0, (reference - observed_at).total_seconds() / 3600.0)
    return _freshness_from_age_hours(
        age_hours,
        fresh=SharedRetailFreshnessPolicy.PRICE_FRESH_HOURS,
        recent=SharedRetailFreshnessPolicy.PRICE_RECENT_HOURS,
        stale=SharedRetailFreshnessPolicy.PRICE_STALE_HOURS,
    )


def classify_availability_freshness(observed_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    if observed_at is None:
        return "UNKNOWN"
    reference = now or _utcnow()
    age_hours = max(0.0, (reference - observed_at).total_seconds() / 3600.0)
    return _freshness_from_age_hours(
        age_hours,
        fresh=SharedRetailFreshnessPolicy.AVAILABILITY_FRESH_HOURS,
        recent=SharedRetailFreshnessPolicy.AVAILABILITY_RECENT_HOURS,
        stale=SharedRetailFreshnessPolicy.AVAILABILITY_STALE_HOURS,
    )


def classify_search_freshness(observed_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    if observed_at is None:
        return "UNKNOWN"
    reference = now or _utcnow()
    age_hours = max(0.0, (reference - observed_at).total_seconds() / 3600.0)
    return "FRESH" if age_hours <= SharedRetailFreshnessPolicy.SEARCH_FRESH_HOURS else "OLD"


def classify_data_state(*, source: Optional[str], freshness: str) -> str:
    if source and source in {"kroger_api", "serpapi_walmart", "receipt"}:
        if freshness == "FRESH":
            return SOURCE_LIVE_PROVIDER
        if freshness == "RECENT":
            return SOURCE_RECENT_CONFIRMED
        return SOURCE_LAST_KNOWN
    if source in {"legacy_cache", "manual"}:
        return SOURCE_ESTIMATE
    return SOURCE_UNKNOWN


def _session_owner() -> str:
    return f"{os.getpid()}:{uuid.uuid4().hex[:8]}"


class SharedRetailFoundationService:
    @staticmethod
    def _snapshot_observed_at(snapshot: dict[str, Any]) -> Optional[str]:
        return snapshot.get("price_observed_at") or snapshot.get("availability_observed_at")

    @staticmethod
    def snapshot_to_candidate(snapshot: dict[str, Any]) -> dict[str, Any]:
        price_cents = snapshot.get("price_cents")
        price = round(float(price_cents) / 100.0, 2) if price_cents is not None else None
        return {
            "requested_query": None,
            "retailer": snapshot.get("retailer"),
            "store": {
                "store_id": snapshot.get("retailer_store_id"),
                "name": snapshot.get("store_name"),
                "address": snapshot.get("store_address"),
                "postal_code": snapshot.get("store_postal_code"),
                "verified": True,
            },
            "product_id": snapshot.get("retailer_product_id"),
            "us_item_id": None,
            "upc": snapshot.get("upc"),
            "title": snapshot.get("title") or snapshot.get("retailer_product_id"),
            "brand": snapshot.get("brand"),
            "variant": snapshot.get("variant"),
            "package_size": snapshot.get("package_size"),
            "price": price,
            "availability": "in_stock" if snapshot.get("availability_status") == "available_for_fulfillment" else (
                "out_of_stock" if snapshot.get("availability_status") == "unavailable" else "unknown"
            ),
            "price_type": snapshot.get("price_type") or "unknown",
            "product_url": None,
            "source": snapshot.get("price_source") or snapshot.get("availability_source") or "shared_retail",
            "retrieved_at": SharedRetailFoundationService._snapshot_observed_at(snapshot),
            "verified_location": True,
            "regular_price": price,
            "promo_price": None,
            "fulfillment": snapshot.get("fulfillment"),
            "data_quality": snapshot.get("data_state"),
            "price_freshness": snapshot.get("price_freshness"),
            "availability_freshness": snapshot.get("availability_freshness"),
        }

    def upsert_store(self, *, retailer: str, store: RetailStore) -> RetailStoreIdentity:
        row = RetailStoreIdentity.query.filter_by(
            retailer=_normalize_text(retailer).lower(),
            retailer_store_id=_normalize_text(store.store_id),
        ).first()
        values = {
            "store_name": _normalize_text(store.name) or _normalize_text(retailer).title(),
            "address": _normalize_text(store.address) or None,
            "postal_code": _normalize_text(store.postal_code) or None,
            "updated_at": _utcnow(),
        }
        if row is None:
            row = RetailStoreIdentity(
                retailer=_normalize_text(retailer).lower(),
                retailer_store_id=_normalize_text(store.store_id),
                **values,
            )
            db.session.add(row)
        else:
            for key, value in values.items():
                if value is not None or key == "updated_at":
                    setattr(row, key, value)
        db.session.commit()
        return row

    def upsert_product(
        self,
        *,
        retailer: str,
        retailer_product_id: str,
        title: str,
        upc: Optional[str] = None,
        brand: Optional[str] = None,
        package_size: Optional[str] = None,
        variant: Optional[str] = None,
        category: Optional[str] = None,
    ) -> RetailProduct:
        row = RetailProduct.query.filter_by(
            retailer=_normalize_text(retailer).lower(),
            retailer_product_id=_normalize_text(retailer_product_id),
        ).first()
        values = {
            "upc": _normalize_text(upc) or None,
            "title": _normalize_text(title) or _normalize_text(retailer_product_id),
            "brand": _normalize_text(brand) or None,
            "package_size": _normalize_text(package_size) or None,
            "variant": _normalize_text(variant) or None,
            "category": _normalize_text(category) or None,
            "updated_at": _utcnow(),
        }
        if row is None:
            row = RetailProduct(
                retailer=_normalize_text(retailer).lower(),
                retailer_product_id=_normalize_text(retailer_product_id),
                **values,
            )
            db.session.add(row)
        else:
            for key, value in values.items():
                if value is not None or key in {"title", "updated_at"}:
                    setattr(row, key, value)
        db.session.commit()
        return row

    def upsert_observation(
        self,
        *,
        retailer: str,
        store: RetailStore,
        retailer_product_id: str,
        title: str,
        upc: Optional[str] = None,
        brand: Optional[str] = None,
        package_size: Optional[str] = None,
        variant: Optional[str] = None,
        category: Optional[str] = None,
        price: Optional[float] = None,
        price_type: Optional[str] = None,
        price_source: Optional[str] = None,
        price_confidence: Optional[str] = None,
        availability: Optional[str] = None,
        fulfillment: Optional[dict[str, bool]] = None,
        availability_source: Optional[str] = None,
        availability_confidence: Optional[str] = None,
        observed_at: Optional[str] = None,
    ) -> StoreProductObservation:
        store_row = self.upsert_store(retailer=retailer, store=store)
        product_row = self.upsert_product(
            retailer=retailer,
            retailer_product_id=retailer_product_id,
            title=title,
            upc=upc,
            brand=brand,
            package_size=package_size,
            variant=variant,
            category=category,
        )
        row = StoreProductObservation.query.filter_by(
            retail_store_id=store_row.id,
            retail_product_id=product_row.id,
        ).first()
        if row is None:
            row = StoreProductObservation(
                retail_store_id=store_row.id,
                retail_product_id=product_row.id,
                price_type="unknown",
            )
            db.session.add(row)

        observed_dt = _parse_iso_datetime(observed_at) or _utcnow()
        price_cents = _to_cents(price)
        if price_cents is not None:
            row.price_cents = price_cents
            row.price_type = _coerce_price_type(price_type)
            row.price_observed_at = observed_dt
            row.price_source = _normalize_text(price_source) or row.price_source
            row.price_confidence = _normalize_text(price_confidence) or row.price_confidence

        normalized_availability = None
        if availability is not None or fulfillment is not None:
            normalized_availability = _coerce_availability_status(availability, fulfillment)
        if normalized_availability is not None:
            row.availability_status = normalized_availability
            if fulfillment is not None:
                row.fulfillment_data_json = json.dumps(fulfillment)
            row.availability_observed_at = observed_dt
            row.availability_source = _normalize_text(availability_source) or row.availability_source
            row.availability_confidence = _normalize_text(availability_confidence) or row.availability_confidence

        row.updated_at = _utcnow()
        db.session.commit()
        return row

    def observation_snapshot(
        self,
        *,
        retailer: str,
        retailer_store_id: str,
        retailer_product_id: str,
    ) -> Optional[dict[str, Any]]:
        store_row = RetailStoreIdentity.query.filter_by(
            retailer=_normalize_text(retailer).lower(),
            retailer_store_id=_normalize_text(retailer_store_id),
        ).first()
        if store_row is None:
            return None
        product_row = RetailProduct.query.filter_by(
            retailer=_normalize_text(retailer).lower(),
            retailer_product_id=_normalize_text(retailer_product_id),
        ).first()
        if product_row is None:
            return None
        row = StoreProductObservation.query.filter_by(
            retail_store_id=store_row.id,
            retail_product_id=product_row.id,
        ).first()
        if row is None:
            return None

        price_freshness = classify_price_freshness(row.price_observed_at)
        availability_freshness = classify_availability_freshness(row.availability_observed_at)
        source_for_state = row.price_source or row.availability_source
        overall_freshness = price_freshness
        if availability_freshness in {"OLD", "STALE"} and overall_freshness in {"FRESH", "RECENT"}:
            overall_freshness = availability_freshness

        return {
            "retailer": store_row.retailer,
            "retailer_store_id": store_row.retailer_store_id,
            "store_name": store_row.store_name,
            "store_address": store_row.address,
            "store_postal_code": store_row.postal_code,
            "retailer_product_id": product_row.retailer_product_id,
            "title": product_row.title,
            "upc": product_row.upc,
            "brand": product_row.brand,
            "package_size": product_row.package_size,
            "variant": product_row.variant,
            "category": product_row.category,
            "price_cents": row.price_cents,
            "price_type": row.price_type,
            "price_source": row.price_source,
            "price_confidence": row.price_confidence,
            "price_observed_at": row.price_observed_at.isoformat() if row.price_observed_at else None,
            "price_freshness": price_freshness,
            "availability_status": row.availability_status or "unknown",
            "fulfillment": json.loads(row.fulfillment_data_json) if row.fulfillment_data_json else None,
            "availability_source": row.availability_source,
            "availability_confidence": row.availability_confidence,
            "availability_observed_at": row.availability_observed_at.isoformat() if row.availability_observed_at else None,
            "availability_freshness": availability_freshness,
            "data_state": classify_data_state(source=source_for_state, freshness=overall_freshness),
        }

    def search_cache_candidates(
        self,
        *,
        retailer: str,
        retailer_store_id: str,
        query: str,
    ) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
        cache = self.get_search_cache(retailer=retailer, retailer_store_id=retailer_store_id, query=query)
        if cache is None:
            return None, []
        candidates: list[dict[str, Any]] = []
        for product_id in cache.get("retailer_product_ids") or []:
            snapshot = self.observation_snapshot(
                retailer=retailer,
                retailer_store_id=retailer_store_id,
                retailer_product_id=product_id,
            )
            if snapshot is None:
                continue
            candidates.append(self.snapshot_to_candidate(snapshot))
        return cache, candidates

    def upsert_search_cache(
        self,
        *,
        retailer: str,
        store: RetailStore,
        query: str,
        retailer_product_ids: list[str],
        source: str,
        observed_at: Optional[str] = None,
    ) -> RetailSearchCache:
        store_row = self.upsert_store(retailer=retailer, store=store)
        normalized_query = normalize_query(query)
        row = RetailSearchCache.query.filter_by(
            retailer=_normalize_text(retailer).lower(),
            retail_store_id=store_row.id,
            normalized_query=normalized_query,
        ).first()
        observed_dt = _parse_iso_datetime(observed_at) or _utcnow()
        values = {
            "retailer_product_ids_json": json.dumps([_normalize_text(v) for v in retailer_product_ids if _normalize_text(v)]),
            "observed_at": observed_dt,
            "source": _normalize_text(source) or "unknown",
            "updated_at": _utcnow(),
        }
        if row is None:
            row = RetailSearchCache(
                retailer=_normalize_text(retailer).lower(),
                retail_store_id=store_row.id,
                normalized_query=normalized_query,
                **values,
            )
            db.session.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        db.session.commit()
        return row

    def get_search_cache(
        self,
        *,
        retailer: str,
        retailer_store_id: str,
        query: str,
    ) -> Optional[dict[str, Any]]:
        normalized = normalize_query(query)
        row = (
            RetailSearchCache.query.join(RetailStoreIdentity, RetailSearchCache.retail_store_id == RetailStoreIdentity.id)
            .filter(RetailSearchCache.retailer == _normalize_text(retailer).lower())
            .filter(RetailStoreIdentity.retailer_store_id == _normalize_text(retailer_store_id))
            .filter(RetailStoreIdentity.retailer == _normalize_text(retailer).lower())
            .filter(RetailSearchCache.normalized_query == normalized)
            .first()
        )
        if row is None:
            record_usage_event(
                category="shared_retail_cache",
                provider="shared_retail",
                operation="shared_retail_search_miss",
                success=True,
                external_call=False,
            )
            return None

        freshness = classify_search_freshness(row.observed_at)
        record_usage_event(
            category="shared_retail_cache",
            provider="shared_retail",
            operation="shared_retail_search_hit",
            success=True,
            external_call=False,
            cache_status="hit",
        )
        return {
            "retailer": row.retailer,
            "retailer_store_id": retailer_store_id,
            "normalized_query": row.normalized_query,
            "retailer_product_ids": json.loads(row.retailer_product_ids_json or "[]"),
            "observed_at": row.observed_at.isoformat() if row.observed_at else None,
            "source": row.source,
            "freshness": freshness,
        }

    def acquire_refresh_lease(
        self,
        *,
        resource_key: str,
        lease_owner: Optional[str] = None,
        lease_seconds: Optional[float] = None,
    ) -> tuple[bool, str]:
        owner = _normalize_text(lease_owner) or _session_owner()
        now = _utcnow()
        seconds = float(lease_seconds if lease_seconds is not None else SharedRetailFreshnessPolicy.LEASE_SECONDS)
        lease_until = now + timedelta(seconds=max(1.0, seconds))
        key = _normalize_text(resource_key)

        try:
            updated = db.session.execute(
                text(
                    """
                    UPDATE retail_refresh_lease
                    SET lease_owner = :owner, lease_until = :lease_until, updated_at = :now
                    WHERE resource_key = :resource_key AND lease_until <= :now
                    """
                ),
                {
                    "owner": owner,
                    "lease_until": lease_until,
                    "now": now,
                    "resource_key": key,
                },
            ).rowcount
        except SQLAlchemyError:
            db.session.rollback()
            updated = 0
        if updated:
            db.session.commit()
            record_usage_event(
                category="shared_retail_cache",
                provider="shared_retail",
                operation="refresh_lease_acquired",
                success=True,
                external_call=False,
            )
            return True, owner

        try:
            db.session.execute(
                text(
                    """
                    INSERT INTO retail_refresh_lease
                        (resource_key, lease_owner, lease_until, created_at, updated_at)
                    VALUES
                        (:resource_key, :owner, :lease_until, :now, :now)
                    """
                ),
                {
                    "resource_key": key,
                    "owner": owner,
                    "lease_until": lease_until,
                    "now": now,
                },
            )
            db.session.commit()
            record_usage_event(
                category="shared_retail_cache",
                provider="shared_retail",
                operation="refresh_lease_acquired",
                success=True,
                external_call=False,
            )
            return True, owner
        except (IntegrityError, SQLAlchemyError):
            db.session.rollback()

        record_usage_event(
            category="shared_retail_cache",
            provider="shared_retail",
            operation="refresh_lease_contended",
            success=True,
            external_call=False,
        )
        return False, owner

    def release_refresh_lease(self, *, resource_key: str, lease_owner: str) -> None:
        now = _utcnow()
        (
            RetailRefreshLease.query
            .filter(RetailRefreshLease.resource_key == _normalize_text(resource_key))
            .filter(RetailRefreshLease.lease_owner == _normalize_text(lease_owner))
            .update(
                {
                    RetailRefreshLease.lease_until: now,
                    RetailRefreshLease.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        db.session.commit()

    def wait_for_refresh(
        self,
        *,
        resource_key: str,
        load_current: Callable[[], Optional[dict[str, Any]]],
        max_wait_seconds: Optional[float] = None,
        poll_seconds: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        deadline = time.monotonic() + float(max_wait_seconds if max_wait_seconds is not None else SharedRetailFreshnessPolicy.LEASE_WAIT_SECONDS)
        pause = float(poll_seconds if poll_seconds is not None else SharedRetailFreshnessPolicy.LEASE_POLL_SECONDS)
        while time.monotonic() < deadline:
            current = load_current()
            if current is not None:
                return current
            row = RetailRefreshLease.query.filter_by(resource_key=_normalize_text(resource_key)).first()
            if row is None or row.lease_until <= _utcnow():
                return None
            time.sleep(max(0.01, pause))
        return load_current()


shared_retail_foundation = SharedRetailFoundationService()