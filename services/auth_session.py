from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from flask import g, has_request_context, request, session

from extensions import db
from models import HouseholdMembership, LoginThrottle, User

SESSION_USER_ID_KEY = "auth_user_id"
SESSION_AUTH_VERSION_KEY = "auth_version"

_LOGIN_WINDOW = timedelta(minutes=15)
_LOGIN_BLOCK = timedelta(minutes=15)
_LOGIN_FAIL_LIMIT = 5


class AuthRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: int
    household_id: int
    role: str
    email: str
    auth_version: int


def _now_like(reference: datetime | None = None) -> datetime:
    """Return now() matching the tz-awareness style of stored timestamp columns.

    PostgreSQL `timestamp without time zone` returns naive datetimes. Using an
    aware UTC clock against those naive values can skew window arithmetic and
    cause throttle counters to reset unexpectedly.
    """
    if reference is not None and reference.tzinfo is not None:
        return datetime.now(reference.tzinfo)
    return datetime.now()


def runtime_env() -> str:
    return str(os.environ.get("RUNG_ENV") or "").strip().lower()


def auth_required_mode() -> bool:
    return runtime_env() in {"production", "beta"}


def header_override_allowed() -> bool:
    if auth_required_mode():
        return False
    raw = str(os.environ.get("RUNG_ALLOW_HOUSEHOLD_HEADER_OVERRIDE") or "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return True


def _load_active_membership(user_id: int) -> HouseholdMembership | None:
    return (
        HouseholdMembership.query
        .filter_by(user_id=int(user_id), active=True)
        .order_by(HouseholdMembership.id.asc())
        .first()
    )


def get_current_principal() -> AuthPrincipal | None:
    if not has_request_context():
        return None
    cached = getattr(g, "_auth_principal", None)
    if cached is not None:
        return cached

    user_id = session.get(SESSION_USER_ID_KEY)
    version = session.get(SESSION_AUTH_VERSION_KEY)
    if user_id is None or version is None:
        return None

    user = User.query.filter_by(id=int(user_id)).first()
    if user is None or not bool(user.active):
        clear_session()
        return None

    if int(user.auth_version or 0) != int(version):
        clear_session()
        return None

    membership = _load_active_membership(int(user.id))
    if membership is None:
        clear_session()
        return None

    principal = AuthPrincipal(
        user_id=int(user.id),
        household_id=int(membership.household_id),
        role=str(membership.role or "member"),
        email=str(user.email),
        auth_version=int(user.auth_version or 0),
    )
    g._auth_principal = principal
    return principal


def require_principal() -> AuthPrincipal:
    principal = get_current_principal()
    if principal is None:
        raise AuthRequiredError("Authentication required.")
    return principal


def establish_session(user: User) -> None:
    if not has_request_context():
        return
    session.clear()
    session[SESSION_USER_ID_KEY] = int(user.id)
    session[SESSION_AUTH_VERSION_KEY] = int(user.auth_version or 0)


def clear_session() -> None:
    if not has_request_context():
        return
    session.pop(SESSION_USER_ID_KEY, None)
    session.pop(SESSION_AUTH_VERSION_KEY, None)


def _subject_key(identity: str, ip_addr: str) -> str:
    ident = str(identity or "").strip().lower() or "unknown"
    ip = str(ip_addr or "").strip() or "unknown"
    return f"{ident}|{ip}"


def _client_ip() -> str:
    if not has_request_context():
        return ""
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return str(request.remote_addr or "").strip()


def _get_or_create_throttle_row(subject_key: str) -> LoginThrottle:
    row = LoginThrottle.query.filter_by(subject_key=subject_key).first()
    if row is not None:
        return row
    now = _now_like()
    row = LoginThrottle(
        subject_key=subject_key,
        failed_count=0,
        window_started_at=now,
        blocked_until=None,
        updated_at=now,
    )
    db.session.add(row)
    db.session.flush()
    return row


def login_is_blocked(identity: str) -> tuple[bool, int]:
    subject_key = _subject_key(identity, _client_ip())
    row = LoginThrottle.query.filter_by(subject_key=subject_key).first()
    if row is None or row.blocked_until is None:
        return False, 0
    now = _now_like(row.blocked_until)
    blocked_until = row.blocked_until
    if blocked_until <= now:
        row.blocked_until = None
        row.failed_count = 0
        row.window_started_at = now
        row.updated_at = now
        db.session.add(row)
        db.session.commit()
        return False, 0
    retry = int((blocked_until - now).total_seconds())
    return True, max(1, retry)


def record_login_failure(identity: str) -> None:
    subject_key = _subject_key(identity, _client_ip())
    row = _get_or_create_throttle_row(subject_key)
    now = _now_like(row.window_started_at)

    started = row.window_started_at or now

    if now - started > _LOGIN_WINDOW:
        row.window_started_at = now
        row.failed_count = 0

    row.failed_count = int(row.failed_count or 0) + 1
    row.updated_at = now
    if row.failed_count >= _LOGIN_FAIL_LIMIT:
        row.blocked_until = now + _LOGIN_BLOCK

    db.session.add(row)
    db.session.commit()


def clear_login_failures(identity: str) -> None:
    subject_key = _subject_key(identity, _client_ip())
    row = LoginThrottle.query.filter_by(subject_key=subject_key).first()
    if row is None:
        return
    db.session.delete(row)
    db.session.commit()
