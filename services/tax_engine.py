from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from extensions import db
from models import (
    RetailProductTaxClass,
    StoreTaxProfile,
    TaxBoundaryAssignment,
    TaxJurisdiction,
    TaxRate,
    TaxSourceDataset,
    TaxabilityRule,
)
from services.usage_meter import record_usage_event
from services.tax_adapters import NormalizedTaxDataset, TaxDataAdapter, source_hash
from services.tax_adapters import (
    PROVENANCE_MANUAL_UNVERIFIED,
    PROVENANCE_OFFICIAL_GOVERNMENT,
    PROVENANCE_OFFICIAL_SST,
    PROVENANCE_SYNTHETIC_TEST,
)


TAX_CLASS_GROCERY_FOOD = "GROCERY_FOOD"
TAX_CLASS_GENERAL_MERCHANDISE = "GENERAL_MERCHANDISE"
TAX_CLASS_PREPARED_FOOD = "PREPARED_FOOD"
TAX_CLASS_EXEMPT = "EXEMPT"
TAX_CLASS_UNKNOWN = "UNKNOWN"

LOCATION_PRECISION_EXACT_ADDRESS = "EXACT_ADDRESS"
LOCATION_PRECISION_ZIP_PLUS_4 = "ZIP_PLUS_4"
LOCATION_PRECISION_ZIP5 = "ZIP5"
LOCATION_PRECISION_CITY_COUNTY = "CITY_COUNTY"
LOCATION_PRECISION_STATE_ONLY = "STATE_ONLY"
LOCATION_PRECISION_UNRESOLVED = "UNRESOLVED"

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

_UNKNOWN_FALLBACK_POLICY = "unknown_uses_general_merchandise_rate"
_MONEY_CENT = Decimal("0.01")

_AUTHORITATIVE_PROVENANCE = {
    PROVENANCE_OFFICIAL_GOVERNMENT,
    PROVENANCE_OFFICIAL_SST,
}

_LEGACY_SOURCE_TYPE_MAP = {
    "state_public": PROVENANCE_MANUAL_UNVERIFIED,
    "sst_sample": PROVENANCE_SYNTHETIC_TEST,
}

_FOOD_TOKENS = {
    "milk", "eggs", "cheese", "bread", "banana", "apple", "rice", "beans", "flour", "sugar",
    "chicken", "beef", "pork", "fish", "tomato", "lettuce", "broccoli", "cereal", "yogurt", "butter",
}
_GENERAL_TOKENS = {
    "detergent", "shampoo", "paper", "toothpaste", "toothbrush", "battery", "cleaner", "soap", "towel",
    "deodorant", "lotion", "trash", "bag", "napkin", "dish", "bleach", "wipes",
}
_PREPARED_TOKENS = {
    "prepared", "hot", "deli", "ready", "sandwich", "rotisserie", "takeout", "meal", "pizza",
}
_EXEMPT_TOKENS = {"prescription", "medicine"}


@dataclass
class StoreTaxProfileResult:
    retailer: str
    retailer_store_id: str
    state: str
    location_precision: str
    confidence: str
    status: str
    general_rate_bps: int
    grocery_rate_bps: int
    prepared_rate_bps: int
    resolved_tax_code: Optional[str]
    source_key: Optional[str]
    source_version: Optional[str]
    source_hash: Optional[str]
    effective_from: date
    effective_to: Optional[date]
    degraded_reason: Optional[str]


@dataclass
class CartTaxResult:
    subtotal_cents: int
    tax_cents: int
    estimated_total_cents: int
    subtotal_by_class_cents: dict[str, int]
    tax_by_class_cents: dict[str, int]
    effective_rate_bps: dict[str, int]
    precision: str
    confidence: str
    source_version: Optional[str]
    unknown_class_count: int
    degraded_reason: Optional[str]


def rounding_cents_from_subtotal_and_rate_bps(subtotal_cents: int, rate_bps: int) -> int:
    subtotal = Decimal(subtotal_cents) / Decimal("100")
    rate = Decimal(rate_bps) / Decimal("10000")
    tax = (subtotal * rate).quantize(_MONEY_CENT, rounding=ROUND_HALF_UP)
    return int((tax * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))


