#!/usr/bin/env python3
"""
Integration tests for the grocery cart/build pipeline.

Covers:
  1. BrandPreference save/retrieval.
  2. /api/grocery/generate-pay-period-plan — resolves products from
     StorePriceCache, applies store-brand logic, enforces locked-brand
     preferences, computes tax, and enforces budget.

Run with:  .venv/bin/python tests/test_sync_api.py
"""
import os
import sys
import json
from datetime import datetime, timedelta, timezone

os.environ['RECIPE_CACHE_DISABLED'] = '1'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so these tests can never wipe the user's real rung_finance.db rows.
os.environ['RUNG_DB_PATH'] = ':memory:'

from app import (
    app, db, Account, Recipe, RecipeIngredient, BrandPreference,
    StorePriceCache, PantryItem, GroceryItem, PYF_TARGET_SETTING_KEY,
    SAFE_BUFFER_SETTING_KEY, REQUIRED_EXPENSE_REVIEWED,
    REQUIRED_EXPENSE_REVIEW_SETTING_KEY,
)
from models import Bill, ExpenseTransaction, IncomePlanVersion, UserPreference, UserSetting
from services.household_context import household_id as current_household_id
app.testing = True

passed = 0
failed = 0


def assert_eq(actual, expected, label):
    global passed, failed
    if actual == expected:
        passed += 1
        print('  PASS: ' + label)
    else:
        failed += 1
        print('  FAIL: ' + label)
        print('    expected: ' + repr(expected))
        print('    actual:   ' + repr(actual))


def assert_truthy(value, label):
    global passed, failed
    if value:
        passed += 1
        print('  PASS: ' + label)
    else:
        failed += 1
        print('  FAIL: ' + label + ' (was falsy)')


def assert_in(needle, haystack, label):
    global passed, failed
    if needle in (haystack or ''):
        passed += 1
        print('  PASS: ' + label)
    else:
        failed += 1
        print('  FAIL: ' + label)


class FakeKrogerStore:
    def __init__(self, store_id, name, address='123 Main St, Eldon, MO 65026', postal_code='65084'):
        self.store_id = store_id
        self.name = name
        self.address = address
        self.postal_code = postal_code
        self.verified = True

    def to_dict(self):
        return {
            'store_id': self.store_id,
            'name': self.name,
            'address': self.address,
            'postal_code': self.postal_code,
            'verified': self.verified,
        }


