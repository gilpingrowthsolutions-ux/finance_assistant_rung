"""
Rung AI Copilot — natural-language parser with native Groq Tool Calling.
=======================================================================

Parses free-form text into structured database actions using Groq's
native tool-calling API (function calling).  The LLM decides which
tools to call based on the user's intent, and the tool results are
executed locally against the Flask-SQLAlchemy database.

Provider auto-detection (in priority order):

  1. **Groq (tool calling)** — if a Groq API key is available, calls
     the Groq API with ``tools=APP_TOOLS`` (tool_choice="auto") and
     executes the chosen tools locally.

  2. **Groq (prompt-based)** — if the key is available but the tool
     call returns no tool_calls, parse the plain-text response as
     JSON (the old return path).

  3. **Ollama** — if ``OLLAMA_BASE_URL`` is set, calls a local
     Ollama instance (defaults to ``http://localhost:11434``).

  4. **Semantic degraded mode** — if no provider is configured,
      returns an empty parsed envelope with a single clarification
      question and ``_fallback: true``.

Usage
-----
    from services.copilot_service import parse_copilot_prompt

    result = parse_copilot_prompt(
        "Cook chicken rice bowl and fajitas this week. "
        "Also add Netflix $22.99/mo. I need dish soap and paper towels."
    )
    # result → {
    #     "tool_results": [
    #       {"tool": "select_active_recipe", "status": "ok", "data": {...}},
    #       {"tool": "add_recurring_bill", "status": "ok", "data": {...}},
    #       {"tool": "add_grocery_item", "status": "ok", "data": {...}},
    #     ],
    #     "selected_recipes": [...],
    #     "grocery_additions": [...],
    #     "discretionary_events": [...],
    #     "bill_updates": [...],
    #     "target_meals": null,
    #     "_fallback": false,
    # }
"""

from __future__ import annotations

import json as _json
import logging
import os
import re as _re
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator
from flask import has_app_context
from extensions import db

LOGGER = logging.getLogger("copilot_service")

_COPILOT_UNAVAILABLE_MESSAGE = "Copilot is temporarily unavailable. Please try again later."


def _new_parser_meta(path: str) -> Dict[str, Any]:
    return {
        "path": path,
        "llm_calls": 0,
        "repair_attempted": False,
        "validation": "unknown",
        "latency_ms": 0,
    }