def _canonicalize_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _normalize_zip5(value: Any) -> str:
    match = re.search(r"\b(\d{5})(?:-\d{4})?\b", str(value or ""))
    return match.group(1) if match else ""


def _normalize_zip4(value: Any) -> str:
    match = re.search(r"\b(\d{5}-\d{4})\b", str(value or ""))
    return match.group(1) if match else ""


def _parse_city_state(city_state: str) -> tuple[str, str]:
    text = str(city_state or "").strip()
    if "," not in text:
        return text, ""
    city, state = text.rsplit(",", 1)
    return city.strip(), state.strip().upper()


def _safe_date(value: Any, fallback: date) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return fallback
    return fallback


def _date_in_effect(target: date, start: date, end: Optional[date]) -> bool:
    if target < start:
        return False
    if end is not None and target > end:
        return False
    return True


def _dataset_is_active_for_date(dataset: TaxSourceDataset, on_date: date) -> bool:
    if dataset.status != "active":
        return False
    return _date_in_effect(on_date, dataset.effective_from, dataset.effective_to)


def _compute_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _compute_source_digest(path: Path) -> str:
    if path.is_file():
        return source_hash(path)

    if not path.is_dir():
        raise ValueError(f"source path does not exist: {path}")

    hashes: list[tuple[str, str]] = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            relative = str(child.relative_to(path))
            hashes.append((relative, source_hash(child)))
    payload = {"path": str(path), "files": hashes}
    return _compute_hash(payload)


def canonical_provenance_type(source_type: str) -> str:
    normalized = str(source_type or "").strip().lower()
    return _LEGACY_SOURCE_TYPE_MAP.get(normalized, normalized or PROVENANCE_MANUAL_UNVERIFIED)


def is_authoritative_provenance(source_type: str) -> bool:
    return canonical_provenance_type(source_type) in _AUTHORITATIVE_PROVENANCE


def validate_provenance_for_activation(dataset: NormalizedTaxDataset) -> list[str]:
    errors: list[str] = []
    provenance = canonical_provenance_type(dataset.source_type)
    if provenance == PROVENANCE_SYNTHETIC_TEST:
        errors.append("synthetic_test datasets cannot be activated")
        return errors

    if provenance in _AUTHORITATIVE_PROVENANCE:
        reference = str(dataset.source_reference or "").strip()
        if not reference:
            errors.append("authoritative datasets require source_reference")
        if "http" not in reference:
            errors.append("authoritative datasets require URL source_reference")
        if str(dataset.version_tag or "").strip().lower() in {"", "unknown"}:
            errors.append("authoritative datasets require stable version_tag")

    return errors


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _find_active_dataset(on_date: date) -> Optional[TaxSourceDataset]:
    rows = (
        TaxSourceDataset.query
        .filter_by(status="active")
        .order_by(TaxSourceDataset.imported_at.desc())
        .all()
    )
    for row in rows:
        if _dataset_is_active_for_date(row, on_date):
            return row
    return None


def _upsert_jurisdiction(*, jurisdiction_type: str, canonical_code: str, state: str, name: str) -> TaxJurisdiction:
    row = TaxJurisdiction.query.filter_by(jurisdiction_type=jurisdiction_type, canonical_code=canonical_code).first()
    if row is None:
        row = TaxJurisdiction(
            jurisdiction_type=jurisdiction_type,
            canonical_code=canonical_code,
            state=state,
            name=name,
        )
        db.session.add(row)
        db.session.flush()
    else:
        row.state = state
        row.name = name
    return row


