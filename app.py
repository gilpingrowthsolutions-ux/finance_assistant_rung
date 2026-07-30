import os
import urllib.parse
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy

# Load environment variables from .env before anything else.
# This makes KROGER_CLIENT_ID / KROGER_CLIENT_SECRET (and any other
# secrets) available via os.environ for the rest of the application.
load_dotenv()

# Live retail API resolvers (Kroger + RapidAPI + local price cache)
from services.store_api import resolve_terms, pick_best
from services.kroger_api import find_nearest_kroger
from services.rapidapi_search import search_local_product, rapid_result_to_product_dict

app = Flask(__name__)
db_path = os.path.join(os.path.dirname(__file__), "rung_finance.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# =============================================================================
# DATABASE MODELS
# =============================================================================
class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    checking_balance = db.Column(db.Float, default=1250.00)
    food_allocation_pct = db.Column(db.Float, default=40.0)  # % of safe disposable
    pay_period_days = db.Column(db.Integer, default=14)
    meals_per_day = db.Column(db.Integer, default=3)
    vault_balance = db.Column(db.Float, default=150.00)
    expected_paycheck = db.Column(db.Float, default=2000.00)
    
    # Geolocated Data & Local Taxes
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    zip_code = db.Column(db.String(10), default="65084")
    city_state = db.Column(db.String(100), default="Versailles, MO")
    sales_tax_rate = db.Column(db.Float, default=0.0825)   # 8.25%
    grocery_tax_rate = db.Column(db.Float, default=0.0125) # 1.25%
    
    # Auto-detected nearest Kroger / Gerbes store
    kroger_location_id = db.Column(db.String(20), nullable=True)
    kroger_store_name = db.Column(db.String(100), default="Kroger")

class Bill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.DateTime, nullable=False)
    is_gas_estimate = db.Column(db.Boolean, default=False)
    is_paid = db.Column(db.Boolean, default=False)

class PantryItem(db.Model):
    __tablename__ = 'pantry_inventory'
    id = db.Column(db.Integer, primary_key=True)
    clean_keyword = db.Column(db.String(100), nullable=False, unique=True) # e.g. "flour", "chicken"
    product_name = db.Column(db.String(150), nullable=False)
    quantity = db.Column(db.Float, default=0.0)
    unit = db.Column(db.String(30), default="oz") # Standardized base unit

class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    servings = db.Column(db.Integer, default=4)
    estimated_cost_per_serving = db.Column(db.Float, default=3.50)
    instructions = db.Column(db.Text, nullable=True)
    source_url = db.Column(db.String(500), nullable=True, unique=True)
    ingredients = db.relationship('RecipeIngredient', backref='recipe', cascade="all, delete-orphan")

class RecipeIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    product_name = db.Column(db.String(100), nullable=False)
    clean_keyword = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    unit = db.Column(db.String(30), default="oz")

class BrandPreference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    clean_keyword = db.Column(db.String(100), nullable=False, unique=True)
    prefer_store_brand = db.Column(db.Boolean, default=True)
    preferred_brand_name = db.Column(db.String(100), nullable=True)

class ExpenseTransaction(db.Model):
    __tablename__ = 'expense_transactions'
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(150), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), default='discretionary')
    date = db.Column(db.DateTime, default=datetime.utcnow)

class GroceryItem(db.Model):
    __tablename__ = 'grocery_items'
    id = db.Column(db.Integer, primary_key=True)
    recipe_ids = db.Column(db.String(200), default='')
    item_name = db.Column(db.String(150), nullable=False)
    estimated_price = db.Column(db.Float, default=0.0)
    store_name = db.Column(db.String(100), default='')
    location_context = db.Column(db.String(100), default='')
    is_purchased = db.Column(db.Boolean, default=False)

class RapidPriceCache(db.Model):
    __tablename__ = 'rapid_price_cache'
    id = db.Column(db.Integer, primary_key=True)
    ingredient_keyword = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)
    store_name = db.Column(db.String(100), default='')
    package_size = db.Column(db.String(100), nullable=True)
    image_url = db.Column(db.String(500), nullable=True)
    product_url = db.Column(db.String(500), nullable=True)
    location = db.Column(db.String(100), default='')
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)


class StorePriceCache(db.Model):
    __tablename__ = 'store_price_cache'
    id = db.Column(db.Integer, primary_key=True)
    store_name = db.Column(db.String(100), nullable=False)
    item_keyword = db.Column(db.String(100), nullable=False)
    product_title = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)
    unit = db.Column(db.String(30), default='each')
    package_size = db.Column(db.String(100), nullable=True)
    image_url = db.Column(db.String(500), nullable=True)
    retailer = db.Column(db.String(50), default='kroger')
    is_store_brand = db.Column(db.Boolean, default=False)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)

# =============================================================================
# UNIT CONVERSION & HELPER ENGINES
# =============================================================================
UNIT_TO_OZ = {
    'oz': 1.0, 'ounce': 1.0, 'ounces': 1.0,
    'lb': 16.0, 'lbs': 16.0, 'pound': 16.0, 'pounds': 16.0,
    'cup': 8.0, 'cups': 8.0,
    'tbsp': 0.5, 'tablespoon': 0.5, 'tablespoons': 0.5,
    'tsp': 0.166, 'teaspoon': 0.166, 'teaspoons': 0.166,
    'g': 0.035, 'gram': 0.035, 'grams': 0.035,
    'unit': 1.0, 'item': 1.0, 'ea': 1.0, 'can': 1.0
}

def normalize_to_standard_unit(quantity, unit):
    clean_u = (unit or 'unit').strip().lower()
    multiplier = UNIT_TO_OZ.get(clean_u, 1.0)
    return quantity * multiplier

