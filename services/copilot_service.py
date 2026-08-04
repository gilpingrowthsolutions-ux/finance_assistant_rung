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

  4. **Regex fallback** — if no provider is configured, a keyword-
     based parser extracts what it can and annotates the result with
     ``_fallback: true`` so the frontend can show a degraded-mode
     banner.

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
import re
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("copilot_service")

# Lazy-loaded Groq client class. Tests patch ``copilot_service._Groq``
# to inject fakes; production imports the SDK on first use via
# ``_get_groq_class``.
_Groq = None

# ============================================================================
# Active Groq Client — reads the key directly from SQLite
# ============================================================================


def get_active_groq_client() -> Any:
    """Instantiate a ``Groq`` client with the key from ``user_settings``.

    Reads the key directly from the SQLite database to avoid circular
    imports with Flask-SQLAlchemy models.  Falls back to the
    ``GROQ_API_KEY`` environment variable if the DB is empty.

    Returns
    -------
    Groq
        An authenticated Groq client instance.

    Raises
    ------
    RuntimeError
        If no Groq API key is found in the DB or environment.
    """
    key = _read_groq_key_from_db()
    if not key:
        key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "No Groq API key configured. "
            "Please go to Settings → AI Copilot BYOK and save your key."
        )

    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError(
            "The 'groq' Python SDK is not installed. "
            "Run: pip install groq"
        )

    return Groq(api_key=key)


