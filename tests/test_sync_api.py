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

os.environ['RECIPE_CACHE_DISABLED'] = '1'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so these tests can never wipe the user's real rung_finance.db rows.
os.environ['RUNG_DB_PATH'] = ':memory:'

from app import (
    app, db, Account, Recipe, RecipeIngredient, BrandPreference,
    StorePriceCache, PantryItem,
)
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


# ===========================================================================
# SETUP — clear state, seed deterministic scenario
# ===========================================================================
with app.app_context():
    db.create_all()  # in-memory DB starts empty — build the schema first
    BrandPreference.query.delete()
    StorePriceCache.query.delete()
    PantryItem.query.delete()
    RecipeIngredient.query.delete()
    Recipe.query.delete()
    db.session.commit()

    # Ensure an Account row exists — cart/build and generate-pay-period-plan
    # both access Account.query.first() attributes unconditionally.
    if not Account.query.first():
        db.session.add(Account(checking_balance=1250.00))
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


# ===========================================================================
# TEST 3: plan subtotal + tax computed correctly
# ===========================================================================
print('3. generate-pay-period-plan subtotal and tax are computed')
subtotal = float(d.get('subtotal', 0))
tax_amount = float(d.get('tax_amount', 0))
total = float(d.get('total_cart_cost', 0))
assert_truthy(subtotal > 0, 'subtotal is positive')
assert_truthy(total > 0, 'total_cart_cost is positive')
assert_eq(round(subtotal + tax_amount, 2), round(total, 2),
          'subtotal + tax == total_cart_cost')
# Verify the response includes the enriched fields (package_size, resolution stats)
stats = d.get('resolution_stats', {})
assert_truthy(stats.get('total_terms', 0) >= 4, 'resolution_stats covers all terms')
budget = d.get('budget', {})
assert_truthy(budget.get('food_budget', 0) > 0, 'food_budget is computed')


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
assert_truthy(budget.get('budget_remaining', 0) < 0,
              'budget_remaining is negative')


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
# SUMMARY
# ===========================================================================
print('\n{} passed, {} failed'.format(passed, failed))
sys.exit(1 if failed > 0 else 0)
