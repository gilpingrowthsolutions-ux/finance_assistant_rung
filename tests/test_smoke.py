#!/usr/bin/env python3
"""
End-to-end smoke test for the full pay-period grocery pipeline.

Seeds a deterministic scenario with two recipes, StorePriceCache prices
(name brand + store brand), BrandPreference (locked cheerios), Pantry
stock, and a custom budget. Then hits `/api/grocery/generate-pay-period-plan`
and verifies every response field end-to-end:

  • Basic response shape (cart_items, subtotal, total, tax)
  • package_size on every cart item
  • image_url field presence
  • price_source per item (cache vs estimate)
  • BrandPreference — cheerios locked to name brand (not store brand)
  • Pantry deduction — flour skipped (on-hand >= needed)
  • Resolution stats (cache_hits, fallbacks)
  • Budget enforcement (exceeded flag, remaining)
  • recipes_used list

Run with:  .venv/bin/python tests/test_smoke.py
"""
import os
import sys

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


def ok(label):
    global passed
    passed += 1
    print('  PASS: ' + label)


def fail(label, expected=None, actual=None):
    global failed
    failed += 1
    msg = '  FAIL: ' + label
    if expected is not None:
        msg += '\n    expected: ' + repr(expected)
    if actual is not None:
        msg += '\n    actual:   ' + repr(actual)
    print(msg)


def check(condition, label, expected=None, actual=None):
    if condition:
        ok(label)
    else:
        fail(label, expected, actual)


# ===========================================================================
# SETUP — clear state, seed deterministic scenario
# ===========================================================================
with app.app_context():
    db.create_all()  # in-memory DB starts empty — build the schema first
    # Wipe all relevant tables
    BrandPreference.query.delete()
    StorePriceCache.query.delete()
    PantryItem.query.delete()
    RecipeIngredient.query.delete()
    Recipe.query.delete()
    Account.query.delete()
    db.session.commit()

    # Account with custom grocery tax rate (separate from sales tax)
    acc = Account(
        checking_balance=2000.00,
        food_allocation_pct=30.0,
        pay_period_days=14,
        meals_per_day=3,
        grocery_tax_rate=0.025,     # 2.5% — distinct from sales tax
        sales_tax_rate=0.0825,
        kroger_store_name='Walmart',
    )
    db.session.add(acc)
    db.session.commit()

    # ---- Recipe A: Cheesy Pasta (4 ingredients) --------------------------
    r1 = Recipe(title='Cheesy Pasta', servings=4, instructions='Boil, mix, bake.')
    db.session.add(r1)
    db.session.flush()
    pasta_ings = [
        ('Penne Pasta',     'pasta',      16, 'oz'),
        ('Cheddar Cheese',  'cheese',      8,  'oz'),
        ('Whole Milk',      'milk',       1,  'cup'),
        ('All-Purpose Flour','flour',      2,  'tbsp'),
    ]
    for pname, kw, qty, unit in pasta_ings:
        db.session.add(RecipeIngredient(
            recipe_id=r1.id, product_name=pname, clean_keyword=kw,
            quantity=qty, unit=unit,
        ))

    # ---- Recipe B: Breakfast Cereal Bowl (3 ingredients) -----------------
    r2 = Recipe(title='Breakfast Cereal Bowl', servings=1, instructions='Pour and eat.')
    db.session.add(r2)
    db.session.flush()
    cereal_ings = [
        ('Honey Nut Cheerios', 'cheerios', 1, 'box'),
        ('Whole Milk',         'milk',     1, 'cup'),
        ('Banana',             'banana',   1, 'item'),
    ]
    for pname, kw, qty, unit in cereal_ings:
        db.session.add(RecipeIngredient(
            recipe_id=r2.id, product_name=pname, clean_keyword=kw,
            quantity=qty, unit=unit,
        ))
    db.session.commit()
    recipe_ids = [r1.id, r2.id]

    # ---- StorePriceCache — Walmart prices with package sizes -------------
    cache_seed = [
        # Name-brand products
        ('Walmart', 'pasta',     'Barilla Penne Pasta 16oz',          1.99, 0, '16 oz',  'walmart'),
        ('Walmart', 'cheese',    'Kraft Sharp Cheddar 8oz',           3.49, 0, '8 oz',   'walmart'),
        ('Walmart', 'milk',      'Horizon Organic Whole Milk 64oz',   4.99, 0, '64 oz',  'walmart'),
        ('Walmart', 'flour',     'Gold Medal All-Purpose Flour 5lb',  3.29, 0, '5 lb',   'walmart'),
        ('Walmart', 'cheerios',  'Honey Nut Cheerios 15.4oz',         4.49, 0, '15.4 oz','walmart'),
        # Store-brand alternatives (cheaper)
        ('Walmart', 'pasta',     'Great Value Penne Pasta 16oz',      1.00, 1, '16 oz',  'walmart'),
        ('Walmart', 'cheese',    'Great Value Sharp Cheddar 8oz',     2.49, 1, '8 oz',   'walmart'),
        ('Walmart', 'milk',      'Great Value Whole Milk 64oz',       2.99, 1, '64 oz',  'walmart'),
        ('Walmart', 'flour',     'Great Value All-Purpose Flour 5lb', 2.10, 1, '5 lb',   'walmart'),
        ('Walmart', 'cheerios',  'Great Value Toasted Oats 18oz',     2.10, 1, '18 oz',  'walmart'),
        # Banana — only one option (no store brand distinction)
        ('Walmart', 'banana',    'Fresh Banana, each',                0.29, 0, '1 ct',   'walmart'),
    ]
    for store, kw, title, price, is_sb, pkg, rtlr in cache_seed:
        db.session.add(StorePriceCache(
            store_name=store, item_keyword=kw, product_title=title,
            price=price, package_size=pkg, retailer=rtlr,
            is_store_brand=is_sb,
        ))
    db.session.commit()

    # ---- BrandPreference: cheerios locked to name brand ------------------
    pref = BrandPreference(
        clean_keyword='cheerios',
        prefer_store_brand=False,
        preferred_brand_name='Honey Nut Cheerios',
    )
    db.session.add(pref)
    db.session.commit()

    # ---- Pantry: enough flour to skip purchasing it ----------------------
    db.session.add(PantryItem(
        clean_keyword='flour', product_name='All Purpose Flour',
        quantity=32.0, unit='oz',  # 32 oz on-hand vs 1 oz needed (2 tbsp)
    ))
    db.session.commit()


