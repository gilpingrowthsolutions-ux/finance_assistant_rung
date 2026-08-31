from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

os.environ['RUNG_DB_PATH'] = ':memory:'

from app import app
from extensions import db
from models import Account, ExpenseTransaction, Household, RetailProductBlock, RetailProductCache, RetailProductPreference, ShoppingCart, ShoppingCartLine, ShoppingRebalanceProposal, ShoppingRebalanceProposalLine, ShoppingStoreChangeReview, ShoppingTripCompletion
from sqlalchemy.exc import IntegrityError
from services.authoritative_cart import (
    approve_store_change,
    cart_dict,
    cancel_store_change,
    choose_current_line_product,
    current_cart,
    replace_current_from_resolution,
    stage_store_change,
)
from services.household_context import household_id
from services.selected_store import ensure_store_identity, get_selected_store, select_store
from services.authoritative_rebalance import approve_proposal, create_proposal, reject_proposal
from services.retail.preferences import save_product_block


def _resolution(title: str, price: float, *, sku: str, availability: str = 'in_stock') -> dict:
    return {'subtotal': price, 'total_cart_cost': price, 'cart_items': [{
        'requirement': {'item_name': 'Laundry detergent', 'base_item': 'laundry detergent', 'quantity': 1, 'unit': 'item', 'source_kind': 'manual'},
        'selected_product': {'product_id': sku, 'title': title, 'brand': 'Rung', 'package_size': '64 loads', 'price': price, 'availability': availability, 'source': 'fixture', 'retailer': 'walmart'},
        'packages_to_buy': 1, 'needs_user_choice': False, 'availability': availability,
    }]}


def _setup() -> tuple[int, Account, dict]:
    with app.app_context():
        db.drop_all(); db.create_all(); hid = household_id(); account = Account(household_id=hid, checking_balance=500)
        db.session.add(account); db.session.flush()
        selected = select_store(hid, retailer='walmart', store_id='A-1', store_name='Store A', account=account)
        db.session.commit()
        return hid, account, selected


def test_cart_is_household_scoped_store_bound_and_backend_totalled() -> None:
    hid, _, selected = _setup()
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A detergent', 12.34, sku='A-SKU'))
        db.session.commit()
        assert cart.total_cents == 1234
        assert cart.retail_store_identity_id == selected['retail_store_identity_id']
        assert current_cart(hid).id == cart.id


def test_dimensional_unresolved_line_never_leaks_internal_package_sentinel() -> None:
    hid, _, selected = _setup()
    resolution = {
        'subtotal': 0, 'total_cart_cost': 0, 'cart_items': [{
            'requirement': {'item_name': 'Rice', 'base_item': 'rice', 'quantity': 2, 'unit': 'cups', 'source_kind': 'manual'},
            'selected_product': {'product_id': 'RICE-BAG', 'title': 'Rice bag', 'price': 5, 'availability': 'in_stock', 'retailer': 'walmart'},
            'packages_to_buy': None, 'needs_user_choice': False, 'availability': 'in_stock',
        }],
    }
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=resolution)
        db.session.commit(); cart_id = cart.id
        assert ShoppingCartLine.query.filter_by(cart_id=cart_id).one().package_count == 1  # internal schema sentinel
        external = cart_dict(db.session.get(ShoppingCart, cart_id))
        assert external['total'] == 0
        assert external['lines'][0]['requirement']['quantity'] == 2
        assert external['lines'][0]['requirement']['unit'] == 'cups'
        assert {key: external['lines'][0][key] for key in ('resolution_state', 'package_count', 'unit_price', 'line_total')} == {
            'resolution_state': 'unresolved', 'package_count': None, 'unit_price': None, 'line_total': None,
        }
    assert app.test_client().get('/api/shopping/current-cart').get_json()['cart']['lines'][0]['package_count'] is None


