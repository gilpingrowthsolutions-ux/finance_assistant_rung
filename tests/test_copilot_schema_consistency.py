from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["RUNG_DB_PATH"] = ":memory:"

from app import (  # noqa: E402
    app,
    db,
    Account,
    ActionAudit,
    Bill,
    ExpenseTransaction,
    GroceryItem,
    MealPlanItem,
    Recipe,
    RecipeIngredient,
)
from services.household_context import household_id as current_household_id

app.testing = True
client = app.test_client()


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1500.0, pay_period_days=14, meals_per_day=3, kroger_store_name="Walmart"))
        db.session.commit()


def _seed_recipes() -> dict[str, int]:
    rows = [
        ("Chicken Rice Bowl", [("Chicken Breast", "chicken", 1.0), ("Rice", "rice", 1.0)]),
        ("Flank Steak Fajitas", [("Steak", "steak", 1.0), ("Tortilla", "tortilla", 1.0)]),
    ]
    out: dict[str, int] = {}
    with app.app_context():
        for title, ingredients in rows:
            rec = Recipe(title=title, servings=4, estimated_cost_per_serving=3.5)
            db.session.add(rec)
            db.session.flush()
            out[title] = rec.id
            for product_name, kw, qty in ingredients:
                db.session.add(
                    RecipeIngredient(
                        recipe_id=rec.id,
                        product_name=product_name,
                        clean_keyword=kw,
                        quantity=qty,
                        unit="item",
                    )
                )
        db.session.commit()
    return out


def _parsed(selected=None, groceries=None, expenses=None, bills=None, target_meals=None) -> dict:
    return {
        "tool_results": [],
        "selected_recipes": selected or [],
        "grocery_additions": groceries or [],
        "discretionary_events": expenses or [],
        "bill_updates": bills or [],
        "target_meals": target_meals,
        "meal_servings": 4,
        "_fallback": False,
    }


def _stage(parsed: dict, text: str = "stage") -> dict:
    import app as appmod

    orig = appmod.parse_copilot_prompt
    appmod.parse_copilot_prompt = lambda _text, **kw: parsed
    try:
        resp = client.post("/api/copilot/stage", json={"text": text, "user_id": "schema-user"})
    finally:
        appmod.parse_copilot_prompt = orig
    assert resp.status_code == 200
    return (resp.get_json() or {}).get("actions_taken") or {}


def _apply(staged_actions: dict, text: str = "apply"):
    return client.post(
        "/api/copilot/apply",
        json={"text": text, "staged_actions": staged_actions, "user_id": "schema-user"},
    )


def _undo(undo_token: str):
    return client.post("/api/copilot/undo", json={"undo_token": undo_token, "user_id": "schema-user"})


def _last_audit_by_operation(operation_id: str) -> ActionAudit | None:
    with app.app_context():
        return ActionAudit.query.filter_by(operation_id=operation_id).first()


def test_a_bill_action_lifecycle() -> None:
    _setup()
    staged = _stage(_parsed(bills=[{"name": "Internet", "amount": 65, "action": "set"}]))
    staged["bills_added"][0]["amount"] = 70.0

    applied = _apply(staged)
    assert applied.status_code == 200
    body = applied.get_json() or {}
    undo_token = body.get("undo_token")
    assert undo_token

    op_id = staged["operation_id"]
    audit = _last_audit_by_operation(op_id)
    payload = json.loads((audit.actions_json if audit else "{}") or "{}")
    assert payload["bills_added"][0]["id"]
    assert payload["bills_added"][0]["amount"] == 70.0

    undone = _undo(undo_token)
    assert undone.status_code == 200
    with app.app_context():
        assert Bill.query.count() == 0


def test_a2_bill_due_date_survives_stage_apply_db_audit() -> None:
    _setup()
    staged = _stage(_parsed(bills=[{"name": "Electric", "amount": 150, "action": "add", "due_day": 15}]))

    staged_bill = (staged.get("bills_added") or [])[0]
    assert staged_bill.get("due_date")

    applied = _apply(staged, text="apply explicit due date")
    assert applied.status_code == 200

    op_id = staged["operation_id"]
    audit = _last_audit_by_operation(op_id)
    payload = json.loads((audit.actions_json if audit else "{}") or "{}")
    audit_bill = (payload.get("bills_added") or [])[0]

    with app.app_context():
        db_bill = Bill.query.order_by(Bill.id.desc()).first()

    assert isinstance(db_bill, Bill)
    assert db_bill is not None and db_bill.due_date is not None
    assert audit_bill.get("due_date") == staged_bill.get("due_date")
    assert db_bill.due_date.isoformat() == staged_bill.get("due_date")


