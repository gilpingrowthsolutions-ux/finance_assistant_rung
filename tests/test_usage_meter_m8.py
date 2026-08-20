from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ.setdefault("PLAID_CLIENT_ID", "plaid_test_client")
os.environ.setdefault("PLAID_SECRET", "plaid_test_secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("PLAID_TOKEN_ENCRYPTION_KEY", "x7cUQ1K8v1SCh4skQ53QqE5s8z3v8c2n6cihVQMcWDo=")

from app import app, db, Account, PlaidItem, ExpenseTransaction  # noqa: E402
from models import UsageEvent  # noqa: E402
from services.retail.base import ProductSearchResult, RetailProduct, RetailStore, ShoppingRequirement  # noqa: E402
from services.retail.cart import build_verified_walmart_cart  # noqa: E402
from services import retail as retail_pkg  # noqa: E402
from services.plaid_foundation import PlaidHttpClient, PlaidRuntimeConfig  # noqa: E402
from services.household_context import household_id as current_household_id  # noqa: E402


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1250.0, food_allocation_pct=40.0, pay_period_days=14, meals_per_day=3))
        db.session.commit()
    return app.test_client()


def _usage_summary(client):
    resp = client.get("/api/internal/usage/summary")
    assert resp.status_code == 200
    return resp.get_json() or {}


def test_deterministic_copilot_records_zero_llm_calls(client):
    resp = client.post("/api/copilot/parse", json={"text": "I spent $12 on gas."})
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert (data.get("actions_taken") or {}).get("expenses_logged")

    summary = _usage_summary(client)
    assert (summary.get("today") or {}).get("llm_calls") == 0


def test_llm_fallback_records_usage_with_tokens_and_cost(client, monkeypatch):
    rates_resp = client.post(
        "/api/internal/usage/rates",
        json={
            "llm": {
                "default_input_per_1k_usd": 0.01,
                "default_output_per_1k_usd": 0.03,
            }
        },
    )
    assert rates_resp.status_code == 200

    def fake_parse(_text, groq_api_key="", staging_only=False, allow_llm=True):
        return {
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "spending_events": [],
            "income_events": [],
            "balance_reconciliation": None,
            "shopping_corrections": [],
            "bill_updates": [],
            "target_meals": None,
            "meal_servings": None,
            "clarification_question": None,
            "_fallback": False,
            "_parse_meta": {"path": "llm_json", "llm_calls": 1, "repair_attempted": False, "validation": "valid", "latency_ms": 12},
            "_llm_usage": {"provider": "groq", "model": "openai/gpt-oss-120b", "llm_calls": 1, "input_tokens": 1200, "output_tokens": 300},
        }

    monkeypatch.setattr("app.parse_copilot_prompt", fake_parse)
    resp = client.post("/api/copilot/parse", json={"text": "ambiguous request"})
    assert resp.status_code == 200

    with app.app_context():
        ev = UsageEvent.query.filter_by(category="llm").order_by(UsageEvent.id.desc()).first()
        assert ev is not None
        assert ev.input_tokens == 1200
        assert ev.output_tokens == 300
        assert ev.cost_status == "known"
        # 1200/1000*0.01 + 300/1000*0.03 = 0.021 USD => 21000 micros
        assert ev.estimated_cost_micros == 21000


def test_unconfigured_rate_records_usage_with_unknown_cost(client, monkeypatch):
    def fake_parse(_text, groq_api_key="", staging_only=False, allow_llm=True):
        return {
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "spending_events": [],
            "income_events": [],
            "balance_reconciliation": None,
            "shopping_corrections": [],
            "bill_updates": [],
            "target_meals": None,
            "_fallback": False,
            "_parse_meta": {"path": "llm_json", "llm_calls": 1, "repair_attempted": False, "validation": "valid", "latency_ms": 10},
            "_llm_usage": {"provider": "groq", "model": "openai/gpt-oss-120b", "llm_calls": 1, "input_tokens": 50, "output_tokens": 50},
        }

    monkeypatch.setattr("app.parse_copilot_prompt", fake_parse)
    resp = client.post("/api/copilot/parse", json={"text": "needs llm"})
    assert resp.status_code == 200

    summary = _usage_summary(client)
    assert (summary.get("today") or {}).get("unknown_unpriced_usage_count", 0) >= 1


class _FakeRetailProvider:
    def find_stores(self, *, postal_code: str):
        return []

    def search_products(self, requirement: ShoppingRequirement, *, store: RetailStore, limit: int = 20):
        product = RetailProduct.now(
            requested_query=requirement.search_query(),
            retailer="walmart",
            store=store,
            product_id="p1",
            us_item_id="u1",
            upc="0001",
            title="Milk",
            brand="Brand",
            variant=None,
            package_size="1 ct",
            price=3.49,
            availability="in_stock",
            price_type="pickup",
            product_url=None,
            source="serpapi_walmart",
            verified_location=True,
        )
        return ProductSearchResult(store, store, [product], 1)

    def get_product(self, product_id: str, *, store: RetailStore, requested_query: str):
        return RetailProduct.now(
            requested_query=requested_query,
            retailer="walmart",
            store=store,
            product_id=product_id,
            us_item_id="u1",
            upc="0001",
            title="Milk",
            brand="Brand",
            variant=None,
            package_size="1 ct",
            price=3.49,
            availability="in_stock",
            price_type="pickup",
            product_url=None,
            source="serpapi_walmart",
            verified_location=True,
        )


