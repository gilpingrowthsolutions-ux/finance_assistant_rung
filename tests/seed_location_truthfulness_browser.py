from __future__ import annotations

import app as app_module
from models import Account, HouseholdMembership, User
from services.household_context import household_id
from services.selected_store import select_store
from werkzeug.security import generate_password_hash

with app_module.app.app_context():
    app_module.db.drop_all()
    app_module.db.create_all()
    hid = household_id()
    account = Account(household_id=hid, checking_balance=1000, zip_code="65084", city_state="Versailles, MO", latitude=38.3605, longitude=-92.5824, is_onboarded=True)
    app_module.db.session.add(account)
    app_module.db.session.flush()
    user = User(email="location-browser@example.com", password_hash=generate_password_hash("location-pass-123"), active=True, auth_version=1)
    app_module.db.session.add(user)
    app_module.db.session.flush()
    app_module.db.session.add(HouseholdMembership(user_id=user.id, household_id=hid, role="owner", active=True))
    select_store(hid, retailer="kroger", store_id="61500116", store_name="Gerbes", postal_code="65084", account=account)
    app_module.set_setting(app_module.LOCATION_SHARING_SETTING_KEY, "true")
