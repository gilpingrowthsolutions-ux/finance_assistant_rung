from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
import calendar
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError, OperationalError

# Import models at module level - safe now that models.py imports from extensions, not app
from models import (
    Account,
    ActionAudit,
    Bill,
    ExpenseTransaction,
    GroceryItem,
    MealPlanItem,
    Recipe,
    RecipeIngredient,
    ShoppingTripCompletion,
)
from extensions import db
from services.household_context import household_id as current_household_id
from services.financial_state import apply_balance_delta, get_household_account, set_balance_absolute
from services.selected_store import get_selected_store
from services.recipe_recommend import recommend_recipes
from services.transaction_reconciliation import (
    detect_plaid_candidates_for_manual_input,
    ensure_plaid_effect_exists,
    keep_separate_after_manual_creation,
)


class MealRequest(BaseModel):
    total_count: int = Field(7, ge=1, le=14)
    servings: int = Field(4, ge=1)
    specific_requirements: List[str] = Field(default_factory=list)
    removed_titles: List[str] = Field(default_factory=list)
    explicit_target: bool = False  # True only when target_meals was set by LLM

    @field_validator("specific_requirements", mode="before")
    def strip_requirements(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(item).strip() if item else "" for item in v]
        return [str(v).strip()]


class OneTimeExpense(BaseModel):
    category: str
    estimated_amount: Optional[float] = None
    description: Optional[str] = None
    merchant: Optional[str] = None
    transaction_date: Optional[str] = None

    @field_validator("category", mode="before")
    def normalize_category(cls, v: str) -> str:
        return str(v).strip() or "discretionary"


class BillAdjustment(BaseModel):
    bill_name: str
    adjustment_type: str = Field("set")
    amount: Optional[float] = None
    due_day: Optional[int] = None

    @field_validator("bill_name", mode="before")
    def normalize_bill_name(cls, v: str) -> str:
        return str(v).strip()

    @field_validator("adjustment_type", mode="before")
    def normalize_adjustment_type(cls, v: Optional[str]) -> str:
        raw = str(v or "set").strip().lower()
        if raw in {"increase", "raise", "up"}:
            return "increase"
        if raw in {"decrease", "reduce", "lower", "down"}:
            return "decrease"
        if raw in {"remove", "delete", "cancel"}:
            return "remove"
        if raw in {"set", "update", "change", "add", "new", "create"}:
            return "set"
        return "set"

    @field_validator("due_day", mode="before")
    def normalize_due_day(cls, v: Any) -> Optional[int]:
        if v in (None, ""):
            return None
        try:
            day = int(str(v).strip())
        except (TypeError, ValueError):
            return None
        if 1 <= day <= 31:
            return day
        return None


class ClarificationFlags(BaseModel):
    need_clarification: bool = False
    clarification_reasons: List[str] = Field(default_factory=list)


class GroceryAddition(BaseModel):
    item_name: str
    base_item: Optional[str] = None
    brand: Optional[str] = None
    variant: Optional[str] = None
    quantity: float = Field(1.0, gt=0)
    unit: Optional[str] = None
    requested_package_size: Optional[str] = None
    category: Optional[str] = "General"

    @field_validator("item_name", mode="before")
    def normalize_item_name(cls, v: Any) -> str:
        return str(v).strip()

    @property
    def has_explicit_specificity(self) -> bool:
        return bool(
            self.brand
            or self.variant
            or self.unit
            or self.requested_package_size
            or self.quantity != 1.0
        )


class CopilotIntentPayload(BaseModel):
    meal_request: Optional[MealRequest] = None
    groceries: List[GroceryAddition] = Field(default_factory=list)
    expenses: List[OneTimeExpense] = Field(default_factory=list)
    income_events: List[Dict[str, Any]] = Field(default_factory=list)
    balance_reconciliation: Optional[Dict[str, Any]] = None
    shopping_corrections: List[Dict[str, Any]] = Field(default_factory=list)
    bill_adjustments: List[BillAdjustment] = Field(default_factory=list)
    clarification_flags: ClarificationFlags = ClarificationFlags()
    raw_user_text: Optional[str] = None


