from __future__ import annotations

import os
import app as app_module

app_module._reverse_geocode_us_location = lambda *_: {"zip_code": "94105", "city_state": "San Francisco, CA", "state_code": "CA"}
app_module._discover_supported_stores = lambda **kwargs: {"status": "ok", "user_message": "", "zip_code": "94105", "city_state": "San Francisco, CA", "state_code": "CA", "stores": [], "provider_results": [], "received": kwargs}

if __name__ == "__main__":
    app_module.app.run(host="127.0.0.1", port=int(os.environ.get("RUNG_BROWSER_PORT", "5051")), debug=False, use_reloader=False)
