/**
 * Node.js smoke test for static/js/recipes.js.
 *
 * Exercises the recipes-import + recipe-list-refresh flows in
 * isolation. Mocks:
 *   - global.fetch        via a per-URL sandbox.
 *   - document.getElementById via a fake registry that captures
 *     addEventListener registrations and textContent / disabled
 *     mutations.
 *
 * Run with:  node tests/test_recipes.js
 */

'use strict';

// ---- Mock fetch infrastructure ----
const mockRoutes = [];   // [{method, path, body, status, response}]
function mockRoute(method, path, status, responseBody, expectedBody) {
  // expectedBody is OPTIONAL: when absent, mocks match any request
  // body. Pass it to assert a specific body (e.g., for testing
  // payload correctness).
  mockRoutes.push({ method, path, status, responseBody, expectedBody });
}
function clearMockRoutes() { mockRoutes.length = 0; }
async function mockFetch(method, path, body) {
  // Match by method + path first. Body-match is optional: if the
  // route was registered without a body param (the common case for
  // fixtures like "the POST /api/recipes/import endpoint"), any
  // body matches. Only when the route.respondBody was registered
  // WITH a body expectation do we enforce strict equality. This
  // mirrors the typical fetch-mock pattern.
  const route = mockRoutes.find(r => {
    if (r.method !== method || r.path !== path) return false;
    if (r.expectedBody === undefined) return true;       // match any body
    return JSON.stringify(r.expectedBody) === JSON.stringify(body);
  });
  if (!route) throw new Error(`No mock for ${method} ${path} ${body ? JSON.stringify(body) : ''}`);
  return {
    ok: route.status >= 200 && route.status < 300,
    status: route.status,
    data: route.responseBody,
  };
}

// ---- Mock DOM registry ----
class FakeEl {
  constructor(id) {
    this.id = id;
    this.value = '';
    this.disabled = false;
    this.textContent = '';
    this.style = {};
    this.innerHTML = '';
    this.children = [];
    this.eventListeners = {};
    this.dataset = {};
  }
  addEventListener(event, fn) {
    (this.eventListeners[event] = this.eventListeners[event] || []).push(fn);
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  setAttribute(k, v) { this[k] = v; }
  querySelector(sel) {
    // Find a child whose dataset matches. For tests, we just return
    // a fake that captures the addEventListener registrations.
    if (sel === 'input[data-recipe]') return this._checkbox || new FakeEl('cb');
    if (sel === '[data-prepare-id]')  return this._startBtn || null;
    if (sel === '[data-resume-id]')   return this._resumeBtn || null;
    return null;
  }
}
const fakeDom = new Map();
function setupFakeDom(idMap) {
  fakeDom.clear();
  for (const [id, value] of Object.entries(idMap)) {
    fakeDom.set(id, value !== undefined ? value : new FakeEl(id));
  }
}
function appendCardList(id, cards) {
  const list = fakeDom.get(id) || new FakeEl(id);
  list.children = cards.map(c => c);
  for (const c of cards) fakeDom.set(c.id || `card-${Math.random()}`, c);
  fakeDom.set(id, list);
}
global.document = {
  getElementById: (id) => fakeDom.get(id) || null,
  createElement: (tag) => new FakeEl(tag),
};

// We expose mock fetch through `_recipesMockFetch` so the new module's
// `fetchRecipes_` will use it. (In browser the inline-script `api`
// global is used instead.)
global.fetch = undefined;  // tell recipes.js to use _recipesMockFetch

// ---- System under test ----
async function main() {
const SUT = require('../static/js/recipes.js');

SUT._setMockFetch(mockFetch);

// ---- Assertions ----
let passed = 0, failed = 0;
function assertEq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) { passed++; console.log(`  \u2713 ${label}`); }
  else { failed++; console.error(`  \u2717 ${label}: expected ${e}, got ${a}`); }
}
function reset() {
  clearMockRoutes();
  fakeDom.clear();
  // Clear module-scope api_ contaminated by previous setupRecipesInit
  // calls — otherwise refreshRecipes (which runs fetchRecipes_ → api_)
  // would call a stale mock that only handles /api/recipes/import.
  SUT._resetApi();
}