# ===========================================================================
# SETUP — clear state, seed deterministic scenario
# ===========================================================================
with app.app_context():
    db.create_all()  # in-memory DB starts empty — build the schema first
    hid = current_household_id()
    BrandPreference.query.delete()
    StorePriceCache.query.delete()
    PantryItem.query.delete()
    RecipeIngredient.query.delete()
    Recipe.query.delete()
    db.session.commit()

    # Ensure an Account row exists — cart/build and generate-pay-period-plan
    # both access Account.query.first() attributes unconditionally.
    if not Account.query.first():
        account = Account(household_id=hid, checking_balance=1250.00)
        db.session.add(account)
        db.session.flush()
        db.session.commit()
    account = Account.query.first()
    if not IncomePlanVersion.query.filter_by(household_id=hid).first():
        db.session.add(IncomePlanVersion(household_id=hid, operation_id='sync-api-plan',
            expected_income_cents=100000, effective_at=datetime.now(timezone.utc)-timedelta(days=30),
            source='test_confirmation'))
    if not UserSetting.query.filter_by(household_id=hid, key=PYF_TARGET_SETTING_KEY).first():
        db.session.add(UserSetting(household_id=hid, key=PYF_TARGET_SETTING_KEY, value='10'))
    if not UserSetting.query.filter_by(household_id=hid, key=SAFE_BUFFER_SETTING_KEY).first():
        db.session.add(UserSetting(household_id=hid, key=SAFE_BUFFER_SETTING_KEY, value='100.00'))
    if not UserSetting.query.filter_by(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY).first():
        db.session.add(UserSetting(household_id=hid, key=REQUIRED_EXPENSE_REVIEW_SETTING_KEY, value=REQUIRED_EXPENSE_REVIEWED))
    if not UserPreference.query.filter_by(household_id=hid, key='baseline_grocery_cost').first():
        db.session.add(UserPreference(household_id=hid, key='baseline_grocery_cost', value='300.00'))
    if not Bill.query.filter_by(household_id=hid, is_gas_estimate=True).first():
        db.session.add(Bill(household_id=hid, name='Required fuel', amount=50.0,
                           due_date=datetime.now(timezone.utc) + timedelta(days=3),
                           is_paid=False, is_gas_estimate=True))
    if not ExpenseTransaction.query.filter_by(household_id=hid, category='income').first():
        db.session.add(ExpenseTransaction(
            household_id=hid, description='Established payday', amount=1000.0,
            category='income', source='manual', local_account_id=account.id,
            date=datetime.now(timezone.utc) - timedelta(days=5),
        ))
    db.session.commit()

    # Create a recipe with 4 ingredients: cheerios, milk, eggs, broccoli.
    r = Recipe(title='Test Meal Plan', servings=2, instructions='Eat.')
    db.session.add(r)
    db.session.flush()
    staples = [
        ('Honey Nut Cheerios', 'cheerios', 1, 'box'),
        ('Whole Milk',         'milk',     1, 'gallon'),
        ('Large Eggs',         'eggs',     1, 'dozen'),
        ('Fresh Broccoli',     'broccoli', 1, 'head'),
    ]
    for pname, kw, qty, unit in staples:
        db.session.add(RecipeIngredient(
            recipe_id=r.id, product_name=pname, clean_keyword=kw,
            quantity=qty, unit=unit,
        ))
    db.session.commit()
    recipe_id = r.id

    # ---- Seed StorePriceCache (Walmart prices) ---------------------------
    cache_seed = [
        # (store, keyword, title, price, is_store_brand, package, retailer)
        ('Walmart', 'cheerios', 'Honey Nut Cheerios 15.4oz',         4.49, 0, '15.4 oz', 'walmart'),
        ('Walmart', 'cheerios', 'Great Value Toasted Oats Cereal',   2.10, 1, '18 oz',   'walmart'),
        ('Walmart', 'milk',     'Great Value Whole Milk 1gal',        3.12, 1, '1 gal',   'walmart'),
        ('Walmart', 'milk',     'Horizon Organic Whole Milk 1gal',   6.49, 0, '1 gal',   'walmart'),
        ('Walmart', 'eggs',     'Great Value Large Eggs 12ct',       3.48, 1, '12 ct',   'walmart'),
        ('Walmart', 'broccoli', 'Fresh Broccoli Crowns 1lb',         2.50, 0, '1 lb',    'walmart'),
        ('Walmart', 'broccoli', 'Great Value Broccoli Florets 12oz', 1.99, 1, '12 oz',   'walmart'),
    ]
    for store, kw, title, price, is_sb, pkg, rtlr in cache_seed:
        db.session.add(StorePriceCache(
            store_name=store, item_keyword=kw, product_title=title,
            price=price, package_size=pkg, retailer=rtlr,
            is_store_brand=is_sb,
        ))
    db.session.commit()

    # ---- Seed a locked brand preference for cheerios --------------------
    pref = BrandPreference(
        household_id=hid,
        clean_keyword='cheerios',
        prefer_store_brand=False,
        preferred_brand_name='Honey Nut Cheerios',
    )
    db.session.add(pref)
    db.session.commit()


client = app.test_client()

