#!/usr/bin/env python3
"""
Smoke tests for StorePriceCache ingestion.

Two surfaces covered:
  1. POST /api/store-cache/upload-csv — JSON-wrapped CSV upload.
  2. scripts.ingest_store_prices.KrogerClient — Kroger API client with
     mocked requests (token + products search).

The Kroger API itself is not exercised; both endpoints of
KrogerClient are mocked via a tiny `MockResponse` shim so the test
runs offline.

Run with:  .venv/bin/python tests/test_ingest.py
"""
import csv
import io
import json
import os
import sys

os.environ.setdefault('RECIPE_CACHE_DISABLED', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so these tests can never wipe the user's real rung_finance.db rows.
os.environ['RUNG_DB_PATH'] = ':memory:'

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


# ===========================================================================
# 1. CSV upload endpoint
# ===========================================================================
from app import app, db, StorePriceCache  # noqa: E402

app.testing = True
client = app.test_client()

print('1. POST /api/store-cache/upload-csv accepts a well-formed CSV')

with app.app_context():
    db.create_all()  # in-memory DB starts empty — build the schema first
    StorePriceCache.query.delete()
    db.session.commit()

csv_body = (
    'store_name,item_keyword,product_title,price,package_size,is_store_brand,retailer\n'
    'Walmart,cheerios,Honey Nut Cheerios 15.4oz,4.49,15.4 oz,0,walmart\n'
    'Walmart,cheerios,Great Value Toasted Oats Cereal,2.10,18 oz,1,walmart\n'
    'Walmart,milk,Great Value Whole Milk 1gal,3.12,1 gal,1,walmart\n'
    'Walmart,milk,Horizon Organic Whole Milk 1gal,6.49,1 gal,0,walmart\n'
    # Bad row: missing store_name. Should be reported in errors, not crash.
    ',eggs,Bad Row Eggs,3.00,12 ct,1,manual\n'
    # Bad row: non-numeric price.
    'Walmart,bad,Bad Price Item,abc,1 lb,0,walmart\n'
    # Bad row: zero price.
    'Walmart,zero,Zero Price Item,0,1 lb,0,walmart\n'
)

resp = client.post('/api/store-cache/upload-csv',
                   json={'csv': csv_body})
assert_eq(resp.status_code, 200, 'CSV upload returns 200')
d = resp.get_json() or {}
assert_eq(d.get('inserted'), 4, '4 rows inserted (bad rows skipped)')
assert_eq(d.get('updated'), 0, 'no updates on first upload')
assert_eq(d.get('skipped'), 3, '3 bad rows skipped (missing store, bad price, zero price)')
assert_eq(len(d.get('errors', [])) >= 3, True,
          'errors list reports each bad row')

with app.app_context():
    rows = StorePriceCache.query.order_by(StorePriceCache.id.asc()).all()
    assert_eq(len(rows), 4, 'DB has 4 cache rows')
    cheerios_rows = [r for r in rows if r.item_keyword == 'cheerios']
    assert_eq(len(cheerios_rows), 2,
              'cheerios keyword has TWO candidates (locked vs store-brand)')
    sb_rows = [r for r in cheerios_rows if r.is_store_brand == 1]
    assert_eq(len(sb_rows), 1,
              'one cheerios row marked is_store_brand=1')


print('2. POST /api/store-cache/upload-csv upserts on re-upload')
# Re-upload with a price change on the first row.
csv_body2 = (
    'store_name,item_keyword,product_title,price,package_size,is_store_brand,retailer\n'
    'Walmart,cheerios,Honey Nut Cheerios 15.4oz,4.99,15.4 oz,0,walmart\n'
)
resp = client.post('/api/store-cache/upload-csv', json={'csv': csv_body2})
d = resp.get_json() or {}
assert_eq(d.get('updated'), 1, 'second upload updates existing row')
assert_eq(d.get('inserted'), 0, 'no new rows from re-upload')

with app.app_context():
    cheerios = StorePriceCache.query.filter_by(
        product_title='Honey Nut Cheerios 15.4oz'
    ).first()
    assert_eq(cheerios.price, 4.99, 'price updated to 4.99 on re-upload')


print('3. POST /api/store-cache/upload-csv validates the header')
resp = client.post('/api/store-cache/upload-csv',
                   json={'csv': 'foo,bar\n1,2\n'})
assert_eq(resp.status_code, 400, 'missing required columns returns 400')
d = resp.get_json() or {}
assert_eq('error' in d, True, '400 response carries error message')


print('4. POST /api/store-cache/upload-csv rejects non-JSON payloads')
resp = client.post('/api/store-cache/upload-csv', json={})
assert_eq(resp.status_code, 400, 'empty body returns 400')


# ===========================================================================
# 5. KrogerClient unit tests (mocked network)
# ===========================================================================
print('5. KrogerClient.search_products with mocked requests')

# We stub the requests module inside the ingest module so the real
# network is never touched. This validates the OAuth2 flow + the
# products parsing path end-to-end without external calls.
import scripts.ingest_store_prices as ingest_mod

call_log: list = []


class MockResp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self._text = text or json.dumps(payload or {})

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f'status {self.status_code}')

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def make_mock_post(payload):
    def _post(url, *args, **kwargs):
        call_log.append(('POST', url, kwargs.get('data')))
        return MockResp(200, payload)
    return _post


