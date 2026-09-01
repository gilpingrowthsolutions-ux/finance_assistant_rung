# pyright: reportUnusedFunction=false
import json
import logging
import os
import re
import secrets
import uuid
import hashlib
import hmac
import requests
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
import click
from flask import Flask, render_template, request, jsonify, session
from flask_migrate import Migrate
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.security import check_password_hash, generate_password_hash

def _load_local_env_files() -> None:
    """Load local dotenv files using explicit project paths.

    This keeps development/beta startup independent from shell `source` quirks,
    while preserving fail-closed safety by never overriding explicitly supplied
    environment variables.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    primary = os.path.join(root, ".env")
    local = os.path.join(root, ".env.local")
    load_dotenv(dotenv_path=primary, override=False)
    if os.path.exists(local):
        load_dotenv(dotenv_path=local, override=False)


# Load environment variables before anything else. Explicit process-level env
# always wins because override=False.
_load_local_env_files()

# Import the shared SQLAlchemy extension (not yet bound to any app)
from extensions import db

# Live retail API resolvers (Kroger + RapidAPI + local price cache)
from services.store_api import resolve_terms, pick_best
from services.kroger_api import find_nearest_kroger
from services.rapidapi_search import search_local_product, rapid_result_to_product_dict
import services.copilot_service as _copilot_service
from services.copilot_service import parse_copilot_prompt, chat_copilot_prompt
from services.usage_meter import (
    check_optional_operation,
    estimate_usage_cost,
    get_usage_controls,
    get_usage_rates,
    record_usage_event,
    set_usage_controls,
    set_usage_rates,
    summarize_usage,
)
from services.household_context import (
    HouseholdResolutionError,
    ensure_legacy_household,
    household_id as current_household_id,
    household_scope_key,
    resolve_household_context,
)
from services.auth_session import (
    AuthRequiredError,
    auth_required_mode,
    clear_login_failures,
    clear_session,
    establish_session,
    get_current_principal,
    header_override_allowed,
    login_is_blocked,
    record_login_failure,
    runtime_env,
)
from services.financial_state import (
    FinancialStateError,
    apply_balance_delta,
    get_household_account,
    set_balance_absolute,
)
from services.transaction_deletion import (
    PROTECTED_MESSAGE,
    delete_transaction_once,
    transaction_delete_eligibility,
)
from services.selected_store import get_selected_store, select_store
from services.recipe_ingredients import coerce_recipe_ingredient

app = Flask(__name__)
LOGGER = logging.getLogger("app")


def _resolve_secret_key() -> str:
    configured = str(os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "").strip()
    if configured:
        return configured
    if bool(getattr(app, "testing", False)):
        return "rung-test-secret-key"
    return secrets.token_urlsafe(48)


def _cookie_secure_enabled() -> bool:
    secure_raw = str(os.environ.get("SESSION_COOKIE_SECURE") or "").strip().lower()
    https_raw = str(os.environ.get("RUNG_HTTPS_ONLY") or "").strip().lower()
    return secure_raw in {"1", "true", "yes", "on"} or https_raw in {"1", "true", "yes", "on"}


def _resolve_database_uri() -> str:
    # Keep explicit SQLite override for isolated local tests/dev workflows.
    db_path = str(os.environ.get("RUNG_DB_PATH") or "").strip()
    if db_path:
        if db_path == ":memory:":
            return "sqlite:///:memory:"
        return f"sqlite:///{db_path}"

    raw_database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if raw_database_url:
        # SQLAlchemy 2 expects postgresql:// rather than legacy postgres://.
        if raw_database_url.startswith("postgres://"):
            raw_database_url = "postgresql://" + raw_database_url[len("postgres://"):]
        return raw_database_url

    # Backward-compatible default for local single-node runtime.
    local_default = os.path.join(os.path.dirname(__file__), "rung_finance.db")
    return f"sqlite:///{local_default}"


def _redacted_database_uri(uri: str) -> str:
    try:
        return make_url(uri).render_as_string(hide_password=True)
    except Exception:
        return "<invalid-database-uri>"


def _positive_int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 1000) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _resolve_engine_options(database_uri: str) -> dict[str, Any]:
    """Return small, bounded connection settings for the configured database.

    SQLite keeps SQLAlchemy's dialect-specific pooling behavior. PostgreSQL
    receives a conservative per-process pool suitable for the friends-and-
    family beta; hosting may tune it without changing application code.
    """
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": _positive_int_env("RUNG_DB_POOL_RECYCLE_SECONDS", 1800, maximum=86400),
    }
    try:
        driver = make_url(database_uri).drivername
    except Exception:
        return options
    if driver.startswith("postgresql"):
        statement_timeout_ms = _positive_int_env(
            "RUNG_DB_STATEMENT_TIMEOUT_MS", 30000, minimum=1000, maximum=300000
        )
        options.update({
            "pool_size": _positive_int_env("RUNG_DB_POOL_SIZE", 5, maximum=50),
            "max_overflow": _positive_int_env("RUNG_DB_MAX_OVERFLOW", 5, minimum=0, maximum=50),
            "pool_timeout": _positive_int_env("RUNG_DB_POOL_TIMEOUT_SECONDS", 10, maximum=120),
            "connect_args": {"options": f"-c statement_timeout={statement_timeout_ms}"},
        })
    return options


app.config["SQLALCHEMY_DATABASE_URI"] = _resolve_database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = _resolve_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _cookie_secure_enabled()
app.config["SESSION_COOKIE_NAME"] = "rung_session"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _resolve_engine_options(app.config["SQLALCHEMY_DATABASE_URI"])

# Initialize the shared SQLAlchemy instance with this Flask app.
# This must happen BEFORE any model definitions.
db.init_app(app)
migrate = Migrate(app, db, compare_type=True)


@app.errorhandler(HouseholdResolutionError)
def handle_household_resolution_error(exc: HouseholdResolutionError):
    message = str(exc)
    if "Authentication required" in message:
        return jsonify({"error": message}), 401
    return jsonify({"error": message}), 403


@app.errorhandler(AuthRequiredError)
def handle_auth_required_error(exc: AuthRequiredError):
    return jsonify({"error": str(exc)}), 401

from services.copilot_intent import (
    StagedActionValidationError,
    apply_staged_actions,
    execute_intent_payload,
    parse_intent_payload,
    stage_intent_payload,
)

# Import all ORM models from the authoritative models module
# This avoids circular imports: models import from extensions,
# app imports from models, services import from models/app.
from models import (
    Account,
    ActionAudit,
    BetaFeedback,
    Bill,
    BrandPreference,
    ExpenseTransaction,
    GroceryItem,
    MealPlanItem,
    PantryItem,
    RapidPriceCache,
    Recipe,
    RecipeIngredient,
    RetailProduct,
    RetailProductCache,
    RetailProductPreference,
    RetailRefreshLease,
    RetailSearchCache,
    RetailStoreIdentity,
    RetailProductSubstitution,
    PlaidAccount,
    PlaidItem,
    PlaidTransaction,
    ShoppingTripCompletion,
    ShoppingCart,
    ShoppingCartLine,
    ShoppingStoreChangeReview,
    ShoppingRebalanceProposal,
    TransactionReconciliation,
    StorePriceCache,
    StoreProductObservation,
    HouseholdShoppingDefault,
    UserPreference,
    UserSetting,
    User,
    HouseholdMembership,
    LoginThrottle,
    SavingsAllocationRun,
    SavingsDestination,
    SavingsGoal,
    SavingsReserve,
    SavingsTransfer,
    BehaviorIntelligenceDecision,
    IncomePlanVersion,
    UsageEvent,
    Household,
    StoreTaxProfile,
    TaxBoundaryAssignment,
    TaxJurisdiction,
    TaxRate,
    TaxSourceDataset,
    TaxabilityRule,
    RetailProductTaxClass,
)
from services.recipe_access import (
    mutable_private_recipe_by_id,
    visible_recipe_by_id,
    visible_recipe_query,
)
from services.meal_plan import (
    current_plan_query,
    historical_plan_query,
    new_plan_item,
    resolve_current_cycle,
)
from services.savings_allocation import (
    SavingsError,
    allocation_plan as savings_allocation_plan,
    apply_allocation as apply_savings_allocation,
    create_goal as create_savings_goal,
    create_reserve as create_savings_reserve,
    list_state as savings_state,
    match_reserve_purpose,
    transfer as savings_transfer,
    update_goal as update_savings_goal,
    update_reserve as update_savings_reserve,
)
from services.paycheck_timeline import build_paycheck_timeline, resolve_cycle
from services.payday_recap import build_payday_recap
from services.behavior_intelligence import build_behavior_intelligence
from services.income_plan import (
    IncomePlanError,
    income_plan_payload,
    record_income_plan,
    resolve_income_plan,
)
from services.tax_engine import (
    TAX_CLASS_GENERAL_MERCHANDISE,
    TAX_CLASS_GROCERY_FOOD,
    canonical_tax_decision,
    cents_to_float as tax_cents_to_float,
    ensure_bootstrap_tax_dataset,
    has_paid_provider_tax_keys,
    resolve_store_tax_profile,
)
from services.plaid_foundation import (
    PlaidFoundationError,
    create_link_token,
    exchange_public_token_and_persist,
    get_plaid_connection_status,
    sync_plaid_transactions,
)
from services.transaction_reconciliation import (
    decide_reconciliation_pair,
    list_reconciliation_proposals,
    project_plaid_transactions,
)
from services.household_defaults import (
    HOUSEHOLD_DEFAULT_ALLOWED_VALUES,
    SHOPPING_STYLE_ALLOWED_VALUES,
    household_defaults_schema,
)

# =============================================================================
# DATABASE MODELS IMPORTED ABOVE
# =============================================================================
# All model classes are imported from models.py
# See models.py for their authoritative definitions


DEFAULT_STARTER_RECIPE_TITLES = [
    "Chicken Rice Bowl",
    "Ground Beef Tacos",
    "Vegetable Stir Fry",
    "Margherita Pizza",
    "Greek Salad",
    "Mushroom Risotto",
    "Thai Green Curry",
]

DEFAULT_STARTER_GROCERY_ITEMS = [
    "rice",
    "tortillas",
    "pasta",
    "tomatoes",
    "chicken breast",
    "ground beef",
    "olive oil",
]

HOUSEHOLD_DEFAULT_OWNER_SCOPE = "household:default"
HOUSEHOLD_DEFAULT_KIND_CATEGORY = "category_default"
HOUSEHOLD_DEFAULT_KIND_STYLE = "shopping_style"
HOUSEHOLD_STYLE_KEY = "shopping_style"
SAFE_BUFFER_SETTING_KEY = "safe_to_spend_buffer_usd"
PYF_TARGET_SETTING_KEY = "pyf_long_term_target_percent"
LOCATION_SHARING_SETTING_KEY = "location_sharing_enabled"
NEXT_PAYDAY_SETTING_KEY = "next_payday_date"
REQUIRED_EXPENSE_REVIEW_SETTING_KEY = "onboarding_required_expense_review"
REQUIRED_EXPENSE_UNANSWERED = "unanswered"
REQUIRED_EXPENSE_NONE = "no_expenses_reviewed"
REQUIRED_EXPENSE_PENDING = "has_expenses_pending_review"
REQUIRED_EXPENSE_REVIEWED = "has_expenses_reviewed"
APP_SCHEMA_VERSION = "m10-beta-readiness-1"


def get_setting(key: str, default: str = '') -> str:
    """Read a user setting from the DB, returning *default* if not found."""
    row = UserSetting.query.filter_by(
        household_id=current_household_id(),
        key=key,
    ).first()
    return row.value if row else default


def _required_expense_review_state() -> str:
    """Return the explicit, household-scoped onboarding expense-review state."""
    state = get_setting(REQUIRED_EXPENSE_REVIEW_SETTING_KEY, REQUIRED_EXPENSE_UNANSWERED)
    return state if state in {
        REQUIRED_EXPENSE_UNANSWERED, REQUIRED_EXPENSE_NONE,
        REQUIRED_EXPENSE_PENDING, REQUIRED_EXPENSE_REVIEWED,
    } else REQUIRED_EXPENSE_UNANSWERED


def _resolve_request_user_id(data: Any) -> str:
    principal = get_current_principal()
    if principal is not None:
        return f"user:{principal.user_id}"
    if isinstance(data, dict):
        explicit = str(data.get("user_id") or "").strip()
        if explicit:
            return explicit
    return household_scope_key()


def _copilot_stage_binding(operation_id: str) -> str:
    """Bind a browser-held Copilot draft to its current household.

    Staged Copilot actions deliberately remain editable review data rather
    than authoritative domain records.  The review payload still needs a
    server-verifiable household binding, however: copying a Household A draft
    into Household B must not turn it into a new B operation.  This signature
    carries no financial authority and intentionally excludes editable review
    fields; apply continues to validate those fields canonically.
    """
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        return ""
    message = f"copilot-stage-v1:{current_household_id()}:{operation_id}".encode("utf-8")
    secret = str(app.config["SECRET_KEY"]).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _copilot_stage_binding_valid(staged_actions: dict[str, Any]) -> bool:
    operation_id = str(staged_actions.get("operation_id") or "").strip()
    supplied = str(staged_actions.get("operation_binding") or "").strip()
    expected = _copilot_stage_binding(operation_id)
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _current_auth_session_payload() -> dict[str, Any]:
    principal = get_current_principal()
    if principal is None:
        return {
            "authenticated": False,
            "auth_required": bool(auth_required_mode()),
            "user": None,
            "household": None,
        }
    return {
        "authenticated": True,
        "auth_required": bool(auth_required_mode()),
        "user": {
            "id": principal.user_id,
            "email": principal.email,
            "role": principal.role,
        },
        "household": {
            "id": principal.household_id,
        },
    }


def _household_account(create_if_missing: bool = True) -> Account:
    return get_household_account(current_household_id(), create_if_missing=create_if_missing)


def _household_bill_query():
    return Bill.query.filter_by(household_id=current_household_id())


def _household_tx_query():
    return ExpenseTransaction.query.filter_by(household_id=current_household_id())


def _household_grocery_query():
    return GroceryItem.query.filter_by(household_id=current_household_id())


def _current_meal_plan_cycle():
    """The sole current-plan authority, based on canonical income schedule."""
    account = _household_account()
    return resolve_current_cycle(
        account=account, next_income=_infer_next_income(account, datetime.now(timezone.utc)),
    )


def _household_meal_plan_query():
    """Current plan only.  Historical rows require the explicit helper below."""
    return current_plan_query(current_household_id(), _current_meal_plan_cycle())


def _household_historical_meal_plan_query():
    return historical_plan_query(current_household_id())


def _new_current_meal_plan_item(recipe_id: int, source: str) -> MealPlanItem:
    return new_plan_item(
        household_id=current_household_id(), recipe_id=recipe_id, source=source,
        cycle=_current_meal_plan_cycle(),
    )


def _household_pantry_query():
    return PantryItem.query.filter_by(household_id=current_household_id())


def _household_brand_pref_query():
    return BrandPreference.query.filter_by(household_id=current_household_id())


def _household_trip_query():
    return ShoppingTripCompletion.query.filter_by(household_id=current_household_id())


def _household_audit_query():
    return ActionAudit.query.filter_by(household_id=current_household_id())


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _db_path_from_uri(uri: str) -> str:
    prefix = "sqlite:///"
    if not str(uri or "").startswith(prefix):
        return ""
    return str(uri)[len(prefix):]


def _classify_db_path(path: str) -> str:
    raw = str(path or "").strip()
    if raw == ":memory:" or raw.endswith(":memory:"):
        return "test"

    normalized = os.path.abspath(raw) if raw else ""
    repo_db = os.path.abspath(os.path.join(os.path.dirname(__file__), "rung_finance.db"))
    if normalized == repo_db:
        return "production"
    if normalized.startswith("/tmp/"):
        return "disposable"
    return "custom"


def _plaid_runtime_enabled() -> bool:
    if _env_flag("PLAID_ENABLED", False):
        return True
    has_client = bool(str(os.environ.get("PLAID_CLIENT_ID") or "").strip())
    has_secret = bool(str(os.environ.get("PLAID_SECRET") or "").strip())
    return has_client and has_secret


def _runtime_capabilities() -> dict[str, Any]:
    plaid_enabled = _plaid_runtime_enabled()
    plaid_key = bool(str(os.environ.get("PLAID_TOKEN_ENCRYPTION_KEY") or "").strip())
    plaid_env = str(os.environ.get("PLAID_ENV") or "sandbox").strip().lower() or "sandbox"

    llm_server = bool(str(os.environ.get("GROQ_API_KEY") or "").strip())
    kroger_cfg = bool(str(os.environ.get("KROGER_CLIENT_ID") or "").strip() and str(os.environ.get("KROGER_CLIENT_SECRET") or "").strip())
    walmart_cfg = bool(str(os.environ.get("SERPAPI_API_KEY") or "").strip())

    return {
        "plaid": {
            "enabled": plaid_enabled,
            "configured": plaid_enabled and plaid_key,
            "env": plaid_env,
            "status": (
                "available"
                if plaid_enabled and plaid_key
                else ("disabled" if not plaid_enabled else "misconfigured")
            ),
            "message": (
                "Bank sync available"
                if plaid_enabled and plaid_key
                else ("Bank sync unavailable" if not plaid_enabled else "Bank sync unavailable: missing token encryption key")
            ),
        },
        "llm": {
            "server_configured": llm_server,
            "status": "available" if llm_server else "unavailable",
            "message": "LLM fallback available" if llm_server else "LLM fallback unavailable",
        },
        "copilot_deterministic": {
            "status": "available",
            "message": "Copilot deterministic commands available",
        },
        "retail": {
            "walmart_live": {
                "configured": walmart_cfg,
                "status": "available" if walmart_cfg else "unavailable",
                "message": "Live Walmart pricing available" if walmart_cfg else "Live Walmart pricing unavailable",
            },
            "kroger_live": {
                "configured": kroger_cfg,
                "status": "available" if kroger_cfg else "unavailable",
                "message": "Kroger pricing available" if kroger_cfg else "Kroger pricing unavailable",
            },
        },
    }


def _validate_startup_configuration() -> None:
    uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    try:
        parsed = make_url(uri)
    except Exception as exc:
        raise RuntimeError("Invalid SQLALCHEMY_DATABASE_URI / DATABASE_URL configuration.") from exc

    if parsed.drivername not in {"sqlite", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}:
        raise RuntimeError("Unsupported database driver. Use sqlite or postgresql.")

    if parsed.drivername == "sqlite" and not _db_path_from_uri(uri):
        raise RuntimeError("SQLite database path is missing.")

    mode = runtime_env()
    if mode in {"production", "beta"}:
        if _env_flag("FLASK_DEBUG", False) or bool(getattr(app, "debug", False)):
            raise RuntimeError("Debug mode must be disabled in production/beta mode.")
        if bool(getattr(app, "testing", False)):
            raise RuntimeError("Testing mode must be disabled in production/beta mode.")
        if not str(os.environ.get("DATABASE_URL") or "").strip():
            raise RuntimeError("DATABASE_URL must be configured in production/beta mode.")
        if parsed.drivername not in {"postgresql", "postgresql+psycopg2", "postgresql+psycopg"}:
            raise RuntimeError("PostgreSQL is required in production/beta mode.")
        configured_secret = str(os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "").strip()
        weak = configured_secret.lower() in {"", "dev", "development", "changeme", "secret", "test", "default"}
        if weak or len(configured_secret) < 32:
            raise RuntimeError("A secure SECRET_KEY (>=32 chars) is required in production/beta mode.")
        if header_override_allowed():
            raise RuntimeError("Household header override must be disabled in production/beta mode.")

    if _plaid_runtime_enabled() and not bool(str(os.environ.get("PLAID_TOKEN_ENCRYPTION_KEY") or "").strip()):
        raise RuntimeError("Plaid is enabled but PLAID_TOKEN_ENCRYPTION_KEY is not configured.")


def _validate_database_connectivity() -> None:
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        safe_uri = _redacted_database_uri(str(app.config.get("SQLALCHEMY_DATABASE_URI") or ""))
        raise RuntimeError(f"Database is unreachable or misconfigured: {safe_uri}") from exc


def _build_confirmation_prompt(actions: dict) -> str:
    pieces = [
        "Please confirm these changes before I apply them:",
    ]
    if actions.get("pending_actions", {}).get("bills"):
        pieces.append(f"- Add {len(actions['pending_actions']['bills'])} bill(s)")
    if actions.get("pending_actions", {}).get("expenses"):
        pieces.append(f"- Log {len(actions['pending_actions']['expenses'])} expense(s)")
    if actions.get("grocery_list"):
        pieces.append(f"- Add {len(actions['grocery_list'])} grocery item(s)")
    if actions.get("grocery_items_added"):
        pieces.append(f"- Add {len(actions['grocery_items_added'])} grocery item(s)")
    pieces.append("Reply with 'confirm' to proceed, or 'cancel' to keep things as-is.")
    return "\n".join(pieces)


def record_action_audit(
    actions: dict,
    raw_text: str = '',
    source: str = 'copilot',
    user_id: str = 'anonymous',
    operation_id: str | None = None,
    undo_token: str | None = None,
    commit: bool = True,
) -> str:
    """Persist a lightweight JSON audit row for executed intent actions.

    Returns the generated undo token for the new audit row.
    """
    token = undo_token or secrets.token_urlsafe(24)
    row = ActionAudit(
        household_id=current_household_id(),
        source=source,
        user_id=(user_id or "anonymous")[:80],
        raw_text=(raw_text or '')[:2000],
        actions_json=json.dumps(actions),
        undo_token=token,
        operation_id=(operation_id or '').strip() or None,
    )
    db.session.add(row)
    if commit:
        db.session.commit()
    return token


def _load_audit_by_token(undo_token: str) -> ActionAudit | None:
    if not undo_token:
        return None
    return _household_audit_query().filter_by(undo_token=undo_token).first()


def _undo_actions_from_audit(audit: ActionAudit) -> dict:
    """Reverse actions recorded in an audit row.

    This is intentionally conservative: it only undoes persisted side effects
    from the confirmed action payload and does not attempt complex repair.
    """
    try:
        actions = json.loads(audit.actions_json or "{}")
    except Exception:
        actions = {}
    if not isinstance(actions, dict):
        actions = {}

    def _as_action_list(key: str) -> list:
        rows = actions.get(key, [])
        return rows if isinstance(rows, list) else []

    def _parse_iso_datetime(value: Any):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    def _normalize_legacy_grocery_row(row: Any) -> dict:
        if isinstance(row, dict):
            return {
                "id": row.get("id"),
                "item_name": row.get("item_name") or row.get("name") or "",
            }
        return {"id": None, "item_name": str(row or "")}

    def _normalize_legacy_expense_row(row: Any) -> dict:
        if not isinstance(row, dict):
            return {"id": None, "description": "", "category": "", "amount": None}
        return {
            "id": row.get("id"),
            "description": row.get("description") or "",
            "category": row.get("category") or "",
            "amount": row.get("amount"),
        }

    def _normalize_legacy_bill_row(row: Any) -> dict:
        if not isinstance(row, dict):
            return {
                "id": None,
                "name": "",
                "amount": None,
                "due_date": None,
                "is_paid": False,
                "is_gas_estimate": False,
            }
        return {
            "id": row.get("id"),
            "name": row.get("name") or "",
            "amount": row.get("amount"),
            "due_date": row.get("due_date"),
            "is_paid": bool(row.get("is_paid", False)),
            "is_gas_estimate": bool(row.get("is_gas_estimate", False)),
            "previous_amount": row.get("previous_amount"),
        }

    undone = {
        "bills_deleted": [],
        "bills_restored": [],
        "bills_reverted": [],
        "expenses_deleted": [],
        "income_deleted": [],
        "balance_reconciliations_reverted": [],
        "shopping_trip_corrections_reverted": [],
        "grocery_items_deleted": [],
        "recipes_removed": [],
        "recipes_restored": [],
    }
    account = _household_account()

    for raw_bill in _as_action_list("bills_added"):
        bill = _normalize_legacy_bill_row(raw_bill)
        bill_id = bill.get("id")
        existing = _household_bill_query().filter_by(id=bill_id).first() if bill_id else None
        if not existing and bill.get("name"):
            existing = _household_bill_query().filter(Bill.name.ilike(f"%{bill['name']}%")).first()
        if existing:
            db.session.delete(existing)
            undone["bills_deleted"].append({"id": existing.id, "name": existing.name})

    for raw_bill in _as_action_list("bills_updated"):
        bill = _normalize_legacy_bill_row(raw_bill)
        previous_amount = bill.get("previous_amount")
        if previous_amount is None:
            continue
        bill_id = bill.get("id")
        existing = _household_bill_query().filter_by(id=bill_id).first() if isinstance(bill_id, int) else None
        if existing is None and bill.get("name"):
            existing = _household_bill_query().filter(Bill.name.ilike(f"%{bill['name']}%")).first()
        if existing is None:
            continue
        existing.amount = float(previous_amount)
        db.session.add(existing)
        undone["bills_reverted"].append({"id": existing.id, "name": existing.name, "amount": existing.amount})

    for raw_bill in _as_action_list("bills_removed"):
        bill = _normalize_legacy_bill_row(raw_bill)
        bill_id = bill.get("id")
        existing = _household_bill_query().filter_by(id=bill_id).first() if isinstance(bill_id, int) else None
        if existing is not None:
            continue
        name = str(bill.get("name") or "").strip()
        amount = bill.get("amount")
        if not name or amount is None:
            continue
        due_date = _parse_iso_datetime(bill.get("due_date")) or (datetime.utcnow() + timedelta(days=14))
        restored = Bill(
            household_id=current_household_id(),
            id=bill_id if isinstance(bill_id, int) else None,
            name=name,
            amount=float(amount),
            due_date=due_date,
            is_paid=bool(bill.get("is_paid", False)),
            is_gas_estimate=bool(bill.get("is_gas_estimate", False)),
        )
        db.session.add(restored)
        db.session.flush()
        undone["bills_restored"].append({"id": restored.id, "name": restored.name, "amount": restored.amount})

    for raw_txn in _as_action_list("expenses_logged"):
        txn = _normalize_legacy_expense_row(raw_txn)
        txn_id = txn.get("id")
        existing = _household_tx_query().filter_by(id=txn_id).first() if txn_id else None
        if not existing and txn.get("description") and txn.get("amount") is not None:
            existing = (
                _household_tx_query()
                .filter_by(description=txn["description"], amount=txn["amount"])
                .order_by(ExpenseTransaction.id.desc())
                .first()
            )
        if existing:
            apply_balance_delta(current_household_id(), float(existing.amount or 0.0))
            db.session.delete(existing)
            undone["expenses_deleted"].append({"id": existing.id, "description": existing.description})

    for raw_income in _as_action_list("income_logged"):
        if not isinstance(raw_income, dict):
            continue
        income_id = raw_income.get("id")
        existing = _household_tx_query().filter_by(id=income_id).first() if income_id else None
        if not existing and raw_income.get("description") and raw_income.get("amount") is not None:
            existing = (
                _household_tx_query()
                .filter_by(description=raw_income["description"], amount=raw_income["amount"], category="income")
                .order_by(ExpenseTransaction.id.desc())
                .first()
            )
        if existing:
            apply_balance_delta(current_household_id(), -float(existing.amount or 0.0))
            db.session.delete(existing)
            undone["income_deleted"].append({"id": existing.id, "description": existing.description})

    for raw_bal in reversed(_as_action_list("balance_reconciliations")):
        if not isinstance(raw_bal, dict) or account is None:
            continue
        prev = raw_bal.get("previous_balance")
        if prev is None:
            continue
        set_balance_absolute(current_household_id(), float(prev))
        undone["balance_reconciliations_reverted"].append({
            "previous_balance": float(prev),
            "new_balance": float(raw_bal.get("new_balance") or prev),
        })

    for raw_corr in _as_action_list("shopping_trip_corrections"):
        if not isinstance(raw_corr, dict):
            continue
        trip = None
        if raw_corr.get("id"):
            trip = _household_trip_query().filter_by(id=raw_corr.get("id")).first()
        if trip is None and raw_corr.get("operation_id"):
            trip = _household_trip_query().filter_by(operation_id=str(raw_corr.get("operation_id"))).first()
        if trip is None and raw_corr.get("trip_token"):
            trip = _household_trip_query().filter_by(trip_token=str(raw_corr.get("trip_token"))).first()
        if trip is None:
            continue

        previous_actual = raw_corr.get("previous_actual_total")
        if previous_actual is None:
            continue
        difference = float(raw_corr.get("difference") or 0.0)
        trip.actual_total_cents = int(round(float(previous_actual) * 100))
        txn = _household_tx_query().filter_by(id=trip.transaction_id).first()
        prev_txn_amount = raw_corr.get("previous_transaction_amount")
        if txn is not None and prev_txn_amount is not None:
            txn.amount = float(prev_txn_amount)
            db.session.add(txn)
        apply_balance_delta(current_household_id(), difference)
        db.session.add(trip)
        undone["shopping_trip_corrections_reverted"].append({
            "trip_token": trip.trip_token,
            "reverted_actual_total": float(previous_actual),
        })

    grocery_rows = _as_action_list("grocery_items_added") + _as_action_list("grocery_list")
    for raw_grocery in grocery_rows:
        grocery = _normalize_legacy_grocery_row(raw_grocery)
        gid = grocery.get("id")
        item_name = grocery.get("item_name")
        existing = _household_grocery_query().filter_by(id=gid).first() if gid else None
        if not existing and item_name:
            existing = (
                _household_grocery_query()
                .filter(GroceryItem.item_name.ilike(f"%{item_name}%"))
                .order_by(GroceryItem.id.desc())
                .first()
            )
        if existing:
            db.session.delete(existing)
            undone["grocery_items_deleted"].append({"id": existing.id, "item_name": existing.item_name})

    for recipe in _as_action_list("recipes_added") + _as_action_list("recipes_auto_filled"):
        if not isinstance(recipe, dict):
            continue
        recipe_id = recipe.get("id")
        if recipe_id is None:
            continue
        plan_item = _household_meal_plan_query().filter_by(recipe_id=recipe_id).first()
        if plan_item:
            db.session.delete(plan_item)
            undone["recipes_removed"].append({"recipe_id": recipe_id})

    for recipe in _as_action_list("recipes_removed"):
        if not isinstance(recipe, dict):
            continue
        recipe_id = recipe.get("id")
        if recipe_id is None:
            continue
        if (
            visible_recipe_by_id(current_household_id(), recipe_id) is not None
            and not _household_meal_plan_query().filter_by(recipe_id=recipe_id).first()
        ):
            db.session.add(_new_current_meal_plan_item(recipe_id, "copilot"))
            undone["recipes_restored"].append({"recipe_id": recipe_id})

    audit.undone_at = datetime.now(timezone.utc)
    db.session.commit()
    return undone


def set_setting(key: str, value: str, *, commit: bool = True) -> None:
    """Upsert a user setting."""
    row = UserSetting.query.filter_by(
        household_id=current_household_id(),
        key=key,
    ).first()
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.session.add(UserSetting(household_id=current_household_id(), key=key, value=value))
    if commit:
        db.session.commit()


def get_user_preference(key: str, default: str = '') -> str:
    """Read an onboarding preference value by key."""
    row = UserPreference.query.filter_by(
        household_id=current_household_id(),
        key=key,
    ).first()
    return row.value if row else default


def set_user_preference(key: str, value: str) -> None:
    """Upsert an onboarding preference value by key."""
    row = UserPreference.query.filter_by(
        household_id=current_household_id(),
        key=key,
    ).first()
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.session.add(UserPreference(household_id=current_household_id(), key=key, value=value))


def _parse_list_pref(raw: str) -> list[str]:
    """Parse JSON array preferences safely from stored text."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        text = str(item or '').strip()
        if text:
            out.append(text)
    return out


def _normalize_list_input(values: Any) -> list[str]:
    """Normalize onboarding list inputs from JSON arrays or CSV text."""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(',')
    if not isinstance(values, list):
        values = [values]
    out = []
    for item in values:
        text = str(item or '').strip()
        if text and text.lower() not in {v.lower() for v in out}:
            out.append(text)
    return out


def _coerce_positive_float(value: Any) -> float | None:
    """Coerce onboarding numeric fields to a positive float or None."""
    if value is None or value == '':
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return round(n, 2)


def _onboarding_state_payload() -> dict:
    """Build a frontend-friendly onboarding state snapshot."""
    hid = current_household_id()
    account = get_household_account(hid)

    favored = _parse_list_pref(get_user_preference('favorite_proteins', ''))
    restrictions = _parse_list_pref(get_user_preference('dietary_restrictions', ''))
    allergies = _parse_list_pref(get_user_preference('allergies', ''))

    grocery_baseline_raw = get_user_preference('baseline_grocery_cost', '')
    fuel_baseline_raw = get_user_preference('baseline_fuel_cost', '')
    grocery_baseline = _coerce_positive_float(grocery_baseline_raw)
    fuel_baseline = _coerce_positive_float(fuel_baseline_raw)

    preferred_names = ['Phone', 'Internet', 'Utilities']
    existing_bills = {b.name.lower(): b for b in _household_bill_query().filter_by(is_gas_estimate=False).all()}
    bill_templates = []
    for name in preferred_names:
        existing = existing_bills.get(name.lower())
        bill_templates.append({
            'name': name,
            'amount': round(existing.amount, 2) if existing else None,
        })

    gas_bill = _household_bill_query().filter_by(is_gas_estimate=True).first()
    if fuel_baseline is None and gas_bill:
        fuel_baseline = round(gas_bill.amount, 2)

    pyf_target = _explicit_household_setting_decimal(PYF_TARGET_SETTING_KEY)
    buffer = _explicit_household_setting_decimal(SAFE_BUFFER_SETTING_KEY)
    shopping = _load_household_shopping_defaults()

    plan = income_plan_payload(hid, at=datetime.now(timezone.utc))
    current_plan = plan.get("current") or {}
    return {
        'is_onboarded': bool(account.is_onboarded),
        'show_onboarding': not bool(account.is_onboarded),
        'defaults': {
            'household_size': int(account.household_size or 4),
            'favorite_proteins': favored,
            'dietary_restrictions': restrictions,
            'allergies': allergies,
            'baseline_grocery_cost': grocery_baseline,
            'baseline_fuel_cost': fuel_baseline,
            'checking_balance': round(float(account.checking_balance), 2) if account.checking_balance is not None else None,
            'pay_period_days': int(account.pay_period_days or 0),
            'expected_paycheck': current_plan.get("expected_income"),
            'expected_paycheck_suggested': (round(float(account.expected_paycheck), 2)
                                             if not current_plan and account.expected_paycheck is not None else None),
            'income_plan': plan,
            'next_payday': get_setting(NEXT_PAYDAY_SETTING_KEY, '') or None,
            'long_term_savings_target_percent': float(pyf_target) if pyf_target is not None else None,
            'protected_buffer': float(buffer) if buffer is not None else None,
            'shopping_style': shopping.get('shopping_style'),
            'household_shopping_defaults': shopping.get('preferences'),
            'location_sharing_enabled': get_setting(LOCATION_SHARING_SETTING_KEY, 'false') == 'true',
        },
        'bill_templates': bill_templates,
        'readiness': _onboarding_readiness(account),
        'required_expense_review': _required_expense_review_state(),
    }


def _onboarding_readiness(account: Account | None) -> dict:
    """Report canonical readiness so onboarding completion is truthful.

    Readiness is derived from the same canonical Pay Yourself First snapshot
    used by the rest of the product; it is never faked from the
    ``is_onboarded`` flag, so a household that only clicked Finish without
    the critical financial setup still reports what is actually missing.
    """
    if account is not None:
        account = Account.query.filter_by(id=account.id).first() or account
    safe = _compute_safe_to_spend_snapshot(account, owner_scope=household_scope_key())
    missing = safe.get("missing_setup") or []
    return {
        "complete": bool(safe.get("complete")),
        "safe_to_spend_available": bool(safe.get("complete")),
        "missing_setup": missing,
    }


