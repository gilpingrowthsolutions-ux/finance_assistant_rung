"""Regression test — the test suite must never touch the user's real data.

Bug being guarded (fixed): test_byok.py's ``_setup()`` ran ``db.drop_all()``
against the PRODUCTION ``rung_finance.db``, wiping the user's saved Groq
API key, and ``test_post_save_key`` POSTed a placeholder key through the
real endpoint which ``_write_env_var()`` then wrote into the real ``.env``.
Result: the app validated ``gsk_test1234567890abcdef`` against Groq,
Groq returned 401, and Settings showed "Key saved but rejected by Groq"
even though the user's real key was fine.

These tests pin down the two isolation guarantees:
  1. With ``RUNG_DB_PATH=:memory:`` (set by every test file), the app's
     SQLAlchemy engine must be the in-memory DB — not rung_finance.db.
  2. ``_write_env_var()`` must be a no-op while ``app.testing`` is True,
     so a test POST can never pollute the real ``.env``.

Run with:  .venv/bin/python tests/test_test_isolation.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Every Python test suite sets this before importing app; this test
# asserts the app actually honors it (so drop_all() can't hit the real DB).
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app, db  # noqa: E402
import app as appmod      # noqa: E402

app.testing = True

passed = 0
failed = 0


def _assert_eq(a, b, msg=""):
    global passed, failed
    if a == b:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {msg}: expected {b!r}, got {a!r}")


def _assert_truthy(val, msg=""):
    global passed, failed
    if val:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {msg}: value is falsy")


# ---------------------------------------------------------------------------
# 1 — The app must be pointed at the in-memory DB when RUNG_DB_PATH is set
# ---------------------------------------------------------------------------

def test_in_memory_db_uri():
    _assert_eq(
        app.config["SQLALCHEMY_DATABASE_URI"],
        "sqlite:///:memory:",
        "app uses in-memory SQLite when RUNG_DB_PATH=:memory:",
    )


def test_engine_url_is_in_memory():
    """The live engine (what drop_all/create_all actually run against)
    must resolve to the in-memory DB, never to rung_finance.db."""
    with app.app_context():
        url = str(db.engine.url)
    _assert_eq(url, "sqlite:///:memory:", f"engine URL is in-memory (got {url!r})")


def test_drop_all_does_not_touch_real_db():
    """Running the standard test teardown (drop_all + create_all) inside
    the app context must not raise and must keep the real DB untouched."""
    real_db = os.path.join(os.path.dirname(__file__), "..", "rung_finance.db")
    before = None
    if os.path.exists(real_db):
        import sqlite3
        conn = sqlite3.connect(real_db)
        try:
            before = conn.execute(
                "SELECT value FROM user_settings WHERE key='groq_api_key'"
            ).fetchall()
        except Exception:
            before = "no-table"
        conn.close()

    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.commit()

    _assert_truthy(True, "drop_all/create_all succeeded on in-memory DB")

    # The real DB must be unchanged (same key rows as before — none).
    if before is not None:
        import sqlite3
        conn = sqlite3.connect(real_db)
        try:
            after = conn.execute(
                "SELECT value FROM user_settings WHERE key='groq_api_key'"
            ).fetchall()
        except Exception:
            after = "no-table"
        conn.close()
        _assert_eq(after, before, "real rung_finance.db untouched by drop_all")


# ---------------------------------------------------------------------------
# 2 — _write_env_var must never write to the real .env during tests
# ---------------------------------------------------------------------------

def test_write_env_var_noop_while_testing():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    before = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            before = f.read()

    # This is exactly what test_byok.py used to do: POST the placeholder
    # through the endpoint, which called _write_env_var("GROQ_API_KEY", ...).
    appmod._write_env_var("GROQ_API_KEY", "gsk_test1234567890abcdef")

    after = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            after = f.read()
    _assert_eq(after, before, ".env byte-for-byte unchanged while app.testing=True")


def test_write_env_var_removal_noop_while_testing():
    """Even the DELETE path (value='') must not rewrite .env during tests."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    before = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            before = f.read()

    appmod._write_env_var("GROQ_API_KEY", "")

    after = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            after = f.read()
    _assert_eq(after, before, ".env unchanged by empty-value write while testing")


# =============================================================================
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"{fn.__name__} ...")
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ERROR: {exc}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