def compute_liquidity_metrics(account):
    """Core Pay Period Liquidity Engine."""
    pay_period_end = datetime.utcnow() + timedelta(days=account.pay_period_days)
    
    # 1. Unpaid bills due within current pay period
    upcoming_bills = Bill.query.filter(
        Bill.is_paid == False,
        Bill.due_date <= pay_period_end,
        Bill.is_gas_estimate == False
    ).all()
    bills_total = sum(b.amount for b in upcoming_bills)
    
    # 2. Gas Allocation
    gas_bill = Bill.query.filter_by(is_gas_estimate=True, is_paid=False).first()
    gas_allocation = gas_bill.amount if gas_bill else 60.00
    
    # 3. True Disposable Cash
    safe_disposable = account.checking_balance - bills_total - gas_allocation
    
    # 4. Food Budget Allocation
    food_budget = max(0.0, safe_disposable * (account.food_allocation_pct / 100.0))
    total_meals = account.pay_period_days * account.meals_per_day
    per_meal_budget = food_budget / total_meals if total_meals > 0 else 0.0
    
    # 5. Non-Food Unallocated Free Cash
    free_cash = safe_disposable - food_budget
    
    return {
        "checking_balance": account.checking_balance,
        "expected_paycheck": account.expected_paycheck if hasattr(account, 'expected_paycheck') else 2000.00,
        "vault_balance": account.vault_balance if hasattr(account, 'vault_balance') else 150.00,
        "upcoming_bills_total": round(bills_total, 2),
        "gas_allocation": round(gas_allocation, 2),
        "safe_disposable_cash": round(safe_disposable, 2),
        "food_budget": round(food_budget, 2),
        "total_meals": total_meals,
        "target_per_meal_budget": round(per_meal_budget, 2),
        "free_cash_remaining": round(free_cash, 2),
        "location": {
            "zip_code": account.zip_code or "65084",
            "city_state": getattr(account, 'city_state', 'Versailles, MO') or "Versailles, MO",
            "sales_tax_rate": account.sales_tax_rate or 0.0825,
            "sales_tax_pct": round((account.sales_tax_rate or 0.0825) * 100, 3),
            "grocery_tax_rate": account.grocery_tax_rate or 0.0125
        }
    }

# =============================================================================
# API ROUTES
# =============================================================================

@app.route("/")
def index():
    """Serves the main single-page UI from templates/index.html"""
    return render_template("index.html")

# ----- RECIPES CRUD ----------------------------------------------------------

@app.route("/api/recipes", methods=["GET", "POST"])
def recipes_crud():
    if request.method == "POST":
        data = request.json or {}
        title = data.get("title", "").strip()
        if not title:
            return jsonify({"error": "Title required"}), 400
        servings = int(data.get("servings", 4))
        instructions = data.get("instructions", "")
        r = Recipe(title=title, servings=servings, instructions=instructions)
        db.session.add(r)
        db.session.flush()
        # Parse ingredients from lines or structured list
        ingredients = data.get("ingredients", [])
        for ing in ingredients:
            if isinstance(ing, dict):
                ri = RecipeIngredient(
                    recipe_id=r.id,
                    product_name=ing.get("product_name", ""),
                    clean_keyword=ing.get("clean_keyword", ""),
                    quantity=float(ing.get("quantity", 1)),
                    unit=ing.get("unit", "oz")
                )
            elif isinstance(ing, str) and ing.strip():
                ri = RecipeIngredient(
                    recipe_id=r.id,
                    product_name=ing.strip(),
                    clean_keyword=ing.strip().lower().replace(' ', '_'),
                    quantity=1,
                    unit="item"
                )
            else:
                continue
            db.session.add(ri)
        db.session.commit()
        return jsonify({"message": "Recipe added", "id": r.id})
    # GET: list all recipes with ingredients
    recipes = Recipe.query.all()
    return jsonify([{
        "id": r.id,
        "title": r.title,
        "servings": r.servings,
        "estimated_cost_per_serving": r.estimated_cost_per_serving,
        "source_url": r.source_url or "",
        "instructions": r.instructions,
        "ingredients": [{
            "id": i.id,
            "product_name": i.product_name,
            "clean_keyword": i.clean_keyword,
            "quantity": i.quantity,
            "unit": i.unit
        } for i in r.ingredients]
    } for r in recipes])

@app.route("/api/recipes/<int:rid>", methods=["DELETE"])
def delete_recipe(rid):
    r = Recipe.query.get(rid)
    if not r:
        return jsonify({"error": "Recipe not found"}), 404
    db.session.delete(r)
    db.session.commit()
    return jsonify({"message": f"Recipe {rid} deleted"})

# ---------------------------------------------------------------------------
# In-memory cache for recipe imports (URL → (timestamp, result_dict)).
# Avoids re-scraping the same URL within TTL seconds. Shared across
# requests in the same process.
# ---------------------------------------------------------------------------
_import_cache: dict = {}    # url → {"ts": float, "data": dict}
_IMPORT_CACHE_TTL = 300     # seconds (5 min)


def _parse_recipe_yields(yields_str):
    """Extract a numeric serving count from a yields string.

    Examples
    --------
    "4 servings"    → 4
    "1 serving"     → 1
    "6-8 servings"  → 6
    "2 dozen"       → 2
    "About 4 cups"  → 4
    ""              → 4   (default fallback)
    "serves 10"     → 10
    None            → 4
    "N/A"           → 4
    """
    import re
    if not yields_str:
        return 4
    s = str(yields_str).strip().lower()
    # Range: "6-8 servings" → 6  (must come before the general digit pattern)
    m = re.search(r'(\d+)\s*[-–]\s*\d+\s*(?:servings?|dozen|pieces?|slices?|cups?|cookies?)', s)
    if m:
        return int(m.group(1))
    # Explicit pattern: digits before serving/dozen/etc.
    m = re.search(r'(\d+)\s*(?:servings?|dozen|pieces?|slices?|cups?|muffins?|cookies?|bars?|rolls?|patties?|meatballs?)', s)
    if m:
        return int(m.group(1))
    # "serves N" or "makes N"
    m = re.search(r'(?:serves|makes)\s+(\d+)', s)
    if m:
        return int(m.group(1))
    # First number in the string (but not a fraction like "1/2")
    m = re.search(r'\b(\d+)\b', s)
    if m:
        return int(m.group(1))
    return 4


