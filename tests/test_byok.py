"""Integration tests for server-managed Copilot provider settings endpoint."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app  # noqa: E402


client = app.test_client()
app.testing = True
_pass = 0
_fail = 0


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


def test_get_reports_server_managed_configuration_state():
    saved = os.environ.get("GROQ_API_KEY")
    try:
        os.environ.pop("GROQ_API_KEY", None)
        resp = client.get("/api/settings/groq-key")
        d = resp.get_json() or {}
        _assert_eq(resp.status_code, 200)
        _assert_eq(d.get("configured"), False, "no env key means not configured")
        _assert_eq(d.get("managed_by"), "server", "management mode is server")

        os.environ["GROQ_API_KEY"] = "gsk_test_server_side"
        resp2 = client.get("/api/settings/groq-key")
        d2 = resp2.get_json() or {}
        _assert_eq(resp2.status_code, 200)
        _assert_eq(d2.get("configured"), True, "env key means configured")
        _assert_eq(d2.get("managed_by"), "server", "management mode is server")
    finally:
        if saved is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = saved


def test_post_is_rejected_for_customer_managed_key():
    resp = client.post("/api/settings/groq-key", json={"api_key": "gsk_test"})
    d = resp.get_json() or {}
    _assert_eq(resp.status_code, 404)
    _assert_truthy("server-side" in (d.get("error") or ""), "error explains server-side management")


def test_delete_is_rejected_for_customer_managed_key():
    resp = client.delete("/api/settings/groq-key")
    d = resp.get_json() or {}
    _assert_eq(resp.status_code, 404)
    _assert_truthy("server-side" in (d.get("error") or ""), "error explains server-side management")


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
