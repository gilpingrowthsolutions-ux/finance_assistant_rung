"""
Rung App Tools — JSON schemas + executable Python functions for Groq Tool Calling.
==================================================================================

Each tool has a JSON schema (sent to the LLM so it knows what arguments to produce)
and a corresponding Python function that performs the actual database operation.

The tool definitions are exported as ``APP_TOOLS`` — a list of dicts that gets
passed directly to ``groq_client.chat.completions.create(tools=APP_TOOLS)``.

Usage
-----
    from services.copilot_tools import APP_TOOLS, execute_app_function

    # In the tool-calling loop:
    result = execute_app_function(function_name, function_args)
    # result → {"status": "ok", "data": {...}} or {"status": "error", "message": "..."}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from services.household_context import household_id as current_household_id
from services.financial_state import apply_balance_delta, get_household_account
from services.selected_store import get_selected_store
from services.recipe_access import visible_recipe_query

LOGGER = logging.getLogger("copilot_tools")


def _touch_recipe_usage(recipe: Any) -> None:
    """Implicit learning hook for tool-driven recipe selections."""
    if recipe is None:
        return
    recipe.usage_frequency = int(getattr(recipe, "usage_frequency", 0) or 0) + 1
    recipe.last_selected_date = datetime.utcnow()

# ============================================================================
# 1 — ADD RECURRING BILL
# ============================================================================

ADD_BILL_TOOL = {
    "type": "function",
    "function": {
        "name": "add_recurring_bill",
        "description": "Add a new recurring bill or subscription (e.g. Netflix $22.99/mo). "
                       "The bill is persisted to the database and shown on the Bills tab.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Bill name, e.g. 'Netflix', 'Electric Bill'",
                },
                "amount": {
                    "type": "number",
                    "description": "Monthly amount in USD. Always positive.",
                },
                "frequency": {
                    "type": "string",
                    "enum": ["monthly", "weekly", "yearly", "once"],
                    "description": "How often the bill recurs. Default: monthly.",
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional ISO date (YYYY-MM-DD) for the next due date. "
                                   "If omitted, defaults to 14 days from now.",
                },
            },
            "required": ["name", "amount"],
        },
    },
}

# ============================================================================
# 2 — ADD GROCERY ITEM
# ============================================================================

ADD_GROCERY_TOOL = {
    "type": "function",
    "function": {
        "name": "add_grocery_item",
        "description": "Add a non-recipe household item to the active grocery list "
                       "(e.g. dish soap, paper towels, laundry detergent). "
                       "The item is added to the Grocery tab's cart.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Name of the item, e.g. 'dish soap', 'paper towels'",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category. Default: 'General'.",
                },
            },
            "required": ["item_name"],
        },
    },
}

# ============================================================================
# 3 — SELECT ACTIVE RECIPE
# ============================================================================

SELECT_RECIPE_TOOL = {
    "type": "function",
    "function": {
        "name": "select_active_recipe",
        "description": "Add a recipe to the active pay-period meal plan. "
                       "The recipe must already exist in the local recipe database. "
                       "If you're not sure about the exact title, match it as closely as possible. "
                       "The recipe will appear in the Grocery tab's Active Recipes expander.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipe_id_or_title": {
                    "type": "string",
                    "description": "Recipe ID (integer) or title string. "
                                   "Fuzzy matching is applied for titles.",
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "remove"],
                    "description": "'add' to include the recipe, 'remove' to exclude it.",
                },
            },
            "required": ["recipe_id_or_title", "action"],
        },
    },
}

# ============================================================================
# 4 — LOG DISCRETIONARY EXPENSE
# ============================================================================

LOG_EXPENSE_TOOL = {
    "type": "function",
    "function": {
        "name": "log_discretionary_expense",
        "description": "Log a one-time discretionary expense, e.g. dining out, "
                       "entertainment, or a shopping purchase. The amount is "
                       "deducted from the safe-to-spend checking balance.",
        "parameters": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "Description of the expense, e.g. 'Dinner at Olive Garden'",
                },
                "amount": {
                    "type": "number",
                    "description": "Total cost in USD. Always positive.",
                },
            },
            "required": ["item_name", "amount"],
        },
    },
}

# ============================================================================
# 5 — SET TARGET MEALS (auto-fill gap)
# ============================================================================

SET_TARGET_MEALS_TOOL = {
    "type": "function",
    "function": {
        "name": "set_target_meals",
        "description": "Set a target number of meals for the current pay period. "
                       "If the user asks for a specific number of meals/recipes/dinners, "
                       "call this tool to set the target so the remaining slots can be "
                       "auto-filled with recommendations from the user's saved recipes, pantry, and preferences.",
        "parameters": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of meals the user wants to plan for this pay period.",
                },
            },
            "required": ["count"],
        },
    },
}

# ============================================================================
# 6 — GET FINANCIAL OVERVIEW (live data context for chat)
# ============================================================================

GET_OVERVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "get_financial_overview",
        "description": "Get the user's current financial snapshot: checking balance, food budget, "
                       "safe disposable cash, upcoming bills, recent transactions, active meal "
                       "plan, and grocery cart. Call this BEFORE answering any question about "
                       "the user's money, budget, bills, or meal plan so answers use real data.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

# ============================================================================
# Aggregate tool list
# ============================================================================

APP_TOOLS: List[Dict[str, Any]] = [
    GET_OVERVIEW_TOOL,
    ADD_BILL_TOOL,
    ADD_GROCERY_TOOL,
    SELECT_RECIPE_TOOL,
    LOG_EXPENSE_TOOL,
    SET_TARGET_MEALS_TOOL,
]

# ============================================================================
# Executable backend functions (called by the tool-calling loop)
# ============================================================================
# These functions receive **kwargs matching the tool's JSON schema and
# perform real database operations via the Flask-SQLAlchemy session.
# They return a dict with "status" ("ok" or "error") and "data" / "message".


def _execute_add_recurring_bill(**kwargs) -> Dict[str, Any]:
    """Insert a row into the ``Bill`` table."""
    # Defer module-level imports to avoid circular dependency at load time.
    from flask import current_app
    from extensions import db
    from models import Bill, Account

    name = (kwargs.get("name") or "").strip()
    amount = float(kwargs.get("amount", 0))
    if not name or amount <= 0:
        return {"status": "error", "message": "Name and positive amount required."}
    with current_app.app_context():
        due_date_str = kwargs.get("due_date", "")
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
            except ValueError:
                due_date = datetime.utcnow() + timedelta(days=14)
        else:
            due_date = datetime.utcnow() + timedelta(days=14)
        b = Bill(household_id=current_household_id(), name=name.title(), amount=amount, due_date=due_date)
        db.session.add(b)
        db.session.commit()
        LOGGER.info("Tool add_recurring_bill: %s $%.2f", name, amount)
        return {
            "status": "ok",
            "data": {"name": name.title(), "amount": amount, "id": b.id},
        }


def _execute_add_grocery_item(**kwargs) -> Dict[str, Any]:
    """Add a row to the ``GroceryItem`` table."""
    from flask import current_app
    from extensions import db
    from models import GroceryItem, Account

    item_name = (kwargs.get("item_name") or "").strip()
    if not item_name:
        return {"status": "error", "message": "Item name required."}
    with current_app.app_context():
        hid = current_household_id()
        account = get_household_account(hid)
        selected = get_selected_store(hid, account=account)
        store = selected.get("name") or "Local Store"
        gi = GroceryItem(household_id=hid, item_name=item_name.title(), estimated_price=0.0, store_name=store)
        db.session.add(gi)
        db.session.commit()
        LOGGER.info("Tool add_grocery_item: %s", item_name)
        return {"status": "ok", "data": {"item_name": item_name.title(), "store_name": store}}


def _execute_select_active_recipe(**kwargs) -> Dict[str, Any]:
    """Add or remove a recipe from the ``MealPlanItem`` (meal plan) table."""
    from flask import current_app
    from extensions import db
    from models import Recipe
    from app import _match_recipe_by_title, _household_meal_plan_query, _new_current_meal_plan_item
    from services.recipe_access import visible_recipe_by_id, visible_recipe_query

    raw = (kwargs.get("recipe_id_or_title") or "").strip()
    action = (kwargs.get("action") or "add").lower()
    if not raw:
        return {"status": "error", "message": "recipe_id_or_title required."}

    with current_app.app_context():
        from app import _current_meal_plan_cycle
        if not _current_meal_plan_cycle().get("key"):
            return {
                "status": "error",
                "message": "Complete pay-cycle setup before changing this pay-period plan.",
            }
        # Try integer ID first, then fuzzy title match
        recipe = None
        try:
            rid = int(raw)
            recipe = visible_recipe_by_id(current_household_id(), rid)
        except (ValueError, TypeError):
            pass
        if not recipe:
            recipe = _match_recipe_by_title(raw)

        if not recipe:
            return {
                "status": "error",
                "message": f"No matching recipe found for '{raw}'. "
                           f"Available recipes: {', '.join(r.title for r in visible_recipe_query(current_household_id()).all())}",
                "data": {"suggested_titles": [r.title for r in visible_recipe_query(current_household_id()).all()]},
            }

        if action == "remove":
            deleted = _household_meal_plan_query().filter_by(recipe_id=recipe.id).delete()
            db.session.commit()
            LOGGER.info("Tool select_active_recipe (remove): %s", recipe.title)
            return {
                "status": "ok",
                "data": {
                    "id": recipe.id,
                    "title": recipe.title,
                    "action": "removed",
                    "was_in_plan": bool(deleted),
                },
            }

        # Add
        existing = _household_meal_plan_query().filter_by(recipe_id=recipe.id).first()
        if existing:
            return {"status": "ok", "data": {"id": recipe.id, "title": recipe.title, "action": "already_in_plan"}}
        if _household_meal_plan_query().count() >= 14:
            return {"status": "error", "message": "Meal plan is full (max 14 recipes). Remove one first."}
        db.session.add(_new_current_meal_plan_item(recipe.id, "copilot"))
        _touch_recipe_usage(recipe)
        db.session.commit()
        LOGGER.info("Tool select_active_recipe (add): %s", recipe.title)
        return {"status": "ok", "data": {"id": recipe.id, "title": recipe.title, "action": "added"}}


def _execute_log_discretionary_expense(**kwargs) -> Dict[str, Any]:
    """Log a discretionary expense and deduct from checking balance."""
    from flask import current_app
    from extensions import db
    from models import ExpenseTransaction, Account

    item_name = (kwargs.get("item_name") or "").strip()
    amount = float(kwargs.get("amount", 0))
    if not item_name or amount <= 0:
        return {"status": "error", "message": "Item name and positive amount required."}
    with current_app.app_context():
        hid = current_household_id()
        account = get_household_account(hid)
        if account:
            t = ExpenseTransaction(
                household_id=hid,
                description=item_name,
                amount=amount,
                category="discretionary",
                source="manual",
                local_account_id=account.id,
            )
            apply_balance_delta(hid, -amount)
        else:
            t = ExpenseTransaction(
                household_id=hid,
                description=item_name,
                amount=amount,
                category="discretionary",
                source="manual",
            )
        db.session.add(t)
        db.session.commit()
        LOGGER.info("Tool log_discretionary_expense: %s $%.2f", item_name, amount)
        return {
            "status": "ok",
            "data": {
                "description": item_name,
                "amount": amount,
                "new_balance": round(account.checking_balance, 2) if account else None,
            },
        }


def _execute_set_target_meals(**kwargs) -> Dict[str, Any]:
    """Record the target meal count so the dispatch can auto-fill remaining slots."""
    count = int(kwargs.get("count", 0))
    if count <= 0 or count > 14:
        return {"status": "error", "message": "Target meals must be between 1 and 14."}
    return {"status": "ok", "data": {"target_meals": count}}


def _execute_get_financial_overview(**kwargs) -> Dict[str, Any]:
    """Return a live snapshot of the user's finances for the chat assistant.

    Includes liquidity metrics, upcoming bills, recent transactions, the
    active meal plan, and the grocery cart so the LLM can answer questions
    with real numbers instead of guessing.
    """
    from flask import current_app
    from extensions import db
    from models import Account, Bill, ExpenseTransaction, MealPlanItem, Recipe, GroceryItem
    from app import _canonical_financial_metrics

    with current_app.app_context():
        hid = current_household_id()
        account = Account.query.filter_by(household_id=hid).first()
        if not account:
            return {"status": "error", "message": "No account is set up yet."}

        metrics = _canonical_financial_metrics(account, owner_scope=f"household:{hid}")

        bills = [
            {
                "name": b.name,
                "amount": b.amount,
                "due_date": b.due_date.strftime("%Y-%m-%d") if b.due_date else "",
                "is_paid": b.is_paid,
            }
            for b in Bill.query.filter_by(household_id=hid).order_by(Bill.due_date.asc()).limit(12)
        ]

        recent_txns = [
            {
                "description": t.description,
                "amount": t.amount,
                "category": t.category,
                "date": t.date.strftime("%Y-%m-%d") if t.date else "",
            }
            for t in ExpenseTransaction.query.filter_by(household_id=hid).order_by(ExpenseTransaction.date.desc()).limit(8)
        ]

        from app import _household_meal_plan_query
        plan_ids = [m.recipe_id for m in _household_meal_plan_query().all()]
        meal_plan = []
        if plan_ids:
            meal_plan = [
                {
                    "id": r.id,
                    "title": r.title,
                    "cost_per_serving": r.estimated_cost_per_serving,
                }
                for r in visible_recipe_query(hid).filter(Recipe.id.in_(plan_ids)).all()
            ]

        grocery_cart = [
            {
                "item_name": g.item_name,
                "estimated_price": g.estimated_price,
                "store_name": g.store_name,
                "is_purchased": g.is_purchased,
            }
            for g in GroceryItem.query.filter_by(household_id=hid).all()
        ]

        LOGGER.info("Tool get_financial_overview served snapshot")
        return {
            "status": "ok",
            "data": {
                "metrics": metrics,
                "financial_state": metrics.get("safe_to_spend"),
                "upcoming_bills": bills,
                "recent_transactions": recent_txns,
                "active_meal_plan": meal_plan,
                "grocery_cart": grocery_cart,
            },
        }


# ============================================================================
# Tool name → function dispatch table
# ============================================================================

_TOOL_DISPATCH: Dict[str, Any] = {
    "get_financial_overview": _execute_get_financial_overview,
    "add_recurring_bill": _execute_add_recurring_bill,
    "add_grocery_item": _execute_add_grocery_item,
    "select_active_recipe": _execute_select_active_recipe,
    "log_discretionary_expense": _execute_log_discretionary_expense,
    "set_target_meals": _execute_set_target_meals,
}


def execute_app_function(function_name: str, arguments: dict) -> Dict[str, Any]:
    """Look up *function_name* in the dispatch table and call it with *arguments*.

    Returns a dict with ``status`` (``"ok"`` or ``"error"``) and ``data`` or
    ``message`` key.
    """
    fn = _TOOL_DISPATCH.get(function_name)
    if not fn:
        return {"status": "error", "message": f"Unknown tool: {function_name}"}
    try:
        return fn(**arguments)
    except Exception as exc:
        LOGGER.exception("Tool %s failed", function_name)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
