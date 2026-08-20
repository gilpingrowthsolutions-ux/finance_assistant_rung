from __future__ import annotations

import hashlib
import hmac
import os

os.environ["RUNG_DB_PATH"] = ":memory:"

import pytest

from app import app, db
from models import Account, Household, MealPlanItem, Recipe
from services.copilot_tools import _execute_select_active_recipe
from services.household_context import household_id as current_household_id


@pytest.fixture()
def client():
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()
        db.session.add(Account(household_id=hid, checking_balance=1000.0, pay_period_days=14))
        db.session.add_all([
            Recipe(title="Recipe One", servings=4),
            Recipe(title="Recipe Two", servings=4),
            Recipe(title="Copilot Recipe", servings=4),
        ])
        db.session.commit()
    return app.test_client()


def _recipe_ids():
    with app.app_context():
        return {row.title: row.id for row in Recipe.query.all()}


def test_served_recipes_control_uses_persisted_meal_plan_handler(client):
    html = client.get("/").get_data(as_text=True)
    assert "cb.addEventListener('change', updateRecipeSelection);" in html
    assert "async function updateRecipeSelection(event)" in html
    assert "api('POST', '/api/meal-plan'" in html
    assert "syncMealPlanCheckboxes" in html


def test_recipe_activation_persists_is_idempotent_and_preserves_removal(client):
    ids = _recipe_ids()
    first = client.post("/api/meal-plan", json={"add": [ids["Recipe One"]]})
    assert first.status_code == 200
    assert (first.get_json() or {})["recipe_ids"] == [ids["Recipe One"]]

    repeated = client.post("/api/meal-plan", json={"add": [ids["Recipe One"]]})
    assert repeated.status_code == 200
    assert (repeated.get_json() or {})["recipe_ids"] == [ids["Recipe One"]]
    with app.app_context():
        assert MealPlanItem.query.count() == 1
        assert MealPlanItem.query.one().source == "user"

    second = client.post("/api/meal-plan", json={"add": [ids["Recipe Two"]]})
    assert (second.get_json() or {})["recipe_ids"] == [ids["Recipe One"], ids["Recipe Two"]]
    reloaded = client.get("/api/meal-plan").get_json() or {}
    assert [row["title"] for row in reloaded["recipes"]] == ["Recipe One", "Recipe Two"]

    removed = client.post("/api/meal-plan", json={"remove": [ids["Recipe One"]]})
    assert (removed.get_json() or {})["recipe_ids"] == [ids["Recipe Two"]]


def test_copilot_and_recipes_share_the_same_plan_and_pay_period_authority(client):
    ids = _recipe_ids()
    assert client.post("/api/meal-plan", json={"add": [ids["Recipe One"]]}).status_code == 200
    with app.app_context():
        result = _execute_select_active_recipe(recipe_id_or_title="Copilot Recipe", action="add")
        assert result["status"] == "ok"
        account = Account.query.filter_by(household_id=current_household_id()).one()
        account.pay_period_days = 7
        db.session.commit()

    plan = client.get("/api/meal-plan").get_json() or {}
    assert set(plan["recipe_ids"]) == {ids["Recipe One"], ids["Copilot Recipe"]}
    with app.app_context():
        rows = MealPlanItem.query.order_by(MealPlanItem.id).all()
        assert {row.source for row in rows} == {"user", "copilot"}


def test_active_recipe_state_is_household_scoped_and_ignores_crafted_target(monkeypatch):
    monkeypatch.setenv("RUNG_HOUSEHOLD_CONTEXT_SECRET", "pkg5-household-secret")
    app.testing = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        a = Household(public_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", legacy_scope_key="a")
        b = Household(public_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", legacy_scope_key="b")
        recipe_a = Recipe(title="A Recipe", servings=4)
        recipe_b = Recipe(title="B Recipe", servings=4)
        db.session.add_all([a, b, recipe_a, recipe_b])
        db.session.flush()
        db.session.add_all([Account(household_id=a.id), Account(household_id=b.id)])
        db.session.add(MealPlanItem(household_id=b.id, recipe_id=recipe_b.id, source="copilot"))
        db.session.commit()
        a_id, b_id, a_public_id = a.id, b.id, a.public_id
        recipe_a_id, recipe_b_id = recipe_a.id, recipe_b.id

    signature = hmac.new(
        b"pkg5-household-secret", a_public_id.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    headers = {"X-Household-Id": a_public_id, "X-Household-Signature": signature}
    response = app.test_client().post(
        "/api/meal-plan",
        headers=headers,
        json={"add": [recipe_a_id], "household_id": b_id},
    )
    assert response.status_code == 200
    assert (response.get_json() or {})["recipe_ids"] == [recipe_a_id]
    with app.app_context():
        assert MealPlanItem.query.filter_by(household_id=a_id, recipe_id=recipe_a_id).count() == 1
        assert MealPlanItem.query.filter_by(household_id=b_id, recipe_id=recipe_b_id).count() == 1
        assert MealPlanItem.query.filter_by(household_id=b_id).count() == 1
