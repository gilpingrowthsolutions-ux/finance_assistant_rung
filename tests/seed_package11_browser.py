"""Create only the disposable schema for first-run onboarding acceptance."""

from app import app
from extensions import db


with app.app_context():
    db.create_all()
    print("created disposable Package 11 browser schema")
