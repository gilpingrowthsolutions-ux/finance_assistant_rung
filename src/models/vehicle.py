from app import db


class VehicleLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mileage = db.Column(db.Float, nullable=False)
    gallons_filled = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, default=db.func.current_timestamp())