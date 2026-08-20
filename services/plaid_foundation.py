from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

import requests
from cryptography.fernet import Fernet

from extensions import db
from models import Account, PlaidAccount, PlaidItem, PlaidTransaction
from services.household_context import household_id as current_household_id
from services.usage_meter import estimate_usage_cost, record_usage_event


class PlaidFoundationError(RuntimeError):
    status_code = 400
    code = "plaid_error"

    def __init__(self, message: str, *, code: Optional[str] = None, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class PlaidConfigError(PlaidFoundationError):
    status_code = 503
    code = "plaid_not_configured"


class PlaidApiError(PlaidFoundationError):
    status_code = 502
    code = "plaid_upstream_error"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _money_to_cents(value: Any) -> int:
    try:
        dec = Decimal(str(value)).copy_abs()
    except Exception:
        dec = Decimal("0")
    return int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _normalize_direction(amount: Any) -> tuple[str, int]:
    try:
        dec = Decimal(str(amount))
    except Exception:
        dec = Decimal("0")
    cents = _money_to_cents(dec)
    if dec >= 0:
        return "outflow", -cents
    return "inflow", cents


def _plaid_host_for_env(env_name: str) -> str:
    env = (env_name or "sandbox").strip().lower()
    if env == "production":
        return "https://production.plaid.com"
    if env == "development":
        return "https://development.plaid.com"
    return "https://sandbox.plaid.com"


@dataclass
class PlaidRuntimeConfig:
    client_id: str
    secret: str
    env: str


class PlaidHttpClient:
    def __init__(self, cfg: PlaidRuntimeConfig) -> None:
        self._cfg = cfg
        self._base_url = _plaid_host_for_env(cfg.env)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        operation = {
            "/link/token/create": "link_token_create",
            "/item/public_token/exchange": "public_token_exchange",
            "/item/get": "item_get",
            "/accounts/get": "accounts_get",
            "/transactions/sync": "transactions_sync",
        }.get(path, "request")
        body = {
            "client_id": self._cfg.client_id,
            "secret": self._cfg.secret,
        }
        body.update(payload)
        try:
            response = requests.post(
                f"{self._base_url}{path}",
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=20,
            )
        except Exception as exc:
            cost = estimate_usage_cost(
                category="plaid",
                provider="plaid",
                operation=operation,
                request_count=1,
            )
            record_usage_event(
                category="plaid",
                provider="plaid",
                operation=operation,
                success=False,
                external_call=True,
                request_count=1,
                estimated_cost_micros=cost.get("estimated_cost_micros"),
                cost_status=cost.get("cost_status"),
                cost_rate_key=cost.get("cost_rate_key"),
                metadata={"error": type(exc).__name__},
            )
            raise PlaidApiError(f"Plaid request failed: {exc}", code="plaid_network_error") from exc

        if response.status_code >= 400:
            cost = estimate_usage_cost(
                category="plaid",
                provider="plaid",
                operation=operation,
                request_count=1,
            )
            record_usage_event(
                category="plaid",
                provider="plaid",
                operation=operation,
                success=False,
                external_call=True,
                request_count=1,
                estimated_cost_micros=cost.get("estimated_cost_micros"),
                cost_status=cost.get("cost_status"),
                cost_rate_key=cost.get("cost_rate_key"),
                metadata={"status_code": int(response.status_code)},
            )
            try:
                err = response.json()
            except Exception:
                err = {}
            message = str(err.get("error_message") or "Plaid request failed.")
            raise PlaidApiError(
                f"Plaid API error ({response.status_code}): {message}",
                code="plaid_api_error",
                status_code=502,
            )

        try:
            payload_json = response.json()
        except Exception as exc:
            cost = estimate_usage_cost(
                category="plaid",
                provider="plaid",
                operation=operation,
                request_count=1,
            )
            record_usage_event(
                category="plaid",
                provider="plaid",
                operation=operation,
                success=False,
                external_call=True,
                request_count=1,
                estimated_cost_micros=cost.get("estimated_cost_micros"),
                cost_status=cost.get("cost_status"),
                cost_rate_key=cost.get("cost_rate_key"),
                metadata={"error": "invalid_json"},
            )
            raise PlaidApiError("Plaid returned invalid JSON.", code="plaid_invalid_json") from exc

        cost = estimate_usage_cost(
            category="plaid",
            provider="plaid",
            operation=operation,
            request_count=1,
        )
        request_id = str(payload_json.get("request_id") or "").strip() if isinstance(payload_json, dict) else ""
        record_usage_event(
            category="plaid",
            provider="plaid",
            operation=operation,
            success=True,
            external_call=True,
            request_count=1,
            estimated_cost_micros=cost.get("estimated_cost_micros"),
            cost_status=cost.get("cost_status"),
            cost_rate_key=cost.get("cost_rate_key"),
            request_id=request_id or None,
            metadata={"status_code": int(response.status_code)},
        )
        return payload_json

    def create_link_token(self, user_scope: str) -> dict[str, Any]:
        return self._post(
            "/link/token/create",
            {
                "client_name": "Rung",
                "country_codes": ["US"],
                "language": "en",
                "products": ["transactions"],
                "user": {"client_user_id": user_scope or "anonymous"},
            },
        )

    def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        return self._post("/item/public_token/exchange", {"public_token": public_token})

    def get_item(self, access_token: str) -> dict[str, Any]:
        return self._post("/item/get", {"access_token": access_token})

    def get_accounts(self, access_token: str) -> dict[str, Any]:
        return self._post("/accounts/get", {"access_token": access_token})

    def transactions_sync(self, access_token: str, cursor: Optional[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {"access_token": access_token}
        if cursor:
            payload["cursor"] = cursor
        return self._post("/transactions/sync", payload)


def _get_runtime_config() -> PlaidRuntimeConfig:
    client_id = (os.environ.get("PLAID_CLIENT_ID") or "").strip()
    secret = (os.environ.get("PLAID_SECRET") or "").strip()
    env = (os.environ.get("PLAID_ENV") or "sandbox").strip()
    if not client_id or not secret:
        raise PlaidConfigError(
            "Plaid is not configured. Set PLAID_CLIENT_ID and PLAID_SECRET on the server.",
            code="plaid_not_configured",
            status_code=503,
        )
    return PlaidRuntimeConfig(client_id=client_id, secret=secret, env=env)


def get_plaid_http_client() -> PlaidHttpClient:
    return PlaidHttpClient(_get_runtime_config())


def _derive_fernet_key(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    raw_key = (os.environ.get("PLAID_TOKEN_ENCRYPTION_KEY") or "").strip()
    if raw_key:
        try:
            key_bytes = raw_key.encode("utf-8")
            Fernet(key_bytes)
            return Fernet(key_bytes)
        except Exception as exc:
            raise PlaidConfigError(
                "PLAID_TOKEN_ENCRYPTION_KEY is invalid. It must be a valid Fernet key.",
                code="plaid_bad_encryption_key",
                status_code=503,
            ) from exc

    if os.environ.get("FLASK_ENV") == "testing" or os.environ.get("PYTEST_CURRENT_TEST"):
        return Fernet(_derive_fernet_key("rung-test-only-plaid-token-key"))

    raise PlaidConfigError(
        "Server is missing PLAID_TOKEN_ENCRYPTION_KEY for secure Plaid token storage.",
        code="plaid_missing_encryption_key",
        status_code=503,
    )


def encrypt_plaid_access_token(token: str) -> str:
    return _get_fernet().encrypt((token or "").encode("utf-8")).decode("utf-8")


def decrypt_plaid_access_token(token_encrypted: str) -> str:
    try:
        return _get_fernet().decrypt((token_encrypted or "").encode("utf-8")).decode("utf-8")
    except Exception as exc:
        raise PlaidConfigError(
            "Stored Plaid access token could not be decrypted. Check encryption key configuration.",
            code="plaid_token_decrypt_failed",
            status_code=503,
        ) from exc


def _upsert_plaid_accounts(
    *,
    owner_scope: str,
    plaid_item: PlaidItem,
    accounts: list[dict[str, Any]],
    rung_account_id: Optional[int],
) -> list[dict[str, Any]]:
    hid = current_household_id()
    active_ids: set[str] = set()
    mapped_account = Account.query.filter_by(household_id=hid, id=rung_account_id).first() if rung_account_id else None
    default_account = mapped_account or Account.query.filter_by(household_id=hid).order_by(Account.id.asc()).first()

    summaries: list[dict[str, Any]] = []
    for row in accounts:
        plaid_account_id = str(row.get("account_id") or "").strip()
        if not plaid_account_id:
            continue
        active_ids.add(plaid_account_id)

        existing = PlaidAccount.query.filter_by(household_id=hid, plaid_account_id=plaid_account_id).first()
        if existing is None:
            existing = PlaidAccount(
                household_id=hid,
                owner_scope=owner_scope,
                plaid_item_id=plaid_item.id,
                plaid_account_id=plaid_account_id,
            )

        existing.owner_scope = owner_scope
        existing.plaid_item_id = plaid_item.id
        existing.name = str(row.get("name") or "").strip()
        existing.official_name = str(row.get("official_name") or "").strip() or None
        existing.mask = str(row.get("mask") or "").strip() or None
        existing.account_type = str(row.get("type") or "").strip() or None
        existing.account_subtype = str(row.get("subtype") or "").strip() or None
        existing.is_active = True

        if existing.rung_account_id is None and default_account is not None:
            existing.rung_account_id = default_account.id

        db.session.add(existing)
        summaries.append(existing.to_summary())

    stale = PlaidAccount.query.filter_by(household_id=hid, owner_scope=owner_scope, plaid_item_id=plaid_item.id, is_active=True).all()
    for row in stale:
        if row.plaid_account_id not in active_ids:
            row.is_active = False
            db.session.add(row)

    return summaries


def create_link_token(user_scope: str) -> dict[str, Any]:
    client = get_plaid_http_client()
    payload = client.create_link_token(user_scope=user_scope)
    return {
        "link_token": payload.get("link_token"),
        "expiration": payload.get("expiration"),
        "request_id": payload.get("request_id"),
    }


def exchange_public_token_and_persist(
    *,
    owner_scope: str,
    public_token: str,
    rung_account_id: Optional[int] = None,
) -> dict[str, Any]:
    hid = current_household_id()
    client = get_plaid_http_client()
    exchange = client.exchange_public_token(public_token)

    plaid_item_id = str(exchange.get("item_id") or "").strip()
    access_token = str(exchange.get("access_token") or "").strip()
    if not plaid_item_id or not access_token:
        raise PlaidApiError("Plaid exchange response was missing required fields.", code="plaid_exchange_invalid")

    item_details = client.get_item(access_token)
    institution_id = None
    institution_name = None
    item_payload = item_details.get("item") or {}
    institution_id = str(item_payload.get("institution_id") or "").strip() or None
    if institution_id:
        institution_name = institution_id

    row = PlaidItem.query.filter_by(household_id=hid, owner_scope=owner_scope, plaid_item_id=plaid_item_id).first()
    if row is None:
        row = PlaidItem(household_id=hid, owner_scope=owner_scope, plaid_item_id=plaid_item_id)

    row.access_token_encrypted = encrypt_plaid_access_token(access_token)
    row.institution_id = institution_id
    row.institution_name = institution_name
    row.connection_status = "connected"
    row.last_error_code = None
    row.last_error_message = None
    db.session.add(row)
    db.session.flush()

    accounts_payload = client.get_accounts(access_token)
    account_summaries = _upsert_plaid_accounts(
        owner_scope=owner_scope,
        plaid_item=row,
        accounts=(accounts_payload.get("accounts") or []),
        rung_account_id=rung_account_id,
    )

    db.session.commit()
    return {
        "item": row.to_summary(),
        "accounts": account_summaries,
    }


def _upsert_transaction_row(*, plaid_item: PlaidItem, tx_payload: dict[str, Any], mode: str, counters: dict[str, int]) -> None:
    plaid_tx_id = str(tx_payload.get("transaction_id") or "").strip()
    if not plaid_tx_id:
        return

    row = PlaidTransaction.query.filter_by(household_id=plaid_item.household_id, plaid_transaction_id=plaid_tx_id).first()
    is_new = row is None
    if row is None:
        row = PlaidTransaction(household_id=plaid_item.household_id, plaid_transaction_id=plaid_tx_id)

    amount = tx_payload.get("amount")
    direction, signed_cents = _normalize_direction(amount)

    row.owner_scope = plaid_item.owner_scope
    row.plaid_item_id = plaid_item.id
    row.plaid_account_id = str(tx_payload.get("account_id") or "").strip() or row.plaid_account_id
    row.pending_transaction_id = str(tx_payload.get("pending_transaction_id") or "").strip() or None
    row.is_pending = bool(tx_payload.get("pending"))
    row.amount_cents = _money_to_cents(amount)
    row.signed_amount_cents = signed_cents
    row.direction = direction
    row.name = str(tx_payload.get("name") or "").strip() or ""
    row.merchant_name = str(tx_payload.get("merchant_name") or "").strip() or None
    row.description = str(tx_payload.get("original_description") or row.name or "").strip() or ""
    row.transaction_date = _parse_iso_date(tx_payload.get("date"))
    row.authorized_date = _parse_iso_date(tx_payload.get("authorized_date"))
    row.iso_currency_code = str(tx_payload.get("iso_currency_code") or "").strip() or None
    category = tx_payload.get("category")
    row.category_json = json.dumps(category) if category is not None else None
    row.raw_json = json.dumps(tx_payload)
    row.is_removed = False
    row.last_seen_at = _utcnow()

    if row.is_pending:
        row.pending_lifecycle_status = "pending"
        if row.replaced_by_transaction_id:
            row.is_active_event = False
        else:
            row.is_active_event = True
    else:
        row.pending_lifecycle_status = "posted"
        row.is_active_event = True

    if not row.is_pending and row.pending_transaction_id:
        pending_row = PlaidTransaction.query.filter_by(
            owner_scope=plaid_item.owner_scope,
            plaid_item_id=plaid_item.id,
            plaid_transaction_id=row.pending_transaction_id,
        ).first()
        if pending_row and pending_row.plaid_transaction_id != row.plaid_transaction_id:
            pending_row.replaced_by_transaction_id = row.plaid_transaction_id
            pending_row.is_active_event = False
            pending_row.pending_lifecycle_status = "superseded"
            db.session.add(pending_row)
        row.replaces_pending_transaction_id = row.pending_transaction_id

    db.session.add(row)

    if mode == "added":
        if is_new:
            counters["added"] += 1
        else:
            counters["modified"] += 1
    else:
        counters["modified"] += 1


def _mark_removed_transactions(*, plaid_item: PlaidItem, removed_rows: list[dict[str, Any]], counters: dict[str, int]) -> None:
    for removed in removed_rows:
        tx_id = str((removed or {}).get("transaction_id") or "").strip()
        if not tx_id:
            continue
        row = PlaidTransaction.query.filter_by(
            owner_scope=plaid_item.owner_scope,
            plaid_item_id=plaid_item.id,
            plaid_transaction_id=tx_id,
        ).first()
        if row is None:
            continue
        if not row.is_removed:
            counters["removed"] += 1
        row.is_removed = True
        row.is_active_event = False
        row.pending_lifecycle_status = "removed"
        row.last_seen_at = _utcnow()
        db.session.add(row)


def sync_plaid_transactions(*, owner_scope: str, plaid_item_id: Optional[str] = None) -> dict[str, Any]:
    hid = current_household_id()
    if plaid_item_id:
        item = PlaidItem.query.filter_by(household_id=hid, owner_scope=owner_scope, plaid_item_id=plaid_item_id).first()
    else:
        item = PlaidItem.query.filter_by(household_id=hid, owner_scope=owner_scope, connection_status="connected").order_by(PlaidItem.id.asc()).first()

    if item is None:
        raise PlaidFoundationError(
            "No connected Plaid Item was found for this user.",
            code="plaid_item_not_found",
            status_code=404,
        )

    access_token = decrypt_plaid_access_token(item.access_token_encrypted)
    client = get_plaid_http_client()

    cursor = item.sync_cursor or None
    next_cursor = cursor
    has_more = True
    page_count = 0
    counters = {"added": 0, "modified": 0, "removed": 0}

    try:
        while has_more:
            page_count += 1
            if page_count > 100:
                raise PlaidApiError("Plaid sync pagination exceeded safety limit.", code="plaid_sync_pagination_limit")
            payload = client.transactions_sync(access_token=access_token, cursor=next_cursor)

            for row in payload.get("added") or []:
                _upsert_transaction_row(plaid_item=item, tx_payload=row, mode="added", counters=counters)
            for row in payload.get("modified") or []:
                _upsert_transaction_row(plaid_item=item, tx_payload=row, mode="modified", counters=counters)
            _mark_removed_transactions(
                plaid_item=item,
                removed_rows=payload.get("removed") or [],
                counters=counters,
            )

            next_cursor = str(payload.get("next_cursor") or next_cursor or "").strip() or None
            has_more = bool(payload.get("has_more"))

        item.sync_cursor = next_cursor
        item.last_sync_at = _utcnow()
        item.connection_status = "connected"
        item.last_error_code = None
        item.last_error_message = None
        db.session.add(item)
        db.session.commit()
    except PlaidFoundationError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        raise PlaidApiError(f"Plaid sync failed: {exc}", code="plaid_sync_failed") from exc

    active_count = PlaidTransaction.query.filter_by(
        household_id=hid,
        owner_scope=owner_scope,
        plaid_item_id=item.id,
        is_removed=False,
        is_active_event=True,
    ).count()

    return {
        "item": item.to_summary(),
        "stats": {
            "added": counters["added"],
            "modified": counters["modified"],
            "removed": counters["removed"],
            "active_events": active_count,
            "pages": page_count,
        },
    }


def get_plaid_connection_status(owner_scope: str) -> dict[str, Any]:
    hid = current_household_id()
    items = PlaidItem.query.filter_by(household_id=hid, owner_scope=owner_scope).order_by(PlaidItem.id.asc()).all()
    item_summaries = [row.to_summary() for row in items]

    item_ids = [row.id for row in items]
    accounts: list[dict[str, Any]] = []
    if item_ids:
        rows = PlaidAccount.query.filter(
            PlaidAccount.household_id == hid,
            PlaidAccount.plaid_item_id.in_(item_ids),
        ).order_by(PlaidAccount.id.asc()).all()
        accounts = [row.to_summary() for row in rows]

    return {
        "connected": any(row.connection_status == "connected" for row in items),
        "items": item_summaries,
        "accounts": accounts,
    }
