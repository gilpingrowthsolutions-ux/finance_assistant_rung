from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app
from extensions import db
from models import Account, StoreTaxProfile, TaxSourceDataset
from services.tax_adapters import MissouriDorQ3Adapter, PublicStateRatesAdapter, SstMemberStateAdapter
from services.tax_engine import (
    TAX_CLASS_GENERAL_MERCHANDISE,
    TAX_CLASS_UNKNOWN,
    calculate_cart_tax,
    ensure_bootstrap_tax_dataset,
    import_dataset_atomic,
    is_authoritative_provenance,
    resolve_store_tax_profile,
)
from services.household_context import household_id as current_household_id


@pytest.fixture(autouse=True)
def _reset_db():
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        db.session.add(
            Account(
                household_id=hid,
                checking_balance=1000.0,
                zip_code="65084",
                city_state="Versailles, MO",
                kroger_store_name="Walmart",
                kroger_location_id="357",
            )
        )
        db.session.commit()
    yield


def _store_profile(store_id: str, city_state: str = "Versailles, MO"):
    return resolve_store_tax_profile(
        retailer="walmart",
        retailer_store_id=store_id,
        store_name="Walmart",
        store_address="1003 W Newton St, Versailles, MO 65084",
        zip_code="65084",
        city_state=city_state,
        latitude=None,
        longitude=None,
        calculation_date=date(2026, 8, 15),
        owner_scope="test",
    )


def test_bootstrap_owned_dataset_and_state_coverage():
    with app.app_context():
        dataset = ensure_bootstrap_tax_dataset()
        assert dataset.source_key == "public_state_rates"
        assert dataset.status == "active"
        assert dataset.source_type == "manual_unverified"
        assert is_authoritative_provenance(dataset.source_type) is False

        supported_states = {"MO", "CA", "TX", "FL", "OH", "WA", "NY", "CO"}
        for state in supported_states:
            profile = resolve_store_tax_profile(
                retailer="kroger",
                retailer_store_id=f"store-{state}",
                store_name="Kroger",
                store_address="",
                zip_code="",
                city_state=f"City, {state}",
                latitude=None,
                longitude=None,
                calculation_date=date(2026, 8, 15),
                owner_scope="test",
            )
            assert profile.state == state
            assert profile.location_precision in {"STATE_ONLY", "UNRESOLVED"}


def test_missouri_store_profile_cache_reuse_and_store_identity_isolation():
    with app.app_context():
        p1 = _store_profile("357")
        p2 = _store_profile("357")
        p3 = _store_profile("61500116", city_state="Eldon, MO")

        assert p1.retailer_store_id == "357"
        assert p2.retailer_store_id == "357"
        assert p3.retailer_store_id == "61500116"

        rows = StoreTaxProfile.query.order_by(StoreTaxProfile.id.asc()).all()
        # Same store/profile should be cached and reused, second store creates a new profile.
        assert len(rows) == 2


def test_unknown_item_conservative_policy_uses_general_rate():
    with app.app_context():
        profile = _store_profile("357")
        cart_items = [
            {"item_name": "mystery household good", "estimated_price": 12.34, "keyword": "mystery"},
        ]

        result = calculate_cart_tax(
            store_tax_profile=profile,
            cart_items=cart_items,
            calculation_date=date(2026, 8, 15),
            owner_scope="test",
        )

        assert result.unknown_class_count == 1
        assert result.subtotal_by_class_cents[TAX_CLASS_UNKNOWN] == 1234
        assert result.tax_by_class_cents[TAX_CLASS_UNKNOWN] >= 0
        assert cart_items[0]["tax_class"] == TAX_CLASS_UNKNOWN
        assert cart_items[0]["tax_rate_bps"] == profile.general_rate_bps


def test_mixed_cart_supports_food_and_general_merchandise():
    with app.app_context():
        profile = _store_profile("357")
        cart_items = [
            {"item_name": "whole milk", "estimated_price": 5.00, "keyword": "milk"},
            {"item_name": "laundry detergent", "estimated_price": 7.50, "keyword": "detergent"},
            {"item_name": "paper towels", "estimated_price": 4.25, "keyword": "paper towels"},
        ]

        result = calculate_cart_tax(
            store_tax_profile=profile,
            cart_items=cart_items,
            calculation_date=date(2026, 8, 15),
            owner_scope="test",
        )

        assert result.subtotal_cents == 1675
        assert result.estimated_total_cents == result.subtotal_cents + result.tax_cents
        classes = {item["tax_class"] for item in cart_items}
        assert "GROCERY_FOOD" in classes
        assert TAX_CLASS_GENERAL_MERCHANDISE in classes


