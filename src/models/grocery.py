from app import db


class Grocery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    is_purchased = db.Column(db.Boolean, default=False)
