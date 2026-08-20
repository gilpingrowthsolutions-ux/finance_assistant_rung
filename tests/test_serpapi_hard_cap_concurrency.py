from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from multiprocessing import current_process

import pytest

POSTGRES_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")
if not POSTGRES_URL:
    pytest.skip("POSTGRES_TEST_DATABASE_URL not set", allow_module_level=True)

os.environ.pop("RUNG_DB_PATH", None)
os.environ["DATABASE_URL"] = POSTGRES_URL
os.environ.setdefault("PLAID_CLIENT_ID", "plaid_test_client")
os.environ.setdefault("PLAID_SECRET", "plaid_test_secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("PLAID_TOKEN_ENCRYPTION_KEY", "x7cUQ1K8v1SCh4skQ53QqE5s8z3v8c2n6cihVQMcWDo=")


def _upgrade_schema() -> None:
    env = dict(os.environ)
    env.pop("RUNG_DB_PATH", None)
    env["DATABASE_URL"] = POSTGRES_URL
    env["FLASK_APP"] = "app.py"
    completed = subprocess.run(
        [
            "/home/ky/finance_assistant/venv/bin/python",
            "-m",
            "flask",
            "db",
            "upgrade",
        ],
        cwd="/home/ky/finance_assistant",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)


if current_process().name == "MainProcess":
    _upgrade_schema()

for module_name in list(sys.modules):
    if module_name == "app" or module_name == "extensions" or module_name == "models" or module_name.startswith("services"):
        del sys.modules[module_name]

from app import app  # noqa: E402
from extensions import db  # noqa: E402
from models import UsageEvent, UsageLimitCounter  # noqa: E402
from services.retail import RetailProduct  # noqa: E402
from services.retail.cart import VERIFIED_WALMART_STORE  # noqa: E402
from services.retail.resolution import retail_resolution_service  # noqa: E402
from services.retail.shared_foundation import shared_retail_foundation  # noqa: E402
from services.retail.walmart_serpapi import WalmartSerpApiProvider  # noqa: E402
from services.usage_meter import set_usage_controls  # noqa: E402


class _CountingProvider(WalmartSerpApiProvider):
    def __init__(self, *, delay_seconds: float = 0.05, fail_first: bool = False) -> None:
        super().__init__(api_key="test")
        self.delay_seconds = delay_seconds
        self.fail_first = fail_first
        self.detail_calls = 0
        self._lock = threading.Lock()

    def search_products(self, requirement, *, store, limit=20):
        raise AssertionError("search_products should not be used in these tests")

    def get_product(self, product_id, *, store, requested_query):
        with self._lock:
            self.detail_calls += 1
            call_number = self.detail_calls
        time.sleep(self.delay_seconds)
        if self.fail_first and call_number == 1:
            raise RuntimeError("simulated serpapi failure")
        return RetailProduct.now(
            requested_query=requested_query,
            retailer="walmart",
            store=store,
            product_id=str(product_id),
            us_item_id=str(product_id),
            upc="000111",
            title=f"{requested_query.title()} 1 ct",
            brand="Brand",
            variant=None,
            package_size="1 ct",
            price=5.25,
            availability="in_stock",
            price_type="regular",
            product_url=None,
            source="serpapi_walmart",
            verified_location=True,
        )


def _reset_runtime_state() -> None:
    with app.app_context():
        db.session.execute(UsageEvent.__table__.delete())
        db.session.execute(UsageLimitCounter.__table__.delete())
        db.session.commit()


def _seed_exact_resource(resource_id: str, *, hours_old: int = 96) -> None:
    observed_at = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    with app.app_context():
        shared_retail_foundation.upsert_observation(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            retailer_product_id=resource_id,
            title=f"{resource_id.replace('-', ' ').title()} 1 ct",
            price=4.99,
            price_type="regular",
            price_source="serpapi_walmart",
            price_confidence="provider_confirmed",
            availability="in_stock",
            availability_source="serpapi_walmart",
            availability_confidence="provider_confirmed",
            observed_at=observed_at,
        )


def _set_controls(*, daily_cap: int | None, monthly_cap: int | None, retail_cap: int | None = 10_000, live_refresh: bool = True, serpapi_enabled: bool = True) -> None:
    with app.app_context():
        set_usage_controls(
            {
                "kill_switches": {
                    "retail_live_refresh_enabled": live_refresh,
                    "serpapi_fallback_enabled": serpapi_enabled,
                },
                "provider_limits": {
                    "serpapi_calls_per_day": daily_cap,
                    "serpapi_calls_per_month": monthly_cap,
                    "retail_external_calls_per_day": retail_cap,
                },
            }
        )


def _resolve_resource(resource_id: str, provider: WalmartSerpApiProvider) -> dict[str, object]:
    with app.app_context():
        assert db.engine.dialect.name == "postgresql"
        return retail_resolution_service.resolve_exact(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            retailer_product_id=resource_id,
            provider=provider,
            owner_scope="shared",
            explicit_live_refresh=False,
        )


def _threaded_concurrent_resolution(resource_ids: list[str], *, daily_cap: int | None, monthly_cap: int | None, retail_cap: int | None = 10_000, fail_first: bool = False) -> tuple[int, int, int, int]:
    _reset_runtime_state()
    for resource_id in resource_ids:
        _seed_exact_resource(resource_id)
    _set_controls(daily_cap=daily_cap, monthly_cap=monthly_cap, retail_cap=retail_cap)

    barrier = threading.Barrier(len(resource_ids))
    provider = _CountingProvider(delay_seconds=0.05, fail_first=fail_first)
    results: list[dict[str, object] | None] = [None] * len(resource_ids)

    def worker(index: int, resource_id: str) -> None:
        with app.app_context():
            assert db.engine.dialect.name == "postgresql"
            barrier.wait()
            results[index] = _resolve_resource(resource_id, provider)

    threads = [threading.Thread(target=worker, args=(index, resource_id)) for index, resource_id in enumerate(resource_ids)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    allowed = sum(1 for result in results if result and bool(result.get("external_call")))
    blocked = len(results) - allowed
    persisted = _counter_usage("retail_external_call:serpapi_walmart", "day")
    return provider.detail_calls, allowed, blocked, persisted


def _counter_usage(limit_key: str, period_type: str) -> int:
    with app.app_context():
        row = (
            db.session.query(UsageLimitCounter.used_count)
            .filter(UsageLimitCounter.limit_key == limit_key)
            .filter(UsageLimitCounter.period_type == period_type)
            .first()
        )
        return int(row[0] or 0) if row is not None else 0


def _multi_process_worker(resource_id: str, queue, barrier) -> None:
    try:
        from app import app as worker_app
        from extensions import db as worker_db
        from services.retail.cart import VERIFIED_WALMART_STORE as WORKER_STORE
        from services.retail.resolution import retail_resolution_service as worker_resolution_service

        provider = _CountingProvider(delay_seconds=0.05)
        with worker_app.app_context():
            assert worker_db.engine.dialect.name == "postgresql"
            barrier.wait()
            result = worker_resolution_service.resolve_exact(
                retailer="walmart",
                store=WORKER_STORE,
                retailer_product_id=resource_id,
                provider=provider,
                owner_scope="shared",
                explicit_live_refresh=False,
            )
            queue.put({
                "dialect": worker_db.engine.dialect.name,
                "invoked": 1 if result.get("external_call") else 0,
            })
    except Exception as exc:  # pragma: no cover - surfaced in parent assertion
        queue.put({"error": f"{type(exc).__name__}:{exc}"})


@pytest.mark.parametrize("daily_cap", [5])
def test_same_process_daily_cap_is_enforced_at_the_provider_boundary(daily_cap: int) -> None:
    resource_ids = [f"daily-{index}" for index in range(15)]
    provider_calls, allowed, blocked, persisted = _threaded_concurrent_resolution(
        resource_ids,
        daily_cap=daily_cap,
        monthly_cap=1_000,
    )
    assert provider_calls == 5
    assert allowed == 5
    assert blocked == 10
    assert persisted == 5


def test_multi_process_postgres_daily_cap_is_enforced_at_the_provider_boundary() -> None:
    resource_ids = [f"pg-daily-{index}" for index in range(10)]
    _reset_runtime_state()
    for resource_id in resource_ids:
        _seed_exact_resource(resource_id)
    _set_controls(daily_cap=5, monthly_cap=1_000)

    ctx = get_context("spawn")
    barrier = ctx.Barrier(len(resource_ids))
    queue = ctx.Queue()
    processes = [ctx.Process(target=_multi_process_worker, args=(resource_id, queue, barrier)) for resource_id in resource_ids]
    for process in processes:
        process.start()
    for process in processes:
        process.join()

    results = [queue.get(timeout=10) for _ in processes]
    assert all("error" not in result for result in results), f"Unexpected worker results: {results}"
    assert all(result["dialect"] == "postgresql" for result in results)
    assert sum(int(result["invoked"]) for result in results) == 5
    with app.app_context():
        assert _counter_usage("retail_external_call:serpapi_walmart", "day") == 5


def test_monthly_cap_limits_concurrent_allowed_calls() -> None:
    resource_ids = [f"monthly-{index}" for index in range(6)]
    provider_calls, allowed, blocked, persisted = _threaded_concurrent_resolution(
        resource_ids,
        daily_cap=1_000,
        monthly_cap=2,
    )
    assert provider_calls == 2
    assert allowed == 2
    assert blocked == 4
    assert persisted == 2


def test_global_retail_cap_limits_serpapi_invocations() -> None:
    resource_ids = [f"retail-{index}" for index in range(8)]
    provider_calls, allowed, blocked, persisted = _threaded_concurrent_resolution(
        resource_ids,
        daily_cap=10,
        monthly_cap=1_000,
        retail_cap=3,
    )
    assert provider_calls == 3
    assert allowed == 3
    assert blocked == 5
    assert persisted == 3


def test_provider_failure_consumes_the_reservation_and_blocks_followup_calls() -> None:
    _reset_runtime_state()
    first_id = "failure-first"
    second_id = "failure-second"
    _seed_exact_resource(first_id)
    _seed_exact_resource(second_id)
    _set_controls(daily_cap=1, monthly_cap=1_000)

    provider = _CountingProvider(delay_seconds=0.01, fail_first=True)
    first = _resolve_resource(first_id, provider)
    second = _resolve_resource(second_id, provider)

    assert provider.detail_calls == 1
    assert first["degraded_reason"] == "provider_failed_last_known"
    assert second["degraded_reason"] == "blocked_by_limit"
    assert _counter_usage("retail_external_call:serpapi_walmart", "day") == 1


def test_zero_call_controls_remain_zero() -> None:
    _reset_runtime_state()
    _set_controls(daily_cap=5, monthly_cap=5, retail_cap=5, live_refresh=False, serpapi_enabled=False)
    _seed_exact_resource("zero-fresh")
    _seed_exact_resource("zero-recent", hours_old=1)
    provider = _CountingProvider(delay_seconds=0.01)

    with app.app_context():
        fresh = retail_resolution_service.resolve_exact(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            retailer_product_id="zero-fresh",
            provider=provider,
            owner_scope="shared",
            explicit_live_refresh=False,
        )
        recent = retail_resolution_service.resolve_exact(
            retailer="walmart",
            store=VERIFIED_WALMART_STORE,
            retailer_product_id="zero-recent",
            provider=provider,
            owner_scope="shared",
            explicit_live_refresh=False,
        )

    assert provider.detail_calls == 0
    assert fresh["external_call"] is False
    assert recent["external_call"] is False
