from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Keep tests isolated from local user data.
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app, db, Account, Bill, Recipe, RecipeIngredient, UserPreference  # noqa: E402
from services.household_context import household_id as current_household_id


client = app.test_client()
app.testing = True


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1250.0, household_size=4, is_onboarded=False))
        db.session.commit()


def _seed_starter_recipes() -> None:
    rows = [
        ("Chicken Rice Bowl", [("Chicken Breast", "chicken"), ("White Rice", "rice")]),
        ("Ground Beef Tacos", [("Ground Beef", "beef"), ("Corn Tortillas", "tortilla")]),
        ("Vegetable Stir Fry", [("Broccoli", "broccoli"), ("Rice Noodles", "rice")]),
        ("Margherita Pizza", [("Pizza Dough", "pizza"), ("Fresh Basil", "basil")]),
    ]
    with app.app_context():
        for title, ingredients in rows:
            recipe = Recipe(title=title, servings=4, estimated_cost_per_serving=3.5, recipe_scope=Recipe.SCOPE_CANONICAL)
            db.session.add(recipe)
            db.session.flush()
            for product_name, keyword in ingredients:
                db.session.add(RecipeIngredient(
                    recipe_id=recipe.id,
                    product_name=product_name,
                    clean_keyword=keyword,
                    quantity=1.0,
                    unit="item",
                ))
        db.session.commit()


def test_onboarding_state_first_launch() -> None:
    _setup()
    resp = client.get("/api/onboarding/state")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert data.get("is_onboarded") is False
    assert data.get("show_onboarding") is True
    defaults = data.get("defaults") or {}
    assert defaults.get("household_size") == 4


def test_onboarding_complete_persists_baselines() -> None:
    _setup()
    _seed_starter_recipes()
    payload = {
        "household_size": 5,
        "favorite_proteins": ["chicken", "salmon"],
        "dietary_restrictions": ["low carb"],
        "allergies": ["peanuts"],
        "recurring_bills": [
            {"name": "Phone", "amount": 95.0},
            {"name": "Internet", "amount": 70.0},
            {"name": "Utilities", "amount": 140.0},
        ],
        "baseline_grocery_cost": 240.0,
        "baseline_fuel_cost": 75.0,
    }

    resp = client.post("/api/onboarding/complete", json=payload)
    assert resp.status_code == 200
    body = resp.get_json() or {}
    assert body.get("saved") is True
    assert body.get("is_onboarded") is True

    with app.app_context():
        account = Account.query.first()
        assert account is not None
        assert account.is_onboarded is True
        assert account.household_size == 5

        prefs = {p.key: p.value for p in UserPreference.query.all()}
        assert json.loads(prefs.get("favorite_proteins", "[]")) == ["chicken", "salmon"]
        assert json.loads(prefs.get("dietary_restrictions", "[]")) == ["low carb"]
        assert json.loads(prefs.get("allergies", "[]")) == ["peanuts"]
        assert float(prefs.get("baseline_grocery_cost", "0")) == 240.0
        assert float(prefs.get("baseline_fuel_cost", "0")) == 75.0

        seeded = json.loads(prefs.get("starter_preferences_seeded", "{}"))
        assert seeded.get("titles")
        assert "Chicken Rice Bowl" in seeded.get("titles", [])

        favorites = Recipe.query.filter_by(is_favorite=True).all()
        favorite_titles = {recipe.title for recipe in favorites}
        assert "Chicken Rice Bowl" in favorite_titles
        assert "Ground Beef Tacos" in favorite_titles

        phone = Bill.query.filter(Bill.name.ilike("%phone%")).first()
        internet = Bill.query.filter(Bill.name.ilike("%internet%")).first()
        utilities = Bill.query.filter(Bill.name.ilike("%utilities%")).first()
        gas = Bill.query.filter_by(is_gas_estimate=True).first()

        assert phone is not None and round(phone.amount, 2) == 95.0
        assert internet is not None and round(internet.amount, 2) == 70.0
        assert utilities is not None and round(utilities.amount, 2) == 140.0
        assert gas is not None and round(gas.amount, 2) == 75.0


def test_onboarding_skip_marks_complete() -> None:
    _setup()
    _seed_starter_recipes()

    resp = client.post("/api/onboarding/skip", json={})
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert data.get("saved") is True
    assert data.get("is_onboarded") is True

    with app.app_context():
        account = Account.query.first()
        assert account is not None
        assert account.is_onboarded is True
        favorites = {recipe.title for recipe in Recipe.query.filter_by(is_favorite=True).all()}
        assert "Chicken Rice Bowl" in favorites