def _persist_onboarding_financial_basics(account: Account, data: dict[str, Any]) -> list[str]:
    """Persist optional financial onboarding inputs to canonical authorities.

    Only fields explicitly present are written, so revisiting onboarding never
    erases existing values. Validation happens before any write; errors are
    returned to the caller so no partial financial state is persisted.
    """
    errors: list[str] = []
    hid = current_household_id()

    balance_cents = None
    if "checking_balance" in data and data["checking_balance"] not in (None, ""):
        try:
            balance_cents = _money_to_cents(data["checking_balance"], field_name="checking_balance")
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if balance_cents < 0:
                errors.append("checking_balance cannot be negative.")

    pay_period_days = None
    if "pay_period_days" in data and data["pay_period_days"] not in (None, ""):
        try:
            pay_period_days = int(data["pay_period_days"])
        except (TypeError, ValueError):
            errors.append("pay_period_days must be a whole number of days.")
        else:
            if pay_period_days < 1:
                errors.append("pay_period_days must be at least 1 day.")

    expected_paycheck_cents = None
    if "expected_paycheck" in data and data["expected_paycheck"] not in (None, ""):
        try:
            expected_paycheck_cents = _money_to_cents(data["expected_paycheck"], field_name="expected_paycheck")
        except (TypeError, ValueError) as exc:
            errors.append("expected_paycheck must be a valid amount.")
        else:
            if expected_paycheck_cents <= 0:
                errors.append("expected_paycheck must be greater than zero.")

    next_payday = None
    if "next_payday" in data and data["next_payday"] not in (None, ""):
        try:
            next_payday = date.fromisoformat(str(data["next_payday"]))
        except (TypeError, ValueError):
            errors.append("next_payday must be a valid YYYY-MM-DD date.")

    pyf_target = None
    if "long_term_savings_target_percent" in data and data["long_term_savings_target_percent"] not in (None, ""):
        try:
            pyf_target = Decimal(str(data["long_term_savings_target_percent"]))
        except (InvalidOperation, TypeError, ValueError):
            errors.append("long_term_savings_target_percent must be a valid percentage.")
        else:
            if pyf_target < 0:
                errors.append("long_term_savings_target_percent cannot be negative.")

    buffer_cents = None
    if "protected_buffer" in data and data["protected_buffer"] not in (None, ""):
        try:
            buffer_cents = _money_to_cents(data["protected_buffer"], field_name="protected_buffer")
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if buffer_cents < 0:
                errors.append("protected_buffer cannot be negative.")

    if errors:
        return errors

    if balance_cents is not None:
        set_balance_absolute(hid, _cents_to_float(balance_cents))
    if pay_period_days is not None:
        account.pay_period_days = pay_period_days
    if next_payday is not None:
        set_setting(NEXT_PAYDAY_SETTING_KEY, next_payday.isoformat(), commit=False)
    if expected_paycheck_cents is not None:
        operation_id = str(data.get("expected_paycheck_operation_id") or "").strip()
        if not operation_id:
            return ["expected_paycheck_operation_id is required when confirming an expected paycheck."]
        effective_payday = None
        if IncomePlanVersion.query.filter_by(household_id=hid).first() is not None:
            if next_payday is not None:
                candidate = datetime.combine(next_payday, datetime.min.time(), tzinfo=timezone.utc)
                cycle = resolve_cycle(account=account, now=datetime.now(timezone.utc),
                                      next_income={"known": True, "date": candidate, "source": "user_pay_schedule"})
                effective_payday = cycle.get("end") if cycle.get("available") else None
            else:
                inferred = _infer_next_income(account, datetime.now(timezone.utc))
                cycle = resolve_cycle(account=account, now=datetime.now(timezone.utc), next_income=inferred)
                effective_payday = cycle.get("end") if cycle.get("available") else None
        try:
            record_income_plan(hid, operation_id=operation_id,
                               expected_income_cents=expected_paycheck_cents,
                               now=datetime.now(timezone.utc), next_payday=effective_payday,
                               source="onboarding_confirmation")
        except IncomePlanError as exc:
            return [str(exc)]
    if pyf_target is not None:
        set_setting(PYF_TARGET_SETTING_KEY, format(pyf_target.normalize(), "f"), commit=False)
    if buffer_cents is not None:
        set_setting(SAFE_BUFFER_SETTING_KEY, f"{_cents_to_float(buffer_cents):.2f}", commit=False)

    return []


def _normalize_household_default_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_household_shopping_defaults(owner_scope: str = HOUSEHOLD_DEFAULT_OWNER_SCOPE) -> dict[str, Any]:
    rows = HouseholdShoppingDefault.query.filter_by(household_id=current_household_id()).all()
    category_defaults: dict[str, str] = {}
    shopping_style = None
    for row in rows:
        if row.preference_kind == HOUSEHOLD_DEFAULT_KIND_CATEGORY:
            category_defaults[row.preference_key] = row.preference_value
        elif row.preference_kind == HOUSEHOLD_DEFAULT_KIND_STYLE and row.preference_key == HOUSEHOLD_STYLE_KEY:
            shopping_style = row.preference_value
    return {
        "preferences": category_defaults,
        "shopping_style": shopping_style,
    }


def _save_household_shopping_defaults(
    payload: dict[str, Any],
    owner_scope: str = HOUSEHOLD_DEFAULT_OWNER_SCOPE,
    *,
    commit: bool = True,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["Body must be a JSON object."]

    raw_preferences = payload.get("preferences", {})
    if raw_preferences is None:
        raw_preferences = {}
    if not isinstance(raw_preferences, dict):
        errors.append("preferences must be an object keyed by canonical preference key.")
        raw_preferences = {}

    invalid_keys = sorted(key for key in raw_preferences.keys() if key not in HOUSEHOLD_DEFAULT_ALLOWED_VALUES)
    if invalid_keys:
        errors.append("Unknown preference keys: " + ", ".join(invalid_keys))

    normalized_preferences: dict[str, str | None] = {}
    for key, value in raw_preferences.items():
        if key not in HOUSEHOLD_DEFAULT_ALLOWED_VALUES:
            continue
        normalized = _normalize_household_default_value(value)
        if normalized is None:
            normalized_preferences[key] = None
            continue
        if normalized not in HOUSEHOLD_DEFAULT_ALLOWED_VALUES[key]:
            errors.append(f"Invalid value '{normalized}' for preference key '{key}'.")
            continue
        normalized_preferences[key] = normalized

    shopping_style_present = "shopping_style" in payload
    normalized_style = _normalize_household_default_value(payload.get("shopping_style")) if shopping_style_present else None
    if shopping_style_present and normalized_style is not None and normalized_style not in SHOPPING_STYLE_ALLOWED_VALUES:
        errors.append(f"Invalid shopping_style '{normalized_style}'.")

    if errors:
        return False, errors

    for key, value in normalized_preferences.items():
        existing = HouseholdShoppingDefault.query.filter_by(
            household_id=current_household_id(),
            preference_kind=HOUSEHOLD_DEFAULT_KIND_CATEGORY,
            preference_key=key,
        ).first()
        if value is None:
            if existing is not None:
                db.session.delete(existing)
            continue
        if existing is None:
            db.session.add(HouseholdShoppingDefault(
                household_id=current_household_id(),
                owner_scope=owner_scope,
                preference_kind=HOUSEHOLD_DEFAULT_KIND_CATEGORY,
                preference_key=key,
                preference_value=value,
            ))
        else:
            existing.preference_value = value
            existing.updated_at = datetime.now(timezone.utc)

    if shopping_style_present:
        existing_style = HouseholdShoppingDefault.query.filter_by(
            household_id=current_household_id(),
            preference_kind=HOUSEHOLD_DEFAULT_KIND_STYLE,
            preference_key=HOUSEHOLD_STYLE_KEY,
        ).first()
        if normalized_style is None:
            if existing_style is not None:
                db.session.delete(existing_style)
        elif existing_style is None:
            db.session.add(HouseholdShoppingDefault(
                household_id=current_household_id(),
                owner_scope=owner_scope,
                preference_kind=HOUSEHOLD_DEFAULT_KIND_STYLE,
                preference_key=HOUSEHOLD_STYLE_KEY,
                preference_value=normalized_style,
            ))
        else:
            existing_style.preference_value = normalized_style
            existing_style.updated_at = datetime.now(timezone.utc)

    if commit:
        db.session.commit()
    return True, []


def seed_default_user_preferences(user_id: str = "anonymous", *, commit: bool = True) -> dict:
    """Seed a curated starter set of favorites and defaults for a new account.

    Starter favorites apply only to trusted canonical catalog rows; ordinary
    onboarding never mutates private or quarantined recipes.
    """
    starter_record = get_user_preference("starter_preferences_seeded", "")
    if starter_record:
        return {"seeded": False, "titles": [], "user_id": user_id, "already_seeded": True}

    seeded_titles: list[str] = []
    for title in DEFAULT_STARTER_RECIPE_TITLES:
        recipe = Recipe.query.filter(
            Recipe.recipe_scope == Recipe.SCOPE_CANONICAL,
            Recipe.title.ilike(title),
        ).first()
        if recipe is None:
            continue
        if not bool(getattr(recipe, "is_favorite", False)):
            recipe.is_favorite = True
        seeded_titles.append(recipe.title)

    if seeded_titles:
        set_user_preference(
            "starter_preferences_seeded",
            json.dumps({"user_id": user_id, "titles": seeded_titles}),
        )
        set_user_preference("starter_grocery_items", json.dumps(DEFAULT_STARTER_GROCERY_ITEMS))
        if commit:
            db.session.commit()

    return {
        "seeded": bool(seeded_titles),
        "titles": seeded_titles,
        "user_id": user_id,
        "already_seeded": False,
    }


# =============================================================================
# UNIT CONVERSION & HELPER ENGINES
# =============================================================================


def _coerce_amount(value):
    """Coerce a money value to float, tolerating '$', commas, and strings.

    Handles ``60``, ``"$60"``, ``"60.00"``, ``"60/mo"``, ``"60 per month"``.
    Returns ``0.0`` when unparseable (callers treat ``<= 0`` as no-op).
    """
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    # Strip monthly/period suffixes: "60/mo", "60 per month", "$60 monthly"
    s = re.sub(r'(?i)(/mo|per\s+month|monthly|/month|/yr|per\s+year|annually).*$', '', s).strip()
    s = s.replace('$', '').replace(',', '')
    try:
        return float(s)
    except ValueError:
        return 0.0


def _serialize_recipe_ingredient(ingredient):
    """Serialize stored fidelity fields plus a non-fabricated display string."""
    name = str(ingredient.product_name or "").strip()
    has_requirement_prefix = bool(re.match(
        r"^(?:\d|[¼½¾⅓⅔⅛⅜⅝⅞]|one\b|two\b|three\b|four\b|five\b|six\b|seven\b|eight\b|nine\b|ten\b|a\b|an\b)",
        name,
        re.IGNORECASE,
    ))
    if ingredient.quantity is None or has_requirement_prefix:
        display_text = name
    else:
        quantity = f"{float(ingredient.quantity):g}"
        display_text = f"{quantity}{f' {ingredient.unit}' if ingredient.unit else ''} {name}".strip()
    return {
        "id": ingredient.id,
        "product_name": ingredient.product_name,
        "clean_keyword": ingredient.clean_keyword,
        "quantity": ingredient.quantity,
        "unit": ingredient.unit,
        "display_text": display_text,
    }


def _serialize_recipe(r):
    """Serialize a Recipe row into the standard API payload shape."""
    return {
        "id": r.id,
        "title": r.title,
        "servings": r.servings,
        "estimated_cost_per_serving": r.estimated_cost_per_serving,
        "is_favorite": bool(getattr(r, "is_favorite", False)),
        "usage_frequency": int(getattr(r, "usage_frequency", 0) or 0),
        "last_selected_date": (
            r.last_selected_date.isoformat() if getattr(r, "last_selected_date", None) else None
        ),
        "source_url": r.source_url or "",
        "instructions": r.instructions,
        "can_edit": r.recipe_scope == Recipe.SCOPE_HOUSEHOLD_PRIVATE and r.household_id == current_household_id(),
        "can_delete": r.recipe_scope == Recipe.SCOPE_HOUSEHOLD_PRIVATE and r.household_id == current_household_id(),
        "ingredients": [_serialize_recipe_ingredient(i) for i in r.ingredients],
    }


def _match_recipe_by_title(title):  # pyright: ignore[reportUnusedFunction]
    """Fuzzy-match a natural-language recipe title against the local DB.

    Token overlap scoring: "chicken rice bowl" matches "Chicken Rice Bowl"
    (1.0); "flank steak fajitas" does NOT match "Chicken Rice Bowl" (0.0).
    Returns the best ``Recipe`` with score >= 0.5, else ``None``.
    """
    t = (title or "").strip().lower()
    if not t:
        return None
    t_tokens = set(re.sub(r'[^a-z0-9 ]', '', t).split())
    if not t_tokens:
        return None
    best, best_score = None, 0.0
    for r in visible_recipe_query(current_household_id()).all():
        rt_tokens = set(re.sub(r'[^a-z0-9 ]', '', (r.title or '').lower()).split())
        if not rt_tokens:
            continue
        overlap = len(t_tokens & rt_tokens)
        if overlap == 0:
            continue
        score = overlap / max(1, len(rt_tokens))
        if score > best_score:
            best_score, best = score, r
    if best and best_score >= 0.5:
        return best
    return None


# Recipe recommendation now in services.recipe_recommend module
# Keep explicit reference for _match_recipe_by_title which is still used.
_ = (_match_recipe_by_title,)


# =============================================================================
# UNIT CONVERSION & HELPER ENGINES
# =============================================================================


UNIT_TO_OZ = {
    'oz': 1.0, 'ounce': 1.0, 'ounces': 1.0,
    'lb': 16.0, 'lbs': 16.0, 'pound': 16.0, 'pounds': 16.0,
    'cup': 8.0, 'cups': 8.0,
    'tbsp': 0.5, 'tablespoon': 0.5, 'tablespoons': 0.5,
    'tsp': 0.166, 'teaspoon': 0.166, 'teaspoons': 0.166,
    'g': 0.035, 'gram': 0.035, 'grams': 0.035,
    'unit': 1.0, 'item': 1.0, 'ea': 1.0, 'can': 1.0
}

def normalize_to_standard_unit(quantity, unit):
    clean_u = (unit or 'unit').strip().lower()
    multiplier = UNIT_TO_OZ.get(clean_u, 1.0)
    return quantity * multiplier


def _is_liquid_keyword(keyword):
    kw = (keyword or '').lower()
    liquid_tokens = (
        'milk', 'cream', 'broth', 'stock', 'water', 'juice',
        'oil', 'vinegar', 'sauce', 'syrup'
    )
    return any(tok in kw for tok in liquid_tokens)


def infer_unit_dimension(unit, keyword=None):
    """Classify recipe/pantry units for package-compatibility checks.

    Returns one of: "mass", "volume", "count", or "unknown".
    """
    u = (unit or '').strip().lower()
    kw = (keyword or '').lower()

    # Safe commodity-specific conversion: butter sticks are a standard mass
    # measure in US packaging (1 stick = 4 oz = 1/4 lb).
    if u in {'stick', 'sticks'} and 'butter' in kw:
        return 'mass'

    if u in {'oz', 'ounce', 'ounces', 'lb', 'lbs', 'pound', 'pounds', 'g', 'gram', 'grams', 'kg', 'kilogram', 'kilograms'}:
        return 'mass'
    if u in {'cup', 'cups', 'tbsp', 'tablespoon', 'tablespoons', 'tsp', 'teaspoon', 'teaspoons'}:
        # Do not assume these culinary volume units map safely to mass for
        # arbitrary ingredients. Only treat as reliable when the ingredient is
        # clearly liquid-like.
        return 'volume' if _is_liquid_keyword(keyword) else 'unknown'
    if u in {'fl oz', 'floz', 'fluid ounce', 'fluid ounces', 'ml', 'milliliter', 'milliliters', 'l', 'liter', 'liters', 'qt', 'quart', 'quarts', 'pt', 'pint', 'pints', 'gal', 'gallon', 'gallons'}:
        return 'volume'
    if u in {'unit', 'item', 'ea', 'each', 'can', 'stick', 'sticks', 'count', 'ct', 'box', 'package', 'pack'}:
        return 'count'
    return 'unknown'


def normalize_requirement_for_selection(quantity, unit, keyword):
    """Normalize ingredient quantities only when conversion is safe.

    Returns (normalized_quantity, required_dimension, reliable_conversion).
    """
    qty = float(quantity or 0)
    u = (unit or '').strip().lower()
    kw = (keyword or '').lower()

    # Safe commodity-specific conversion for butter sticks.
    if u in {'stick', 'sticks'} and 'butter' in kw:
        return qty * 4.0, 'mass', True

    dim = infer_unit_dimension(unit, keyword=keyword)
    if dim == 'unknown':
        return qty, 'unknown', False

    return normalize_to_standard_unit(qty, unit), dim, True


def _normalize_manual_grocery_keyword(value: Any) -> str:
    """Convert a direct grocery request to the same normalized keyword used in recipe ingredients."""
    text = (value or '').strip()
    if not text:
        return ''
    text = text.lower().replace('_', ' ')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    text = ' '.join(text.split())
    return text.replace(' ', '_')


_MONEY_QUANT = Decimal("0.01")


def _to_decimal_money(value: Any, *, field_name: str = "amount", allow_negative: bool = False) -> Decimal:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Invalid {field_name}.")
    if not allow_negative and dec < Decimal("0"):
        raise ValueError(f"{field_name} cannot be negative.")
    return dec.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _money_to_cents(value: Any, *, field_name: str = "amount") -> int:
    dec = _to_decimal_money(value, field_name=field_name)
    cents = (dec * 100).to_integral_value(rounding=ROUND_HALF_UP)
    return int(cents)


def _cents_to_float(cents: int) -> float:
    return float((Decimal(int(cents)) / Decimal("100")).quantize(_MONEY_QUANT))


def _normalize_store_tax_context(
    *,
    account: Account,
    retailer: str,
    store_name: str,
    store_id: str,
    store_address: str,
    postal_code: str,
    city_state: str = "",
) -> dict[str, Any]:
    address = str(store_address or "").strip()
    explicit_city_state = str(city_state or "").strip()
    if not explicit_city_state and address:
        match = re.search(r",\s*([^,]+),\s*([A-Za-z]{2})\s+\d{5}(?:-\d{4})?\s*$", address)
        if match:
            explicit_city_state = f"{match.group(1).strip()}, {match.group(2).upper()}"
    return {
        "retailer": str(retailer or "").strip().lower() or "unknown",
        "retailer_store_id": str(store_id or "").strip() or "unknown",
        "store_name": str(store_name or "").strip(),
        "store_address": address,
        "zip_code": _normalize_zip_code(postal_code or ""),
        "city_state": explicit_city_state,
        "latitude": None,
        "longitude": None,
    }


def _apply_owned_tax_to_cart(
    *,
    account: Account,
    owner_scope: str,
    cart_items: list[dict[str, Any]],
    retailer: str,
    store_name: str,
    store_id: str,
    store_address: str,
    postal_code: str,
    city_state: str = "",
    purchase_context: str = "selected_physical_store",
    actual_tax_cents: int | None = None,
    actual_total_cents: int | None = None,
) -> dict[str, Any]:
    calculation_date = datetime.now(timezone.utc).date()
    context = _normalize_store_tax_context(
        account=account,
        retailer=retailer,
        store_name=store_name,
        store_id=store_id,
        store_address=store_address,
        postal_code=postal_code,
        city_state=city_state,
    )

    profile = resolve_store_tax_profile(
        retailer=context["retailer"],
        retailer_store_id=context["retailer_store_id"],
        store_name=context["store_name"],
        store_address=context["store_address"],
        zip_code=context["zip_code"],
        city_state=context["city_state"],
        latitude=context["latitude"],
        longitude=context["longitude"],
        calculation_date=calculation_date,
        owner_scope=owner_scope,
    )

    decision = canonical_tax_decision(
        store_tax_profile=profile,
        cart_items=cart_items,
        calculation_date=calculation_date,
        owner_scope=owner_scope,
        purchase_context=purchase_context,
        city=context["city_state"].split(",", 1)[0],
        postal_code=context["zip_code"],
        actual_tax_cents=actual_tax_cents,
        actual_total_cents=actual_total_cents,
    )

    weighted_rate = 0.0
    if decision["subtotal_cents"] > 0 and decision["tax_cents"] is not None:
        weighted_rate = float(decision["tax_cents"] / decision["subtotal_cents"])

    return {
        "subtotal": tax_cents_to_float(decision["subtotal_cents"]),
        "tax_amount": tax_cents_to_float(decision["tax_cents"]) if decision["tax_cents"] is not None else None,
        "total_cart_cost": tax_cents_to_float(decision["total_cents"]) if decision["total_cents"] is not None else None,
        "grocery_tax_rate": round(weighted_rate * 100, 3),
        "applied_tax_pct": round(weighted_rate * 100, 3),
        "tax_engine": {**decision,
            "provider": "rung_owned",
            "precision": decision["jurisdiction"]["precision"],
            "confidence": decision["status"],
            "source_version": decision["source"]["version"],
            "unknown_class_count": decision["taxability_basis"]["unknown_class_count"],
            "subtotal_by_class_cents": decision["taxability_basis"]["subtotal_by_class_cents"],
            "tax_by_class_cents": decision["taxability_basis"]["tax_by_class_cents"],
            "unknown_policy": "tax_not_included_until_supported",
        },
    }


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get_safe_buffer_cents() -> int:
    raw = get_setting(SAFE_BUFFER_SETTING_KEY, "0")
    try:
        return _money_to_cents(raw, field_name="safe_to_spend_buffer")
    except ValueError:
        return 0


def _latest_manual_activity_at() -> datetime | None:
    row = (
        _household_tx_query()
        .filter(ExpenseTransaction.source == "manual")
        .order_by(ExpenseTransaction.date.desc(), ExpenseTransaction.id.desc())
        .first()
    )
    if row is None or row.date is None:
        return None
    dt = row.date
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _planned_grocery_commitment_cents(account: Account, bills_total_cents: int, gas_cents: int) -> tuple[int, str]:
    """Return the pay-period grocery commitment in cents and its source.

    Commitments should come from the period plan, not from every balance
    mutation. Prefer expected paycheck as the planning base.
    """
    pct = Decimal(str(account.food_allocation_pct or 0))
    if pct <= 0:
        return 0, "none"

    plan = resolve_income_plan(account.household_id, at=datetime.now(timezone.utc))
    expected_paycheck_cents = int(plan.expected_income_cents) if plan else 0
    if expected_paycheck_cents > 0:
        planning_base_cents = max(0, expected_paycheck_cents - bills_total_cents - gas_cents)
        source = "income_plan_v1"
    else:
        checking_cents = _money_to_cents(account.checking_balance, field_name="checking_balance")
        planning_base_cents = max(0, checking_cents - bills_total_cents - gas_cents)
        source = "checking_balance_fallback"

    commitment_cents = max(
        0,
        int(
            (
                Decimal(planning_base_cents)
                * pct
                / Decimal("100")
            ).to_integral_value(rounding=ROUND_HALF_UP)
        ),
    )
    return commitment_cents, source


def _infer_next_income(account: Account, now_utc: datetime) -> dict[str, Any]:
    period_days = max(1, int(account.pay_period_days or 0))
    explicit_payday = get_setting(NEXT_PAYDAY_SETTING_KEY, "")
    if explicit_payday:
        try:
            next_date = date.fromisoformat(explicit_payday)
        except ValueError:
            next_date = None
        if next_date is not None:
            while next_date < now_utc.date():
                next_date += timedelta(days=period_days)
            return {
                "known": True,
                "date": datetime.combine(next_date, datetime.min.time(), tzinfo=timezone.utc),
                "days_until": max(0, (next_date - now_utc.date()).days),
                "amount": (round(resolve_income_plan(account.household_id, at=now_utc).expected_income_cents / 100, 2)
                           if resolve_income_plan(account.household_id, at=now_utc) else None),
                "source": "user_pay_schedule",
            }
    latest_income = (
        _household_tx_query()
        .filter(ExpenseTransaction.category == "income")
        .order_by(ExpenseTransaction.date.desc(), ExpenseTransaction.id.desc())
        .first()
    )
    if latest_income is None or latest_income.date is None:
        return {
            "known": False,
            "date": None,
            "days_until": None,
            "amount": (round(resolve_income_plan(account.household_id, at=now_utc).expected_income_cents / 100, 2)
                       if resolve_income_plan(account.household_id, at=now_utc) else None),
            "source": "missing_income_history",
        }

    base_dt = latest_income.date
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=timezone.utc)
    else:
        base_dt = base_dt.astimezone(timezone.utc)

    next_dt = base_dt
    while next_dt.date() <= now_utc.date():
        next_dt = next_dt + timedelta(days=period_days)

    plan = resolve_income_plan(account.household_id, at=now_utc)
    amount = (int(plan.expected_income_cents) / 100) if plan else None

    return {
        "known": True,
        "date": next_dt,
        "days_until": max(0, (next_dt.date() - now_utc.date()).days),
        "amount": round(amount, 2) if amount is not None else None,
        "source": "derived_from_income_history",
    }


def _household_readiness(account: Account | None, owner_scope: str = "anonymous") -> dict[str, Any]:
    missing_financial: list[str] = []
    setup_gaps: list[str] = []

    if account is None:
        missing_financial.append("checking_balance")
        missing_financial.append("pay_period_or_income_history")
        return {
            "ready": False,
            "safe_to_spend_available": False,
            "missing_critical": missing_financial,
            "setup_gaps": setup_gaps,
        }

    if account.checking_balance is None:
        missing_financial.append("checking_balance")

    pay_period_days = int(account.pay_period_days or 0)
    expected_paycheck = resolve_income_plan(account.household_id, at=datetime.now(timezone.utc))
    has_income_history = bool(
        _household_tx_query()
        .filter(ExpenseTransaction.category == "income")
        .first()
    )
    if pay_period_days <= 0 or (expected_paycheck is None and not has_income_history):
        missing_financial.append("pay_period_or_income_history")

    zip_code = str(account.zip_code or "").strip()
    if not zip_code:
        setup_gaps.append("zip_code")

    selected_store = get_selected_store(current_household_id(), account=account)
    retailer = str(selected_store.get("retailer") or "walmart").strip().lower()
    if retailer == "kroger" and not str(selected_store.get("store_id") or "").strip():
        setup_gaps.append("kroger_location")

    return {
        "ready": len(missing_financial) == 0,
        "safe_to_spend_available": len(missing_financial) == 0,
        "missing_critical": missing_financial,
        "setup_gaps": setup_gaps,
        "retailer": retailer,
        "owner_scope": owner_scope,
    }


def _build_freshness(owner_scope: str) -> dict[str, Any]:
    controls = get_usage_controls()
    switches = controls.get("kill_switches") or {}
    plaid_sync_enabled = bool(switches.get("plaid_sync_enabled", True))
    plaid_status = get_plaid_connection_status(owner_scope)
    items = plaid_status.get("items") or []

    latest_sync: datetime | None = None
    for item in items:
        parsed = _parse_iso_datetime((item or {}).get("last_sync_at"))
        if parsed is not None and (latest_sync is None or parsed > latest_sync):
            latest_sync = parsed

    if plaid_status.get("connected"):
        if latest_sync is not None:
            stamp = latest_sync.strftime("%b %d, %Y %I:%M %p UTC")
            if plaid_sync_enabled:
                text = f"Bank updated {stamp}"
            else:
                text = f"Bank sync paused; using last bank update {stamp}"
            return {
                "source": "bank",
                "bank_connected": True,
                "plaid_sync_enabled": plaid_sync_enabled,
                "last_sync_at": latest_sync.isoformat(),
                "text": text,
            }
        if plaid_sync_enabled:
            text = "Bank connected; waiting for first sync"
        else:
            text = "Bank connected; sync paused"
        return {
            "source": "bank",
            "bank_connected": True,
            "plaid_sync_enabled": plaid_sync_enabled,
            "last_sync_at": None,
            "text": text,
        }

    manual_at = _latest_manual_activity_at()
    if manual_at is None:
        text = "Manual balance mode"
    else:
        text = f"Manual balance last confirmed {manual_at.strftime('%b %d, %Y')}"
    return {
        "source": "manual",
        "bank_connected": False,
        "plaid_sync_enabled": plaid_sync_enabled,
        "last_sync_at": None,
        "text": text,
    }


def _compute_legacy_safe_to_spend_snapshot(account: Account, owner_scope: str = "anonymous", now_utc: datetime | None = None) -> dict[str, Any]:
    readiness = _household_readiness(account, owner_scope=owner_scope)
    if not readiness.get("safe_to_spend_available", False):
        return {
            "state": "needs_setup",
            "safe_to_spend_cents": None,
            "safe_to_spend": None,
            "overcommitted": False,
            "until_payday_days": None,
            "next_expected_income": {
                "known": False,
                "date": None,
                "date_display": None,
                "amount": None,
                "source": "missing_setup",
            },
            "breakdown": {
                "lines": [],
                "checks": {"sum_matches_safe_to_spend": False},
            },
            "components": {
                "usable_money": float(account.checking_balance or 0.0),
                "bills_before_payday": 0.0,
                "grocery_commitment_total": 0.0,
                "grocery_spend_to_date": 0.0,
                "groceries_remaining": 0.0,
                "other_committed_spending": 0.0,
                "protected_buffer": _cents_to_float(_get_safe_buffer_cents()),
                "grocery_commitment_source": "unavailable",
            },
            "freshness": _build_freshness(owner_scope),
            "explanation": "Safe-to-Spend is unavailable until setup is complete.",
            "window_end": None,
            "readiness": readiness,
        }

    now = now_utc or datetime.now(timezone.utc)
    next_income = _infer_next_income(account, now)

    if next_income.get("known") and isinstance(next_income.get("date"), datetime):
        window_end = next_income["date"]
    else:
        window_end = now + timedelta(days=max(1, int(account.pay_period_days or 14)))

    bills = (
        _household_bill_query()
        .filter(
            Bill.is_paid == False,
            Bill.is_gas_estimate == False,
            Bill.due_date <= window_end,
        )
        .all()
    )
    bills_total_cents = sum(_money_to_cents(row.amount, field_name="bill amount") for row in bills)

    gas_bill = _household_bill_query().filter_by(is_gas_estimate=True, is_paid=False).first()
    gas_cents = _money_to_cents(gas_bill.amount if gas_bill else 60.0, field_name="gas allocation")

    checking_cents = _money_to_cents(account.checking_balance, field_name="checking_balance")
    food_budget_cents, food_budget_source = _planned_grocery_commitment_cents(
        account,
        bills_total_cents,
        gas_cents,
    )

    grocery_spend_cents = _money_to_cents(_sum_grocery_spend_for_period(account, now), field_name="grocery_spend_to_date")
    grocery_remaining_cents = max(0, food_budget_cents - grocery_spend_cents)

    buffer_cents = _get_safe_buffer_cents()
    safe_to_spend_cents = checking_cents - bills_total_cents - gas_cents - grocery_remaining_cents - buffer_cents

    if safe_to_spend_cents < 0:
        state = "overcommitted"
    elif safe_to_spend_cents <= 5000:
        state = "tight"
    else:
        state = "positive"

    lines = [
        {"key": "usable_money", "label": "Current usable money", "amount_cents": checking_cents},
        {"key": "bills_before_payday", "label": "Bills before payday", "amount_cents": -bills_total_cents},
        {"key": "groceries_remaining", "label": "Groceries remaining", "amount_cents": -grocery_remaining_cents},
        {"key": "other_committed", "label": "Other committed spending", "amount_cents": -gas_cents},
        {"key": "protected_buffer", "label": "Protected buffer", "amount_cents": -buffer_cents},
        {"key": "safe_to_spend", "label": "Safe to Spend", "amount_cents": safe_to_spend_cents},
    ]

    freshness = _build_freshness(owner_scope)
    next_income_date = next_income.get("date")

    explanation = None
    if state == "overcommitted":
        explanation = (
            "Your protected bills, grocery needs, and buffer exceed the money "
            f"available before payday by ${_cents_to_float(abs(safe_to_spend_cents)):.2f}."
        )
    elif not next_income.get("known"):
        explanation = "Next payday is not set. Safe-to-Spend is based on known balances, bills, groceries, and buffer."

    return {
        "state": state,
        "safe_to_spend_cents": safe_to_spend_cents,
        "safe_to_spend": _cents_to_float(safe_to_spend_cents),
        "overcommitted": state == "overcommitted",
        "until_payday_days": next_income.get("days_until"),
        "next_expected_income": {
            "known": bool(next_income.get("known")),
            "date": next_income_date.isoformat() if isinstance(next_income_date, datetime) else None,
            "date_display": next_income_date.strftime("%b %d") if isinstance(next_income_date, datetime) else None,
            "amount": next_income.get("amount"),
            "source": next_income.get("source"),
        },
        "breakdown": {
            "lines": [
                {
                    "key": row["key"],
                    "label": row["label"],
                    "amount_cents": int(row["amount_cents"]),
                    "amount": _cents_to_float(int(row["amount_cents"])),
                }
                for row in lines
            ],
            "checks": {
                "sum_matches_safe_to_spend": (
                    checking_cents - bills_total_cents - grocery_remaining_cents - gas_cents - buffer_cents
                ) == safe_to_spend_cents,
            },
        },
        "components": {
            "usable_money": _cents_to_float(checking_cents),
            "bills_before_payday": _cents_to_float(bills_total_cents),
            "grocery_commitment_total": _cents_to_float(food_budget_cents),
            "grocery_spend_to_date": _cents_to_float(grocery_spend_cents),
            "groceries_remaining": _cents_to_float(grocery_remaining_cents),
            "other_committed_spending": _cents_to_float(gas_cents),
            "protected_buffer": _cents_to_float(buffer_cents),
            "grocery_commitment_source": food_budget_source,
        },
        "freshness": freshness,
        "explanation": explanation,
        "window_end": window_end.isoformat(),
        "readiness": readiness,
    }


