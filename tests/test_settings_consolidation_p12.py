"""Focused regression tests for Package 12 — Settings + Location Consolidation.

Covers:
- /api/settings/location-sharing GET/POST
- /api/settings/current-location GET
- GPS nearby-store discovery does NOT auto-select a store
- Selected store remains unchanged through location refresh
"""

from __future__ import annotations

import os

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest
from datetime import datetime, timezone

from app import app, db
from models import Account, Household, UserSetting, RetailStoreIdentity
from services.household_context import household_id as current_household_id
from services.selected_store import get_selected_store, select_store


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        db.session.add(Account(
            household_id=hid,
            checking_balance=1000.0,
            zip_code="65084",
            city_state="Eldon, MO",
        ))
        db.session.commit()
    return app.test_client()


# ── Location Sharing ────────────────────────────────────────────────────

class TestLocationSharing:
    def test_default_is_off(self, client):
        resp = client.get("/api/settings/location-sharing")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["location_sharing_enabled"] is False

    def test_toggle_on(self, client):
        resp = client.post(
            "/api/settings/location-sharing",
            json={"location_sharing_enabled": True},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["location_sharing_enabled"] is True

        # Verify persistence
        resp2 = client.get("/api/settings/location-sharing")
        assert resp2.get_json()["location_sharing_enabled"] is True

    def test_toggle_off(self, client):
        # Turn on first
        client.post(
            "/api/settings/location-sharing",
            json={"location_sharing_enabled": True},
        )
        # Turn off
        resp = client.post(
            "/api/settings/location-sharing",
            json={"location_sharing_enabled": False},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["location_sharing_enabled"] is False

    def test_missing_field_returns_400(self, client):
        resp = client.post(
            "/api/settings/location-sharing",
            json={},
        )
        assert resp.status_code == 400

    def test_invalid_type_returns_400(self, client):
        resp = client.post(
            "/api/settings/location-sharing",
            json={"location_sharing_enabled": "yes"},
        )
        assert resp.status_code == 400

    def test_household_scoped(self, client):
        """Location sharing is household-scoped via UserSetting."""
        with app.app_context():
            hid = current_household_id()
            setting = UserSetting.query.filter_by(
                household_id=hid,
                key="location_sharing_enabled",
            ).first()
            # Before toggle, no row exists
            assert setting is None

        client.post(
            "/api/settings/location-sharing",
            json={"location_sharing_enabled": True},
        )

        with app.app_context():
            hid = current_household_id()
            setting = UserSetting.query.filter_by(
                household_id=hid,
                key="location_sharing_enabled",
            ).first()
            assert setting is not None
            assert setting.value == "true"


# ── Current Location (read-only) ────────────────────────────────────────

class TestCurrentLocation:
    def test_returns_account_location(self, client):
        resp = client.get("/api/settings/current-location")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["zip_code"] == "65084"
        assert body["city_state"] == "Eldon, MO"

    def test_includes_selected_store(self, client):
        resp = client.get("/api/settings/current-location")
        assert resp.status_code == 200
        body = resp.get_json()
        store = body.get("selected_store", {})
        assert isinstance(store, dict)
        # No store selected yet — store_id should be empty or legacy
        assert store.get("store_id") is not None  # present in response

    def test_includes_location_sharing_state(self, client):
        resp = client.get("/api/settings/current-location")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "location_sharing_enabled" in body
        assert body["location_sharing_enabled"] is False

    def test_after_store_selection_shows_canonical(self, client):
        with app.app_context():
            hid = current_household_id()
            select_store(
                hid,
                retailer="walmart",
                store_id="store-123",
                store_name="Walmart Supercenter",
                address="123 Main St",
                city="Eldon",
                state="MO",
                postal_code="65026",
            )
            db.session.commit()

        resp = client.get("/api/settings/current-location")
        assert resp.status_code == 200
        body = resp.get_json()
        store = body["selected_store"]
        assert store["store_id"] == "store-123"
        assert store["name"] == "Walmart Supercenter"
        assert store["canonical"] is True

    def test_read_only_does_not_mutate(self, client):
        """GET /api/settings/current-location must not change any state."""
        before_resp = client.get("/api/settings/current-location")
        before = before_resp.get_json()

        # Call it again
        after_resp = client.get("/api/settings/current-location")
        after = after_resp.get_json()

        assert before == after


# ── GPS must NOT silently change selected store ──────────────────────────

class TestGPSDoesNotChangeSelectedStore:
    def test_nearby_stores_discovery_does_not_select(self, client, monkeypatch):
        """POST /api/location/nearby-stores discovers stores but does NOT
        auto-select one — the user must explicitly choose."""
        import app as app_module

        # Mock the store discovery to return stores
        monkeypatch.setattr(
            app_module,
            "_discover_supported_stores",
            lambda **kwargs: {
                "status": "ok",
                "zip_code": "94105",
                "city_state": "San Francisco, CA",
                "state_code": "CA",
                "stores": [
                    {
                        "retailer": "walmart",
                        "store_id": "walmart-94105-1",
                        "name": "Walmart Supercenter",
                        "address": "100 Market St, San Francisco, CA 94105",
                        "postal_code": "94105",
                        "distance_miles": 1.2,
                    }
                ],
            },
        )

        resp = client.post(
            "/api/location/nearby-stores",
            json={"auto_detect": True, "latitude": 37.78, "longitude": -122.39},
        )
        assert resp.status_code == 200
        data = resp.get_json()

        # Stores were found
        assert len(data["stores"]) == 1

        # But the selected store should NOT have changed
        with app.app_context():
            hid = current_household_id()
            account = Account.query.filter_by(household_id=hid).first()
            selected = get_selected_store(hid, account=account)
            # No canonical store was auto-selected
            assert selected.get("canonical") is False

    def test_area_check_does_not_select_store(self, client, monkeypatch):
        """POST /api/location/area-check detects new area but does NOT
        auto-select a store."""
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

        resp = client.post(
            "/api/location/area-check",
            json={"latitude": 37.78, "longitude": -122.39},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["new_area_detected"] is True

        # No store should be auto-selected
        with app.app_context():
            hid = current_household_id()
            account = Account.query.filter_by(household_id=hid).first()
            selected = get_selected_store(hid, account=account)
            assert selected.get("canonical") is False

    def test_explicit_store_survives_location_refresh(self, client, monkeypatch):
        """After user selects a store, nearby-stores discovery does NOT
        change the selected store."""
        import app as app_module

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

        monkeypatch.setattr(
            app_module,
            "_discover_supported_stores",
            lambda **kwargs: {
                "status": "ok",
                "zip_code": "65084",
                "city_state": "Eldon, MO",
                "state_code": "MO",
                "stores": [
                    {
                        "retailer": "kroger",
                        "store_id": "kroger-new-store",
                        "name": "Kroger Marketplace",
                        "address": "789 Elm St, Eldon, MO 65026",
                        "postal_code": "65026",
                        "distance_miles": 2.0,
                    }
                ],
            },
        )

        resp = client.post(
            "/api/location/nearby-stores",
            json={"zip_code": "65084"},
        )
        assert resp.status_code == 200

        # Original store must still be selected
        with app.app_context():
            hid = current_household_id()
            selected = get_selected_store(hid)
            assert selected["store_id"] == "my-chosen-store"
            assert selected["name"] == "My Chosen Walmart"
            assert selected["canonical"] is True

    def test_select_store_requires_explicit_user_action(self, client):
        """Store selection only happens through /api/location/select-store."""
        with app.app_context():
            hid = current_household_id()
            selected = get_selected_store(hid)
            assert selected.get("canonical") is False

        # Explicit selection
        resp = client.post(
            "/api/location/select-store",
            json={
                "retailer": "walmart",
                "store_id": "explicit-store",
                "store_name": "Explicit Store",
                "store_address": "123 Main St",
                "zip_code": "65026",
                "city_state": "Eldon, MO",
            },
        )
        assert resp.status_code == 200

        with app.app_context():
            hid = current_household_id()
            selected = get_selected_store(hid)
            assert selected["store_id"] == "explicit-store"
            assert selected["canonical"] is True