class StagedActionValidationError(ValueError):
    """Structured validation failure for reviewed staged actions."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


def _normalize_text(value: str) -> str:
    return (value or "").strip()


def _household_account() -> Account:
    return get_household_account(current_household_id())


def _bill_query():
    return Bill.query.filter_by(household_id=current_household_id())


def _tx_query():
    return ExpenseTransaction.query.filter_by(household_id=current_household_id())


def _meal_plan_query():
    return MealPlanItem.query.filter_by(household_id=current_household_id())


def _trip_query():
    return ShoppingTripCompletion.query.filter_by(household_id=current_household_id())


def _audit_query():
    return ActionAudit.query.filter_by(household_id=current_household_id())


def _unresolved_recipe_action(requested_name: str, action: str = "add", reason: str = "recipe_not_found") -> Dict[str, Any]:
    title = _normalize_text(requested_name)
    return {
        "title": title,
        "requested_title": title,
        "action": action,
        "status": "unresolved",
        "reason": reason,
        "substitute_recipe_id": None,
        "decision": "",
    }


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


_STAGE_METADATA_KEYS = {
    "summary",
    "staged",
    "requires_confirmation",
    "undo_token",
    "already_applied",
}

_ACTION_LIST_KEYS = (
    "recipes_added",
    "recipes_auto_filled",
    "recipes_suggested",
    "grocery_list",
    "grocery_items_added",
    "expenses_logged",
    "income_logged",
    "balance_reconciliations",
    "shopping_trip_corrections",
    "bills_added",
    "bills_updated",
    "bills_removed",
)


def _new_operation_id() -> str:
    return "op_" + uuid.uuid4().hex


def _sanitize_for_fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if k in _STAGE_METADATA_KEYS:
                continue
            if k == "user_modified":
                continue
            out[k] = _sanitize_for_fingerprint(v)
        return out
    if isinstance(value, list):
        return [_sanitize_for_fingerprint(v) for v in value]
    return value


def _operation_fingerprint(staged_actions: Dict[str, Any]) -> str:
    canonical = _sanitize_for_fingerprint(staged_actions)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _extract_operation_id(staged_actions: Dict[str, Any]) -> str:
    op = str(staged_actions.get("operation_id") or "").strip()
    if not op:
        raise ValueError("staged_actions.operation_id is required; re-stage the request and apply again.")
    return op


def _replay_applied_operation(audit_row: Any) -> Dict[str, Any]:
    try:
        applied = json.loads(audit_row.actions_json or "{}")
    except Exception:
        applied = {}
    if not isinstance(applied, dict):
        applied = {}
    applied["undo_token"] = audit_row.undo_token
    applied["operation_id"] = audit_row.operation_id
    applied["already_applied"] = True
    return applied


def _replay_matching_operation(audit_row: Any, operation_fp: str) -> Dict[str, Any]:
    """Replay only when an operation identity names the exact same payload."""
    try:
        existing_payload = json.loads(audit_row.actions_json or "{}")
    except Exception:
        existing_payload = {}
    existing_fp = (str(existing_payload.get("operation_fingerprint") or "").strip()
                   if isinstance(existing_payload, dict) else "")
    if not existing_fp or existing_fp != operation_fp:
        raise ValueError(
            "operation_id was already used with different staged content; "
            "re-stage to get a new operation_id."
        )
    return _replay_applied_operation(audit_row)


def _parse_recipe_suggestion_resolution(rec: Any, index: int) -> Dict[str, Any]:
    """Validate one staged unresolved recipe record.

    Returns one of:
      {"kind": "reject", ...}
      {"kind": "substitute", "recipe_id": int, ...}
      {"kind": "unresolved", ...}
      {"kind": "invalid", ...}
    """
    if not isinstance(rec, dict):
        return {
            "kind": "invalid",
            "index": index,
            "requested_title": "",
            "reason": "invalid_suggestion_payload",
        }

    requested = _normalize_text(str(rec.get("requested_title") or rec.get("title") or ""))
    decision = _normalize_text(str(rec.get("decision") or "")).lower()
    sub_id = _coerce_int(rec.get("substitute_recipe_id"))

    if decision in {"reject", "remove", "skip", "ignore"}:
        return {
            "kind": "reject",
            "index": index,
            "requested_title": requested,
            "reason": "user_rejected",
        }

    if sub_id is not None:
        return {
            "kind": "substitute",
            "index": index,
            "requested_title": requested,
            "recipe_id": sub_id,
        }

    return {
        "kind": "unresolved",
        "index": index,
        "requested_title": requested,
        "reason": _normalize_text(str(rec.get("reason") or "recipe_not_found")) or "recipe_not_found",
    }


def _normalize_staged_actions_for_apply(staged_actions: Dict[str, Any], operation_id: str) -> Dict[str, Any]:
    """Normalize and validate reviewed staged actions into canonical objects.

    Raises StagedActionValidationError when any action payload is malformed.
    """
    normalized: Dict[str, Any] = {
        "operation_id": operation_id,
        "target_meals": staged_actions.get("target_meals"),
        "meal_servings": staged_actions.get("meal_servings"),
        "clarification_flags": staged_actions.get("clarification_flags") or {},
    }

    issues: Dict[str, List[Dict[str, Any]]] = {}

    for key in _ACTION_LIST_KEYS:
        rows = staged_actions.get(key, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            issues.setdefault("invalid_action_lists", []).append({
                "field": key,
                "reason": "expected_list",
            })
            rows = []
        normalized[key] = rows

    normalized_recipes: Dict[str, List[Dict[str, Any]]] = {
        "recipes_added": [],
        "recipes_auto_filled": [],
    }
    for key in ("recipes_added", "recipes_auto_filled"):
        for idx, rec in enumerate(normalized[key]):
            if not isinstance(rec, dict):
                issues.setdefault("invalid_recipe_actions", []).append({
                    "field": key,
                    "index": idx,
                    "reason": "expected_object",
                })
                continue
            rid = _coerce_int(rec.get("id"))
            if rid is None:
                issues.setdefault("invalid_recipe_actions", []).append({
                    "field": key,
                    "index": idx,
                    "reason": "missing_recipe_id",
                })
                continue
            title = _normalize_text(str(rec.get("title") or ""))
            normalized_recipes[key].append({"id": rid, "title": title})
        normalized[key] = normalized_recipes[key]

    normalized_grocery_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(normalized["grocery_items_added"]):
        if isinstance(item, str):
            # Legacy compatibility: older payloads sent bare item strings.
            item = {"item_name": item}
        if not isinstance(item, dict):
            issues.setdefault("invalid_grocery_actions", []).append({
                "field": "grocery_items_added",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        item_name = _normalize_text(str(item.get("item_name") or ""))
        if not item_name:
            issues.setdefault("invalid_grocery_actions", []).append({
                "field": "grocery_items_added",
                "index": idx,
                "reason": "missing_item_name",
            })
            continue
        estimated_raw = item.get("estimated_price")
        if estimated_raw in (None, ""):
            estimated_price = 0.0
        else:
            estimated_price = _normalize_amount(estimated_raw)
            if estimated_price is None:
                issues.setdefault("invalid_grocery_actions", []).append({
                    "field": "grocery_items_added",
                    "index": idx,
                    "reason": "invalid_estimated_price",
                    "value": estimated_raw,
                })
                continue
        normalized_grocery_items.append({
            "item_name": item_name,
            "base_item": _normalize_text(str(item.get("base_item") or item_name)).lower(),
            "brand": _normalize_text(str(item.get("brand") or "")) or None,
            "variant": _normalize_text(str(item.get("variant") or "")) or None,
            "quantity": float(item.get("quantity") or 1.0),
            "unit": _normalize_text(str(item.get("unit") or "")) or None,
            "requested_package_size": _normalize_text(str(item.get("requested_package_size") or "")) or None,
            "estimated_price": round(float(estimated_price), 2),
            "category": _normalize_text(str(item.get("category") or "General")) or "General",
        })
    normalized["grocery_items_added"] = normalized_grocery_items

    normalized_grocery_list: List[Dict[str, Any]] = []
    for idx, item in enumerate(normalized["grocery_list"]):
        if not isinstance(item, dict):
            issues.setdefault("invalid_grocery_actions", []).append({
                "field": "grocery_list",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        item_name = _normalize_text(str(item.get("item_name") or ""))
        if not item_name:
            issues.setdefault("invalid_grocery_actions", []).append({
                "field": "grocery_list",
                "index": idx,
                "reason": "missing_item_name",
            })
            continue
        qty_raw = item.get("quantity")
        quantity = _coerce_float(qty_raw)
        if qty_raw not in (None, "") and quantity is None:
            issues.setdefault("invalid_grocery_actions", []).append({
                "field": "grocery_list",
                "index": idx,
                "reason": "invalid_quantity",
                "value": qty_raw,
            })
            continue
        est_raw = item.get("estimated_price")
        est_price = _normalize_amount(est_raw) if est_raw not in (None, "") else 0.0
        if est_raw not in (None, "") and est_price is None:
            issues.setdefault("invalid_grocery_actions", []).append({
                "field": "grocery_list",
                "index": idx,
                "reason": "invalid_estimated_price",
                "value": est_raw,
            })
            continue
        normalized_grocery_list.append({
            "item_name": item_name,
            "clean_keyword": _normalize_text(str(item.get("clean_keyword") or "")),
            "quantity": round(float(quantity or 0.0), 2),
            "unit": _normalize_text(str(item.get("unit") or "unit")) or "unit",
            "estimated_price": round(float(est_price or 0.0), 2),
        })
    normalized["grocery_list"] = normalized_grocery_list

    normalized_expenses: List[Dict[str, Any]] = []
    for idx, exp in enumerate(normalized["expenses_logged"]):
        if not isinstance(exp, dict):
            issues.setdefault("invalid_expense_actions", []).append({
                "field": "expenses_logged",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        category = _normalize_text(str(exp.get("category") or exp.get("description") or "discretionary")).lower()
        if not category:
            category = "discretionary"
        amount_raw = exp.get("amount")
        amount = _normalize_amount(amount_raw)
        if amount_raw not in (None, "") and amount is None:
            issues.setdefault("invalid_expense_actions", []).append({
                "field": "expenses_logged",
                "index": idx,
                "reason": "invalid_amount",
                "value": amount_raw,
            })
            continue
        if amount is None:
            amount = _historical_average_expense(category)
        normalized_expenses.append({
            "description": _normalize_text(str(exp.get("description") or category.title())) or category.title(),
            "category": category,
            "amount": round(float(amount or 0.0), 2),
            "merchant": _normalize_text(str(exp.get("merchant") or "")) or None,
            "transaction_date": _normalize_text(str(exp.get("transaction_date") or exp.get("date") or "")) or None,
            "reconciliation_action": _normalize_text(str(exp.get("reconciliation_action") or "")).lower() or None,
            "selected_plaid_transaction_id": _normalize_text(str(exp.get("selected_plaid_transaction_id") or "")) or None,
            "candidate_plaid_transactions": exp.get("candidate_plaid_transactions") if isinstance(exp.get("candidate_plaid_transactions"), list) else [],
        })
    normalized["expenses_logged"] = normalized_expenses

    normalized_income: List[Dict[str, Any]] = []
    for idx, row in enumerate(normalized["income_logged"]):
        if not isinstance(row, dict):
            issues.setdefault("invalid_income_actions", []).append({
                "field": "income_logged",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        amount_raw = row.get("amount")
        amount = _normalize_amount(amount_raw)
        if amount is None:
            issues.setdefault("invalid_income_actions", []).append({
                "field": "income_logged",
                "index": idx,
                "reason": "invalid_amount",
                "value": amount_raw,
            })
            continue
        normalized_income.append({
            "source": _normalize_text(str(row.get("source") or "income")) or "income",
            "amount": round(float(amount), 2),
            "date": _normalize_text(str(row.get("date") or "")) or None,
            "note": _normalize_text(str(row.get("note") or "")) or None,
        })
    normalized["income_logged"] = normalized_income

    normalized_balances: List[Dict[str, Any]] = []
    for idx, row in enumerate(normalized["balance_reconciliations"]):
        if not isinstance(row, dict):
            issues.setdefault("invalid_balance_actions", []).append({
                "field": "balance_reconciliations",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        target_raw = row.get("new_balance")
        if target_raw in (None, ""):
            target_raw = row.get("target_balance")
        target = _normalize_amount(target_raw)
        if target is None:
            issues.setdefault("invalid_balance_actions", []).append({
                "field": "balance_reconciliations",
                "index": idx,
                "reason": "invalid_target_balance",
                "value": target_raw,
            })
            continue
        current = _normalize_amount(row.get("current_balance"))
        normalized_balances.append({
            "current_balance": round(float(current), 2) if current is not None else None,
            "new_balance": round(float(target), 2),
            "reason": _normalize_text(str(row.get("reason") or "manual_reconciliation")) or "manual_reconciliation",
        })
    normalized["balance_reconciliations"] = normalized_balances

    normalized_corrections: List[Dict[str, Any]] = []
    for idx, row in enumerate(normalized["shopping_trip_corrections"]):
        if not isinstance(row, dict):
            issues.setdefault("invalid_shopping_corrections", []).append({
                "field": "shopping_trip_corrections",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        target_raw = row.get("new_actual_total")
        target = _normalize_amount(target_raw)
        if target is None:
            issues.setdefault("invalid_shopping_corrections", []).append({
                "field": "shopping_trip_corrections",
                "index": idx,
                "reason": "invalid_new_actual_total",
                "value": target_raw,
            })
            continue
        normalized_corrections.append({
            "id": _coerce_int(row.get("id")),
            "operation_id": _normalize_text(str(row.get("operation_id") or "")) or None,
            "trip_token": _normalize_text(str(row.get("trip_token") or "")) or None,
            "transaction_id": _coerce_int(row.get("transaction_id")),
            "previous_actual_total": round(float(_normalize_amount(row.get("previous_actual_total")) or 0.0), 2),
            "new_actual_total": round(float(target), 2),
            "reason": _normalize_text(str(row.get("reason") or "manual_correction")) or "manual_correction",
        })
    normalized["shopping_trip_corrections"] = normalized_corrections

    normalized_bills_added: List[Dict[str, Any]] = []
    for idx, bill in enumerate(normalized["bills_added"]):
        if not isinstance(bill, dict):
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_added",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        name = _normalize_text(str(bill.get("name") or ""))
        amount_raw = bill.get("amount")
        amount = _normalize_amount(amount_raw)
        if not name:
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_added",
                "index": idx,
                "reason": "missing_name",
            })
            continue
        if amount is None:
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_added",
                "index": idx,
                "reason": "invalid_amount",
                "value": amount_raw,
            })
            continue
        due_date_raw = bill.get("due_date")
        due_date = _parse_staged_due_date(due_date_raw)
        if due_date_raw not in (None, "") and due_date is None:
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_added",
                "index": idx,
                "reason": "invalid_due_date",
                "value": due_date_raw,
            })
            continue
        row = {"name": name, "amount": round(float(amount), 2)}
        if due_date is not None:
            row["due_date"] = due_date.isoformat()
        normalized_bills_added.append(row)
    normalized["bills_added"] = normalized_bills_added

    normalized_bills_updated: List[Dict[str, Any]] = []
    for idx, bill in enumerate(normalized["bills_updated"]):
        if not isinstance(bill, dict):
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_updated",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        bid = _coerce_int(bill.get("id"))
        name = _normalize_text(str(bill.get("name") or ""))
        amount_raw = bill.get("amount")
        amount = _normalize_amount(amount_raw)
        if bid is None and not name:
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_updated",
                "index": idx,
                "reason": "missing_id_or_name",
            })
            continue
        if amount is None:
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_updated",
                "index": idx,
                "reason": "invalid_amount",
                "value": amount_raw,
            })
            continue
        due_date_raw = bill.get("due_date")
        due_date = _parse_staged_due_date(due_date_raw)
        if due_date_raw not in (None, "") and due_date is None:
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_updated",
                "index": idx,
                "reason": "invalid_due_date",
                "value": due_date_raw,
            })
            continue
        row = {"id": bid, "name": name, "amount": round(float(amount), 2)}
        if due_date is not None:
            row["due_date"] = due_date.isoformat()
        normalized_bills_updated.append(row)
    normalized["bills_updated"] = normalized_bills_updated

    normalized_bills_removed: List[Dict[str, Any]] = []
    for idx, bill in enumerate(normalized["bills_removed"]):
        if not isinstance(bill, dict):
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_removed",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        bid = _coerce_int(bill.get("id"))
        name = _normalize_text(str(bill.get("name") or ""))
        if bid is None and not name:
            issues.setdefault("invalid_bill_actions", []).append({
                "field": "bills_removed",
                "index": idx,
                "reason": "missing_id_or_name",
            })
            continue
        normalized_bills_removed.append({"id": bid, "name": name})
    normalized["bills_removed"] = normalized_bills_removed

    normalized_suggested: List[Dict[str, Any]] = []
    for idx, rec in enumerate(normalized["recipes_suggested"]):
        if not isinstance(rec, dict):
            issues.setdefault("invalid_recipe_actions", []).append({
                "field": "recipes_suggested",
                "index": idx,
                "reason": "expected_object",
            })
            continue
        requested = _normalize_text(str(rec.get("requested_title") or rec.get("title") or ""))
        normalized_suggested.append({
            "title": _normalize_text(str(rec.get("title") or requested)),
            "requested_title": requested,
            "action": _normalize_text(str(rec.get("action") or "add")) or "add",
            "status": _normalize_text(str(rec.get("status") or "unresolved")) or "unresolved",
            "reason": _normalize_text(str(rec.get("reason") or "recipe_not_found")) or "recipe_not_found",
            "substitute_recipe_id": _coerce_int(rec.get("substitute_recipe_id")),
            "decision": _normalize_text(str(rec.get("decision") or "")),
        })
    normalized["recipes_suggested"] = normalized_suggested

    if issues:
        if issues.get("invalid_grocery_actions"):
            code = "invalid_grocery_action_payload"
            message = "Invalid staged grocery action payload."
        elif issues.get("invalid_expense_actions"):
            code = "invalid_expense_action_payload"
            message = "Invalid staged expense action payload."
        elif issues.get("invalid_income_actions"):
            code = "invalid_income_action_payload"
            message = "Invalid staged income action payload."
        elif issues.get("invalid_balance_actions"):
            code = "invalid_balance_action_payload"
            message = "Invalid staged balance action payload."
        elif issues.get("invalid_shopping_corrections"):
            code = "invalid_shopping_correction_payload"
            message = "Invalid staged shopping correction payload."
        elif issues.get("invalid_bill_actions"):
            code = "invalid_bill_action_payload"
            message = "Invalid staged bill action payload."
        elif issues.get("invalid_recipe_actions"):
            code = "invalid_recipe_action_payload"
            message = "Invalid staged recipe action payload."
        else:
            code = "invalid_staged_actions_schema"
            message = "Invalid staged actions schema."
        raise StagedActionValidationError(
            message,
            details={
                "code": code,
                "issues": issues,
                "operation_id": operation_id,
            },
        )

    return normalized


def _normalize_bill_adjustment_type(raw: Any) -> str:
    value = str(raw or "set").strip().lower()
    if value in {"increase", "raise", "raised", "up", "went up", "higher"}:
        return "increase"
    if value in {"decrease", "reduce", "reduced", "down", "went down", "lower"}:
        return "decrease"
    if value in {"remove", "delete", "cancel"}:
        return "remove"
    if value in {"set", "update", "change", "add", "new", "create"}:
        return "set"
    return "set"


def _normalize_amount(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    s = re.sub(r'(?i)(/mo|per\s+month|monthly|/month|/yr|per\s+year|annually).*$', '', s).strip()
    s = s.replace('$', '').replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_due_day(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        day = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 1 <= day <= 31:
        return day
    return None


def _resolve_due_date_from_day(day: int, now: Optional[datetime] = None) -> datetime:
    ref = now or datetime.utcnow()
    year = ref.year
    month = ref.month

    while True:
        last_day = calendar.monthrange(year, month)[1]
        candidate_day = min(day, last_day)
        candidate = ref.replace(year=year, month=month, day=candidate_day)
        if candidate >= ref:
            return candidate
        month += 1
        if month > 12:
            month = 1
            year += 1


def _parse_staged_due_date(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Support either YYYY-MM-DD or full ISO timestamp forms.
    parse_attempts = [text]
    if " " in text and "T" not in text:
        parse_attempts.append(text.replace(" ", "T", 1))
    for candidate in parse_attempts:
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _set_clarification_once(flags: ClarificationFlags, question: str) -> None:
    """Store at most one user-facing clarification question."""
    text = (question or "").strip()
    if not text:
        return
    if flags.need_clarification and flags.clarification_reasons:
        return
    flags.need_clarification = True
    flags.clarification_reasons = [text]


def _find_bill_by_name(bill_name: str):

    if not bill_name:
        return None
    return _bill_query().filter(Bill.name.ilike(f"%{bill_name}%")).first()


def _historical_average_expense(category: str) -> float:

    txns = _tx_query().filter(ExpenseTransaction.category.ilike(f"%{category}%")).all()
    if not txns:
        return 20.0
    return sum(t.amount for t in txns) / max(1, len(txns))


def _historical_bill_amount(bill_name: str) -> Optional[float]:

    rows = _tx_query().filter(
        ExpenseTransaction.description.ilike(f"%{bill_name}%")
    ).all()
    if not rows:
        return None
    return sum(r.amount for r in rows) / max(1, len(rows))


def _resolve_shopping_trip_completion(correction: Dict[str, Any]) -> Optional[ShoppingTripCompletion]:
    trip_id = _coerce_int(correction.get("id"))
    if trip_id is not None:
        row = _trip_query().filter_by(id=trip_id).first()
        if row is not None:
            return row

    operation_id = _normalize_text(str(correction.get("operation_id") or ""))
    if operation_id:
        row = _trip_query().filter_by(operation_id=operation_id).first()
        if row is not None:
            return row

    trip_token = _normalize_text(str(correction.get("trip_token") or ""))
    if trip_token:
        row = _trip_query().filter_by(trip_token=trip_token).first()
        if row is not None:
            return row

    selector = _normalize_text(str(correction.get("selector") or "")).lower()
    if selector in {"latest", "last"}:
        return (
            _trip_query()
            .order_by(ShoppingTripCompletion.completed_at.desc(), ShoppingTripCompletion.id.desc())
            .first()
        )
    return None


def _normalize_tokens(text: str) -> set[str]:
    """Normalize free text into alphanumeric tokens for matching."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    return {t for t in cleaned.split() if t}