def _explicit_household_setting_decimal(key: str) -> Decimal | None:
    row = UserSetting.query.filter_by(household_id=current_household_id(), key=key).first()
    if row is None:
        return None
    try:
        value = Decimal(str(row.value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value


def _explicit_household_preference_cents(key: str) -> int | None:
    row = UserPreference.query.filter_by(household_id=current_household_id(), key=key).first()
    if row is None:
        return None
    try:
        cents = _money_to_cents(row.value, field_name=key)
    except ValueError:
        return None
    return cents if cents >= 0 else None


def _compute_safe_to_spend_snapshot(account: Account, owner_scope: str = "anonymous", now_utc: datetime | None = None) -> dict[str, Any]:
    """Build the canonical household Pay Yourself First snapshot."""
    from services.pyf_financial_state import calculate_pyf_snapshot

    now = now_utc or datetime.now(timezone.utc)
    missing: list[str] = []
    checking_cents = None
    if account is None or account.checking_balance is None:
        missing.append("checking_balance")
    else:
        checking_cents = _money_to_cents(account.checking_balance, field_name="checking_balance")

    pay_period_days = int(getattr(account, "pay_period_days", 0) or 0) if account is not None else 0
    if pay_period_days <= 0:
        missing.append("pay_period_days")

    next_income = _infer_next_income(account, now) if account is not None and pay_period_days > 0 else {"known": False}
    if not next_income.get("known"):
        missing.append("payday")

    period_income_cents = None
    income_plan = resolve_income_plan(account.household_id, at=now) if account is not None else None
    if income_plan is not None:
        period_income_cents = int(income_plan.expected_income_cents)
    elif next_income.get("amount") is not None and float(next_income.get("amount") or 0) > 0:
        period_income_cents = _money_to_cents(next_income["amount"], field_name="period_income")
    else:
        missing.append("current_period_income")

    target_pct = _explicit_household_setting_decimal(PYF_TARGET_SETTING_KEY)
    if target_pct is None or target_pct < 0:
        target_pct = None
        missing.append("long_term_savings_target_percent")

    buffer_cents = _explicit_household_setting_decimal(SAFE_BUFFER_SETTING_KEY)
    if buffer_cents is None or buffer_cents < 0:
        protected_buffer_cents = None
        missing.append("protected_checking_buffer")
    else:
        protected_buffer_cents = _money_to_cents(buffer_cents, field_name="protected_buffer")

    # Required-expense review is a presence/readiness fact, not a financial
    # input. In particular, no rows must never be interpreted as the user
    # having reviewed and confirmed that they have no required expenses.
    # An explicit reviewed-none answer makes missing grocery/fuel components
    # known zero for this snapshot only; it does not synthesize settings,
    # Bills, or transactions.
    required_expense_review = _required_expense_review_state()
    no_expenses_reviewed = required_expense_review == REQUIRED_EXPENSE_NONE
    if required_expense_review in {REQUIRED_EXPENSE_UNANSWERED, REQUIRED_EXPENSE_PENDING}:
        missing.append("required_expenses_review")

    grocery_baseline_cents = _explicit_household_preference_cents("baseline_grocery_cost")
    if grocery_baseline_cents is None:
        if no_expenses_reviewed:
            grocery_baseline_cents = 0
        else:
            missing.append("grocery_need")

    window_end = next_income.get("date") if isinstance(next_income.get("date"), datetime) else None
    if window_end is None and pay_period_days > 0:
        window_end = now + timedelta(days=pay_period_days)

    bills_total_cents = 0
    if window_end is not None:
        bills = _household_bill_query().filter(
            Bill.is_paid == False,
            Bill.is_gas_estimate == False,
            Bill.due_date <= window_end,
        ).all()
        bills_total_cents = sum(_money_to_cents(row.amount, field_name="bill amount") for row in bills)

    fuel_bill = _household_bill_query().filter_by(is_gas_estimate=True, is_paid=False).first()
    if fuel_bill is None:
        if no_expenses_reviewed:
            fuel_cents = 0
        else:
            fuel_cents = None
            missing.append("fuel_or_transport_need")
    else:
        fuel_cents = _money_to_cents(fuel_bill.amount, field_name="fuel need")

    grocery_spend_cents = 0
    if account is not None:
        grocery_spend_cents = _money_to_cents(
            _sum_grocery_spend_for_period(account, now),
            field_name="grocery_spend_to_date",
        )
    grocery_remaining_cents = None if grocery_baseline_cents is None else max(0, grocery_baseline_cents - grocery_spend_cents)

    needs = [
        {"key": "bills", "label": "Bills before payday", "amount_cents": bills_total_cents, "amount": _cents_to_float(bills_total_cents)},
    ]
    if grocery_remaining_cents is not None:
        needs.append({"key": "groceries_remaining", "label": "Required groceries remaining", "amount_cents": grocery_remaining_cents, "amount": _cents_to_float(grocery_remaining_cents)})
    if fuel_cents is not None:
        needs.append({"key": "fuel_transport", "label": "Required fuel / transport", "amount_cents": fuel_cents, "amount": _cents_to_float(fuel_cents)})

    snapshot = calculate_pyf_snapshot(
        checking_cents=checking_cents,
        period_income_cents=period_income_cents,
        savings_target_percent=target_pct,
        protected_buffer_cents=protected_buffer_cents,
        needs=needs,
        missing_setup=missing,
    )
    snapshot["until_payday_days"] = next_income.get("days_until")
    next_date = next_income.get("date")
    snapshot["next_expected_income"] = {
        "known": bool(next_income.get("known")),
        "date": next_date.isoformat() if isinstance(next_date, datetime) else None,
        "date_display": next_date.strftime("%b %d") if isinstance(next_date, datetime) else None,
        "amount": next_income.get("amount"),
        "source": next_income.get("source") or "missing_setup",
    }
    snapshot["freshness"] = _build_freshness(owner_scope)
    snapshot["window_end"] = window_end.isoformat() if isinstance(window_end, datetime) else None
    snapshot["components"] = {
        "usable_money": _cents_to_float(checking_cents) if checking_cents is not None else None,
        "actual_forecast_needs": snapshot.get("needs_total"),
        "bills_before_payday": _cents_to_float(bills_total_cents),
        "bills_before_payday_count": len(bills) if window_end is not None else 0,
        "grocery_commitment_total": _cents_to_float(grocery_baseline_cents) if grocery_baseline_cents is not None else None,
        "grocery_spend_to_date": _cents_to_float(grocery_spend_cents),
        "groceries_remaining": _cents_to_float(grocery_remaining_cents) if grocery_remaining_cents is not None else None,
        "other_committed_spending": _cents_to_float(fuel_cents) if fuel_cents is not None else None,
        "protected_buffer": _cents_to_float(protected_buffer_cents) if protected_buffer_cents is not None else None,
        "grocery_commitment_source": "explicit_onboarding_baseline" if grocery_baseline_cents is not None else "missing_setup",
        "required_expense_review": required_expense_review,
    }
    if snapshot.get("complete"):
        lines = [
            {"key": "checking", "label": "Current checking", "amount_cents": checking_cents},
            {"key": "needs", "label": "Actual / forecast Needs", "amount_cents": -int(snapshot["needs_total_cents"])},
            {"key": "savings", "label": "Current-period savings protection", "amount_cents": -int(snapshot["feasible_savings_cents"])},
            {"key": "buffer", "label": "Protected checking buffer", "amount_cents": -int(protected_buffer_cents)},
            {"key": "safe", "label": "Safe to Spend", "amount_cents": int(snapshot["safe_to_spend_cents"])},
        ]
        snapshot["breakdown"] = {"lines": [{**row, "amount": _cents_to_float(row["amount_cents"])} for row in lines], "checks": {"sum_matches_safe_to_spend": sum(row["amount_cents"] for row in lines[:-1]) == lines[-1]["amount_cents"]}}
        if snapshot["feasibility"] == "partial_target_feasible":
            snapshot["explanation"] = "The full long-term savings target does not fit this period. Rung is preserving Needs and your checking buffer and showing the maximum temporary contribution."
        elif snapshot["feasibility"] == "no_contribution_feasible":
            snapshot["explanation"] = "No savings contribution is feasible this period without using money needed for required expenses or the protected checking buffer."
        else:
            snapshot["explanation"] = "The full long-term savings target is feasible after current Needs and the protected checking buffer."
    else:
        snapshot["breakdown"] = {"lines": [], "checks": {"sum_matches_safe_to_spend": False}}
        snapshot["explanation"] = "Safe-to-Spend needs setup before Rung can calculate it truthfully."
    snapshot["readiness"] = {"ready": bool(snapshot.get("complete")), "safe_to_spend_available": bool(snapshot.get("complete")), "missing_critical": snapshot.get("missing_setup") or [], "setup_gaps": []}
    return snapshot


def _sum_grocery_spend_for_period(account: Account, now_utc: datetime | None = None) -> float:
    now = now_utc or datetime.now(timezone.utc)
    start = now - timedelta(days=int(account.pay_period_days or 14))
    rows = _household_tx_query().filter(
        ExpenseTransaction.category == "grocery",
        ExpenseTransaction.date >= start,
        ExpenseTransaction.date <= now,
    ).all()
    total_cents = sum(_money_to_cents(row.amount, field_name="transaction amount") for row in rows)
    return _cents_to_float(total_cents)


def compute_liquidity_metrics(account):
    """Core Pay Period Liquidity Engine."""
    now_utc = datetime.now(timezone.utc)
    pay_period_days = max(0, int(account.pay_period_days or 0))
    pay_period_end = now_utc + timedelta(days=pay_period_days)
    checking_balance = float(account.checking_balance or 0.0)
    
    # 1. Unpaid bills due within current pay period
    upcoming_bills = _household_bill_query().filter(
        Bill.is_paid == False,
        Bill.due_date <= pay_period_end,
        Bill.is_gas_estimate == False
    ).all()
    bills_total = sum(b.amount for b in upcoming_bills)
    
    # 2. Gas Allocation
    gas_bill = _household_bill_query().filter_by(is_gas_estimate=True, is_paid=False).first()
    gas_allocation = gas_bill.amount if gas_bill else 60.00
    
    # 3. True Disposable Cash
    safe_disposable = checking_balance - bills_total - gas_allocation
    
    # 4. Food Budget Allocation
    food_budget = max(0.0, safe_disposable * (account.food_allocation_pct / 100.0))
    total_meals = pay_period_days * int(account.meals_per_day or 0)
    per_meal_budget = food_budget / total_meals if total_meals > 0 else 0.0
    
    # 5. Grocery spend progress for current pay period
    grocery_spend_to_date = _sum_grocery_spend_for_period(account)
    grocery_budget_remaining = max(0.0, food_budget - grocery_spend_to_date)

    # 6. Non-Food Unallocated Free Cash
    free_cash = safe_disposable - food_budget
    
    safe_snapshot = _compute_safe_to_spend_snapshot(account)

    bootstrap_location = _is_bootstrap_location(account)
    location_zip = "" if bootstrap_location else str(account.zip_code or "").strip()
    location_city_state = "" if bootstrap_location else str(getattr(account, 'city_state', "") or "").strip()

    selected_store = get_selected_store(current_household_id(), account=account)
    return {
        "authority": "legacy_liquidity_compatibility_only",
        "authoritative": False,
        "checking_balance": account.checking_balance,
        "food_allocation_pct": float(account.food_allocation_pct or 0.0),
        "pay_period_days": pay_period_days,
        "meals_per_day": int(account.meals_per_day or 3),
        "expected_paycheck": account.expected_paycheck,
        "vault_balance": account.vault_balance if hasattr(account, 'vault_balance') else 150.00,
        "upcoming_bills_total": round(bills_total, 2),
        "gas_allocation": round(gas_allocation, 2),
        "safe_disposable_cash": round(safe_disposable, 2),
        "food_budget": round(food_budget, 2),
        "grocery_spend_to_date": round(grocery_spend_to_date, 2),
        "grocery_budget_remaining": round(grocery_budget_remaining, 2),
        "total_meals": total_meals,
        "target_per_meal_budget": round(per_meal_budget, 2),
        "free_cash_remaining": round(free_cash, 2),
        "safe_to_spend": safe_snapshot,
        "location": {
            "zip_code": location_zip,
            "city_state": location_city_state,
            "sales_tax_rate": None,
            "sales_tax_pct": None,
            "grocery_tax_rate": None,
            "tax_authority": "canonical_tax_engine_at_purchase",
            "legacy_tax_rates_authoritative": False,
            "store_name": selected_store.get("name") or "",
            "location_id": selected_store.get("store_id") or "",
            "selected_store": selected_store,
            "is_saved": not bootstrap_location,
        }
    }


def _canonical_financial_metrics(account: Account, owner_scope: str = "anonymous") -> dict[str, Any]:
    snapshot = _compute_safe_to_spend_snapshot(account, owner_scope=owner_scope)
    return {
        "authority": "canonical_pyf_v1",
        "checking_balance": round(float(account.checking_balance or 0.0), 2),
        "safe_to_spend": snapshot,
    }


def _resolve_cart_grocery_budget(account: Account, data: dict[str, Any], owner_scope: str) -> tuple[float | None, str, dict[str, Any] | None]:
    """Resolve explicit override or canonical grocery Need remaining."""
    if data.get("budget_limit") is not None:
        try:
            explicit_cents = _money_to_cents(data.get("budget_limit"), field_name="budget_limit")
        except ValueError as exc:
            return None, "explicit_request", {"error": str(exc), "code": "invalid_budget_limit"}
        if explicit_cents < 0:
            return None, "explicit_request", {"error": "budget_limit cannot be negative.", "code": "invalid_budget_limit"}
        return _cents_to_float(explicit_cents), "explicit_request", None

    snapshot = _compute_safe_to_spend_snapshot(account, owner_scope=owner_scope)
    grocery_remaining = (snapshot.get("components") or {}).get("groceries_remaining")
    if not snapshot.get("complete") or grocery_remaining is None:
        return None, "canonical_grocery_need_remaining", {
            "error": "Grocery budget is unavailable until canonical financial and grocery Needs setup is complete.",
            "code": "grocery_budget_setup_required",
            "budget": {
                "available": False,
                "source": "canonical_grocery_need_remaining",
                "grocery_need_remaining": None,
            },
            "missing_setup": snapshot.get("missing_setup") or [],
        }
    return round(float(grocery_remaining), 2), "canonical_grocery_need_remaining", None


def _persist_kroger_store_choice(account: Account, store: Any) -> None:
    store_id = str(getattr(store, "store_id", "") or "").strip()
    store_name = str(getattr(store, "name", "") or "").strip()
    if store_id:
        account.kroger_location_id = store_id
    if store_name:
        account.kroger_store_name = store_name


def _is_bootstrap_location(account: Account) -> bool:
    zip_code = str(account.zip_code or "").strip()
    city_state = str(getattr(account, "city_state", "") or "").strip()
    has_coords = account.latitude is not None and account.longitude is not None
    if has_coords:
        return False
    return zip_code == "65084" and city_state in {"", "Versailles, MO"}


_US_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# Baseline state rates for auto-detected location defaults.
# These are coarse defaults used only when the user asks for automatic
# detection and does not provide explicit tax values.
_STATE_SALES_TAX_DEFAULTS = {
    "AK": 0.0,
    "AL": 0.04,
    "AR": 0.065,
    "AZ": 0.056,
    "CA": 0.0725,
    "CO": 0.029,
    "CT": 0.0635,
    "DC": 0.06,
    "DE": 0.0,
    "FL": 0.06,
    "GA": 0.04,
    "HI": 0.04,
    "IA": 0.06,
    "ID": 0.06,
    "IL": 0.0625,
    "IN": 0.07,
    "KS": 0.065,
    "KY": 0.06,
    "LA": 0.0445,
    "MA": 0.0625,
    "MD": 0.06,
    "ME": 0.055,
    "MI": 0.06,
    "MN": 0.06875,
    "MO": 0.04225,
    "MS": 0.07,
    "NC": 0.0475,
    "ND": 0.05,
    "NE": 0.055,
    "NH": 0.0,
    "NJ": 0.06625,
    "NM": 0.05125,
    "NV": 0.0685,
    "NY": 0.04,
    "OH": 0.0575,
    "OK": 0.045,
    "OR": 0.0,
    "PA": 0.06,
    "RI": 0.07,
    "SC": 0.06,
    "SD": 0.045,
    "TN": 0.07,
    "TX": 0.0625,
    "UT": 0.061,
    "VA": 0.053,
    "VT": 0.06,
    "WA": 0.065,
    "WI": 0.05,
    "WV": 0.06,
    "WY": 0.04,
}

_STATE_GROCERY_TAX_DEFAULTS = {
    # Most states do not tax groceries at the state level.
    "AK": 0.0,
    "AL": 0.03,
    "AR": 0.00125,
    "AZ": 0.0,
    "CA": 0.0,
    "CO": 0.0,
    "CT": 0.0,
    "DC": 0.0,
    "DE": 0.0,
    "FL": 0.0,
    "GA": 0.0,
    "HI": 0.0,
    "IA": 0.0,
    "ID": 0.0,
    "IL": 0.01,
    "IN": 0.0,
    "KS": 0.0,
    "KY": 0.0,
    "LA": 0.0,
    "MA": 0.0,
    "MD": 0.0,
    "ME": 0.0,
    "MI": 0.0,
    "MN": 0.0,
    "MO": 0.01225,
    "MS": 0.07,
    "NC": 0.02,
    "ND": 0.0,
    "NE": 0.0,
    "NH": 0.0,
    "NJ": 0.0,
    "NM": 0.0,
    "NV": 0.0,
    "NY": 0.0,
    "OH": 0.0,
    "OK": 0.0,
    "OR": 0.0,
    "PA": 0.0,
    "RI": 0.0,
    "SC": 0.0,
    "SD": 0.0,
    "TN": 0.04,
    "TX": 0.0,
    "UT": 0.03,
    "VA": 0.015,
    "VT": 0.0,
    "WA": 0.0,
    "WI": 0.0,
    "WV": 0.0,
    "WY": 0.0,
}

# State-level grocery food tax behavior used to translate combined
# location sales tax into grocery tax where possible.
# - exempt: groceries are generally exempt at state+local level
# - full: groceries are generally taxed at full combined rate
# - reduced: groceries are taxed below general sales tax (state-specific)
_STATE_GROCERY_TAX_MODE = {
    "AL": "full",
    "AR": "reduced",
    "HI": "full",
    "ID": "full",
    "IL": "reduced",
    "KS": "full",
    "MO": "reduced",
    "MS": "full",
    "OK": "full",
    "SD": "full",
    "TN": "reduced",
    "UT": "reduced",
    "VA": "reduced",
}


def _normalize_zip_code(raw_zip: Any) -> str:
    value = str(raw_zip or "").strip()
    match = _US_ZIP_RE.search(value)
    return match.group(1) if match else ""


def _estimate_tax_rates_for_state(state_code: str) -> tuple[float, float]:
    code = str(state_code or "").strip().upper()
    sales = _STATE_SALES_TAX_DEFAULTS.get(code, 0.0825)
    grocery = _STATE_GROCERY_TAX_DEFAULTS.get(code, 0.0125)
    return float(sales), float(grocery)


def _coerce_rate(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed < 0:
        return 0.0
    return min(parsed, 0.25)


def _lookup_combined_sales_tax_by_zip(zip_code: str) -> float | None:
    clean_zip = _normalize_zip_code(zip_code)
    if not clean_zip:
        return None
    try:
        dataset = ensure_bootstrap_tax_dataset()
    except Exception:
        return None

    assignment = (
        TaxBoundaryAssignment.query
        .filter_by(dataset_id=dataset.id, geographic_key_type="zip5", geographic_key=clean_zip)
        .order_by(TaxBoundaryAssignment.effective_from.desc())
        .first()
    )
    if assignment is None:
        return None

    rate = (
        TaxRate.query
        .filter_by(
            dataset_id=dataset.id,
            jurisdiction_id=assignment.jurisdiction_id,
            tax_class=TAX_CLASS_GENERAL_MERCHANDISE,
        )
        .order_by(TaxRate.effective_from.desc())
        .first()
    )
    if rate is None:
        return None
    return _coerce_rate((int(rate.rate_basis_points or 0) / 10000.0))


def _state_rate_from_owned_dataset(state_code: str, tax_class: str) -> float | None:
    state = str(state_code or "").strip().upper()
    if len(state) != 2:
        return None
    try:
        dataset = ensure_bootstrap_tax_dataset()
    except Exception:
        return None

    jurisdiction = TaxJurisdiction.query.filter_by(jurisdiction_type="state", canonical_code=f"STATE:{state}").first()
    if jurisdiction is None:
        return None
    rate = (
        TaxRate.query
        .filter_by(
            dataset_id=dataset.id,
            jurisdiction_id=jurisdiction.id,
            tax_class=tax_class,
        )
        .order_by(TaxRate.effective_from.desc())
        .first()
    )
    if rate is None:
        return None
    return _coerce_rate((int(rate.rate_basis_points or 0) / 10000.0))
    return None


def _derive_grocery_rate_from_combined(combined_sales_rate: float, state_code: str) -> float:
    combined = _coerce_rate(combined_sales_rate)
    code = str(state_code or "").strip().upper()
    state_sales = _STATE_SALES_TAX_DEFAULTS.get(code)
    state_grocery = _STATE_GROCERY_TAX_DEFAULTS.get(code)
    mode = _STATE_GROCERY_TAX_MODE.get(code, "exempt")

    if mode == "full":
        return combined
    if mode == "reduced" and state_sales is not None and state_grocery is not None:
        # Preserve local component from combined tax while reducing only the
        # state portion to the state's grocery-food rate.
        local_component = max(0.0, combined - float(state_sales))
        return _coerce_rate(float(state_grocery) + local_component)
    if mode == "reduced" and state_grocery is not None:
        return _coerce_rate(float(state_grocery))
    return 0.0


def _estimate_tax_rates_for_location(zip_code: str, state_code: str) -> tuple[float, float]:
    combined_sales = _lookup_combined_sales_tax_by_zip(zip_code)
    if combined_sales is not None:
        grocery = _derive_grocery_rate_from_combined(combined_sales, state_code)
        return _coerce_rate(combined_sales), _coerce_rate(grocery)
    owned_sales = _state_rate_from_owned_dataset(state_code, TAX_CLASS_GENERAL_MERCHANDISE)
    owned_grocery = _state_rate_from_owned_dataset(state_code, TAX_CLASS_GROCERY_FOOD)
    if owned_sales is not None and owned_grocery is not None:
        return _coerce_rate(owned_sales), _coerce_rate(owned_grocery)
    return _estimate_tax_rates_for_state(state_code)


def _reverse_geocode_us_location(latitude: float, longitude: float) -> dict[str, str]:
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "format": "jsonv2",
                "lat": latitude,
                "lon": longitude,
                "addressdetails": 1,
            },
            headers={
                "User-Agent": "Rung/1.0 (finance-assistant)",
            },
            timeout=6,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        address = payload.get("address") or {}
        zip_code = _normalize_zip_code(address.get("postcode"))
        state_code = str(address.get("state_code") or "").strip().upper()
        city = (
            str(address.get("city") or "").strip()
            or str(address.get("town") or "").strip()
            or str(address.get("village") or "").strip()
            or str(address.get("hamlet") or "").strip()
        )
        city_state = ", ".join(part for part in [city, state_code] if part)
        return {
            "zip_code": zip_code,
            "state_code": state_code,
            "city_state": city_state,
        }
    except Exception:
        return {}


def _store_city_from_address(address: str) -> str:
    text = str(address or "").strip()
    if not text:
        return ""
    pieces = [part.strip() for part in text.split(",") if part.strip()]
    if len(pieces) >= 2:
        return pieces[-2]
    return ""


def _store_choice_row(retailer: str, store: Any) -> dict[str, Any]:
    address = str(getattr(store, "address", "") or "").strip()
    postal_code = str(getattr(store, "postal_code", "") or "").strip()
    return {
        "retailer": retailer,
        "retailer_display": "Walmart" if retailer == "walmart" else "Kroger",
        "store_id": str(getattr(store, "store_id", "") or "").strip(),
        "name": str(getattr(store, "name", "") or "").strip() or ("Walmart" if retailer == "walmart" else "Kroger"),
        "address": address,
        "city": _store_city_from_address(address),
        "postal_code": postal_code,
        "distance_miles": None,
        "pickup_supported": None,
        "verified": bool(getattr(store, "verified", False)),
    }


def _append_location_diag(event: dict[str, Any]) -> None:
    """Write safe nearby-store diagnostics for runtime troubleshooting.

    Never include secrets, auth headers, tokens, or raw provider payloads.
    """
    try:
        line = json.dumps(event, separators=(",", ":"), sort_keys=True)
        with open("/tmp/rung_location_nearby_diag.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        LOGGER.exception("failed to append nearby-store diagnostics")


def _discover_supported_stores(*, zip_code: str = "", latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
    from services.retail.router import get_retail_provider

    resolved_zip = _normalize_zip_code(zip_code)
    resolved_city_state = ""
    resolved_state_code = ""

    if (not resolved_zip) and latitude is not None and longitude is not None:
        reverse_geo = _reverse_geocode_us_location(latitude, longitude)
        resolved_zip = _normalize_zip_code(reverse_geo.get("zip_code"))
        resolved_city_state = str(reverse_geo.get("city_state") or "").strip()
        resolved_state_code = str(reverse_geo.get("state_code") or "").strip().upper()

    if not resolved_zip:
        return {
            "status": "current_location_unavailable",
            "user_message": "We couldn't determine your current location. Try again or enter your ZIP code.",
            "stores": [],
            "zip_code": "",
            "city_state": resolved_city_state,
            "state_code": resolved_state_code,
        }

    stores: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    provider_success_count = 0
    provider_failure_count = 0
    provider_results: list[dict[str, Any]] = []

    for retailer in ("walmart", "kroger"):
        try:
            provider = get_retail_provider(retailer)
            candidates = provider.find_stores(postal_code=resolved_zip)
            provider_success_count += 1
            provider_results.append({
                "retailer": retailer,
                "success": True,
                "error_type": "",
                "stores_count": len(candidates or []),
            })
            for store in candidates:
                store_id = str(getattr(store, "store_id", "") or "").strip()
                if not store_id:
                    continue
                key = (retailer, store_id)
                if key in seen:
                    continue
                seen.add(key)
                stores.append(_store_choice_row(retailer, store))
        except Exception as exc:
            LOGGER.exception("nearby store lookup failed for retailer=%s zip=%s", retailer, resolved_zip)
            provider_failure_count += 1
            provider_results.append({
                "retailer": retailer,
                "success": False,
                "error_type": type(exc).__name__,
                "stores_count": 0,
            })

    stores = sorted(stores, key=lambda row: (str(row.get("retailer") or ""), str(row.get("name") or ""), str(row.get("store_id") or "")))
    if stores:
        return {
            "status": "ok",
            "user_message": "",
            "stores": stores,
            "zip_code": resolved_zip,
            "city_state": resolved_city_state,
            "state_code": resolved_state_code,
            "provider_results": provider_results,
        }

    if provider_success_count > 0:
        return {
            "status": "no_supported_store",
            "user_message": "We found your location, but couldn't find a supported store nearby.",
            "stores": [],
            "zip_code": resolved_zip,
            "city_state": resolved_city_state,
            "state_code": resolved_state_code,
            "provider_results": provider_results,
        }

    if provider_failure_count > 0:
        return {
            "status": "store_search_unavailable",
            "user_message": "We found your location, but store search isn't available right now. Try again shortly.",
            "stores": [],
            "zip_code": resolved_zip,
            "city_state": resolved_city_state,
            "state_code": resolved_state_code,
            "provider_results": provider_results,
        }

    return {
        "status": "no_supported_store",
        "user_message": "We found your location, but couldn't find a supported store nearby.",
        "stores": [],
        "zip_code": resolved_zip,
        "city_state": resolved_city_state,
        "state_code": resolved_state_code,
        "provider_results": provider_results,
    }


def _selected_store_payload(account: Account) -> dict[str, Any]:
    selected = get_selected_store(current_household_id(), account=account)
    selected["city_state"] = ", ".join(filter(None, [selected.get("city"), selected.get("state")]))
    return selected


def _resolve_kroger_store_selection(account: Account, requested_store_name: str = "") -> dict[str, Any]:
    from services.retail.base import RetailStore
    from services.retail.router import get_retail_provider

    selected = get_selected_store(current_household_id(), account=account)
    saved_location_id = str(selected.get("store_id") or "").strip()
    saved_store_name = str(selected.get("name") or requested_store_name or "Kroger").strip() or "Kroger"
    postal_code = str(account.zip_code or "").strip()

    if saved_location_id:
        return {
            "state": "ready",
            "store": RetailStore(
                store_id=saved_location_id,
                name=saved_store_name,
                address=None,
                postal_code=postal_code or None,
                verified=True,
            ),
            "stores": [],
            "persisted": False,
        }

    if not postal_code:
        return {
            "state": "none",
            "message": "Save a ZIP code first so Rung can find nearby Kroger-family stores.",
        }

    provider = get_retail_provider("kroger")
    stores = provider.find_stores(postal_code=postal_code)

    unique_stores = []
    seen_ids = set()
    for store in stores:
        store_id = str(getattr(store, "store_id", "") or "").strip()
        if not store_id or store_id in seen_ids:
            continue
        seen_ids.add(store_id)
        unique_stores.append(store)

    if not unique_stores:
        return {
            "state": "none",
            "message": f"No Kroger-family stores were found near ZIP {postal_code}.",
        }

    return {
        "state": "choice",
        "message": f"Choose a Kroger-family store for ZIP {postal_code}.",
        "store_choice": {
            "retailer": "kroger",
            "zip_code": postal_code,
            "selected_store_name": saved_store_name,
            "stores": [store.to_dict() for store in unique_stores],
        },
    }


@app.route("/api/location/nearby-stores", methods=["POST"])
def location_nearby_stores():
    account = _household_account()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    data: dict[str, Any] = request.json or {}
    auto_detect = bool(data.get("auto_detect"))
    zip_code = _normalize_zip_code(data.get("zip_code"))

    latitude = None
    longitude = None
    if auto_detect:
        try:
            latitude = float(str(data.get("latitude", "")).strip())
            longitude = float(str(data.get("longitude", "")).strip())
        except (TypeError, ValueError):
            return jsonify({
                "status": "current_location_unavailable",
                "user_message": "We couldn't determine your current location. Try again or enter your ZIP code.",
            }), 400
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return jsonify({
                "status": "current_location_unavailable",
                "user_message": "We couldn't determine your current location. Try again or enter your ZIP code.",
            }), 400
    elif not zip_code:
        return jsonify({
            "status": "invalid_zip_code",
            "user_message": "We couldn't save that location. Please check the ZIP code and try again.",
        }), 400

    lookup = _discover_supported_stores(zip_code=zip_code, latitude=latitude, longitude=longitude)
    status = str(lookup.get("status") or "")
    selected = _selected_store_payload(account)
    selected_zip = str(selected.get("postal_code") or "").strip()
    detected_zip = str(lookup.get("zip_code") or "").strip()
    new_area_detected = bool(selected_zip and detected_zip and selected_zip != detected_zip)

    code = 200
    if status in {"current_location_unavailable", "invalid_zip_code"}:
        code = 422
    elif status == "store_search_unavailable":
        code = 503

    # Safe diagnostics for owner beta runtime troubleshooting.
    _append_location_diag({
        "event": "nearby_stores",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "auth_required_mode": bool(auth_required_mode()),
        "input": {
            "auto_detect": auto_detect,
            "zip_code": zip_code,
            "latitude_rounded": (round(float(latitude), 4) if latitude is not None else None),
            "longitude_rounded": (round(float(longitude), 4) if longitude is not None else None),
        },
        "resolved": {
            "zip_code": str(lookup.get("zip_code") or ""),
            "city_state": str(lookup.get("city_state") or ""),
            "state_code": str(lookup.get("state_code") or ""),
        },
        "providers": lookup.get("provider_results") or [],
        "result": {
            "status": status,
            "http_code": code,
            "stores_total": len(lookup.get("stores") or []),
            "reason": (
                "provider_success_zero_and_failures_present"
                if status == "store_search_unavailable"
                else ("no_supported_stores" if status == "no_supported_store" else "ok_or_other")
            ),
        },
    })

    return jsonify({
        "status": status,
        "user_message": str(lookup.get("user_message") or ""),
        "location": {
            "zip_code": detected_zip,
            "city_state": str(lookup.get("city_state") or "").strip(),
            "source": "gps" if auto_detect else "zip",
        },
        "stores": lookup.get("stores") or [],
        "selected_store": selected,
        "new_area_detected": new_area_detected,
    }), code


@app.route("/api/location/select-store", methods=["POST"])
def location_select_store():
    account = _household_account()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    data: dict[str, Any] = request.json or {}
    retailer = str(data.get("retailer") or "").strip().lower()
    if retailer not in {"walmart", "kroger"}:
        return jsonify({
            "error": "invalid_retailer",
            "user_message": "Please choose a supported store.",
        }), 400

    location_id = str(data.get("store_id") or "").strip()
    store_name = str(data.get("store_name") or "").strip()
    if not location_id:
        return jsonify({
            "error": "invalid_store",
            "user_message": "Please choose a supported store.",
        }), 400

    # Legacy location controls remain discovery/initial-selection compatible,
    # but may not bypass Store Change Review after a cart exists.
    from services.authoritative_cart import current_cart
    existing_cart = current_cart(current_household_id())
    selected_now = get_selected_store(current_household_id(), account=account)
    if existing_cart is not None and (
        str(selected_now.get("retailer") or "").lower() != retailer
        or str(selected_now.get("store_id") or "") != location_id
    ):
        return jsonify({
            "error": "store_change_review_required",
            "user_message": "Review the Store Change in Shopping before changing your active store.",
        }), 409

    zip_code = _normalize_zip_code(data.get("zip_code"))
    city_state = str(data.get("city_state") or "").strip()
    state_code = ""
    if city_state and "," in city_state:
        state_code = city_state.rsplit(",", 1)[-1].strip().upper()

    store_address = str(data.get("store_address") or "").strip()
    store_city = str(data.get("city") or "").strip()
    if not store_city and city_state:
        store_city = city_state.rsplit(",", 1)[0].strip()
    selected = select_store(
        current_household_id(),
        retailer=retailer,
        store_id=location_id,
        store_name=store_name or ("Walmart" if retailer == "walmart" else "Kroger"),
        address=store_address,
        city=store_city,
        state=state_code,
        postal_code=zip_code,
        account=account,
    )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({
            "error": "location_save_failed",
            "user_message": "We couldn't save that location. Please check the ZIP code and try again.",
        }), 500

    return jsonify({
        "status": "store_selected",
        "user_message": "Store saved.",
        "location": {
            "zip_code": account.zip_code or "",
            "city_state": account.city_state or "",
            "tax_authority": "canonical_tax_engine_at_purchase",
            "store_name": selected["name"],
            "location_id": selected["store_id"],
            "is_saved": True,
        },
        "store": {
            "found": True,
            "status": "resolved",
            "retailer": retailer,
            "name": selected["name"],
            "location_id": selected["store_id"],
            "selected_store": selected,
        },
    })


@app.route("/api/location/area-check", methods=["POST"])
def location_area_check():
    account = _household_account()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    data = request.json or {}
    try:
        latitude = float(str(data.get("latitude", "")).strip())
        longitude = float(str(data.get("longitude", "")).strip())
    except (TypeError, ValueError):
        return jsonify({
            "status": "current_location_unavailable",
            "user_message": "We couldn't determine your current location. Try again or enter your ZIP code.",
        }), 400

    reverse_geo = _reverse_geocode_us_location(latitude, longitude)
    detected_zip = _normalize_zip_code(reverse_geo.get("zip_code"))
    if not detected_zip:
        return jsonify({
            "status": "current_location_unavailable",
            "user_message": "We couldn't determine your current location. Try again or enter your ZIP code.",
        }), 422

    current_zip = _normalize_zip_code(account.zip_code or "")
    new_area_detected = bool(current_zip and current_zip != detected_zip)
    return jsonify({
        "status": "ok",
        "detected_zip": detected_zip,
        "current_zip": current_zip,
        "new_area_detected": new_area_detected,
        "user_message": "Looks like you're in a new area. See stores near you?" if new_area_detected else "",
    })

# =============================================================================
# API ROUTES
# =============================================================================

_AUTH_EXEMPT_API_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/session",
}


@app.before_request
def enforce_authentication_boundary():
    if not auth_required_mode():
        return None
    if request.method == "OPTIONS":
        return None
    if not str(request.path or "").startswith("/api/"):
        return None
    if str(request.path) in _AUTH_EXEMPT_API_PATHS:
        return None
    if get_current_principal() is None:
        return jsonify({"error": "Authentication required."}), 401
    return None


@app.route("/api/auth/session", methods=["GET"])
def auth_session_current():
    return jsonify(_current_auth_session_payload())


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    invalid = {"error": "Invalid credentials."}

    if not email or not password:
        return jsonify(invalid), 401

    blocked, retry_after = login_is_blocked(email)
    if blocked:
        return jsonify({"error": "Invalid credentials.", "retry_after_seconds": retry_after}), 429

    user = User.query.filter(db.func.lower(User.email) == email).first()
    membership = None
    if user is not None and bool(user.active):
        membership = HouseholdMembership.query.filter_by(user_id=user.id, active=True).first()

    valid = (
        user is not None
        and bool(user.active)
        and membership is not None
        and bool(check_password_hash(str(user.password_hash), password))
    )
    if not valid:
        record_login_failure(email)
        clear_session()
        return jsonify(invalid), 401

    if user is None:
        record_login_failure(email)
        clear_session()
        return jsonify(invalid), 401
    clear_login_failures(email)
    establish_session(user)
    return jsonify(_current_auth_session_payload())


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    clear_session()
    return jsonify({"authenticated": False})

@app.route("/")
def index():
    """Serves the main single-page UI from templates/index.html"""
    return render_template("index.html")

# ----- RECIPES CRUD ----------------------------------------------------------

@app.route("/api/recipes", methods=["GET", "POST"])
def recipes_crud():
    if request.method == "POST":
        data = request.json or {}
        title = data.get("title", "").strip()
        if not title:
            return jsonify({"error": "Title required"}), 400
        servings = int(data.get("servings", 4))
        instructions = data.get("instructions", "")
        # Scope and owner are server authority; never accept client-provided
        # scope/household fields for ordinary recipe creation.
        r = Recipe(
            title=title,
            servings=servings,
            instructions=instructions,
            recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE,
            household_id=current_household_id(),
        )
        db.session.add(r)
        db.session.flush()
        # Parse ingredients from lines or structured list
        ingredients = data.get("ingredients", [])
        for ing in ingredients:
            parsed = coerce_recipe_ingredient(ing)
            if not parsed:
                continue
            product_name = parsed["product_name"]
            ri = RecipeIngredient(
                recipe_id=r.id,
                product_name=product_name,
                clean_keyword=parsed.get("clean_keyword") or _derive_clean_keyword(product_name),
                quantity=parsed.get("quantity"),
                unit=parsed.get("unit"),
            )
            db.session.add(ri)
        db.session.commit()
        return jsonify({"message": "Recipe added", "id": r.id})
    return jsonify([_serialize_recipe(r) for r in visible_recipe_query(current_household_id()).all()])

@app.route("/api/recipes/<int:rid>", methods=["DELETE"])
def delete_recipe(rid):
    r = mutable_private_recipe_by_id(current_household_id(), rid)
    if not r:
        return jsonify({"error": "Recipe not found"}), 404
    # Deleting an active private recipe would require an explicit product
    # lifecycle decision.  Do not silently cascade a plan/history mutation.
    if _household_meal_plan_query().filter_by(recipe_id=r.id).first():
        return jsonify({"error": "Remove this recipe from the current plan before deleting it."}), 409
    r.tombstoned_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"message": f"Recipe {rid} deleted"})

# ---------------------------------------------------------------------------
# In-memory cache for recipe imports (URL → (timestamp, result_dict)).
# Avoids re-scraping the same URL within TTL seconds. Shared across
# requests in the same process.
# ---------------------------------------------------------------------------
_import_cache: dict = {}    # url → {"ts": float, "data": dict}
_IMPORT_CACHE_TTL = 300     # seconds (5 min)


def _parse_recipe_yields(yields_str):
    """Extract a numeric serving count from a yields string.

    Examples
    --------
    "4 servings"    → 4
    "1 serving"     → 1
    "6-8 servings"  → 6
    "2 dozen"       → 2
    "About 4 cups"  → 4
    ""              → 4   (default fallback)
    "serves 10"     → 10
    None            → 4
    "N/A"           → 4
    """
    import re
    if not yields_str:
        return 4
    s = str(yields_str).strip().lower()
    # Range: "6-8 servings" → 6  (must come before the general digit pattern)
    m = re.search(r'(\d+)\s*[-–]\s*\d+\s*(?:servings?|dozen|pieces?|slices?|cups?|cookies?)', s)
    if m:
        return int(m.group(1))
    # Explicit pattern: digits before serving/dozen/etc.
    m = re.search(r'(\d+)\s*(?:servings?|dozen|pieces?|slices?|cups?|muffins?|cookies?|bars?|rolls?|patties?|meatballs?)', s)
    if m:
        return int(m.group(1))
    # "serves N" or "makes N"
    m = re.search(r'(?:serves|makes)\s+(\d+)', s)
    if m:
        return int(m.group(1))
    # First number in the string (but not a fraction like "1/2")
    m = re.search(r'\b(\d+)\b', s)
    if m:
        return int(m.group(1))
    return 4


def _derive_clean_keyword(product_name):
    """Derive a clean keyword from a product name for pantry matching.

    Strips quantities, units, qualifiers, and noise words, then returns
    the last meaningful word(s) lowercased with underscores.

    Examples
    --------
    "2 cups all-purpose flour"         → "flour"
    "½ teaspoon extra-virgin olive oil" → "olive_oil"
    "Sea salt"                         → "salt"
    "Crumbled feta cheese"             → "feta_cheese"
    "1 lb boneless skinless chicken breast" → "chicken_breast"
    "Chopped chives"                   → "chives"
    "2 eggs, per small ramekin"        → "eggs"
    "Olive oil, for drizzling"         → "olive_oil"
    """
    import re
    s = product_name.strip().lower()
    # Strip leading quantity + unit + fraction (e.g. "1 lb", "½ teaspoon", "2 cups")
    s = re.sub(r'^[\d\s¼½¾⅓⅔⅛⅜⅝⅞/.\-–]+\s*', '', s)

    # Pass 1: Strip measurement / container words from the START only.
    # (cup, oz, lb, gram, can, bottle, etc. — these are units, not food names)
    _UNIT_RE = re.compile(
        r'^(cup|teaspoon|tablespoon|tbsp|tsp|oz|ounce|lb|lbs|pound|'
        r'can|clove|head|stalk|sprig|pinch|dash|bunch|piece|slice|'
        r'jar|bottle|bag|box|package|pack|g|gram|kg|ml|l|liter|'
        r'quart|pint|gallon|dozen)s?\s+'
    )
    while True:
        s2 = _UNIT_RE.sub('', s, count=1)
        if s2 == s:
            break
        s = s2

    # Pass 2: Strip descriptor / preparation words from ANYWHERE in the
    # string (not just the start).  Fixes cases like
    #   "mature cheddar finely grated" → "mature cheddar" → "cheddar"
    # where "finely" and "grated" are mid-string modifiers that the
    # old ^-anchored regex never saw.
    _DESC_RE = re.compile(
        r'\b(whole|large|small|medium|'
        r'fresh|frozen|dried|chopped|minced|diced|sliced|crushed|'
        r'grated|shredded|ground|boneless|skinless|trimmed|cooked|'
        r'uncooked|raw|ripe|organic|sea|all.purpose|crumbled|sautéed|'
        r'roasted|toasted|peeled|seeded|cored|halved|quartered|'
        r'thinly|finely|coarsely|roughly|kosher)s?\b\s*'
    )
    while True:
        s2 = _DESC_RE.sub('', s, count=1)
        s2 = re.sub(r'\s+', ' ', s2).strip()
        if s2 == s:
            break
        s = s2
    # Strip trailing serving instructions: ", per ...", ", for ...", ", to ...", ", or ..."
    s = re.sub(r',\s*(?:per\s+\w+(?:\s+\w+)*|for\s+\w+(?:\s+\w+)*|to\s+\w+(?:\s+\w+)*|or\s+\w+(?:\s+\w+)*|as\s+\w+(?:\s+\w+)*)', '', s)
    # Strip trailing single-word descriptors: ", thawed", ", rinsed", ", drained", etc.
    s = re.sub(r',\s+\w+$', '', s)
    # Strip parenthetical notes
    s = re.sub(r'\([^)]*\)', '', s)
    # Strip "to taste", "for garnish", "optional", "divided", etc.
    s = re.sub(r',?\s*(?:to taste|for garnish|for serving|or as needed|as needed|optional|divided|or more|more if desired).*', '', s)
    s = s.strip().strip(',').strip()
    if not s:
        return product_name.strip().lower().replace(' ', '_')
    # Take last 2-3 words as the core ingredient name
    words = s.split()
    if len(words) <= 2:
        core = ' '.join(words)
    else:
        core = ' '.join(words[-2:])
    # Clean: lowercase, replace non-alphanumeric with underscore, collapse runs
    core = re.sub(r'[^a-z0-9]+', '_', core.lower())
    core = re.sub(r'_+', '_', core).strip('_')
    # Strip leading connectors that the last-2-words heuristic can pick up,
    # e.g. "salt and pepper" → last-2 = "and pepper" → "and_pepper" → "pepper"
    core = re.sub(r'^(and|or|with|plus|of|for|in|a|an|the)_+', '', core)
    if not core:
        core = re.sub(r'[^a-z0-9]+', '_', words[-1].lower()).strip('_')
    return core


def _scrape_with_curl_cffi(url):
    """Fallback scraper for Cloudflare-protected sites.

    Uses ``curl_cffi`` to impersonate a Chrome browser's TLS
    fingerprint, fetches the page HTML, and extracts schema.org
    Recipe data from embedded JSON-LD ``<script>`` tags.

    Returns a dict with the same shape as recipe-scrapers output,
    or ``None`` if no Recipe data could be extracted.
    """
    import re
    import json as _json
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None

    try:
        resp = cffi_requests.get(url, impersonate='chrome120', timeout=15)
        if resp.status_code != 200:
            return None
        html = resp.text
    except Exception:
        return None

    # ---- Extract JSON-LD blocks ----
    ld_blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    for block in ld_blocks:
        try:
            data = _json.loads(block)
        except (_json.JSONDecodeError, ValueError):
            continue

        # JSON-LD can be a single object or a list of objects.
        items = data if isinstance(data, list) else [data]
        for item in items:
            types = item.get('@type', [])
            if not isinstance(types, list):
                types = [types]
            if 'Recipe' not in types:
                continue

            # ---- Found a Recipe object ----
            title = item.get('headline') or item.get('name', 'Imported Recipe')

            # Ingredients
            raw_ingredients = item.get('recipeIngredient', [])
            if isinstance(raw_ingredients, str):
                raw_ingredients = [raw_ingredients]

            # Instructions
            instructions = item.get('recipeInstructions', '')
            if isinstance(instructions, list):
                # List of HowToStep objects
                steps = []
                for step in instructions:
                    if isinstance(step, dict):
                        steps.append(step.get('text', str(step)))
                    elif isinstance(step, str):
                        steps.append(step)
                instructions = '\n'.join(steps)
            elif isinstance(instructions, dict):
                instructions = instructions.get('text', str(instructions))

            # Total time: prefer totalTime, fall back to cookTime + prepTime
            total_time = None
            for key in ('totalTime', 'cookTime', 'prepTime'):
                raw = item.get(key)
                if raw and isinstance(raw, str) and raw.startswith('PT'):
                    # ISO 8601 duration: PT20M → 20, PT1H30M → 90
                    h = re.search(r'(\d+)H', raw)
                    m = re.search(r'(\d+)M', raw)
                    mins = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
                    if mins > 0:
                        total_time = mins
                        break

            # Servings / yield
            yields_val = item.get('recipeYield')
            if isinstance(yields_val, list):
                yields_val = yields_val[0] if yields_val else None
            yields_str = str(yields_val) if yields_val else None

            # Image
            image_obj = item.get('image')
            if isinstance(image_obj, dict):
                image_url = image_obj.get('url')
            elif isinstance(image_obj, list) and image_obj:
                first_img = image_obj[0]
                image_url = first_img.get('url') if isinstance(first_img, dict) else str(first_img)
            else:
                image_url = str(image_obj) if image_obj else None

            return {
                'title': title,
                'total_time': total_time,
                'yields_str': yields_str,
                'instructions': instructions,
                'image_url': image_url,
                'raw_ingredients': raw_ingredients,
                'source': 'json-ld',
            }

    return None  # No Recipe found in any JSON-LD block


@app.route("/api/recipes/import", methods=["POST"])
def import_recipe():
    """Scrape a recipe from a URL and persist it to the local database.

    Uses ``recipe-scrapers`` to extract title, total time, servings,
    ingredients, instructions, and image URL. Falls back to
    ``curl_cffi`` + JSON-LD parsing for sites with Cloudflare
    protection (e.g. allrecipes.com, seriouseats.com).

    Results are cached in memory for 5 minutes.

    Request body
    ------------
    {"url": "https://..."}

    Returns (200 on success)
    ------------------------
    {
      "recipe": {...},
      "action": "created" | "updated",
      "cache": {"status": "ok", ...}
    }
    """
    import time
    global _import_cache

    data = request.json or {}
    url = (data.get("url", "") or "").strip()
    if not url:
        return jsonify({"error": "URL required", "cache": {"status": "error", "hit": False}}), 400

    # ---- Cache check ----
    now = time.time()
    if url in _import_cache:
        entry = _import_cache[url]
        age = now - entry["ts"]
        if age < _IMPORT_CACHE_TTL:
            cached = dict(entry["data"])  # shallow copy
            cached["cache"] = {"status": "ok", "hit": True, "fresh": True, "age_seconds": int(age)}
            # Fix action: if this URL is already in the DB, it's an update, not a create.
            if Recipe.query.filter_by(source_url=url, household_id=current_household_id(), recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE, tombstoned_at=None).first():
                cached["action"] = "updated"
            return jsonify(cached)
        else:
            del _import_cache[url]

    # ---- Scrape (primary: recipe-scrapers) ----
    try:
        from recipe_scrapers import scrape_me
    except ImportError:
        return jsonify({
            "error": "Recipe import requires the recipe-scrapers library. Install with: pip install recipe-scrapers",
            "cache": {"status": "error", "hit": False}
        }), 501

    scraper_error = None
    scraper = None
    try:
        scraper = scrape_me(url)
    except Exception as exc:
        scraper_error = exc

    if scraper_error is not None:
        # ---- Fallback: curl_cffi + JSON-LD for Cloudflare-blocked sites ----
        fallback = _scrape_with_curl_cffi(url)
        if fallback:
            title = fallback['title']
            total_time = fallback['total_time']
            yields_str = fallback['yields_str']
            instructions = fallback['instructions']
            image_url = fallback['image_url']
            raw_ingredients = fallback['raw_ingredients']
        else:
            return jsonify({
                "error": "Could not scrape that URL.",
                "detail": f"{type(scraper_error).__name__}: {scraper_error}",
                "url": url,
                "cache": {"status": "error", "hit": False, "error_detail": str(scraper_error)[:200]}
            }), 500
    else:
        if scraper is None:
            return jsonify({
                "error": "Could not scrape that URL.",
                "detail": "Recipe scraper returned no result.",
                "url": url,
                "cache": {"status": "error", "hit": False, "error_detail": "empty_scraper"}
            }), 500
        # recipe-scrapers succeeded — extract normally
        try:
            title = scraper.title()
        except Exception:
            title = "Imported Recipe"

        try:
            total_time = scraper.total_time()
        except Exception:
            total_time = None

        try:
            yields_str = scraper.yields()
        except Exception:
            yields_str = "4 servings"

        try:
            instructions = scraper.instructions()
        except Exception:
            instructions = ""

        try:
            image_url = scraper.image() if hasattr(scraper, 'image') else None
        except Exception:
            image_url = None

        try:
            raw_ingredients = scraper.ingredients()
        except Exception:
            raw_ingredients = []

    # ---- Shared: parse servings ----
    servings = _parse_recipe_yields(yields_str)

    # ---- Persist to database (upsert by source_url) ----
    existing = Recipe.query.filter_by(
        source_url=url, household_id=current_household_id(), recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE,
        tombstoned_at=None,
    ).first()
    action = "created"

    if existing:
        # Update the existing recipe with fresh scraped data.
        existing.title = title
        existing.servings = servings
        existing.instructions = instructions
        # Replace old ingredients with new ones.
        RecipeIngredient.query.filter_by(recipe_id=existing.id).delete()
        recipe = existing
        action = "updated"
    else:
        recipe = Recipe(
            title=title,
            servings=servings,
            estimated_cost_per_serving=3.50,
            source_url=url,
            instructions=instructions,
            recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE,
            household_id=current_household_id(),
        )
        db.session.add(recipe)
        db.session.flush()  # get recipe.id

    for ing_str in raw_ingredients:
        parsed = coerce_recipe_ingredient(ing_str)
        if not parsed:
            continue
        product_name = parsed["product_name"]
        ri = RecipeIngredient(
            recipe_id=recipe.id,
            product_name=product_name,
            clean_keyword=parsed.get("clean_keyword") or _derive_clean_keyword(product_name),
            quantity=parsed.get("quantity"),
            unit=parsed.get("unit"),
        )
        db.session.add(ri)

    db.session.commit()

    # ---- Build response ----
    result = {
        "recipe": {
            "id": recipe.id,
            "title": title,
            "servings": yields_str,
            "total_time": total_time,
            "source_url": url,
            "image_url": image_url,
            "instructions": instructions
        },
        "action": action,
    }

    # Store in cache — include action so cache hits reflect it too.
    _import_cache[url] = {"ts": now, "data": dict(result)}

    result["cache"] = {"status": "ok", "hit": False, "age_seconds": 0}
    return jsonify(result)

@app.route("/api/recipes/generate", methods=["POST"])
def generate_recipes():
    """Generate a meal plan from selected recipe IDs and return a cart.

    Also serves the Grocery tab's Active Recipes expander — recipe
    objects include full ingredient lists so the frontend can render
    per-recipe ingredient bullets without an extra round-trip.
    """
    data = request.json or {}
    recipe_ids = data.get("recipe_ids", [])
    if not recipe_ids or not isinstance(recipe_ids, list):
        return jsonify({"error": "Provide recipe_ids (list of int)"}), 400
    recipes = visible_recipe_query(current_household_id()).filter(Recipe.id.in_(recipe_ids)).all()
    if len({int(r.id) for r in recipes}) != len({int(rid) for rid in recipe_ids if str(rid).strip().isdigit()}):
        return jsonify({"error": "Recipe not found"}), 404
    return jsonify({
        "recipes": [{
            "id": r.id,
            "title": r.title,
            "servings": r.servings,
            "source_url": r.source_url or "",
            "estimated_cost_per_serving": r.estimated_cost_per_serving,
            "ingredients": [_serialize_recipe_ingredient(i) for i in r.ingredients]
        } for r in recipes],
        "total_meals": len(recipes)
    })

# ----- ACTIVE PAY-PERIOD MEAL PLAN -------------------------------------------

MEAL_PLAN_MAX = 14  # cap: Active Pay-Period Recipes expander shows at most 14


def _serialize_meal_plan():
    """Serialize the persisted meal plan (ordered by insertion)."""
    items = _household_meal_plan_query().order_by(MealPlanItem.created_at.asc()).all()
    ids = [m.recipe_id for m in items]
    recipes = []
    by_id = {r.id: r for r in visible_recipe_query(current_household_id()).filter(Recipe.id.in_(ids)).all()} if ids else {}
    for rid in ids:
        r = by_id.get(rid)
        if r:
            recipes.append(_serialize_recipe(r))
    return {
        "recipe_ids": ids,
        "recipes": recipes,
        "count": len(ids),
        "max": MEAL_PLAN_MAX,
    }


@app.route("/api/meal-plan", methods=["GET", "POST"])
def meal_plan():
    """Persisted active pay-period meal plan (server-side source of truth).

    GET
        Returns ``{recipe_ids, recipes, count, max}`` where each recipe
        carries full ingredient data for the Grocery tab expander.

    POST
        Body supports:
          ``{"recipe_ids": [1, 2, 3]}``  — replace the whole plan (capped)
          ``{"add": [4], "remove": [5]}`` — incremental updates

    Returns the updated plan with the same shape as GET.
    """
    if request.method == "GET":
        return jsonify(_serialize_meal_plan())

    if not _current_meal_plan_cycle().get("key"):
        return jsonify({"error": "Complete pay-cycle setup before changing this pay-period plan."}), 409

    data = request.json or {}
    if data.get("recipe_ids") is not None:
        # Replace semantics: wipe existing plan, insert new IDs in order.
        ids = data["recipe_ids"]
        if not isinstance(ids, list):
            return jsonify({"error": "recipe_ids must be a list"}), 400
        validated = []
        for rid in ids[:MEAL_PLAN_MAX]:
            try:
                rid = int(rid)
            except (ValueError, TypeError):
                continue
            if visible_recipe_by_id(current_household_id(), rid) is None:
                return jsonify({"error": "Recipe not found"}), 404
            if rid not in validated:
                validated.append(rid)
        _household_meal_plan_query().delete()
        for rid in validated:
            db.session.add(_new_current_meal_plan_item(rid, "user"))
        db.session.commit()
        return jsonify(_serialize_meal_plan())

    add_ids = data.get("add") or []
    remove_ids = data.get("remove") or []
    for rid in remove_ids:
        try:
            rid = int(rid)
        except (ValueError, TypeError):
            continue
        _household_meal_plan_query().filter_by(recipe_id=rid).delete()
    for rid in add_ids:
        try:
            rid = int(rid)
        except (ValueError, TypeError):
            continue
        if visible_recipe_by_id(current_household_id(), rid) is None:
            return jsonify({"error": "Recipe not found"}), 404
        if _household_meal_plan_query().filter_by(recipe_id=rid).first():
            continue
        if _household_meal_plan_query().count() >= MEAL_PLAN_MAX:
            break
        db.session.add(_new_current_meal_plan_item(rid, "user"))
    db.session.commit()
    return jsonify(_serialize_meal_plan())


@app.route("/api/meal-plan/clear", methods=["POST"])
def clear_meal_plan():
    """Empty the active meal plan (start a new pay period)."""
    if not _current_meal_plan_cycle().get("key"):
        return jsonify({"error": "Complete pay-cycle setup before changing this pay-period plan."}), 409
    _household_meal_plan_query().delete()
    db.session.commit()
    return jsonify(_serialize_meal_plan())

# ----- TRANSACTIONS CRUD -----------------------------------------------------

@app.route("/api/transactions", methods=["GET", "POST"])
def transactions_crud():
    if request.method == "POST":
        data = request.json or {}
        desc = data.get("description", "").strip()
        if not desc:
            return jsonify({"error": "Description required"}), 400
        amount = float(data.get("amount", 0))
        category = data.get("category", "discretionary")
        hid = current_household_id()
        account = _household_account()
        t = ExpenseTransaction(
            household_id=hid,
            description=desc,
            amount=amount,
            category=category,
            source="manual",
            local_account_id=account.id if account else None,
        )
        db.session.add(t)
        apply_balance_delta(hid, -amount)
        db.session.commit()
        return jsonify({
            "message": "Expense logged",
            "id": t.id,
            "new_balance": _household_account().checking_balance if account else None,
            "amount_authority": "confirmed_transaction_total",
            "tax_estimate_applied": False,
        })
    # GET list
    txns = _household_tx_query().order_by(ExpenseTransaction.date.desc()).all()
    return jsonify([{
        "id": t.id,
        "description": t.description,
        "amount": t.amount,
        "category": t.category,
        "date": t.date.strftime("%Y-%m-%d %H:%M") if t.date else "",
        **transaction_delete_eligibility(t, current_household_id()).to_api(),
    } for t in txns])

@app.route("/transactions/<int:txn_id>", methods=["DELETE"])
def delete_transaction(txn_id):
    hid = current_household_id()
    outcome, new_balance = delete_transaction_once(txn_id, hid)
    if outcome == "missing":
        return jsonify({"error": "Transaction not found"}), 404
    if outcome == "protected":
        return jsonify({"error": PROTECTED_MESSAGE}), 409
    return jsonify({"message": f"Transaction {txn_id} deleted", "new_balance": round(new_balance, 2)})


def _plaid_error_payload(exc: Exception) -> tuple[dict[str, Any], int]:
    if isinstance(exc, PlaidFoundationError):
        return {
            "error": str(exc),
            "code": exc.code,
        }, int(getattr(exc, "status_code", 400) or 400)
    return {
        "error": "Plaid request failed.",
        "code": "plaid_request_failed",
    }, 500


@app.route("/api/plaid/link-token", methods=["POST"])
def plaid_link_token():
    data = request.json or {}
    user_id = _resolve_request_user_id(data)
    try:
        payload = create_link_token(user_scope=user_id)
    except Exception as exc:
        err, status = _plaid_error_payload(exc)
        return jsonify(err), status
    return jsonify(payload)


@app.route("/api/plaid/exchange-public-token", methods=["POST"])
def plaid_exchange_public_token():
    data = request.json or {}
    user_id = _resolve_request_user_id(data)
    public_token = str(data.get("public_token") or "").strip()
    if not public_token:
        return jsonify({"error": "public_token is required.", "code": "missing_public_token"}), 400

    rung_account_id = None
    raw_id = data.get("rung_account_id")
    if raw_id is not None:
        try:
            rung_account_id = int(raw_id)
        except (TypeError, ValueError):
            return jsonify({"error": "rung_account_id must be an integer.", "code": "bad_rung_account_id"}), 400

    try:
        result = exchange_public_token_and_persist(
            owner_scope=user_id,
            public_token=public_token,
            rung_account_id=rung_account_id,
        )
    except Exception as exc:
        err, status = _plaid_error_payload(exc)
        return jsonify(err), status
    return jsonify(result)


@app.route("/api/plaid/status", methods=["GET"])
def plaid_status():
    user_id = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    payload = get_plaid_connection_status(user_id)
    return jsonify(payload)


@app.route("/api/plaid/sync-transactions", methods=["POST"])
def plaid_sync_transactions():
    data = request.json or {}
    user_id = _resolve_request_user_id(data)
    plaid_item_id = str(data.get("plaid_item_id") or "").strip() or None
    gate = check_optional_operation(user_id, "plaid_sync_call")
    if not gate.get("allowed", True):
        record_usage_event(
            owner_scope=user_id,
            category="plaid",
            provider="plaid",
            operation="transactions_sync_blocked",
            success=False,
            external_call=False,
            request_count=1,
            cost_status="unknown",
            metadata={"code": gate.get("code")},
        )
        return jsonify({
            "error": gate.get("message") or "Plaid sync is currently unavailable.",
            "code": gate.get("code") or "plaid_sync_blocked",
        }), 429
    try:
        payload = sync_plaid_transactions(owner_scope=user_id, plaid_item_id=plaid_item_id)
        projection = project_plaid_transactions(
            owner_scope=user_id,
            plaid_item_id=(payload.get("item") or {}).get("id"),
        )
        payload["projection"] = projection
    except Exception as exc:
        err, status = _plaid_error_payload(exc)
        return jsonify(err), status
    return jsonify(payload)


@app.route("/api/plaid/transactions", methods=["GET"])
def plaid_transactions_list():
    user_id = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    item_id = request.args.get("plaid_item_id")
    hid = current_household_id()

    rows = PlaidTransaction.query.filter_by(household_id=hid, owner_scope=user_id)
    if item_id:
        item = PlaidItem.query.filter_by(household_id=hid, owner_scope=user_id, plaid_item_id=str(item_id)).first()
        if item is None:
            return jsonify({"transactions": []})
        rows = rows.filter_by(plaid_item_id=item.id)

    results = rows.order_by(PlaidTransaction.transaction_date.desc(), PlaidTransaction.id.desc()).limit(200).all()
    return jsonify({"transactions": [row.to_summary() for row in results]})


@app.route("/api/reconciliation/proposals", methods=["GET"])
def reconciliation_proposals_list():
    user_id = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    rows = list_reconciliation_proposals(owner_scope=user_id)
    return jsonify({"proposals": rows})


@app.route("/api/reconciliation/decision", methods=["POST"])
def reconciliation_decide():
    data = request.json or {}
    user_id = _resolve_request_user_id(data)
    action = str(data.get("action") or "").strip().lower()
    manual_id = data.get("manual_transaction_id")
    plaid_tx_id = str(data.get("plaid_transaction_id") or "").strip()

    try:
        manual_id_int = int(str(manual_id or "").strip())
    except (TypeError, ValueError):
        return jsonify({"error": "manual_transaction_id must be an integer."}), 400
    if not plaid_tx_id:
        return jsonify({"error": "plaid_transaction_id is required."}), 400

    try:
        result = decide_reconciliation_pair(
            owner_scope=user_id,
            manual_transaction_id=manual_id_int,
            plaid_transaction_id=plaid_tx_id,
            action=action,
            user_id=user_id,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        LOGGER.exception("reconciliation decision failed")
        return jsonify({"error": "Could not apply reconciliation decision."}), 500

    account = _household_account()
    return jsonify({
        "result": result,
        "metrics": _canonical_financial_metrics(account, owner_scope=user_id) if account else None,
    })

# ----- BILLS CRUD ------------------------------------------------------------

@app.route("/bills", methods=["GET", "POST"])
def bills_crud():
    if request.method == "POST":
        data = request.json or {}
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        amount = _coerce_amount(data.get("amount", 0))
        due_date_str = data.get("due_date", "")
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
            except ValueError:
                due_date = datetime.now(timezone.utc) + timedelta(days=7)
        else:
            due_date = datetime.now(timezone.utc) + timedelta(days=7)
        b = Bill(household_id=current_household_id(), name=name, amount=amount, due_date=due_date)
        db.session.add(b)
        db.session.commit()
        return jsonify({"message": "Bill added", "id": b.id})
    # GET list
    bills = _household_bill_query().order_by(Bill.due_date.asc()).all()
    return jsonify([{
        "id": b.id,
        "name": b.name,
        "amount": b.amount,
        "due_date": b.due_date.strftime("%Y-%m-%d") if b.due_date else "",
        "is_paid": b.is_paid
    } for b in bills])

@app.route("/bills/<int:bid>/pay", methods=["POST"])
def toggle_bill(bid):
    b = _household_bill_query().filter_by(id=bid).first()
    if not b:
        return jsonify({"error": "Bill not found"}), 404
    b.is_paid = not b.is_paid
    db.session.commit()
    return jsonify({"message": f"Bill {bid} toggled", "is_paid": b.is_paid})

@app.route("/bills/<int:bid>", methods=["DELETE"])
def delete_bill(bid):
    b = _household_bill_query().filter_by(id=bid).first()
    if not b:
        return jsonify({"error": "Bill not found"}), 404
    db.session.delete(b)
    db.session.commit()
    return jsonify({"message": f"Bill {bid} deleted"})


def _validate_groq_key(api_key: str):
    """Best-effort live validation of a Groq key against the models endpoint.

    Returns
    -------
    (valid, note)
        ``valid`` is True/False when Groq answers, None when the check
        couldn't complete (network issue/timeout).  ``note`` is a
        human-readable string for the Settings panel.
    """
    try:
        import requests as _req
        resp = _req.get(
            "https://api.groq.com/openai/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=5,
        )
        if resp.status_code == 200:
            return True, "Key validated!"
        if resp.status_code == 401:
            return False, "Groq rejected this key — check console.groq.com/keys"
        if resp.status_code == 429:
            return None, "Groq rate-limited — validation will retry on next use."
        return None, f"Groq returned HTTP {resp.status_code}."
    except Exception as exc:
        return None, f"Could not reach Groq (network issue: {exc})."


def _write_env_var(key: str, value: str) -> None:
    """Write, update, or remove a single variable in the .env file.

    If the key already exists in .env, its value is replaced in-place.
    If the key doesn't exist, it's appended to the end of the file.
    If the file doesn't exist, it's created.
    If ``value`` is empty, the key's line is removed entirely (used by
    the DELETE endpoint so a removed Groq key doesn't linger as an
    env-var fallback and keep reporting "configured").

    This is used so the Groq API key survives test suite DB wipes
    (which destroy the ``user_settings`` table).
    """
    # Never write to the real .env from tests.  test_byok.py POSTs a fake
    # placeholder key through the real endpoint, which would otherwise
    # clobber the user's actual GROQ_API_KEY with "gsk_test123...".
    if getattr(app, "testing", False):
        return
    if not key:
        return
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path, "r+") as f:
                lines = f.readlines()
                new_lines = []
                for line in lines:
                    if line.strip().startswith(f"{key}="):
                        if value:
                            new_lines.append(f'{key}="{value}"\n')
                        # empty value → drop the line entirely
                        continue
                    new_lines.append(line)
                if value and not any(
                    l.strip().startswith(f"{key}=") for l in new_lines
                ):
                    new_lines.append(f'{key}="{value}"\n')
                f.seek(0)
                f.writelines(new_lines)
                f.truncate()
        else:
            if value:
                with open(env_path, "w") as f:
                    f.write(f'{key}="{value}"\n')
    except Exception as exc:
        import logging
        logging.getLogger("app").warning("Could not write %s to .env: %s", key, exc)


@app.route("/api/settings/groq-key", methods=["GET", "POST", "DELETE"])
def groq_key_settings():
    """Deprecated BYOK endpoint.

    Copilot credentials are now server-side only. This endpoint remains for
    backward compatibility with older clients but no longer accepts user keys.
    """
    if request.method == "GET":
        configured = bool((os.environ.get("GROQ_API_KEY") or "").strip())
        return jsonify({
            "configured": configured,
            "managed_by": "server",
        })

    return jsonify({
        "error": "Copilot provider credentials are managed server-side.",
    }), 404


@app.route("/api/settings/grocery-retailer", methods=["GET", "POST"])
def grocery_retailer_settings():
    """Compatibility retailer preference; canonical exact store wins."""
    setting_key = "grocery_active_retailer"
    account = _household_account()
    selected = get_selected_store(current_household_id(), account=account) if account else {}

    if request.method == "GET":
        if selected.get("canonical"):
            return jsonify({"retailer": selected["retailer"], "canonical_store": selected})
        retailer = get_setting(setting_key, "walmart") or "walmart"
        return jsonify({"retailer": retailer})

    data = request.json or {}
    retailer = str(data.get("retailer") or "").strip().lower()
    if retailer not in {"walmart", "kroger", "dollar_general"}:
        return jsonify({"error": "retailer must be walmart, kroger, or dollar_general"}), 400

    if selected.get("canonical"):
        return jsonify({
            "retailer": selected["retailer"],
            "canonical_store": selected,
            "selection_required": retailer != selected["retailer"],
        })

    set_setting(setting_key, retailer)
    return jsonify({"retailer": retailer})


@app.route("/api/settings/household-shopping-defaults", methods=["GET", "POST"])
def household_shopping_defaults_settings():
    """Read or update household-level shopping defaults and shopping style."""
    if request.method == "GET":
        stored = _load_household_shopping_defaults()
        return jsonify({
            "definitions": household_defaults_schema(),
            "preferences": stored["preferences"],
            "shopping_style": stored["shopping_style"],
        })

    payload = request.json or {}
    ok, errors = _save_household_shopping_defaults(payload)
    if not ok:
        return jsonify({
            "error": "Invalid household shopping defaults payload.",
            "details": errors,
        }), 400

    stored = _load_household_shopping_defaults()
    return jsonify({
        "saved": True,
        "preferences": stored["preferences"],
        "shopping_style": stored["shopping_style"],
    })


@app.route("/api/internal/usage/summary", methods=["GET"])
def internal_usage_summary():
    """Internal/operator usage summary for daily/monthly cost and call telemetry."""
    user_id = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    return jsonify(summarize_usage(user_id))


@app.route("/api/internal/usage/controls", methods=["GET", "POST"])
def internal_usage_controls():
    if request.method == "GET":
        return jsonify(get_usage_controls())
    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "controls payload must be an object."}), 400
    return jsonify(set_usage_controls(payload))