def test_b_expense_lifecycle() -> None:
    _setup()
    staged = _stage(_parsed(expenses=[{"description": "fuel", "amount": 20.0}]))
    staged["expenses_logged"][0]["amount"] = 22.5
    staged["expenses_logged"][0]["category"] = "gas"

    applied = _apply(staged)
    assert applied.status_code == 200
    body = applied.get_json() or {}
    undo_token = body.get("undo_token")
    assert undo_token

    op_id = staged["operation_id"]
    audit = _last_audit_by_operation(op_id)
    payload = json.loads((audit.actions_json if audit else "{}") or "{}")
    expense = payload["expenses_logged"][0]
    assert isinstance(expense.get("id"), int)
    assert expense["category"] == "gas"
    assert expense["amount"] == 22.5

    undone = _undo(undo_token)
    assert undone.status_code == 200
    with app.app_context():
        assert ExpenseTransaction.query.count() == 0


def test_c_grocery_lifecycle() -> None:
    _setup()
    staged = _stage(_parsed(groceries=["paper towels"]))
    staged["grocery_items_added"][0]["estimated_price"] = 4.25

    applied = _apply(staged)
    assert applied.status_code == 200
    body = applied.get_json() or {}
    undo_token = body.get("undo_token")
    assert undo_token

    op_id = staged["operation_id"]
    audit = _last_audit_by_operation(op_id)
    payload = json.loads((audit.actions_json if audit else "{}") or "{}")
    grocery = payload["grocery_items_added"][0]
    assert isinstance(grocery.get("id"), int)
    assert grocery["item_name"] == "Paper Towels"
    assert grocery["estimated_price"] == 4.25

    undone = _undo(undo_token)
    assert undone.status_code == 200
    with app.app_context():
        assert GroceryItem.query.count() == 0


def test_d_resolved_recipe_lifecycle() -> None:
    _setup()
    _seed_recipes()
    staged = _stage(_parsed(selected=[{"title": "Chicken Rice Bowl", "action": "add"}], target_meals=1))

    applied = _apply(staged)
    assert applied.status_code == 200
    undo_token = (applied.get_json() or {}).get("undo_token")
    assert undo_token

    with app.app_context():
        assert MealPlanItem.query.count() == 1

    undone = _undo(undo_token)
    assert undone.status_code == 200
    with app.app_context():
        assert MealPlanItem.query.count() == 0


def test_e_rejected_unresolved_recipe_lifecycle() -> None:
    _setup()
    _seed_recipes()
    staged = _stage(
        _parsed(
            selected=[{"title": "Sushi Rolls", "action": "add"}],
            bills=[{"name": "Internet", "amount": 60.0, "action": "set"}],
        )
    )
    staged["recipes_suggested"][0]["decision"] = "reject"

    applied = _apply(staged)
    assert applied.status_code == 200
    undo_token = (applied.get_json() or {}).get("undo_token")
    assert undo_token

    with app.app_context():
        assert Bill.query.count() == 1

    op_id = staged["operation_id"]
    audit = _last_audit_by_operation(op_id)
    payload = json.loads((audit.actions_json if audit else "{}") or "{}")
    assert payload["recipes_rejected"]


def test_f_substituted_recipe_lifecycle() -> None:
    _setup()
    ids = _seed_recipes()
    staged = _stage(_parsed(selected=[{"title": "Sushi Rolls", "action": "add"}], target_meals=1))
    staged["recipes_suggested"][0]["substitute_recipe_id"] = ids["Flank Steak Fajitas"]

    applied = _apply(staged)
    assert applied.status_code == 200
    body = applied.get_json() or {}
    row = (body.get("actions_taken") or {}).get("recipes_added", [])[0]
    assert row["id"] == ids["Flank Steak Fajitas"]
    assert row["resolution"] == "substituted"


