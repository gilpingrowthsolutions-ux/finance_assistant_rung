from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, GroceryItem, UserPreference, UserSetting
from services.household_context import household_id
from services.retail.base import RetailStore
from services.selected_store import select_store


def _verified_cart(store_id: str, store_name: str, total: float = 12.34) -> dict:
    return {
        "cart_items": [{
            "keyword": "milk", "product_label": "Whole Milk", "estimated_price": total,
            "unit_price": total, "packages_to_buy": 1, "confirmed_local_store": True,
            "price_source": "provider_fixture", "store_name": store_name,
        }],
        "subtotal": total,
        "total_cart_cost": total,
        "grocery_tax_rate": 0.0,
        "tax_amount": 0.0,
        "pantry_items_skipped": 0,
        "recipes_used": [],
        "store": {"store_id": store_id, "name": store_name, "postal_code": "65026"},
    }


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = household_id()
        account = Account(
            household_id=hid, checking_balance=1200.0, expected_paycheck=1000.0,
            pay_period_days=14, food_allocation_pct=99.0,
        )
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="20"),
            UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100.00"),
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="200.00"),
            Bill(household_id=hid, name="Required fuel", amount=50.0,
                 due_date=datetime.now(timezone.utc) + timedelta(days=3),
                 is_paid=False, is_gas_estimate=True),
            ExpenseTransaction(
                household_id=hid, description="Established payday", amount=1000.0,
                category="income", source="manual", local_account_id=account.id,
                date=datetime.now(timezone.utc) - timedelta(days=5),
            ),
            GroceryItem(household_id=hid, item_name="milk", store_name="Walmart"),
        ])
        select_store(hid, retailer="walmart", store_id="357", store_name="Walmart Supercenter", account=account)
        db.session.commit()
    yield app.test_client()


def _safe(client) -> dict:
    response = client.get("/api/budget/summary")
    assert response.status_code == 200
    return (response.get_json() or {})["safe_to_spend"]


def test_legacy_fields_do_not_control_canonical_safe_to_spend(client):
    before = _safe(client)
    with app.app_context():
        account = Account.query.one()
        account.food_allocation_pct = 1.0
        account.meals_per_day = 12
        db.session.commit()
    after = _safe(client)
    assert after["authority"] == "canonical_pyf_v1"
    assert after["safe_to_spend_cents"] == before["safe_to_spend_cents"]
    assert after["needs_total_cents"] == before["needs_total_cents"]


def test_walmart_default_is_canonical_grocery_need_remaining(client):
    fixture = _verified_cart("357", "Walmart Supercenter")
    with patch("services.retail.cart.build_verified_retail_cart", return_value=fixture) as build:
        response = client.post("/api/grocery/generate-pay-period-plan", json={"recipe_ids": []})
    assert response.status_code == 200
    budget = response.get_json()["budget"]
    assert budget["grocery_need_budget"] == 200.0
    assert budget["budget_source"] == "canonical_grocery_need_remaining"
    assert budget["food_budget"] == 200.0
    assert budget["food_budget_compatibility_alias"] is True
    assert build.call_args.kwargs["budget_limit"] == 200.0


def test_explicit_override_wins_without_mutating_pyf(client):
    before = _safe(client)
    fixture = _verified_cart("357", "Walmart Supercenter")
    with patch("services.retail.cart.build_verified_retail_cart", return_value=fixture) as build:
        response = client.post("/api/grocery/generate-pay-period-plan", json={
            "recipe_ids": [], "budget_limit": "45.67",
        })
    assert response.status_code == 200
    budget = response.get_json()["budget"]
    assert budget["grocery_need_budget"] == 45.67
    assert budget["budget_source"] == "explicit_request"
    assert build.call_args.kwargs["budget_limit"] == 45.67
    assert _safe(client) == before


def test_realized_grocery_spend_reduces_default_without_double_count(client):
    with app.app_context():
        account = Account.query.one()
        account.checking_balance = 1150.0
        db.session.add(ExpenseTransaction(
            household_id=household_id(), description="Finished Shopping", amount=50.0,
            category="grocery", source="finished_shopping", local_account_id=account.id,
            date=datetime.now(timezone.utc),
        ))
        db.session.commit()
    safe = _safe(client)
    assert safe["components"]["grocery_spend_to_date"] == 50.0
    assert safe["components"]["groceries_remaining"] == 150.0
    fixture = _verified_cart("357", "Walmart Supercenter")
    with patch("services.retail.cart.build_verified_retail_cart", return_value=fixture):
        response = client.post("/api/grocery/generate-pay-period-plan", json={"recipe_ids": []})
    assert response.get_json()["budget"]["grocery_need_budget"] == 150.0


def test_missing_setup_never_falls_back_to_legacy_values(client):
    with app.app_context():
        UserPreference.query.filter_by(key="baseline_grocery_cost").delete()
        account = Account.query.one()
        account.food_allocation_pct = 100.0
        db.session.commit()
    response = client.post("/api/grocery/generate-pay-period-plan", json={"recipe_ids": []})
    body = response.get_json() or {}
    assert response.status_code == 409
    assert body["code"] == "grocery_budget_setup_required"
    assert body["budget"]["available"] is False
    assert "grocery_need" in body["missing_setup"]
    assert "food_budget" not in body["budget"]


def test_kroger_uses_same_canonical_default(client):
    with app.app_context():
        hid = household_id()
        account = Account.query.one()
        select_store(hid, retailer="kroger", store_id="loc-a", store_name="Gerbes - Eldon", account=account)
        db.session.commit()
    fixture = _verified_cart("loc-a", "Gerbes - Eldon")
    with patch("services.retail.cart.build_verified_retail_cart", return_value=fixture) as build:
        response = client.post("/api/grocery/generate-pay-period-plan", json={"recipe_ids": []})
    assert response.status_code == 200
    assert response.get_json()["budget"]["grocery_need_budget"] == 200.0
    assert build.call_args.kwargs["retailer"] == "kroger"
    assert isinstance(build.call_args.kwargs["store"], RetailStore)