@app.route("/api/internal/usage/rates", methods=["GET", "POST"])
def internal_usage_rates():
    if request.method == "GET":
        return jsonify(get_usage_rates())
    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "rates payload must be an object."}), 400
    return jsonify(set_usage_rates(payload))


def _latest_retail_refresh() -> dict[str, Any]:
    latest_verified = (
        db.session.query(db.func.max(RetailProductCache.retrieved_at)).scalar()
    )
    latest_estimated = (
        db.session.query(db.func.max(StorePriceCache.last_updated)).scalar()
    )
    return {
        "verified_retail_cache_at": latest_verified.isoformat() if latest_verified else None,
        "store_price_cache_at": latest_estimated.isoformat() if latest_estimated else None,
    }


def _sanitized_recent_errors(limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        UsageEvent.query
        .filter(UsageEvent.success == False)
        .order_by(UsageEvent.created_at.desc())
        .limit(max(1, min(int(limit or 20), 50)))
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "category": row.category,
            "provider": row.provider,
            "operation": row.operation,
            "cost_status": row.cost_status,
            "cache_status": row.cache_status,
        })
    return out


@app.route("/api/internal/beta/readiness", methods=["GET"])
def beta_readiness():
    account = _household_account()
    owner_scope = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    safe_snapshot = _compute_safe_to_spend_snapshot(account, owner_scope=owner_scope) if account else None
    return jsonify({
        "readiness": _household_readiness(account, owner_scope=owner_scope),
        "safe_to_spend_state": (safe_snapshot or {}).get("state"),
    })


