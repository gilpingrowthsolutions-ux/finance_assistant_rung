"""Feature 5 final backend gate: authoritative Shopping invariants.

Every fixture is an in-memory disposable database.  The fake providers are
store-aware deliberately: a Store B result can only be obtained by resolving
the durable requirement again, never by reusing Store A's retail identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"] = "feature5-gate-secret"

from app import app
from extensions import db
from models import (Account, ExpenseTransaction, GroceryItem, Household,
                    Recipe, RecipeIngredient, ShoppingCart, ShoppingCartLine,
                    ShoppingRebalanceProposal, ShoppingStoreChangeReview,
                    ShoppingTripCompletion)
from services.authoritative_cart import (approve_store_change, cancel_store_change,
                                         current_cart, replace_current_from_resolution,
                                         stage_store_change)
from services.authoritative_rebalance import approve_proposal, create_proposal, reject_proposal
from services.household_context import household_id as default_household_id
from services.retail import ProductSearchResult, RetailProduct, RetailStore, ShoppingRequirement
from services.retail.cart import build_verified_retail_cart
from services.selected_store import ensure_store_identity, get_selected_store, select_store


class StoreProvider:
    def __init__(self, rows: dict[str, list[dict]]): self.rows = rows
    def search_products(self, requirement, *, store, limit=20):
        products = []
        for row in self.rows.get(store.store_id, []):
            products.append(RetailProduct.now(requested_query=requirement.search_query(), retailer="walmart", store=store,
                product_id=row.get("sku"), us_item_id=row.get("sku"), upc=None, title=row.get("title", "Detergent"),
                brand="Test", variant=None, package_size=row.get("package"), price=row.get("price"),
                availability=row.get("availability", "in_stock"), price_type="unknown", product_url="https://example.test/p",
                source="feature5_fixture", verified_location=True))
        return ProductSearchResult(store, store, products, len(products))
    def get_product(self, product_id, *, store, requested_query): raise AssertionError("search fixture has full products")


class FailingProvider(StoreProvider):
    def search_products(self, requirement, *, store, limit=20): raise RuntimeError("provider unavailable")


def _store(store_id: str) -> RetailStore:
    return RetailStore(store_id, f"Store {store_id}", "", "", True)


def _reset():
    with app.app_context():
        db.drop_all(); db.create_all()


def _product_resolution(requirement: dict, *, sku="sku-a", package="64 loads", price=12.34, availability="in_stock", packages=1):
    return {"subtotal": price * packages, "total_cart_cost": price * packages, "cart_items": [{
        "requirement": requirement,
        "selected_product": {"product_id": sku, "title": sku, "package_size": package, "price": price,
                             "availability": availability, "retailer": "walmart", "source": "feature5_fixture"},
        "packages_to_buy": packages, "availability": availability,
        "needs_user_choice": availability != "in_stock",
    }]}


def _manual(hid: int, *, quantity=1.0, unit="bottle") -> GroceryItem:
    requirement = ShoppingRequirement("laundry detergent", "laundry detergent", quantity=quantity, unit=unit, source_kind="manual")
    row = GroceryItem(household_id=hid, item_name="laundry detergent", shopping_requirement_json=json.dumps(requirement.__dict__))
    db.session.add(row); db.session.flush(); return row


def _account_store(hid: int, store_id="A"):
    account = Account(household_id=hid, checking_balance=500.0)
    db.session.add(account); db.session.flush()
    return account, select_store(hid, retailer="walmart", store_id=store_id, store_name=f"Store {store_id}", account=account)


def _signed(public_id: str) -> dict[str, str]:
    signature = hmac.new(os.environ["RUNG_HOUSEHOLD_CONTEXT_SECRET"].encode(), public_id.encode(), hashlib.sha256).hexdigest()
    return {"X-Household-Id": public_id, "X-Household-Signature": signature}


def test_feature5_store_b_reconstructs_recipe_and_manual_underlying_intent(monkeypatch):
    _reset()
    provider = StoreProvider({"A": [{"sku": "A-SKU", "package": "64 loads", "price": 12.34}],
                              "B": [{"sku": "B-SKU", "package": "92 loads", "price": 16.78}],
                              "O": [{"sku": "O-SKU", "availability": "out_of_stock", "package": "40 loads", "price": 8.0}],
                              "U": []})
    with app.app_context():
        hid = default_household_id(); _account_store(hid); manual = _manual(hid, quantity=2, unit="bottle")
        # Active recipe requirements use the same cart resolver.  Patch only the meal-plan adapter here;
        # its output is an immutable recipe requirement rather than an old cart line.
        recipe_requirement = ShoppingRequirement("1 bottle dish soap", "dish soap", quantity=1, unit="bottle", source_kind="recipe", source_requirement_id=91, source_recipe_id=7)
        monkeypatch.setattr("services.retail.cart.active_recipe_requirements", lambda household_id: [recipe_requirement])
        monkeypatch.setattr("services.retail.cart.current_household_id", lambda: hid)
        store_a = _store("A"); store_b = _store("B")
        a = build_verified_retail_cart(retailer="walmart", store=store_a, provider=provider)
        b = build_verified_retail_cart(retailer="walmart", store=store_b, provider=provider)
        a_manual, b_manual = (next(x for x in cart["cart_items"] if x["requirement"]["base_item"] == "laundry detergent") for cart in (a, b))
        assert a_manual["requirement"]["source_requirement_id"] == b_manual["requirement"]["source_requirement_id"] == manual.id
        assert (a_manual["requirement"]["quantity"], a_manual["requirement"]["unit"]) == (b_manual["requirement"]["quantity"], b_manual["requirement"]["unit"]) == (2.0, "bottle")
        assert (a_manual["selected_product"]["product_id"], a_manual["selected_product"]["package_size"], a_manual["selected_product"]["price"]) != (b_manual["selected_product"]["product_id"], b_manual["selected_product"]["package_size"], b_manual["selected_product"]["price"])
        assert all(x["requirement"]["source_recipe_id"] == 7 for x in (a["cart_items"][1], b["cart_items"][1]))
        for store_id, expected in (("O", "unavailable"), ("U", "unresolved")):
            item = build_verified_retail_cart(retailer="walmart", store=_store(store_id), provider=provider)["cart_items"][0]
            assert item["requirement"]["source_requirement_id"] == manual.id and item["requirement"]["quantity"] == 2.0
            assert item["resolved"] is False and (item["availability"] == "out_of_stock" if expected == "unavailable" else item["needs_user_choice"])


def test_feature5_manual_general_retail_lifecycle_survives_reload_rebalance_and_store_change():
    _reset()
    with app.app_context():
        hid = default_household_id(); account, selected = _account_store(hid); request = _manual(hid)
        req = {**json.loads(request.shopping_requirement_json), "source_requirement_id": request.id}
        cart = replace_current_from_resolution(household_id=hid, store_identity_id=selected["retail_store_identity_id"], resolved_cart=_product_resolution(req)); db.session.commit()
        assert current_cart(hid).id == cart.id and GroceryItem.query.filter_by(household_id=hid).count() == 1
        change = {"choice_key": "laundry detergent", "proposed_product": {"product_id": "rebalance", "title": "rebalance", "package_size": "48 loads", "price": 10.0, "availability": "in_stock"}}
        rejected = create_proposal(household_id=hid, cart=cart, operation_id="reject", changes=[change]); reject_proposal(household_id=hid, proposal_id=rejected.id)
        approved = create_proposal(household_id=hid, cart=cart, operation_id="approve", changes=[change]); approve_proposal(household_id=hid, proposal_id=approved.id, selected_store_id=selected["retail_store_identity_id"])
        target = ensure_store_identity(retailer="walmart", store_id="B", store_name="Store B")
        review = stage_store_change(household_id=hid, current=cart, target_store_identity_id=target.id, resolved_cart=_product_resolution(req, sku="store-b", package="92 loads", price=16.0), operation_id="change")
        approve_store_change(household_id=hid, review_id=review.id, store={"retailer":"walmart", "store_id":"B", "name":"Store B"}, account=account); db.session.commit()
        reloaded = current_cart(hid); line = ShoppingCartLine.query.filter_by(cart_id=reloaded.id).one()
        assert GroceryItem.query.filter_by(household_id=hid).count() == 1 and line.requirement_key == f"manual:requirement:{request.id}"
        assert json.loads(line.requirement_json)["source_requirement_id"] == request.id and get_selected_store(hid)["store_id"] == "B"


def test_feature5_completed_cart_is_immutable_after_real_finish_and_new_cart_work():
    _reset()
    with app.app_context():
        hid = default_household_id(); account, selected = _account_store(hid); request = _manual(hid)
        req = {**json.loads(request.shopping_requirement_json), "source_requirement_id": request.id}
        cart_a = replace_current_from_resolution(household_id=hid, store_identity_id=selected["retail_store_identity_id"], resolved_cart=_product_resolution(req, packages=2)); cart_a_id = cart_a.id; db.session.commit()
    response = app.test_client().post("/api/grocery/finished-shopping/complete", json={"confirm": True, "operation_id": "finish-a"})
    assert response.status_code == 200
    transaction_id = response.get_json()["transaction_id"]
    with app.app_context():
        completed = db.session.get(ShoppingCart, cart_a_id); line = ShoppingCartLine.query.filter_by(cart_id=cart_a_id).one()
        snapshot = (completed.id, completed.retail_store_identity_id, completed.status, completed.version, completed.subtotal_cents, completed.total_cents, line.requirement_key, line.provider_product_id, line.package_count, line.unit_price_cents, line.line_total_cents)
        cart_b = replace_current_from_resolution(household_id=hid, store_identity_id=selected["retail_store_identity_id"], resolved_cart=_product_resolution(req, sku="b", price=9.0));
        proposal = create_proposal(household_id=hid, cart=cart_b, operation_id="rebalance-b", changes=[{"choice_key":"laundry detergent", "proposed_product":{"product_id":"b2", "title":"b2", "price":8.0,"availability":"in_stock"}}]); approve_proposal(household_id=hid, proposal_id=proposal.id, selected_store_id=selected["retail_store_identity_id"])
        target = ensure_store_identity(retailer="walmart", store_id="C", store_name="Store C")
        review = stage_store_change(household_id=hid, current=cart_b, target_store_identity_id=target.id, resolved_cart=_product_resolution(req, sku="c", price=7.0), operation_id="change-b")
        approve_store_change(household_id=hid, review_id=review.id, store={"retailer":"walmart","store_id":"C","name":"Store C"}, account=account); db.session.commit()
        completed = db.session.get(ShoppingCart, cart_a_id); line = ShoppingCartLine.query.filter_by(cart_id=cart_a_id).one()
        assert snapshot == (completed.id, completed.retail_store_identity_id, completed.status, completed.version, completed.subtotal_cents, completed.total_cents, line.requirement_key, line.provider_product_id, line.package_count, line.unit_price_cents, line.line_total_cents)
        assert completed.status == "completed" and current_cart(hid).id != cart_a_id
        with pytest.raises(ValueError): create_proposal(household_id=hid, cart=completed, operation_id="illegal", changes=[])
        balance_before_delete = float(Account.query.filter_by(household_id=hid).one().checking_balance)
    # This is the transaction created by the real Feature 5 finish endpoint,
    # not a fabricated completion fixture: ordinary delete is protected and
    # cannot alter checking/Safe-to-Spend provenance.
    assert app.test_client().delete(f"/transactions/{transaction_id}").status_code == 409
    with app.app_context():
        assert db.session.get(ExpenseTransaction, transaction_id) is not None
        assert float(Account.query.filter_by(household_id=hid).one().checking_balance) == balance_before_delete
        assert ShoppingTripCompletion.query.filter_by(transaction_id=transaction_id).count() == 1


def test_feature5_two_household_isolation_for_carts_reviews_proposals_completion_and_routes():
    _reset()
    with app.app_context():
        a = Household(public_id="a0000000-0000-0000-0000-000000000000", legacy_scope_key="a"); b = Household(public_id="b0000000-0000-0000-0000-000000000000", legacy_scope_key="b"); db.session.add_all([a,b]); db.session.flush(); a_id, b_id, a_public, b_public = a.id, b.id, a.public_id, b.public_id
        aa, sa = _account_store(a_id, "A"); ab, sb = _account_store(b_id, "B"); ra, rb = _manual(a_id), _manual(b_id)
        qa, qb = ({**json.loads(row.shopping_requirement_json), "source_requirement_id": row.id} for row in (ra,rb))
        ca = replace_current_from_resolution(household_id=a_id, store_identity_id=sa["retail_store_identity_id"], resolved_cart=_product_resolution(qa)); ca_id = ca.id; cb = replace_current_from_resolution(household_id=b_id, store_identity_id=sb["retail_store_identity_id"], resolved_cart=_product_resolution(qb, sku="b")); cb_id = cb.id
        target = ensure_store_identity(retailer="walmart", store_id="A2", store_name="A2"); review = stage_store_change(household_id=a_id, current=ca, target_store_identity_id=target.id, resolved_cart=_product_resolution(qa, sku="a2"), operation_id="a-review"); review_id = review.id
        proposal = create_proposal(household_id=b_id, cart=cb, operation_id="b-rebalance", changes=[]); proposal_id = proposal.id; db.session.commit()
    client = app.test_client(); headers_b = _signed(b_public)
    assert client.get("/api/shopping/current-cart", headers=headers_b).get_json()["cart"]["id"] == cb_id
    assert client.post(f"/api/shopping/store-change/{review_id}/approve", headers=headers_b).status_code == 404
    assert client.post(f"/api/shopping/store-change/{review_id}/cancel", headers=headers_b).status_code == 404
    assert client.post(f"/api/grocery/rebalance/{proposal_id}/reject", headers=_signed(a_public)).status_code == 404
    # B cannot finish A: its own unresolved current line is made non-finishable and A remains untouched.
    with app.app_context(): ShoppingCartLine.query.filter_by(cart_id=cb_id).update({"resolution_state":"unresolved"}); db.session.commit()
    assert client.post("/api/grocery/finished-shopping/complete", headers=headers_b, json={"confirm":True,"operation_id":"cross-a"}).status_code == 400
    with app.app_context():
        assert current_cart(a_id).id == ca_id and db.session.get(ShoppingStoreChangeReview, review_id).status == "pending"
        assert db.session.get(ShoppingRebalanceProposal, proposal_id).status == "pending" and ShoppingTripCompletion.query.count() == 0
        assert GroceryItem.query.filter_by(household_id=a_id).count() == GroceryItem.query.filter_by(household_id=b_id).count() == 1


def test_feature5_preferences_provider_failure_and_package_conversion_path():
    _reset()
    with app.app_context():
        hid = default_household_id(); _account_store(hid); row = _manual(hid, quantity=2, unit="cup")
        # Two cups are a requirement, not two retail packages; no safe cup->package conversion is invented.
        empty = StoreProvider({"A": []}); cart = build_verified_retail_cart(retailer="walmart", store=_store("A"), provider=empty)
        item = cart["cart_items"][0]
        assert item["requirement"]["quantity"] == 2.0 and item["requirement"]["unit"] == "cup" and item["packages_to_buy"] is None and item["resolved"] is False
        selected = get_selected_store(hid); current = replace_current_from_resolution(household_id=hid, store_identity_id=selected["retail_store_identity_id"], resolved_cart=_product_resolution({**json.loads(row.shopping_requirement_json),"source_requirement_id":row.id})); db.session.commit()
        # Provider failure degrades to an unresolved requirement, never a fake
        # exact-store price or a mutation of selection/current-cart authority.
        failed = build_verified_retail_cart(retailer="walmart", store=_store("FAIL"), provider=FailingProvider({}), force_refresh=True)
        assert failed["cart_items"][0]["resolved"] is False and failed["cart_items"][0]["selected_product"] is None
        assert current_cart(hid).id == current.id and get_selected_store(hid)["store_id"] == "A"