def _finish_parser_meta(meta: Dict[str, Any], started: float) -> Dict[str, Any]:
    meta["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return meta


def _base_result() -> Dict[str, Any]:
    return {
        "tool_results": [],
        "selected_recipes": [],
        "grocery_additions": [],
        "shopping_requirements": [],
        "discretionary_events": [],
        "spending_events": [],
        "income_events": [],
        "balance_reconciliation": None,
        "shopping_corrections": [],
        "bill_updates": [],
        "target_meals": None,
        "meal_servings": None,
        "clarification_question": None,
        "_fallback": False,
    }


def _dedupe_dict_rows(rows: List[Dict[str, Any]], keys: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        sig = tuple((k, str(row.get(k) or "").strip().lower()) for k in keys)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(row)
    return out


def _dedupe_strings(rows: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in rows:
        text = str(item or "").strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_deterministic_result(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = _base_result()
    payload.update(data or {})
    payload["selected_recipes"] = _dedupe_dict_rows(
        [r for r in (payload.get("selected_recipes") or []) if isinstance(r, dict) and str(r.get("title") or "").strip()],
        ["title", "action"],
    )
    payload["grocery_additions"] = _dedupe_strings(payload.get("grocery_additions") or [])
    payload["shopping_requirements"] = _dedupe_dict_rows(
        [
            row for row in (payload.get("shopping_requirements") or [])
            if isinstance(row, dict) and str(row.get("item_name") or "").strip()
        ],
        ["item_name", "quantity", "unit", "requested_package_size"],
    )
    payload["discretionary_events"] = _dedupe_dict_rows(
        [r for r in (payload.get("discretionary_events") or []) if isinstance(r, dict) and str(r.get("description") or "").strip()],
        ["description", "amount"],
    )
    payload["spending_events"] = _dedupe_dict_rows(
        [
            r
            for r in (payload.get("spending_events") or [])
            if isinstance(r, dict)
            and (_normalize_text(r.get("merchant")) or _normalize_text(r.get("description")))
        ],
        ["merchant", "description", "category", "amount", "date"],
    )
    payload["income_events"] = _dedupe_dict_rows(
        [
            r
            for r in (payload.get("income_events") or [])
            if isinstance(r, dict) and _normalize_text(r.get("source")) and r.get("amount") is not None
        ],
        ["source", "amount", "date", "note"],
    )
    bal = payload.get("balance_reconciliation")
    if isinstance(bal, dict):
        target = bal.get("target_balance")
        try:
            target_float = float(target)
        except (TypeError, ValueError):
            target_float = None
        if target_float is None:
            payload["balance_reconciliation"] = None
        else:
            payload["balance_reconciliation"] = {
                "target_balance": target_float,
                "reason": _normalize_text(bal.get("reason")) or None,
            }
    else:
        payload["balance_reconciliation"] = None
    payload["shopping_corrections"] = _dedupe_dict_rows(
        [
            r
            for r in (payload.get("shopping_corrections") or [])
            if isinstance(r, dict) and r.get("new_actual_total") is not None
        ],
        ["operation_id", "trip_token", "selector", "new_actual_total"],
    )
    payload["bill_updates"] = _dedupe_dict_rows(
        [r for r in (payload.get("bill_updates") or []) if isinstance(r, dict) and str(r.get("name") or "").strip()],
        ["name", "action", "amount", "due_day"],
    )
    return payload


def _split_grocery_item_list(raw_value: Any) -> List[str]:
    """Split comma/and-delimited grocery phrases without breaking multi-word items."""
    if raw_value is None:
        return []
    if isinstance(raw_value, (list, tuple, set)):
        flattened: List[str] = []
        for item in raw_value:
            flattened.extend(_split_grocery_item_list(item))
        return flattened
    text = str(raw_value).strip()
    if not text:
        return []
    text = _re.sub(r",\s+(?=and\b)", " ", text, flags=_re.IGNORECASE)
    pieces = [piece.strip() for piece in _re.split(r"\s*(?:,\s*|\s+and\s+)\s*", text) if piece and piece.strip()]
    out: List[str] = []
    for piece in pieces:
        cleaned = piece.strip().strip(".,; ")
        if not cleaned or "$" in cleaned:
            continue
        out.append(cleaned.lower())
    return out


_SHOPPING_QUANTITY_WORDS = {
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
}


def _singularize_shopping_unit(value: str) -> str:
    unit = str(value or "").strip().lower()
    if unit.endswith("ies"):
        return unit[:-3] + "y"
    if unit.endswith("s") and not unit.endswith("ss"):
        return unit[:-1]
    return unit


def _extract_explicit_brand_and_variant(item_name: str) -> tuple[Optional[str], Optional[str], str]:
    text = str(item_name or "").strip()
    words = text.split()
    if len(words) < 2 or not words[0][:1].isupper():
        return None, None, text.lower()

    brand_words = [words[0]]
    offset = 1
    if len(words) >= 3 and words[1] == "&" and words[2][:1].isupper():
        brand_words.extend(words[1:3])
        offset = 3

    remainder = words[offset:]
    if not remainder:
        return " ".join(brand_words), None, text.lower()
    if "&" in brand_words and len(remainder) >= 2:
        return " ".join(brand_words), " ".join(remainder[:-1]), remainder[-1].lower()
    if len(remainder) <= 2:
        return " ".join(brand_words), None, " ".join(remainder).lower()

    base_item = " ".join(remainder[-2:]).lower()
    variant = " ".join(remainder[:-2]).strip() or None
    return " ".join(brand_words), variant, base_item


def _parse_shopping_requirement(raw_item: str) -> Dict[str, Any]:
    original = str(raw_item or "").strip().strip(".,; ")
    remainder = original
    quantity = 1.0
    unit: Optional[str] = None
    requested_package_size: Optional[str] = None

    package_match = _re.match(
        r"^(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)\s+"
        r"(\d+(?:\.\d+)?)\s*(oz|ounce|ounces|lb|lbs|pound|pounds|fl\s*oz|ml|l)\s+"
        r"([A-Za-z]+)\s+of\s+(.+)$",
        remainder,
        _re.IGNORECASE,
    )
    if package_match:
        raw_quantity, size_value, size_unit, container, remainder = package_match.groups()
        quantity = _SHOPPING_QUANTITY_WORDS.get(raw_quantity.lower(), float(raw_quantity) if raw_quantity[0].isdigit() else 1.0)
        unit = _singularize_shopping_unit(container)
        requested_package_size = f"{size_value} {size_unit.lower().replace('  ', ' ')}"
    else:
        quantity_match = _re.match(
            r"^(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)\s+"
            r"([A-Za-z]+)\s+of\s+(.+)$",
            remainder,
            _re.IGNORECASE,
        )
        if quantity_match:
            raw_quantity, raw_unit, remainder = quantity_match.groups()
            quantity = _SHOPPING_QUANTITY_WORDS.get(raw_quantity.lower(), float(raw_quantity) if raw_quantity[0].isdigit() else 1.0)
            unit = _singularize_shopping_unit(raw_unit)
        else:
            dozen_match = _re.match(r"^(a|an|one|two|\d+)?\s*dozen\s+(.+)$", remainder, _re.IGNORECASE)
            if dozen_match:
                raw_quantity, remainder = dozen_match.groups()
                quantity = _SHOPPING_QUANTITY_WORDS.get(str(raw_quantity or "a").lower(), float(raw_quantity) if raw_quantity and raw_quantity.isdigit() else 1.0)
                unit = "dozen"
            else:
                remainder = _re.sub(r"^(?:a|an)\s+", "", remainder, count=1, flags=_re.IGNORECASE)

    item_name = remainder.strip().strip(".,; ")
    brand, variant, base_item = _extract_explicit_brand_and_variant(item_name)
    return {
        "item_name": item_name,
        "base_item": base_item,
        "brand": brand,
        "variant": variant,
        "quantity": quantity,
        "unit": unit,
        "requested_package_size": requested_package_size,
        "category": "General",
    }


def _shopping_list_chunk(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    lower = raw.lower()

    embedded = _re.search(
        r"\b(?:add|get|buy|pick\s+up)\s+(.+?)\s+(?:to|on)\s+(?:my\s+|the\s+)?"
        r"(?:groceries|grocery(?:\s+list)?|shopping\s+list)\b",
        raw,
        _re.IGNORECASE,
    )
    if embedded:
        return embedded.group(1).strip()

    if "$" in raw or _re.search(r"(?:/mo(?:nth)?|per\s+month|monthly)\b", lower):
        return None
    if _re.search(r"\b(?:bill|expense|spent|checking|balance|meal\s+plan|meals?|recipes?|dinner)\b", lower):
        return None
    # General meal-planning phrasing should defer to semantic parsing.
    if _re.search(r"\b(?:something|anything)\s+to\s+eat\b", lower):
        return None
    if _re.search(r"\bfeed\b", lower) and _re.search(r"\b(?:week|pay\s*period)\b", lower):
        return None

    patterns = (
        r"^put\s+(.+?)\s+on\s+(?:my\s+|the\s+)?(?:grocery|shopping)(?:\s+list)?[.!?]*$",
        r"^(?:add|get|buy|pick\s+up)\s+(.+?)\s+(?:to|on)\s+(?:my\s+|the\s+)?(?:groceries|grocery(?:\s+list)?|shopping\s+list)[.!?]*$",
        r"^(?:add|get|buy|pick\s+up|i\s+need|we\s+need|we(?:'re|\s+are)\s+out\s+of)\s+(.+?)[.!?]*$",
    )
    for pattern in patterns:
        match = _re.match(pattern, raw, _re.IGNORECASE)
        if match:
            chunk = match.group(1).strip()
            if chunk.lower().startswith("to "):
                return None
            return chunk
    return None

def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

def _extract_usage_from_sdk_response(response: Any) -> Dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    prompt_tokens = _safe_int(getattr(usage, "prompt_tokens", None))
    completion_tokens = _safe_int(getattr(usage, "completion_tokens", None))
    if prompt_tokens is None and completion_tokens is None:
        return {}
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
    }

def _extract_usage_from_json_body(body: Dict[str, Any]) -> Dict[str, Any]:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}
    prompt_tokens = _safe_int(usage.get("prompt_tokens"))
    completion_tokens = _safe_int(usage.get("completion_tokens"))
    if prompt_tokens is None and completion_tokens is None:
        return {}
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
    }


def _looks_like_unsupported_grocery_remove(text: str) -> bool:
    return bool(
        _re.search(r"\bremove\s+.+\s+from\s+(?:my\s+)?grocery(?:\s+list)?\b", text, _re.IGNORECASE)
    )


def _looks_like_mark_bill_paid(text: str) -> bool:
    return bool(
        _re.search(r"\bmark\s+.+\s+bill\s+paid\b", text, _re.IGNORECASE)
    )


def _deterministic_fast_parse(text: str) -> Optional[Dict[str, Any]]:
    """High-confidence parser for common commands; returns None when uncertain."""
    raw = (text or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    payload = _base_result()

    # Manual balance reconciliation intents.
    balance_match = _re.search(
        r"\b(?:set|reconcile|update|adjust)\s+(?:my\s+)?(?:checking\s+)?balance\s+(?:to|at)\s+\$?(\d+(?:\.\d{1,2})?)\b",
        raw,
        _re.IGNORECASE,
    )
    if not balance_match:
        balance_match = _re.search(
            r"\b(?:my\s+)?(?:checking\s+)?balance\s+is\s+\$?(\d+(?:\.\d{1,2})?)\b",
            raw,
            _re.IGNORECASE,
        )
    if balance_match:
        payload["balance_reconciliation"] = {
            "target_balance": float(balance_match.group(1)),
            "reason": "manual_reconciliation",
        }

    # Income logging intents.
    for m in _re.finditer(
        r"\b(?:got\s+paid|received|deposit(?:ed)?|paycheck(?:\s+for)?|income(?:\s+of)?|add\s+income|log\s+income|record\s+income)\b[^$\d]{0,20}\$?(\d[\d,]*(?:\.\d{1,2})?)\b",
        raw,
        _re.IGNORECASE,
    ):
        parsed_amount = float(m.group(1).replace(",", ""))
        payload["income_events"].append({
            "source": "income",
            "amount": parsed_amount,
            "date": None,
            "note": None,
        })
    for m in _re.finditer(
        r"\b(?:add|log|record)\s+\$?(\d[\d,]*(?:\.\d{1,2})?)\s+(?:income|paycheck|deposit)\b",
        raw,
        _re.IGNORECASE,
    ):
        parsed_amount = float(m.group(1).replace(",", ""))
        payload["income_events"].append({
            "source": "income",
            "amount": parsed_amount,
            "date": None,
            "note": None,
        })

    # Finished-shopping correction intents.
    corr_operation = _re.search(
        r"\b(?:correct|update|adjust)\s+(?:a\s+)?(?:finished\s+shopping|shopping\s+trip|grocery\s+trip)\b[^\n]*?\b(op_[a-z0-9_]+)\b[^\n]*?\b(?:to|actual(?:\s+to)?|amount(?:\s+to)?)\s+\$?(\d+(?:\.\d{1,2})?)\b",
        raw,
        _re.IGNORECASE,
    )
    if corr_operation:
        payload["shopping_corrections"].append({
            "operation_id": corr_operation.group(1).strip(),
            "new_actual_total": float(corr_operation.group(2)),
        })
    corr_trip = _re.search(
        r"\b(?:correct|update|adjust)\s+(?:a\s+)?(?:finished\s+shopping|shopping\s+trip|grocery\s+trip)\b[^\n]*?\btrip[_\-\w]{4,}\b[^\n]*?\b(?:to|actual(?:\s+to)?|amount(?:\s+to)?)\s+\$?(\d+(?:\.\d{1,2})?)\b",
        raw,
        _re.IGNORECASE,
    )
    if corr_trip:
        token_match = _re.search(r"\b(trip[_\-\w]{4,})\b", raw, _re.IGNORECASE)
        payload["shopping_corrections"].append({
            "trip_token": token_match.group(1) if token_match else None,
            "new_actual_total": float(corr_trip.group(1)),
        })
    corr_latest = _re.search(
        r"\b(?:correct|update|adjust)\s+(?:my\s+)?(?:last|latest)\s+(?:finished\s+shopping|shopping\s+trip|grocery\s+trip)\b[^\n]*?\$?(\d+(?:\.\d{1,2})?)\b",
        raw,
        _re.IGNORECASE,
    )
    if corr_latest:
        payload["shopping_corrections"].append({
            "selector": "latest",
            "new_actual_total": float(corr_latest.group(1)),
        })

    def _extract_due_day_hint(segment: str) -> Optional[int]:
        m = _re.search(
            r"\bdue\s+(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b",
            segment,
            _re.IGNORECASE,
        )
        if not m:
            return None
        try:
            day = int(m.group(1))
        except (TypeError, ValueError):
            return None
        return day if 1 <= day <= 31 else None

    # Explicit unsupported actions should fail safely instead of guessing.
    if _looks_like_unsupported_grocery_remove(lower):
        payload["clarification_question"] = (
            "I can stage grocery additions, but removing existing grocery rows needs manual review in the Grocery tab right now."
        )
        return payload
    if _looks_like_mark_bill_paid(lower):
        payload["clarification_question"] = (
            "I can stage bill add/update/remove actions, but marking a bill paid is not yet a staged Copilot action."
        )
        return payload

    # Ambiguous historical amount request: only stage if history exists.
    if _re.search(r"\bnormally\s+spend\s+on\s+gas\b", lower):
        if _has_category_history("gas"):
            payload["discretionary_events"].append({"description": "gas", "amount": None})
        else:
            payload["clarification_question"] = (
                "I do not have enough gas-spend history to infer an amount. Please provide the amount to log."
            )

    # Short fuel phrases are spending intents, not grocery intents.
    if _re.match(r"^(?:add|log|record)\s+(?:gas|fuel)\b", lower):
        payload["discretionary_events"].append({"description": "gas", "amount": None})
    elif _re.match(r"^put\s+fuel\s+down\b", lower):
        payload["discretionary_events"].append({"description": "gas", "amount": None})
    elif _re.search(r"\bfill\s+up\s+(?:the\s+)?tank\b", lower):
        payload["discretionary_events"].append({"description": "gas", "amount": None})

    shopping_chunk = _shopping_list_chunk(raw)
    if shopping_chunk and not payload["discretionary_events"]:
        for raw_item in _re.split(r"\s*(?:,\s*|\s+and\s+)\s*", _re.sub(r",\s+(?=and\b)", " ", shopping_chunk, flags=_re.IGNORECASE)):
            requirement = _parse_shopping_requirement(raw_item)
            if requirement["item_name"]:
                payload["grocery_additions"].append(requirement["item_name"].lower())
                payload["shopping_requirements"].append(requirement)

    # Bills with amount-after-name: "add my electric bill for $150"
    for m in _re.finditer(
        r"\badd\s+(?:my\s+)?([A-Za-z][A-Za-z0-9 '&\-]+?)\s+bill(?:\s+for)?\s+\$?(\d+(?:\.\d{1,2})?)\b",
        raw,
        _re.IGNORECASE,
    ):
        due_day = _extract_due_day_hint(raw[m.end():])
        row = {
            "name": m.group(1).strip().lower(),
            "amount": float(m.group(2)),
            "action": "add",
        }
        if due_day is not None:
            row["due_day"] = due_day
        payload["bill_updates"].append(row)

    # Bills with amount-before-name: "add my $120 internet bill"
    for m in _re.finditer(
        r"\badd\s+(?:my\s+)?\$?(\d+(?:\.\d{1,2})?)\s+([A-Za-z][A-Za-z0-9 '&\-]+?)\s+bill\b",
        raw,
        _re.IGNORECASE,
    ):
        due_day = _extract_due_day_hint(raw[m.end():])
        row = {
            "name": m.group(2).strip().lower(),
            "amount": float(m.group(1)),
            "action": "add",
        }
        if due_day is not None:
            row["due_day"] = due_day
        payload["bill_updates"].append(row)

    # Explicit monthly shorthand still maps to bill add.
    for m in _re.finditer(
        r"\badd\s+([A-Za-z][A-Za-z0-9 '&\-]+?)\s+\$\s*(\d+(?:\.\d{1,2})?)\s*(?:/mo(?:nth)?|per\s+month|monthly)\b",
        raw,
        _re.IGNORECASE,
    ):
        due_day = _extract_due_day_hint(raw[m.end():])
        row = {
            "name": m.group(1).strip().lower(),
            "amount": float(m.group(2)),
            "action": "add",
        }
        if due_day is not None:
            row["due_day"] = due_day
        payload["bill_updates"].append(row)

    # Rent shorthand commonly omits the word "bill".
    for m in _re.finditer(
        r"\badd\s+(?:my\s+)?rent\s+(?:for\s+)?\$?(\d+(?:\.\d{1,2})?)\b",
        raw,
        _re.IGNORECASE,
    ):
        due_day = _extract_due_day_hint(raw[m.end():])
        row = {
            "name": "rent",
            "amount": float(m.group(1)),
            "action": "add",
        }
        if due_day is not None:
            row["due_day"] = due_day
        payload["bill_updates"].append(row)

    # Expense patterns.
    for m in _re.finditer(
        r"\badd\s+(?:a\s+)?\$?(\d+(?:\.\d{1,2})?)\s+([A-Za-z][A-Za-z0-9 ]+?)\s+expense\b",
        raw,
        _re.IGNORECASE,
    ):
        payload["discretionary_events"].append({
            "description": m.group(2).strip().lower(),
            "amount": float(m.group(1)),
        })
    for m in _re.finditer(
        r"\b(?:add|log)\s+(?:the\s+)?\$?(\d+(?:\.\d{1,2})?)\s+(?:i\s+spent\s+on\s+)?([A-Za-z][A-Za-z0-9 ]+?)\s+(?:expense\b|today\b|this\s+week\b|$)",
        raw,
        _re.IGNORECASE,
    ):
        payload["discretionary_events"].append({
            "description": m.group(2).strip().lower(),
            "amount": float(m.group(1)),
        })
    for m in _re.finditer(r"\bspent\s+\$?(\d+(?:\.\d{1,2})?)\s+on\s+([A-Za-z][A-Za-z0-9 ]+)\b", raw, _re.IGNORECASE):
        payload["discretionary_events"].append({
            "description": m.group(2).strip().lower(),
            "amount": float(m.group(1)),
        })
    for m in _re.finditer(
        r"\bspent\s+\$?(\d+(?:\.\d{1,2})?)\s+(?:at|on)\s+([A-Za-z][A-Za-z0-9 '&\-]+)\b",
        raw,
        _re.IGNORECASE,
    ):
        payload["spending_events"].append({
            "merchant": m.group(2).strip(),
            "description": m.group(2).strip(),
            "category": "discretionary",
            "amount": float(m.group(1)),
            "date": None,
        })
    for m in _re.finditer(
        r"\b(?:record|log|add)\s+(?:spending|expense)\s+(?:of\s+)?\$?(\d+(?:\.\d{1,2})?)\s+(?:at|on|for)\s+([A-Za-z][A-Za-z0-9 '&\-]+)\b",
        raw,
        _re.IGNORECASE,
    ):
        payload["spending_events"].append({
            "merchant": m.group(2).strip(),
            "description": m.group(2).strip(),
            "category": "discretionary",
            "amount": float(m.group(1)),
            "date": None,
        })

    # Recipe selection patterns.
    for m in _re.finditer(
        r"\badd\s+([A-Za-z][A-Za-z0-9 '&\-]+?)\s+to\s+(?:my\s+)?(?:meal\s+plan|meals?)\b",
        raw,
        _re.IGNORECASE,
    ):
        payload["selected_recipes"].append({"title": m.group(1).strip(), "action": "add"})
    for m in _re.finditer(r"\badd\s+([A-Za-z][A-Za-z0-9 '&\-]+?)\s+for\s+dinner\b", raw, _re.IGNORECASE):
        payload["selected_recipes"].append({"title": m.group(1).strip(), "action": "add"})

    # Meal target requests.
    m_target = _re.search(r"\b(?:plan|prep|make)\s+(\d+)\s+(?:dinners?|meals?|recipes?)\b", raw, _re.IGNORECASE)
    if m_target:
        payload["target_meals"] = int(m_target.group(1))

    normalized = _normalize_deterministic_result(payload)
    has_actions = bool(
        normalized["selected_recipes"]
        or normalized["grocery_additions"]
        or normalized["discretionary_events"]
        or normalized["spending_events"]
        or normalized["income_events"]
        or normalized["balance_reconciliation"]
        or normalized["shopping_corrections"]
        or normalized["bill_updates"]
        or normalized["target_meals"] is not None
    )
    if has_actions or normalized.get("clarification_question"):
        return normalized
    return None


def _extract_json_object(content: str) -> str:
    text = _strip_json_fences(content)
    if not text:
        return ""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _context_recipe_titles(limit: int = 30) -> List[str]:
    if not has_app_context():
        return []
    try:
        from services.household_context import household_id
        from services.recipe_access import visible_recipe_query

        rows = [
            str(row.title or "").strip()
            for row in visible_recipe_query(household_id()).order_by(Recipe.id.desc()).limit(max(1, int(limit or 30))).all()
        ]
        return [r for r in rows if r]
    except Exception:
        return []


def _context_bill_names(limit: int = 20) -> List[str]:
    if not has_app_context():
        return []
    try:
        from models import Bill

        rows = [
            str(row.name or "").strip()
            for row in Bill.query.order_by(Bill.id.desc()).limit(max(1, int(limit or 20))).all()
        ]
        return [r for r in rows if r]
    except Exception:
        return []


def _intent_context_suffix(user_text: str) -> str:
    """Attach only minimal context relevant to the current prompt."""
    text = str(user_text or "").lower()
    lines: List[str] = []

    if any(tok in text for tok in ["meal", "recipe", "cook", "dinner", "lunch", "breakfast"]):
        recipes = _context_recipe_titles(limit=25)
        if recipes:
            lines.append("Known recipe titles (match against these when possible):")
            lines.append("- " + " | ".join(recipes))

    if "bill" in text or "subscription" in text or "rent" in text:
        bills = _context_bill_names(limit=15)
        if bills:
            lines.append("Known recurring bill names:")
            lines.append("- " + " | ".join(bills))

    if not lines:
        return ""
    return "\n\nContext for this request only:\n" + "\n".join(lines)


def _has_category_history(category: str) -> bool:
    if not has_app_context():
        return False
    cat = str(category or "").strip().lower()
    if not cat:
        return False
    try:
        from models import ExpenseTransaction

        count = (
            ExpenseTransaction.query
            .filter(
                db.func.lower(ExpenseTransaction.category).like(f"%{cat}%")
                | db.func.lower(ExpenseTransaction.description).like(f"%{cat}%")
            )
            .count()
        )
        return bool(int(count or 0) > 0)
    except Exception:
        return False


class SemanticRecipeSelection(BaseModel):
    title: str
    action: str = "add"

    @field_validator("title", mode="before")
    def _title(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("action", mode="before")
    def _action(cls, v: Any) -> str:
        value = str(v or "add").strip().lower()
        return value if value in {"add", "remove"} else "add"


class SemanticGroceryItem(BaseModel):
    item_name: str
    category: str = "General"

    @field_validator("item_name", mode="before")
    def _item_name(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("category", mode="before")
    def _category(cls, v: Any) -> str:
        return str(v or "General").strip() or "General"


class SemanticExpenseEvent(BaseModel):
    description: str
    amount: Optional[float] = None

    @field_validator("description", mode="before")
    def _description(cls, v: Any) -> str:
        return str(v or "").strip() or "discretionary"


class SemanticBillUpdate(BaseModel):
    name: str
    action: str = "set"
    amount: Optional[float] = None
    due_day: Optional[int] = None

    @field_validator("name", mode="before")
    def _name(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("action", mode="before")
    def _bill_action(cls, v: Any) -> str:
        value = str(v or "set").strip().lower()
        if value in {"increase", "raise", "up", "higher"}:
            return "increase"
        if value in {"decrease", "reduce", "down", "lower"}:
            return "decrease"
        if value in {"remove", "delete", "cancel"}:
            return "remove"
        if value in {"add", "new", "create"}:
            return "add"
        return "set"

    @field_validator("due_day", mode="before")
    def _due_day(cls, v: Any) -> Optional[int]:
        if v in (None, ""):
            return None
        try:
            day = int(str(v).strip())
        except (TypeError, ValueError):
            return None
        if 1 <= day <= 31:
            return day
        return None


class SemanticSpendingEvent(BaseModel):
    description: str
    amount: Optional[float] = None
    merchant: Optional[str] = None
    category: Optional[str] = "discretionary"
    date: Optional[str] = None

    @field_validator("description", mode="before")
    def _spending_description(cls, v: Any) -> str:
        return str(v or "").strip() or "expense"


class SemanticIncomeEvent(BaseModel):
    source: str = "income"
    amount: Optional[float] = None
    date: Optional[str] = None
    note: Optional[str] = None

    @field_validator("source", mode="before")
    def _income_source(cls, v: Any) -> str:
        return str(v or "").strip() or "income"


class SemanticBalanceReconciliation(BaseModel):
    target_balance: float
    reason: Optional[str] = None


class SemanticShoppingCorrection(BaseModel):
    operation_id: Optional[str] = None
    trip_token: Optional[str] = None
    selector: Optional[str] = None
    new_actual_total: float


class SemanticIntentResult(BaseModel):
    selected_recipes: List[SemanticRecipeSelection] = Field(default_factory=list)
    grocery_additions: List[SemanticGroceryItem] = Field(default_factory=list)
    discretionary_events: List[SemanticExpenseEvent] = Field(default_factory=list)
    spending_events: List[SemanticSpendingEvent] = Field(default_factory=list)
    income_events: List[SemanticIncomeEvent] = Field(default_factory=list)
    balance_reconciliation: Optional[SemanticBalanceReconciliation] = None
    shopping_corrections: List[SemanticShoppingCorrection] = Field(default_factory=list)
    bill_updates: List[SemanticBillUpdate] = Field(default_factory=list)
    target_meals: Optional[int] = None
    meal_servings: Optional[int] = None
    clarification_question: Optional[str] = None

# Lazy-loaded Groq client class. Tests patch ``copilot_service._Groq``
# to inject fakes; production imports the SDK on first use via
# ``_get_groq_class``.
_Groq = None

# ============================================================================
# Active Groq Client — reads the key directly from SQLite
# ============================================================================


def get_active_groq_client() -> Any:
    """Instantiate a ``Groq`` client with server-side configuration.

    Uses the ``GROQ_API_KEY`` environment variable only.

    Returns
    -------
    Groq
        An authenticated Groq client instance.

    Raises
    ------
    RuntimeError
        If no Groq API key is configured server-side.
    """
    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("No server-side Groq API key configured.")

    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError(
            "The 'groq' Python SDK is not installed. "
            "Run: pip install groq"
        )

    return Groq(api_key=key)


# ============================================================================
# System prompt — tells the LLM what tools are available
# ============================================================================

_COPILOT_SYSTEM_PROMPT = """You are Rung's financial assistant. The user will describe what they want to do with their money and meal planning this pay period.

You have access to the following tools. Use them to directly execute the user's requests, interpreting plain-language phrasing the way a helpful human planner would.

1. **add_recurring_bill(name, amount, frequency, due_date)** — Add a new recurring bill or subscription. Call this when the user says things like "add Netflix $22.99/mo", "subscribe to HBO Max $14.99", "set up my phone bill", or "I need to pay my utilities".

2. **add_grocery_item(item_name, category)** — Add a non-recipe household item to the grocery list. Call this when the user says "I need dish soap", "add paper towels", "get laundry detergent", or "please add toilet paper".

3. **select_active_recipe(recipe_id_or_title, action)** — Add or remove a recipe from the active pay-period meal plan. Call this when the user says "cook chicken rice bowl", "meal prep fajitas", "remove chicken rice bowl", "let's make tacos", or "I'm cooking salmon tonight". Use the exact title the user provides when a recipe name is present.

4. **log_discretionary_expense(item_name, amount)** — Log a one-time discretionary expense like dining out or entertainment. Call this when the user says "dinner at Olive Garden $45", "buy concert tickets $80", "spent $15 on coffee", or "I went out for lunch".

5. **set_target_meals(count)** — Set a target number of meals for the pay period. Call this when the user says "plan dinners", "I want meals this week", "meal prep 10", or "I need 5 recipes". If the user asks for recipes or meals but does not name specific dishes, set the target and let the backend auto-fill with the most likely recipes based on their saved kitchen data and preferences.

Rules:
- Interpret plain language and paraphrases as intent, just like a human assistant would.
- Call the appropriate tool(s) for each action the user describes.
- You can call multiple tools in a single response if the user wants multiple things.
- If a recipe title is ambiguous, use the user's exact wording.
- If the user asks for a number of recipes or meals without naming them, use set_target_meals(count) so the server can choose likely recipes for them.
- If the user doesn't mention something, don't invent it.
- Return ONLY valid tool calls. Do not add commentary."""


_STRUCTURED_INTENT_PROMPT = """You are an intent parser for Rung, a finance + meal-planning assistant.

Parse the user's natural language into a strict JSON object with this shape:
{
    "selected_recipes": [{"title": string, "action": "add"|"remove"}],
    "grocery_additions": [{"item_name": string, "category": string}],
    "discretionary_events": [{"description": string, "amount": number|null}],
    "spending_events": [{"description": string, "merchant": string|null, "category": string|null, "amount": number|null, "date": string|null}],
    "income_events": [{"source": string, "amount": number|null, "date": string|null, "note": string|null}],
    "balance_reconciliation": {"target_balance": number, "reason": string|null}|null,
    "shopping_corrections": [{"operation_id": string|null, "trip_token": string|null, "selector": string|null, "new_actual_total": number}],
    "bill_updates": [{"name": string, "action": "add"|"set"|"increase"|"decrease"|"remove", "amount": number|null, "due_day": number|null}],
    "target_meals": number|null,
    "meal_servings": number|null,
    "clarification_question": string|null
}

Parsing rules:
- Use semantic understanding of conversational language. Map paraphrases, slang, and indirect phrasing to the right domain actions.
- Food/meal intent should map to recipes + meal targets even when recipes are not named (e.g., dinners, dishes, something to eat).
- Bill changes should map to bill_updates, including increase/decrease deltas.
- If the user specifies a monthly due day (for example "due on the 15th"), set bill_updates[].due_day to that day (1-31).
- If due-day wording is missing or ambiguous, keep due_day as null.
- One-time spend intent (like gas/fuel/fill-up) should map to discretionary_events.
- Spending entries with merchant/date context should map to spending_events.
- Income/paycheck/deposit logging should map to income_events.
- "Set/reconcile balance" requests should map to balance_reconciliation.target_balance.
- Finished-shopping correction requests should map to shopping_corrections.
- If amount is unknown but can be inferred from history by backend, set amount to null instead of asking.
- Only ask for clarification when truly required and unguessable. If needed, return exactly one short, precise clarification_question.
- Never include keys not in the schema.
- Output JSON only."""


# ============================================================================
# Model configuration — Groq retires models over time, so we keep a small
# fallback chain per path and allow env overrides.
# ============================================================================

# Models tried (in order) for native tool calling.
# Override with GROQ_TOOL_MODEL (comma-separated list supported).
DEFAULT_TOOL_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "llama-3.3-70b-versatile",  # deprecated (retires 2026-08-16) — last resort
]

# Models tried (in order) for the plain-text JSON path.
DEFAULT_JSON_MODELS = [
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",  # deprecated (retires 2026-08-16) — last resort
]


def _model_chain(env_key: str, defaults: List[str]) -> List[str]:
    """Return the env-overridable model list for a provider path."""
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return list(defaults)


def _tool_models() -> List[str]:
    return _model_chain("GROQ_TOOL_MODEL", DEFAULT_TOOL_MODELS)


def _json_models() -> List[str]:
    return _model_chain("GROQ_JSON_MODEL", DEFAULT_JSON_MODELS)


def _extract_groq_error_message(body: Any) -> Optional[str]:
    """Extract a helpful message from a Groq API error body."""
    if not body:
        return None
    if isinstance(body, dict):
        error = body.get("error") or body.get("errors")
        if isinstance(error, dict):
            return error.get("message") or error.get("code")
        if isinstance(error, list) and error:
            first = error[0]
            if isinstance(first, dict):
                return first.get("message") or first.get("code")
    return None


def _strip_json_fences(content: str) -> str:
    """Remove optional markdown code fences from model JSON output."""
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _semantic_result_to_legacy(result: SemanticIntentResult) -> Dict[str, Any]:
    """Normalize structured intent output to the API's legacy payload shape."""
    return {
        "tool_results": [],
        "selected_recipes": [
            {"title": item.title, "action": item.action}
            for item in result.selected_recipes
            if item.title
        ],
        "grocery_additions": [
            item.item_name
            for item in result.grocery_additions
            if item.item_name
        ],
        "discretionary_events": [
            {"description": ev.description, "amount": ev.amount}
            for ev in result.discretionary_events
            if ev.description
        ],
        "spending_events": [
            {
                "description": ev.description,
                "merchant": ev.merchant,
                "category": ev.category,
                "amount": ev.amount,
                "date": ev.date,
            }
            for ev in result.spending_events
            if ev.description
        ],
        "income_events": [
            {
                "source": ev.source,
                "amount": ev.amount,
                "date": ev.date,
                "note": ev.note,
            }
            for ev in result.income_events
            if ev.source
        ],
        "balance_reconciliation": (
            {
                "target_balance": result.balance_reconciliation.target_balance,
                "reason": result.balance_reconciliation.reason,
            }
            if result.balance_reconciliation is not None
            else None
        ),
        "shopping_corrections": [
            {
                "operation_id": row.operation_id,
                "trip_token": row.trip_token,
                "selector": row.selector,
                "new_actual_total": row.new_actual_total,
            }
            for row in result.shopping_corrections
        ],
        "bill_updates": [
            {"name": b.name, "amount": b.amount, "action": b.action, "due_day": b.due_day}
            for b in result.bill_updates
            if b.name
        ],
        "target_meals": result.target_meals,
        "meal_servings": result.meal_servings,
        "clarification_question": (result.clarification_question or "").strip() or None,
        "_fallback": False,
    }


def _parse_semantic_json(content: str) -> Optional[Dict[str, Any]]:
    """Parse and validate structured JSON output from an LLM."""
    body = _extract_json_object(content)
    if not body:
        return None
    try:
        raw = _json.loads(body)
        validated = SemanticIntentResult.model_validate(raw)
        return _semantic_result_to_legacy(validated)
    except (_json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return None


def _friendly_status_msg(
    status: Optional[int], model: str = "", body: Any = None
) -> str:
    """Human-readable message for a Groq HTTP status (best effort)."""
    if status == 401:
        return (
            "Groq rejected your API key (HTTP 401). "
            "Check console.groq.com/keys and save a valid key in Settings."
        )
    if status == 429:
        return "Groq rate-limited the request (HTTP 429) — wait a moment and try again."
    if status == 404:
        return f"Groq model '{model}' is unavailable (HTTP 404)."
    if status == 400:
        detail = _extract_groq_error_message(body)
        if detail:
            return f"Groq returned HTTP 400: {detail}"
        return "Groq returned HTTP 400."
    if status:
        return f"Groq API returned HTTP {status}."
    return "Could not reach Groq."


def _is_structured_output_validation_error(status: Optional[int], body: Any = None) -> bool:
    """Detect provider-side JSON-structure validation failures.

    These usually show up as HTTP 400 with messages like
    ``Failed to validate JSON`` or ``failed_generation``.
    """
    if status != 400:
        return False
    detail = (_extract_groq_error_message(body) or "").lower()
    blob = ""
    if isinstance(body, (dict, list)):
        try:
            blob = _json.dumps(body).lower()
        except Exception:
            blob = str(body).lower()
    else:
        blob = str(body or "").lower()
    text = f"{detail} {blob}"
    return any(
        needle in text
        for needle in (
            "failed to validate json",
            "failed_generation",
            "json",
            "schema",
            "structured",
            "tool_use_failed",
        )
    )


def _get_groq_class():
    """Lazily import and cache the Groq client class.

    Returns the ``Groq`` class, or ``None`` if the SDK isn't installed.
    Tests can patch the module-level ``copilot_service._Groq`` to inject
    a fake client class (returned as-is).
    """
    global _Groq
    if _Groq is None:
        try:
            from groq import Groq as _Groq
        except ImportError:
            _Groq = False
    return _Groq or None


# ============================================================================
# Provider: Groq (native tool calling with the groq SDK)
# ============================================================================


def _call_groq_tools(prompt: str, api_key: str = "") -> Dict[str, Any]:
    """Send *prompt* to the Groq API with native tool calling.

    Tries the ``_tool_models()`` chain (env-overridable, default
    ``openai/gpt-oss-120b``) with the ``APP_TOOLS`` schemas, executing
    tool calls locally and returning the structured result.

    Falls back to the old prompt-based JSON path if the model returns
    a plain-text response instead of tool calls.

    Returns
    -------
    dict
        Normal result dict (``_fallback: False``), or ``__no_key__``
        sentinel (with optional ``__error__``) when the call failed.
    """
    if not api_key:
        api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        return _no_key_result()

    from services.copilot_tools import APP_TOOLS, execute_app_function

    groq_class = _get_groq_class()
    if groq_class is None:
        return _error_result(
            "The 'groq' Python SDK is not installed. "
            "Run: pip install -r requirements.txt (or pip install groq)"
        )

    system_msg = {"role": "system", "content": _COPILOT_SYSTEM_PROMPT}
    user_msg = {"role": "user", "content": prompt}

    client = groq_class(api_key=api_key)
    last_err = None
    for model in _tool_models():
        try:
            # --- First call: get the tool calls ---
            response = client.chat.completions.create(
                model=model,
                messages=[system_msg, user_msg],
                tools=APP_TOOLS,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=2048,
            )

            choice = response.choices[0]
            msg = choice.message

            # If the model returns a plain-text response (no tool calls),
            # try to parse it as JSON (old fallback path).
            if not msg.tool_calls and msg.content:
                return _parse_plain_text_response(msg.content)

            # If no tool calls and no content, return empty result.
            if not msg.tool_calls:
                return _empty_result()

            # --- Execute each tool call ---
            tool_results = []
            for tc in msg.tool_calls:
                function_name = tc.function.name
                try:
                    function_args = _json.loads(tc.function.arguments)
                except (_json.JSONDecodeError, ValueError):
                    function_args = {}

                result = execute_app_function(function_name, function_args)
                tool_results.append({
                    "tool": function_name,
                    "arguments": function_args,
                    "status": result.get("status", "error"),
                    "data": result.get("data"),
                    "message": result.get("message"),
                })

            usage_meta = _extract_usage_from_sdk_response(response)

            return {
                "tool_results": tool_results,
                "selected_recipes": [],
                "grocery_additions": [],
                "discretionary_events": [],
                "spending_events": [],
                "income_events": [],
                "balance_reconciliation": None,
                "shopping_corrections": [],
                "bill_updates": [],
                "target_meals": _get_target_from_results(tool_results),
                "_fallback": False,
                "_llm_usage": {
                    "provider": "groq",
                    "model": model,
                    "llm_calls": 1,
                    **usage_meta,
                },
            }

        except Exception as exc:
            status = getattr(exc, "status_code", None)
            body = getattr(exc, "body", None)
            if body is None and hasattr(exc, "response"):
                body = getattr(exc, "response", None)
            LOGGER.warning("Groq tool-calling failed on %s: %s", model, exc)
            if status == 404:
                # Model retired/unavailable — try the next one in the chain.
                last_err = _friendly_status_msg(404, model)
                continue
            if status is None:
                # Network/timeout — keep the actionable detail.
                return _error_result(f"Could not reach Groq ({type(exc).__name__}: {exc})")
            return _error_result(_friendly_status_msg(status, model, body))

    return _error_result(last_err or "All configured Groq models failed.")


def _get_target_from_results(tool_results):
    """Extract the target_meals count from tool_results, if set."""
    for tr in tool_results:
        if tr.get("tool") == "set_target_meals" and tr.get("status") == "ok":
            data = tr.get("data") or {}
            return data.get("target_meals")
    return None


def _parse_plain_text_response(content: str) -> Dict[str, Any]:
    """Try to parse a plain-text LLM response as JSON (old prompt-based path)."""
    if not content:
        return _empty_result()
    parsed = _parse_semantic_json(content)
    if parsed is not None:
        return parsed
    return _empty_result()


def _no_key_result() -> Dict[str, Any]:
    """Return the 'no key' result — triggers the next provider in the chain."""
    return {"__no_key__": True}


def _error_result(message: str, kind: str = "provider_error") -> Dict[str, Any]:
    """Return a 'key configured but call failed' result with a real message.

    Carries ``__no_key__: True`` so the provider chain still falls
    through to semantic degraded mode, plus ``__error__``
    so ``parse_copilot_prompt`` can attach the honest reason for the
    frontend instead of the misleading "no LLM configured" banner.
    """
    return {"__no_key__": True, "__error__": message, "__error_kind__": kind}


def _empty_result() -> Dict[str, Any]:
    """Return an empty result with the standard shape."""
    return {
        "tool_results": [],
        "selected_recipes": [],
        "grocery_additions": [],
        "discretionary_events": [],
        "spending_events": [],
        "income_events": [],
        "balance_reconciliation": None,
        "shopping_corrections": [],
        "bill_updates": [],
        "target_meals": None,
        "meal_servings": None,
        "clarification_question": None,
        "_fallback": False,
    }


# ============================================================================
# Provider: Groq (old prompt-based JSON path — fallback)
# ============================================================================


def _call_groq_json(prompt: str, api_key: str = "") -> Optional[Dict[str, Any]]:
    """Parse user intent through a structured JSON completion on Groq.

    This path is used when native tool-calling is unavailable or returns no
    tool calls. It still relies on LLM semantic reasoning, then validates the
    output with Pydantic before dispatching.
    """
    if not api_key:
        api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        return None

    try:
        import requests
    except ImportError:
        return _error_result("The 'requests' Python SDK is not installed.")

    def _request_json(model: str, user_prompt: str, system_prompt: str, strict_json: bool = True):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }
        if strict_json:
            # Prefer strict JSON payloads and enforce schema in deterministic code.
            payload["response_format"] = {"type": "json_object"}
        return requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )

    last_err = None
    context_suffix = _intent_context_suffix(prompt)
    system_prompt = _STRUCTURED_INTENT_PROMPT + context_suffix
    for model in _json_models():
        try:
            resp = _request_json(model, prompt, system_prompt)
            if resp.status_code == 404:
                # Model retired/unavailable — try the next one in the chain.
                last_err = _friendly_status_msg(404, model)
                continue
            if resp.status_code != 200:
                body = None
                try:
                    body = resp.json()
                except Exception:
                    pass
                if _is_structured_output_validation_error(resp.status_code, body):
                    LOGGER.warning(
                        "Groq strict JSON response failed validation on %s; retrying without strict response_format.",
                        model,
                    )
                    repair_system = (
                        _STRUCTURED_INTENT_PROMPT
                        + context_suffix
                        + "\n\nReturn ONLY one valid JSON object with the required schema."
                    )
                    relaxed = _request_json(model, prompt, repair_system, strict_json=False)
                    if relaxed.status_code == 200:
                        relaxed_body = relaxed.json()
                        usage_relaxed = _extract_usage_from_json_body(relaxed_body)
                        relaxed_content = (
                            relaxed_body.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                            .strip()
                        )
                        repaired = _parse_semantic_json(relaxed_content)
                        if repaired is not None:
                            repaired.setdefault("_parse_meta", {})
                            repaired["_parse_meta"].update({
                                "llm_calls": 2,
                                "repair_attempted": True,
                                "validation": "repaired",
                                "provider": "groq",
                                "model": model,
                            })
                            repaired["_llm_usage"] = {
                                "provider": "groq",
                                "model": model,
                                "llm_calls": 2,
                                **usage_relaxed,
                            }
                            return repaired
                    return {
                        "tool_results": [],
                        "selected_recipes": [],
                        "grocery_additions": [],
                        "discretionary_events": [],
                        "spending_events": [],
                        "income_events": [],
                        "balance_reconciliation": None,
                        "shopping_corrections": [],
                        "bill_updates": [],
                        "target_meals": None,
                        "meal_servings": None,
                        "clarification_question": "I couldn't parse that safely. Please rephrase with the exact action and amount.",
                        "_fallback": False,
                        "_parse_error": "invalid_structured_output",
                        "_parse_meta": {
                            "llm_calls": 2,
                            "repair_attempted": True,
                            "validation": "invalid",
                            "provider": "groq",
                            "model": model,
                        },
                        "_llm_usage": {
                            "provider": "groq",
                            "model": model,
                            "llm_calls": 2,
                        },
                    }

                kind = "provider_error"
                if resp.status_code in {401, 403}:
                    kind = "provider_auth"
                return _error_result(_friendly_status_msg(resp.status_code, model, body), kind=kind)
            body = resp.json()
            usage_primary = _extract_usage_from_json_body(body)
            content = (
                body.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not content:
                return None
            parsed = _parse_semantic_json(content)
            if parsed is not None:
                parsed.setdefault("_parse_meta", {})
                parsed["_parse_meta"].update({
                    "llm_calls": 1,
                    "repair_attempted": False,
                    "validation": "valid",
                    "provider": "groq",
                    "model": model,
                })
                parsed["_llm_usage"] = {
                    "provider": "groq",
                    "model": model,
                    "llm_calls": 1,
                    **usage_primary,
                }
                return parsed

            # One safe repair attempt only.
            repair_system = (
                _STRUCTURED_INTENT_PROMPT
                + context_suffix
                + "\n\nYour previous output was malformed or schema-invalid. "
                  "Return ONLY one valid JSON object with the required schema."
            )
            repair = _request_json(model, prompt, repair_system)
            if repair.status_code == 200:
                repair_body = repair.json()
                usage_repair = _extract_usage_from_json_body(repair_body)
                repair_content = (
                    repair_body.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                repaired = _parse_semantic_json(repair_content)
                if repaired is not None:
                    total_input = (usage_primary.get("input_tokens") or 0) + (usage_repair.get("input_tokens") or 0)
                    total_output = (usage_primary.get("output_tokens") or 0) + (usage_repair.get("output_tokens") or 0)
                    repaired.setdefault("_parse_meta", {})
                    repaired["_parse_meta"].update({
                        "llm_calls": 2,
                        "repair_attempted": True,
                        "validation": "repaired",
                        "provider": "groq",
                        "model": model,
                    })
                    repaired["_llm_usage"] = {
                        "provider": "groq",
                        "model": model,
                        "llm_calls": 2,
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                    }
                    return repaired
            return {
                "tool_results": [],
                "selected_recipes": [],
                "grocery_additions": [],
                "discretionary_events": [],
                "spending_events": [],
                "income_events": [],
                "balance_reconciliation": None,
                "shopping_corrections": [],
                "bill_updates": [],
                "target_meals": None,
                "meal_servings": None,
                "clarification_question": "I couldn't parse that safely. Please rephrase with the exact action and amount.",
                "_fallback": False,
                "_parse_error": "invalid_structured_output",
                "_parse_meta": {
                    "llm_calls": 2,
                    "repair_attempted": True,
                    "validation": "invalid",
                    "provider": "groq",
                    "model": model,
                },
                "_llm_usage": {
                    "provider": "groq",
                    "model": model,
                    "llm_calls": 2,
                    **usage_primary,
                },
            }
        except (_json.JSONDecodeError, ValueError):
            return None
        except Exception as exc:
            LOGGER.warning("Groq JSON call failed on %s: %s", model, exc)
            return _error_result(
                f"Could not reach Groq ({type(exc).__name__}: {exc})",
                kind="provider_unreachable",
            )

    return _error_result(last_err or "All configured Groq JSON models failed.")


# ============================================================================
# Provider: Ollama (local LLM server)
# ============================================================================


def _call_ollama(prompt: str) -> Optional[Dict[str, Any]]:
    """Send *prompt* to a local Ollama instance and return parsed JSON."""
    base_url = (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
    model = (os.environ.get("OLLAMA_MODEL") or "llama3.1:8b").strip()

    try:
        import requests
    except ImportError:
        return None

    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "system": _STRUCTURED_INTENT_PROMPT,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 1024},
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        raw = (body.get("response") or "").strip()
        if not raw:
            return None
        return _parse_semantic_json(raw)
    except (_json.JSONDecodeError, Exception) as exc:
        LOGGER.warning("Ollama call failed: %s", exc)
        return None


# ============================================================================
# Public API
# ============================================================================

_BILL_RE = _re.compile(
    r'(?:(?:add|and)\s+)?([A-Za-z][A-Za-z0-9 _\-]*?)\s+\$\s*(\d+(?:\.\d{1,2})?)'
    r'\s*(?:/mo(?:nth)?|per\s+month|monthly)',
    _re.IGNORECASE,
)
# Matches "add ServiceName $X" without a /mo suffix (explicit one-time add-as-bill)
_EXPLICIT_ADD_BILL_RE = _re.compile(
    r'\badd\s+([A-Za-z][A-Za-z0-9 _\-]+?)\s+\$\s*(\d+(?:\.\d{1,2})?)\b'
    r'(?!\s*/mo|\s+per\s+month|\s+monthly)',
    _re.IGNORECASE,
)
_MEAL_RE = _re.compile(
    r'(?:plan\s+)?(\d+)\s+(?:dinners?|meals?|recipes?)',
    _re.IGNORECASE,
)
_RECIPE_RE = _re.compile(
    r'(?:cook|make|meal\s+prep|prepare)\s+([A-Za-z][A-Za-z0-9 ]+?)(?=\s+and\s+[A-Za-z]|\s*,|'
    r'\s+(?:this|for|this\s+week|tonight|today)|\s*$)',
    _re.IGNORECASE,
)
_GROCERY_RE = _re.compile(
    r'(?:i\s+need|need\s+some|get|buy|pick\s+up)\s+((?:[A-Za-z][A-Za-z0-9 ]*?)(?:\s+and\s+[A-Za-z][A-Za-z0-9 ]*?)*?)'
    r'(?=\s+for\s+the\s+|\s+from\s+|\s*\.\s*|\s*$)',
    _re.IGNORECASE,
)
_EXPENSE_RE = _re.compile(
    r'(?:dinner|lunch|breakfast|coffee|gas|bought|purchase|buy)\b'
    r'(?:\s+out)?(?:\s+at\s+[A-Za-z0-9 ]+?)?\s+\$\s*(\d+(?:\.\d{1,2})?)\b',
    _re.IGNORECASE,
)
_BUY_EXPENSE_RE = _re.compile(
    r'buy(?:\s+a)?\s+([A-Za-z][A-Za-z0-9 ]+?)\s+\$\s*(\d+(?:\.\d{1,2})?)\b',
    _re.IGNORECASE,
)


def _regex_parse_text(text: str) -> Dict[str, Any]:
    """Minimal keyword/regex parser used when no LLM provider is available."""
    bill_updates: List[Dict[str, Any]] = []
    grocery_additions: List[str] = []
    discretionary_events: List[Dict[str, Any]] = []
    selected_recipes: List[Dict[str, Any]] = []
    target_meals: Optional[int] = None

    bill_spans = []
    for m in _BILL_RE.finditer(text):
        name = m.group(1).strip().lower()
        if name and not _re.fullmatch(r'(?:add|the|a|an|my|your|our|and)', name, _re.IGNORECASE):
            bill_updates.append({"name": name, "amount": float(m.group(2)), "action": "add"})
            bill_spans.append((m.start(), m.end()))

    for m in _EXPLICIT_ADD_BILL_RE.finditer(text):
        if any(s <= m.start() < e for s, e in bill_spans):
            continue
        name = m.group(1).strip().lower()
        if name:
            bill_updates.append({"name": name, "amount": float(m.group(2)), "action": "add"})
            bill_spans.append((m.start(), m.end()))

    m_meals = _MEAL_RE.search(text)
    if m_meals:
        target_meals = int(m_meals.group(1))

    for m in _RECIPE_RE.finditer(text):
        title = m.group(1).strip()
        if title:
            selected_recipes.append({"title": title, "action": "add"})

    m_grocery = _GROCERY_RE.search(text)
    if m_grocery:
        for item in _split_grocery_item_list(m_grocery.group(1)):
            if item and not any(s in item for s in ["/mo", "per month"]):
                grocery_additions.append(item)

    for m in _EXPENSE_RE.finditer(text):
        # skip if this position overlaps with a bill match
        if any(s <= m.start() < e for s, e in bill_spans):
            continue
        description = m.group(0).split("$")[0].strip().lower()
        discretionary_events.append({"description": description, "amount": float(m.group(1))})

    for m in _BUY_EXPENSE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in bill_spans):
            continue
        # only treat as expense if no /mo suffix follows
        remaining = text[m.end():]
        if _re.match(r'\s*/mo', remaining, _re.IGNORECASE):
            continue
        description = "buy " + m.group(1).strip().lower()
        discretionary_events.append({"description": description, "amount": float(m.group(2))})

    return {
        "tool_results": [],
        "selected_recipes": selected_recipes,
        "grocery_additions": grocery_additions,
        "discretionary_events": discretionary_events,
        "spending_events": [],
        "income_events": [],
        "balance_reconciliation": None,
        "shopping_corrections": [],
        "bill_updates": bill_updates,
        "target_meals": target_meals,
    }


def parse_copilot_prompt(
    user_text: str,
    groq_api_key: str = "",
    staging_only: bool = False,
    allow_llm: bool = True,
) -> Dict[str, Any]:
    """Parse free-form user text into structured Rung actions.

    Provider priority:
      1. Groq (native tool calling with ``groq`` SDK)
      2. Groq (old prompt-based JSON)
      3. Ollama
            4. Empty semantic fallback (no provider available)

        The ``_fallback`` key is ``True`` only when no LLM provider is available.

    Parameters
    ----------
    user_text : str
        Natural-language description of what the user wants to do.
    groq_api_key : str, optional
        Groq API key from the BYOK settings. If empty, falls back to
        the ``GROQ_API_KEY`` environment variable.

    Returns
    -------
    dict
        Keys: ``tool_results``, ``selected_recipes``, ``grocery_additions``,
        ``discretionary_events``, ``bill_updates``, ``target_meals``,
        ``_fallback``.
    """
    started = time.perf_counter()
    if not user_text or not user_text.strip():
        meta = _finish_parser_meta(_new_parser_meta("empty"), started)
        out = _base_result()
        out["_parse_meta"] = meta
        return out

    prompt = user_text.strip()

    # 0. Deterministic high-confidence parser: no LLM call required.
    deterministic = _deterministic_fast_parse(prompt)
    if deterministic is not None:
        meta = _finish_parser_meta(_new_parser_meta("deterministic"), started)
        meta["validation"] = "valid"
        deterministic["_parse_meta"] = meta
        LOGGER.info(
            "copilot.parse path=%s llm_calls=%s repair=%s validation=%s latency_ms=%s",
            meta.get("path"),
            meta.get("llm_calls"),
            meta.get("repair_attempted"),
            meta.get("validation"),
            meta.get("latency_ms"),
        )
        return deterministic

    # Track the honest reason when a key IS configured but the LLM call
    # fails — so the UI can say "Groq rejected your key" instead of the
    # misleading "no LLM configured".
    llm_error = None
    llm_error_kind = None

    # 1. Try Groq native tool calling (with explicit key, falling back to env var)
    #    unless staging_only is requested. Staging mode must remain mutation-free.
    if not groq_api_key:
        groq_api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if allow_llm and groq_api_key and not staging_only:
        result = _call_groq_tools(prompt, api_key=groq_api_key)
        # Only return the result if the API call actually succeeded.
        # ``__no_key__`` signals a failure (no key, auth reject, network,
        # parse error) that should fall through to the next provider.
        if not result.get("__no_key__"):
            meta = _finish_parser_meta(_new_parser_meta("llm_tool_calling"), started)
            meta["llm_calls"] = 1
            meta["validation"] = "tool_call"
            result["_parse_meta"] = meta
            if not isinstance(result.get("_llm_usage"), dict):
                result["_llm_usage"] = {"provider": "groq", "llm_calls": 1}
            LOGGER.info(
                "copilot.parse path=%s llm_calls=%s repair=%s validation=%s latency_ms=%s",
                meta.get("path"),
                meta.get("llm_calls"),
                meta.get("repair_attempted"),
                meta.get("validation"),
                meta.get("latency_ms"),
            )
            return result
        llm_error = result.get("__error__")
        llm_error_kind = result.get("__error_kind__")

    # 2. Try Groq old prompt-based JSON
    result = _call_groq_json(prompt, api_key=groq_api_key) if allow_llm else None
    if result is not None:
        if result.get("__error__"):
            # Prefer the API's verdict (e.g. a 401) over an earlier
            # SDK-missing message — the requests-based JSON path can
            # succeed without the groq SDK, so its error is the real one.
            llm_error = result.get("__error__") or llm_error
            llm_error_kind = result.get("__error_kind__") or llm_error_kind
        else:
            result["_fallback"] = False
            meta = _new_parser_meta("llm_json")
            parse_meta = result.get("_parse_meta") if isinstance(result.get("_parse_meta"), dict) else {}
            meta["llm_calls"] = int(parse_meta.get("llm_calls") or 1)
            meta["repair_attempted"] = bool(parse_meta.get("repair_attempted", False))
            meta["validation"] = str(parse_meta.get("validation") or "valid")
            result["_parse_meta"] = _finish_parser_meta(meta, started)
            LOGGER.info(
                "copilot.parse path=%s llm_calls=%s repair=%s validation=%s latency_ms=%s",
                result["_parse_meta"].get("path"),
                result["_parse_meta"].get("llm_calls"),
                result["_parse_meta"].get("repair_attempted"),
                result["_parse_meta"].get("validation"),
                result["_parse_meta"].get("latency_ms"),
            )
            return result

    # 3. Try Ollama
    result = _call_ollama(prompt) if allow_llm else None
    if result is not None:
        result["_fallback"] = False
        meta = _finish_parser_meta(_new_parser_meta("llm_ollama"), started)
        meta["llm_calls"] = 1
        meta["validation"] = "valid"
        result["_parse_meta"] = meta
        if not isinstance(result.get("_llm_usage"), dict):
            result["_llm_usage"] = {
                "provider": "ollama",
                "model": str(os.environ.get("OLLAMA_MODEL") or "llama3.1:8b"),
                "llm_calls": 1,
            }
        LOGGER.info(
            "copilot.parse path=%s llm_calls=%s repair=%s validation=%s latency_ms=%s",
            meta.get("path"),
            meta.get("llm_calls"),
            meta.get("repair_attempted"),
            meta.get("validation"),
            meta.get("latency_ms"),
        )
        return result

    # 4. Regex degraded mode (no LLM provider available).
    LOGGER.info("No working LLM provider — returning regex fallback envelope")
    fallback = _regex_parse_text(prompt)
    fallback["meal_servings"] = None
    has_actions = bool(
        fallback.get("selected_recipes")
        or fallback.get("grocery_additions")
        or fallback.get("discretionary_events")
        or fallback.get("spending_events")
        or fallback.get("income_events")
        or fallback.get("balance_reconciliation")
        or fallback.get("shopping_corrections")
        or fallback.get("bill_updates")
        or fallback.get("target_meals") is not None
    )
    if not has_actions:
        fallback["clarification_question"] = _COPILOT_UNAVAILABLE_MESSAGE
    else:
        fallback["clarification_question"] = None
    fallback["_fallback"] = True
    if llm_error:
        fallback["_llm_error"] = llm_error
    if llm_error_kind:
        fallback["_llm_error_kind"] = llm_error_kind
    meta = _finish_parser_meta(_new_parser_meta("regex_fallback"), started)
    meta["validation"] = "degraded"
    fallback["_parse_meta"] = meta
    LOGGER.info(
        "copilot.parse path=%s llm_calls=%s repair=%s validation=%s latency_ms=%s",
        meta.get("path"),
        meta.get("llm_calls"),
        meta.get("repair_attempted"),
        meta.get("validation"),
        meta.get("latency_ms"),
    )
    return fallback


# ============================================================================
# Multi-turn chat (hybrid: conversational replies + action execution)
# ============================================================================

_CHAT_SYSTEM_PROMPT = """You are Rung's friendly personal finance assistant. The user is chatting with you about their budget, bills, groceries, and meal planning. You can BOTH have a natural conversation AND perform actions on their account.

You have access to these tools:

1. **get_financial_overview()** — call this BEFORE answering any question about the user's money, budget, bills, transactions, meal plan, or grocery cart. It returns their live financial snapshot (real numbers).

2. **add_recurring_bill(name, amount, frequency, due_date)** — add a recurring bill/subscription ("add Netflix $22.99/mo").

3. **add_grocery_item(item_name, category)** — add a household item to the grocery cart ("I need dish soap").

4. **select_active_recipe(recipe_id_or_title, action)** — add/remove a recipe from the active meal plan ("cook chicken rice bowl"). If the user says something like "let's make tacos", "I want to cook salmon", or "remove scrambled eggs", use this tool accordingly.

5. **log_discretionary_expense(item_name, amount)** — log a one-time expense ("dinner out $45").

6. **set_target_meals(count)** — set a meal target for the pay period ("plan 5 dinners"). If the user asks for a number of recipes or meals without giving titles, use this tool so the backend can auto-fill likely recipes from their saved library, pantry, and preferences.

Rules:
- Interpret plain language and paraphrases as intent, just like a human assistant would.
- If the user asks a QUESTION ("how much can I spend?", "what bills do I have?", "can I afford this?"), call get_financial_overview first, then answer conversationally using the real data.
- If the user wants you to DO something (add a bill, log an expense, add groceries, plan meals), call the matching tool.
- If the user is just chatting ("hello", "thanks", "what can you do?"), respond conversationally WITHOUT calling tools.
- You may call multiple tools in a single turn.
- After calling tools, always reply with a short, friendly summary of what you did.
- Keep replies concise and warm. Use simple markdown (**bold** for emphasis, short bullet lists when helpful).
- Never invent numbers — only state figures that came from get_financial_overview."""


def chat_copilot_prompt(messages: List[Dict[str, str]], groq_api_key: str = "") -> Dict[str, Any]:
    """Multi-turn hybrid chat: conversational replies AND action execution.

    Sends the full message history to Groq with ``tool_choice="auto"`` so
    the model can either reply conversationally (no tool calls) or perform
    actions via ``APP_TOOLS`` (executed locally, then a summary reply is
    generated from the tool results).

    Parameters
    ----------
    messages : list of {"role": "user"|"assistant", "content": str}
        The conversation so far (including the latest user message).
    groq_api_key : str, optional
        Groq API key from BYOK settings. Falls back to ``GROQ_API_KEY``.

    Returns
    -------
    dict
        Keys: ``reply``, ``tool_results``, plus the standard legacy lists
        (``selected_recipes``, ``grocery_additions``, ``discretionary_events``,
        ``bill_updates``, ``target_meals``) and ``_fallback`` / ``_llm_error``.
    """
    if not groq_api_key:
        groq_api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not groq_api_key:
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        regex = _regex_parse_text(last_user) if last_user else {}
        return {
            "reply": (
                _COPILOT_UNAVAILABLE_MESSAGE
            ),
            "tool_results": [],
            "selected_recipes": regex.get("selected_recipes", []),
            "grocery_additions": regex.get("grocery_additions", []),
            "discretionary_events": regex.get("discretionary_events", []),
            "spending_events": regex.get("spending_events", []),
            "income_events": regex.get("income_events", []),
            "balance_reconciliation": regex.get("balance_reconciliation"),
            "shopping_corrections": regex.get("shopping_corrections", []),
            "bill_updates": regex.get("bill_updates", []),
            "target_meals": regex.get("target_meals"),
            "meal_servings": None,
            "clarification_question": _COPILOT_UNAVAILABLE_MESSAGE,
            "_fallback": True,
        }

    from services.copilot_tools import APP_TOOLS, execute_app_function

    groq_class = _get_groq_class()
    if groq_class is None:
        return {
            "reply": "The 'groq' Python SDK is not installed. Run: pip install groq",
            "tool_results": [], "selected_recipes": [], "grocery_additions": [],
            "discretionary_events": [], "spending_events": [], "income_events": [],
            "balance_reconciliation": None, "shopping_corrections": [],
            "bill_updates": [], "target_meals": None,
            "_fallback": True,
            "_llm_error": "The 'groq' Python SDK is not installed.",
        }

    # Build the message list: system prompt + sanitized history (last 16).
    history = [
        {"role": m.get("role"), "content": str(m.get("content") or "")}
        for m in messages[-16:]
        if m.get("role") in ("user", "assistant")
    ]
    system_msg = {"role": "system", "content": _CHAT_SYSTEM_PROMPT}

    client = groq_class(api_key=groq_api_key)
    last_err = None
    tool_results = []
    total_input_tokens = 0
    total_output_tokens = 0
    llm_call_count = 0

    def _degraded(llm_error: str) -> Dict[str, Any]:
        """Degrade gracefully when model calls fail."""
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        regex = _regex_parse_text(last_user) if last_user else {}
        return {
            "reply": (
                "I couldn't reach the AI model to safely parse that request. "
                "Please try again in a moment."
            ),
            "tool_results": [],
            "selected_recipes": regex.get("selected_recipes", []),
            "grocery_additions": regex.get("grocery_additions", []),
            "discretionary_events": regex.get("discretionary_events", []),
            "spending_events": regex.get("spending_events", []),
            "income_events": regex.get("income_events", []),
            "balance_reconciliation": regex.get("balance_reconciliation"),
            "shopping_corrections": regex.get("shopping_corrections", []),
            "bill_updates": regex.get("bill_updates", []),
            "target_meals": regex.get("target_meals"),
            "meal_servings": None,
            "clarification_question": "Could you resend your request once the model connection is back?",
            "_fallback": True,
            "_llm_error": llm_error,
        }

    for model in _tool_models():
        try:
            # ---- First call: let the model chat and/or call tools ----
            response = client.chat.completions.create(
                model=model,
                messages=[system_msg] + history,
                tools=APP_TOOLS,
                tool_choice="auto",
                temperature=0.4,
                max_tokens=1024,
            )
            usage_first = _extract_usage_from_sdk_response(response)
            total_input_tokens += int(usage_first.get("input_tokens") or 0)
            total_output_tokens += int(usage_first.get("output_tokens") or 0)
            llm_call_count += 1
            msg = response.choices[0].message

            # Pure conversational reply (no tools) — run a dedicated structured
            # parse pass on the latest user message so actionable requests can
            # still execute even when the chat response is plain text.
            if not msg.tool_calls:
                last_user = next(
                    (m.get("content", "") for m in reversed(messages)
                     if m.get("role") == "user"),
                    "",
                )
                parsed = _call_groq_json(last_user, api_key=groq_api_key) or _empty_result()
                has_actions = bool(
                    parsed.get("selected_recipes")
                    or parsed.get("grocery_additions")
                    or parsed.get("discretionary_events")
                    or parsed.get("spending_events")
                    or parsed.get("income_events")
                    or parsed.get("balance_reconciliation")
                    or parsed.get("shopping_corrections")
                    or parsed.get("bill_updates")
                    or parsed.get("target_meals") is not None
                )
                if has_actions:
                    parsed["reply"] = msg.content or ""
                    parsed["_fallback"] = False
                    parsed_usage = parsed.get("_llm_usage") if isinstance(parsed.get("_llm_usage"), dict) else {}
                    parsed["_llm_usage"] = {
                        "provider": "groq",
                        "model": model,
                        "llm_calls": llm_call_count + int(parsed_usage.get("llm_calls") or 0),
                        "input_tokens": total_input_tokens + int(parsed_usage.get("input_tokens") or 0),
                        "output_tokens": total_output_tokens + int(parsed_usage.get("output_tokens") or 0),
                    }
                    return parsed

                # Last-resort: regex parse the user message so plain-text
                # replies (e.g. "Sure, I can plan 5 dinners") still execute.
                regex_parsed = _regex_parse_text(last_user)
                has_regex = bool(
                    regex_parsed.get("selected_recipes")
                    or regex_parsed.get("grocery_additions")
                    or regex_parsed.get("discretionary_events")
                    or regex_parsed.get("spending_events")
                    or regex_parsed.get("income_events")
                    or regex_parsed.get("balance_reconciliation")
                    or regex_parsed.get("shopping_corrections")
                    or regex_parsed.get("bill_updates")
                    or regex_parsed.get("target_meals") is not None
                )
                if has_regex:
                    regex_parsed["reply"] = msg.content or ""
                    regex_parsed["_fallback"] = False
                    regex_parsed["_llm_usage"] = {
                        "provider": "groq",
                        "model": model,
                        "llm_calls": llm_call_count,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    }
                    return regex_parsed

                return {
                    "reply": msg.content or "",
                    "tool_results": [], "selected_recipes": [],
                    "grocery_additions": [], "discretionary_events": [],
                    "spending_events": [], "income_events": [],
                    "balance_reconciliation": None, "shopping_corrections": [],
                    "bill_updates": [], "target_meals": None,
                    "_fallback": False,
                    "_llm_usage": {
                        "provider": "groq",
                        "model": model,
                        "llm_calls": llm_call_count,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                }

            # ---- Execute the requested tools ----
            tool_results = []
            assistant_tool_calls = []
            tool_messages = []
            for tc in msg.tool_calls:
                function_name = tc.function.name
                try:
                    function_args = _json.loads(tc.function.arguments)
                except (_json.JSONDecodeError, ValueError):
                    function_args = {}
                result = execute_app_function(function_name, function_args)
                tool_results.append({
                    "tool": function_name,
                    "arguments": function_args,
                    "status": result.get("status", "error"),
                    "data": result.get("data"),
                    "message": result.get("message"),
                })
                assistant_tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "arguments": tc.function.arguments,
                    },
                })
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _json.dumps(result),
                })

            # ---- Second call: generate a friendly summary reply ----
            # Tool protocol requires the assistant's tool_calls message to
            # immediately precede its tool results. Build a fresh list so
            # ordering is: [system, history..., assistant(tool_calls),
            # tool results...] — never interleave with the raw history.
            messages2 = (
                [system_msg]
                + history
                + [{"role": "assistant", "content": msg.content or "", "tool_calls": assistant_tool_calls}]
                + tool_messages
            )
            try:
                response2 = client.chat.completions.create(
                    model=model,
                    messages=messages2,
                    tools=APP_TOOLS,
                    tool_choice="none",  # summary turn: force plain text, no new tool calls
                    temperature=0.4,
                    max_tokens=1024,
                )
                usage_second = _extract_usage_from_sdk_response(response2)
                total_input_tokens += int(usage_second.get("input_tokens") or 0)
                total_output_tokens += int(usage_second.get("output_tokens") or 0)
                llm_call_count += 1
                reply = response2.choices[0].message.content or "Done!"
            except Exception as exc2:
                status2 = getattr(exc2, "status_code", None)
                if status2 == 400:
                    # Some models (e.g. openai/gpt-oss-120b) IGNORE
                    # tool_choice="none" and emit another tool call, which
                    # Groq rejects with HTTP 400 "tool_use_failed" instead
                    # of suppressing it. Retry the summary WITHOUT the tool
                    # protocol (no tools, no tool_calls/tool messages) so
                    # the model is forced to reply in plain text.
                    LOGGER.warning(
                        "Groq summary call rejected (HTTP 400, tool_choice=none ignored); "
                        "retrying without tools: %s", exc2
                    )
                    results_blob = _json.dumps(tool_results, default=str)[:2000]
                    fallback_msgs = (
                        [system_msg]
                        + history
                        + [{
                            "role": "user",
                            "content": (
                                "You just performed these actions for the user:\n"
                                f"{results_blob}\n\n"
                                "Reply with a short, friendly summary of what you did."
                            ),
                        }]
                    )
                    resp2 = client.chat.completions.create(
                        model=model,
                        messages=fallback_msgs,
                        temperature=0.4,
                        max_tokens=1024,
                    )
                    usage_second = _extract_usage_from_sdk_response(resp2)
                    total_input_tokens += int(usage_second.get("input_tokens") or 0)
                    total_output_tokens += int(usage_second.get("output_tokens") or 0)
                    llm_call_count += 1
                    reply = resp2.choices[0].message.content or "Done!"
                else:
                    raise

            return {
                "reply": reply,
                "tool_results": tool_results,
                "selected_recipes": [], "grocery_additions": [],
                "discretionary_events": [], "spending_events": [], "income_events": [],
                "balance_reconciliation": None, "shopping_corrections": [], "bill_updates": [],
                "target_meals": _get_target_from_results(tool_results),
                "_fallback": False,
                "_llm_usage": {
                    "provider": "groq",
                    "model": model,
                    "llm_calls": llm_call_count,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
            }

        except Exception as exc:
            status = getattr(exc, "status_code", None)
            LOGGER.warning("Groq chat failed on %s: %s", model, exc)
            if status == 404:
                last_err = _friendly_status_msg(404, model)
                continue
            detail = (
                f"Could not reach Groq ({type(exc).__name__}: {exc})"
                if status is None
                else _friendly_status_msg(status, model)
            )
            if tool_results:
                # Tools already executed + persisted — report what ran rather
                # than re-dispatching via regex (which would double-apply).
                return {
                    "reply": "", "tool_results": tool_results,
                    "selected_recipes": [], "grocery_additions": [],
                    "discretionary_events": [], "spending_events": [], "income_events": [],
                    "balance_reconciliation": None, "shopping_corrections": [], "bill_updates": [],
                    "target_meals": None, "_fallback": False,
                    "_llm_error": detail,
                }
            return _degraded(detail)

    return _degraded(last_err or "All configured Groq chat models failed.")