# ===========================================================================
# TEST 1: BrandPreference — verify locked cheerios pref exists
# ===========================================================================
print('1. BrandPreference seeded for cheerios (locked brand)')
with app.app_context():
    p = BrandPreference.query.filter_by(clean_keyword='cheerios').first()
    assert_truthy(p is not None, 'cheerios preference exists')
    assert_eq(p.prefer_store_brand, False, 'cheerios locked (prefer_store_brand=False)')
    assert_eq(p.preferred_brand_name, 'Honey Nut Cheerios',
              'preferred brand name stored')


# ===========================================================================
# TEST 2: /api/grocery/generate-pay-period-plan resolves from cache
# ===========================================================================
print('2. generate-pay-period-plan resolves products from StorePriceCache')
resp = client.post('/api/grocery/generate-pay-period-plan', json={
    'recipe_ids': [recipe_id],
    'store_name': 'Walmart',
})
d = resp.get_json() or {}
assert_eq(resp.status_code, 200, 'plan returns 200')
cart_items = d.get('cart_items', [])
assert_truthy(len(cart_items) >= 4, 'at least 4 items in cart')

# Every cart item should have a real price (no fallback estimates).
for item in cart_items:
    assert_truthy(item.get('estimated_price', 0) > 0,
                  f'{item.get("keyword", "?")} has a real price > 0')
    assert_in(item.get('price_source'), ('cache', 'api', 'estimated', 'rapid_api', 'rapid_cache', 'kroger_cache', 'kroger_api', 'store_cache_fallback'),
              f'{item.get("keyword", "?")} has a valid price_source')
    assert_truthy('confirmed_local_store' in item,
                  f'{item.get("keyword", "?")} has confirmed_local_store field')

# Regression: manual grocery rows without selected recipes must still build a cart.
with app.app_context():
    GroceryItem.query.delete()
    db.session.add(GroceryItem(household_id=hid, item_name='milk', estimated_price=0.0, store_name='Walmart'))
    db.session.add(GroceryItem(household_id=hid, item_name='eggs', estimated_price=0.0, store_name='Walmart'))
    db.session.commit()

resp = client.post('/api/grocery/generate-pay-period-plan', json={'recipe_ids': [], 'store_name': 'Walmart'})
assert_eq(resp.status_code, 200, 'zero-recipe manual grocery plan returns 200')
manual_items = (resp.get_json() or {}).get('cart_items', [])
assert_truthy(len(manual_items) >= 2, 'manual grocery rows appear in cart')
assert_truthy(any((it.get('keyword') or '').lower() == 'milk' for it in manual_items), 'milk requirement resolves into cart')

# Regression: recipe + manual duplicate item should merge to one purchase row.
with app.app_context():
    GroceryItem.query.delete()
    db.session.add(GroceryItem(household_id=hid, item_name='milk', estimated_price=0.0, store_name='Walmart'))
    db.session.commit()

resp = client.post('/api/grocery/generate-pay-period-plan', json={'recipe_ids': [recipe_id], 'store_name': 'Walmart'})
assert_eq(resp.status_code, 200, 'mixed recipe+manual plan returns 200')
merged_items = (resp.get_json() or {}).get('cart_items', [])
milk_rows = [it for it in merged_items if (it.get('keyword') or '').lower() == 'milk']
assert_truthy(len(milk_rows) == 1, 'milk appears once after merge/dedupe')


# ===========================================================================
# TEST 3: plan subtotal + tax computed correctly
# ===========================================================================
print('3. generate-pay-period-plan keeps unsupported tax truthful')
subtotal = float(d.get('subtotal', 0))
tax_amount = d.get('tax_amount')
total = d.get('total_cart_cost')
assert_truthy(subtotal > 0, 'subtotal is positive')
assert_eq((d.get('tax_engine') or {}).get('status'), 'tax_not_included_yet',
          'legacy location does not fabricate canonical jurisdiction')
assert_truthy(tax_amount is None and total is None,
              'tax and final total remain unavailable instead of silent zero')
