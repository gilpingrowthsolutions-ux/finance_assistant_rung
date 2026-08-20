"""
Authoritative ORM model definitions.

All database model classes are defined here and import db from extensions.py.
This eliminates circular imports and provides a single source of truth for
model definitions.

Services and routes should import models from this module, not from app.py.
"""

from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Optional

from extensions import db


class ModelBase(db.Model):
    """Abstract base class for all ORM models."""
    __abstract__ = True
    __table__: Any
    __table_args__ = {"extend_existing": True}

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class Household(ModelBase):
    __tablename__ = 'household'
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), nullable=False, unique=True, default=lambda: str(uuid4()))
    legacy_scope_key = db.Column(db.String(120), nullable=True, unique=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class User(ModelBase):
    __tablename__ = 'auth_user'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    auth_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class HouseholdMembership(ModelBase):
    __tablename__ = 'household_membership'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('auth_user.id'), nullable=False, index=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    role = db.Column(db.String(40), nullable=False, default='owner')
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('user_id', 'household_id', name='uq_household_membership_user_household'),
        {'extend_existing': True},
    )


class LoginThrottle(ModelBase):
    __tablename__ = 'auth_login_throttle'
    id = db.Column(db.Integer, primary_key=True)
    subject_key = db.Column(db.String(320), nullable=False, unique=True)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    window_started_at = db.Column(db.DateTime, nullable=False)
    blocked_until = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class Account(ModelBase):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    checking_balance = db.Column(db.Float, default=1250.00)
    food_allocation_pct = db.Column(db.Float, default=40.0)  # % of safe disposable
    pay_period_days = db.Column(db.Integer, default=14)
    meals_per_day = db.Column(db.Integer, default=3)
    vault_balance = db.Column(db.Float, default=150.00)
    expected_paycheck = db.Column(db.Float, default=2000.00)
    is_onboarded = db.Column(db.Boolean, default=False)
    household_size = db.Column(db.Integer, default=4)
    
    # Geolocated Data & Local Taxes
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    zip_code = db.Column(db.String(10), default="65084")
    city_state = db.Column(db.String(100), default="Versailles, MO")
    sales_tax_rate = db.Column(db.Float, default=0.0825)   # 8.25%
    grocery_tax_rate = db.Column(db.Float, default=0.0125) # 1.25%
    balance_version = db.Column(db.Integer, nullable=False, default=0)
    
    # Auto-detected nearest Kroger / Gerbes store
    kroger_location_id = db.Column(db.String(20), nullable=True)
    kroger_store_name = db.Column(db.String(100), default="Kroger")

    __table_args__ = (
        db.UniqueConstraint('household_id', name='uq_account_household_id'),
        {'extend_existing': True},
    )


class Bill(ModelBase):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.DateTime, nullable=False)
    is_gas_estimate = db.Column(db.Boolean, default=False)
    is_paid = db.Column(db.Boolean, default=False)


class PantryItem(ModelBase):
    __tablename__ = 'pantry_inventory'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    clean_keyword = db.Column(db.String(100), nullable=False)
    product_name = db.Column(db.String(150), nullable=False)
    quantity = db.Column(db.Float, default=0.0)
    unit = db.Column(db.String(30), default="oz")

    __table_args__ = (
        db.UniqueConstraint('household_id', 'clean_keyword', name='uq_pantry_household_keyword'),
        {'extend_existing': True},
    )


class Recipe(ModelBase):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    servings = db.Column(db.Integer, default=4)
    estimated_cost_per_serving = db.Column(db.Float, default=3.50)
    is_favorite = db.Column(db.Boolean, default=False)
    usage_frequency = db.Column(db.Integer, default=0)
    last_selected_date = db.Column(db.DateTime, nullable=True)
    instructions = db.Column(db.Text, nullable=True)
    source_url = db.Column(db.String(500), nullable=True, unique=True)
    ingredients = db.relationship('RecipeIngredient', backref='recipe', cascade="all, delete-orphan")


class RecipeIngredient(ModelBase):
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    product_name = db.Column(db.String(100), nullable=False)
    clean_keyword = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Float, nullable=True)
    unit = db.Column(db.String(30), nullable=True)


class BrandPreference(ModelBase):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    clean_keyword = db.Column(db.String(100), nullable=False)
    prefer_store_brand = db.Column(db.Boolean, default=True)
    preferred_brand_name = db.Column(db.String(100), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('household_id', 'clean_keyword', name='uq_brand_pref_household_keyword'),
        {'extend_existing': True},
    )