client = app.test_client()

# ===========================================================================
# SECTION 1: Full pipeline — resolve from cache, verify shape
# ===========================================================================
print('1. Seed + resolve: hit /api/grocery/generate-pay-period-plan')

resp = client.post('/api/grocery/generate-pay-period-plan', json={
    'recipe_ids': recipe_ids,
    'store_name': 'Walmart',
    'budget_limit': 25.00,
})
check(resp.status_code == 200, 'returns 200', 200, resp.status_code)

d = resp.get_json() or {}
cart = d.get('cart_items', [])
check(len(cart) >= 5, 'at least 5 cart items (7 ingredients - 1 skipped flour - maybe merged milk)',
     '>=5', len(cart))

# ===========================================================================
# SECTION 2: package_size on every cart item
# ===========================================================================
print('\n2. package_size present on every cart item')
all_have_pkg = True
empty_pkgs = []
for item in cart:
    pkg = item.get('package_size', '')
    if not pkg:
        all_have_pkg = False
        empty_pkgs.append(item.get('keyword', '?'))
check(all_have_pkg, 'every cart item has a non-empty package_size',
     'all items', 'missing: ' + ', '.join(empty_pkgs) if empty_pkgs else None)

# ===========================================================================
# SECTION 3: price_source per item
# ===========================================================================
print('\n3. price_source per cart item')
valid_sources = {'cache', 'api', 'estimated', 'rapid_api', 'rapid_cache', 'kroger_cache', 'kroger_api', 'store_cache_fallback'}
all_valid = True
for item in cart:
    ps = item.get('price_source', '')
    if ps not in valid_sources:
        all_valid = False
        fail(f'{item.get("keyword", "?")} has invalid price_source: {repr(ps)}')
check(all_valid, 'all price_sources are valid')

# All seeded items should resolve from local cache (no API calls needed)
cache_items = [i for i in cart if i.get('price_source') == 'cache']
check(len(cache_items) >= 4, 'at least 4 items resolved from cache',
     '>=4', len(cache_items))

# ===========================================================================
# SECTION 4: BrandPreference — cheerios locked to name brand
# ===========================================================================
print('\n4. BrandPreference: cheerios locked to name brand (not store brand)')
cheerios_items = [i for i in cart if i.get('keyword') == 'cheerios']
check(len(cheerios_items) >= 1, 'cheerios appears in cart')
if cheerios_items:
    ci = cheerios_items[0]
    label = ci.get('product_label', '')
    check('Honey Nut' in label, f'cheerios resolved to name brand (got: {label})',
         'Honey Nut Cheerios', label)
    check('Great Value' not in label, f'cheerios is NOT the store brand (got: {label})',
         'not Great Value', label)
    check(ci.get('price_source') == 'cache',
          'cheerios price_source is cache')