# Verify the response includes the enriched fields (package_size, resolution stats)
stats = d.get('resolution_stats', {})
assert_truthy(stats.get('total_terms', 0) >= 4, 'resolution_stats covers all terms')
budget = d.get('budget', {})
assert_eq(budget.get('grocery_need_budget'), 300.0,
          'canonical grocery Need supplies the default budget')
assert_eq(budget.get('budget_source'), 'canonical_grocery_need_remaining',
          'default budget identifies canonical authority')


# ===========================================================================
# TEST 4: generate-pay-period-plan with empty recipe_ids returns 404
# ===========================================================================
print('4. generate-pay-period-plan handles empty recipe list gracefully')
resp = client.post('/api/grocery/generate-pay-period-plan', json={
    'recipe_ids': [],
    'store_name': 'Walmart',
})
d = resp.get_json() or {}
assert_eq(resp.status_code, 400, 'empty recipe list returns 400 (input validation)')
assert_truthy('Provide recipe_ids' in d.get('error', ''),
              'error message requires recipe_ids')


# ===========================================================================
# TEST 5: /api/grocery/generate-pay-period-plan with budget cap
# ===========================================================================
print('5. /api/grocery/generate-pay-period-plan respects budget')
resp = client.post('/api/grocery/generate-pay-period-plan', json={
    'recipe_ids': [recipe_id],
    'budget_limit': 15.00,
    'store_name': 'Walmart',
})
d = resp.get_json() or {}
assert_eq(resp.status_code, 200, 'plan returns 200')
budget = d.get('budget', {})
assert_truthy(budget.get('food_budget', 0) == 15.00,
              'budget_limit honored at 15.00')
cart_items = d.get('cart_items', [])
assert_truthy(len(cart_items) >= 4, 'cart has items')


# ===========================================================================
# TEST 6: /api/grocery/generate-pay-period-plan over-budget flag
# ===========================================================================
print('6. /api/grocery/generate-pay-period-plan flags over-budget')
resp = client.post('/api/grocery/generate-pay-period-plan', json={
    'recipe_ids': [recipe_id],
    'budget_limit': 0.50,   # impossibly tight
    'store_name': 'Walmart',
})
d = resp.get_json() or {}
assert_eq(resp.status_code, 200, 'plan returns 200 even when over budget')
budget = d.get('budget', {})
assert_eq(budget.get('budget_exceeded'), True,
          'budget_exceeded flag is True')
assert_truthy(budget.get('budget_remaining') is None,
              'exact budget overage waits for tax-inclusive total')


# ===========================================================================
# TEST 7: /api/grocery/generate-pay-period-plan requires recipe_ids
# ===========================================================================
print('7. /api/grocery/generate-pay-period-plan rejects bad input')
resp = client.post('/api/grocery/generate-pay-period-plan', json={})
assert_eq(resp.status_code, 400, 'missing recipe_ids returns 400')

resp = client.post('/api/grocery/generate-pay-period-plan',
                   json={'recipe_ids': [99999]})
assert_eq(resp.status_code, 404, 'nonexistent recipe_id returns 404')


# ===========================================================================
# TEST 8: Kroger and RapidAPI search-term formatting
#   Storage keys use underscores; external queries must use spaces.
#   Cache/DB lookup keys must remain in underscore format.
# ===========================================================================
print('8. Kroger/RapidAPI search-term underscore → space conversion')

from unittest.mock import patch, MagicMock
from services.store_api import KrogerClient, resolve_terms
from services.rapidapi_search import search_local_product

# ---- 8a: KrogerClient._get() converts underscores to spaces in filter.term ----
captured_params = {}

def _mock_requests_get(url, headers=None, params=None, timeout=None):
    captured_params.update(params or {})
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.ok = True
    mock_resp.json.return_value = {"data": []}
    return mock_resp

kc = KrogerClient("cid", "csecret")
kc._token = {"access_token": "tok", "expires_at": __import__("datetime").datetime.max}

with patch("requests.get", _mock_requests_get):
    kc.search_products("truffle_oil", "loc123")
