/**
 * Node.js smoke test for static/js/grocery.js.
 *
 * Exercises the grocery-list refresh + generate + delete flows in
 * isolation. Mocks:
 *   - global.fetch via a per-URL sandbox.
 *   - document.querySelectorAll / document.getElementById via FakeEl.
 *
 * Run with:  node tests/test_grocery.js
 */

'use strict';

// ---- Mock fetch infrastructure (mirrors test_recipes.js) ----
const mockRoutes = [];
function mockRoute(method, path, status, responseBody, expectedBody) {
  mockRoutes.push({ method, path, status, responseBody, expectedBody });
}
function clearMockRoutes() { mockRoutes.length = 0; }
async function mockFetch(method, path, body) {
  const route = mockRoutes.find(r => {
    if (r.method !== method || r.path !== path) return false;
    if (r.expectedBody === undefined) return true;
    return JSON.stringify(r.expectedBody) === JSON.stringify(body);
  });
  if (!route) throw new Error('No mock for ' + method + ' ' + path + (body ? ' ' + JSON.stringify(body) : ''));
  return { ok: route.status >= 200 && route.status < 300, status: route.status, data: route.responseBody };
}

// ---- Mock DOM registry (mirrors test_recipes.js) ----
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
    this._checked = false;
    this.attributes = {};
  }
  addEventListener(event, fn) {
    (this.eventListeners[event] = this.eventListeners[event] || []).push(fn);
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  setAttribute(k, v) { this.attributes[k] = v; }
  getAttribute(k) { return this.attributes[k]; }
  querySelectorAll(sel) {
    // For 'input[type=checkbox][data-recipe]:checked' selector, return
    // the checked items in this.children where applicable.
    if (sel === 'input[type="checkbox"][data-recipe]:checked') {
      return this.children.filter(c => c._checked && c.attributes['data-recipe']);
    }
    return [];
  }
  querySelector(sel) {
    if (sel === 'button[data-id]') return null;  // not used directly
    return null;
  }
}

// ---- Build a fake grocery-list container with N child rows ----
function buildGroceryContainer(id, rows) {
  const el = new FakeEl(id);
  el.children = rows.map(r => {
    const btn = new FakeEl('btn');
    btn.attributes['data-id'] = String(r.id);
    const row = new FakeEl('row');
    row.innerHTML = '<button data-id="' + r.id + '">Delete</button>';
    row.children = [btn];
    btn.parent = row;
    return row;
  });
  return el;
}

// Build a fake recipes-list container with N checked/unchecked checkboxes
function buildRecipesContainer(id, recipes) {
  const el = new FakeEl(id);
  el.children = recipes.map(r => {
    const cb = new FakeEl('cb');
    cb.attributes['data-recipe'] = String(r.id);
    cb._checked = r.checked || false;
    return cb;
  });
  return el;
}

const fakeDom = new Map();
function setupFakeDom(idMap) {
  fakeDom.clear();
  for (const [id, value] of Object.entries(idMap)) {
    fakeDom.set(id, value !== undefined ? value : new FakeEl(id));
  }
}
global.document = {
  getElementById: (id) => fakeDom.get(id) || null,
  createElement: (tag) => new FakeEl(tag),
  querySelectorAll: (sel) => {
    // Look up the recipes container (the only place we use querySelectorAll).
    if (sel === 'input[type="checkbox"][data-recipe]:checked') {
      const container = fakeDom.get('recipeListContainer');
      if (!container) return [];
      return container.children.filter(c => c._checked && c.attributes['data-recipe']);
    }
    return [];
  },
};

// ---- System under test ----
async function main() {
const SUT = require('../static/js/grocery.js');

// ---- Assertions ----
let passed = 0, failed = 0;
function assertEq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) { passed++; console.log('  \u2713 ' + label); }
  else { failed++; console.error('  \u2717 ' + label + ': expected ' + e + ', got ' + a); }
}
function reset() {
  clearMockRoutes();
  fakeDom.clear();
}

// ============================================================================
console.log('1. getSelectedRecipeIds: reads checked checkboxes from DOM');
// ============================================================================
reset();
setupFakeDom({
  'recipeListContainer': buildRecipesContainer('recipeListContainer', [
    { id: 1, checked: true },
    { id: 2, checked: false },
    { id: 3, checked: true },
    { id: 4, checked: false },
  ]),
});
assertEq(SUT.getSelectedRecipeIds(), [1, 3], 'returns array of checked recipe IDs (numeric)');

