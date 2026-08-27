"""Regression coverage for trusted canonical recipe seeding."""
from __future__ import annotations

import json

from app import app, db
from models import Household, Recipe, RecipeIngredient
from seed_recipes import seed_recipes


def _write_rows(tmp_path, rows):
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def test_seed_is_canonical_only_idempotent_and_preserves_private_quarantine(tmp_path):
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        household = Household(public_id="33333333-3333-3333-3333-333333333333", legacy_scope_key="seed")
        db.session.add(household)
        db.session.add_all([
            Recipe(title="Shared title", recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, household_id=1),
            Recipe(title="Legacy title", recipe_scope=Recipe.SCOPE_LEGACY_QUARANTINED),
        ])
        db.session.commit()

    path = _write_rows(tmp_path, [{
        "title": "Shared title", "servings": 3,
        "ingredients": ["2 cups rice"], "category": "Dinner", "area": "Global",
        "instructions": "Cook gently.",
    }])
    assert seed_recipes(path) == {"inserted": 1, "skipped": 0, "total": 1, "errors": []}
    assert seed_recipes(path) == {"inserted": 0, "skipped": 1, "total": 1, "errors": []}
    with app.app_context():
        canonical = Recipe.query.filter_by(title="Shared title", recipe_scope=Recipe.SCOPE_CANONICAL).one()
        assert canonical.household_id is None
        assert canonical.instructions == "[Category: Dinner] [Area: Global]\nCook gently."
        ingredient = RecipeIngredient.query.filter_by(recipe_id=canonical.id).one()
        assert (ingredient.product_name, ingredient.quantity, ingredient.unit) == ("2 cups rice", 2.0, "cup")
        assert Recipe.query.filter_by(recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE).count() == 1
        assert Recipe.query.filter_by(recipe_scope=Recipe.SCOPE_LEGACY_QUARANTINED).count() == 1


def test_seed_row_failure_rolls_back_inside_context_and_subsequent_run_works(tmp_path, monkeypatch):
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
    path = _write_rows(tmp_path, [
        {"title": "Good row", "ingredients": ["1 lb chicken"]},
        {"title": "Fail row", "ingredients": ["2 cups FAIL"]},
    ])
    original_add = db.session.add

    def fail_one_ingredient(row):
        if isinstance(row, RecipeIngredient) and row.product_name == "2 cups FAIL":
            raise RuntimeError("injected mid-seed failure")
        return original_add(row)

    monkeypatch.setattr(db.session, "add", fail_one_ingredient)
    result = seed_recipes(path)
    assert result["inserted"] == 1
    assert len(result["errors"]) == 1
    with app.app_context():
        assert Recipe.query.filter_by(title="Good row").count() == 1
        assert Recipe.query.filter_by(title="Fail row").count() == 0
        assert RecipeIngredient.query.count() == 1
    monkeypatch.setattr(db.session, "add", original_add)
    result = seed_recipes(_write_rows(tmp_path, [{"title": "Recovered row", "ingredients": ["2 cups rice"]}]))
    assert result == {"inserted": 1, "skipped": 0, "total": 1, "errors": []}
    with app.app_context():
        assert Recipe.query.filter_by(title="Recovered row", recipe_scope=Recipe.SCOPE_CANONICAL).count() == 1