def _recipe_text_match_strength(recipe: Any, term: str) -> float:
    """Return [0..1] strength for how well a recipe matches a requested term."""
    term_tokens = _normalize_tokens(term)
    if not term_tokens:
        return 0.0

    title = str(getattr(recipe, "title", "") or "")
    title_tokens = _normalize_tokens(title)
    ingredient_tokens: set[str] = set()
    for ing in getattr(recipe, "ingredients", []) or []:
        ingredient_tokens |= _normalize_tokens(getattr(ing, "clean_keyword", ""))
        ingredient_tokens |= _normalize_tokens(getattr(ing, "product_name", ""))

    text_blob = " ".join(
        [title.lower()]
        + [str(getattr(ing, "clean_keyword", "") or "").lower() for ing in getattr(recipe, "ingredients", []) or []]
        + [str(getattr(ing, "product_name", "") or "").lower() for ing in getattr(recipe, "ingredients", []) or []]
    )
    # Substring match preserves short category-like prompts (e.g., mexican).
    substring_hit = 1.0 if str(term or "").strip().lower() in text_blob else 0.0

    title_overlap = len(term_tokens & title_tokens) / max(1, len(term_tokens))
    ingredient_overlap = len(term_tokens & ingredient_tokens) / max(1, len(term_tokens))
    return max(substring_hit, title_overlap * 0.8 + ingredient_overlap * 0.9)


def _recipe_habit_score(recipe: Any) -> float:
    """Compute implicit-habit score from usage frequency + recency."""
    usage = int(getattr(recipe, "usage_frequency", 0) or 0)
    usage_component = min(usage, 50) * 1.8

    recency_component = 0.0
    last = getattr(recipe, "last_selected_date", None)
    if last is not None:
        try:
            age_days = max(0, (datetime.utcnow() - last).days)
            recency_component = max(0.0, 24.0 - min(age_days, 24))
        except Exception:
            recency_component = 0.0

    return usage_component + recency_component


def _touch_recipe_usage(recipe_id: int) -> None:
    """Implicit learning hook for recipe selections made by Copilot."""
    from models import Recipe

    recipe = Recipe.query.get(recipe_id)
    if recipe is None:
        return
    recipe.usage_frequency = int(getattr(recipe, "usage_frequency", 0) or 0) + 1
    recipe.last_selected_date = datetime.utcnow()
    db.session.add(recipe)


def _match_recipe_by_title_or_keyword(term: str):

    if not term:
        return None

    recipes = Recipe.query.all()
    if not recipes:
        return None

    # Pass 1: category/text match candidates with tiered preference/habit scoring.
    scored_matches: List[Tuple[float, Any]] = []
    for recipe in recipes:
        strength = _recipe_text_match_strength(recipe, term)
        if strength <= 0:
            continue

        favorite_bonus = 120.0 if bool(getattr(recipe, "is_favorite", False)) else 0.0
        habit_bonus = _recipe_habit_score(recipe)
        # Tier 1 favorite > Tier 2 habit > Tier 3 discovery tie-breakers.
        total = favorite_bonus + habit_bonus + (strength * 40.0) + 8.0
        scored_matches.append((total, recipe))

    if scored_matches:
        scored_matches.sort(key=lambda row: row[0], reverse=True)
        return scored_matches[0][1]

    # Keep explicit unmatched multi-word requests as suggestions rather than
    # force-predicting a recipe the user did not ask for.
    if len(_normalize_tokens(term)) > 1:
        return None

    # Pass 2: no explicit category match found -> habit-driven prediction fallback.
    predicted: List[Tuple[float, Any]] = []
    for recipe in recipes:
        favorite_hint = 10.0 if bool(getattr(recipe, "is_favorite", False)) else 0.0
        total = _recipe_habit_score(recipe) + favorite_hint + 2.0
        predicted.append((total, recipe))
    predicted.sort(key=lambda row: row[0], reverse=True)
    return predicted[0][1] if predicted else None


def _recommend_recipes(exclude_ids: List[int], limit: int = 14, seed_ids: Optional[List[int]] = None):
    """Recommend recipes - delegates to services.recipe_recommend module."""
    return recommend_recipes(exclude_ids=exclude_ids, limit=limit, seed_ids=seed_ids)


def _favorite_and_history_recipe_ids(limit: int = 20) -> List[int]:
    """Collect recipe IDs from user favorites and historical action audits.

    Preference order:
      1) current user-selected meal-plan items (source='user')
      2) most frequent + most recent IDs from ActionAudit recipe actions
    """

    favored_ids: List[int] = []
    for row in (
        _meal_plan_query()
        .filter_by(source="user")
        .order_by(MealPlanItem.created_at.desc())
        .all()
    ):
        if row.recipe_id not in favored_ids:
            favored_ids.append(row.recipe_id)

    score: Dict[int, int] = {}
    for audit in _audit_query().order_by(ActionAudit.created_at.desc()).limit(100).all():
        try:
            actions = json.loads(audit.actions_json or "{}")
        except Exception:
            continue
        for block in ("recipes_added", "recipes_auto_filled"):
            for rec in actions.get(block, []) or []:
                rid = rec.get("id")
                if isinstance(rid, int):
                    score[rid] = score.get(rid, 0) + 1

    history_ids = [rid for rid, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)]

    combined: List[int] = []
    for rid in favored_ids + history_ids:
        if rid not in combined:
            combined.append(rid)
        if len(combined) >= limit:
            break
    return combined


def _starter_recipe_ids(limit: int = 7) -> List[int]:
    """Return curated global starter recipe IDs in priority order."""
    from app import DEFAULT_STARTER_RECIPE_TITLES

    ids: List[int] = []
    for starter_title in DEFAULT_STARTER_RECIPE_TITLES:
        recipe = Recipe.query.filter(Recipe.title.ilike(starter_title)).first()
        if recipe is None or recipe.id in ids:
            continue
        ids.append(recipe.id)
        if len(ids) >= limit:
            break
    return ids


def _add_recipe_to_plan(recipe_id: int, source: str) -> bool:

    if _meal_plan_query().filter_by(recipe_id=recipe_id).first():
        return False
    if _meal_plan_query().count() >= 14:
        return False
    db.session.add(MealPlanItem(household_id=current_household_id(), recipe_id=recipe_id, source=source))
    _touch_recipe_usage(recipe_id)
    return True


def _aggregate_ingredients(recipes: List[Any], servings: int) -> List[Dict[str, Any]]:
    aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for recipe in recipes:
        scale = servings / max(1, recipe.servings)
        for ingredient in recipe.ingredients:
            key = (ingredient.clean_keyword, ingredient.unit)
            qty = round((ingredient.quantity or 0.0) * scale, 2)
            if key not in aggregated:
                aggregated[key] = {
                    "item_name": ingredient.product_name.title(),
                    "clean_keyword": ingredient.clean_keyword,
                    "quantity": 0.0,
                    "unit": ingredient.unit,
                }
            aggregated[key]["quantity"] += qty
    return [dict(v, quantity=round(v["quantity"], 2)) for v in aggregated.values()]


def _resolve_preferred_grocery_item(term: str) -> str:
    """Resolve a grocery term to a favorited grocery item when possible."""
    from models import GroceryItem

    text = _normalize_text(term)
    if not text:
        return ""

    tokens = _normalize_tokens(text)
    favorites = GroceryItem.query.filter_by(household_id=current_household_id(), is_favorite=True).all()
    if not favorites:
        return text

    best_item = None
    best_score = 0.0
    for item in favorites:
        name = str(getattr(item, "item_name", "") or "")
        name_tokens = _normalize_tokens(name)
        overlap = len(tokens & name_tokens) / max(1, len(tokens))
        # Keep low-effort broad-term support by also checking substring matches.
        if overlap <= 0 and text.lower() not in name.lower():
            continue
        score = overlap + 1.0
        if score > best_score:
            best_score = score
            best_item = item

    if best_item is None:
        return text
    return str(getattr(best_item, "item_name", text) or text)