class ExpenseTransaction(ModelBase):
    __tablename__ = 'expense_transactions'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    description = db.Column(db.String(150), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), default='discretionary')
    source = db.Column(db.String(30), nullable=False, default='manual')
    plaid_transaction_id = db.Column(db.String(120), nullable=True, unique=True)
    local_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class GroceryItem(ModelBase):
    __tablename__ = 'grocery_items'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    recipe_ids = db.Column(db.String(200), default='')
    item_name = db.Column(db.String(150), nullable=False)
    estimated_price = db.Column(db.Float, default=0.0)
    store_name = db.Column(db.String(100), default='')
    location_context = db.Column(db.String(100), default='')
    is_purchased = db.Column(db.Boolean, default=False)
    is_favorite = db.Column(db.Boolean, default=False)
    shopping_requirement_json = db.Column(db.Text, nullable=True)


class RapidPriceCache(ModelBase):
    __tablename__ = 'rapid_price_cache'
    id = db.Column(db.Integer, primary_key=True)
    ingredient_keyword = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)
    store_name = db.Column(db.String(100), default='')
    package_size = db.Column(db.String(100), nullable=True)
    image_url = db.Column(db.String(500), nullable=True)
    product_url = db.Column(db.String(500), nullable=True)
    location = db.Column(db.String(100), default='')
    last_updated = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class StorePriceCache(ModelBase):
    __tablename__ = 'store_price_cache'
    id = db.Column(db.Integer, primary_key=True)
    store_name = db.Column(db.String(100), nullable=False)
    item_keyword = db.Column(db.String(100), nullable=False)
    product_title = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)
    unit = db.Column(db.String(30), default='each')
    package_size = db.Column(db.String(100), nullable=True)
    image_url = db.Column(db.String(500), nullable=True)
    retailer = db.Column(db.String(50), default='kroger')
    is_store_brand = db.Column(db.Boolean, default=False)
    last_updated = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class RetailProductCache(ModelBase):
    __tablename__ = 'retail_product_cache'
    id = db.Column(db.Integer, primary_key=True)
    retailer = db.Column(db.String(50), nullable=False)
    store_id = db.Column(db.String(40), nullable=False)
    store_name = db.Column(db.String(150), nullable=False)
    store_address = db.Column(db.String(300), nullable=False)
    requested_query = db.Column(db.String(300), nullable=False)
    base_item = db.Column(db.String(150), nullable=False)
    product_id = db.Column(db.String(100), nullable=True)
    us_item_id = db.Column(db.String(100), nullable=True)
    title = db.Column(db.String(300), nullable=False)
    package_size = db.Column(db.String(100), nullable=True)
    price = db.Column(db.Float, nullable=True)
    availability = db.Column(db.String(30), default='unknown')
    provider_source = db.Column(db.String(80), nullable=False)
    verified_location = db.Column(db.Boolean, default=False, nullable=False)
    response_json = db.Column(db.Text, nullable=False)
    retrieved_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('retailer', 'store_id', 'requested_query', name='uq_retail_cache_store_query'),
        {'extend_existing': True},
    )


class RetailProduct(ModelBase):
    __tablename__ = 'retail_product'
    id = db.Column(db.Integer, primary_key=True)
    retailer = db.Column(db.String(50), nullable=False)
    retailer_product_id = db.Column(db.String(120), nullable=False)
    upc = db.Column(db.String(50), nullable=True)
    title = db.Column(db.String(300), nullable=False)
    brand = db.Column(db.String(150), nullable=True)
    package_size = db.Column(db.String(100), nullable=True)
    variant = db.Column(db.String(150), nullable=True)
    category = db.Column(db.String(150), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('retailer', 'retailer_product_id', name='uq_retail_product_identity'),
        db.Index('ix_retail_product_retailer_upc', 'retailer', 'upc'),
        {'extend_existing': True},
    )


class RetailStoreIdentity(ModelBase):
    __tablename__ = 'retail_store_identity'
    id = db.Column(db.Integer, primary_key=True)
    retailer = db.Column(db.String(50), nullable=False)
    retailer_store_id = db.Column(db.String(80), nullable=False)
    store_name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.String(300), nullable=True)
    city = db.Column(db.String(120), nullable=True)
    state = db.Column(db.String(40), nullable=True)
    postal_code = db.Column(db.String(20), nullable=True)
    latitude = db.Column(db.Numeric(10, 6), nullable=True)
    longitude = db.Column(db.Numeric(10, 6), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('retailer', 'retailer_store_id', name='uq_retail_store_identity'),
        {'extend_existing': True},
    )