def test_explicit_current_cart_product_choice_is_requirement_scoped_server_authoritative_and_isolated() -> None:
    """The mutation accepts only its exact server candidate and trusts no browser money."""
    hid, _, selected = _setup()
    requirement = {'item_name': 'Laundry detergent', 'base_item': 'laundry detergent', 'quantity': 3,
                   'unit': 'bottle', 'source_kind': 'manual', 'source_requirement_id': 101}
    resolved = _resolution('Store A detergent', 8, sku='A-SKU')
    resolved['cart_items'][0].update({'requirement': requirement, 'packages_to_buy': 3})
    resolved['subtotal'] = resolved['total_cart_cost'] = 24
    alternate = {'product_id': 'A-ALT', 'us_item_id': 'A-ALT-US', 'retailer': 'walmart',
                 'title': 'Store A alternate', 'brand': 'Fixture', 'package_size': '64 loads',
                 'price': 12, 'availability': 'in_stock', 'source': 'verified-fixture'}
    unrelated = {'item_name': 'Paper towels', 'base_item': 'paper towels', 'quantity': 1,
                 'unit': 'package', 'source_kind': 'manual', 'source_requirement_id': 202}
    unrelated_product = {**alternate, 'product_id': 'OTHER-REQ', 'us_item_id': 'OTHER-REQ-US', 'price': 1}
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=resolved)
        line = ShoppingCartLine.query.filter_by(cart_id=cart.id).one()
        for query, req, candidate in [('laundry detergent', requirement, alternate), ('paper towels', unrelated, unrelated_product)]:
            db.session.add(RetailProductCache(retailer='walmart', store_id='A-1', store_name='Store A', store_address='',
                requested_query=query, base_item=req['base_item'], title='fixture', provider_source='fixture', verified_location=True,
                response_json=json.dumps({'requirement': req, 'candidates': [candidate], 'alternatives': [candidate]}), retrieved_at=datetime.now(timezone.utc)))
        db.session.commit(); cart_id, line_id = cart.id, line.id

    client = app.test_client()
    # Browser supplied price/line/cart/package values are intentionally ignored.
    response = client.post('/api/shopping/current-cart/choose-product', json={
        'cart_id': cart_id, 'line_id': line_id, 'version': 1, 'product_id': 'A-ALT',
        'price': 0.01, 'line_total': 0.01, 'cart_total': 0.01, 'package_count': 999,
    })
    assert response.status_code == 200
    result = response.get_json()['cart']
    assert result['version'] == 2 and result['total'] == 36
    assert result['lines'][0]['product_id'] == 'A-ALT'
    assert (result['lines'][0]['package_count'], result['lines'][0]['unit_price'], result['lines'][0]['line_total']) == (3, 12, 36)
    with app.app_context():
        saved = db.session.get(ShoppingCart, cart_id); saved_line = db.session.get(ShoppingCartLine, line_id)
        assert saved.version == 2 and (saved.subtotal_cents, saved.total_cents) == (3600, 3600)
        assert json.loads(saved_line.requirement_json) == requirement
        assert ShoppingCart.query.filter_by(household_id=hid, status='current').count() == 1
        assert RetailProductPreference.query.filter_by(household_id=hid).count() == 0
        assert RetailProductBlock.query.filter_by(household_id=hid).count() == 0
    # A fresh request/session observes the durable choice, while stale and
    # unrelated-requirement candidates produce controlled conflicts only.
    assert client.get('/api/shopping/current-cart').get_json()['cart']['lines'][0]['product_id'] == 'A-ALT'
    assert client.post('/api/shopping/current-cart/choose-product', json={'cart_id': cart_id, 'line_id': line_id, 'version': 1, 'product_id': 'A-ALT'}).status_code == 409
    assert client.post('/api/shopping/current-cart/choose-product', json={'cart_id': cart_id, 'line_id': line_id, 'version': 2, 'product_id': 'OTHER-REQ'}).status_code == 409
    with app.app_context():
        assert db.session.get(ShoppingCartLine, line_id).provider_product_id == 'A-ALT'
        foreign = Household(legacy_scope_key='choice-foreign'); db.session.add(foreign); db.session.flush()
        foreign_account = Account(household_id=foreign.id, checking_balance=100); db.session.add(foreign_account); db.session.flush()
        foreign_store = select_store(foreign.id, retailer='walmart', store_id='F', store_name='Foreign', account=foreign_account)
        foreign_cart = replace_current_from_resolution(household_id=foreign.id, store_identity_id=foreign_store['retail_store_identity_id'], resolved_cart=resolved)
        foreign_line = ShoppingCartLine.query.filter_by(cart_id=foreign_cart.id).one(); db.session.commit()
        with pytest.raises(LookupError):
            choose_current_line_product(household_id=foreign.id, cart_id=cart_id, line_id=line_id, expected_version=2, product=alternate)
        assert foreign_line.provider_product_id == 'A-SKU'


def test_database_allows_only_one_current_cart_per_household() -> None:
    hid, _, selected = _setup()
    with app.app_context():
        first = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A detergent', 12.34, sku='A-SKU'))
        db.session.commit()
        db.session.add(ShoppingCart(household_id=hid, retail_store_identity_id=selected['retail_store_identity_id'], status='current', version=99))
        try:
            db.session.commit()
            assert False, 'the partial unique index must reject a second current cart'
        except IntegrityError:
            db.session.rollback()
        assert current_cart(hid).id == first.id
        replacement = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A detergent newer', 10.99, sku='A-SKU-2'))
        db.session.commit()
        assert replacement.status == 'current'
        assert db.session.get(ShoppingCart, first.id).status == 'retired'