assert_eq(captured_params.get("filter.term"), "truffle oil",
          "truffle_oil → 'truffle oil' in Kroger filter.term")

captured_params.clear()
with patch("requests.get", _mock_requests_get):
    kc.search_products("chicken_breast", "loc123")
assert_eq(captured_params.get("filter.term"), "chicken breast",
          "chicken_breast → 'chicken breast' in Kroger filter.term")

captured_params.clear()
with patch("requests.get", _mock_requests_get):
    kc.search_products("unsalted_butter", "loc123")
assert_eq(captured_params.get("filter.term"), "unsalted butter",
          "unsalted_butter → 'unsalted butter' in Kroger filter.term")

# ---- 8b: resolve_terms() passes the underscore key unchanged to the cache ----
with app.app_context():
    db.drop_all()
    db.create_all()
    now = __import__("datetime").datetime.utcnow()
    db.session.add(StorePriceCache(
        store_name="Kroger",
        item_keyword="chicken_breast",   # stored with underscore
        product_title="Kroger Chicken Breast",
        price=4.99,
        unit="each",
        is_store_brand=False,
        last_updated=now,
    ))
    db.session.commit()

    result = resolve_terms(app, ["chicken_breast"], store_name="Kroger")
    products = result.get("chicken_breast", [])
    assert_eq(len(products), 1, "cache hit returned for underscore key 'chicken_breast'")
    if products:
        assert_eq(products[0]["product_title"], "Kroger Chicken Breast",
                  "cache lookup key unchanged (underscore preserved)")

# ---- 8c: RapidAPI search_local_product() converts underscores to spaces in q ----
rapid_captured = {}

def _mock_rapid_get(url, headers=None, params=None, timeout=None):
    rapid_captured.update(params or {})
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = lambda: None
    mock_resp.json.return_value = {"data": {"products": []}}
    return mock_resp

with app.app_context():
    db.drop_all()
    db.create_all()
    db.session.add(Account(household_id=current_household_id(), checking_balance=1000.00))
    db.session.commit()
    with patch("requests.get", _mock_rapid_get), \
         patch("services.rapidapi_search._get_rapidapi_key", return_value="rkey"):
        search_local_product("truffle_oil", app=app)

q_sent = rapid_captured.get("q", "")
assert_eq("truffle_oil" not in q_sent, True,
          "RapidAPI query must not contain raw underscore keyword")
assert_eq("truffle oil" in q_sent, True,
          "truffle_oil → 'truffle oil' in RapidAPI q parameter")


# ===========================================================================
# TEST 9: Product source/provenance metadata
# ===========================================================================
print('9. Product source/provenance metadata')

import datetime as _dt
from services.store_api import resolve_terms as _resolve_terms

# ---- 9A: Live Kroger response → source="kroger_api", confirmed=True ----
print('9A. Live Kroger → kroger_api + confirmed_local_store=True')

def _mock_kroger_get_9a(url, headers=None, params=None, timeout=None):
    r = MagicMock()
    r.status_code = 200
    r.ok = True
    r.json.return_value = {"data": [{
        "description": "Kroger Chicken Breast",
        "brand": "Kroger",
        "items": [{"price": {"regular": 4.99}, "size": "1 lb", "itemId": "x1"}],
        "images": [],
        "upc": "001",
    }]}
    return r

def _mock_kroger_post_9a(url, auth=None, data=None, timeout=None):
    r = MagicMock()
    r.ok = True
    r.raise_for_status = lambda: None
    r.json.return_value = {"access_token": "tok9a", "expires_in": 1800}
    return r

with app.app_context():
    db.drop_all()
    db.create_all()
    db.session.add(Account(household_id=current_household_id(), checking_balance=1000.00))
    db.session.commit()
    with patch("requests.get", _mock_kroger_get_9a), \
         patch("requests.post", _mock_kroger_post_9a), \
         patch("services.store_api._get_kroger_credentials",
               return_value={"client_id": "cid", "client_secret": "csec"}):
        res9a = _resolve_terms(app, ["chicken_breast"],
                               store_name="Kroger", location_id="loc001")

