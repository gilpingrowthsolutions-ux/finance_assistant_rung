from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

os.environ["RUNG_DB_PATH"] = ":memory:"
os.environ.pop("GROQ_API_KEY", None)

from app import app
from extensions import db
from models import Account, ActionAudit, GroceryItem
from services.household_context import household_id as current_household_id

app.testing = True
client = app.test_client()


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1000.0, kroger_store_name="Walmart"))
        db.session.commit()


def _stage(text: str) -> dict:
    response = client.post("/api/copilot/stage", json={"text": text, "user_id": "apply-test"})
    assert response.status_code == 200
    body = response.get_json() or {}
    meta = (body.get("parsed") or {}).get("_parse_meta") or {}
    assert meta.get("path") == "deterministic"
    assert meta.get("llm_calls") == 0
    return body.get("actions_taken") or {}


def _apply(staged: dict):
    return client.post(
        "/api/copilot/apply",
        json={"staged_actions": staged, "text": "apply staged shopping", "user_id": "apply-test"},
    )


def test_one_shopping_item_applies_with_structured_requirement() -> None:
    _setup()
    response = _apply(_stage("Add shampoo."))

    assert response.status_code == 200
    body = response.get_json() or {}
    assert len((body.get("actions_taken") or {}).get("grocery_items_added") or []) == 1
    with app.app_context():
        row = GroceryItem.query.one()
        requirement = json.loads(row.shopping_requirement_json or "{}")
        assert requirement["base_item"] == "shampoo"
        assert requirement["quantity"] == 1.0


def test_five_shopping_items_apply_and_response_matches_writes() -> None:
    _setup()
    staged = _stage("Add milk, eggs, bread, bananas, and peanut butter to my grocery list.")
    response = _apply(staged)

    assert response.status_code == 200
    applied = ((response.get_json() or {}).get("actions_taken") or {}).get("grocery_items_added") or []
    assert len(applied) == 5
    with app.app_context():
        assert GroceryItem.query.count() == 5
        assert GroceryItem.query.filter(GroceryItem.shopping_requirement_json.isnot(None)).count() == 5
        assert [json.loads(row.shopping_requirement_json)["base_item"] for row in GroceryItem.query.order_by(GroceryItem.id)] == [
            "milk", "eggs", "bread", "bananas", "peanut butter",
        ]


def test_five_non_food_items_apply() -> None:
    _setup()
    response = _apply(_stage(
        "Add dish soap, laundry detergent, shampoo, toothpaste, and toilet paper to my shopping list."
    ))

    assert response.status_code == 200
    with app.app_context():
        assert GroceryItem.query.count() == 5
        assert [row.item_name for row in GroceryItem.query.order_by(GroceryItem.id)] == [
            "Dish Soap", "Laundry Detergent", "Shampoo", "Toothpaste", "Toilet Paper",
        ]


def test_specific_product_structure_survives_apply() -> None:
    _setup()
    response = _apply(_stage("Add Jif creamy peanut butter."))

    assert response.status_code == 200
    with app.app_context():
        requirement = json.loads(GroceryItem.query.one().shopping_requirement_json or "{}")
        assert requirement["base_item"] == "peanut butter"
        assert requirement["brand"] == "Jif"
        assert requirement["variant"] == "creamy"
        assert requirement["quantity"] == 1.0
        assert requirement["unit"] is None
        assert requirement["requested_package_size"] is None


def test_apply_is_idempotent() -> None:
    _setup()
    staged = _stage("Add milk, eggs, bread, bananas, and peanut butter to my grocery list.")
    first = _apply(staged)
    second = _apply(staged)

    assert first.status_code == 200
    assert second.status_code == 200
    assert ((second.get_json() or {}).get("actions_taken") or {}).get("already_applied") is True
    with app.app_context():
        assert GroceryItem.query.count() == 5
        assert ActionAudit.query.filter_by(operation_id=staged["operation_id"]).count() == 1


def test_invalid_staged_action_returns_400_without_partial_writes() -> None:
    _setup()
    staged = _stage("Add shampoo and toothpaste.")
    staged["grocery_items_added"].append({"item_name": "", "quantity": 1})
    response = _apply(staged)

    assert response.status_code == 400
    body = response.get_json() or {}
    assert (body.get("validation") or {}).get("code") == "invalid_grocery_action_payload"
    with app.app_context():
        assert GroceryItem.query.count() == 0
        assert ActionAudit.query.count() == 0


def test_commit_failure_rolls_back_all_shopping_rows() -> None:
    _setup()
    staged = _stage("Add shampoo and toothpaste.")
    with patch.object(db.session, "commit", side_effect=RuntimeError("forced commit failure")):
        with pytest.raises(RuntimeError, match="forced commit failure"):
            with app.app_context():
                from services.copilot_intent import apply_staged_actions
                apply_staged_actions(staged, user_id="apply-test")

    with app.app_context():
        assert GroceryItem.query.count() == 0
        assert ActionAudit.query.count() == 0