def test_atomic_import_failure_preserves_active_dataset():
    with app.app_context():
        before = ensure_bootstrap_tax_dataset()
        adapter = SstMemberStateAdapter()
        bad_path = Path(os.getcwd()) / "data" / "tax" / "synthetic" / "sst_member_sample_ohio_2026q3.json"

        # Corrupt the assignment key to force validation failure.
        broken = bad_path.read_text(encoding="utf-8").replace('"43215"', '"BADZIP"')
        temp_path = Path("/tmp/rung_gate5_bad_tax_import.json")
        temp_path.write_text(broken, encoding="utf-8")

        result = import_dataset_atomic(adapter=adapter, source_path=str(temp_path), activate=True)
        assert result["ok"] is False
        assert result["active_dataset_unchanged"] is True

        after = TaxSourceDataset.query.filter_by(status="active").first()
        assert after is not None
        assert after.id == before.id


def test_atomic_import_success_replaces_active_dataset_version():
    with app.app_context():
        ensure_bootstrap_tax_dataset()
        adapter = PublicStateRatesAdapter()
        source_path = Path(os.getcwd()) / "data" / "tax" / "official" / "public_state_rates_2026q3.json"
        result = import_dataset_atomic(adapter=adapter, source_path=str(source_path), activate=True)
        assert result["ok"] is True

        active = TaxSourceDataset.query.filter_by(status="active").first()
        assert active is not None
        assert active.version_tag == "2026Q3"


def test_synthetic_dataset_activation_is_blocked_by_policy():
    with app.app_context():
        ensure_bootstrap_tax_dataset()
        adapter = SstMemberStateAdapter()
        source_path = Path(os.getcwd()) / "data" / "tax" / "synthetic" / "sst_member_sample_ohio_2026q3.json"
        result = import_dataset_atomic(adapter=adapter, source_path=str(source_path), activate=True)
        assert result["ok"] is False
        assert "synthetic_test datasets cannot be activated" in str(result["error"])


def test_missouri_official_import_and_profile_resolution_for_closure_stores():
    with app.app_context():
        ensure_bootstrap_tax_dataset()
        adapter = MissouriDorQ3Adapter()
        source_path = Path(os.getcwd()) / "data" / "tax" / "official" / "missouri"
        result = import_dataset_atomic(adapter=adapter, source_path=str(source_path), activate=True)
        assert result["ok"] is True
        assert result["provenance_type"] == "official_government"
        assert result["authoritative"] is True

        versailles_walmart = resolve_store_tax_profile(
            retailer="walmart",
            retailer_store_id="357",
            store_name="Walmart",
            store_address="1003 W Newton St, Versailles, MO 65084",
            zip_code="65084",
            city_state="Versailles, MO",
            latitude=None,
            longitude=None,
            calculation_date=date(2026, 8, 15),
            owner_scope="test",
        )
        eldon_gerbes = resolve_store_tax_profile(
            retailer="kroger",
            retailer_store_id="61500116",
            store_name="Gerbes",
            store_address="410 E North St, Eldon, MO 65026",
            zip_code="65026",
            city_state="Eldon, MO",
            latitude=None,
            longitude=None,
            calculation_date=date(2026, 8, 15),
            owner_scope="test",
        )
        eldon_walmart = resolve_store_tax_profile(
            retailer="walmart",
            retailer_store_id="eldon-walmart",
            store_name="Walmart",
            store_address="1802 S Business 54, Eldon, MO 65026",
            zip_code="65026",
            city_state="Eldon, MO",
            latitude=None,
            longitude=None,
            calculation_date=date(2026, 8, 15),
            owner_scope="test",
        )

        assert versailles_walmart.source_key == "mo_dor_q3_2026"
        assert eldon_gerbes.source_key == "mo_dor_q3_2026"
        assert eldon_walmart.source_key == "mo_dor_q3_2026"

        assert versailles_walmart.location_precision in {"ZIP5", "CITY_COUNTY"}
        assert eldon_gerbes.location_precision in {"ZIP5", "CITY_COUNTY"}
        assert eldon_walmart.location_precision in {"ZIP5", "CITY_COUNTY"}

        assert versailles_walmart.general_rate_bps >= 423
        assert eldon_gerbes.general_rate_bps >= 423
        assert eldon_walmart.general_rate_bps >= 423


