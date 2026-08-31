"""Deterministic stored-authority fixture for the Feature 5 browser seam."""
import json
from datetime import datetime, timedelta, timezone

from app import app
from extensions import db
from models import (Account, Bill, GroceryItem, Household, HouseholdMembership,
                    HouseholdShoppingDefault, IncomePlanVersion, RetailProductBlock,
                    RetailProductCache, RetailProductPreference, RetailProductSubstitution,
                    User, UserPreference, UserSetting)
from services.selected_store import select_store
from werkzeug.security import generate_password_hash


def candidate(sku, title, price, *, availability='in_stock', brand='Fixture'):
    return {'product_id': sku, 'us_item_id': sku, 'retailer': 'walmart', 'title': title,
            'brand': brand, 'package_size': '1 package', 'price': price,
            'availability': availability, 'verified_location': True, 'source': 'fixture'}


def add_requirement(household_id, index, name, rows):
    requirement = {'item_name': name, 'base_item': name.lower(), 'quantity': 1,
                   'unit': 'package', 'source_kind': 'manual', 'source_requirement_id': index}
    db.session.add(GroceryItem(household_id=household_id, item_name=name,
                               shopping_requirement_json=json.dumps(requirement)))
    payload = {'selection_policy_version': 5, 'requirement': requirement,
               'selected_product': None, 'candidates': rows, 'alternatives': rows,
               'selection_confidence': 'low', 'needs_user_choice': True}
    db.session.add(RetailProductCache(retailer='walmart', store_id='A', store_name='Store A',
        store_address='', requested_query=name.lower(), base_item=name.lower(), title='fixture',
        provider_source='serpapi_walmart', verified_location=True,
        response_json=json.dumps(payload), retrieved_at=datetime.now(timezone.utc)))


with app.app_context():
    db.drop_all(); db.create_all()
    h = Household(legacy_scope_key='feature5-preference'); db.session.add(h); db.session.flush()
    u = User(email='feature5-preference@example.com', password_hash=generate_password_hash('browser-preference'), active=True, auth_version=1)
    db.session.add_all([u, Account(household_id=h.id, checking_balance=500, pay_period_days=14)]); db.session.flush()
    db.session.add(HouseholdMembership(user_id=u.id, household_id=h.id, role='owner', active=True))
    db.session.add_all([
        IncomePlanVersion(household_id=h.id, operation_id='preference-income', expected_income_cents=100000, effective_at=datetime.now(timezone.utc), source='fixture'),
        UserSetting(household_id=h.id, key='pyf_long_term_target_percent', value='0'),
        UserSetting(household_id=h.id, key='safe_to_spend_buffer_usd', value='0.00'),
        UserSetting(household_id=h.id, key='next_payday_date', value=(datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()),
        UserSetting(household_id=h.id, key='onboarding_required_expense_review', value='has_expenses_reviewed'),
        UserPreference(household_id=h.id, key='baseline_grocery_cost', value='100.00'),
        Bill(household_id=h.id, name='Fixture fuel Need', amount=0, due_date=datetime.now(timezone.utc), is_gas_estimate=True, is_paid=False),
        HouseholdShoppingDefault(household_id=h.id, owner_scope='household:default', preference_kind='category_default', preference_key='milk_type', preference_value='lactose_free'),
        HouseholdShoppingDefault(household_id=h.id, owner_scope='household:default', preference_kind='shopping_style', preference_key='shopping_style', preference_value='save_most'),
    ])
    add_requirement(h.id, 1, 'Favorite milk', [candidate('FAV-USUAL', 'Favorite milk usual', 2), candidate('FAV-FAVORITE', 'Favorite milk favorite', 9)])
    add_requirement(h.id, 2, 'Usual coffee', [candidate('COFFEE-USUAL', 'Usual coffee dark', 9), candidate('COFFEE-CHEAP', 'Cheap coffee', 1)])
    add_requirement(h.id, 3, 'Substitute yogurt', [candidate('YOGURT-USUAL', 'Usual yogurt', 2, availability='out_of_stock'), candidate('YOGURT-APPROVED', 'Approved yogurt', 7), candidate('YOGURT-CHEAP', 'Cheap yogurt', 1)])
    add_requirement(h.id, 4, 'Milk', [candidate('MILK-WHOLE', 'Whole milk', 2), candidate('MILK-LF', 'Lactose free milk', 7)])
    add_requirement(h.id, 5, 'Shampoo', [candidate('SHAMPOO-CHEAP', 'Budget shampoo', 2), candidate('SHAMPOO-PRICEY', 'Premium shampoo', 8)])
    add_requirement(h.id, 6, 'Blocked bread', [candidate('BREAD-BLOCKED', 'Blocked bread', 1), candidate('BREAD-OK', 'Allowed bread', 4)])
    prefs = []
    def pref(base, kind, sku, title):
        row = RetailProductPreference(household_id=h.id, base_item=base, normalized_base_item=base.lower(), preference_type=kind, preferred_product_title=title, retailer='walmart', retailer_product_id=sku, retailer_us_item_id=sku, source='fixture')
        db.session.add(row); prefs.append(row); return row
    pref('favorite milk', 'usual', 'FAV-USUAL', 'Favorite milk usual')
    pref('favorite milk', 'favorite', 'FAV-FAVORITE', 'Favorite milk favorite')
    pref('usual coffee', 'usual', 'COFFEE-USUAL', 'Usual coffee dark')
    usual_yogurt = pref('substitute yogurt', 'usual', 'YOGURT-USUAL', 'Usual yogurt')
    blocked = pref('blocked bread', 'favorite', 'BREAD-BLOCKED', 'Blocked bread')
    db.session.flush()
    db.session.add(RetailProductSubstitution(household_id=h.id, base_item='substitute yogurt', normalized_base_item='substitute yogurt', preferred_preference_id=usual_yogurt.id, substitute_product_title='Approved yogurt', retailer='walmart', retailer_product_id='YOGURT-APPROVED', retailer_us_item_id='YOGURT-APPROVED', approval_type='explicit'))
    db.session.add(RetailProductBlock(household_id=h.id, block_type='exact_product', retailer='walmart', retailer_product_id='BREAD-BLOCKED', retailer_us_item_id='BREAD-BLOCKED', block_key='exact:walmart:BREAD-BLOCKED'))
    select_store(h.id, retailer='walmart', store_id='A', store_name='Store A', account=Account.query.filter_by(household_id=h.id).one())
    db.session.commit()