def test_g_mixed_multi_action_operation() -> None:
    _setup()
    _seed_recipes()
    staged = _stage(
        _parsed(
            selected=[{"title": "Chicken Rice Bowl", "action": "add"}],
            groceries=["dish soap"],
            expenses=[{"description": "gas", "amount": 18.0}],
            bills=[{"name": "Internet", "amount": 65.0, "action": "set"}],
            target_meals=1,
        )
    )

    applied = _apply(staged)
    assert applied.status_code == 200
    undo_token = (applied.get_json() or {}).get("undo_token")
    assert undo_token

    with app.app_context():
        assert Bill.query.count() == 1
        assert ExpenseTransaction.query.count() == 1
        assert GroceryItem.query.count() >= 1
        assert MealPlanItem.query.count() == 1

    undone = _undo(undo_token)
    assert undone.status_code == 200
    with app.app_context():
        assert Bill.query.count() == 0
        assert ExpenseTransaction.query.count() == 0
        assert GroceryItem.query.count() == 0
        assert MealPlanItem.query.count() == 0


def test_h_malformed_grocery_returns_structured_error_and_no_writes() -> None:
    _setup()
    staged = _stage(_parsed(groceries=["dish soap"]))
    staged["grocery_items_added"][0]["estimated_price"] = "not-a-number"

    resp = _apply(staged)
    body = resp.get_json() or {}
    assert resp.status_code == 400
    assert ((body.get("validation") or {}).get("code") == "invalid_grocery_action_payload")

    with app.app_context():
        assert Bill.query.count() == 0
        assert ExpenseTransaction.query.count() == 0
        assert GroceryItem.query.count() == 0
        assert ActionAudit.query.filter_by(operation_id=staged["operation_id"]).count() == 0


def test_i_malformed_expense_returns_structured_error_and_no_writes() -> None:
    _setup()
    staged = _stage(_parsed(expenses=[{"description": "gas", "amount": 20.0}]))
    staged["expenses_logged"][0]["amount"] = "oops"

    resp = _apply(staged)
    body = resp.get_json() or {}
    assert resp.status_code == 400
    assert ((body.get("validation") or {}).get("code") == "invalid_expense_action_payload")

    with app.app_context():
        assert Bill.query.count() == 0
        assert ExpenseTransaction.query.count() == 0
        assert GroceryItem.query.count() == 0
        assert ActionAudit.query.filter_by(operation_id=staged["operation_id"]).count() == 0


def test_j_frontend_edit_preserves_backend_required_fields() -> None:
    _setup()
    _seed_recipes()
    staged = _stage(_parsed(selected=[{"title": "Chicken Rice Bowl", "action": "add"}], target_meals=1))

    original_id = staged["recipes_added"][0]["id"]
    staged["recipes_added"][0]["title"] = "Reviewer Label"
    assert staged["recipes_added"][0]["id"] == original_id

    resp = _apply(staged)
    assert resp.status_code == 200
    with app.app_context():
        assert MealPlanItem.query.filter_by(recipe_id=original_id).count() == 1


def test_k_audit_contains_canonical_structured_data() -> None:
    _setup()
    _seed_recipes()
    staged = _stage(
        _parsed(
            selected=[{"title": "Chicken Rice Bowl", "action": "add"}],
            groceries=["paper towels"],
            expenses=[{"description": "gas", "amount": 30.0}],
            bills=[{"name": "Internet", "amount": 60.0, "action": "set"}],
            target_meals=1,
        )
    )
    resp = _apply(staged)
    assert resp.status_code == 200

    audit = _last_audit_by_operation(staged["operation_id"])
    payload = json.loads((audit.actions_json if audit else "{}") or "{}")
    assert isinstance(payload["grocery_items_added"][0], dict)
    assert isinstance(payload["grocery_items_added"][0].get("id"), int)
    assert isinstance(payload["expenses_logged"][0], dict)
    assert isinstance(payload["expenses_logged"][0].get("id"), int)
    assert isinstance(payload["bills_added"][0], dict)
    assert isinstance(payload["bills_added"][0].get("id"), int)


