"""Seed the explicitly disposable database used by Package 12 browser acceptance."""

from datetime import datetime, timedelta, timezone

from app import (
    LOCATION_SHARING_SETTING_KEY,
    NEXT_PAYDAY_SETTING_KEY,
    PYF_TARGET_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY,
    _save_household_shopping_defaults,
    app,
)
from extensions import db
from models import Account, GroceryItem, IncomePlanVersion, UserSetting
from services.household_context import household_id
from services.selected_store import select_store


with app.app_context():
    db.create_all()
    hid = household_id()
    account = Account(
        household_id=hid,
        checking_balance=1750.50,
        pay_period_days=14,
        expected_paycheck=2100.0,
        zip_code="65084",
        city_state="Versailles, MO",
        latitude=38.4314,
        longitude=-92.8410,
        is_onboarded=True,
    )
    db.session.add(account)
    db.session.flush()
    db.session.add_all([
        IncomePlanVersion(household_id=hid, operation_id="settings-browser-current-plan",
                          expected_income_cents=210000,
                          effective_at=datetime.now(timezone.utc) - timedelta(days=7),
                          source="test_confirmation"),
        UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="18.5"),
        UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="225.00"),
        UserSetting(household_id=hid, key=LOCATION_SHARING_SETTING_KEY, value="false"),
        UserSetting(household_id=hid, key=NEXT_PAYDAY_SETTING_KEY,
                    value=(datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()),
        GroceryItem(household_id=hid, item_name="milk", store_name=""),
    ])
    ok, errors = _save_household_shopping_defaults({
        "shopping_style": "save_most",
        "preferences": {"milk_type": "whole", "bread_type": "wheat"},
    }, commit=False)
    assert ok, errors
    select_store(
        hid,
        retailer="walmart",
        store_id="357",
        store_name="Walmart — Versailles",
        address="1003 W Newton St, Versailles, MO 65084",
        city="Versailles",
        state="MO",
        postal_code="65084",
        account=account,
    )
    db.session.commit()
    print(f"seeded Package 12 household={hid} account={account.id}")