@app.route("/api/internal/beta/diagnostics", methods=["GET"])
def beta_diagnostics():
    owner_scope = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    account = _household_account()
    capabilities = _runtime_capabilities()
    readiness = _household_readiness(account, owner_scope=owner_scope)
    plaid = get_plaid_connection_status(owner_scope)
    db_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    db_path = _db_path_from_uri(db_uri)
    migration_revision = None
    try:
        migration_revision = db.session.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
    except Exception:
        migration_revision = None
    active_dataset = None
    try:
        active_dataset = ensure_bootstrap_tax_dataset()
    except Exception:
        active_dataset = None

    selected_profile = None
    if account is not None:
        selected_store = get_selected_store(current_household_id(), account=account)
        selected_profile = (
            StoreTaxProfile.query
            .filter_by(
                retailer=str(selected_store.get("retailer") or "unknown"),
                retailer_store_id=str(selected_store.get("store_id") or "unknown"),
            )
            .order_by(StoreTaxProfile.resolved_at.desc())
            .first()
        )

    return jsonify({
        "app": {
            "healthy": True,
            "schema_version": APP_SCHEMA_VERSION,
            "debug": bool(getattr(app, "debug", False)),
        },
        "database": {
            "uri_kind": "sqlite" if db_uri.startswith("sqlite:///") else "other",
            "path": db_path,
            "classification": _classify_db_path(db_path),
            "postgres_ready": bool(db_uri.startswith("postgresql")),
            "migration_head": migration_revision,
        },
        "authentication": {
            "mode": "principal_membership" if auth_required_mode() else "compatibility",
            "session": _current_auth_session_payload(),
            "header_override_enabled": header_override_allowed(),
        },
        "readiness": readiness,
        "capabilities": capabilities,
        "plaid": {
            "connected": bool(plaid.get("connected")),
            "items": len(plaid.get("items") or []),
            "last_sync_at": max([
                str((item or {}).get("last_sync_at") or "")
                for item in (plaid.get("items") or [])
            ] or [""]) or None,
            "sync_enabled": bool((get_usage_controls().get("kill_switches") or {}).get("plaid_sync_enabled", True)),
        },
        "retail_freshness": _latest_retail_refresh(),
        "usage_controls": get_usage_controls(),
        "cost_controls": {
            "serpapi_fallback_enabled": bool((get_usage_controls().get("kill_switches") or {}).get("serpapi_fallback_enabled", False)),
            "llm_enabled": bool((get_usage_controls().get("kill_switches") or {}).get("llm_enabled", True)),
            "plaid_sync_enabled": bool((get_usage_controls().get("kill_switches") or {}).get("plaid_sync_enabled", True)),
        },
        "recent_errors": _sanitized_recent_errors(limit=int(request.args.get("limit", 20) or 20)),
        "tax_engine": {
            "provider": "rung_owned",
            "paid_provider_keys_present": has_paid_provider_tax_keys(),
            "normal_path_uses_paid_provider": False,
            "active_dataset": {
                "source_key": (active_dataset.source_key if active_dataset else None),
                "version": (active_dataset.version_tag if active_dataset else None),
                "source_hash": (active_dataset.source_hash if active_dataset else None),
                "effective_from": (active_dataset.effective_from.isoformat() if active_dataset else None),
                "effective_to": (active_dataset.effective_to.isoformat() if active_dataset and active_dataset.effective_to else None),
                "status": (active_dataset.status if active_dataset else "unavailable"),
            },
            "selected_store_profile": {
                "retailer": (selected_profile.retailer if selected_profile else None),
                "retailer_store_id": (selected_profile.retailer_store_id if selected_profile else None),
                "location_precision": (selected_profile.location_precision if selected_profile else None),
                "confidence": (selected_profile.confidence if selected_profile else None),
                "resolved_tax_code": (selected_profile.resolved_tax_code if selected_profile else None),
                "general_rate_bps": (selected_profile.general_rate_basis_points if selected_profile else None),
                "grocery_rate_bps": (selected_profile.grocery_rate_basis_points if selected_profile else None),
                "prepared_rate_bps": (selected_profile.prepared_rate_basis_points if selected_profile else None),
            },
        },
    })


@app.route("/api/internal/beta/feedback", methods=["GET", "POST"])
def beta_feedback_collection():
    if request.method == "GET":
        status = str(request.args.get("status") or "").strip().lower()
        rows = BetaFeedback.query
        if status in {"open", "resolved"}:
            rows = rows.filter(BetaFeedback.status == status)
        items = rows.order_by(BetaFeedback.created_at.desc()).limit(200).all()
        return jsonify({"feedback": [row.to_summary() for row in items]})

    payload = request.json or {}
    category = str(payload.get("category") or "general").strip().lower() or "general"
    description = str(payload.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description is required."}), 400
    screen_context = str(payload.get("screen_context") or "").strip() or None

    row = BetaFeedback(
        category=category[:40],
        description=description[:500],
        screen_context=(screen_context[:120] if screen_context else None),
        status="open",
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"feedback": row.to_summary()}), 201


@app.route("/api/internal/beta/feedback/<int:feedback_id>", methods=["PATCH"])
def beta_feedback_update(feedback_id: int):
    row = BetaFeedback.query.get(feedback_id)
    if row is None:
        return jsonify({"error": "feedback not found."}), 404

    payload = request.json or {}
    status = str(payload.get("status") or "").strip().lower()
    if status and status not in {"open", "resolved"}:
        return jsonify({"error": "status must be open or resolved."}), 400
    if status:
        row.status = status

    db.session.add(row)
    db.session.commit()
    return jsonify({"feedback": row.to_summary()})


@app.route("/api/onboarding/state", methods=["GET"])
def onboarding_state():
    """Return first-launch onboarding state and default field values."""
    return jsonify(_onboarding_state_payload())


@app.route("/api/onboarding/required-expenses-review", methods=["POST"])
def onboarding_required_expenses_review():
    """Persist the household's explicit required-expense review transition.

    This intentionally changes only the canonical review state. It never
    creates, modifies, or deletes Bills/Needs, so an explicit "no expenses"
    answer cannot manufacture financial data or erase real financial data.
    """
    data = request.json or {}
    answer = str(data.get("answer") or "").strip().lower()
    if answer == "no":
        state = REQUIRED_EXPENSE_NONE
    elif answer == "yes":
        state = REQUIRED_EXPENSE_REVIEWED if data.get("review_complete") is True else REQUIRED_EXPENSE_PENDING
    else:
        return jsonify({"error": "answer must be 'yes' or 'no'."}), 400

    set_setting(REQUIRED_EXPENSE_REVIEW_SETTING_KEY, state)
    account = get_household_account(current_household_id())
    return jsonify({
        "saved": True,
        "required_expense_review": state,
        "readiness": _onboarding_readiness(account),
    })


@app.route("/api/onboarding/skip", methods=["POST"])
def onboarding_skip():
    """Mark onboarding complete without requiring any setup input."""
    hid = current_household_id()
    account = get_household_account(hid)
    account.is_onboarded = True
    seed_default_user_preferences(_resolve_request_user_id(request.json or {}), commit=False)
    db.session.commit()
    return jsonify({
        'saved': True,
        'is_onboarded': True,
        'welcome_message': (
            "Welcome to Rung. You're all set to start in Copilot. "
            "You can add preferences or bill baselines any time from Settings."
        ),
        'readiness': _onboarding_readiness(account),
    })


@app.route("/api/onboarding/complete", methods=["POST"])
def onboarding_complete():
    """Persist onboarding inputs and mark first-launch setup as complete."""
    data = request.json or {}

    hid = current_household_id()
    account = get_household_account(hid)

    # An explicit durable "no required expenses" review answer is authoritative
    # over whatever the client happens to submit. This protects against a
    # browser that switched a YES-review answer back to NO without clearing
    # already-typed grocery/fuel/bill fields: those stale values must never
    # manufacture Bills or baseline preferences once the household has
    # explicitly reviewed and confirmed zero required expenses.
    review_state = _required_expense_review_state()
    if review_state == REQUIRED_EXPENSE_NONE:
        data = dict(data)
        data.pop('baseline_grocery_cost', None)
        data.pop('baseline_fuel_cost', None)
        data.pop('recurring_bills', None)

    household_size = data.get('household_size', account.household_size or 4)
    try:
        household_size = max(1, int(household_size))
    except (TypeError, ValueError):
        household_size = 4
    account.household_size = household_size

    if 'favorite_proteins' in data:
        set_user_preference('favorite_proteins', json.dumps(_normalize_list_input(data.get('favorite_proteins'))))
    if 'dietary_restrictions' in data:
        set_user_preference('dietary_restrictions', json.dumps(_normalize_list_input(data.get('dietary_restrictions'))))
    if 'allergies' in data:
        set_user_preference('allergies', json.dumps(_normalize_list_input(data.get('allergies'))))

    grocery_baseline = _coerce_positive_float(data.get('baseline_grocery_cost'))
    fuel_baseline = _coerce_positive_float(data.get('baseline_fuel_cost'))
    if grocery_baseline is not None:
        set_user_preference('baseline_grocery_cost', f"{grocery_baseline:.2f}")
    if fuel_baseline is not None:
        set_user_preference('baseline_fuel_cost', f"{fuel_baseline:.2f}")

    shopping_payload: dict[str, Any] = {}
    if "shopping_style" in data:
        shopping_payload["shopping_style"] = data.get("shopping_style")
    if "household_shopping_defaults" in data:
        shopping_payload["preferences"] = data.get("household_shopping_defaults")
    if shopping_payload:
        ok, shop_errors = _save_household_shopping_defaults(shopping_payload, commit=False)
        if not ok:
            db.session.rollback()
            return jsonify({"error": "Invalid household shopping defaults.", "details": shop_errors}), 400

    if "location_sharing_enabled" in data:
        if not isinstance(data.get("location_sharing_enabled"), bool):
            db.session.rollback()
            return jsonify({"error": "location_sharing_enabled must be true or false."}), 400
        set_setting(
            LOCATION_SHARING_SETTING_KEY,
            "true" if data["location_sharing_enabled"] else "false",
            commit=False,
        )

    financial_errors = _persist_onboarding_financial_basics(account, data)
    if financial_errors:
        db.session.rollback()
        return jsonify({"error": "Invalid financial setup.", "details": financial_errors}), 400

    recurring_bills = data.get('recurring_bills') or []
    if isinstance(recurring_bills, list):
        for item in recurring_bills:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            amount = _coerce_positive_float(item.get('amount'))
            if not name or amount is None:
                continue

            existing = _household_bill_query().filter(Bill.name.ilike(name)).first()
            if existing:
                existing.amount = amount
                existing.is_gas_estimate = False
            else:
                db.session.add(Bill(
                    household_id=hid,
                    name=name.title(),
                    amount=amount,
                    due_date=datetime.now(timezone.utc) + timedelta(days=14),
                    is_gas_estimate=False,
                    is_paid=False,
                ))

    if fuel_baseline is not None:
        gas_bill = _household_bill_query().filter_by(is_gas_estimate=True).first()
        if gas_bill:
            gas_bill.amount = fuel_baseline
        else:
            db.session.add(Bill(
                household_id=hid,
                name='Gas Allocation',
                amount=fuel_baseline,
                due_date=datetime.now(timezone.utc) + timedelta(days=2),
                is_gas_estimate=True,
                is_paid=False,
            ))

    account.is_onboarded = True
    seed_default_user_preferences(_resolve_request_user_id(data), commit=False)
    db.session.commit()

    bill_count = len(recurring_bills) if isinstance(recurring_bills, list) else 0
    welcome = (
        f"Welcome to Rung. I saved your household size ({household_size})"
        + (f", captured {bill_count} recurring bill baseline(s)," if bill_count else ',')
        + " and I'm ready to help you plan meals, groceries, and spending."
    )

    return jsonify({
        'saved': True,
        'is_onboarded': True,
        'welcome_message': welcome,
        'readiness': _onboarding_readiness(account),
    })


# ----- ACCOUNT UPDATE (balance + ratios) ------------------------------------

@app.route("/api/account/update", methods=["POST"])
def update_account():
    account = _household_account()
    if not account:
        return jsonify({"error": "Account not found"}), 404
    data = request.json or {}
    if "expected_paycheck" in data and not str(data.get("expected_paycheck_operation_id") or "").strip():
        return jsonify({"error": "expected_paycheck_operation_id is required when confirming an expected paycheck."}), 400
    if "pay_period_days" in data:
        try:
            pay_period_days = int(data["pay_period_days"])
        except (TypeError, ValueError):
            return jsonify({"error": "pay_period_days must be a whole number from 1 to 31."}), 400
        if isinstance(data["pay_period_days"], bool) or str(data["pay_period_days"]).strip() != str(pay_period_days) or not 1 <= pay_period_days <= 31:
            return jsonify({"error": "pay_period_days must be a whole number from 1 to 31."}), 400
    checking_balance = float(account.checking_balance or 0.0)
    if "checking_balance" in data:
        checking_balance = set_balance_absolute(current_household_id(), float(data["checking_balance"]))
    if "food_allocation_pct" in data:
        account.food_allocation_pct = float(data["food_allocation_pct"])
    if "pay_period_days" in data:
        account.pay_period_days = pay_period_days
    if "meals_per_day" in data:
        account.meals_per_day = int(data["meals_per_day"])
    plan_created = False
    if "expected_paycheck" in data:
        try:
            cents = _money_to_cents(data["expected_paycheck"], field_name="expected_paycheck")
            if cents <= 0:
                raise IncomePlanError("Expected paycheck must be greater than zero.")
            now = datetime.now(timezone.utc)
            inferred = _infer_next_income(account, now)
            cycle = resolve_cycle(account=account, now=now, next_income=inferred)
            _row, plan_created = record_income_plan(
                current_household_id(), operation_id=str(data["expected_paycheck_operation_id"]).strip(),
                expected_income_cents=cents, now=now,
                next_payday=cycle.get("end") if cycle.get("available") else None,
                source="settings_confirmation",
            )
        except (IncomePlanError, ValueError) as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
    db.session.commit()
    return jsonify({"message": "Account updated", "checking_balance": round(checking_balance, 2),
                    "income_plan_created": plan_created,
                    "income_plan": income_plan_payload(current_household_id(), at=datetime.now(timezone.utc))})

# ----- GROCERY LIST (GET/DELETE) --------------------------------------------

@app.route("/api/grocery", methods=["GET", "POST"])
def grocery_list():
    if request.method == "POST":
        data = request.json or {}
        # Shopping's direct-item entry writes an abstract household request,
        # never a fabricated product, price, package, or store observation.
        # Product resolution remains the responsibility of the canonical cart
        # pipeline after the user has explicitly selected a physical store.
        item_name = str(data.get("item_name") or "").strip()
        if item_name:
            if len(item_name) > 150:
                return jsonify({"error": "Item name is too long."}), 400
            existing = _household_grocery_query().filter(
                GroceryItem.is_purchased.is_(False),
                db.func.lower(GroceryItem.item_name) == item_name.lower(),
                db.or_(GroceryItem.recipe_ids == '', GroceryItem.recipe_ids.is_(None)),
            ).first()
            if existing:
                return jsonify({
                    "message": "Item is already on this shopping list.",
                    "item": {"id": existing.id, "item_name": existing.item_name},
                    "created": False,
                })
            selected = get_selected_store(current_household_id(), account=_household_account())
            row = GroceryItem(
                household_id=current_household_id(),
                item_name=item_name,
                estimated_price=0.0,
                store_name=str(selected.get("name") or ""),
                location_context=str(selected.get("address") or ""),
            )
            db.session.add(row)
            db.session.commit()
            return jsonify({
                "message": "Shopping item added.",
                "item": {"id": row.id, "item_name": row.item_name},
                "created": True,
            }), 201
        recipe_ids = data.get("recipe_ids", [])
        store_name = data.get("store_name", "")
        # Build cart from recipes
        recipes = visible_recipe_query(current_household_id()).filter(Recipe.id.in_(recipe_ids)).all() if recipe_ids else []
        if recipe_ids and len(recipes) != len({int(rid) for rid in recipe_ids if str(rid).strip().isdigit()}):
            return jsonify({"error": "Recipe not found"}), 404
        # Clear old grocery items for this session
        _household_grocery_query().delete()
        items = []
        for r in recipes:
            for ing in r.ingredients:
                gi = GroceryItem(
                    household_id=current_household_id(),
                    item_name=ing.product_name,
                    estimated_price=round(2.00 + (float(ing.quantity or 0.0) * 0.15), 2),
                    store_name=store_name or "Local Store",
                    location_context=""
                )
                db.session.add(gi)
                items.append(gi)
        db.session.commit()
        return jsonify({"message": f"Grocery list generated with {len(items)} items"})
    # GET list
    items = _household_grocery_query().order_by(GroceryItem.id).all()
    account = _household_account()
    selected = get_selected_store(current_household_id(), account=account) if account else {}
    selected_store = selected.get("name") or "Kroger"
    location_id = selected.get("store_id") or None
    selected_retailer = str(selected.get("retailer") or "kroger").strip().lower()
    serialized = []
    for i in items:
        term = (i.item_name or "").strip()
        row = {
            "id": i.id,
            "item_name": term,
            "estimated_price": float(i.estimated_price or 0.0),
            "store_name": i.store_name or selected_store,
            "location_context": i.location_context or (account.city_state if account else ""),
            "is_purchased": bool(i.is_purchased),
            "is_favorite": bool(getattr(i, "is_favorite", False)),
            "quantity": 1,
            "price_source": "estimated",
            "confirmed_local_store": False,
            "product_label": term,
        }
        # This legacy resolver is Kroger-specific. Walmart and other retailer
        # product resolution belongs to the authoritative cart pipeline.
        if term and selected_retailer == "kroger":
            resolved = resolve_terms(app, [term], store_name=selected_store, location_id=location_id, limit=5)
            candidates = resolved.get(term.lower(), []) or resolved.get(term, [])
            best = None
            if candidates:
                best = pick_best(
                    candidates,
                    prefer_store_brand=True,
                    keyword=term.lower(),
                    net_needed=1,
                    required_dimension="unknown",
                )
            if best:
                row["product_label"] = best.get("product_title") or term
                row["item_name"] = best.get("product_title") or term
                row["estimated_price"] = round(float(best.get("price") or 0.0), 2)
                row["store_name"] = best.get("source_store_name") or best.get("store_name") or selected_store
                row["price_source"] = best.get("source", "estimated")
                row["confirmed_local_store"] = str(best.get("source", "")).lower() in {"kroger_cache", "kroger_api"}
                row["package_size"] = best.get("package_size")
                row["image_url"] = best.get("image_url")
        serialized.append(row)
    return jsonify({"items": serialized})

@app.route("/api/grocery/<int:gid>", methods=["DELETE"])
def delete_grocery_item(gid):
    gi = _household_grocery_query().filter_by(id=gid).first()
    if not gi:
        return jsonify({"error": "Item not found"}), 404
    db.session.delete(gi)
    db.session.commit()
    return jsonify({"message": f"Grocery item {gid} deleted"})


