from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("RUNG_DB_PATH", f"/tmp/rung_gate7_cfg_{uuid.uuid4().hex}.db")

import app as appmod  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RUNG_DB_PATH", raising=False)
    monkeypatch.delenv("RUNG_ALLOW_HOUSEHOLD_HEADER_OVERRIDE", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    monkeypatch.setenv("PLAID_ENABLED", "0")
    monkeypatch.setenv("PLAID_CLIENT_ID", "")
    monkeypatch.setenv("PLAID_SECRET", "")
    appmod.app.testing = False
    appmod.app.debug = False


def test_beta_mode_requires_database_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNG_ENV", "beta")
    monkeypatch.setenv("RUNG_DB_PATH", "/tmp/should_not_be_allowed.db")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    appmod.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////tmp/should_not_be_allowed.db"
    with pytest.raises(RuntimeError, match="DATABASE_URL must be configured"):
        appmod._validate_startup_configuration()


def test_beta_mode_requires_postgresql(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNG_ENV", "beta")
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/beta.db")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    appmod.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////tmp/beta.db"
    with pytest.raises(RuntimeError, match="PostgreSQL is required"):
        appmod._validate_startup_configuration()


def test_beta_mode_rejects_weak_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNG_ENV", "beta")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("SECRET_KEY", "changeme")
    appmod.app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://user:pass@localhost:5432/db"
    with pytest.raises(RuntimeError, match="secure SECRET_KEY"):
        appmod._validate_startup_configuration()


def test_beta_mode_rejects_debug(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNG_ENV", "beta")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("FLASK_DEBUG", "1")
    appmod.app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://user:pass@localhost:5432/db"
    with pytest.raises(RuntimeError, match="Debug mode must be disabled"):
        appmod._validate_startup_configuration()


def test_non_production_allows_sqlite(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNG_ENV", "development")
    appmod.app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////tmp/dev.db"
    appmod._validate_startup_configuration()