class StoreProductObservation(ModelBase):
    __tablename__ = 'store_product_observation'
    id = db.Column(db.Integer, primary_key=True)
    retail_store_id = db.Column(db.Integer, db.ForeignKey('retail_store_identity.id'), nullable=False)
    retail_product_id = db.Column(db.Integer, db.ForeignKey('retail_product.id'), nullable=False)

    price_cents = db.Column(db.Integer, nullable=True)
    price_type = db.Column(db.String(40), nullable=False, default='unknown')
    price_observed_at = db.Column(db.DateTime, nullable=True)
    price_source = db.Column(db.String(80), nullable=True)
    price_confidence = db.Column(db.String(60), nullable=True)

    availability_status = db.Column(db.String(60), nullable=True)
    fulfillment_data_json = db.Column(db.Text, nullable=True)
    availability_observed_at = db.Column(db.DateTime, nullable=True)
    availability_source = db.Column(db.String(80), nullable=True)
    availability_confidence = db.Column(db.String(60), nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('retail_store_id', 'retail_product_id', name='uq_store_product_observation_identity'),
        db.Index('ix_store_product_observation_store_product', 'retail_store_id', 'retail_product_id'),
        db.Index('ix_store_product_observation_price_observed_at', 'price_observed_at'),
        db.Index('ix_store_product_observation_availability_observed_at', 'availability_observed_at'),
        {'extend_existing': True},
    )


class RetailSearchCache(ModelBase):
    __tablename__ = 'retail_search_cache'
    id = db.Column(db.Integer, primary_key=True)
    retailer = db.Column(db.String(50), nullable=False)
    retail_store_id = db.Column(db.Integer, db.ForeignKey('retail_store_identity.id'), nullable=False)
    normalized_query = db.Column(db.String(300), nullable=False)
    retailer_product_ids_json = db.Column(db.Text, nullable=False, default='[]')
    observed_at = db.Column(db.DateTime, nullable=False)
    source = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('retailer', 'retail_store_id', 'normalized_query', name='uq_retail_search_cache_identity'),
        db.Index('ix_retail_search_cache_lookup', 'retailer', 'retail_store_id', 'normalized_query'),
        db.Index('ix_retail_search_cache_observed_at', 'observed_at'),
        {'extend_existing': True},
    )


class RetailRefreshLease(ModelBase):
    __tablename__ = 'retail_refresh_lease'
    id = db.Column(db.Integer, primary_key=True)
    resource_key = db.Column(db.String(300), nullable=False)
    lease_owner = db.Column(db.String(120), nullable=False)
    lease_until = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('resource_key', name='uq_retail_refresh_lease_resource_key'),
        db.Index('ix_retail_refresh_lease_resource_until', 'resource_key', 'lease_until'),
        {'extend_existing': True},
    )


class RetailProductPreference(ModelBase):
    __tablename__ = 'retail_product_preference'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    base_item = db.Column(db.String(150), nullable=False)
    normalized_base_item = db.Column(db.String(150), nullable=False)
    preference_type = db.Column(db.String(20), nullable=False)
    preferred_brand = db.Column(db.String(150), nullable=True)
    preferred_variant = db.Column(db.String(150), nullable=True)
    preferred_package_size = db.Column(db.String(100), nullable=True)
    preferred_product_title = db.Column(db.String(300), nullable=False)
    upc = db.Column(db.String(50), nullable=True)
    retailer = db.Column(db.String(50), nullable=True)
    retailer_product_id = db.Column(db.String(100), nullable=True)
    retailer_us_item_id = db.Column(db.String(100), nullable=True)
    source = db.Column(db.String(50), default='user_explicit', nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'household_id',
            'normalized_base_item',
            'preference_type',
            'retailer',
            name='uq_retail_preference_base_type',
        ),
        {'extend_existing': True},
    )