def parse_intent_payload(parsed: Dict[str, Any], user_text: str = "") -> CopilotIntentPayload:
    meal_request = None
    selected_recipes = []
    removed_titles = []

    for sel in parsed.get("selected_recipes", []):
        action = sel.get("action", "add").lower()
        if action == "add":
            selected_recipes.append(_normalize_text(sel.get("title", "")))
        elif action == "remove":
            removed_titles.append(_normalize_text(sel.get("title", "")))

    raw_target_meals = parsed.get("target_meals")
    target_meals = raw_target_meals
    parsed_servings = parsed.get("meal_servings")
    if target_meals is None and selected_recipes:
        target_meals = len(selected_recipes)

    if target_meals is not None or selected_recipes or removed_titles:
        requested_count = target_meals or max(len(selected_recipes), 7)
        # Guard against out-of-range model output so staging never raises.
        requested_count = max(1, min(14, int(requested_count)))
        meal_request = MealRequest(
            total_count=requested_count,
            servings=int(parsed_servings) if parsed_servings else 4,
            specific_requirements=[r for r in selected_recipes if r],
            removed_titles=[r for r in removed_titles if r],
            explicit_target=(raw_target_meals is not None),
        )

    groceries: List[GroceryAddition] = []
    for requirement in parsed.get("shopping_requirements", []) or []:
        if not isinstance(requirement, dict):
            continue
        item_text = _normalize_text(str(requirement.get("item_name") or ""))
        if item_text:
            groceries.append(GroceryAddition(**dict(requirement, item_name=item_text)))

    if groceries:
        parsed_grocery_additions: List[Any] = []
    else:
        parsed_grocery_additions = parsed.get("grocery_additions", [])
    for item_name in parsed_grocery_additions:
        if isinstance(item_name, list):
            for nested_item in item_name:
                item_text = _normalize_text(str(nested_item))
                if item_text:
                    groceries.append(GroceryAddition(item_name=item_text))
            continue
        if isinstance(item_name, dict):
            item_text = _normalize_text(str(item_name.get("item_name") or item_name.get("name") or ""))
            if item_text:
                groceries.append(GroceryAddition(item_name=item_text))
            continue
        text = re.sub(r",\s+(?=and\b)", " ", str(item_name or ""), flags=re.IGNORECASE)
        for split_item in re.split(r"\s*(?:,\s*|\s+and\s+)\s*", text):
            item_text = _normalize_text(split_item)
            if item_text and not any(marker in item_text.lower() for marker in ["/mo", "per month"]):
                groceries.append(GroceryAddition(item_name=item_text))

    expenses = []
    for ev in parsed.get("discretionary_events", []):
        amount = _normalize_amount(ev.get("amount"))
        desc = _normalize_text(str(ev.get("description") or "discretionary")) or "discretionary"
        expenses.append(
            OneTimeExpense(
                category=desc.lower(),
                estimated_amount=amount,
                description=desc,
                merchant=None,
                transaction_date=None,
            )
        )

    for ev in parsed.get("spending_events", []) or []:
        if not isinstance(ev, dict):
            continue
        amount = _normalize_amount(ev.get("amount"))
        merchant = _normalize_text(str(ev.get("merchant") or ev.get("description") or ""))
        category = _normalize_text(str(ev.get("category") or "discretionary")).lower() or "discretionary"
        desc = _normalize_text(str(ev.get("description") or merchant or category.title()))
        expenses.append(
            OneTimeExpense(
                category=category,
                estimated_amount=amount,
                description=desc or category.title(),
                merchant=merchant or None,
                transaction_date=_normalize_text(str(ev.get("date") or "")) or None,
            )
        )

    deduped_by_key: Dict[tuple[Optional[float], str], OneTimeExpense] = {}
    for row in expenses:
        key = (
            round(float(row.estimated_amount), 2) if row.estimated_amount is not None else None,
            _normalize_text(row.description or row.merchant or row.category or "").lower(),
        )
        existing = deduped_by_key.get(key)
        if existing is None:
            deduped_by_key[key] = row
            continue

        # Keep the richer row when duplicate intents describe the same spend.
        existing_has_merchant = bool(_normalize_text(existing.merchant))
        row_has_merchant = bool(_normalize_text(row.merchant))
        if row_has_merchant and not existing_has_merchant:
            existing.merchant = row.merchant
        if (_normalize_text(existing.category).lower() == "discretionary"
                and _normalize_text(row.category).lower() != "discretionary"):
            existing.category = row.category
        if not _normalize_text(existing.description) and _normalize_text(row.description):
            existing.description = row.description
        if not _normalize_text(existing.transaction_date) and _normalize_text(row.transaction_date):
            existing.transaction_date = row.transaction_date

    expenses = list(deduped_by_key.values())

    income_events: List[Dict[str, Any]] = []
    for ev in parsed.get("income_events", []) or []:
        if not isinstance(ev, dict):
            continue
        amount = _normalize_amount(ev.get("amount"))
        if amount is None:
            continue
        income_events.append({
            "source": _normalize_text(str(ev.get("source") or "income")) or "income",
            "amount": round(float(amount), 2),
            "date": _normalize_text(str(ev.get("date") or "")) or None,
            "note": _normalize_text(str(ev.get("note") or "")) or None,
        })

    balance_reconciliation = None
    raw_balance = parsed.get("balance_reconciliation")
    if isinstance(raw_balance, dict):
        target = _normalize_amount(raw_balance.get("target_balance"))
        if target is not None:
            balance_reconciliation = {
                "target_balance": round(float(target), 2),
                "reason": _normalize_text(str(raw_balance.get("reason") or "")) or "manual_reconciliation",
            }

    shopping_corrections: List[Dict[str, Any]] = []
    for row in parsed.get("shopping_corrections", []) or []:
        if not isinstance(row, dict):
            continue
        target_amount = _normalize_amount(row.get("new_actual_total"))
        if target_amount is None:
            continue
        shopping_corrections.append({
            "operation_id": _normalize_text(str(row.get("operation_id") or "")) or None,
            "trip_token": _normalize_text(str(row.get("trip_token") or "")) or None,
            "selector": _normalize_text(str(row.get("selector") or "")) or None,
            "new_actual_total": round(float(target_amount), 2),
        })

    bill_adjustments = []
    for bill in parsed.get("bill_updates", []):
        name = _normalize_text(bill.get("name", ""))
        if not name:
            continue
        action = bill.get("action", "set")
        adjustment_type = _normalize_bill_adjustment_type(
            bill.get("adjustment_type") or action
        )
        amount = _normalize_amount(bill.get("amount"))
        due_day = _normalize_due_day(bill.get("due_day"))
        bill_adjustments.append(
            BillAdjustment(
                bill_name=name,
                adjustment_type=adjustment_type,
                amount=amount,
                due_day=due_day,
            )
        )

    clarification_flags = ClarificationFlags()
    if parsed.get("clarification_question"):
        _set_clarification_once(
            clarification_flags,
            str(parsed.get("clarification_question") or "").strip(),
        )

    return CopilotIntentPayload(
        meal_request=meal_request,
        groceries=groceries,
        expenses=expenses,
        income_events=income_events,
        balance_reconciliation=balance_reconciliation,
        shopping_corrections=shopping_corrections,
        bill_adjustments=bill_adjustments,
        clarification_flags=clarification_flags,
        raw_user_text=user_text,
    )


def resolve_meal_request(meal_request: MealRequest) -> Dict[str, Any]:

    actions: Dict[str, Any] = {
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_removed": [],
        "recipes_suggested": [],
        "grocery_list": [],
    }

    added_recipes: List[Any] = []
    existing_plan_ids = {item.recipe_id for item in _meal_plan_query().all()}
    recommendation_seed_ids: List[int] = []

    # Process explicit remove requests first, tracking excluded IDs
    removed_ids: set = set()
    for title in (meal_request.removed_titles or []):
        recipe = _match_recipe_by_title_or_keyword(title)
        if recipe:
            removed_ids.add(recipe.id)
            if recipe.id in existing_plan_ids:
                item = _meal_plan_query().filter_by(recipe_id=recipe.id).first()
                if item:
                    db.session.delete(item)
                    existing_plan_ids.discard(recipe.id)
                    actions["recipes_removed"].append({"id": recipe.id, "title": recipe.title})

    for requirement in meal_request.specific_requirements:
        recipe = _match_recipe_by_title_or_keyword(requirement)
        if not recipe:
            actions["recipes_suggested"].append(_unresolved_recipe_action(requirement, action="add"))
            continue
        if recipe.id in removed_ids:
            continue
        if _add_recipe_to_plan(recipe.id, "copilot"):
            added_recipes.append(recipe)
            actions["recipes_added"].append({"id": recipe.id, "title": recipe.title})
            existing_plan_ids.add(recipe.id)
            recommendation_seed_ids.append(recipe.id)

    # If all specific requirements were unmatched (nothing added, only suggested),
    # skip auto-fill unless the target was explicitly set by the user.
    if (
        meal_request.specific_requirements
        and not added_recipes
        and not meal_request.explicit_target
    ):
        db.session.commit()
        return actions

    # Auto-fill only when target was explicit or at least one specific recipe was added.
    if not meal_request.explicit_target and not added_recipes and not removed_ids:
        db.session.commit()
        return actions

    # Backfill from known user preference/history before generic recommendations.
    if len(existing_plan_ids) < meal_request.total_count:
        fill = min(meal_request.total_count - len(existing_plan_ids), 14 - len(existing_plan_ids))
        for rid in _favorite_and_history_recipe_ids(limit=fill * 2):
            if rid in existing_plan_ids or rid in removed_ids:
                continue
            rec = Recipe.query.get(rid)
            if rec is None:
                continue
            if _add_recipe_to_plan(rec.id, "autofill"):
                added_recipes.append(rec)
                actions["recipes_auto_filled"].append({"id": rec.id, "title": rec.title})
                existing_plan_ids.add(rec.id)
                recommendation_seed_ids.append(rec.id)
                if len(existing_plan_ids) >= meal_request.total_count:
                    break

    if len(existing_plan_ids) < meal_request.total_count:
        fill = min(meal_request.total_count - len(existing_plan_ids), 14 - len(existing_plan_ids))
        seed_ids = list(dict.fromkeys(recommendation_seed_ids + list(existing_plan_ids)))
        for rec in _recommend_recipes(list(existing_plan_ids) + list(removed_ids), limit=fill, seed_ids=seed_ids):
            if rec.id in removed_ids:
                continue
            if _add_recipe_to_plan(rec.id, "autofill"):
                added_recipes.append(rec)
                actions["recipes_auto_filled"].append({"id": rec.id, "title": rec.title})
                existing_plan_ids.add(rec.id)

    if len(existing_plan_ids) < meal_request.total_count and not recommendation_seed_ids:
        starter_ids = _starter_recipe_ids(limit=meal_request.total_count - len(existing_plan_ids))
        for rid in starter_ids:
            if rid in existing_plan_ids or rid in removed_ids:
                continue
            rec = Recipe.query.get(rid)
            if rec is None:
                continue
            if _add_recipe_to_plan(rec.id, "starter"):
                added_recipes.append(rec)
                actions["recipes_auto_filled"].append({"id": rec.id, "title": rec.title})
                existing_plan_ids.add(rec.id)

    if not added_recipes and meal_request.total_count > len(existing_plan_ids):
        seed_ids = list(dict.fromkeys(recommendation_seed_ids + list(existing_plan_ids)))
        for rec in _recommend_recipes(list(existing_plan_ids) + list(removed_ids), limit=meal_request.total_count - len(existing_plan_ids), seed_ids=seed_ids):
            if rec.id in removed_ids:
                continue
            if _add_recipe_to_plan(rec.id, "autofill"):
                added_recipes.append(rec)
                actions["recipes_auto_filled"].append({"id": rec.id, "title": rec.title})
                existing_plan_ids.add(rec.id)

    if added_recipes:
        actions["grocery_list"] = _aggregate_ingredients(added_recipes, meal_request.servings)
        for item in actions["grocery_list"]:
            db.session.add(GroceryItem(
                household_id=current_household_id(),
                item_name=item["item_name"],
                estimated_price=0.0,
                store_name="Local Store",
                recipe_ids=",".join(str(r.id) for r in added_recipes),
            ))

    db.session.commit()
    return actions


