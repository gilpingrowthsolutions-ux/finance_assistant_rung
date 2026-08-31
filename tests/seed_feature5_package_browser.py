"""Disposable provider-cache fixture for package truthfulness through Build Cart."""
import json
from datetime import datetime, timedelta, timezone
from app import app
from extensions import db
from models import Account, Bill, GroceryItem, Household, HouseholdMembership, IncomePlanVersion, RetailProductCache, User, UserPreference, UserSetting
from services.selected_store import select_store
from werkzeug.security import generate_password_hash


def candidate(sku, title, price, availability="in_stock"):
    return {"product_id": sku, "us_item_id": sku+"-us", "retailer": "walmart", "title": title, "brand": "Fixture", "package_size": "1 package", "price": price, "availability": availability, "verified_location": True, "data_quality": "RECENT_CONFIRMED", "source": "fixture"}


with app.app_context():
    db.drop_all(); db.create_all(); h=Household(legacy_scope_key="feature5-package"); db.session.add(h); db.session.flush()
    u=User(email="feature5-package@example.com",password_hash=generate_password_hash("browser-package"),active=True,auth_version=1); a=Account(household_id=h.id,checking_balance=500,pay_period_days=14); db.session.add_all([u,a]); db.session.flush(); db.session.add(HouseholdMembership(user_id=u.id,household_id=h.id,role="owner",active=True)); select_store(h.id,retailer="walmart",store_id="A",store_name="Store A",postal_code="65084",account=a)
    reqs=[{"item_name":"Multi-pack soap","base_item":"multi-pack soap","quantity":3,"unit":"bottle","source_kind":"manual"},{"item_name":"Rice","base_item":"rice","quantity":2,"unit":"cups","source_kind":"manual"},{"item_name":"Unavailable eggs","base_item":"unavailable eggs","quantity":1,"unit":"carton","source_kind":"manual"}]
    for req in reqs: db.session.add(GroceryItem(household_id=h.id,item_name=req["item_name"],shopping_requirement_json=json.dumps(req)))
    db.session.add_all([IncomePlanVersion(household_id=h.id,operation_id="package-income",expected_income_cents=100000,effective_at=datetime.now(timezone.utc),source="fixture"),UserSetting(household_id=h.id,key="pyf_long_term_target_percent",value="0"),UserSetting(household_id=h.id,key="safe_to_spend_buffer_usd",value="0"),UserSetting(household_id=h.id,key="next_payday_date",value=(datetime.now(timezone.utc).date()+timedelta(days=14)).isoformat()),UserSetting(household_id=h.id,key="onboarding_required_expense_review",value="has_expenses_reviewed"),UserPreference(household_id=h.id,key="baseline_grocery_cost",value="100"),Bill(household_id=h.id,name="Fixture fuel",amount=0,due_date=datetime.now(timezone.utc),is_gas_estimate=True,is_paid=False)])
    for req,row in zip(reqs,[candidate("PACK-SOAP","Multi-pack soap",2),candidate("DIM-RICE","Rice bag",5),candidate("OOS-EGGS","Unavailable eggs",4,"out_of_stock")]):
        payload={"selection_policy_version":5,"requirement":req,"selected_product":(None if row["availability"] == "out_of_stock" else row),"candidates":[row],"alternatives":[row],"selection_confidence":"suggested","needs_user_choice":False}
        db.session.add(RetailProductCache(retailer="walmart",store_id="A",store_name="Store A",store_address="1 Fixture Way",requested_query=req["base_item"],base_item=req["base_item"],title="fixture",provider_source="serpapi_walmart",verified_location=True,response_json=json.dumps(payload),retrieved_at=datetime.now(timezone.utc)))
    db.session.commit()
