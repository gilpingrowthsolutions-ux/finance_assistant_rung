"""Integration tests for BYOK (Bring Your Own Key) Groq key management."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so db.drop_all()/create_all() can never wipe rung_finance.db (which
# previously destroyed the saved Groq API key on every test run).
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app, db, UserSetting

client = app.test_client()
app.testing = True
_pass = 0
_fail = 0


def _setup():
    """Reset database to a clean state (and hide any env-var key)."""
    # app.py runs load_dotenv() at import, so GROQ_API_KEY from .env can
    # leak into os.environ and make the DB-only tests see a "configured"
    # key.  Remove it so these tests exercise the DB state in isolation.
    os.environ.pop("GROQ_API_KEY", None)
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.commit()


def _stub_validation():
    """Return a context manager that stubs live Groq validation (no network)."""
    import contextlib
    import app as appmod

    @contextlib.contextmanager
    def _cm():
        orig = appmod._validate_groq_key
        appmod._validate_groq_key = lambda k: (None, "no network in test")
        try:
            yield
        finally:
            appmod._validate_groq_key = orig

    return _cm()


def _assert_eq(a, b, msg=""):
    global _pass, _fail
    if a == b:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL {msg}: expected {b!r}, got {a!r}")


def _assert_truthy(val, msg=""):
    global _pass, _fail
    if val:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL {msg}: value is falsy")


# ---------------------------------------------------------------------------
# 1 — GET: no key configured
# ---------------------------------------------------------------------------


def test_get_no_key():
    _setup()
    resp = client.get("/api/settings/groq-key")
    _assert_eq(resp.status_code, 200)
    d = resp.get_json() or {}
    _assert_eq(d.get("configured"), False, "no key configured")
    _assert_eq(d.get("key_preview"), None, "preview is None")


# ---------------------------------------------------------------------------
# 2 — POST: invalid format
# ---------------------------------------------------------------------------


def test_post_invalid_format():
    _setup()
    resp = client.post("/api/settings/groq-key", json={"api_key": "abc123"})
    d = resp.get_json() or {}
    _assert_eq(resp.status_code, 400, "rejects non-gsk_ keys")
    _assert_truthy("gsk_" in d.get("error", ""), "error mentions gsk_ prefix")


def test_post_empty_key():
    _setup()
    resp = client.post("/api/settings/groq-key", json={"api_key": ""})
    _assert_eq(resp.status_code, 400, "empty key returns 400")


def test_post_missing_body():
    _setup()
    resp = client.post("/api/settings/groq-key", json={})
    _assert_eq(resp.status_code, 400, "missing api_key returns 400")


# ---------------------------------------------------------------------------
# 3 — POST: save valid-format key (no network validation available in test)
# ---------------------------------------------------------------------------


def test_post_save_key():
    """Save a key with valid format.

    The endpoint saves the key first, then validates best-effort; the
    validation call is stubbed so this test is hermetic (no network).
    """
    _setup()
    with _stub_validation():
        resp = client.post(
            "/api/settings/groq-key",
            json={"api_key": "gsk_test1234567890abcdef"},
        )
    d = resp.get_json() or {}
    _assert_eq(resp.status_code, 200, "save-first returns 200")
    _assert_eq(d.get("configured"), True, "key marked as configured")
    with app.app_context():
        row = UserSetting.query.get("groq_api_key")
        _assert_truthy(row is not None, "key persisted to DB")
        _assert_eq(row.value, "gsk_test1234567890abcdef")


# ---------------------------------------------------------------------------
# 4 — GET: key configured
# ---------------------------------------------------------------------------


def test_get_with_key():
    _setup()
    with app.app_context():
        db.session.add(UserSetting(key="groq_api_key", value="gsk_abcdefgh12345678"))
        db.session.commit()

    with _stub_validation():
        resp = client.get("/api/settings/groq-key")
    d = resp.get_json() or {}
    _assert_eq(d.get("configured"), True, "configured is True")
    preview = d.get("key_preview", "")
    _assert_truthy("..." in preview, f"preview is masked: {preview}")
    _assert_truthy("gsk_" in preview, "preview starts with gsk_")


# ---------------------------------------------------------------------------
# 5 — DELETE: remove key
# ---------------------------------------------------------------------------


def test_delete_key():
    _setup()
    with app.app_context():
        db.session.add(UserSetting(key="groq_api_key", value="gsk_test"))
        db.session.commit()

    resp = client.delete("/api/settings/groq-key")
    d = resp.get_json() or {}
    _assert_eq(resp.status_code, 200, "DELETE returns 200")
    _assert_eq(d.get("configured"), False, "key no longer configured")

    with app.app_context():
        row = UserSetting.query.get("groq_api_key")
        _assert_truthy(row.value == "", "key value cleared")


# ---------------------------------------------------------------------------
# 6 — parse_copilot_prompt accepts groq_api_key parameter
# ---------------------------------------------------------------------------


def test_copilot_with_explicit_key():
    """When a key is passed explicitly, _call_groq uses it (not env var)."""
    _setup()
    from services.copilot_service import parse_copilot_prompt

    # No key provided -> should use regex fallback (no env var, no explicit key)
    result = parse_copilot_prompt("add netflix 10/mo")
    _assert_eq(result.get("_fallback"), True, "no key -> regex fallback")

    # Explicit key provided (will fail 401 but that's fine — the function
    # correctly passes it through; the test just verifies it doesn't crash)
    result = parse_copilot_prompt("add netflix 10/mo", groq_api_key="gsk_fake")
    # Falls back to regex after Groq fails with 401, which is correct behavior
    _assert_truthy(isinstance(result, dict), "returns a dict with explicit key")


# =============================================================================
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"{fn.__name__} ...")
        try:
            fn()
        except Exception as exc:
            _fail += 1
            print(f"  ERROR: {exc}")
    print(f"\n{_pass} passed, {_fail} failed")
    sys.exit(1 if _fail else 0)