def _derive_clean_keyword(product_name):
    """Derive a clean keyword from a product name for pantry matching.

    Strips quantities, units, qualifiers, and noise words, then returns
    the last meaningful word(s) lowercased with underscores.

    Examples
    --------
    "2 cups all-purpose flour"         → "flour"
    "½ teaspoon extra-virgin olive oil" → "olive_oil"
    "Sea salt"                         → "salt"
    "Crumbled feta cheese"             → "feta_cheese"
    "1 lb boneless skinless chicken breast" → "chicken_breast"
    "Chopped chives"                   → "chives"
    "2 eggs, per small ramekin"        → "eggs"
    "Olive oil, for drizzling"         → "olive_oil"
    """
    import re
    s = product_name.strip().lower()
    # Strip leading quantity + unit + fraction (e.g. "1 lb", "½ teaspoon", "2 cups")
    s = re.sub(r'^[\d\s¼½¾⅓⅔⅛⅜⅝⅞/.\-–]+\s*', '', s)
    # Strip common unit words at the start — loop because re.sub with ^
    # only anchors once; multiple leading qualifiers need multiple passes.
    _QUAL_RE = re.compile(
        r'^(cup|teaspoon|tablespoon|tbsp|tsp|oz|ounce|lb|lbs|pound|'
        r'can|clove|head|stalk|sprig|pinch|dash|bunch|piece|slice|'
        r'jar|bottle|bag|box|package|pack|g|gram|kg|ml|l|liter|'
        r'quart|pint|gallon|dozen|whole|large|small|medium|'
        r'fresh|frozen|dried|chopped|minced|diced|sliced|crushed|'
        r'grated|shredded|ground|boneless|skinless|trimmed|cooked|'
        r'uncooked|raw|ripe|organic|sea|all.purpose|crumbled|sautéed|'
        r'roasted|toasted|peeled|seeded|cored|halved|quartered|'
        r'thinly|finely|coarsely|roughly|kosher)s?\s+'
    )
    while True:
        s2 = _QUAL_RE.sub('', s, count=1)
        if s2 == s:
            break
        s = s2
    # Strip trailing serving instructions: ", per ...", ", for ...", ", to ...", ", or ..."
    s = re.sub(r',\s*(?:per\s+\w+(?:\s+\w+)*|for\s+\w+(?:\s+\w+)*|to\s+\w+(?:\s+\w+)*|or\s+\w+(?:\s+\w+)*|as\s+\w+(?:\s+\w+)*)', '', s)
    # Strip trailing single-word descriptors: ", thawed", ", rinsed", ", drained", etc.
    s = re.sub(r',\s+\w+$', '', s)
    # Strip parenthetical notes
    s = re.sub(r'\([^)]*\)', '', s)
    # Strip "to taste", "for garnish", "optional", "divided", etc.
    s = re.sub(r',?\s*(?:to taste|for garnish|for serving|or as needed|as needed|optional|divided|or more|more if desired).*', '', s)
    s = s.strip().strip(',').strip()
    if not s:
        return product_name.strip().lower().replace(' ', '_')
    # Take last 2-3 words as the core ingredient name
    words = s.split()
    if len(words) <= 2:
        core = ' '.join(words)
    else:
        core = ' '.join(words[-2:])
    # Clean: lowercase, replace non-alphanumeric with underscore, collapse runs
    core = re.sub(r'[^a-z0-9]+', '_', core.lower())
    core = re.sub(r'_+', '_', core).strip('_')
    return core


def _scrape_with_curl_cffi(url):
    """Fallback scraper for Cloudflare-protected sites.

    Uses ``curl_cffi`` to impersonate a Chrome browser's TLS
    fingerprint, fetches the page HTML, and extracts schema.org
    Recipe data from embedded JSON-LD ``<script>`` tags.

    Returns a dict with the same shape as recipe-scrapers output,
    or ``None`` if no Recipe data could be extracted.
    """
    import re
    import json as _json
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None

    try:
        resp = cffi_requests.get(url, impersonate='chrome120', timeout=15)
        if resp.status_code != 200:
            return None
        html = resp.text
    except Exception:
        return None

    # ---- Extract JSON-LD blocks ----
    ld_blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    for block in ld_blocks:
        try:
            data = _json.loads(block)
        except (_json.JSONDecodeError, ValueError):
            continue

        # JSON-LD can be a single object or a list of objects.
        items = data if isinstance(data, list) else [data]
        for item in items:
            types = item.get('@type', [])
            if not isinstance(types, list):
                types = [types]
            if 'Recipe' not in types:
                continue

            # ---- Found a Recipe object ----
            title = item.get('headline') or item.get('name', 'Imported Recipe')

            # Ingredients
            raw_ingredients = item.get('recipeIngredient', [])
            if isinstance(raw_ingredients, str):
                raw_ingredients = [raw_ingredients]

            # Instructions
            instructions = item.get('recipeInstructions', '')
            if isinstance(instructions, list):
                # List of HowToStep objects
                steps = []
                for step in instructions:
                    if isinstance(step, dict):
                        steps.append(step.get('text', str(step)))
                    elif isinstance(step, str):
                        steps.append(step)
                instructions = '\n'.join(steps)
            elif isinstance(instructions, dict):
                instructions = instructions.get('text', str(instructions))

            # Total time: prefer totalTime, fall back to cookTime + prepTime
            total_time = None
            for key in ('totalTime', 'cookTime', 'prepTime'):
                raw = item.get(key)
                if raw and isinstance(raw, str) and raw.startswith('PT'):
                    # ISO 8601 duration: PT20M → 20, PT1H30M → 90
                    h = re.search(r'(\d+)H', raw)
                    m = re.search(r'(\d+)M', raw)
                    mins = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
                    if mins > 0:
                        total_time = mins
                        break

            # Servings / yield
            yields_val = item.get('recipeYield')
            if isinstance(yields_val, list):
                yields_val = yields_val[0] if yields_val else None
            yields_str = str(yields_val) if yields_val else None

            # Image
            image_obj = item.get('image')
            if isinstance(image_obj, dict):
                image_url = image_obj.get('url')
            elif isinstance(image_obj, list) and image_obj:
                first_img = image_obj[0]
                image_url = first_img.get('url') if isinstance(first_img, dict) else str(first_img)
            else:
                image_url = str(image_obj) if image_obj else None

            return {
                'title': title,
                'total_time': total_time,
                'yields_str': yields_str,
                'instructions': instructions,
                'image_url': image_url,
                'raw_ingredients': raw_ingredients,
                'source': 'json-ld',
            }

    return None  # No Recipe found in any JSON-LD block


