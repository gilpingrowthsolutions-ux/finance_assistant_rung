"""
Rung AI Copilot — natural-language parser for financial actions.
================================================================

Accepts free-form text (typed or voice-transcribed) and parses it into
structured database actions: recipe selection, grocery items,
discretionary spending, and bill updates.

Provider auto-detection (in priority order):

  1. **Groq** — if ``GROQ_API_KEY`` is set, calls the Groq API
     (fast open-weights inference via ``llama-3.1-8b-instant``).

  2. **Ollama** — if ``OLLAMA_BASE_URL`` is set, calls a local
     Ollama instance (defaults to ``http://localhost:11434``).

  3. **Regex fallback** — if neither provider is configured, a
     keyword-based parser extracts what it can and annotates the
     result with ``_fallback: true`` so the frontend can show a
     degraded-mode banner.

Usage
-----
    from services.copilot_service import parse_copilot_prompt

    result = parse_copilot_prompt(
        "Cook chicken rice bowl and fajitas this week. "
        "Also add Netflix $22.99/mo. I need dish soap and paper towels."
    )
    # result → {
    #     "selected_recipes": [{"title": "chicken rice bowl"}, ...],
    #     "grocery_additions": ["dish soap", "paper towels"],
    #     "discretionary_events": [],
    #     "bill_updates": [{"name": "Netflix", "amount": 22.99}],
    #     ...
    # }
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("copilot_service")

# ---------------------------------------------------------------------------
# System prompt — tells the LLM exactly what JSON shape to produce
# ---------------------------------------------------------------------------

_COPILOT_SYSTEM_PROMPT = """You are Rung's financial assistant. The user will describe what they want to do with their money and meal planning this pay period.

Parse their message into a single JSON object with these keys:

  selected_recipes: list of { "title": "<recipe name>", "action": "add" | "remove" }
    — Recipes the user wants to cook (or stop cooking). Use "add" or "remove".
    If they just mention a meal without a clear add/remove signal, default to "add".

  grocery_additions: list of strings
    — Non-recipe household items they need (detergent, soap, paper towels, etc.).
    One string per item. Keep names short and lowercase.

  discretionary_events: list of { "description": "<event>", "amount": <float> }
    — Dining out, entertainment, one-off purchases. Amount is in USD.

  bill_updates: list of { "name": "<bill>", "amount": <float>, "action": "add" | "remove" }
    — New recurring bills or changes to existing ones. Amount is monthly in USD.