def _build_trip_token(*, retailer: str, store_id: str, store_name: str, cart_signature: str, planned_total_cents: int) -> str:
    raw = "|".join([
        str(retailer or "").strip().lower(),
        str(store_id or "").strip(),
        str(store_name or "").strip().lower(),
        str(cart_signature or "").strip().lower(),
        str(int(planned_total_cents)),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _authoritative_finished_payload(data: dict[str, Any], *, household_id: int, for_update: bool = False) -> dict[str, Any]:
    """Load checkout truth from the durable current cart, never the browser."""
    from services.authoritative_cart import current_cart
    cart = current_cart(household_id, for_update=for_update)
    if cart is None:
        raise ValueError("Build and approve a store-bound cart before finishing shopping.")
    selected = get_selected_store(household_id)
    if not selected.get("canonical") or int(selected.get("retail_store_identity_id") or 0) != cart.retail_store_identity_id:
        raise ValueError("Your current cart no longer matches the selected physical store.")
    unresolved = ShoppingCartLine.query.filter_by(cart_id=cart.id).filter(
        db.or_(ShoppingCartLine.resolution_state != "resolved", ShoppingCartLine.availability != "in_stock")
    ).first()
    if unresolved is not None:
        raise ValueError("Resolve every cart line before finishing shopping.")
    if cart.total_cents <= 0:
        raise ValueError("The authoritative cart total must be greater than zero.")
    operation_id = str(data.get("operation_id") or "").strip() or f"trip_{uuid.uuid4().hex}"
    return {
        "cart": cart,
        "planned_total_cents": cart.total_cents,
        "actual_total_cents": cart.total_cents,
        "actual_amount_source": "authoritative_cart",
        "retailer": str(selected.get("retailer") or ""),
        "store_name": str(selected.get("name") or ""),
        "store_id": str(selected.get("store_id") or ""),
        "cart_signature": f"cart:{cart.id}:v{cart.version}",
        "operation_id": operation_id,
    }


@app.route("/api/grocery/finished-shopping/stage", methods=["POST"])
def grocery_finished_shopping_stage():
    data = request.json or {}
    try:
        payload = _authoritative_finished_payload(data, household_id=current_household_id())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    planned = payload["planned_total_cents"]
    actual = payload["actual_total_cents"]
    diff = actual - planned

    return jsonify({
        "staged": True,
        "requires_confirmation": True,
        "operation_id": payload["operation_id"],
        "trip_preview": {
            "grocery_spending": _cents_to_float(actual),
            "planned_total": _cents_to_float(planned),
            "actual_total": _cents_to_float(actual),
            "difference": _cents_to_float(diff),
            "difference_sign": "plus" if diff > 0 else ("minus" if diff < 0 else "zero"),
            "retailer": payload["retailer"],
            "store_name": payload["store_name"],
            "store_id": payload["store_id"],
            "amount_source": payload["actual_amount_source"],
        },
        "message": "Relevant financial totals will be updated after confirmation.",
    })


@app.route("/api/grocery/finished-shopping/complete", methods=["POST"])
def grocery_finished_shopping_complete():
    data = request.json or {}
    user_id = _resolve_request_user_id(data)
    hid = current_household_id()
    confirm = bool(data.get("confirm", False))
    if not confirm:
        return jsonify({"error": "Confirmation required."}), 400

    # Claim/replay checks precede current-cart validation: a finished cart is
    # immutable, but retries of its already-completed operation are safe.
    requested_op_id = str(data.get("operation_id") or "").strip()
    replay = _household_trip_query().filter_by(operation_id=requested_op_id).first() if requested_op_id else None
    if replay is not None:
        txn = _household_tx_query().filter_by(id=replay.transaction_id).first()
        account = _household_account()
        return jsonify({"completed": True, "already_completed": True, "operation_id": replay.operation_id,
            "trip_token": replay.trip_token, "transaction_id": replay.transaction_id,
            "transaction_amount": round(float(txn.amount), 2) if txn else _cents_to_float(replay.actual_total_cents),
            "planned_total": _cents_to_float(replay.planned_total_cents), "actual_total": _cents_to_float(replay.actual_total_cents),
            "amount_source": replay.amount_source, "completed_at": replay.completed_at.isoformat() if replay.completed_at else None,
            "metrics": _canonical_financial_metrics(account) if account else None})
    requested_trip_token = str(data.get("trip_token") or "").strip()
    if requested_trip_token and _household_trip_query().filter_by(trip_token=requested_trip_token).first() is not None:
        return jsonify({"error": "This shopping trip has already been completed.", "trip_token": requested_trip_token}), 409

    try:
        # Claim the authoritative cart row for the remainder of this explicit
        # financial transaction.  A database uniqueness constraint on the
        # completion is still the retry-safe final backstop.
        payload = _authoritative_finished_payload(data, household_id=hid, for_update=True)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    trip_token = str(data.get("trip_token") or "").strip()
    if not trip_token:
        trip_token = _build_trip_token(
            retailer=payload["retailer"],
            store_id=payload["store_id"],
            store_name=payload["store_name"],
            cart_signature=payload["cart_signature"],
            planned_total_cents=payload["planned_total_cents"],
        )

    op_id = payload["operation_id"]
    existing = _household_trip_query().filter_by(operation_id=op_id).first()
    if existing:
        txn = _household_tx_query().filter_by(id=existing.transaction_id).first()
        account = _household_account()
        return jsonify({
            "completed": True,
            "already_completed": True,
            "operation_id": existing.operation_id,
            "trip_token": existing.trip_token,
            "transaction_id": existing.transaction_id,
            "transaction_amount": round(float(txn.amount), 2) if txn else _cents_to_float(existing.actual_total_cents),
            "planned_total": _cents_to_float(existing.planned_total_cents),
            "actual_total": _cents_to_float(existing.actual_total_cents),
            "amount_source": existing.amount_source,
            "completed_at": existing.completed_at.isoformat() if existing.completed_at else None,
            "metrics": _canonical_financial_metrics(account) if account else None,
        })

    duplicate_trip = _household_trip_query().filter_by(trip_token=trip_token).first()
    if duplicate_trip:
        return jsonify({
            "error": "This shopping trip has already been completed.",
            "operation_id": duplicate_trip.operation_id,
            "trip_token": duplicate_trip.trip_token,
        }), 409

    account = _household_account()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    amount = _cents_to_float(payload["actual_total_cents"])
    description_parts = ["Grocery trip"]
    if payload["store_name"]:
        description_parts.append(payload["store_name"])
    if payload["retailer"]:
        description_parts.append(f"({payload['retailer']})")
    description = " ".join(description_parts)

    undo_token = uuid.uuid4().hex
    try:
        record_action_audit(
            {
                "operation_id": op_id,
                "status": "in_progress",
                "kind": "grocery_finished_shopping",
                "trip_token": trip_token,
            },
            raw_text="finished_shopping_complete",
            source="grocery_finished_shopping",
            user_id=user_id,
            operation_id=op_id,
            undo_token=undo_token,
            commit=False,
        )
        db.session.flush()

        txn = ExpenseTransaction(
            household_id=hid,
            description=description[:150],
            amount=amount,
            category="grocery",
            source="manual",
            local_account_id=account.id if account else None,
            date=datetime.now(timezone.utc),
        )
        db.session.add(txn)
        apply_balance_delta(hid, -amount)
        db.session.flush()

        trip = ShoppingTripCompletion(
            household_id=hid,
            operation_id=op_id,
            trip_token=trip_token,
            transaction_id=txn.id,
            retailer=payload["retailer"],
            store_name=payload["store_name"],
            store_id=payload["store_id"] or None,
            planned_total_cents=payload["planned_total_cents"],
            actual_total_cents=payload["actual_total_cents"],
            amount_source=payload["actual_amount_source"],
            cart_signature=payload["cart_signature"],
            shopping_cart_id=payload["cart"].id,
            manual_provisional=True,
            completed_at=datetime.now(timezone.utc),
        )
        db.session.add(trip)
        payload["cart"].status = "completed"
        payload["cart"].completed_at = trip.completed_at

        audit_row = _household_audit_query().filter_by(operation_id=op_id).first()
        if audit_row:
            audit_row.actions_json = json.dumps({
                "operation_id": op_id,
                "undo_token": undo_token,
                "kind": "grocery_finished_shopping",
                "completed_trip": {
                    "trip_token": trip_token,
                    "transaction_id": txn.id,
                    "planned_total": _cents_to_float(payload["planned_total_cents"]),
                    "actual_total": _cents_to_float(payload["actual_total_cents"]),
                    "amount_source": payload["actual_amount_source"],
                },
            })

        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Concurrent duplicate submit can trip unique operation_id claim; replay.
        replay = _household_trip_query().filter_by(operation_id=op_id).first()
        if replay is None:
            replay = _household_trip_query().filter_by(trip_token=trip_token).first()
        if replay is not None:
            txn = _household_tx_query().filter_by(id=replay.transaction_id).first()
            account = _household_account()
            return jsonify({
                "completed": True,
                "already_completed": True,
                "operation_id": replay.operation_id,
                "trip_token": replay.trip_token,
                "transaction_id": replay.transaction_id,
                "transaction_amount": round(float(txn.amount), 2) if txn else _cents_to_float(replay.actual_total_cents),
                "planned_total": _cents_to_float(replay.planned_total_cents),
                "actual_total": _cents_to_float(replay.actual_total_cents),
                "amount_source": replay.amount_source,
                "completed_at": replay.completed_at.isoformat() if replay.completed_at else None,
                "metrics": _canonical_financial_metrics(account) if account else None,
            })
        raise
    except Exception:
        db.session.rollback()
        raise

    metrics = _canonical_financial_metrics(account)
    return jsonify({
        "completed": True,
        "already_completed": False,
        "operation_id": op_id,
        "trip_token": trip_token,
        "transaction_id": txn.id,
        "transaction_amount": round(amount, 2),
        "planned_total": _cents_to_float(payload["planned_total_cents"]),
        "actual_total": _cents_to_float(payload["actual_total_cents"]),
        "amount_source": payload["actual_amount_source"],
        "completed_at": trip.completed_at.isoformat(),
        "undo_token": undo_token,
        "metrics": metrics,
    })


@app.route("/api/grocery/finished-shopping/status", methods=["GET"])
def grocery_finished_shopping_status():
    _ = current_household_id()
    trip_token = str(request.args.get("trip_token") or "").strip()
    operation_id = str(request.args.get("operation_id") or "").strip()
    if not trip_token and not operation_id:
        return jsonify({"completed": False})

    row = None
    if operation_id:
        row = _household_trip_query().filter_by(operation_id=operation_id).first()
    if row is None and trip_token:
        row = _household_trip_query().filter_by(trip_token=trip_token).first()
    if row is None:
        return jsonify({"completed": False})

    txn = _household_tx_query().filter_by(id=row.transaction_id).first()
    account = _household_account()
    return jsonify({
        "completed": True,
        "operation_id": row.operation_id,
        "trip_token": row.trip_token,
        "transaction_id": row.transaction_id,
        "transaction_amount": round(float(txn.amount), 2) if txn else _cents_to_float(row.actual_total_cents),
        "planned_total": _cents_to_float(row.planned_total_cents),
        "actual_total": _cents_to_float(row.actual_total_cents),
        "amount_source": row.amount_source,
        "retailer": row.retailer,
        "store_name": row.store_name,
        "store_id": row.store_id,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "metrics": _canonical_financial_metrics(account) if account else None,
    })


def _apply_canonical_tax_to_rebalance_preview(
    preview: dict[str, Any],
    cart_items: list[dict[str, Any]],
    *,
    retailer: str,
    owner_scope: str,
) -> dict[str, Any]:
    """Replace optimizer subtotal comparisons with canonical store-tax totals."""
    account = _household_account()
    selected = get_selected_store(current_household_id(), account=account) if account else {}
    if not account or not selected.get("canonical") or str(selected.get("retailer") or "").lower() != retailer:
        return {**preview, "eligible": False, "status": "tax_context_required", "tax": {"status": "tax_not_included_yet", "label": "Tax not included yet"}}

    proposed_items = json.loads(json.dumps(cart_items))
    changes = {str(row.get("choice_key") or ""): row for row in preview.get("changes") or []}
    for item in proposed_items:
        requirement = item.get("requirement") or {}
        key = " ".join(str(requirement.get("base_item") or item.get("keyword") or "").strip().lower().split())
        change = changes.get(key)
        if not change:
            continue
        product = dict(change.get("proposed_product") or {})
        item["selected_product"] = {**dict(item.get("selected_product") or {}), **product, "retailer": retailer}
        item["estimated_price"] = _cents_to_float(int(product.get("line_price_cents") or 0))
        item["product_label"] = product.get("title") or item.get("product_label")

    common = {
        "account": account,
        "owner_scope": owner_scope,
        "retailer": retailer,
        "store_name": str(selected.get("name") or "Selected store"),
        "store_id": str(selected.get("store_id") or ""),
        "store_address": str(selected.get("address") or ""),
        "postal_code": str(selected.get("postal_code") or ""),
        "city_state": ", ".join(filter(None, [str(selected.get("city") or "").strip(), str(selected.get("state") or "").strip()])),
    }
    base_tax = _apply_owned_tax_to_cart(cart_items=json.loads(json.dumps(cart_items)), **common)
    proposed_tax = _apply_owned_tax_to_cart(cart_items=proposed_items, **common)
    if base_tax["total_cart_cost"] is None or proposed_tax["total_cart_cost"] is None:
        return {**preview, "eligible": False, "status": "tax_context_required", "tax": proposed_tax["tax_engine"]}

    base_total = _money_to_cents(base_tax["total_cart_cost"], field_name="base total")
    optimized_total = _money_to_cents(proposed_tax["total_cart_cost"], field_name="optimized total")
    budget_cents = int(preview.get("budget_cents") or 0)
    changes_list = preview.get("changes") or []
    status = "within_budget" if base_total <= budget_cents else (
        "rebalance_available" if changes_list and optimized_total <= budget_cents else (
            "rebalance_partial" if changes_list else "over_budget_no_acceptable_savings"
        )
    )
    return {
        **preview,
        "eligible": bool(changes_list),
        "status": status,
        "base_total_cents": base_total,
        "optimized_total_cents": optimized_total,
        "required_savings_cents": max(0, base_total - budget_cents),
        "max_available_savings_cents": max(0, base_total - optimized_total),
        "remaining_cents": max(0, budget_cents - optimized_total),
        "still_over_budget_cents": max(0, optimized_total - budget_cents),
        "tax": proposed_tax["tax_engine"],
    }


def _authoritative_rebalance_items(cart: ShoppingCart) -> list[dict[str, Any]]:
    """Rehydrate optimizer input from server-owned cart-line provenance."""
    items: list[dict[str, Any]] = []
    for line in ShoppingCartLine.query.filter_by(cart_id=cart.id).order_by(ShoppingCartLine.id).all():
        try:
            saved = json.loads(line.provenance_json or '{}').get('item') or {}
        except (TypeError, ValueError):
            saved = {}
        requirement = json.loads(line.requirement_json or '{}')
        product = dict(saved.get('selected_product') or {})
        product.update({'product_id': line.provider_product_id, 'us_item_id': line.provider_us_item_id,
                        'title': line.title, 'brand': line.brand, 'package_size': line.package_size,
                        'price': (line.unit_price_cents or 0) / 100, 'availability': line.availability,
                        'retailer': line.retailer})
        items.append({**saved, 'requirement': requirement, 'selected_product': product,
                      'packages_to_buy': line.package_count, 'estimated_price': (line.line_total_cents or 0) / 100,
                      'resolved': line.resolution_state == 'resolved', 'availability': line.availability})
    return items


@app.route("/api/grocery/rebalance/preview", methods=["POST"])
def grocery_rebalance_preview():
    from services.retail.cart import _load_household_shopping_defaults, propose_rebalance_preview

    data = request.json or {}
    from services.authoritative_cart import current_cart
    from services.authoritative_rebalance import _proposal_dict, create_proposal
    hid = current_household_id(); cart = current_cart(hid)
    if cart is None: return jsonify({'error': 'Build a current cart before rebalancing.'}), 409
    cart_items = _authoritative_rebalance_items(cart)
    budget_limit = float(data.get("budget_limit") or 0)
    raw_context = data.get("cart_context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
    selected = get_selected_store(hid)
    retailer = str(selected.get("retailer") or "").strip().lower()
    protected = set()
    for key in data.get("protected_choice_keys") or []:
        normalized = " ".join(str(key or "").strip().lower().split())
        if normalized:
            protected.add(normalized)
    last_changed = " ".join(str(data.get("last_changed_choice_key") or "").strip().lower().split())
    if last_changed:
        protected.add(last_changed)

    preview = propose_rebalance_preview(
        cart_items=cart_items,
        budget_limit=budget_limit,
        tax_rate=0.0,
        retailer=retailer,
        defaults=_load_household_shopping_defaults(),
        protected_choice_keys=protected,
        context=context,
    )
    preview = _apply_canonical_tax_to_rebalance_preview(preview, cart_items, retailer=retailer, owner_scope=_resolve_request_user_id(data))
    preview["protected_choice_keys"] = sorted(protected)
    op_id = str(data.get('operation_id') or f'rebalance_{uuid.uuid4().hex}')
    proposal = create_proposal(household_id=hid, cart=cart, operation_id=op_id, changes=preview.get('changes') or [])
    db.session.commit()
    return jsonify({**preview, 'proposal': _proposal_dict(proposal), 'authoritative_cart_id': cart.id, 'authoritative_cart_version': cart.version})


@app.route("/api/grocery/rebalance/apply", methods=["POST"])
def grocery_rebalance_apply():
    data = request.json or {}
    from services.authoritative_cart import cart_dict
    from services.authoritative_rebalance import _proposal_dict, approve_proposal
    proposal_id = int(data.get('proposal_id') or 0)
    if not proposal_id: return jsonify({'error': 'proposal_id is required.'}), 400
    selected = get_selected_store(current_household_id())
    try:
        proposal = approve_proposal(household_id=current_household_id(), proposal_id=proposal_id,
                                    selected_store_id=int(selected.get('retail_store_identity_id') or 0))
        db.session.commit()
    except LookupError: return jsonify({'error': 'Rebalance proposal not found.'}), 404
    except ValueError as exc:
        db.session.rollback(); return jsonify({'error': str(exc), 'code': 'stale_rebalance_proposal'}), 409
    if proposal.status == 'stale':
        return jsonify({'error': 'Rebalance proposal is stale.', 'code': 'stale_rebalance_proposal', 'proposal': _proposal_dict(proposal)}), 409
    cart = db.session.get(ShoppingCart, proposal.base_cart_id)
    return jsonify({'applied': True, 'proposal': _proposal_dict(proposal), 'authoritative_cart': cart_dict(cart)})


@app.route('/api/grocery/rebalance/<int:proposal_id>/reject', methods=['POST'])
def grocery_rebalance_reject(proposal_id: int):
    from services.authoritative_rebalance import _proposal_dict, reject_proposal
    try:
        proposal = reject_proposal(household_id=current_household_id(), proposal_id=proposal_id); db.session.commit()
    except LookupError: return jsonify({'error': 'Rebalance proposal not found.'}), 404
    return jsonify({'rejected': proposal.status == 'rejected', 'proposal': _proposal_dict(proposal)})

def _savings_request_cents(data: dict[str, Any], key: str = "amount") -> int:
    cents_key = key + "_cents"
    if cents_key in data:
        try: cents = int(data[cents_key])
        except (TypeError, ValueError): raise SavingsError(f"{cents_key} must be whole cents.")
        if cents <= 0: raise SavingsError(f"{cents_key} must be positive.")
        return cents
    return _money_to_cents(data.get(key), field_name=key)


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""): return None
    try: return date.fromisoformat(str(value))
    except ValueError: raise SavingsError("target_date must use YYYY-MM-DD.")


def _savings_cycle_key(account: Account, pyf: dict[str, Any]) -> str:
    """Use the same current-cycle identity for preview and one-time apply."""
    return str(pyf.get("window_end") or "").split("T", 1)[0] or f"pay-period-{max(1, int(account.pay_period_days or 14))}"


def _current_savings_allocation_plan(household_id: int, account: Account, pyf: dict[str, Any], *, cycle_key: str) -> dict[str, Any]:
    """Plan only savings not already allocated in the authoritative cycle run.

    PYF feasibility is the total protection for the cycle.  A completed
    allocation run must reduce the *remaining* amount shown to the user; it
    must not make the same protected dollars appear newly allocable again.
    """
    cycle_feasible_cents = max(0, int(pyf.get("feasible_savings_cents") or 0))
    existing = SavingsAllocationRun.query.filter_by(
        household_id=household_id, cycle_key=cycle_key,
    ).first()
    already_allocated_cents = min(
        cycle_feasible_cents,
        max(0, int(existing.allocated_cents)) if existing is not None else 0,
    )
    remaining_available_cents = max(0, cycle_feasible_cents - already_allocated_cents)
    plan = savings_allocation_plan(
        household_id,
        remaining_available_cents,
        pay_period_days=max(1, int(account.pay_period_days or 14)),
    )
    plan.update({
        "cycle_key": cycle_key,
        "cycle_feasible_cents": cycle_feasible_cents,
        "already_allocated_cents": already_allocated_cents,
        "remaining_available_cents": remaining_available_cents,
    })
    return plan


@app.route("/api/savings/state", methods=["GET"])
def get_savings_state():
    account = _household_account()
    return jsonify(savings_state(current_household_id(), pay_period_days=max(1, int(account.pay_period_days or 14))))


@app.route("/api/goals", methods=["POST"])
def create_goal_api():
    data = request.json or {}
    try:
        create_savings_goal(current_household_id(), operation_id=str(data.get("operation_id") or "").strip(), name=str(data.get("name") or ""), target_cents=_savings_request_cents(data, "target"), target_date=_parse_optional_date(data.get("target_date")), priority=int(data.get("priority", 100)))
        return jsonify(savings_state(current_household_id(), pay_period_days=max(1, int(_household_account().pay_period_days or 14)))), 201
    except (SavingsError, ValueError) as exc: db.session.rollback(); return jsonify({"error": str(exc)}), 400


@app.route("/api/goals/<int:goal_id>", methods=["PATCH"])
def update_goal_api(goal_id: int):
    data = request.json or {}; changes: dict[str, Any] = {}
    try:
        for key in ("name", "priority", "status"):
            if key in data: changes[key] = data[key]
        if "target" in data or "target_cents" in data: changes["target_cents"] = _savings_request_cents(data, "target")
        if "target_date" in data: changes["target_date"] = _parse_optional_date(data.get("target_date"))
        update_savings_goal(current_household_id(), goal_id, changes)
        return jsonify(savings_state(current_household_id(), pay_period_days=max(1, int(_household_account().pay_period_days or 14))))
    except (SavingsError, ValueError) as exc: db.session.rollback(); return jsonify({"error": str(exc)}), 400


@app.route("/api/reserves", methods=["POST"])
def create_reserve_api():
    data = request.json or {}
    try:
        create_savings_reserve(current_household_id(), operation_id=str(data.get("operation_id") or "").strip(), name=str(data.get("name") or ""), category=str(data.get("category") or "custom"), target_cents=_savings_request_cents(data, "target"), priority=int(data.get("priority", 100)))
        return jsonify(savings_state(current_household_id(), pay_period_days=max(1, int(_household_account().pay_period_days or 14)))), 201
    except (SavingsError, ValueError) as exc: db.session.rollback(); return jsonify({"error": str(exc)}), 400


@app.route("/api/reserves/<int:reserve_id>", methods=["PATCH"])
def update_reserve_api(reserve_id: int):
    data = request.json or {}; changes: dict[str, Any] = {}
    try:
        for key in ("name", "priority", "status"):
            if key in data: changes[key] = data[key]
        if "target" in data or "target_cents" in data: changes["target_cents"] = _savings_request_cents(data, "target")
        update_savings_reserve(current_household_id(), reserve_id, changes)
        return jsonify(savings_state(current_household_id(), pay_period_days=max(1, int(_household_account().pay_period_days or 14))))
    except (SavingsError, ValueError) as exc: db.session.rollback(); return jsonify({"error": str(exc)}), 400


@app.route("/api/savings/transfer", methods=["POST"])
def savings_transfer_api():
    data = request.json or {}
    if data.get("confirm") is not True: return jsonify({"error": "Review and confirm this transfer before saving.", "requires_confirmation": True}), 409
    try:
        source_id = int(data["source_destination_id"]) if data.get("source_destination_id") is not None else None
        transfer_type = str(data.get("transfer_type") or "transfer")
        purpose = str(data.get("purpose") or "")
        if transfer_type == "reserve_use" and source_id is not None:
            source_reserve = SavingsReserve.query.filter_by(household_id=current_household_id(), destination_id=source_id).first()
            if source_reserve is None: raise SavingsError("Reserve use must come from a Reserve.")
            match = match_reserve_purpose(purpose)
            if source_reserve.category != "emergency" and match.get("category") != source_reserve.category:
                return jsonify({"error": "The purpose does not clearly match this protected Reserve.", "purpose_match": match, "requires_review": True}), 409
        row = savings_transfer(current_household_id(), operation_id=str(data.get("operation_id") or "").strip(), amount_cents=_savings_request_cents(data), source_id=source_id, destination_id=int(data["destination_id"]) if data.get("destination_id") is not None else None, transfer_type=transfer_type, purpose=purpose)
        return jsonify({"transfer_id": row.id, "operation_id": row.operation_id, "is_expense": False, "state": savings_state(current_household_id(), pay_period_days=max(1, int(_household_account().pay_period_days or 14)))})
    except (SavingsError, ValueError) as exc: db.session.rollback(); return jsonify({"error": str(exc)}), 400


@app.route("/api/savings/allocation/preview", methods=["GET"])
def savings_allocation_preview_api():
    account = _household_account(); pyf = _compute_safe_to_spend_snapshot(account)
    if not pyf.get("complete"): return jsonify({"error": "Complete financial setup before allocating savings.", "pyf": pyf}), 409
    cycle_key = _savings_cycle_key(account, pyf)
    return jsonify(_current_savings_allocation_plan(current_household_id(), account, pyf, cycle_key=cycle_key))


@app.route("/api/savings/allocation/apply", methods=["POST"])
def savings_allocation_apply_api():
    data = request.json or {}
    if data.get("confirm") is not True: return jsonify({"error": "Review and confirm the allocation before saving.", "requires_confirmation": True}), 409
    operation_id = str(data.get("operation_id") or "").strip()
    if not operation_id: return jsonify({"error": "operation_id is required."}), 400
    account = _household_account(); pyf = _compute_safe_to_spend_snapshot(account)
    if not pyf.get("complete"): return jsonify({"error": "Complete financial setup before allocating savings."}), 409
    cycle_key = _savings_cycle_key(account, pyf)
    plan = _current_savings_allocation_plan(current_household_id(), account, pyf, cycle_key=cycle_key)
    try:
        run = apply_savings_allocation(current_household_id(), operation_id=operation_id, cycle_key=cycle_key, plan=plan)
        return jsonify({"allocation_run_id": run.id, "operation_id": run.operation_id, "plan": plan, "state": savings_state(current_household_id(), pay_period_days=max(1, int(account.pay_period_days or 14)))})
    except (SavingsError, IntegrityError) as exc: db.session.rollback(); return jsonify({"error": str(exc)}), 400


@app.route("/api/reserves/purpose-match", methods=["POST"])
def reserve_purpose_match_api():
    return jsonify(match_reserve_purpose(str((request.json or {}).get("text") or "")))


@app.route("/api/budget/summary", methods=["GET"])
def get_budget_summary():
    account = _household_account()
    if not account:
        return jsonify({"error": "Account settings missing"}), 400
    owner_scope = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    metrics = compute_liquidity_metrics(account)
    plan = income_plan_payload(current_household_id(), at=datetime.now(timezone.utc))
    metrics["account_state"] = {
        "checking_balance": round(float(account.checking_balance), 2) if account.checking_balance is not None else None,
        "pay_period_days": int(account.pay_period_days or 0),
        "expected_paycheck": (plan.get("current") or {}).get("expected_income"),
        "expected_paycheck_authority": "income_plan_v1",
        "next_expected_paycheck": (plan.get("pending") or {}).get("expected_income"),
        "next_expected_paycheck_effective_at": (plan.get("pending") or {}).get("effective_at"),
        "legacy_expected_paycheck_suggestion": (round(float(account.expected_paycheck), 2)
                                                  if not plan.get("current") and account.expected_paycheck is not None else None),
    }
    metrics["readiness"] = _household_readiness(account, owner_scope=owner_scope)
    metrics["safe_to_spend"] = _compute_safe_to_spend_snapshot(account, owner_scope=owner_scope)
    return jsonify(metrics)


@app.route("/api/paycheck-timeline", methods=["GET"])
def paycheck_timeline_api():
    """Read-only Package 15 projection; this endpoint never creates an account."""
    account = _household_account(create_if_missing=False)
    if account is None:
        return jsonify({
            "authority": "paycheck_timeline_v1", "read_only": True,
            "status": "unavailable", "setup_needed": True,
            "cycle": {"available": False, "missing": ["account", "authoritative_pay_schedule"]},
            "events": [], "important_events": [],
            "trajectory": {"status": "unavailable", "amount_cents": None, "amount": None,
                           "reasons": ["Complete financial and pay-cycle setup to use the timeline."]},
        })
    hid = current_household_id()
    owner_scope = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    now = datetime.now(timezone.utc)
    next_income = _infer_next_income(account, now)
    pyf = _compute_safe_to_spend_snapshot(account, owner_scope=owner_scope, now_utc=now)
    payload = build_paycheck_timeline(
        household_id=hid, account=account, now=now, next_income=next_income,
        pyf_snapshot=pyf,
        bill_query=lambda household_id, start, end: Bill.query.filter(
            Bill.household_id == household_id, Bill.due_date >= start, Bill.due_date < end,
        ).order_by(Bill.due_date.asc(), Bill.id.asc()).all(),
        transaction_query=lambda household_id, start, end: ExpenseTransaction.query.filter(
            ExpenseTransaction.household_id == household_id,
            ExpenseTransaction.date >= start, ExpenseTransaction.date < end,
        ).order_by(ExpenseTransaction.date.asc(), ExpenseTransaction.id.asc()).all(),
        transfer_query=lambda household_id, start, end: SavingsTransfer.query.filter(
            SavingsTransfer.household_id == household_id,
            SavingsTransfer.created_at >= start, SavingsTransfer.created_at < end,
        ).order_by(SavingsTransfer.created_at.asc(), SavingsTransfer.id.asc()).all(),
        allocation_query=lambda household_id, cycle_key: SavingsAllocationRun.query.filter_by(
            household_id=household_id, cycle_key=cycle_key,
        ).order_by(SavingsAllocationRun.id.asc()).all(),
        destination_query=lambda household_id: SavingsDestination.query.filter_by(
            household_id=household_id,
        ).order_by(SavingsDestination.id.asc()).all(),
    )
    payload["safe_to_spend_proof"] = {
        "authority": pyf.get("authority"),
        "safe_to_spend_cents": pyf.get("safe_to_spend_cents"),
        "trajectory_affects_safe_to_spend": False,
    }
    return jsonify(payload)


@app.route("/api/payday-recap", methods=["GET"])
def payday_recap_api():
    """Read-only Package 17 completed-cycle recap."""
    hid = current_household_id()
    account = Account.query.filter_by(household_id=hid).first()
    if account is None:
        return jsonify({
            "authority": "payday_recap_v1", "read_only": True,
            "status": "missing_setup", "completed_cycle": None,
            "finish_status": "unavailable", "finish_amount_cents": None,
            "finish_amount": None,
            "finish_reasons": ["Complete financial and pay-cycle setup before Rung can identify a finished cycle."],
            "protected_summary": None, "biggest_changes": [],
            "completed_cycle_detail": None, "current_cycle": None,
            "next_payday": None, "current_safe_to_spend_cents": None,
            "current_safe_to_spend": None,
            "safe_to_spend_authority": "canonical_pyf_v1",
            "current_setup_complete": False,
            "current_setup_missing": ["account", "authoritative_pay_schedule"],
            "informational_only": True, "financial_mutations": False,
            "safe_to_spend_effect_cents": 0,
        })
    now = datetime.now(timezone.utc)
    owner_scope = _resolve_request_user_id({"user_id": request.args.get("user_id")})
    current_safe = _compute_safe_to_spend_snapshot(account, owner_scope=owner_scope, now_utc=now)
    return jsonify(build_payday_recap(
        household_id=hid, account=account, now=now,
        next_income=_infer_next_income(account, now),
        current_safe_snapshot=current_safe,
        bill_query=lambda household_id, start, end: Bill.query.filter(
            Bill.household_id == household_id, Bill.due_date >= start, Bill.due_date < end,
        ).order_by(Bill.due_date.asc(), Bill.id.asc()).all(),
        transaction_query=lambda household_id, start, end: ExpenseTransaction.query.filter(
            ExpenseTransaction.household_id == household_id,
            ExpenseTransaction.date >= start, ExpenseTransaction.date < end,
        ).order_by(ExpenseTransaction.date.asc(), ExpenseTransaction.id.asc()).all(),
        transfer_query=lambda household_id, start, end: SavingsTransfer.query.filter(
            SavingsTransfer.household_id == household_id,
            SavingsTransfer.created_at >= start, SavingsTransfer.created_at < end,
        ).order_by(SavingsTransfer.created_at.asc(), SavingsTransfer.id.asc()).all(),
        allocation_query=lambda household_id, cycle_key: SavingsAllocationRun.query.filter_by(
            household_id=household_id, cycle_key=cycle_key,
        ).order_by(SavingsAllocationRun.id.asc()).all(),
        destination_query=lambda household_id: SavingsDestination.query.filter_by(
            household_id=household_id,
        ).order_by(SavingsDestination.id.asc()).all(),
        income_plan_resolver=lambda household_id, cycle_start: resolve_income_plan(
            household_id, at=cycle_start,
        ),
    ))


def _behavior_intelligence_snapshot() -> dict[str, Any]:
    hid = current_household_id()
    now = datetime.now(timezone.utc)
    # Intelligence is useful before balance setup and must not create an
    # Account merely because a household reads or saves an advisory choice.
    account = Account.query.filter_by(household_id=hid).first()
    return build_behavior_intelligence(
        household_id=hid,
        transactions=ExpenseTransaction.query.filter_by(household_id=hid).order_by(
            ExpenseTransaction.date.asc(), ExpenseTransaction.id.asc()).all(),
        bills=Bill.query.filter_by(household_id=hid).order_by(Bill.id.asc()).all(),
        decisions=BehaviorIntelligenceDecision.query.filter_by(household_id=hid).order_by(
            BehaviorIntelligenceDecision.created_at.asc(), BehaviorIntelligenceDecision.id.asc()).all(),
        now=now,
        checking_cents=_money_to_cents(account.checking_balance, field_name="checking_balance") if account and account.checking_balance is not None else None,
    )


@app.route("/api/behavior-intelligence", methods=["GET"])
def behavior_intelligence_api():
    return jsonify(_behavior_intelligence_snapshot())


@app.route("/api/behavior-intelligence/decision", methods=["POST"])
def behavior_intelligence_decision_api():
    data = request.json or {}
    operation_id = str(data.get("operation_id") or "").strip()
    candidate_key = str(data.get("candidate_key") or "").strip().lower()
    action = str(data.get("action") or "").strip().lower()
    classification = str(data.get("classification") or "").strip().lower() or None
    if not operation_id or not candidate_key or action not in {"ignore", "important", "classify"}:
        return jsonify({"error": "operation_id, candidate_key, and a supported action are required."}), 400
    if action == "classify" and classification not in {"need", "discretionary", "transfer"}:
        return jsonify({"error": "A supported household classification is required."}), 400
    if action != "classify": classification = None
    hid = current_household_id()
    existing = BehaviorIntelligenceDecision.query.filter_by(household_id=hid, operation_id=operation_id).first()
    requested = (candidate_key, action, classification)
    if existing:
        if (existing.candidate_key, existing.action, existing.classification) != requested:
            return jsonify({"error": "operation_id was already used for a different decision."}), 409
        return jsonify({"decision_id": existing.id, "already_applied": True, "intelligence": _behavior_intelligence_snapshot()})
    try:
        row = BehaviorIntelligenceDecision(
            household_id=hid, operation_id=operation_id, candidate_key=candidate_key,
            action=action, classification=classification,
            pattern_signature=str(data.get("pattern_signature") or "").strip() or None,
            typical_amount_cents=int(data["typical_amount_cents"]) if data.get("typical_amount_cents") is not None else None,
            cadence_days=int(data["cadence_days"]) if data.get("cadence_days") is not None else None,
            occurrence_count=int(data["occurrence_count"]) if data.get("occurrence_count") is not None else None,
        )
        db.session.add(row); db.session.commit()
    except (TypeError, ValueError):
        db.session.rollback(); return jsonify({"error": "Decision evidence must use whole-number values."}), 400
    except IntegrityError:
        db.session.rollback()
        existing = BehaviorIntelligenceDecision.query.filter_by(household_id=hid, operation_id=operation_id).first()
        if existing:
            if (existing.candidate_key, existing.action, existing.classification) != requested:
                return jsonify({"error": "operation_id was already used for a different decision."}), 409
            return jsonify({"decision_id": existing.id, "already_applied": True, "intelligence": _behavior_intelligence_snapshot()})
        raise
    return jsonify({"decision_id": row.id, "already_applied": False, "intelligence": _behavior_intelligence_snapshot()}), 201


@app.route("/api/behavior-intelligence/stage-recurring-bill", methods=["POST"])
def behavior_intelligence_stage_recurring_bill_api():
    data = request.json or {}; candidate_key = str(data.get("candidate_key") or "").strip().lower()
    candidate = next((row for row in _behavior_intelligence_snapshot()["recurring_candidates"] if row["candidate_key"] == candidate_key), None)
    if candidate is None or candidate.get("existing_bill"):
        return jsonify({"error": "That recurring candidate is unavailable or already represented by a Bill."}), 404
    evidence = candidate["evidence"]; due = datetime.now(timezone.utc) + timedelta(days=max(1, int(evidence.get("cadence_days") or 30)))
    staged = {
        "operation_id": "op_behavior_bill_" + uuid.uuid4().hex,
        "bills_added": [{"name": candidate["canonical_merchant"].title(), "amount": _cents_to_float(int(evidence["typical_amount_cents"])), "due_date": due.date().isoformat()}],
        "requires_confirmation": True, "staged": True,
        "summary": "Review this possible recurring charge before adding one recurring Bill.",
    }
    staged["operation_binding"] = _copilot_stage_binding(staged["operation_id"])
    return jsonify({"staged_actions": staged, "candidate": candidate, "financial_mutations": False})


@app.route("/api/behavior-intelligence/savings-preview", methods=["POST"])
def behavior_intelligence_savings_preview_api():
    data = request.json or {}; candidate_key = str(data.get("candidate_key") or "").strip().lower()
    try: percent = int(data.get("reduction_percent") or 50)
    except (TypeError, ValueError): return jsonify({"error": "reduction_percent must be 25, 50, or 75."}), 400
    if percent not in {25, 50, 75}: return jsonify({"error": "reduction_percent must be 25, 50, or 75."}), 400
    opportunity = next((row for row in _behavior_intelligence_snapshot()["opportunities"] if row["candidate_key"] == candidate_key), None)
    if opportunity is None: return jsonify({"error": "That savings opportunity is unavailable."}), 404
    amount_cents = int(opportunity["projection"]["reductions"][str(percent)]["period_savings_cents"])
    hid = current_household_id(); account = _household_account(create_if_missing=False)
    kinds = {row.kind for row in SavingsDestination.query.filter_by(household_id=hid).all()}
    plan = None
    if {"flexible", "wealth_cash", "wealth_investment"} <= kinds:
        plan = savings_allocation_plan(hid, amount_cents, pay_period_days=max(1, int(account.pay_period_days or 14)) if account else 14)
    return jsonify({"candidate_key": candidate_key, "reduction_percent": percent, "hypothetical_cents": amount_cents, "hypothetical": _cents_to_float(amount_cents), "allocation_preview": plan, "basis": opportunity["projection"]["basis"], "requires_confirmation_for_any_future_change": True, "mutated": False})


@app.route("/api/settings/safe-to-spend", methods=["GET", "POST"])
def safe_to_spend_settings():
    if request.method == "GET":
        return jsonify({
            "protected_buffer": _cents_to_float(_get_safe_buffer_cents()),
        })

    data = request.json or {}
    if "protected_buffer" not in data:
        return jsonify({"error": "protected_buffer is required."}), 400
    try:
        buffer_cents = _money_to_cents(data.get("protected_buffer"), field_name="protected_buffer")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    set_setting(SAFE_BUFFER_SETTING_KEY, f"{_cents_to_float(buffer_cents):.2f}")
    return jsonify({"protected_buffer": _cents_to_float(buffer_cents)})


@app.route("/api/settings/pay-yourself-first", methods=["GET", "POST"])
def pay_yourself_first_settings():
    current = _explicit_household_setting_decimal(PYF_TARGET_SETTING_KEY)
    if request.method == "GET":
        return jsonify({
            "long_term_savings_target_percent": float(current) if current is not None and current >= 0 else None,
        })

    data = request.json or {}
    if "long_term_savings_target_percent" not in data:
        return jsonify({"error": "long_term_savings_target_percent is required."}), 400
    try:
        target = Decimal(str(data.get("long_term_savings_target_percent")))
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"error": "Savings target must be a valid percentage."}), 400
    if target < 0:
        return jsonify({"error": "Savings target cannot be negative."}), 400
    set_setting(PYF_TARGET_SETTING_KEY, format(target.normalize(), "f"))
    return jsonify({"long_term_savings_target_percent": float(target)})

@app.route("/api/settings/location-sharing", methods=["GET", "POST"])
def location_sharing_settings():
    """Read or toggle Location Sharing for the current household.

    Location Sharing controls whether device-location-based nearby-store
    discovery is permitted. It never selects or changes the canonical
    shopping store.
    """
    if request.method == "GET":
        enabled = get_setting(LOCATION_SHARING_SETTING_KEY, 'false') == 'true'
        return jsonify({"location_sharing_enabled": enabled})

    data = request.json or {}
    if "location_sharing_enabled" not in data:
        return jsonify({"error": "location_sharing_enabled is required."}), 400
    if not isinstance(data.get("location_sharing_enabled"), bool):
        return jsonify({"error": "location_sharing_enabled must be true or false."}), 400

    set_setting(
        LOCATION_SHARING_SETTING_KEY,
        "true" if data["location_sharing_enabled"] else "false",
    )
    return jsonify({"location_sharing_enabled": data["location_sharing_enabled"]})


@app.route("/api/settings/current-location", methods=["GET"])
def current_location_settings():
    """Read-only current device-location context from persisted account state.

    Returns the stored ZIP, city/state, and selected-store information so
    Settings can display a read-only location context. Device GPS may
    refresh nearby-store discovery context but must never silently
    select or change the canonical shopping store.
    """
    account = _household_account()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    selected = get_selected_store(current_household_id(), account=account)
    return jsonify({
        "zip_code": account.zip_code or "",
        "city_state": account.city_state or "",
        "latitude": account.latitude,
        "longitude": account.longitude,
        "selected_store": {
            "retailer": selected.get("retailer", ""),
            "name": selected.get("name", ""),
            "store_id": selected.get("store_id", ""),
            "address": selected.get("address", ""),
            "canonical": selected.get("canonical", False),
        },
        "location_sharing_enabled": get_setting(LOCATION_SHARING_SETTING_KEY, 'false') == 'true',
    })


@app.route("/api/decision/can-i-buy", methods=["POST"])
def can_i_buy():
    """Deterministic affordability check against current Safe-to-Spend.

    This endpoint is advisory-only and never writes any financial rows.
    """
    account = _household_account()
    if not account:
        return jsonify({"error": "Account settings missing"}), 400

    data = request.json or {}
    item_name = str(data.get("item_name") or "Proposed purchase").strip() or "Proposed purchase"
    try:
        purchase_cents = _money_to_cents(data.get("cost"), field_name="cost")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    owner_scope = _resolve_request_user_id(data)
    purchase_context = str(data.get("purchase_context") or "selected_physical_store").strip().lower()
    category = str(data.get("tax_category") or "unknown").strip().lower()
    category_evidence = {
        "general_merchandise": "household soap",
        "grocery_food": "grocery milk",
        "prepared_food": "prepared takeout meal",
        "exempt": "prescription medicine",
    }.get(category, item_name)
    selected_store = get_selected_store(current_household_id(), account=account)
    if purchase_context == "selected_physical_store" and selected_store.get("canonical"):
        retailer = str(selected_store.get("retailer") or "unknown")
        store_id = str(selected_store.get("store_id") or "unknown")
        store_name = str(selected_store.get("name") or "Selected store")
        store_address = str(selected_store.get("address") or "")
        postal_code = str(selected_store.get("postal_code") or "")
        city_state = ", ".join(filter(None, [str(selected_store.get("city") or "").strip(), str(selected_store.get("state") or "").strip()]))
    elif purchase_context in {"manual_local", "online_delivery"}:
        retailer = "manual"
        context_state = str(data.get("state") or "").strip().upper()
        context_postal = _normalize_zip_code(data.get("postal_code") or "")
        store_id = f"{purchase_context}:{context_state or 'unknown'}:{context_postal or 'unknown'}"
        store_name = "Confirmed purchase location" if purchase_context == "manual_local" else "Delivery destination"
        store_address = ""
        postal_code = context_postal
        city_state = ", ".join(filter(None, [str(data.get("city") or "").strip(), str(data.get("state") or "").strip().upper()]))
    else:
        retailer, store_id, store_name, store_address, postal_code, city_state = "unknown", "unknown", "Unspecified purchase", "", "", ""

    try:
        actual_tax_cents = _money_to_cents(data.get("actual_tax"), field_name="actual_tax") if data.get("actual_tax") not in (None, "") else None
        actual_total_cents = _money_to_cents(data.get("actual_total"), field_name="actual_total") if data.get("actual_total") not in (None, "") else None
        tax_payload = _apply_owned_tax_to_cart(
            account=account,
            owner_scope=owner_scope,
            cart_items=[{"item_name": category_evidence, "estimated_price": _cents_to_float(purchase_cents)}],
            retailer=retailer,
            store_name=store_name,
            store_id=store_id,
            store_address=store_address,
            postal_code=postal_code,
            city_state=city_state,
            purchase_context=purchase_context,
            actual_tax_cents=actual_tax_cents,
            actual_total_cents=actual_total_cents,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    safe = _compute_safe_to_spend_snapshot(account, owner_scope=owner_scope)
    if safe.get("state") == "needs_setup":
        return jsonify({
            "error": "Safe-to-Spend is unavailable until setup is complete.",
            "code": "needs_setup",
            "readiness": safe.get("readiness") or _household_readiness(account, owner_scope=owner_scope),
        }), 409
    now_safe_cents = int(safe.get("safe_to_spend_cents") or 0)
    decision = tax_payload["tax_engine"]
    total_cents = decision.get("total_cents")
    if total_cents is None:
        return jsonify({
            "item_name": item_name,
            "purchase": _cents_to_float(purchase_cents),
            "purchase_total": None,
            "safe_to_spend_now": _cents_to_float(now_safe_cents),
            "safe_to_spend_after": None,
            "short_by": None,
            "approved": None,
            "tax": decision,
            "message": "Tax is not included yet. Confirm the purchase jurisdiction and item category before relying on a final affordability answer.",
            "financial_mutations": False,
        })
    after_cents = now_safe_cents - int(total_cents)
    short_by_cents = max(0, -after_cents)
    approved = after_cents >= 0

    return jsonify({
        "item_name": item_name,
        "purchase": _cents_to_float(purchase_cents),
        "purchase_total": _cents_to_float(int(total_cents)),
        "safe_to_spend_now": _cents_to_float(now_safe_cents),
        "safe_to_spend_after": _cents_to_float(after_cents),
        "short_by": _cents_to_float(short_by_cents),
        "approved": approved,
        "tax": decision,
        "financial_mutations": False,
        "message": (
            f"Yes, the {decision['label'].lower()} tax-inclusive amount fits within your current Safe-to-Spend amount."
            if approved
            else f"The {decision['label'].lower()} tax-inclusive amount is ${_cents_to_float(short_by_cents):.2f} over your current Safe-to-Spend amount."
        ),
    })


def _dispatch_parsed(parsed: dict, user_text: str = "", user_id: str = "anonymous") -> dict:
    """Execute a parsed Copilot result against the database and report actions.

    Shared by ``/api/copilot/parse`` (single-turn) and ``/api/copilot/chat``
    (multi-turn hybrid).  Handles both the native tool-calling path
    (``tool_results`` — actions already persisted by ``execute_app_function``)
    and the legacy flat-list path (``bill_updates``, ``discretionary_events``,
    ``grocery_additions``, ``selected_recipes`` — dispatched here).

    Returns the ``actions_taken`` dict for the API response.
    """
    actions = {
        "bills_added": [],
        "bills_removed": [],
        "expenses_logged": [],
        "income_logged": [],
        "balance_reconciliations": [],
        "shopping_trip_corrections": [],
        "grocery_items_added": [],
        "recipes_added": [],        # recipes actually persisted to the meal plan
        "recipes_auto_filled": [],  # recommendation engine fills the gap
        "recipes_removed": [],
        "recipes_suggested": [],    # requested titles with NO local match
        "target_meals": parsed.get("target_meals"),
    }

    # ---- Process tool_results (Groq native tool-calling path) ----
    # When the LLM uses native tool calling, the actions were already
    # persisted by execute_app_function — we just report them.
    tool_results = parsed.get("tool_results", [])
    if tool_results:
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
                    actions["recipes_added"].append({
                        "id": data.get("id"),
                        "title": data.get("title", ""),
                    })
                elif act == "removed":
                    actions["recipes_removed"].append({
                        "id": data.get("id"),
                        "title": data.get("title", ""),
                    })
            elif tool_name == "log_discretionary_expense":
                actions["expenses_logged"].append({
                    "description": data.get("description", ""),
                    "amount": data.get("amount", 0),
                })
            elif tool_name == "set_target_meals":
                target_meals = data.get("target_meals", parsed.get("target_meals"))
                actions["target_meals"] = target_meals
        return actions

    # ---- Legacy / intent-based dispatch path (no native tool_results) ----
    # If the LLM didn't set target_meals, check the raw user text for a number.
    if parsed.get("target_meals") is None and user_text:
        m = re.search(r'(\d+)\s*(?:meals?|dinners?|dishes?|recipes?)', user_text, re.IGNORECASE)
        if m:
            parsed = dict(parsed)
            parsed["target_meals"] = int(m.group(1))

    intent_payload = parse_intent_payload(parsed, user_text)
    if (
        intent_payload.meal_request
        or intent_payload.expenses
        or intent_payload.income_events
        or intent_payload.balance_reconciliation
        or intent_payload.shopping_corrections
        or intent_payload.bill_adjustments
        or intent_payload.groceries
    ):
        return execute_intent_payload(intent_payload, user_id=user_id)

    # Nothing actionable found.
    return actions


def _public_copilot_error(technical_error: str | None) -> str | None:
    """Return a customer-safe Copilot error string.

    Technical provider details stay server-side in logs and are never exposed
    to browser clients.
    """
    if not technical_error:
        return None
    LOGGER.warning("Copilot provider error: %s", technical_error)
    return "Copilot is temporarily unavailable. Please try again later."


def _public_parsed_payload(parsed: dict[str, Any] | None) -> dict[str, Any]:
    """Return only user-safe parsed fields for API responses."""
    if not isinstance(parsed, dict):
        return {}
    allowed_keys = {
        "tool_results",
        "selected_recipes",
        "grocery_additions",
        "shopping_requirements",
        "discretionary_events",
        "spending_events",
        "income_events",
        "balance_reconciliation",
        "shopping_corrections",
        "bill_updates",
        "target_meals",
        "meal_servings",
        "clarification_question",
    }
    out = {key: parsed.get(key) for key in allowed_keys if key in parsed}
    meta = parsed.get("_parse_meta")
    if isinstance(meta, dict):
        out["_parse_meta"] = {
            "path": meta.get("path"),
            "llm_calls": meta.get("llm_calls"),
            "repair_attempted": meta.get("repair_attempted"),
            "validation": meta.get("validation"),
            "latency_ms": meta.get("latency_ms"),
        }
    return out


def _record_llm_usage(owner_scope: str, payload: dict[str, Any], operation: str) -> None:
    """Persist one LLM usage event when a Copilot path actually called an external model."""
    if not isinstance(payload, dict):
        return
    raw_meta = payload.get("_parse_meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    raw_usage = payload.get("_llm_usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    llm_calls = int(usage.get("llm_calls") or meta.get("llm_calls") or 0)
    if llm_calls <= 0:
        return

    provider = str(usage.get("provider") or "groq").strip().lower() or "groq"
    model = str(usage.get("model") or "").strip() or None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    request_id = str(usage.get("request_id") or "").strip() or None

    cost = estimate_usage_cost(
        category="llm",
        provider=provider,
        operation=operation,
        request_count=llm_calls,
        llm_provider=provider,
        llm_model=model,
        input_tokens=(int(input_tokens) if input_tokens is not None else None),
        output_tokens=(int(output_tokens) if output_tokens is not None else None),
    )
    record_usage_event(
        owner_scope=owner_scope,
        category="llm",
        provider=provider,
        operation=operation,
        success=True,
        external_call=True,
        request_count=llm_calls,
        llm_provider=provider,
        llm_model=model,
        input_tokens=(int(input_tokens) if input_tokens is not None else None),
        output_tokens=(int(output_tokens) if output_tokens is not None else None),
        estimated_cost_micros=cost.get("estimated_cost_micros"),
        cost_status=cost.get("cost_status"),
        cost_rate_key=cost.get("cost_rate_key"),
        request_id=request_id,
        metadata={"path": meta.get("path") or payload.get("_llm_path")},
    )


def _parse_copilot_prompt_compat(
    user_text: str,
    *,
    staging_only: bool = False,
    allow_llm: bool = True,
) -> dict[str, Any]:
    """Call parse_copilot_prompt while tolerating older monkeypatched signatures in tests."""
    try:
        return parse_copilot_prompt(
            user_text,
            staging_only=staging_only,
            allow_llm=allow_llm,
        )
    except TypeError:
        try:
            return parse_copilot_prompt(user_text, staging_only=staging_only)
        except TypeError:
            return parse_copilot_prompt(user_text)


@app.route("/api/copilot/parse", methods=["POST"])
def copilot_parse():
    """AI Copilot — parse a single natural-language message and dispatch actions.

    Legacy single-turn endpoint (no conversation history).  The multi-turn
    hybrid chat lives at ``/api/copilot/chat``.

    Request body
    ------------
    {"text": "Cook chicken rice bowl. Add Netflix $22.99/mo. I need dish soap."}

    Returns (200)
    -------------
    {
      "parsed": { <structured intent from LLM/regex> },
      "actions_taken": { ... },
      "tool_results": [...],
      "_fallback": bool,
      "llm_error": str|null
    }
    """
    data = request.json or {}
    user_text = data.get("text", "").strip()
    user_id = _resolve_request_user_id(data)
    if not user_text:
        return jsonify({"error": "Provide 'text' field with your request"}), 400

    llm_gate = check_optional_operation(user_id, "llm_call")

    # ---- Parse (server-side provider credentials only) ----
    parsed = _parse_copilot_prompt_compat(user_text, allow_llm=bool(llm_gate.get("allowed", True)))
    if not llm_gate.get("allowed", True):
        parsed["_llm_error"] = llm_gate.get("message") or "Copilot advanced model calls are currently unavailable."

    actions = _dispatch_parsed(parsed, user_text, user_id=user_id)
    _record_llm_usage(user_id, parsed, "copilot_parse")

    return jsonify({
        "parsed": _public_parsed_payload(parsed),
        "actions_taken": actions,
        "tool_results": parsed.get("tool_results", []),
        "_fallback": parsed.get("_fallback", False),
        "llm_error": _public_copilot_error(parsed.get("_llm_error")),
        "clarification_question": parsed.get("clarification_question"),
    })


@app.route("/api/copilot/chat", methods=["POST"])
def copilot_chat():
    """Multi-turn hybrid chat: conversational replies + action execution.

    Request body
    ------------
    {
      "messages": [
        {"role": "user", "content": "How much can I spend on food?"},
        {"role": "assistant", "content": "You have about $X left..."},
        {"role": "user", "content": "Add Netflix $22.99/mo"}
      ]
    }

    Returns (200)
    -------------
    {
      "reply": str,               # the assistant's conversational response
      "actions_taken": { ... },   # what was executed (bills, groceries, ...)
      "tool_results": [...],
      "_fallback": bool,
      "llm_error": str|null
    }
    """
    data = request.json or {}
    messages = data.get("messages") or []
    user_id = _resolve_request_user_id(data)
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "Provide 'messages' (non-empty list of {role, content})"}), 400
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            return jsonify({"error": "Each message needs role 'user' or 'assistant'"}), 400
    if messages[-1].get("role") != "user":
        return jsonify({"error": "Last message must be from the user"}), 400

    llm_gate = check_optional_operation(user_id, "llm_call")

    # ---- Chat (server-side provider credentials only) ----
    if llm_gate.get("allowed", True):
        result = _copilot_service.chat_copilot_prompt(messages)
    else:
        user_text = str(messages[-1].get("content") or "")
        parsed = _parse_copilot_prompt_compat(user_text, allow_llm=False)
        parsed["_llm_error"] = llm_gate.get("message") or "Copilot advanced model calls are currently unavailable."
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
        result = dict(parsed)
        result["reply"] = (
            "I can still run deterministic commands right now. "
            "Please try again later for open-ended Copilot chat."
            if not has_actions else ""
        )

    user_text = messages[-1].get("content", "")
    # If the model returned a plain-text reply but the regex fallback
    # detected actionable items (no native tool calls), apply conservative
    # gating: compute intent payload and require confirmation for risky
    # items. This prevents the model from silently causing high-impact
    # changes when it didn't use native tool calling.
    is_plain_text_with_actions = (
        not result.get("tool_results")
        and bool(result.get("reply"))
        and (
            result.get("grocery_additions")
            or result.get("bill_updates")
            or result.get("discretionary_events")
            or result.get("spending_events")
            or result.get("income_events")
            or result.get("balance_reconciliation")
            or result.get("shopping_corrections")
            or result.get("selected_recipes")
        )
    )

    if is_plain_text_with_actions:
        # Parse intent and run the non-confirming executor so the response
        # can indicate `requires_confirmation` and `pending_actions`.
        intent_payload = parse_intent_payload(result, user_text)
        from services.copilot_intent import _execute_intent_payload

        actions = _execute_intent_payload(intent_payload, confirm=False, user_id=user_id)
    else:
        actions = _dispatch_parsed(result, user_text, user_id=user_id)

    _record_llm_usage(user_id, result, "copilot_chat")

    response = {
        "reply": result.get("reply", ""),
        "actions_taken": actions,
        "tool_results": result.get("tool_results", []),
        "_fallback": result.get("_fallback", False),
        "llm_error": _public_copilot_error(result.get("_llm_error")),
        "clarification_question": result.get("clarification_question"),
    }
    if actions.get("requires_confirmation"):
        response["confirmation_prompt"] = _build_confirmation_prompt(actions)
    return jsonify(response)