class RetailProductSubstitution(ModelBase):
    __tablename__ = 'retail_product_substitution'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    base_item = db.Column(db.String(150), nullable=False)
    normalized_base_item = db.Column(db.String(150), nullable=False)
    preferred_preference_id = db.Column(
        db.Integer,
        db.ForeignKey('retail_product_preference.id', ondelete='CASCADE'),
        nullable=False,
    )
    substitute_brand = db.Column(db.String(150), nullable=True)
    substitute_variant = db.Column(db.String(150), nullable=True)
    substitute_package_size = db.Column(db.String(100), nullable=True)
    substitute_product_title = db.Column(db.String(300), nullable=False)
    substitute_upc = db.Column(db.String(50), nullable=True)
    retailer = db.Column(db.String(50), nullable=True)
    retailer_product_id = db.Column(db.String(100), nullable=True)
    retailer_us_item_id = db.Column(db.String(100), nullable=True)
    approval_type = db.Column(db.String(30), default='explicit', nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'household_id',
            'preferred_preference_id',
            'retailer',
            'retailer_us_item_id',
            name='uq_retail_substitution_preference_product',
        ),
        {'extend_existing': True},
    )


class UserSetting(ModelBase):
    """Key-value store for per-user settings (API keys, preferences, etc.)."""
    __tablename__ = 'user_settings'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    key = db.Column(db.String(80), nullable=False)
    value = db.Column(db.Text, default='')
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('household_id', 'key', name='uq_user_setting_household_key'),
        {'extend_existing': True},
    )


class UserPreference(ModelBase):
    """Key-value store for user baseline preferences captured in onboarding."""
    __tablename__ = 'user_preferences'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    key = db.Column(db.String(80), nullable=False)
    value = db.Column(db.Text, default='')
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('household_id', 'key', name='uq_user_preference_household_key'),
        {'extend_existing': True},
    )


class HouseholdShoppingDefault(ModelBase):
    """Structured household-level shopping defaults (category + style)."""
    __tablename__ = 'household_shopping_defaults'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    owner_scope = db.Column(db.String(120), nullable=False, default='household:default')
    preference_kind = db.Column(db.String(30), nullable=False, default='category_default')
    preference_key = db.Column(db.String(80), nullable=False)
    preference_value = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'household_id',
            'preference_kind',
            'preference_key',
            name='uq_household_default_scope_kind_key',
        ),
        {'extend_existing': True},
    )


class MealPlanItem(ModelBase):
    """A recipe selected for the current pay period (the active meal plan)."""
    __tablename__ = 'meal_plan'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    source = db.Column(db.String(20), default='user')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('household_id', 'recipe_id', name='uq_meal_plan_household_recipe'),
        {'extend_existing': True},
    )


class ActionAudit(ModelBase):
    """A simple audit log of executed actions to support confirmation/undo."""
    __tablename__ = 'action_audit'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    source = db.Column(db.String(50), default='copilot')
    user_id = db.Column(db.String(80), default='anonymous', nullable=False)
    raw_text = db.Column(db.Text, default='')
    actions_json = db.Column(db.Text, default='{}')
    undo_token = db.Column(db.String(80), nullable=False)
    operation_id = db.Column(db.String(80), nullable=True)
    undone_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('household_id', 'undo_token', name='uq_action_audit_household_undo_token'),
        db.UniqueConstraint('household_id', 'operation_id', name='uq_action_audit_household_operation_id'),
        {'extend_existing': True},
    )


class ShoppingTripCompletion(ModelBase):
    """Metadata for a completed grocery trip tied to a financial transaction."""
    __tablename__ = 'shopping_trip_completion'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    operation_id = db.Column(db.String(80), nullable=False)
    trip_token = db.Column(db.String(120), nullable=False)
    transaction_id = db.Column(db.Integer, db.ForeignKey('expense_transactions.id'), nullable=False)
    retailer = db.Column(db.String(40), nullable=False, default='')
    store_name = db.Column(db.String(120), nullable=False, default='')
    store_id = db.Column(db.String(40), nullable=True)
    planned_total_cents = db.Column(db.Integer, nullable=False)
    actual_total_cents = db.Column(db.Integer, nullable=False)
    amount_source = db.Column(db.String(20), nullable=False, default='planned')
    cart_signature = db.Column(db.String(120), nullable=False, default='')
    manual_provisional = db.Column(db.Boolean, nullable=False, default=True)
    completed_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('household_id', 'operation_id', name='uq_shopping_trip_household_operation_id'),
        db.UniqueConstraint('household_id', 'trip_token', name='uq_shopping_trip_household_trip_token'),
        {'extend_existing': True},
    )


