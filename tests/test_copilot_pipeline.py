from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Keep tests isolated from local user data.
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import (  # noqa: E402
    app,
    db,
    Account,
    ActionAudit,
    Bill,
    ExpenseTransaction,
    Recipe,
    RecipeIngredient,
)
from services.household_context import household_id as current_household_id


client = app.test_client()
app.testing = True


def _setup() -> None:
    os.environ.pop("GROQ_API_KEY", None)
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=2000.0, pay_period_days=14, meals_per_day=3))
        db.session.commit()


def _seed_recipes() -> dict[str, int]:
    rows = [
        ("Ground Beef Tacos", 4, [("ground beef", "ground_beef", 1.0), ("tortilla", "tortilla", 1.0)]),
        ("Pulled Pork Bowls", 4, [("pulled pork", "pulled_pork", 1.0), ("rice", "rice", 1.0)]),
        ("Turkey Chili", 4, [("turkey", "turkey", 1.0), ("beans", "beans", 1.0)]),
        ("Chicken Rice Bowl", 4, [("chicken", "chicken", 1.0), ("rice", "rice", 1.0)]),
        ("Veggie Pasta", 4, [("pasta", "pasta", 1.0), ("tomato", "tomato", 1.0)]),
        ("Salmon Sheet Pan", 4, [("salmon", "salmon", 1.0), ("potato", "potato", 1.0)]),
        ("Bean Burritos", 4, [("beans", "beans", 1.0), ("tortilla", "tortilla", 1.0)]),
        ("Steak Fajitas", 4, [("steak", "steak", 1.0), ("pepper", "pepper", 1.0)]),
    ]
    ids: dict[str, int] = {}
    with app.app_context():
        for title, servings, ingredients in rows:
            recipe = Recipe(title=title, servings=servings, estimated_cost_per_serving=3.50)
            db.session.add(recipe)
            db.session.flush()
            ids[title] = recipe.id
            for product_name, keyword, qty in ingredients:
                db.session.add(
                    RecipeIngredient(
                        recipe_id=recipe.id,
                        product_name=product_name,
                        clean_keyword=keyword,
                        quantity=qty,
                        unit="item",
                    )
                )
        db.session.commit()
    return ids


def _seed_history_and_finance(recipe_ids: dict[str, int]) -> None:
    with app.app_context():
        # Historical one-time gas transactions (for omitted amount inference).
        db.session.add_all(
            [
                ExpenseTransaction(household_id=current_household_id(), description="Gas fill-up", amount=60.0, category="gas"),
                ExpenseTransaction(household_id=current_household_id(), description="Fuel station", amount=70.0, category="gas"),
                ExpenseTransaction(household_id=current_household_id(), description="Phone bill payment", amount=85.0, category="utilities"),
            ]
        )

        # Historical recipe usage to bias autofill toward known recipes.
        history_actions = {
            "recipes_added": [
                {"id": recipe_ids["Turkey Chili"], "title": "Turkey Chili"},
                {"id": recipe_ids["Chicken Rice Bowl"], "title": "Chicken Rice Bowl"},
            ],
            "recipes_auto_filled": [
                {"id": recipe_ids["Turkey Chili"], "title": "Turkey Chili"}
            ],
        }
        db.session.add(
            ActionAudit(
                household_id=current_household_id(),
                source="copilot_intent",
                user_id="tester",
                raw_text="previous plan",
                actions_json=json.dumps(history_actions),
                undo_token="history-seed-token",
                created_at=datetime.utcnow() - timedelta(days=1),
            )
        )
        db.session.commit()


