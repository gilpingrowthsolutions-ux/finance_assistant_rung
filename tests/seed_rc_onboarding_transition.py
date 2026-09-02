"""Fresh provisioned household for RC onboarding-to-Overview acceptance."""
from werkzeug.security import generate_password_hash
from app import app
from extensions import db
from models import Account, Household, HouseholdMembership, User

with app.app_context():
    db.drop_all(); db.create_all()
    household = Household(legacy_scope_key="rc-onboarding-transition")
    db.session.add(household); db.session.flush()
    account = Account(household_id=household.id, checking_balance=0, pay_period_days=14, is_onboarded=False)
    user = User(email="rc-onboarding@example.com", password_hash=generate_password_hash("rc-onboarding-pass"), active=True, auth_version=1)
    db.session.add_all([account, user]); db.session.flush()
    db.session.add(HouseholdMembership(user_id=user.id, household_id=household.id, role="owner", active=True))
    db.session.commit()
