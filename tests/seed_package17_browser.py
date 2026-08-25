"""Seed only the explicit disposable Package 17 browser database."""
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import NEXT_PAYDAY_SETTING_KEY, PYF_TARGET_SETTING_KEY, SAFE_BUFFER_SETTING_KEY, app
from extensions import db
from models import Account, Bill, ExpenseTransaction, HouseholdMembership, IncomePlanVersion, SavingsAllocationRun, SavingsDestination, SavingsGoal, SavingsReserve, SavingsTransfer, ShoppingTripCompletion, User, UserPreference, UserSetting
from services.household_context import household_id

with app.app_context():
    db.create_all(); hid=household_id(); now=datetime.now(timezone.utc)
    current_start=now.date()-timedelta(days=7); previous_start=current_start-timedelta(days=14)
    account=Account(household_id=hid,checking_balance=2000,expected_paycheck=9999,pay_period_days=14,is_onboarded=True)
    db.session.add(account);db.session.flush()
    user=User(email="recap-browser@example.com",password_hash=generate_password_hash("browser-pass-123"),active=True)
    db.session.add(user);db.session.flush();db.session.add(HouseholdMembership(user_id=user.id,household_id=hid,role="owner",active=True))
    goal_dest=SavingsDestination(household_id=hid,kind="goal",name="Family Vacation",priority=1)
    reserve_dest=SavingsDestination(household_id=hid,kind="reserve",name="Emergency Reserve",priority=2)
    db.session.add_all([goal_dest,reserve_dest]);db.session.flush()
    db.session.add_all([
        IncomePlanVersion(household_id=hid,operation_id="browser-current-plan",expected_income_cents=100000,effective_at=datetime.combine(previous_start,datetime.min.time(),tzinfo=timezone.utc),source="onboarding_confirmation"),
        SavingsGoal(household_id=hid,destination_id=goal_dest.id,create_operation_id="goal-create",target_cents=100000,status="active"),
        SavingsReserve(household_id=hid,destination_id=reserve_dest.id,create_operation_id="reserve-create",category="emergency",target_cents=50000,status="active"),
        UserSetting(household_id=hid,key=NEXT_PAYDAY_SETTING_KEY,value=(current_start+timedelta(days=14)).isoformat()),
        UserSetting(household_id=hid,key=PYF_TARGET_SETTING_KEY,value="20"),UserSetting(household_id=hid,key=SAFE_BUFFER_SETTING_KEY,value="100"),
        UserPreference(household_id=hid,key="baseline_grocery_cost",value="100"),
        Bill(household_id=hid,name="Electric Utility",amount=100,due_date=datetime.combine(previous_start+timedelta(days=5),datetime.min.time(),tzinfo=timezone.utc),is_paid=True),
        Bill(household_id=hid,name="Current Rent",amount=400,due_date=now+timedelta(days=3),is_paid=False),
        Bill(household_id=hid,name="Required fuel",amount=50,due_date=now+timedelta(days=2),is_paid=False,is_gas_estimate=True),
        SavingsAllocationRun(household_id=hid,operation_id="completed-cycle-run",cycle_key=current_start.isoformat(),feasible_cents=20000,allocated_cents=20000),
        SavingsTransfer(household_id=hid,operation_id="goal-funding",destination_id=goal_dest.id,amount_cents=12000,transfer_type="pyf_allocation",created_at=datetime.combine(previous_start+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)),
        SavingsTransfer(household_id=hid,operation_id="reserve-funding",destination_id=reserve_dest.id,amount_cents=8000,transfer_type="pyf_allocation",created_at=datetime.combine(previous_start+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)),
    ])
    paycheck=ExpenseTransaction(household_id=hid,description="Paycheck",amount=1000,category="income",source="manual",local_account_id=account.id,date=datetime.combine(previous_start,datetime.min.time(),tzinfo=timezone.utc))
    electric=ExpenseTransaction(household_id=hid,description="Electric Utility payment",amount=80,category="utilities",source="manual",plaid_transaction_id="linked-electric",local_account_id=account.id,date=datetime.combine(previous_start+timedelta(days=5),datetime.min.time(),tzinfo=timezone.utc))
    shopping=ExpenseTransaction(household_id=hid,description="Finished Shopping",amount=75,category="grocery",source="manual",plaid_transaction_id="linked-shopping",local_account_id=account.id,date=datetime.combine(previous_start+timedelta(days=8),datetime.min.time(),tzinfo=timezone.utc))
    dining=ExpenseTransaction(household_id=hid,description="Family dining",amount=40,category="dining",source="manual",local_account_id=account.id,date=datetime.combine(previous_start+timedelta(days=10),datetime.min.time(),tzinfo=timezone.utc))
    db.session.add_all([paycheck,electric,shopping,dining]);db.session.flush()
    db.session.add(ShoppingTripCompletion(household_id=hid,operation_id="trip-complete",trip_token="trip-17",transaction_id=shopping.id,retailer="walmart",store_name="Local Store",planned_total_cents=7500,actual_total_cents=7500,amount_source="actual",cart_signature="pkg17",manual_provisional=True,completed_at=shopping.date))
    db.session.commit();print("seeded",hid)
