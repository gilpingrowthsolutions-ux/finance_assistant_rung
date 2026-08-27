"""Focused Feature 4 pay-period and tombstone authority tests."""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ["RUNG_DB_PATH"] = ":memory:"

import app as app_module
import pytest

from extensions import db
from models import Account, MealPlanItem, Recipe, RecipeIngredient
from services.household_context import household_id
from services.recipe_requirements import active_recipe_requirements


def _cycle(start: str, end: str):
    return {
        "available": True, "key": f"paycheck:{start}:{end}",
        "start": datetime.fromisoformat(f"{start}T00:00:00+00:00"),
        "end": datetime.fromisoformat(f"{end}T00:00:00+00:00"),
    }


@pytest.fixture()
def client(monkeypatch):
    app_module.app.testing = True
    with app_module.app.app_context():
        db.drop_all(); db.create_all()
        hid = household_id()
        db.session.add(Account(household_id=hid, pay_period_days=14))
        canonical = Recipe(title="Catalog Bowl", recipe_scope=Recipe.SCOPE_CANONICAL)
        private = Recipe(title="Private Bowl", recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, household_id=hid)
        db.session.add_all([canonical, private]); db.session.flush()
        db.session.add_all([
            RecipeIngredient(recipe_id=canonical.id, product_name="2 cups rice", clean_keyword="rice", quantity=2, unit="cup"),
            RecipeIngredient(recipe_id=private.id, product_name="1 lb beans", clean_keyword="beans", quantity=1, unit="lb"),
        ])
        db.session.commit()
    monkeypatch.setattr(app_module, "_current_meal_plan_cycle", lambda: _cycle("2026-08-14", "2026-08-28"))
    return app_module.app.test_client()


def test_cycle_rows_are_immutable_current_only_and_unique(client, monkeypatch):
    with app_module.app.app_context():
        recipes = {r.title: r.id for r in Recipe.query.all()}
        hid = household_id()
    assert client.post("/api/meal-plan", json={"add": [recipes["Catalog Bowl"]]}).status_code == 200
    assert client.post("/api/meal-plan", json={"add": [recipes["Catalog Bowl"]]}).get_json()["count"] == 1
    with app_module.app.app_context():
        first = MealPlanItem.query.one()
        assert first.cycle_key == "paycheck:2026-08-14:2026-08-28"
        assert [r.base_item for r in active_recipe_requirements(hid)] == ["rice"]

    monkeypatch.setattr(app_module, "_current_meal_plan_cycle", lambda: _cycle("2026-08-28", "2026-09-11"))
    assert client.get("/api/meal-plan").get_json()["count"] == 0
    assert client.post("/api/meal-plan", json={"add": [recipes["Catalog Bowl"]]}).status_code == 200
    with app_module.app.app_context():
        rows = MealPlanItem.query.order_by(MealPlanItem.id).all()
        assert len(rows) == 2 and rows[0].cycle_key != rows[1].cycle_key
        assert [r.base_item for r in active_recipe_requirements(hid)] == ["rice"]
    assert client.post("/api/meal-plan/clear").status_code == 200
    with app_module.app.app_context():
        assert MealPlanItem.query.count() == 1
        assert MealPlanItem.query.one().cycle_key == "paycheck:2026-08-14:2026-08-28"


def test_private_delete_blocks_current_then_tombstones_historical(client, monkeypatch):
    with app_module.app.app_context():
        private = Recipe.query.filter_by(title="Private Bowl").one()
        private_id = private.id
    assert client.post("/api/meal-plan", json={"add": [private_id]}).status_code == 200
    assert client.delete(f"/api/recipes/{private_id}").status_code == 409
    assert client.post("/api/meal-plan", json={"remove": [private_id]}).status_code == 200
    assert client.delete(f"/api/recipes/{private_id}").status_code == 200
    with app_module.app.app_context():
        private = db.session.get(Recipe, private_id)
        assert private is not None and private.tombstoned_at is not None
        assert RecipeIngredient.query.filter_by(recipe_id=private_id).count() == 1
    assert private_id not in {r["id"] for r in client.get("/api/recipes").get_json()}
    assert client.post("/api/meal-plan", json={"add": [private_id]}).status_code == 404

    # A separate private historical activation keeps identity after tombstone.
    with app_module.app.app_context():
        historical = Recipe(title="History", recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, household_id=household_id())
        db.session.add(historical); db.session.flush()
        db.session.add(MealPlanItem(household_id=household_id(), recipe_id=historical.id, source="user",
                                    cycle_key="paycheck:2026-08-14:2026-08-28",
                                    cycle_start=datetime(2026, 8, 14, tzinfo=timezone.utc),
                                    cycle_end=datetime(2026, 8, 28, tzinfo=timezone.utc)))
        db.session.commit(); historical_id = historical.id
    monkeypatch.setattr(app_module, "_current_meal_plan_cycle", lambda: _cycle("2026-08-28", "2026-09-11"))
    assert client.delete(f"/api/recipes/{historical_id}").status_code == 200
    with app_module.app.app_context():
        assert db.session.get(Recipe, historical_id).tombstoned_at is not None
        assert MealPlanItem.query.filter_by(recipe_id=historical_id).count() == 1


def test_missing_current_cycle_reads_empty_and_rejects_all_mutations(client, monkeypatch):
    with app_module.app.app_context():
        hid = household_id()
        recipe_id = Recipe.query.filter_by(title="Catalog Bowl").one().id
    monkeypatch.setattr(app_module, "_current_meal_plan_cycle", lambda: {"available": False})
    assert client.get("/api/meal-plan").get_json()["count"] == 0
    for url, body in (
        ("/api/meal-plan", {"add": [recipe_id]}),
        ("/api/meal-plan", {"remove": [recipe_id]}),
        ("/api/meal-plan", {"recipe_ids": []}),
        ("/api/meal-plan/clear", {}),
    ):
        response = client.post(url, json=body)
        assert response.status_code == 409
        assert "pay-cycle setup" in response.get_json()["error"]
    with app_module.app.app_context():
        assert MealPlanItem.query.filter_by(household_id=hid).count() == 0
