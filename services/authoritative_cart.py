"""Durable Shopping cart and staged store-change authority.

The browser may display these records but never supplies their prices, store,
or financial total back as authority.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from extensions import db
from models import ShoppingCart, ShoppingCartLine, ShoppingStoreChangeReview
from services.selected_store import select_store


def _cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def _line_key(item: dict[str, Any], ordinal: int) -> str:
    requirement = item.get('requirement') or {}
    source = str(requirement.get('source_kind') or 'manual').strip().lower()
    source_id = requirement.get('source_requirement_id')
    if source_id not in (None, ''):
        return f"{source}:requirement:{source_id}"
    stable = {key: requirement.get(key) for key in (
        'source_recipe_id', 'item_name', 'base_item', 'brand', 'variant',
        'quantity', 'unit', 'requested_package_size', 'source_text',
    )}
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:20]
    return f"{source}:legacy:{digest}:{ordinal}"


def _line_from_item(cart: ShoppingCart, item: dict[str, Any], ordinal: int) -> ShoppingCartLine:
    product = item.get('selected_product') or {}
    requirement = item.get('requirement') or {}
    # A dimensional recipe/request with no proven conversion is deliberately
    # not a one-package purchase.  The schema keeps a positive sentinel count
    # internally, while resolution_state and nullable money retain the actual
    # customer-visible truth until Rung can resolve a compatible package.
    package_known = item.get('packages_to_buy') is not None
    package_count = max(1, int(item.get('packages_to_buy') or 1))
    unit = _cents(product.get('price')) if package_known else None
    total = unit * package_count if unit is not None else None
    resolved = (bool(product.get('product_id') or product.get('us_item_id'))
                and not bool(item.get('needs_user_choice')) and package_known)
    available = str(product.get('availability') or item.get('availability') or 'unknown')
    if available == 'out_of_stock':
        state = 'unavailable'
    elif resolved and unit is not None:
        state = 'resolved'
    else:
        state = 'unresolved'
    return ShoppingCartLine(
        cart_id=cart.id, requirement_key=_line_key(item, ordinal),
        requirement_json=json.dumps(requirement, sort_keys=True),
        retailer=str(product.get('retailer') or '').lower() or 'unknown',
        provider_product_id=str(product.get('product_id') or '') or None,
        provider_us_item_id=str(product.get('us_item_id') or '') or None,
        title=str(product.get('title') or item.get('product_label') or requirement.get('item_name') or 'Unresolved item')[:300],
        brand=(str(product.get('brand'))[:150] if product.get('brand') else None),
        package_size=(str(product.get('package_size'))[:160] if product.get('package_size') else None),
        package_count=package_count, unit_price_cents=unit, line_total_cents=total,
        availability=available, resolution_state=state,
        provider_source=(str(product.get('source') or item.get('price_source') or '')[:100] or None),
        resolved_at=datetime.now(timezone.utc) if resolved else None,
        provenance_json=json.dumps({'item': item, 'provider': product.get('source') or item.get('price_source')}, sort_keys=True, default=str),
    )


def current_cart(household_id: int, *, for_update: bool = False) -> ShoppingCart | None:
    """Return the sole current cart, optionally claiming it for a mutation.

    PostgreSQL callers use the row lock to serialize a replacement/finish with
    any other cart transition.  SQLite deliberately falls back to its normal
    write locking; the partial unique index remains the final invariant on
    both engines.
    """
    query = ShoppingCart.query.filter_by(household_id=household_id, status='current')
    if for_update:
        query = query.with_for_update()
    return query.one_or_none()


def cart_dict(cart: ShoppingCart) -> dict[str, Any]:
    lines = ShoppingCartLine.query.filter_by(cart_id=cart.id).order_by(ShoppingCartLine.id).all()
    def line_dict(x: ShoppingCartLine) -> dict[str, Any]:
        # The line is authoritative; saved resolver candidates are display-only
        # alternatives for an explicit subsequent choice.
        try:
            saved = json.loads(x.provenance_json or '{}').get('item') or {}
        except (TypeError, ValueError):
            saved = {}
        try:
            requirement = json.loads(x.requirement_json or '{}')
        except (TypeError, ValueError):
            requirement = {}
        candidates = []
        for candidate in [saved.get('selected_product')] + list(saved.get('alternatives') or []):
            if not isinstance(candidate, dict):
                continue
            identity = str(candidate.get('us_item_id') or candidate.get('product_id') or '')
            if not identity or identity in {str(x.provider_us_item_id or ''), str(x.provider_product_id or '')}:
                continue
            candidates.append(candidate)
        package_known = x.resolution_state == 'resolved' and x.unit_price_cents is not None
        return {'id': x.id, 'requirement_key': x.requirement_key, 'title': x.title, 'brand': x.brand, 'package_size': x.package_size,
                'package_count': x.package_count if package_known else None, 'unit_price': x.unit_price_cents / 100 if x.unit_price_cents is not None else None,
                'line_total': x.line_total_cents / 100 if x.line_total_cents is not None else None, 'availability': x.availability,
                'resolution_state': x.resolution_state, 'product_id': x.provider_product_id, 'us_item_id': x.provider_us_item_id,
                'provider': x.provider_source, 'requirement': requirement, 'alternatives': candidates}
    return {'id': cart.id, 'status': cart.status, 'version': cart.version, 'store_identity_id': cart.retail_store_identity_id,
            'subtotal': cart.subtotal_cents / 100, 'total': cart.total_cents / 100,
            'lines': [line_dict(x) for x in lines]}


def choose_current_line_product(*, household_id: int, cart_id: int, line_id: int,
                                expected_version: int, product: dict[str, Any]) -> ShoppingCart:
    """Persist an explicit, server-validated product choice on one current line."""
    cart = current_cart(household_id, for_update=True)
    if cart is None or cart.id != int(cart_id):
        raise LookupError('Current cart not found.')
    if cart.version != int(expected_version):
        raise ValueError('Cart changed; reload before choosing a product.')
    line = ShoppingCartLine.query.filter_by(id=int(line_id), cart_id=cart.id).with_for_update().first()
    if line is None:
        raise LookupError('Cart line not found.')
    price = _cents(product.get('price'))
    if price is None or str(product.get('availability') or '') == 'out_of_stock':
        raise ValueError('That product is not currently available at this store.')
    old_total = int(line.line_total_cents or 0)
    line.provider_product_id = str(product.get('product_id') or '') or None
    line.provider_us_item_id = str(product.get('us_item_id') or '') or None
    line.title = str(product.get('title') or line.title)[:300]
    line.brand = str(product.get('brand') or '')[:150] or None
    line.package_size = str(product.get('package_size') or '')[:160] or None
    line.retailer = str(product.get('retailer') or line.retailer or '').lower() or 'unknown'
    line.provider_source = str(product.get('source') or line.provider_source or '')[:100] or None
    line.unit_price_cents = price
    line.line_total_cents = price * max(1, int(line.package_count or 1))
    line.availability = str(product.get('availability') or 'in_stock')
    line.resolution_state = 'resolved'; line.resolved_at = datetime.now(timezone.utc)
    cart.subtotal_cents = max(0, int(cart.subtotal_cents or 0) - old_total + line.line_total_cents)
    cart.total_cents = max(0, int(cart.total_cents or 0) - old_total + line.line_total_cents)
    cart.version += 1
    return cart


def replace_current_from_resolution(*, household_id: int, store_identity_id: int, resolved_cart: dict[str, Any], source: str = 'retail_resolution') -> ShoppingCart:
    """Replace only a current cart at the same selected physical store."""
    existing = current_cart(household_id, for_update=True)
    if existing is not None:
        existing.status = 'retired'
        db.session.flush()
    cart = ShoppingCart(household_id=household_id, retail_store_identity_id=store_identity_id, status='current',
                        version=(existing.version + 1 if existing else 1), source=source,
                        subtotal_cents=_cents(resolved_cart.get('subtotal')) or 0,
                        total_cents=_cents(resolved_cart.get('total_cart_cost')) or _cents(resolved_cart.get('subtotal')) or 0)
    db.session.add(cart); db.session.flush()
    seen_keys: set[str] = set()
    for ordinal, item in enumerate(resolved_cart.get('cart_items') or []):
        line = _line_from_item(cart, item, ordinal)
        if line.requirement_key in seen_keys:
            raise ValueError('A provider resolution represented one shopping requirement more than once.')
        seen_keys.add(line.requirement_key)
        db.session.add(line)
    db.session.flush()
    return cart


def stage_store_change(*, household_id: int, current: ShoppingCart, target_store_identity_id: int, resolved_cart: dict[str, Any], operation_id: str) -> ShoppingStoreChangeReview:
    if current.household_id != household_id or current.status != 'current':
        raise ValueError('Store-change review requires this household’s current cart.')
    existing = ShoppingStoreChangeReview.query.filter_by(household_id=household_id, operation_id=operation_id).first()
    if existing is not None:
        return existing
    for prior in ShoppingStoreChangeReview.query.filter_by(household_id=household_id, status='pending').all():
        prior.status = 'cancelled'; prior.decided_at = datetime.now(timezone.utc)
        prior_staged = db.session.get(ShoppingCart, prior.staged_cart_id)
        if prior_staged is not None and prior_staged.status == 'staged':
            prior_staged.status = 'retired'
    staged = ShoppingCart(household_id=household_id, retail_store_identity_id=target_store_identity_id, status='staged',
                          version=current.version + 1, source='store_change_review', subtotal_cents=_cents(resolved_cart.get('subtotal')) or 0,
                          total_cents=_cents(resolved_cart.get('total_cart_cost')) or _cents(resolved_cart.get('subtotal')) or 0)
    db.session.add(staged); db.session.flush()
    seen_keys: set[str] = set()
    for ordinal, item in enumerate(resolved_cart.get('cart_items') or []):
        line = _line_from_item(staged, item, ordinal)
        if line.requirement_key in seen_keys:
            raise ValueError('A provider resolution represented one shopping requirement more than once.')
        seen_keys.add(line.requirement_key)
        db.session.add(line)
    review = ShoppingStoreChangeReview(household_id=household_id, current_cart_id=current.id, staged_cart_id=staged.id,
        from_store_identity_id=current.retail_store_identity_id, to_store_identity_id=target_store_identity_id, operation_id=operation_id)
    db.session.add(review); db.session.flush(); return review


def cancel_store_change(*, household_id: int, review_id: int) -> ShoppingStoreChangeReview:
    review = ShoppingStoreChangeReview.query.filter_by(id=review_id, household_id=household_id).first()
    if review is None: raise LookupError('Store-change review not found.')
    if review.status == 'pending':
        review.status = 'cancelled'; review.decided_at = datetime.now(timezone.utc)
        staged = db.session.get(ShoppingCart, review.staged_cart_id)
        if staged is not None: staged.status = 'retired'
    return review


def approve_store_change(*, household_id: int, review_id: int, store: dict[str, Any], account: Any) -> ShoppingStoreChangeReview:
    review = ShoppingStoreChangeReview.query.filter_by(id=review_id, household_id=household_id).with_for_update().first()
    if review is None: raise LookupError('Store-change review not found.')
    if review.status == 'approved': return review
    if review.status != 'pending': raise ValueError('This store-change review is no longer available.')
    # Claim the mutable authority before changing either store selection or
    # cart status.  The caller owns the transaction through its commit.
    current = current_cart(household_id, for_update=True)
    staged = db.session.get(ShoppingCart, review.staged_cart_id)
    if current is None or current.id != review.current_cart_id or staged is None or current.status != 'current' or staged.status != 'staged': raise ValueError('Store-change review is stale.')
    if current.household_id != household_id or staged.household_id != household_id or staged.retail_store_identity_id != review.to_store_identity_id:
        raise ValueError('Store-change review does not match authoritative cart state.')
    # One transaction caller commits: store selection and cart status cannot diverge.
    select_store(household_id, retailer=store['retailer'], store_id=store['store_id'], store_name=store['name'], address=store.get('address') or '', city=store.get('city') or '', state=store.get('state') or '', postal_code=store.get('postal_code') or '', account=account)
    current.status = 'retired'
    db.session.flush()
    staged.status = 'current'; review.status = 'approved'; review.decided_at = datetime.now(timezone.utc)
    return review