def ensure_bootstrap_tax_dataset() -> TaxSourceDataset:
    today = date.today()
    active = _find_active_dataset(today)
    if active is not None:
        return active

    fixture_path = Path(os.path.dirname(__file__)).parent / "data" / "tax" / "official" / "public_state_rates_2026q3.json"
    if not fixture_path.exists():
        fixture_path = Path(os.getcwd()) / "data" / "tax" / "official" / "public_state_rates_2026q3.json"

    payload = _load_json(fixture_path)
    source_hash = _compute_hash(payload)
    effective_from = _safe_date(payload.get("effective_from"), today)
    effective_to_raw = payload.get("effective_to")
    effective_to = _safe_date(effective_to_raw, today) if effective_to_raw else None

    staged = TaxSourceDataset(
        source_key=str(payload.get("source_key") or "public_state_rates").strip(),
        source_type=canonical_provenance_type(str(payload.get("source_type") or PROVENANCE_MANUAL_UNVERIFIED)),
        jurisdiction_state=None,
        source_name=str(payload.get("source_name") or "Public planning rates snapshot").strip(),
        source_reference=str(payload.get("source_reference") or "").strip() or None,
        source_hash=source_hash,
        version_tag=str(payload.get("version_tag") or "unknown").strip(),
        published_at=datetime.now(timezone.utc),
        effective_from=effective_from,
        effective_to=effective_to,
        imported_at=datetime.now(timezone.utc),
        status="staged",
    )
    db.session.add(staged)
    db.session.flush()

    sanity_limit_bps = 2000
    for row in payload.get("states") or []:
        state = str(row.get("state") or "").strip().upper()
        if len(state) != 2:
            raise ValueError("invalid state in official tax fixture")

        general_bps = int(row.get("general_rate_bps") or 0)
        grocery_bps = int(row.get("grocery_rate_bps") or 0)
        prepared_bps = int(row.get("prepared_rate_bps") or general_bps)

        for rate in (general_bps, grocery_bps, prepared_bps):
            if rate < 0 or rate > sanity_limit_bps:
                raise ValueError("official tax fixture contains invalid bps")

        jurisdiction = _upsert_jurisdiction(
            jurisdiction_type="state",
            canonical_code=f"STATE:{state}",
            state=state,
            name=state,
        )
        rates = [
            (TAX_CLASS_GENERAL_MERCHANDISE, general_bps),
            (TAX_CLASS_GROCERY_FOOD, grocery_bps),
            (TAX_CLASS_PREPARED_FOOD, prepared_bps),
            (TAX_CLASS_EXEMPT, 0),
        ]
        for tax_class, bps in rates:
            db.session.add(
                TaxRate(
                    dataset_id=staged.id,
                    jurisdiction_id=jurisdiction.id,
                    tax_code=f"{state}-STATE",
                    tax_class=tax_class,
                    rate_basis_points=bps,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    source_confidence=str(row.get("confidence") or CONFIDENCE_MEDIUM),
                )
            )

        db.session.add(
            TaxBoundaryAssignment(
                dataset_id=staged.id,
                geographic_key_type="state",
                geographic_key=state,
                assignment_precision=LOCATION_PRECISION_STATE_ONLY,
                jurisdiction_id=jurisdiction.id,
                tax_code=f"{state}-STATE",
                effective_from=effective_from,
                effective_to=effective_to,
                source_confidence=str(row.get("confidence") or CONFIDENCE_MEDIUM),
            )
        )

        db.session.add(
            TaxabilityRule(
                dataset_id=staged.id,
                jurisdiction_id=jurisdiction.id,
                state=state,
                tax_class=TAX_CLASS_UNKNOWN,
                treatment="fallback_to_general",
                override_rate_basis_points=general_bps,
                effective_from=effective_from,
                effective_to=effective_to,
                source_confidence=CONFIDENCE_MEDIUM,
            )
        )

    staged.status = "active"
    db.session.commit()
    record_usage_event(
        owner_scope="system",
        category="tax_engine",
        provider="rung_owned",
        operation="tax_dataset_import_success",
        success=True,
        external_call=False,
        request_count=1,
        metadata={"source_key": staged.source_key, "version": staged.version_tag},
    )
    return staged


