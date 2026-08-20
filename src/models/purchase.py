from datetime import datetime

# Compatibility shim: keep legacy src.models imports on the app's registered
# SQLAlchemy instance instead of creating a second, unbound database object.
from app import db


class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False, default='discretionary')
    date = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Purchase {self.item_name}>'
