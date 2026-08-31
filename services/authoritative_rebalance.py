"""Persisted review authority for Shopping Rebalance."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from extensions import db
from models import ShoppingCart, ShoppingCartLine, ShoppingRebalanceProposal, ShoppingRebalanceProposalLine
from services.retail.preferences import candidate_is_blocked


def _cents(value: Any) -> int | None:
    if value is None: return None
    return int(round(float(value) * 100))


def _proposal_dict(row: ShoppingRebalanceProposal) -> dict[str, Any]:
    lines = ShoppingRebalanceProposalLine.query.filter_by(proposal_id=row.id).order_by(ShoppingRebalanceProposalLine.id).all()
    return {'id': row.id, 'status': row.status, 'operation_id': row.operation_id, 'base_cart_id': row.base_cart_id,
            'base_cart_version': row.base_cart_version, 'changes': [{'source_cart_line_id': x.source_cart_line_id,
            'requirement_key': x.requirement_key, 'old_product_id': x.old_product_id, 'proposed_product_id': x.proposed_product_id,
            'proposed_title': x.proposed_title, 'proposed_package_size': x.proposed_package_size, 'package_count': x.package_count,
            'old_line_total': x.old_line_total_cents / 100 if x.old_line_total_cents is not None else None,
            'proposed_line_total': x.proposed_line_total_cents / 100 if x.proposed_line_total_cents is not None else None,
            'availability': x.proposed_availability, 'resolution_state': x.proposed_resolution_state} for x in lines]}


def create_proposal(*, household_id: int, cart: ShoppingCart, operation_id: str, changes: list[dict[str, Any]]) -> ShoppingRebalanceProposal:
    existing = ShoppingRebalanceProposal.query.filter_by(household_id=household_id, operation_id=operation_id).first()
    if existing: return existing
    if cart.status != 'current' or cart.household_id != household_id: raise ValueError('Current cart is unavailable.')
    proposal = ShoppingRebalanceProposal(household_id=household_id, base_cart_id=cart.id, base_cart_version=cart.version, operation_id=operation_id)
    db.session.add(proposal); db.session.flush()
    source_lines = {x.id: x for x in ShoppingCartLine.query.filter_by(cart_id=cart.id).all()}
    for change in changes:
        key = str(change.get('choice_key') or '')
        source = next((x for x in source_lines.values() if x.requirement_key == key), None)
        if source is None:
            source = next((x for x in source_lines.values() if str(json.loads(x.requirement_json or '{}').get('base_item') or '').strip().lower() == key), None)
        if source is None: continue
        product = dict(change.get('proposed_product') or {})
        if candidate_is_blocked(product, retailer=source.retailer, household_id=household_id):
            continue
        # `line_price_cents` is the server-calculated total for all packages,
        # not a unit price.  Modern optimizer previews include explicit unit
        # cents; tolerate older server rows only when their total divides
        # exactly, never by guessing a fractional cent package price.
        price = product.get('unit_price_cents')
        price = int(price) if price is not None else _cents(product.get('price'))
        count = max(1, int(product.get('packages_to_buy') or source.package_count))
        line_total = product.get('line_price_cents')
        proposed_line_total = int(line_total) if line_total is not None else None
        if price is None and proposed_line_total is not None and proposed_line_total % count == 0:
            price = proposed_line_total // count
        if price is not None and proposed_line_total is not None and price * count != proposed_line_total:
            raise ValueError('Rebalance proposal price/package totals are inconsistent.')
        availability = str(product.get('availability') or source.availability or 'unknown')
        resolved = bool(product.get('product_id')) and price is not None and availability == 'in_stock'
        db.session.add(ShoppingRebalanceProposalLine(proposal_id=proposal.id, source_cart_line_id=source.id,
            requirement_key=source.requirement_key, old_product_id=source.provider_product_id,
            proposed_product_id=str(product.get('product_id') or '') or None, proposed_title=str(product.get('title') or source.title),
            proposed_brand=str(product.get('brand') or '') or None, proposed_package_size=str(product.get('package_size') or '') or None,
            package_count=count, old_line_total_cents=source.line_total_cents, proposed_unit_price_cents=price,
            proposed_line_total_cents=(proposed_line_total if proposed_line_total is not None else (price * count if price is not None else None)), proposed_availability=availability,
            proposed_resolution_state='resolved' if resolved else ('unavailable' if availability == 'out_of_stock' else 'unresolved'),
            provenance_json=json.dumps({'proposal_product': product}, sort_keys=True, default=str)))
    db.session.flush(); return proposal


def reject_proposal(*, household_id: int, proposal_id: int) -> ShoppingRebalanceProposal:
    row = ShoppingRebalanceProposal.query.filter_by(id=proposal_id, household_id=household_id).first()
    if row is None: raise LookupError('Rebalance proposal not found.')
    if row.status == 'pending': row.status = 'rejected'; row.decided_at = datetime.now(timezone.utc)
    return row


def approve_proposal(*, household_id: int, proposal_id: int, selected_store_id: int) -> ShoppingRebalanceProposal:
    row = ShoppingRebalanceProposal.query.filter_by(id=proposal_id, household_id=household_id).with_for_update().first()
    if row is None: raise LookupError('Rebalance proposal not found.')
    if row.status == 'approved': return row
    # The proposal and the one mutable cart are both claimed until the caller
    # commits.  SQLite falls back to database write locking; PostgreSQL gets
    # row locks, while the persisted status/version checks remain backstops.
    cart = ShoppingCart.query.filter_by(id=row.base_cart_id, household_id=household_id).with_for_update().first()
    if row.status != 'pending' or cart is None or cart.status != 'current' or cart.version != row.base_cart_version or cart.retail_store_identity_id != selected_store_id:
        if row.status == 'pending': row.status = 'stale'; row.decided_at = datetime.now(timezone.utc)
        return row
    changes = ShoppingRebalanceProposalLine.query.filter_by(proposal_id=row.id).all()
    for change in changes:
        if change.proposed_resolution_state != 'resolved': raise ValueError('Rebalance proposal contains an unsafe product resolution.')
        line = db.session.get(ShoppingCartLine, change.source_cart_line_id)
        if line is None or line.cart_id != cart.id: raise ValueError('Rebalance proposal line is stale.')
        try:
            proposal_product = dict(json.loads(change.provenance_json or '{}').get('proposal_product') or {})
        except (TypeError, ValueError):
            proposal_product = {}
        if candidate_is_blocked(proposal_product, retailer=line.retailer, household_id=household_id):
            raise ValueError('Rebalance proposal contains a blocked product.')
        line.provider_product_id, line.title, line.brand = change.proposed_product_id, change.proposed_title, change.proposed_brand
        line.package_size, line.package_count = change.proposed_package_size, change.package_count
        line.unit_price_cents, line.line_total_cents = change.proposed_unit_price_cents, change.proposed_line_total_cents
        line.availability, line.resolution_state = change.proposed_availability, change.proposed_resolution_state
    cart.subtotal_cents = sum(x.line_total_cents or 0 for x in ShoppingCartLine.query.filter_by(cart_id=cart.id).all())
    cart.total_cents = cart.subtotal_cents
    cart.version += 1; row.status = 'approved'; row.decided_at = datetime.now(timezone.utc)
    return row
