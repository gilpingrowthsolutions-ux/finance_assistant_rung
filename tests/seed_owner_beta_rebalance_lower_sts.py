"""State Y: the existing Shopping fixture plus one exact-payday required Need."""
from datetime import datetime, timedelta, timezone

# The base fixture establishes state X (canonical STS $500, exact Store A/cart).
import tests.seed_feature5_rebalance_browser  # noqa: F401

from app import app
from extensions import db
from models import Bill, Household

with app.app_context():
    household = Household.query.filter_by(legacy_scope_key='feature5-rebalance').one()
    # State X already has the $20 required grocery baseline. Expected income
    # is $1,000 on this date; this $1,460 Need leaves a $460 forward reserve,
    # lowering State X's $480 STS to State Y's exact $20.
    db.session.add(Bill(household_id=household.id, name='Forward required Need',
                        amount=1460, due_date=datetime.now(timezone.utc) + timedelta(days=14),
                        is_paid=False))
    db.session.commit()