def _state_rates_for_dataset(dataset_id: int, jurisdiction_id: int, on_date: date) -> dict[str, int]:
    rows = (
        TaxRate.query
        .filter_by(dataset_id=dataset_id, jurisdiction_id=jurisdiction_id)
        .all()
    )
    out = {
        TAX_CLASS_GENERAL_MERCHANDISE: 0,
        TAX_CLASS_GROCERY_FOOD: 0,
        TAX_CLASS_PREPARED_FOOD: 0,
    }
    for row in rows:
        if not _date_in_effect(on_date, row.effective_from, row.effective_to):
            continue
        if row.tax_class in out:
            out[row.tax_class] = int(row.rate_basis_points or 0)
    if out[TAX_CLASS_PREPARED_FOOD] == 0:
        out[TAX_CLASS_PREPARED_FOOD] = out[TAX_CLASS_GENERAL_MERCHANDISE]
    return out


def _resolve_boundary(
    *,
    dataset_id: int,
    calculation_date: date,
    address: str,
    zip_code: str,
    city: str,
    state: str,
) -> tuple[Optional[TaxBoundaryAssignment], str, str]:
    normalized_address = _canonicalize_text(address)
    zip4 = _normalize_zip4(zip_code)
    zip5 = _normalize_zip5(zip_code)
    city_state = f"{_canonicalize_text(city)}|{state}" if city and state else ""

    candidates = [
        ("address", normalized_address, LOCATION_PRECISION_EXACT_ADDRESS),
        ("zip4", zip4, LOCATION_PRECISION_ZIP_PLUS_4),
        ("zip5", zip5, LOCATION_PRECISION_ZIP5),
        ("city_state", city_state, LOCATION_PRECISION_CITY_COUNTY),
        ("state", state, LOCATION_PRECISION_STATE_ONLY),
    ]

    for key_type, key, precision in candidates:
        if not key:
            continue
        row = (
            TaxBoundaryAssignment.query
            .filter_by(dataset_id=dataset_id, geographic_key_type=key_type, geographic_key=key)
            .order_by(TaxBoundaryAssignment.effective_from.desc())
            .first()
        )
        if row is None:
            continue
        if _date_in_effect(calculation_date, row.effective_from, row.effective_to):
            confidence = CONFIDENCE_HIGH if precision in {LOCATION_PRECISION_EXACT_ADDRESS, LOCATION_PRECISION_ZIP_PLUS_4} else (
                CONFIDENCE_MEDIUM if precision in {LOCATION_PRECISION_ZIP5, LOCATION_PRECISION_CITY_COUNTY} else CONFIDENCE_LOW
            )
            return row, precision, confidence
    return None, LOCATION_PRECISION_UNRESOLVED, CONFIDENCE_LOW


def _lookup_profile_cache(
    *,
    retailer: str,
    retailer_store_id: str,
    calculation_date: date,
    dataset: TaxSourceDataset,
    normalized_address: str,
) -> Optional[StoreTaxProfileResult]:
    cached = (
        StoreTaxProfile.query
        .filter_by(retailer=retailer, retailer_store_id=retailer_store_id)
        .order_by(StoreTaxProfile.resolved_at.desc())
        .first()
    )
    if cached is None:
        return None
    if not _date_in_effect(calculation_date, cached.effective_from, cached.effective_to):
        return None
    if int(cached.source_dataset_id or 0) != int(dataset.id):
        return None
    if (cached.normalized_address or "") != normalized_address:
        return None

    return StoreTaxProfileResult(
        retailer=retailer,
        retailer_store_id=retailer_store_id,
        state=str(cached.state or "").upper(),
        location_precision=cached.location_precision,
        confidence=cached.confidence,
        status=cached.status,
        general_rate_bps=int(cached.general_rate_basis_points or 0),
        grocery_rate_bps=int(cached.grocery_rate_basis_points or 0),
        prepared_rate_bps=int(cached.prepared_rate_basis_points or cached.general_rate_basis_points or 0),
        resolved_tax_code=cached.resolved_tax_code,
        source_key=dataset.source_key,
        source_version=cached.source_version,
        source_hash=cached.source_hash,
        effective_from=cached.effective_from,
        effective_to=cached.effective_to,
        degraded_reason=None if cached.location_precision != LOCATION_PRECISION_STATE_ONLY else "state_only_fallback",
    )