@app.route("/api/recipes/import", methods=["POST"])
def import_recipe():
    """Scrape a recipe from a URL and persist it to the local database.

    Uses ``recipe-scrapers`` to extract title, total time, servings,
    ingredients, instructions, and image URL. Falls back to
    ``curl_cffi`` + JSON-LD parsing for sites with Cloudflare
    protection (e.g. allrecipes.com, seriouseats.com).

    Results are cached in memory for 5 minutes.

    Request body
    ------------
    {"url": "https://..."}

    Returns (200 on success)
    ------------------------
    {
      "recipe": {...},
      "action": "created" | "updated",
      "cache": {"status": "ok", ...}
    }
    """
    import re
    import time
    global _import_cache

    data = request.json or {}
    url = (data.get("url", "") or "").strip()
    if not url:
        return jsonify({"error": "URL required", "cache": {"status": "error", "hit": False}}), 400

    # ---- Cache check ----
    now = time.time()
    if url in _import_cache:
        entry = _import_cache[url]
        age = now - entry["ts"]
        if age < _IMPORT_CACHE_TTL:
            cached = dict(entry["data"])  # shallow copy
            cached["cache"] = {"status": "ok", "hit": True, "fresh": True, "age_seconds": int(age)}
            # Fix action: if this URL is already in the DB, it's an update, not a create.
            if Recipe.query.filter_by(source_url=url).first():
                cached["action"] = "updated"
            return jsonify(cached)
        else:
            del _import_cache[url]

    # ---- Scrape (primary: recipe-scrapers) ----
    try:
        from recipe_scrapers import scrape_me
    except ImportError:
        return jsonify({
            "error": "Recipe import requires the recipe-scrapers library. Install with: pip install recipe-scrapers",
            "cache": {"status": "error", "hit": False}
        }), 501

    scraper_error = None
    try:
        scraper = scrape_me(url)
    except Exception as exc:
        scraper_error = exc

    if scraper_error is not None:
        # ---- Fallback: curl_cffi + JSON-LD for Cloudflare-blocked sites ----
        fallback = _scrape_with_curl_cffi(url)
        if fallback:
            title = fallback['title']
            total_time = fallback['total_time']
            yields_str = fallback['yields_str']
            instructions = fallback['instructions']
            image_url = fallback['image_url']
            raw_ingredients = fallback['raw_ingredients']
        else:
            return jsonify({
                "error": "Could not scrape that URL.",
                "detail": f"{type(scraper_error).__name__}: {scraper_error}",
                "url": url,
                "cache": {"status": "error", "hit": False, "error_detail": str(scraper_error)[:200]}
            }), 500
    else:
        # recipe-scrapers succeeded — extract normally
        try:
            title = scraper.title()
        except Exception:
            title = "Imported Recipe"

        try:
            total_time = scraper.total_time()
        except Exception:
            total_time = None

        try:
            yields_str = scraper.yields()
        except Exception:
            yields_str = "4 servings"

        try:
            instructions = scraper.instructions()
        except Exception:
            instructions = ""

        try:
            image_url = scraper.image() if hasattr(scraper, 'image') else None
        except Exception:
            image_url = None

        try:
            raw_ingredients = scraper.ingredients()
        except Exception:
            raw_ingredients = []

    # ---- Shared: parse servings ----
    servings = _parse_recipe_yields(yields_str)

    # ---- Persist to database (upsert by source_url) ----
    existing = Recipe.query.filter_by(source_url=url).first()
    action = "created"

    if existing:
        # Update the existing recipe with fresh scraped data.
        existing.title = title
        existing.servings = servings
        existing.instructions = instructions
        # Replace old ingredients with new ones.
        RecipeIngredient.query.filter_by(recipe_id=existing.id).delete()
        recipe = existing
        action = "updated"
    else:
        recipe = Recipe(
            title=title,
            servings=servings,
            estimated_cost_per_serving=3.50,
            source_url=url,
            instructions=instructions
        )
        db.session.add(recipe)
        db.session.flush()  # get recipe.id

    for ing_str in raw_ingredients:
        ing_str = ing_str.strip()
        if not ing_str:
            continue
        kw = _derive_clean_keyword(ing_str)
        ri = RecipeIngredient(
            recipe_id=recipe.id,
            product_name=ing_str,
            clean_keyword=kw,
            quantity=1,
            unit="item"
        )
        db.session.add(ri)

    db.session.commit()

    # ---- Build response ----
    result = {
        "recipe": {
            "id": recipe.id,
            "title": title,
            "servings": yields_str,
            "total_time": total_time,
            "source_url": url,
            "image_url": image_url,
            "instructions": instructions
        },
        "action": action,
    }

    # Store in cache — include action so cache hits reflect it too.
    _import_cache[url] = {"ts": now, "data": dict(result)}

    result["cache"] = {"status": "ok", "hit": False, "age_seconds": 0}
    return jsonify(result)

@app.route("/api/recipes/generate", methods=["POST"])
def generate_recipes():
    """Generate a meal plan from selected recipe IDs and return a cart.

    Also serves the Grocery tab's Active Recipes expander — recipe
    objects include full ingredient lists so the frontend can render
    per-recipe ingredient bullets without an extra round-trip.
    """
    data = request.json or {}
    recipe_ids = data.get("recipe_ids", [])
    if not recipe_ids or not isinstance(recipe_ids, list):
        return jsonify({"error": "Provide recipe_ids (list of int)"}), 400
    recipes = Recipe.query.filter(Recipe.id.in_(recipe_ids)).all()
    return jsonify({
        "recipes": [{
            "id": r.id,
            "title": r.title,
            "servings": r.servings,
            "source_url": r.source_url or "",
            "estimated_cost_per_serving": r.estimated_cost_per_serving,
            "ingredients": [{
                "id": i.id,
                "product_name": i.product_name,
                "clean_keyword": i.clean_keyword,
                "quantity": i.quantity,
                "unit": i.unit
            } for i in r.ingredients]
        } for r in recipes],
        "total_meals": len(recipes)
    })

# ----- TRANSACTIONS CRUD -----------------------------------------------------

@app.route("/api/transactions", methods=["GET", "POST"])
def transactions_crud():
    if request.method == "POST":
        data = request.json or {}
        desc = data.get("description", "").strip()
        if not desc:
            return jsonify({"error": "Description required"}), 400
        amount = float(data.get("amount", 0))
        category = data.get("category", "discretionary")
        t = ExpenseTransaction(description=desc, amount=amount, category=category)
        db.session.add(t)
        # Also decrement checking balance
        account = Account.query.first()
        if account:
            account.checking_balance -= amount
        db.session.commit()
        return jsonify({"message": "Expense logged", "id": t.id, "new_balance": account.checking_balance if account else None})
    # GET list
    txns = ExpenseTransaction.query.order_by(ExpenseTransaction.date.desc()).all()
    return jsonify([{
        "id": t.id,
        "description": t.description,
        "amount": t.amount,
        "category": t.category,
        "date": t.date.strftime("%Y-%m-%d %H:%M") if t.date else ""
    } for t in txns])

@app.route("/transactions/<int:txn_id>", methods=["DELETE"])
def delete_transaction(txn_id):
    t = ExpenseTransaction.query.get(txn_id)
    if not t:
        return jsonify({"error": "Transaction not found"}), 404
    db.session.delete(t)
    db.session.commit()
    return jsonify({"message": f"Transaction {txn_id} deleted"})

