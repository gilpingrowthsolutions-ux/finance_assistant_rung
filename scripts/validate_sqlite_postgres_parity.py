#!/usr/bin/env python3
"""Parity validator for disposable SQLite -> PostgreSQL migration acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine.url import make_url

TABLES = [
    "household",
    "account",
    "bill",
    "expense_transactions",
    "grocery_items",
    "shopping_trip_completion",
    "user_settings",
    "user_preferences",
    "retail_product_preference",
    "retail_product_substitution",
    "plaid_item",
    "plaid_account",
    "plaid_transaction",
    "transaction_reconciliation",
    "action_audit",
    "usage_event",
    "beta_feedback",
    "meal_plan",
    "pantry_inventory",
    "recipe",
    "recipe_ingredient",
    "retail_product_cache",
    "store_price_cache",
    "rapid_price_cache",
    "household_shopping_defaults",
    "brand_preference",
]

LEGACY_DEFAULT_PUBLIC_ID = "a4c0e258-94f4-4447-8b8e-31bd9cbf8b45"
LEGACY_DEFAULT_SCOPE_KEY = "legacy-sqlite-import"


def _abort(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def _normalize_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _logical_digest(
    *,
    source_engine,
    target_engine,
    table_name: str,
    source_meta: MetaData,
    target_meta: MetaData,
    legacy_single_household: bool,
) -> tuple[int, int, str, str]:
    if table_name not in target_meta.tables:
        raise RuntimeError(f"target missing table: {table_name}")

    target_table = target_meta.tables[table_name]
    source_table = source_meta.tables.get(table_name)

    target_rows: list[dict[str, Any]] = []
    target_order_cols = list(target_table.primary_key.columns) or list(target_table.columns)
    with target_engine.connect() as conn:
        for row in conn.execute(select(target_table).order_by(*target_order_cols)).mappings().all():
            target_rows.append(dict(row))

    if source_table is None:
        if legacy_single_household and table_name == "household":
            source_rows = [{
                "id": 1,
                "public_id": LEGACY_DEFAULT_PUBLIC_ID,
                "legacy_scope_key": LEGACY_DEFAULT_SCOPE_KEY,
            }]
        else:
            source_rows = []
    else:
        source_order_cols = list(source_table.primary_key.columns) or list(source_table.columns)
        with source_engine.connect() as conn:
            source_rows = [dict(r) for r in conn.execute(select(source_table).order_by(*source_order_cols)).mappings().all()]

    source_cols = set(source_table.columns.keys()) if source_table is not None else set()
    target_cols = set(target_table.columns.keys())
    shared_cols = sorted(source_cols & target_cols)

    # Legacy single-household source does not store household_id; compare on shared columns only.
    if legacy_single_household and "household_id" in shared_cols and "household_id" not in source_cols:
        shared_cols.remove("household_id")

    # Legacy user settings/preferences had no synthetic integer id column.
    if table_name in {"user_settings", "user_preferences"} and "id" in shared_cols and "id" not in source_cols:
        shared_cols.remove("id")
    if table_name in {"user_settings", "user_preferences"} and "updated_at" in shared_cols:
        shared_cols.remove("updated_at")

    def _project(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        projected = []
        for row in rows:
            projected.append({k: _normalize_value(row.get(k)) for k in shared_cols})
        projected.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return projected

    source_projected = _project(source_rows)
    target_projected = _project(target_rows)

    source_payload = json.dumps(source_projected, sort_keys=True, separators=(",", ":"))
    target_payload = json.dumps(target_projected, sort_keys=True, separators=(",", ":"))

    return (
        len(source_rows),
        len(target_rows),
        hashlib.sha256(source_payload.encode("utf-8")).hexdigest(),
        hashlib.sha256(target_payload.encode("utf-8")).hexdigest(),
    )


def _sum_by_column(engine_url: str, table_name: str, column_name: str) -> Decimal:
    engine = create_engine(engine_url, future=True)
    with engine.connect() as conn:
        raw = conn.execute(text(f'SELECT COALESCE(SUM("{column_name}"), 0) FROM "{table_name}"')).scalar()
    return Decimal(str(raw or 0)).quantize(Decimal("0.01"))


def _pair_set(engine_url: str, query: str) -> set[tuple[Any, ...]]:
    engine = create_engine(engine_url, future=True)
    with engine.connect() as conn:
        rows = conn.execute(text(query)).all()
    return {tuple(row) for row in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate parity between source SQLite and target PostgreSQL.")
    parser.add_argument("--source-sqlite", required=True, help="Path to source SQLite copy.")
    parser.add_argument("--target-url", required=True, help="Target PostgreSQL SQLAlchemy URL.")
    args = parser.parse_args()

    source_path = str(Path(args.source_sqlite).expanduser().resolve())
    if not Path(source_path).exists():
        _abort(f"Source SQLite file does not exist: {source_path}")

    source_url = f"sqlite:///{source_path}"
    target_url = args.target_url.strip()

    if make_url(source_url).drivername != "sqlite":
        _abort("Source must be sqlite.")
    if not make_url(target_url).drivername.startswith("postgresql"):
        _abort("Target must be postgresql.")

    result: dict[str, Any] = {
        "row_counts": {},
        "digests": {},
        "financial_values": {},
        "relationships": {},
        "identity": {},
        "mismatches": [],
    }

    source_engine = create_engine(source_url, future=True)
    target_engine = create_engine(target_url, future=True)
    source_meta = MetaData()
    target_meta = MetaData()
    source_meta.reflect(bind=source_engine, resolve_fks=False)
    target_meta.reflect(bind=target_engine, resolve_fks=False)
    legacy_single_household = "household" not in source_meta.tables

    for table_name in TABLES:
        src_count, dst_count, src_digest, dst_digest = _logical_digest(
            source_engine=source_engine,
            target_engine=target_engine,
            table_name=table_name,
            source_meta=source_meta,
            target_meta=target_meta,
            legacy_single_household=legacy_single_household,
        )
        result["row_counts"][table_name] = {"source": src_count, "target": dst_count}
        result["digests"][table_name] = {"source": src_digest, "target": dst_digest}
        if src_count != dst_count:
            result["mismatches"].append(f"row_count:{table_name}")
        if src_digest != dst_digest:
            result["mismatches"].append(f"digest:{table_name}")

    financial_checks = [
        ("account", "checking_balance"),
        ("account", "vault_balance"),
        ("account", "expected_paycheck"),
        ("bill", "amount"),
        ("expense_transactions", "amount"),
    ]
    for table_name, col in financial_checks:
        src_sum = _sum_by_column(source_url, table_name, col)
        dst_sum = _sum_by_column(target_url, table_name, col)
        key = f"{table_name}.{col}"
        result["financial_values"][key] = {"source": str(src_sum), "target": str(dst_sum)}
        if src_sum != dst_sum:
            result["mismatches"].append(f"financial_sum:{key}")

    relationship_queries = {
        "expense_to_account": "SELECT id, local_account_id FROM expense_transactions ORDER BY id",
        "trip_to_transaction": "SELECT id, transaction_id FROM shopping_trip_completion ORDER BY id",
        "plaid_item_to_account": "SELECT id, plaid_item_id, rung_account_id FROM plaid_account ORDER BY id",
        "recon_to_manual": "SELECT id, manual_transaction_id, plaid_transaction_id FROM transaction_reconciliation ORDER BY id",
        "sub_to_pref": "SELECT id, preferred_preference_id FROM retail_product_substitution ORDER BY id",
    }
    for key, query in relationship_queries.items():
        try:
            src_set = _pair_set(source_url, query)
        except Exception:
            src_set = set()
        dst_set = _pair_set(target_url, query)
        result["relationships"][key] = {"source_count": len(src_set), "target_count": len(dst_set)}
        if src_set != dst_set:
            result["mismatches"].append(f"relationship:{key}")

    identity_queries = {
        "action_operation_ids": "SELECT operation_id FROM action_audit WHERE operation_id IS NOT NULL ORDER BY operation_id",
        "trip_tokens": "SELECT trip_token FROM shopping_trip_completion ORDER BY trip_token",
        "trip_operation_ids": "SELECT operation_id FROM shopping_trip_completion ORDER BY operation_id",
        "plaid_tx_ids": "SELECT plaid_transaction_id FROM plaid_transaction ORDER BY plaid_transaction_id",
        "recon_pairs": "SELECT manual_transaction_id, plaid_transaction_id FROM transaction_reconciliation ORDER BY manual_transaction_id, plaid_transaction_id",
    }
    for key, query in identity_queries.items():
        try:
            src_set = _pair_set(source_url, query)
        except Exception:
            src_set = set()
        dst_set = _pair_set(target_url, query)
        result["identity"][key] = {"source_count": len(src_set), "target_count": len(dst_set)}
        if src_set != dst_set:
            result["mismatches"].append(f"identity:{key}")

    print(json.dumps(result, indent=2, sort_keys=True))
    if result["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