def resolve_store_tax_profile(
    *,
    retailer: str,
    retailer_store_id: str,
    store_name: str,
    store_address: str,
    zip_code: str,
    city_state: str,
    latitude: Optional[float],
    longitude: Optional[float],
    calculation_date: date,
    owner_scope: str,
) -> StoreTaxProfileResult:
    dataset = ensure_bootstrap_tax_dataset()
    retailer_norm = str(retailer or "").strip().lower() or "unknown"
    store_id_norm = str(retailer_store_id or "").strip() or "unknown"
    normalized_address = _canonicalize_text(store_address)

    city, state_from_city = _parse_city_state(city_state)
    zip5 = _normalize_zip5(zip_code)
    state = state_from_city
    if len(state) != 2 and normalized_address:
        tokens = normalized_address.split()
        if tokens:
            maybe_state = tokens[-2].upper() if len(tokens) >= 2 else ""
            if len(maybe_state) == 2 and maybe_state.isalpha():
                state = maybe_state

    cached = _lookup_profile_cache(
        retailer=retailer_norm,
        retailer_store_id=store_id_norm,
        calculation_date=calculation_date,
        dataset=dataset,
        normalized_address=normalized_address,
    )
    if cached is not None:
        record_usage_event(
            owner_scope=owner_scope,
            category="tax_engine",
            provider="rung_owned",
            operation="store_tax_profile_hits",
            success=True,
            external_call=False,
            request_count=1,
        )
        return cached

    record_usage_event(
        owner_scope=owner_scope,
        category="tax_engine",
        provider="rung_owned",
        operation="store_tax_profile_misses",
        success=True,
        external_call=False,
        request_count=1,
    )

    boundary, precision, confidence = _resolve_boundary(
        dataset_id=dataset.id,
        calculation_date=calculation_date,
        address=store_address,
        zip_code=zip_code,
        city=city,
        state=state,
    )

    degraded_reason = None
    jurisdiction_id: Optional[int] = None
    resolved_tax_code: Optional[str] = None
    if boundary is None and state:
        boundary = (
            TaxBoundaryAssignment.query
            .filter_by(dataset_id=dataset.id, geographic_key_type="state", geographic_key=state)
            .first()
        )
        precision = LOCATION_PRECISION_STATE_ONLY if boundary is not None else LOCATION_PRECISION_UNRESOLVED
        confidence = CONFIDENCE_LOW
        degraded_reason = "state_only_fallback" if boundary is not None else "unresolved"

    general_rate_bps = 0
    grocery_rate_bps = 0
    prepared_rate_bps = 0

    if boundary is not None:
        jurisdiction_id = int(boundary.jurisdiction_id)
        resolved_tax_code = boundary.tax_code
        rates = _state_rates_for_dataset(dataset.id, jurisdiction_id, calculation_date)
        general_rate_bps = int(rates[TAX_CLASS_GENERAL_MERCHANDISE])
        grocery_rate_bps = int(rates[TAX_CLASS_GROCERY_FOOD])
        prepared_rate_bps = int(rates[TAX_CLASS_PREPARED_FOOD])
    else:
        degraded_reason = "unresolved"

    if precision == LOCATION_PRECISION_UNRESOLVED:
        record_usage_event(
            owner_scope=owner_scope,
            category="tax_engine",
            provider="rung_owned",
            operation="tax_low_precision_calculation",
            success=True,
            external_call=False,
            request_count=1,
        )

    profile = StoreTaxProfile(
        retailer=retailer_norm,
        retailer_store_id=store_id_norm,
        store_name=str(store_name or "").strip() or None,
        normalized_address=normalized_address or None,
        postal_code=zip5 or None,
        city=city or None,
        county=None,
        state=state or None,
        latitude=latitude,
        longitude=longitude,
        resolved_jurisdiction_id=jurisdiction_id,
        resolved_tax_code=resolved_tax_code,
        location_precision=precision,
        confidence=confidence,
        status="resolved" if boundary is not None else "unresolved",
        general_rate_basis_points=general_rate_bps,
        grocery_rate_basis_points=grocery_rate_bps,
        prepared_rate_basis_points=prepared_rate_bps,
        effective_from=dataset.effective_from,
        effective_to=dataset.effective_to,
        source_dataset_id=dataset.id,
        source_version=dataset.version_tag,
        source_hash=dataset.source_hash,
        resolved_at=datetime.now(timezone.utc),
    )
    db.session.add(profile)
    db.session.commit()

    return StoreTaxProfileResult(
        retailer=retailer_norm,
        retailer_store_id=store_id_norm,
        state=state,
        location_precision=precision,
        confidence=confidence,
        status="resolved" if boundary is not None else "unresolved",
        general_rate_bps=general_rate_bps,
        grocery_rate_bps=grocery_rate_bps,
        prepared_rate_bps=prepared_rate_bps,
        resolved_tax_code=resolved_tax_code,
        source_key=dataset.source_key,
        source_version=dataset.version_tag,
        source_hash=dataset.source_hash,
        effective_from=dataset.effective_from,
        effective_to=dataset.effective_to,
        degraded_reason=degraded_reason,
    )