class PlaidItem(ModelBase):
    """A connected Plaid Item and its sync cursor/token metadata."""
    __tablename__ = 'plaid_item'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    owner_scope = db.Column(db.String(80), nullable=False, default='anonymous')
    plaid_item_id = db.Column(db.String(120), nullable=False, unique=True)
    access_token_encrypted = db.Column(db.Text, nullable=False)
    institution_id = db.Column(db.String(120), nullable=True)
    institution_name = db.Column(db.String(200), nullable=True)
    sync_cursor = db.Column(db.Text, nullable=True)
    connection_status = db.Column(db.String(30), nullable=False, default='connected')
    last_sync_at = db.Column(db.DateTime, nullable=True)
    last_error_code = db.Column(db.String(120), nullable=True)
    last_error_message = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('household_id', 'plaid_item_id', name='uq_plaid_item_household_item'),
        {'extend_existing': True},
    )

    def to_summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'owner_scope': self.owner_scope,
            'plaid_item_id': self.plaid_item_id,
            'institution_id': self.institution_id,
            'institution_name': self.institution_name,
            'sync_cursor_present': bool(self.sync_cursor),
            'connection_status': self.connection_status,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'last_error_code': self.last_error_code,
            'last_error_message': self.last_error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class PlaidAccount(ModelBase):
    """Stable map of Plaid account identity to a local Rung account."""
    __tablename__ = 'plaid_account'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    owner_scope = db.Column(db.String(80), nullable=False, default='anonymous')
    plaid_item_id = db.Column(db.Integer, db.ForeignKey('plaid_item.id'), nullable=False)
    plaid_account_id = db.Column(db.String(120), nullable=False, unique=True)
    rung_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    name = db.Column(db.String(200), nullable=False, default='')
    official_name = db.Column(db.String(200), nullable=True)
    mask = db.Column(db.String(20), nullable=True)
    account_type = db.Column(db.String(40), nullable=True)
    account_subtype = db.Column(db.String(60), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('household_id', 'plaid_account_id', name='uq_plaid_account_household_account'),
        {'extend_existing': True},
    )

    def to_summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'owner_scope': self.owner_scope,
            'plaid_item_id': self.plaid_item_id,
            'plaid_account_id': self.plaid_account_id,
            'rung_account_id': self.rung_account_id,
            'name': self.name,
            'official_name': self.official_name,
            'mask': self.mask,
            'account_type': self.account_type,
            'account_subtype': self.account_subtype,
            'is_active': bool(self.is_active),
        }


