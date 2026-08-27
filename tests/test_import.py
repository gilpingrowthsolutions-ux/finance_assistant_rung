#!/usr/bin/env python3
"""
Unit + integration tests for the recipe import pipeline.

Covers three surfaces:
  1. ``_parse_recipe_yields()``      — pure-function unit tests.
  2. ``_derive_clean_keyword()``     — pure-function unit tests.
  3. ``POST /api/recipes/import``    — Flask test-client with mocked
     recipe-scrapers (offline) + one live guard.

The recipe-scrapers library is mocked for all endpoint tests so the
suite runs offline.  A live guard at the end verifies that the real
``scrape_me`` import succeeds (the library IS reachable from the
venv), but does NOT hit the network.

Run with:  .venv/bin/python tests/test_import.py
"""

import os
import sys
import time
from unittest.mock import patch, MagicMock

os.environ.setdefault('RECIPE_CACHE_DISABLED', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate tests from the user's real database: use an in-memory SQLite DB
# so these tests can never wipe the user's real rung_finance.db rows.
os.environ['RUNG_DB_PATH'] = ':memory:'

from app import (
    app, db, Recipe, RecipeIngredient,
    _parse_recipe_yields, _derive_clean_keyword,
    _scrape_with_curl_cffi,
)
import app as app_mod
from services.household_context import household_id

app.testing = True
client = app.test_client()

# ---------------------------------------------------------------------------
# Clear DB so tests are reproducible across runs (same pattern as
# test_sync_api.py).
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()  # in-memory DB starts empty — build the schema first
    RecipeIngredient.query.delete()
    Recipe.query.delete()
    db.session.commit()

# ---------------------------------------------------------------------------
# Helpers (mirror the existing test conventions)
# ---------------------------------------------------------------------------
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
        print('    needle: ' + repr(needle))
        print('    haystack did not contain needle')


# ===========================================================================
# 1. _parse_recipe_yields  —  pure-function unit tests
# ===========================================================================
print('1. _parse_recipe_yields')

cases = [
    ('4 servings',      4),
    ('1 serving',       1),
    ('2 dozen',         2),
    ('6-8 servings',    6),
    ('8 - 10 cookies',  8),
    ('',                4),
    (None,              4),
    ('serves 10',       10),
    ('Makes 12 cookies', 12),
    ('8',               8),
    ('A guatemalan dish', 4),
    ('About 24 cookies', 24),
    ('N/A',             4),
    ('To taste',        4),
    ('\u2014',          4),
    ('3 large patties', 3),
    ('makes 6 meatballs', 6),
]
for inp, exp in cases:
    got = _parse_recipe_yields(inp)
    assert_eq(got, exp, 'yields(' + repr(inp) + ') \u2192 ' + str(exp))


# ===========================================================================
# 2. _derive_clean_keyword  —  pure-function unit tests
# ===========================================================================
print('2. _derive_clean_keyword')

kw_cases = [
    ('2 cups all-purpose flour',          'flour'),
    ('\u00bd teaspoon extra-virgin olive oil', 'olive_oil'),
    ('Sea salt',                          'salt'),
    ('Crumbled feta cheese',              'feta_cheese'),
    ('1 lb boneless skinless chicken breast', 'chicken_breast'),
    ('Chopped chives',                    'chives'),
    ('1 (16 oz) can black beans',         'black_beans'),
    ('salt to taste',                     'salt'),
    ('Olive oil, for drizzling',          'olive_oil'),
    ('2 eggs, per small ramekin',         'eggs'),
    ('Frozen peas, thawed',               'peas'),
    ('Fresh dill',                        'dill'),
    ('Saut\u00e9ed asparagus',           'asparagus'),
    ('Microgreens',                       'microgreens'),
    ('1 cup shredded cheddar cheese',     'cheddar_cheese'),
    ('3 cloves garlic, minced',           'garlic'),
    ('\u00bc cup grated Parmesan',       'parmesan'),
    ('1 tablespoon soy sauce',            'soy_sauce'),
    ('Kosher salt',                       'salt'),
    ('1 can (15 oz) diced tomatoes',      'tomatoes'),
]
for inp, exp in kw_cases:
    got = _derive_clean_keyword(inp)
    assert_eq(got, exp, 'keyword(' + repr(inp) + ')')


# ===========================================================================
# 3. POST /api/recipes/import  —  empty URL
# ===========================================================================
print('3. POST /api/recipes/import \u2014 empty URL returns 400')
resp = client.post('/api/recipes/import', json={'url': ''})
assert_eq(resp.status_code, 400, 'empty URL returns 400')
d = resp.get_json() or {}
assert_eq(d.get('error'), 'URL required', 'error message for empty URL')
assert_eq(d.get('cache', {}).get('status'), 'error', 'cache status is error')

resp2 = client.post('/api/recipes/import', json={})
assert_eq(resp2.status_code, 400, 'missing url key returns 400')

resp3 = client.post('/api/recipes/import', json={'url': '   '})
assert_eq(resp3.status_code, 400, 'whitespace-only URL returns 400')


# ===========================================================================
# 4. POST /api/recipes/import  —  successful scrape (mocked)
# ===========================================================================
print('4. POST /api/recipes/import \u2014 successful scrape (mocked)')

mock_scraper = MagicMock()
mock_scraper.title.return_value = 'Test Bolognese'
mock_scraper.total_time.return_value = 45
mock_scraper.yields.return_value = '6 servings'
mock_scraper.instructions.return_value = 'Step 1: Cook pasta.\nStep 2: Make sauce.'
mock_scraper.ingredients.return_value = [
    '1 lb ground beef',
    '2 tbsp olive oil',
    '3 cloves garlic, minced',
    '1 can crushed tomatoes',
]
mock_scraper.image.return_value = 'https://example.com/bolognese.jpg'

app_mod._import_cache.clear()

# Patch recipe_scrapers.scrape_me — the endpoint imports it *inside*
# the function body, so we must patch at the source module.
with patch('recipe_scrapers.scrape_me', return_value=mock_scraper) as mock_scrape:
    resp = client.post('/api/recipes/import',
                       json={'url': 'https://example.com/bolognese'})

    assert_eq(resp.status_code, 200, 'import returns 200 on success')
    d = resp.get_json() or {}

    recipe = d.get('recipe', {})
    assert_eq(recipe.get('title'), 'Test Bolognese', 'imported title correct')
    assert_eq(recipe.get('servings'), '6 servings', 'servings string preserved')
    assert_eq(recipe.get('total_time'), 45, 'total time parsed')
    assert_eq(recipe.get('source_url'), 'https://example.com/bolognese',
              'source_url echoed back')
    assert_eq(recipe.get('image_url'), 'https://example.com/bolognese.jpg',
              'image_url captured')

    instructions = recipe.get('instructions', '')
    assert_in('Cook pasta', instructions, 'instructions contain step 1')
    assert_in('Make sauce', instructions, 'instructions contain step 2')

    cache = d.get('cache', {})
    assert_eq(cache.get('status'), 'ok', 'cache status ok')
    assert_eq(cache.get('hit'), False, 'cache miss on first import')
    assert_eq(cache.get('age_seconds'), 0, 'age_seconds 0 on fresh import')

    mock_scrape.assert_called_once_with('https://example.com/bolognese')


# ===========================================================================
# 5. POST /api/recipes/import  —  scraper methods return None
# ===========================================================================
print('5. POST /api/recipes/import \u2014 handles None returns gracefully')

mock_scraper_none = MagicMock()
mock_scraper_none.title.return_value = 'Minimal Recipe'
mock_scraper_none.total_time.return_value = None
mock_scraper_none.yields.return_value = None
mock_scraper_none.instructions.return_value = None
mock_scraper_none.ingredients.return_value = ['just salt']
mock_scraper_none.image.return_value = None

# Clear import cache without needing app context (it's a plain dict).
app_mod._import_cache.clear()

with patch('recipe_scrapers.scrape_me', return_value=mock_scraper_none):
    resp = client.post('/api/recipes/import',
                       json={'url': 'https://example.com/minimal'})
    assert_eq(resp.status_code, 200, 'None returns still yield 200')
    d = resp.get_json() or {}
    recipe = d.get('recipe', {})
    assert_eq(recipe.get('title'), 'Minimal Recipe', 'title preserved')
    # yields=None goes into the response as-is (the raw scraper value).
    # _parse_recipe_yields(None)=4 only affects the DB servings column.
    assert_eq(recipe.get('total_time'), None, 'total_time None stored as-is')
    assert_eq(recipe.get('instructions'), None, 'instructions None preserved')
    assert_eq(recipe.get('image_url'), None, 'image_url None preserved')


# ===========================================================================
# 6. POST /api/recipes/import  —  cache hit (self-contained, no DB dependency)
# ===========================================================================
print('6. POST /api/recipes/import \u2014 cache hit returns cached data')

# Use a unique URL that no other test touches so the test is fully
# self-contained.  We exercise both cache-hit action paths:
#   a) no recipe in DB  → action = "created"
#   b) recipe exists    → action = "updated"
CACHE_URL = 'https://example.com/cache-test-only'

app_mod._import_cache.clear()
app_mod._import_cache[CACHE_URL] = {
    'ts': time.time(),
    'data': {
        'recipe': {
            'id': 99, 'title': 'Cached Recipe', 'servings': '2 servings',
            'total_time': 15, 'source_url': CACHE_URL,
            'image_url': 'https://example.com/cached.jpg',
            'instructions': 'Just reheat.',
        },
        'action': 'created',
    },
}

# --- Path (a): no recipe for this URL in DB → action stays "created" ---
with app.app_context():
    Recipe.query.filter_by(source_url=CACHE_URL).delete()
    db.session.commit()

with patch('recipe_scrapers.scrape_me', side_effect=RuntimeError('scraper should not be called on cache hit')) as mock_scrape:
    resp = client.post('/api/recipes/import', json={'url': CACHE_URL})
    assert_eq(resp.status_code, 200, 'cached import returns 200')
    d = resp.get_json() or {}
    cache = d.get('cache', {})
    assert_eq(cache.get('status'), 'ok', 'cache status ok on hit')
    assert_eq(cache.get('hit'), True, 'cache hit on cached URL')
    assert_truthy(cache.get('age_seconds', 0) >= 0, 'age_seconds non-negative')
    mock_scrape.assert_not_called()
    assert_eq(d.get('action'), 'created',
              'action = created when no recipe in DB')
    assert_eq(d['recipe']['title'], 'Cached Recipe', 'cached title preserved')

# --- Path (b): recipe now exists in DB → action becomes "updated" ---
with app.app_context():
    db.session.add(Recipe(
        title='Cached Recipe', servings=2, source_url=CACHE_URL,
        instructions='Just reheat.', recipe_scope=Recipe.SCOPE_HOUSEHOLD_PRIVATE,
        household_id=household_id(),
    ))
    db.session.commit()

# Refresh the cache entry so we don't hit the 300s TTL edge (ts must be fresh).
app_mod._import_cache[CACHE_URL]['ts'] = time.time()

with patch('recipe_scrapers.scrape_me') as mock_scrape2:
    resp2 = client.post('/api/recipes/import', json={'url': CACHE_URL})
    assert_eq(resp2.status_code, 200, 'cached import returns 200 (path b)')
    d2 = resp2.get_json() or {}
    assert_eq(d2.get('action'), 'updated',
              'action = updated when recipe exists in DB')
    mock_scrape2.assert_not_called()

# Clean up so this test doesn't leave a side-effect in DB.
with app.app_context():
    Recipe.query.filter_by(source_url=CACHE_URL).delete()
    db.session.commit()
    app_mod._import_cache.clear()


# ===========================================================================
# 7. POST /api/recipes/import  —  database persistence
# ===========================================================================
print('7. POST /api/recipes/import \u2014 imported recipe persisted to DB')

with app.app_context():
    recipe = Recipe.query.filter_by(title='Test Bolognese').first()
    assert_truthy(recipe is not None, 'recipe saved to DB')
    if recipe:
        assert_eq(recipe.servings, 6, 'servings parsed and persisted')
        assert_eq(recipe.instructions.count('Step'), 2,
                  'instructions persisted (2 steps)')

        ings = RecipeIngredient.query.filter_by(recipe_id=recipe.id).all()
        assert_eq(len(ings), 4, '4 ingredients persisted')
        assert_eq(ings[0].product_name, '1 lb ground beef',
                  'first ingredient stored verbatim')
        # 'ground' and 'crushed' are also qualifiers in the stripper,
        # so they get removed: 'ground beef' → 'beef', 'crushed tomatoes' → 'tomatoes'.
        keywords = [i.clean_keyword for i in ings]
        assert_in('beef', keywords, 'ground beef keyword → beef')
        assert_in('tomatoes', keywords, 'crushed tomatoes keyword → tomatoes')


# ===========================================================================
# 8. POST /api/recipes/import  —  re-import updates existing recipe by source_url
# ===========================================================================
print('8. POST /api/recipes/import \u2014 re-import same URL updates recipe')

app_mod._import_cache.clear()

with app.app_context():
    # First, verify the recipe from test 4 is in the DB with source_url set.
    recipe = Recipe.query.filter_by(title='Test Bolognese').first()
    assert_truthy(recipe is not None, 'recipe from test 4 exists in DB')
    assert_eq(recipe.source_url, 'https://example.com/bolognese',
              'source_url persisted')
    original_id = recipe.id
    original_ing_count = RecipeIngredient.query.filter_by(recipe_id=original_id).count()
    # We know test 4 created 4 ingredients.

# Re-import the same URL with updated scraper data (title changed, fewer ingredients).
mock_scraper_update = MagicMock()
mock_scraper_update.title.return_value = 'Test Bolognese V2'
mock_scraper_update.total_time.return_value = 35
mock_scraper_update.yields.return_value = '8 servings'
mock_scraper_update.instructions.return_value = 'Updated instructions.'
mock_scraper_update.ingredients.return_value = ['1 lb pasta', '2 cups sauce']
mock_scraper_update.image.return_value = 'https://example.com/bolognese-v2.jpg'

with patch('recipe_scrapers.scrape_me', return_value=mock_scraper_update) as mock_scrape:
    resp = client.post('/api/recipes/import',
                       json={'url': 'https://example.com/bolognese'})

    assert_eq(resp.status_code, 200, 're-import returns 200')
    d = resp.get_json() or {}

    recipe_out = d.get('recipe', {})
    assert_eq(recipe_out.get('title'), 'Test Bolognese V2',
              'title updated on re-import')
    assert_eq(recipe_out.get('servings'), '8 servings',
              'servings updated on re-import')
    assert_eq(recipe_out.get('total_time'), 35,
              'total_time updated on re-import')

    # Same ID — we updated the existing row, didn't create a duplicate.
    assert_eq(recipe_out.get('id'), original_id,
              'same recipe ID (updated, not duplicated)')

    # Action field tells the frontend this was an update.
    assert_eq(d.get('action'), 'updated',
              'action is "updated" on re-import')

    cache = d.get('cache', {})
    assert_eq(cache.get('hit'), False, 'cache miss (cleared above)')
    assert_eq(cache.get('age_seconds'), 0, 'fresh scrape age_seconds 0')

    mock_scrape.assert_called_once_with('https://example.com/bolognese')

# Verify DB state: old ingredients replaced with new ones.
with app.app_context():
    recipe = Recipe.query.get(original_id)
    assert_eq(recipe.title, 'Test Bolognese V2',
              'DB title updated')
    assert_eq(recipe.servings, 8, 'DB servings updated')
    assert_eq(recipe.source_url, 'https://example.com/bolognese',
              'DB source_url unchanged')

    ings = RecipeIngredient.query.filter_by(recipe_id=original_id).all()
    assert_eq(len(ings), 2, 'old ingredients replaced; only 2 new ones')
    # Also verify no duplicate recipe was created.
    count = Recipe.query.filter_by(source_url='https://example.com/bolognese').count()
    assert_eq(count, 1, 'only one recipe row for this source_url')


# ===========================================================================
# 9. POST /api/recipes/import  —  scrape failure (RuntimeError)
# ===========================================================================
print('9. POST /api/recipes/import \u2014 scrape failure returns 500')

with app.app_context():
    app_mod._import_cache.clear()

with patch('recipe_scrapers.scrape_me',
           side_effect=RuntimeError('403 Forbidden')):
    resp = client.post('/api/recipes/import',
                       json={'url': 'https://blocked.example.com/secret'})
    assert_eq(resp.status_code, 500, 'scrape error returns 500')
    d = resp.get_json() or {}
    assert_eq(d.get('error'), 'Could not scrape that URL.',
              'generic error message')
    assert_in('403 Forbidden', d.get('detail', ''),
              'detail includes original error')
    assert_eq(d.get('url'), 'https://blocked.example.com/secret',
              'url field echoed')
    cache = d.get('cache', {})
    assert_eq(cache.get('status'), 'error', 'cache status error on failure')
    assert_eq(cache.get('hit'), False, 'cache hit false on failure')
    assert_in('403 Forbidden', cache.get('error_detail', ''),
              'cache error_detail has original message')


# ===========================================================================
# 10. POST /api/recipes/import  —  ImportError (library not installed)
# ===========================================================================
print('10. POST /api/recipes/import \u2014 ImportError returns 501')

app_mod._import_cache.clear()

# The endpoint does ``from recipe_scrapers import scrape_me`` inside
# the function body.  We need the *import* to fail.  Hooking
# builtins.__import__ is the only reliable way to block the import
# since recipe_scrapers is actually installed and Python can find it
# even after evicting sys.modules.
import builtins
_orig_import = builtins.__import__


def _block_recipe_scrapers(name, *args, **kwargs):
    if name == 'recipe_scrapers' or name.startswith('recipe_scrapers.'):
        raise ImportError('No module named recipe_scrapers')
    return _orig_import(name, *args, **kwargs)


builtins.__import__ = _block_recipe_scrapers
try:
    resp = client.post('/api/recipes/import',
                       json={'url': 'https://example.com/any'})
    assert_eq(resp.status_code, 501, 'ImportError returns 501')
    d = resp.get_json() or {}
    assert_in('recipe-scrapers', d.get('error', ''),
              'error message mentions recipe-scrapers')
    assert_eq(d.get('cache', {}).get('status'), 'error',
              'cache status is error')
finally:
    builtins.__import__ = _orig_import


# ===========================================================================
# 11. POST /api/recipes/import  —  different URL returns fresh
# ===========================================================================
print('11. POST /api/recipes/import \u2014 different URL is fresh scrape')

mock_scraper2 = MagicMock()
mock_scraper2.title.return_value = 'Different Recipe'
mock_scraper2.total_time.return_value = 10
mock_scraper2.yields.return_value = '2 servings'
mock_scraper2.instructions.return_value = 'Mix everything.'
mock_scraper2.ingredients.return_value = ['1 cup flour']
mock_scraper2.image.return_value = ''

with patch('recipe_scrapers.scrape_me', return_value=mock_scraper2) as mock_scrape:
    resp = client.post('/api/recipes/import',
                       json={'url': 'https://example.com/different'})
    assert_eq(resp.status_code, 200, 'different URL returns 200')
    d = resp.get_json() or {}
    assert_eq(d['recipe']['title'], 'Different Recipe',
              'different URL scrapes fresh')
    cache = d.get('cache', {})
    assert_eq(cache.get('hit'), False, 'different URL is cache miss')
    assert_eq(cache.get('age_seconds'), 0, 'fresh scrape age_seconds 0')
    mock_scrape.assert_called_once_with('https://example.com/different')


# ===========================================================================
# 12. _derive_clean_keyword  —  edge cases
# ===========================================================================
print('12. _derive_clean_keyword \u2014 edge cases')

edge_cases = [
    ('',         ''),
    ('Salt',     'salt'),
    ('Pepper',   'pepper'),
    ('Eggs',     'eggs'),
    ('Sea salt and freshly ground black pepper', 'black_pepper'),
    ('  2   cups    flour   ', 'flour'),
]
for inp, exp in edge_cases:
    got = _derive_clean_keyword(inp)
    assert_eq(got, exp, 'keyword_edge(' + repr(inp) + ')')


# ===========================================================================
# 12b. _derive_clean_keyword  —  conjunction/connector guard
#      Keywords must never begin with "and_", "or_", "with_", or similar
#      meaningless connector tokens.
# ===========================================================================
print('12b. _derive_clean_keyword \u2014 connector guard')

_CONNECTOR_PREFIX = ('and_', 'or_', 'with_', 'plus_', 'for_', 'of_')

def _assert_no_connector_prefix(kw, label):
    """Fail if kw starts with a meaningless connector token."""
    for pfx in _CONNECTOR_PREFIX:
        assert_truthy(not kw.startswith(pfx),
                      f'{label}: keyword {kw!r} must not start with {pfx!r}')

connector_cases = [
    # (input,                          expected_keyword)
    ('salt and pepper to taste',       'pepper'),
    ('1/2 stick unsalted butter',      'unsalted_butter'),
    ('extra virgin olive oil',         'olive_oil'),
    ('boneless skinless chicken breast', 'chicken_breast'),
    ('truffle oil',                    'truffle_oil'),
    ('salt',                           'salt'),
    ('black pepper',                   'black_pepper'),
]
for inp, exp in connector_cases:
    got = _derive_clean_keyword(inp)
    assert_eq(got, exp, 'keyword_connector(' + repr(inp) + ')')
    _assert_no_connector_prefix(got, 'keyword_connector(' + repr(inp) + ')')


# ===========================================================================
# 13. _scrape_with_curl_cffi  —  JSON-LD parser unit tests (mocked HTTP)
# ===========================================================================
print('13. _scrape_with_curl_cffi \u2014 basic JSON-LD Recipe extraction')

def _make_cffi_mock(status=200, html=''):
    """Return a MagicMock that mimics curl_cffi.requests.get()."""
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.text = html
    mock_get = MagicMock(return_value=mock_resp)
    return mock_get

SIMPLE_RECIPE_JSON = '''
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "Recipe",
  "name": "Simple Pasta",
  "recipeIngredient": ["1 lb pasta", "2 cups sauce"],
  "recipeInstructions": [
    {"@type": "HowToStep", "text": "Boil water."},
    {"@type": "HowToStep", "text": "Cook pasta."}
  ],
  "image": {"url": "https://example.com/pasta.jpg"},
  "totalTime": "PT30M",
  "recipeYield": "4 servings"
}
</script>
'''

mock_get = _make_cffi_mock(html=SIMPLE_RECIPE_JSON)
with patch('curl_cffi.requests.get', mock_get):
    result = _scrape_with_curl_cffi('https://example.com/pasta')
    assert_truthy(result is not None, 'returns non-None for valid JSON-LD')
    assert_eq(result['title'], 'Simple Pasta', 'title extracted')
    assert_eq(result['total_time'], 30, 'totalTime PT30M parsed to 30')
    assert_eq(result['yields_str'], '4 servings', 'yields extracted')
    assert_eq(result['image_url'], 'https://example.com/pasta.jpg', 'image extracted')
    assert_eq(len(result['raw_ingredients']), 2, '2 ingredients')
    assert_eq(result['raw_ingredients'][0], '1 lb pasta', 'first ingredient')
    assert_in('Boil water', result['instructions'], 'instructions have step 1')
    assert_in('Cook pasta', result['instructions'], 'instructions have step 2')
    assert_eq(result['source'], 'json-ld', 'source marked json-ld')


print()
print('13b. _scrape_with_curl_cffi \u2014 @type as array ["Recipe", "NewsArticle"]')

ALLRECIPES_STYLE = '''
<script type="application/ld+json">
[{
  "@context": "http://schema.org",
  "@type": ["Recipe", "NewsArticle"],
  "headline": "Chicken Parmesan",
  "recipeIngredient": ["4 chicken breasts", "1 cup cheese"],
  "recipeInstructions": "Bake at 350 for 30 min.",
  "image": "https://example.com/chicken.jpg",
  "cookTime": "PT20M",
  "recipeYield": 4
}]
</script>
'''

mock_get2 = _make_cffi_mock(html=ALLRECIPES_STYLE)
with patch('curl_cffi.requests.get', mock_get2):
    result = _scrape_with_curl_cffi('https://www.allrecipes.com/recipe/123')
    assert_truthy(result is not None, 'returns non-None for array @type')
    assert_eq(result['title'], 'Chicken Parmesan', 'headline used as title')
    assert_eq(result['total_time'], 20, 'cookTime PT20M parsed')
    assert_eq(result['yields_str'], '4', 'numeric yields cast to string')
    assert_eq(len(result['raw_ingredients']), 2, '2 ingredients')
    # Instructions as plain string (not list)
    assert_in('Bake at 350', result['instructions'], 'string instructions preserved')


print()
print('13c. _scrape_with_curl_cffi \u2014 ISO 8601 duration parsing')

def _test_duration(iso_str, expected_mins, label):
    html = '''<script type="application/ld+json">
    {"@context":"http://schema.org","@type":"Recipe","name":"T",
     "recipeIngredient":[],"totalTime":"''' + iso_str + '''"}
    </script>'''
    mock = _make_cffi_mock(html=html)
    with patch('curl_cffi.requests.get', mock):
        r = _scrape_with_curl_cffi('https://x.com/t')
        assert_eq(r['total_time'], expected_mins, label)

_test_duration('PT10M', 10, 'PT10M → 10')
_test_duration('PT1H30M', 90, 'PT1H30M → 90')
_test_duration('PT2H', 120, 'PT2H → 120')
_test_duration('P0D', None, 'P0D (0 mins) → None')


print()
print('13d. _scrape_with_curl_cffi \u2014 missing optional fields')

NO_OPTIONALS = '''
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"Recipe","name":"Bare Minimum",
 "recipeIngredient":["just salt"]}
</script>
'''

mock_no = _make_cffi_mock(html=NO_OPTIONALS)
with patch('curl_cffi.requests.get', mock_no):
    result = _scrape_with_curl_cffi('https://x.com/bare')
    assert_truthy(result is not None, 'returns non-None for minimal recipe')
    assert_eq(result['title'], 'Bare Minimum', 'title from name')
    assert_eq(result['total_time'], None, 'missing totalTime → None')
    assert_eq(result['yields_str'], None, 'missing recipeYield → None')
    assert_eq(result['image_url'], None, 'missing image → None')
    assert_eq(result['instructions'], '', 'missing instructions → empty string')
    assert_eq(len(result['raw_ingredients']), 1, '1 ingredient')


print()
print('13e. _scrape_with_curl_cffi \u2014 instructions as HowToStep + string hybrid')

HOWTO_INSTRUCTIONS = '''
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"Recipe","name":"Soup",
 "recipeIngredient":["water"],
 "recipeInstructions":[
   {"@type":"HowToStep","text":"Step one."},
   "Step two (plain string).",
   {"@type":"HowToStep","text":"Step three."}
 ]}
</script>
'''

mock_how = _make_cffi_mock(html=HOWTO_INSTRUCTIONS)
with patch('curl_cffi.requests.get', mock_how):
    result = _scrape_with_curl_cffi('https://x.com/soup')
    lines = result['instructions'].split('\n')
    assert_eq(len(lines), 3, '3 instruction lines')
    assert_eq(lines[0], 'Step one.', 'HowToStep text extracted')
    assert_eq(lines[1], 'Step two (plain string).', 'plain string step preserved')
    assert_eq(lines[2], 'Step three.', 'second HowToStep text extracted')


print()
print('13f. _scrape_with_curl_cffi \u2014 image variants')

def _test_image(image_json, expected_url, label):
    html = '''<script type="application/ld+json">
    {"@context":"http://schema.org","@type":"Recipe","name":"I",
     "recipeIngredient":[],"image":''' + image_json + '''}
    </script>'''
    mock = _make_cffi_mock(html=html)
    with patch('curl_cffi.requests.get', mock):
        r = _scrape_with_curl_cffi('https://x.com/img')
        assert_eq(r['image_url'], expected_url, label)

_test_image('{"url":"https://x.com/a.jpg"}', 'https://x.com/a.jpg', 'image dict with url')
_test_image('[{"url":"https://x.com/b.jpg"}]', 'https://x.com/b.jpg', 'image list of dicts → first url')
_test_image('"https://x.com/c.jpg"', 'https://x.com/c.jpg', 'image as plain string URL')


print()
print('13g. _scrape_with_curl_cffi \u2014 error / no-data paths')

NO_RECIPE = '<script type="application/ld+json">{"@type":"WebPage","name":"Not a recipe"}</script>'
mock_nr = _make_cffi_mock(html=NO_RECIPE)
with patch('curl_cffi.requests.get', mock_nr):
    result = _scrape_with_curl_cffi('https://x.com/nope')
    assert_eq(result, None, 'no Recipe in JSON-LD → None')

NO_JSONLD = '<html><body>Just a page, no structured data.</body></html>'
mock_nj = _make_cffi_mock(html=NO_JSONLD)
with patch('curl_cffi.requests.get', mock_nj):
    result = _scrape_with_curl_cffi('https://x.com/plain')
    assert_eq(result, None, 'no JSON-LD blocks → None')

mock_404 = _make_cffi_mock(status=404, html='Not Found')
with patch('curl_cffi.requests.get', mock_404):
    result = _scrape_with_curl_cffi('https://x.com/404')
    assert_eq(result, None, 'non-200 status → None')

with patch('curl_cffi.requests.get', side_effect=ConnectionError('timeout')):
    result = _scrape_with_curl_cffi('https://x.com/timeout')
    assert_eq(result, None, 'HTTP exception → None')


print()
print('13h. _scrape_with_curl_cffi \u2014 single ingredient as string')

SINGLE_INGREDIENT = '''
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"Recipe","name":"One Ing",
 "recipeIngredient":"1 cup love"}
</script>
'''

mock_si = _make_cffi_mock(html=SINGLE_INGREDIENT)
with patch('curl_cffi.requests.get', mock_si):
    result = _scrape_with_curl_cffi('https://x.com/one')
    assert_eq(len(result['raw_ingredients']), 1, 'string ingredient wrapped in list')
    assert_eq(result['raw_ingredients'][0], '1 cup love', 'ingredient preserved')


# ===========================================================================
# 14. Live guard: recipe-scrapers importable (no network)
# ===========================================================================
print('14. recipe-scrapers importable from venv (offline guard)')
try:
    from recipe_scrapers import scrape_me  # noqa: F401
    assert_truthy(True, 'recipe_scrapers.scrape_me importable')
except ImportError:
    assert_truthy(False, 'recipe_scrapers.scrape_me importable')


# ===========================================================================
# SUMMARY
# ===========================================================================
def _main():
    print('\n{} passed, {} failed'.format(passed, failed))
    sys.exit(1 if failed > 0 else 0)


def test_import_script_checks() -> None:
    assert failed == 0, f"import script checks failed: {failed}"


if __name__ == '__main__':
    _main()