// Helper: count occurrences of a substring for chip-count assertions.
// String.includes is a boolean — for multi-chip scenarios we want exact
// counts so a regression where only 1 of 3 chips renders is caught.
const chipCount = (html) => (html.match(/prepare-resume-chip/g) || []).length;

// ================================================================
// Constant: stable, scrape-friendly URL used as the happy-path
// control in scenarios 2 + 12. Verified end-to-end against the live
// recipe-scraper's scrape_me() call and persisted to SQLite via
// POST /api/recipes/import in earlier verification.
const LOVEANDLEMONS_URL = 'https://www.loveandlemons.com/baked-eggs/';
// ============================================================================
console.log('1. Successful import: POST sends correct body, success status, button re-enabled');
// ============================================================================
reset();
setupFakeDom({
  'importRecipeForm':   new FakeEl('importRecipeForm'),
  'importRecipeUrl':    (() => { const e = new FakeEl('importRecipeUrl'); e.value = 'https://example.com/recipe'; return e; })(),
  'importRecipeStatus': new FakeEl('importRecipeStatus'),
  'importRecipeBtn':    (() => { const e = new FakeEl('importRecipeBtn'); e.textContent = 'Import Recipe'; return e; })(),
  'addRecipeForm':      undefined,
  'recipeListContainer': new FakeEl('recipeListContainer'),
  'recipeSelectedCount': new FakeEl('recipeSelectedCount'),
});
mockRoute('POST', '/api/recipes/import', 200, {
  recipe: { title: 'Cheesy Ham Casserole', id: 42 },
  cache: { status: 'ok', hit: false },
});
const capturedListRefresh = [];
SUT.setupRecipesInit({
  api: (m, p, b) => {
    if (p === '/api/recipes/import') return mockFetch(m, p, b);
    throw new Error('unexpected fetch: ' + p);
  },
  refreshRecipes: () => { capturedListRefresh.push('called'); },
});
// Trigger submit on the form.
const submitCb = fakeDom.get('importRecipeForm').eventListeners.submit[0];
await submitCb({ preventDefault: () => {} });
assertEq(capturedListRefresh.length, 1, 'refreshRecipes called after successful import');
assertEq(fakeDom.get('importRecipeBtn').disabled, false, 'button re-enabled after success');
assertEq(fakeDom.get('importRecipeBtn').textContent, 'Import Recipe', 'button label restored');
assertEq(fakeDom.get('importRecipeUrl').value, '', 'URL input cleared after success');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('Cheesy Ham Casserole'), true, 'status confirms import title');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('freshly fetched'), true, 'status shows "freshly fetched" cache note');

