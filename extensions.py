"""
Centralized extension initialization.

This module provides a single, authoritative SQLAlchemy instance that can be
safely used across the entire application, including in Flask's debug reloader.

The key pattern: SQLAlchemy() is created WITHOUT passing the Flask app, then
db.init_app(app) is called after the Flask app is created. This allows the
same db instance to be bound to different Flask app instances (e.g., parent vs
reloader child process).
"""

import os
from pathlib import Path
from typing import Optional

from flask import current_app
from flask_sqlalchemy import SQLAlchemy


def _is_production_rung_db_path(path_value: str) -> bool:
	if not path_value:
		return False
	try:
		resolved = Path(path_value).expanduser().resolve()
	except Exception:
		return False
	return resolved.name == "rung_finance.db"


def assert_safe_destructive_db_target(db_uri: Optional[str], rung_db_path: Optional[str]) -> None:
	"""Fail closed unless destructive ops target an explicit isolated DB.

	Rules:
	- ``rung_finance.db`` is always blocked.
	- ``RUNG_DB_PATH`` must be explicitly set for destructive operations.
	- ``RUNG_DB_PATH`` must point to an isolated DB (or ``:memory:``).
	"""
	uri = str(db_uri or "")
	env_path = str(rung_db_path or "").strip()

	if "rung_finance.db" in uri:
		raise RuntimeError(
			"Refusing destructive DB operation: target resolves to production rung_finance.db."
		)

	if not env_path:
		raise RuntimeError(
			"Refusing destructive DB operation: RUNG_DB_PATH must be explicitly set "
			"to an isolated test database before importing the app."
		)

	if env_path == ":memory:":
		return

	if _is_production_rung_db_path(env_path):
		raise RuntimeError(
			"Refusing destructive DB operation: RUNG_DB_PATH points to rung_finance.db."
		)


class SafeSQLAlchemy(SQLAlchemy):
	def drop_all(self, bind_key="__all__") -> None:  # type: ignore[override]
		assert_safe_destructive_db_target(
			current_app.config.get("SQLALCHEMY_DATABASE_URI"),
			os.environ.get("RUNG_DB_PATH"),
		)
		return super().drop_all(bind_key=bind_key)


db = SafeSQLAlchemy()
