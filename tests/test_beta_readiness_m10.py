from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ.setdefault("PLAID_CLIENT_ID", "plaid_test_client")
os.environ.setdefault("PLAID_SECRET", "plaid_test_secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("PLAID_TOKEN_ENCRYPTION_KEY", "x7cUQ1K8v1SCh4skQ53QqE5s8z3v8c2n6cihVQMcWDo=")

from app import (  # noqa: E402
    PYF_TARGET_SETTING_KEY,
    REQUIRED_EXPENSE_REVIEWED,
    REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY,
    app,
    db,
    init_db,
    _validate_startup_configuration,
)
from services.household_context import household_id as current_household_id  # noqa: E402
from models import (  # noqa: E402
    Account,
    Bill,
    ExpenseTransaction,
    GroceryItem,
    PlaidAccount,
    PlaidItem,
    PlaidTransaction,
    ShoppingTripCompletion,
    TransactionReconciliation, IncomePlanVersion,
    UserPreference,
    UserSetting,
)


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        account = Account(
                household_id=current_household_id(),
                checking_balance=1000.0,
                food_allocation_pct=40.0,
                pay_period_days=14,
                meals_per_day=3,
                expected_paycheck=1400.0,
                zip_code="65084",
                is_onboarded=False,
            )
        db.session.add(account)
        db.session.flush()
        hid = current_household_id()
        db.session.add_all([
            IncomePlanVersion(household_id=hid, operation_id="beta-plan", expected_income_cents=140000, effective_at=datetime.now(timezone.utc)-timedelta(days=30), source="test_confirmation"),
            UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value="10"),
            UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value="100.00"),
            UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
            UserPreference(household_id=hid, key="baseline_grocery_cost", value="200.00"),
            Bill(household_id=hid, name="Required fuel", amount=60.0, due_date=datetime.now(timezone.utc) + timedelta(days=3), is_gas_estimate=True, is_paid=False),
            ExpenseTransaction(household_id=hid, description="Established payday", amount=1400.0, category="income", source="manual", local_account_id=account.id, date=datetime.now(timezone.utc) - timedelta(days=5)),
        ])
        db.session.commit()
    return app.test_client()


def _summary(client):
    r = client.get("/api/budget/summary")
    assert r.status_code == 200
    return r.get_json() or {}


def _safe_state(client) -> str | None:
    return ((_summary(client).get("safe_to_spend") or {}).get("state"))


def _add_income(amount: float = 1400.0, days_ago: int = 0):
    with app.app_context():
        account = Account.query.first()
        account.checking_balance = float(account.checking_balance or 0.0) + amount
        db.session.add(
            ExpenseTransaction(
                household_id=current_household_id(),
                description="Payroll",
                amount=amount,
                category="income",
                source="manual",
                local_account_id=account.id,
                date=datetime.now(timezone.utc) - timedelta(days=days_ago),
            )
        )
        db.session.add(account)
        db.session.commit()


def _seed_plaid_identity(owner_scope: str = "anonymous"):
    with app.app_context():
        item = PlaidItem(
            household_id=current_household_id(),
            owner_scope=owner_scope,
            plaid_item_id="item_beta",
            access_token_encrypted="enc",
            connection_status="connected",
            institution_name="Sandbox Bank",
            last_sync_at=datetime.now(timezone.utc),
        )
        db.session.add(item)
        db.session.flush()
        db.session.add(
            PlaidAccount(
                household_id=current_household_id(),
                owner_scope=owner_scope,
                plaid_item_id=item.id,
                plaid_account_id="acc_beta",
                rung_account_id=1,
                name="Checking",
                is_active=True,
            )
        )
        db.session.add(
            PlaidTransaction(
                household_id=current_household_id(),
                owner_scope=owner_scope,
                plaid_item_id=item.id,
                plaid_transaction_id="tx_beta_1",
                plaid_account_id="acc_beta",
                amount_cents=2500,
                signed_amount_cents=-2500,
                direction="outflow",
                name="Coffee",
                merchant_name="Coffee",
                description="Coffee",
                transaction_date=datetime.now(timezone.utc).date(),
                authorized_date=datetime.now(timezone.utc).date(),
            )
        )
        db.session.commit()


