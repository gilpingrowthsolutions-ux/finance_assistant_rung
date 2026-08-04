"""Integration tests for the AI Copilot parser and dispatch endpoint."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so db.drop_all()/create_all() can never wipe rung_finance.db.
os.environ["RUNG_DB_PATH"] = ":memory:"

from app import app, db, Account, Bill, ExpenseTransaction, GroceryItem

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
        db.session.add(Account(checking_balance=1250.00))
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
    """/api/copilot/parse returns the llm_error field for the frontend."""
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
    _assert_truthy("401" in d.get("llm_error", ""), "llm_error mentions 401")


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
    _assert_truthy("401" in d.get("llm_error", ""), "llm_error mentions 401")
    items = d.get("actions_taken", {}).get("grocery_items_added", [])
    _assert_eq(len(items), 1, "grocery item still parsed via regex")


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


# ---------------------------------------------------------------------------
# 6 — Bill removal (placeholder — regex fallback won't catch this)
# ---------------------------------------------------------------------------


def test_copilot_bill_removal():
    """Ensure the remove action path doesn't crash."""
    _setup()
    from datetime import datetime, timedelta

    with app.app_context():
        b = Bill(name="Hulu", amount=15.99, due_date=datetime.utcnow() + timedelta(days=10))
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
