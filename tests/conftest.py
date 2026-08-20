from __future__ import annotations

import os

# Standing safety rule:
# Never run destructive DB operations (for example db.drop_all()) against an
# imported Rung app unless RUNG_DB_PATH is explicitly set to an isolated,
# disposable database BEFORE importing app.py and the resolved URI is verified
# non-production.

# Individual tests may override this with their own temporary file before importing app.
os.environ.setdefault("RUNG_DB_PATH", ":memory:")