def _copilot_read_only_financial_response(user_text: str, *, user_id: str) -> dict[str, Any] | None:
    """Answer bounded financial questions from the canonical PYF snapshot.

    This is deliberately a presentation adapter: it does not recalculate
    Needs, savings feasibility, the protected buffer, or expected income, and
    it performs no writes. Exact historical change attribution is not claimed
    because Rung does not currently retain a complete before/after provenance
    read model for Safe-to-Spend.
    """
    text = str(user_text or "").strip()
    lowered = re.sub(r"\s+", " ", text.lower())
    if not lowered or re.search(r"\bgoal\b", lowered):
        return None

    explanation_request = bool(
        re.search(r"\bwhy\b.*\bsafe[ -]?to[ -]?spend\b", lowered)
        or re.search(r"\bexplain\b.*\bsafe[ -]?to[ -]?spend\b", lowered)
        or re.search(r"\bwhat(?:'s| is)?\s+(?:affecting|driving|determining)\b.*\bsafe[ -]?to[ -]?spend\b", lowered)
    )
    affordability_request = bool(
        re.search(r"\bcan i (?:afford|spend|buy)\b", lowered)
        or re.search(r"\bis\s+\$?[0-9][0-9,]*(?:\.\d{1,2})?\s+(?:safe|okay|ok)\s+(?:for me\s+)?to spend\b", lowered)
    )
    if not explanation_request and not affordability_request:
        return None

    account = _household_account(create_if_missing=False)
    snapshot = _compute_safe_to_spend_snapshot(account, owner_scope=user_id) if account is not None else {
        "complete": False,
        "state": "needs_setup",
        "missing_setup": ["account"],
        "readiness": {"ready": False, "missing_critical": ["account"]},
    }

    base_actions: dict[str, Any] = {
        "requires_confirmation": False,
        "staged": False,
        "read_only": True,
        "financial_mutations": False,
    }
    base_response: dict[str, Any] = {
        "parsed": {"path": "deterministic_financial_read_only_v1"},
        "actions_taken": base_actions,
        "tool_results": [],
        "_fallback": False,
        "llm_error": None,
        "clarification_question": None,
        "user_id": user_id,
    }

    if not snapshot.get("complete"):
        missing = list(snapshot.get("missing_setup") or [])
        labels = {
            "account": "account setup",
            "checking_balance": "current checking balance",
            "pay_period_days": "pay-cycle schedule",
            "payday": "payday",
            "current_period_income": "effective-dated expected income",
            "long_term_savings_target_percent": "Pay Yourself First target",
            "protected_checking_buffer": "protected checking buffer",
            "grocery_need": "required grocery amount",
            "fuel_or_transport_need": "required fuel or transportation amount",
        }
        missing_text = ", ".join(labels.get(item, item.replace("_", " ")) for item in missing) or "financial setup"
        base_actions.update({
            "summary": f"I can’t answer that truthfully until Rung has: {missing_text}. No financial state was changed.",
            "setup_needed": True,
            "missing_setup": missing,
        })
        base_response["parsed"].update({"intent": "safe_to_spend_explanation" if explanation_request else "purchase_affordability"})
        return base_response

    safe_cents = int(snapshot.get("safe_to_spend_cents") or 0)
    components = snapshot.get("components") or {}
    breakdown = snapshot.get("breakdown") or {}
    lines = {str(row.get("key")): row for row in (breakdown.get("lines") or []) if isinstance(row, dict)}

    if explanation_request:
        checking_cents = int((lines.get("checking") or {}).get("amount_cents") or 0)
        needs_cents = abs(int((lines.get("needs") or {}).get("amount_cents") or 0))
        savings_cents = abs(int((lines.get("savings") or {}).get("amount_cents") or 0))
        buffer_cents = abs(int((lines.get("buffer") or {}).get("amount_cents") or 0))
        summary = (
            "I can explain what determines your current Safe-to-Spend, but Rung does not yet have complete verified "
            "before-and-after provenance to attribute the exact change to one event. "
            f"Your current Safe-to-Spend is ${_cents_to_float(safe_cents):,.2f}: current checking is "
            f"${_cents_to_float(checking_cents):,.2f}, with ${_cents_to_float(needs_cents):,.2f} protected for "
            f"current Needs, ${_cents_to_float(savings_cents):,.2f} protected for this cycle’s Pay Yourself First "
            f"contribution, and ${_cents_to_float(buffer_cents):,.2f} kept as your checking buffer. "
            "I won’t claim that a recent transaction caused the change without verified causal history."
        )
        base_actions.update({
            "summary": summary,
            "intent": "safe_to_spend_explanation",
            "safe_to_spend_cents": safe_cents,
            "causal_provenance": "not_available",
            "canonical_components": {
                "checking_cents": checking_cents,
                "needs_cents": needs_cents,
                "pyf_protection_cents": savings_cents,
                "protected_buffer_cents": buffer_cents,
            },
        })
        base_response["parsed"].update({"intent": "safe_to_spend_explanation"})
        return base_response

    amount_match = re.search(r"\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text)
    if amount_match is None:
        amount_match = re.search(r"\b([0-9][0-9,]*(?:\.\d{1,2})?)\b", text)
    if amount_match is None:
        return None
    try:
        requested_cents = _money_to_cents(amount_match.group(1).replace(",", ""), field_name="purchase amount")
    except ValueError:
        return None
    remaining_cents = safe_cents - requested_cents
    fits = remaining_cents >= 0
    if fits:
        summary = (
            f"Yes. ${_cents_to_float(requested_cents):,.2f} fits within your current "
            f"${_cents_to_float(safe_cents):,.2f} Safe-to-Spend, leaving ${_cents_to_float(remaining_cents):,.2f}. "
            "Current Needs, Pay Yourself First protection, and your checking buffer remain protected."
        )
    else:
        summary = (
            f"No. ${_cents_to_float(requested_cents):,.2f} is ${_cents_to_float(abs(remaining_cents)):,.2f} above your "
            f"current ${_cents_to_float(safe_cents):,.2f} Safe-to-Spend. Rung does not treat protected savings, "
            "current Needs, or your checking buffer as spending permission."
        )
    base_actions.update({
        "summary": summary,
        "intent": "purchase_affordability",
        "requested_amount_cents": requested_cents,
        "safe_to_spend_cents": safe_cents,
        "remaining_after_purchase_cents": remaining_cents,
        "fits": fits,
        "canonical_authority": "canonical_pyf_v1",
        "protected_components": components,
    })
    base_response["parsed"].update({"intent": "purchase_affordability", "requested_amount_cents": requested_cents})
    return base_response


@app.route("/api/copilot/stage", methods=["POST"])
def copilot_stage():
    """Generate a dry-run Copilot proposal with zero DB mutations."""
    data = request.json or {}
    user_text = (data.get("text") or "").strip()
    user_id = _resolve_request_user_id(data)
    if not user_text:
        return jsonify({"error": "Provide 'text' field with your request"}), 400

    # Goals use a deterministic financial parser before any optional model.
    # This intentionally supports a narrow, reviewable form and leaves
    # ambiguous language unresolved instead of granting an LLM write authority.
    goal_match = re.search(
        r"(?:add|create|save for|afford)\s+(?:a\s+)?(?P<name>[a-z][a-z0-9 '&-]{1,80}?)(?:\s+goal)?\s+(?:for|of|target(?:ing)?|cost(?:ing)?)\s*\$?(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)",
        user_text,
        flags=re.IGNORECASE,
    )
    goal_language = bool(re.search(r"\bgoal\b", user_text, flags=re.IGNORECASE) or re.search(r"\b(?:save for|afford)\b", user_text, flags=re.IGNORECASE))
    if goal_match and goal_language:
        name = re.sub(r"\s+goal$", "", goal_match.group("name"), flags=re.IGNORECASE).strip().title()
        amount_cents = _money_to_cents(goal_match.group("amount").replace(",", ""), field_name="goal target")
        date_match = re.search(r"(?:by|before)\s+(\d{4}-\d{2}-\d{2})", user_text, flags=re.IGNORECASE)
        target_date = date_match.group(1) if date_match else None
        operation_id = "op_goal_" + uuid.uuid4().hex
        account = _household_account()
        preview_goal = {"name": name, "target_cents": amount_cents, "target_amount": _cents_to_float(amount_cents), "target_date": target_date, "priority": 100}
        plan = savings_allocation_plan(current_household_id(), int((_compute_safe_to_spend_snapshot(account).get("feasible_savings_cents") or 0)), pay_period_days=max(1, int(account.pay_period_days or 14)))
        staged = {"operation_id": operation_id, "goals_added": [preview_goal], "requires_confirmation": True, "staged": True, "summary": f"Add {name} as a Goal after review.", "allocation_effect": plan}
        staged["operation_binding"] = _copilot_stage_binding(operation_id)
        return jsonify({"parsed": {"goal": preview_goal, "path": "deterministic_goal_v1"}, "actions_taken": staged, "tool_results": [], "_fallback": False, "llm_error": None, "clarification_question": None, "user_id": user_id})

    # Read-only financial questions use the same canonical PYF snapshot as
    # Overview and /api/budget/summary. Keep this ahead of the generic parser:
    # that parser treats words such as "afford" as a possible mutation and can
    # otherwise ask for a balance Rung already authoritatively knows.
    read_only_financial = _copilot_read_only_financial_response(user_text, user_id=user_id)
    if read_only_financial is not None:
        return jsonify(read_only_financial)

    llm_gate = check_optional_operation(user_id, "llm_call")
    parsed = _parse_copilot_prompt_compat(
        user_text,
        staging_only=True,
        allow_llm=bool(llm_gate.get("allowed", True)),
    )
    if not llm_gate.get("allowed", True):
        parsed["_llm_error"] = llm_gate.get("message") or "Copilot advanced model calls are currently unavailable."

    # Native tool calls are intentionally disabled in staging_only parse mode,
    # but keep a defensive fallback so the endpoint remains stable.
    if parsed.get("tool_results"):
        return jsonify({
            "parsed": _public_parsed_payload(parsed),
            "actions_taken": {
                "requires_confirmation": True,
                "staged": True,
                "summary": "Tool-call actions need confirmation before apply.",
                "tool_results": parsed.get("tool_results", []),
            },
            "tool_results": parsed.get("tool_results", []),
            "_fallback": parsed.get("_fallback", False),
            "llm_error": _public_copilot_error(parsed.get("_llm_error")),
            "clarification_question": parsed.get("clarification_question"),
            "user_id": user_id,
        })

    from services.copilot_intent import parse_intent_payload, stage_intent_payload
    intent_payload = parse_intent_payload(parsed, user_text)
    staged = stage_intent_payload(intent_payload, user_id=user_id)
    staged["operation_binding"] = _copilot_stage_binding(str(staged.get("operation_id") or ""))
    parse_meta = parsed.get("_parse_meta") if isinstance(parsed, dict) else {}
    if isinstance(parse_meta, dict):
        LOGGER.info(
            "copilot.stage parser_path=%s llm_calls=%s repair=%s validation=%s operation_id=%s latency_ms=%s",
            parse_meta.get("path"),
            parse_meta.get("llm_calls"),
            parse_meta.get("repair_attempted"),
            parse_meta.get("validation"),
            staged.get("operation_id"),
            parse_meta.get("latency_ms"),
        )
    return jsonify({
        "parsed": _public_parsed_payload(parsed),
        "actions_taken": staged,
        "tool_results": parsed.get("tool_results", []),
        "_fallback": parsed.get("_fallback", False),
        "llm_error": _public_copilot_error(parsed.get("_llm_error")),
        "clarification_question": parsed.get("clarification_question"),
        "user_id": user_id,
    })


@app.route("/api/copilot/apply", methods=["POST"])
def copilot_apply_staged():
    """Apply a reviewed/edited staged proposal from /api/copilot/stage."""
    data = request.json or {}
    user_id = _resolve_request_user_id(data)
    staged_actions = data.get("staged_actions")
    user_text = (data.get("text") or "").strip()

    if not isinstance(staged_actions, dict):
        return jsonify({"error": "Provide 'staged_actions' object from /api/copilot/stage."}), 400
    if not _copilot_stage_binding_valid(staged_actions):
        return jsonify({"error": "This Copilot draft belongs to a different household or is no longer valid. Re-stage it before saving."}), 409

    if isinstance(staged_actions.get("goals_added"), list) and staged_actions.get("goals_added"):
        rows = staged_actions.get("goals_added") or []
        if len(rows) != 1: return jsonify({"error": "Review one Goal at a time."}), 400
        row = rows[0] if isinstance(rows[0], dict) else {}
        operation_id = str(staged_actions.get("operation_id") or "").strip()
        try:
            target_cents = _money_to_cents(row.get("target_amount"), field_name="goal target") if row.get("target_amount") is not None else int(row.get("target_cents") or 0)
            goal = create_savings_goal(current_household_id(), operation_id=operation_id, name=str(row.get("name") or ""), target_cents=target_cents, target_date=_parse_optional_date(row.get("target_date")), priority=int(row.get("priority", 100)))
            return jsonify({"actions_taken": {"operation_id": operation_id, "goals_added": [{"id": goal.id, "name": row.get("name"), "target_cents": goal.target_cents}], "already_applied": SavingsGoal.query.filter_by(household_id=current_household_id(), create_operation_id=operation_id).count() == 1}, "undo_token": None})
        except (SavingsError, ValueError) as exc:
            db.session.rollback(); return jsonify({"error": str(exc)}), 400
        except OperationalError as exc:
            db.session.rollback()
            if "database is locked" in str(exc).lower():
                return jsonify({"error": "The database is busy. No changes were saved; retry this approved operation."}), 503
            raise

    try:
        applied = apply_staged_actions(staged_actions, raw_user_text=user_text, user_id=user_id)
    except StagedActionValidationError as exc:
        payload: dict[str, Any] = {"error": str(exc)}
        if getattr(exc, "details", None):
            payload["validation"] = exc.details
        return jsonify(payload), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OperationalError as exc:
        db.session.rollback()
        if "database is locked" in str(exc).lower():
            return jsonify({"error": "The database is busy. No changes were saved; retry this approved operation."}), 503
        raise
    return jsonify({
        "actions_taken": applied,
        "undo_token": applied.get("undo_token"),
    })


@app.route("/api/copilot/confirm", methods=["POST"])
def copilot_confirm():
    """Confirm and persist pending intent actions derived from a user message.

    Accepts the same `text` payload as `/api/copilot/parse`. If the parser
    previously indicated that confirmation was required, calling this
    endpoint will apply the pending actions (bills/expenses) and return the
    newly-applied `actions_taken` payload. This is intentionally simple and
    synchronous so the UI can call it directly after showing a confirmation
    dialog to the user.
    """
    data = request.json or {}
    user_text = (data.get("text") or "").strip()
    user_id = _resolve_request_user_id(data)
    if not user_text:
        return jsonify({"error": "Provide 'text' field with your request"}), 400

    llm_gate = check_optional_operation(user_id, "llm_call")
    parsed = _parse_copilot_prompt_compat(user_text, allow_llm=bool(llm_gate.get("allowed", True)))
    if not llm_gate.get("allowed", True):
        parsed["_llm_error"] = llm_gate.get("message") or "Copilot advanced model calls are currently unavailable."

    _record_llm_usage(user_id, parsed, "copilot_confirm")

    # If the parser returned native tool calls, just return that result.
    if parsed.get("tool_results"):
        # Let the existing intent tooling convert tool_results to actions.
        from services.copilot_intent import _tool_results_to_actions

        parsed["actions_taken"] = _tool_results_to_actions(parsed.get("tool_results", []))
        return jsonify({
            "parsed": _public_parsed_payload(parsed),
            "actions_taken": parsed["actions_taken"],
            "tool_results": parsed.get("tool_results", []),
            "_fallback": parsed.get("_fallback", False),
        })

    # Otherwise parse intent and persist with confirmation flag.
    from services.copilot_intent import parse_intent_payload, _execute_intent_payload
    intent_payload = parse_intent_payload(parsed, user_text)
    actions = _execute_intent_payload(intent_payload, confirm=True, user_id=user_id)
    response = {
        "parsed": _public_parsed_payload(parsed),
        "actions_taken": actions,
        "tool_results": parsed.get("tool_results", []),
        "_fallback": parsed.get("_fallback", False),
    }
    if actions.get("undo_token"):
        response["undo_token"] = actions["undo_token"]
    return jsonify(response)


@app.route("/api/copilot/undo", methods=["POST"])
def copilot_undo():
    """Undo a previously confirmed Copilot action using its undo token."""
    data = request.json or {}
    undo_token = (data.get("undo_token") or "").strip()
    if not undo_token:
        return jsonify({"error": "Provide 'undo_token' field."}), 400
    user_id = _resolve_request_user_id(data)

    audit = _load_audit_by_token(undo_token)
    if not audit:
        return jsonify({"error": "Invalid undo_token."}), 404
    if audit.undone_at is not None:
        return jsonify({"error": "This action has already been undone."}), 400

    # user_id is preserved for audit/trace but is not currently used by undo logic.
    _ = user_id

    try:
        undone = _undo_actions_from_audit(audit)
    except Exception as exc:
        return jsonify({"error": f"Undo failed: {exc}"}), 500

    return jsonify({
        "undo_token": undo_token,
        "undone_at": audit.undone_at.isoformat() if audit.undone_at else None,
        "undone_actions": undone,
    })


@app.route("/api/recipes/search", methods=["GET"])
def search_recipes():
    """
    Keyword recipe search with TheMealDB API fallback.
    
    Query params:
      q  — search keyword (e.g. ?q=chicken). If omitted, returns all local recipes.
    
    Returns a flat JSON array of unified recipe objects, each with a
    `source` field: "local" or "themealdb".
    """
    query = request.args.get("q", "").strip()

    # --- 1. Local SQLite search ---
    local_recipes = []
    if query:
        like_pat = f"%{query}%"
        # Single search across title, instructions, and ingredient fields.
        # Uses OUTER JOIN so recipes with no ingredients still match on
        # title or instructions. DISTINCT prevents duplicates from the join.
        matches = (
            visible_recipe_query(current_household_id())
            .outerjoin(RecipeIngredient)
            .filter(
                db.or_(
                    Recipe.title.ilike(like_pat),
                    Recipe.instructions.ilike(like_pat),
                    RecipeIngredient.product_name.ilike(like_pat),
                    RecipeIngredient.clean_keyword.ilike(like_pat),
                )
            )
            .distinct()
            .all()
        )
        for r in matches:
            local_recipes.append({
                "id": str(r.id),
                "title": r.title,
                "description": (r.instructions or "")[:200],
                "servings": r.servings,
                "source": "local",
            })
    else:
        # No query — return all local recipes (backward-compatible)
        for r in visible_recipe_query(current_household_id()).all():
            local_recipes.append({
                "id": str(r.id),
                "title": r.title,
                "description": (r.instructions or "")[:200],
                "servings": r.servings,
                "source": "local",
            })

    # --- 2. TheMealDB API supplement (only when q is provided) ---
    # Always fetch TheMealDB to supplement local results, then merge
    # with dedup so every matching local recipe plus external recipes
    # are returned together — no arbitrary cap.
    if not query:
        return jsonify(local_recipes)

    try:
        import urllib.request
        from urllib.parse import quote
        import json as py_json

        url = f"https://www.themealdb.com/api/json/v1/1/search.php?s={quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Rung/1.0 (finance-assistant)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
            data = py_json.loads(body)

        meals = data.get("meals") or []

        # Build a set of normalized local titles for dedup.
        # All local recipes are included first; TheMealDB entries are
        # appended only when no local recipe with the same title exists.
        local_titles = {" ".join(r["title"].lower().split()) for r in local_recipes}

        for meal in meals:
            meal_title = meal.get("strMeal", "Unknown").strip()
            meal_key = " ".join(meal_title.lower().split())
            if meal_key in local_titles:
                continue
            instructions = meal.get("strInstructions") or ""
            desc = instructions[:200] if instructions else ""
            local_recipes.append({
                "id": f"themealdb_{meal.get('idMeal', '0')}",
                "title": meal_title,
                "description": desc,
                "source": "themealdb",
                "image_url": meal.get("strMealThumb", ""),
                "category": meal.get("strCategory", ""),
                "area": meal.get("strArea", ""),
            })

        return jsonify(local_recipes)

    except Exception:
        # Graceful offline fallback — return whatever local results we have
        if local_recipes:
            return jsonify(local_recipes)
        return jsonify({"error": "Could not reach TheMealDB. Try again later.", "results": []}), 503

def _persist_authoritative_cart_response(verified_cart: dict[str, Any], *, household_id: int, selected_store: dict[str, Any]) -> dict[str, Any]:
    """Make a provider resolution the one current cart for its canonical store."""
    from services.authoritative_cart import cart_dict, replace_current_from_resolution
    identity_id = int(selected_store.get("retail_store_identity_id") or 0)
    if not identity_id:
        return verified_cart
    cart = replace_current_from_resolution(
        household_id=household_id, store_identity_id=identity_id, resolved_cart=verified_cart,
    )
    db.session.commit()
    verified_cart["authoritative_cart"] = cart_dict(cart)
    verified_cart["cart_id"] = cart.id
    return verified_cart


def _store_change_review_dict(review: ShoppingStoreChangeReview) -> dict[str, Any]:
    from services.authoritative_cart import cart_dict
    staged = db.session.get(ShoppingCart, review.staged_cart_id)
    current = db.session.get(ShoppingCart, review.current_cart_id)
    current_store = db.session.get(RetailStoreIdentity, review.from_store_identity_id)
    proposed_store = db.session.get(RetailStoreIdentity, review.to_store_identity_id)
    return {"id": review.id, "status": review.status, "operation_id": review.operation_id,
            "current_cart": cart_dict(current) if current else None, "reviewed_cart": cart_dict(staged) if staged else None,
            # Store names are review facts, not client guesses.  The dialog
            # needs both names to communicate that the current store remains
            # active until an explicit approval.
            "current_store": {"store_id": current_store.retailer_store_id, "name": current_store.store_name} if current_store else None,
            "proposed_store": {"store_id": proposed_store.retailer_store_id, "name": proposed_store.store_name} if proposed_store else None}


@app.route("/api/shopping/current-cart", methods=["GET"])
def shopping_current_cart():
    from services.authoritative_cart import cart_dict, current_cart
    cart = current_cart(current_household_id())
    return jsonify({"cart": cart_dict(cart) if cart else None, "selected_store": get_selected_store(current_household_id())})


@app.route("/api/shopping/current-cart/choose-product", methods=["POST"])
def shopping_current_cart_choose_product():
    """Persist one explicit current-cart choice using only server cache truth."""
    from services.authoritative_cart import _line_key, cart_dict, choose_current_line_product, current_cart
    data = request.json or {}; hid = current_household_id()
    try:
        cart_id, line_id, version = int(data.get('cart_id')), int(data.get('line_id')), int(data.get('version'))
    except (TypeError, ValueError):
        return jsonify({'error': 'cart_id, line_id, and version are required.'}), 400
    requested = str(data.get('product_id') or '').strip()
    if not requested: return jsonify({'error': 'product_id is required.'}), 400
    cart = current_cart(hid)
    if cart is None or cart.id != cart_id: return jsonify({'error': 'Current cart not found.'}), 404
    line = ShoppingCartLine.query.filter_by(id=line_id, cart_id=cart.id).first()
    identity = db.session.get(RetailStoreIdentity, cart.retail_store_identity_id)
    if line is None or identity is None: return jsonify({'error': 'Cart line not found.'}), 404
    candidate = None
    # A store-scoped product is still not interchangeable across requirements.
    # Match the cache envelope to the durable line key before accepting any
    # candidate, so an unrelated same-store search cannot be selected.
    for row in RetailProductCache.query.filter_by(retailer=identity.retailer, store_id=identity.retailer_store_id, verified_location=True).order_by(RetailProductCache.retrieved_at.desc()).all():
        try: payload = json.loads(row.response_json or '{}')
        except (TypeError, ValueError): continue
        cached_requirement = payload.get('requirement') or {}
        if _line_key({'requirement': cached_requirement}, 0) != line.requirement_key:
            continue
        for product in (payload.get('candidates') or []) + (payload.get('alternatives') or []) + ([payload.get('selected_product')] if payload.get('selected_product') else []):
            if str(product.get('product_id') or '') == requested or str(product.get('us_item_id') or '') == requested:
                candidate = dict(product); break
        if candidate: break
    if candidate is None: return jsonify({'error': 'Requested product is not a verified candidate at this store.'}), 409
    try:
        cart = choose_current_line_product(household_id=hid, cart_id=cart_id, line_id=line_id, expected_version=version, product=candidate)
        db.session.commit()
    except LookupError as exc: return jsonify({'error': str(exc)}), 404
    except ValueError as exc: db.session.rollback(); return jsonify({'error': str(exc)}), 409
    return jsonify({'cart': cart_dict(cart)})


@app.route("/api/shopping/store-change/start", methods=["POST"])
def shopping_store_change_start():
    """Resolve a target store into a staged cart without changing canonical state."""
    from services.authoritative_cart import current_cart, stage_store_change
    from services.retail.base import RetailStore
    from services.retail.cart import build_verified_retail_cart
    from services.selected_store import ensure_store_identity
    data = request.json or {}; hid = current_household_id(); account = _household_account(); current = current_cart(hid)
    if account is None or current is None: return jsonify({"error": "Build a current cart before changing stores."}), 409
    retailer, store_id = str(data.get("retailer") or "").strip().lower(), str(data.get("store_id") or "").strip()
    if retailer not in {"walmart", "kroger"} or not store_id: return jsonify({"error": "Choose an exact supported physical store."}), 400
    identity = ensure_store_identity(retailer=retailer, store_id=store_id, store_name=str(data.get("store_name") or retailer.title()), address=str(data.get("address") or ""), city=str(data.get("city") or ""), state=str(data.get("state") or ""), postal_code=str(data.get("postal_code") or ""))
    try:
        resolved = build_verified_retail_cart(retailer=retailer, store=RetailStore(store_id=store_id, name=identity.store_name, address=identity.address, postal_code=identity.postal_code, verified=True), budget_limit=None, owner_scope=_resolve_request_user_id(data))
    except Exception:
        db.session.rollback(); LOGGER.exception("store-change target resolution failed")
        return jsonify({"error": "We could not resolve that store for review. Your selected store and cart were not changed."}), 502
    tax = _apply_owned_tax_to_cart(account=account, owner_scope=_resolve_request_user_id(data), cart_items=resolved.get("cart_items") or [], retailer=retailer, store_name=identity.store_name, store_id=store_id, store_address=identity.address or "", postal_code=identity.postal_code or "", city_state=", ".join(filter(None, [identity.city or "", identity.state or ""])))
    resolved.update({"subtotal": tax["subtotal"], "total_cart_cost": tax["total_cart_cost"]})
    review = stage_store_change(household_id=hid, current=current, target_store_identity_id=identity.id, resolved_cart=resolved, operation_id=str(data.get("operation_id") or f"store_change_{uuid.uuid4().hex}"))
    db.session.commit()
    return jsonify({"staged": True, "review": _store_change_review_dict(review), "selected_store": get_selected_store(hid)})


@app.route("/api/shopping/store-change/<int:review_id>/cancel", methods=["POST"])
def shopping_store_change_cancel(review_id: int):
    from services.authoritative_cart import cancel_store_change
    try: review = cancel_store_change(household_id=current_household_id(), review_id=review_id); db.session.commit()
    except LookupError: return jsonify({"error": "Store-change review not found."}), 404
    return jsonify({"cancelled": True, "review": _store_change_review_dict(review), "selected_store": get_selected_store(current_household_id())})


@app.route("/api/shopping/store-change/<int:review_id>/approve", methods=["POST"])
def shopping_store_change_approve(review_id: int):
    from services.authoritative_cart import approve_store_change
    review = ShoppingStoreChangeReview.query.filter_by(id=review_id, household_id=current_household_id()).first()
    if review is None: return jsonify({"error": "Store-change review not found."}), 404
    target = db.session.get(RetailStoreIdentity, review.to_store_identity_id)
    try:
        review = approve_store_change(household_id=current_household_id(), review_id=review_id, store={"retailer": target.retailer, "store_id": target.retailer_store_id, "name": target.store_name, "address": target.address, "city": target.city, "state": target.state, "postal_code": target.postal_code}, account=_household_account())
        db.session.commit()
    except ValueError as exc:
        db.session.rollback(); return jsonify({"error": str(exc)}), 409
    return jsonify({"approved": True, "review": _store_change_review_dict(review), "selected_store": get_selected_store(current_household_id())})


@app.route("/api/grocery/generate-pay-period-plan", methods=["POST"])
def generate_pay_period_plan():
    """
    Live grocery resolver — resolves every ingredient keyword via
    ``services.store_api.resolve_terms()`` (cache-first, Kroger API
    fallback), then builds and validates a pay-period cart within the
    canonical grocery Need remaining.

    Request body
    ------------
    recipe_ids : list[int]
        IDs of the recipes to generate a plan for.
    store_name : str, optional
        Override store name (default "Kroger").
    location_id : str, optional
        Kroger location ID for live API queries. If omitted, resolution
        uses whatever is already in StorePriceCache (no API calls).
    force_refresh : bool, optional
        If True, skip cache and hit the API for every term.
    budget_limit : float, optional
        Override the grocery budget cap. Without an override, defaults to the
        canonical current-period grocery Need remaining.

    Returns
    -------
    JSON with the same shape as the cart_items block (subtotal, tax,
    total_cart_cost, etc.) plus:
      resolution_stats {cache_hits, api_hits, fallbacks, total_terms}
      budget {grocery_need_budget, food_budget (compatibility alias),
              budget_source, budget_exceeded, budget_remaining}
      recipes_used [{id, title}]
    """
    account = _household_account()
    if not account:
        return jsonify({"error": "Account settings missing"}), 400

    data = request.json or {}
    user_id = _resolve_request_user_id(data)
    one_time_choices = data.get("one_time_choices") if isinstance(data.get("one_time_choices"), dict) else {}
    if "recipe_ids" not in data:
        return jsonify({"error": "Provide recipe_ids (list of int)"}), 400

    grocery_budget, grocery_budget_source, grocery_budget_error = _resolve_cart_grocery_budget(account, data, user_id)
    if grocery_budget_error is not None:
        return jsonify(grocery_budget_error), 409 if grocery_budget_error.get("code") == "grocery_budget_setup_required" else 400

    raw_recipe_ids = data.get("recipe_ids", [])
    if raw_recipe_ids is None:
        raw_recipe_ids = []
    if not isinstance(raw_recipe_ids, list):
        return jsonify({"error": "Provide recipe_ids (list of int)"}), 400
    recipe_ids = [int(rid) for rid in raw_recipe_ids if str(rid).strip() not in {"", "None"}]

    # Canonical exact-store state wins. Request values remain a compatibility
    # fallback for households that have not explicitly selected a store yet.
    selected_store = get_selected_store(current_household_id(), account=account)
    has_canonical_store = bool(selected_store.get("canonical") and selected_store.get("store_id"))
    raw_store_name = data.get("store_name", None)
    explicit_store_name = isinstance(raw_store_name, str) and bool(raw_store_name.strip())
    store_name = (
        str(selected_store.get("name") or "").strip()
        if has_canonical_store
        else (str(raw_store_name).strip() if explicit_store_name else str(selected_store.get("name") or "Kroger"))
    )
    location_id = (
        str(selected_store.get("store_id") or "").strip()
        if has_canonical_store
        else data.get("location_id", selected_store.get("store_id") or "")
    )
    selected_retailer = str(selected_store.get("retailer") or "").strip().lower()
    if not has_canonical_store:
        selected_retailer = "walmart" if store_name.lower() == "walmart" else "kroger"
    force_refresh = bool(data.get("force_refresh", False))
    budget_limit = data.get("budget_limit", None)
    use_verified_cart = bool(data.get("use_verified_cart", False))
    manual_rows = _household_grocery_query().filter(GroceryItem.is_purchased.is_(False)).filter(
        db.or_(GroceryItem.recipe_ids == '', GroceryItem.recipe_ids.is_(None))
    ).all()
    has_active_recipes = bool(_household_meal_plan_query().first())

    # Persisted active recipes (Package 5 MealPlanItem) are the authoritative
    # recipe source for the verified cart. A request-only ``recipe_ids`` list
    # that is *not* mirrored in the meal plan stays on the legacy resolver
    # path, preserving existing transient recipe-driven behavior.
    walmart_verified = (
        (has_canonical_store or explicit_store_name)
        and selected_retailer == "walmart"
        and (bool(manual_rows) or has_active_recipes)
        and (not recipe_ids or has_active_recipes)
    )

    if (use_verified_cart or walmart_verified) and selected_retailer == "walmart":
        from services.retail.base import RetailStore
        from services.retail.cart import build_verified_retail_cart, build_verified_walmart_cart
        food_budget = float(grocery_budget)
        try:
            if has_canonical_store:
                verified_cart = build_verified_retail_cart(
                    retailer="walmart",
                    store=RetailStore(
                        store_id=location_id,
                        name=store_name,
                        address=selected_store.get("address") or None,
                        postal_code=selected_store.get("postal_code") or None,
                        verified=True,
                    ),
                    force_refresh=force_refresh,
                    budget_limit=food_budget,
                    tax_rate=0.0,
                    owner_scope=user_id,
                    one_time_choices=one_time_choices,
                )
            else:
                verified_cart = build_verified_walmart_cart(
                    force_refresh=force_refresh,
                    budget_limit=food_budget,
                    tax_rate=0.0,
                    owner_scope=user_id,
                    one_time_choices=one_time_choices,
                )
        except Exception:
            LOGGER.exception("walmart verified cart failed")
            return jsonify({
                "error": "Live Walmart pricing is currently unavailable.",
                "code": "retail_provider_unavailable",
                "degraded_mode": "manual_shopping_available",
            }), 502

        store_payload = verified_cart.get("store") or {}
        tax_payload = _apply_owned_tax_to_cart(
            account=account,
            owner_scope=user_id,
            cart_items=verified_cart.get("cart_items") or [],
            retailer="walmart",
            store_name=str(store_payload.get("name") or "Walmart").strip(),
            store_id=str(store_payload.get("store_id") or "357").strip(),
            store_address=str(store_payload.get("address") or "").strip(),
            postal_code=str(store_payload.get("postal_code") or selected_store.get("postal_code") or "").strip(),
            city_state=", ".join(filter(None, [str(selected_store.get("city") or "").strip(), str(selected_store.get("state") or "").strip()])),
        )

        verified_cart.update({
            "subtotal": tax_payload["subtotal"],
            "grocery_tax_rate": tax_payload["grocery_tax_rate"],
            "applied_tax_pct": tax_payload["applied_tax_pct"],
            "tax_amount": tax_payload["tax_amount"],
            "total_cart_cost": tax_payload["total_cart_cost"],
            "tax_engine": tax_payload["tax_engine"],
            "budget": {
                "available": True,
                "grocery_need_budget": round(food_budget, 2),
                "food_budget": round(food_budget, 2),
                "food_budget_compatibility_alias": True,
                "budget_source": grocery_budget_source,
                "budget_exceeded": (float(tax_payload["total_cart_cost"]) > food_budget) if tax_payload["total_cart_cost"] is not None else (float(tax_payload["subtotal"]) > food_budget),
                "budget_remaining": round(food_budget - float(tax_payload["total_cart_cost"]), 2) if tax_payload["total_cart_cost"] is not None else None,
            },
            "store_config_warning": None,
        })
        return jsonify(_persist_authoritative_cart_response(verified_cart, household_id=current_household_id(), selected_store=selected_store))

    if (use_verified_cart or explicit_store_name or has_canonical_store) and selected_retailer == "kroger":
        from services.retail.base import RetailStore
        from services.retail.cart import build_verified_retail_cart

        kroger_store = None
        if str(location_id or "").strip():
            kroger_store = RetailStore(
                store_id=str(location_id).strip(),
                name=store_name or "Kroger",
                address=selected_store.get("address") or None,
                postal_code=selected_store.get("postal_code") or str(account.zip_code or "").strip() or None,
                verified=True,
            )
        else:
            resolution = _resolve_kroger_store_selection(account, requested_store_name=store_name)
            if resolution.get("state") == "none":
                return jsonify({
                    "status": "store_unavailable",
                    "error": resolution.get("message", "No Kroger-family stores were found."),
                    "store_config_warning": {
                        "code": "no_kroger_store",
                        "message": resolution.get("message", "No Kroger-family stores were found."),
                        "selected_store": store_name,
                    },
                    "store_choice": None,
                })
            if resolution.get("state") == "choice":
                return jsonify({
                    "status": "store_choice_required",
                    "message": resolution.get("message", "Choose a Kroger-family store to continue."),
                    "store_choice": resolution.get("store_choice"),
                    "store_config_warning": None,
                })

            kroger_store = resolution.get("store")
            if resolution.get("persisted"):
                _persist_kroger_store_choice(account, kroger_store)
                db.session.commit()

        if kroger_store is None:
            return jsonify({
                "status": "store_unavailable",
                "error": "No Kroger-family store could be resolved.",
                "store_choice": None,
            })

        food_budget = float(grocery_budget)
        try:
            verified_cart = build_verified_retail_cart(
                retailer="kroger",
                store=kroger_store,
                force_refresh=force_refresh,
                budget_limit=food_budget,
                tax_rate=0.0,
                owner_scope=user_id,
                one_time_choices=one_time_choices,
            )
        except Exception:
            LOGGER.exception("kroger verified cart failed")
            return jsonify({
                "error": "Live Kroger pricing is currently unavailable.",
                "code": "retail_provider_unavailable",
                "degraded_mode": "manual_shopping_available",
            }), 502

        store_payload = verified_cart.get("store") or {}
        tax_payload = _apply_owned_tax_to_cart(
            account=account,
            owner_scope=user_id,
            cart_items=verified_cart.get("cart_items") or [],
            retailer="kroger",
            store_name=str(store_payload.get("name") or kroger_store.name or "Kroger").strip(),
            store_id=str(store_payload.get("store_id") or kroger_store.store_id or "").strip(),
            store_address=str(store_payload.get("address") or kroger_store.address or "").strip(),
            postal_code=str(store_payload.get("postal_code") or kroger_store.postal_code or "").strip(),
            city_state=", ".join(filter(None, [str(selected_store.get("city") or "").strip(), str(selected_store.get("state") or "").strip()])),
        )

        verified_cart.update({
            "subtotal": tax_payload["subtotal"],
            "grocery_tax_rate": tax_payload["grocery_tax_rate"],
            "applied_tax_pct": tax_payload["applied_tax_pct"],
            "tax_amount": tax_payload["tax_amount"],
            "total_cart_cost": tax_payload["total_cart_cost"],
            "tax_engine": tax_payload["tax_engine"],
            "budget": {
                "available": True,
                "grocery_need_budget": round(food_budget, 2),
                "food_budget": round(food_budget, 2),
                "food_budget_compatibility_alias": True,
                "budget_source": grocery_budget_source,
                "budget_exceeded": (float(tax_payload["total_cart_cost"]) > food_budget) if tax_payload["total_cart_cost"] is not None else (float(tax_payload["subtotal"]) > food_budget),
                "budget_remaining": round(food_budget - float(tax_payload["total_cart_cost"]), 2) if tax_payload["total_cart_cost"] is not None else None,
            },
            "store_config_warning": None,
        })
        return jsonify(_persist_authoritative_cart_response(verified_cart, household_id=current_household_id(), selected_store=selected_store))

    # Warn when the selected store is Kroger but no location ID is configured.
    # Results will silently fall back to third-party sources without this guard.
    _kroger_selected = "kroger" in store_name.lower()
    store_config_warning = None
    if _kroger_selected and not location_id:
        store_config_warning = {
            "code": "no_location_id",
            "message": (
                "Kroger store location not configured. "
                "Live Kroger prices are unavailable; results may be from "
                "third-party sources and are not confirmed Kroger products."
            ),
            "selected_store": store_name,
            "resolution": "Go to Settings and use 'Detect Location' to configure your Kroger store.",
        }

    if not recipe_ids and not manual_rows:
        return jsonify({"error": "Provide recipe_ids (list of int)"}), 400

    recipes = visible_recipe_query(current_household_id()).filter(Recipe.id.in_(recipe_ids)).all() if recipe_ids else []
    if recipe_ids and not recipes:
        return jsonify({"error": "No matching recipes found"}), 404

    # --- Step 1: Aggregate ingredient keywords ---
    required_ingredients: dict = {}
    required_dimensions: dict = {}
    required_conversion_uncertain: dict = {}
    for r in recipes:
        for ing in r.ingredients:
            kw = ing.clean_keyword.lower()
            qty_std, dim, reliable = normalize_requirement_for_selection(ing.quantity, ing.unit, kw)
            required_ingredients[kw] = required_ingredients.get(kw, 0.0) + qty_std
            prev = required_dimensions.get(kw)
            if prev is None:
                required_dimensions[kw] = dim
            elif prev != dim:
                required_dimensions[kw] = 'unknown'
            required_conversion_uncertain[kw] = bool(required_conversion_uncertain.get(kw, False) or (not reliable))

    # Direct/manual grocery requests remain abstract until the deterministic resolver handles them.
    # These rows are not fake store products; they are active shopping requests.
    for row in manual_rows:
        term = (row.item_name or '').strip()
        if not term:
            continue
        kw = _normalize_manual_grocery_keyword(term)
        if not kw:
            continue
        required_ingredients[kw] = required_ingredients.get(kw, 0.0) + 1.0
        required_dimensions.setdefault(kw, 'unknown')
        required_conversion_uncertain[kw] = bool(required_conversion_uncertain.get(kw, False) or True)

    # Mixed recipe+manual runs consume staging rows after they are incorporated.
    if manual_rows and recipe_ids:
        for row in manual_rows:
            row.is_purchased = True
        db.session.commit()

    unique_terms = list(required_ingredients.keys())

    # --- Step 2: Live API resolution (cache-first, Kroger fallback) ---
    try:
        if force_refresh:
            gate = check_optional_operation(user_id, "retail_external_call")
            if not gate.get("allowed", True):
                return jsonify({
                    "error": gate.get("message") or "Live retail refresh is currently unavailable.",
                    "code": gate.get("code") or "retail_live_disabled",
                }), 429
        resolved = resolve_terms(
            app,
            unique_terms,
            store_name=store_name,
            location_id=location_id if location_id else None,
            limit=5,
            force_refresh=force_refresh,
        )
    except Exception:
        # If the resolver itself crashes (e.g. import error), fall back
        # to an empty resolution dict — the loop below will use estimates.
        resolved = {}

    # --- Step 2b: RapidAPI supplement for terms with no Kroger results ---
    rapid_hits = 0
    for kw in unique_terms:
        existing = resolved.get(kw, [])
        if existing:
            continue  # already have Kroger products — keep them

        try:
            rapid_result = search_local_product(kw, store_name=store_name, location="", app=app)
            if rapid_result:
                product_dict = rapid_result_to_product_dict(rapid_result)
                resolved[kw] = [product_dict]
                rapid_hits += 1
        except Exception:
            pass  # Silently fall through — the loop below will use estimates

    # --- Step 3: Compute resolution stats ---
    # Sources from resolve_terms: "kroger_cache", "kroger_api"
    # Sources from RapidAPI path: "rapid_api", "rapid_cache", "store_cache_fallback"
    _LOCAL_STORE_SOURCES = {"kroger_cache", "kroger_api"}
    cache_hits = 0
    api_hits = 0
    fallbacks = 0  # counted in Step 4 when estimate fallback is actually used
    for kw, products in resolved.items():
        for p in products:
            src = p.get("source", "")
            if src in ("kroger_cache", "store_cache_fallback"):
                cache_hits += 1
            elif src == "kroger_api":
                api_hits += 1
            elif src in ("rapid_api", "rapid_cache"):
                pass  # counted in rapid_hits

    # --- Step 4: Pantry deduction + product selection ---
    pantry_stock = {p.clean_keyword.lower(): p for p in _household_pantry_query().all()}
    prefs = {b.clean_keyword.lower(): b for b in _household_brand_pref_query().all()}

    cart_items = []
    subtotal = 0.0
    pantry_items_used = 0

    for kw, req_qty in required_ingredients.items():
        on_hand_qty = 0.0
        if kw in pantry_stock:
            req_dim = required_dimensions.get(kw)
            pantry_qty, pantry_dim, pantry_reliable = normalize_requirement_for_selection(
                pantry_stock[kw].quantity,
                pantry_stock[kw].unit,
                kw,
            )
            # Only apply pantry deduction when both sides are safely
            # comparable in the same canonical dimension.
            if req_dim != 'unknown' and pantry_reliable and pantry_dim == req_dim:
                on_hand_qty = pantry_qty
            else:
                required_conversion_uncertain[kw] = True

        if on_hand_qty >= req_qty:
            pantry_items_used += 1
            continue

        net_needed = req_qty - on_hand_qty
        pref = prefs.get(kw)
        use_store_brand = pref.prefer_store_brand if pref else True

        products = resolved.get(kw, [])
        best = pick_best(
            products,
            prefer_store_brand=use_store_brand,
            keyword=kw,
            net_needed=net_needed,
            required_dimension=required_dimensions.get(kw),
        ) if products else None

        if best:
            packages_to_buy = int(best.get("packages_to_buy", 1) or 1)
            unit_price = round(best["price"], 2)
            line_price = round(unit_price * packages_to_buy, 2)
            product_label = best["product_title"]
            price_source = best.get("source", "kroger_cache")
            confirmed = price_source in _LOCAL_STORE_SOURCES
            # Use the actual source store, not the selected store, for non-local products.
            store = best.get("source_store_name") or store_name if confirmed else (
                best.get("store_name") or best.get("source_store_name") or None
            )
        else:
            # Graceful fallback — estimate
            fallbacks += 1
            packages_to_buy = 1
            unit_price = round(2.00 + (net_needed * 0.15), 2)
            line_price = unit_price
            readable_name = kw.replace("_", " ").title()
            product_label = f"{readable_name} (estimate)"
            price_source = "estimated"
            confirmed = False
            store = None

        subtotal += line_price
        pkg = best.get("package_size", "") if best else ""
        img = best.get("image_url", "") if best else ""
        cart_items.append({
            "keyword": kw,
            "product_label": product_label,
            "net_quantity_needed_oz": round(net_needed, 2),
            "estimated_price": line_price,
            "unit_price": unit_price,
            "packages_to_buy": packages_to_buy,
            "price_source": price_source,
            "confirmed_local_store": confirmed,
            "package_selection_uncertain": bool(required_conversion_uncertain.get(kw, False)) or (bool(best.get("package_parse_uncertain", False)) if best else True),
            "store_name": store,
            "package_size": pkg,
            "image_url": img,
        })

    # --- Step 5: Tax + budget enforcement (owned store tax engine) ---
    tax_payload = _apply_owned_tax_to_cart(
        account=account,
        owner_scope=user_id,
        cart_items=cart_items,
        retailer=selected_retailer,
        store_name=store_name,
        store_id=str(location_id or "unknown").strip(),
        store_address=str(selected_store.get("address") or "").strip(),
        postal_code=str(selected_store.get("postal_code") or "").strip(),
        city_state=", ".join(filter(None, [str(selected_store.get("city") or "").strip(), str(selected_store.get("state") or "").strip()])),
    )

    # Determine the food budget
    food_budget = float(grocery_budget)

    budget_exceeded = (float(tax_payload["total_cart_cost"]) > food_budget) if tax_payload["total_cart_cost"] is not None else (float(tax_payload["subtotal"]) > food_budget)
    budget_remaining = round(food_budget - float(tax_payload["total_cart_cost"]), 2) if tax_payload["total_cart_cost"] is not None else None

    return jsonify({
        "retailer": selected_retailer,
        "store_id": str(location_id or "").strip(),
        "store_name": store_name,
        "pantry_items_skipped": pantry_items_used,
        "cart_items": cart_items,
        "subtotal": tax_payload["subtotal"],
        "grocery_tax_rate": tax_payload["grocery_tax_rate"],
        "applied_tax_pct": tax_payload["applied_tax_pct"],
        "tax_amount": tax_payload["tax_amount"],
        "total_cart_cost": tax_payload["total_cart_cost"],
        "tax_engine": tax_payload["tax_engine"],
        "resolution_stats": {
            "cache_hits": cache_hits,
            "api_hits": api_hits,
            "rapid_hits": rapid_hits,
            "fallbacks": fallbacks,
            "total_terms": len(unique_terms),
        },
        "budget": {
            "available": True,
            "grocery_need_budget": round(food_budget, 2),
            "food_budget": round(food_budget, 2),
            "food_budget_compatibility_alias": True,
            "budget_source": grocery_budget_source,
            "budget_exceeded": budget_exceeded,
            "budget_remaining": budget_remaining,
        },
        "store_config_warning": store_config_warning,
        "recipes_used": [{"id": r.id, "title": r.title} for r in recipes],
    })

@app.route("/api/rapid-price/search", methods=["GET"])
def rapid_price_search():
    """
    On-demand product search via RapidAPI Real-Time Product Search.

    Query params:
      q (required)  — Ingredient keyword (e.g. "cilantro", "chicken breast")
      store_name    — Optional store name to narrow results (e.g. "Walmart")

    Returns
    -------
    JSON with ``found`` and ``product`` keys:

    .. code-block:: json

        {"found": true, "product": {"title": "...", "price": 1.99, ...}}
        {"found": false, "product": null}
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing required query param 'q'"}), 400

    store_name = request.args.get("store_name", "").strip() or None

    try:
        result = search_local_product(query, store_name=store_name, location="", app=app)
    except Exception as exc:
        return jsonify({"error": f"Search failed: {exc}", "found": False, "product": None}), 502

    if result:
        return jsonify({"found": True, "product": result})
    else:
        return jsonify({"found": False, "product": None})


@app.route("/api/retail/product-preference", methods=["GET", "POST", "DELETE"])
def retail_product_preference():
    from services.retail.preferences import (
        forget_product_preference,
        get_product_preference,
        preference_to_dict,
        save_product_preference,
    )

    data = request.json or {} if request.method != "GET" else request.args
    base_item = str(data.get("base_item") or "").strip()
    if not base_item:
        return jsonify({"error": "base_item is required."}), 400

    try:
        retailer = str(data.get("retailer") or "").strip() or None
        if request.method == "GET":
            preference = get_product_preference(base_item, retailer=retailer)
            return jsonify({"preference": preference_to_dict(preference) if preference else None})
        if request.method == "DELETE":
            deleted = forget_product_preference(
                base_item,
                data.get("preference_type"),
                retailer=retailer,
            )
            return jsonify({"deleted": deleted, "base_item": base_item})

        preference, detail_calls = save_product_preference(
            base_item=base_item,
            preference_type=str(data.get("preference_type") or "usual"),
            retailer=str(data.get("retailer") or "walmart"),
            store_id=str(data.get("store_id") or ""),
            product_identity=str(data.get("product_identity") or ""),
        )
        return jsonify({
            "preference": preference_to_dict(preference),
            "product_detail_calls": detail_calls,
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        LOGGER.exception("Retail product preference request failed")
        return jsonify({"error": f"Could not save product preference: {exc}"}), 502


@app.route("/api/retail/product-block", methods=["GET", "POST", "DELETE"])
def retail_product_block():
    """Household-scoped negative product authority for automatic selection."""
    from services.retail.preferences import (
        list_product_blocks, product_block_to_dict, remove_product_block,
        save_product_block,
    )

    data = request.args if request.method == "GET" else (request.json or {})
    try:
        if request.method == "GET":
            return jsonify({"blocks": [product_block_to_dict(row) for row in list_product_blocks()]})
        if request.method == "DELETE":
            block_id = int(data.get("block_id") or 0)
            if not block_id:
                return jsonify({"error": "block_id is required."}), 400
            if not remove_product_block(block_id):
                return jsonify({"error": "Product block not found."}), 404
            return jsonify({"deleted": True, "block_id": block_id})
        block = save_product_block(
            block_type=str(data.get("block_type") or ""),
            retailer=data.get("retailer"), product_id=data.get("product_id"),
            us_item_id=data.get("us_item_id"), brand=data.get("brand"),
        )
        return jsonify({"block": product_block_to_dict(block)})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        LOGGER.exception("Retail product block request failed")
        db.session.rollback()
        return jsonify({"error": "Could not save this product block."}), 502


@app.route("/api/retail/product-substitution", methods=["GET", "POST", "DELETE"])
def retail_product_substitution():
    from services.retail.preferences import (
        get_product_preference,
        get_product_substitutions,
        remove_product_substitution,
        save_product_substitution,
        substitution_to_dict,
    )

    data = request.args if request.method == "GET" else (request.json or {})
    base_item = str(data.get("base_item") or "").strip()
    if not base_item:
        return jsonify({"error": "base_item is required."}), 400
    try:
        if request.method == "GET":
            preference = get_product_preference(base_item)
            substitutions = get_product_substitutions(preference.id) if preference else []
            return jsonify({"substitutions": [substitution_to_dict(row) for row in substitutions]})
        if request.method == "DELETE":
            substitution_id = data.get("substitution_id")
            if substitution_id is None:
                return jsonify({"error": "substitution_id is required."}), 400
            deleted = remove_product_substitution(int(substitution_id), base_item=base_item)
            return jsonify({"deleted": deleted, "base_item": base_item})

        substitution, detail_calls = save_product_substitution(
            base_item=base_item,
            product_identity=str(data.get("product_identity") or ""),
            retailer=str(data.get("retailer") or "walmart"),
            store_id=str(data.get("store_id") or ""),
        )
        return jsonify({
            "substitution": substitution_to_dict(substitution),
            "product_detail_calls": detail_calls,
        })
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        LOGGER.exception("Retail product substitution request failed")
        return jsonify({"error": f"Could not update product substitution: {exc}"}), 502


@app.route("/api/pantry", methods=["GET", "POST"])
def manage_pantry():
    """Tab 4: Inventory Management & Stock Updates."""
    if request.method == "POST":
        data = request.json or {}
        kw = data.get("clean_keyword", "").strip().lower()
        p_name = data.get("product_name", kw.title())
        qty = float(data.get("quantity", 0.0))
        unit = data.get("unit", "oz")
        
        item = _household_pantry_query().filter_by(clean_keyword=kw).first()
        if item:
            item.quantity += qty
        else:
            item = PantryItem(household_id=current_household_id(), clean_keyword=kw, product_name=p_name, quantity=qty, unit=unit)
            db.session.add(item)
            
        db.session.commit()
        return jsonify({"message": "Pantry item updated successfully"})
        
    items = _household_pantry_query().all()
    return jsonify([{
        "id": i.id,
        "keyword": i.clean_keyword,
        "product_name": i.product_name,
        "quantity": i.quantity,
        "unit": i.unit
    } for i in items])

@app.route("/api/pantry/cook", methods=["POST"])
def cook_recipe():
    """Tab 4: Automatically depletes pantry stock when a meal is cooked."""
    data = request.json or {}
    recipe_id = data.get("recipe_id")
    recipe = visible_recipe_by_id(current_household_id(), recipe_id)
    if not recipe:
        return jsonify({"error": "Recipe not found"}), 404
        
    for ing in recipe.ingredients:
        if ing.quantity is None or not ing.unit:
            # The source did not provide a safely understood requirement;
            # do not invent a pantry deduction.
            continue
        kw = ing.clean_keyword.lower()
        required_qty = normalize_to_standard_unit(ing.quantity, ing.unit)
        
        item = _household_pantry_query().filter_by(clean_keyword=kw).first()
        if item:
            item_on_hand_std = normalize_to_standard_unit(item.quantity, item.unit)
            new_qty_std = max(0.0, item_on_hand_std - required_qty)
            item.quantity = new_qty_std
            
    db.session.commit()
    return jsonify({"message": f"Cooked {recipe.title}! Pantry inventory automatically depleted."})

@app.route("/api/vault/sweep", methods=["POST"])
def sweep_vault():
    """Tab 5: Micro-Savings Sweeper."""
    account = _household_account()
    if not account:
        return jsonify({"error": "Account settings missing"}), 400

    data = request.json or {}
    amount = float(data.get("amount", 0.0))
    
    if amount <= 0 or amount > account.checking_balance:
        return jsonify({"error": "Invalid sweep amount"}), 400

    hid = current_household_id()
    new_checking_balance = apply_balance_delta(hid, -amount)
    account.vault_balance += amount
    db.session.commit()
    
    return jsonify({
        "new_checking_balance": round(new_checking_balance, 2),
        "new_vault_balance": round(account.vault_balance, 2)
    })

@app.route("/api/location/update", methods=["POST"])
def update_location():
    """Tab 6: Location & Tax settings engine.

    When the user saves their ZIP code or coordinates, the endpoint
    auto-detects the nearest Kroger / Gerbes store via the Kroger
    Locations API and saves the location ID to the account.
    """
    account = _household_account()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    data = request.json or {}

    auto_detect = bool(data.get("auto_detect"))
    zip_code = ""

    def _safe_coord(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    posted_zip_raw = data.get("zip_code")
    posted_zip = _normalize_zip_code(posted_zip_raw)

    if auto_detect:
        latitude = _safe_coord(data.get("latitude"))
        longitude = _safe_coord(data.get("longitude"))
        if latitude is None or longitude is None or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return jsonify({
                "error": "current_location_unavailable",
                "user_message": "We couldn't get your current location. Enter your ZIP code instead.",
            }), 400
        account.latitude = latitude
        account.longitude = longitude
    else:
        if posted_zip_raw is None or not str(posted_zip_raw).strip() or not posted_zip:
            return jsonify({
                "error": "invalid_zip_code",
                "user_message": "We couldn't save that location. Please check the ZIP code and try again.",
            }), 400
        zip_code = posted_zip
        # Manual ZIP updates should not keep stale city/state labels from prior defaults.
        account.city_state = ""

    reverse_geo = {}
    resolved_city_state = str(account.city_state or "").strip()
    if auto_detect and account.latitude is not None and account.longitude is not None:
        reverse_geo = _reverse_geocode_us_location(account.latitude, account.longitude)
        resolved_zip = _normalize_zip_code(reverse_geo.get("zip_code"))
        if not resolved_zip:
            return jsonify({
                "error": "current_location_unavailable",
                "user_message": "We couldn't get your current location. Enter your ZIP code instead.",
            }), 422
        zip_code = resolved_zip
        if reverse_geo.get("city_state"):
            resolved_city_state = str(reverse_geo["city_state"] or "").strip()

    account.zip_code = zip_code
    if resolved_city_state:
        account.city_state = resolved_city_state

    selected = get_selected_store(current_household_id(), account=account)
    store_found = bool(str(selected.get("store_id") or "").strip())
    store_lookup_status = "not_attempted"
    user_message = "Location saved."

    explicit_store_name = str(data.get("store_name") or "").strip()
    explicit_location_id = str(data.get("location_id") or "").strip()
    if explicit_store_name or explicit_location_id:
        if explicit_location_id:
            from services.authoritative_cart import current_cart
            current_cart_row = current_cart(current_household_id())
            target_retailer = str(data.get("retailer") or ("walmart" if "walmart" in explicit_store_name.lower() else "kroger")).lower()
            if current_cart_row is not None and (
                str(selected.get("retailer") or "").lower() != target_retailer
                or str(selected.get("store_id") or "") != explicit_location_id
            ):
                db.session.rollback()
                return jsonify({
                    "error": "store_change_review_required",
                    "user_message": "Review the Store Change in Shopping before changing your active store.",
                }), 409
            selected = select_store(
                current_household_id(),
                retailer=target_retailer,
                store_id=explicit_location_id,
                store_name=explicit_store_name or "Selected Store",
                postal_code=account.zip_code or "",
                city=(account.city_state or "").split(",", 1)[0],
                state=((account.city_state or "").rsplit(",", 1)[-1].strip() if "," in (account.city_state or "") else ""),
                account=account,
            )
            store_found = True
            store_lookup_status = "resolved"
        if explicit_store_name and not explicit_location_id:
            store_lookup_status = "named_store_saved"
    else:
        # Keep selected-store state stable unless the user explicitly picks one.
        # Location changes can trigger nearby-store discovery, but they must not
        # silently overwrite the current shopping store.
        if store_found:
            store_lookup_status = "unchanged"
        else:
            store_lookup_status = "store_choice_required"
            user_message = "Location saved. Choose a nearby supported store to continue shopping."

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({
            "error": "location_save_failed",
            "user_message": "We couldn't save that location. Please check the ZIP code and try again.",
        }), 500
    
    selected = get_selected_store(current_household_id(), account=account)
    return jsonify({
        "message": "Location updated successfully",
        "user_message": user_message,
        "zip": account.zip_code,
        "location": {
            "zip_code": account.zip_code or "",
            "city_state": account.city_state or "",
            "tax_authority": "canonical_tax_engine_at_purchase",
            "store_name": selected.get("name") or "",
            "location_id": selected.get("store_id") or "",
            "selected_store": selected,
            "latitude": account.latitude,
            "longitude": account.longitude,
        },
        "store": {
            "found": store_found,
            "name": selected.get("name"),
            "location_id": selected.get("store_id"),
            "status": store_lookup_status,
        },
    })

# ----- STORE PRICE CACHE CSV UPLOAD -----------------------------------------

@app.route("/api/store-cache/upload-csv", methods=["POST"])
def upload_store_cache_csv():
    """Bulk-upsert store prices from a CSV payload."""
    import csv
    import io

    data = request.json or {}
    csv_text = data.get("csv", "").strip()
    if not csv_text:
        return jsonify({"error": "Missing 'csv' field in JSON body"}), 400

    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"store_name", "item_keyword", "product_title", "price"}
    if reader.fieldnames is None or not required.issubset(set(c for c in reader.fieldnames if c)):
        return jsonify({"error": "CSV must include columns: store_name,item_keyword,product_title,price"}), 400

    inserted = 0
    updated = 0
    skipped = 0
    errors = []

    for row_num, row in enumerate(reader, start=2):
        store = (row.get("store_name") or "").strip()
        keyword = (row.get("item_keyword") or "").strip()
        title = (row.get("product_title") or "").strip()
        price_raw = (row.get("price") or "").strip()
        pkg = (row.get("package_size") or "").strip()
        is_sb = (row.get("is_store_brand") or "0").strip()
        retailer = (row.get("retailer") or "").strip()

        if not store:
            skipped += 1
            errors.append(f"Row {row_num}: missing store_name")
            continue
        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            skipped += 1
            errors.append(f"Row {row_num}: invalid price '{price_raw}'")
            continue
        if price <= 0:
            skipped += 1
            errors.append(f"Row {row_num}: zero or negative price")
            continue

        existing = StorePriceCache.query.filter_by(
            store_name=store, product_title=title
        ).first()
        if existing:
            existing.price = price
            existing.package_size = pkg or existing.package_size
            existing.is_store_brand = int(is_sb) if is_sb else existing.is_store_brand
            existing.retailer = retailer or existing.retailer
            updated += 1
        else:
            db.session.add(StorePriceCache(
                store_name=store, item_keyword=keyword, product_title=title,
                price=price, package_size=pkg, retailer=retailer,
                is_store_brand=int(is_sb) if is_sb else 0,
            ))
            inserted += 1

    db.session.commit()
    return jsonify({
        "inserted": inserted, "updated": updated,
        "skipped": skipped, "errors": errors,
    })


def _normalize_beta_email(value: str) -> str:
    return str(value or "").strip().lower()


@app.cli.command("beta-user-create")
@click.option("--email", required=True, help="Login email for the beta user.")
@click.option("--password", required=True, help="Initial password for the beta user.")
@click.option("--household-public-id", required=True, help="Target household public_id to bind membership.")
@click.option("--role", default="owner", show_default=True, help="Membership role label.")
def beta_user_create(email: str, password: str, household_public_id: str, role: str) -> None:
    """Create a beta auth user and bind them to a household."""
    normalized_email = _normalize_beta_email(email)
    if not normalized_email or len(password or "") < 8:
        raise click.ClickException("Email is required and password must be at least 8 characters.")

    row = Household.query.filter_by(public_id=str(household_public_id).strip()).first()
    if row is None:
        raise click.ClickException("Household public_id not found.")

    existing = User.query.filter(db.func.lower(User.email) == normalized_email).first()
    if existing is not None:
        raise click.ClickException("User already exists.")

    user = User(
        email=normalized_email,
        password_hash=generate_password_hash(password),
        active=True,
        auth_version=1,
    )
    db.session.add(user)
    db.session.flush()
    membership = HouseholdMembership(
        user_id=user.id,
        household_id=row.id,
        role=str(role or "owner").strip() or "owner",
        active=True,
    )
    db.session.add(membership)
    db.session.commit()
    click.echo(f"created user={user.email} user_id={user.id} household_id={row.id}")


@app.cli.command("beta-user-assign-household")
@click.option("--email", required=True, help="Existing beta user email.")
@click.option("--household-public-id", required=True, help="Target household public_id.")
@click.option("--role", default="member", show_default=True, help="Membership role label.")
def beta_user_assign_household(email: str, household_public_id: str, role: str) -> None:
    """Assign or reactivate household membership for an existing beta user."""
    normalized_email = _normalize_beta_email(email)
    user = User.query.filter(db.func.lower(User.email) == normalized_email).first()
    if user is None:
        raise click.ClickException("User not found.")
    row = Household.query.filter_by(public_id=str(household_public_id).strip()).first()
    if row is None:
        raise click.ClickException("Household public_id not found.")

    membership = HouseholdMembership.query.filter_by(user_id=user.id, household_id=row.id).first()
    if membership is None:
        membership = HouseholdMembership(
            user_id=user.id,
            household_id=row.id,
            role=str(role or "member").strip() or "member",
            active=True,
        )
        db.session.add(membership)
    else:
        membership.role = str(role or membership.role or "member").strip() or "member"
        membership.active = True
        db.session.add(membership)
    db.session.commit()
    click.echo(f"assigned user={user.email} household_id={row.id} role={membership.role}")


@app.cli.command("beta-user-reset-password")
@click.option("--email", required=True, help="Existing beta user email.")
@click.option("--password", required=True, help="New password.")
def beta_user_reset_password(email: str, password: str) -> None:
    """Reset beta user password and invalidate active sessions."""
    normalized_email = _normalize_beta_email(email)
    user = User.query.filter(db.func.lower(User.email) == normalized_email).first()
    if user is None:
        raise click.ClickException("User not found.")
    if len(password or "") < 8:
        raise click.ClickException("Password must be at least 8 characters.")
    user.password_hash = generate_password_hash(password)
    user.auth_version = int(user.auth_version or 0) + 1
    db.session.add(user)
    db.session.commit()
    click.echo(f"password reset user={user.email}")


@app.cli.command("beta-user-set-active")
@click.option("--email", required=True, help="Existing beta user email.")
@click.option("--active", type=bool, required=True, help="true to enable, false to disable.")
def beta_user_set_active(email: str, active: bool) -> None:
    """Enable or disable a beta user account."""
    normalized_email = _normalize_beta_email(email)
    user = User.query.filter(db.func.lower(User.email) == normalized_email).first()
    if user is None:
        raise click.ClickException("User not found.")
    user.active = bool(active)
    user.auth_version = int(user.auth_version or 0) + 1
    db.session.add(user)
    db.session.commit()
    click.echo(f"updated user={user.email} active={str(bool(active)).lower()}")

def init_db():
    with app.app_context():
        _validate_startup_configuration()
        _validate_database_connectivity()

        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        db_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
        db_path = _db_path_from_uri(db_uri)
        db_class = _classify_db_path(db_path)
        require_alembic_version = db_class not in {"test", "disposable"}

        required_tables = {
            "household",
            "auth_user",
            "household_membership",
            "auth_login_throttle",
            "account",
            "income_plan_version",
            "bill",
            "expense_transactions",
            "shopping_trip_completion",
            "user_settings",
            "user_preferences",
            "action_audit",
            "plaid_item",
            "plaid_account",
            "plaid_transaction",
            "transaction_reconciliation",
            "retail_product",
            "retail_store_identity",
            "store_product_observation",
            "retail_search_cache",
            "retail_refresh_lease",
        }
        if require_alembic_version:
            required_tables.add("alembic_version")
        missing = sorted(required_tables - existing_tables)
        if missing:
            raise RuntimeError(
                "Database schema is not up to date. Run migrations before startup "
                f"(missing tables: {', '.join(missing)})."
            )

        # Keep compatibility default household behavior, but only after schema
        # is migration-ready. No runtime DDL is performed here.
        legacy_household = ensure_legacy_household()

        if _env_flag("RUNG_BOOTSTRAP_DEMO_DATA", False):
            acc = get_household_account(legacy_household.id)
            # Demo bootstrap is explicitly opted in; populate the canonical
            # row instead of racing a second Account insert.
            if acc.checking_balance is None:
                acc.checking_balance = 1250.00
                acc.food_allocation_pct = 40.0
                acc.pay_period_days = 14
                acc.meals_per_day = 3
                acc.expected_paycheck = 2000.00
                db.session.commit()
            seed_default_user_preferences("bootstrap")

if __name__ == "__main__":
    init_db()
    # With proper db.init_app() pattern and models in separate module, reloader works correctly
    app.run(port=5000, debug=True)
