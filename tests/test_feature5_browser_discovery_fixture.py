"""Narrow guard for the Feature 5 real-browser discovery provider."""
from __future__ import annotations

import os
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import Account
from services.household_context import household_id
from services.selected_store import get_selected_store
from tests.run_feature5_browser_server import deterministic_nearby_stores


def test_browser_discovery_fixture_offers_stores_without_selecting_or_mutating_cart(monkeypatch):
    import app as app_module
    with app.app_context():
        db.drop_all(); db.create_all(); db.session.add(Account(household_id=household_id(), checking_balance=100)); db.session.commit()
    monkeypatch.setattr(app_module, "_discover_supported_stores", deterministic_nearby_stores)
    response = app.test_client().post("/api/location/nearby-stores", json={"zip_code": "65084"})
    assert response.status_code == 200
    assert [(row["store_id"], row["retailer"]) for row in response.get_json()["stores"]] == [("A", "walmart"), ("B", "walmart")]
    with app.app_context():
        assert get_selected_store(household_id()).get("canonical") is False