# ----- BILLS CRUD ------------------------------------------------------------

@app.route("/bills", methods=["GET", "POST"])
def bills_crud():
    if request.method == "POST":
        data = request.json or {}
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        amount = float(data.get("amount", 0))
        due_date_str = data.get("due_date", "")
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
            except ValueError:
                due_date = datetime.utcnow() + timedelta(days=7)
        else:
            due_date = datetime.utcnow() + timedelta(days=7)
        b = Bill(name=name, amount=amount, due_date=due_date)
        db.session.add(b)
        db.session.commit()
        return jsonify({"message": "Bill added", "id": b.id})
    # GET list
    bills = Bill.query.order_by(Bill.due_date.asc()).all()
    return jsonify([{
        "id": b.id,
        "name": b.name,
        "amount": b.amount,
        "due_date": b.due_date.strftime("%Y-%m-%d") if b.due_date else "",
        "is_paid": b.is_paid
    } for b in bills])

@app.route("/bills/<int:bid>/pay", methods=["POST"])
def toggle_bill(bid):
    b = Bill.query.get(bid)
    if not b:
        return jsonify({"error": "Bill not found"}), 404
    b.is_paid = not b.is_paid
    db.session.commit()
    return jsonify({"message": f"Bill {bid} toggled", "is_paid": b.is_paid})

@app.route("/bills/<int:bid>", methods=["DELETE"])
def delete_bill(bid):
    b = Bill.query.get(bid)
    if not b:
        return jsonify({"error": "Bill not found"}), 404
    db.session.delete(b)
    db.session.commit()
    return jsonify({"message": f"Bill {bid} deleted"})

# ----- ACCOUNT UPDATE (balance + ratios) ------------------------------------

@app.route("/api/account/update", methods=["POST"])
def update_account():
    account = Account.query.first()
    if not account:
        return jsonify({"error": "Account not found"}), 404
    data = request.json or {}
    if "checking_balance" in data:
        account.checking_balance = float(data["checking_balance"])
    if "food_allocation_pct" in data:
        account.food_allocation_pct = float(data["food_allocation_pct"])
    if "pay_period_days" in data:
        account.pay_period_days = int(data["pay_period_days"])
    if "meals_per_day" in data:
        account.meals_per_day = int(data["meals_per_day"])
    if "expected_paycheck" in data:
        account.expected_paycheck = float(data["expected_paycheck"])
    db.session.commit()
    return jsonify({"message": "Account updated", "checking_balance": round(account.checking_balance, 2)})

# ----- GROCERY LIST (GET/DELETE) --------------------------------------------

@app.route("/api/grocery", methods=["GET", "POST"])
def grocery_list():
    if request.method == "POST":
        data = request.json or {}
        recipe_ids = data.get("recipe_ids", [])
        store_name = data.get("store_name", "")
        # Build cart from recipes
        recipes = Recipe.query.filter(Recipe.id.in_(recipe_ids)).all() if recipe_ids else []
        # Clear old grocery items for this session
        GroceryItem.query.delete()
        items = []
        for r in recipes:
            for ing in r.ingredients:
                gi = GroceryItem(
                    item_name=ing.product_name,
                    estimated_price=round(2.00 + (ing.quantity * 0.15), 2),
                    store_name=store_name or "Local Store",
                    location_context=""
                )
                db.session.add(gi)
                items.append(gi)
        db.session.commit()
        return jsonify({"message": f"Grocery list generated with {len(items)} items"})
    # GET list
    items = GroceryItem.query.all()
    return jsonify({
        "items": [{
            "id": i.id,
            "item_name": i.item_name,
            "estimated_price": i.estimated_price,
            "store_name": i.store_name,
            "location_context": i.location_context,
            "is_purchased": i.is_purchased,
            "quantity": 1
        } for i in items]
    })

@app.route("/api/grocery/<int:gid>", methods=["DELETE"])
def delete_grocery_item(gid):
    gi = GroceryItem.query.get(gid)
    if not gi:
        return jsonify({"error": "Item not found"}), 404
    db.session.delete(gi)
    db.session.commit()
    return jsonify({"message": f"Grocery item {gid} deleted"})

@app.route("/api/budget/summary", methods=["GET"])
def get_budget_summary():
    account = Account.query.first()
    if not account:
        return jsonify({"error": "Account settings missing"}), 400
    return jsonify(compute_liquidity_metrics(account))

@app.route("/api/decision/can-i-buy", methods=["POST"])
def can_i_buy():
    """Tab 1: Decision Engine Evaluator."""
    account = Account.query.first()
    data = request.json or {}
    item_name = data.get("item_name", "Requested Item")
    item_cost = float(data.get("cost", 0.0))
    
    metrics = compute_liquidity_metrics(account)
    free_cash = metrics["free_cash_remaining"]
    
    approved = item_cost <= free_cash
    margin_after = free_cash - item_cost
    
    return jsonify({
        "item_name": item_name,
        "item_cost": item_cost,
        "approved": approved,
        "unallocated_free_cash": free_cash,
        "remaining_buffer_after_purchase": round(margin_after, 2),
        "message": "Purchase Approved!" if approved else f"Purchase Denied. Exceeds safe unallocated cash by ${abs(margin_after):.2f}"
    })