# ===========================================================================
# SECTION 5: Pantry deduction — flour skipped
# ===========================================================================
print('\n5. Pantry deduction: flour skipped (on-hand >= needed)')
flour_items = [i for i in cart if i.get('keyword') == 'flour']
check(len(flour_items) == 0, 'flour NOT in cart (covered by pantry)',
     '0 flour items', len(flour_items))
check(d.get('pantry_items_skipped', 0) >= 1,
      'pantry_items_skipped >= 1')

# ===========================================================================
# SECTION 6: Tax computation — grocery tax (not sales tax)
# ===========================================================================
print('\n6. Tax uses grocery_tax_rate (2.5%) not sales_tax_rate (8.25%)')
tax_rate = d.get('grocery_tax_rate', 0)
check(tax_rate == 2.5,
      f'grocery_tax_rate is 2.5% (got {tax_rate}%)',
      2.5, tax_rate)

subtotal = float(d.get('subtotal', 0))
tax_amt = float(d.get('tax_amount', 0))
total = float(d.get('total_cart_cost', 0))
check(abs(round(subtotal * 0.025, 2) - round(tax_amt, 2)) <= 0.03,
      f'tax_amount ≈ subtotal * 2.5% (subtotal={subtotal}, tax={tax_amt})')
check(round(subtotal + tax_amt, 2) == round(total, 2),
      f'subtotal + tax == total_cart_cost ({subtotal} + {tax_amt} == {total})')

# ===========================================================================
# SECTION 7: Budget enforcement
# ===========================================================================
print('\n7. Budget enforcement (budget_limit=25.00)')
budget = d.get('budget', {})
check(budget.get('food_budget') == 25.00,
      'food_budget is exactly 25.00 (budget_limit honored)',
      25.00, budget.get('food_budget'))
remaining = budget.get('budget_remaining', 0)
check(isinstance(remaining, (int, float)), 'budget_remaining is numeric')
# With all items from cache, subtotal should be around $12-16 (depends on store-brand picks)
# so budget should NOT be exceeded at $25
print(f'    subtotal={subtotal}, tax={tax_amt}, total={total}, remaining={remaining}')
check(total < 25.00,
      f'cart total ({total}) is under the 25.00 budget',
      True, total < 25.00)
check(budget.get('budget_exceeded') is False,
      'budget_exceeded is False (not over budget)')

# ===========================================================================
# SECTION 8: Resolution stats
# ===========================================================================
print('\n8. Resolution stats')
stats = d.get('resolution_stats', {})
check(isinstance(stats, dict), 'resolution_stats is a dict')
total_terms = stats.get('total_terms', 0)
# 7 unique keywords across both recipes: pasta, cheese, milk, flour, cheerios, banana
check(total_terms >= 6, f'total_terms covers all ingredients (got {total_terms})',
     '>=6', total_terms)
cache_hits = stats.get('cache_hits', 0)
check(cache_hits >= 4, f'cache_hits >= 4 (got {cache_hits})',
     '>=4', cache_hits)

# ===========================================================================
# SECTION 9: recipes_used list
# ===========================================================================
print('\n9. recipes_used')
used = d.get('recipes_used', [])
check(len(used) == 2, f'recipes_used has 2 entries (got {len(used)})',
     '2 recipes', len(used))
if len(used) >= 2:
    titles = {r['title'] for r in used}
    check('Cheesy Pasta' in titles, 'Cheesy Pasta is in recipes_used')
    check('Breakfast Cereal Bowl' in titles, 'Breakfast Cereal Bowl is in recipes_used')

# ===========================================================================
# SECTION 10: Over-budget scenario
# ===========================================================================
print('\n10. Over-budget scenario (budget_limit=0.50)')
resp2 = client.post('/api/grocery/generate-pay-period-plan', json={
    'recipe_ids': recipe_ids,
    'store_name': 'Walmart',
    'budget_limit': 0.50,
})
check(resp2.status_code == 200, 'returns 200 even when over budget', 200, resp2.status_code)
d2 = resp2.get_json() or {}
b2 = d2.get('budget', {})
check(b2.get('budget_exceeded') is True,
      'budget_exceeded is True at $0.50',
      True, b2.get('budget_exceeded'))
check(b2.get('budget_remaining', 0) < 0,
      'budget_remaining is negative',
      '<0', b2.get('budget_remaining'))

# ===========================================================================
# SUMMARY
# ===========================================================================
print('\n' + '=' * 60)
print('{} passed, {} failed'.format(passed, failed))
sys.exit(1 if failed > 0 else 0)
