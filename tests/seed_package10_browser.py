import json
import os
from datetime import datetime, timedelta, timezone

from app import PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, GroceryItem, RetailProductCache, UserPreference, UserSetting
from services.household_context import household_id
from services.selected_store import select_store


missing = os.environ.get("RUNG_P10_MISSING_SETUP") == "1"
with app.app_context():
    db.create_all()
    hid = household_id()
    account = Account(
        household_id=hid, checking_balance=1200.0, expected_paycheck=1000.0,
        pay_period_days=14, food_allocation_pct=99.0, zip_code="65084",
    )
    db.session.add(account)
    db.session.flush()
    rows = [
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100.00"),
        Bill(household_id=hid, name="Rent", amount=300.0,
             due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=False),
        Bill(household_id=hid, name="Required fuel", amount=100.0,
             due_date=datetime.now(timezone.utc) + timedelta(days=3), is_paid=False, is_gas_estimate=True),
        ExpenseTransaction(
            household_id=hid, description="Established payday", amount=1000.0,
            category="income", source="manual", local_account_id=account.id,
            date=datetime.now(timezone.utc) - timedelta(days=5),
        ),
        GroceryItem(household_id=hid, item_name="milk", store_name="Walmart"),
    ]
    if not missing:
        rows.append(UserPreference(household_id=hid, key="baseline_grocery_cost", value="200.00"))
    db.session.add_all(rows)
    select_store(
        hid, retailer="walmart", store_id="357", store_name="Walmart — Versailles",
        address="1003 W Newton St, Versailles, MO 65084", postal_code="65084", account=account,
    )
    selected = {
        "title": "Great Value Whole Milk", "product_id": "milk-1", "us_item_id": "milk-1",
        "package_size": "1 gal", "price": 4.0, "availability": "in_stock",
        "verified_location": True, "source": "serpapi_walmart", "data_quality": "RECENT_CONFIRMED",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "requirement": {"item_name": "milk", "base_item": "milk", "quantity": 1.0, "category": "General"},
        "selected_product": selected, "alternatives": [], "candidates": [selected],
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "selection_confidence": "suggested",
        "needs_user_choice": False, "selection_policy_version": 5,
    }
    db.session.add(RetailProductCache(
        retailer="walmart", store_id="357", store_name="Walmart — Versailles",
        store_address="1003 W Newton St, Versailles, MO 65084", requested_query="milk",
        base_item="milk", product_id="milk-1", us_item_id="milk-1", title="Great Value Whole Milk",
        package_size="1 gal", price=4.0, availability="in_stock", provider_source="serpapi_walmart",
        verified_location=True, response_json=json.dumps(payload), retrieved_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.session.commit()
    print(f"seeded package10 missing={missing} household={hid} account={account.id}")
