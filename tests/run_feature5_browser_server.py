"""Real Flask launcher for Feature 5 browser acceptance.

Only external nearby-store discovery is replaced.  The browser still calls
the real endpoint and must explicitly select a returned physical store.
"""
from __future__ import annotations

import os

import app as app_module


def deterministic_nearby_stores(**_kwargs):
    return {
        "status": "ok", "user_message": "", "zip_code": "65084",
        "city_state": "Versailles, MO", "state_code": "MO", "provider_results": [{"retailer": "fixture", "success": True, "stores_count": 2}],
        "stores": [
            {"retailer": "walmart", "store_id": "A", "name": "Store A", "address": "1 Fixture Way, Versailles, MO 65084", "postal_code": "65084"},
            {"retailer": "walmart", "store_id": "B", "name": "Store B", "address": "2 Fixture Way, Versailles, MO 65084", "postal_code": "65084"},
        ],
    }


app_module._discover_supported_stores = deterministic_nearby_stores

if __name__ == "__main__":
    app_module.app.run(host="127.0.0.1", port=int(os.environ.get("RUNG_BROWSER_PORT", "5051")), debug=False, use_reloader=False)