def resolve_expenses(expenses: List[OneTimeExpense]) -> Dict[str, Any]:

    actions = {"expenses_logged": []}
    hid = current_household_id()
    account = _household_account()

    for expense in expenses:
        amount = expense.estimated_amount
        if amount is None:
            amount = _historical_average_expense(expense.category)
        amount = round(amount or 0.0, 2)
        tx = ExpenseTransaction(
            household_id=hid,
            description=expense.category.title(),
            amount=amount,
            category=expense.category,
            source="manual",
            local_account_id=account.id if account else None,
        )
        db.session.add(tx)
        apply_balance_delta(hid, -amount)
        actions["expenses_logged"].append({"description": expense.category, "amount": amount})

    db.session.commit()
    return actions


def resolve_grocery_additions(groceries: List[GroceryAddition]) -> Dict[str, Any]:

    actions = {"grocery_items_added": []}
    account = _household_account()
    for grocery in groceries:
        if not grocery.item_name:
            continue
        resolved_name = _resolve_preferred_grocery_item(grocery.item_name)
        gi = GroceryItem(
            household_id=current_household_id(),
            item_name=resolved_name.title(),
            estimated_price=0.0,
            store_name=(get_selected_store(current_household_id(), account=account).get("name") or "Local Store"),
            shopping_requirement_json=json.dumps(grocery.model_dump()),
        )
        db.session.add(gi)
        actions["grocery_items_added"].append(resolved_name.lower())

    db.session.commit()
    return actions


def resolve_bill_adjustments(bill_adjustments: List[BillAdjustment], flags: ClarificationFlags) -> Dict[str, Any]:

    actions = {"bills_added": [], "bills_updated": [], "bills_removed": []}

    for bill in bill_adjustments:
        existing = _find_bill_by_name(bill.bill_name)
        if bill.adjustment_type == "remove":
            if existing:
                db.session.delete(existing)
                actions["bills_removed"].append({"name": existing.name, "amount": existing.amount})
            else:
                _set_clarification_once(
                    flags,
                    f"Could not remove '{bill.bill_name}' because no matching recurring bill was found."
                )
            continue

        amount = bill.amount
        baseline = _historical_bill_amount(bill.bill_name)

        if existing:
            if amount is None and bill.adjustment_type in {"increase", "decrease"}:
                _set_clarification_once(
                    flags,
                    f"I need the delta amount to adjust '{bill.bill_name}'."
                )
                continue
            if amount is None:
                amount = baseline
            if amount is None:
                _set_clarification_once(
                    flags,
                    f"I need an amount to set up or adjust '{bill.bill_name}'."
                )
                continue
            if bill.adjustment_type == "increase":
                existing.amount += amount
            elif bill.adjustment_type == "decrease":
                existing.amount = max(0.0, existing.amount - amount)
            else:
                existing.amount = amount
            db.session.add(existing)
            actions["bills_updated"].append({"name": existing.name, "amount": existing.amount})
            continue

        if bill.adjustment_type in {"increase", "decrease"}:
            if amount is None:
                _set_clarification_once(
                    flags,
                    f"I need the delta amount to adjust '{bill.bill_name}'."
                )
                continue
            if baseline is None:
                _set_clarification_once(
                    flags,
                    f"I couldn't find a recurring or historical baseline for '{bill.bill_name}'. "
                    "Please share the baseline amount."
                )
                continue
            amount = baseline + amount if bill.adjustment_type == "increase" else max(0.0, baseline - amount)
        else:
            if amount is None:
                amount = baseline
            if amount is None:
                _set_clarification_once(
                    flags,
                    f"I need an amount to set up or adjust '{bill.bill_name}'."
                )
                continue

        due_date = datetime.utcnow() + timedelta(days=14)
        new_bill = Bill(name=bill.bill_name.title(), amount=amount, due_date=due_date)
        new_bill.household_id = current_household_id()
        db.session.add(new_bill)
        actions["bills_added"].append({"name": bill.bill_name, "amount": new_bill.amount})

    db.session.commit()
    return actions


def build_intent_summary(actions: Dict[str, Any], flags: ClarificationFlags) -> str:
    messages: List[str] = []
    if actions.get("recipes_added"):
        messages.append(f"Added {len(actions['recipes_added'])} recipe(s) to your meal plan.")
    if actions.get("recipes_auto_filled"):
        messages.append(f"Auto-filled {len(actions['recipes_auto_filled'])} additional recipe(s).")
    if actions.get("grocery_list"):
        messages.append(f"Added {len(actions['grocery_list'])} grocery item(s) to your shopping list.")
    if actions.get("expenses_logged"):
        messages.append(f"Logged {len(actions['expenses_logged'])} one-time expense(s).")
    if actions.get("income_logged"):
        messages.append(f"Logged {len(actions['income_logged'])} income item(s).")
    if actions.get("balance_reconciliations"):
        messages.append(f"Prepared {len(actions['balance_reconciliations'])} balance reconciliation update(s).")
    if actions.get("shopping_trip_corrections"):
        messages.append(f"Prepared {len(actions['shopping_trip_corrections'])} finished-shopping correction(s).")
    if actions.get("bills_updated"):
        messages.append(f"Updated {len(actions['bills_updated'])} recurring bill(s).")
    if actions.get("bills_added"):
        messages.append(f"Added {len(actions['bills_added'])} recurring bill(s).")
    if actions.get("bills_removed"):
        messages.append(f"Removed {len(actions['bills_removed'])} recurring bill(s).")
    if flags.need_clarification:
        question = flags.clarification_reasons[0] if flags.clarification_reasons else "I need one detail to continue."
        messages.append("I need more information: " + question)
    if not messages:
        return "I parsed your request but did not make any changes." 
    return " ".join(messages)


def execute_intent_payload(payload: CopilotIntentPayload, user_id: str = "anonymous") -> Dict[str, Any]:
    from models import GroceryItem, MealPlanItem, Recipe, Account, ExpenseTransaction, Bill
    from extensions import db
    """Execute an intent payload.

    By default this function persistently applies safe actions and returns a
    description of what was applied. Risky or ambiguous actions are not
    persisted unless the caller sets `confirm=True` (see parameter added
    below). When risky actions are detected this returns `requires_confirmation`
    + `pending_actions` describing what would be applied.
    """
    # Preserve previous behaviour for direct callers: execute immediately.
    return _execute_intent_payload(payload, confirm=True, user_id=user_id)


def _execute_intent_payload(payload: CopilotIntentPayload, confirm: bool = False, user_id: str = "anonymous") -> Dict[str, Any]:
    actions: Dict[str, Any] = {
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_suggested": [],
        "grocery_list": [],
        "grocery_items_added": [],
        "expenses_logged": [],
        "income_logged": [],
        "balance_reconciliations": [],
        "shopping_trip_corrections": [],
        "bills_added": [],
        "bills_updated": [],
        "bills_removed": [],
        "target_meals": None,
    }

    # Risk thresholds
    BILL_CONFIRM_THRESHOLD = 50.0
    EXPENSE_CONFIRM_THRESHOLD = 50.0

    # Partition risky vs safe items
    safe_bills = []
    risky_bills = []
    for b in (payload.bill_adjustments or []):
        amt = b.amount
        if b.adjustment_type == "remove":
            risky_bills.append(b)
        elif amt is None:
            risky_bills.append(b)
        else:
            try:
                if float(amt) >= BILL_CONFIRM_THRESHOLD:
                    risky_bills.append(b)
                else:
                    safe_bills.append(b)
            except Exception:
                risky_bills.append(b)

    safe_expenses = []
    risky_expenses = []
    for e in (payload.expenses or []):
        amt = e.estimated_amount
        if amt is None:
            risky_expenses.append(e)
        else:
            try:
                if float(amt) >= EXPENSE_CONFIRM_THRESHOLD:
                    risky_expenses.append(e)
                else:
                    safe_expenses.append(e)
            except Exception:
                risky_expenses.append(e)

    pending_actions = {"bills": [b.model_dump() for b in risky_bills], "expenses": [e.model_dump() for e in risky_expenses]}
    risky_balance = bool(payload.balance_reconciliation)
    risky_corrections = list(payload.shopping_corrections or [])
    if risky_balance:
        pending_actions["balance_reconciliations"] = [payload.balance_reconciliation]
    if risky_corrections:
        pending_actions["shopping_trip_corrections"] = risky_corrections

    # If not confirming, only apply safe actions and return pending for risky
    if not confirm and (risky_bills or risky_expenses or risky_balance or risky_corrections):
        # Apply non-risky bits (groceries, meals, safe bills/expenses)
        if payload.meal_request:
            meal_actions = resolve_meal_request(payload.meal_request)
            actions.update(meal_actions)
            actions["target_meals"] = payload.meal_request.total_count

        if payload.groceries:
            grocery_actions = resolve_grocery_additions(payload.groceries)
            actions.update(grocery_actions)

        if safe_expenses:
            expense_actions = resolve_expenses(safe_expenses)
            actions.update(expense_actions)

        if safe_bills:
            bill_actions = resolve_bill_adjustments(safe_bills, payload.clarification_flags)
            actions.update(bill_actions)

        if payload.income_events:
            hid = current_household_id()
            account = _household_account()
            for row in payload.income_events:
                amount = round(float(row.get("amount") or 0.0), 2)
                source = _normalize_text(str(row.get("source") or "income")) or "income"
                note = _normalize_text(str(row.get("note") or ""))
                tx = ExpenseTransaction(
                    household_id=hid,
                    description=(note or source.title()),
                    amount=amount,
                    category="income",
                    source="manual",
                    local_account_id=account.id if account else None,
                )
                db.session.add(tx)
                apply_balance_delta(hid, amount)
                actions["income_logged"].append({
                    "description": tx.description,
                    "source": source,
                    "amount": amount,
                    "note": note or None,
                })
            db.session.commit()

        actions["clarification_flags"] = payload.clarification_flags.model_dump()
        actions["summary"] = build_intent_summary(actions, payload.clarification_flags)
        actions["requires_confirmation"] = True
        actions["pending_actions"] = pending_actions
        return actions

    # Confirmed path: apply everything
    if payload.meal_request:
        meal_actions = resolve_meal_request(payload.meal_request)
        actions.update(meal_actions)
        actions["target_meals"] = payload.meal_request.total_count

    if payload.groceries:
        grocery_actions = resolve_grocery_additions(payload.groceries)
        actions.update(grocery_actions)

    if payload.expenses:
        expense_actions = resolve_expenses(payload.expenses)
        actions.update(expense_actions)

    if payload.income_events:
        hid = current_household_id()
        account = _household_account()
        for row in payload.income_events:
            amount = round(float(row.get("amount") or 0.0), 2)
            source = _normalize_text(str(row.get("source") or "income")) or "income"
            note = _normalize_text(str(row.get("note") or ""))
            tx = ExpenseTransaction(
                household_id=hid,
                description=(note or source.title()),
                amount=amount,
                category="income",
                source="manual",
                local_account_id=account.id if account else None,
            )
            db.session.add(tx)
            apply_balance_delta(hid, amount)
            actions["income_logged"].append({
                "description": tx.description,
                "source": source,
                "amount": amount,
                "note": note or None,
            })
        db.session.commit()

    if payload.balance_reconciliation:
        account = _household_account()
        target = _normalize_amount((payload.balance_reconciliation or {}).get("target_balance"))
        if account and target is not None:
            previous_balance = round(float(account.checking_balance or 0.0), 2)
            set_balance_absolute(current_household_id(), round(float(target), 2))
            db.session.commit()
            actions["balance_reconciliations"].append({
                "previous_balance": previous_balance,
                "new_balance": round(float(target), 2),
                "difference": round(float(target) - previous_balance, 2),
                "reason": _normalize_text(str((payload.balance_reconciliation or {}).get("reason") or "manual_reconciliation")) or "manual_reconciliation",
            })

    if payload.shopping_corrections:
        hid = current_household_id()
        for row in payload.shopping_corrections:
            trip = _resolve_shopping_trip_completion(row)
            if trip is None:
                continue
            target = _normalize_amount(row.get("new_actual_total"))
            if target is None:
                continue
            old_actual = round(float(trip.actual_total_cents or 0) / 100.0, 2)
            new_actual = round(float(target), 2)
            delta = round(new_actual - old_actual, 2)
            trip.actual_total_cents = int(round(new_actual * 100))
            trip.amount_source = "manual_correction"
            txn = _tx_query().filter_by(id=trip.transaction_id).first()
            previous_txn_amount = None
            if txn is not None:
                previous_txn_amount = round(float(txn.amount or 0.0), 2)
                txn.amount = new_actual
                db.session.add(txn)
            apply_balance_delta(hid, -delta)
            db.session.add(trip)
            actions["shopping_trip_corrections"].append({
                "id": trip.id,
                "operation_id": trip.operation_id,
                "trip_token": trip.trip_token,
                "transaction_id": trip.transaction_id,
                "previous_actual_total": old_actual,
                "new_actual_total": new_actual,
                "difference": delta,
                "previous_transaction_amount": previous_txn_amount,
            })
        db.session.commit()

    if payload.bill_adjustments:
        bill_actions = resolve_bill_adjustments(payload.bill_adjustments, payload.clarification_flags)
        actions.update(bill_actions)

    actions["clarification_flags"] = payload.clarification_flags.model_dump()
    actions["summary"] = build_intent_summary(actions, payload.clarification_flags)

    # Audit what we applied
    try:
        from app import record_action_audit
        token = record_action_audit(
            actions,
            raw_text=(payload.raw_user_text or ""),
            source="copilot_intent",
            user_id=user_id,
        )
        actions["undo_token"] = token
    except Exception:
        # non-fatal; auditing is best-effort
        pass

    return actions


