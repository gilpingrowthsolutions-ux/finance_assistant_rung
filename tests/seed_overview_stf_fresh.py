"""Seed only the explicit disposable Overview+Safe-to-Spend Scenario A (Setup
Needed) browser database.

Deliberately creates ONLY a Household + User + HouseholdMembership. No
Account, Bill, UserSetting, UserPreference, or IncomePlanVersion rows are
created here: the point of Scenario A is to prove the served app's own lazy
bare-Account creation and truthful missing-setup detection, not a scripted
"looks ready" fixture.
"""
from __future__ import annotations

from werkzeug.security import generate_password_hash

from app import app
from extensions import db
from models import Household, HouseholdMembership, User

with app.app_context():
    db.create_all()
    household = Household()
    db.session.add(household)
    db.session.flush()
    user = User(email="sts-fresh@example.com", password_hash=generate_password_hash("sts-pass-123"), active=True)
    db.session.add(user)
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=household.id, role="owner", active=True))
    db.session.commit()
    print("seeded fresh household_id", household.id, "user_id", user.id)