class PlaidTransaction(ModelBase):
    """Authoritative external Plaid transaction identity for later reconciliation."""
    __tablename__ = 'plaid_transaction'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    owner_scope = db.Column(db.String(80), nullable=False, default='anonymous')
    plaid_item_id = db.Column(db.Integer, db.ForeignKey('plaid_item.id'), nullable=False)
    plaid_transaction_id = db.Column(db.String(120), nullable=False, unique=True)
    plaid_account_id = db.Column(db.String(120), nullable=False)
    pending_transaction_id = db.Column(db.String(120), nullable=True)
    replaces_pending_transaction_id = db.Column(db.String(120), nullable=True)
    replaced_by_transaction_id = db.Column(db.String(120), nullable=True)
    is_pending = db.Column(db.Boolean, nullable=False, default=False)
    is_removed = db.Column(db.Boolean, nullable=False, default=False)
    is_active_event = db.Column(db.Boolean, nullable=False, default=True)
    pending_lifecycle_status = db.Column(db.String(40), nullable=False, default='posted')
    amount_cents = db.Column(db.Integer, nullable=False, default=0)
    signed_amount_cents = db.Column(db.Integer, nullable=False, default=0)
    direction = db.Column(db.String(20), nullable=False, default='outflow')
    name = db.Column(db.String(300), nullable=False, default='')
    merchant_name = db.Column(db.String(300), nullable=True)
    description = db.Column(db.String(300), nullable=False, default='')
    iso_currency_code = db.Column(db.String(10), nullable=True)
    transaction_date = db.Column(db.Date, nullable=True)
    authorized_date = db.Column(db.Date, nullable=True)
    category_json = db.Column(db.Text, nullable=True)
    raw_json = db.Column(db.Text, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.Index('ix_plaid_transaction_household_item_account', 'household_id', 'plaid_item_id', 'plaid_account_id'),
        db.Index('ix_plaid_transaction_pending_id', 'pending_transaction_id'),
        {'extend_existing': True},
    )

    def to_summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'owner_scope': self.owner_scope,
            'plaid_item_id': self.plaid_item_id,
            'plaid_transaction_id': self.plaid_transaction_id,
            'plaid_account_id': self.plaid_account_id,
            'pending_transaction_id': self.pending_transaction_id,
            'replaces_pending_transaction_id': self.replaces_pending_transaction_id,
            'replaced_by_transaction_id': self.replaced_by_transaction_id,
            'is_pending': bool(self.is_pending),
            'is_removed': bool(self.is_removed),
            'is_active_event': bool(self.is_active_event),
            'pending_lifecycle_status': self.pending_lifecycle_status,
            'amount_cents': int(self.amount_cents or 0),
            'signed_amount_cents': int(self.signed_amount_cents or 0),
            'direction': self.direction,
            'name': self.name,
            'merchant_name': self.merchant_name,
            'description': self.description,
            'iso_currency_code': self.iso_currency_code,
            'transaction_date': self.transaction_date.isoformat() if self.transaction_date else None,
            'authorized_date': self.authorized_date.isoformat() if self.authorized_date else None,
            'last_seen_at': self.last_seen_at.isoformat() if self.last_seen_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class TransactionReconciliation(ModelBase):
    """Pairwise reconciliation state between manual and Plaid transactions."""
    __tablename__ = 'transaction_reconciliation'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    owner_scope = db.Column(db.String(80), nullable=False, default='anonymous')
    manual_transaction_id = db.Column(db.Integer, db.ForeignKey('expense_transactions.id'), nullable=False)
    plaid_transaction_id = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(30), nullable=False, default='proposed')
    match_strength = db.Column(db.Integer, nullable=False, default=0)
    user_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    confirmation_action = db.Column(db.String(40), nullable=True)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            'household_id',
            'manual_transaction_id',
            'plaid_transaction_id',
            name='uq_tx_recon_owner_manual_plaid',
        ),
        db.Index('ix_tx_recon_household_status', 'household_id', 'status'),
        {'extend_existing': True},
    )

    def to_summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'owner_scope': self.owner_scope,
            'manual_transaction_id': self.manual_transaction_id,
            'plaid_transaction_id': self.plaid_transaction_id,
            'status': self.status,
            'match_strength': int(self.match_strength or 0),
            'user_confirmed': bool(self.user_confirmed),
            'confirmation_action': self.confirmation_action,
            'confirmed_at': self.confirmed_at.isoformat() if self.confirmed_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class UsageEvent(ModelBase):
    """Operational usage/cost telemetry for internal metering and controls."""
    __tablename__ = 'usage_event'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=True, index=True)
    owner_scope = db.Column(db.String(120), nullable=False, default='anonymous')
    category = db.Column(db.String(40), nullable=False, default='unknown')
    provider = db.Column(db.String(80), nullable=False, default='unknown')
    operation = db.Column(db.String(80), nullable=False, default='unknown')
    success = db.Column(db.Boolean, nullable=False, default=True)
    external_call = db.Column(db.Boolean, nullable=False, default=False)
    request_count = db.Column(db.Integer, nullable=False, default=1)
    cache_status = db.Column(db.String(20), nullable=True)
    force_refresh = db.Column(db.Boolean, nullable=False, default=False)
    llm_provider = db.Column(db.String(80), nullable=True)
    llm_model = db.Column(db.String(120), nullable=True)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    estimated_cost_micros = db.Column(db.Integer, nullable=True)
    cost_status = db.Column(db.String(30), nullable=False, default='unconfigured')
    cost_rate_key = db.Column(db.String(120), nullable=True)
    operation_id = db.Column(db.String(120), nullable=True)
    request_id = db.Column(db.String(120), nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.Index('ix_usage_event_owner_created', 'owner_scope', 'created_at'),
        db.Index('ix_usage_event_category_provider_created', 'category', 'provider', 'created_at'),
        {'extend_existing': True},
    )

    def to_summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'owner_scope': self.owner_scope,
            'category': self.category,
            'provider': self.provider,
            'operation': self.operation,
            'success': bool(self.success),
            'external_call': bool(self.external_call),
            'request_count': int(self.request_count or 0),
            'cache_status': self.cache_status,
            'force_refresh': bool(self.force_refresh),
            'llm_provider': self.llm_provider,
            'llm_model': self.llm_model,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'estimated_cost_micros': self.estimated_cost_micros,
            'cost_status': self.cost_status,
            'cost_rate_key': self.cost_rate_key,
            'operation_id': self.operation_id,
            'request_id': self.request_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class UsageLimitCounter(ModelBase):
    __tablename__ = 'usage_limit_counter'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False, index=True)
    limit_key = db.Column(db.String(120), nullable=False)
    period_type = db.Column(db.String(20), nullable=False)
    period_start = db.Column(db.DateTime, nullable=False)
    used_count = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('household_id', 'limit_key', 'period_type', 'period_start', name='uq_usage_limit_counter_period'),
        db.Index('ix_usage_limit_counter_lookup', 'household_id', 'limit_key', 'period_type', 'period_start'),
        {'extend_existing': True},
    )


