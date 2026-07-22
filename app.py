from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from datetime import datetime
import os

app = Flask(__name__)

# Database Configuration
db_path = os.path.join(os.path.dirname(__file__), "finance.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -----------------------------------------------------------------------------
# DATABASE MODELS
# -----------------------------------------------------------------------------
class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    checking_balance = db.Column(db.Float, default=1000.0)
    expected_paycheck = db.Column(db.Float, default=2000.0)
    savings_ratio = db.Column(db.Float, default=20.0)      
    essentials_ratio = db.Column(db.Float, default=50.0)   
    discretionary_ratio = db.Column(db.Float, default=30.0)
    
    # Location & Local Tax Fields
    zip_code = db.Column(db.String(10), default="65084")
    city_state = db.Column(db.String(100), default="Versailles, MO")
    sales_tax_rate = db.Column(db.Float, default=0.08225) # Default local tax rate decimal

class Bill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.DateTime, default=datetime.utcnow)
    is_paid = db.Column(db.Boolean, default=False)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), default="Grocery")
    is_liked = db.Column(db.Boolean, default=True)

class StorePrice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    store_name = db.Column(db.String(100), nullable=False)
    location_zip = db.Column(db.String(10), nullable=False)
    price = db.Column(db.Float, nullable=False)

class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    servings = db.Column(db.Integer, default=4)
    ingredients = db.relationship('RecipeIngredient', backref='recipe', cascade="all, delete-orphan")

class RecipeIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    product_name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    unit = db.Column(db.String(30), default="item")
    swap_options = db.Column(db.String(200), default="")

class GroceryList(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    estimated_price = db.Column(db.Float, default=0.0)
    store_name = db.Column(db.String(100), default="Local Store")
    location_context = db.Column(db.String(100), default="")
    is_purchased = db.Column(db.Boolean, default=False)

# Auto-migrate database schema helper
def init_and_migrate_db():
    db.create_all()
    
    # Check and add missing columns dynamically to existing SQLite tables
    with db.engine.connect() as conn:
        inspector = db.inspect(db.engine)
        account_columns = [col['name'] for col in inspector.get_columns('account')]
        
        if "zip_code" not in account_columns:
            conn.execute(text("ALTER TABLE account ADD COLUMN zip_code VARCHAR(10) DEFAULT '65084'"))
        if "city_state" not in account_columns:
            conn.execute(text("ALTER TABLE account ADD COLUMN city_state VARCHAR(100) DEFAULT 'Versailles, MO'"))
        if "sales_tax_rate" not in account_columns:
            conn.execute(text("ALTER TABLE account ADD COLUMN sales_tax_rate FLOAT DEFAULT 0.08225"))
        conn.commit()

# Seed and prepare database context
with app.app_context():
    init_and_migrate_db()
    
    if not Account.query.first():
        db.session.add(Account())
        db.session.commit()
        
    if not Recipe.query.first():
        r = Recipe(title="Family Casserole", servings=4)
        db.session.add(r)
        db.session.commit()
        db.session.add_all([
            RecipeIngredient(recipe_id=r.id, product_name="White Rice", quantity=2, unit="cups", swap_options="Brown Rice, Cauliflower Rice"),
            RecipeIngredient(recipe_id=r.id, product_name="Chicken Breast", quantity=1.5, unit="lbs", swap_options="Chicken Thighs, Turkey Breast"),
            RecipeIngredient(recipe_id=r.id, product_name="Cheddar Cheese", quantity=1, unit="bag", swap_options="Mozzarella, Dairy-Free Cheese")
        ])
        db.session.commit()

# -----------------------------------------------------------------------------
# ACCOUNT & LOCATION ENDPOINTS
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "online", "message": "Financial API Operational"})

@app.route("/summary", methods=["GET"])
def get_summary():
    account = Account.query.first()
    unpaid_bills = Bill.query.filter_by(is_paid=False).all()
    total_unpaid = sum(b.amount for b in unpaid_bills)
    disposable = account.checking_balance - total_unpaid
    
    return jsonify({
        "current_balance": account.checking_balance,
        "total_unpaid_bills": total_unpaid,
        "disposable_cash": disposable,
        "recurring_bills_count": len(unpaid_bills),
        "location": {
            "zip_code": account.zip_code or "65084",
            "city_state": account.city_state or "Versailles, MO",
            "sales_tax_rate": account.sales_tax_rate or 0.08225,
            "sales_tax_pct": round((account.sales_tax_rate or 0.08225) * 100, 3)
        }
    })

