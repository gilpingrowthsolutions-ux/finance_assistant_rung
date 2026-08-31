"""Authoritative current-cart fixture for served Rebalance acceptance."""
import json
from datetime import datetime, timedelta, timezone

from app import app
from extensions import db
from models import Account, Bill, Household, HouseholdMembership, IncomePlanVersion, User, UserPreference, UserSetting
from services.authoritative_cart import replace_current_from_resolution
from services.selected_store import select_store
from werkzeug.security import generate_password_hash


def product(sku, title, price):
    return {"product_id": sku, "us_item_id": sku + "-us", "retailer": "walmart", "title": title,
            "brand": "Fixture", "package_size": "64 loads", "price": price, "availability": "in_stock", "source": "fixture"}


with app.app_context():
    db.drop_all(); db.create_all()
    household = Household(legacy_scope_key="feature5-rebalance")
    db.session.add(household); db.session.flush()
    user = User(email="feature5-rebalance@example.com", password_hash=generate_password_hash("browser-rebalance"), active=True, auth_version=1)
    account = Account(household_id=household.id, checking_balance=500, pay_period_days=14)
    db.session.add_all([user, account]); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=household.id, role="owner", active=True))
    selected = select_store(household.id, retailer="walmart", store_id="A", store_name="Store A", address="1 Fixture Way", postal_code="65084", account=account)
    db.session.add_all([IncomePlanVersion(household_id=household.id, operation_id="rebalance-income", expected_income_cents=100000, effective_at=datetime.now(timezone.utc), source="fixture"),
                        UserSetting(household_id=household.id, key="pyf_long_term_target_percent", value="0"), UserSetting(household_id=household.id, key="safe_to_spend_buffer_usd", value="0.00"), UserSetting(household_id=household.id, key="next_payday_date", value=(datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()), UserSetting(household_id=household.id, key="onboarding_required_expense_review", value="has_expenses_reviewed"), UserPreference(household_id=household.id, key="baseline_grocery_cost", value="20.00"), Bill(household_id=household.id, name="Fixture fuel Need", amount=0, due_date=datetime.now(timezone.utc), is_gas_estimate=True, is_paid=False)])
    current, value, unchanged = product("RB-CURRENT", "Rebalance current detergent", 10), product("RB-VALUE", "Rebalance value detergent", 5), product("RB-DISH", "Rebalance dish soap", 4)
    detergent = {"item_name":"Laundry detergent", "base_item":"laundry detergent", "quantity":3, "unit":"bottle", "source_kind":"manual", "source_requirement_id":1}
    dish = {"item_name":"Dish soap", "base_item":"dish soap", "quantity":1, "unit":"bottle", "source_kind":"manual", "source_requirement_id":2}
    resolution = {"subtotal":34, "total_cart_cost":34, "cart_items":[
        {"requirement":detergent, "selected_product":current, "alternatives":[value], "packages_to_buy":3, "needs_user_choice":False, "availability":"in_stock", "selection_confidence":"suggested"},
        {"requirement":dish, "selected_product":unchanged, "alternatives":[], "packages_to_buy":1, "needs_user_choice":False, "availability":"in_stock", "selection_confidence":"suggested"}]}
    replace_current_from_resolution(household_id=household.id, store_identity_id=selected["retail_store_identity_id"], resolved_cart=resolution)
    db.session.commit()