def test_retail_external_and_cache_events(client, monkeypatch):
    req = ShoppingRequirement(item_name="milk", base_item="milk")
    store = RetailStore(store_id="357", name="Walmart", address=None, postal_code="65084", verified=True)

    monkeypatch.setattr("services.retail.cart._active_manual_requirements", lambda: [req])
    monkeypatch.setattr("services.retail.cart._load_cached", lambda *args, **kwargs: None)

    with app.app_context():
        payload = build_verified_walmart_cart(force_refresh=False, provider=_FakeRetailProvider(), owner_scope="u1")
        assert payload["resolution_stats"]["search_calls"] == 1

    with app.app_context():
        external = UsageEvent.query.filter_by(category="retail_provider", operation="product_search").all()
        cache_miss = UsageEvent.query.filter_by(category="retail_cache", cache_status="miss").all()
        assert len(external) >= 1
        assert len(cache_miss) >= 1

    # second run from cache should not add a new external search event
    cached_payload = {
        "selected_product": {
            "requested_query": "milk",
            "retailer": "walmart",
            "store": store.to_dict(),
            "product_id": "p1",
            "us_item_id": "u1",
            "upc": "0001",
            "title": "Milk",
            "brand": "Brand",
            "variant": None,
            "package_size": "1 ct",
            "price": 3.49,
            "availability": "in_stock",
            "price_type": "pickup",
            "product_url": None,
            "source": "serpapi_walmart",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "verified_location": True,
            "regular_price": None,
            "promo_price": None,
            "fulfillment": None,
        },
        "alternatives": [],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "selection_confidence": "high",
        "needs_user_choice": False,
        "candidates": [],
    }
    monkeypatch.setattr("services.retail.cart._load_cached", lambda *args, **kwargs: cached_payload)

    with app.app_context():
        before = UsageEvent.query.filter_by(category="retail_provider", operation="product_search").count()
        payload2 = build_verified_walmart_cart(force_refresh=False, provider=_FakeRetailProvider(), owner_scope="u1")
        after = UsageEvent.query.filter_by(category="retail_provider", operation="product_search").count()
        assert payload2["resolution_stats"]["search_calls"] == 0
        assert after == before


def test_retail_kill_switch_degrades_without_fabricated_confirmed_local(client, monkeypatch):
    client.post(
        "/api/internal/usage/controls",
        json={"kill_switches": {"retail_live_refresh_enabled": False}},
    )
    req = ShoppingRequirement(item_name="milk", base_item="milk")
    monkeypatch.setattr("services.retail.cart._active_manual_requirements", lambda: [req])
    monkeypatch.setattr("services.retail.cart._load_cached", lambda *args, **kwargs: None)

    with app.app_context():
        payload = build_verified_walmart_cart(force_refresh=True, provider=_FakeRetailProvider(), owner_scope="u2")
        item = payload["cart_items"][0]
        assert item["resolved"] is False
        assert item["confirmed_local_store"] is False