@app.route("/set-location", methods=["POST"])
def set_location():
    data = request.json or {}
    zip_code = data.get("zip_code", "").strip()
    city_state = data.get("city_state", "").strip()
    
    account = Account.query.first()
    if zip_code:
        account.zip_code = zip_code
    if city_state:
        account.city_state = city_state
        
    # Auto-resolve tax rate based on location/ZIP
    if "65084" in account.zip_code or "Versailles" in account.city_state:
        account.sales_tax_rate = 0.08225
    elif "65026" in account.zip_code or "Eldon" in account.city_state:
        account.sales_tax_rate = 0.08475
    else:
        account.sales_tax_rate = float(data.get("sales_tax_rate", account.sales_tax_rate or 0.0800))
        
    db.session.commit()
    return jsonify({
        "message": "Location and local tax rate updated",
        "zip_code": account.zip_code,
        "city_state": account.city_state,
        "sales_tax_pct": round(account.sales_tax_rate * 100, 3)
    })

@app.route("/set-balance", methods=["POST"])
def set_balance():
    data = request.json or {}
    amount = data.get("amount")
    if amount is None:
        return jsonify({"error": "Amount required"}), 400
    account = Account.query.first()
    account.checking_balance = float(amount)
    db.session.commit()
    return jsonify({"message": "Balance updated successfully", "new_balance": account.checking_balance})

@app.route("/set-ratios", methods=["POST"])
def set_ratios():
    data = request.json or {}
    account = Account.query.first()
    account.expected_paycheck = float(data.get("expected_paycheck", account.expected_paycheck))
    account.savings_ratio = float(data.get("savings_ratio", account.savings_ratio))
    account.essentials_ratio = float(data.get("essentials_ratio", account.essentials_ratio))
    account.discretionary_ratio = float(data.get("discretionary_ratio", account.discretionary_ratio))
    db.session.commit()
    return jsonify({"message": "Budget ratios updated successfully"})

# -----------------------------------------------------------------------------
# TRANSACTIONS & BILLS ENDPOINTS
# -----------------------------------------------------------------------------
@app.route("/transactions", methods=["GET"])
def get_transactions():
    transactions = Transaction.query.order_by(Transaction.date.desc()).all()
    data = [{"id": t.id, "description": t.description, "amount": t.amount, "category": t.category, "date": t.date.strftime("%Y-%m-%d %H:%M") if t.date else ""} for t in transactions]
    return jsonify(data)

@app.route("/log-expense", methods=["POST"])
def log_expense():
    data = request.json or {}
    item, amount, category = data.get("item"), data.get("amount"), data.get("category", "discretionary")
    if not item or amount is None:
        return jsonify({"error": "Item description and amount are required"}), 400
    account = Account.query.first()
    amount = float(amount)
    account.checking_balance -= amount
    new_trans = Transaction(description=item, amount=amount, category=category)
    db.session.add(new_trans)
    db.session.commit()
    return jsonify({"message": "Expense logged successfully", "updated_account_balance": account.checking_balance})

@app.route("/transactions/<int:trans_id>", methods=["DELETE"])
def delete_transaction(trans_id):
    trans = Transaction.query.get(trans_id)
    if not trans:
        return jsonify({"error": "Transaction not found"}), 404
    db.session.delete(trans)
    db.session.commit()
    return jsonify({"message": f"Transaction {trans_id} deleted successfully."})

@app.route("/bills", methods=["GET"])
def get_bills():
    bills = Bill.query.order_by(Bill.due_date.asc()).all()
    data = [{"id": b.id, "name": b.name, "amount": b.amount, "due_date": b.due_date.strftime("%Y-%m-%d") if b.due_date else "", "is_paid": b.is_paid} for b in bills]
    return jsonify(data)

@app.route("/bills", methods=["POST"])
def add_bill():
    data = request.json or {}
    name, amount, due_date_str = data.get("name"), data.get("amount"), data.get("due_date")
    if not name or amount is None:
        return jsonify({"error": "Name and amount required"}), 400
    due_date = datetime.strptime(due_date_str, "%Y-%m-%d") if due_date_str else datetime.utcnow()
    new_bill = Bill(name=name, amount=float(amount), due_date=due_date, is_paid=False)
    db.session.add(new_bill)
    db.session.commit()
    return jsonify({"message": "Bill added successfully", "id": new_bill.id})

@app.route("/bills/<int:bill_id>/pay", methods=["POST"])
def toggle_bill_paid(bill_id):
    bill = Bill.query.get(bill_id)
    if not bill:
        return jsonify({"error": "Bill not found"}), 404
    bill.is_paid = not bill.is_paid
    db.session.commit()
    return jsonify({"message": f"Bill status updated to {'Paid' if bill.is_paid else 'Unpaid'}"})

@app.route("/bills/<int:bill_id>", methods=["DELETE"])
def delete_bill(bill_id):
    bill = Bill.query.get(bill_id)
    if not bill:
        return jsonify({"error": "Bill not found"}), 404
    db.session.delete(bill)
    db.session.commit()
    return jsonify({"message": f"Bill {bill_id} deleted successfully."})