class BetaFeedback(ModelBase):
    """Lightweight local beta feedback records."""
    __tablename__ = 'beta_feedback'
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=True, index=True)
    category = db.Column(db.String(40), nullable=False, default='general')
    description = db.Column(db.String(500), nullable=False, default='')
    screen_context = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='open')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'category': self.category,
            'description': self.description,
            'screen_context': self.screen_context,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class TaxSourceDataset(ModelBase):
    __tablename__ = 'tax_source_dataset'
    id = db.Column(db.Integer, primary_key=True)
    source_key = db.Column(db.String(80), nullable=False)
    source_type = db.Column(db.String(40), nullable=False)
    jurisdiction_state = db.Column(db.String(2), nullable=True)
    source_name = db.Column(db.String(200), nullable=False)
    source_reference = db.Column(db.Text, nullable=True)
    source_hash = db.Column(db.String(128), nullable=False)
    version_tag = db.Column(db.String(80), nullable=False)
    published_at = db.Column(db.DateTime, nullable=True)
    effective_from = db.Column(db.Date, nullable=False)
    effective_to = db.Column(db.Date, nullable=True)
    imported_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    status = db.Column(db.String(30), nullable=False, default='staged')

    __table_args__ = (
        db.UniqueConstraint('source_key', 'version_tag', name='uq_tax_source_dataset_source_version'),
        db.Index('ix_tax_source_dataset_status_effective', 'status', 'effective_from', 'effective_to'),
        {'extend_existing': True},
    )


class TaxJurisdiction(ModelBase):
    __tablename__ = 'tax_jurisdiction'
    id = db.Column(db.Integer, primary_key=True)
    jurisdiction_type = db.Column(db.String(30), nullable=False)
    canonical_code = db.Column(db.String(120), nullable=False)
    state = db.Column(db.String(2), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    parent_jurisdiction_id = db.Column(db.Integer, db.ForeignKey('tax_jurisdiction.id'), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('jurisdiction_type', 'canonical_code', name='uq_tax_jurisdiction_type_code'),
        db.Index('ix_tax_jurisdiction_state_name', 'state', 'name'),
        {'extend_existing': True},
    )


class TaxRate(ModelBase):
    __tablename__ = 'tax_rate'
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Integer, db.ForeignKey('tax_source_dataset.id'), nullable=False)
    jurisdiction_id = db.Column(db.Integer, db.ForeignKey('tax_jurisdiction.id'), nullable=False)
    tax_code = db.Column(db.String(120), nullable=False)
    tax_class = db.Column(db.String(40), nullable=False, default='GENERAL_MERCHANDISE')
    rate_basis_points = db.Column(db.Integer, nullable=False)
    effective_from = db.Column(db.Date, nullable=False)
    effective_to = db.Column(db.Date, nullable=True)
    source_confidence = db.Column(db.String(30), nullable=False, default='medium')

    __table_args__ = (
        db.UniqueConstraint(
            'dataset_id',
            'jurisdiction_id',
            'tax_code',
            'tax_class',
            'effective_from',
            name='uq_tax_rate_dataset_jurisdiction_class_start',
        ),
        db.Index('ix_tax_rate_lookup', 'jurisdiction_id', 'tax_class', 'effective_from', 'effective_to'),
        db.Index('ix_tax_rate_dataset', 'dataset_id'),
        {'extend_existing': True},
    )


class TaxBoundaryAssignment(ModelBase):
    __tablename__ = 'tax_boundary_assignment'
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Integer, db.ForeignKey('tax_source_dataset.id'), nullable=False)
    geographic_key_type = db.Column(db.String(30), nullable=False)
    geographic_key = db.Column(db.String(200), nullable=False)
    assignment_precision = db.Column(db.String(30), nullable=False)
    jurisdiction_id = db.Column(db.Integer, db.ForeignKey('tax_jurisdiction.id'), nullable=False)
    tax_code = db.Column(db.String(120), nullable=False)
    effective_from = db.Column(db.Date, nullable=False)
    effective_to = db.Column(db.Date, nullable=True)
    source_confidence = db.Column(db.String(30), nullable=False, default='medium')

    __table_args__ = (
        db.UniqueConstraint(
            'dataset_id',
            'geographic_key_type',
            'geographic_key',
            'tax_code',
            'effective_from',
            name='uq_tax_boundary_dataset_key_code_start',
        ),
        db.Index('ix_tax_boundary_lookup', 'geographic_key_type', 'geographic_key', 'effective_from', 'effective_to'),
        db.Index('ix_tax_boundary_dataset', 'dataset_id'),
        {'extend_existing': True},
    )


