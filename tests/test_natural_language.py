from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Keep tests isolated from local user data.
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app, db, Account, ExpenseTransaction  # noqa: E402
from services.copilot_intent import (  # noqa: E402
    BillAdjustment,
    ClarificationFlags,
    CopilotIntentPayload,
    execute_intent_payload,
    parse_intent_payload,
    resolve_bill_adjustments,
)
from services.copilot_service import parse_copilot_prompt  # noqa: E402
import services.copilot_service as cs  # noqa: E402
from services.household_context import household_id as current_household_id


client = app.test_client()
app.testing = True


def _setup() -> None:
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=2500.0, pay_period_days=14, meals_per_day=3))
        db.session.add(ExpenseTransaction(household_id=current_household_id(), description="Phone bill payment", amount=85.0, category="utilities"))
        db.session.add(ExpenseTransaction(household_id=current_household_id(), description="Gas fill-up", amount=60.0, category="gas"))
        db.session.add(ExpenseTransaction(household_id=current_household_id(), description="Fuel station", amount=70.0, category="gas"))
        db.session.commit()


class _FakeMsg:
    def __init__(self, content: str, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeResponse:
    def __init__(self, msg: _FakeMsg):
        self.choices = [type("C", (), {"message": msg})()]


class _FakeCompletions:
    def __init__(self, resolver: Callable[[str], Dict[str, Any]]):
        self._resolver = resolver

    def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = m.get("content", "")
                break
        payload = self._resolver(user_text)
        return _FakeResponse(_FakeMsg(json.dumps(payload), []))


class _FakeGroq:
    def __init__(self, resolver: Callable[[str], Dict[str, Any]]):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(resolver)})()


def _parse_with_fake_semantic_llm(text: str, resolver: Callable[[str], Dict[str, Any]]) -> Dict[str, Any]:
    orig_groq = cs._Groq
    cs._Groq = lambda api_key: _FakeGroq(resolver)
    try:
        return parse_copilot_prompt(text, groq_api_key="gsk_fake")
    finally:
        cs._Groq = orig_groq


@pytest.mark.parametrize(
    "phrase",
    [
        "Can you handle dinners for this week?",
        "Need some meals sorted out.",
        "Give me a few dishes to cook.",
        "I need something to eat for the week.",
        "Help me feed four folks this pay period.",
    ],
)
def test_natural_language_meal_phrases_map_to_meal_payload(phrase: str):
    _setup()

    def resolver(_text: str) -> Dict[str, Any]:
        return {
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "bill_updates": [],
            "target_meals": 6,
            "meal_servings": 4,
            "clarification_question": None,
        }

    parsed = _parse_with_fake_semantic_llm(phrase, resolver)
    assert parsed.get("_fallback") is False
    payload = parse_intent_payload(parsed, phrase)
    assert payload.meal_request is not None
    assert payload.meal_request.total_count == 6
    assert payload.meal_request.servings == 4


@pytest.mark.parametrize(
    "phrase",
    [
        "my phone bill went up $10",
        "cell bill is ten bucks more",
        "adjust my mobile carrier payment",
    ],
)
def test_natural_language_bill_adjustment_phrases_map_to_bill_updates(phrase: str):
    _setup()

    def resolver(text: str) -> Dict[str, Any]:
        if "ten bucks more" in text:
            amount = 10.0
            action = "increase"
        elif "went up" in text:
            amount = 10.0
            action = "increase"
        else:
            amount = None
            action = "set"
        return {
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "bill_updates": [{"name": "phone", "action": action, "amount": amount}],
            "target_meals": None,
            "meal_servings": None,
            "clarification_question": None,
        }

    parsed = _parse_with_fake_semantic_llm(phrase, resolver)
    payload = parse_intent_payload(parsed, phrase)
    assert len(payload.bill_adjustments) == 1
    assert payload.bill_adjustments[0].bill_name == "phone"

    with app.app_context():
        actions = execute_intent_payload(payload)
    changed = actions.get("bills_added") or actions.get("bills_updated")
    assert changed


@pytest.mark.parametrize(
    "phrase",
    [
        "add gas",
        "put fuel down",
        "fill up the tank",
    ],
)
def test_natural_language_expense_phrases_map_to_one_time_expenses(phrase: str):
    _setup()

    def resolver(_text: str) -> Dict[str, Any]:
        return {
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [{"description": "gas", "amount": None}],
            "bill_updates": [],
            "target_meals": None,
            "meal_servings": None,
            "clarification_question": None,
        }

    parsed = _parse_with_fake_semantic_llm(phrase, resolver)
    payload = parse_intent_payload(parsed, phrase)
    assert len(payload.expenses) == 1
    assert payload.expenses[0].category == "gas"

    with app.app_context():
        actions = execute_intent_payload(payload)
    gas_events = [e for e in actions.get("expenses_logged", []) if e.get("description", "").lower() == "gas"]
    assert gas_events
    # Historical average from setup: (60 + 70) / 2 = 65
    assert gas_events[0]["amount"] == 65.0


def test_natural_language_contextual_defaults_before_clarification():
    _setup()

    parsed = {
        "selected_recipes": [],
        "grocery_additions": [],
        "discretionary_events": [],
        "bill_updates": [{"name": "phone", "action": "set", "amount": None}],
        "target_meals": None,
        "meal_servings": None,
        "clarification_question": None,
    }
    payload = parse_intent_payload(parsed, "adjust my mobile carrier payment")
    with app.app_context():
        actions = execute_intent_payload(payload)

    changed = actions.get("bills_added") or actions.get("bills_updated")
    assert changed
    assert changed[0]["amount"] == 85.0
    flags = actions.get("clarification_flags", {})
    assert flags.get("need_clarification") is False


def test_natural_language_single_precise_clarification_question():
    _setup()

    flags = ClarificationFlags()
    adjustments = [
        BillAdjustment(bill_name="unknown_service", adjustment_type="increase", amount=None),
        BillAdjustment(bill_name="other_service", adjustment_type="remove", amount=None),
    ]

    with app.app_context():
        _ = resolve_bill_adjustments(adjustments, flags)

    assert flags.need_clarification is True
    assert len(flags.clarification_reasons) == 1
    assert isinstance(flags.clarification_reasons[0], str)
    assert flags.clarification_reasons[0].strip() != ""
