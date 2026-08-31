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

const fs = require('fs');
const path = require('path');

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
assertEq(metaEl3.textContent, 'Tax used: 8.225%', 'meta shows applied tax rate');
assertEq(totalEl3.textContent, 'Total (with tax): $8.11', 'total shows tax-inclusive total');
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
assertEq(listEl4.innerHTML, '<div class="empty">No grocery list yet. Build one to get started.</div>', 'empty state when no items');
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
assertEq(listEl5.innerHTML, '<div class="empty">We could not load your grocery list right now.</div>', 'error empty state when GET fails');

// ============================================================================
console.log('\n6. setupGroceryInit is the idempotent production controller for cart actions');
// ============================================================================
reset();
setupFakeDom({
  'shopping': new FakeEl('shopping'),
  'generateRecipesBtn': new FakeEl('generateRecipesBtn'),
  'buildCartBtn': new FakeEl('buildCartBtn'),
  'rebalanceCartBtn': new FakeEl('rebalanceCartBtn'),
});
const buildCalls6 = [];
let rebalanceCalls6 = 0;
const deps6 = {
  buildCart: (forceRefresh) => { buildCalls6.push(forceRefresh); },
  rebalanceCart: () => { rebalanceCalls6++; },
};
await SUT.setupGroceryInit(deps6);
await SUT.setupGroceryInit(deps6);
assertEq(fakeDom.get('generateRecipesBtn').eventListeners.click.length, 1, 'reinitialization does not duplicate Build Shopping Plan handlers');
assertEq(fakeDom.get('buildCartBtn').eventListeners.click.length, 1, 'reinitialization does not duplicate Build Cart handlers');
assertEq(fakeDom.get('rebalanceCartBtn').eventListeners.click.length, 1, 'reinitialization does not duplicate Rebalance Cart handlers');
await fakeDom.get('generateRecipesBtn').eventListeners.click[0]();
await fakeDom.get('buildCartBtn').eventListeners.click[0]();
await fakeDom.get('rebalanceCartBtn').eventListeners.click[0]();
assertEq(buildCalls6.length, 2, 'Build Shopping Plan + Build Cart both use the cart build runtime');
assertEq(rebalanceCalls6, 1, 'Rebalance Cart uses the preview-review-apply runtime');

// ============================================================================
console.log('\n7. setupGroceryInit restores and persists retailer context before rebuilding');
// ============================================================================
reset();
const storeSelect7 = new FakeEl('storeSel');
storeSelect7.value = 'kroger';
setupFakeDom({
  'storeSel': storeSelect7,
});
const lifecycle7 = [];
await SUT.setupGroceryInit({
  restoreGroceryRetailerSelection: async () => { lifecycle7.push('restore'); },
  persistGroceryRetailerSelection: async (retailer) => { lifecycle7.push('persist:' + retailer); },
  buildCart: async () => { lifecycle7.push('build'); },
});
await storeSelect7.eventListeners.change[0]();
assertEq(lifecycle7, ['restore', 'persist:kroger', 'build'], 'retailer restores first, then persists before cart rebuild');

// ============================================================================
console.log('\n8. setupGroceryInit owns quick search and Finished Shopping initialization');
// ============================================================================
reset();
setupFakeDom({
  'shopping': new FakeEl('shopping'),
  'rapidSearchBtn': new FakeEl('rapidSearchBtn'),
  'rapidSearchInput': new FakeEl('rapidSearchInput'),
});
let searchCalls8 = 0;
let finishedInitCalls8 = 0;
const deps8 = {
  buildCart: async () => {},
  searchProductPrice: () => { searchCalls8++; },
  initFinishedShoppingFlow: async () => { finishedInitCalls8++; },
};
await SUT.setupGroceryInit(deps8);
await SUT.setupGroceryInit(deps8);
assertEq(fakeDom.get('rapidSearchBtn').eventListeners.click.length, 1, 'reinitialization does not duplicate quick-search handlers');
await fakeDom.get('rapidSearchBtn').eventListeners.click[0]();
await fakeDom.get('rapidSearchInput').eventListeners.keypress[0]({ key: 'Enter' });
await fakeDom.get('rapidSearchInput').eventListeners.keypress[0]({ key: 'Escape' });
assertEq(searchCalls8, 2, 'quick search runs from click and Enter only');
assertEq(finishedInitCalls8, 1, 'Finished Shopping is initialized once by the Grocery controller');