def _tool_results_to_actions(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    actions = {
        "bills_added": [],
        "bills_removed": [],
        "expenses_logged": [],
        "grocery_items_added": [],
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_removed": [],
        "recipes_suggested": [],
        "target_meals": None,
    }
    for tr in tool_results:
        tool_name = tr.get("tool", "")
        status = tr.get("status", "")
        data = tr.get("data") or {}
        if status != "ok":
            continue
        if tool_name == "add_recurring_bill":
            actions["bills_added"].append({
                "name": data.get("name", ""),
                "amount": data.get("amount", 0),
            })
        elif tool_name == "add_grocery_item":
            item = data.get("item_name", "").lower()
            if item:
                actions["grocery_items_added"].append(item)
        elif tool_name == "select_active_recipe":
            act = data.get("action", "")
            if act == "added":
                actions["recipes_added"].append({"id": data.get("id"), "title": data.get("title", "")})
            elif act == "removed":
                actions["recipes_removed"].append({"id": data.get("id"), "title": data.get("title", "")})
        elif tool_name == "log_discretionary_expense":
            actions["expenses_logged"].append({"description": data.get("description", ""), "amount": data.get("amount", 0)})
        elif tool_name == "set_target_meals":
            actions["target_meals"] = data.get("target_meals")
    return actions


def _preview_meal_request(meal_request: MealRequest) -> Dict[str, Any]:
    """Build a meal-plan preview without mutating DB state."""

    preview: Dict[str, Any] = {
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_suggested": [],
        "grocery_list": [],
        "target_meals": meal_request.total_count,
        "meal_servings": meal_request.servings,
    }

    existing_plan_ids = {item.recipe_id for item in _meal_plan_query().all()}
    planned_ids = set(existing_plan_ids)
    selected: List[Any] = []
    recommendation_seed_ids: List[int] = []

    for requirement in meal_request.specific_requirements:
        recipe = _match_recipe_by_title_or_keyword(requirement)
        if recipe is None:
            preview["recipes_suggested"].append(_unresolved_recipe_action(requirement, action="add"))
            continue
        if recipe.id in planned_ids or len(planned_ids) >= 14:
            continue
        selected.append(recipe)
        planned_ids.add(recipe.id)
        recommendation_seed_ids.append(recipe.id)
        preview["recipes_added"].append({"id": recipe.id, "title": recipe.title})

    if len(planned_ids) < meal_request.total_count:
        fill = min(meal_request.total_count - len(planned_ids), 14 - len(planned_ids))
        for rid in _favorite_and_history_recipe_ids(limit=fill * 2):
            if rid in planned_ids:
                continue
            rec = Recipe.query.get(rid)
            if rec is None:
                continue
            selected.append(rec)
            planned_ids.add(rec.id)
            recommendation_seed_ids.append(rec.id)
            preview["recipes_auto_filled"].append({"id": rec.id, "title": rec.title})
            if len(planned_ids) >= meal_request.total_count:
                break

    if len(planned_ids) < meal_request.total_count:
        fill = min(meal_request.total_count - len(planned_ids), 14 - len(planned_ids))
        seed_ids = list(dict.fromkeys(recommendation_seed_ids + list(planned_ids)))
        for rec in _recommend_recipes(list(planned_ids), limit=fill, seed_ids=seed_ids):
            if rec.id in planned_ids:
                continue
            selected.append(rec)
            planned_ids.add(rec.id)
            preview["recipes_auto_filled"].append({"id": rec.id, "title": rec.title})

    if len(planned_ids) < meal_request.total_count and not recommendation_seed_ids:
        starter_ids = _starter_recipe_ids(limit=meal_request.total_count - len(planned_ids))
        for rid in starter_ids:
            if rid in planned_ids:
                continue
            rec = Recipe.query.get(rid)
            if rec is None:
                continue
            selected.append(rec)
            planned_ids.add(rec.id)
            preview["recipes_auto_filled"].append({"id": rec.id, "title": rec.title})

    if selected:
        preview["grocery_list"] = _aggregate_ingredients(selected, meal_request.servings)

    return preview


def stage_intent_payload(payload: CopilotIntentPayload, user_id: str = "anonymous") -> Dict[str, Any]:
    """Create a dry-run action preview for human review.

    This function never mutates the database.
    """
    actions: Dict[str, Any] = {
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_suggested": [],
        "grocery_list": [],
        "grocery_items_added": [],
        "expenses_logged": [],
        "income_logged": [],
        "balance_reconciliations": [],
        "shopping_trip_corrections": [],
        "bills_added": [],
        "bills_updated": [],
        "bills_removed": [],
        "target_meals": None,
        "meal_servings": None,
    }

    if payload.meal_request:
        meal_preview = _preview_meal_request(payload.meal_request)
        actions["recipes_added"] = meal_preview.get("recipes_added", [])
        actions["recipes_auto_filled"] = meal_preview.get("recipes_auto_filled", [])
        actions["recipes_suggested"] = meal_preview.get("recipes_suggested", [])
        actions["grocery_list"] = meal_preview.get("grocery_list", [])
        actions["target_meals"] = meal_preview.get("target_meals")
        actions["meal_servings"] = meal_preview.get("meal_servings")

    for grocery in payload.groceries or []:
        if not grocery.item_name:
            continue
        resolved_name = (
            grocery.item_name
            if grocery.has_explicit_specificity
            else _resolve_preferred_grocery_item(grocery.item_name)
        )
        actions["grocery_items_added"].append({
            "item_name": resolved_name.lower(),
            "base_item": (grocery.base_item or grocery.item_name).lower(),
            "brand": grocery.brand,
            "variant": grocery.variant,
            "quantity": grocery.quantity,
            "unit": grocery.unit,
            "requested_package_size": grocery.requested_package_size,
            "category": grocery.category or "General",
        })

    for expense in payload.expenses or []:
        amount = expense.estimated_amount
        if amount is None:
            amount = _historical_average_expense(expense.category)
        direction = "inflow" if str(expense.category or "").strip().lower() == "income" else "outflow"
        merchant_or_desc = _normalize_text(expense.merchant or expense.description or "")
        candidates = detect_plaid_candidates_for_manual_input(
            owner_scope=user_id,
            amount=round(float(amount or 0.0), 2),
            direction=direction,
            merchant_or_description=merchant_or_desc,
            transaction_date=expense.transaction_date,
        )

        actions["expenses_logged"].append({
            "description": _normalize_text(expense.description or expense.category.title()) or expense.category.title(),
            "category": expense.category,
            "amount": round(float(amount or 0.0), 2),
            "merchant": expense.merchant,
            "transaction_date": expense.transaction_date,
            "candidate_plaid_transactions": candidates,
            "reconciliation_action": "",
            "selected_plaid_transaction_id": (candidates[0]["plaid_transaction_id"] if len(candidates) == 1 else None),
        })

    for row in payload.income_events or []:
        amount = _normalize_amount(row.get("amount"))
        if amount is None:
            continue
        income_candidates = detect_plaid_candidates_for_manual_input(
            owner_scope=user_id,
            amount=round(float(amount), 2),
            direction="inflow",
            merchant_or_description=_normalize_text(str(row.get("source") or row.get("note") or "income")),
            transaction_date=_normalize_text(str(row.get("date") or "")) or None,
        )
        actions["income_logged"].append({
            "source": _normalize_text(str(row.get("source") or "income")) or "income",
            "amount": round(float(amount), 2),
            "date": _normalize_text(str(row.get("date") or "")) or None,
            "note": _normalize_text(str(row.get("note") or "")) or None,
            "candidate_plaid_transactions": income_candidates,
            "reconciliation_action": "",
            "selected_plaid_transaction_id": (income_candidates[0]["plaid_transaction_id"] if len(income_candidates) == 1 else None),
        })

    account = _household_account()
    if payload.balance_reconciliation and account:
        target = _normalize_amount(payload.balance_reconciliation.get("target_balance"))
        if target is not None:
            current_balance = round(float(account.checking_balance or 0.0), 2)
            target_balance = round(float(target), 2)
            actions["balance_reconciliations"].append({
                "current_balance": current_balance,
                "new_balance": target_balance,
                "difference": round(target_balance - current_balance, 2),
                "reason": _normalize_text(str(payload.balance_reconciliation.get("reason") or "manual_reconciliation")) or "manual_reconciliation",
            })

    for correction in payload.shopping_corrections or []:
        trip = _resolve_shopping_trip_completion(correction)
        if trip is None:
            _set_clarification_once(
                payload.clarification_flags,
                "I could not find the completed shopping trip to correct. Please provide the trip token or operation ID.",
            )
            continue
        target = _normalize_amount(correction.get("new_actual_total"))
        if target is None:
            _set_clarification_once(
                payload.clarification_flags,
                "I need the corrected actual amount for the shopping trip.",
            )
            continue
        old_actual = round(float(trip.actual_total_cents or 0) / 100.0, 2)
        new_actual = round(float(target), 2)
        txn = _tx_query().filter_by(id=trip.transaction_id).first()
        actions["shopping_trip_corrections"].append({
            "id": trip.id,
            "operation_id": trip.operation_id,
            "trip_token": trip.trip_token,
            "transaction_id": trip.transaction_id,
            "retailer": trip.retailer,
            "store_name": trip.store_name,
            "planned_total": round(float(trip.planned_total_cents or 0) / 100.0, 2),
            "previous_actual_total": old_actual,
            "new_actual_total": new_actual,
            "difference": round(new_actual - old_actual, 2),
            "previous_transaction_amount": round(float(txn.amount), 2) if txn else old_actual,
        })

    for bill in payload.bill_adjustments or []:
        existing = _find_bill_by_name(bill.bill_name)
        if bill.adjustment_type == "remove":
            if existing:
                actions["bills_removed"].append({
                    "id": existing.id,
                    "name": existing.name,
                    "amount": float(existing.amount or 0.0),
                })
            else:
                _set_clarification_once(
                    payload.clarification_flags,
                    f"Could not remove '{bill.bill_name}' because no matching recurring bill was found.",
                )
            continue

        baseline = _historical_bill_amount(bill.bill_name)
        if existing:
            next_amount: Optional[float] = bill.amount
            if next_amount is None and bill.adjustment_type in {"increase", "decrease"}:
                _set_clarification_once(
                    payload.clarification_flags,
                    f"I need the delta amount to adjust '{bill.bill_name}'.",
                )
                continue
            if next_amount is None:
                next_amount = baseline
            if next_amount is None:
                _set_clarification_once(
                    payload.clarification_flags,
                    f"I need an amount to set up or adjust '{bill.bill_name}'.",
                )
                continue
            if bill.adjustment_type == "increase":
                next_amount = float(existing.amount) + float(next_amount)
            elif bill.adjustment_type == "decrease":
                next_amount = max(0.0, float(existing.amount) - float(next_amount))
            staged_update = {
                "id": existing.id,
                "name": existing.name,
                "amount": round(float(next_amount), 2),
            }
            if bill.due_day is not None:
                staged_update["due_date"] = _resolve_due_date_from_day(bill.due_day).isoformat()
            actions["bills_updated"].append(staged_update)
            continue

        new_amount: Optional[float] = bill.amount
        if bill.adjustment_type in {"increase", "decrease"}:
            if new_amount is None:
                _set_clarification_once(
                    payload.clarification_flags,
                    f"I need the delta amount to adjust '{bill.bill_name}'.",
                )
                continue
            if baseline is None:
                _set_clarification_once(
                    payload.clarification_flags,
                    f"I couldn't find a recurring or historical baseline for '{bill.bill_name}'. Please share the baseline amount.",
                )
                continue
            if bill.adjustment_type == "increase":
                new_amount = float(baseline) + float(new_amount)
            else:
                new_amount = max(0.0, float(baseline) - float(new_amount))
        else:
            if new_amount is None:
                new_amount = baseline
            if new_amount is None:
                _set_clarification_once(
                    payload.clarification_flags,
                    f"I need an amount to set up or adjust '{bill.bill_name}'.",
                )
                continue

        staged_bill = {
            "name": bill.bill_name.title(),
            "amount": round(float(new_amount), 2),
        }
        if bill.due_day is not None:
            staged_bill["due_date"] = _resolve_due_date_from_day(bill.due_day).isoformat()
        actions["bills_added"].append(staged_bill)

    actions["clarification_flags"] = payload.clarification_flags.model_dump()
    actions["summary"] = build_intent_summary(actions, payload.clarification_flags)
    actions["requires_confirmation"] = True
    actions["staged"] = True
    actions["operation_id"] = _new_operation_id()
    return actions


def _apply_staged_actions_once(staged_actions: Dict[str, Any], raw_user_text: str = "", user_id: str = "anonymous") -> Dict[str, Any]:
    """Apply a reviewed staging payload.

    Accepts a user-editable staged action structure and persists it.
    """
    from models import Account, ActionAudit, Bill, ExpenseTransaction, GroceryItem, Recipe
    from extensions import db
    from app import record_action_audit

    operation_id = _extract_operation_id(staged_actions)
    operation_fp = _operation_fingerprint(staged_actions)

    existing = _audit_query().filter_by(operation_id=operation_id).first()
    if existing:
        return _replay_matching_operation(existing, operation_fp)

    normalized = _normalize_staged_actions_for_apply(staged_actions, operation_id)

    applied: Dict[str, Any] = {
        "recipes_added": [],
        "recipes_auto_filled": [],
        "recipes_suggested": normalized.get("recipes_suggested", []),
        "recipes_rejected": [],
        "grocery_list": [],
        "grocery_items_added": [],
        "expenses_logged": [],
        "income_logged": [],
        "balance_reconciliations": [],
        "shopping_trip_corrections": [],
        "bills_added": [],
        "bills_updated": [],
        "bills_removed": [],
        "target_meals": normalized.get("target_meals"),
        "meal_servings": normalized.get("meal_servings"),
        "operation_id": operation_id,
        "already_applied": False,
        "reconciliation_existing_used": [],
    }

    unresolved_recipe_actions: List[Dict[str, Any]] = []
    recipe_substitutions: List[Dict[str, Any]] = []
    invalid_recipe_actions: List[Dict[str, Any]] = []

    for idx, rec in enumerate(normalized.get("recipes_suggested", []) or []):
        resolution = _parse_recipe_suggestion_resolution(rec, idx)
        kind = resolution.get("kind")
        if kind == "reject":
            applied["recipes_rejected"].append({
                "requested_title": resolution.get("requested_title", ""),
                "reason": resolution.get("reason", "user_rejected"),
            })
            continue
        if kind == "substitute":
            recipe_substitutions.append(resolution)
            continue
        if kind == "invalid":
            invalid_recipe_actions.append(resolution)
            continue
        unresolved_recipe_actions.append({
            "index": resolution.get("index"),
            "requested_title": resolution.get("requested_title", ""),
            "reason": resolution.get("reason", "recipe_not_found"),
            "status": "unresolved",
        })

    if invalid_recipe_actions:
        raise StagedActionValidationError(
            "Invalid staged recipe suggestion payload.",
            details={
                "code": "invalid_recipe_action_payload",
                "invalid_recipe_actions": invalid_recipe_actions,
                "operation_id": operation_id,
            },
        )

    if unresolved_recipe_actions:
        raise StagedActionValidationError(
            "Unresolved recipe requests remain. Reject or substitute each unresolved recipe before apply.",
            details={
                "code": "unresolved_recipe_actions",
                "unresolved_recipe_actions": unresolved_recipe_actions,
                "operation_id": operation_id,
            },
        )

    undo_token = uuid.uuid4().hex

    try:
        # Claim operation_id first so retries/double-clicks/concurrent apply
        # attempts cannot execute duplicate side effects.
        record_action_audit(
            {
                "operation_id": operation_id,
                "operation_fingerprint": operation_fp,
                "status": "in_progress",
            },
            raw_text=(raw_user_text or ""),
            source="copilot_staged_apply",
            user_id=user_id,
            operation_id=operation_id,
            undo_token=undo_token,
            commit=False,
        )
        db.session.flush()

        recipes_to_add: List[Tuple[int, str]] = []
        for key in ("recipes_added", "recipes_auto_filled"):
            for rec in normalized.get(key, []) or []:
                rid = rec.get("id")
                if isinstance(rid, int):
                    recipes_to_add.append((rid, key))

        for resolved in recipe_substitutions:
            rid = int(resolved["recipe_id"])
            recipes_to_add.append((rid, "recipes_added"))

        invalid_recipe_ids: List[Dict[str, Any]] = []
        validated_to_add: List[Tuple[int, str, Optional[str]]] = []
        for rid, source_key in recipes_to_add:
            rec = Recipe.query.get(rid)
            if rec is None:
                invalid_recipe_ids.append({"recipe_id": rid, "source": source_key})
                continue
            requested = None
            if source_key == "recipes_added":
                for sub in recipe_substitutions:
                    if int(sub["recipe_id"]) == rid:
                        requested = sub.get("requested_title")
                        break
            validated_to_add.append((rid, source_key, requested))

        if invalid_recipe_ids:
            raise StagedActionValidationError(
                "One or more recipe IDs are invalid.",
                details={
                    "code": "invalid_recipe_id",
                        "invalid_recipe_actions": invalid_recipe_ids,
                        "operation_id": operation_id,
                    },
                )

        added_recipe_ids: List[int] = []
        for rid, source_key, requested_title in validated_to_add:
            source = "copilot" if source_key == "recipes_added" else "autofill"
            if _add_recipe_to_plan(rid, source):
                recipe = Recipe.query.get(rid)
                title = recipe.title if recipe else ""
                row = {"id": rid, "title": title}
                if requested_title:
                    row["requested_title"] = requested_title
                    row["resolution"] = "substituted"
                applied[source_key].append(row)
                added_recipe_ids.append(rid)

        grocery_candidates = normalized.get("grocery_list", []) or []
        if not grocery_candidates and added_recipe_ids:
            servings = int(normalized.get("meal_servings") or 4)
            selected_recipes = [Recipe.query.get(rid) for rid in added_recipe_ids]
            selected_recipes = [r for r in selected_recipes if r is not None]
            grocery_candidates = _aggregate_ingredients(selected_recipes, servings)

        for item in grocery_candidates:
            if not isinstance(item, dict):
                continue
            item_name = _normalize_text(str(item.get("item_name", "")))
            if not item_name:
                continue
            est_price = _normalize_amount(item.get("estimated_price"))
            if est_price is None:
                est_price = 0.0
            gro = GroceryItem(
                household_id=current_household_id(),
                item_name=item_name,
                estimated_price=float(est_price),
                store_name="Local Store",
                recipe_ids=",".join(str(rid) for rid in added_recipe_ids),
            )
            db.session.add(gro)
            db.session.flush()
            applied["grocery_list"].append({
                "id": gro.id,
                "item_name": gro.item_name,
                "clean_keyword": _normalize_text(str(item.get("clean_keyword" or ""))),
                "quantity": round(float(item.get("quantity") or 0.0), 2),
                "unit": _normalize_text(str(item.get("unit") or "unit")) or "unit",
                "estimated_price": float(gro.estimated_price or 0.0),
                "store_name": gro.store_name,
                "recipe_ids": gro.recipe_ids,
            })

        hid = current_household_id()
        account = _household_account()
        
        for item in normalized.get("grocery_items_added", []) or []:
            name = _normalize_text(str(item.get("item_name", "")))
            estimated_price = _normalize_amount(item.get("estimated_price"))
            if estimated_price is None:
                estimated_price = 0.0
            category = _normalize_text(str(item.get("category") or "General")) or "General"
            gi = GroceryItem(
                household_id=hid,
                item_name=name.title(),
                estimated_price=float(estimated_price),
                store_name=(get_selected_store(hid, account=account).get("name") or "Local Store"),
                shopping_requirement_json=json.dumps({
                    "item_name": name,
                    "base_item": item.get("base_item") or name.lower(),
                    "brand": item.get("brand"),
                    "variant": item.get("variant"),
                    "quantity": float(item.get("quantity") or 1.0),
                    "unit": item.get("unit"),
                    "requested_package_size": item.get("requested_package_size"),
                    "category": category,
                }),
            )
            db.session.add(gi)
            db.session.flush()
            applied["grocery_items_added"].append({
                "id": gi.id,
                "item_name": gi.item_name,
                "base_item": item.get("base_item") or name.lower(),
                "brand": item.get("brand"),
                "variant": item.get("variant"),
                "quantity": float(item.get("quantity") or 1.0),
                "unit": item.get("unit"),
                "requested_package_size": item.get("requested_package_size"),
                "estimated_price": float(gi.estimated_price or 0.0),
                "store_name": gi.store_name,
                "category": category,
            })

        for exp in normalized.get("expenses_logged", []) or []:
            category = _normalize_text(str(exp.get("category") or "discretionary")).lower() or "discretionary"
            amount = round(float(exp.get("amount") or 0.0), 2)
            description = _normalize_text(str(exp.get("description") or category.title())) or category.title()

            candidate_ids: list[str] = []
            for cand in exp.get("candidate_plaid_transactions") or []:
                if isinstance(cand, dict):
                    txid = _normalize_text(str(cand.get("plaid_transaction_id") or ""))
                else:
                    txid = _normalize_text(str(cand or ""))
                if txid:
                    candidate_ids.append(txid)
            selected_plaid_id = _normalize_text(str(exp.get("selected_plaid_transaction_id") or "")) or None
            reconciliation_action = _normalize_text(str(exp.get("reconciliation_action") or "")).lower()

            if candidate_ids:
                if reconciliation_action not in {"use_existing", "record_another"}:
                    raise StagedActionValidationError(
                        "Choose Use Existing Transaction or Record Another for possible bank duplicates.",
                        details={
                            "code": "reconciliation_choice_required",
                            "operation_id": operation_id,
                            "expense_description": description,
                        },
                    )
                if selected_plaid_id is None and len(candidate_ids) == 1:
                    selected_plaid_id = candidate_ids[0]
                if selected_plaid_id and selected_plaid_id not in candidate_ids:
                    raise StagedActionValidationError(
                        "selected_plaid_transaction_id must be one of the proposed candidates.",
                        details={
                            "code": "invalid_reconciliation_candidate",
                            "operation_id": operation_id,
                            "selected_plaid_transaction_id": selected_plaid_id,
                        },
                    )
                if reconciliation_action == "use_existing":
                    if selected_plaid_id:
                        ensure_plaid_effect_exists(owner_scope=user_id, plaid_transaction_id=selected_plaid_id)
                    applied["reconciliation_existing_used"].append({
                        "description": description,
                        "amount": amount,
                        "plaid_transaction_id": selected_plaid_id,
                    })
                    continue

            tx = ExpenseTransaction(
                household_id=hid,
                description=description,
                amount=amount,
                category=category,
                source="manual",
                local_account_id=account.id if account else None,
            )
            db.session.add(tx)
            db.session.flush()
            apply_balance_delta(hid, -amount)

            if reconciliation_action == "record_another" and selected_plaid_id:
                keep_separate_after_manual_creation(
                    owner_scope=user_id,
                    manual_transaction_id=tx.id,
                    plaid_transaction_id=selected_plaid_id,
                )

            applied["expenses_logged"].append({
                "id": tx.id,
                "description": description,
                "category": category,
                "amount": amount,
                "merchant": exp.get("merchant"),
                "transaction_date": exp.get("transaction_date"),
                "reconciliation_action": reconciliation_action or None,
                "selected_plaid_transaction_id": selected_plaid_id,
            })

        for row in normalized.get("income_logged", []) or []:
            amount = round(float(row.get("amount") or 0.0), 2)
            source = _normalize_text(str(row.get("source") or "income")) or "income"
            note = _normalize_text(str(row.get("note") or ""))
            description = note or source.title()

            income_candidate_ids: list[str] = []
            for cand in row.get("candidate_plaid_transactions") or []:
                if isinstance(cand, dict):
                    txid = _normalize_text(str(cand.get("plaid_transaction_id") or ""))
                else:
                    txid = _normalize_text(str(cand or ""))
                if txid:
                    income_candidate_ids.append(txid)
            income_action = _normalize_text(str(row.get("reconciliation_action") or "")).lower()
            income_selected = _normalize_text(str(row.get("selected_plaid_transaction_id") or "")) or None

            if income_candidate_ids:
                if income_action not in {"use_existing", "record_another"}:
                    raise StagedActionValidationError(
                        "Choose Use Existing Transaction or Record Another for possible bank duplicates.",
                        details={
                            "code": "reconciliation_choice_required",
                            "operation_id": operation_id,
                            "income_source": source,
                        },
                    )
                if income_selected is None and len(income_candidate_ids) == 1:
                    income_selected = income_candidate_ids[0]
                if income_selected and income_selected not in income_candidate_ids:
                    raise StagedActionValidationError(
                        "selected_plaid_transaction_id must be one of the proposed candidates.",
                        details={
                            "code": "invalid_reconciliation_candidate",
                            "operation_id": operation_id,
                            "selected_plaid_transaction_id": income_selected,
                        },
                    )
                if income_action == "use_existing":
                    if income_selected:
                        ensure_plaid_effect_exists(owner_scope=user_id, plaid_transaction_id=income_selected)
                    applied["reconciliation_existing_used"].append({
                        "description": description,
                        "amount": amount,
                        "plaid_transaction_id": income_selected,
                    })
                    continue

            tx = ExpenseTransaction(
                household_id=hid,
                description=description,
                amount=amount,
                category="income",
                source="manual",
                local_account_id=account.id if account else None,
            )
            db.session.add(tx)
            db.session.flush()
            apply_balance_delta(hid, amount)

            if income_action == "record_another" and income_selected:
                keep_separate_after_manual_creation(
                    owner_scope=user_id,
                    manual_transaction_id=tx.id,
                    plaid_transaction_id=income_selected,
                )

            applied["income_logged"].append({
                "id": tx.id,
                "source": source,
                "note": note or None,
                "amount": amount,
                "description": description,
                "date": row.get("date"),
                "reconciliation_action": income_action or None,
                "selected_plaid_transaction_id": income_selected,
            })

        for row in normalized.get("balance_reconciliations", []) or []:
            if account is None:
                continue
            previous_balance = round(float(account.checking_balance or 0.0), 2)
            new_balance = round(float(row.get("new_balance") or previous_balance), 2)
            set_balance_absolute(hid, new_balance)
            applied["balance_reconciliations"].append({
                "previous_balance": previous_balance,
                "new_balance": new_balance,
                "difference": round(new_balance - previous_balance, 2),
                "reason": _normalize_text(str(row.get("reason") or "manual_reconciliation")) or "manual_reconciliation",
            })

        for row in normalized.get("shopping_trip_corrections", []) or []:
            trip = _resolve_shopping_trip_completion(row)
            if trip is None:
                continue
            txn = _tx_query().filter_by(id=trip.transaction_id).first()
            old_actual = round(float(trip.actual_total_cents or 0) / 100.0, 2)
            new_actual = round(float(row.get("new_actual_total") or old_actual), 2)
            delta = round(new_actual - old_actual, 2)
            trip.actual_total_cents = int(round(new_actual * 100))
            trip.amount_source = "manual_correction"
            db.session.add(trip)
            previous_txn_amount = None
            if txn is not None:
                previous_txn_amount = round(float(txn.amount or 0.0), 2)
                txn.amount = new_actual
                db.session.add(txn)
            # Keep financial side effects as a difference-only adjustment.
            apply_balance_delta(hid, -delta)
            applied["shopping_trip_corrections"].append({
                "id": trip.id,
                "operation_id": trip.operation_id,
                "trip_token": trip.trip_token,
                "transaction_id": trip.transaction_id,
                "planned_total": round(float(trip.planned_total_cents or 0) / 100.0, 2),
                "previous_actual_total": old_actual,
                "new_actual_total": new_actual,
                "difference": delta,
                "previous_transaction_amount": previous_txn_amount,
            })

        for bill in normalized.get("bills_removed", []) or []:
            bid = bill.get("id")
            existing = _bill_query().filter_by(id=bid).first() if isinstance(bid, int) else None
            if existing is None:
                name = _normalize_text(str(bill.get("name", "")))
                if name:
                    existing = _bill_query().filter(Bill.name.ilike(f"%{name}%")).first()
            
            if existing:
                applied["bills_removed"].append({
                    "id": existing.id,
                    "name": existing.name,
                    "amount": float(existing.amount or 0.0),
                    "due_date": existing.due_date.isoformat() if existing.due_date else None,
                    "is_paid": bool(existing.is_paid),
                    "is_gas_estimate": bool(existing.is_gas_estimate),
                })
                db.session.delete(existing)

        for bill in normalized.get("bills_updated", []) or []:
            bid = bill.get("id")
            existing = _bill_query().filter_by(id=bid).first() if isinstance(bid, int) else None
            if existing is None:
                name = _normalize_text(str(bill.get("name", "")))
                if name:
                    existing = _bill_query().filter(Bill.name.ilike(f"%{name}%")).first()
            
            if existing is None:
                continue
            prior_amount = float(existing.amount or 0.0)
            amount = float(bill.get("amount") or 0.0)
            existing.amount = amount
            due_date = _parse_staged_due_date(bill.get("due_date"))
            if due_date is not None:
                existing.due_date = due_date
            db.session.add(existing)
            applied["bills_updated"].append({
                "id": existing.id,
                "name": existing.name,
                "previous_amount": prior_amount,
                "amount": float(existing.amount or 0.0),
                "due_date": existing.due_date.isoformat() if existing.due_date else None,
            })

        for bill in normalized.get("bills_added", []) or []:
            name = _normalize_text(str(bill.get("name", "")))
            amount = float(bill.get("amount") or 0.0)
            due_date = _parse_staged_due_date(bill.get("due_date"))
            if due_date is None:
                due_date = datetime.utcnow() + timedelta(days=14)
            row = Bill(household_id=hid, name=name.title(), amount=float(amount), due_date=due_date)
            db.session.add(row)
            db.session.flush()
            applied["bills_added"].append({
                "id": row.id,
                "name": row.name,
                "amount": float(row.amount or 0.0),
                "due_date": row.due_date.isoformat() if row.due_date else None,
                "is_paid": bool(row.is_paid),
                "is_gas_estimate": bool(row.is_gas_estimate),
            })

        flags = ClarificationFlags()
        clar = normalized.get("clarification_flags") or {}
        if isinstance(clar, dict):
            flags.need_clarification = bool(clar.get("need_clarification", False))
            reasons = clar.get("clarification_reasons") or []
            if isinstance(reasons, list):
                flags.clarification_reasons = [str(r) for r in reasons if str(r).strip()]

        applied["clarification_flags"] = flags.model_dump()
        applied["summary"] = build_intent_summary(applied, flags)
        applied["operation_fingerprint"] = operation_fp

        # Finalize the claimed audit row in-place so writes + audit/undo
        # are committed atomically as one operation.
        claim = _audit_query().filter_by(operation_id=operation_id).first()
        if claim is None:
            claim = _audit_query().filter_by(undo_token=undo_token).first()
        if claim is None:
            raise RuntimeError("Failed to finalize staged operation audit claim.")
        claim.actions_json = json.dumps(applied)
        claim.source = "copilot_staged_apply"
        claim.raw_text = (raw_user_text or "")[:2000]
        claim.user_id = (user_id or "anonymous")[:80]
        db.session.add(claim)

        db.session.commit()
            
        applied["undo_token"] = undo_token
        return applied
    except IntegrityError:
        db.session.rollback()
        duplicate = _audit_query().filter_by(operation_id=operation_id).first()
        if duplicate:
            return _replay_matching_operation(duplicate, operation_fp)
        raise
    except Exception:
        db.session.rollback()
        raise


def apply_staged_actions(staged_actions: Dict[str, Any], raw_user_text: str = "", user_id: str = "anonymous") -> Dict[str, Any]:
    """Apply once, with bounded SQLite lock retry around the atomic operation.

    PostgreSQL converges through the household/operation unique claim. SQLite
    is local/disposable but can reject a losing concurrent writer before that
    writer observes the committed claim. Retrying the entire rolled-back
    transaction lets it converge on the winner without weakening integrity.
    """
    for attempt in range(5):
        try:
            return _apply_staged_actions_once(
                staged_actions, raw_user_text=raw_user_text, user_id=user_id
            )
        except OperationalError as exc:
            db.session.rollback()
            dialect = str(getattr(db.engine.dialect, "name", ""))
            locked = "database is locked" in str(exc).lower()
            if dialect != "sqlite" or not locked or attempt == 4:
                raise
            time.sleep(0.025 * (2 ** attempt))
    raise RuntimeError("Copilot staged apply retry loop exhausted.")


def process_copilot_command(
    user_text: str,
    groq_api_key: str = "",
    stage_only: bool = False,
    user_id: str = "anonymous",
) -> Dict[str, Any]:
    from services.copilot_service import parse_copilot_prompt

    try:
        parsed = parse_copilot_prompt(user_text, groq_api_key=groq_api_key, staging_only=stage_only)
    except TypeError:
        # Backward compatibility for tests/mocks that patch the older
        # parse_copilot_prompt(user_text, groq_api_key="") signature.
        parsed = parse_copilot_prompt(user_text, groq_api_key=groq_api_key)
    if parsed.get("tool_results"):
        parsed["actions_taken"] = _tool_results_to_actions(parsed.get("tool_results", []))
        return parsed

    intent_payload = parse_intent_payload(parsed, user_text)
    if stage_only:
        parsed["actions_taken"] = stage_intent_payload(intent_payload, user_id=user_id)
    else:
        parsed["actions_taken"] = execute_intent_payload(intent_payload, user_id=user_id)
    return parsed