def _read_groq_key_from_db() -> str:
    """Read the Groq API key directly from SQLite ``user_settings`` table.

    Avoids importing Flask-SQLAlchemy models to prevent circular imports.
    Returns ``''`` if the table doesn't exist or the key is not set.
    """
    import sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "rung_finance.db")
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(
            "SELECT value FROM user_settings WHERE key = 'groq_api_key'"
        )
        row = c.fetchone()
        conn.close()
        return (row[0] or "").strip() if row else ""
    except (sqlite3.OperationalError, Exception):
        return ""


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

            return {
                "tool_results": tool_results,
                "selected_recipes": [],
                "grocery_additions": [],
                "discretionary_events": [],
                "bill_updates": [],
                "target_meals": _get_target_from_results(tool_results),
                "_fallback": False,
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
    try:
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        data = _json.loads(content)
        data["_fallback"] = False
        return data
    except (_json.JSONDecodeError, ValueError):
        return _empty_result()


def _no_key_result() -> Dict[str, Any]:
    """Return the 'no key' result — triggers the next provider in the chain."""
    return {"__no_key__": True}


def _error_result(message: str) -> Dict[str, Any]:
    """Return a 'key configured but call failed' result with a real message.

    Carries ``__no_key__: True`` so the provider chain still falls
    through to the regex fallback (degraded mode), plus ``__error__``
    so ``parse_copilot_prompt`` can attach the honest reason for the
    frontend instead of the misleading "no LLM configured" banner.
    """
    return {"__no_key__": True, "__error__": message}


def _empty_result() -> Dict[str, Any]:
    """Return an empty result with the standard shape."""
    return {
        "tool_results": [],
        "selected_recipes": [],
        "grocery_additions": [],
        "discretionary_events": [],
        "bill_updates": [],
        "target_meals": None,
        "_fallback": False,
    }


# ============================================================================
# Provider: Groq (old prompt-based JSON path — fallback)
# ============================================================================


def _call_groq_json(prompt: str, api_key: str = "") -> Optional[Dict[str, Any]]:
    """Send *prompt* to the Groq API and return the parsed JSON response.

    Tries the ``_json_models()`` chain (env-overridable, default
    ``openai/gpt-oss-20b``).  This is the old prompt-based path — used
    when the tool-calling call issued a plain-text response.

    Returns ``None`` only when no key is available.  When a key IS set
    but the call fails, returns an ``__error__`` dict so the caller can
    surface the honest reason instead of silently degrading.
    """
    if not api_key:
        api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        return None

    try:
        import requests
    except ImportError:
        return _error_result("The 'requests' Python SDK is not installed.")

    last_err = None
    for model in _json_models():
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _COPILOT_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 1024,
                },
                timeout=15,
            )
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
                return _error_result(_friendly_status_msg(resp.status_code, model, body))
            body = resp.json()
            content = (
                body.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not content:
                return None
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            return _json.loads(content)
        except (_json.JSONDecodeError, ValueError):
            return None
        except Exception as exc:
            LOGGER.warning("Groq JSON call failed on %s: %s", model, exc)
            return _error_result(
                f"Could not reach Groq ({type(exc).__name__}: {exc})"
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
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return _json.loads(raw)
    except (_json.JSONDecodeError, Exception) as exc:
        LOGGER.warning("Ollama call failed: %s", exc)
        return None


# ============================================================================
# Fallback: regex-based keyword parser (no LLM required)
# ============================================================================

_BILL_PATTERNS = [
    re.compile(
        r"(?:add\s+)?(?P<name>[^(]+?)\s*"
        r"\$?(?P<amount>\d+(?:\.\d{1,2})?)"
        r"\s*(?:\/|per\s+)?mo(?:nth)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:subscribe|add|new)\s+(?P<name>[^(]+?)\s*"
        r"\$?(?P<amount>\d+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    ),
]

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

_HOUSEHOLD_TRIGGERS = re.compile(
    r"\b(dish\s*soap|detergent|paper\s*towels|toilet\s*paper|hand\s*soap|"
    r"sponges?|trash\s*bags?|ziploc|aluminum\s*foil|plastic\s*wrap|"
    r"laundry\s*detergent|bleach|windex|clorox|tide|dawn|febreze|"
    r"shampoo|conditioner|body\s*wash|deodorant|toothpaste|floss)\b",
    re.IGNORECASE,
)

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
        "tool_results": [],
        "selected_recipes": [],
        "grocery_additions": [],
        "discretionary_events": [],
        "bill_updates": [],
        "target_meals": None,
        "_fallback": True,
    }

    s = user_text.strip()

    # ---- Bills ----
    for pat in _BILL_PATTERNS:
        for m in pat.finditer(s):
            name = (m.group("name") or "").strip().lower()
            amount = float(m.group("amount") or 0)
            if name and amount > 0:
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
            amount = float(amount_str) if amount_str else 20.0
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

    # ---- Target meal count (\"7 meals\", \"plan 5 dinners\") ----
    m = re.search(r'(\d+)\s*(?:meals?|dinners?|dishes?|recipes?)', s, re.IGNORECASE)
    if m:
        result["target_meals"] = int(m.group(1))

    return result


# ============================================================================
# Public API
# ============================================================================


def parse_copilot_prompt(user_text: str, groq_api_key: str = "") -> Dict[str, Any]:
    """Parse free-form user text into structured Rung actions.

    Provider priority:
      1. Groq (native tool calling with ``groq`` SDK)
      2. Groq (old prompt-based JSON)
      3. Ollama
      4. Regex fallback

    The ``_fallback`` key is ``True`` only when the regex parser was used.

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
    if not user_text or not user_text.strip():
        return {
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "bill_updates": [],
            "target_meals": None,
            "_fallback": False,
        }

    prompt = user_text.strip()

    # Track the honest reason when a key IS configured but the LLM call
    # fails — so the UI can say "Groq rejected your key" instead of the
    # misleading "no LLM configured".
    llm_error = None

    # 1. Try Groq native tool calling (with explicit key, falling back to env var)
    if not groq_api_key:
        groq_api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if groq_api_key:
        result = _call_groq_tools(prompt, api_key=groq_api_key)
        # Only return the result if the API call actually succeeded.
        # ``__no_key__`` signals a failure (no key, auth reject, network,
        # parse error) that should fall through to the next provider.
        if not result.get("__no_key__"):
            return result
        llm_error = result.get("__error__")

    # 2. Try Groq old prompt-based JSON
    result = _call_groq_json(prompt, api_key=groq_api_key)
    if result is not None:
        if result.get("__error__"):
            # Prefer the API's verdict (e.g. a 401) over an earlier
            # SDK-missing message — the requests-based JSON path can
            # succeed without the groq SDK, so its error is the real one.
            llm_error = result.get("__error__") or llm_error
        else:
            result["_fallback"] = False
            return result

    # 3. Try Ollama
    result = _call_ollama(prompt)
    if result is not None:
        result["_fallback"] = False
        return result

    # 4. Regex fallback (degraded mode) — attach the real error if a key
    #    was configured but the LLM provider call failed.
    LOGGER.info("No working LLM provider — using regex fallback parser")
    fallback = _regex_fallback(user_text)
    if llm_error:
        fallback["_llm_error"] = llm_error
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
        # No LLM — degrade to keyword parsing of the latest user message.
        last_user = next(
            (m.get("content", "") for m in reversed(messages)
             if m.get("role") == "user"),
            "",
        )
        fb = _regex_fallback(last_user)
        fb["reply"] = (
            "⚠️ I'm running in offline mode (no LLM configured), so I "
            "parsed your last message with keyword matching. "
            "Save a Groq API key in Settings to chat with me properly."
        )
        return fb

    from services.copilot_tools import APP_TOOLS, execute_app_function

    groq_class = _get_groq_class()
    if groq_class is None:
        return {
            "reply": "The 'groq' Python SDK is not installed. Run: pip install groq",
            "tool_results": [], "selected_recipes": [], "grocery_additions": [],
            "discretionary_events": [], "bill_updates": [], "target_meals": None,
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

    def _degraded(llm_error: str) -> Dict[str, Any]:
        """Degrade to keyword parsing, attaching the honest error."""
        last_user = next(
            (m.get("content", "") for m in reversed(messages)
             if m.get("role") == "user"),
            "",
        )
        fb = _regex_fallback(last_user)
        fb["_fallback"] = True
        fb["_llm_error"] = llm_error
        fb["reply"] = (
            "⚠️ I couldn't reach the AI model, so I parsed your last "
            "message with keyword matching. " + llm_error
        )
        return fb

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
            msg = response.choices[0].message

            # Pure conversational reply (no tools) — return it directly.
            if not msg.tool_calls:
                return {
                    "reply": msg.content or "",
                    "tool_results": [], "selected_recipes": [],
                    "grocery_additions": [], "discretionary_events": [],
                    "bill_updates": [], "target_meals": None,
                    "_fallback": False,
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
                    reply = resp2.choices[0].message.content or "Done!"
                else:
                    raise

            return {
                "reply": reply,
                "tool_results": tool_results,
                "selected_recipes": [], "grocery_additions": [],
                "discretionary_events": [], "bill_updates": [],
                "target_meals": _get_target_from_results(tool_results),
                "_fallback": False,
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
                    "discretionary_events": [], "bill_updates": [],
                    "target_meals": None, "_fallback": False,
                    "_llm_error": detail,
                }
            return _degraded(detail)

    return _degraded(last_err or "All configured Groq chat models failed.")