class StoreTaxProfile(ModelBase):
    __tablename__ = 'store_tax_profile'
    id = db.Column(db.Integer, primary_key=True)
    retailer = db.Column(db.String(50), nullable=False)
    retailer_store_id = db.Column(db.String(80), nullable=False)
    store_name = db.Column(db.String(200), nullable=True)
    normalized_address = db.Column(db.String(300), nullable=True)
    postal_code = db.Column(db.String(10), nullable=True)
    city = db.Column(db.String(120), nullable=True)
    county = db.Column(db.String(120), nullable=True)
    state = db.Column(db.String(2), nullable=True)
    latitude = db.Column(db.Numeric(10, 6), nullable=True)
    longitude = db.Column(db.Numeric(10, 6), nullable=True)
    resolved_jurisdiction_id = db.Column(db.Integer, db.ForeignKey('tax_jurisdiction.id'), nullable=True)
    resolved_tax_code = db.Column(db.String(120), nullable=True)
    location_precision = db.Column(db.String(30), nullable=False, default='UNRESOLVED')
    confidence = db.Column(db.String(30), nullable=False, default='low')
    status = db.Column(db.String(30), nullable=False, default='unresolved')
    general_rate_basis_points = db.Column(db.Integer, nullable=True)
    grocery_rate_basis_points = db.Column(db.Integer, nullable=True)
    prepared_rate_basis_points = db.Column(db.Integer, nullable=True)
    effective_from = db.Column(db.Date, nullable=False)
    effective_to = db.Column(db.Date, nullable=True)
    source_dataset_id = db.Column(db.Integer, db.ForeignKey('tax_source_dataset.id'), nullable=True)
    source_version = db.Column(db.String(80), nullable=True)
    source_hash = db.Column(db.String(128), nullable=True)
    resolved_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        db.Index('ix_store_tax_profile_lookup', 'retailer', 'retailer_store_id', 'effective_from', 'effective_to'),
        db.Index('ix_store_tax_profile_dataset', 'source_dataset_id'),
        {'extend_existing': True},
    )


class TaxabilityRule(ModelBase):
    __tablename__ = 'taxability_rule'
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Integer, db.ForeignKey('tax_source_dataset.id'), nullable=False)
    jurisdiction_id = db.Column(db.Integer, db.ForeignKey('tax_jurisdiction.id'), nullable=True)
    state = db.Column(db.String(2), nullable=False)
    tax_class = db.Column(db.String(40), nullable=False)
    treatment = db.Column(db.String(40), nullable=False)
    override_rate_basis_points = db.Column(db.Integer, nullable=True)
    effective_from = db.Column(db.Date, nullable=False)
    effective_to = db.Column(db.Date, nullable=True)
    source_confidence = db.Column(db.String(30), nullable=False, default='medium')

    __table_args__ = (
        db.UniqueConstraint(
            'dataset_id',
            'state',
            'jurisdiction_id',
            'tax_class',
            'effective_from',
            name='uq_taxability_rule_dataset_scope_class_start',
        ),
        db.Index('ix_taxability_rule_lookup', 'state', 'tax_class', 'effective_from', 'effective_to'),
        {'extend_existing': True},
    )


class RetailProductTaxClass(ModelBase):
    __tablename__ = 'retail_product_tax_class'
    id = db.Column(db.Integer, primary_key=True)
    retailer = db.Column(db.String(50), nullable=False)
    retailer_product_id = db.Column(db.String(120), nullable=True)
    upc = db.Column(db.String(50), nullable=True)
    canonical_tax_class = db.Column(db.String(40), nullable=False)
    source = db.Column(db.String(40), nullable=False, default='deterministic_mapping')
    confidence = db.Column(db.String(30), nullable=False, default='medium')
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('retailer', 'retailer_product_id', name='uq_retail_product_tax_class_retailer_product'),
        db.Index('ix_retail_product_tax_class_retailer_upc', 'retailer', 'upc'),
        {'extend_existing': True},
    )
