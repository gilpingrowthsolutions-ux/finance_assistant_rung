from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from extensions import db
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from models import RetailProductBlock, RetailProductCache, RetailProductPreference, RetailProductSubstitution
from services.household_context import household_id as current_household_id
from services.retail import RetailStore, ShoppingRequirement, WalmartSerpApiProvider

PREFERENCE_TYPES = {"usual", "favorite"}
RETAILER_CANONICAL = {
    "walmart": "walmart",
    "kroger": "kroger",
    "gerbes": "gerbes",
}


def normalize_base_item(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def normalize_brand(value: str) -> str:
    return normalize_base_item(value)


def save_product_block(*, block_type: str, retailer: str | None = None, product_id: str | None = None,
                       us_item_id: str | None = None, brand: str | None = None) -> RetailProductBlock:
    block_type = str(block_type or '').strip().lower()
    retailer = normalize_retailer(retailer)
    if block_type == 'exact_product':
        if retailer not in {'walmart', 'kroger', 'gerbes'}: raise ValueError('An exact product block requires a supported retailer.')
        product_id, us_item_id = _text(product_id), _text(us_item_id)
        identity = us_item_id or product_id
        if not identity: raise ValueError('An exact retailer product identity is required.')
        values = {'retailer_product_id': product_id, 'retailer_us_item_id': us_item_id, 'normalized_brand': None, 'block_key': f'exact:{retailer}:{identity}'}
    elif block_type == 'brand':
        normalized = normalize_brand(str(brand or ''))
        if not normalized: raise ValueError('A brand is required.')
        if retailer is not None: raise ValueError('A brand block is household-wide and cannot be retailer-specific.')
        values = {'retailer_product_id': None, 'retailer_us_item_id': None, 'normalized_brand': normalized, 'block_key': f'brand:{normalized}'}
    else: raise ValueError('block_type must be exact_product or brand.')
    hid = current_household_id()
    if block_type == 'brand':
        row = RetailProductBlock.query.filter_by(household_id=hid, block_key=values['block_key']).first()
        if row is None:
            row = RetailProductBlock(household_id=hid, block_type=block_type, retailer=retailer, **values)
            db.session.add(row)
        db.session.commit()
        return row

    def matching_rows() -> list[RetailProductBlock]:
        predicates = []
        if product_id:
            predicates.append(RetailProductBlock.retailer_product_id == product_id)
        if us_item_id:
            predicates.append(RetailProductBlock.retailer_us_item_id == us_item_id)
        return RetailProductBlock.query.filter_by(household_id=hid, block_type='exact_product', retailer=retailer).filter(or_(*predicates)).order_by(RetailProductBlock.id).all()

    try:
        # Keep a losing concurrent insert confined to its savepoint.  The
        # endpoint still intentionally commits this standalone preference
        # mutation, consistent with existing preference-service writes.
        with db.session.begin_nested():
            rows = matching_rows()
            row = rows[0] if rows else None
            # A later provider observation that contains both forms proves
            # only those two IDs equivalent.  Never infer a relationship from
            # titles, brands, UPCs, or raw provider strings.
            if row is not None:
                for duplicate in rows[1:]:
                    db.session.delete(duplicate)
                row.retailer_product_id = row.retailer_product_id or product_id
                row.retailer_us_item_id = row.retailer_us_item_id or us_item_id
            else:
                row = RetailProductBlock(household_id=hid, block_type='exact_product', retailer=retailer, **values)
                db.session.add(row)
            db.session.flush()
    except IntegrityError:
        # The partial unique indexes are the concurrency backstop.  Roll only
        # this savepoint, then attach the now-visible canonical block.
        rows = matching_rows()
        if not rows:
            raise
        row = rows[0]
        for duplicate in rows[1:]:
            db.session.delete(duplicate)
        row.retailer_product_id = row.retailer_product_id or product_id
        row.retailer_us_item_id = row.retailer_us_item_id or us_item_id
    db.session.commit()
    return row


def candidate_is_blocked(candidate: dict[str, Any], *, retailer: str | None = None, household_id: int | None = None) -> bool:
    rows = RetailProductBlock.query.filter_by(household_id=household_id or current_household_id()).all()
    actual_retailer = normalize_retailer(retailer or candidate.get('retailer'))
    identities = {str(candidate.get('product_id') or ''), str(candidate.get('us_item_id') or '')}
    brand = normalize_brand(str(candidate.get('brand') or ''))
    for row in rows:
        if row.retailer and normalize_retailer(row.retailer) != actual_retailer: continue
        if row.block_type == 'exact_product' and ({str(row.retailer_product_id or ''), str(row.retailer_us_item_id or '')} & identities - {''}): return True
        if row.block_type == 'brand' and brand and brand == row.normalized_brand: return True
    return False


def product_block_to_dict(row: RetailProductBlock) -> dict[str, Any]:
    return {
        'id': row.id, 'block_type': row.block_type, 'retailer': row.retailer,
        'product_id': row.retailer_product_id, 'us_item_id': row.retailer_us_item_id,
        'brand': row.normalized_brand, 'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def remove_product_block(block_id: int) -> bool:
    row = RetailProductBlock.query.filter_by(id=block_id, household_id=current_household_id()).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def list_product_blocks() -> list[RetailProductBlock]:
    return RetailProductBlock.query.filter_by(household_id=current_household_id()).order_by(RetailProductBlock.id).all()


def filter_automatic_candidates(requirement: ShoppingRequirement, candidates: Iterable[dict[str, Any]], *, retailer: str,
                                explicit_product_id: str | None = None) -> list[dict[str, Any]]:
    """Blocks are eligibility filters; an explicit current SKU/brand overrides only itself."""
    requested_ids = {str(getattr(requirement, 'requested_product_id', '') or ''), str(explicit_product_id or '')}
    explicit_brand = normalize_brand(requirement.brand or '')
    result = []
    for candidate in candidates:
        if not candidate_is_blocked(candidate, retailer=retailer): result.append(candidate); continue
        identity = {str(candidate.get('product_id') or ''), str(candidate.get('us_item_id') or '')}
        if (requested_ids - {''}) & identity or (explicit_brand and explicit_brand == normalize_brand(candidate.get('brand') or '')):
            result.append(candidate)
    return result


def normalize_retailer(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    return RETAILER_CANONICAL.get(normalized, normalized)


def get_product_preference(base_item: str, *, retailer: Optional[str] = None) -> Optional[RetailProductPreference]:
    normalized = normalize_base_item(base_item)
    rows = RetailProductPreference.query.filter_by(household_id=current_household_id(), normalized_base_item=normalized).all()
    requested_retailer = normalize_retailer(retailer)
    if requested_retailer:
        current_retailer_rows = [
            row for row in rows
            if normalize_retailer(row.retailer) == requested_retailer
        ]
        if current_retailer_rows:
            rows = current_retailer_rows
        else:
            # Cross-retailer fallback is permitted only when identity is UPC-safe.
            rows = [
                row for row in rows
                if normalize_retailer(row.retailer) != requested_retailer and bool((row.upc or "").strip())
            ]
    if not rows:
        return None
    priority = {"favorite": 2, "usual": 1}
    return max(rows, key=lambda row: (priority.get(row.preference_type, 0), row.updated_at, row.id))


def save_product_preference(
    *,
    base_item: str,
    preference_type: str,
    retailer: str,
    store_id: str,
    product_identity: str,
    provider: Optional[Any] = None,
) -> tuple[RetailProductPreference, int]:
    preference_type = str(preference_type or "").strip().lower()
    if preference_type not in PREFERENCE_TYPES:
        raise ValueError("preference_type must be 'usual' or 'favorite'.")
    normalized = normalize_base_item(base_item)
    if not normalized:
        raise ValueError("base_item is required.")
    retailer = normalize_retailer(retailer)
    if retailer not in {"walmart", "kroger", "gerbes"}:
        raise ValueError("Unsupported retailer for product preference.")

    candidate = _find_verified_cached_candidate(normalized, product_identity, retailer=retailer, store_id=store_id)
    if candidate is None:
        raise ValueError("Selected product is not present in the verified retailer candidate cache.")

    detail_calls = 0
    if not candidate.get("upc"):
        detail_id = candidate.get("us_item_id") or candidate.get("product_id")
        if detail_id:
            resolver = provider or WalmartSerpApiProvider()
            store_data = candidate.get("store") or {}
            store = RetailStore(
                store_id=str(store_data.get("store_id") or "357"),
                name=store_data.get("name") or "Walmart — Versailles",
                address=store_data.get("address") or "1003 W Newton St, Versailles, MO 65084",
                postal_code=store_data.get("postal_code") or "65084",
                verified=bool(store_data.get("verified", True)),
            )
            detail = resolver.get_product(
                str(detail_id),
                store=store,
                requested_query=str(candidate.get("requested_query") or base_item),
            )
            detail_calls = 1
            candidate = {**candidate, **detail.to_dict()}

    row = RetailProductPreference.query.filter_by(
        household_id=current_household_id(),
        normalized_base_item=normalized,
        preference_type=preference_type,
        retailer=retailer,
    ).first()
    previous_identity = (
        row.upc or row.retailer_us_item_id or row.retailer_product_id
        if row is not None else None
    )
    next_identity = candidate.get("upc") or candidate.get("us_item_id") or candidate.get("product_id")
    values = {
        "base_item": str(base_item).strip(),
        "preferred_brand": _text(candidate.get("brand")),
        "preferred_variant": _text(candidate.get("variant")),
        "preferred_package_size": _text(candidate.get("package_size")),
        "preferred_product_title": str(candidate.get("title") or "").strip(),
        "upc": _text(candidate.get("upc")),
        "retailer": retailer,
        "retailer_product_id": _text(candidate.get("product_id")),
        "retailer_us_item_id": _text(candidate.get("us_item_id")),
        "source": "user_explicit",
        "updated_at": datetime.now(timezone.utc),
    }
    if not values["preferred_product_title"]:
        raise ValueError("Selected product title is missing.")
    if row is None:
        row = RetailProductPreference(
            household_id=current_household_id(),
            normalized_base_item=normalized,
            preference_type=preference_type,
            **values,
        )
        db.session.add(row)
    else:
        if previous_identity and next_identity and str(previous_identity) != str(next_identity):
            RetailProductSubstitution.query.filter_by(
                household_id=current_household_id(),
                preferred_preference_id=row.id,
            ).delete(
                synchronize_session=False
            )
        for key, value in values.items():
            setattr(row, key, value)
    db.session.commit()
    return row, detail_calls


def forget_product_preference(
    base_item: str,
    preference_type: Optional[str] = None,
    *,
    retailer: Optional[str] = None,
) -> int:
    normalized = normalize_base_item(base_item)
    query = RetailProductPreference.query.filter_by(normalized_base_item=normalized)
    query = query.filter_by(household_id=current_household_id())
    if preference_type:
        normalized_type = str(preference_type).strip().lower()
        if normalized_type not in PREFERENCE_TYPES:
            raise ValueError("preference_type must be 'usual' or 'favorite'.")
        query = query.filter_by(preference_type=normalized_type)
    normalized_retailer = normalize_retailer(retailer)
    if normalized_retailer:
        query = query.filter(db.func.lower(RetailProductPreference.retailer) == normalized_retailer)
    preference_ids = [row.id for row in query.all()]
    if preference_ids:
        RetailProductSubstitution.query.filter(
            RetailProductSubstitution.household_id == current_household_id(),
            RetailProductSubstitution.preferred_preference_id.in_(preference_ids)
        ).delete(synchronize_session=False)
    count = query.delete(synchronize_session=False)
    db.session.commit()
    return count


def preference_to_dict(row: RetailProductPreference) -> dict[str, Any]:
    return {
        "id": row.id,
        "base_item": row.base_item,
        "normalized_base_item": row.normalized_base_item,
        "preference_type": row.preference_type,
        "preferred_brand": row.preferred_brand,
        "preferred_variant": row.preferred_variant,
        "preferred_package_size": row.preferred_package_size,
        "preferred_product_title": row.preferred_product_title,
        "upc": row.upc,
        "retailer": row.retailer,
        "retailer_product_id": row.retailer_product_id,
        "retailer_us_item_id": row.retailer_us_item_id,
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def requirement_allows_saved_preference(requirement: ShoppingRequirement) -> bool:
    return not bool(requirement.brand or requirement.variant or requirement.requested_package_size)


def save_product_substitution(
    *,
    base_item: str,
    product_identity: str,
    retailer: str,
    store_id: str,
    provider: Optional[Any] = None,
) -> tuple[RetailProductSubstitution, int]:
    preference = get_product_preference(base_item)
    if preference is None:
        raise ValueError("A usual or favorite product is required before approving a substitute.")
    retailer = retailer.lower().strip()
    if retailer not in {"walmart", "kroger", "gerbes"}:
        raise ValueError("Unsupported retailer for product substitution.")

    normalized = normalize_base_item(base_item)
    candidate = _find_verified_cached_candidate(normalized, product_identity, retailer=retailer, store_id=store_id)
    if candidate is None:
        raise ValueError("Selected substitute is not present in the verified retailer candidate cache.")
    if _candidate_matches_preference(preference, candidate):
        raise ValueError("The preferred product cannot also be its substitute.")

    detail_calls = 0
    if not candidate.get("upc"):
        detail_id = candidate.get("us_item_id") or candidate.get("product_id")
        if detail_id:
            resolver = provider or WalmartSerpApiProvider()
            store_data = candidate.get("store") or {}
            detail = resolver.get_product(
                str(detail_id),
                store=RetailStore(
                    store_id=str(store_data.get("store_id") or "357"),
                    name=store_data.get("name") or "Walmart — Versailles",
                    address=store_data.get("address") or "1003 W Newton St, Versailles, MO 65084",
                    postal_code=store_data.get("postal_code") or "65084",
                    verified=bool(store_data.get("verified", True)),
                ),
                requested_query=str(candidate.get("requested_query") or base_item),
            )
            detail_calls = 1
            candidate = {**candidate, **detail.to_dict()}

    row = RetailProductSubstitution.query.filter_by(
        household_id=current_household_id(),
        preferred_preference_id=preference.id,
        retailer="walmart",
        retailer_us_item_id=_text(candidate.get("us_item_id")),
    ).first()
    values = {
        "base_item": str(base_item).strip(),
        "normalized_base_item": normalized,
        "substitute_brand": _text(candidate.get("brand")),
        "substitute_variant": _text(candidate.get("variant")),
        "substitute_package_size": _text(candidate.get("package_size")),
        "substitute_product_title": str(candidate.get("title") or "").strip(),
        "substitute_upc": _text(candidate.get("upc")),
        "retailer": retailer,
        "retailer_product_id": _text(candidate.get("product_id")),
        "retailer_us_item_id": _text(candidate.get("us_item_id")),
        "approval_type": "explicit",
        "updated_at": datetime.now(timezone.utc),
    }
    if row is None:
        row = RetailProductSubstitution(preferred_preference_id=preference.id, **values)
        row.household_id = current_household_id()
        db.session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.session.commit()
    return row, detail_calls


def get_product_substitutions(preference_id: int, *, retailer: Optional[str] = None) -> list[RetailProductSubstitution]:
    query = (
        RetailProductSubstitution.query
        .filter_by(household_id=current_household_id(), preferred_preference_id=preference_id, approval_type="explicit")
    )
    if retailer:
        query = query.filter(db.func.lower(RetailProductSubstitution.retailer) == retailer.lower())
    return query.order_by(RetailProductSubstitution.updated_at.desc(), RetailProductSubstitution.id.desc()).all()


def remove_product_substitution(substitution_id: int, *, base_item: str) -> int:
    normalized = normalize_base_item(base_item)
    row = RetailProductSubstitution.query.filter_by(
        household_id=current_household_id(),
        id=int(substitution_id),
        normalized_base_item=normalized,
    ).first()
    if row is None:
        return 0
    db.session.delete(row)
    db.session.commit()
    return 1


def substitution_to_dict(row: RetailProductSubstitution) -> dict[str, Any]:
    return {
        "id": row.id,
        "base_item": row.base_item,
        "normalized_base_item": row.normalized_base_item,
        "preferred_preference_id": row.preferred_preference_id,
        "substitute_brand": row.substitute_brand,
        "substitute_variant": row.substitute_variant,
        "substitute_package_size": row.substitute_package_size,
        "substitute_product_title": row.substitute_product_title,
        "substitute_upc": row.substitute_upc,
        "retailer": row.retailer,
        "retailer_product_id": row.retailer_product_id,
        "retailer_us_item_id": row.retailer_us_item_id,
        "approval_type": row.approval_type,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def match_approved_substitution(
    substitutions: Iterable[RetailProductSubstitution],
    candidates: Iterable[dict[str, Any]],
    *,
    retailer: Optional[str] = None,
) -> tuple[Optional[RetailProductSubstitution], Optional[dict[str, Any]]]:
    rows = list(candidates)
    for substitution in substitutions:
        if retailer and (substitution.retailer or "").lower() != retailer.lower():
            continue
        matched = _match_substitution_identity(substitution, rows)
        if matched is not None and matched.get("availability") != "out_of_stock":
            return substitution, matched
    return None, None


def match_preference(
    preference: RetailProductPreference,
    candidates: Iterable[dict[str, Any]],
    *,
    retailer: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    rows = list(candidates)
    preference_retailer = (preference.retailer or "").lower()
    requested_retailer = (retailer or "").lower()
    if requested_retailer and preference_retailer and preference_retailer != requested_retailer:
        # A retailer-specific identity cannot be inferred from a title or base item.
        if not preference.upc:
            return None
        rows = [row for row in rows if str(row.get("upc") or "") == str(preference.upc)]
    identity_checks = (
        ("upc", preference.upc),
        ("us_item_id", preference.retailer_us_item_id),
        ("product_id", preference.retailer_product_id),
    )
    for field, expected in identity_checks:
        if not expected:
            continue
        match = next((row for row in rows if str(row.get(field) or "") == str(expected)), None)
        if match:
            return match

    preferred_title = normalize_base_item(preference.preferred_product_title)
    exact_title = next(
        (row for row in rows if normalize_base_item(str(row.get("title") or "")) == preferred_title),
        None,
    )
    if exact_title:
        return exact_title

    descriptive_constraints = [
        (field, expected)
        for field, expected in (
        ("brand", preference.preferred_brand),
        ("variant", preference.preferred_variant),
        ("package_size", preference.preferred_package_size),
        )
        if expected
    ]
    if not descriptive_constraints:
        return None
    constrained = rows
    for field, expected in descriptive_constraints:
        expected_tokens = set(normalize_base_item(expected).split())
        constrained = [
            row for row in constrained
            if expected_tokens.issubset(set(normalize_base_item(str(row.get(field) or row.get("title") or "")).split()))
        ]
    return constrained[0] if constrained else None


def _find_verified_cached_candidate(normalized_base: str, identity: str, *, retailer: str, store_id: str) -> Optional[dict[str, Any]]:
    rows = RetailProductCache.query.filter_by(
        retailer=retailer,
        store_id=store_id,
        verified_location=True,
        provider_source="serpapi_walmart" if retailer == "walmart" else "kroger_api",
    ).order_by(RetailProductCache.retrieved_at.desc()).all()
    for row in rows:
        if normalize_base_item(row.base_item) != normalized_base:
            continue
        try:
            payload = json.loads(row.response_json)
        except (TypeError, ValueError):
            continue
        products = [payload.get("selected_product")] + list(payload.get("alternatives") or [])
        for product in products:
            if not isinstance(product, dict) or not product.get("verified_location"):
                continue
            store = product.get("store") or {}
            if str(store.get("store_id") or "") != store_id:
                continue
            identities = {
                str(product.get("upc") or ""),
                str(product.get("product_id") or ""),
                str(product.get("us_item_id") or ""),
            }
            if str(identity) in identities:
                return product
    return None


def _candidate_matches_preference(
    preference: RetailProductPreference,
    candidate: dict[str, Any],
) -> bool:
    return match_preference(preference, [candidate]) is not None


def _match_substitution_identity(
    substitution: RetailProductSubstitution,
    candidates: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    for field, expected in (
        ("upc", substitution.substitute_upc),
        ("us_item_id", substitution.retailer_us_item_id),
        ("product_id", substitution.retailer_product_id),
    ):
        if expected:
            matched = next(
                (row for row in candidates if str(row.get(field) or "") == str(expected)),
                None,
            )
            if matched:
                return matched
    title = normalize_base_item(substitution.substitute_product_title)
    return next(
        (row for row in candidates if normalize_base_item(str(row.get("title") or "")) == title),
        None,
    )


def _text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None
