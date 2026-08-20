"""Integration tests for the AI Copilot parser and dispatch endpoint."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so db.drop_all()/create_all() can never wipe rung_finance.db.
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app, db, Account, Bill, ExpenseTransaction, GroceryItem, Recipe, RecipeIngredient, MealPlanItem, ActionAudit
import services.copilot_intent as ci
import services.copilot_service as cs
from services.household_context import household_id as current_household_id

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

client = app.test_client()
app.testing = True
_pass = 0
_fail = 0


def _setup():
    """Fresh DB with a single account (and no env-var key leak)."""
    # app.py runs load_dotenv() at import, so GROQ_API_KEY from .env can
    # leak into os.environ and make tests hit the real API.  Remove it
    # so the regex-fallback tests are hermetic.
    os.environ.pop("GROQ_API_KEY", None)
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add(Account(household_id=current_household_id(), checking_balance=1250.00))
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


def _seed_recipes():
    """Seed a small recipe library for meal-plan auto-fill tests."""
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
        for title, cost, ingredients in rows:
            r = Recipe(title=title, servings=4, estimated_cost_per_serving=cost)
            db.session.add(r)
            db.session.flush()
            for name, kw in ingredients:
                db.session.add(RecipeIngredient(
                    recipe_id=r.id,
                    product_name=name,
                    clean_keyword=kw,
                    quantity=1.0,
                    unit="oz",
                ))
        db.session.commit()


# ---------------------------------------------------------------------------
# 1 — Regex fallback parser (no LLM credentials)
# ---------------------------------------------------------------------------


def test_copilot_parse_bills_regex():
    """Regex fallback extracts bills with $/mo pattern."""
    _setup()
    with app.app_context():
        Bill.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "Add Netflix $22.99/mo and Spotify $9.99 per month"},
    )
    _assert_eq(resp.status_code, 200, "copilot/parse returns 200")
    d = resp.get_json() or {}

    _assert_truthy(d.get("_fallback"), "regex fallback flag is set")
    actions = d.get("actions_taken", {})
    bills = actions.get("bills_added", [])
    _assert_eq(len(bills), 2, "two bills extracted")
    names = {b["name"] for b in bills}
    _assert_truthy("netflix" in names or "spotify" in names, "bill names found")

    with app.app_context():
        db_bills = Bill.query.all()
        _assert_eq(len(db_bills), 2, "two bills persisted")


def test_copilot_parse_grocery_regex():
    """Regex fallback extracts household items."""
    _setup()
    with app.app_context():
        GroceryItem.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "I need dish soap and paper towels for the kitchen"},
    )
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    items = actions.get("grocery_items_added", [])
    _assert_truthy(len(items) >= 1, "at least one grocery item found")

    with app.app_context():
        db_items = GroceryItem.query.all()
        _assert_truthy(len(db_items) >= 1, "grocery items persisted")


def test_copilot_parse_discretionary_regex():
    """Regex fallback extracts dining-out events."""
    _setup()
    with app.app_context():
        ExpenseTransaction.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "Dinner out at Olive Garden $45"},
    )
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    events = actions.get("expenses_logged", [])
    _assert_truthy(len(events) >= 1, "discretionary event extracted")


def test_copilot_parse_recipes_regex():
    """Regex fallback extracts recipe suggestions."""
    _setup()
    resp = client.post(
        "/api/copilot/parse",
        json={"text": "Cook chicken rice bowl and flank steak fajitas this week"},
    )
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    recipes = actions.get("recipes_suggested", [])
    _assert_truthy(len(recipes) >= 1, "at least one recipe suggested")


# ---------------------------------------------------------------------------
# 2 — Combined multi-intent input
# ---------------------------------------------------------------------------


def test_copilot_multi_intent():
    """A single message with bills + grocery + discretionary is handled."""
    _setup()
    with app.app_context():
        Bill.query.delete()
        ExpenseTransaction.query.delete()
        GroceryItem.query.delete()
        db.session.commit()

    text = (
        "Meal prep chicken rice bowl. "
        "Add Netflix $22.99/mo. "
        "I need dish soap and paper towels. "
        "Dinner out at Chili's $35"
    )
    resp = client.post("/api/copilot/parse", json={"text": text})
    _assert_eq(resp.status_code, 200)
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})

    total = (
        len(actions.get("bills_added", []))
        + len(actions.get("expenses_logged", []))
        + len(actions.get("grocery_items_added", []))
        + len(actions.get("recipes_suggested", []))
    )
    _assert_truthy(total >= 2, f"multi-intent had at least 2 actions (got {total})")


# ---------------------------------------------------------------------------
# 3 — Empty / missing input
# ---------------------------------------------------------------------------


def test_copilot_empty_text():
    _setup()
    resp = client.post("/api/copilot/parse", json={"text": ""})
    _assert_eq(resp.status_code, 400, "empty text returns 400")


def test_copilot_missing_text():
    _setup()
    resp = client.post("/api/copilot/parse", json={})
    _assert_eq(resp.status_code, 400, "missing text returns 400")


# ---------------------------------------------------------------------------
# 4 — No-op input (text with no actionable content)
# ---------------------------------------------------------------------------


def test_copilot_noop():
    """Gibberish that matches no patterns returns empty actions."""
    _setup()
    resp = client.post("/api/copilot/parse", json={"text": "hello world"})
    _assert_eq(resp.status_code, 200)
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    total = sum(len(v) for v in (actions or {}).values() if isinstance(v, list))
    _assert_eq(total, 0, "no actions for no-op input")


# ---------------------------------------------------------------------------
# 5 — Account balance decrement on expenses
# ---------------------------------------------------------------------------


def test_copilot_expense_decrements_balance():
    """Logging an expense via copilot should decrement checking_balance."""
    _setup()
    with app.app_context():
        acc = Account.query.first()
        acc.checking_balance = 1000.00
        ExpenseTransaction.query.delete()
        db.session.commit()

    resp = client.post(
        "/api/copilot/parse",
        json={"text": "buy a new impact driver $120"},
    )
    _assert_eq(resp.status_code, 200)

    with app.app_context():
        acc = Account.query.first()
        _assert_truthy(acc.checking_balance < 1000.00, "balance was decremented")


# ---------------------------------------------------------------------------
# 5b — Real LLM errors are surfaced (not masked as "no LLM configured")
# ---------------------------------------------------------------------------


def test_copilot_surfaces_groq_error():
    """When a key IS configured but Groq rejects it, the response carries
    ``llm_error`` (the honest reason) instead of only ``_fallback``.

    This is the regression test for the reported bug: settings said the
    key was configured, but the Copilot showed "offline mode (no LLM
    configured)" because every Groq failure was swallowed and reported
    as a missing key.
    """
    import services.copilot_service as cs

    orig_tools = cs._call_groq_tools
    orig_json = cs._call_groq_json
    orig_ollama = cs._call_ollama

    def fake_tools(prompt, api_key=""):
        return {"__no_key__": True, "__error__": "Groq rejected your API key (HTTP 401)."}

    def fake_json(prompt, api_key=""):
        return None

    cs._call_groq_tools = fake_tools
    cs._call_groq_json = fake_json
    cs._call_ollama = lambda prompt: None
    try:
        result = cs.parse_copilot_prompt(
            "add netflix 10/mo", groq_api_key="gsk_configured_but_rejected"
        )
    finally:
        cs._call_groq_tools = orig_tools
        cs._call_groq_json = orig_json
        cs._call_ollama = orig_ollama

    _assert_eq(result.get("_fallback"), True, "regex fallback still runs")
    _assert_truthy(result.get("_llm_error"), "_llm_error is set")
    _assert_truthy("401" in result.get("_llm_error", ""), "llm_error mentions 401")


def test_copilot_no_error_without_key():
    """Without any key, the plain regex fallback has NO llm_error — the
    classic 'offline mode' banner is still correct in that case.

    NOTE: ``app`` calls ``load_dotenv()`` at import, so ``GROQ_API_KEY``
    may be present in ``os.environ`` even when the DB is wiped. We
    temporarily remove it so this test exercises the true "no key" path.
    """
    _setup()
    import services.copilot_service as cs

    saved_env = os.environ.pop("GROQ_API_KEY", None)
    orig_ollama = cs._call_ollama
    cs._call_ollama = lambda prompt: None
    try:
        result = cs.parse_copilot_prompt("add netflix 10/mo", groq_api_key="")
    finally:
        if saved_env is not None:
            os.environ["GROQ_API_KEY"] = saved_env
        cs._call_ollama = orig_ollama

    _assert_eq(result.get("_fallback"), True, "regex fallback used")
    _assert_eq(result.get("_llm_error"), None, "no llm_error when no key")


def test_copilot_endpoint_passes_llm_error():
    """/api/copilot/parse returns a customer-safe llm_error field."""
    _setup()
    import services.copilot_service as cs

    orig_tools = cs._call_groq_tools
    orig_json = cs._call_groq_json
    orig_ollama = cs._call_ollama
    cs._call_groq_tools = lambda prompt, api_key="": {
        "__no_key__": True, "__error__": "Groq rejected your API key (HTTP 401)."
    }
    cs._call_groq_json = lambda prompt, api_key="": None
    cs._call_ollama = lambda prompt: None

    # The endpoint reads the key from the DB or env var, so we must set it
    # here (even though the stub ignores it) for the endpoint to enter the
    # LLM branch instead of skipping straight to regex.
    os.environ["GROQ_API_KEY"] = "gsk_test_llm_error_endpoint"
    try:
        resp = client.post(
            "/api/copilot/parse",
            json={"text": "add netflix 10/mo"},
        )
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._call_groq_tools = orig_tools
        cs._call_groq_json = orig_json
        cs._call_ollama = orig_ollama

    d = resp.get_json() or {}
    _assert_truthy(d.get("llm_error"), "endpoint includes llm_error")
    _assert_eq(
        d.get("llm_error"),
        "Copilot is temporarily unavailable. Please try again later.",
        "llm_error is customer-safe",
    )
    _assert_truthy("401" not in d.get("llm_error", ""), "llm_error omits HTTP status")
    _assert_truthy("groq" not in d.get("llm_error", "").lower(), "llm_error omits provider name")


# ---------------------------------------------------------------------------
# 5c — Multi-turn hybrid chat endpoint (/api/copilot/chat)
# ---------------------------------------------------------------------------


def test_chat_endpoint_requires_messages():
    """Missing/invalid messages payload is rejected."""
    _setup()
    resp = client.post("/api/copilot/chat", json={})
    _assert_eq(resp.status_code, 400, "empty body rejected")
    resp = client.post("/api/copilot/chat", json={"messages": []})
    _assert_eq(resp.status_code, 400, "empty messages rejected")
    resp = client.post("/api/copilot/chat", json={"messages": [{"role": "assistant", "content": "hi"}]})
    _assert_eq(resp.status_code, 400, "non-user last message rejected")


def test_chat_endpoint_regex_fallback():
    """With no key, /api/copilot/chat degrades to keyword parsing and still
    returns a reply + executed actions (bills persisted)."""
    _setup()
    import services.copilot_service as cs

    # Keep the test hermetic: no env key → chat returns before any
    # provider call, so no stubs are needed (regex fallback only).
    os.environ.pop("GROQ_API_KEY", None)
    resp = client.post(
        "/api/copilot/chat",
        json={"messages": [{"role": "user", "content": "Add Netflix $22.99/mo"}]},
    )

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    _assert_truthy(d.get("reply"), "has a reply string")
    _assert_truthy(d.get("_fallback"), "regex fallback flag set")
    bills = d.get("actions_taken", {}).get("bills_added", [])
    _assert_eq(len(bills), 1, "bill parsed from message")
    with app.app_context():
        _assert_eq(Bill.query.count(), 1, "bill persisted")


def test_chat_endpoint_with_llm_error():
    """When a key IS configured but Groq rejects it, chat returns llm_error
    and still dispatches keyword-parsed actions (degraded but honest)."""
    _setup()
    import services.copilot_service as cs

    # chat_copilot_prompt builds its own Groq client, so we stub the
    # module-level cs._Groq with a client whose create() raises 401.
    class FakeStatusError(Exception):
        status_code = 401

    class FakeCompletions:
        def create(self, **kwargs):
            raise FakeStatusError("Groq rejected the API key")

    class FakeGroq:
        def __init__(self, api_key):
            self.chat = type("C", (), {"completions": FakeCompletions()})()

    orig_groq = cs._Groq
    cs._Groq = FakeGroq
    os.environ["GROQ_API_KEY"] = "gsk_test_chat_llm_error"
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content": "I need dish soap"}]},
        )
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._Groq = orig_groq

    d = resp.get_json() or {}
    _assert_truthy(d.get("llm_error"), "llm_error surfaced")
    _assert_eq(
        d.get("llm_error"),
        "Copilot is temporarily unavailable. Please try again later.",
        "llm_error is customer-safe",
    )
    _assert_truthy("401" not in d.get("llm_error", ""), "llm_error omits HTTP status")
    items = d.get("actions_taken", {}).get("grocery_items_added", [])
    _assert_eq(len(items), 1, "grocery item still parsed via regex")


def test_chat_endpoint_plain_text_response_falls_back_to_parser():
    """A plain-text chat reply may still require execution via the parser."""
    _setup()
    import services.copilot_service as cs

    class FakeToolCall:
        def __init__(self, name, arguments):
            self.function = type("F", (), {"name": name, "arguments": arguments})()
            self.id = "call_1"

    class FakeMsg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class FakeResponse:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    class FakeCompletions:
        def __init__(self):
            self.count = 0
            self.creates = []

        def create(self, **kwargs):
            self.creates.append(kwargs)
            self.count += 1
            if self.count == 1:
                return FakeResponse(FakeMsg("Sure, I set your meal target to 5 dinners."))
            return FakeResponse(FakeMsg(None, [FakeToolCall("set_target_meals", '{"count": 5}')]))

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeGroq:
        def __init__(self, api_key):
            self.chat = FakeChat()

    orig_groq = cs._Groq
    cs._Groq = FakeGroq
    os.environ["GROQ_API_KEY"] = "gsk_test_plain_text_fallback"
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content": "plan 5 dinners"}]},
        )
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._Groq = orig_groq

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    _assert_eq(d.get("_fallback"), False, "not fallback")
    _assert_eq(d.get("actions_taken", {}).get("target_meals"), 5, "target meals persisted")
    tr = d.get("tool_results", [])
    _assert_eq(len(tr), 0, "no tool_results when using regex fallback")


def test_chat_endpoint_plain_text_response_executes_intent_pipeline():
    """A plain-text chat reply with no tool calls should still execute parsed actions."""
    _setup()
    import services.copilot_service as cs

    class FakeMsg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class FakeResponse:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeResponse(FakeMsg("Sure, I added those items for you."))

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeGroq:
        def __init__(self, api_key):
            self.chat = FakeChat()

    orig_groq = cs._Groq
    orig_json = cs._call_groq_json
    cs._Groq = FakeGroq
    cs._call_groq_json = lambda prompt, api_key="": {
        "tool_results": [],
        "selected_recipes": [],
        "grocery_additions": ["dish soap"],
        "discretionary_events": [],
        "bill_updates": [{"name": "netflix", "amount": 22.99, "action": "add"}],
        "target_meals": None,
        "_fallback": False,
    }
    os.environ["GROQ_API_KEY"] = "gsk_test_plain_text_intent_pipeline"
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content": "Add Netflix $22.99/mo and dish soap"}]},
        )
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._Groq = orig_groq
        cs._call_groq_json = orig_json

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    _assert_eq(d.get("_fallback"), False, "not fallback")
    actions = d.get("actions_taken", {})
    _assert_eq(len(actions.get("bills_added", [])), 1, "bill action executed")
    _assert_eq(len(actions.get("grocery_items_added", [])), 1, "grocery action executed")
    _assert_eq(actions.get("grocery_items_added", [])[0], "dish soap", "grocery item persisted")
    with app.app_context():
        _assert_eq(Bill.query.count(), 1, "bill persisted")
        _assert_eq(GroceryItem.query.count(), 1, "grocery item persisted")


def test_chat_plan_speak_auto_fill_persists_meal_plan():
    """Plan language in chat should persist target meals and auto-filled recipes."""
    _setup()
    _seed_recipes()
    import services.copilot_service as cs

    class FakeMsg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class FakeResponse:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    class FakeCompletions:
        def __init__(self):
            self.count = 0
            self.creates = []

        def create(self, **kwargs):
            self.creates.append(kwargs)
            self.count += 1
            if self.count == 1:
                return FakeResponse(FakeMsg("Sure, I can plan 5 dinners for you."))
            return FakeResponse(FakeMsg(None, []))

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeGroq:
        def __init__(self, api_key):
            self.chat = FakeChat()

    orig_groq = cs._Groq
    cs._Groq = FakeGroq
    os.environ["GROQ_API_KEY"] = "gsk_test_plan_speak_auto_fill"
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content": "plan 5 dinners"}]},
        )
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._Groq = orig_groq

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    _assert_eq(d.get("_fallback"), False, "not fallback")
    _assert_eq(d.get("actions_taken", {}).get("target_meals"), 5, "target meals persisted")
    with app.app_context():
        _assert_eq(MealPlanItem.query.count(), 5, "meal plan auto-filled to 5 recipes")


def test_chat_prompt_multi_turn_history():
    """chat_copilot_prompt threads the full history and executes tools.

    Also asserts the Groq tool protocol: the assistant tool_calls message
    immediately precedes its tool results, the first call uses
    tool_choice="auto", and the summary call forces tool_choice="none".
    """
    _setup()
    import services.copilot_service as cs

    # Simulate the Groq client: first call returns a tool call,
    # second call returns the summary text.
    class FakeMsg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class FakeResponse:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    class FakeCompletions:
        def __init__(self, calls):
            self._calls = list(calls)
            self.creates = []

        def create(self, **kwargs):
            self.creates.append(kwargs)
            return FakeResponse(FakeMsg(*self._calls.pop(0)))

    class FakeChat:
        def __init__(self, calls):
            self.completions = FakeCompletions(calls)

    class FakeGroq:
        def __init__(self, calls):
            self.chat = FakeChat(calls)

    class FakeToolCall:
        def __init__(self, name, arguments):
            self.function = type("F", (), {"name": name, "arguments": arguments})()
            self.id = "call_1"

    calls = [
        (None, [FakeToolCall("set_target_meals", '{"count": 3}')]),  # first turn: tool
        ("Great — I set your target to 3 meals!", None),             # second turn: summary
    ]
    fake_client = FakeGroq(calls)

    orig_groq = cs._Groq
    cs._Groq = lambda api_key: fake_client
    try:
        result = cs.chat_copilot_prompt(
            [{"role": "user", "content": "plan 3 meals"}],
            groq_api_key="gsk_fake_but_stubbed",
        )
    finally:
        cs._Groq = orig_groq

    _assert_eq(result.get("reply"), "Great — I set your target to 3 meals!", "summary reply returned")
    _assert_eq(result.get("_fallback"), False, "not fallback")
    results = result.get("tool_results", [])
    _assert_eq(len(results), 1, "one tool executed")
    _assert_eq(results[0]["tool"], "set_target_meals", "tool name")
    _assert_eq(results[0]["status"], "ok", "tool succeeded")

    # ---- Protocol assertions (regression guard) ----
    creates = fake_client.chat.completions.creates
    _assert_eq(len(creates), 2, "two create calls (tool turn + summary)")
    _assert_eq(creates[0].get("tool_choice"), "auto", "first call allows tools")
    _assert_eq(creates[1].get("tool_choice"), "none", "summary call forces plain text")
    msgs2 = creates[1].get("messages", [])
    roles = [m.get("role") for m in msgs2]
    _assert_eq(roles[-2], "assistant", "assistant tool_calls precedes tool results")
    _assert_eq(roles[-1], "tool", "tool result follows assistant message")
    _assert_truthy(msgs2[-2].get("tool_calls"), "assistant message carries tool_calls")


def test_chat_summary_turn_400_tool_use_failed_retries_without_tools():
    """Regression: some Groq models ignore tool_choice="none" and emit
    another tool call on the summary turn, which Groq rejects with HTTP
    400 'tool_use_failed'.  The summary must then be retried WITHOUT the
    tool protocol so the user still gets a reply instead of an error.

    Reproduces:  Groq chat failed on openai/gpt-oss-120b: Error code: 400 -
    {'error': {'message': 'Tool choice is none, but model called a tool',
     'code': 'tool_use_failed', ...}}
    """
    _setup()
    import services.copilot_service as cs

    class FakeStatusError(Exception):
        status_code = 400

    class FakeMsg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class FakeResponse:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    class FakeToolCall:
        def __init__(self, name, arguments):
            self.function = type("F", (), {"name": name, "arguments": arguments})()
            self.id = "call_1"

    class FakeCompletions:
        def __init__(self, calls):
            self._calls = list(calls)
            self.creates = []

        def create(self, **kwargs):
            self.creates.append(kwargs)
            if self._calls:
                return FakeResponse(FakeMsg(*self._calls.pop(0)))
            # Summary turn #1: model IGNORES tool_choice="none" and tries
            # to call set_target_meals again → Groq returns HTTP 400.
            if kwargs.get("tool_choice") == "none":
                raise FakeStatusError("Tool choice is none, but model called a tool")
            # Summary turn #2 (no tools): succeeds with plain text.
            return FakeResponse(FakeMsg("I set your meal target to 7!"))

    class FakeChat:
        def __init__(self, calls):
            self.completions = FakeCompletions(calls)

    class FakeGroq:
        def __init__(self, calls):
            self.chat = FakeChat(calls)

    calls = [
        (None, [FakeToolCall("set_target_meals", '{"count": 7}')]),  # turn 1: tool call
    ]
    fake_client = FakeGroq(calls)

    orig_groq = cs._Groq
    cs._Groq = lambda api_key: fake_client
    try:
        result = cs.chat_copilot_prompt(
            [{"role": "user", "content": "plan 7 meals"}],
            groq_api_key="gsk_fake_but_stubbed",
        )
    finally:
        cs._Groq = orig_groq

    # The retry must produce a real reply and keep the executed tool.
    _assert_eq(result.get("reply"), "I set your meal target to 7!", "summary reply from no-tools retry")
    _assert_eq(result.get("_fallback"), False, "not fallback")
    results = result.get("tool_results", [])
    _assert_eq(len(results), 1, "one tool executed")
    _assert_eq(results[0]["tool"], "set_target_meals", "tool name")
    _assert_eq(results[0]["status"], "ok", "tool succeeded")

    # Protocol assertions: the failed summary call passed tools, the
    # retry must NOT include tools (that's the fix).
    creates = fake_client.chat.completions.creates
    _assert_eq(len(creates), 3, "three create calls (tool turn + failed summary + retry)")
    _assert_eq(creates[0].get("tool_choice"), "auto", "first call allows tools")
    _assert_eq(creates[1].get("tool_choice"), "none", "summary call attempts tool_choice=none")
    _assert_eq(creates[2].get("tools"), None, "retry omits tools entirely")
    _assert_eq(creates[2].get("tool_choice"), None, "retry omits tool_choice")


def test_chat_multi_intent_end_to_end():
    """Multi-intent prompt executes BOTH tools, persists rows to rung.db, and
    returns the structured actions payload the UI chips render from."""
    _setup()
    import services.copilot_service as cs

    class FakeToolCall:
        def __init__(self, name, arguments, cid):
            self.function = type("F", (), {"name": name, "arguments": arguments})()
            self.id = cid

    class FakeMsg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class FakeResponse:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    class FakeCompletions:
        def __init__(self, calls):
            self._calls = list(calls)

        def create(self, **kwargs):
            return FakeResponse(FakeMsg(*self._calls.pop(0)))

    class FakeGroq:
        def __init__(self, api_key):
            completions = FakeCompletions([
                # turn 1: two parallel tool calls (bill + grocery)
                (None, [
                    FakeToolCall("add_recurring_bill", '{"name": "Netflix", "amount": 22.99}', "call_bill"),
                    FakeToolCall("add_grocery_item", '{"item_name": "laundry detergent"}', "call_grocery"),
                ]),
                # turn 2: friendly summary
                ("Done! I added Netflix to your bills and laundry detergent to your grocery list.", None),
            ])
            self.chat = type("C", (), {"completions": completions})()

    orig_groq = cs._Groq
    cs._Groq = FakeGroq
    os.environ["GROQ_API_KEY"] = "gsk_test_multi_intent"
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content":
                "Add Netflix for $22.99 to my bills and put laundry detergent on my grocery list."}]},
        )
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._Groq = orig_groq

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}

    # ---- Structured reply payload (rendered as chat bubbles) ----
    _assert_truthy(d.get("reply"), "assistant reply present")
    _assert_truthy("Netflix" in d.get("reply", ""), "reply mentions the bill")
    _assert_truthy("laundry detergent" in d.get("reply", "").lower(), "reply mentions the grocery item")

    # ---- Tool results captured (source of action chips) ----
    tr = d.get("tool_results", [])
    _assert_eq(len(tr), 2, "both tools executed")
    tools = {t.get("tool") for t in tr}
    _assert_eq(tools, {"add_recurring_bill", "add_grocery_item"}, "exactly the two requested tools")

    # ---- Structured actions_taken (drives the ✅/🛒 chips) ----
    a = d.get("actions_taken", {})
    bills = a.get("bills_added", [])
    _assert_eq(len(bills), 1, "one bill reported")
    _assert_eq(bills[0]["name"], "Netflix", "bill name in action chip")
    _assert_eq(bills[0]["amount"], 22.99, "bill amount in action chip")
    groceries = a.get("grocery_items_added", [])
    _assert_eq(len(groceries), 1, "one grocery item reported")
    _assert_eq(groceries[0], "laundry detergent", "grocery item in action chip")

    # ---- DB rows persisted in rung.db (UI refresh will pick them up) ----
    with app.app_context():
        _assert_eq(Bill.query.count(), 1, "bill persisted")
        b = Bill.query.first()
        _assert_eq(b.name, "Netflix", "bill name in DB")
        _assert_eq(b.amount, 22.99, "bill amount in DB")
        _assert_eq(GroceryItem.query.count(), 1, "grocery item persisted")
        gi = GroceryItem.query.first()
        _assert_eq(gi.item_name.lower(), "laundry detergent", "grocery item in DB")


def test_execute_intent_payload_directly_handles_groceries_bills_and_expenses():
    """The intent pipeline should persist groceries, bills, and one-time expenses."""
    _setup()
    with app.app_context():
        payload = ci.CopilotIntentPayload(
            groceries=[ci.GroceryAddition(item_name="dish soap")],
            bill_adjustments=[ci.BillAdjustment(bill_name="Internet", amount=59.99)],
            expenses=[ci.OneTimeExpense(category="coffee", estimated_amount=4.50)],
        )
        actions = ci.execute_intent_payload(payload)

        _assert_eq(actions["grocery_items_added"], ["dish soap"], "grocery item added")
        _assert_eq(actions["bills_added"][0]["name"], "Internet", "bill added")
        _assert_eq(actions["bills_added"][0]["amount"], 59.99, "bill amount stored")
        _assert_eq(actions["expenses_logged"][0]["amount"], 4.5, "expense amount logged")

        _assert_eq(GroceryItem.query.count(), 1, "grocery item persisted")
        _assert_eq(Bill.query.count(), 1, "bill persisted")
        _assert_eq(ExpenseTransaction.query.count(), 1, "expense persisted")


def test_process_copilot_command_orchestrates_parsing_and_intent_execution():
    """process_copilot_command should use parsed results to execute the intent payload."""
    _setup()
    with app.app_context():
        original_parse = cs.parse_copilot_prompt
        cs.parse_copilot_prompt = lambda text, groq_api_key="": {
            "grocery_additions": ["paper towels"],
            "bill_updates": [{"name": "Hulu", "amount": 14.99, "action": "add"}],
            "discretionary_events": [{"description": "movie tickets", "amount": 20.0}],
            "selected_recipes": [],
            "target_meals": None,
            "tool_results": [],
        }
        try:
            result = ci.process_copilot_command("Add Hulu and paper towels", groq_api_key="")
        finally:
            cs.parse_copilot_prompt = original_parse

        _assert_truthy(result.get("actions_taken"), "actions_taken is present")
        actions = result["actions_taken"]
        _assert_eq(actions["grocery_items_added"], ["paper towels"], "grocery item executed")
        _assert_eq(actions["bills_added"][0]["name"], "Hulu", "bill executed")
        _assert_eq(actions["expenses_logged"][0]["description"], "movie tickets", "expense executed")
        _assert_eq(GroceryItem.query.count(), 1, "grocery item persisted via orchestrator")
        _assert_eq(Bill.query.count(), 1, "bill persisted via orchestrator")
        _assert_eq(ExpenseTransaction.query.count(), 1, "expense persisted via orchestrator")


def test_chat_endpoint_multi_turn_plain_text_executes_intent_pipeline():
    """Multi-turn chat should still execute intent actions when the model replies in plain text."""
    _setup()

    class FakeMsg:
        def __init__(self, content):
            self.content = content
            self.tool_calls = []

    class FakeResponse:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeResponse(FakeMsg("Sure, I added those items for you."))

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeGroq:
        def __init__(self, api_key):
            self.chat = FakeChat()

    orig_groq = cs._Groq
    orig_json = cs._call_groq_json
    cs._Groq = FakeGroq
    cs._call_groq_json = lambda prompt, api_key="": {
        "tool_results": [],
        "selected_recipes": [],
        "grocery_additions": ["paper towels"],
        "discretionary_events": [],
        "bill_updates": [{"name": "netflix", "amount": 22.99, "action": "add"}],
        "target_meals": None,
        "_fallback": False,
    }
    os.environ["GROQ_API_KEY"] = "gsk_test_multi_turn_plain_text"
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={
                "messages": [
                    {"role": "assistant", "content": "How can I help?"},
                    {"role": "user", "content": "Add Netflix $22.99/mo and paper towels"},
                ]
            },
        )
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._Groq = orig_groq
        cs._call_groq_json = orig_json

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    _assert_eq(d.get("_fallback"), False, "not fallback")
    _assert_eq(d.get("tool_results", []), [], "no native tool_results")
    actions = d.get("actions_taken", {})
    _assert_eq(len(actions.get("bills_added", [])), 1, "bill action executed")
    _assert_eq(len(actions.get("grocery_items_added", [])), 1, "grocery action executed")
    _assert_eq(actions.get("grocery_items_added", [])[0], "paper towels", "grocery item persisted")
    with app.app_context():
        _assert_eq(Bill.query.count(), 1, "bill persisted")
        _assert_eq(GroceryItem.query.count(), 1, "grocery item persisted")


def test_confirm_endpoint_applies_pending_risky_actions():
    """When chat returns plain text and parser marks a high-value bill,
    the chat response should indicate confirmation is required, and the
    `/api/copilot/confirm` endpoint should apply the pending actions.
    """
    _setup()
    import services.copilot_service as cs

    # Fake chat result: plain-text reply but parsed bill_updates with high amount
    def fake_chat(messages, groq_api_key=""):
        return {
            "reply": "OK, I noted that for you.",
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "bill_updates": [{"name": "Premium Service", "amount": 120.0, "action": "add"}],
            "target_meals": None,
            "_fallback": False,
        }

    orig_chat = cs.chat_copilot_prompt
    cs.chat_copilot_prompt = fake_chat
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content": "Add Premium Service $120"}]},
        )
    finally:
        cs.chat_copilot_prompt = orig_chat

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    actions = d.get("actions_taken", {})
    _assert_eq(actions.get("requires_confirmation", True), True, "requires confirmation flagged")
    pending = actions.get("pending_actions", {})
    _assert_truthy(pending.get("bills"), "pending bills present")

    # Now call confirm endpoint to persist the pending bill
    resp2 = client.post("/api/copilot/confirm", json={"text": "Add Premium Service $120"})
    _assert_eq(resp2.status_code, 200, "confirm returns 200")
    d2 = resp2.get_json() or {}
    actions2 = d2.get("actions_taken", {})
    _assert_truthy(actions2.get("bills_added"), "bill added on confirm")
    undo_token = d2.get("actions_taken", {}).get("undo_token")
    _assert_truthy(undo_token, "undo token returned")
    with app.app_context():
        _assert_eq(Bill.query.count(), 1, "bill persisted after confirm")

    resp3 = client.post("/api/copilot/undo", json={"undo_token": undo_token})
    _assert_eq(resp3.status_code, 200, "undo returns 200")
    d3 = resp3.get_json() or {}
    _assert_truthy(d3.get("undone_actions", {}), "undo actions present")
    with app.app_context():
        _assert_eq(Bill.query.count(), 0, "bill removed after undo")


def test_confirm_endpoint_records_action_audit_user_id():
    """Confirmed actions should record the requesting user id in the audit log."""
    _setup()
    import services.copilot_service as cs

    def fake_chat(messages, groq_api_key=""):
        return {
            "reply": "OK, I noted that for you.",
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "bill_updates": [{"name": "Premium Service", "amount": 120.0, "action": "add"}],
            "target_meals": None,
            "_fallback": False,
        }

    orig_chat = cs.chat_copilot_prompt
    cs.chat_copilot_prompt = fake_chat
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content": "Add Premium Service $120"}]},
            headers={"X-User-Id": "tester-123"},
        )
    finally:
        cs.chat_copilot_prompt = orig_chat

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    _assert_truthy(d.get("confirmation_prompt"), "confirmation prompt returned")
    actions = d.get("actions_taken", {})
    _assert_eq(actions.get("requires_confirmation", True), True, "requires confirmation flagged")

    resp2 = client.post(
        "/api/copilot/confirm",
        json={"text": "Add Premium Service $120"},
        headers={"X-User-Id": "tester-123"},
    )
    _assert_eq(resp2.status_code, 200, "confirm returns 200")
    d2 = resp2.get_json() or {}
    undo_token = d2.get("actions_taken", {}).get("undo_token")
    _assert_truthy(undo_token, "undo token returned")

    with app.app_context():
        audit = ActionAudit.query.filter_by(undo_token=undo_token).first()
        _assert_truthy(audit, "audit row exists")
        _assert_eq(audit.user_id, "tester-123", "audit row records header user_id")


def test_confirm_endpoint_records_action_audit_body_user_id():
    """Confirmed actions should record JSON payload user_id in the audit log."""
    _setup()
    import services.copilot_service as cs

    def fake_chat(messages, groq_api_key=""):
        return {
            "reply": "OK, I noted that for you.",
            "tool_results": [],
            "selected_recipes": [],
            "grocery_additions": [],
            "discretionary_events": [],
            "bill_updates": [{"name": "Premium Service", "amount": 120.0, "action": "add"}],
            "target_meals": None,
            "_fallback": False,
        }

    orig_chat = cs.chat_copilot_prompt
    cs.chat_copilot_prompt = fake_chat
    try:
        resp = client.post(
            "/api/copilot/chat",
            json={"messages": [{"role": "user", "content": "Add Premium Service $120"}], "user_id": "payload-456"},
        )
    finally:
        cs.chat_copilot_prompt = orig_chat

    _assert_eq(resp.status_code, 200, "chat returns 200")
    d = resp.get_json() or {}
    _assert_truthy(d.get("confirmation_prompt"), "confirmation prompt returned")

    resp2 = client.post(
        "/api/copilot/confirm",
        json={"text": "Add Premium Service $120", "user_id": "payload-456"},
    )
    _assert_eq(resp2.status_code, 200, "confirm returns 200")
    d2 = resp2.get_json() or {}
    undo_token = d2.get("actions_taken", {}).get("undo_token")
    _assert_truthy(undo_token, "undo token returned")

    with app.app_context():
        audit = ActionAudit.query.filter_by(undo_token=undo_token).first()
        _assert_truthy(audit, "audit row exists")
        _assert_eq(audit.user_id, "payload-456", "audit row records body user_id")


def test_copilot_stage_endpoint_is_dry_run_and_non_mutating():
    """/api/copilot/stage should build a proposal without writing to DB."""
    _setup()
    _seed_recipes()

    orig_json = cs._call_groq_json
    cs._call_groq_json = lambda prompt, api_key="": {
        "tool_results": [],
        "selected_recipes": [{"title": "chicken rice bowl", "action": "add"}],
        "grocery_additions": ["paper towels"],
        "discretionary_events": [{"description": "gas", "amount": 40}],
        "bill_updates": [{"name": "Internet", "amount": 65, "action": "set"}],
        "target_meals": 3,
        "meal_servings": 4,
        "_fallback": False,
    }
    os.environ["GROQ_API_KEY"] = "gsk_test_stage"
    try:
        resp = client.post("/api/copilot/stage", json={"text": "Plan 3 meals and add internet + gas + paper towels"})
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._call_groq_json = orig_json

    _assert_eq(resp.status_code, 200, "stage returns 200")
    data = resp.get_json() or {}
    actions = data.get("actions_taken", {})
    _assert_eq(actions.get("staged", False), True, "staged flag set")
    _assert_eq(actions.get("requires_confirmation", False), True, "requires confirmation set")
    _assert_truthy(actions.get("recipes_added") or actions.get("recipes_auto_filled"), "staged recipe proposal exists")
    _assert_truthy(actions.get("grocery_items_added"), "staged grocery additions exist")
    _assert_truthy(actions.get("expenses_logged"), "staged expenses exist")
    _assert_truthy(actions.get("bills_added") or actions.get("bills_updated"), "staged bill changes exist")

    with app.app_context():
        _assert_eq(MealPlanItem.query.count(), 0, "dry-run should not add meal plan items")
        _assert_eq(GroceryItem.query.count(), 0, "dry-run should not add grocery items")
        _assert_eq(ExpenseTransaction.query.count(), 0, "dry-run should not add expenses")
        _assert_eq(Bill.query.count(), 0, "dry-run should not add bills")


def test_copilot_apply_endpoint_persists_reviewed_staged_actions():
    """/api/copilot/apply should persist edited staged actions and return undo token."""
    _setup()
    _seed_recipes()

    orig_json = cs._call_groq_json
    cs._call_groq_json = lambda prompt, api_key="": {
        "tool_results": [],
        "selected_recipes": [{"title": "chicken rice bowl", "action": "add"}],
        "grocery_additions": ["paper towels"],
        "discretionary_events": [{"description": "gas", "amount": 40}],
        "bill_updates": [{"name": "Internet", "amount": 65, "action": "set"}],
        "target_meals": 2,
        "meal_servings": 4,
        "_fallback": False,
    }
    os.environ["GROQ_API_KEY"] = "gsk_test_apply"
    try:
        stage_resp = client.post("/api/copilot/stage", json={"text": "Plan meals and add internet"})
    finally:
        os.environ.pop("GROQ_API_KEY", None)
        cs._call_groq_json = orig_json

    _assert_eq(stage_resp.status_code, 200, "stage returns 200")
    staged = (stage_resp.get_json() or {}).get("actions_taken") or {}

    # Simulate user review edits before apply.
    if staged.get("expenses_logged"):
        staged["expenses_logged"][0]["amount"] = 25.0
    staged["grocery_items_added"].append({"item_name": "dish soap", "estimated_price": 4.25})

    apply_resp = client.post(
        "/api/copilot/apply",
        json={"text": "Plan meals and add internet", "staged_actions": staged},
    )
    _assert_eq(apply_resp.status_code, 200, "apply returns 200")

    applied = (apply_resp.get_json() or {}).get("actions_taken", {})
    _assert_truthy(applied.get("undo_token"), "undo token returned")
    _assert_truthy(applied.get("recipes_added") or applied.get("recipes_auto_filled"), "recipe actions applied")
    _assert_truthy(applied.get("grocery_items_added"), "grocery actions applied")
    _assert_truthy(applied.get("bills_added") or applied.get("bills_updated"), "bill actions applied")

    with app.app_context():
        _assert_truthy(MealPlanItem.query.count() >= 1, "meal plan persisted")
        _assert_truthy(GroceryItem.query.count() >= 1, "grocery persisted")
        _assert_truthy(ExpenseTransaction.query.count() >= 1, "expense persisted")
        _assert_truthy(Bill.query.count() >= 1, "bill persisted")


# ---------------------------------------------------------------------------
# 6 — Bill removal (placeholder — regex fallback won't catch this)
# ---------------------------------------------------------------------------


def test_copilot_bill_removal():
    """Ensure the remove action path doesn't crash."""
    _setup()
    from datetime import datetime, timedelta

    with app.app_context():
        b = Bill(household_id=current_household_id(), name="Hulu", amount=15.99, due_date=datetime.utcnow() + timedelta(days=10))
        db.session.add(b)
        db.session.commit()

    resp = client.post("/api/copilot/parse", json={"text": "Remove Hulu"})
    _assert_eq(resp.status_code, 200)
    d = resp.get_json() or {}
    _assert_truthy("actions_taken" in d, "response has actions_taken")


# =============================================================================
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
