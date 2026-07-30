"""
Kroger OAuth2 Authenticator — secure token management with auto-refresh.
=======================================================================

Standalone module: reads ``KROGER_CLIENT_ID`` and ``KROGER_CLIENT_SECRET``
from the environment (loaded via ``python-dotenv`` in ``app.py``), obtains
a bearer token via the OAuth2 client_credentials grant, and caches it in
memory with automatic refresh before expiry.

Usage
-----
    from services.kroger_api import KrogerAuth

    auth = KrogerAuth()
    token = auth.get_access_token()          # returns str or None
    auth2 = KrogerAuth()                     # shares same singleton token

Or as a one-shot convenience:
    from services.kroger_api import get_kroger_token

    token = get_kroger_token()               # returns str or None
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import requests

KROGER_TOKEN_URL = "https://api.kroger.com/v1/connect/oauth2/token"
DEFAULT_SCOPE = "product.compact"
TOKEN_SAFETY_MARGIN_S = 60  # refresh this many seconds before actual expiry
DEFAULT_TIMEOUT_S = 10

# User-Agent header sent with all Kroger API requests
_USER_AGENT = "Rung/1.0 (finance-assistant; +https://github.com/rung-finance)"

LOGGER = logging.getLogger("kroger_api")


class _TokenStore:
    """Module-level singleton that holds the cached token across imports.

    Using a class with module-level instance avoids global-mutability
    lint warnings while still providing a single in-memory cache shared
    by ``services.store_api``, ``scripts/ingest_store_prices.py``, and
    any other caller in the same process.
    """

    def __init__(self) -> None:
        self._token: Optional[Dict[str, Any]] = None

    def get(self) -> Optional[Dict[str, Any]]:
        return self._token

    def set(self, token: Dict[str, Any]) -> None:
        self._token = token

    def invalidate(self) -> None:
        self._token = None


_token_store = _TokenStore()


def _read_credentials() -> Optional[Dict[str, str]]:
    """Read Kroger API credentials from ``os.environ``.

    Returns a dict with ``client_id`` and ``client_secret`` on success,
    or ``None`` with a logged warning if either is missing.
    """
    cid = os.environ.get("KROGER_CLIENT_ID", "").strip()
    csec = os.environ.get("KROGER_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        LOGGER.warning(
            "KROGER_CLIENT_ID / KROGER_CLIENT_SECRET not set in environment. "
            "Create a .env file with these variables or export them manually."
        )
        return None
    return {"client_id": cid, "client_secret": csec}


def _fetch_token(client_id: str, client_secret: str) -> Optional[Dict[str, Any]]:
    """POST to the Kroger OAuth2 token endpoint.

    Returns the parsed JSON response dict (augmented with an
    ``expires_at`` datetime field) on success, or ``None`` on failure.
    """
    try:
        resp = requests.post(
            KROGER_TOKEN_URL,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials", "scope": DEFAULT_SCOPE},
            headers={"User-Agent": _USER_AGENT},
            timeout=DEFAULT_TIMEOUT_S,
        )
        resp.raise_for_status()
    except Exception as exc:
        LOGGER.error("Kroger token request failed: %s", exc)
        return None

    data = resp.json()
    if "access_token" not in data:
        LOGGER.error("Kroger token response missing access_token: %s", data)
        return None

    expires_in = int(data.get("expires_in", 1800))
    data["expires_at"] = datetime.utcnow() + timedelta(
        seconds=max(TOKEN_SAFETY_MARGIN_S, expires_in - TOKEN_SAFETY_MARGIN_S)
    )
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class KrogerAuth:
    """Kroger OAuth2 authenticator with in-memory token caching.

    All instances share the same module-level ``_TokenStore`` so
    credentials are fetched at most once per process lifetime (plus any
    number of automatic refreshes when the cached token expires).

    Usage::

        auth = KrogerAuth()
        token = auth.get_access_token()   # "Bearer eyJ..."

        # The token is automatically refreshed when it nears expiry.
    """

    def __init__(self) -> None:
        self._creds: Optional[Dict[str, str]] = None

    def get_access_token(self) -> Optional[str]:
        """Return a valid bearer token string, or ``None`` if unavailable.

        On first call (or after the cached token expires) this method
        reads credentials from ``os.environ`` and makes a single POST to
        the Kroger OAuth2 endpoint. Subsequent calls return the cached
        token until it is within 60 seconds of expiry, at which point a
        fresh token is fetched automatically.
        """
        cached = _token_store.get()
        now = datetime.utcnow()

        # If we have a cached token that's still fresh, return it directly.
        if cached and cached.get("expires_at", now) > now:
            return cached["access_token"]

        # Need a fresh token.  Read credentials on first use.
        if self._creds is None:
            self._creds = _read_credentials()
        if self._creds is None:
            return None

        token_data = _fetch_token(self._creds["client_id"], self._creds["client_secret"])
        if token_data is None:
            return None

        _token_store.set(token_data)
        return token_data["access_token"]


# Convenience singleton instance so callers that just want a quick token
# don't need to instantiate KrogerAuth themselves.
_singleton_auth = KrogerAuth()


def get_kroger_token() -> Optional[str]:
    """One-shot convenience: return a valid Kroger bearer token.

    Equivalent to ``KrogerAuth().get_access_token()`` but uses a shared
    module-level instance so repeated calls benefit from caching.

    Example::

        from services.kroger_api import get_kroger_token

        token = get_kroger_token()
        if token:
            headers = {\"Authorization\": f\"Bearer {token}\"}
    """
    return _singleton_auth.get_access_token()


# Downstream modules that need the raw credential values (e.g. to
# construct their own KrogerClient) can use this function.
def get_kroger_credentials() -> Optional[Dict[str, str]]:
    """Return ``{client_id, client_secret}`` from the environment.

    Returns ``None`` if either variable is unset.
    """
    return _read_credentials()


# ---------------------------------------------------------------------------
# Nearest-store locator
# ---------------------------------------------------------------------------

def find_nearest_kroger(
    zip_code: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    radius_miles: int = 100,
) -> Optional[Dict[str, str]]:
    """Query the Kroger Locations API for the nearest store to *zip_code*.

    Tries coordinates first (most precise), then falls back to ZIP search.
    Returns a dict with ``location_id``, ``store_name``, ``address``,
    and ``chain`` on success, or ``None`` if no store is found.

    Example return::

        {
            "location_id": "61500116",
            "store_name": "Gerbes - Eldon",
            "address": "105 E North St, Eldon, MO 65026",
            "chain": "GERBES",
        }
    """
    token = get_kroger_token()
    if not token:
        return None

    params: Dict[str, Any] = {
        "filter.limit": 5,
        "filter.radiusInMiles": radius_miles,
    }

    # Prefer coordinate-based search (most accurate)
    if latitude is not None and longitude is not None:
        params["filter.latLong.near"] = f"{latitude},{longitude}"
    else:
        params["filter.zipCode.near"] = zip_code

    try:
        resp = requests.get(
            "https://api.kroger.com/v1/locations",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            params=params,
            timeout=DEFAULT_TIMEOUT_S,
        )
        resp.raise_for_status()
    except Exception as exc:
        LOGGER.warning("Kroger location search failed: %s", exc)
        return None

    body = resp.json()
    locations = body.get("data", [])
    if not locations:
        LOGGER.info("No Kroger locations found near %s", zip_code)
        return None

    # Pick the first result (closest match)
    loc = locations[0]
    loc_id = loc.get("locationId", "")
    chain = loc.get("chain", "Kroger")
    store_name = loc.get("name", "").strip()
    addr = loc.get("address", {})
    addr_str = (
        f"{addr.get('addressLine1', '')}, "
        f"{addr.get('city', '')}, {addr.get('state', '')} {addr.get('zipCode', '')}"
    ).strip(", ")

    LOGGER.info(
        "Found nearest store: %s (ID: %s) — %s",
        store_name or f"{chain} store",
        loc_id,
        addr_str,
    )

    return {
        "location_id": loc_id,
        "store_name": store_name or f"{chain.title()} - {addr.get('city', 'Unknown')}",
        "address": addr_str,
        "chain": chain,
        "chain_display": "Gerbes" if chain.upper() == "GERBES" else chain.title(),
    }
