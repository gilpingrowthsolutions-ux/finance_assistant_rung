from __future__ import annotations

import os
from typing import Any, Optional

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ["PLAID_TOKEN_ENCRYPTION_KEY"] = "oR4fJCv6Em2vQ2n80AFM9x5QdSXQ66j3qnP8Qf1EBz8="
os.environ.setdefault("PLAID_CLIENT_ID", "plaid-client-id-test")
os.environ.setdefault("PLAID_SECRET", "plaid-secret-test")
os.environ.setdefault("PLAID_ENV", "sandbox")

from app import app
from extensions import db
from models import Account, PlaidAccount, PlaidItem, PlaidTransaction
import services.plaid_foundation as pf
from services.household_context import household_id as current_household_id


app.testing = True
client = app.test_client()


class FakePlaidClient:
    def __init__(self) -> None:
        self.link_users: list[str] = []
        self.exchanged_tokens: list[str] = []
        self.sync_cursors: list[Optional[str]] = []
        self.sync_pages: list[dict[str, Any]] = []
        self.accounts_payload: list[dict[str, Any]] = []
        self.item_payload: dict[str, Any] = {"item": {"institution_id": "ins_1"}}

    def create_link_token(self, user_scope: str) -> dict[str, Any]:
        self.link_users.append(user_scope)
        return {
            "link_token": "link-sandbox-token",
            "expiration": "2030-01-01T00:00:00Z",
            "request_id": "req-link",
        }

    def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        self.exchanged_tokens.append(public_token)
        return {
            "access_token": "access-sandbox-token",
            "item_id": "item_sandbox_1",
            "request_id": "req-exchange",
        }

    def get_item(self, access_token: str) -> dict[str, Any]:
        return self.item_payload

    def get_accounts(self, access_token: str) -> dict[str, Any]:
        return {"accounts": list(self.accounts_payload)}

    def transactions_sync(self, access_token: str, cursor: Optional[str]) -> dict[str, Any]:
        self.sync_cursors.append(cursor)
        if self.sync_pages:
            return self.sync_pages.pop(0)
        return {
            "added": [],
            "modified": [],
            "removed": [],
            "has_more": False,
            "next_cursor": cursor,
        }


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1200.0))
        db.session.commit()


def _connect_item(monkeypatch: pytest.MonkeyPatch, fake: FakePlaidClient, *, user_id: str = "plaid-user") -> dict[str, Any]:
    monkeypatch.setattr(pf, "get_plaid_http_client", lambda: fake)
    fake.accounts_payload = [
        {
            "account_id": "acc_1",
            "name": "Plaid Checking",
            "official_name": "Primary Checking",
            "mask": "0000",
            "type": "depository",
            "subtype": "checking",
        }
    ]
    r = client.post(
        "/api/plaid/exchange-public-token",
        json={"public_token": "public-sandbox-token", "user_id": user_id},
    )
    assert r.status_code == 200
    return r.get_json() or {}


def _base_pending_tx() -> dict[str, Any]:
    return {
        "transaction_id": "tx_pending_1",
        "account_id": "acc_1",
        "pending_transaction_id": None,
        "pending": True,
        "amount": 38.0,
        "name": "DOLLAR GENERAL #123",
        "merchant_name": "Dollar General",
        "date": "2026-08-12",
        "authorized_date": "2026-08-12",
        "iso_currency_code": "USD",
    }


def _base_posted_tx() -> dict[str, Any]:
    return {
        "transaction_id": "tx_posted_1",
        "account_id": "acc_1",
        "pending_transaction_id": "tx_pending_1",
        "pending": False,
        "amount": 38.0,
        "name": "DOLLAR GENERAL #123",
        "merchant_name": "Dollar General",
        "date": "2026-08-13",
        "authorized_date": "2026-08-12",
        "iso_currency_code": "USD",
    }