def test_duplicate_durable_requirement_cannot_be_persisted_twice() -> None:
    hid, _, selected = _setup()
    duplicate = _resolution('A detergent', 12.34, sku='A-SKU')
    duplicate['cart_items'][0]['requirement']['source_requirement_id'] = 41
    duplicate['cart_items'].append({**duplicate['cart_items'][0], 'selected_product': {**duplicate['cart_items'][0]['selected_product'], 'product_id': 'A-SKU-duplicate'}})
    with app.app_context():
        try:
            replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=duplicate)
            assert False, 'same durable requirement must not become two cart lines'
        except ValueError as exc:
            assert 'more than once' in str(exc)
            db.session.rollback()


def test_store_change_is_staged_cancelled_or_atomically_approved() -> None:
    hid, account, selected = _setup()
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A detergent', 12.34, sku='A-SKU'))
        target = ensure_store_identity(retailer='walmart', store_id='B-2', store_name='Store B')
        review = stage_store_change(household_id=hid, current=cart, target_store_identity_id=target.id, resolved_cart=_resolution('B detergent', 10.99, sku='B-SKU'), operation_id='change-b')
        db.session.commit()
        assert get_selected_store(hid)['store_id'] == 'A-1'
        assert current_cart(hid).id == cart.id
        cancel_store_change(household_id=hid, review_id=review.id); db.session.commit()
        assert get_selected_store(hid)['store_id'] == 'A-1'
        assert current_cart(hid).id == cart.id
        review = stage_store_change(household_id=hid, current=cart, target_store_identity_id=target.id, resolved_cart=_resolution('B detergent', 10.99, sku='B-SKU'), operation_id='change-b-2')
        approved = approve_store_change(household_id=hid, review_id=review.id, store={'retailer': 'walmart', 'store_id': 'B-2', 'name': 'Store B'}, account=account)
        db.session.commit()
        assert approved.status == 'approved'
        assert get_selected_store(hid)['store_id'] == 'B-2'
        assert current_cart(hid).retail_store_identity_id == target.id
        assert ShoppingCart.query.filter_by(household_id=hid, status='current').count() == 1
        assert approve_store_change(household_id=hid, review_id=review.id, store={'retailer': 'walmart', 'store_id': 'B-2', 'name': 'Store B'}, account=account).id == review.id