def classify_tax_class_for_item(item: dict[str, Any]) -> tuple[str, str]:
    selected = item.get("selected_product") if isinstance(item.get("selected_product"), dict) else {}
    retailer = str((selected or {}).get("retailer") or item.get("retailer") or "").strip().lower()
    product_id = str((selected or {}).get("product_id") or "").strip() or None
    upc = str((selected or {}).get("upc") or "").strip() or None

    if retailer and (product_id or upc):
        query = RetailProductTaxClass.query.filter_by(retailer=retailer)
        if product_id:
            row = query.filter_by(retailer_product_id=product_id).first()
            if row is not None:
                return row.canonical_tax_class, "stored_product_tax_class"
        if upc:
            row = query.filter_by(upc=upc).first()
            if row is not None:
                return row.canonical_tax_class, "stored_upc_tax_class"

    evidence_parts = [
        item.get("keyword"),
        item.get("item_name"),
        item.get("product_label"),
        (selected or {}).get("title"),
        (selected or {}).get("brand"),
        (selected or {}).get("variant"),
        (selected or {}).get("category"),
        (selected or {}).get("department"),
    ]
    text = _canonicalize_text(" ".join(str(part or "") for part in evidence_parts))
    tokens = set(text.split())

    if tokens & _EXEMPT_TOKENS:
        result = TAX_CLASS_EXEMPT
        reason = "keyword_exempt"
    elif tokens & _PREPARED_TOKENS:
        result = TAX_CLASS_PREPARED_FOOD
        reason = "keyword_prepared"
    elif tokens & _GENERAL_TOKENS:
        result = TAX_CLASS_GENERAL_MERCHANDISE
        reason = "keyword_general"
    elif tokens & _FOOD_TOKENS:
        result = TAX_CLASS_GROCERY_FOOD
        reason = "keyword_food"
    else:
        result = TAX_CLASS_UNKNOWN
        reason = "insufficient_evidence"

    if retailer and (product_id or upc) and result != TAX_CLASS_UNKNOWN:
        row = RetailProductTaxClass(
            retailer=retailer,
            retailer_product_id=product_id,
            upc=upc,
            canonical_tax_class=result,
            source="deterministic_mapping",
            confidence=CONFIDENCE_MEDIUM,
        )
        try:
            db.session.add(row)
            db.session.commit()
        except Exception:
            db.session.rollback()

    return result, reason


