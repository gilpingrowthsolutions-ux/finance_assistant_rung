from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False, default='discretionary')
    date = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Purchase {self.item_name}>'