// ============================================================================
console.log('\n2. getSelectedRecipeIds: handles empty selection gracefully');
// ============================================================================
reset();
setupFakeDom({
  'recipeListContainer': buildRecipesContainer('recipeListContainer', [
    { id: 1, checked: false },
    { id: 2, checked: false },
  ]),
});
assertEq(SUT.getSelectedRecipeIds(), [], 'returns empty array when nothing checked');

// ============================================================================
console.log('\n3. refreshGrocery happy path: GET returns items + meta + total, renders correctly');
// ============================================================================
reset();
const listEl3 = new FakeEl('groceryListContainer');
const totalEl3 = new FakeEl('groceryTotal');
const metaEl3 = new FakeEl('groceryMeta');
setupFakeDom({
  'groceryListContainer': listEl3,
  'groceryTotal': totalEl3,
  'groceryMeta': metaEl3,
});
mockRoute('GET', '/api/grocery', 200, {
  items: [
    { id: 10, item_name: 'Eggs', quantity: 2, estimated_price: 4.50, store_name: 'Walmart', location_context: 'Versailles, MO' },
    { id: 11, item_name: 'Bread', quantity: 1, estimated_price: 3.00, store_name: 'Walmart', location_context: 'Versailles, MO' },
  ],
  applied_tax_pct: 8.225,
  estimated_total_with_tax: 8.11,
});
SUT._setMockFetch(mockFetch);
await SUT.refreshGrocery();
assertEq(metaEl3.textContent, 'Tax rate applied: 8.225%', 'meta shows applied tax rate');
assertEq(totalEl3.textContent, 'Total (tax incl.): $8.11', 'total shows tax-inclusive total');
assertEq(listEl3.children.length, 2, 'renders 2 grocery items');
assertEq(listEl3.children[0].innerHTML.includes('Eggs'), true, 'row 1 shows Eggs');
assertEq(listEl3.children[0].innerHTML.includes('data-id="10"'), true, 'row 1 has correct data-id');
assertEq(listEl3.children[1].innerHTML.includes('Bread'), true, 'row 2 shows Bread');

// ============================================================================
console.log('\n4. refreshGrocery empty: GET returns [], shows empty state');
// ============================================================================
reset();
const listEl4 = new FakeEl('groceryListContainer');
const totalEl4 = new FakeEl('groceryTotal');
const metaEl4 = new FakeEl('groceryMeta');
setupFakeDom({
  'groceryListContainer': listEl4,
  'groceryTotal': totalEl4,
  'groceryMeta': metaEl4,
});
mockRoute('GET', '/api/grocery', 200, { items: [] });
SUT._setMockFetch(mockFetch);
await SUT.refreshGrocery();
assertEq(listEl4.innerHTML, '<div class="empty">No active grocery list yet — generate one to begin.</div>', 'empty state when no items');
assertEq(totalEl4.textContent, '', 'total cleared when no items');

// ============================================================================
console.log('\n5. refreshGrocery error: GET fails, shows error empty state');
// ============================================================================
reset();
const listEl5 = new FakeEl('groceryListContainer');
setupFakeDom({
  'groceryListContainer': listEl5,
  'groceryTotal': new FakeEl('groceryTotal'),
  'groceryMeta': new FakeEl('groceryMeta'),
});
mockRoute('GET', '/api/grocery', 500, { error: 'database unavailable' });
SUT._setMockFetch(mockFetch);
await SUT.refreshGrocery();
assertEq(listEl5.innerHTML, '<div class="empty">Could not load grocery list.</div>', 'error empty state when GET fails');

// ============================================================================
console.log('\n6. setupGroceryInit Generate handler: empty selection shows error flash, no fetch');
// ============================================================================
reset();
setupFakeDom({
  'generateGroceryBtn': new FakeEl('generateGroceryBtn'),
  'recipeListContainer': buildRecipesContainer('recipeListContainer', [
    { id: 1, checked: false },
  ]),
});
const capturedFlash6 = [];
let fetchCalled6 = false;
SUT.setupGroceryInit({
  flash: (msg, kind) => capturedFlash6.push({ msg, kind }),
  refreshGrocery: () => { fetchCalled6 = true; },
});
await fakeDom.get('generateGroceryBtn').eventListeners.click[0]();
assertEq(fetchCalled6, false, 'no fetch when no recipes selected');
assertEq(capturedFlash6.length, 1, 'flash called once on empty selection');
assertEq(capturedFlash6[0].kind, 'error', 'flash kind is error');
assertEq(capturedFlash6[0].msg, 'Select at least one recipe in the Recipes tab first', 'flash message prompts recipe selection');