def calculate_cart_tax(
    *,
    store_tax_profile: StoreTaxProfileResult,
    cart_items: list[dict[str, Any]],
    calculation_date: date,
    owner_scope: str,
) -> CartTaxResult:
    del calculation_date

    subtotal_by_class = {
        TAX_CLASS_GROCERY_FOOD: 0,
        TAX_CLASS_GENERAL_MERCHANDISE: 0,
        TAX_CLASS_PREPARED_FOOD: 0,
        TAX_CLASS_EXEMPT: 0,
        TAX_CLASS_UNKNOWN: 0,
    }
    tax_by_class = {
        TAX_CLASS_GROCERY_FOOD: 0,
        TAX_CLASS_GENERAL_MERCHANDISE: 0,
        TAX_CLASS_PREPARED_FOOD: 0,
        TAX_CLASS_EXEMPT: 0,
        TAX_CLASS_UNKNOWN: 0,
    }

    rates = {
        TAX_CLASS_GROCERY_FOOD: int(store_tax_profile.grocery_rate_bps),
        TAX_CLASS_GENERAL_MERCHANDISE: int(store_tax_profile.general_rate_bps),
        TAX_CLASS_PREPARED_FOOD: int(store_tax_profile.prepared_rate_bps),
        TAX_CLASS_EXEMPT: 0,
        TAX_CLASS_UNKNOWN: int(store_tax_profile.general_rate_bps),
    }

    unknown_count = 0
    for item in cart_items:
        line_price = Decimal(str(item.get("estimated_price") or 0)).quantize(_MONEY_CENT, rounding=ROUND_HALF_UP)
        subtotal_cents = int((line_price * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
        item_class, class_reason = classify_tax_class_for_item(item)
        if item_class == TAX_CLASS_UNKNOWN:
            unknown_count += 1
            record_usage_event(
                owner_scope=owner_scope,
                category="tax_engine",
                provider="rung_owned",
                operation="tax_unknown_item_class",
                success=True,
                external_call=False,
                request_count=1,
                metadata={"reason": class_reason},
            )

        applied_rate_bps = rates.get(item_class, rates[TAX_CLASS_GENERAL_MERCHANDISE])
        line_tax_cents = rounding_cents_from_subtotal_and_rate_bps(subtotal_cents, applied_rate_bps)

        subtotal_by_class[item_class] += subtotal_cents
        tax_by_class[item_class] += line_tax_cents

        item["tax_class"] = item_class
        item["tax_class_reason"] = class_reason
        item["tax_rate_bps"] = applied_rate_bps
        item["line_subtotal_cents"] = subtotal_cents
        item["line_tax_cents"] = line_tax_cents

    subtotal_cents = sum(subtotal_by_class.values())
    tax_cents = sum(tax_by_class.values())

    degraded = store_tax_profile.degraded_reason
    if store_tax_profile.location_precision in {LOCATION_PRECISION_STATE_ONLY, LOCATION_PRECISION_UNRESOLVED}:
        record_usage_event(
            owner_scope=owner_scope,
            category="tax_engine",
            provider="rung_owned",
            operation="tax_state_fallback_used",
            success=True,
            external_call=False,
            request_count=1,
        )

    record_usage_event(
        owner_scope=owner_scope,
        category="tax_engine",
        provider="rung_owned",
        operation="tax_calculation_requests",
        success=True,
        external_call=False,
        request_count=1,
    )

    return CartTaxResult(
        subtotal_cents=subtotal_cents,
        tax_cents=tax_cents,
        estimated_total_cents=subtotal_cents + tax_cents,
        subtotal_by_class_cents=subtotal_by_class,
        tax_by_class_cents=tax_by_class,
        effective_rate_bps=rates,
        precision=store_tax_profile.location_precision,
        confidence=store_tax_profile.confidence,
        source_version=store_tax_profile.source_version,
        unknown_class_count=unknown_count,
        degraded_reason=degraded,
    )


def cents_to_float(value: int) -> float:
    return float((Decimal(value) / Decimal("100")).quantize(_MONEY_CENT, rounding=ROUND_HALF_UP))


def has_paid_provider_tax_keys() -> bool:
    return bool(str(os.environ.get("TAXJAR_API_KEY") or "").strip() or str(os.environ.get("API_NINJAS_API_KEY") or "").strip())


def _persist_normalized_dataset(dataset: NormalizedTaxDataset, *, digest: str, status: str) -> TaxSourceDataset:
    staged = TaxSourceDataset(
        source_key=dataset.source_key,
        source_type=dataset.source_type,
        jurisdiction_state=None,
        source_name=dataset.source_name,
        source_reference=dataset.source_reference,
        source_hash=digest,
        version_tag=dataset.version_tag,
        published_at=datetime.now(timezone.utc),
        effective_from=dataset.effective_from,
        effective_to=dataset.effective_to,
        imported_at=datetime.now(timezone.utc),
        status=status,
    )
    db.session.add(staged)
    db.session.flush()

    for record in dataset.records:
        jurisdiction = _upsert_jurisdiction(
            jurisdiction_type=record.jurisdiction_type,
            canonical_code=record.jurisdiction_code,
            state=record.state,
            name=record.jurisdiction_name,
        )
        db.session.add(
            TaxBoundaryAssignment(
                dataset_id=staged.id,
                geographic_key_type=record.assignment_key_type,
                geographic_key=record.assignment_key,
                assignment_precision=record.assignment_precision,
                jurisdiction_id=jurisdiction.id,
                tax_code=record.tax_code,
                effective_from=record.effective_from,
                effective_to=record.effective_to,
                source_confidence=record.confidence,
            )
        )
        for tax_class, bps in (
            (TAX_CLASS_GENERAL_MERCHANDISE, record.general_rate_bps),
            (TAX_CLASS_GROCERY_FOOD, record.grocery_rate_bps),
            (TAX_CLASS_PREPARED_FOOD, record.prepared_rate_bps),
            (TAX_CLASS_EXEMPT, 0),
        ):
            db.session.add(
                TaxRate(
                    dataset_id=staged.id,
                    jurisdiction_id=jurisdiction.id,
                    tax_code=record.tax_code,
                    tax_class=tax_class,
                    rate_basis_points=int(bps),
                    effective_from=record.effective_from,
                    effective_to=record.effective_to,
                    source_confidence=record.confidence,
                )
            )
    return staged


def import_dataset_atomic(*, adapter: TaxDataAdapter, source_path: str, activate: bool = True) -> dict[str, Any]:
    path = Path(source_path)
    active_before = _find_active_dataset(date.today())

    try:
        normalized = adapter.parse_source(path)
        normalized.source_type = canonical_provenance_type(normalized.source_type)
        errors = adapter.validate_records(normalized)
        if errors:
            raise ValueError("; ".join(errors))

        if activate:
            provenance_errors = validate_provenance_for_activation(normalized)
            if provenance_errors:
                raise ValueError("; ".join(provenance_errors))

        existing = TaxSourceDataset.query.filter_by(
            source_key=normalized.source_key,
            version_tag=normalized.version_tag,
        ).first()
        if existing is not None:
            if activate and existing.status != "active":
                TaxSourceDataset.query.filter_by(status="active").update({"status": "inactive"})
                existing.status = "active"
                db.session.commit()
            return {
                "ok": True,
                "dataset_id": existing.id,
                "version": existing.version_tag,
                "status": existing.status,
                "provenance_type": canonical_provenance_type(existing.source_type),
                "authoritative": is_authoritative_provenance(existing.source_type),
                "idempotent": True,
            }

        digest = _compute_source_digest(path)
        staged = _persist_normalized_dataset(normalized, digest=digest, status="staged")

        if activate:
            TaxSourceDataset.query.filter_by(status="active").update({"status": "inactive"})
            staged.status = "active"

        db.session.commit()
        record_usage_event(
            owner_scope="system",
            category="tax_engine",
            provider="rung_owned",
            operation="tax_dataset_import_success",
            success=True,
            external_call=False,
            request_count=1,
            metadata={"source_key": normalized.source_key, "version": normalized.version_tag},
        )
        return {
            "ok": True,
            "dataset_id": staged.id,
            "version": staged.version_tag,
            "status": staged.status,
            "provenance_type": canonical_provenance_type(staged.source_type),
            "authoritative": is_authoritative_provenance(staged.source_type),
        }
    except Exception as exc:
        db.session.rollback()
        record_usage_event(
            owner_scope="system",
            category="tax_engine",
            provider="rung_owned",
            operation="tax_dataset_import_failure",
            success=False,
            external_call=False,
            request_count=1,
            metadata={"error": str(exc)},
        )
        active_after = _find_active_dataset(date.today())
        return {
            "ok": False,
            "error": str(exc),
            "active_dataset_unchanged": bool(
                (active_before is None and active_after is None)
                or (active_before is not None and active_after is not None and active_before.id == active_after.id)
            ),
        }