// ============================================================================
console.log('\n9. getCartSourcePresentation confirmed local item: green confirmed indicator');
// ============================================================================
const confirmed9 = SUT.getCartSourcePresentation({
  price_source: 'kroger_api',
  confirmed_local_store: true,
  store_name: 'Kroger - West',
}, 'Kroger');
assertEq(confirmed9.badgeHtml.includes('Store-Checked (Live)'), true, 'confirmed local item uses live confirmed badge');
assertEq(confirmed9.detailHtml.includes('Checked at Kroger - West'), true, 'confirmed local item shows actual store name');
assertEq(confirmed9.detailHtml.includes('Not confirmed'), false, 'confirmed local item does not show fallback warning text');

// ============================================================================
console.log('\n10. getCartSourcePresentation RapidAPI fallback: clearly not confirmed local');
// ============================================================================
const rapid10 = SUT.getCartSourcePresentation({
  price_source: 'rapid_api',
  confirmed_local_store: false,
  store_name: 'RapidMart',
}, 'Kroger');
assertEq(rapid10.badgeHtml.includes('Other Source'), true, 'RapidAPI item uses other-source badge');
assertEq(rapid10.detailHtml.includes('Store found: RapidMart'), true, 'RapidAPI item shows source store');
assertEq(rapid10.detailHtml.includes('Not yet confirmed at Kroger'), true, 'RapidAPI item explicitly says not confirmed at selected store');

// ============================================================================
console.log('\n11. getCartSourcePresentation estimated item: estimate badge + not confirmed context');
// ============================================================================
const est11 = SUT.getCartSourcePresentation({
  price_source: 'estimated',
  confirmed_local_store: false,
}, 'Kroger');
assertEq(est11.badgeHtml.includes('Estimate'), true, 'estimated item uses estimate badge');
assertEq(est11.detailHtml.includes('Store not available'), true, 'estimated item shows unresolved source detail');
assertEq(est11.detailHtml.includes('Not yet confirmed at Kroger'), true, 'estimated item says not confirmed at selected store');

// ============================================================================
console.log('\n12. getCartSourcePresentation store cache fallback: treated as non-local third-party');
// ============================================================================
const fallback12 = SUT.getCartSourcePresentation({
  price_source: 'store_cache_fallback',
  confirmed_local_store: false,
  store_name: 'Generic Store',
}, 'Kroger');
assertEq(fallback12.badgeHtml.includes('Other Source (Saved)'), true, 'store_cache_fallback badge is not local-confirmed cache');
assertEq(fallback12.detailHtml.includes('Not yet confirmed at Kroger'), true, 'store_cache_fallback explicitly not confirmed at selected store');

// ============================================================================
console.log('\n13. production template initializes the tested Grocery controller');
// ============================================================================
const productionTemplate = fs.readFileSync(path.join(__dirname, '..', 'templates', 'index.html'), 'utf8');
assertEq(productionTemplate.includes("await setupGroceryInit({"), true, 'production awaits setupGroceryInit');
assertEq(productionTemplate.includes("generateRecipesBtn').addEventListener"), false, 'production has no duplicate inline Build Shopping Plan listener');
assertEq(productionTemplate.includes("buildCartBtn').addEventListener"), false, 'production has no duplicate inline Rebalance Cart listener');
assertEq(productionTemplate.includes("rapidSearchBtn').addEventListener"), false, 'production has no duplicate inline quick-search listener');
assertEq(productionTemplate.includes("if (typeof buildCart === 'function') buildCart();"), false, 'tab navigation does not run a competing cart build');
assertEq(productionTemplate.includes("'/api/shopping/current-cart/choose-product'"), true, 'product choice is persisted through the authoritative current-cart endpoint');
assertEq(productionTemplate.includes('item.selected_product = chosen'), false, 'product choice does not locally substitute rendered cart authority');
assertEq(productionTemplate.includes('if (item.estimated_price != null && item.promo_price != null'), true, 'promo display cannot fabricate an unknown-quantity total');
assertEq(productionTemplate.includes("'/api/grocery/rebalance/preview'"), true, 'served browser calls authoritative rebalance preview endpoint');
assertEq(productionTemplate.includes("'/api/grocery/rebalance/apply'"), true, 'served browser calls authoritative rebalance apply endpoint');
assertEq(productionTemplate.includes('id="rebalanceReviewDialog"'), true, 'served browser renders an explicit rebalance review dialog');
assertEq(productionTemplate.includes("normalizeCartContext(d, canonicalStore.retailer || '', storeName)"), true, 'cart context keeps canonical retailer identity separate from store display name');
assertEq(productionTemplate.includes('normalizeCartContext(d, storeName, storeName)'), false, 'store display name is never used as the retailer key');

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
