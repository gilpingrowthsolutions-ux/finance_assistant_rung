"""Deterministic prerequisites for the persistent closed-beta RC browser flow."""
from datetime import datetime, timedelta, timezone
import json
from werkzeug.security import generate_password_hash
from app import app, NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, REQUIRED_EXPENSE_REVIEW_SETTING_KEY, REQUIRED_EXPENSE_NONE
from extensions import db
from models import Account, Household, HouseholdMembership, IncomePlanVersion, Recipe, RecipeIngredient, RetailProductCache, User, UserSetting

def product():
    return {"product_id":"RC-SOAP","us_item_id":"RC-SOAP-us","retailer":"walmart","title":"RC Fixture Soap","brand":"Fixture","package_size":"2 bottles","price":6,"availability":"in_stock","verified_location":True,"source":"fixture"}

with app.app_context():
    db.drop_all(); db.create_all()
    a,b=Household(legacy_scope_key='rc-a'),Household(legacy_scope_key='rc-b'); db.session.add_all([a,b]);db.session.flush()
    ua=User(email='rc-a@example.com',password_hash=generate_password_hash('rc-a-pass'),active=True,auth_version=1);ub=User(email='rc-b@example.com',password_hash=generate_password_hash('rc-b-pass'),active=True,auth_version=1)
    aa=Account(household_id=a.id,checking_balance=0,pay_period_days=14,is_onboarded=False);ab=Account(household_id=b.id,checking_balance=91,pay_period_days=14,is_onboarded=True)
    db.session.add_all([ua,ub,aa,ab]);db.session.flush();db.session.add_all([HouseholdMembership(user_id=ua.id,household_id=a.id,role='owner',active=True),HouseholdMembership(user_id=ub.id,household_id=b.id,role='owner',active=True)])
    now=datetime.now(timezone.utc)
    db.session.add(IncomePlanVersion(household_id=b.id,operation_id='b-plan',expected_income_cents=10000,effective_at=now-timedelta(days=1),source='fixture'))
    db.session.add_all([UserSetting(household_id=b.id,key=NEXT_PAYDAY_SETTING_KEY,value=(now.date()+timedelta(days=7)).isoformat()),UserSetting(household_id=b.id,key=PYF_TARGET_SETTING_KEY,value='0'),UserSetting(household_id=b.id,key=SAFE_BUFFER_SETTING_KEY,value='0.00'),UserSetting(household_id=b.id,key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY,value=REQUIRED_EXPENSE_NONE)])
    recipe=Recipe(title='RC Fixture Recipe',servings=2,instructions='Use fixture soap.',recipe_scope=Recipe.SCOPE_CANONICAL);db.session.add(recipe);db.session.flush();db.session.add(RecipeIngredient(recipe_id=recipe.id,product_name='3 bottles fixture soap',clean_keyword='fixture soap',quantity=3,unit='bottles'))
    req={"item_name":"fixture soap","base_item":"fixture soap","quantity":3,"unit":"bottles","source_kind":"recipe"};payload={"selection_policy_version":5,"requirement":req,"selected_product":product(),"candidates":[product()],"alternatives":[],"selection_confidence":"suggested","needs_user_choice":False}
    db.session.add(RetailProductCache(retailer='walmart',store_id='A',store_name='Store A',store_address='1 Fixture Way',requested_query='fixture soap',base_item='fixture soap',title='fixture',provider_source='serpapi_walmart',verified_location=True,response_json=json.dumps(payload),retrieved_at=now));db.session.commit()