def test_plaid_api_usage_event_records_request_not_transaction_count(client, monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {
                "request_id": "req_123",
                "added": [{"id": 1}, {"id": 2}, {"id": 3}],
                "modified": [],
                "removed": [],
                "next_cursor": "c1",
                "has_more": False,
            }

    monkeypatch.setattr("services.plaid_foundation.requests.post", lambda *args, **kwargs: _Resp())

    with app.app_context():
        client_obj = PlaidHttpClient(PlaidRuntimeConfig(client_id="c", secret="s", env="sandbox"))
        client_obj.transactions_sync("access", None)
        ev = UsageEvent.query.filter_by(category="plaid", operation="transactions_sync").order_by(UsageEvent.id.desc()).first()
        assert ev is not None
        assert ev.request_count == 1


def test_plaid_sync_kill_switch_preserves_connection_and_manual_flow(client):
    client.post(
        "/api/internal/usage/controls",
        json={"kill_switches": {"plaid_sync_enabled": False}},
    )

    with app.app_context():
        row = PlaidItem(
            household_id=current_household_id(),
            owner_scope="anonymous",
            plaid_item_id="it_123",
            access_token_encrypted="enc",
            connection_status="connected",
        )
        db.session.add(row)
        db.session.commit()

    status_before = client.get("/api/plaid/status")
    assert status_before.status_code == 200
    assert (status_before.get_json() or {}).get("connected") is True

    blocked = client.post("/api/plaid/sync-transactions", json={})
    assert blocked.status_code == 429

    status_after = client.get("/api/plaid/status")
    assert status_after.status_code == 200
    assert (status_after.get_json() or {}).get("connected") is True

    tx_resp = client.post("/api/transactions", json={"description": "manual", "amount": 9.5, "category": "discretionary"})
    assert tx_resp.status_code == 200


def test_usage_events_do_not_change_household_financials(client):
    before = client.get("/api/budget/summary").get_json() or {}

    with app.app_context():
        db.session.add(
            UsageEvent(
                owner_scope="anonymous",
                category="llm",
                provider="groq",
                operation="copilot_parse",
                success=True,
                external_call=True,
                request_count=1,
                estimated_cost_micros=25000,
                cost_status="known",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

    after = client.get("/api/budget/summary").get_json() or {}
    assert before.get("checking_balance") == after.get("checking_balance")
    assert before.get("safe_disposable") == after.get("safe_disposable")


def test_aggregate_daily_and_monthly_split(client):
    with app.app_context():
        now = datetime.now(timezone.utc)
        current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_month = current_month_start - timedelta(days=1)

        db.session.add(
            UsageEvent(
                owner_scope="anonymous",
                category="llm",
                provider="groq",
                operation="copilot_parse",
                success=True,
                external_call=True,
                request_count=1,
                estimated_cost_micros=100000,
                cost_status="known",
                created_at=now,
            )
        )
        db.session.add(
            UsageEvent(
                owner_scope="anonymous",
                category="llm",
                provider="groq",
                operation="copilot_parse",
                success=True,
                external_call=True,
                request_count=1,
                estimated_cost_micros=700000,
                cost_status="known",
                created_at=previous_month,
            )
        )
        db.session.commit()

    summary = _usage_summary(client)
    today = summary.get("today") or {}
    month = summary.get("month") or {}
    assert today.get("known_estimated_cost_micros") == 100000
    assert month.get("known_estimated_cost_micros") >= 100000


def test_llm_kill_switch_preserves_deterministic_path(client):
    client.post(
        "/api/internal/usage/controls",
        json={"kill_switches": {"llm_enabled": False}},
    )
    resp = client.post("/api/copilot/parse", json={"text": "I spent $12 on gas."})
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert (data.get("actions_taken") or {}).get("expenses_logged")


def test_provider_specific_llm_limit_blocks_optional_calls(client, monkeypatch):
    client.post(
        "/api/internal/usage/controls",
        json={"provider_limits": {"llm_calls_per_day": 1}},
    )
    with app.app_context():
        db.session.add(
            UsageEvent(
                owner_scope="anonymous",
                category="llm",
                provider="groq",
                operation="copilot_parse",
                success=True,
                external_call=True,
                request_count=1,
                cost_status="unknown",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

    def fake_parse(_text, groq_api_key="", staging_only=False, allow_llm=True):
        # Should run with allow_llm=False after gate and therefore produce deterministic fallback.
        assert allow_llm is False
        return {
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "spending_events": [],
            "income_events": [],
            "balance_reconciliation": None,
            "shopping_corrections": [],
            "bill_updates": [],
            "target_meals": None,
            "_fallback": True,
            "_parse_meta": {"path": "regex_fallback", "llm_calls": 0, "repair_attempted": False, "validation": "degraded", "latency_ms": 3},
        }

    monkeypatch.setattr("app.parse_copilot_prompt", fake_parse)
    resp = client.post("/api/copilot/parse", json={"text": "tell me something open ended"})
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert "temporarily" in str(payload.get("llm_error") or "").lower()


def test_usage_metadata_excludes_sensitive_text(client, monkeypatch):
    def fake_parse(_text, groq_api_key="", staging_only=False, allow_llm=True):
        return {
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "spending_events": [],
            "income_events": [],
            "balance_reconciliation": None,
            "shopping_corrections": [],
            "bill_updates": [],
            "target_meals": None,
            "_fallback": False,
            "_parse_meta": {"path": "llm_json", "llm_calls": 1, "repair_attempted": False, "validation": "valid", "latency_ms": 7},
            "_llm_usage": {"provider": "groq", "model": "model", "llm_calls": 1, "input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr("app.parse_copilot_prompt", fake_parse)
    secret_prompt = "my api_key is abc and plaid token is xyz"
    resp = client.post("/api/copilot/parse", json={"text": secret_prompt})
    assert resp.status_code == 200

    with app.app_context():
        ev = UsageEvent.query.filter_by(category="llm").order_by(UsageEvent.id.desc()).first()
        assert ev is not None
        blob = (ev.metadata_json or "") + " " + (ev.llm_model or "") + " " + (ev.provider or "")
        assert "api_key" not in blob.lower()
        assert "token" not in blob.lower()
        assert "abc" not in blob.lower()
        assert "xyz" not in blob.lower()