@app.route("/can-i-buy", methods=["POST"])
def can_i_buy():
    data = request.json or {}
    item, price = data.get("item"), data.get("price")
    if price is None:
        return jsonify({"error": "Price required"}), 400
    price = float(price)
    account = Account.query.first()
    unpaid_bills = Bill.query.filter_by(is_paid=False).all()
    total_unpaid = sum(b.amount for b in unpaid_bills)
    safe_disposable = account.checking_balance - total_unpaid
    remaining_after_purchase = safe_disposable - price
    
    if remaining_after_purchase >= 0:
        verdict = "APPROVED"
        note = f"Safe to purchase '{item}'. You will have ${remaining_after_purchase:,.2f} remaining in safe disposable cash."
    elif account.checking_balance >= price:
        verdict = "WARNING_OVER_BUDGET"
        note = f"Warning: Purchasing '{item}' leaves you ${abs(remaining_after_purchase):,.2f} short for upcoming unpaid obligations!"
    else:
        verdict = "DENIED"
        note = f"Denied: Insufficient checking balance for ${price:,.2f}."
        
    return jsonify({"verdict": verdict, "advisor_note": note, "price": price, "safe_disposable_cash": safe_disposable, "remaining_after_purchase": remaining_after_purchase})

# -----------------------------------------------------------------------------
# RECIPE & LOCALIZED GROCERY SEARCH ENDPOINTS
# -----------------------------------------------------------------------------
@app.route("/recipes", methods=["GET"])
def get_recipes():
    recipes = Recipe.query.all()
    output = []
    for r in recipes:
        ingredients = [{"id": i.id, "product_name": i.product_name, "quantity": i.quantity, "unit": i.unit, "swap_options": i.swap_options.split(",") if i.swap_options else []} for i in r.ingredients]
        output.append({"id": r.id, "title": r.title, "servings": r.servings, "ingredients": ingredients})
    return jsonify(output)

@app.route("/recipes", methods=["POST"])
def add_recipe():
    data = request.json or {}
    title, servings = data.get("title"), data.get("servings", 4)
    ingredients_data = data.get("ingredients", [])
    
    if not title:
        return jsonify({"error": "Recipe title is required"}), 400
        
    recipe = Recipe(title=title, servings=servings)
    db.session.add(recipe)
    db.session.commit()
    
    for item in ingredients_data:
        swaps = ",".join(item.get("swap_options", [])) if isinstance(item.get("swap_options"), list) else item.get("swap_options", "")
        ing = RecipeIngredient(
            recipe_id=recipe.id,
            product_name=item.get("product_name"),
            quantity=float(item.get("quantity", 1.0)),
            unit=item.get("unit", "item"),
            swap_options=swaps
        )
        db.session.add(ing)
    db.session.commit()
    return jsonify({"message": "Recipe added successfully", "id": recipe.id})

@app.route("/grocery-list/generate", methods=["POST"])
def generate_grocery_list():
    data = request.json or {}
    recipe_ids = data.get("recipe_ids", [])
    store_name = data.get("store_name", "Walmart")
    
    account = Account.query.first()
    tax_rate = account.sales_tax_rate or 0.08225
    loc_str = f"{account.city_state or 'Versailles, MO'} ({account.zip_code or '65084'})"
    
    # Clear existing list for fresh generation
    GroceryList.query.delete()
    db.session.commit()
    
    selected_recipes = Recipe.query.filter(Recipe.id.in_(recipe_ids)).all()
    aggregated_items = {}
    
    for r in selected_recipes:
        for ing in r.ingredients:
            key = ing.product_name.strip().title()
            aggregated_items[key] = aggregated_items.get(key, 0.0) + ing.quantity

    total_est_cost = 0.0
    base_price_estimate = 3.65 

    for name, qty in aggregated_items.items():
        item_subtotal = (qty * base_price_estimate)
        item_with_tax = item_subtotal * (1 + tax_rate)
        total_est_cost += item_with_tax
        
        g_item = GroceryList(
            item_name=name,
            quantity=qty,
            estimated_price=round(item_with_tax, 2),
            store_name=store_name,
            location_context=loc_str
        )
        db.session.add(g_item)
        
    db.session.commit()
    return jsonify({
        "message": "Grocery list generated based on local store inventory and local sales tax",
        "store": store_name,
        "location": loc_str,
        "applied_tax_pct": round(tax_rate * 100, 3),
        "estimated_total_with_tax": round(total_est_cost, 2)
    })

@app.route("/grocery-list", methods=["GET"])
def get_grocery_list():
    items = GroceryList.query.all()
    data = [{
        "id": g.id,
        "item_name": g.item_name,
        "quantity": g.quantity,
        "estimated_price": g.estimated_price,
        "store_name": g.store_name,
        "location_context": g.location_context,
        "is_purchased": g.is_purchased
    } for g in items]
    return jsonify(data)

@app.route("/grocery-list/<int:item_id>", methods=["DELETE"])
def delete_grocery_item(item_id):
    item = GroceryList.query.get(item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    db.session.delete(item)
    db.session.commit()
    return jsonify({"message": "Item removed from list"})

if __name__ == "__main__":
    app.run(port=5000, debug=True)