def test_l_undo_consumes_canonical_audit() -> None:
    _setup()
    _seed_recipes()
    staged = _stage(
        _parsed(
            selected=[{"title": "Chicken Rice Bowl", "action": "add"}],
            groceries=["paper towels"],
            expenses=[{"description": "gas", "amount": 30.0}],
            bills=[{"name": "Internet", "amount": 60.0, "action": "set"}],
            target_meals=1,
        )
    )
    resp = _apply(staged)
    undo_token = (resp.get_json() or {}).get("undo_token")
    assert undo_token

    undo = _undo(undo_token)
    assert undo.status_code == 200
    with app.app_context():
        assert Bill.query.count() == 0
        assert ExpenseTransaction.query.count() == 0
        assert GroceryItem.query.count() == 0
        assert MealPlanItem.query.count() == 0


def test_m_legacy_audit_payload_still_undoes() -> None:
    _setup()
    ids = _seed_recipes()

    with app.app_context():
        bill = Bill(household_id=current_household_id(), name="Legacy Bill", amount=40.0, due_date=datetime.utcnow())
        expense = ExpenseTransaction(household_id=current_household_id(), description="Legacy Gas", amount=33.0, category="gas")
        grocery = GroceryItem(household_id=current_household_id(), item_name="Legacy Soap", estimated_price=3.0, store_name="Legacy")
        plan = MealPlanItem(household_id=current_household_id(), recipe_id=ids["Chicken Rice Bowl"], source="copilot")
        db.session.add_all([bill, expense, grocery, plan])
        db.session.flush()

        legacy_actions = {
            "bills_added": [{"name": "Legacy Bill", "amount": 40.0}],
            "expenses_logged": [{"description": "Legacy Gas", "amount": 33.0}],
            "grocery_items_added": ["Legacy Soap"],
            "recipes_added": [{"id": ids["Chicken Rice Bowl"], "title": "Chicken Rice Bowl"}],
        }
        audit = ActionAudit(
            household_id=current_household_id(),
            source="copilot_staged_apply",
            user_id="legacy-user",
            raw_text="legacy",
            actions_json=json.dumps(legacy_actions),
            undo_token="legacy-undo-token",
        )
        db.session.add(audit)
        db.session.commit()

    undo = _undo("legacy-undo-token")
    assert undo.status_code == 200
    with app.app_context():
        assert Bill.query.filter(Bill.name.ilike("%Legacy Bill%")).count() == 0
        assert ExpenseTransaction.query.filter(ExpenseTransaction.description.ilike("%Legacy Gas%")).count() == 0
        assert GroceryItem.query.filter(GroceryItem.item_name.ilike("%Legacy Soap%")).count() == 0
        assert MealPlanItem.query.filter_by(recipe_id=ids["Chicken Rice Bowl"]).count() == 0


def test_n_edit_before_first_apply_succeeds() -> None:
    _setup()
    staged = _stage(_parsed(bills=[{"name": "Internet", "amount": 60.0, "action": "set"}]))
    staged["bills_added"][0]["amount"] = 66.0

    resp = _apply(staged)
    assert resp.status_code == 200


def test_o_retry_exact_edited_payload_is_already_applied() -> None:
    _setup()
    staged = _stage(_parsed(bills=[{"name": "Internet", "amount": 60.0, "action": "set"}]))
    staged["bills_added"][0]["amount"] = 66.0

    r1 = _apply(staged)
    r2 = _apply(staged)
    assert r1.status_code == 200
    assert r2.status_code == 200
    body2 = r2.get_json() or {}
    assert ((body2.get("actions_taken") or {}).get("already_applied") is True)

    with app.app_context():
        assert Bill.query.count() == 1


def test_p_changed_payload_after_successful_apply_conflicts_for_same_operation_id() -> None:
    _setup()
    staged = _stage(_parsed(bills=[{"name": "Internet", "amount": 60.0, "action": "set"}]))

    first = _apply(staged)
    assert first.status_code == 200

    staged["bills_added"][0]["amount"] = 99.0
    second = _apply(staged)
    body = second.get_json() or {}

    assert second.status_code == 400
    assert "operation_id was already used with different staged content" in (body.get("error") or "")

    with app.app_context():
        assert Bill.query.count() == 1
