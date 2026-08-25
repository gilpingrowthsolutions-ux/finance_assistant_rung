"""Standalone destructive-free contention probe for a verified disposable PostgreSQL DB."""
from __future__ import annotations
import multiprocessing as mp
import uuid


def worker(args):
    household_id, destination_id, index = args
    from app import app
    from services.savings_allocation import apply_allocation
    plan = {"feasible_cents": 12345, "allocated_cents": 12345, "allocations": [{"destination_id": destination_id, "amount_cents": 12345, "reason": "waterfall_remainder"}]}
    with app.app_context():
        return apply_allocation(household_id, operation_id=f"pg-op-{index}", cycle_key="contention-cycle", plan=plan).id


if __name__ == "__main__":
    from app import app
    from extensions import db
    from models import Household, SavingsAllocationRun, SavingsDestination, SavingsTransfer
    scope = "pkg1314-" + uuid.uuid4().hex
    with app.app_context():
        household = Household(public_id=str(uuid.uuid4()), legacy_scope_key=scope); db.session.add(household); db.session.flush()
        destination = SavingsDestination(household_id=household.id, kind="flexible", name="Flexible Savings", priority=1000); db.session.add(destination); db.session.commit()
        household_id, destination_id = household.id, destination.id
    with mp.Pool(8) as pool:
        ids = pool.map(worker, [(household_id, destination_id, i) for i in range(8)])
    with app.app_context():
        runs = SavingsAllocationRun.query.filter_by(household_id=household_id).count()
        rows = SavingsTransfer.query.filter_by(household_id=household_id).all()
        result = {"worker_run_ids": ids, "runs": runs, "transfers": len(rows), "amount_cents": sum(row.amount_cents for row in rows)}
        print(result)
        assert runs == 1 and len(rows) == 1 and result["amount_cents"] == 12345 and len(set(ids)) == 1
