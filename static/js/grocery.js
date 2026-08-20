// !!! WARNING — grocery.js depends on recipes.js loading FIRST.
// !! `getSelectedRecipeIds()` queries the data-recipe checkboxes that
// !! refreshRecipes() renders. If you reorder these two <script> tags,
// !! Generate-from-selected-recipes will silently send an empty list.
// !! Today the tags are loaded synchronously in <head> with recipes.js
// !! first, so the order is guaranteed.
//
// !!! WARNING — grocery.js is loaded BEFORE the inline body script.
// !! The exported functions reference globals defined in the inline
// !! body script (`escapeHtml`, `flash`, `api`). Those references are
// !! resolved at CALL-time (when the inline body's init() runs), not at
// !! LOAD-time. Don't move these references into module-level code
// !! without also moving the <script> tag to AFTER the inline body script.

/**
 * Grocery UI module
 * =================
 *
 * Standalone module extracted from templates/index.html so the
 * grocery-list refresh + generation flows can be tested in isolation.
 * Loaded via <script src="/static/js/grocery.js"></script> in <head>
 * AFTER recipes.js.
 *
 * Public API (callable as globals from the main inline script):
 *   - refreshGrocery              Renders the grocery list + meta + total.
 *   - getSelectedRecipeIds        Reads checked recipe IDs from the DOM.
 *   - setupGroceryInit(deps)      Authoritative production Grocery controller.
 *
 * Dependencies (resolved at call-time, not load-time):
 *   - `escapeHtml`  from the main inline script.
 *   - `flash`       from the main inline script.
 *   - `api`         from the main inline script.
 *   - `document`    from the global browser object.
 *   - DOM checkboxes `[data-recipe]` from recipes.js's refreshRecipes().
 *
 * Node.js testability: the conditional `module.exports` block at the
 * bottom exports the helpers for isolation testing. The browser ignores
 * that block (no `module` global in browser globals).
 */

'use strict';

// ----------------------------------------------------------------------------
// PLACEHOLDER GLOBALS (resolved at call-time by the inline body script).
// ----------------------------------------------------------------------------
// Same rationale as in recipes.js: declare empty lets at top so the TDZ
// window is empty from the moment the script begins executing. The inline
// body script must NOT redeclare any of these (would throw SyntaxError).
var api_;
var escapeHtml;
var flash_;
var updateRecipeSelection_;

/**
 * Read the selected recipe IDs from the DOM checkboxes rendered by
 * recipes.js's refreshRecipes() function. Each card has an
 * `<input type="checkbox" data-recipe="<id>" />`; this function
 * collects the IDs of the checked ones.
 *
 * Replaces the original inline-body handler's read of
 * `window.__rungSelectedRecipes` (a fragile global that had to be kept
 * in sync by another helper). The DOM is the source of truth.
 *
 * @returns {number[]} Array of recipe-id numbers for checked boxes.
 */
function getSelectedRecipeIds() {
  const checked = document.querySelectorAll('input[type="checkbox"][data-recipe]:checked');
  const ids = [];
  checked.forEach(cb => {
    const id = parseInt(cb.getAttribute('data-recipe'), 10);
    if (Number.isFinite(id)) ids.push(id);
  });
  return ids;
}

/**
 * Internal fetch wrapper. Three-tier fallback (mirrors recipes.js):
 *   1. `api_` global (set by the inline body script's `api()` function).
 *   2. `_groceryMockFetch` test hook (set by tests via SUT._setMockFetch).
 *   3. Direct `globalThis.fetch` (used in browser fall-through).
 *
 * Branch 3 is defensive: throws if `globalThis.fetch` is unavailable
 * so a missing-test-mock does NOT silently fire a real network
 * request in Node 18+.
 */
async function fetchGrocery_(method, path, body) {
  if (typeof api_ === 'function') return api_(method, path, body);
  if (typeof _groceryMockFetch === 'function') {
    return _groceryMockFetch(method, path, body);
  }
  if (typeof globalThis !== 'undefined' && typeof globalThis.fetch === 'function') {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const r = await globalThis.fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, data };
  }
  throw new Error('grocery.js: no api function or fetch available; set up the mock or wire api() before calling refreshGrocery()');
}

/**
 * Module-internal escapeHtml wrapper. Falls back to the inline-script
 * `escapeHtml` global, or to a tiny inline implementation if absent.
 */
