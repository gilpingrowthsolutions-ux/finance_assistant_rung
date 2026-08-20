from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import time
from typing import Any, Optional

from flask import has_request_context, request
from sqlalchemy import func, text
from sqlalchemy.exc import OperationalError

from extensions import db
from models import UsageEvent, UsageLimitCounter, UserSetting
from services.household_context import household_id as current_household_id

USAGE_RATES_SETTING_KEY = "usage_rates_v1"
USAGE_CONTROLS_SETTING_KEY = "usage_controls_v1"
SERPAPI_FALLBACK_ENABLED_KEY = "serpapi_fallback_enabled"
_MICRO = Decimal("1000000")

_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "prompt",
    "api_key",
    "key",
    "credential",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_micros(amount_usd: Decimal) -> int:
    return int((amount_usd * _MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _micros_to_dollars(micros: int) -> float:
    return float((Decimal(int(micros or 0)) / _MICRO).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _day_start(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(ts: datetime) -> datetime:
    return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _json_load_setting(key: str, default: dict[str, Any]) -> dict[str, Any]:
    row = UserSetting.query.filter_by(household_id=current_household_id(), key=key).first()
    if row is None or not str(row.value or "").strip():
        return json.loads(json.dumps(default))
    try:
        payload = json.loads(row.value)
    except (TypeError, ValueError):
        return json.loads(json.dumps(default))
    if not isinstance(payload, dict):
        return json.loads(json.dumps(default))
    merged = json.loads(json.dumps(default))
    _deep_merge_dict(merged, payload)
    return merged


def _json_upsert_setting(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    hid = current_household_id()
    row = UserSetting.query.filter_by(household_id=hid, key=key).first()
    serialized = json.dumps(payload)
    now = _utcnow()
    if row is None:
        db.session.add(UserSetting(household_id=hid, key=key, value=serialized, updated_at=now))
    else:
        row.value = serialized
        row.updated_at = now
    db.session.commit()
    return payload


def _deep_merge_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge_dict(target[key], value)
            continue
        target[key] = value


def default_usage_rates() -> dict[str, Any]:
    return {
        "llm": {
            "default_input_per_1k_usd": None,
            "default_output_per_1k_usd": None,
            "models": {},
        },
        "retail": {
            "walmart_serpapi_product_search_usd": None,
            "walmart_serpapi_product_detail_usd": None,
            "kroger_api_product_search_usd": None,
            "kroger_api_product_detail_usd": None,
            "rapidapi_search_usd": None,
        },
        "plaid": {
            "link_token_create_usd": None,
            "public_token_exchange_usd": None,
            "item_get_usd": None,
            "accounts_get_usd": None,
            "transactions_sync_usd": None,
        },
    }


def default_usage_controls() -> dict[str, Any]:
    return {
        "kill_switches": {
            "llm_enabled": True,
            "retail_live_refresh_enabled": True,
            "plaid_sync_enabled": True,
            "serpapi_fallback_enabled": False,
        },
        "global_limits": {
            "daily_cost_micros": 5_000_000,
            "monthly_cost_micros": 100_000_000,
        },
        "provider_limits": {
            "llm_calls_per_day": 500,
            "retail_external_calls_per_day": 500,
            "plaid_sync_calls_per_day": 200,
            "serpapi_calls_per_day": 120,
            "serpapi_calls_per_month": 1200,
        },
    }


def get_usage_rates() -> dict[str, Any]:
    return _json_load_setting(USAGE_RATES_SETTING_KEY, default_usage_rates())


def set_usage_rates(payload: dict[str, Any]) -> dict[str, Any]:
    current = get_usage_rates()
    if isinstance(payload, dict):
        _deep_merge_dict(current, payload)
    return _json_upsert_setting(USAGE_RATES_SETTING_KEY, current)


def get_usage_controls() -> dict[str, Any]:
    return _json_load_setting(USAGE_CONTROLS_SETTING_KEY, default_usage_controls())


def set_usage_controls(payload: dict[str, Any]) -> dict[str, Any]:
    current = get_usage_controls()
    if isinstance(payload, dict):
        _deep_merge_dict(current, payload)
    return _json_upsert_setting(USAGE_CONTROLS_SETTING_KEY, current)


def _coerce_scope(owner_scope: Optional[str]) -> str:
    if owner_scope and str(owner_scope).strip():
        return str(owner_scope).strip()
    if has_request_context():
        return str(request.headers.get("X-User-Id") or "anonymous").strip() or "anonymous"
    return "anonymous"


def _sanitize_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        k = str(key or "").strip()
        if not k:
            continue
        lowered = k.lower()
        if any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            as_text = str(value)
            if len(as_text) > 300:
                clean[k] = as_text[:300]
            else:
                clean[k] = value
            continue
        if isinstance(value, dict):
            clean[k] = _sanitize_metadata(value)
            continue
        if isinstance(value, list):
            items = []
            for item in value[:20]:
                if isinstance(item, (str, int, float, bool)) or item is None:
                    items.append(item)
                elif isinstance(item, dict):
                    items.append(_sanitize_metadata(item))
            clean[k] = items
    return clean


def estimate_usage_cost(
    *,
    category: str,
    provider: str,
    operation: str,
    request_count: int = 1,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> dict[str, Any]:
    rates = get_usage_rates()
    count = max(1, int(request_count or 1))

    if category == "llm":
        llm_rates = rates.get("llm") or {}
        model_key = f"{str(llm_provider or provider or '').strip().lower()}/{str(llm_model or '').strip().lower()}".strip("/")
        model_rates = (llm_rates.get("models") or {}).get(model_key, {}) if model_key else {}
        input_rate = _as_decimal(model_rates.get("input_per_1k_usd"))
        output_rate = _as_decimal(model_rates.get("output_per_1k_usd"))
        if input_rate is None:
            input_rate = _as_decimal(llm_rates.get("default_input_per_1k_usd"))
        if output_rate is None:
            output_rate = _as_decimal(llm_rates.get("default_output_per_1k_usd"))
        if input_rate is None and output_rate is None:
            return {"estimated_cost_micros": None, "cost_status": "unconfigured", "cost_rate_key": "llm.unconfigured"}
        if input_tokens is None and output_tokens is None:
            return {"estimated_cost_micros": None, "cost_status": "unknown", "cost_rate_key": "llm.missing_tokens"}
        in_tokens = max(0, int(input_tokens or 0))
        out_tokens = max(0, int(output_tokens or 0))
        total = Decimal("0")
        if input_rate is not None:
            total += (Decimal(in_tokens) / Decimal("1000")) * input_rate
        if output_rate is not None:
            total += (Decimal(out_tokens) / Decimal("1000")) * output_rate
        return {
            "estimated_cost_micros": _to_micros(total),
            "cost_status": "known",
            "cost_rate_key": f"llm.models.{model_key}" if model_key else "llm.default",
        }

    rate_key = None
    raw_rate = None
    if category == "retail_provider":
        retail = rates.get("retail") or {}
        key = f"{provider}_{operation}_usd"
        raw_rate = retail.get(key)
        rate_key = f"retail.{key}"
    elif category == "plaid":
        plaid = rates.get("plaid") or {}
        key = f"{operation}_usd"
        raw_rate = plaid.get(key)
        rate_key = f"plaid.{key}"

    rate = _as_decimal(raw_rate)
    if rate is None:
        return {"estimated_cost_micros": None, "cost_status": "unconfigured", "cost_rate_key": rate_key or "unconfigured"}
    total = Decimal(count) * rate
    return {"estimated_cost_micros": _to_micros(total), "cost_status": "known", "cost_rate_key": rate_key}


def _sum_known_cost_micros(owner_scope: str, start: datetime, end: datetime) -> int:
    value = (
        db.session.query(func.coalesce(func.sum(UsageEvent.estimated_cost_micros), 0))
        .filter(UsageEvent.owner_scope == owner_scope)
        .filter(UsageEvent.created_at >= start)
        .filter(UsageEvent.created_at < end)
        .filter(UsageEvent.cost_status == "known")
        .scalar()
    )
    return int(value or 0)


def _count_requests(
    owner_scope: str,
    start: datetime,
    end: datetime,
    *,
    category: Optional[str] = None,
    provider: Optional[str] = None,
    operation: Optional[str] = None,
    external_call: Optional[bool] = None,
) -> int:
    query = (
        db.session.query(func.coalesce(func.sum(UsageEvent.request_count), 0))
        .filter(UsageEvent.owner_scope == owner_scope)
        .filter(UsageEvent.created_at >= start)
        .filter(UsageEvent.created_at < end)
    )
    if category is not None:
        query = query.filter(UsageEvent.category == category)
    if provider is not None:
        query = query.filter(UsageEvent.provider == provider)
    if operation is not None:
        query = query.filter(UsageEvent.operation == operation)
    if external_call is not None:
        query = query.filter(UsageEvent.external_call.is_(external_call))
    return int(query.scalar() or 0)


def _limit_key(operation: str, provider: Optional[str]) -> str:
    return f"{operation}:{provider}" if provider else operation


def _reserve_usage_slot(
    owner_scope: str,
    *,
    operation: str,
    provider: Optional[str],
    period_type: str,
    limit: int,
    now: datetime,
) -> tuple[bool, int]:
    if limit <= 0:
        return False, 0

    start, end = (_day_start(now), now + timedelta(seconds=1)) if period_type == "day" else (_month_start(now), now + timedelta(seconds=1))
    limit_key = _limit_key(operation, provider)
    seeded_usage = _count_requests(
        owner_scope,
        start,
        end,
        category="retail_provider",
        provider=provider,
        external_call=True,
    )

    for _attempt in range(50):
        try:
            household_id = current_household_id()
            insert_params = {
                "household_id": household_id,
                "limit_key": limit_key,
                "period_type": period_type,
                "period_start": start,
                "seeded_usage": seeded_usage,
                "now": now,
            }
            update_params = {**insert_params, "limit": int(limit)}
            lookup_params = {
                "household_id": household_id,
                "limit_key": limit_key,
                "period_type": period_type,
                "period_start": start,
            }
            if db.engine.dialect.name == "postgresql":
                db.session.execute(
                    text(
                        """
                        INSERT INTO usage_limit_counter (
                            household_id, limit_key, period_type, period_start, used_count, updated_at
                        ) VALUES (
                            :household_id, :limit_key, :period_type, :period_start, :seeded_usage, :now
                        ) ON CONFLICT (household_id, limit_key, period_type, period_start) DO NOTHING
                        """
                    ),
                    insert_params,
                )
            else:
                db.session.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO usage_limit_counter (
                            household_id, limit_key, period_type, period_start, used_count, updated_at
                        ) VALUES (
                            :household_id, :limit_key, :period_type, :period_start, :seeded_usage, :now
                        )
                        """
                    ),
                    insert_params,
                )

            update_result = db.session.execute(
                text(
                    """
                    UPDATE usage_limit_counter
                    SET used_count = used_count + 1, updated_at = :now
                    WHERE household_id = :household_id
                      AND limit_key = :limit_key
                      AND period_type = :period_type
                      AND period_start = :period_start
                      AND used_count < :limit
                    """
                ),
                update_params,
            )
            current_used = db.session.execute(
                text(
                    """
                    SELECT used_count
                    FROM usage_limit_counter
                    WHERE household_id = :household_id
                      AND limit_key = :limit_key
                      AND period_type = :period_type
                      AND period_start = :period_start
                    """
                ),
                lookup_params,
            ).scalar()
            if int(update_result.rowcount or 0) > 0:
                return True, int(current_used or 0)
            return False, int(current_used or 0)
        except OperationalError:
            db.session.rollback()
            time.sleep(0.01)
            continue

    raise RuntimeError("usage limit reservation timed out")


def check_optional_operation(
    owner_scope: Optional[str],
    operation_kind: str,
    *,
    projected_cost_micros: Optional[int] = None,
    enforce_retail_daily_limit: bool = True,
) -> dict[str, Any]:
    scope = _coerce_scope(owner_scope)
    controls = get_usage_controls()
    switches = controls.get("kill_switches") or {}
    provider_limits = controls.get("provider_limits") or {}
    global_limits = controls.get("global_limits") or {}

    now = _utcnow()
    day_start = _day_start(now)
    month_start = _month_start(now)

    if operation_kind == "llm_call" and not bool(switches.get("llm_enabled", True)):
        return {"allowed": False, "code": "llm_disabled", "message": "Copilot advanced model calls are currently unavailable."}
    if operation_kind == "retail_external_call" and not bool(switches.get("retail_live_refresh_enabled", True)):
        return {"allowed": False, "code": "retail_live_disabled", "message": "Live retail refresh is currently unavailable."}
    if operation_kind == "plaid_sync_call" and not bool(switches.get("plaid_sync_enabled", True)):
        return {"allowed": False, "code": "plaid_sync_disabled", "message": "Plaid sync is currently unavailable."}

    if operation_kind == "llm_call":
        cap = provider_limits.get("llm_calls_per_day")
        if cap is not None:
            used = _count_requests(scope, day_start, now + timedelta(seconds=1), category="llm", external_call=True)
            if used >= int(cap):
                return {"allowed": False, "code": "llm_daily_limit", "message": "Copilot advanced model calls are temporarily limited."}

    if operation_kind == "retail_external_call" and enforce_retail_daily_limit:
        cap = provider_limits.get("retail_external_calls_per_day")
        if cap is not None:
            used = _count_requests(scope, day_start, now + timedelta(seconds=1), category="retail_provider", external_call=True)
            if used >= int(cap):
                return {"allowed": False, "code": "retail_daily_limit", "message": "Live retail refresh is temporarily limited."}

    if operation_kind == "plaid_sync_call":
        cap = provider_limits.get("plaid_sync_calls_per_day")
        if cap is not None:
            used = _count_requests(scope, day_start, now + timedelta(seconds=1), category="plaid", operation="transactions_sync", external_call=True)
            if used >= int(cap):
                return {"allowed": False, "code": "plaid_sync_daily_limit", "message": "Plaid sync is temporarily limited."}

    if projected_cost_micros is not None and int(projected_cost_micros) > 0:
        daily_limit = global_limits.get("daily_cost_micros")
        monthly_limit = global_limits.get("monthly_cost_micros")
        if daily_limit is not None:
            daily_used = _sum_known_cost_micros(scope, day_start, now + timedelta(seconds=1))
            if daily_used + int(projected_cost_micros) > int(daily_limit):
                return {"allowed": False, "code": "daily_cost_limit", "message": "Optional external calls are temporarily limited."}
        if monthly_limit is not None:
            monthly_used = _sum_known_cost_micros(scope, month_start, now + timedelta(seconds=1))
            if monthly_used + int(projected_cost_micros) > int(monthly_limit):
                return {"allowed": False, "code": "monthly_cost_limit", "message": "Optional external calls are temporarily limited."}

    return {"allowed": True, "code": "ok", "message": "ok"}


def check_retail_provider_operation(
    owner_scope: Optional[str],
    *,
    provider: str,
    projected_cost_micros: Optional[int] = None,
    require_explicit_allowance: bool = False,
) -> dict[str, Any]:
    scope = _coerce_scope(owner_scope)
    normalized_provider = str(provider or "").strip().lower()
    base_gate = check_optional_operation(
        scope,
        "retail_external_call",
        projected_cost_micros=projected_cost_micros,
        enforce_retail_daily_limit=False,
    )
    if not base_gate.get("allowed", False):
        return {
            "allowed": False,
            "code": str(base_gate.get("code") or "retail_blocked"),
            "message": str(base_gate.get("message") or "Live retail refresh is temporarily unavailable."),
        }

    controls = get_usage_controls()
    switches = controls.get("kill_switches") or {}
    provider_limits = controls.get("provider_limits") or {}
    now = _utcnow()
    day_start = _day_start(now)
    month_start = _month_start(now)
    window_end = now + timedelta(seconds=1)

    if normalized_provider == "serpapi_walmart":
        fallback_enabled = bool(switches.get(SERPAPI_FALLBACK_ENABLED_KEY, False))
        if require_explicit_allowance and not fallback_enabled:
            return {
                "allowed": False,
                "code": "blocked_unconfigured",
                "message": "Walmart fallback is not configured.",
            }
        daily_cap = provider_limits.get("serpapi_calls_per_day")
        monthly_cap = provider_limits.get("serpapi_calls_per_month")
        if require_explicit_allowance and (daily_cap is None or monthly_cap is None):
            return {
                "allowed": False,
                "code": "blocked_unconfigured",
                "message": "Walmart fallback requires explicit daily and monthly call ceilings.",
            }

        if not require_explicit_allowance and (daily_cap is None or monthly_cap is None):
            return {"allowed": True, "code": "ok", "message": "ok"}

        try:
            retail_cap = provider_limits.get("retail_external_calls_per_day")
            if retail_cap is not None:
                retail_allowed, retail_used = _reserve_usage_slot(
                    scope,
                    operation="retail_external_call",
                    provider=None,
                    period_type="day",
                    limit=int(retail_cap),
                    now=now,
                )
                if not retail_allowed:
                    raise ValueError(f"retail daily limit reached at {retail_used}.")

            daily_allowed, daily_used = _reserve_usage_slot(
                scope,
                operation="retail_external_call",
                provider=normalized_provider,
                period_type="day",
                limit=int(daily_cap),
                now=now,
            )
            if not daily_allowed:
                raise ValueError(f"Walmart fallback daily limit reached at {daily_used}.")

            monthly_allowed, monthly_used = _reserve_usage_slot(
                scope,
                operation="retail_external_call",
                provider=normalized_provider,
                period_type="month",
                limit=int(monthly_cap),
                now=now,
            )
            if not monthly_allowed:
                raise ValueError(f"Walmart fallback monthly limit reached at {monthly_used}.")

            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            message = str(exc)
            code = "blocked_by_limit"
            if "retail daily" in message:
                return {"allowed": False, "code": code, "message": "Live retail refresh is temporarily limited."}
            if "daily" in message:
                return {"allowed": False, "code": code, "message": "Walmart fallback daily limit reached."}
            return {"allowed": False, "code": code, "message": "Walmart fallback monthly limit reached."}
        except Exception:
            db.session.rollback()
            raise

        return {
            "allowed": True,
            "code": "ok",
            "message": "ok",
            "retail_cap": int(retail_cap) if retail_cap is not None else None,
            "daily_cap": int(daily_cap),
            "daily_used": int(daily_used),
            "monthly_cap": int(monthly_cap),
            "monthly_used": int(monthly_used),
        }

    return {"allowed": True, "code": "ok", "message": "ok"}


def record_usage_event(
    *,
    category: str,
    provider: str,
    operation: str,
    owner_scope: Optional[str] = None,
    success: bool = True,
    external_call: bool = False,
    request_count: int = 1,
    cache_status: Optional[str] = None,
    force_refresh: bool = False,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    estimated_cost_micros: Optional[int] = None,
    cost_status: Optional[str] = None,
    cost_rate_key: Optional[str] = None,
    operation_id: Optional[str] = None,
    request_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> UsageEvent:
    scope = _coerce_scope(owner_scope)
    hid = current_household_id()
    status = str(cost_status or ("known" if estimated_cost_micros is not None else "unconfigured")).strip().lower()
    if status not in {"known", "unknown", "unconfigured"}:
        status = "unknown"
    row = UsageEvent(
        household_id=hid,
        owner_scope=scope,
        category=str(category or "unknown").strip().lower() or "unknown",
        provider=str(provider or "unknown").strip().lower() or "unknown",
        operation=str(operation or "unknown").strip().lower() or "unknown",
        success=bool(success),
        external_call=bool(external_call),
        request_count=max(1, int(request_count or 1)),
        cache_status=(str(cache_status).strip().lower() if cache_status else None),
        force_refresh=bool(force_refresh),
        llm_provider=(str(llm_provider).strip().lower() if llm_provider else None),
        llm_model=(str(llm_model).strip() if llm_model else None),
        input_tokens=(int(input_tokens) if input_tokens is not None else None),
        output_tokens=(int(output_tokens) if output_tokens is not None else None),
        estimated_cost_micros=(int(estimated_cost_micros) if estimated_cost_micros is not None else None),
        cost_status=status,
        cost_rate_key=(str(cost_rate_key).strip() if cost_rate_key else None),
        operation_id=(str(operation_id).strip() if operation_id else None),
        request_id=(str(request_id).strip() if request_id else None),
        metadata_json=json.dumps(_sanitize_metadata(metadata or {})) if metadata else None,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _aggregate_period(owner_scope: str, start: datetime, end: datetime) -> dict[str, Any]:
    rows = (
        UsageEvent.query
        .filter(UsageEvent.owner_scope == owner_scope)
        .filter(UsageEvent.created_at >= start)
        .filter(UsageEvent.created_at < end)
        .order_by(UsageEvent.created_at.asc())
        .all()
    )

    known_cost_micros = 0
    unknown_usage_count = 0
    llm_calls = 0
    retail_external_calls = 0
    plaid_calls = 0
    retail_cache_hits = 0
    retail_cache_misses = 0
    breakdown: dict[str, int] = {}

    for row in rows:
        if row.cost_status == "known" and row.estimated_cost_micros is not None:
            known_cost_micros += int(row.estimated_cost_micros)
            key = f"{row.category}:{row.provider}"
            breakdown[key] = breakdown.get(key, 0) + int(row.estimated_cost_micros)
        else:
            unknown_usage_count += int(row.request_count or 0)

        if row.category == "llm" and row.external_call:
            llm_calls += int(row.request_count or 0)
        if row.category == "retail_provider" and row.external_call:
            retail_external_calls += int(row.request_count or 0)
        if row.category == "plaid" and row.external_call:
            plaid_calls += int(row.request_count or 0)
        if row.category == "retail_cache":
            if row.cache_status == "hit":
                retail_cache_hits += int(row.request_count or 0)
            elif row.cache_status == "miss":
                retail_cache_misses += int(row.request_count or 0)

    cache_total = retail_cache_hits + retail_cache_misses
    cache_hit_rate = (float(retail_cache_hits) / float(cache_total)) if cache_total else None

    return {
        "known_estimated_cost_micros": known_cost_micros,
        "known_estimated_cost_usd": _micros_to_dollars(known_cost_micros),
        "unknown_unpriced_usage_count": int(unknown_usage_count),
        "llm_calls": int(llm_calls),
        "retail_external_calls": int(retail_external_calls),
        "plaid_calls": int(plaid_calls),
        "retail_cache_hits": int(retail_cache_hits),
        "retail_cache_misses": int(retail_cache_misses),
        "retail_cache_hit_rate": cache_hit_rate,
        "known_cost_breakdown": {k: _micros_to_dollars(v) for k, v in sorted(breakdown.items())},
        "event_count": len(rows),
    }


def summarize_usage(owner_scope: Optional[str]) -> dict[str, Any]:
    scope = _coerce_scope(owner_scope)
    now = _utcnow()
    day_start = _day_start(now)
    month_start = _month_start(now)
    day_end = day_start + timedelta(days=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)

    today = _aggregate_period(scope, day_start, day_end)
    month = _aggregate_period(scope, month_start, next_month)

    controls = get_usage_controls()
    rates = get_usage_rates()

    return {
        "owner_scope": scope,
        "today": today,
        "month": month,
        "controls": controls,
        "rates": rates,
    }
