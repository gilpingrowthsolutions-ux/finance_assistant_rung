"""Package 12 Browser Acceptance — Settings + Location Consolidation.

Uses an explicit disposable SQLite database to verify:
- Settings loads with correct structure
- PYF target persists through canonical authority
- Protected buffer persists through canonical authority
- Shopping defaults persist/reload
- Location Sharing toggles and persists
- Current location context is read-only
- Nearby-store discovery does not auto-select a store
- Previously selected store remains unchanged through GPS/location refresh
- Settings contains no required operational store-selection flow
- No provider/API secrets appear
- Reload preserves state
- No duplicate mutation requests
"""

from __future__ import annotations

import os

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest
from app import app, db
from models import Account, UserSetting
from services.household_context import household_id as current_household_id
from services.selected_store import get_selected_store, select_store


@pytest.fixture(autouse=True)
def setup_db():
    """Create fresh schema and a test household in a disposable DB."""
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        db.session.add(Account(
            household_id=hid,
            checking_balance=1500.0,
            pay_period_days=14,
            expected_paycheck=2000.0,
        ))
        db.session.commit()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client():
    return app.test_client()


# ── Settings Loads ───────────────────────────────────────────────────────

def test_settings_loads_real_persisted_values(client):
    """Settings page loads real persisted values from canonical authority."""
    resp = client.get("/api/budget/summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "checking_balance" in data
    assert data["checking_balance"] == 1500.0


# ── PYF Target ──────────────────────────────────────────────────────────

def test_pyp_target_persists_through_canonical_authority(client):
    """PYF target persists through canonical authority and survives reload."""
    resp = client.post("/api/settings/pay-yourself-first", json={
        "long_term_savings_target_percent": 25.0
    })
    assert resp.status_code == 200
    assert resp.get_json()["long_term_savings_target_percent"] == 25.0

    # Reload
    resp2 = client.get("/api/settings/pay-yourself-first")
    assert resp2.status_code == 200
    assert resp2.get_json()["long_term_savings_target_percent"] == 25.0

    # In budget summary
    resp3 = client.get("/api/budget/summary")
    safe = resp3.get_json().get("safe_to_spend", {})
    assert float(safe.get("long_term_savings_target_percent", 0)) == 25.0


# ── Protected Buffer ────────────────────────────────────────────────────

def test_protected_buffer_persists_through_canonical_authority(client):
    """Protected buffer persists through canonical authority."""
    resp = client.post("/api/settings/safe-to-spend", json={
        "protected_buffer": 200.0
    })
    assert resp.status_code == 200
    assert resp.get_json()["protected_buffer"] == 200.0

    resp2 = client.get("/api/settings/safe-to-spend")
    assert resp2.status_code == 200
    assert resp2.get_json()["protected_buffer"] == 200.0


# ── Shopping Defaults ───────────────────────────────────────────────────

def test_shopping_defaults_persist_and_reload(client):
    """Shopping defaults persist and reload correctly."""
    resp = client.post("/api/settings/household-shopping-defaults", json={
        "shopping_style": "save_most",
        "preferences": {"milk_type": "whole"},
    })
    assert resp.status_code == 200
    assert resp.get_json()["shopping_style"] == "save_most"

    resp2 = client.get("/api/settings/household-shopping-defaults")
    assert resp2.status_code == 200
    data = resp2.get_json()
    assert data["shopping_style"] == "save_most"
    assert data["preferences"].get("milk_type") == "whole"


# ── Location Sharing ────────────────────────────────────────────────────

def test_location_sharing_toggles_and_persists(client):
    """Location Sharing toggles and persists across reload."""
    # Default is off
    resp = client.get("/api/settings/location-sharing")
    assert resp.status_code == 200
    assert resp.get_json()["location_sharing_enabled"] is False

    # Toggle on
    resp2 = client.post("/api/settings/location-sharing", json={
        "location_sharing_enabled": True
    })
    assert resp2.status_code == 200
    assert resp2.get_json()["location_sharing_enabled"] is True

    # Reload
    resp3 = client.get("/api/settings/location-sharing")
    assert resp3.get_json()["location_sharing_enabled"] is True

    # Toggle off
    resp4 = client.post("/api/settings/location-sharing", json={
        "location_sharing_enabled": False
    })
    assert resp4.status_code == 200
    assert resp4.get_json()["location_sharing_enabled"] is False

    # Reload
    resp5 = client.get("/api/settings/location-sharing")
    assert resp5.get_json()["location_sharing_enabled"] is False


# ── Current Location (read-only) ────────────────────────────────────────

def test_current_location_is_read_only(client):
    """Current location context is read-only and shows store context."""
    resp = client.get("/api/settings/current-location")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "zip_code" in data
    assert "city_state" in data
    assert "selected_store" in data
    assert "location_sharing_enabled" in data

    # Call again — should be identical (read-only)
    resp2 = client.get("/api/settings/current-location")
    assert resp2.get_json() == data


def test_current_location_after_store_selection(client):
    """After store selection, current-location shows canonical store."""
    with app.app_context():
        hid = current_household_id()
        select_store(
            hid,
            retailer="walmart",
            store_id="store-789",
            store_name="Walmart Supercenter",
            address="123 Main St",
            city="Eldon",
            state="MO",
            postal_code="65026",
        )
        db.session.commit()

    resp = client.get("/api/settings/current-location")
    assert resp.status_code == 200
    store = resp.get_json()["selected_store"]
    assert store["store_id"] == "store-789"
    assert store["canonical"] is True


# ── GPS must NOT auto-select store ──────────────────────────────────────

def test_nearby_stores_discovery_does_not_select(client, monkeypatch):
    """POST /api/location/nearby-stores discovers but does NOT auto-select."""
    import app as app_module
    monkeypatch.setattr(app_module, "_discover_supported_stores", lambda **_kwargs: {
        "status": "ok", "user_message": "", "stores": [],
        "zip_code": "65084", "city_state": "Versailles, MO", "state_code": "MO",
        "provider_results": [],
    })
    resp = client.post("/api/location/nearby-stores", json={"zip_code": "65084"})
    assert resp.status_code == 200

    # No store should be auto-selected
    with app.app_context():
        hid = current_household_id()
        selected = get_selected_store(hid)
        assert selected.get("canonical") is False


def test_area_check_does_not_select_store(client, monkeypatch):
    """POST /api/location/area-check detects area but does NOT auto-select."""
    import app as app_module
    monkeypatch.setattr(
        app_module,
        "_reverse_geocode_us_location",
        lambda _lat, _lon: {
            "zip_code": "94105",
            "state_code": "CA",
            "city_state": "San Francisco, CA",
        },
    )

    resp = client.post("/api/location/area-check", json={
        "latitude": 37.78, "longitude": -122.39,
    })
    assert resp.status_code == 200
    assert resp.get_json()["new_area_detected"] is True

    # No store auto-selected
    with app.app_context():
        hid = current_household_id()
        selected = get_selected_store(hid)
        assert selected.get("canonical") is False


def test_selected_store_survives_location_refresh(client, monkeypatch):
    """After explicit selection, discovery must not change it."""
    with app.app_context():
        hid = current_household_id()
        select_store(
            hid,
            retailer="walmart",
            store_id="my-chosen-store",
            store_name="My Chosen Walmart",
            address="456 Oak St",
            city="Eldon",
            state="MO",
            postal_code="65026",
        )
        db.session.commit()

    import app as app_module
    monkeypatch.setattr(app_module, "_discover_supported_stores", lambda **_kwargs: {
        "status": "ok", "user_message": "", "stores": [],
        "zip_code": "65084", "city_state": "Versailles, MO", "state_code": "MO",
        "provider_results": [],
    })
    resp = client.post("/api/location/nearby-stores", json={"zip_code": "65084"})
    assert resp.status_code == 200

    with app.app_context():
        hid = current_household_id()
        selected = get_selected_store(hid)
        assert selected["store_id"] == "my-chosen-store"
        assert selected["canonical"] is True


# ── No Secrets ──────────────────────────────────────────────────────────

def test_no_provider_secrets_in_settings(client):
    """Settings endpoints must not expose provider/API secrets."""
    resp = client.get("/api/settings/groq-key")
    data = resp.get_json()
    assert "configured" in data
    assert "api_key" not in data

    resp2 = client.get("/api/settings/current-location")
    data2 = resp2.get_json()
    for key in data2:
        val = str(data2[key]).lower()
        assert "api_key" not in val
        assert "secret" not in val


# ── Reload Preserves State ──────────────────────────────────────────────

def test_reload_preserves_all_settings(client):
    """All Settings values survive a full reload cycle."""
    client.post("/api/settings/pay-yourself-first", json={
        "long_term_savings_target_percent": 30.0
    })
    client.post("/api/settings/safe-to-spend", json={
        "protected_buffer": 250.0
    })
    client.post("/api/settings/location-sharing", json={
        "location_sharing_enabled": True
    })
    client.post("/api/settings/household-shopping-defaults", json={
        "shopping_style": "prefer_brands_when_possible",
        "preferences": {"bread_type": "wheat"},
    })

    # Reload all
    pyf = client.get("/api/settings/pay-yourself-first").get_json()
    buf = client.get("/api/settings/safe-to-spend").get_json()
    ls = client.get("/api/settings/location-sharing").get_json()
    shop = client.get("/api/settings/household-shopping-defaults").get_json()
    loc = client.get("/api/settings/current-location").get_json()

    assert pyf["long_term_savings_target_percent"] == 30.0
    assert buf["protected_buffer"] == 250.0
    assert ls["location_sharing_enabled"] is True
    assert shop["shopping_style"] == "prefer_brands_when_possible"
    assert shop["preferences"]["bread_type"] == "wheat"
    assert loc["location_sharing_enabled"] is True


# ── No Duplicate Mutations ──────────────────────────────────────────────

def test_no_duplicate_mutations_on_idempotent_save(client):
    """Idempotent save should not create duplicate settings rows."""
    client.post("/api/settings/pay-yourself-first", json={
        "long_term_savings_target_percent": 20.0
    })
    client.post("/api/settings/pay-yourself-first", json={
        "long_term_savings_target_percent": 20.0
    })

    with app.app_context():
        hid = current_household_id()
        count = UserSetting.query.filter_by(
            household_id=hid,
            key="pyf_long_term_target_percent",
        ).count()
        assert count == 1, f"Expected 1 PYF setting, got {count}"


# ── Shopping/Copilot Store Behavior ─────────────────────────────────────

def test_shopping_copilot_store_behavior_preserved(client):
    """Shopping and Copilot still use canonical selected-store."""
    with app.app_context():
        hid = current_household_id()
        select_store(
            hid,
            retailer="kroger",
            store_id="kroger-456",
            store_name="Kroger Marketplace",
            address="200 Elm St",
            city="Versailles",
            state="MO",
            postal_code="65084",
        )
        db.session.commit()

    resp = client.get("/api/settings/current-location")
    store = resp.get_json()["selected_store"]
    assert store["retailer"] == "kroger"
    assert store["store_id"] == "kroger-456"
    assert store["canonical"] is True
