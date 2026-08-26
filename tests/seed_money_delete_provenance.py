"""Add a safe Finished Shopping provenance fixture to a disposable Money DB."""
from __future__ import annotations

from app import app
from extensions import db
from models import ExpenseTransaction, ShoppingTripCompletion


with app.app_context():
    tx = ExpenseTransaction.query.filter_by(description="Existing pharmacy pickup").one()
    db.session.add(ShoppingTripCompletion(
        household_id=tx.household_id,
        operation_id="money-delete-protected-shopping",
        trip_token="money-delete-protected-shopping-trip",
        transaction_id=tx.id,
        retailer="walmart",
        store_name="Walmart",
        store_id="357",
        planned_total_cents=1800,
        actual_total_cents=1800,
        cart_signature="money-delete-protected-shopping-cart",
    ))
    db.session.commit()
    print(f"seeded protected transaction_id={tx.id}")
