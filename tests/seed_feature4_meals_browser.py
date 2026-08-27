"""Seed the disposable authenticated browser database for Feature 4 Meals.

Run with ``PYTHONPATH=.`` and ``RUNG_DB_PATH`` set before this module imports
the application.  This is deliberately real completed-onboarding data rather
than a browser-only shortcut.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import (NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY,
                 REQUIRED_EXPENSE_NONE, REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
                 SAFE_BUFFER_SETTING_KEY, _onboarding_readiness, app)
from extensions import db
from models import (Account, Household, HouseholdMembership, IncomePlanVersion,
                    MealPlanItem, Recipe, RecipeIngredient, User, UserSetting)


def add_recipe(title, *, scope, household_id=None, ingredient="rice", quantity=2, unit="cup"):
    recipe = Recipe(title=title, servings=4, instructions=f"Cook {title} gently.",
                    recipe_scope=scope, household_id=household_id)
    db.session.add(recipe); db.session.flush()
    db.session.add(RecipeIngredient(recipe_id=recipe.id, product_name=f"{quantity} {unit}s {ingredient}",
                                    clean_keyword=ingredient, quantity=quantity, unit=unit))
    return recipe


with app.app_context():
    db.create_all()
    a = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="browser-a")
    b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="browser-b")
    db.session.add_all([a, b]); db.session.flush()
    now = datetime.now(timezone.utc)
    for household in (a, b):
        db.session.add(Account(household_id=household.id, checking_balance=1500, pay_period_days=14,
                               is_onboarded=True, zip_code="65084", kroger_store_name="Walmart — Versailles"))
        db.session.add(IncomePlanVersion(household_id=household.id, operation_id=f"browser-plan-{household.id}",
                                         expected_income_cents=200000, effective_at=now-timedelta(days=2), source="browser"))
        db.session.add_all([
            UserSetting(household_id=household.id, key=NEXT_PAYDAY_SETTING_KEY,
                        value=(now.date()+timedelta(days=7)).isoformat()),
            UserSetting(household_id=household.id, key=PYF_TARGET_SETTING_KEY, value="10"),
            UserSetting(household_id=household.id, key=SAFE_BUFFER_SETTING_KEY, value="150.00"),
            UserSetting(household_id=household.id, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
                        value=REQUIRED_EXPENSE_NONE),
        ])
    user_a = User(email="feature4-browser@example.com", password_hash=generate_password_hash("browser-pass-123"), active=True, auth_version=1)
    user_b = User(email="feature4-browser-b@example.com", password_hash=generate_password_hash("browser-pass-456"), active=True, auth_version=1)
    db.session.add_all([user_a, user_b]); db.session.flush()
    db.session.add_all([
        HouseholdMembership(user_id=user_a.id, household_id=a.id, role="owner", active=True),
        HouseholdMembership(user_id=user_b.id, household_id=b.id, role="owner", active=True),
    ])
    canonical = add_recipe("Canonical Rice Bowl", scope=Recipe.SCOPE_CANONICAL)
    prior = add_recipe("Prior Cycle Soup", scope=Recipe.SCOPE_CANONICAL, ingredient="beans")
    add_recipe("Quarantined Legacy", scope=Recipe.SCOPE_LEGACY_QUARANTINED)
    add_recipe("B Private", scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, household_id=b.id, ingredient="oats")
    historical_private = add_recipe("Historical Private", scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, household_id=a.id, ingredient="barley")
    start = datetime.combine(now.date()-timedelta(days=21), datetime.min.time(), tzinfo=timezone.utc)
    db.session.add(MealPlanItem(household_id=a.id, recipe_id=prior.id, source="fixture",
                                cycle_key=f"paycheck:{start.date()}:{(start+timedelta(days=14)).date()}", cycle_start=start, cycle_end=start+timedelta(days=14)))
    db.session.add(MealPlanItem(household_id=a.id, recipe_id=historical_private.id, source="fixture-private-history",
                                cycle_key=f"paycheck:{start.date()}:{(start+timedelta(days=14)).date()}", cycle_start=start, cycle_end=start+timedelta(days=14)))
    db.session.commit()
    # Assert the data has the same completed financial authority that the
    # served app reads.  No request/session shortcut is used here.
    for household in (a, b):
        account = Account.query.filter_by(household_id=household.id).one()
        assert account.is_onboarded is True
        assert IncomePlanVersion.query.filter_by(household_id=household.id).count() == 1
        keys = {row.key for row in UserSetting.query.filter_by(household_id=household.id).all()}
        assert {NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY,
                REQUIRED_EXPENSE_REVIEW_SETTING_KEY} <= keys
    print(f"seeded Feature 4 household_a={a.id} household_b={b.id} canonical={canonical.id} prior={prior.id}")
