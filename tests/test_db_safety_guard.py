from __future__ import annotations

import os
import pytest

from extensions import assert_safe_destructive_db_target


def test_blocks_production_uri_even_if_env_missing() -> None:
    with pytest.raises(RuntimeError):
        assert_safe_destructive_db_target(
            "sqlite:////home/ky/finance_assistant/rung_finance.db",
            None,
        )


def test_requires_explicit_rung_db_path() -> None:
    with pytest.raises(RuntimeError):
        assert_safe_destructive_db_target(
            "sqlite:////tmp/test.db",
            "",
        )


def test_allows_in_memory_test_db() -> None:
    assert_safe_destructive_db_target("sqlite:///:memory:", ":memory:")


def test_blocks_env_pointing_to_rung_finance_db() -> None:
    with pytest.raises(RuntimeError):
        assert_safe_destructive_db_target(
            "sqlite:////tmp/some_other.db",
            "/home/ky/finance_assistant/rung_finance.db",
        )


def test_allows_explicit_non_production_file_path() -> None:
    assert_safe_destructive_db_target(
        "sqlite:////tmp/rung_test_sandbox.db",
        "/tmp/rung_test_sandbox.db",
    )