@app.route("/api/recipes/search", methods=["GET"])
def search_recipes():
    """
    Keyword recipe search with TheMealDB API fallback.
    
    Query params:
      q  — search keyword (e.g. ?q=chicken). If omitted, returns all local recipes.
    
    Returns a flat JSON array of unified recipe objects, each with a
    `source` field: "local" or "themealdb".
    """
    query = request.args.get("q", "").strip()

    # --- 1. Local SQLite search ---
    local_recipes = []
    if query:
        like_pat = f"%{query}%"
        # Single search across title, instructions, and ingredient fields.
        # Uses OUTER JOIN so recipes with no ingredients still match on
        # title or instructions. DISTINCT prevents duplicates from the join.
        matches = (
            db.session.query(Recipe)
            .outerjoin(RecipeIngredient)
            .filter(
                db.or_(
                    Recipe.title.ilike(like_pat),
                    Recipe.instructions.ilike(like_pat),
                    RecipeIngredient.product_name.ilike(like_pat),
                    RecipeIngredient.clean_keyword.ilike(like_pat),
                )
            )
            .distinct()
            .all()
        )
        for r in matches:
            local_recipes.append({
                "id": str(r.id),
                "title": r.title,
                "description": (r.instructions or "")[:200],
                "servings": r.servings,
                "source": "local",
            })
    else:
        # No query — return all local recipes (backward-compatible)
        for r in Recipe.query.all():
            local_recipes.append({
                "id": str(r.id),
                "title": r.title,
                "description": (r.instructions or "")[:200],
                "servings": r.servings,
                "source": "local",
            })

    # --- 2. TheMealDB API supplement (only when q is provided) ---
    # Always fetch TheMealDB to supplement local results, then merge
    # with dedup so every matching local recipe plus external recipes
    # are returned together — no arbitrary cap.
    if not query:
        return jsonify(local_recipes)

    try:
        import urllib.request
        import json as py_json

        url = f"https://www.themealdb.com/api/json/v1/1/search.php?s={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Rung/1.0 (finance-assistant)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
            data = py_json.loads(body)

        meals = data.get("meals") or []

        # Build a set of normalized local titles for dedup.
        # All local recipes are included first; TheMealDB entries are
        # appended only when no local recipe with the same title exists.
        local_titles = {" ".join(r["title"].lower().split()) for r in local_recipes}

        for meal in meals:
            meal_title = meal.get("strMeal", "Unknown").strip()
            meal_key = " ".join(meal_title.lower().split())
            if meal_key in local_titles:
                continue
            instructions = meal.get("strInstructions") or ""
            desc = instructions[:200] if instructions else ""
            local_recipes.append({
                "id": f"themealdb_{meal.get('idMeal', '0')}",
                "title": meal_title,
                "description": desc,
                "source": "themealdb",
                "image_url": meal.get("strMealThumb", ""),
                "category": meal.get("strCategory", ""),
                "area": meal.get("strArea", ""),
            })

        return jsonify(local_recipes)

    except Exception:
        # Graceful offline fallback — return whatever local results we have
        if local_recipes:
            return jsonify(local_recipes)
        return jsonify({"error": "Could not reach TheMealDB. Try again later.", "results": []}), 503

@app.route("/api/grocery/generate-pay-period-plan", methods=["POST"])
def generate_pay_period_plan():
    """
    Live grocery resolver — resolves every ingredient keyword via
    ``services.store_api.resolve_terms()`` (cache-first, Kroger API
    fallback), then builds and validates a pay-period cart within the
    user's food budget.

    Request body
    ------------
    recipe_ids : list[int]
        IDs of the recipes to generate a plan for.
    store_name : str, optional
        Override store name (default "Kroger").
    location_id : str, optional
        Kroger location ID for live API queries. If omitted, resolution
        uses whatever is already in StorePriceCache (no API calls).
    force_refresh : bool, optional
        If True, skip cache and hit the API for every term.
    budget_limit : float, optional
        Override the food budget cap. Defaults to account's computed
        food_budget from ``compute_liquidity_metrics()``.

    Returns
    -------
    JSON with the same shape as the cart_items block (subtotal, tax,
    total_cart_cost, etc.) plus:
      resolution_stats {cache_hits, api_hits, fallbacks, total_terms}
      budget {food_budget, budget_exceeded, budget_remaining}
      recipes_used [{id, title}]
    """
    account = Account.query.first()
    data = request.json or {}
    recipe_ids = data.get("recipe_ids", [])
    
    # Use the account's auto-detected store settings as defaults;
    # request body can still override per-call if needed.
    store_name = data.get("store_name", account.kroger_store_name or "Kroger")
    location_id = data.get("location_id", account.kroger_location_id or "")
    force_refresh = bool(data.get("force_refresh", False))
    budget_limit = data.get("budget_limit", None)

    if not recipe_ids or not isinstance(recipe_ids, list):
        return jsonify({"error": "Provide recipe_ids (list of int)"}), 400

    recipes = Recipe.query.filter(Recipe.id.in_(recipe_ids)).all()
    if not recipes:
        return jsonify({"error": "No matching recipes found"}), 404

    # --- Step 1: Aggregate ingredient keywords ---
    required_ingredients: dict = {}
    for r in recipes:
        for ing in r.ingredients:
            kw = ing.clean_keyword.lower()
            qty_std = normalize_to_standard_unit(ing.quantity, ing.unit)
            required_ingredients[kw] = required_ingredients.get(kw, 0.0) + qty_std

    unique_terms = list(required_ingredients.keys())

    # --- Step 2: Live API resolution (cache-first, Kroger fallback) ---
    try:
        resolved = resolve_terms(
            app,
            unique_terms,
            store_name=store_name,
            location_id=location_id if location_id else None,
            limit=5,
            force_refresh=force_refresh,
        )
    except Exception:
        # If the resolver itself crashes (e.g. import error), fall back
        # to an empty resolution dict — the loop below will use estimates.
        resolved = {}

    # --- Step 2b: RapidAPI supplement for terms with no Kroger results ---
    rapid_hits = 0
    for kw in unique_terms:
        existing = resolved.get(kw, [])
        if existing:
            continue  # already have Kroger products — keep them

        try:
            rapid_result = search_local_product(kw, store_name=store_name, location="", app=app)
            if rapid_result:
                product_dict = rapid_result_to_product_dict(rapid_result)
                resolved[kw] = [product_dict]
                rapid_hits += 1
        except Exception:
            pass  # Silently fall through — the loop below will use estimates

    # --- Step 3: Compute resolution stats ---
    cache_hits = 0
    api_hits = 0
    fallbacks = 0  # counted in Step 4 when estimate fallback is actually used
    for kw, products in resolved.items():
        for p in products:
            if p.get("source") == "cache":
                cache_hits += 1
            elif p.get("source") == "api":
                api_hits += 1
            elif p.get("source") in ("rapid_api", "rapid_cache"):
                pass  # Already counted in rapid_hits

    # --- Step 4: Pantry deduction + product selection ---
    pantry_stock = {p.clean_keyword.lower(): p for p in PantryItem.query.all()}
    prefs = {b.clean_keyword.lower(): b for b in BrandPreference.query.all()}

    cart_items = []
    subtotal = 0.0
    pantry_items_used = 0

    for kw, req_qty in required_ingredients.items():
        on_hand_qty = 0.0
        if kw in pantry_stock:
            on_hand_qty = normalize_to_standard_unit(pantry_stock[kw].quantity, pantry_stock[kw].unit)

        if on_hand_qty >= req_qty:
            pantry_items_used += 1
            continue

        net_needed = req_qty - on_hand_qty
        pref = prefs.get(kw)
        use_store_brand = pref.prefer_store_brand if pref else True

        products = resolved.get(kw, [])
        best = pick_best(products, prefer_store_brand=use_store_brand) if products else None

        if best:
            unit_price = round(best["price"], 2)
            product_label = best["product_title"]
            price_source = best.get("source", "cache")
            store = store_name
        else:
            # Graceful fallback — estimate
            fallbacks += 1
            unit_price = round(2.00 + (net_needed * 0.15), 2)
            readable_name = kw.replace("_", " ").title()
            product_label = f"{readable_name} (estimate)"
            price_source = "estimated"
            store = store_name

        subtotal += unit_price
        pkg = best.get("package_size", "") if best else ""
        img = best.get("image_url", "") if best else ""
        cart_items.append({
            "keyword": kw,
            "product_label": product_label,
            "net_quantity_needed_oz": round(net_needed, 2),
            "estimated_price": unit_price,
            "price_source": price_source,
            "store_name": store,
            "package_size": pkg,
            "image_url": img,
        })

    # --- Step 5: Tax + budget enforcement ---
    tax_rate = account.grocery_tax_rate or account.sales_tax_rate or 0.0
    tax_amount = subtotal * tax_rate
    total_cart_cost = subtotal + tax_amount

    # Determine the food budget
    if budget_limit is not None:
        food_budget = float(budget_limit)
    else:
        metrics = compute_liquidity_metrics(account)
        food_budget = metrics["food_budget"]

    budget_exceeded = total_cart_cost > food_budget
    budget_remaining = round(food_budget - total_cart_cost, 2)

    return jsonify({
        "pantry_items_skipped": pantry_items_used,
        "cart_items": cart_items,
        "subtotal": round(subtotal, 2),
        "grocery_tax_rate": round(tax_rate * 100, 2),
        "tax_amount": round(tax_amount, 2),
        "total_cart_cost": round(total_cart_cost, 2),
        "resolution_stats": {
            "cache_hits": cache_hits,
            "api_hits": api_hits,
            "rapid_hits": rapid_hits,
            "fallbacks": fallbacks,
            "total_terms": len(unique_terms),
        },
        "budget": {
            "food_budget": round(food_budget, 2),
            "budget_exceeded": budget_exceeded,
            "budget_remaining": budget_remaining,
        },
        "recipes_used": [{"id": r.id, "title": r.title} for r in recipes],
    })