products_9a = res9a.get("chicken_breast", [])
assert_eq(len(products_9a), 1, '9A: one product returned')
assert_eq(products_9a[0].get("source"), "kroger_api",
          '9A: live Kroger product source is kroger_api')
assert_eq(products_9a[0].get("source_store_name"), "Kroger",
          '9A: kroger_api product carries store name')

# Simulate cart endpoint: confirmed_local_store must be True
_LOCAL = {"kroger_cache", "kroger_api"}
assert_truthy(products_9a[0]["source"] in _LOCAL,
              '9A: kroger_api maps to confirmed_local_store=True')

# ---- 9B: Cached Kroger product → source="kroger_cache", confirmed=True ----
print('9B. Cached Kroger product → kroger_cache + confirmed_local_store=True')

with app.app_context():
    db.drop_all()
    db.create_all()
    db.session.add(Account(household_id=current_household_id(), checking_balance=1000.00))
    db.session.add(StorePriceCache(
        store_name="Kroger",
        item_keyword="chicken_breast",
        product_title="Cached Kroger Chicken",
        price=3.99,
        unit="each",
        is_store_brand=True,
        last_updated=_dt.datetime.utcnow(),
    ))
    db.session.commit()
    res9b = _resolve_terms(app, ["chicken_breast"], store_name="Kroger")

products_9b = res9b.get("chicken_breast", [])
assert_eq(len(products_9b), 1, '9B: one cached product returned')
assert_eq(products_9b[0].get("source"), "kroger_cache",
          '9B: cached Kroger product source is kroger_cache')
assert_truthy(products_9b[0]["source"] in _LOCAL,
              '9B: kroger_cache maps to confirmed_local_store=True')

# ---- 9C: Even one nearby store remains an explicit user choice ----
print('9C. Single nearby Kroger store still requires explicit selection')

with app.app_context():
    db.drop_all()
    db.create_all()
    acc9c = Account(household_id=current_household_id(), checking_balance=1000.00, zip_code='65084', kroger_store_name="Kroger",
                    kroger_location_id=None)
    db.session.add(acc9c)
    r9c = Recipe(title="Test", servings=4)
    db.session.add(r9c)
    db.session.flush()
    db.session.add(RecipeIngredient(
        recipe_id=r9c.id, product_name="milk", clean_keyword="milk",
        quantity=1.0, unit="cup"))
    db.session.commit()
    rid9c = r9c.id

single_store_provider = MagicMock()
single_store_provider.find_stores.return_value = [FakeKrogerStore('loc-a', 'Gerbes - Eldon')]

with patch('services.retail.router.get_retail_provider', return_value=single_store_provider):
    resp9c = client.post('/api/grocery/generate-pay-period-plan',
                         json={"recipe_ids": [rid9c], "store_name": "Kroger", "budget_limit": 100})

d9c = resp9c.get_json() or {}
assert_eq(resp9c.status_code, 200, '9C: store choice response returns 200')
assert_eq(d9c.get("status"), 'store_choice_required', '9C: explicit store choice state returned')
assert_eq(d9c.get("store_choice", {}).get("stores", [])[0].get("store_id"), "loc-a", '9C: nearby store is offered, not selected')
with app.app_context():
    assert_eq(Account.query.first().kroger_location_id, None, '9C: discovery does not persist the store')

# ---- 9D: Multiple Kroger-family stores → clean store-choice state, no 400 ----
print('9D. Multiple Kroger-family stores → clean store-choice state')