function escapeHtml_(s) {
  if (typeof escapeHtml === 'function') return escapeHtml(s);
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Build UI labels/colors for cart source display so non-confirmed items are
 * clearly distinguishable from confirmed local-store products.
 *
 * @param {object} item
 * @param {string} selectedStoreName
 * @returns {{badgeHtml: string, detailHtml: string, isConfirmedLocal: boolean}}
 */
function getCartSourcePresentation(item, selectedStoreName) {
  item = item || {};
  var ps = String(item.price_source || 'estimated').toLowerCase();
  var selectedStore = selectedStoreName || 'selected store';
  var actualStore = item.store_name ? escapeHtml_(item.store_name) : '';
  var confirmed = item.confirmed_local_store === true;

  if (confirmed) {
    var live = (ps === 'api' || ps === 'kroger_api');
    var confirmedLabel = live ? 'Store-Checked (Live)' : 'Store-Checked (Saved)';
    var confirmedDetail = actualStore
      ? 'Checked at ' + actualStore
      : 'Checked at selected store';
    return {
      isConfirmedLocal: true,
      badgeHtml: '<span class="badge" style="background:var(--accent-soft);color:var(--accent);border-color:rgba(45,191,110,.3);">' + confirmedLabel + '</span>',
      detailHtml: '<div class="li-meta" style="color:var(--accent);">' + confirmedDetail + '</div>',
    };
  }

  var badgeLabel = 'Needs Review';
  var badgeStyle = 'background:rgba(107,114,128,.12);color:var(--text-mute);border-color:rgba(107,114,128,.30);';
  if (ps === 'rapid_api') {
    badgeLabel = 'Other Source';
    badgeStyle = 'background:rgba(59,130,246,.12);color:#3b82f6;border-color:rgba(59,130,246,.25);';
  } else if (ps === 'rapid_cache' || ps === 'store_cache_fallback') {
    badgeLabel = 'Other Source (Saved)';
    badgeStyle = 'background:rgba(59,130,246,.10);color:#2563eb;border-color:rgba(59,130,246,.22);';
  } else if (ps === 'estimated') {
    badgeLabel = 'Estimate';
    badgeStyle = 'background:rgba(245,158,11,.10);color:var(--warn);border-color:rgba(245,158,11,.25);';
  }

  var sourceLine = actualStore ? ('Store found: ' + actualStore) : ('Store not available');
  var detail = sourceLine + ' • Not yet confirmed at ' + escapeHtml_(selectedStore);
  return {
    isConfirmedLocal: false,
    badgeHtml: '<span class="badge" style="' + badgeStyle + '">' + badgeLabel + '</span>',
    detailHtml: '<div class="li-meta" style="color:var(--warn);">' + detail + '</div>',
  };
}

/**

/**
 * Fetch /api/grocery and render the list. Renders per-item rows with
 * a Delete button, plus meta (applied tax rate) and total.
 */
async function refreshGrocery() {
  // The old grocery-list UI (groceryListContainer / groceryTotal / groceryMeta)
  // was replaced by the Pay Period Plan + Store Cart panels.  If those
  // elements no longer exist in the DOM this is a no-op so callers that
  // haven't been migrated yet (e.g. the boot init()) don't crash.
  const list = document.getElementById('groceryListContainer');
  if (!list) return;
  const totalEl = document.getElementById('groceryTotal');
  const metaEl = document.getElementById('groceryMeta');
  list.innerHTML = '';
  if (totalEl) totalEl.textContent = '';
  if (metaEl) metaEl.textContent = '';
  const resp = await fetchGrocery_('GET', '/api/grocery', undefined);
  if (!resp.ok) {
    list.innerHTML = '<div class="empty">We could not load your grocery list right now.</div>';
    return;
  }
  const data = resp.data || {};
  const items = data.items || [];
  if (items.length === 0) {
    list.innerHTML = '<div class="empty">No grocery list yet. Build one to get started.</div>';
    return;
  }
  if (data.applied_tax_pct != null && metaEl) {
    metaEl.textContent = 'Tax used: ' + data.applied_tax_pct + '%';
  }
  items.forEach(g => {
    const row = document.createElement('div');
    row.className = 'list-item';

    const productName = g.product_label || g.item_name || 'Item';
    const price = Number(g.estimated_price ?? g.unit_price ?? 0) || 0;
    const priceSource = String(g.price_source || 'estimated').toLowerCase();
    const isConfirmed = g.confirmed_local_store === true || ['kroger_api', 'kroger_cache', 'store_cache_fallback'].includes(priceSource);
    const quantityLabel = Number(g.quantity || 1);
    const displayStore = g.store_name || 'Store unavailable';
    const packageSize = g.package_size || (isConfirmed ? '' : 'Needs review / estimated');
    const sourceText = priceSource === 'estimated' || !priceSource
      ? 'Needs review / estimated'
      : priceSource.replace(/_/g, ' ');

    row.innerHTML = `
      <div class="li-title">${quantityLabel}× <strong>${escapeHtml_(productName)}</strong></div>
      <div class="li-amount">$${price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
      <div class="li-meta">${escapeHtml_(displayStore)}</div>
      <div class="li-meta">${escapeHtml_(packageSize || g.location_context || '')}</div>
      <div class="li-meta" style="color:${isConfirmed ? 'var(--accent)' : 'var(--warn)'};">${escapeHtml_(sourceText)}</div>
      <button class="btn is-danger" type="button" data-id="${g.id}">Remove</button>
    `;
    list.appendChild(row);
  });
  list.querySelectorAll('button[data-id]').forEach(b => {
    b.addEventListener('click', async () => {
      await fetchGrocery_('DELETE', '/api/grocery/' + b.dataset.id, undefined);
      if (typeof flash_ === 'function') flash_('Item removed', 'success');
      await refreshGrocery();
    });
  });
  if (data.estimated_total_with_tax != null && totalEl) {
    totalEl.textContent = 'Total (with tax): $' + Number(data.estimated_total_with_tax).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  } else if (data.estimated_subtotal != null && totalEl) {
    totalEl.textContent = 'Subtotal: $' + Number(data.estimated_subtotal).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
}

/**
 * Fetch /api/recipes/generate with the selected IDs, then render recipe
 * mini-cards inside the Active Recipes expander on the Grocery tab.
 *
 * The server-persisted meal plan (/api/meal-plan) is the source of truth
 * — the Copilot writes matched recipes there, so this unions those IDs
 * with any locally-checked DOM boxes. Caps display at 14 recipes. When
 * zero recipes are selected the expander shows a helpful prompt linking
 * to the Recipes tab.
 */
async function refreshActiveRecipesExpander() {
  const expander = document.getElementById('activeRecipesExpander');
  if (!expander) return;
  const grid = document.getElementById('activeRecipesGrid');
  const countEl = document.getElementById('activeRecipeCount');
  if (!grid) return;

  const ids = getSelectedRecipeIds();
  // Union with the server-persisted meal plan (Copilot additions).
  try {
    const planResp = await fetchGrocery_('GET', '/api/meal-plan', undefined);
    if (planResp && planResp.ok && planResp.data && Array.isArray(planResp.data.recipe_ids)) {
      planResp.data.recipe_ids.forEach(function (rid) {
        const n = parseInt(rid, 10);
        if (Number.isFinite(n) && ids.indexOf(n) === -1) ids.push(n);
      });
    }
  } catch (_e) { /* server plan unavailable — fall back to checked boxes */ }
  var max = 14;
  var cappedIds = ids.slice(0, max);
  var truncated = ids.length > max;

  if (countEl) {
    countEl.textContent = truncated
      ? String(cappedIds.length) + ' of ' + String(ids.length) + ' Selected'
      : String(cappedIds.length) + ' Selected';
  }

  if (cappedIds.length === 0) {
    grid.innerHTML = '<div class="empty">No recipes selected for this pay period yet. Go to Recipes to pick meals.</div>';
    return;
  }

  // Fetch recipe details including ingredients
  var resp = await fetchGrocery_('POST', '/api/recipes/generate', { recipe_ids: cappedIds });
  if (!resp.ok) {
    grid.innerHTML = '<div class="empty">We could not load selected recipes right now.</div>';
    return;
  }
  var data = resp.data || {};
  var recipes = data.recipes || [];

  if (recipes.length === 0) {
    grid.innerHTML = '<div class="empty">No recipes matched the selected items.</div>';
    return;
  }

  grid.innerHTML = '';
  recipes.forEach(function (r) {
    var card = document.createElement('div');
    card.className = 'recipe-mini-card';
    var ings = (r.ingredients || []);
    var ingHtml = ings.map(function (i) {
      if (i.display_text) return '<li><strong>' + escapeHtml_(i.display_text) + '</strong></li>';
      var requirement = i.quantity != null
        ? escapeHtml_(String(i.quantity)) + (i.unit ? ' ' + escapeHtml_(i.unit) : '') + ' \u2014 '
        : '';
      return '<li>' + requirement + '<strong>' + escapeHtml_(i.product_name || '') + '</strong></li>';
    }).join('');
    card.innerHTML =
      '<div class="rmc-title">' + escapeHtml_(r.title || 'Recipe') + '</div>' +
      '<div class="rmc-servings">' + (r.servings || 1) + ' serving' + ((r.servings || 1) !== 1 ? 's' : '') + '</div>' +
      '<ul class="rmc-ingredients">' + ingHtml + '</ul>';
    grid.appendChild(card);
  });

  // When the response carried fewer recipes than we requested (some
  // IDs didn't match), also reflect the truncation if applicable.
  if (truncated && countEl) {
    countEl.textContent = String(recipes.length) + ' of ' + String(ids.length) + ' Selected';
  }
}

/**
 * Initialize the production Grocery UI once, after the DOM is ready.
 * This is the sole owner of Grocery action listeners; the injected helpers
 * retain the existing cart rendering and backend contracts.
 *
 * @param {object} deps
 * @param {Function} deps.buildCart authoritative cart build/render helper
 * @param {Function} deps.searchProductPrice quick product search helper
 * @param {Function} deps.restoreGroceryRetailerSelection retailer restore helper
 * @param {Function} deps.persistGroceryRetailerSelection retailer persistence helper
 * @param {Function} deps.initFinishedShoppingFlow Finished Shopping initializer
 */
async function setupGroceryInit(deps) {
  deps = deps || {};
  const groceryRoot = document.getElementById('grocery');
  if (groceryRoot && groceryRoot.__rungGroceryInitialized) return;
  if (groceryRoot) groceryRoot.__rungGroceryInitialized = true;

  // Bind module-scoped placeholder used by the click handlers above.
  // Inline-script globals fall through when deps don't provide them;
  // typeof guards keep the read safe in Node tests where `flash` is
  // undeclared (would otherwise throw ReferenceError).
  flash_ = (deps && typeof deps.flash === 'function') ? deps.flash
         : (typeof flash === 'function' ? flash : null);
  api_ = (deps && typeof deps.api === 'function') ? deps.api : api_;

  const buildCartFn = deps.buildCart;
  const rebalanceCartFn = deps.rebalanceCart;
  const searchProductPriceFn = deps.searchProductPrice;
  const persistRetailerFn = deps.persistGroceryRetailerSelection;
  const restoreRetailerFn = deps.restoreGroceryRetailerSelection;
  const initFinishedShoppingFn = deps.initFinishedShoppingFlow;

  if (typeof buildCartFn !== 'function') {
    if (groceryRoot) groceryRoot.__rungGroceryInitialized = false;
    throw new Error('setupGroceryInit requires buildCart');
  }

  try {
    // Restore the saved retailer before any cart action can run so the first
    // browser request uses the same retailer context shown in the selector.
    if (typeof restoreRetailerFn === 'function') await restoreRetailerFn();

    const generateBtn = document.getElementById('generateRecipesBtn');
    if (generateBtn) generateBtn.addEventListener('click', function () {
      buildCartFn(false);
    });

    const rebalanceBtn = document.getElementById('buildCartBtn');
    if (rebalanceBtn) rebalanceBtn.addEventListener('click', function () {
      if (typeof rebalanceCartFn === 'function') return rebalanceCartFn();
      return buildCartFn(false);
    });

    const storeSelect = document.getElementById('storeSel');
    if (storeSelect) storeSelect.addEventListener('change', async function () {
      if (typeof persistRetailerFn === 'function') {
        await persistRetailerFn(storeSelect.value);
      }
      await buildCartFn();
    });

    const rapidBtn = document.getElementById('rapidSearchBtn');
    const rapidInput = document.getElementById('rapidSearchInput');
    if (rapidBtn && typeof searchProductPriceFn === 'function') {
      rapidBtn.addEventListener('click', searchProductPriceFn);
    }
    if (rapidInput && typeof searchProductPriceFn === 'function') {
      rapidInput.addEventListener('keypress', function (event) {
        if (event.key === 'Enter') searchProductPriceFn();
      });
    }

    if (typeof initFinishedShoppingFn === 'function') {
      await initFinishedShoppingFn();
    }
  } catch (error) {
    if (groceryRoot) groceryRoot.__rungGroceryInitialized = false;
    throw error;
  }
}

// ----------------------------------------------------------------------------
// TEST HOOKS (Node only — browser never touches these).
// ----------------------------------------------------------------------------
// `_groceryMockFetch` is set only by the smoke test harness via
// `SUT._setMockFetch(fn)`. It exists in module scope so fetchGrocery_
// finds it via the typeof fallback branch. The browser never reads
// or writes this variable.
let _groceryMockFetch;

// ----------------------------------------------------------------------------
// CONDITIONAL COMMONJS EXPORT — Node.js testability.
// ----------------------------------------------------------------------------
// The browser has no `module` global, so the typeof guard is false and
// this block is skipped at runtime in production. In Node (test runner,
// smoke tests), it exposes the helpers as a requireable object so the
// API contract can be verified without spinning up a browser. See
// tests/test_grocery.js.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    refreshGrocery,
    getSelectedRecipeIds,
    setupGroceryInit,
    refreshActiveRecipesExpander,
    getCartSourcePresentation,
    _setMockFetch:   (fn) => { _groceryMockFetch = fn; },
    _resetMockFetch: () => { _groceryMockFetch = null; },
  };
}
