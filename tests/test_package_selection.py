#!/usr/bin/env python3
"""Cross-unit package-selection regression tests.

Run with:
    .venv/bin/python tests/test_package_selection.py
"""

import os
import sys

os.environ['RECIPE_CACHE_DISABLED'] = '1'
os.environ['RUNG_DB_PATH'] = ':memory:'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, Account, Recipe, RecipeIngredient, PantryItem, StorePriceCache
from services.household_context import household_id as current_household_id


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


def run_case(label, recipe_qty, recipe_unit, keyword, product_title, package_size, price=4.99):
    with app.app_context():
        db.drop_all()
        db.create_all()
        hid = current_household_id()

        acc = Account(household_id=hid, checking_balance=500.00, kroger_store_name='Walmart')
        db.session.add(acc)
        db.session.flush()

        r = Recipe(title='Case ' + label, servings=2, instructions='Cook')
        db.session.add(r)
        db.session.flush()
        db.session.add(RecipeIngredient(
            recipe_id=r.id,
            product_name=keyword,
            clean_keyword=keyword,
            quantity=float(recipe_qty),
            unit=recipe_unit,
        ))
        db.session.add(StorePriceCache(
            store_name='Walmart',
            item_keyword=keyword,
            product_title=product_title,
            price=float(price),
            package_size=package_size,
            retailer='walmart',
            is_store_brand=True,
        ))
        db.session.commit()
        rid = r.id

    client = app.test_client()
    resp = client.post('/api/grocery/generate-pay-period-plan', json={
        'recipe_ids': [rid],
        'store_name': 'Walmart',
        'budget_limit': 100.0,
    })
    body = resp.get_json() or {}
    items = body.get('cart_items', [])
    selected = None
    for item in items:
        if item.get('keyword') == keyword:
            selected = item
            break
    return resp, body, selected


print('A. Need 0.5 stick butter; real package says 8 oz -> one package satisfies')
resp_a, body_a, item_a = run_case('A', 0.5, 'stick', 'butter', 'Unsalted Butter', '8 oz', 4.29)
check(resp_a.status_code == 200, 'A endpoint returns 200', 200, resp_a.status_code)
check(item_a is not None, 'A butter item exists')
if item_a is not None:
    check(item_a.get('packages_to_buy') == 1, 'A packages_to_buy is 1', 1, item_a.get('packages_to_buy'))
    check(item_a.get('price_source') in ('kroger_cache', 'store_cache_fallback', 'kroger_api'),
          'A uses a real product source', 'real source', item_a.get('price_source'))


print('\nB. Need 1.5 sticks butter; real product says 8 oz -> one package satisfies')
resp_b, body_b, item_b = run_case('B', 1.5, 'sticks', 'butter', 'Unsalted Butter', '8 oz', 4.29)
check(resp_b.status_code == 200, 'B endpoint returns 200', 200, resp_b.status_code)
check(item_b is not None, 'B butter item exists')
if item_b is not None:
    check(item_b.get('packages_to_buy') == 1, 'B packages_to_buy is 1', 1, item_b.get('packages_to_buy'))


print('\nC. Need 5 sticks butter; real product says 16 oz -> two packages required')
resp_c, body_c, item_c = run_case('C', 5.0, 'sticks', 'butter', 'Unsalted Butter', '16 oz', 5.99)
check(resp_c.status_code == 200, 'C endpoint returns 200', 200, resp_c.status_code)
check(item_c is not None, 'C butter item exists')
if item_c is not None:
    check(item_c.get('packages_to_buy') == 2, 'C packages_to_buy is 2', 2, item_c.get('packages_to_buy'))


print('\nD. Need 12 oz cheese; product says 1 lb -> one package')
resp_d, body_d, item_d = run_case('D', 12.0, 'oz', 'cheese', 'Cheddar Cheese', '1 lb', 3.99)
check(resp_d.status_code == 200, 'D endpoint returns 200', 200, resp_d.status_code)
check(item_d is not None, 'D cheese item exists')
if item_d is not None:
    check(item_d.get('packages_to_buy') == 1, 'D packages_to_buy is 1', 1, item_d.get('packages_to_buy'))


print('\nE. Need 3 eggs; product says 12 ct -> one package')
resp_e, body_e, item_e = run_case('E', 3.0, 'item', 'eggs', 'Large Eggs', '12 ct', 2.99)
check(resp_e.status_code == 200, 'E endpoint returns 200', 200, resp_e.status_code)
check(item_e is not None, 'E eggs item exists')
if item_e is not None:
    check(item_e.get('packages_to_buy') == 1, 'E packages_to_buy is 1', 1, item_e.get('packages_to_buy'))


print('\nF. Unsafe cross-dimension: cups of flour vs oz package -> must not fabricate conversion')
resp_f, body_f, item_f = run_case('F', 1.0, 'cup', 'flour', 'All-Purpose Flour', '16 oz', 2.10)
check(resp_f.status_code == 200, 'F endpoint returns 200', 200, resp_f.status_code)
check(item_f is not None, 'F flour item exists')
if item_f is not None:
    check(item_f.get('price_source') == 'estimated',
          'F falls back to estimate when conversion is unsafe',
          'estimated', item_f.get('price_source'))
    check(bool(item_f.get('package_selection_uncertain', False)),
          'F marks package selection as uncertain', True, item_f.get('package_selection_uncertain'))

print('\n{} passed, {} failed'.format(passed, failed))


def test_package_selection_regression_script_checks() -> None:
    assert failed == 0, f"package-selection script checks failed: {failed}"


if __name__ == '__main__':
    sys.exit(1 if failed > 0 else 0)