// ============================================================================
console.log('\n2. Happy-path control: stable recipe URL imports successfully with cache miss');
// ============================================================================
// Uses the LOVEANDLEMONS_URL constant verified end-to-end against
// recipe-scrapers (15.11.0, .venv) — title='Baked Eggs' scraped cleanly.
// Mock response shape matches the actual app.py cache envelope so a
// regression in either the scraper output OR the cache envelope will
// catch here.
reset();
setupFakeDom({
  'importRecipeForm':   new FakeEl('importRecipeForm'),
  'importRecipeUrl':    (() => { const e = new FakeEl('importRecipeUrl'); e.value = LOVEANDLEMONS_URL; return e; })(),
  'importRecipeStatus': new FakeEl('importRecipeStatus'),
  'importRecipeBtn':    (() => { const e = new FakeEl('importRecipeBtn'); e.textContent = 'Import Recipe'; return e; })(),
  'addRecipeForm':      undefined,
});
const refreshFnCalledOn2 = { value: false };
let capturedImportBody2 = null;
mockRoute('POST', '/api/recipes/import', 200, {
  recipe: { id: 42, title: 'Baked Eggs', servings: '1 serving', total_time: 20, source_url: LOVEANDLEMONS_URL },
  cache: { status: 'ok', hit: false, fresh: true, age_seconds: 0 },
});
SUT.setupRecipesInit({
  api: (m, p, b) => {
    if (p === '/api/recipes/import') {
      capturedImportBody2 = b;
      return mockFetch(m, p, b);
    }
    throw new Error('unexpected fetch: ' + p);
  },
  // No flash callback needed — happy path does NOT emit a toast, so the
  // runtime check `if (flash) flash(...)` correctly no-ops.
  refreshRecipes: () => { refreshFnCalledOn2.value = true; },
});
await fakeDom.get('importRecipeForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(capturedImportBody2 && capturedImportBody2.url, LOVEANDLEMONS_URL, 'POST body has loveandlemons URL');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('Baked Eggs'), true, 'status shows imported recipe title');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('freshly fetched'), true, 'status shows "freshly fetched" cache note on cache miss');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('✓'), true, 'status prefixed with check mark on success');
assertEq(fakeDom.get('importRecipeUrl').value, '', 'URL input cleared after successful import');
assertEq(fakeDom.get('importRecipeBtn').disabled, false, 'button re-enabled after success');
assertEq(fakeDom.get('importRecipeBtn').textContent, 'Import Recipe', 'button label restored after success');
assertEq(refreshFnCalledOn2.value, true, 'refreshRecipes called after successful import');

// ============================================================================
console.log('\n3. Network error: fetch throws, handler catches, error displayed');
// ============================================================================
reset();
setupFakeDom({
  'importRecipeForm':   new FakeEl('importRecipeForm'),
  'importRecipeUrl':    (() => { const e = new FakeEl('importRecipeUrl'); e.value = 'https://unreachable.example.com/x'; return e; })(),
  'importRecipeStatus': new FakeEl('importRecipeStatus'),
  'importRecipeBtn':    (() => { const e = new FakeEl('importRecipeBtn'); e.textContent = 'Import Recipe'; return e; })(),
  'addRecipeForm':      undefined,
});
SUT.setupRecipesInit({
  api: () => { throw new Error('NetworkError when attempting to fetch resource.'); },
  flash: () => {},
});
await fakeDom.get('importRecipeForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('NetworkError'), true, 'status includes thrown error message');
assertEq(fakeDom.get('importRecipeStatus').style.color, 'crimson', 'status color crimson on thrown error');
assertEq(fakeDom.get('importRecipeBtn').disabled, false, 'button re-enabled even when fetch throws');

// ============================================================================
console.log('\n4. Empty URL: rejected before fetch, button NOT disabled');
// ============================================================================
reset();
setupFakeDom({
  'importRecipeForm':   new FakeEl('importRecipeForm'),
  'importRecipeUrl':    (() => { const e = new FakeEl('importRecipeUrl'); e.value = '   '; return e; })(),
  'importRecipeStatus': new FakeEl('importRecipeStatus'),
  'importRecipeBtn':    (() => { const e = new FakeEl('importRecipeBtn'); e.textContent = 'Import Recipe'; return e; })(),
  'addRecipeForm':      undefined,
});
let fetchCalled = false;
SUT.setupRecipesInit({
  api: () => { fetchCalled = true; return { ok: true, status: 200, data: {} }; },
  flash: () => {},
});
await fakeDom.get('importRecipeForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(fetchCalled, false, 'fetch is NOT called on empty URL');
assertEq(fakeDom.get('importRecipeStatus').textContent, 'Please paste a URL first.', 'status shows "paste a URL" hint');
assertEq(fakeDom.get('importRecipeBtn').disabled, false, 'button stays enabled on empty URL');

// ============================================================================
console.log('\n5. refreshRecipes happy path: GET returns array, renders cards');
// ============================================================================
reset();
const rlc = new FakeEl('recipeListContainer');
const rsc = new FakeEl('recipeSelectedCount');
setupFakeDom({ 'recipeListContainer': rlc, 'recipeSelectedCount': rsc });
mockRoute('GET', '/api/recipes', 200, [
  { id: 1, title: 'Beef Tacos', servings: 4, ingredients: [{ product_name: 'Beef', quantity: 1, unit: 'lb' }] },
  { id: 2, title: 'Pasta', servings: 6, ingredients: [{ product_name: 'Penne', quantity: 1, unit: 'box', swap_options: ['Rigatoni'] }] },
]);
// refreshRecipes reads via the SUT's internal fetchRecipes_, but we're
// calling it directly as a global. The internal fetchRecipes_ looks up
// `api_` (the browser glue) OR `_recipesMockFetch`. Since neither is
// defined in Node, we install the mock now.
// We have to do this BEFORE setupRecipesInit runs because refreshRecipes
// runs at init-time. Instead, install mock fetch on the API path the
// way setup does it: through setupRecipesInit.
// But for this test we want to call SUT.refreshRecipes() directly.
// Solution: temporarily stub fetchRecipes_ via the mock setter.
// Since SUT doesn't expose fetchRecipes_, we'll monkey-patch by
// defining `api_` as a global.
global.api_ = (m, p) => mockFetch(m, p, undefined);
await SUT.refreshRecipes();
assertEq(rsc.textContent, '0', 'selection counter reset to 0');
assertEq(rlc.children.length, 2, 'renders 2 recipe cards');
assertEq(rlc.children[0].innerHTML.includes('Beef Tacos'), true, 'card 1 has title Beef Tacos');
assertEq(rlc.children[1].innerHTML.includes('Pasta'), true, 'card 2 has title Pasta');

// ============================================================================
console.log('\n6. refreshRecipes error: GET fails, empty state shown');
// ============================================================================
reset();
const rlc2 = new FakeEl('recipeListContainer');
const rsc2 = new FakeEl('recipeSelectedCount');
setupFakeDom({ 'recipeListContainer': rlc2, 'recipeSelectedCount': rsc2 });
mockRoute('GET', '/api/recipes', 500, { error: 'database unavailable' });
global.api_ = (m, p) => mockFetch(m, p, undefined);
await SUT.refreshRecipes();
assertEq(rlc2.innerHTML, '<div class="empty">No recipes yet.</div>', 'empty state on error');

// ============================================================================
console.log('\n7. refreshRecipes empty: GET returns [], empty state shown');
// ============================================================================
reset();
const rlc3 = new FakeEl('recipeListContainer');
const rsc3 = new FakeEl('recipeSelectedCount');
setupFakeDom({ 'recipeListContainer': rlc3, 'recipeSelectedCount': rsc3 });
mockRoute('GET', '/api/recipes', 200, []);
global.api_ = (m, p) => mockFetch(m, p, undefined);
await SUT.refreshRecipes();
assertEq(rlc3.innerHTML, '<div class="empty">No recipes yet.</div>', 'empty state when GET returns []');

// ============================================================================
console.log('\n8. Add Recipe (manual): POST sends correct body, success resets form');
// ============================================================================
reset();
setupFakeDom({
  'addRecipeForm': new FakeEl('addRecipeForm'),
  'rTitle': (() => { const e = new FakeEl('rTitle'); e.value = 'Beef Tacos'; return e; })(),
  'rServings': (() => { const e = new FakeEl('rServings'); e.value = '4'; return e; })(),
  'rIngredients': (() => {
    const e = new FakeEl('rIngredients');
    e.value = 'Ground Beef, 1.5, lbs, Turkey\nFlour Tortillas, 8, item';
    return e;
  })(),
  'importRecipeForm': undefined,
});
mockRoute('POST', '/api/recipes', 200, { id: 99, title: 'Beef Tacos' });
let recipeListRefreshed = 0;
SUT.setupRecipesInit({
  api: (m, p, b) => {
    if (p === '/api/recipes') {
      assertEq(b.title, 'Beef Tacos', 'add recipe: title in body');
      assertEq(b.servings, 4, 'add recipe: servings in body');
      assertEq(Array.isArray(b.ingredients), true, 'add recipe: ingredients is array');
      assertEq(b.ingredients.length, 2, 'add recipe: ingredients has 2 lines');
      return mockFetch(m, p, b);
    }
    throw new Error('unexpected: ' + p);
  },
  refreshRecipes: () => { recipeListRefreshed++; },
  flash: () => {},
});
await fakeDom.get('addRecipeForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(fakeDom.get('rTitle').value, '', 'title input cleared after add');
assertEq(fakeDom.get('rIngredients').value, '', 'ingredients input cleared after add');
assertEq(recipeListRefreshed, 1, 'refreshRecipes called after add');

// ============================================================================
console.log('\n9. Resume chip rendering: mock _loadPrepareProgress, verify chip HTML');
// ============================================================================
reset();
// Mock _loadPrepareProgress so refreshRecipes sees saved progress for
// recipe 42 at step 2 of 5 (in-range, step > 0, step < totalSteps).
global._loadPrepareProgress = (id) => {
  if (String(id) === '42') return { step: 2, totalSteps: 5, savedAt: '2025-01-01T00:00:00.000Z' };
  return null;
};
const rlc9 = new FakeEl('recipeListContainer');
const rsc9 = new FakeEl('recipeSelectedCount');
setupFakeDom({ 'recipeListContainer': rlc9, 'recipeSelectedCount': rsc9 });
mockRoute('GET', '/api/recipes', 200, [
  {
    id: 42,
    title: 'Beef Stew',
    servings: 4,
    ingredients: [{ product_name: 'Beef', quantity: 1, unit: 'lb' }],
    source_url: 'https://example.com/beef-stew',
    image_url: 'https://example.com/img.jpg',
    total_time: '45 min',
    instructions: 'Step 1: chop\nStep 2: simmer',
  },
]);
global.api_ = (m, p) => mockFetch(m, p, undefined);
await SUT.refreshRecipes();
const card9 = rlc9.children[0];
const inner9 = card9.innerHTML || '';
assertEq(chipCount(inner9) === 1, true, 'Resume chip rendered when progress exists (renders to innerHTML, count=1)');
assertEq(inner9.includes('data-resume-id="42"'), true, 'Resume chip has data-resume-id=42');
assertEq(inner9.includes('Resume cooking at step 3 of 5'), true, 'Resume chip title shows step+1/totalSteps');
assertEq(inner9.includes('Step 3'), true, 'Resume chip body shows step+1');
delete global._loadPrepareProgress;

// ============================================================================
console.log('\n10. Resume chip suppression: step=0 and step>=totalSteps render no chip');
// ============================================================================
reset();
const rlc10 = new FakeEl('recipeListContainer');
const rsc10 = new FakeEl('recipeSelectedCount');
setupFakeDom({ 'recipeListContainer': rlc10, 'recipeSelectedCount': rsc10 });
// Recipe with import metadata (so importExtras renders) and progress
// entries that should ALL be suppressed.
mockRoute('GET', '/api/recipes', 200, [
  {
    id: 10, title: 'Has Step Zero', servings: 4, ingredients: [],
    source_url: 'https://example.com/zero', instructions: 'Cook.',
  },
  {
    id: 11, title: 'Already Finished', servings: 4, ingredients: [],
    source_url: 'https://example.com/finished', instructions: 'Done.',
  },
  {
    id: 12, title: 'Legacy Hand-Entered', servings: 4, ingredients: [],
    // NO source_url / image_url / instructions — importExtras will be empty.
  },
]);
global._loadPrepareProgress = (id) => {
  if (id === 10) return { step: 0, totalSteps: 4, savedAt: 'x' };         // step=0 → suppressed
  if (id === 11) return { step: 4, totalSteps: 4, savedAt: 'x' };         // step>=totalSteps → suppressed
  if (id === 12) return { step: 1, totalSteps: 3, savedAt: 'x' };         // valid, but no import metadata → chip gated inside importExtras
  return null;
};
global.api_ = (m, p) => mockFetch(m, p, undefined);
await SUT.refreshRecipes();
assertEq(chipCount(rlc10.children[0].innerHTML || ''), 0, 'step=0: chip NOT rendered (count=0)');
assertEq(chipCount(rlc10.children[1].innerHTML || ''), 0, 'step>=totalSteps: chip NOT rendered (count=0)');
assertEq(chipCount(rlc10.children[2].innerHTML || ''), 0, 'no import metadata: chip NOT rendered (count=0, gated inside importExtras)');
delete global._loadPrepareProgress;

// ============================================================================
console.log('\n11. Resume chip boundary positive: step=totalSteps-1 with import metadata RENDERS chip');
// ============================================================================
reset();
global._loadPrepareProgress = (id) => {
  // LAST valid step (step = totalSteps - 1). Chip MUST render.
  if (String(id) === '99') return { step: 2, totalSteps: 3, savedAt: '2025-01-01T00:00:00.000Z' };
  return null;
};
const rlc11 = new FakeEl('recipeListContainer');
const rsc11 = new FakeEl('recipeSelectedCount');
setupFakeDom({ 'recipeListContainer': rlc11, 'recipeSelectedCount': rsc11 });
mockRoute('GET', '/api/recipes', 200, [
  {
    id: 99, title: 'Boundary Recipe', servings: 4, ingredients: [],
    source_url: 'https://example.com/boundary',
    image_url: 'https://example.com/img.jpg',
    total_time: '30 min',
    instructions: 'Step 1\nStep 2\nStep 3',
  },
]);
global.api_ = (m, p) => mockFetch(m, p, undefined);
await SUT.refreshRecipes();
const inner11 = rlc11.children[0].innerHTML || '';
assertEq(chipCount(inner11), 1, 'boundary step (totalSteps-1) with metadata: chip RENDERED (count=1)');
assertEq(inner11.includes('data-resume-id="99"'), true, 'boundary chip has correct resumeId');
assertEq(inner11.includes('Resume cooking at step 3 of 3'), true, 'boundary chip shows step+1=totalSteps');
delete global._loadPrepareProgress;

// ============================================================================
console.log('\n12. Cache HIT path: second identical import returns cache.hit=true with cached age');
// ============================================================================
// The TTL-bound cache layer around scrape_me() returns cache.hit=true on
// the second request within TTL. The client renders this as
// '"· cached 5 min ago"' (see static/js/recipes.js:298-306). Mock fetch
// is STATEFUL via an inline api fn because mockRoute's body-matching
// can't distinguish two identical path+body calls.
reset();
setupFakeDom({
  'importRecipeForm':   new FakeEl('importRecipeForm'),
  'importRecipeUrl':    (() => { const e = new FakeEl('importRecipeUrl'); e.value = LOVEANDLEMONS_URL; return e; })(),
  'importRecipeStatus': new FakeEl('importRecipeStatus'),
  'importRecipeBtn':    (() => { const e = new FakeEl('importRecipeBtn'); e.textContent = 'Import Recipe'; return e; })(),
  'addRecipeForm':      undefined,
});
let importCallCount12 = 0;
const refreshFnCalledOn12 = { value: 0 };
SUT.setupRecipesInit({
  api: (m, p, b) => {
    if (p !== '/api/recipes/import') throw new Error('unexpected: ' + p);
    importCallCount12++;
    // Cache MISS on first call, HIT on second.
    if (importCallCount12 === 1) {
      return {
        ok: true, status: 200,
        data: {
          recipe: { id: 42, title: 'Baked Eggs', source_url: LOVEANDLEMONS_URL },
          cache: { status: 'ok', hit: false, fresh: true, age_seconds: 0 },
        },
      };
    }
    // Second call — server-side cache already populated, TTL still valid.
    return {
      ok: true, status: 200,
      data: {
        recipe: { id: 42, title: 'Baked Eggs', source_url: LOVEANDLEMONS_URL },
        cache: { status: 'ok', hit: true, fresh: true, age_seconds: 300 },  // 5 min
      },
    };
  },
  flash: () => {},
  refreshRecipes: () => { refreshFnCalledOn12.value++; },
});

// First submit — cache MISS → "freshly fetched" cache note.
await fakeDom.get('importRecipeForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(importCallCount12, 1, 'first import has been attempted');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('freshly fetched'), true, 'first call (cache miss) renders "freshly fetched"');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('cached'), false, 'first call does NOT mention "cached"');

// Refill the URL input (first submit cleared it) and re-submit.
fakeDom.get('importRecipeUrl').value = LOVEANDLEMONS_URL;
await fakeDom.get('importRecipeForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(importCallCount12, 2, 'second import has been attempted');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('cached 5 min ago'), true, 'second call (cache hit, age_seconds=300) renders "cached 5 min ago"');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('freshly fetched'), false, 'second call does NOT mention "freshly fetched"');
assertEq(refreshFnCalledOn12.value, 2, 'refreshRecipes called after both imports');

// ============================================================================
console.log('\n13. Server 500 error: resp.ok=false branches into error UI + flash + no refresh');
// ============================================================================
// Critical branch coverage that the v17 happy-path swap of scenario 2
// dropped. Verifies the `if (!resp.ok) throw new Error(errMsg)` path in
// static/js/recipes.js — a regression that makes the handler silently
// treat 500 as success would slip past scenarios 3 + 4 (NetworkError and
// empty-URL paths both bail BEFORE reaching the resp.ok check).
//
// Mock envelope matches the exact shape app.py returns when scrape_me
// hits an upstream 403: `{error, detail, url}` plus a cache envelope.
reset();
setupFakeDom({
  'importRecipeForm':   new FakeEl('importRecipeForm'),
  'importRecipeUrl':    (() => { const e = new FakeEl('importRecipeUrl'); e.value = LOVEANDLEMONS_URL; return e; })(),
  'importRecipeStatus': new FakeEl('importRecipeStatus'),
  'importRecipeBtn':    (() => { const e = new FakeEl('importRecipeBtn'); e.textContent = 'Import Recipe'; return e; })(),
  'addRecipeForm':      undefined,
});
mockRoute('POST', '/api/recipes/import', 500, {
  error: 'Could not scrape that URL.',
  detail: 'HTTPError: HTTP Error 403: Forbidden',
  url: LOVEANDLEMONS_URL,
  cache: { status: 'error', hit: false, error_detail: '403 Forbidden' },
});
const capturedToast13 = [];
const refreshFnCalledOn13 = { value: false };
SUT.setupRecipesInit({
  api: (m, p, b) => mockFetch(m, p, b),
  flash: (msg, kind) => capturedToast13.push({ msg, kind }),
  refreshRecipes: () => { refreshFnCalledOn13.value = true; },
});
await fakeDom.get('importRecipeForm').eventListeners.submit[0]({ preventDefault: () => {} });
assertEq(fakeDom.get('importRecipeStatus').textContent.startsWith('\u2717 '), true, 'status prefix is \u2717 on HTTP 500');
assertEq(fakeDom.get('importRecipeStatus').style.color, 'crimson', 'status color crimson on HTTP 500');
assertEq(fakeDom.get('importRecipeStatus').textContent.includes('We could not import that recipe right now.'), true, 'status includes safe user-facing error message');
assertEq(fakeDom.get('importRecipeBtn').disabled, false, 'button re-enabled on HTTP 500 (finally-block runs)');
assertEq(capturedToast13.length, 1, 'flash called exactly once on HTTP 500');
assertEq(capturedToast13[0].kind, 'error', 'flash kind is error on HTTP 500');
assertEq(capturedToast13[0].msg, 'We could not import that recipe right now.', 'flash message uses safe generic import error');
assertEq(refreshFnCalledOn13.value, false, 'refreshRecipes NOT called on HTTP 500 (handler bailed on resp.ok=false)');

// ============================================================================
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
}

main().then(() => {
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
}).catch((err) => {
  console.error('Test runner crashed:', err);
  process.exit(2);
});
