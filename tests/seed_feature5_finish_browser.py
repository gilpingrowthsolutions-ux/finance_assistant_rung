"""Disposable authoritative cart and financial state for Finished Shopping UI acceptance."""
from datetime import datetime, timedelta, timezone

from app import app
from extensions import db
import json
from models import Account, Bill, GroceryItem, Household, HouseholdMembership, IncomePlanVersion, RetailProductCache, User, UserPreference, UserSetting
from services.authoritative_cart import replace_current_from_resolution
from services.selected_store import select_store
from werkzeug.security import generate_password_hash


def product(sku, title, price, package):
    return {"product_id": sku, "us_item_id": sku + "-us", "retailer": "walmart", "title": title,
            "brand": "Fixture", "package_size": package, "price": price, "availability": "in_stock", "source": "fixture"}


with app.app_context():
    db.drop_all(); db.create_all()
    household = Household(legacy_scope_key="feature5-finish")
    db.session.add(household); db.session.flush()
    user = User(email="feature5-finish@example.com", password_hash=generate_password_hash("browser-finish"), active=True, auth_version=1)
    account = Account(household_id=household.id, checking_balance=500, pay_period_days=14)
    db.session.add_all([user, account]); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=household.id, role="owner", active=True))
    store = select_store(household.id, retailer="walmart", store_id="B", store_name="Store B", address="2 Fixture Way", postal_code="65084", account=account)
    db.session.add_all([
        IncomePlanVersion(household_id=household.id, operation_id="finish-income", expected_income_cents=100000, effective_at=datetime.now(timezone.utc), source="fixture"),
        UserSetting(household_id=household.id, key="pyf_long_term_target_percent", value="0"),
        UserSetting(household_id=household.id, key="safe_to_spend_buffer_usd", value="0.00"),
        UserSetting(household_id=household.id, key="next_payday_date", value=(datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()),
        UserSetting(household_id=household.id, key="onboarding_required_expense_review", value="has_expenses_reviewed"),
        UserPreference(household_id=household.id, key="baseline_grocery_cost", value="100.00"),
        Bill(household_id=household.id, name="Fixture fuel Need", amount=0, due_date=datetime.now(timezone.utc), is_gas_estimate=True, is_paid=False),
    ])
    detergent = {"item_name": "Laundry detergent", "base_item": "laundry detergent", "quantity": 2, "unit": "bottle", "source_kind": "manual", "source_requirement_id": 1}
    soap = {"item_name": "Dish soap", "base_item": "dish soap", "quantity": 1, "unit": "bottle", "source_kind": "manual", "source_requirement_id": 2}
    resolution = {"subtotal": 34, "total_cart_cost": 34, "cart_items": [
        {"requirement": detergent, "selected_product": product("FIN-DETERGENT", "Finish detergent", 15, "64 loads"), "alternatives": [], "packages_to_buy": 2, "needs_user_choice": False, "availability": "in_stock"},
        {"requirement": soap, "selected_product": product("FIN-SOAP", "Finish dish soap", 4, "18 oz"), "alternatives": [], "packages_to_buy": 1, "needs_user_choice": False, "availability": "in_stock"},
    ]}
    # This is later legitimate Shopping work.  It remains inactive only while
    # the fixture's initial authoritative cart is current, then Build Cart
    # resolves it into a distinct new current cart after completion.
    db.session.add(GroceryItem(household_id=household.id, item_name="Laundry detergent", shopping_requirement_json=json.dumps(detergent)))
    payload = {"selection_policy_version": 5, "requirement": detergent, "selected_product": product("LATER-DETERGENT", "Later detergent", 11, "48 loads"), "candidates": [product("LATER-DETERGENT", "Later detergent", 11, "48 loads")], "alternatives": [], "selection_confidence": "suggested", "needs_user_choice": False}
    db.session.add(RetailProductCache(retailer="walmart", store_id="B", store_name="Store B", store_address="2 Fixture Way", requested_query="laundry detergent", base_item="laundry detergent", title="fixture", provider_source="serpapi_walmart", verified_location=True, response_json=json.dumps(payload), retrieved_at=datetime.now(timezone.utc)))
    replace_current_from_resolution(household_id=household.id, store_identity_id=store["retail_store_identity_id"], resolved_cart=resolution)
    db.session.commit()
