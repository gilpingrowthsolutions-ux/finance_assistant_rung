#!/usr/bin/env python3
"""Controlled legacy SQLite -> PostgreSQL importer for Gate 2B readiness.

Safety goals:
- never mutate production SQLite by default
- fail closed when source/target shapes are unexpected
- avoid duplicate imports unless explicitly allowed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.engine.url import make_url

PRODUCTION_SQLITE = "/home/ky/finance_assistant/rung_finance.db"
TABLE_ORDER = [
    "household",
    "recipe",
    "account",
    "bill",
    "pantry_inventory",
    "recipe_ingredient",
    "brand_preference",
    "grocery_items",
    "rapid_price_cache",
    "store_price_cache",
    "retail_product_cache",
    "retail_product_preference",
    "retail_product_substitution",
    "meal_plan",
    "expense_transactions",
    "shopping_trip_completion",
    "plaid_item",
    "plaid_account",
    "plaid_transaction",
    "transaction_reconciliation",
    "user_settings",
    "user_preferences",
    "household_shopping_defaults",
    "action_audit",
    "usage_event",
    "beta_feedback",
]

LEGACY_DEFAULT_PUBLIC_ID = "a4c0e258-94f4-4447-8b8e-31bd9cbf8b45"
LEGACY_DEFAULT_SCOPE_KEY = "legacy-sqlite-import"


def _abort(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def _parse_url(raw: str) -> URL:
    try:
        return make_url(raw)
    except Exception as exc:
        _abort(f"Invalid URL '{raw}': {exc}")


def _engine(url: str) -> Engine:
    return create_engine(url, future=True)


def _run_migrations(target_url: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = target_url
    env["FLASK_APP"] = "app.py"
    cmd = [
        "/home/ky/finance_assistant/venv/bin/python",
        "-m",
        "flask",
        "db",
        "upgrade",
    ]
    subprocess.run(cmd, cwd="/home/ky/finance_assistant", env=env, check=True)


def _table_exists(engine: Engine, table_name: str) -> bool:
    query = text("SELECT 1 FROM information_schema.tables WHERE table_name=:name")
    if engine.dialect.name == "sqlite":
        query = text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name")
    with engine.connect() as conn:
        return conn.execute(query, {"name": table_name}).first() is not None


def _reset_sequences(engine: Engine, table_name: str) -> None:
    if not engine.dialect.name.startswith("postgresql"):
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
                "COALESCE((SELECT MAX(id) FROM \"" + table_name + "\"), 1), "
                "(SELECT MAX(id) IS NOT NULL FROM \"" + table_name + "\"))"
            ),
            {"table_name": table_name},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy SQLite rows into PostgreSQL in deterministic order.")
    parser.add_argument("--source-sqlite", required=True, help="Path to source SQLite file (use a disposable copy).")
    parser.add_argument("--target-url", required=True, help="Target PostgreSQL SQLAlchemy URL.")
    parser.add_argument("--apply-migrations", action="store_true", help="Run flask db upgrade on target before import.")
    parser.add_argument("--allow-nonempty-target", action="store_true", help="Allow import into non-empty target tables.")
    parser.add_argument("--allow-production-source", action="store_true", help="Allow using production SQLite path directly.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report counts without writing rows.")
    parser.add_argument("--legacy-household-public-id", default=LEGACY_DEFAULT_PUBLIC_ID, help="Public household id for legacy single-household SQLite imports.")
    args = parser.parse_args()

    source_path = str(Path(args.source_sqlite).expanduser().resolve())
    prod_path = str(Path(PRODUCTION_SQLITE).resolve())
    if source_path == prod_path and not args.allow_production_source:
        _abort("Refusing to import directly from production SQLite path. Use a disposable copy.")

    if not Path(source_path).exists():
        _abort(f"Source SQLite file does not exist: {source_path}")

    source_url = f"sqlite:///{source_path}"
    target_url = args.target_url.strip()

    src = _parse_url(source_url)
    dst = _parse_url(target_url)
    if src.drivername != "sqlite":
        _abort("Source must be sqlite.")
    if not dst.drivername.startswith("postgresql"):
        _abort("Target must be postgresql://...")

    if args.apply_migrations:
        _run_migrations(target_url)

    source_engine = _engine(source_url)
    target_engine = _engine(target_url)

    src_meta = MetaData()
    dst_meta = MetaData()
    src_meta.reflect(bind=source_engine, resolve_fks=False)
    dst_meta.reflect(bind=target_engine, only=TABLE_ORDER, resolve_fks=False)

    source_tables = set(src_meta.tables.keys())
    legacy_single_household = "household" not in source_tables
    if legacy_single_household:
        print("INFO: source SQLite has no household table; applying legacy single-household import mapping", file=sys.stderr)

    summary: dict[str, Any] = {"source": source_path, "target_driver": dst.drivername, "tables": {}}

    legacy_household_id = 1
    legacy_household_public_id = str(args.legacy_household_public_id or LEGACY_DEFAULT_PUBLIC_ID).strip() or LEGACY_DEFAULT_PUBLIC_ID

    for table_name in TABLE_ORDER:
        if table_name not in dst_meta.tables:
            _abort(f"Target missing required table: {table_name}. Did migrations run?")

        dst_table = dst_meta.tables[table_name]

        src_rows: list[dict[str, Any]] = []
        if table_name == "household" and legacy_single_household:
            src_rows = [{
                "id": legacy_household_id,
                "public_id": legacy_household_public_id,
                "legacy_scope_key": LEGACY_DEFAULT_SCOPE_KEY,
                "created_at": datetime.now(timezone.utc),
            }]
        elif table_name in src_meta.tables:
            src_table = src_meta.tables[table_name]
            with source_engine.connect() as src_conn:
                src_rows = [dict(row) for row in src_conn.execute(select(src_table)).mappings().all()]

        with target_engine.connect() as dst_conn:
            dst_count = int(dst_conn.execute(text(f'SELECT COUNT(1) FROM "{table_name}"')).scalar() or 0)

        if dst_count > 0 and not args.allow_nonempty_target:
            _abort(
                f"Target table '{table_name}' is not empty ({dst_count} rows). "
                "Use --allow-nonempty-target only for explicitly disposable reruns."
            )

        summary["tables"][table_name] = {
            "source_rows": len(src_rows),
            "target_rows_before": dst_count,
        }

        if args.dry_run or not src_rows:
            continue

        dst_cols = {col.name for col in dst_table.columns}
        normalized_rows: list[dict[str, Any]] = []
        for idx, raw in enumerate(src_rows, start=1):
            row = dict(raw)

            # Legacy schemas were single-household and did not persist household_id.
            if legacy_single_household and "household_id" in dst_cols and "household_id" not in row:
                row["household_id"] = legacy_household_id

            # Legacy user setting/preference tables had no integer primary key.
            if table_name in {"user_settings", "user_preferences"} and "id" in dst_cols and "id" not in row:
                row["id"] = idx

            # Gate 2A+ balance_version is required and may be absent in older SQLite files.
            if table_name == "account" and "balance_version" in dst_cols and row.get("balance_version") is None:
                row["balance_version"] = 0

            normalized = {k: v for k, v in row.items() if k in dst_cols}
            normalized_rows.append(normalized)

        with target_engine.begin() as dst_conn:
            dst_conn.execute(dst_table.insert(), normalized_rows)

        _reset_sequences(target_engine, table_name)

    if not args.dry_run:
        for table_name in TABLE_ORDER:
            with target_engine.connect() as conn:
                count_after = int(conn.execute(text(f'SELECT COUNT(1) FROM "{table_name}"')).scalar() or 0)
            summary["tables"][table_name]["target_rows_after"] = count_after

    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
