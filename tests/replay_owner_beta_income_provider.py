"""Replay the already-stored provider observations through production projection."""
from app import app
from extensions import db
from models import User
from services.transaction_reconciliation import project_plaid_transactions

with app.app_context():
    user = User.query.filter_by(email='income-review-browser@example.com').one()
    result = project_plaid_transactions(f'user:{user.id}')
    db.session.commit()
    print(result)
