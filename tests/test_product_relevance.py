#!/usr/bin/env python3
"""Deterministic product relevance regression tests for store_api pick_best."""

import os
import sys

os.environ['RECIPE_CACHE_DISABLED'] = '1'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.store_api import pick_best, rank_product_candidates


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


def top_title(rankings):
    return rankings[0]['product_title'] if rankings else ''


print('A. truffle oil: valid product beats truffle pizza')
a_products = [
    {"product_title": "Italian Truffle Oil", "brand": "Gourmet", "price": 8.99, "package_size": "8 oz", "is_store_brand": False, "categories": ["Oils"]},
    {"product_title": "Frozen Truffle Pizza", "brand": "Kitchen", "price": 6.99, "package_size": "16 oz", "is_store_brand": False, "categories": ["Frozen", "Pizza"]},
]
a_rank = rank_product_candidates('truffle_oil', a_products)
check(top_title(a_rank) == 'Italian Truffle Oil', 'A ranking places truffle oil first', 'Italian Truffle Oil', top_title(a_rank))
a_best = pick_best(a_products, prefer_store_brand=False, keyword='truffle_oil', net_needed=4, required_dimension='mass')
check(a_best is not None, 'A pick_best returns relevant result')
check(a_best.get('product_title') == 'Italian Truffle Oil', 'A selected truffle oil, not pizza', 'Italian Truffle Oil', a_best.get('product_title'))


print('\nB. black pepper: ground black pepper valid, Dr Pepper rejected')
b_products = [
    {"product_title": "Ground Black Pepper", "brand": "Spice Co", "price": 2.99, "package_size": "3 oz", "is_store_brand": False, "categories": ["Spices"]},
    {"product_title": "Dr Pepper Soda", "brand": "Dr Pepper", "price": 1.99, "package_size": "12 ct", "is_store_brand": False, "categories": ["Beverages"]},
]
b_rank = rank_product_candidates('black_pepper', b_products)
check(top_title(b_rank) == 'Ground Black Pepper', 'B ranking places black pepper first', 'Ground Black Pepper', top_title(b_rank))
b_best = pick_best(b_products, prefer_store_brand=False, keyword='black_pepper', net_needed=1, required_dimension='mass')
check(b_best is not None, 'B pick_best returns relevant result')
check(b_best.get('product_title') == 'Ground Black Pepper', 'B selected black pepper, not soda', 'Ground Black Pepper', b_best.get('product_title'))


print('\nC. butter: unsalted butter valid, butter-flavored popcorn rejected')
c_products = [
    {"product_title": "Unsalted Butter", "brand": "Farm", "price": 4.29, "package_size": "1 lb", "is_store_brand": True, "categories": ["Dairy"]},
    {"product_title": "Butter-Flavored Popcorn", "brand": "Snacky", "price": 2.50, "package_size": "6 oz", "is_store_brand": False, "categories": ["Snacks"]},
]
c_rank = rank_product_candidates('butter', c_products)
check(top_title(c_rank) == 'Unsalted Butter', 'C ranking places butter first', 'Unsalted Butter', top_title(c_rank))
c_best = pick_best(c_products, prefer_store_brand=True, keyword='butter', net_needed=8, required_dimension='mass')
check(c_best is not None, 'C pick_best returns relevant result')
check(c_best.get('product_title') == 'Unsalted Butter', 'C selected butter, not popcorn', 'Unsalted Butter', c_best.get('product_title'))


print('\nD. chicken breast: raw chicken preferred over frozen dinner')
d_products = [
    {"product_title": "Boneless Skinless Chicken Breast", "brand": "Butcher", "price": 9.49, "package_size": "2 lb", "is_store_brand": True, "categories": ["Meat", "Raw"]},
    {"product_title": "Chicken Breast Frozen Dinner", "brand": "QuickMeal", "price": 4.99, "package_size": "12 oz", "is_store_brand": False, "categories": ["Frozen", "Entree"]},
]
d_rank = rank_product_candidates('chicken_breast', d_products)
check(top_title(d_rank) == 'Boneless Skinless Chicken Breast', 'D ranking places raw chicken first', 'Boneless Skinless Chicken Breast', top_title(d_rank))
d_best = pick_best(d_products, prefer_store_brand=True, keyword='chicken_breast', net_needed=24, required_dimension='mass')
check(d_best is not None, 'D pick_best returns relevant result')
check(d_best.get('product_title') == 'Boneless Skinless Chicken Breast', 'D selected raw chicken, not frozen dinner', 'Boneless Skinless Chicken Breast', d_best.get('product_title'))


print('\nE. olive oil: extra virgin valid, flavored snack rejected')
e_products = [
    {"product_title": "Extra Virgin Olive Oil", "brand": "Harvest", "price": 6.99, "package_size": "16.9 oz", "is_store_brand": False, "categories": ["Oils"]},
    {"product_title": "Olive-Oil-Flavored Crackers", "brand": "Snacky", "price": 2.99, "package_size": "8 oz", "is_store_brand": False, "categories": ["Snacks", "Crackers"]},
]
e_rank = rank_product_candidates('olive_oil', e_products)
check(top_title(e_rank) == 'Extra Virgin Olive Oil', 'E ranking places olive oil first', 'Extra Virgin Olive Oil', top_title(e_rank))
e_best = pick_best(e_products, prefer_store_brand=False, keyword='olive_oil', net_needed=8, required_dimension='mass')
check(e_best is not None, 'E pick_best returns relevant result')
check(e_best.get('product_title') == 'Extra Virgin Olive Oil', 'E selected olive oil, not snack', 'Extra Virgin Olive Oil', e_best.get('product_title'))


print('\nF. no relevant candidate: return unresolved (None)')
f_products = [
    {"product_title": "Truffle Pizza", "brand": "Kitchen", "price": 6.99, "package_size": "16 oz", "is_store_brand": False, "categories": ["Frozen", "Pizza"]},
    {"product_title": "Garlic Bread", "brand": "Bakery", "price": 3.49, "package_size": "10 oz", "is_store_brand": False, "categories": ["Bakery"]},
]
f_rank = rank_product_candidates('truffle_oil', f_products)
check(f_rank[0]['valid'] is False, 'F top-ranked candidate still invalid for keyword')
f_best = pick_best(f_products, prefer_store_brand=False, keyword='truffle_oil', net_needed=4, required_dimension='mass')
check(f_best is None, 'F pick_best returns None when nothing is safely relevant')


print('\n{} passed, {} failed'.format(passed, failed))


def test_product_relevance_regression_script_checks() -> None:
    assert failed == 0, f"product-relevance script checks failed: {failed}"


if __name__ == '__main__':
    sys.exit(1 if failed > 0 else 0)
