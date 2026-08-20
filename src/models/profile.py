from app import db


class AccountBalance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    current_balance = db.Column(db.Float, nullable=False, default=0.0)
    expected_paycheck = db.Column(db.Float, default=2000.0)
    paycheck_frequency_days = db.Column(db.Integer, default=14)
    next_payday = db.Column(db.Date, nullable=True)

    savings_ratio = db.Column(db.Float, default=20.0)
    essentials_ratio = db.Column(db.Float, default=50.0)
    discretionary_ratio = db.Column(db.Float, default=30.0)

    safety_cushion = db.Column(db.Float, default=100.0)
    weekly_grocery_budget = db.Column(db.Float, default=150.0)
    weekly_fuel_budget = db.Column(db.Float, default=50.0)


class RecurringBill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    is_paid = db.Column(db.Boolean, default=False)