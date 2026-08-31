"""Seed a disposable, deterministic served Shopping acceptance database."""
import json
from datetime import datetime, timedelta, timezone
from app import app
from extensions import db
from models import Account, Bill, GroceryItem, Household, HouseholdMembership, IncomePlanVersion, RetailProductCache, User, UserPreference, UserSetting
from werkzeug.security import generate_password_hash
from services.selected_store import select_store

def cache(hid, store_id, query, requirement, rows):
    # This is the exact persisted search-cache envelope consumed by the
    # verified Walmart resolver (not a legacy rendered-cart response).
    payload={'selection_policy_version':5,'requirement':requirement,'selected_product':None,'candidates':rows,'alternatives':rows,'selection_confidence':'low','needs_user_choice':True}
    db.session.add(RetailProductCache(retailer='walmart',store_id=store_id,store_name='Store '+store_id,store_address='',requested_query=query,base_item='laundry detergent',title='fixture',provider_source='serpapi_walmart',verified_location=True,response_json=json.dumps(payload),retrieved_at=datetime.now(timezone.utc)))

with app.app_context():
    db.drop_all(); db.create_all()
    a=Household(legacy_scope_key='feature5-a'); b=Household(legacy_scope_key='feature5-b'); db.session.add_all([a,b]); db.session.flush()
    ua=User(email='feature5-a@example.com',password_hash=generate_password_hash('browser-pass-a'),active=True,auth_version=1); ub=User(email='feature5-b@example.com',password_hash=generate_password_hash('browser-pass-b'),active=True,auth_version=1); db.session.add_all([ua,ub,Account(household_id=a.id,checking_balance=500,pay_period_days=14),Account(household_id=b.id,checking_balance=500,pay_period_days=14)]); db.session.flush(); db.session.add_all([HouseholdMembership(user_id=ua.id,household_id=a.id,role='owner',active=True),HouseholdMembership(user_id=ub.id,household_id=b.id,role='owner',active=True)])
    requirement={'item_name':'Laundry detergent','base_item':'laundry detergent','quantity':3,'unit':'bottle','source_kind':'manual','source_requirement_id':1}
    dish_requirement={'item_name':'Dish soap','base_item':'dish soap','quantity':1,'unit':'bottle','source_kind':'manual','source_requirement_id':2}
    db.session.add(GroceryItem(household_id=a.id,item_name='Laundry detergent',shopping_requirement_json=json.dumps(requirement)))
    db.session.add(GroceryItem(household_id=a.id,item_name='Dish soap',shopping_requirement_json=json.dumps(dish_requirement)))
    # The cart endpoint uses canonical grocery-Need remaining, never a UI
    # default.  Seed only the financial readiness facts needed for this
    # acceptance household; it does not create a store, cart, or provider row.
    db.session.add_all([
        IncomePlanVersion(household_id=a.id, operation_id='feature5-income', expected_income_cents=100000, effective_at=datetime.now(timezone.utc), source='fixture'),
        UserSetting(household_id=a.id, key='pyf_long_term_target_percent', value='0'),
        UserSetting(household_id=a.id, key='safe_to_spend_buffer_usd', value='0.00'),
        UserSetting(household_id=a.id, key='next_payday_date', value=(datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()),
        UserSetting(household_id=a.id, key='onboarding_required_expense_review', value='has_expenses_reviewed'),
        UserPreference(household_id=a.id, key='baseline_grocery_cost', value='100.00'),
        Bill(household_id=a.id, name='Fixture fuel Need', amount=0, due_date=datetime.now(timezone.utc), is_gas_estimate=True, is_paid=False),
    ])
    db.session.flush()
    row=lambda sku,title,price:{'product_id':sku,'us_item_id':sku+'-us','retailer':'walmart','title':title,'brand':'Fixture','package_size':'64 loads','price':price,'availability':'in_stock','verified_location':True,'source':'fixture'}
    # The cached candidates are scoped to the same durable cart requirement:
    # _line_key(requirement, 0) is manual:requirement:1.
    cache(a.id,'A','laundry detergent',requirement,[row('A-SKU','Store A detergent',8.00),row('A-ALT','Store A alternate',12.00)])
    cache(a.id,'A','dish soap',dish_requirement,[row('A-DISH','Store A dish soap',4.00)])
    # Store B is deliberately different; a Store Change Review must resolve
    # this requirement again rather than carrying Store A's product forward.
    cache(a.id,'B','laundry detergent',requirement,[row('B-SKU','Store B detergent',15.00)])
    cache(a.id,'B','dish soap',dish_requirement,[row('B-DISH','Store B dish soap',4.00) | {'availability':'out_of_stock'}])
    db.session.commit()
