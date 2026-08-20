from __future__ import annotations

import hashlib
import hmac
import os
from threading import RLock
from dataclasses import dataclass
from typing import Optional

from flask import g, has_request_context, request
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Household
from services.auth_session import auth_required_mode, get_current_principal, header_override_allowed

DEFAULT_LEGACY_SCOPE = "anonymous"
_HOUSEHOLD_CREATE_LOCK = RLock()


class HouseholdResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HouseholdContext:
    household_id: int
    household_public_id: str
    scope_key: str


def _normalize_scope_key(value: Optional[str]) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_LEGACY_SCOPE


def _request_scope_key() -> str:
    # Compatibility default: in unauthenticated mode we stay on the legacy
    # singleton scope rather than trusting client-supplied household selectors.
    return DEFAULT_LEGACY_SCOPE


def _trusted_request_public_id() -> str:
    if not has_request_context():
        return ""

    if auth_required_mode():
        if str(request.headers.get("X-Household-Id") or "").strip() or str(request.headers.get("X-Household-Signature") or "").strip():
            raise HouseholdResolutionError("Household header override is disabled in production mode.")
        return ""

    if not header_override_allowed():
        return ""

    public_id = str(request.headers.get("X-Household-Id") or "").strip()
    if not public_id:
        return ""

    shared_secret = str(os.getenv("RUNG_HOUSEHOLD_CONTEXT_SECRET") or "").strip()
    if not shared_secret:
        # Production-safe default: do not honor client household overrides
        # unless a trusted binding/signature mechanism is configured.
        return ""

    signature = str(request.headers.get("X-Household-Signature") or "").strip().lower()
    if not signature:
        raise HouseholdResolutionError("Missing household context signature.")

    expected = hmac.new(
        shared_secret.encode("utf-8"),
        public_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HouseholdResolutionError("Invalid household context signature.")

    return public_id


def _lookup_by_public_id(public_id: str) -> Optional[Household]:
    pid = str(public_id or "").strip()
    if not pid:
        return None
    return Household.query.filter_by(public_id=pid).first()


def _lookup_by_scope_key(scope_key: str) -> Optional[Household]:
    return Household.query.filter_by(legacy_scope_key=scope_key).first()


def _create_household(scope_key: str) -> Household:
    with _HOUSEHOLD_CREATE_LOCK:
        # Fresh browser startup resolves the anonymous household from several
        # concurrent read requests. Double-check inside the lock so only one
        # canonical household is created for the unique scope key.
        existing = _lookup_by_scope_key(scope_key)
        if existing is not None:
            return existing
        row = Household(legacy_scope_key=scope_key)
        db.session.add(row)
        try:
            db.session.commit()
            return row
        except IntegrityError:
            # A different worker won the database uniqueness race. Restore a
            # usable session and load that canonical row; never invent/merge.
            db.session.rollback()
            existing = _lookup_by_scope_key(scope_key)
            if existing is None:
                raise
            return existing


def resolve_household_context(
    *,
    require_request_scope: bool = False,
    create_if_missing: bool = True,
) -> HouseholdContext:
    cached = getattr(g, "_household_context", None) if has_request_context() else None
    if cached is not None:
        return cached

    principal = get_current_principal() if has_request_context() else None
    if principal is not None:
        row = Household.query.filter_by(id=int(principal.household_id)).first()
        if row is None:
            raise HouseholdResolutionError("Authenticated household membership is invalid.")
        ctx = HouseholdContext(
            household_id=int(row.id),
            household_public_id=str(row.public_id),
            scope_key=str(row.legacy_scope_key or DEFAULT_LEGACY_SCOPE),
        )
        if has_request_context():
            g._household_context = ctx
        return ctx

    if auth_required_mode() and has_request_context():
        raise HouseholdResolutionError("Authentication required.")

    explicit_public_id = _trusted_request_public_id() if has_request_context() else ""

    row = _lookup_by_public_id(explicit_public_id) if explicit_public_id else None

    if row is None:
        if has_request_context():
            scope_key = _request_scope_key()
            if require_request_scope and not scope_key:
                raise HouseholdResolutionError("Household context is required.")
        else:
            scope_key = DEFAULT_LEGACY_SCOPE
        row = _lookup_by_scope_key(scope_key)
        if row is None and create_if_missing:
            row = _create_household(scope_key)

    if row is None:
        raise HouseholdResolutionError("Unable to resolve household context.")

    ctx = HouseholdContext(
        household_id=int(row.id),
        household_public_id=str(row.public_id),
        scope_key=str(row.legacy_scope_key or DEFAULT_LEGACY_SCOPE),
    )
    if has_request_context():
        g._household_context = ctx
    return ctx


def household_id() -> int:
    return resolve_household_context().household_id


def household_scope_key() -> str:
    return resolve_household_context().scope_key


def ensure_legacy_household() -> Household:
    row = _lookup_by_scope_key(DEFAULT_LEGACY_SCOPE)
    if row is None:
        row = _create_household(DEFAULT_LEGACY_SCOPE)
    return row