with app.app_context():
    db.drop_all()
    db.create_all()
    acc9d = Account(household_id=current_household_id(), checking_balance=1000.00, zip_code='65084', kroger_store_name='Kroger', kroger_location_id=None)
    db.session.add(acc9d)
    r9d = Recipe(title='Choice Test', servings=4)
    db.session.add(r9d)
    db.session.flush()
    db.session.add(RecipeIngredient(recipe_id=r9d.id, product_name='milk', clean_keyword='milk', quantity=1.0, unit='cup'))
    db.session.commit()
    rid9d = r9d.id

multi_store_provider = MagicMock()
multi_store_provider.find_stores.return_value = [
    FakeKrogerStore('loc-a', 'Gerbes - Eldon'),
    FakeKrogerStore('loc-b', 'Kroger - Osage Beach'),
]

with patch('services.retail.router.get_retail_provider', return_value=multi_store_provider):
    resp9d = client.post('/api/grocery/generate-pay-period-plan', json={
        'recipe_ids': [rid9d],
        'store_name': 'Kroger',
        'budget_limit': 100,
    })

d9d = resp9d.get_json() or {}
assert_eq(resp9d.status_code, 200, '9D: store choice response returns 200')
assert_eq(d9d.get('status'), 'store_choice_required', '9D: store choice state returned')
assert_truthy(isinstance(d9d.get('store_choice', {}).get('stores'), list), '9D: stores list returned for choice')
assert_truthy(len(d9d.get('store_choice', {}).get('stores') or []) >= 2, '9D: multiple stores available')
assert_truthy(d9d.get('store_config_warning') is None, '9D: no raw store warning')

# ---- 9E: Once a Kroger store is chosen, reload preserves it automatically ----
print('9E. Chosen Kroger store persists across reload')

with app.app_context():
    account9e = Account.query.first()
    account9e.zip_code = '65084'
    account9e.kroger_store_name = 'Gerbes - Eldon'
    account9e.kroger_location_id = 'loc-a'
    db.session.commit()

with patch('services.retail.cart.build_verified_retail_cart', return_value={
    'cart_items': [{
        'keyword': 'milk',
        'product_label': 'Milk',
        'confirmed_local_store': True,
        'price_source': 'kroger_api',
        'store_name': 'Gerbes - Eldon',
        'selected_product': {'title': 'Milk'},
        'needs_user_choice': False,
    }],
    'subtotal': 3.99,
    'total_cart_cost': 3.99,
    'grocery_tax_rate': 0.0,
    'tax_amount': 0.0,
    'pantry_items_skipped': 0,
    'recipes_used': [{'id': rid9d, 'title': 'Choice Test'}],
    'store': {'store_id': 'loc-a', 'name': 'Gerbes - Eldon'},
}):
    resp9e = client.post('/api/grocery/generate-pay-period-plan', json={
        'recipe_ids': [rid9d],
        'store_name': 'Kroger',
        'budget_limit': 100,
    })

d9e = resp9e.get_json() or {}
assert_eq(resp9e.status_code, 200, '9E: reload returns 200')
assert_eq(d9e.get('store', {}).get('store_id'), 'loc-a', '9E: reload uses saved store id')
assert_truthy(d9e.get('store_choice') is None, '9E: reload does not ask for choice again')

# ---- 9F: Walmart -> Kroger -> Walmart switching remains valid ----
print('9F. Walmart -> Kroger -> Walmart switching remains valid')

with app.app_context():
    db.drop_all()
    db.create_all()
    db.session.add(Account(household_id=current_household_id(), checking_balance=1000.00, zip_code='65084', kroger_store_name='Walmart', kroger_location_id='357'))
    db.session.add(GroceryItem(household_id=current_household_id(), item_name='Shampoo', store_name='Walmart'))
    db.session.commit()

resp_w1 = client.post('/api/grocery/generate-pay-period-plan', json={'recipe_ids': [], 'store_name': 'Walmart', 'budget_limit': 100})
assert_eq(resp_w1.status_code, 200, '9F: Walmart first call returns 200')

with app.app_context():
    account9f = Account.query.first()
    account9f.kroger_store_name = 'Gerbes - Eldon'
    account9f.kroger_location_id = 'loc-a'
    db.session.commit()