def test_link_token_endpoint_uses_server_side_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    monkeypatch.setattr(pf, "get_plaid_http_client", lambda: fake)

    r = client.post("/api/plaid/link-token", json={"user_id": "abc"})
    assert r.status_code == 200
    body = r.get_json() or {}
    assert body.get("link_token") == "link-sandbox-token"
    assert fake.link_users == ["abc"]


def test_missing_plaid_configuration_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)

    r = client.post("/api/plaid/link-token", json={"user_id": "abc"})
    assert r.status_code == 503
    body = r.get_json() or {}
    assert body.get("code") == "plaid_not_configured"
    assert "Traceback" not in (body.get("error") or "")


def test_public_token_exchange_persists_item_identity_and_secure_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    body = _connect_item(monkeypatch, fake)

    with app.app_context():
        items = PlaidItem.query.all()
        assert len(items) == 1
        row = items[0]
        assert row.plaid_item_id == "item_sandbox_1"
        assert row.access_token_encrypted != "access-sandbox-token"

    assert "access_token" not in body
    assert body.get("item", {}).get("sync_cursor_present") is False


def test_repeated_item_exchange_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)
    _connect_item(monkeypatch, fake)

    with app.app_context():
        assert PlaidItem.query.count() == 1
        assert PlaidAccount.query.count() == 1


def test_plaid_accounts_persist_with_stable_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    with app.app_context():
        row = PlaidAccount.query.one()
        assert row.plaid_account_id == "acc_1"
        assert row.is_active is True


def test_account_mapping_remains_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    monkeypatch.setattr(pf, "get_plaid_http_client", lambda: fake)
    fake.accounts_payload = [
        {
            "account_id": "acc_1",
            "name": "Plaid Checking",
            "official_name": "Primary Checking",
            "mask": "0000",
            "type": "depository",
            "subtype": "checking",
        }
    ]

    r1 = client.post(
        "/api/plaid/exchange-public-token",
        json={"public_token": "public-sandbox-token", "user_id": "plaid-user", "rung_account_id": 1},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/plaid/exchange-public-token",
        json={"public_token": "public-sandbox-token", "user_id": "plaid-user", "rung_account_id": 1},
    )
    assert r2.status_code == 200

    with app.app_context():
        row = PlaidAccount.query.one()
        assert row.rung_account_id == 1


def test_initial_sync_persists_transactions_and_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    fake.sync_pages = [
        {
            "added": [_base_pending_tx()],
            "modified": [],
            "removed": [],
            "has_more": False,
            "next_cursor": "cursor_1",
        }
    ]

    r = client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"})
    assert r.status_code == 200
    body = r.get_json() or {}
    assert (body.get("stats") or {}).get("added") == 1

    with app.app_context():
        assert PlaidTransaction.query.count() == 1
        item = PlaidItem.query.one()
        assert item.sync_cursor == "cursor_1"


