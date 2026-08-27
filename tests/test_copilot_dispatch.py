"""Integration tests for the AI Copilot dispatch rewrite.

Covers the three fixes in app.py's ``/api/copilot/parse`` endpoint:

  1. **Bill amount coercion** — the LLM sometimes returns ``amount`` as a
     string like ``"$60"`` or ``"60/mo"``.  ``float("$60")`` raised
     ValueError and the bill was silently skipped.  Now ``_coerce_amount``
     parses it and the bill actually persists.

  2. **Recipes actually added to the meal plan** — ``selected_recipes``
     used to be echoed back as suggestions only.  Now matched titles are
     fuzzy-matched against the local Recipe DB and PERSISTED to the
     server-side ``meal_plan`` table (visible in /api/meal-plan and the
     Grocery tab expander).

  3. **Auto-fill** — when the user asks for N meals but only names fewer
     (``target_meals``), the dispatch top-ups the plan with recommended
     recipes the user is likely to like.  Recipes the user explicitly
     asked to REMOVE are never re-added by the auto-filler.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so db.drop_all()/create_all() can never wipe rung_finance.db.
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import (
    app, db, Account, Bill, Recipe, RecipeIngredient, MealPlanItem,
)
from services.copilot_service import parse_copilot_prompt
from services.household_context import household_id as current_household_id
from tests.meal_plan_support import install_current_cycle

client = app.test_client()
app.testing = True
_pass = 0
_fail = 0


def _setup():
    # Keep tests hermetic: app.py loads .env at import, which can set
    # GROQ_API_KEY in os.environ and trigger real API calls.
    os.environ.pop("GROQ_API_KEY", None)
    install_current_cycle()
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.commit()
        acc = Account(household_id=current_household_id(), checking_balance=1250.00)
        db.session.add(acc)
        db.session.commit()


def _seed_recipes():
    """Seed a small recipe library with distinct ingredient profiles."""
    with app.app_context():
        rows = [
            ("Chicken Rice Bowl", 3.20, [("chicken", "chicken"), ("rice", "rice")]),
            ("Flank Steak Fajitas", 4.10, [("steak", "steak"), ("pepper", "pepper"), ("tortilla", "tortilla")]),
            ("Turkey Chili", 2.60, [("turkey", "turkey"), ("bean", "bean")]),
            ("Veggie Stir Fry", 3.00, [("broccoli", "broccoli"), ("rice", "rice")]),
            ("Pasta Marinara", 2.40, [("pasta", "pasta"), ("tomato", "tomato")]),
            ("Salmon Sheet Pan", 5.00, [("salmon", "salmon"), ("potato", "potato")]),
            ("Black Bean Tacos", 2.10, [("bean", "bean"), ("tortilla", "tortilla")]),
        ]
        for title, cost, ings in rows:
            r = Recipe(title=title, servings=4, estimated_cost_per_serving=cost, recipe_scope=Recipe.SCOPE_CANONICAL)
            db.session.add(r)
            db.session.flush()
            for name, kw in ings:
                db.session.add(RecipeIngredient(
                    recipe_id=r.id, product_name=name, clean_keyword=kw,
                    quantity=1.0, unit="oz",
                ))
        db.session.commit()


def _assert_eq(a, b, msg=""):
    global _pass, _fail
    if a == b:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL {msg}: expected {b!r}, got {a!r}")


def _assert_truthy(val, msg=""):
    global _pass, _fail
    if val:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL {msg}: value is falsy")


# ---------------------------------------------------------------------------
# 1 — Bill with string amount "$60" actually persists (the reported bug)
# ---------------------------------------------------------------------------

def test_bill_amount_string_coercion():
    _setup()
    # Simulate the LLM returning amount as a string with a currency sign.
    import app as appmod

    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "bill_updates": [{"name": "gas bill", "amount": "$60", "action": "add"}],
        "discretionary_events": [], "grocery_additions": [],
        "selected_recipes": [], "target_meals": None,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "add gas bill $60"})
    finally:
        appmod.parse_copilot_prompt = orig

    d = resp.get_json() or {}
    _assert_eq(resp.status_code, 200, "parse returns 200")
    added = d.get("actions_taken", {}).get("bills_added", [])
    _assert_eq(len(added), 1, "bill is reported as added")
    _assert_eq(added[0]["amount"], 60.0, "amount coerced to 60.0")
    with app.app_context():
        bills = [(b.name, b.amount) for b in Bill.query.all()]
        _assert_truthy(any(b[1] == 60.0 for b in bills), "bill persisted to DB")
        _assert_eq(bills[0][0], "Gas Bill", "bill name title-cased")


def test_bill_amount_slash_mo_string():
    _setup()
    import app as appmod

    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "bill_updates": [{"name": "hbo max", "amount": "14.99/mo", "action": "add"}],
        "discretionary_events": [], "grocery_additions": [],
        "selected_recipes": [], "target_meals": None,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "add hbo max 14.99/mo"})
    finally:
        appmod.parse_copilot_prompt = orig

    d = resp.get_json() or {}
    added = d.get("actions_taken", {}).get("bills_added", [])
    _assert_eq(len(added), 1, "bill reported")
    _assert_eq(added[0]["amount"], 14.99, "14.99/mo coerced to 14.99")


# ---------------------------------------------------------------------------
# 2 — Selected recipes are persisted to the meal plan (not just suggested)
# ---------------------------------------------------------------------------

def test_recipes_added_to_meal_plan():
    _setup()
    _seed_recipes()
    import app as appmod

    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "bill_updates": [], "discretionary_events": [], "grocery_additions": [],
        "selected_recipes": [{"title": "Chicken Rice Bowl", "action": "add"}],
        "target_meals": None,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "cook chicken rice bowl"})
    finally:
        appmod.parse_copilot_prompt = orig

    d = resp.get_json() or {}
    at = d.get("actions_taken", {})
    _assert_eq(len(at.get("recipes_added", [])), 1, "recipe added to plan")
    _assert_eq(at.get("recipes_added", [])[0]["title"], "Chicken Rice Bowl", "added title")
    _assert_eq(len(at.get("recipes_suggested", [])), 0, "no unmatched titles")

    with app.app_context():
        plan = [(m.recipe_id, m.source) for m in MealPlanItem.query.all()]
        _assert_eq(len(plan), 1, "meal plan has 1 row")
        _assert_eq(plan[0][1], "copilot", "source is copilot")
        rid = Recipe.query.filter_by(title="Chicken Rice Bowl").first().id
        _assert_eq(plan[0][0], rid, "meal plan references the matched recipe")

    # GET /api/meal-plan should now return the persisted recipe.
    resp = client.get("/api/meal-plan")
    d = resp.get_json() or {}
    _assert_eq(d.get("count"), 1, "meal-plan GET count")
    _assert_eq(d["recipes"][0]["title"], "Chicken Rice Bowl", "meal-plan GET title")


def test_unmatched_recipe_goes_to_suggestions():
    _setup()
    _seed_recipes()
    import app as appmod

    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "bill_updates": [], "discretionary_events": [], "grocery_additions": [],
        "selected_recipes": [{"title": "Sushi Rolls", "action": "add"}],
        "target_meals": None,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "cook sushi rolls"})
    finally:
        appmod.parse_copilot_prompt = orig

    d = resp.get_json() or {}
    at = d.get("actions_taken", {})
    _assert_eq(len(at.get("recipes_added", [])), 0, "no local match -> not added")
    _assert_eq(len(at.get("recipes_suggested", [])), 1, "unmatched title suggested")
    _assert_eq(at.get("recipes_suggested", [])[0]["title"], "Sushi Rolls", "suggestion title")
    with app.app_context():
        _assert_eq(MealPlanItem.query.count(), 0, "meal plan untouched")


# ---------------------------------------------------------------------------
# 3 — Auto-fill: ask for 7, only name 2 → fill the remaining 5
# ---------------------------------------------------------------------------

def test_autofill_to_target():
    _setup()
    _seed_recipes()
    import app as appmod

    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "bill_updates": [], "discretionary_events": [], "grocery_additions": [],
        "selected_recipes": [
            {"title": "Chicken Rice Bowl", "action": "add"},
            {"title": "Flank Steak Fajitas", "action": "add"},
        ],
        "target_meals": 7,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "plan 7 dinners, chicken rice bowl and flank steak fajitas"})
    finally:
        appmod.parse_copilot_prompt = orig

    d = resp.get_json() or {}
    at = d.get("actions_taken", {})
    _assert_eq(len(at.get("recipes_added", [])), 2, "2 requested recipes added")
    _assert_eq(at.get("target_meals"), 7, "target_meals echoed")
    filled = at.get("recipes_auto_filled", [])
    _assert_eq(len(filled), 5, "5 auto-filled to reach target of 7")
    with app.app_context():
        _assert_eq(MealPlanItem.query.count(), 7, "meal plan has exactly 7 recipes")
        sources = {m.source for m in MealPlanItem.query.all()}
        _assert_truthy("autofill" in sources, "auto-fill rows tagged source=autofill")


def test_autofill_never_readds_removed_recipe():
    _setup()
    _seed_recipes()
    import app as appmod

    # User asks to remove the only chicken recipe and plan 5 dinners. The
    # removed recipe must NOT be re-added by the auto-filler — even if it
    # was never in the meal plan, the removed id is tracked in `removed_ids`
    # and excluded from the auto-fill candidate set.
    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "bill_updates": [], "discretionary_events": [], "grocery_additions": [],
        "selected_recipes": [
            {"title": "Chicken Rice Bowl", "action": "remove"},
        ],
        "target_meals": 5,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "remove chicken rice bowl, plan 5 dinners"})
    finally:
        appmod.parse_copilot_prompt = orig

    d = resp.get_json() or {}
    at = d.get("actions_taken", {})
    # The recipe was never IN the meal plan, so `recipes_removed` is empty
    # (the dispatch only emits it when the recipe was actually in the plan).
    # That's correct — nothing was removed.  The key check is the exclusion.
    with app.app_context():
        chicken_id = Recipe.query.filter_by(title="Chicken Rice Bowl").first().id
        plan_ids = {m.recipe_id for m in MealPlanItem.query.all()}
        _assert_truthy(chicken_id not in plan_ids, "removed recipe NOT re-added by auto-fill")
        _assert_eq(len(plan_ids), 5, "5 auto-filled (none is the removed one)")


def test_autofill_detects_target_from_regex():
    _setup()
    _seed_recipes()
    import app as appmod

    # No LLM target_meals — the dispatch regex should detect "5 meals".
    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda text, **kw: {
        "bill_updates": [], "discretionary_events": [], "grocery_additions": [],
        "selected_recipes": [], "target_meals": None,
    }
    try:
        resp = client.post("/api/copilot/parse", json={"text": "plan 5 meals this week"})
    finally:
        appmod.parse_copilot_prompt = orig

    d = resp.get_json() or {}
    at = d.get("actions_taken", {})
    _assert_eq(at.get("target_meals"), 5, "regex detected target_meals=5")
    with app.app_context():
        _assert_eq(MealPlanItem.query.count(), 5, "5 auto-filled from regex target")


# ---------------------------------------------------------------------------
# 4 — /api/meal-plan endpoints (replace + incremental + clear)
# ---------------------------------------------------------------------------

def test_meal_plan_replace_and_incremental():
    _setup()
    _seed_recipes()
    with app.app_context():
        ids = [r.id for r in Recipe.query.order_by(Recipe.id).limit(3).all()]

    # Replace semantics
    resp = client.post("/api/meal-plan", json={"recipe_ids": ids})
    d = resp.get_json() or {}
    _assert_eq(d.get("count"), 3, "replace sets 3 recipes")
    _assert_eq(d.get("max"), 14, "max is 14")

    # Incremental add
    with app.app_context():
        extra = Recipe.query.filter(~Recipe.id.in_(ids)).first().id
    resp = client.post("/api/meal-plan", json={"add": [extra]})
    d = resp.get_json() or {}
    _assert_eq(d.get("count"), 4, "add increments to 4")

    # Incremental remove
    resp = client.post("/api/meal-plan", json={"remove": [ids[0]]})
    d = resp.get_json() or {}
    _assert_eq(d.get("count"), 3, "remove decrements to 3")
    _assert_truthy(ids[0] not in d.get("recipe_ids", []), "removed id gone")

    # Clear
    resp = client.post("/api/meal-plan/clear")
    d = resp.get_json() or {}
    _assert_eq(d.get("count"), 0, "clear empties the plan")


def test_meal_plan_cap_at_14():
    _setup()
    _seed_recipes()
    with app.app_context():
        all_ids = [r.id for r in Recipe.query.all()]
        # Only 7 seeded — seed more to exceed the cap.
        for i in range(10):
            r = Recipe(title=f"Extra Recipe {i}", servings=2, recipe_scope=Recipe.SCOPE_CANONICAL)
            db.session.add(r)
        db.session.commit()
        all_ids = [r.id for r in Recipe.query.all()]

    resp = client.post("/api/meal-plan", json={"recipe_ids": all_ids})
    d = resp.get_json() or {}
    _assert_eq(d.get("count"), 14, "plan capped at 14")
    _assert_eq(len(d.get("recipe_ids", [])), 14, "recipe_ids capped")


# ---------------------------------------------------------------------------
# 5 — parser now extracts target_meals (LLM prompt + regex fallback)
# ---------------------------------------------------------------------------

def _pop_env_key():
    """Hide any GROQ_API_KEY so the parser takes the pure-regex path."""
    return os.environ.pop("GROQ_API_KEY", None)


def test_parser_regex_target_meals():
    saved = _pop_env_key()
    try:
        result = parse_copilot_prompt("plan 7 dinners this week", groq_api_key="")
    finally:
        if saved is not None:
            os.environ["GROQ_API_KEY"] = saved
    _assert_eq(result.get("target_meals"), 7, "regex fallback detects target_meals")


def test_parser_no_false_target():
    saved = _pop_env_key()
    try:
        result = parse_copilot_prompt("cook chicken rice bowl for dinner", groq_api_key="")
    finally:
        if saved is not None:
            os.environ["GROQ_API_KEY"] = saved
    _assert_eq(result.get("target_meals"), None, "no digit-meal phrase -> None")


# ===========================================================================
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"{fn.__name__} ...")
        try:
            fn()
        except Exception as exc:
            _fail += 1
            print(f"  ERROR: {exc}")
    print(f"\n{_pass} passed, {_fail} failed")
    sys.exit(1 if _fail else 0)
