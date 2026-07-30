"""Integration tests for BYOK (Bring Your Own Key) Groq key management."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app, db, UserSetting

client = app.test_client()
app.testing = True
_pass = 0
_fail = 0


def _setup():
    """Reset database to a clean state."""
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.commit()


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

    On a machine with internet access, Groq will reject this fake key
    with HTTP 401 (correct behavior). On an isolated machine, the
    network call fails and the key is saved with a warning.
    Both are valid outcomes for this test."""
    _setup()
    resp = client.post(
        "/api/settings/groq-key",
        json={"api_key": "gsk_test1234567890abcdef"},
    )
    d = resp.get_json() or {}

    # Accept either:
    #   200 = network down, key saved with warning
    #   401 = Groq rejected the fake key (correct behavior)
    if resp.status_code == 200:
        _assert_eq(d.get("configured"), True, "key marked as configured")
        with app.app_context():
            row = UserSetting.query.get("groq_api_key")
            _assert_truthy(row is not None, "key persisted to DB")
            _assert_eq(row.value, "gsk_test1234567890abcdef")
    elif resp.status_code == 401:
        _assert_truthy("Invalid" in d.get("error", ""), "401 rejects invalid key")
        # Key should NOT be saved when Groq explicitly rejects it
        with app.app_context():
            row = UserSetting.query.get("groq_api_key")
            _assert_truthy(row is None or row.value == "", "key not saved on rejection")
    else:
        _assert_eq(resp.status_code, 200, f"unexpected status {resp.status_code}")


# ---------------------------------------------------------------------------
# 4 — GET: key configured
# ---------------------------------------------------------------------------


def test_get_with_key():
    _setup()
    with app.app_context():
        db.session.add(UserSetting(key="groq_api_key", value="gsk_abcdefgh12345678"))
        db.session.commit()

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
# 6 — copilot_service._get_groq_key reads from DB
# ---------------------------------------------------------------------------


def test_get_groq_key_from_db():
    _setup()
    from services.copilot_service import _get_groq_key

    # No key saved
    key = _get_groq_key()
    _assert_eq(key, "", "no key returns empty string")

    # Save a key
    with app.app_context():
        db.session.add(UserSetting(key="groq_api_key", value="gsk_from_db"))
        db.session.commit()

    key = _get_groq_key()
    _assert_eq(key, "gsk_from_db", "reads key from DB")


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
