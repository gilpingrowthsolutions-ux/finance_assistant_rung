"""Inject a deterministic provider payload through Rung's ingestion projection.

This is a browser-fixture helper, not a shortcut to a reconciliation decision:
it uses the same raw-provider upsert and projection functions as sync, and it
never creates a reconciliation or a linked/superseded final state itself.
"""
from __future__ import annotations

import json
import sys
from datetime import date

from app import app
from extensions import db
from models import (Account, ExpenseTransaction, HouseholdMembership,
                    IncomePyfProtection, PlaidItem, SavingsTransfer,
                    SavingsTransferReconciliation, User)
from services.plaid_foundation import _upsert_transaction_row
from services.transaction_reconciliation import project_plaid_transactions


EMAIL = "physical-transfer-browser@example.com"


def main() -> None:
    event = (sys.argv[1] if len(sys.argv) > 1 else "match").strip()
    if event not in {"match", "separate"}:
        raise SystemExit("event must be match or separate")
    with app.app_context():
        user = User.query.filter_by(email=EMAIL).one()
        membership = HouseholdMembership.query.filter_by(user_id=user.id, active=True).one()
        hid = membership.household_id
        item = PlaidItem.query.filter_by(household_id=hid, owner_scope=f"user:{user.id}").first()
        if item is None:
            item = PlaidItem(household_id=hid, owner_scope=f"user:{user.id}",
                             plaid_item_id="owner-beta-provider-item", access_token_encrypted="fixture")
            db.session.add(item)
            db.session.flush()
        amount = "80.00" if event == "match" else "30.00"
        payload = {
            "transaction_id": f"owner-beta-{event}-provider-transfer",
            "account_id": "owner-beta-checking",
            "amount": amount,
            "date": date.today().isoformat(),
            "name": "TRANSFER TO SAVINGS",
            "merchant_name": "TRANSFER TO SAVINGS",
            "original_description": "TRANSFER TO SAVINGS",
            "pending": False,
            "category": ["Transfer", "Internal Account Transfer"],
        }
        counters = {"added": 0, "modified": 0, "removed": 0}
        _upsert_transaction_row(plaid_item=item, tx_payload=payload, mode="added", counters=counters)
        db.session.commit()
        projection = project_plaid_transactions(f"user:{user.id}")
        account = Account.query.filter_by(household_id=hid).one()
        print(json.dumps({
            "projection": projection,
            "checking": account.checking_balance,
            "transfers": SavingsTransfer.query.filter_by(household_id=hid).count(),
            "outflows": SavingsTransfer.query.filter_by(household_id=hid, external_direction="outflow").count(),
            "protections": IncomePyfProtection.query.filter_by(household_id=hid).count(),
            "income": ExpenseTransaction.query.filter_by(household_id=hid, category="income").count(),
            "proposals": SavingsTransferReconciliation.query.filter_by(household_id=hid, status="proposed").count(),
            "matched": SavingsTransferReconciliation.query.filter_by(household_id=hid, status="matched").count(),
            "rejected": SavingsTransferReconciliation.query.filter_by(household_id=hid, status="rejected").count(),
        }))


if __name__ == "__main__":
    main()
