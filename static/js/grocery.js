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
 *   - setupGroceryInit(deps)      Wires the Generate button + per-item Delete.
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
    list.innerHTML = '<div class="empty">Could not load grocery list.</div>';
    return;
  }
  const data = resp.data || {};
  const items = data.items || [];
  if (items.length === 0) {
    list.innerHTML = '<div class="empty">No active grocery list yet — generate one to begin.</div>';
    return;
  }
  if (data.applied_tax_pct != null && metaEl) {
    metaEl.textContent = 'Tax rate applied: ' + data.applied_tax_pct + '%';
  }
  items.forEach(g => {
    const row = document.createElement('div');
    row.className = 'list-item';
    row.innerHTML = `
      <div class="li-title">${Number(g.quantity || 1)}× <strong>${escapeHtml_(g.item_name)}</strong></div>
      <div class="li-amount">$${(Number(g.estimated_price) || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
      <div class="li-meta">${escapeHtml_(g.store_name || '')}</div>
      <div class="li-meta">${escapeHtml_(g.location_context || '')}</div>
      <button class="btn is-danger" type="button" data-id="${g.id}">Delete</button>
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
    totalEl.textContent = 'Total (tax incl.): $' + Number(data.estimated_total_with_tax).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  } else if (data.estimated_subtotal != null && totalEl) {
    totalEl.textContent = 'Subtotal: $' + Number(data.estimated_subtotal).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
}

/**
 * Fetch /api/recipes/generate with the selected IDs, then render recipe
 * mini-cards inside the Active Recipes expander on the Grocery tab.
 *
 * Caps display at 14 recipes. When zero recipes are selected the
 * expander shows a helpful prompt linking to the Recipes tab.
 */
async function refreshActiveRecipesExpander() {
  const expander = document.getElementById('activeRecipesExpander');
  if (!expander) return;
  const grid = document.getElementById('activeRecipesGrid');
  const countEl = document.getElementById('activeRecipeCount');
  if (!grid) return;

  const ids = getSelectedRecipeIds();
  var max = 14;
  var cappedIds = ids.slice(0, max);
  var truncated = ids.length > max;

  if (countEl) {
    countEl.textContent = truncated
      ? String(cappedIds.length) + ' of ' + String(ids.length) + ' Selected'
      : String(cappedIds.length) + ' Selected';
  }

  if (cappedIds.length === 0) {
    grid.innerHTML = '<div class="empty">No recipes selected for this pay period yet. Go to the Recipes tab to pick your meals!</div>';
    return;
  }

  // Fetch recipe details including ingredients
  var resp = await fetchGrocery_('POST', '/api/recipes/generate', { recipe_ids: cappedIds });
  if (!resp.ok) {
    grid.innerHTML = '<div class="empty">Could not load selected recipes.</div>';
    return;
  }
  var data = resp.data || {};
  var recipes = data.recipes || [];

  if (recipes.length === 0) {
    grid.innerHTML = '<div class="empty">No recipes matched the selected IDs.</div>';
    return;
  }

  grid.innerHTML = '';
  recipes.forEach(function (r) {
    var card = document.createElement('div');
    card.className = 'recipe-mini-card';
    var ings = (r.ingredients || []);
    var ingHtml = ings.map(function (i) {
      return '<li>' + (i.quantity || 1) + ' ' + escapeHtml_(i.unit || 'oz') + ' \u2014 <strong>' + escapeHtml_(i.product_name || '') + '</strong></li>';
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
 * Wire the Generate-from-selected-recipes button. Call this once
 * from the inline body's init() AFTER the DOM is ready.
 *
 * @param {object} deps
 * @param {Function} deps.flash            toast notification (msg, kind)
 * @param {Function} deps.refreshGrocery   grocery-list refresh (defaults to module's refreshGrocery)
 */
function setupGroceryInit(deps) {
  deps = deps || {};
  // Bind module-scoped placeholder used by the click handlers above.
  // Inline-script globals fall through when deps don't provide them;
  // typeof guards keep the read safe in Node tests where `flash` is
  // undeclared (would otherwise throw ReferenceError).
  flash_ = (deps && typeof deps.flash === 'function') ? deps.flash
         : (typeof flash === 'function' ? flash : null);
  const refreshFn = deps.refreshGrocery || refreshGrocery;
  const flashFn   = deps.flash || flash;

  const btn = document.getElementById('generateGroceryBtn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const ids = getSelectedRecipeIds();
    if (ids.length === 0) {
      if (flashFn) flashFn('Select at least one recipe in the Recipes tab first', 'error');
      return;
    }
    const resp = // Phase 5: 'Generate Recipes' button -> /api/grocery/generate-pay-period-plan
    await fetchGrocery_('POST', '/api/grocery/generate-pay-period-plan', {
      recipe_ids: ids,
      store_name: (document.getElementById('storeSel') || {}).value,
      budget_limit: parseFloat((document.getElementById('budgetInput') || {}).value) || null,
    });
    if (!resp.ok) {
      const errMsg = (resp.data && (resp.data.error || resp.data.message)) || ('Generation failed (' + resp.status + ')');
      if (flashFn) flashFn(errMsg, 'error');
      return;
    }
    const data = resp.data || {};
    const tax = data.applied_tax_pct != null ? ' · Tax: ' + data.applied_tax_pct + '%' : '';
    if (flashFn) flashFn('Generated ' + ((data && data.recipes && data.recipes.length) || 0) + ' recipes for the pay period', 'success');
    await refreshFn();
  });
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
    _setMockFetch:   (fn) => { _groceryMockFetch = fn; },
    _resetMockFetch: () => { _groceryMockFetch = null; },
  };
}
