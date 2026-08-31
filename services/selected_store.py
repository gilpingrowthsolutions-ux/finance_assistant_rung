"""Household-scoped canonical selected shopping store authority."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from extensions import db
from models import Account, RetailStoreIdentity, UserSetting


SELECTED_STORE_SETTING_KEY = "selected_shopping_store"
LEGACY_RETAILER_SETTING_KEY = "grocery_active_retailer"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _legacy_retailer(account: Account | None) -> str:
    name = _clean(getattr(account, "kroger_store_name", "")).lower()
    if "walmart" in name:
        return "walmart"
    if "dollar general" in name:
        return "dollar_general"
    return "kroger"


def _payload_from_identity(row: RetailStoreIdentity, stored: dict[str, Any]) -> dict[str, Any]:
    return {
        "retailer": _clean(stored.get("retailer") or row.retailer).lower(),
        "store_id": _clean(stored.get("store_id") or row.retailer_store_id),
        "name": _clean(stored.get("name") or row.store_name),
        "address": _clean(stored.get("address") or row.address),
        "city": _clean(stored.get("city") or row.city),
        "state": _clean(stored.get("state") or row.state),
        "postal_code": _clean(stored.get("postal_code") or row.postal_code),
        "latitude": stored.get("latitude") if stored.get("latitude") is not None else (float(row.latitude) if row.latitude is not None else None),
        "longitude": stored.get("longitude") if stored.get("longitude") is not None else (float(row.longitude) if row.longitude is not None else None),
        "retail_store_identity_id": row.id,
        "canonical": True,
        "selected_at": stored.get("selected_at"),
    }


def get_selected_store(household_id: int, *, account: Account | None = None) -> dict[str, Any]:
    """Resolve canonical state, falling back to legacy fields only for old data."""
    setting = UserSetting.query.filter_by(
        household_id=household_id,
        key=SELECTED_STORE_SETTING_KEY,
    ).first()
    if setting is not None:
        try:
            stored = json.loads(setting.value or "{}")
        except (TypeError, ValueError):
            stored = {}
        identity_id = stored.get("retail_store_identity_id")
        row = db.session.get(RetailStoreIdentity, identity_id) if identity_id else None
        if row is None:
            retailer = _clean(stored.get("retailer")).lower()
            store_id = _clean(stored.get("store_id"))
            if retailer and store_id:
                row = RetailStoreIdentity.query.filter_by(
                    retailer=retailer,
                    retailer_store_id=store_id,
                ).first()
        if row is not None:
            return _payload_from_identity(row, stored)

    if account is None:
        account = Account.query.filter_by(household_id=household_id).first()
    return {
        "retailer": _legacy_retailer(account),
        "store_id": _clean(getattr(account, "kroger_location_id", "")),
        "name": _clean(getattr(account, "kroger_store_name", "")),
        "address": "",
        "city": "",
        "state": "",
        "postal_code": _clean(getattr(account, "zip_code", "")),
        "latitude": None,
        "longitude": None,
        "retail_store_identity_id": None,
        "canonical": False,
        "selected_at": None,
    }


def select_store(
    household_id: int,
    *,
    retailer: str,
    store_id: str,
    store_name: str,
    address: str = "",
    city: str = "",
    state: str = "",
    postal_code: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    account: Account | None = None,
) -> dict[str, Any]:
    """Persist one exact store and update legacy compatibility mirrors atomically."""
    retailer = _clean(retailer).lower()
    store_id = _clean(store_id)
    store_name = _clean(store_name) or retailer.replace("_", " ").title()
    if not retailer or not store_id:
        raise ValueError("retailer and store_id are required")

    row = RetailStoreIdentity.query.filter_by(
        retailer=retailer,
        retailer_store_id=store_id,
    ).first()
    values = {
        "store_name": store_name,
        "address": _clean(address) or None,
        "city": _clean(city) or None,
        "state": _clean(state) or None,
        "postal_code": _clean(postal_code) or None,
        "latitude": latitude,
        "longitude": longitude,
        "updated_at": datetime.now(timezone.utc),
    }
    if row is None:
        row = RetailStoreIdentity(
            retailer=retailer,
            retailer_store_id=store_id,
            **values,
        )
        db.session.add(row)
        db.session.flush()
    else:
        for key, value in values.items():
            if value is not None or key == "updated_at":
                setattr(row, key, value)

    selected_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "retail_store_identity_id": row.id,
        "retailer": retailer,
        "store_id": store_id,
        "name": store_name,
        "address": _clean(address),
        "city": _clean(city),
        "state": _clean(state),
        "postal_code": _clean(postal_code),
        "latitude": latitude,
        "longitude": longitude,
        "selected_at": selected_at,
    }
    setting = UserSetting.query.filter_by(
        household_id=household_id,
        key=SELECTED_STORE_SETTING_KEY,
    ).first()
    if setting is None:
        setting = UserSetting(household_id=household_id, key=SELECTED_STORE_SETTING_KEY)
        db.session.add(setting)
    setting.value = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    setting.updated_at = datetime.now(timezone.utc)

    retailer_setting = UserSetting.query.filter_by(
        household_id=household_id,
        key=LEGACY_RETAILER_SETTING_KEY,
    ).first()
    if retailer_setting is None:
        retailer_setting = UserSetting(household_id=household_id, key=LEGACY_RETAILER_SETTING_KEY)
        db.session.add(retailer_setting)
    retailer_setting.value = retailer
    retailer_setting.updated_at = datetime.now(timezone.utc)

    if account is None:
        account = Account.query.filter_by(household_id=household_id).first()
    if account is not None:
        account.kroger_location_id = store_id
        account.kroger_store_name = store_name

    db.session.flush()
    return _payload_from_identity(row, payload)


def ensure_store_identity(*, retailer: str, store_id: str, store_name: str, address: str = "", city: str = "", state: str = "", postal_code: str = "") -> RetailStoreIdentity:
    """Register a discovered physical store without selecting it for anyone."""
    retailer, store_id = _clean(retailer).lower(), _clean(store_id)
    if not retailer or not store_id:
        raise ValueError("retailer and store_id are required")
    row = RetailStoreIdentity.query.filter_by(retailer=retailer, retailer_store_id=store_id).first()
    if row is None:
        row = RetailStoreIdentity(retailer=retailer, retailer_store_id=store_id, store_name=_clean(store_name) or retailer.title(), address=_clean(address) or None, city=_clean(city) or None, state=_clean(state) or None, postal_code=_clean(postal_code) or None)
        db.session.add(row); db.session.flush()
    return row