def test_second_sync_uses_saved_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    fake.sync_pages = [{"added": [], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_1"}]
    r1 = client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"})
    assert r1.status_code == 200

    fake.sync_pages = [{"added": [], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_2"}]
    r2 = client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"})
    assert r2.status_code == 200

    assert fake.sync_cursors[0] is None
    assert fake.sync_cursors[1] == "cursor_1"


def test_sync_pagination_runs_until_has_more_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    fake.sync_pages = [
        {"added": [_base_pending_tx()], "modified": [], "removed": [], "has_more": True, "next_cursor": "cursor_mid"},
        {"added": [_base_posted_tx()], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_done"},
    ]

    r = client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"})
    assert r.status_code == 200
    body = r.get_json() or {}
    assert (body.get("stats") or {}).get("pages") == 2


def test_repeated_identical_sync_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    page = {"added": [_base_pending_tx()], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_1"}
    fake.sync_pages = [dict(page)]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    fake.sync_pages = [dict(page)]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    with app.app_context():
        assert PlaidTransaction.query.count() == 1


def test_modified_transaction_updates_existing_record(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    fake.sync_pages = [{"added": [_base_pending_tx()], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_1"}]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    mod = _base_pending_tx()
    mod["name"] = "DOLLAR GENERAL #777"
    fake.sync_pages = [{"added": [], "modified": [mod], "removed": [], "has_more": False, "next_cursor": "cursor_2"}]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    with app.app_context():
        row = PlaidTransaction.query.filter_by(plaid_transaction_id="tx_pending_1").one()
        assert row.name == "DOLLAR GENERAL #777"


def test_removed_transaction_is_marked_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    fake.sync_pages = [{"added": [_base_pending_tx()], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_1"}]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    fake.sync_pages = [
        {
            "added": [],
            "modified": [],
            "removed": [{"transaction_id": "tx_pending_1"}],
            "has_more": False,
            "next_cursor": "cursor_2",
        }
    ]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    with app.app_context():
        row = PlaidTransaction.query.filter_by(plaid_transaction_id="tx_pending_1").one()
        assert row.is_removed is True
        assert row.is_active_event is False


def test_pending_transaction_is_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    fake.sync_pages = [{"added": [_base_pending_tx()], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_1"}]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    with app.app_context():
        row = PlaidTransaction.query.filter_by(plaid_transaction_id="tx_pending_1").one()
        assert row.is_pending is True
        assert row.pending_lifecycle_status == "pending"


def test_posted_transaction_links_pending_and_avoids_double_active_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    fake.sync_pages = [
        {
            "added": [_base_pending_tx()],
            "modified": [],
            "removed": [],
            "has_more": False,
            "next_cursor": "cursor_1",
        }
    ]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    fake.sync_pages = [
        {
            "added": [_base_posted_tx()],
            "modified": [],
            "removed": [],
            "has_more": False,
            "next_cursor": "cursor_2",
        }
    ]
    r = client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"})
    assert r.status_code == 200

    with app.app_context():
        pending = PlaidTransaction.query.filter_by(plaid_transaction_id="tx_pending_1").one()
        posted = PlaidTransaction.query.filter_by(plaid_transaction_id="tx_posted_1").one()
        assert pending.replaced_by_transaction_id == "tx_posted_1"
        assert pending.is_active_event is False
        assert posted.is_active_event is True
        active_count = PlaidTransaction.query.filter_by(is_removed=False, is_active_event=True).count()
        assert active_count == 1


def test_different_transaction_ids_remain_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    a = _base_pending_tx()
    b = dict(_base_pending_tx())
    b["transaction_id"] = "tx_pending_2"
    b["name"] = "DOLLAR GENERAL #999"

    fake.sync_pages = [{"added": [a, b], "modified": [], "removed": [], "has_more": False, "next_cursor": "cursor_1"}]
    assert client.post("/api/plaid/sync-transactions", json={"user_id": "plaid-user"}).status_code == 200

    with app.app_context():
        assert PlaidTransaction.query.count() == 2


def test_access_tokens_never_appear_in_public_status_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    _connect_item(monkeypatch, fake)

    r = client.get("/api/plaid/status", query_string={"user_id": "plaid-user"})
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "access-sandbox-token" not in text
    assert "access_token" not in text


def test_plaid_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise pf.PlaidApiError("Plaid API error (400): simulated upstream failure")

    monkeypatch.setattr(pf, "create_link_token", _boom)
    monkeypatch.setattr("app.create_link_token", _boom)

    r = client.post("/api/plaid/link-token", json={"user_id": "abc"})
    assert r.status_code == 502
    body = r.get_json() or {}
    assert body.get("code") == "plaid_upstream_error"
    assert "Traceback" not in (body.get("error") or "")


def test_sync_requires_connected_item(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    fake = FakePlaidClient()
    monkeypatch.setattr(pf, "get_plaid_http_client", lambda: fake)

    r = client.post("/api/plaid/sync-transactions", json={"user_id": "no-item-user"})
    assert r.status_code == 404
    body = r.get_json() or {}
    assert body.get("code") == "plaid_item_not_found"
