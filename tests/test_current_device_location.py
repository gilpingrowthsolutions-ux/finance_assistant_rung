"""Focused live-device location truthfulness and store-invariance coverage."""
from __future__ import annotations

import os

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import app, db
from models import Account, Household
from services.household_context import household_id as current_household_id
from services.selected_store import get_selected_store, select_store


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        account = Account(
            household_id=hid, zip_code="65084", city_state="Versailles, MO",
            latitude=38.3605, longitude=-92.5824,
        )
        db.session.add(account)
        db.session.flush()
        select_store(hid, retailer="kroger", store_id="61500116", store_name="Gerbes", account=account)
        db.session.commit()
    return app.test_client()


def _store_id(client):
    return client.get("/api/settings/current-location").get_json()["selected_store"]["store_id"]


def test_sharing_off_never_accepts_or_reports_a_current_device_location(client):
    assert _store_id(client) == "61500116"
    response = client.post("/api/location/current-device", json={
        "outcome": "available", "latitude": 37.7749, "longitude": -122.4194,
    })
    assert response.status_code == 409
    current = client.get("/api/settings/current-location").get_json()
    assert current["current_device_location"]["status"] == "disabled"
    assert _store_id(client) == "61500116"


def test_successful_device_update_is_current_discovery_input_and_keeps_store(client, monkeypatch):
    assert client.post("/api/settings/location-sharing", json={"location_sharing_enabled": True}).status_code == 200
    import app as app_module
    monkeypatch.setattr(app_module, "_reverse_geocode_us_location", lambda *_args: {
        "zip_code": "94105", "city_state": "San Francisco, CA", "state_code": "CA",
    })
    response = client.post("/api/location/current-device", json={
        "outcome": "available", "latitude": 37.7749, "longitude": -122.4194,
    })
    assert response.status_code == 200
    current = response.get_json()["current_device_location"]
    assert current["status"] == "available"
    assert current["location"]["zip_code"] == "94105"
    assert current["location"]["latitude"] == 37.7749
    assert _store_id(client) == "61500116"

    captured = {}
    def discover(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "user_message": "", "stores": [], "zip_code": "94105", "city_state": "San Francisco, CA", "state_code": "CA"}
    monkeypatch.setattr(app_module, "_discover_supported_stores", discover)
    discovery = client.post("/api/location/nearby-stores", json={
        "auto_detect": True, "latitude": 37.7749, "longitude": -122.4194,
    })
    assert discovery.status_code == 200
    assert captured == {"zip_code": "", "latitude": 37.7749, "longitude": -122.4194}
    assert _store_id(client) == "61500116"


def test_failure_never_masquerades_legacy_location_as_current_and_preserves_store(client):
    assert client.post("/api/settings/location-sharing", json={"location_sharing_enabled": True}).status_code == 200
    response = client.post("/api/location/current-device", json={"outcome": "unavailable"})
    assert response.status_code == 200
    current = response.get_json()["current_device_location"]
    assert current["status"] == "unavailable"
    assert current["last_known_location"]["zip_code"] == "65084"
    assert _store_id(client) == "61500116"


def test_latest_success_survives_revisit_without_silent_store_change(client, monkeypatch):
    assert client.post("/api/settings/location-sharing", json={"location_sharing_enabled": True}).status_code == 200
    import app as app_module
    monkeypatch.setattr(app_module, "_reverse_geocode_us_location", lambda *_args: {"zip_code": "63101", "city_state": "St Louis, MO"})
    client.post("/api/location/current-device", json={"outcome": "available", "latitude": 38.627, "longitude": -90.199})
    revisited = client.get("/api/settings/current-location").get_json()
    assert revisited["current_device_location"]["location"]["zip_code"] == "63101"
    assert _store_id(client) == "61500116"


def test_current_device_location_is_household_scoped(client, monkeypatch):
    with app.app_context():
        a_id = current_household_id()
        b = Household(public_id="bbbbbbbb-0000-0000-0000-000000000000", legacy_scope_key="b")
        db.session.add(b)
        db.session.flush()
        db.session.add(Account(household_id=b.id, zip_code="10001", city_state="New York, NY"))
        db.session.commit()
        b_id = b.id
    assert client.post("/api/settings/location-sharing", json={"location_sharing_enabled": True}).status_code == 200
    import app as app_module
    monkeypatch.setattr(app_module, "_reverse_geocode_us_location", lambda *_args: {"zip_code": "94105", "city_state": "San Francisco, CA"})
    client.post("/api/location/current-device", json={"outcome": "available", "latitude": 37.7749, "longitude": -122.4194})
    with app.app_context():
        assert get_selected_store(a_id)["store_id"] == "61500116"
        # B has no device-location setting and cannot inherit A's value.
        from models import UserSetting
        assert UserSetting.query.filter_by(household_id=b_id, key="current_device_location").first() is None
