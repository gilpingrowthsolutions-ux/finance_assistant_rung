"""Disposable two-household Feature 5 isolation fixture; no production hooks."""
from datetime import datetime, timedelta, timezone
import json
from app import app
from extensions import db
from models import (Account, Bill, ExpenseTransaction, GroceryItem, Household, HouseholdMembership,
    IncomePlanVersion, RetailProductBlock, ShoppingCart, ShoppingCartLine, ShoppingRebalanceProposal,
    ShoppingStoreChangeReview, ShoppingTripCompletion, User, UserPreference, UserSetting)
from services.authoritative_cart import replace_current_from_resolution
from services.selected_store import select_store
from werkzeug.security import generate_password_hash

def product(sku,title,price): return {'product_id':sku,'us_item_id':sku+'-us','retailer':'walmart','title':title,'brand':'Fixture','package_size':'1 package','price':price,'availability':'in_stock','source':'fixture'}
def resolution(sku,title,price):
    req={'item_name':'Isolation detergent','base_item':'isolation detergent','quantity':1,'unit':'package','source_kind':'manual','source_requirement_id':1}
    return {'subtotal':price,'total_cart_cost':price,'cart_items':[{'requirement':req,'selected_product':product(sku,title,price),'alternatives':[],'packages_to_buy':1,'needs_user_choice':False,'availability':'in_stock'}]}
with app.app_context():
 db.drop_all(); db.create_all()
 a,b=Household(legacy_scope_key='iso-a'),Household(legacy_scope_key='iso-b'); db.session.add_all([a,b]); db.session.flush()
 ua,ub=User(email='feature5-isolation-a@example.com',password_hash=generate_password_hash('iso-a'),active=True,auth_version=1),User(email='feature5-isolation-b@example.com',password_hash=generate_password_hash('iso-b'),active=True,auth_version=1)
 aa,ab=Account(household_id=a.id,checking_balance=500,pay_period_days=14),Account(household_id=b.id,checking_balance=500,pay_period_days=14); db.session.add_all([ua,ub,aa,ab]); db.session.flush(); db.session.add_all([HouseholdMembership(user_id=ua.id,household_id=a.id,role='owner',active=True),HouseholdMembership(user_id=ub.id,household_id=b.id,role='owner',active=True)])
 for h in (a,b): db.session.add_all([IncomePlanVersion(household_id=h.id,operation_id='income-'+str(h.id),expected_income_cents=100000,effective_at=datetime.now(timezone.utc),source='fixture'),UserSetting(household_id=h.id,key='pyf_long_term_target_percent',value='0'),UserSetting(household_id=h.id,key='safe_to_spend_buffer_usd',value='0'),UserSetting(household_id=h.id,key='next_payday_date',value=(datetime.now(timezone.utc).date()+timedelta(days=14)).isoformat()),UserSetting(household_id=h.id,key='onboarding_required_expense_review',value='has_expenses_reviewed'),UserPreference(household_id=h.id,key='baseline_grocery_cost',value='100'),Bill(household_id=h.id,name='fuel',amount=0,due_date=datetime.now(timezone.utc),is_gas_estimate=True,is_paid=False)])
 sa=select_store(a.id,retailer='walmart',store_id='A',store_name='Store A',account=aa); sb=select_store(b.id,retailer='walmart',store_id='B',store_name='Store B',account=ab)
 old=replace_current_from_resolution(household_id=a.id,store_identity_id=sa['retail_store_identity_id'],resolved_cart=resolution('A-HIST','A historical detergent',9)); old.status='completed'; old.completed_at=datetime.now(timezone.utc); tx=ExpenseTransaction(household_id=a.id,description='A finished trip',amount=9,category='grocery',source='manual',date=datetime.now(timezone.utc)); db.session.add(tx); db.session.flush(); trip=ShoppingTripCompletion(household_id=a.id,operation_id='a-completed-op',trip_token='a-completed-token',transaction_id=tx.id,retailer='walmart',store_name='Store A',store_id='A',planned_total_cents=900,actual_total_cents=900,amount_source='authoritative_cart',cart_signature='a-history',shopping_cart_id=old.id,manual_provisional=True,completed_at=old.completed_at); db.session.add(trip)
 current=replace_current_from_resolution(household_id=a.id,store_identity_id=sa['retail_store_identity_id'],resolved_cart=resolution('A-CURRENT','A selected detergent',12)); db.session.add(RetailProductBlock(household_id=a.id,block_type='exact_product',retailer='walmart',retailer_product_id='A-BLOCK',retailer_us_item_id='A-BLOCK-us',block_key='exact:walmart:A-BLOCK-us'))
 staged=ShoppingCart(household_id=a.id,retail_store_identity_id=sb['retail_store_identity_id'],status='staged',version=1,source='store_change',subtotal_cents=10,total_cents=10); db.session.add(staged); db.session.flush(); db.session.add(ShoppingCartLine(cart_id=staged.id,requirement_key='manual:requirement:1',requirement_json='{}',retailer='walmart',title='A staged detergent',package_count=1,unit_price_cents=10,line_total_cents=10,availability='in_stock',resolution_state='resolved'))
 review=ShoppingStoreChangeReview(household_id=a.id,current_cart_id=current.id,staged_cart_id=staged.id,from_store_identity_id=sa['retail_store_identity_id'],to_store_identity_id=sb['retail_store_identity_id'],status='pending',operation_id='a-store-review'); db.session.add(review); db.session.flush(); proposal=ShoppingRebalanceProposal(household_id=a.id,base_cart_id=current.id,base_cart_version=current.version,operation_id='a-rebalance',status='pending'); db.session.add(proposal)
 replace_current_from_resolution(household_id=b.id,store_identity_id=sb['retail_store_identity_id'],resolved_cart=resolution('B-CURRENT','B independent detergent',4)); db.session.commit()