Rules:
- If the user doesn't mention a category, return an empty list for it.
- Only include items the user explicitly mentioned — do not invent anything.
- If a recipe title is ambiguous, use the user's exact wording.
- Return ONLY valid JSON, no markdown fences, no commentary."""


# ---------------------------------------------------------------------------
# Provider: Groq (cloud-hosted open-weights LLM)
# ---------------------------------------------------------------------------

def _call_groq(prompt: str, api_key: str = "") -> Optional[Dict[str, Any]]:
    """Send *prompt* to the Groq API and return the parsed JSON response.

    Uses the ``llama-3.1-8b-instant`` model for low-latency parsing.
    *api_key* overrides the ``GROQ_API_KEY`` environment variable.
    Returns ``None`` on any failure (network, auth, bad response).
    """
    if not api_key:
        api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        return None

    try:
        import requests
    except ImportError:
        LOGGER.warning("requests not available for Groq call")
        return None

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": _COPILOT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()

        content = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            return None

        # Strip markdown fences if present
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

        return _json.loads(content)

    except _json.JSONDecodeError as exc:
        LOGGER.warning("Groq returned invalid JSON: %s", exc)
        return None
    except Exception as exc:
        LOGGER.warning("Groq call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Provider: Ollama (local LLM server)
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> Optional[Dict[str, Any]]:
    """Send *prompt* to a local Ollama instance and return parsed JSON.

    Defaults to ``llama3.1:8b`` model. Configure via ``OLLAMA_MODEL`` env var.
    Returns ``None`` on any failure.
    """
    base_url = (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
    model = (os.environ.get("OLLAMA_MODEL") or "llama3.1:8b").strip()

    try:
        import requests
    except ImportError:
        LOGGER.warning("requests not available for Ollama call")
        return None

    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "system": _COPILOT_SYSTEM_PROMPT,
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

        # Ollama sometimes wraps JSON in markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        return _json.loads(raw)

    except _json.JSONDecodeError as exc:
        LOGGER.warning("Ollama returned invalid JSON: %s", exc)
        return None
    except Exception as exc:
        LOGGER.warning("Ollama call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Fallback: regex-based keyword parser (no LLM required)
# ---------------------------------------------------------------------------

# Phrases that signal a bill / subscription being added.
_BILL_PATTERNS = [
    # "$22.99/mo", "$10 per month", etc.
    re.compile(
        r"(?:add\s+)?(?P<name>[^(]+?)\s*"
        r"\$?(?P<amount>\d+(?:\.\d{1,2})?)"
        r"\s*(?:\/|per\s+)?mo(?:nth)?",
        re.IGNORECASE,
    ),
    # "subscribe to Netflix $22.99"
    re.compile(
        r"(?:subscribe|add|new)\s+(?P<name>[^(]+?)\s*"
        r"\$?(?P<amount>\d+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
]

# Phrases that signal discretionary spending.
_DISCRETIONARY_PATTERNS = [
    re.compile(
        r"(?:dinner|dining|lunch|breakfast|eat)\s+(?:out|at)\s+(?P<desc>.+?)\s*"
        r"\$?(?P<amount>\d+(?:\.\d{1,2})?)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"buy\s+(?P<desc>.+?)\s+\$?(?P<amount>\d+(?:\.\d{1,2})?)\s*",
        re.IGNORECASE,
    ),
]

# Common kitchen/household items (seeded list for regex fallback).
_HOUSEHOLD_TRIGGERS = re.compile(
    r"\b(dish\s*soap|detergent|paper\s*towels|toilet\s*paper|hand\s*soap|"
    r"sponges?|trash\s*bags?|ziploc|aluminum\s*foil|plastic\s*wrap|"
    r"laundry\s*detergent|bleach|windex|clorox|tide|dawn|febreze|"
    r"shampoo|conditioner|body\s*wash|deodorant|toothpaste|floss)\b",
    re.IGNORECASE,
)

# "cook X", "make X", "meal prep X"  → recipe selection
_RECIPE_TRIGGERS = re.compile(
    r"(?:cook|make|prepare|meal\s*prep)\s+(?P<title>.+?)(?:this\s*week|"
    r"for\s*dinner|for\s*lunch|tonight|today|tomorrow|\.|$)",
    re.IGNORECASE,
)


def _regex_fallback(user_text: str) -> Dict[str, Any]:
    """Extract structured actions using regex patterns.

    Returns a dict with the same shape as the LLM response plus a
    ``_fallback: true`` flag for the frontend.
    """
    result: Dict[str, Any] = {
        "selected_recipes": [],
        "grocery_additions": [],
        "discretionary_events": [],
        "bill_updates": [],
        "_fallback": True,
    }

    s = user_text.strip()

    # ---- Bills ----
    for pat in _BILL_PATTERNS:
        for m in pat.finditer(s):
            name = (m.group("name") or "").strip().lower()
            amount = float(m.group("amount") or 0)
            if name and amount > 0:
                # Skip if already found
                if not any(b["name"] == name for b in result["bill_updates"]):
                    result["bill_updates"].append({
                        "name": name,
                        "amount": amount,
                        "action": "add",
                    })

    # ---- Discretionary events ----
    for pat in _DISCRETIONARY_PATTERNS:
        for m in pat.finditer(s):
            desc = (m.group("desc") or "").strip()
            amount_str = m.group("amount")
            amount = float(amount_str) if amount_str else 20.0  # default estimate
            if desc:
                result["discretionary_events"].append({
                    "description": desc,
                    "amount": amount,
                })

    # ---- Grocery additions ----
    for m in _HOUSEHOLD_TRIGGERS.finditer(s):
        item = m.group(0).lower().strip()
        if item not in result["grocery_additions"]:
            result["grocery_additions"].append(item)

    # ---- Recipe selection ----
    for m in _RECIPE_TRIGGERS.finditer(s):
        title = (m.group("title") or "").strip().rstrip(".,;!?")
        if title and len(title) > 2:
            result["selected_recipes"].append({"title": title, "action": "add"})

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_copilot_prompt(user_text: str, groq_api_key: str = "") -> Dict[str, Any]:
    """Parse free-form user text into structured Rung actions.

    Provider priority: Groq → Ollama → regex fallback.
    The ``_fallback`` key is ``True`` only when the regex parser was used.

    Parameters
    ----------
    user_text : str
        Natural-language description of what the user wants to do
        (e.g., "Cook chicken rice bowl. Add Netflix $22.99/mo. I need dish soap.")
    groq_api_key : str, optional
        Groq API key from the BYOK settings. If empty, falls back to
        the ``GROQ_API_KEY`` environment variable.

    Returns
    -------
    dict
        Keys: ``selected_recipes``, ``grocery_additions``,
        ``discretionary_events``, ``bill_updates``, ``_fallback``.
    """
    if not user_text or not user_text.strip():
        return {
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "bill_updates": [],
            "_fallback": False,
        }

    prompt = user_text.strip()

    # 1. Try Groq (with explicit key, falling back to env var)
    result = _call_groq(prompt, api_key=groq_api_key)
    if result is not None:
        result["_fallback"] = False
        return result

    # 2. Try Ollama
    result = _call_ollama(prompt)
    if result is not None:
        result["_fallback"] = False
        return result

    # 3. Regex fallback
    LOGGER.info("No LLM provider configured — using regex fallback parser")
    return _regex_fallback(user_text)