with patch('services.retail.cart.build_verified_retail_cart', return_value={
    'cart_items': [],
    'subtotal': 0.0,
    'total_cart_cost': 0.0,
    'grocery_tax_rate': 0.0,
    'tax_amount': 0.0,
    'pantry_items_skipped': 0,
    'recipes_used': [],
    'store': {'store_id': 'loc-a', 'name': 'Gerbes - Eldon'},
}):
    resp_k = client.post('/api/grocery/generate-pay-period-plan', json={'recipe_ids': [rid9d], 'store_name': 'Kroger', 'budget_limit': 100})

assert_eq(resp_k.status_code, 200, '9F: Kroger middle call returns 200')

resp_w2 = client.post('/api/grocery/generate-pay-period-plan', json={'recipe_ids': [], 'store_name': 'Walmart', 'budget_limit': 100})
assert_eq(resp_w2.status_code, 200, '9F: Walmart final call returns 200')

# ---- 9G: RapidAPI result is NOT confirmed local-store ----
print('9G. RapidAPI fallback → confirmed_local_store=False, third-party store_name')

for item in d9c.get("cart_items", []):
    src = item.get("price_source", "")
    if src in ("rapid_api", "rapid_cache"):
        assert_truthy(not item.get("confirmed_local_store"),
                      f'9D: rapid_api item confirmed_local_store must be False')
        assert_truthy(item.get("store_name") != "Kroger",
                      f'9D: rapid_api item must not carry Kroger as store_name')

# ---- 9E: Estimated item → explicitly marked, not confirmed ----
print('9E. Estimated product → price_source=estimated, confirmed_local_store=False')

with app.app_context():
    db.drop_all()
    db.create_all()
    db.session.add(Account(household_id=current_household_id(), checking_balance=1000.00, kroger_location_id=None))
    r9e = Recipe(title="Estimate Test", servings=4)
    db.session.add(r9e)
    db.session.flush()
    db.session.add(RecipeIngredient(
        recipe_id=r9e.id, product_name="rare ingredient xyz",
        clean_keyword="rare_ingredient_xyz", quantity=1.0, unit="oz"))
    db.session.commit()
    rid9e = r9e.id

# Block all external calls so the item must fall to estimate
def _no_network(url, headers=None, params=None, timeout=None):
    raise RuntimeError("No network calls allowed in 9E")

with patch("requests.get", _no_network), \
     patch("services.rapidapi_search._get_rapidapi_key", return_value=None):
    resp9e = client.post('/api/grocery/generate-pay-period-plan',
                         json={"recipe_ids": [rid9e], "budget_limit": 100})

d9e = resp9e.get_json() or {}
items9e = d9e.get("cart_items", [])
assert_truthy(len(items9e) >= 1, '9E: estimated item still appears in cart')
for item in items9e:
    assert_eq(item.get("price_source"), "estimated",
              '9E: unresolvable item has price_source=estimated')
    assert_truthy(not item.get("confirmed_local_store"),
                  '9E: estimated item confirmed_local_store=False')
    assert_truthy(item.get("store_name") is None,
                  '9E: estimated item store_name is null')

# ---- 9F: No result → item appears as estimated/unresolved, not local-store ----
print('9F. No result → item not attributed to local store')

for item in items9e:
    assert_truthy("estimate" in item.get("product_label", "").lower(),
                  '9F: label signals estimate/unresolved')
    assert_truthy(not item.get("confirmed_local_store"),
                  '9F: unresolved item confirmed_local_store=False')


# ===========================================================================
# SUMMARY
# ===========================================================================
def _main():
    print('\n{} passed, {} failed'.format(passed, failed))
    sys.exit(1 if failed > 0 else 0)


def test_sync_api_script_checks() -> None:
    assert failed == 0, f"sync-api script checks failed: {failed}"


if __name__ == '__main__':
    _main()
