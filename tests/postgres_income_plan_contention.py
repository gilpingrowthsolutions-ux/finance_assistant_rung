"""Eight-process idempotency probe for an explicitly disposable PostgreSQL DB."""
from __future__ import annotations
import multiprocessing as mp

PAYLOAD={"expected_paycheck":1450,"expected_paycheck_operation_id":"pkg17-pg-same-operation"}


def worker(_index):
    from app import app
    response=app.test_client().post("/api/account/update",json=PAYLOAD)
    body=response.get_json() or {}
    return response.status_code, ((body.get("income_plan") or {}).get("pending") or {}).get("id")


if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from app import NEXT_PAYDAY_SETTING_KEY, app
    from extensions import db
    from models import Account, Household, IncomePlanVersion, UserSetting
    from services.income_plan import resolve_income_plan
    from services.household_context import ensure_legacy_household
    with app.app_context():
        household=ensure_legacy_household(); now=datetime.now(timezone.utc)
        hid=household.id
        IncomePlanVersion.query.filter_by(household_id=hid).delete()
        account=Account.query.filter_by(household_id=hid).first()
        if account is None:
            account=Account(household_id=hid,checking_balance=2000,pay_period_days=14);db.session.add(account)
        setting=UserSetting.query.filter_by(household_id=hid,key=NEXT_PAYDAY_SETTING_KEY).first()
        if setting is None: db.session.add(UserSetting(household_id=hid,key=NEXT_PAYDAY_SETTING_KEY,value=(now.date()+timedelta(days=7)).isoformat()))
        else: setting.value=(now.date()+timedelta(days=7)).isoformat()
        db.session.add(IncomePlanVersion(household_id=hid,operation_id="pkg17-pg-initial",expected_income_cents=100000,effective_at=now-timedelta(days=30),source="test_confirmation"))
        db.session.commit()
    with mp.Pool(8) as pool: results=pool.map(worker,range(8))
    with app.app_context():
        rows=IncomePlanVersion.query.filter_by(household_id=hid).order_by(IncomePlanVersion.id).all()
        matching=[row for row in rows if row.operation_id==PAYLOAD["expected_paycheck_operation_id"]]
        print({"responses":results,"rows":len(rows),"matching":len(matching),"ids":sorted({row.id for row in matching})})
        assert len(rows)==2 and len(matching)==1
        assert {status for status,_ in results}=={200}
        assert len({identifier for _,identifier in results})==1
        other=Household(public_id=str(uuid4()));db.session.add(other);db.session.flush()
        db.session.add(IncomePlanVersion(household_id=other.id,operation_id="other-household-plan",expected_income_cents=77700,effective_at=now-timedelta(days=1),source="test_confirmation"));db.session.commit()
        assert resolve_income_plan(hid,at=now).expected_income_cents==100000
        assert resolve_income_plan(hid,at=now+timedelta(days=8)).expected_income_cents==145000
        assert resolve_income_plan(other.id,at=now).expected_income_cents==77700