@app.route("/api/rapid-price/search", methods=["GET"])
def rapid_price_search():
    """
    On-demand product search via RapidAPI Real-Time Product Search.

    Query params:
      q (required)  — Ingredient keyword (e.g. "cilantro", "chicken breast")
      store_name    — Optional store name to narrow results (e.g. "Walmart")

    Returns
    -------
    JSON with ``found`` and ``product`` keys:

    .. code-block:: json

        {"found": true, "product": {"title": "...", "price": 1.99, ...}}
        {"found": false, "product": null}
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing required query param 'q'"}), 400

    store_name = request.args.get("store_name", "").strip() or None

    try:
        result = search_local_product(query, store_name=store_name, location="", app=app)
    except Exception as exc:
        return jsonify({"error": f"Search failed: {exc}", "found": False, "product": None}), 502

    if result:
        return jsonify({"found": True, "product": result})
    else:
        return jsonify({"found": False, "product": None})


@app.route("/api/pantry", methods=["GET", "POST"])
def manage_pantry():
    """Tab 4: Inventory Management & Stock Updates."""
    if request.method == "POST":
        data = request.json or {}
        kw = data.get("clean_keyword", "").strip().lower()
        p_name = data.get("product_name", kw.title())
        qty = float(data.get("quantity", 0.0))
        unit = data.get("unit", "oz")
        
        item = PantryItem.query.filter_by(clean_keyword=kw).first()
        if item:
            item.quantity += qty
        else:
            item = PantryItem(clean_keyword=kw, product_name=p_name, quantity=qty, unit=unit)
            db.session.add(item)
            
        db.session.commit()
        return jsonify({"message": "Pantry item updated successfully"})
        
    items = PantryItem.query.all()
    return jsonify([{
        "id": i.id,
        "keyword": i.clean_keyword,
        "product_name": i.product_name,
        "quantity": i.quantity,
        "unit": i.unit
    } for i in items])

@app.route("/api/pantry/cook", methods=["POST"])
def cook_recipe():
    """Tab 4: Automatically depletes pantry stock when a meal is cooked."""
    data = request.json or {}
    recipe_id = data.get("recipe_id")
    recipe = Recipe.query.get(recipe_id)
    if not recipe:
        return jsonify({"error": "Recipe not found"}), 404
        
    for ing in recipe.ingredients:
        kw = ing.clean_keyword.lower()
        required_qty = normalize_to_standard_unit(ing.quantity, ing.unit)
        
        item = PantryItem.query.filter_by(clean_keyword=kw).first()
        if item:
            item_on_hand_std = normalize_to_standard_unit(item.quantity, item.unit)
            new_qty_std = max(0.0, item_on_hand_std - required_qty)
            item.quantity = new_qty_std
            
    db.session.commit()
    return jsonify({"message": f"Cooked {recipe.title}! Pantry inventory automatically depleted."})

@app.route("/api/vault/sweep", methods=["POST"])
def sweep_vault():
    """Tab 5: Micro-Savings Sweeper."""
    account = Account.query.first()
    data = request.json or {}
    amount = float(data.get("amount", 0.0))
    
    if amount <= 0 or amount > account.checking_balance:
        return jsonify({"error": "Invalid sweep amount"}), 400
        
    account.checking_balance -= amount
    account.vault_balance += amount
    db.session.commit()
    
    return jsonify({
        "new_checking_balance": round(account.checking_balance, 2),
        "new_vault_balance": round(account.vault_balance, 2)
    })

@app.route("/api/location/update", methods=["POST"])
def update_location():
    """Tab 6: Location & Tax settings engine.

    When the user saves their ZIP code or coordinates, the endpoint
    auto-detects the nearest Kroger / Gerbes store via the Kroger
    Locations API and saves the location ID to the account.
    """
    account = Account.query.first()
    if not account:
        return jsonify({"error": "Account not found"}), 404

    data = request.json or {}
    
    zip_code = str(data.get("zip_code", account.zip_code or ""))
    latitude = data.get("latitude", account.latitude)
    longitude = data.get("longitude", account.longitude)
    
    account.zip_code = zip_code
    if latitude is not None:
        account.latitude = float(latitude)
    if longitude is not None:
        account.longitude = float(longitude)
    account.sales_tax_rate = float(data.get("sales_tax_rate", account.sales_tax_rate))
    account.grocery_tax_rate = float(data.get("grocery_tax_rate", account.grocery_tax_rate))
    
    store_found = False
    
    # Auto-detect nearest Kroger store from location (only if we have a ZIP or coords)
    has_location = bool(zip_code.strip()) or (
        account.latitude is not None and account.longitude is not None
    )
    if has_location:
        try:
            nearest = find_nearest_kroger(
                zip_code=zip_code or "",
                latitude=account.latitude,
                longitude=account.longitude,
            )
            if nearest:
                account.kroger_location_id = nearest["location_id"]
                account.kroger_store_name = nearest["chain_display"]
                store_found = True
        except Exception:
            # If Kroger API is unavailable, just keep whatever was stored before
            pass
    
    db.session.commit()
    
    return jsonify({
        "message": "Location and tax rates updated successfully",
        "zip": account.zip_code,
        "store": {
            "found": store_found,
            "name": account.kroger_store_name,
            "location_id": account.kroger_location_id,
        },
    })

# ----- STORE PRICE CACHE CSV UPLOAD -----------------------------------------

@app.route("/api/store-cache/upload-csv", methods=["POST"])
def upload_store_cache_csv():
    """Bulk-upsert store prices from a CSV payload."""
    import csv
    import io

    data = request.json or {}
    csv_text = data.get("csv", "").strip()
    if not csv_text:
        return jsonify({"error": "Missing 'csv' field in JSON body"}), 400

    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"store_name", "item_keyword", "product_title", "price"}
    if reader.fieldnames is None or not required.issubset(set(c for c in reader.fieldnames if c)):
        return jsonify({"error": "CSV must include columns: store_name,item_keyword,product_title,price"}), 400

    inserted = 0
    updated = 0
    skipped = 0
    errors = []

    for row_num, row in enumerate(reader, start=2):
        store = (row.get("store_name") or "").strip()
        keyword = (row.get("item_keyword") or "").strip()
        title = (row.get("product_title") or "").strip()
        price_raw = (row.get("price") or "").strip()
        pkg = (row.get("package_size") or "").strip()
        is_sb = (row.get("is_store_brand") or "0").strip()
        retailer = (row.get("retailer") or "").strip()

        if not store:
            skipped += 1
            errors.append(f"Row {row_num}: missing store_name")
            continue
        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            skipped += 1
            errors.append(f"Row {row_num}: invalid price '{price_raw}'")
            continue
        if price <= 0:
            skipped += 1
            errors.append(f"Row {row_num}: zero or negative price")
            continue

        existing = StorePriceCache.query.filter_by(
            store_name=store, product_title=title
        ).first()
        if existing:
            existing.price = price
            existing.package_size = pkg or existing.package_size
            existing.is_store_brand = int(is_sb) if is_sb else existing.is_store_brand
            existing.retailer = retailer or existing.retailer
            updated += 1
        else:
            db.session.add(StorePriceCache(
                store_name=store, item_keyword=keyword, product_title=title,
                price=price, package_size=pkg, retailer=retailer,
                is_store_brand=int(is_sb) if is_sb else 0,
            ))
            inserted += 1

    db.session.commit()
    return jsonify({
        "inserted": inserted, "updated": updated,
        "skipped": skipped, "errors": errors,
    })

# =============================================================================
# INITIALIZE DATABASE & SEED DEMO DATA
# =============================================================================
def init_db():
    with app.app_context():
        db.create_all()
        
        # Migrate existing database: add new columns if missing
        try:
            from sqlalchemy import inspect as sa_inspect
            inspector = sa_inspect(db.engine)
            account_cols = {c["name"] for c in inspector.get_columns("account")}
            if "kroger_location_id" not in account_cols:
                db.session.execute(db.text("ALTER TABLE account ADD COLUMN kroger_location_id VARCHAR(20)"))
            if "kroger_store_name" not in account_cols:
                db.session.execute(db.text("ALTER TABLE account ADD COLUMN kroger_store_name VARCHAR(100) DEFAULT 'Kroger'"))
            recipe_cols = {c["name"] for c in inspector.get_columns("recipe")}
            if "source_url" not in recipe_cols:
                db.session.execute(db.text("ALTER TABLE recipe ADD COLUMN source_url VARCHAR(500)"))
            rapid_cols = {c["name"] for c in inspector.get_columns("rapid_price_cache")}
            if "package_size" not in rapid_cols:
                db.session.execute(db.text("ALTER TABLE rapid_price_cache ADD COLUMN package_size VARCHAR(100)"))
            if "image_url" not in rapid_cols:
                db.session.execute(db.text("ALTER TABLE rapid_price_cache ADD COLUMN image_url VARCHAR(500)"))
            db.session.commit()
        except Exception:
            pass  # Fresh database — columns already exist
        

        
        if not Account.query.first():
            acc = Account(checking_balance=1250.00, food_allocation_pct=40.0, pay_period_days=14, meals_per_day=3)
            db.session.add(acc)
            
            b1 = Bill(name="Electric Bill", amount=120.00, due_date=datetime.utcnow() + timedelta(days=5))
            b2 = Bill(name="Internet", amount=65.00, due_date=datetime.utcnow() + timedelta(days=8))
            b3 = Bill(name="Gas Allocation", amount=55.00, due_date=datetime.utcnow() + timedelta(days=2), is_gas_estimate=True)
            db.session.add_all([b1, b2, b3])
            
            p1 = PantryItem(clean_keyword="flour", product_name="All Purpose Flour", quantity=32.0, unit="oz")
            p2 = PantryItem(clean_keyword="chicken", product_name="Chicken Breasts", quantity=16.0, unit="oz")
            db.session.add_all([p1, p2])
            
            r1 = Recipe(title="Chicken Rice Bowl", servings=2, estimated_cost_per_serving=3.20, instructions="Cook chicken and serve over rice.")
            db.session.add(r1)
            db.session.flush()
            
            ri1 = RecipeIngredient(recipe_id=r1.id, product_name="Chicken Breast", clean_keyword="chicken", quantity=24.0, unit="oz")
            ri2 = RecipeIngredient(recipe_id=r1.id, product_name="White Rice", clean_keyword="rice", quantity=8.0, unit="oz")
            db.session.add_all([ri1, ri2])
            
            db.session.commit()

if __name__ == "__main__":
    init_db()
    app.run(port=5000, debug=True)