def test_01_clean_household_first_use_path(client):
    state = client.get("/api/onboarding/state")
    assert state.status_code == 200
    assert (state.get_json() or {}).get("show_onboarding") is True

    upd = client.post(
        "/api/account/update",
            json={
            "checking_balance": 1200.0,
                "expected_paycheck": 1500.0,
                "expected_paycheck_operation_id": "beta-update-plan",
            "pay_period_days": 14,
            "food_allocation_pct": 35.0,
        },
    )
    assert upd.status_code == 200

    bill = client.post(
        "/bills",
        json={"name": "Rent", "amount": 600, "due_date": (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")},
    )
    assert bill.status_code == 200

    buffer_set = client.post("/api/settings/safe-to-spend", json={"protected_buffer": 150.0})
    assert buffer_set.status_code == 200

    defaults = client.post(
        "/api/settings/household-shopping-defaults",
        json={"shopping_style": "save_most"},
    )
    assert defaults.status_code == 200

    location = client.post(
        "/api/location/update",
        json={"zip_code": "65084", "store_name": "Walmart", "location_id": "357"},
    )
    assert location.status_code == 200


def test_location_autodetect_updates_zip_tax_and_store(client, monkeypatch):
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
    monkeypatch.setattr(
        app_module,
        "find_nearest_kroger",
        lambda **_kwargs: {
            "location_id": "12345",
            "store_name": "Kroger Marketplace",
            "chain_display": "Kroger",
            "zip_code": "94105",
            "state_code": "CA",
            "city_state": "San Francisco, CA",
        },
    )

    resp = client.post(
        "/api/location/update",
        json={
            "auto_detect": True,
            "latitude": 37.789,
            "longitude": -122.394,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json() or {}
    location = body.get("location") or {}

    assert location.get("zip_code") == "94105"
    assert location.get("store_name") == "Kroger"
    assert location.get("location_id") == ""
    assert location.get("city_state") == "San Francisco, CA"
    assert location.get("sales_tax_rate") is None
    assert location.get("grocery_tax_rate") is None
    assert location.get("tax_authority") == "canonical_tax_engine_at_purchase"
    store = body.get("store") or {}
    assert store.get("found") is False
    assert store.get("status") == "store_choice_required"

    summary = _summary(client)
    summary_location = summary.get("location") or {}
    assert summary_location.get("zip_code") == "94105"
    assert summary_location.get("store_name") == "Kroger"
    assert summary_location.get("location_id") == ""
    assert summary_location.get("sales_tax_rate") is None
    assert summary_location.get("grocery_tax_rate") is None
    assert summary_location.get("tax_authority") == "canonical_tax_engine_at_purchase"


def test_location_autodetect_uses_zip_combined_tax_lookup(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_reverse_geocode_us_location",
        lambda _lat, _lon: {
            "zip_code": "64106",
            "state_code": "MO",
            "city_state": "Kansas City, MO",
        },
    )
    monkeypatch.setattr(
        app_module,
        "find_nearest_kroger",
        lambda **_kwargs: {
            "location_id": "888",
            "store_name": "Kroger Marketplace",
            "chain_display": "Kroger",
            "zip_code": "64106",
            "state_code": "MO",
            "city_state": "Kansas City, MO",
        },
    )
    monkeypatch.setattr(app_module, "_lookup_combined_sales_tax_by_zip", lambda _zip: 0.091)

    resp = client.post(
        "/api/location/update",
        json={
            "auto_detect": True,
            "latitude": 39.1,
            "longitude": -94.58,
        },
    )
    assert resp.status_code == 200
    location = (resp.get_json() or {}).get("location") or {}

    # Device location is discovery context, never purchase-tax authority.
    assert location.get("sales_tax_rate") is None
    assert location.get("grocery_tax_rate") is None
    assert location.get("tax_authority") == "canonical_tax_engine_at_purchase"


def test_manual_zip_save_updates_location_without_selecting_store(client):
    resp = client.post(
        "/api/location/update",
        json={
            "zip_code": "65026",
            "sales_tax_rate": 0.0825,
            "grocery_tax_rate": 0.0125,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json() or {}
    location = body.get("location") or {}
    store = body.get("store") or {}

    assert location.get("zip_code") == "65026"
    assert location.get("city_state") == ""
    assert store.get("found") is False
    assert store.get("status") == "store_choice_required"
    assert store.get("location_id") in {None, ""}

    summary = _summary(client)
    summary_location = summary.get("location") or {}
    assert summary_location.get("zip_code") == "65026"
    assert summary_location.get("city_state") == ""
    assert summary_location.get("is_saved") is True


def test_manual_zip_invalid_rejected_with_safe_message(client):
    resp = client.post(
        "/api/location/update",
        json={
            "zip_code": "eldon-mo",
            "sales_tax_rate": 0.0825,
            "grocery_tax_rate": 0.0125,
        },
    )
    assert resp.status_code == 400
    body = resp.get_json() or {}
    assert body.get("error") == "invalid_zip_code"
    assert body.get("user_message") == "We couldn't save that location. Please check the ZIP code and try again."


def test_autodetect_updates_location_but_requires_explicit_store_choice(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_reverse_geocode_us_location",
        lambda _lat, _lon: {
            "zip_code": "65026",
            "state_code": "MO",
            "city_state": "Eldon, MO",
        },
    )
    resp = client.post(
        "/api/location/update",
        json={
            "auto_detect": True,
            "latitude": 38.348,
            "longitude": -92.581,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json() or {}
    store = body.get("store") or {}
    assert store.get("found") is False
    assert store.get("status") == "store_choice_required"
    assert body.get("user_message") == "Location saved. Choose a nearby supported store to continue shopping."


def test_autodetect_reverse_geocode_failure_returns_safe_message(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "_reverse_geocode_us_location", lambda _lat, _lon: {})

    resp = client.post(
        "/api/location/update",
        json={
            "auto_detect": True,
            "latitude": 38.348,
            "longitude": -92.581,
        },
    )
    assert resp.status_code == 422
    body = resp.get_json() or {}
    assert body.get("error") == "current_location_unavailable"
    assert body.get("user_message") == "We couldn't get your current location. Enter your ZIP code instead."


def test_bootstrap_default_location_not_reported_as_saved(client):
    summary = _summary(client)
    location = summary.get("location") or {}
    assert location.get("zip_code") == ""
    assert location.get("city_state") == ""
    assert location.get("is_saved") is False


def test_manual_zip_update_does_not_call_store_provider(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(
        app_module,
        "find_nearest_kroger",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("location update must not discover or select a store")),
    )
    resp = client.post(
        "/api/location/update",
        json={
            "zip_code": "65026",
            "sales_tax_rate": 0.0825,
            "grocery_tax_rate": 0.0125,
        },
    )
    assert resp.status_code == 200
    body = resp.get_json() or {}
    location = body.get("location") or {}
    store = body.get("store") or {}
    assert location.get("zip_code") == "65026"
    assert store.get("found") is False
    assert store.get("status") == "store_choice_required"
    assert body.get("user_message") == "Location saved. Choose a nearby supported store to continue shopping."


def test_nearby_stores_from_current_location_returns_multiple_choices(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_discover_supported_stores",
        lambda **_kwargs: {
            "status": "ok",
            "user_message": "",
            "zip_code": "65026",
            "city_state": "Eldon, MO",
            "state_code": "MO",
            "stores": [
                {
                    "retailer": "walmart",
                    "retailer_display": "Walmart",
                    "store_id": "357",
                    "name": "Walmart Supercenter",
                    "address": "100 Main St, Eldon, MO 65026",
                    "city": "Eldon",
                    "postal_code": "65026",
                    "distance_miles": 1.2,
                    "pickup_supported": True,
                    "verified": True,
                },
                {
                    "retailer": "kroger",
                    "retailer_display": "Kroger",
                    "store_id": "61500116",
                    "name": "Gerbes - Eldon",
                    "address": "105 E North St, Eldon, MO 65026",
                    "city": "Eldon",
                    "postal_code": "65026",
                    "distance_miles": 2.1,
                    "pickup_supported": True,
                    "verified": True,
                },
            ],
        },
    )

    resp = client.post(
        "/api/location/nearby-stores",
        json={"auto_detect": True, "latitude": 38.35, "longitude": -92.58},
    )
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("status") == "ok"
    assert len(body.get("stores") or []) == 2
    assert body.get("stores")[0].get("store_id") == "357"
    assert body.get("stores")[1].get("store_id") == "61500116"


def test_nearby_stores_zip_fallback(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_discover_supported_stores",
        lambda **_kwargs: {
            "status": "ok",
            "user_message": "",
            "zip_code": "65026",
            "city_state": "Eldon, MO",
            "state_code": "MO",
            "stores": [
                {
                    "retailer": "walmart",
                    "retailer_display": "Walmart",
                    "store_id": "357",
                    "name": "Walmart Supercenter",
                    "address": "100 Main St, Eldon, MO 65026",
                    "city": "Eldon",
                    "postal_code": "65026",
                    "distance_miles": None,
                    "pickup_supported": None,
                    "verified": True,
                }
            ],
        },
    )

    resp = client.post("/api/location/nearby-stores", json={"zip_code": "65026"})
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("status") == "ok"
    assert (body.get("location") or {}).get("source") == "zip"
    assert len(body.get("stores") or []) == 1


def test_select_store_persists_exact_store_id_and_survives_reload(client):
    resp = client.post(
        "/api/location/select-store",
        json={
            "retailer": "walmart",
            "store_id": "357",
            "store_name": "Walmart Supercenter",
            "zip_code": "65026",
            "city_state": "Eldon, MO",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("status") == "store_selected"
    assert (body.get("store") or {}).get("location_id") == "357"

    summary = _summary(client)
    location = summary.get("location") or {}
    assert location.get("store_name") == "Walmart Supercenter"
    assert location.get("location_id") == "357"
    assert location.get("zip_code") == ""
    assert (location.get("selected_store") or {}).get("postal_code") == "65026"


def test_area_check_reports_new_area_without_silent_store_change(client, monkeypatch):
    import app as app_module

    selected = client.post(
        "/api/location/select-store",
        json={
            "retailer": "walmart",
            "store_id": "357",
            "store_name": "Walmart Supercenter",
            "zip_code": "65026",
            "city_state": "Eldon, MO",
        },
    )
    assert selected.status_code == 200

    monkeypatch.setattr(
        app_module,
        "_reverse_geocode_us_location",
        lambda _lat, _lon: {"zip_code": "63101", "state_code": "MO", "city_state": "St Louis, MO"},
    )

    check = client.post(
        "/api/location/area-check",
        json={"latitude": 38.627, "longitude": -90.199},
    )
    assert check.status_code == 200
    check_body = check.get_json() or {}
    assert check_body.get("new_area_detected") is True

    summary = _summary(client)
    location = summary.get("location") or {}
    assert location.get("location_id") == "357"
    assert location.get("store_name") == "Walmart Supercenter"


def test_nearby_stores_no_supported_store_message(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_discover_supported_stores",
        lambda **_kwargs: {
            "status": "no_supported_store",
            "user_message": "We found your location, but couldn't find a supported store nearby.",
            "zip_code": "65026",
            "city_state": "Eldon, MO",
            "state_code": "MO",
            "stores": [],
        },
    )

    resp = client.post(
        "/api/location/nearby-stores",
        json={"auto_detect": True, "latitude": 38.35, "longitude": -92.58},
    )
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("status") == "no_supported_store"
    assert body.get("user_message") == "We found your location, but couldn't find a supported store nearby."


def test_nearby_stores_provider_outage_safe_message(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_discover_supported_stores",
        lambda **_kwargs: {
            "status": "store_search_unavailable",
            "user_message": "We found your location, but store search isn't available right now. Try again shortly.",
            "zip_code": "65026",
            "city_state": "Eldon, MO",
            "state_code": "MO",
            "stores": [],
        },
    )

    resp = client.post(
        "/api/location/nearby-stores",
        json={"auto_detect": True, "latitude": 38.35, "longitude": -92.58},
    )
    assert resp.status_code == 503
    body = resp.get_json() or {}
    assert body.get("status") == "store_search_unavailable"
    assert body.get("user_message") == "We found your location, but store search isn't available right now. Try again shortly."
    location = body.get("location") or {}
    assert location.get("zip_code") == "65026"
    assert location.get("city_state") == "Eldon, MO"


def test_02_ready_vs_needs_setup_state(client):
    ready = client.get("/api/internal/beta/readiness")
    assert ready.status_code == 200
    assert ((ready.get_json() or {}).get("readiness") or {}).get("ready") is True

    with app.app_context():
        a = Account.query.first()
        a.expected_paycheck = 0.0
        a.pay_period_days = 0
        db.session.add(a)
        db.session.commit()

    not_ready = client.get("/api/internal/beta/readiness")
    body = not_ready.get_json() or {}
    assert (body.get("readiness") or {}).get("ready") is False
    assert "pay_period_or_income_history" in ((body.get("readiness") or {}).get("missing_critical") or [])


def test_03_safe_to_spend_unavailable_when_critical_data_missing(client):
    with app.app_context():
        a = Account.query.first()
        a.expected_paycheck = 0.0
        a.pay_period_days = 0
        db.session.add(a)
        db.session.commit()

    s = _summary(client)
    safe = s.get("safe_to_spend") or {}
    assert safe.get("state") == "needs_setup"
    assert safe.get("safe_to_spend") is None


def test_04_manual_only_household_path_works_without_plaid(client):
    ctl = client.post("/api/internal/usage/controls", json={"kill_switches": {"plaid_sync_enabled": False}})
    assert ctl.status_code == 200

    exp = client.post("/api/transactions", json={"description": "Coffee", "amount": 10, "category": "discretionary"})
    inc = client.post("/api/transactions", json={"description": "Gift", "amount": -25, "category": "income"})
    assert exp.status_code == 200
    assert inc.status_code == 200

    affordability = client.post("/api/decision/can-i-buy", json={"item_name": "Soap", "cost": 8.25})
    assert affordability.status_code == 200


def test_05_plaid_household_path_still_exposed(client):
    _seed_plaid_identity()
    status = client.get("/api/plaid/status")
    assert status.status_code == 200
    body = status.get_json() or {}
    assert body.get("connected") is True


def test_06_provider_capability_status_reporting(client):
    d = client.get("/api/internal/beta/diagnostics")
    assert d.status_code == 200
    body = d.get_json() or {}
    caps = body.get("capabilities") or {}
    assert "plaid" in caps
    assert "retail" in caps
    assert "llm" in caps
    assert "copilot_deterministic" in caps


def test_07_missing_optional_provider_does_not_crash_startup(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("KROGER_CLIENT_ID", raising=False)
    monkeypatch.delenv("KROGER_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("PLAID_ENABLED", "0")
    _validate_startup_configuration()


def test_08_missing_required_secret_for_enabled_plaid_fails_safely(monkeypatch):
    monkeypatch.setenv("PLAID_ENABLED", "1")
    monkeypatch.delenv("PLAID_TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        _validate_startup_configuration()


def test_09_existing_db_upgrade_is_nondestructive(client):
    with app.app_context():
        db.session.add(UserSetting(household_id=current_household_id(), key="m10_upgrade_marker", value="1"))
        db.session.add(ExpenseTransaction(household_id=current_household_id(), description="Before upgrade", amount=12.0, category="discretionary"))
        db.session.commit()
    init_db()
    with app.app_context():
        assert UserSetting.query.filter_by(
            household_id=current_household_id(),
            key="m10_upgrade_marker",
        ).first() is not None
        assert ExpenseTransaction.query.filter_by(description="Before upgrade").count() == 1


def test_10_existing_preferences_preserved(client):
    with app.app_context():
        db.session.add(UserPreference(household_id=current_household_id(), key="favorite_proteins", value='["chicken"]'))
        db.session.commit()
    init_db()
    with app.app_context():
        row = UserPreference.query.filter_by(
            household_id=current_household_id(),
            key="favorite_proteins",
        ).first()
        assert row is not None
        assert "chicken" in str(row.value)


def test_11_existing_financial_transactions_preserved(client):
    with app.app_context():
        db.session.add(ExpenseTransaction(household_id=current_household_id(), description="Persist me", amount=22.0, category="discretionary"))
        db.session.commit()
    init_db()
    with app.app_context():
        assert ExpenseTransaction.query.filter_by(description="Persist me").count() == 1


def test_12_existing_plaid_metadata_preserved(client):
    _seed_plaid_identity()
    init_db()
    with app.app_context():
        assert PlaidItem.query.count() == 1
        assert PlaidAccount.query.count() == 1
        assert PlaidTransaction.query.count() == 1


def test_13_retailer_outage_degrades_safely(client, monkeypatch):
    from services.retail import cart as retail_cart

    with app.app_context():
        db.session.add(
            GroceryItem(
                household_id=current_household_id(),
                item_name="milk",
                estimated_price=0.0,
                store_name="Walmart",
                location_context="",
                is_purchased=False,
                recipe_ids="",
            )
        )
        db.session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(retail_cart, "build_verified_walmart_cart", _boom)

    resp = client.post(
        "/api/grocery/generate-pay-period-plan",
        json={"recipe_ids": [], "store_name": "walmart", "use_verified_cart": True},
    )
    assert resp.status_code == 502
    body = resp.get_json() or {}
    assert body.get("code") == "retail_provider_unavailable"
    assert body.get("degraded_mode") == "manual_shopping_available"


def test_14_llm_outage_preserves_deterministic_commands(client):
    ctl = client.post("/api/internal/usage/controls", json={"kill_switches": {"llm_enabled": False}})
    assert ctl.status_code == 200
    parsed = client.post("/api/copilot/parse", json={"text": "I spent $9.00 on snacks", "user_id": "anonymous"})
    assert parsed.status_code == 200
    actions = (parsed.get_json() or {}).get("actions_taken") or {}
    assert len(actions.get("expenses_logged") or []) >= 1


def test_15_plaid_outage_preserves_manual_workflows(client):
    ctl = client.post("/api/internal/usage/controls", json={"kill_switches": {"plaid_sync_enabled": False}})
    assert ctl.status_code == 200

    sync = client.post("/api/plaid/sync-transactions", json={"user_id": "anonymous"})
    assert sync.status_code == 429

    manual = client.post("/api/transactions", json={"description": "Manual expense", "amount": 11.0, "category": "discretionary"})
    assert manual.status_code == 200


def test_16_no_sensitive_secrets_in_diagnostics(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-groq-key")
    monkeypatch.setenv("SERPAPI_API_KEY", "super-secret-serp")
    body = client.get("/api/internal/beta/diagnostics").get_json() or {}
    text = str(body)
    assert "super-secret-groq-key" not in text
    assert "super-secret-serp" not in text


def test_17_feedback_record_stores_no_automatic_financial_payload(client):
    resp = client.post(
        "/api/internal/beta/feedback",
        json={"category": "ux", "description": "Button label is unclear", "screen_context": "overview"},
    )
    assert resp.status_code == 201
    row = (resp.get_json() or {}).get("feedback") or {}
    assert row.get("category") == "ux"
    assert "checking_balance" not in str(row)
    assert "transactions" not in str(row)


def test_18_shopping_to_finished_shopping_updates_safe_to_spend_once(client):
    before = float(((_summary(client).get("safe_to_spend") or {}).get("safe_to_spend") or 0.0))

    stage = client.post(
        "/api/grocery/finished-shopping/stage",
        json={"planned_total": 40.0, "actual_total": 45.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "m10-trip"},
    )
    op_id = (stage.get_json() or {}).get("operation_id")
    done = client.post(
        "/api/grocery/finished-shopping/complete",
        json={"planned_total": 40.0, "actual_total": 45.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "m10-trip", "operation_id": op_id, "confirm": True},
    )
    assert done.status_code == 200

    duplicate = client.post(
        "/api/grocery/finished-shopping/complete",
        json={"planned_total": 40.0, "actual_total": 45.0, "use_planned_total": False, "retailer": "walmart", "store_name": "Walmart", "store_id": "357", "cart_signature": "m10-trip", "operation_id": op_id, "confirm": True},
    )
    assert duplicate.status_code == 200

    after = float(((_summary(client).get("safe_to_spend") or {}).get("safe_to_spend") or 0.0))
    with app.app_context():
        assert ShoppingTripCompletion.query.count() == 1
    assert after <= before


def test_19_reconciliation_preserves_single_effect(client):
    manual = client.post("/api/transactions", json={"description": "Store", "amount": 25.0, "category": "discretionary"})
    manual_id = (manual.get_json() or {}).get("id")

    _seed_plaid_identity()

    with app.app_context():
        before = ExpenseTransaction.query.count()
        db.session.add(
            TransactionReconciliation(
                household_id=current_household_id(),
                owner_scope="anonymous",
                manual_transaction_id=manual_id,
                plaid_transaction_id="tx_beta_1",
                status="proposed",
                match_strength=98,
            )
        )
        db.session.commit()

    decision = client.post(
        "/api/reconciliation/decision",
        json={
            "user_id": "anonymous",
            "action": "match",
            "manual_transaction_id": manual_id,
            "plaid_transaction_id": "tx_beta_1",
        },
    )
    assert decision.status_code == 200
    with app.app_context():
        after = ExpenseTransaction.query.count()
    assert after == before


def test_20_db_destructive_guard_still_active(client):
    from extensions import assert_safe_destructive_db_target

    with pytest.raises(RuntimeError):
        assert_safe_destructive_db_target(
            "sqlite:////home/ky/finance_assistant/rung_finance.db",
            None,
        )
