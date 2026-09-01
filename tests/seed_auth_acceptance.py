"""Disposable fixture for the served authentication/session acceptance spec."""

from app import app
from extensions import db
from models import Account, Household, HouseholdMembership, User
from werkzeug.security import generate_password_hash


with app.app_context():
    db.drop_all()
    db.create_all()

    household_a = Household(legacy_scope_key="auth-browser-a")
    household_b = Household(legacy_scope_key="auth-browser-b")
    db.session.add_all([household_a, household_b])
    db.session.flush()

    user_a = User(
        email="auth-browser@example.com",
        password_hash=generate_password_hash("auth-pass-123"),
        active=True,
        auth_version=1,
    )
    user_b = User(
        email="auth-browser-b@example.com",
        password_hash=generate_password_hash("auth-pass-b-123"),
        active=True,
        auth_version=1,
    )
    db.session.add_all([user_a, user_b])
    db.session.flush()
    db.session.add_all([
        Account(household_id=household_a.id, checking_balance=111.0, is_onboarded=True),
        Account(household_id=household_b.id, checking_balance=222.0, is_onboarded=True),
        HouseholdMembership(user_id=user_a.id, household_id=household_a.id, role="owner", active=True),
        HouseholdMembership(user_id=user_b.id, household_id=household_b.id, role="owner", active=True),
    ])
    db.session.commit()
