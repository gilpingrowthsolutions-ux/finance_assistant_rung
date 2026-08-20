from __future__ import annotations

import os
import hashlib
import hmac

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import app, db
from models import Account, GroceryItem, Household, UserSetting
from services.copilot_tools import _execute_add_grocery_item
from services.household_context import household_id as current_household_id
from services.selected_store import get_selected_store, select_store


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        db.session.add(Account(household_id=hid, checking_balance=1000.0, zip_code="65084"))
        db.session.commit()
    return app.test_client()


def _select(client, *, store_id: str, name: str, postal_code: str = "65026"):
    return client.post("/api/location/select-store", json={
        "retailer": "walmart",
        "store_id": store_id,
        "store_name": name,
        "store_address": f"{store_id} Main St, Eldon, MO {postal_code}",
        "zip_code": postal_code,
        "city_state": "Eldon, MO",
    })


def test_explicit_selection_is_canonical_for_shopping_copilot_and_reload(client, monkeypatch):
    selected = _select(client, store_id="store-a", name="Store A")
    assert selected.status_code == 200

    with app.app_context():
        hid = current_household_id()
        canonical = get_selected_store(hid)
        assert canonical["canonical"] is True
        assert canonical["store_id"] == "store-a"
        assert UserSetting.query.filter_by(household_id=hid, key="selected_shopping_store").count() == 1

    with app.app_context():
        result = _execute_add_grocery_item(item_name="milk")
    assert result["data"]["store_name"] == "Store A"

    captured = {}

    def fake_tax(**kwargs):
        captured.update(kwargs)
        return {
            "subtotal": 2.0, "tax_amount": 0.1, "total_cart_cost": 2.1,
            "grocery_tax_rate": 1.25, "applied_tax_pct": 5.0, "tax_engine": {},
        }

    import app as app_module
    monkeypatch.setattr(app_module, "_apply_owned_tax_to_cart", fake_tax)
    monkeypatch.setattr(app_module, "resolve_terms", lambda *_args, **_kwargs: {})
    cart = client.post("/api/grocery/generate-pay-period-plan", json={"recipe_ids": [], "budget_limit": 100.0})
    assert cart.status_code == 200
    body = cart.get_json() or {}
    cart_store = body.get("store") or {}
    assert (cart_store["store_id"], cart_store["name"]) == ("store-a", "Store A")
    assert captured["store_id"] == "store-a"
    assert captured["postal_code"] == "65026"

    reloaded = client.get("/api/budget/summary").get_json() or {}
    assert ((reloaded.get("location") or {}).get("selected_store") or {}).get("store_id") == "store-a"


def test_discovery_and_gps_never_overwrite_then_explicit_switch_updates_all_consumers(client, monkeypatch):
    assert _select(client, store_id="store-a", name="Store A").status_code == 200
    import app as app_module
    monkeypatch.setattr(app_module, "_discover_supported_stores", lambda **_kwargs: {
        "status": "ok", "user_message": "", "zip_code": "63101",
        "city_state": "St Louis, MO", "state_code": "MO", "stores": [{
            "retailer": "walmart", "store_id": "store-b", "name": "Store B",
            "address": "2 Market St, St Louis, MO 63101", "postal_code": "63101",
        }],
    })
    discovery = client.post("/api/location/nearby-stores", json={"zip_code": "63101"})
    assert discovery.status_code == 200
    assert (discovery.get_json() or {})["selected_store"]["store_id"] == "store-a"

    monkeypatch.setattr(app_module, "_reverse_geocode_us_location", lambda *_args: {
        "zip_code": "63101", "state_code": "MO", "city_state": "St Louis, MO",
    })
    gps = client.post("/api/location/update", json={
        "auto_detect": True, "latitude": 38.627, "longitude": -90.199,
    })
    assert gps.status_code == 200
    assert ((gps.get_json() or {})["location"]["selected_store"])["store_id"] == "store-a"

    assert _select(client, store_id="store-b", name="Store B", postal_code="63101").status_code == 200
    with app.app_context():
        result = _execute_add_grocery_item(item_name="bread")
    assert result["data"]["store_name"] == "Store B"
    summary = client.get("/api/budget/summary").get_json() or {}
    assert ((summary["location"]["selected_store"])["store_id"]) == "store-b"


def test_legacy_mirrors_cannot_override_canonical_state(client):
    assert _select(client, store_id="store-a", name="Store A").status_code == 200
    with app.app_context():
        hid = current_household_id()
        account = Account.query.filter_by(household_id=hid).one()
        account.kroger_location_id = "legacy-other"
        account.kroger_store_name = "Legacy Other"
        retailer = UserSetting.query.filter_by(household_id=hid, key="grocery_active_retailer").one()
        retailer.value = "kroger"
        db.session.commit()
        selected = get_selected_store(hid, account=account)
        assert (selected["retailer"], selected["store_id"], selected["name"]) == ("walmart", "store-a", "Store A")

    compatibility = client.post("/api/settings/grocery-retailer", json={"retailer": "kroger"})
    assert compatibility.status_code == 200
    assert (compatibility.get_json() or {})["retailer"] == "walmart"


def test_selected_store_is_household_scoped_and_crafted_request_cannot_cross_households(monkeypatch):
    app.testing = True
    monkeypatch.setenv("RUNG_HOUSEHOLD_CONTEXT_SECRET", "pkg4-household-secret")
    with app.app_context():
        db.drop_all()
        db.create_all()
        a = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="a")
        b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="b")
        db.session.add_all([a, b])
        db.session.flush()
        account_a = Account(household_id=a.id)
        account_b = Account(household_id=b.id)
        db.session.add_all([account_a, account_b])
        db.session.flush()
        select_store(a.id, retailer="walmart", store_id="a-store", store_name="A Store", account=account_a)
        select_store(b.id, retailer="kroger", store_id="b-store", store_name="B Store", account=account_b)
        db.session.commit()
        assert get_selected_store(a.id)["store_id"] == "a-store"
        assert get_selected_store(b.id)["store_id"] == "b-store"

        a_public_id = a.public_id
        a_id = a.id
        b_id = b.id

    signature = hmac.new(
        b"pkg4-household-secret", a_public_id.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    crafted = app.test_client().post(
        "/api/location/select-store",
        headers={"X-Household-Id": a_public_id, "X-Household-Signature": signature},
        json={
            "household_id": b_id,
            "retailer": "walmart", "store_id": "a-new", "store_name": "A New",
        },
    )
    assert crafted.status_code == 200
    with app.app_context():
        assert get_selected_store(a_id)["store_id"] == "a-new"
        assert get_selected_store(b_id)["store_id"] == "b-store"