def test_store_approval_failure_rolls_back_selected_store_and_cart(monkeypatch) -> None:
    hid, account, selected = _setup()
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A', 12.34, sku='A'))
        target = ensure_store_identity(retailer='walmart', store_id='B-2', store_name='Store B')
        review = stage_store_change(household_id=hid, current=cart, target_store_identity_id=target.id, resolved_cart=_resolution('B', 10.99, sku='B'), operation_id='rollback-b')
        db.session.commit()
        import services.authoritative_cart as authority
        original = authority.select_store
        def fail_after_store(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('injected after selected store mutation')
        monkeypatch.setattr(authority, 'select_store', fail_after_store)
        try:
            approve_store_change(household_id=hid, review_id=review.id, store={'retailer': 'walmart', 'store_id': 'B-2', 'name': 'Store B'}, account=account)
        except RuntimeError:
            db.session.rollback()
        assert get_selected_store(hid)['store_id'] == 'A-1'
        assert current_cart(hid).id == cart.id
        assert db.session.get(ShoppingCart, review.staged_cart_id).status == 'staged'
        assert db.session.get(ShoppingStoreChangeReview, review.id).status == 'pending'


def test_durable_rebalance_preview_reject_approve_and_stale() -> None:
    hid, _, selected = _setup()
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A detergent', 12.34, sku='A-SKU'))
        db.session.commit()
        line = ShoppingCartLine.query.filter_by(cart_id=cart.id).one()
        change = {'choice_key': 'laundry detergent', 'proposed_product': {'product_id': 'B-SKU', 'title': 'B detergent', 'brand': 'B', 'package_size': '64 loads', 'price': 9.99, 'availability': 'in_stock'}}
        preview = create_proposal(household_id=hid, cart=cart, operation_id='rebalance-reject', changes=[change])
        db.session.commit()
        assert preview.status == 'pending' and line.provider_product_id == 'A-SKU'
        reject_proposal(household_id=hid, proposal_id=preview.id); db.session.commit()
        assert db.session.get(ShoppingCartLine, line.id).provider_product_id == 'A-SKU'
        assert reject_proposal(household_id=hid, proposal_id=preview.id).status == 'rejected'
        proposal = create_proposal(household_id=hid, cart=cart, operation_id='rebalance-approve', changes=[change])
        approved = approve_proposal(household_id=hid, proposal_id=proposal.id, selected_store_id=selected['retail_store_identity_id'])
        db.session.commit()
        assert approved.status == 'approved'
        assert db.session.get(ShoppingCartLine, line.id).provider_product_id == 'B-SKU'
        assert db.session.get(ShoppingCart, cart.id).version == 2
        stale = create_proposal(household_id=hid, cart=db.session.get(ShoppingCart, cart.id), operation_id='rebalance-stale', changes=[change])
        db.session.commit()
        db.session.get(ShoppingCart, cart.id).version += 1; db.session.commit()
        assert approve_proposal(household_id=hid, proposal_id=stale.id, selected_store_id=selected['retail_store_identity_id']).status == 'stale'
        db.session.commit()
        assert db.session.get(ShoppingRebalanceProposal, stale.id).status == 'stale'


def test_rebalance_proposal_preserves_server_unit_and_line_money_for_multiple_packages() -> None:
    hid, _, selected = _setup()
    with app.app_context():
        # Current line is one package; the reviewed product needs three.
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A detergent', 4.00, sku='A-SKU'))
        line = ShoppingCartLine.query.filter_by(cart_id=cart.id).one()
        change = {'choice_key': 'laundry detergent', 'proposed_product': {
            'product_id': 'B-SKU', 'title': 'B detergent', 'brand': 'B', 'package_size': '20 loads',
            'unit_price_cents': 275, 'packages_to_buy': 3, 'line_price_cents': 825,
            'availability': 'in_stock',
        }}
        proposal = create_proposal(household_id=hid, cart=cart, operation_id='rebalance-multi-package', changes=[change])
        db.session.commit()
        stored = ShoppingRebalanceProposalLine.query.filter_by(proposal_id=proposal.id).one()
        assert (stored.proposed_unit_price_cents, stored.package_count, stored.proposed_line_total_cents) == (275, 3, 825)
        assert stored.proposed_unit_price_cents * stored.package_count == stored.proposed_line_total_cents
        approve_proposal(household_id=hid, proposal_id=proposal.id, selected_store_id=selected['retail_store_identity_id'])
        db.session.commit()
        line = db.session.get(ShoppingCartLine, line.id)
        assert (line.unit_price_cents, line.package_count, line.line_total_cents) == (275, 3, 825)


def test_rebalance_never_persists_or_approves_a_product_blocked_after_preview() -> None:
    hid, _, selected = _setup()
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('Current', 12, sku='CURRENT'))
        proposal = create_proposal(household_id=hid, cart=cart, operation_id='blocked-preview', changes=[{
            'choice_key': 'laundry detergent', 'proposed_product': {'product_id': 'CHEAP', 'us_item_id': 'cheap-us', 'title': 'Cheap', 'brand': 'Blocked Brand', 'price': 4, 'availability': 'in_stock'}
        }])
        assert ShoppingRebalanceProposalLine.query.filter_by(proposal_id=proposal.id).count() == 1
        save_product_block(block_type='brand', brand=' blocked brand ')
        with __import__('pytest').raises(ValueError, match='blocked'):
            approve_proposal(household_id=hid, proposal_id=proposal.id, selected_store_id=selected['retail_store_identity_id'])
        db.session.rollback()
        assert ShoppingCartLine.query.filter_by(cart_id=cart.id).one().provider_product_id == 'CURRENT'


def test_replacement_failure_rolls_back_retirement_and_leaves_no_partial_cart(monkeypatch) -> None:
    """A failure after the old cart is retired is still one atomic replacement."""
    hid, _, selected = _setup()
    with app.app_context():
        original = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A', 12.34, sku='A'))
        db.session.commit()
        import services.authoritative_cart as authority
        monkeypatch.setattr(authority, '_line_from_item', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('injected before commit')))
        try:
            replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('B', 9.99, sku='B'))
        except RuntimeError:
            db.session.rollback()
        assert current_cart(hid).id == original.id
        assert db.session.get(ShoppingCart, original.id).status == 'current'
        assert ShoppingCart.query.filter_by(household_id=hid).count() == 1