def make_mock_get(responses):
    """responses: list of (url_predicate, MockResp) in call order."""
    state = {'i': 0}

    def _get(url, *args, **kwargs):
        idx = state['i']
        if idx >= len(responses):
            raise RuntimeError('no more mocked responses')
        predicate, resp = responses[idx]
        state['i'] += 1
        if not predicate(url):
            raise RuntimeError(f'unexpected GET {url} (call {idx})')
        call_log.append(('GET', url, kwargs.get('params')))
        return resp
    return _get


ingest_mod.requests = type('R', (), {'post': None, 'get': None})

# Token call
ingest_mod.requests.post = make_mock_post({
    'access_token': 'fake-token-xyz',
    'expires_in': 1800,
})

# Two products
products_payload = {
    'data': [
        {
            'productId': '0001111041700',
            'brand': 'Kroger',
            'description': 'Kroger Whole Vitamin D Milk',
            'items': [{
                'price': {'regular': 3.29, 'promo': 0.0},
                'size': '1 gal',
            }],
            'images': [{'sizes': [{'url': 'https://example.com/milk.jpg'}]}],
        },
        {
            'productId': '0001111041701',
            'brand': 'Horizon',
            'description': 'Horizon Organic Whole Milk',
            'items': [{
                'price': {'regular': 6.49, 'promo': 0.0},
                'size': '1 gal',
            }],
            'images': [],
        },
    ],
}
ingest_mod.requests.get = make_mock_get([
    (lambda u: u == ingest_mod.KROGER_PRODUCTS_URL,
     MockResp(200, products_payload)),
])

kc = ingest_mod.KrogerClient('cid', 'csecret')
out = kc.search_products('milk', '14100943', limit=2)
assert_truthy(out is not None, 'KrogerClient returns products on success')
if out is not None:
    assert_eq(len(out), 2, 'two products parsed from response')
    assert_eq(out[0]['product_title'],
              'Kroger Whole Vitamin D Milk', 'first product title parsed')
    assert_eq(out[0]['price'], 3.29, 'first product price parsed')
    assert_eq(out[0]['is_store_brand'], 1,
              'Kroger brand auto-detected as store-brand')
    assert_eq(out[0]['package_size'], '1 gal', 'package size parsed')
    assert_eq(out[1]['is_store_brand'], 0,
              'Horizon brand correctly NOT flagged as store-brand')
    assert_eq(out[1]['image_url'], '',
              'missing images yield empty image_url (no crash)')


# ===========================================================================
# 6. Dry-run path
# ===========================================================================
print('6. CLI dry-run path prints but does not write')

# Reset DB so we can assert no rows were inserted.
with app.app_context():
    before_count = StorePriceCache.query.count()

# Reuse the same mock infrastructure but skip the DB write.
import argparse
dry_args = argparse.Namespace(
    config=None,
    store='Kroger',
    location_id='14100943',
    terms='milk',
    limit=2,
    dry_run=True,
    verbose=False,
)

# Re-mock so the second search_products call also returns data.
ingest_mod.requests.get = make_mock_get([
    (lambda u: True, MockResp(200, products_payload)),
])

stats = ingest_mod.run_ingest(
    app,
    kc,
    {
        'stores': [{
            'store_name': 'Kroger',
            'location_id': '14100943',
            'terms': ['milk'],
            'limit': 2,
        }],
    },
    dry_run=True,
)
assert_eq(stats.get('inserted'), 2,
          'dry-run reports 2 inserts (does NOT actually write)')

with app.app_context():
    after_count = StorePriceCache.query.count()
assert_eq(after_count, before_count,
          'dry-run left DB row count unchanged')


# ===========================================================================
# SUMMARY
# ===========================================================================
def _main():
    print('\n{} passed, {} failed'.format(passed, failed))
    sys.exit(1 if failed > 0 else 0)


def test_ingest_script_checks() -> None:
    assert failed == 0, f"ingest script checks failed: {failed}"


if __name__ == '__main__':
    _main()