"""Seed the explicit disposable Money (Accounts/Transactions/Bills/Cash Flow)
beta-qualification browser database: one financially-ready household with
hand-calculable canonical Safe-to-Spend inputs used to trace Scenarios B-H,
plus one second isolated household (Scenario I) with different, truthfully
"no required expenses reviewed" numbers.

Ready household canonical arithmetic (see services/pyf_financial_state.py
and app.py's ``_compute_safe_to_spend_snapshot``):
  checking            = $2,000.00
  period income (plan)= $1,500.00 (only used for the PYF percent target)
  PYF target          = 10%              -> target_savings = $150.00
  protected buffer    = $200.00
  window_end          = next payday (+10 days)
  Needs (initial):
    Rent bill (due in 5 days, unpaid)      = $600.00
    Internet bill (due in 7 days, unpaid)  =  $70.00
    Required fuel (gas estimate, unpaid)   =  $50.00
    Grocery baseline remaining             = $300.00 (no grocery spend yet)
    Needs total                            = $1,020.00
  available_after_needs_and_buffer = 2000 - 1020 - 200 = $780.00
  feasible_savings = min(150, 780) = $150.00  (full_target_feasible)
  Safe-to-Spend (initial) = 780 - 150 = $630.00

The Internet bill is intentionally left unpaid so Scenario E ("Mark Paid")
has a real forecast Need to remove without manufacturing a checking-balance
change. One pre-existing discretionary transaction is seeded so the
Transactions list is non-empty on first load without perturbing the Needs
math above (checking_balance is stored directly on Account, not derived by
summing transactions).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import (
    NEXT_PAYDAY_SETTING_KEY,
    PYF_TARGET_SETTING_KEY,
    REQUIRED_EXPENSE_NONE,
    REQUIRED_EXPENSE_REVIEWED,
    REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY,
    app,
)
from extensions import db
from models import (
    Account,
    Bill,
    ExpenseTransaction,
    Household,
    HouseholdMembership,
    IncomePlanVersion,
    User,
    UserPreference,
    UserSetting,
)

with app.app_context():
    db.create_all()
    now = datetime.now(timezone.utc)
    next_payday = (now + timedelta(days=10)).date()

    # --- Ready household -------------------------------------------------
    ready = Household()
    db.session.add(ready)
    db.session.flush()
    ready_hid = ready.id

    account = Account(household_id=ready_hid, checking_balance=2000.00, pay_period_days=14, is_onboarded=True)
    db.session.add(account)
    db.session.flush()

    ready_user = User(email="money-ready@example.com", password_hash=generate_password_hash("money-pass-123"), active=True)
    db.session.add(ready_user)
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=ready_user.id, household_id=ready_hid, role="owner", active=True))

    db.session.add_all([
        IncomePlanVersion(household_id=ready_hid, operation_id="money-ready-plan", expected_income_cents=150000,
                           effective_at=now - timedelta(days=1), source="test_setup"),
        UserSetting(household_id=ready_hid, key=NEXT_PAYDAY_SETTING_KEY, value=next_payday.isoformat()),
        UserSetting(household_id=ready_hid, key=PYF_TARGET_SETTING_KEY, value="10"),
        UserSetting(household_id=ready_hid, key=SAFE_BUFFER_SETTING_KEY, value="200.00"),
        UserSetting(household_id=ready_hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED),
        UserPreference(household_id=ready_hid, key="baseline_grocery_cost", value="300.00"),
        Bill(household_id=ready_hid, name="Rent", amount=600.00, due_date=now + timedelta(days=5), is_paid=False),
        Bill(household_id=ready_hid, name="Internet", amount=70.00, due_date=now + timedelta(days=7), is_paid=False),
        Bill(household_id=ready_hid, name="Required fuel", amount=50.00, due_date=now + timedelta(days=3),
             is_paid=False, is_gas_estimate=True),
        ExpenseTransaction(household_id=ready_hid, description="Existing pharmacy pickup", amount=18.00,
                            category="discretionary", source="manual", local_account_id=account.id,
                            date=now - timedelta(days=2)),
    ])

    # --- Second isolated household (Scenario I + mobile pass, Scenario J) -
    iso = Household()
    db.session.add(iso)
    db.session.flush()
    iso_hid = iso.id

    iso_account = Account(household_id=iso_hid, checking_balance=333.00, pay_period_days=14, is_onboarded=True)
    db.session.add(iso_account)
    db.session.flush()

    iso_user = User(email="money-iso@example.com", password_hash=generate_password_hash("money-pass-123"), active=True)
    db.session.add(iso_user)
    db.session.flush()
    db.session.add(HouseholdMembership(user_id=iso_user.id, household_id=iso_hid, role="owner", active=True))

    # Explicit reviewed-none makes grocery/fuel known-zero without a Bill row
    # (see app.py's no_expenses_reviewed branch): Needs = $0.00, so this
    # household's Safe-to-Spend is $238.00 (333 checking - 0 needs - 50
    # buffer - 45 feasible PYF at 5% of $900), distinct from the ready
    # household on every axis, proving isolation rather than coincidence.
    db.session.add_all([
        IncomePlanVersion(household_id=iso_hid, operation_id="money-iso-plan", expected_income_cents=90000,
                           effective_at=now - timedelta(days=1), source="test_setup"),
        UserSetting(household_id=iso_hid, key=NEXT_PAYDAY_SETTING_KEY, value=(now + timedelta(days=6)).date().isoformat()),
        UserSetting(household_id=iso_hid, key=PYF_TARGET_SETTING_KEY, value="5"),
        UserSetting(household_id=iso_hid, key=SAFE_BUFFER_SETTING_KEY, value="50.00"),
        UserSetting(household_id=iso_hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_NONE),
    ])

    db.session.commit()
    print("seeded ready_household_id", ready_hid, "iso_household_id", iso_hid)
