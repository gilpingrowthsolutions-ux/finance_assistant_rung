"""Standalone concurrency probe for a verified disposable PostgreSQL database."""
from __future__ import annotations
import multiprocessing as mp


PAYLOAD = {
    "operation_id": "pkg16-pg-ignore-once",
    "candidate_key": "recurring:planet fitness",
    "action": "ignore",
    "pattern_signature": "pg-gate-signature",
    "typical_amount_cents": 1500,
    "cadence_days": 30,
    "occurrence_count": 3,
}


def worker(_index):
    from app import app
    response = app.test_client().post("/api/behavior-intelligence/decision", json=PAYLOAD)
    return response.status_code, response.get_json().get("decision_id")


if __name__ == "__main__":
    from app import app
    from extensions import db
    from models import BehaviorIntelligenceDecision
    from services.household_context import ensure_legacy_household
    with app.app_context():
        ensure_legacy_household(); db.session.commit()
    with mp.Pool(8) as pool:
        results = pool.map(worker, range(8))
    with app.app_context():
        rows = BehaviorIntelligenceDecision.query.filter_by(operation_id=PAYLOAD["operation_id"]).all()
        print({"responses": results, "rows": len(rows), "decision_ids": sorted({row.id for row in rows})})
        assert len(rows) == 1
        assert {status for status, _ in results} <= {200, 201}
        assert len({identifier for _, identifier in results}) == 1