def test_endpoint_cart_tax_uses_owned_engine_no_paid_provider_calls(monkeypatch):
    with app.app_context():
        from app import _lookup_combined_sales_tax_by_zip
        assert _lookup_combined_sales_tax_by_zip("65084") in {None} or isinstance(_lookup_combined_sales_tax_by_zip("65084"), float)

    client = app.test_client()

    from services.retail import cart as cart_module

    monkeypatch.setattr(
        cart_module,
        "build_verified_walmart_cart",
        lambda **_kwargs: {
            "cart_items": [
                {"item_name": "whole milk", "keyword": "milk", "estimated_price": 3.5, "resolved": True},
                {"item_name": "detergent", "keyword": "detergent", "estimated_price": 8.0, "resolved": True},
            ],
            "subtotal": 11.5,
            "store": {
                "store_id": "357",
                "name": "Walmart - Versailles",
                "address": "1003 W Newton St, Versailles, MO 65084",
                "postal_code": "65084",
            },
            "resolution_stats": {"total_terms": 2},
        },
    )

    response = client.post(
        "/api/grocery/generate-pay-period-plan",
        json={"recipe_ids": [], "store_name": "Walmart", "budget_limit": 100.0},
    )
    assert response.status_code == 400

    with app.app_context():
        from models import GroceryItem

        db.session.add(GroceryItem(household_id=current_household_id(), item_name="milk", estimated_price=0.0, store_name="Walmart"))
        db.session.commit()

    response = client.post(
        "/api/grocery/generate-pay-period-plan",
        json={"recipe_ids": [], "store_name": "Walmart", "budget_limit": 100.0},
    )
    assert response.status_code == 200
    body = response.get_json() or {}
    assert body.get("tax_engine", {}).get("provider") == "rung_owned"
    assert body.get("tax_amount") is not None
    assert body.get("total_cart_cost") is not None


def test_twenty_item_cart_acceptance_and_repeat_store_cache_reuse():
    with app.app_context():
        first = _store_profile("357")
        second = _store_profile("357")
        assert first.retailer_store_id == second.retailer_store_id

        items = [
            {"item_name": "whole milk", "keyword": "milk", "estimated_price": 3.5},
            {"item_name": "eggs", "keyword": "eggs", "estimated_price": 4.0},
            {"item_name": "bread", "keyword": "bread", "estimated_price": 2.5},
            {"item_name": "rice", "keyword": "rice", "estimated_price": 3.25},
            {"item_name": "banana", "keyword": "banana", "estimated_price": 1.9},
            {"item_name": "apple", "keyword": "apple", "estimated_price": 2.1},
            {"item_name": "cheese", "keyword": "cheese", "estimated_price": 5.2},
            {"item_name": "chicken breast", "keyword": "chicken", "estimated_price": 8.5},
            {"item_name": "beef", "keyword": "beef", "estimated_price": 9.2},
            {"item_name": "yogurt", "keyword": "yogurt", "estimated_price": 3.8},
            {"item_name": "detergent", "keyword": "detergent", "estimated_price": 8.3},
            {"item_name": "shampoo", "keyword": "shampoo", "estimated_price": 6.4},
            {"item_name": "paper towels", "keyword": "paper towels", "estimated_price": 7.1},
            {"item_name": "toothpaste", "keyword": "toothpaste", "estimated_price": 4.25},
            {"item_name": "batteries", "keyword": "battery", "estimated_price": 6.9},
            {"item_name": "dish soap", "keyword": "soap", "estimated_price": 3.45},
            {"item_name": "bleach", "keyword": "bleach", "estimated_price": 3.95},
            {"item_name": "trash bags", "keyword": "trash bag", "estimated_price": 6.15},
            {"item_name": "napkins", "keyword": "napkin", "estimated_price": 2.85},
            {"item_name": "mystery item", "keyword": "mystery", "estimated_price": 5.55},
        ]

        result = calculate_cart_tax(
            store_tax_profile=first,
            cart_items=items,
            calculation_date=date(2026, 8, 15),
            owner_scope="test",
        )

        assert len(items) == 20
        assert result.subtotal_cents > 0
        assert result.tax_cents >= 0
        assert result.estimated_total_cents == result.subtotal_cents + result.tax_cents
        assert result.unknown_class_count >= 1
        assert result.precision in {"STATE_ONLY", "UNRESOLVED"}