// ============================================================================
console.log('\n7. setupGroceryInit Generate happy path: POSTs recipe_ids + store_name + budget_limit, refreshes');
// ============================================================================
reset();
setupFakeDom({
  'generateGroceryBtn': new FakeEl('generateGroceryBtn'),
  'storeSel': (() => { const e = new FakeEl('storeSel'); e.value = 'Aldi'; return e; })(),
  'budgetInput': (() => { const e = new FakeEl('budgetInput'); e.value = '100'; return e; })(),
  'recipeListContainer': buildRecipesContainer('recipeListContainer', [
    { id: 7, checked: true },
    { id: 8, checked: true },
    { id: 9, checked: false },
  ]),
  'groceryListContainer': new FakeEl('groceryListContainer'),
  'groceryTotal': new FakeEl('groceryTotal'),
  'groceryMeta': new FakeEl('groceryMeta'),
});
let capturedGenerateBody = null;
// The handler now calls POST /api/grocery/generate-pay-period-plan
mockRoute('POST', '/api/grocery/generate-pay-period-plan', 200, {
  recipes_used: [{ id: 7, title: 'Recipe 7' }, { id: 8, title: 'Recipe 8' }],
  cart_items: [],
  subtotal: 0,
  total_cart_cost: 0,
}, { recipe_ids: [7, 8], store_name: 'Aldi', budget_limit: 100 });
const capturedFlash7 = [];
let refreshCalled7 = 0;
SUT._setMockFetch((m, p, b) => {
  capturedGenerateBody = b;
  return mockFetch(m, p, b);
});
SUT.setupGroceryInit({
  flash: (msg, kind) => capturedFlash7.push({ msg, kind }),
  refreshGrocery: () => { refreshCalled7++; },
});
await fakeDom.get('generateGroceryBtn').eventListeners.click[0]();
assertEq(capturedGenerateBody && capturedGenerateBody.recipe_ids, [7, 8], 'POST body includes selected recipe_ids');
assertEq(capturedGenerateBody && capturedGenerateBody.store_name, 'Aldi', 'POST body includes store_name');
assertEq(capturedGenerateBody && capturedGenerateBody.budget_limit, 100, 'POST body includes budget_limit');
assertEq(capturedFlash7.length, 1, 'flash called once on success');
assertEq(capturedFlash7[0].kind, 'success', 'flash kind is success');
assertEq(refreshCalled7, 1, 'refreshGrocery called once after successful generate');

// ============================================================================
console.log('\n8. setupGroceryInit Generate server error: flash with API error, no refresh');
// ============================================================================
reset();
setupFakeDom({
  'generateGroceryBtn': new FakeEl('generateGroceryBtn'),
  'storeSel': (() => { const e = new FakeEl('storeSel'); e.value = 'Walmart'; return e; })(),
  'recipeListContainer': buildRecipesContainer('recipeListContainer', [
    { id: 1, checked: true },
  ]),
});
mockRoute('POST', '/api/grocery/generate-pay-period-plan', 400, { error: 'recipe_ids must be a non-empty list' });
const capturedFlash8 = [];
let refreshCalled8 = 0;
SUT.setupGroceryInit({
  flash: (msg, kind) => capturedFlash8.push({ msg, kind }),
  refreshGrocery: () => { refreshCalled8++; },
});
await fakeDom.get('generateGroceryBtn').eventListeners.click[0]();
assertEq(capturedFlash8.length, 1, 'flash called on HTTP error');
assertEq(capturedFlash8[0].kind, 'error', 'flash kind is error');
assertEq(capturedFlash8[0].msg, 'recipe_ids must be a non-empty list', 'flash shows server error');
assertEq(refreshCalled8, 0, 'refresh NOT called on error');

// ============================================================================
console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
}

main().then(() => {
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed > 0 ? 1 : 0);
}).catch((err) => {
  console.error('Test runner crashed:', err);
  process.exit(2);
});