def test_copilot_pipeline_multi_part_prompt_end_to_end():
    _setup()
    recipe_ids = _seed_recipes()
    _seed_history_and_finance(recipe_ids)

    import app as appmod

    original_parse = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "tool_results": [],
        "selected_recipes": [
            {"title": "ground beef", "action": "add"},
            {"title": "pulled pork", "action": "add"},
        ],
        "grocery_additions": [],
        "discretionary_events": [{"description": "gas", "amount": None}],
        "bill_updates": [{"name": "phone", "action": "increase", "amount": 10}],
        "target_meals": 7,
        "meal_servings": 4,
    }

    prompt = (
        "Plan 7 dinners for 4 people with 1 ground beef and 1 pulled pork. "
        "Also add gas for this week and my phone bill went up $10."
    )

    try:
        resp = client.post("/api/copilot/parse", json={"text": prompt})
    finally:
        appmod.parse_copilot_prompt = original_parse

    assert resp.status_code == 200
    body = resp.get_json() or {}
    actions = body.get("actions_taken", {})

    # Unified single-response execution summary.
    assert isinstance(actions, dict)
    assert actions.get("target_meals") == 7
    assert "recipes_added" in actions
    assert "recipes_auto_filled" in actions
    assert "grocery_list" in actions
    assert "expenses_logged" in actions
    assert "bills_added" in actions or "bills_updated" in actions

    # Explicit requirements are locked first.
    explicit_titles = {r["title"] for r in actions.get("recipes_added", [])}
    assert "Ground Beef Tacos" in explicit_titles
    assert "Pulled Pork Bowls" in explicit_titles

    # Backfilled to full target count (7) in one pass.
    total_planned = len(actions.get("recipes_added", [])) + len(actions.get("recipes_auto_filled", []))
    assert total_planned == 7

    # Backfill should incorporate historical recipe usage from prior audit data.
    auto_titles = {r["title"] for r in actions.get("recipes_auto_filled", [])}
    assert "Turkey Chili" in auto_titles or "Chicken Rice Bowl" in auto_titles

    # Aggregated grocery list is generated and scaled to requested servings.
    grocery = actions.get("grocery_list", [])
    assert len(grocery) > 0
    keywords = {i.get("clean_keyword") for i in grocery}
    assert "ground_beef" in keywords
    assert "pulled_pork" in keywords

    # Gas amount omitted -> inferred from historical gas average (60 + 70) / 2.
    expenses = actions.get("expenses_logged", [])
    assert len(expenses) >= 1
    gas_events = [e for e in expenses if e.get("description", "").lower() == "gas"]
    assert gas_events
    assert gas_events[0]["amount"] == 65.0

    # Phone bill increase with missing recurring baseline -> fallback to historical payment baseline.
    with app.app_context():
        bill = Bill.query.filter(Bill.name.ilike("%phone%")).first()
        assert bill is not None
        assert round(bill.amount, 2) == 95.0


def test_preference_engine_prioritizes_favorite_for_matching_category():
    _setup()
    _seed_recipes()

    with app.app_context():
        tacos = Recipe.query.filter(Recipe.title.ilike("%tacos%")).first()
        burrito = Recipe.query.filter(Recipe.title.ilike("%burritos%")).first()
        assert tacos is not None
        assert burrito is not None
        tacos.is_favorite = True
        tacos.usage_frequency = 2
        burrito.usage_frequency = 10
        db.session.commit()

    import app as appmod

    original_parse = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "tool_results": [],
        "selected_recipes": [{"title": "tortilla", "action": "add"}],
        "grocery_additions": [],
        "discretionary_events": [],
        "bill_updates": [],
        "target_meals": 1,
        "meal_servings": 4,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "plan one tortilla-based meal"})
    finally:
        appmod.parse_copilot_prompt = original_parse

    assert resp.status_code == 200
    actions = (resp.get_json() or {}).get("actions_taken", {})
    added = actions.get("recipes_added", [])
    assert added
    assert added[0]["title"] == "Ground Beef Tacos"


def test_preference_engine_uses_habit_when_no_favorite_matches_category():
    _setup()
    _seed_recipes()

    with app.app_context():
        for r in Recipe.query.all():
            r.is_favorite = False
            r.usage_frequency = 1
            r.last_selected_date = datetime.utcnow() - timedelta(days=30)
        fajitas = Recipe.query.filter(Recipe.title.ilike("%fajitas%")).first()
        assert fajitas is not None
        fajitas.usage_frequency = 18
        fajitas.last_selected_date = datetime.utcnow()
        db.session.commit()

    import app as appmod

    original_parse = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "tool_results": [],
        "selected_recipes": [{"title": "mexican", "action": "add"}],
        "grocery_additions": [],
        "discretionary_events": [],
        "bill_updates": [],
        "target_meals": 1,
        "meal_servings": 4,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "plan one mexican dinner"})
    finally:
        appmod.parse_copilot_prompt = original_parse

    assert resp.status_code == 200
    actions = (resp.get_json() or {}).get("actions_taken", {})
    added = actions.get("recipes_added", [])
    assert added
    assert added[0]["title"] == "Steak Fajitas"


def test_cold_start_broad_meal_prompt_uses_starter_defaults():
    _setup()
    _seed_recipes()

    import app as appmod

    original_parse = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "tool_results": [],
        "selected_recipes": [],
        "grocery_additions": [],
        "discretionary_events": [],
        "bill_updates": [],
        "target_meals": 4,
        "meal_servings": 4,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "What should I make for dinner?"})
    finally:
        appmod.parse_copilot_prompt = original_parse

    assert resp.status_code == 200
    body = resp.get_json() or {}
    actions = body.get("actions_taken", {})

    assert actions.get("target_meals") == 4
    starter_titles = {r["title"] for r in actions.get("recipes_added", []) + actions.get("recipes_auto_filled", [])}
    assert starter_titles
    assert "Chicken Rice Bowl" in starter_titles or "Ground Beef Tacos" in starter_titles
    assert actions.get("grocery_list")
