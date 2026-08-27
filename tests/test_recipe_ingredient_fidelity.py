from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

import app as app_module
from app import app, db
from models import Account, MealPlanItem, PantryItem, Recipe, RecipeIngredient
from seed_recipes import parse_ingredient as parse_seed_ingredient
from services.household_context import household_id
from services.recipe_ingredients import parse_recipe_ingredient
from tests.meal_plan_support import install_current_cycle


@pytest.fixture()
def client(monkeypatch):
    install_current_cycle(monkeypatch)
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=household_id(), checking_balance=500.0))
        db.session.commit()
        app_module._import_cache.clear()
    return app.test_client()


@pytest.mark.parametrize(
    ("line", "quantity", "unit", "name"),
    [
        ("2 cups rice", 2.0, "cup", "2 cups rice"),
        ("1.5 lb chicken", 1.5, "lb", "1.5 lb chicken"),
        ("3 cans beans", 3.0, "can", "3 cans beans"),
        ("2 cloves garlic", 2.0, "clove", "2 cloves garlic"),
        ("1 1/2 tablespoons oil", 1.5, "tbsp", "1 1/2 tablespoons oil"),
        ("½ teaspoon salt", 0.5, "tsp", "½ teaspoon salt"),
        ("2 eggs", 2.0, "item", "2 eggs"),
    ],
)
def test_deterministic_recipe_requirement_parsing(line, quantity, unit, name):
    parsed = parse_recipe_ingredient(line)
    assert parsed is not None
    assert parsed["quantity"] == pytest.approx(quantity)
    assert parsed["unit"] == unit
    assert parsed["product_name"] == name


def test_unknown_requirement_stays_verbatim_without_fabricated_one_item():
    parsed = parse_recipe_ingredient("salt to taste")
    assert parsed == {
        "product_name": "salt to taste",
        "quantity": None,
        "unit": None,
        "source_text": "salt to taste",
    }
    ambiguous = parse_recipe_ingredient("about two cups flour")
    assert ambiguous["product_name"] == "about two cups flour"
    assert ambiguous["quantity"] is None
    assert ambiguous["unit"] is None


def test_manual_create_serialize_reload_and_active_plan_fidelity(client):
    response = client.post("/api/recipes", json={
        "title": "Fidelity Dinner",
        "servings": 4,
        "ingredients": [
            "2 cups rice",
            "1.5 lb chicken",
            "3 cans beans",
            "2 cloves garlic",
            "salt to taste",
        ],
        "household_id": 999999,
    })
    assert response.status_code == 200
    recipe_id = response.get_json()["id"]

    serialized = next(row for row in client.get("/api/recipes").get_json() if row["id"] == recipe_id)
    assert [(row["product_name"], row["quantity"], row["unit"]) for row in serialized["ingredients"]] == [
        ("2 cups rice", 2.0, "cup"),
        ("1.5 lb chicken", 1.5, "lb"),
        ("3 cans beans", 3.0, "can"),
        ("2 cloves garlic", 2.0, "clove"),
        ("salt to taste", None, None),
    ]

    assert client.post("/api/meal-plan", json={"add": [recipe_id]}).status_code == 200
    plan = client.get("/api/meal-plan").get_json()
    assert plan["recipe_ids"] == [recipe_id]
    assert plan["recipes"][0]["ingredients"] == serialized["ingredients"]
    with app.app_context():
        assert MealPlanItem.query.filter_by(household_id=household_id(), recipe_id=recipe_id).count() == 1


def test_import_path_repairs_historical_one_item_degradation(client):
    scraper = MagicMock()
    scraper.title.return_value = "Imported Fidelity Recipe"
    scraper.total_time.return_value = 30
    scraper.yields.return_value = "4 servings"
    scraper.instructions.return_value = "Cook it."
    scraper.image.return_value = None
    scraper.ingredients.return_value = [
        "2 cups rice", "1.5 lb chicken", "3 cans beans", "salt to taste",
    ]
    with patch("recipe_scrapers.scrape_me", return_value=scraper):
        response = client.post("/api/recipes/import", json={"url": "https://example.test/fidelity"})
    assert response.status_code == 200

    with app.app_context():
        recipe = Recipe.query.filter_by(source_url="https://example.test/fidelity").one()
        rows = RecipeIngredient.query.filter_by(recipe_id=recipe.id).order_by(RecipeIngredient.id).all()
        assert [(row.product_name, row.quantity, row.unit) for row in rows] == [
            ("2 cups rice", 2.0, "cup"),
            ("1.5 lb chicken", 1.5, "lb"),
            ("3 cans beans", 3.0, "can"),
            ("salt to taste", None, None),
        ]
        assert not any(row.quantity == 1 and row.unit == "item" for row in rows)


def test_structured_and_seed_paths_share_fidelity_rules(client):
    response = client.post("/api/recipes", json={
        "title": "Structured Recipe",
        "ingredients": [
            {"product_name": "Chicken", "quantity": "1.5", "unit": "pounds", "clean_keyword": "chicken"},
            {"product_name": "2 cans tomatoes"},
        ],
    })
    assert response.status_code == 200
    recipe_id = response.get_json()["id"]
    generated = client.post("/api/recipes/generate", json={"recipe_ids": [recipe_id]}).get_json()
    ingredients = generated["recipes"][0]["ingredients"]
    assert [(row["product_name"], row["quantity"], row["unit"]) for row in ingredients] == [
        ("Chicken", 1.5, "lb"),
        ("2 cans tomatoes", 2.0, "can"),
    ]
    assert parse_seed_ingredient("3 cloves garlic")["quantity"] == 3.0
    assert parse_seed_ingredient("3 cloves garlic")["unit"] == "clove"


def test_no_volume_mass_or_package_conversion_is_invented():
    cup = parse_recipe_ingredient("2 cups flour")
    pound = parse_recipe_ingredient("2 lb flour")
    can = parse_recipe_ingredient("2 cans tomatoes")
    assert (cup["quantity"], cup["unit"]) == (2.0, "cup")
    assert (pound["quantity"], pound["unit"]) == (2.0, "lb")
    assert (can["quantity"], can["unit"]) == (2.0, "can")


def test_unknown_requirement_does_not_invent_pantry_depletion(client):
    created = client.post("/api/recipes", json={
        "title": "Uncertain Seasoning",
        "ingredients": ["salt to taste"],
    }).get_json()
    with app.app_context():
        db.session.add(PantryItem(
            household_id=household_id(), clean_keyword="salt",
            product_name="Salt", quantity=10.0, unit="oz",
        ))
        db.session.commit()
    assert client.post("/api/pantry/cook", json={"recipe_id": created["id"]}).status_code == 200
    with app.app_context():
        assert PantryItem.query.filter_by(household_id=household_id(), clean_keyword="salt").one().quantity == 10.0