def test_rebalance_stale_foreign_and_replay_matrix_has_one_legitimate_mutation() -> None:
    hid, account, selected = _setup()
    with app.app_context():
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=_resolution('A', 12, sku='A'))
        second = _resolution('Unchanged', 3, sku='UNCHANGED'); second['cart_items'][0]['requirement']['base_item'] = 'paper towels'; second['cart_items'][0]['requirement']['item_name'] = 'Paper towels'
        # Keep an unrelated requirement on the same cart.
        resolved = _resolution('A', 12, sku='A'); resolved['cart_items'].extend(second['cart_items'])
        db.session.rollback(); cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=resolved); db.session.commit()
        lines = ShoppingCartLine.query.filter_by(cart_id=cart.id).order_by(ShoppingCartLine.id).all(); old_other = lines[1].provider_product_id
        change = {'choice_key': 'laundry detergent', 'proposed_product': {'product_id': 'B', 'title': 'B', 'price': 9, 'availability': 'in_stock'}}
        proposal = create_proposal(household_id=hid, cart=cart, operation_id='once', changes=[change]); same = create_proposal(household_id=hid, cart=cart, operation_id='once', changes=[change])
        assert same.id == proposal.id
        assert approve_proposal(household_id=hid, proposal_id=proposal.id, selected_store_id=selected['retail_store_identity_id']).status == 'approved'
        version = cart.version
        assert approve_proposal(household_id=hid, proposal_id=proposal.id, selected_store_id=selected['retail_store_identity_id']).status == 'approved'
        assert reject_proposal(household_id=hid, proposal_id=proposal.id).status == 'approved'
        assert cart.version == version and lines[1].provider_product_id == old_other
        rejected = create_proposal(household_id=hid, cart=cart, operation_id='reject-first', changes=[change]); reject_proposal(household_id=hid, proposal_id=rejected.id)
        assert approve_proposal(household_id=hid, proposal_id=rejected.id, selected_store_id=selected['retail_store_identity_id']).status == 'rejected'
        stale = create_proposal(household_id=hid, cart=cart, operation_id='replacement-stale', changes=[change]); db.session.commit()
        replacement = replace_current_from_resolution(household_id=hid, store_identity_id=selected['retail_store_identity_id'], resolved_cart=resolved); db.session.commit()
        assert approve_proposal(household_id=hid, proposal_id=stale.id, selected_store_id=selected['retail_store_identity_id']).status == 'stale'
        target = ensure_store_identity(retailer='walmart', store_id='B', store_name='B')
        review = stage_store_change(household_id=hid, current=replacement, target_store_identity_id=target.id, resolved_cart=resolved, operation_id='change')
        stale_store = create_proposal(household_id=hid, cart=replacement, operation_id='store-stale', changes=[change])
        approve_store_change(household_id=hid, review_id=review.id, store={'retailer':'walmart','store_id':'B','name':'B'}, account=account)
        assert approve_proposal(household_id=hid, proposal_id=stale_store.id, selected_store_id=target.id).status == 'stale'
        foreign = Household(legacy_scope_key='foreign'); db.session.add(foreign); db.session.flush()
        for fn in (approve_proposal, reject_proposal):
            try: fn(household_id=foreign.id, proposal_id=proposal.id, **({'selected_store_id': target.id} if fn is approve_proposal else {}))
            except LookupError: pass
            else: assert False, 'foreign household must not access proposal'
        assert ExpenseTransaction.query.count() == 0


def test_unresolved_or_unavailable_current_cart_cannot_finish(client=None) -> None:
    # Endpoint proof, not merely the Finished Shopping helper.
    hid, _, selected = _setup()
    for state, availability in (('unresolved', 'unknown'), ('unavailable', 'out_of_stock')):
        with app.app_context():
            ShoppingCart.query.filter_by(household_id=hid, status='current').update({'status': 'retired'})
            cart = ShoppingCart(household_id=hid, retail_store_identity_id=selected['retail_store_identity_id'], status='current', subtotal_cents=100, total_cents=100)
            db.session.add(cart); db.session.flush()
            cart_id = cart.id
            db.session.add(ShoppingCartLine(cart_id=cart.id, requirement_key=f'manual:{state}', requirement_json='{"base_item":"rice"}', retailer='walmart', title='rice', package_count=1, unit_price_cents=100, line_total_cents=100, availability=availability, resolution_state=state, provenance_json='{}'))
            db.session.commit(); before = float(Account.query.filter_by(household_id=hid).one().checking_balance or 0)
        response = app.test_client().post('/api/grocery/finished-shopping/complete', json={'confirm': True, 'operation_id': f'blocked-{state}'})
        assert response.status_code == 400
        with app.app_context():
            assert ShoppingTripCompletion.query.count() == 0 and ExpenseTransaction.query.count() == 0
            assert current_cart(hid).id == cart_id
            assert float(Account.query.filter_by(household_id=hid).one().checking_balance or 0) == before
