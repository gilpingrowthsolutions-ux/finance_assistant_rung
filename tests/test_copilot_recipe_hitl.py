#!/usr/bin/env python3
"""Recipe-focused HITL tests for staging/apply resolved vs unresolved behavior."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import (  # noqa: E402
    app,
    db,
    Account,
    Recipe,
    RecipeIngredient,
    MealPlanItem,
    Bill,
    ExpenseTransaction,
    ActionAudit,
)
from services.household_context import household_id as current_household_id

app.testing = True
client = app.test_client()

_pass = 0
_fail = 0


def _ok(msg: str) -> None:
    global _pass
    _pass += 1
    print("  PASS:", msg)


def _bad(msg: str, expected=None, actual=None) -> None:
    global _fail
    _fail += 1
    print("  FAIL:", msg)
    if expected is not None:
        print("    expected:", repr(expected))
    if actual is not None:
        print("    actual:  ", repr(actual))


def _check(cond: bool, msg: str, expected=None, actual=None) -> None:
    if cond:
        _ok(msg)
    else:
        _bad(msg, expected, actual)


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1200.0, pay_period_days=14, meals_per_day=3, kroger_store_name="Walmart"))
        db.session.commit()


def _seed_recipes() -> dict[str, int]:
    rows = [
        ("Chicken Rice Bowl", [("Chicken Breast", "chicken", 1.0), ("Rice", "rice", 1.0)]),
        ("Flank Steak Fajitas", [("Steak", "steak", 1.0), ("Tortilla", "tortilla", 1.0)]),
    ]
    ids: dict[str, int] = {}
    with app.app_context():
        for title, ingredients in rows:
            r = Recipe(title=title, servings=4, estimated_cost_per_serving=3.5, recipe_scope=Recipe.SCOPE_CANONICAL)
            db.session.add(r)
            db.session.flush()
            ids[title] = r.id
            for product_name, kw, qty in ingredients:
                db.session.add(
                    RecipeIngredient(
                        recipe_id=r.id,
                        product_name=product_name,
                        clean_keyword=kw,
                        quantity=qty,
                        unit="item",
                    )
                )
        db.session.commit()
    return ids


def _parsed_for(selected: list[dict], bills=None, expenses=None) -> dict:
    return {
        "tool_results": [],
        "selected_recipes": selected,
        "grocery_additions": [],
        "discretionary_events": expenses or [],
        "bill_updates": bills or [],
        "target_meals": None,
        "meal_servings": 4,
        "_fallback": False,
    }


def _stage_with_parsed(parsed: dict, text: str = "plan meals") -> dict:
    import app as appmod

    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda _text, **kw: parsed
    try:
        resp = client.post("/api/copilot/stage", json={"text": text, "user_id": "recipe-hitl"})
    finally:
        appmod.parse_copilot_prompt = orig

    _check(resp.status_code == 200, "stage returns 200", 200, resp.status_code)
    return (resp.get_json() or {}).get("actions_taken") or {}


def _apply(staged_actions: dict, text: str = "apply"):
    return client.post(
        "/api/copilot/apply",
        json={"text": text, "staged_actions": staged_actions, "user_id": "recipe-hitl"},
    )


def test_a_exact_existing_recipe_resolves_and_applies() -> None:
    print("A. exact existing recipe resolves to ID and apply succeeds")
    _setup()
    ids = _seed_recipes()

    staged = _stage_with_parsed(_parsed_for([{"title": "Chicken Rice Bowl", "action": "add"}]), text="add chicken rice bowl")
    added = staged.get("recipes_added") or []
    _check(len(added) == 1, "stage has one resolved recipe")
    _check(isinstance(added[0].get("id"), int), "resolved recipe includes stable ID")
    _check(added[0].get("id") == ids["Chicken Rice Bowl"], "resolved ID matches recipe")
    _check(len(staged.get("recipes_suggested") or []) == 0, "no unresolved recipe actions for exact match")

    resp = _apply(staged, text="add chicken rice bowl")
    _check(resp.status_code == 200, "apply succeeds", 200, resp.status_code)
    with app.app_context():
        _check(MealPlanItem.query.count() == 1, "meal plan row created")


def test_b_reasonable_existing_recipe_match_resolves() -> None:
    print("\nB. approximate existing recipe match resolves")
    _setup()
    ids = _seed_recipes()

    staged = _stage_with_parsed(_parsed_for([{"title": "chicken bowl", "action": "add"}]), text="add chicken bowl")
    added = staged.get("recipes_added") or []
    _check(len(added) == 1, "approximate match resolved to one recipe")
    _check(added[0].get("id") == ids["Chicken Rice Bowl"], "approximate match resolved to expected recipe ID")


def test_c_unknown_recipe_staged_as_unresolved() -> None:
    print("\nC. unknown recipe is staged as unresolved")
    _setup()
    _seed_recipes()

    staged = _stage_with_parsed(_parsed_for([{"title": "Sushi Rolls", "action": "add"}]), text="add sushi rolls")
    unresolved = staged.get("recipes_suggested") or []
    _check(len(unresolved) == 1, "one unresolved recipe action is staged")
    row = unresolved[0]
    _check(row.get("status") == "unresolved", "unresolved status is explicit", "unresolved", row.get("status"))
    _check(row.get("reason") == "recipe_not_found", "unresolved reason is explicit", "recipe_not_found", row.get("reason"))
    _check(row.get("requested_title") == "Sushi Rolls", "requested title preserved")


def test_d_e_unresolved_apply_rejected_and_not_marked_success() -> None:
    print("\nD/E. unresolved recipe cannot silently apply and operation is not marked successful")
    _setup()
    _seed_recipes()

    staged = _stage_with_parsed(_parsed_for([{"title": "Sushi Rolls", "action": "add"}]), text="add sushi rolls")
    op_id = staged.get("operation_id")
    resp = _apply(staged, text="add sushi rolls")
    body = resp.get_json() or {}
    _check(resp.status_code == 400, "apply rejects unresolved recipe", 400, resp.status_code)
    _check(((body.get("validation") or {}).get("code") == "unresolved_recipe_actions"), "structured unresolved validation code returned")
    unresolved = ((body.get("validation") or {}).get("unresolved_recipe_actions") or [])
    _check(len(unresolved) == 1, "validation identifies unresolved recipe action")

    with app.app_context():
        _check(MealPlanItem.query.count() == 0, "no meal plan side effects on unresolved apply")
        _check(ActionAudit.query.filter_by(operation_id=op_id).count() == 0, "unresolved apply is not marked successfully applied")


def test_f_reject_unresolved_allows_remaining_actions() -> None:
    print("\nF. rejecting unresolved recipe allows other valid staged actions")
    _setup()
    _seed_recipes()

    staged = _stage_with_parsed(
        _parsed_for(
            [{"title": "Sushi Rolls", "action": "add"}],
            bills=[{"name": "Electric", "amount": 80.0, "action": "add"}],
            expenses=[{"description": "gas", "amount": 20.0}],
        ),
        text="add sushi rolls, electric bill, and gas",
    )
    staged["recipes_suggested"][0]["decision"] = "reject"

    resp = _apply(staged, text="apply with rejected recipe")
    _check(resp.status_code == 200, "apply succeeds after unresolved recipe rejection", 200, resp.status_code)
    with app.app_context():
        _check(Bill.query.count() == 1, "bill side effect applied")
        _check(ExpenseTransaction.query.count() == 1, "expense side effect applied")


def test_g_substitute_existing_recipe_id_applies() -> None:
    print("\nG. substituting unresolved recipe with existing recipe ID applies")
    _setup()
    ids = _seed_recipes()

    staged = _stage_with_parsed(_parsed_for([{"title": "Sushi Rolls", "action": "add"}]), text="add sushi rolls")
    staged["recipes_suggested"][0]["substitute_recipe_id"] = ids["Flank Steak Fajitas"]

    resp = _apply(staged, text="substitute recipe")
    _check(resp.status_code == 200, "apply succeeds after substitution", 200, resp.status_code)

    with app.app_context():
        rows = MealPlanItem.query.all()
        row_ids = {r.recipe_id for r in rows}
        _check(len(rows) >= 1, "at least one meal plan row created")
        _check(ids["Flank Steak Fajitas"] in row_ids, "substituted recipe ID persisted")


def test_h_invalid_tampered_recipe_id_rejected() -> None:
    print("\nH. tampered substitute recipe ID is rejected")
    _setup()
    _seed_recipes()

    staged = _stage_with_parsed(_parsed_for([{"title": "Sushi Rolls", "action": "add"}]), text="add sushi rolls")
    staged["recipes_suggested"][0]["substitute_recipe_id"] = 999999

    resp = _apply(staged, text="invalid substitution")
    body = resp.get_json() or {}
    _check(resp.status_code == 400, "invalid recipe ID rejected", 400, resp.status_code)
    _check(((body.get("validation") or {}).get("code") == "invalid_recipe_id"), "structured invalid_recipe_id validation code returned")


def test_i_multi_action_unresolved_recipe_blocks_whole_apply() -> None:
    print("\nI. unresolved recipe with valid bill+expense blocks whole staged apply")
    _setup()
    _seed_recipes()

    staged = _stage_with_parsed(
        _parsed_for(
            [{"title": "Sushi Rolls", "action": "add"}],
            bills=[{"name": "Electric", "amount": 80.0, "action": "add"}],
            expenses=[{"description": "gas", "amount": 20.0}],
        ),
        text="multi-action unresolved sushi recipe",
    )

    resp = _apply(staged, text="multi-action apply")
    _check(resp.status_code == 400, "apply rejected when unresolved recipe remains", 400, resp.status_code)

    with app.app_context():
        _check(Bill.query.count() == 0, "bill not applied when recipe unresolved")
        _check(ExpenseTransaction.query.count() == 0, "expense not applied when recipe unresolved")
        _check(MealPlanItem.query.count() == 0, "meal plan unchanged when recipe unresolved")


def test_j_idempotency_regression_resolved_recipe_apply_twice() -> None:
    print("\nJ. resolved recipe operation applied twice remains idempotent")
    _setup()
    _seed_recipes()

    staged = _stage_with_parsed(_parsed_for([{"title": "Chicken Rice Bowl", "action": "add"}]), text="add chicken rice bowl")
    r1 = _apply(staged, text="apply once")
    r2 = _apply(staged, text="apply twice")
    b1 = r1.get_json() or {}
    b2 = r2.get_json() or {}

    _check(r1.status_code == 200 and r2.status_code == 200, "both apply calls succeed")
    _check((b2.get("actions_taken") or {}).get("already_applied") is True, "second apply reports already_applied")
    _check((b1.get("undo_token") or "") == (b2.get("undo_token") or ""), "second apply reuses original undo token")

    with app.app_context():
        _check(MealPlanItem.query.count() == 1, "no duplicate meal plan rows")


def _main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()

    print("\n" + "=" * 60)
    print(f"{_pass} passed, {_fail} failed")
    raise SystemExit(1 if _fail else 0)


if __name__ == "__main__":
    _main()
